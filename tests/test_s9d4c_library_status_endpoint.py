"""S9d4c Fase A step 3: GET /api/libraries/{name}/items carries the per-library
status (enabled, overlay_profile, overlay_element_count) the UI needs for the
home status line ("Library disabled" / "Overlays: <profile> (N elements)" /
"Overlays: none") and the plan-row overlay labels ("with overlay (<profile>)"
/ "no overlay"). These are derived straight from ctx.config via the same
_profile_overlay_names helper plan_builder itself uses (single source of
truth, S9d4c step 2b) -- independent of whatever the (here: faked) plan
builder returns.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from finmeta.api import AppContext, create_app
from finmeta.engine.plan import WritePlan


def _fake_plan(cfg, jf, tmdb, sources, library=None, item_id=None, config_dir=None, **kwargs):
    return WritePlan(items=[])


def _ctx(tmp_path, monkeypatch, config_yaml: str) -> AppContext:
    monkeypatch.setenv("JELLYFIN_API_KEY", "fake-key")
    monkeypatch.setenv("TMDB_KEY", "fake-tmdb-key")
    monkeypatch.setenv("FINMETA_AUTH", "off")
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    cfg_file = cfg_dir / "config.yml"
    cfg_file.write_text(config_yaml)
    return AppContext(
        str(cfg_file), config_dir=str(cfg_dir),
        db_path=str(cfg_dir / "jobs.db"), overrides_path=str(cfg_dir / "overrides.db"),
        plan_builder=_fake_plan,
    )


def test_enabled_library_with_profile_reports_status(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, monkeypatch, """\
dry_run: true
server:
  url: http://fake:8096
  api_key_env: JELLYFIN_API_KEY
libraries:
  Movies:
    type: movies
    enabled: true
    overlay_profile: standard
overlays:
  profiles:
    standard:
      elements: [resolution, hdr, audio]
""")
    with TestClient(create_app(ctx)) as client:
        r = client.get("/api/libraries/Movies/items")
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is True
    assert body["overlay_profile"] == "standard"
    assert body["overlay_element_count"] == 3


def test_disabled_library_reports_status(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, monkeypatch, """\
dry_run: true
server:
  url: http://fake:8096
  api_key_env: JELLYFIN_API_KEY
libraries:
  Anime:
    type: movies
    enabled: false
    overlay_profile: standard
overlays:
  profiles:
    standard:
      elements: [resolution]
""")
    with TestClient(create_app(ctx)) as client:
        r = client.get("/api/libraries/Anime/items")
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is False
    # Still reports the assigned profile/count -- disabled just means "no plan
    # work happens", not "profile info is hidden".
    assert body["overlay_profile"] == "standard"
    assert body["overlay_element_count"] == 1


def test_enabled_library_without_profile_reports_zero_elements(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, monkeypatch, """\
dry_run: true
server:
  url: http://fake:8096
  api_key_env: JELLYFIN_API_KEY
libraries:
  Movies:
    type: movies
    enabled: true
""")
    with TestClient(create_app(ctx)) as client:
        r = client.get("/api/libraries/Movies/items")
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is True
    assert body["overlay_profile"] is None
    assert body["overlay_element_count"] == 0


def test_deprecated_overlays_list_reports_through_the_auto_profile(tmp_path, monkeypatch):
    """The (deprecated) overlays list alone -- migrated at load time into an
    auto profile (S9d4c step 2b) -- must still be reflected here (single
    source of truth: the endpoint reads overlay_profile, not the old list)."""
    ctx = _ctx(tmp_path, monkeypatch, """\
dry_run: true
server:
  url: http://fake:8096
  api_key_env: JELLYFIN_API_KEY
libraries:
  Movies:
    type: movies
    enabled: true
    overlays: [resolution, rating]
""")
    with TestClient(create_app(ctx)) as client:
        r = client.get("/api/libraries/Movies/items")
    assert r.status_code == 200
    body = r.json()
    assert body["overlay_profile"] == "Movies-auto"
    assert body["overlay_element_count"] == 2
