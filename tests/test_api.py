"""Offline API tests using FastAPI TestClient with injected fakes.

Design: function-scoped tmp config dir; fake plan_builder and
candidates_provider so no live server or network is needed.
Metadata-only FileWrites keep the apply path simple.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pytest
import yaml

from finmeta.api import AppContext, create_app
from finmeta.api.proxy import encode_token, decode_token
from finmeta.config.models import Config
from finmeta.engine.plan import FileWrite, ItemPlan, WritePlan
from finmeta.sources.candidate import Candidate
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

CONFIG_YAML = """\
dry_run: true
server:
  url: http://fake:8096
  api_key_env: JELLYFIN_API_KEY
path_mappings:
  - jellyfin: /media
    local: /tmp/fake-media
libraries:
  Movies:
    type: movies
    metadata:
      enabled: true
    artwork:
      poster: true
      backdrop: true
    theme_song: false
    overlays: []
"""


def _fake_plan(cfg, jf, tmdb, sources, library=None, item_id=None, config_dir=None, **kwargs):
    """Return a minimal WritePlan with one item and one metadata file."""
    fw = FileWrite(
        target_path="/tmp/fake-media/Movie1.nfo",
        kind="metadata",
        source_ref="generated",
        content_sha256="a" * 64,
        status="create",
    )
    item = ItemPlan(
        item_id="item-1",
        item_name="Movie 1",
        media_type="movie",
        tmdb_id=42,
        matched_by="provider_id",
        files=[fw],
    )
    return WritePlan(items=[item])


def _fake_candidates(item, cfg, jf, tmdb, sources, mapper):
    """Return a fixed set of candidates for the fake item."""
    by_kind = {
        "poster": [
            Candidate(kind="poster", source="tmdb",
                      ref="/tmp/fake-poster.jpg", lang="en",
                      width=1000, height=1500, score=0.9),
            Candidate(kind="poster", source="tmdb",
                      ref="/tmp/fake-media/fake-poster2.jpg", lang=None,
                      width=500, height=750, score=0.7),
        ],
    }
    return by_kind, 42


@pytest.fixture
def tmp_config(tmp_path, monkeypatch):
    """Create a tmp config dir with a valid config.yml and set env vars."""
    monkeypatch.setenv("JELLYFIN_API_KEY", "fake-key")
    monkeypatch.setenv("TMDB_KEY", "fake-tmdb-key")
    monkeypatch.setenv("FINMETA_AUTH", "off")  # S9a: default tests bypass auth
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    cfg_file = cfg_dir / "config.yml"
    # `local-assets` is this fixture's legitimate local artwork root: the proxy
    # refuses local refs outside the configured asset/media roots since
    # 2026-10-22 (docs/SECURITY_REVIEW.md "H1"), so proxy tests put their
    # files there -- the same place LocalAssetSource reads in production.
    (tmp_path / "local-assets").mkdir(exist_ok=True)
    cfg_file.write_text(
        CONFIG_YAML.rstrip("\n") + "\n"
        + "sources:\n  local:\n    enabled: true\n"
        + f"    path: {tmp_path / 'local-assets'}\n")
    return cfg_dir


@pytest.fixture
def context(tmp_config):
    """Build an AppContext with injected fakes."""
    ctx = AppContext(
        str(tmp_config / "config.yml"),
        config_dir=str(tmp_config),
        db_path=str(tmp_config / "jobs.db"),
        overrides_path=str(tmp_config / "overrides.db"),
        plan_builder=_fake_plan,
        candidates_provider=_fake_candidates,
    )
    return ctx


@pytest.fixture
def client(context):
    """TestClient with the app wired to our context."""
    app = create_app(context)
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# core routes
# ---------------------------------------------------------------------------

def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["version"] == "2.0.0"
    assert body["dry_run"] is True
    assert body["active_run"] is None
    assert body["active_run_scope"] is None


def test_health_reports_active_run_scope_for_page_reload_resume(client):
    """Job status lost on page reload: a reconnecting client needs to know
    not just THAT a run is active but which library/item it targets, so it
    can resume into the right panel."""
    ctx = client.app.state.context
    assert ctx.claim_run("resume-test-run", action="apply", library="Movies", item_id=None)
    try:
        r = client.get("/api/health")
        body = r.json()
        assert body["active_run"] == "resume-test-run"
        assert body["active_run_scope"] == {"action": "apply", "library": "Movies", "item_id": None}
    finally:
        ctx.release_run("resume-test-run")
    r2 = client.get("/api/health")
    assert r2.json()["active_run_scope"] is None


def test_libraries(client):
    r = client.get("/api/libraries")
    assert r.status_code == 200
    body = r.json()
    assert body["dry_run"] is True
    # `sample_size` was dropped from this payload with the sample endpoint
    # and SamplePreview.tsx (2026-10-22, operator request).
    assert "sample_size" not in body
    names = [lib["name"] for lib in body["libraries"]]
    assert "Movies" in names
    movies = next(lib for lib in body["libraries"] if lib["name"] == "Movies")
    assert movies["type"] == "movies"
    assert "poster" in movies["artwork_enabled"]
    assert "backdrop" in movies["artwork_enabled"]


def test_library_items(client):
    r = client.get("/api/libraries/Movies/items")
    assert r.status_code == 200
    body = r.json()
    assert body["library"] == "Movies"
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["item_id"] == "item-1"
    assert item["item_name"] == "Movie 1"
    assert item["tmdb_id"] == 42
    assert item["status"] == "changes"  # metadata file is "create"
    assert item["diff"]["create"] == 1


def test_library_items_unknown(client):
    r = client.get("/api/libraries/Nonexistent/items")
    assert r.status_code == 404


def test_library_items_defaults_to_a_bounded_limit(tmp_config):
    """Audit 2026-11-11 "C5": GET .../items with no `limit` query param
    used to reach plan_builder.build_plan as limit=None, which fetches
    the WHOLE library (jf.iter_items, no Jellyfin-side Limit) synchronously
    inside the request thread -- ~7s/item cold. It must now reach
    build_plan as a bounded int (matching /items/light's own default)
    even when the caller omits the param entirely."""
    seen_limit = {}

    def _spy_plan(cfg, jf, tmdb, sources, library=None, item_id=None,
                  config_dir=None, **kwargs):
        seen_limit["limit"] = kwargs.get("limit")
        return _fake_plan(cfg, jf, tmdb, sources, library=library,
                          item_id=item_id, config_dir=config_dir, **kwargs)

    ctx = AppContext(
        str(tmp_config / "config.yml"), config_dir=str(tmp_config),
        db_path=str(tmp_config / "jobs.db"),
        overrides_path=str(tmp_config / "overrides.db"),
        plan_builder=_spy_plan, candidates_provider=_fake_candidates,
    )
    with TestClient(create_app(ctx)) as c:
        r = c.get("/api/libraries/Movies/items")
    assert r.status_code == 200
    assert seen_limit["limit"] == 100
    assert seen_limit["limit"] is not None


def test_item_plan(client):
    r = client.get("/api/items/item-1/plan")
    assert r.status_code == 200
    body = r.json()
    assert body["item_id"] == "item-1"
    assert body["tmdb_id"] == 42
    assert body["status"] == "changes"
    assert len(body["files"]) == 1
    assert body["files"][0]["kind"] == "metadata"


def test_item_plan_not_found(client):
    r = client.get("/api/items/no-such-item/plan")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# item image proxy (SPEC_S14_UI.md P3, decision D8 -- the modern UI's
# poster grid needs a cheap per-item image, no TMDb matching)
# ---------------------------------------------------------------------------

def test_item_image_returns_bytes(client, context):
    from unittest.mock import MagicMock
    mock_jf = MagicMock()
    mock_jf.get_image_bytes.return_value = (b"\xff\xd8fake-jpeg", "image/jpeg")
    mock_jf.close.return_value = None
    context.make_jellyfin = lambda cfg=None: mock_jf

    r = client.get("/api/items/item-1/image?kind=poster")
    assert r.status_code == 200
    assert r.content == b"\xff\xd8fake-jpeg"
    assert r.headers["content-type"] == "image/jpeg"
    mock_jf.get_image_bytes.assert_called_once_with("item-1", "Primary", max_width=None)


def test_item_image_forwards_max_width(client, context):
    """2026-11-10: grid thumbnails ask Jellyfin to downscale server-side."""
    from unittest.mock import MagicMock
    mock_jf = MagicMock()
    mock_jf.get_image_bytes.return_value = (b"\xff\xd8fake-jpeg", "image/jpeg")
    mock_jf.close.return_value = None
    context.make_jellyfin = lambda cfg=None: mock_jf

    r = client.get("/api/items/item-1/image?kind=poster&max_width=320")
    assert r.status_code == 200
    mock_jf.get_image_bytes.assert_called_once_with("item-1", "Primary", max_width=320)


def test_item_image_404_when_jellyfin_has_none(client, context):
    from unittest.mock import MagicMock
    mock_jf = MagicMock()
    mock_jf.get_image_bytes.return_value = None
    mock_jf.close.return_value = None
    context.make_jellyfin = lambda cfg=None: mock_jf

    r = client.get("/api/items/item-1/image?kind=logo")
    assert r.status_code == 404


def test_item_image_unknown_kind_is_400(client):
    r = client.get("/api/items/item-1/image?kind=not-a-real-kind")
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# bulk run (SPEC_S14_UI.md P3 follow-up: Library page bulk selection, S13d)
# ---------------------------------------------------------------------------

def test_bulk_run_empty_items_422(client):
    r = client.post("/api/items/bulk-run", json={"action": "apply", "item_ids": []})
    assert r.status_code == 422


def test_bulk_run_too_many_items_422(client):
    ids = [f"item-{i}" for i in range(60)]
    r = client.post("/api/items/bulk-run", json={"action": "apply", "item_ids": ids})
    assert r.status_code == 422
    assert "too many items" in r.json()["detail"]


def test_bulk_run_invalid_action_422(client):
    r = client.post("/api/items/bulk-run", json={"action": "bogus", "item_ids": ["item-1"]})
    assert r.status_code == 422


def test_bulk_run_apply_refused_under_dry_run(client, context):
    assert context.config.dry_run is True
    r = client.post("/api/items/bulk-run", json={"action": "apply", "item_ids": ["item-1"]})
    assert r.status_code == 409
    assert "dry_run" in r.json()["detail"]


def test_bulk_run_apply_queues_when_slot_busy(client, context):
    """Every item queues on the SAME FIFO the webhook drains -- not a
    second, parallel queueing mechanism."""
    cfg2 = context.config.model_copy(update={"dry_run": False})
    context._cfg = cfg2
    assert context.claim_run("test-run-block")
    try:
        r = client.post("/api/items/bulk-run",
                        json={"action": "apply", "item_ids": ["item-1", "item-2"]})
        assert r.status_code == 202
        results = r.json()["results"]
        assert all(res["queued"] is True for res in results)
        assert all(res["run_id"] is None for res in results)
        assert set(context.peek_webhook_queue()) == {"item-1", "item-2"}
    finally:
        context.release_run("test-run-block")
        for i in ("item-1", "item-2"):
            context.drop_webhook_item(i)


def test_bulk_run_plan_does_not_use_the_apply_only_queue_when_busy(client, context):
    """A busy plan is reported, not silently turned into a queued apply --
    the webhook queue's drain always re-submits as "apply"."""
    assert context.claim_run("test-run-block")
    try:
        r = client.post("/api/items/bulk-run",
                        json={"action": "plan", "item_ids": ["item-1"]})
        assert r.status_code == 202
        result = r.json()["results"][0]
        assert result["queued"] is False
        assert result["run_id"] is None
        assert "already in progress" in result["error"]
        assert context.peek_webhook_queue() == []  # nothing silently queued
    finally:
        context.release_run("test-run-block")


# ---------------------------------------------------------------------------
# config routes
# ---------------------------------------------------------------------------

def test_config_get(client):
    r = client.get("/api/config")
    assert r.status_code == 200
    body = r.json()
    assert body["dry_run"] is True
    assert "yaml" in body
    assert "parsed" in body


def test_config_put_valid(client, context):
    new_yaml = CONFIG_YAML.replace("dry_run: true", "dry_run: false")
    r = client.put("/api/config", json={"yaml": new_yaml})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["dry_run"] is False
    # last-good was written (contains the PREVIOUS config for undo)
    lg = context.last_good_path()
    assert lg.is_file()
    assert "dry_run: true" in lg.read_text()  # the old config


def test_config_put_invalid(client):
    r = client.put("/api/config", json={"yaml": "not_a_dict: [1, 2, 3" })
    assert r.status_code == 400


def test_config_put_unknown_key(client):
    r = client.put("/api/config", json={"yaml": "bogus_key: 42\nserver:\n  url: http://x\n"})
    assert r.status_code == 400


def test_config_undo(client, context):
    # First, change config
    new_yaml = CONFIG_YAML.replace("dry_run: true", "dry_run: false")
    client.put("/api/config", json={"yaml": new_yaml})
    assert context.config.dry_run is False
    # Undo
    r = client.post("/api/config/undo")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert context.config.dry_run is True


def test_config_undo_no_last_good(client, context):
    # No prior put means no last-good file
    r = client.post("/api/config/undo")
    assert r.status_code == 404


def test_overlay_profile_elements_and_style_round_trip(client, context):
    """Regression guard, SPEC_S14_UI.md phase P5: a profile's FLAT `elements`
    list and its nested `style.elements` must BOTH survive a full config
    round trip. The 2026-10-19 bug was exactly this -- the Overlays tab's
    save button wrote only `{style: doc}` and silently discarded the
    profile's flat `elements` list the engine actually reads to decide which
    badges a real apply draws. Required to exist BEFORE any Overlay Studio
    work (the new UI shares this same PUT /api/config save path)."""
    new_yaml = CONFIG_YAML + """
overlays:
  profiles:
    my-profile:
      elements: ["imdb", "resolution"]
      style:
        elements:
          imdb:
            enabled: true
            x_offset: 7
"""
    r = client.put("/api/config", json={"yaml": new_yaml})
    assert r.status_code == 200, r.text

    got = client.get("/api/config").json()["parsed"]
    profile = got["overlays"]["profiles"]["my-profile"]
    assert profile["elements"] == ["imdb", "resolution"]
    assert profile["style"]["elements"]["imdb"]["x_offset"] == 7

    # And a second, unrelated config write (e.g. toggling dry_run, exactly
    # what the Studio's OWN save does for a different key) must not touch
    # this profile at all -- the failure mode was a save silently dropping
    # fields it didn't know to preserve.
    again_yaml = new_yaml.replace("dry_run: true", "dry_run: false")
    r2 = client.put("/api/config", json={"yaml": again_yaml})
    assert r2.status_code == 200, r2.text
    got2 = client.get("/api/config").json()["parsed"]
    profile2 = got2["overlays"]["profiles"]["my-profile"]
    assert profile2["elements"] == ["imdb", "resolution"]
    assert profile2["style"]["elements"]["imdb"]["x_offset"] == 7


# ---------------------------------------------------------------------------
# candidates routes
# ---------------------------------------------------------------------------

def test_candidates(client, context):
    # Mock the JellyfinClient so get_item doesn't hit a real server
    from unittest.mock import MagicMock
    mock_jf = MagicMock()
    mock_item = MagicMock()
    mock_item.id = "item-1"
    mock_item.type = "Movie"
    mock_jf.get_item.return_value = mock_item
    mock_jf.close.return_value = None
    context.make_jellyfin = lambda cfg=None: mock_jf
    # Also mock TmdbClient to avoid real connections
    mock_tmdb = MagicMock()
    mock_tmdb.close.return_value = None
    context.make_tmdb = lambda cfg=None: mock_tmdb
    # Mock sources/mapper
    context.make_sources = lambda cfg=None: {}
    context.make_mapper = lambda cfg=None: MagicMock()

    r = client.get("/api/items/item-1/candidates")
    assert r.status_code == 200
    body = r.json()
    assert body["item_id"] == "item-1"
    assert body["tmdb_id"] == 42
    cands = body["candidates"]
    assert len(cands) == 2
    assert cands[0]["kind"] == "poster"
    assert cands[0]["source"] == "tmdb"
    assert cands[0]["selected"] is True  # first is auto-selected
    assert cands[1]["selected"] is False
    # proxy_url is well-formed
    assert cands[0]["proxy_url"].startswith("/api/proxy/")


def test_select_candidate(client, context):
    # Select the second poster
    r = client.put("/api/items/item-1/candidates",
                   json={"kind": "poster", "ref": "/tmp/fake-media/fake-poster2.jpg", "source": "tmdb"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["selected"] == "/tmp/fake-media/fake-poster2.jpg"
    # Verify override was stored
    ovr = context.overrides.get("item-1", "poster")
    assert ovr is not None
    assert ovr["ref"] == "/tmp/fake-media/fake-poster2.jpg"


def test_select_candidate_invalidates_the_plan_cache(client, context):
    """Regression: selecting a candidate must change what GET .../plan
    returns right away, even when that item's plan was already cached from
    an earlier view (the normal case -- you open an item, THEN pick a
    candidate for it). Before this fix, select_candidate() never called
    ctx.invalidate_plans(), so AppContext.get_plan's (generation, scope)
    cache kept serving the pre-selection plan it already had cached under
    "item:item-1" -- reported live as "clicking poster/backdrop/logo
    candidates doesn't switch anything, even with dry_run off"."""
    # Warm the cache exactly like opening the item in the UI would -- the
    # fixture plan has no "poster" FileWrite at all until an override adds
    # one (engine.api.overrides.apply_overrides' "kind not in the base
    # plan" branch), so this doubles as the clearest possible before/after:
    # a file that plain did not exist in the cached response must appear
    # once a candidate for it is picked.
    r = client.get("/api/items/item-1/plan")
    assert r.status_code == 200
    assert not any(f["kind"] == "poster" for f in r.json()["files"])

    r = client.put("/api/items/item-1/candidates",
                   json={"kind": "poster", "ref": "/tmp/fake-media/fake-poster2.jpg", "source": "tmdb"})
    assert r.status_code == 200

    r = client.get("/api/items/item-1/plan")
    assert r.status_code == 200
    after = next(f for f in r.json()["files"] if f["kind"] == "poster")
    assert after["source_ref"] == "/tmp/fake-media/fake-poster2.jpg"


def test_clear_candidate(client, context):
    # Set then clear
    client.put("/api/items/item-1/candidates",
               json={"kind": "poster", "ref": "/tmp/fake-media/fake-poster2.jpg"})
    assert context.overrides.get("item-1", "poster") is not None
    r = client.put("/api/items/item-1/candidates",
                   json={"kind": "poster", "ref": None})
    assert r.status_code == 200
    assert context.overrides.get("item-1", "poster") is None


def test_reshuffle_changes_deterministic_pick(client, context):
    """S9d4a step 2: reshuffle bumps the counter; random_top_n then picks a
    different candidate (when >1), and is stable until the next reshuffle."""
    from finmeta.engine.artwork_selection import select_candidate as sel
    ranked = [
        Candidate(kind="poster", source="tmdb", ref=f"/tmp/p{i}.jpg", lang="en",
                  width=1000, height=1500, score=0.9 - i * 0.1)
        for i in range(3)
    ]
    # deterministic: same inputs -> same pick
    assert sel(ranked, preset="random_top_n", top_n=3, item_id="item-1",
               kind="poster", reshuffle_count=0).ref == \
        sel(ranked, preset="random_top_n", top_n=3, item_id="item-1",
            kind="poster", reshuffle_count=0).ref
    # a bump changes the pick (with 3 candidates over counts 0..2 at least one differs)
    picks = [sel(ranked, preset="random_top_n", top_n=3, item_id="item-1",
                 kind="poster", reshuffle_count=c).ref for c in range(3)]
    assert len(set(picks)) > 1

    # endpoint: POST /api/items/{id}/reshuffle bumps per-kind counters
    r = client.post("/api/items/item-1/reshuffle",
                    json={"kinds": ["poster", "backdrop"]})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["counts"] == {"poster": 1, "backdrop": 1}
    counts = context.overrides.reshuffle_counts("item-1")
    assert counts == {"poster": 1, "backdrop": 1}
    # second call increments again; untouched kinds stay absent
    r2 = client.post("/api/items/item-1/reshuffle", json={"kinds": ["poster"]})
    assert r2.json()["counts"] == {"poster": 2}


def test_reshuffle_clears_artwork_lock(client, context):
    """S9d5 "re-pick once" built on reshuffle (CLAUDE.md §4.1): once an
    item+kind is locked (first successful apply), reshuffle must also clear
    the lock -- otherwise plan_builder would keep skipping resolution for it
    and the bumped counter would have no visible effect."""
    context.overrides.set_lock("item-1", "poster", "/tmp/locked-poster.jpg", "a" * 64)
    assert context.overrides.get_lock("item-1", "poster") is not None

    r = client.post("/api/items/item-1/reshuffle", json={"kinds": ["poster"]})
    assert r.status_code == 200
    assert context.overrides.get_lock("item-1", "poster") is None
    # untouched kind's lock (if any) is unaffected
    context.overrides.set_lock("item-1", "backdrop", "/tmp/locked-backdrop.jpg", "b" * 64)
    client.post("/api/items/item-1/reshuffle", json={"kinds": ["poster"]})
    assert context.overrides.get_lock("item-1", "backdrop") is not None


def test_skip_kind_removes_row_from_plan(client, context):
    """S9d4b: skipping a kind drops its FileWrite from the item plan;
    unskipping restores it. Plan recompute (cache invalidation) respected."""
    # baseline: one metadata row
    r = client.get("/api/items/item-1/plan")
    assert [f["kind"] for f in r.json()["files"]] == ["metadata"]

    # skip the metadata kind -> row disappears, status becomes "empty"
    # (NOT "unmatched" -- item-1 is matched fine, it just has nothing left
    # to write; "unmatched" is reserved for a real TMDb match failure, see
    # routes_core._item_entries).
    r = client.post("/api/items/item-1/skip-kinds", json={"kind": "metadata"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["skipped"] == ["metadata"]

    r = client.get("/api/items/item-1/plan")
    assert r.json()["files"] == []
    assert r.json()["status"] == "empty"
    # library view agrees (diff counts recomputed)
    r = client.get("/api/libraries/Movies/items")
    item = next(i for i in r.json()["items"] if i["item_id"] == "item-1")
    assert item["files"] == []
    assert item["status"] == "empty"

    # unskip -> row comes back
    r = client.delete("/api/items/item-1/skip-kinds/metadata")
    assert r.status_code == 200
    assert r.json()["removed"] is True
    assert r.json()["skipped"] == []
    r = client.get("/api/items/item-1/plan")
    assert [f["kind"] for f in r.json()["files"]] == ["metadata"]

    # unskip again: nothing to remove, still ok
    r = client.delete("/api/items/item-1/skip-kinds/metadata")
    assert r.status_code == 200
    assert r.json()["removed"] is False


def test_item_plan_response_carries_skipped(client, context):
    """S9d4b step 4: the item-plan response lists skipped kinds (server truth)."""
    # baseline: nothing skipped
    r = client.get("/api/items/item-1/plan")
    assert r.status_code == 200
    assert r.json()["skipped"] == []

    # skip a kind -> plan response carries it in `skipped`
    r = client.post("/api/items/item-1/skip-kinds", json={"kind": "metadata"})
    assert r.status_code == 200
    r = client.get("/api/items/item-1/plan")
    assert r.status_code == 200
    body = r.json()
    assert body["skipped"] == ["metadata"]
    # the skipped row is gone from files (no local state — server recompute)
    assert [f["kind"] for f in body["files"]] == []

    # unskip -> cleared again
    r = client.delete("/api/items/item-1/skip-kinds/metadata")
    assert r.status_code == 200
    r = client.get("/api/items/item-1/plan")
    assert r.json()["skipped"] == []


def test_skip_kind_persists_across_replans(context):
    """S9d4b: the skip survives plan cache invalidation (bulk/library plans)."""
    context.overrides.set_skip("item-1", "metadata")
    context.invalidate_plans()
    plan = context.get_plan(item_id="item-1")
    item = next(ip for ip in plan.items if ip.item_id == "item-1")
    assert item.files == []
    # library-scoped plan (the path bulk apply uses) also respects the skip
    lib_plan = context.get_plan(library="Movies")
    item = next(ip for ip in lib_plan.items if ip.item_id == "item-1")
    assert item.files == []
    # unskip restores in both scopes
    assert context.overrides.clear_skip("item-1", "metadata") is True
    context.invalidate_plans()
    plan = context.get_plan(item_id="item-1")
    assert len(plan.items[0].files) == 1


def test_plan_cache_is_bounded(context):
    """Audit 2026-11-11 "C3": AppContext._cache used to grow forever --
    one entry per item viewed, one per library page, nothing ever evicted.
    Requesting more distinct scopes than the cap must never leave more than
    the cap's worth of entries behind."""
    from finmeta.api.state import _PLAN_CACHE_MAX_ENTRIES
    for i in range(_PLAN_CACHE_MAX_ENTRIES + 20):
        context.get_plan(item_id=f"item-{i}")
    assert len(context._cache) == _PLAN_CACHE_MAX_ENTRIES


def test_plan_cache_lru_protects_recently_touched_entries(context):
    """A cache hit marks that entry as most-recently-used, so a scope that
    gets re-viewed survives even though a chronologically-newer-but-never-
    revisited scope does not."""
    from finmeta.api.state import _PLAN_CACHE_MAX_ENTRIES
    for i in range(_PLAN_CACHE_MAX_ENTRIES):
        context.get_plan(item_id=f"item-{i}")
    gen = context._gen
    oldest_key = (gen, "item:item-0")
    second_key = (gen, "item:item-1")
    assert oldest_key in context._cache
    assert second_key in context._cache

    context.get_plan(item_id="item-0")  # a hit -- touches "item-0"
    # One more NEW scope forces an eviction. The least-recently-used entry
    # is now "item-1" (never touched again), not "item-0" (just touched),
    # even though "item-0" is chronologically the older of the two.
    context.get_plan(item_id=f"item-{_PLAN_CACHE_MAX_ENTRIES}")

    assert oldest_key in context._cache
    assert second_key not in context._cache
    assert len(context._cache) == _PLAN_CACHE_MAX_ENTRIES


# ---------------------------------------------------------------------------
# proxy routes
# ---------------------------------------------------------------------------

def test_proxy_token_roundtrip():
    token = encode_token("poster", "/some/path.jpg")
    kind, ref = decode_token(token)
    assert kind == "poster"
    assert ref == "/some/path.jpg"


def test_proxy_token_malformed():
    kind, ref = decode_token("not-valid-base64!!!")
    assert kind is None
    assert ref is None


def test_proxy_token_no_separator():
    import base64
    raw = base64.urlsafe_b64encode(b"poster").decode().rstrip("=")
    kind, ref = decode_token(raw)
    assert ref is None  # no pipe means no ref


def test_proxy_local_file(client, tmp_path):
    # Create a fake local file inside the configured local asset root (see
    # tmp_config): refs outside it are refused since the 2026-10-22 proxy fix.
    img = tmp_path / "local-assets" / "test.jpg"
    img.write_bytes(b"\xff\xd8\xff\xe0fake-jpeg")
    token = encode_token("poster", str(img))
    r = client.get(f"/api/proxy/{token}")
    assert r.status_code == 200
    assert r.content == b"\xff\xd8\xff\xe0fake-jpeg"
    assert r.headers["content-type"].startswith("image/jpeg")


def test_proxy_response_is_cacheable(client, tmp_path):
    """A token is a deterministic encoding of (kind, ref) -- same token,
    same bytes, always -- so the browser should be allowed to cache it
    instead of re-fetching on every render. Candidate grids can carry
    50-100+ live-proxied thumbnails; with no cache header at all, any
    re-render that remounted an <img> re-fetched every single one from
    scratch, reported live as thumbnails visibly reloading/"jumping"."""
    img = tmp_path / "local-assets" / "test.jpg"
    img.write_bytes(b"\xff\xd8\xff\xe0fake-jpeg")
    token = encode_token("poster", str(img))
    r = client.get(f"/api/proxy/{token}")
    assert r.status_code == 200
    assert "max-age" in r.headers.get("cache-control", "")


def test_proxy_not_found(client, tmp_path):
    token = encode_token("poster", str(tmp_path / "local-assets" / "nonexistent.jpg"))
    r = client.get(f"/api/proxy/{token}")
    assert r.status_code == 404


def test_proxy_bad_token(client):
    r = client.get("/api/proxy/badtoken")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# run routes
# ---------------------------------------------------------------------------

def test_run_start_plan(client, context):
    r = client.post("/api/runs", json={"action": "plan"})
    assert r.status_code == 202
    body = r.json()
    assert body["status"] == "running"
    assert "job_id" in body
    # Wait for the worker to finish (it should be fast with a fake plan)
    time.sleep(0.3)
    # Slot should be released
    assert context.active_run is None


def test_run_conflict(client, context):
    # Claim the slot manually to simulate an in-progress run
    assert context.claim_run("test-run-block")
    r = client.post("/api/runs", json={"action": "plan"})
    assert r.status_code == 409
    assert "already in progress" in r.json()["detail"]
    context.release_run("test-run-block")


def test_run_invalid_action(client):
    r = client.post("/api/runs", json={"action": "bogus"})
    assert r.status_code == 422



# test_runs_list / test_run_undo_no_manifest / test_run_undo_with_manifest were
# removed with GET /api/runs and POST /api/runs/undo themselves (2026-10-22,
# operator request -- see docs/SECURITY_REVIEW.md "D2"). Undo as a FEATURE is
# unaffected and still covered at engine level by tests/test_runs.py
# (restore/delete/manifest) -- the routes were thin wrappers with no caller.
def test_job_404(client):
    r = client.get("/api/jobs/no-such-run")
    assert r.status_code == 404


def test_logs_export_empty(client):
    """No runs yet -> still a well-formed (non-error) download, not a crash."""
    r = client.get("/api/logs/export")
    assert r.status_code == 200
    assert "attachment" in r.headers["content-disposition"]
    assert "finmeta-logs-" in r.headers["content-disposition"]
    assert "no runs logged yet" in r.text


def test_logs_export_redacts_secrets_and_includes_run_content(client, context):
    """Operator request (2026-10-26): a downloadable log, same convention as
    Sonarr/Radarr's Log Files -- every line must go through the same
    redact() used elsewhere (D2/secrets.py), even for a secret that leaked
    into a log line unredacted at write time."""
    from finmeta.engine.jobs import finish_run, init_db, log, start_run
    context.secrets.set("jellyfin_api_key", "super-secret-value-123")
    init_db(context.db_path)
    start_run(context.db_path, "run-export-test")
    log(context.db_path, "run-export-test", "info",
        "apply started scope=lib:Movies")
    # Deliberately NOT pre-redacted -- this is exactly the case the export's
    # own redact() pass (not just the write-time one) exists to still catch.
    log(context.db_path, "run-export-test", "error",
        "upstream call failed with key=super-secret-value-123")
    finish_run(context.db_path, "run-export-test", "ok")

    r = client.get("/api/logs/export")
    assert r.status_code == 200
    assert "super-secret-value-123" not in r.text
    assert "run-export-test" in r.text
    assert "apply started scope=lib:Movies" in r.text
    assert "upstream call failed with key=" in r.text




# ---------------------------------------------------------------------------
# plan cache invalidation
# ---------------------------------------------------------------------------

def test_plan_cache_invalidation_on_override(client, context):
    # First call populates cache
    r1 = client.get("/api/libraries/Movies/items")
    assert r1.status_code == 200
    # Setting an override invalidates the plan cache
    client.put("/api/items/item-1/candidates",
               json={"kind": "poster", "ref": "/tmp/new-ref.jpg"})
    # Next call should rebuild (still works)
    r2 = client.get("/api/libraries/Movies/items")
    assert r2.status_code == 200


# ---------------------------------------------------------------------------
# apply_overrides re-annotates status (S6b)
# ---------------------------------------------------------------------------

class TestApplyOverridesStatus:
    def test_override_swap_marks_update(self, tmp_path):
        """Swapping source_ref+sha to different bytes re-stamps status=update."""
        from finmeta.api.overrides import apply_overrides
        base = tmp_path / "Movies" / "M1"
        base.mkdir(parents=True)
        target = base / "poster.jpg"
        target.write_bytes(b"OLD")
        src_new = tmp_path / "new.jpg"
        src_new.write_bytes(b"NEW")
        plan = WritePlan(items=[ItemPlan(
            item_id="i1", item_name="M1", media_type="movie", tmdb_id=1,
            matched_by="provider_id",
            files=[FileWrite(target_path=str(target), kind="poster",
                             source_ref=str(target),
                             content_sha256="0" * 64, status="unchanged")],
        )])
        ovs = {"i1": [{"kind": "poster", "ref": str(src_new), "source": "local"}]}
        apply_overrides(plan, ovs, allowed_roots=(tmp_path,))
        fw = plan.items[0].files[0]
        assert fw.source_ref == str(src_new)
        # on-disk (OLD) != new content (NEW) -> update
        assert fw.status == "update"

    def test_override_matching_disk_is_unchanged(self, tmp_path):
        from finmeta.api.overrides import apply_overrides
        base = tmp_path / "M"
        base.mkdir(parents=True)
        target = base / "poster.jpg"
        target.write_bytes(b"SAME")
        src = tmp_path / "same.jpg"
        src.write_bytes(b"SAME")
        plan = WritePlan(items=[ItemPlan(
            item_id="i1", item_name="M1", media_type="movie", tmdb_id=1,
            matched_by="provider_id",
            files=[FileWrite(target_path=str(target), kind="poster",
                             source_ref=str(target),
                             content_sha256="1" * 64, status="create")],
        )])
        ovs = {"i1": [{"kind": "poster", "ref": str(src), "source": "local"}]}
        apply_overrides(plan, ovs, allowed_roots=(tmp_path,))
        assert plan.items[0].files[0].status == "unchanged"

    def test_no_override_keeps_status(self, tmp_path):
        from finmeta.api.overrides import apply_overrides
        target = tmp_path / "M" / "poster.jpg"
        target.parent.mkdir(parents=True)
        plan = WritePlan(items=[ItemPlan(
            item_id="i1", item_name="M1", media_type="movie", tmdb_id=1,
            matched_by="provider_id",
            files=[FileWrite(target_path=str(target), kind="poster",
                             source_ref="x", content_sha256="2" * 64,
                             status="unchanged")],
        )])
        apply_overrides(plan, {})
        assert plan.items[0].files[0].status == "unchanged"


# ---------------------------------------------------------------------------
# S9a: secrets (write-only), service test, help
# ---------------------------------------------------------------------------

def test_secrets_list_never_returns_values(client):
    r = client.get("/api/secrets")
    assert r.status_code == 200
    body = r.json()
    assert body["secrets"], "expected secret descriptors"
    for _name, view in body["secrets"].items():
        assert "value" not in view, "API must never return secret values"
        assert {"configured", "source", "hint"} <= set(view)


def test_secret_put_get_delete(client, monkeypatch):
    monkeypatch.delenv("FANART_KEY", raising=False)
    assert client.get("/api/secrets/fanart_key").json()["source"] == "none"
    assert client.put("/api/secrets/fanart_key",
                      json={"value": "fanart-dummy-9988"}).status_code == 200
    view = client.get("/api/secrets/fanart_key").json()
    assert view["source"] == "ui"
    assert view["hint"] == "••••9988"
    assert view["hint"] != "fanart-dummy-9988"  # masked, never the value
    assert client.delete("/api/secrets/fanart_key").json()["source"] == "none"


def test_secret_put_env_locked(client, monkeypatch):
    monkeypatch.setenv("FANART_KEY", "env-dummy-7777")
    r = client.put("/api/secrets/fanart_key", json={"value": "should-fail"})
    assert r.status_code == 409


def test_secret_unknown_404(client):
    assert client.get("/api/secrets/not_a_real_secret").status_code == 404


def test_secrets_list_hides_invalid_instance_refs(client, context):
    """S9d2-fix2 step 6a: an instance whose api_key is not a valid secret name
    must NOT appear in GET /api/secrets (known specs + is_arr_secret_name only)."""
    cfg = context.config
    bad = dict(cfg.model_dump())
    bad["media_apps"] = {
        "sonarr": [{"name": "Sonarr", "url": "http://127.0.0.1:8989",
                    "api_key": "totally-invalid-ref", "timeout": 30,
                    "verify_ssl": True, "enabled": True}],
        "radarr": [],
    }
    context.put_config(__import__("yaml").safe_dump(bad))
    names = set(client.get("/api/secrets").json()["secrets"].keys())
    assert "totally-invalid-ref" not in names
    # well-formed instance key of the same instance IS listed (from config)
    good = dict(cfg.model_dump())
    good["media_apps"] = {
        "sonarr": [{"name": "Sonarr", "url": "http://127.0.0.1:8989",
                    "api_key": "sonarr_sonarr_api_key", "timeout": 30,
                    "verify_ssl": True, "enabled": True}],
        "radarr": [],
    }
    context.put_config(__import__("yaml").safe_dump(good))
    names = set(client.get("/api/secrets").json()["secrets"].keys())
    assert "sonarr_sonarr_api_key" in names


def test_library_items_flags_invalid_instance_ref(client, context):
    """S9d2-fix2 step 6a: the instance API flags an invalid api_key reference."""
    cfg = context.config
    bad = dict(cfg.model_dump())
    bad["media_apps"] = {
        "sonarr": [{"name": "Sonarr", "url": "http://127.0.0.1:8989",
                    "api_key": "totally-invalid-ref", "timeout": 30,
                    "verify_ssl": True, "enabled": True}],
        "radarr": [],
    }
    context.put_config(__import__("yaml").safe_dump(bad))
    body = client.get("/api/libraries/Movies/items").json()
    inst = body["media_apps"]["sonarr"][0]
    assert inst["name"] == "Sonarr"
    assert inst["api_key_valid"] is False


def test_secret_put_arr_name_not_in_config(client, context):
    """PUT /api/secrets/<kind>_<inst>_api_key works even when no such instance
    exists in config yet (the form saves the key BEFORE writing the instance).

    The name is derived from an instance named "sonarr-4k" through the SAME slug
    rule the UI uses (SettingsPage: lowercase, non-alnum runs -> "_"); no name is
    hand-written here."""
    inst_name = "sonarr-4k"
    slug = "".join(c if c.isalnum() else "_" for c in inst_name.lower())
    import re
    slug = re.sub(r"^_+|_+$", "", slug) or "instance"
    secret_name = f"sonarr_{slug}_api_key"
    assert secret_name == "sonarr_sonarr_4k_api_key"
    r = client.put(f"/api/secrets/{secret_name}", json={"value": "k-123"})
    assert r.status_code == 200, r.text


def test_service_test_injected(client, context):
    context.service_testers = {
        "jellyfin": lambda cfg: {"ok": True, "latency_ms": 9,
                                 "version": "10.8.12", "error": None},
        "tmdb": lambda cfg: {"ok": True, "latency_ms": 40,
                             "version": "tmdb", "error": None},
    }
    r = client.post("/api/services/jellyfin/test")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "latency_ms": 9,
                        "version": "10.8.12", "error": None}
    assert client.post("/api/services/tmdb/test").json()["ok"] is True
    assert client.post("/api/services/doesnotexist/test").status_code == 404


def test_service_test_redacts_error(client, context):
    context.secrets.set("fanart_key", "dummy-secret-AAAA")
    context.service_testers = {
        "fanart": lambda cfg: {"ok": False, "latency_ms": None, "version": None,
                               "error": "rejected key dummy-secret-AAAA"},
    }
    r = client.post("/api/services/fanart/test")
    assert r.status_code == 200
    err = r.json()["error"]
    assert "dummy-secret-AAAA" not in err
    assert "__FINMETA_SECRET__" in err


def test_help_route(client):
    r = client.get("/api/help")
    assert r.status_code == 200
    h = r.json()["help"]
    assert "dry_run" in h and "provider.tmdb" in h and "secret.tmdb_key" in h


# ---------------------------------------------------------------------------
# S9b: secret resolution — env first, then the Settings store (hot-reload)
# ---------------------------------------------------------------------------

def test_jellyfin_secret_store_only_client_and_tester(client, context, monkeypatch):
    """A Jellyfin key saved via Settings (store, no env) must be sent by the
    client AND used by the service tester, with the client's exact header."""
    import httpx
    import respx
    monkeypatch.delenv("JELLYFIN_API_KEY", raising=False)
    context.secrets.set("jellyfin_api_key", "store-jf-key-1234")
    jf = context.make_jellyfin()
    assert jf.token == "store-jf-key-1234"
    jf.close()
    captured: dict = {}
    def _side(request):
        captured["auth"] = request.headers.get("Authorization", "")
        return httpx.Response(200, json={"Version": "10.8.13"})
    with respx.mock:
        respx.get("http://fake:8096/System/Info").mock(side_effect=_side)
        r = client.post("/api/services/jellyfin/test")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["version"] == "10.8.13"
    assert 'Token="store-jf-key-1234"' in captured["auth"]
    assert "MediaBrowser" in captured["auth"]


def test_jellyfin_secret_hot_reload_no_restart(context, monkeypatch):
    monkeypatch.delenv("JELLYFIN_API_KEY", raising=False)
    context.secrets.set("jellyfin_api_key", "first-0001")
    assert context.make_jellyfin().token == "first-0001"
    context.secrets.set("jellyfin_api_key", "second-2222")
    assert context.resolve_secret("jellyfin_api_key") == "second-2222"
    assert context.make_jellyfin().token == "second-2222"


def test_tmdb_and_sources_secret_store_only(context, monkeypatch):
    monkeypatch.delenv("TMDB_KEY", raising=False)
    monkeypatch.delenv("FANART_KEY", raising=False)
    context.secrets.set("tmdb_key", "store-tmdb-5678")
    context.secrets.set("fanart_key", "store-fanart-9012")
    tmdb = context.make_tmdb()
    assert tmdb.api_key == "store-tmdb-5678"
    sources = context.make_sources()
    assert sources["tmdb"].api_key == "store-tmdb-5678"
    # fanart is disabled in this config, but its key still resolves from the store
    assert context.resolve_secret("fanart_key") == "store-fanart-9012"
    tmdb.close()
    sources["tmdb"].close()


# ---------------------------------------------------------------------------
# B7 (audit 2026-11-11): config.yml writes are atomic.
# ---------------------------------------------------------------------------
#
# put_config used to write config.yml with a plain Path.write_text(), which
# truncates the file before the new content lands -- a crash, a full disk or
# a container kill mid-write left a truncated or empty config.yml, the file
# whose loss stops the app from even starting. These confirm the write now
# goes through api.atomic.write_text_atomic (temp file + fsync + os.replace),
# the same guarantee secrets.yml/auth.yml already had.

def test_put_config_write_is_atomic_and_leaves_no_temp_file(context):
    before = context.config_text()
    new_yaml = before.replace("dry_run: true", "dry_run: false")
    context.put_config(new_yaml)
    leftovers = [p.name for p in context.config_dir.iterdir()
                if p.name.startswith(".finmeta_")]
    assert leftovers == []


def test_put_config_failed_write_leaves_the_old_config_on_disk(context, monkeypatch):
    """A torn write (simulated: os.replace fails after the temp file is
    fully written) must leave config.yml exactly as it was -- not
    truncated, not half-written -- because write_text_atomic never
    touches the real path until the replace, which here never happens."""
    from finmeta.api import atomic

    before_text = context.config_text()
    before_dry_run = context.config.dry_run

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(atomic.os, "replace", boom)
    new_yaml = before_text.replace("dry_run: true", "dry_run: false")
    with pytest.raises(OSError):
        context.put_config(new_yaml)
    assert context.config_path.read_text() == before_text
    monkeypatch.undo()  # restore real os.replace before reloading from disk
    ok, _err = context.reload()
    assert ok is True
    assert context.config.dry_run == before_dry_run


def test_load_config_wraps_oserror_in_configerror(tmp_path):
    """A missing config file must raise ConfigError (naming the path),
    not a bare FileNotFoundError with no context for the caller."""
    from finmeta.config.loading import ConfigError, load_config

    missing = tmp_path / "does-not-exist.yml"
    with pytest.raises(ConfigError, match=str(missing)):
        load_config(missing)
