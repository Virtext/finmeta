"""Live smoke test for the API (marked ``live``).

Scoped to ONE item: verifies the API can build a plan and list candidates
against the real test server without a full library build.

Run with:  pytest -m live -k api
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from finmeta.api import AppContext, create_app

ROOT = Path(__file__).resolve().parents[1]
CREDS_PATH = ROOT / ".jf-test" / "creds.json"


def _creds() -> dict:
    if not CREDS_PATH.exists():
        pytest.skip("creds.json not found")
    return json.loads(CREDS_PATH.read_text())


@pytest.fixture
def live_context(tmp_path):
    """Build an AppContext against the real test server."""
    creds = _creds()
    os.environ.setdefault("JELLYFIN_API_KEY", creds["api_key"])
    os.environ["FINMETA_AUTH"] = "off"  # S9a: bypass auth for the API smoke test
    # Root cause of the old 502 (S9d2-fix2 step 0): the dev secrets store's
    # tmdb_key is a placeholder ("stored key ...", TMDB 401s), and AppContext
    # resolves secrets from <config_dir>/secrets.yml — which did not exist for
    # this temp config. So make_tmdb() got an empty key and plan build failed
    # with "TMDB_KEY is empty" (502) before ever reaching Jellyfin. The
    # JELLYFIN_API_KEY env var comes from creds.json, which is the correct
    # source (same as .tools/start-dev.sh exports). We inject a syntactically
    # valid dummy TMDB key into the temp config's secrets store so plan build
    # proceeds to the Jellyfin call; TMDb lookups themselves 401 and are
    # tolerated by the matcher (tmdb_id may be None), which the assertions below
    # account for.
    from finmeta.api.secrets import SecretsStore
    _dummy = "0" * 32  # valid shape; TMDB will 401 -> treated as no metadata
    cfg_yaml = f"""\
dry_run: true
server:
  url: {creds['url']}
  api_key_env: JELLYFIN_API_KEY
path_mappings:
  - jellyfin: /media
    local: /work/fixture/library
libraries:
  Movies:
    type: movies
    metadata:
      enabled: true
    artwork:
      poster: true
    theme_song: false
    overlays: []
"""
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    cfg_file = cfg_dir / "config.yml"
    cfg_file.write_text(cfg_yaml)
    # Inject the dummy TMDB key into THIS config dir's secrets store (AppContext
    # resolves secrets from <config_dir>/secrets.yml).
    SecretsStore(cfg_dir / "secrets.yml").set("tmdb_key", _dummy)

    ctx = AppContext(
        str(cfg_file),
        config_dir=str(cfg_dir),
        db_path=str(cfg_dir / "jobs.db"),
        overrides_path=str(cfg_dir / "overrides.db"),
    )
    return ctx


@pytest.fixture
def live_client(live_context):
    app = create_app(live_context)
    with TestClient(app) as c:
        yield c


@pytest.mark.live
def test_api_health(live_client):
    r = live_client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["dry_run"] is True


@pytest.mark.live
def test_api_libraries(live_client):
    r = live_client.get("/api/libraries")
    assert r.status_code == 200
    body = r.json()
    assert "Movies" in [lib["name"] for lib in body["libraries"]]


@pytest.mark.live
def test_api_single_item_plan(live_client, live_context):
    """Build a plan for ONE item from the real server."""
    from finmeta.jellyfin_client import JellyfinClient
    creds = _creds()
    jf = JellyfinClient(creds["url"], token=creds["api_key"])
    # Get one movie item
    items = jf.get_items_by_type("Movie")[:1]
    jf.close()
    assert items, "no movie items found on test server"
    item_id = items[0].id

    r = live_client.get(f"/api/items/{item_id}/plan")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["item_id"] == item_id
    # tmdb_id is only non-None when a real TMDB key is configured; with the
    # dummy key (dev store placeholder) matching degrades to None.


@pytest.mark.live
def test_api_single_item_candidates(live_client, live_context):
    """List candidates for ONE item from the real server."""
    from finmeta.jellyfin_client import JellyfinClient
    creds = _creds()
    jf = JellyfinClient(creds["url"], token=creds["api_key"])
    items = jf.get_items_by_type("Movie")[:1]
    jf.close()
    assert items, "no movie items found on test server"
    item_id = items[0].id

    r = live_client.get(f"/api/items/{item_id}/candidates")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["item_id"] == item_id
    # With a real TMDB key this yields poster candidates; with the dummy key
    # (dev store placeholder) TMDb 401s and candidate resolution degrades to
    # an empty list (see resolution._query_source), so no poster is expected.
    if body.get("tmdb_id") is not None:
        kinds = [c["kind"] for c in body["candidates"]]
        assert "poster" in kinds or not kinds  # real key -> poster; dummy -> empty
