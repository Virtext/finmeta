"""SPEC_S10 §C: the scan findings store (schema, paging, filters, retention)."""
from __future__ import annotations

import pytest

from finmeta.engine.scan_store import (
    Finding, add_findings, delete_scan, findings_by_ids, finish_scan, get_scan,
    init_scan_db, latest_scan, list_findings, list_scans, mark_fixed,
    prune_scans, scan_counts, start_scan, bump_items_scanned,
)


def _f(code: str, severity: str = "warning", *, item="i1", name="A Movie",
       library="Movies", fix="apply_item", detail=None) -> Finding:
    return Finding(
        library=library, check_code=code, severity=severity, item_id=item,
        item_name=name, media_type="movie", detail=detail or {}, fix_kind=fix,
    )


def test_finding_rejects_unknown_severity_and_fix_kind():
    with pytest.raises(ValueError):
        Finding(library="Movies", check_code="artwork.missing.poster", severity="critical")
    with pytest.raises(ValueError):
        Finding(library="Movies", check_code="artwork.missing.poster",
                severity="error", fix_kind="delete_everything")


def test_finding_group_is_the_part_before_the_first_dot():
    assert _f("artwork.missing.poster").group == "artwork"
    assert _f("path_unmapped", item=None).group == "path_unmapped"


def test_scan_lifecycle_counts_and_latest(tmp_path):
    db = tmp_path / "jobs.db"
    start_scan(db, "scan-1", library="Movies", tier=1)
    assert get_scan(db, "scan-1")["status"] == "running"
    n = add_findings(db, "scan-1", [
        _f("artwork.missing.poster", "error"),
        _f("artwork.missing.logo", "warning", item="i2", name="B Movie"),
        _f("duplicates.multi_version", "info", item="i3", name="C Movie", fix="none"),
    ])
    assert n == 3
    bump_items_scanned(db, "scan-1", 42)
    finish_scan(db, "scan-1", "ok")

    row = get_scan(db, "scan-1")
    assert (row["status"], row["finding_count"], row["items_scanned"]) == ("ok", 3, 42)
    assert row["finished_at"] and row["library"] == "Movies"

    counts = scan_counts(db, "scan-1")
    assert counts["by_severity"] == {"error": 1, "warning": 1, "info": 1}
    assert counts["by_group"] == {"artwork": 2, "duplicates": 1}
    assert counts["by_check"]["artwork.missing.poster"] == 1

    assert latest_scan(db)["scan_id"] == "scan-1"
    assert [s["scan_id"] for s in list_scans(db)] == ["scan-1"]


def test_reads_work_before_any_scan_ran(tmp_path):
    """A GET that lands before the first scan must answer, not 500."""
    db = tmp_path / "jobs.db"
    assert list_scans(db) == []
    assert latest_scan(db) is None
    assert get_scan(db, "nope") is None
    assert list_findings(db, "nope") == ([], 0)
    assert scan_counts(db, "nope") == {"by_severity": {}, "by_group": {}, "by_check": {}}


def test_detail_round_trips_as_an_object_and_is_redacted(tmp_path):
    db = tmp_path / "jobs.db"
    start_scan(db, "s", tier=1)
    add_findings(db, "s", [
        _f("artwork.missing.poster", "error",
           detail={"kinds": ["poster"], "path": "/media/Movies/X/secret-token"}),
    ], redact=lambda t: t.replace("secret-token", "***"))
    rows, total = list_findings(db, "s")
    assert total == 1
    assert rows[0]["detail"]["kinds"] == ["poster"]
    assert rows[0]["detail"]["path"].endswith("***")
    assert rows[0]["group"] == "artwork"


def test_findings_ordered_error_first_then_code_then_name(tmp_path):
    db = tmp_path / "jobs.db"
    start_scan(db, "s", tier=1)
    add_findings(db, "s", [
        _f("duplicates.same_folder", "info", item="i9", name="Zed"),
        _f("artwork.missing.poster", "error", item="i2", name="Beta"),
        _f("artwork.missing.poster", "error", item="i1", name="Alpha"),
        _f("media.no_stream", "warning", item="i3", name="Gamma"),
    ])
    rows, total = list_findings(db, "s")
    assert total == 4
    assert [r["item_name"] for r in rows] == ["Alpha", "Beta", "Gamma", "Zed"]


