"""SPEC_S10 §I: one tier-1 scan against the Jellyfin test server (``-m live``).

Read-only by construction: this exercises the scan pass only, never the fix
endpoint. The test asserts that nothing in the fixture tree was touched --
a "health check" that silently wrote files would be the worst possible bug
in this feature.

Run with:  pytest -m live tests/test_scan_live.py
Requires:  the test server bootstrapped (scripts/jf_bootstrap.py) and
           fixture/library generated (fixture/generate.py).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Tuple

import pytest

from finmeta.engine.scanner import SCAN_ITEM_FIELDS, scan_library
from finmeta.jellyfin_client import JellyfinClient, PathMapper
from finmeta.config.models import MovieLibrary

ROOT = Path(__file__).resolve().parents[1]
CREDS_PATH = ROOT / ".jf-test" / "creds.json"
FIXTURE = ROOT / "fixture" / "library"
MAPPINGS = [{"jellyfin": "/media", "local": str(FIXTURE)}]


def _creds() -> dict:
    if not CREDS_PATH.exists():
        pytest.skip("creds.json not found; run scripts/jf_bootstrap.py first")
    return json.loads(CREDS_PATH.read_text())


@pytest.fixture(scope="module")
def client() -> JellyfinClient:
    c = _creds()
    jf = JellyfinClient(c["url"], token=c["api_key"])
    users = jf._request("GET", "/Users") or []
    user_id = next((u["Id"] for u in users
                    if u.get("Name") == c.get("username", "finmeta")), None)
    if not user_id:
        pytest.skip(f"user {c.get('username')!r} not found on server")
    jf.user_id = user_id
    yield jf
    jf.close()


def _tree_state() -> Dict[str, Tuple[int, float]]:
    """Every file under the fixture tree with its size and mtime."""
    if not FIXTURE.is_dir():
        pytest.skip("fixture/library not generated; run fixture/generate.py")
    return {str(p): (p.stat().st_size, p.stat().st_mtime)
            for p in FIXTURE.rglob("*") if p.is_file()}


def _scan(client, library: str, kinds: Dict[str, bool]) -> Tuple[object, List]:
    lib_cfg = MovieLibrary(type="movies", enabled=True, artwork=kinds)
    batches: List[list] = []
    res = scan_library(
        client, library=library, lib_cfg=lib_cfg,
        mapper=PathMapper.from_config(MAPPINGS), emit=batches.append, page_size=5,
    )
    return res, [f for batch in batches for f in batch]


@pytest.mark.live
def test_a_managed_applied_library_has_no_false_positives(client):
    """Movies has been applied, so all three artwork kinds exist on disk AND
    in Jellyfin: no artwork/duplicate/path/media findings at all.

    The one thing it may legitimately report is
    ``artwork_quality.unreadable.logo``: this run found two real ``logo.png``
    files in the fixture tree whose first bytes are ``<?xml version="1`` --
    TMDb SVG logos written out as .png, a genuine apply-path bug the scanner
    surfaced (fixed in ``sources/tmdb.py`` the same day; the files stay broken
    until something re-applies them, which is exactly what the finding's
    ``apply_item`` fix does). Asserted as "only that code, if anything" so the
    test stays true both before and after those files get rewritten."""
    before = _tree_state()
    res, findings = _scan(client, "Movies",
                          {"poster": True, "backdrop": True, "logo": True})
    assert res.skipped_reason is None
    assert res.items_scanned == 10        # fixture Movies library
    unexpected = [f.check_code for f in findings
                  if f.check_code != "artwork_quality.unreadable.logo"]
    assert unexpected == []
    for f in findings:
        assert f.fix_kind == "apply_item"
        assert f.detail["file"].endswith("logo.png")
    assert _tree_state() == before, "a scan must never touch the media tree"


@pytest.mark.live
def test_the_untouched_library_reports_orphans_on_every_kind(client):
    """``Untouched`` holds video files only -- no sidecars Finmeta wrote --
    while Jellyfin's own providers did fetch images for it. That is exactly
    the orphan_jellyfin case, here confirmed against a real server."""
    before = _tree_state()
    res, findings = _scan(client, "Untouched",
                          {"poster": True, "backdrop": True, "logo": True})
    assert res.items_scanned == 2
    assert len(findings) == 6
    assert {f.check_code for f in findings} == {
        "artwork.orphan_jellyfin.poster",
        "artwork.orphan_jellyfin.backdrop",
        "artwork.orphan_jellyfin.logo",
    }
    for f in findings:
        assert f.library == "Untouched"
        assert f.severity == "info"
        assert f.fix_kind == "apply_item"
        assert f.item_id and f.item_name
        assert f.detail["file"].startswith(str(FIXTURE))
        assert f.detail["file_present"] is False
        assert f.detail["jellyfin_image"] is True
    assert _tree_state() == before, "a scan must never touch the media tree"


@pytest.mark.live
def test_a_kind_the_library_does_not_have_on_disk(client):
    """Movies has no landscape.jpg, but Jellyfin reports a Thumb for every
    item -- so enabling `landscape` turns all ten into orphan findings."""
    res, findings = _scan(client, "Movies", {"landscape": True})
    assert res.items_scanned == 10
    assert [f.check_code for f in findings] == [
        "artwork.orphan_jellyfin.landscape"] * 10


@pytest.mark.live
def test_a_disabled_library_is_never_even_fetched(client):
    lib_cfg = MovieLibrary(type="movies", enabled=False, artwork={"poster": True})
    batches: List[list] = []
    res = scan_library(client, library="Movies", lib_cfg=lib_cfg,
                       mapper=PathMapper.from_config(MAPPINGS),
                       emit=batches.append)
    assert res.skipped_reason == "library disabled"
    assert (res.items_scanned, batches) == (0, [])


@pytest.mark.live
def test_scan_pass_asks_jellyfin_only_for_what_it_needs(client):
    """The field list must be valid for the real server (not just a fake).

    ImageTags/BackdropImageTags are NOT ItemFields values -- asking for them
    would 400 -- so this pins that the scan's own field list is accepted and
    still returns the image tags the artwork check reads.
    """
    lib = client.get_library("Movies")
    assert lib is not None
    page = client.get_items_page(
        parent_id=lib.item_id, recursive=True,
        include_item_types=["Movie", "Series"], fields=SCAN_ITEM_FIELDS,
        limit=1, start_index=0, sort_by=["SortName"],
    )
    assert page.total_record_count > 0
    item = page.items[0]
    assert item.path, "the scan needs Path to find artwork on disk"
    assert isinstance(item.image_tags, dict)
