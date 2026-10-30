"""GET /api/libraries/{name}/items/light (2026-10-23): the Jellyfin-only
item list the browser now loads first -- no TMDb/fanart, no WritePlan.
Season/Episode items are filtered out; only Movie/Series are listed.
"""
from __future__ import annotations

from typing import Dict, List, Optional

import pytest
from fastapi.testclient import TestClient

from finmeta.api import AppContext, create_app
from finmeta.jellyfin_client import ItemPage
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
        self.get_items_page_calls: List[dict] = []

    def get_library(self, name: str) -> Optional[Library]:
        return self._libs.get(name)

    def get_items(self, parent_id: Optional[str] = None, recursive: bool = True) -> List[Item]:
        return list(self._items.get(parent_id, []))

    def get_items_page(self, *, parent_id=None, recursive=True, include_item_types=None,
                       fields=None, user_id=None, limit=None, start_index=None,
                       sort_by=None, sort_order=None, search_term=None) -> ItemPage:
        self.get_items_page_calls.append({
            "parent_id": parent_id, "include_item_types": include_item_types,
            "fields": fields, "limit": limit, "start_index": start_index,
            "search_term": search_term,
        })
        pool = list(self._items.get(parent_id, []))
        if include_item_types:
            pool = [it for it in pool if it.type in include_item_types]
        if search_term:
            pool = [it for it in pool if search_term.lower() in it.name.lower()]
        total = len(pool)
        start = start_index or 0
        page_items = pool[start:start + limit] if limit is not None else pool[start:]
        return ItemPage(items=page_items, total_record_count=total, start_index=start)

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
    shared_jf = FakeJf(libs, items)
    ctx.make_jellyfin = lambda cfg=None: shared_jf  # type: ignore[method-assign]
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
    assert "summary" not in body


def test_unknown_library_is_404(client):
    r = client.get("/api/libraries/Nonexistent/items/light")
    assert r.status_code == 404


def test_library_metadata_fields_present(client):
    r = client.get("/api/libraries/Movies/items/light")
    body = r.json()
    assert isinstance(body["enabled"], bool)
    assert "media_apps" in body


# ---------------------------------------------------------------------------
# Pagination fix (2026-10-30): never an unbounded /Items call, limit/offset
# reach Jellyfin, total/has_more are correct, season/episode type-filtered
# AT Jellyfin (IncludeItemTypes) rather than in Python after the fact.
# ---------------------------------------------------------------------------

def test_default_pagination_fields_present(client, context):
    r = client.get("/api/libraries/Movies/items/light")
    body = r.json()
    assert body["total_items"] == 2
    assert body["limit"] == 100
    assert body["offset"] == 0
    assert body["has_more"] is False


def test_limit_and_offset_reach_jellyfin_and_window_the_page(client, context):
    r = client.get("/api/libraries/Movies/items/light?limit=1&offset=1")
    assert r.status_code == 200
    body = r.json()
    assert [i["item_id"] for i in body["items"]] == ["m2"]
    assert body["total_items"] == 2
    assert body["limit"] == 1
    assert body["offset"] == 1
    assert body["has_more"] is False

    jf = context.make_jellyfin()
    assert len(jf.get_items_page_calls) >= 1
    last = jf.get_items_page_calls[-1]
    assert last["limit"] == 1
    assert last["start_index"] == 1
    assert last["include_item_types"] == ["Movie", "Series"]
    # Minimal fields: never Path/ProviderIds/Overview on this endpoint.
    assert last["fields"] is None


def test_has_more_true_when_a_further_page_exists(client, context):
    r = client.get("/api/libraries/Movies/items/light?limit=1&offset=0")
    body = r.json()
    assert body["has_more"] is True


def test_limit_is_bounded_1_to_500(client):
    assert client.get("/api/libraries/Movies/items/light?limit=0").status_code == 422
    assert client.get("/api/libraries/Movies/items/light?limit=501").status_code == 422
    assert client.get("/api/libraries/Movies/items/light?offset=-1").status_code == 422


def test_search_forwards_as_search_term_and_filters(client, context):
    r = client.get("/api/libraries/Movies/items/light?search=Two")
    assert r.status_code == 200
    body = r.json()
    assert [i["item_id"] for i in body["items"]] == ["m2"]
    assert body["total_items"] == 1

    jf = context.make_jellyfin()
    assert jf.get_items_page_calls[-1]["search_term"] == "Two"


def test_never_an_unbounded_items_call(client, context):
    """Every /items/light request must send a bounded Limit -- never None."""
    client.get("/api/libraries/Movies/items/light")
    jf = context.make_jellyfin()
    assert jf.get_items_page_calls
    for call in jf.get_items_page_calls:
        assert call["limit"] is not None
        assert call["include_item_types"] == ["Movie", "Series"]


def test_jellyfin_502_mapping_still_works(context, client):
    def _boom(**kwargs):
        raise RuntimeError("Jellyfin unreachable")

    jf = context.make_jellyfin()
    jf.get_items_page = _boom  # type: ignore[method-assign]
    r = client.get("/api/libraries/Movies/items/light")
    assert r.status_code == 502