def test_findings_filters_and_paging(tmp_path):
    db = tmp_path / "jobs.db"
    start_scan(db, "s", tier=1)
    add_findings(db, "s", [
        _f("artwork.missing.poster", "error", item=f"i{i}", name=f"M{i:02d}")
        for i in range(5)
    ] + [
        _f("artwork.stale_jellyfin.poster", "error", item="t1", name="T1"),
        _f("duplicates.same_folder", "warning", item="d1", name="D1", library="TV"),
    ])

    # whole group, both codes
    rows, total = list_findings(db, "s", check="artwork")
    assert total == 6
    # one exact code
    rows, total = list_findings(db, "s", check="artwork.missing.poster")
    assert total == 5
    # by library and severity
    assert list_findings(db, "s", library="TV")[1] == 1
    assert list_findings(db, "s", severity="warning")[1] == 1
    # paging is stable and non-overlapping
    page1, total = list_findings(db, "s", check="artwork.missing.poster", limit=2, offset=0)
    page2, _ = list_findings(db, "s", check="artwork.missing.poster", limit=2, offset=2)
    assert total == 5
    assert [r["item_name"] for r in page1] == ["M00", "M01"]
    assert [r["item_name"] for r in page2] == ["M02", "M03"]


def test_findings_by_ids_and_mark_fixed(tmp_path):
    db = tmp_path / "jobs.db"
    start_scan(db, "s", tier=1)
    add_findings(db, "s", [
        _f("artwork.missing.poster", "error", item="i1", name="A"),
        _f("artwork.missing.logo", "warning", item="i2", name="B"),
    ])
    rows, _ = list_findings(db, "s")
    ids = [r["id"] for r in rows]
    assert findings_by_ids(db, "s", []) == []
    picked = findings_by_ids(db, "s", [ids[1], ids[0]])
    assert [p["id"] for p in picked] == [ids[1], ids[0]]       # given order
    assert findings_by_ids(db, "s", [9999]) == []              # unknown ids drop out
    mark_fixed(db, [ids[0]])
    rows, _ = list_findings(db, "s")
    by_id = {r["id"]: r for r in rows}
    assert by_id[ids[0]]["fixed_at"] and by_id[ids[1]]["fixed_at"] is None


def test_prune_keeps_newest_and_deletes_their_findings(tmp_path):
    db = tmp_path / "jobs.db"
    init_scan_db(db)
    for i in range(4):
        sid = f"scan-{i}"
        start_scan(db, sid, tier=1)
        add_findings(db, sid, [_f("artwork.missing.poster", "error")])
        finish_scan(db, sid, "ok")
        # started_at has second resolution; force a deterministic order
        import sqlite3
        with sqlite3.connect(str(db)) as c:
            c.execute("UPDATE scans SET started_at = ? WHERE scan_id = ?",
                      (f"2026-11-0{i+1}T00:00:00Z", sid))

    assert prune_scans(db, keep=2) == 2
    assert [s["scan_id"] for s in list_scans(db)] == ["scan-3", "scan-2"]
    # the pruned scans' findings are gone, the kept ones' are not
    assert list_findings(db, "scan-0") == ([], 0)
    assert list_findings(db, "scan-3")[1] == 1
    assert prune_scans(db, keep=0) == 0          # 0 = disabled, keeps everything
    assert len(list_scans(db)) == 2


def test_prune_never_touches_a_running_scan(tmp_path):
    db = tmp_path / "jobs.db"
    for i in range(3):
        start_scan(db, f"done-{i}", tier=1)
        finish_scan(db, f"done-{i}", "ok")
    start_scan(db, "live", tier=1)
    prune_scans(db, keep=1)
    assert get_scan(db, "live") is not None


def test_delete_scan(tmp_path):
    db = tmp_path / "jobs.db"
    start_scan(db, "s", tier=1)
    add_findings(db, "s", [_f("artwork.missing.poster", "error")])
    assert delete_scan(db, "s") is True
    assert get_scan(db, "s") is None
    assert list_findings(db, "s") == ([], 0)
    assert delete_scan(db, "s") is False
