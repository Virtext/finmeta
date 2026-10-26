"""A TV show that fails TMDb matching must be reported in plan.unmatched
with a reason, exactly like a movie -- not silently dropped.

Before this fix, only the Movie branch of build_plan's main loop checked
``match.tmdb_id is None`` and added an UnmatchedItem before continuing.
The Series branch called _build_tvshow_plan unconditionally; an unmatched
show came back as ItemPlan(tmdb_id=None, files=[]), which then failed the
"if ip.files or ip.tmdb_id" append check and vanished from BOTH plan.items
and plan.unmatched. GET /api/items/{id}/plan could then find it in neither
list and raised a bare, reasonless 404 "item {id} not in plan" -- reported
live for a TV library with several shows that don't have a TMDb match.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from finmeta.config.models import Config, Metadata, ServerConfig, TvArtwork, TvLibrary
from finmeta.engine.plan_builder import build_plan
from finmeta.jellyfin_client.types import Item, Library


class FakeJf:
    def __init__(self, libraries: Dict[str, Library],
                items_by_parent: Dict[str, List[Item]]):
        self._libraries = libraries
        self._items_by_parent = items_by_parent

    def get_library(self, name: str) -> Optional[Library]:
        return self._libraries.get(name)

    def get_libraries(self) -> List[Library]:
        return list(self._libraries.values())

    def get_item(self, item_id: str) -> Item:
        for items in self._items_by_parent.values():
            for it in items:
                if it.id == item_id:
                    return it
        raise KeyError(item_id)

    def get_items(self, parent_id: Optional[str] = None, recursive: bool = True) -> List[Item]:
        return list(self._items_by_parent.get(parent_id, []))

    def get_items_media_streams_raw(self, item_ids, batch_size=50):
        return {}

    def close(self):
        pass


class FakeTmdbNoMatch:
    """Every lookup comes back empty -- nothing ever matches."""

    def search_tv(self, query, year=None, language=None):
        return []

    def search_movies(self, query, year=None, language=None):
        return []

    def find_by_external_id(self, media_type, imdb_id, language=None):
        return None


def _show_item(item_id: str, parent_id: str) -> Item:
    # No provider_ids and no parseable year in the name -> both matching
    # steps in engine.matching.match_item fail, same as a real unrecognized
    # title would.
    return Item(id=item_id, name="Some Unrecognized Show", type="Series",
               path=f"/media/{item_id}", provider_ids={}, parent_id=parent_id)


def _cfg() -> Config:
    lib = TvLibrary(type="tvshows", enabled=True,
                    metadata=Metadata(enabled=True), artwork=TvArtwork())
    return Config(dry_run=True, server=ServerConfig(url="http://x:8096"),
                 libraries={"Shows": lib})


def test_unmatched_series_reported_not_silently_dropped():
    cfg = _cfg()
    jf_libs = {"Shows": Library(name="Shows", item_id="lib-shows", collection_type="tvshows")}
    show = _show_item("s1", parent_id="lib-shows")
    jf = FakeJf(jf_libs, {"lib-shows": [show]})

    plan = build_plan(cfg, jf, FakeTmdbNoMatch(), {}, library="Shows")

    assert plan.items == []
    assert [u.item_id for u in plan.unmatched] == ["s1"]
    assert "no TMDb match" in plan.unmatched[0].reason


def test_unmatched_series_item_scope_also_reported():
    """Same check via the item_id scope (what GET /api/items/{id}/plan uses)."""
    cfg = _cfg()
    jf_libs = {"Shows": Library(name="Shows", item_id="lib-shows", collection_type="tvshows",
                                locations=["/media"])}
    show = _show_item("s1", parent_id="lib-shows")
    jf = FakeJf(jf_libs, {"lib-shows": [show]})

    plan = build_plan(cfg, jf, FakeTmdbNoMatch(), {}, item_id="s1")

    assert plan.items == []
    assert [u.item_id for u in plan.unmatched] == ["s1"]
