"""GET /api/libraries/{name}/items/light (2026-10-23): the Jellyfin-only
item list the browser now loads first -- no TMDb/fanart, no WritePlan.
Season/Episode items are filtered out; only Movie/Series are listed.
"""
from __future__ import annotations

from typing import Dict, List, Optional

import pytest
from fastapi.testclient import TestClient

from finmeta.api import AppContext, create_app
from finmeta.jellyfin_client.types import Item, Library

CONFIG_YAML = """\
dry_run: true
server:
  url: http://fake:8096
  api_key_env: JELLYFIN_API_KEY
libraries:
  Movies:
    type: movies
    metadata:
      enabled: true
    artwork: {}
    theme_song: false
    overlays: []
"""


class FakeJf:
    def __init__(self, libs: Dict[str, Library], items_by_parent: Dict[str, List[Item]]):
        self._libs = libs
        self._items = items_by_parent

    def get_library(self, name: str) -> Optional[Library]:
        return self._libs.get(name)

    def get_items(self, parent_id: Optional[str] = None, recursive: bool = True) -> List[Item]:
        return list(self._items.get(parent_id, []))

    def close(self):
        pass


@pytest.fixture
def context(tmp_path, monkeypatch):
    monkeypatch.setenv("FINMETA_AUTH", "off")
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "config.yml").write_text(CONFIG_YAML)
    ctx = AppContext(str(cfg_dir / "config.yml"), config_dir=str(cfg_dir),
                     db_path=str(cfg_dir / "jobs.db"), overrides_path=str(cfg_dir / "overrides.db"))
    libs = {"Movies": Library(name="Movies", item_id="lib-movies", collection_type="movies")}
    items = {
        "lib-movies": [
            Item(id="m1", name="Movie One", type="Movie", path="/media/m1.mkv", provider_ids={}),
            Item(id="m2", name="Movie Two", type="Movie", path="/media/m2.mkv", provider_ids={}),
            # A movie library's recursive fetch shouldn't normally contain
            # Season/Episode, but filter them out defensively anyway.
            Item(id="s1", name="Stray Season", type="Season", path="/media/s1", provider_ids={}),
        ],
    }
    ctx.make_jellyfin = lambda cfg=None: FakeJf(libs, items)  # type: ignore[method-assign]
    return ctx


@pytest.fixture
def client(context):
    with TestClient(create_app(context)) as c:
        yield c


def test_light_list_has_names_only_no_diff_or_tmdb_fields(client):
    r = client.get("/api/libraries/Movies/items/light")
    assert r.status_code == 200
    body = r.json()
    assert body["library"] == "Movies"
    assert [i["item_id"] for i in body["items"]] == ["m1", "m2"]
    assert body["items"][0] == {"item_id": "m1", "item_name": "Movie One", "media_type": "movie"}
    assert "diff" not in body["items"][0]
    assert "total_items" not in body
    assert "summary" not in body


def test_unknown_library_is_404(client):
    r = client.get("/api/libraries/Nonexistent/items/light")
    assert r.status_code == 404


def test_library_metadata_fields_present(client):
    r = client.get("/api/libraries/Movies/items/light")
    body = r.json()
    assert isinstance(body["enabled"], bool)
    assert "media_apps" in body
