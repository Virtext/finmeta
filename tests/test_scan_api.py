"""SPEC_S10 §E: the scan run action, the findings API and the fix endpoint.

Offline: a fake Jellyfin client is injected, so nothing here talks to a
server or a provider.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

from finmeta.api import AppContext, create_app
from finmeta.api.routes_scans import MAX_FIX_ITEMS
from finmeta.engine import scan_store
from finmeta.engine.scan_store import Finding

from _scan_helpers import FakeJellyfin, movie


CONFIG_TEMPLATE = """\
dry_run: {dry_run}
server:
  url: http://fake:8096
  api_key_env: JELLYFIN_API_KEY
path_mappings:
  - jellyfin: /media
    local: {media}
libraries:
  Movies:
    type: movies
    enabled: true
    metadata:
      enabled: false
    artwork:
      poster: true
    theme_song: false
    overlays: []
"""


class RecordingJellyfin(FakeJellyfin):
    """FakeJellyfin that also records refresh_item calls and closes cleanly."""

    def __init__(self, items, **kw):
        super().__init__(items, **kw)
        self.refreshed: List[Dict[str, Any]] = []
        self.closed = 0

    def refresh_item(self, item_id: str, **kw: Any) -> None:
        self.refreshed.append({"item_id": item_id, **kw})

    def close(self) -> None:
        self.closed += 1


@pytest.fixture
def media(tmp_path) -> Path:
    d = tmp_path / "media"
    d.mkdir()
    return d


@pytest.fixture
def ctx_factory(tmp_path, media, monkeypatch):
    monkeypatch.setenv("JELLYFIN_API_KEY", "fake-key")
    monkeypatch.setenv("FINMETA_AUTH", "off")

    def _make(*, dry_run: bool = True, items=None, exists: bool = True,
              extra_yaml: str = ""):
        cfg_dir = tmp_path / f"config-{int(dry_run)}-{exists}-{len(extra_yaml)}"
        cfg_dir.mkdir(exist_ok=True)
        (cfg_dir / "config.yml").write_text(
            CONFIG_TEMPLATE.format(dry_run=str(dry_run).lower(), media=media)
            + extra_yaml)
        ctx = AppContext(
            str(cfg_dir / "config.yml"), config_dir=str(cfg_dir),
            db_path=str(cfg_dir / "jobs.db"),
            overrides_path=str(cfg_dir / "overrides.db"),
        )
        jf = RecordingJellyfin(items if items is not None else [], exists=exists)
        ctx.make_jellyfin = lambda cfg=None: jf      # type: ignore[assignment]
        ctx._fake_jf = jf                            # type: ignore[attr-defined]
        return ctx

    return _make


def _client(ctx) -> TestClient:
    return TestClient(create_app(ctx))


def _wait_for_run(ctx, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while ctx.active_run is not None and time.monotonic() < deadline:
        time.sleep(0.02)
    assert ctx.active_run is None, "scan run never released the slot"


def _seed(ctx, findings, *, scan_id="scan-1", library="Movies"):
    scan_store.start_scan(ctx.db_path, scan_id, library=library, tier=1)
    scan_store.add_findings(ctx.db_path, scan_id, findings)
    scan_store.finish_scan(ctx.db_path, scan_id, "ok")
    rows, _ = scan_store.list_findings(ctx.db_path, scan_id)
    return scan_id, rows


def _f(code, severity="error", *, item="item-1", fix="apply_item", name="A Movie"):
    return Finding(library="Movies", check_code=code, severity=severity,
                   item_id=item, item_name=name, media_type="movie", fix_kind=fix)


# ---------------------------------------------------------------------------
# starting a scan
# ---------------------------------------------------------------------------

def test_scan_runs_end_to_end_and_stores_findings(ctx_factory):
    ctx = ctx_factory(items=[movie("1", "Alpha"), movie("2", "Beta")])
    with _client(ctx) as client:
        r = client.post("/api/runs", json={"action": "scan"})
        assert r.status_code == 202
        job_id = r.json()["job_id"]
        _wait_for_run(ctx)

        job = client.get(f"/api/jobs/{job_id}").json()
        assert job["status"] == "ok"
        assert any("scan Movies: 2 items" in e["message"] for e in job["log"])

        latest = client.get("/api/scans/latest").json()
        assert latest["scan"]["scan_id"] == job_id      # scan_id IS the run id
        assert latest["scan"]["items_scanned"] == 2
        assert latest["scan"]["tier"] == 1
        assert latest["counts"]["by_check"]["artwork.missing.poster"] == 2

        findings = client.get(f"/api/scans/{job_id}/findings",
                              params={"check": "artwork"}).json()
        assert findings["total"] == 2
        assert {f["item_name"] for f in findings["findings"]} == {"Alpha", "Beta"}
        assert all(f["fix_kind"] == "apply_item" for f in findings["findings"])
        # the fake items carry complete metadata and healthy streams, so the
        # only findings are the artwork ones -- and SC2's match checks need
        # tier 2 even for items that would fail them
        assert latest["counts"]["by_group"] == {"artwork": 2}
        assert "metadata.year_mismatch" not in latest["counts"]["by_check"]
    assert ctx._fake_jf.closed >= 1                     # the client is not leaked


def test_scan_is_read_only_so_dry_run_does_not_block_it(ctx_factory):
    ctx = ctx_factory(dry_run=True, items=[movie("1", "Alpha")])
    with _client(ctx) as client:
        assert client.post("/api/runs", json={"action": "scan"}).status_code == 202
        _wait_for_run(ctx)
        assert client.get("/api/scans/latest").json()["scan"]["status"] == "ok"


def test_scan_takes_the_single_run_slot(ctx_factory):
    ctx = ctx_factory(items=[movie("1", "Alpha")])
    with _client(ctx) as client:
        assert ctx.claim_run("blocking-run", action="apply")
        r = client.post("/api/runs", json={"action": "scan"})
        assert r.status_code == 409
        assert "already in progress" in r.json()["detail"]
        ctx.release_run("blocking-run")


def test_scan_library_scope_and_unknown_library(ctx_factory):
    ctx = ctx_factory(items=[movie("1", "Alpha")])
    with _client(ctx) as client:
        assert client.post("/api/runs",
                           json={"action": "scan", "library": "Movies"}).status_code == 202
        _wait_for_run(ctx)
        assert client.get("/api/scans/latest").json()["scan"]["library"] == "Movies"

        r = client.post("/api/runs", json={"action": "scan", "library": "Nope"})
        assert r.status_code == 202          # accepted, then reported in the log
        _wait_for_run(ctx)
        scan_id = client.get("/api/scans/latest").json()["scan"]["scan_id"]
        assert client.get(f"/api/scans/{scan_id}").json()["scan"]["status"] == "error"
        msgs = [e["message"] for e in client.get(f"/api/jobs/{scan_id}").json()["log"]]
        assert any("unknown library" in m for m in msgs)


def test_library_missing_in_jellyfin_is_logged_not_fatal(ctx_factory):
    ctx = ctx_factory(items=[movie("1", "Alpha")], exists=False)
    with _client(ctx) as client:
        client.post("/api/runs", json={"action": "scan"})
        _wait_for_run(ctx)
        scan = client.get("/api/scans/latest").json()["scan"]
        assert (scan["status"], scan["items_scanned"]) == ("ok", 0)
        msgs = [e["message"] for e in client.get(f"/api/jobs/{scan['scan_id']}").json()["log"]]
        assert any("not found in Jellyfin" in m for m in msgs)


def test_invalid_action_and_tier(ctx_factory):
    ctx = ctx_factory()
    with _client(ctx) as client:
        r = client.post("/api/runs", json={"action": "bogus"})
        assert r.status_code == 422 and "scan" in r.json()["detail"]
        assert client.post("/api/runs",
                           json={"action": "scan", "tier": 3}).status_code == 422


# ---------------------------------------------------------------------------
# reading findings
# ---------------------------------------------------------------------------

def test_findings_filters_paging_and_404s(ctx_factory):
    ctx = ctx_factory()
    scan_id, _ = _seed(ctx, [
        _f("artwork.missing.poster", "error", item=f"i{i}", name=f"M{i:02d}")
        for i in range(4)
    ] + [
        _f("duplicates.multi_version", "info", item="d1", name="Dup", fix="none"),
    ])
    with _client(ctx) as client:
        assert client.get("/api/scans").json()["scans"][0]["scan_id"] == scan_id
        body = client.get(f"/api/scans/{scan_id}/findings",
                          params={"check": "artwork", "limit": 2}).json()
        assert (body["total"], body["has_more"]) == (4, True)
        assert [f["item_name"] for f in body["findings"]] == ["M00", "M01"]
        page2 = client.get(f"/api/scans/{scan_id}/findings",
                           params={"check": "artwork", "limit": 2, "offset": 2}).json()
        assert [f["item_name"] for f in page2["findings"]] == ["M02", "M03"]
        assert page2["has_more"] is False
        assert client.get(f"/api/scans/{scan_id}/findings",
                          params={"severity": "info"}).json()["total"] == 1
        assert client.get(f"/api/scans/{scan_id}/findings",
                          params={"severity": "nope"}).status_code == 422
        assert client.get("/api/scans/nope/findings").status_code == 404
        assert client.get("/api/scans/nope").status_code == 404


def test_latest_is_empty_before_any_scan(ctx_factory):
    ctx = ctx_factory()
    with _client(ctx) as client:
        body = client.get("/api/scans/latest").json()
        assert body["scan"] is None and body["counts"]["by_severity"] == {}
        assert client.get("/api/scans").json()["scans"] == []


# ---------------------------------------------------------------------------
# fixing
# ---------------------------------------------------------------------------

def test_apply_fix_is_refused_while_dry_run(ctx_factory):
    ctx = ctx_factory(dry_run=True)
    scan_id, rows = _seed(ctx, [_f("artwork.missing.poster")])
    with _client(ctx) as client:
        r = client.post(f"/api/scans/{scan_id}/fix",
                        json={"finding_ids": [rows[0]["id"]]})
        assert r.status_code == 409
        assert "dry_run" in r.json()["detail"]
    # nothing was stamped: the whole request was refused
    rows, _ = scan_store.list_findings(ctx.db_path, scan_id)
    assert rows[0]["fixed_at"] is None


def test_apply_fix_starts_one_item_run_and_queues_the_rest(ctx_factory):
    ctx = ctx_factory(dry_run=False)
    scan_id, rows = _seed(ctx, [
        _f("artwork.missing.poster", item="item-1", name="A"),
        _f("artwork.missing.logo", "warning", item="item-1", name="A"),
        _f("artwork.missing.poster", item="item-2", name="B"),
    ])
    with _client(ctx) as client:
        # hold the slot so the submit path is deterministic: everything queues
        assert ctx.claim_run("blocking-run", action="apply")
        r = client.post(f"/api/scans/{scan_id}/fix",
                        json={"finding_ids": [x["id"] for x in rows]})
        assert r.status_code == 200
        body = r.json()
        # two findings on item-1 collapse into ONE apply for that item
        assert [s["item_id"] for s in body["submitted"]] == ["item-1", "item-2"]
        assert all(s["queued"] for s in body["submitted"])
        assert body["not_fixable"] == []
        ctx.release_run("blocking-run")
    # every finding that was part of a submitted fix is stamped
    rows, _ = scan_store.list_findings(ctx.db_path, scan_id)
    assert all(r["fixed_at"] for r in rows)


def test_candidate_picker_fix_reshuffles_then_applies_once(ctx_factory):
    """2026-11-10: operator-requested "try to upgrade low quality artwork" --
    candidate_picker findings now reshuffle the flagged kind(s) and fold
    into the SAME apply as any apply_item finding on that item, instead of
    only offering a manual pick."""
    ctx = ctx_factory(dry_run=False)
    scan_id, rows = _seed(ctx, [
        Finding(library="Movies", check_code="artwork_quality.low_resolution.poster",
                severity="warning", item_id="item-1", item_name="A",
                media_type="movie", fix_kind="candidate_picker",
                detail={"kind": "poster"}),
        Finding(library="Movies", check_code="artwork_quality.low_resolution.backdrop",
                severity="warning", item_id="item-1", item_name="A",
                media_type="movie", fix_kind="candidate_picker",
                detail={"kind": "backdrop"}),
        _f("artwork.missing.logo", "warning", item="item-1", name="A"),
    ])
    with _client(ctx) as client:
        assert ctx.claim_run("blocking-run", action="apply")   # deterministic: queues
        r = client.post(f"/api/scans/{scan_id}/fix",
                        json={"finding_ids": [x["id"] for x in rows]})
        assert r.status_code == 200
        body = r.json()
        # three findings on one item -> ONE apply, not three
        assert [s["item_id"] for s in body["submitted"]] == ["item-1"]
        ctx.release_run("blocking-run")
    assert ctx.overrides.reshuffle_counts("item-1") == {"poster": 1, "backdrop": 1}
    rows, _ = scan_store.list_findings(ctx.db_path, scan_id)
    assert all(r["fixed_at"] for r in rows)


def test_candidate_picker_fix_refused_while_dry_run(ctx_factory):
    ctx = ctx_factory(dry_run=True)
    scan_id, rows = _seed(ctx, [
        Finding(library="Movies", check_code="artwork_quality.low_resolution.poster",
                severity="warning", item_id="item-1", item_name="A",
                media_type="movie", fix_kind="candidate_picker",
                detail={"kind": "poster"}),
    ])
    with _client(ctx) as client:
        r = client.post(f"/api/scans/{scan_id}/fix",
                        json={"finding_ids": [rows[0]["id"]]})
        assert r.status_code == 409
    # refused before any reshuffle happened -- not half-applied
    assert ctx.overrides.reshuffle_counts("item-1") == {}


def test_refresh_fix_works_under_dry_run_and_calls_jellyfin(ctx_factory):
    ctx = ctx_factory(dry_run=True)
    scan_id, rows = _seed(ctx, [
        _f("artwork.stale_jellyfin.poster", item="item-9", fix="refresh_item"),
    ])
    with _client(ctx) as client:
        r = client.post(f"/api/scans/{scan_id}/fix",
                        json={"finding_ids": [rows[0]["id"]]})
        assert r.status_code == 200
        assert r.json()["submitted"] == [{"item_id": "item-9", "action": "refresh_item"}]
    call = ctx._fake_jf.refreshed[0]
    assert call["item_id"] == "item-9"
    # ask Jellyfin to re-read what is on disk -- never to replace it
    assert call["replace_all_images"] is False
    assert call["replace_all_metadata"] is False


def test_non_actionable_findings_come_back_as_not_fixable(ctx_factory):
    ctx = ctx_factory(dry_run=False)
    scan_id, rows = _seed(ctx, [
        _f("duplicates.same_provider_id", "warning", fix="none"),
        _f("path_unmapped", "error", fix="external"),
    ])
    with _client(ctx) as client:
        r = client.post(f"/api/scans/{scan_id}/fix",
                        json={"finding_ids": [x["id"] for x in rows]})
        assert r.status_code == 200
        assert sorted(r.json()["not_fixable"]) == sorted(x["id"] for x in rows)
        assert r.json()["submitted"] == []


def test_fix_rejects_empty_unknown_scan_and_oversized_requests(ctx_factory):
    ctx = ctx_factory(dry_run=False)
    scan_id, rows = _seed(ctx, [
        _f("artwork.missing.poster", item=f"item-{i}", name=f"M{i:03d}")
        for i in range(MAX_FIX_ITEMS + 1)
    ])
    with _client(ctx) as client:
        assert client.post(f"/api/scans/{scan_id}/fix",
                           json={"finding_ids": []}).status_code == 422
        assert client.post("/api/scans/nope/fix",
                           json={"finding_ids": [1]}).status_code == 404
        r = client.post(f"/api/scans/{scan_id}/fix",
                        json={"finding_ids": [x["id"] for x in rows]})
        assert r.status_code == 422
        assert "narrow the selection" in r.json()["detail"]
        # unknown ids are reported, not fatal
        ok = client.post(f"/api/scans/{scan_id}/fix",
                         json={"finding_ids": [rows[0]["id"], 999999]})
        assert ok.status_code == 200
        assert ok.json()["unknown"] == [999999]


def test_delete_scan(ctx_factory):
    ctx = ctx_factory()
    scan_id, _ = _seed(ctx, [_f("artwork.missing.poster")])
    with _client(ctx) as client:
        assert client.delete(f"/api/scans/{scan_id}").status_code == 200
        assert client.get(f"/api/scans/{scan_id}").status_code == 404
        assert client.delete(f"/api/scans/{scan_id}").status_code == 404


def test_keep_scans_from_config_prunes_old_scans(ctx_factory):
    """Retention is config-driven (SPEC_S10 §D) and runs at the end of a scan."""
    ctx = ctx_factory(items=[movie("1", "Alpha")],
                      extra_yaml="scan:\n  keep_scans: 1\n")
    with _client(ctx) as client:
        first = client.post("/api/runs", json={"action": "scan"}).json()["job_id"]
        _wait_for_run(ctx)
        second = client.post("/api/runs", json={"action": "scan"}).json()["job_id"]
        _wait_for_run(ctx)
        scans = client.get("/api/scans").json()["scans"]
        assert [s["scan_id"] for s in scans] == [second]
        # the pruned scan's findings went with it
        assert client.get(f"/api/scans/{first}").status_code == 404
        log = client.get(f"/api/jobs/{second}").json()["log"]
        assert any("pruned 1 old scan" in e["message"] for e in log)


def test_severity_floor_from_config_is_applied(ctx_factory):
    """`severity_floor: error` drops the orphan/info findings before storage."""
    ctx = ctx_factory(
        items=[movie("1", "Alpha", media_source_count=2)],
        extra_yaml="scan:\n  severity_floor: error\n")
    with _client(ctx) as client:
        client.post("/api/runs", json={"action": "scan"})
        _wait_for_run(ctx)
        latest = client.get("/api/scans/latest").json()
        # the missing poster is an error and stays; multi_version (info) is gone
        assert latest["counts"]["by_check"] == {"artwork.missing.poster": 1}


def test_checks_off_in_config_skips_the_group(ctx_factory):
    ctx = ctx_factory(
        items=[movie("1", "Alpha"), movie("2", "Alpha copy")],
        extra_yaml="scan:\n  checks:\n    artwork: false\n")
    with _client(ctx) as client:
        client.post("/api/runs", json={"action": "scan"})
        _wait_for_run(ctx)
        counts = client.get("/api/scans/latest").json()["counts"]
        assert "artwork.missing.poster" not in counts["by_check"]


# ---------------------------------------------------------------------------
# tier 2
# ---------------------------------------------------------------------------

class FakeTmdb:
    """Provider-id matches cost nothing; details are the only call here."""

    def __init__(self):
        self.details_calls = []
        self.closed = 0

    def movie_details(self, tmdb_id, language=None):
        self.details_calls.append(tmdb_id)
        return {"release_date": "2021-10-22"}

    def tv_details(self, tmdb_id, language=None):
        self.details_calls.append(tmdb_id)
        return {"first_air_date": "2021-10-22"}

    def close(self):
        self.closed += 1


def test_a_deep_scan_runs_the_tier2_checks(ctx_factory):
    """tier=2 records itself on the scan row and reaches SC2's match checks."""
    ctx = ctx_factory(items=[movie("1", "Alpha")])
    # the item claims 1999, the fake TMDb says 2021 -> wrong title
    ctx._fake_jf.items[0].production_year = 1999
    tmdb = FakeTmdb()
    ctx.make_tmdb = lambda cfg=None: tmdb                 # type: ignore[assignment]
    with _client(ctx) as client:
        r = client.post("/api/runs", json={"action": "scan", "tier": 2})
        assert r.status_code == 202
        _wait_for_run(ctx)
        latest = client.get("/api/scans/latest").json()
        assert latest["scan"]["tier"] == 2
        assert latest["counts"]["by_check"]["metadata.year_mismatch"] == 1
    assert tmdb.details_calls == [1]          # movie("1") carries Tmdb id "1"
    assert tmdb.closed >= 1                   # the client is not leaked


def test_a_plain_scan_never_builds_a_tmdb_client(ctx_factory):
    ctx = ctx_factory(items=[movie("1", "Alpha")])
    called = []
    ctx.make_tmdb = lambda cfg=None: called.append(1) or FakeTmdb()  # type: ignore
    with _client(ctx) as client:
        client.post("/api/runs", json={"action": "scan"})
        _wait_for_run(ctx)
    assert called == []


def test_a_deep_scan_without_sonarr_configured_still_finishes(ctx_factory):
    ctx = ctx_factory(items=[movie("1", "Alpha")])
    ctx.make_tmdb = lambda cfg=None: FakeTmdb()           # type: ignore[assignment]
    with _client(ctx) as client:
        client.post("/api/runs", json={"action": "scan", "tier": 2})
        _wait_for_run(ctx)
        scan = client.get("/api/scans/latest").json()["scan"]
        assert scan["status"] == "ok"
