"""routes_candidates._default_candidates_provider must resolve an item's
OWNING library the same way engine.plan_builder.build_plan does (path-prefix
match against Jellyfin's own reported Locations, see
plan_builder._resolve_item_library) -- not "the first configured library of
the matching movies/tvshows type", which this used to do. With more than one
library of the same type configured (not a contrived case -- reported live
with three tvshows-type libraries at once), that silently used the WRONG
library's artwork-kind settings, so candidates for a kind the item's real
library enables never showed up: looked exactly like "poster/backdrop/logo
won't load at all" for any library that wasn't the one it happened to pick.
"""
from __future__ import annotations

from typing import Dict, List, Optional
from unittest.mock import patch

from finmeta.api.routes_candidates import _default_candidates_provider
from finmeta.config.models import Config, Metadata, ServerConfig, TvArtwork, TvLibrary
from finmeta.jellyfin_client.types import Item, Library


class FakeJf:
    def __init__(self, libraries: Dict[str, Library]):
        self._libraries = libraries

    def get_libraries(self) -> List[Library]:
        return list(self._libraries.values())


def _tv_lib(enabled: bool, poster: bool) -> TvLibrary:
    return TvLibrary(type="tvshows", enabled=enabled,
                     metadata=Metadata(enabled=False),
                     artwork=TvArtwork(poster=poster))


def _show_item(path: str) -> Item:
    return Item(id="s1", name="Some Show (2020)", type="Series", path=path,
               provider_ids={"Tmdb": "42"})  # short-circuits match_item, no tmdb calls


def test_candidates_use_the_path_matched_librarys_kinds_not_the_first_configured_one():
    cfg = Config(dry_run=True, server=ServerConfig(url="http://x:8096"), libraries={
        # "First" sorts/declares before "Second" but does NOT own this item.
        "First": _tv_lib(enabled=True, poster=False),
        "Second": _tv_lib(enabled=True, poster=True),
    })
    jf = FakeJf({
        "First": Library(name="First", item_id="lib-first", collection_type="tvshows",
                         locations=["/media/first-root"]),
        "Second": Library(name="Second", item_id="lib-second", collection_type="tvshows",
                          locations=["/media/second-root"]),
    })
    item = _show_item("/media/second-root/Some Show (2020)/Season 01/ep.mkv")

    captured = {}

    def _fake_collect_candidates(sources, precedence, mt, tmdb_id, kinds, language, fallback):
        captured["kinds"] = kinds
        return {}

    with patch("finmeta.api.routes_candidates.collect_candidates", _fake_collect_candidates):
        _default_candidates_provider(item, cfg, jf, tmdb=None, sources={}, mapper=None)

    # Second (the real owner) has poster=True; First (would've been picked by
    # the old "first configured library of this type" logic) has poster=False.
    assert captured["kinds"] == ["poster"]


def test_candidates_empty_kinds_when_item_resolves_to_no_library():
    """Mirrors plan_builder's own "couldn't resolve" case (a real path that
    matches no configured library's Locations) -- must not crash, and must
    not silently borrow some other library's kinds either."""
    cfg = Config(dry_run=True, server=ServerConfig(url="http://x:8096"), libraries={
        "Only": _tv_lib(enabled=True, poster=True),
    })
    jf = FakeJf({
        "Only": Library(name="Only", item_id="lib-only", collection_type="tvshows",
                        locations=["/media/only-root"]),
    })
    item = _show_item("/media/completely-unrelated/Some Show (2020)/Season 01/ep.mkv")

    captured = {}

    def _fake_collect_candidates(sources, precedence, mt, tmdb_id, kinds, language, fallback):
        captured["kinds"] = kinds
        return {}

    with patch("finmeta.api.routes_candidates.collect_candidates", _fake_collect_candidates):
        _default_candidates_provider(item, cfg, jf, tmdb=None, sources={}, mapper=None)

    assert captured["kinds"] == []
