"""Collection Builder: user-defined `filter`/`list` collections.

Covers _build_api_actions' handling of cfg.collections: full-run sync
(honours sync_mode), scoped runs stay additive, items from other libraries
never leak in, disabled libraries are left alone; plus the duplicate-name
config check.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from finmeta.config.models import (
    Config, Metadata, MovieArtwork, MovieLibrary, ServerConfig,
)
from finmeta.engine.plan import ItemPlan
from finmeta.engine.plan_builder import _build_api_actions


def _lib(enabled=True):
    return MovieLibrary(type="movies", enabled=enabled,
                        metadata=Metadata(enabled=False), artwork=MovieArtwork())


def _cfg(collections, sync_mode="sync", b_enabled=True):
    return Config(dry_run=True, server=ServerConfig(url="http://x:8096"),
                  libraries={"A": _lib(), "B": _lib(b_enabled)},
                  collections=collections, sync_mode=sync_mode)


def _ip(tmdb_id, item_id, library):
    return ItemPlan(item_id=item_id, item_name=item_id, media_type="movie",
                    tmdb_id=tmdb_id, library=library)


def _tmdb(details):
    t = MagicMock()
    t.movie_details.side_effect = lambda tid, language=None: details.get(tid, {})
    return t


DETAILS = {
    1: {"release_date": "1999-01-01", "vote_average": 8.0, "genres": [{"name": "Horror"}]},
    2: {"release_date": "2010-01-01", "vote_average": 5.0, "genres": [{"name": "Horror"}]},
    3: {"release_date": "1999-01-01", "vote_average": 9.0, "genres": [{"name": "Horror"}]},
}
ITEMS = [_ip(1, "a1", "A"), _ip(2, "a2", "A"), _ip(3, "b3", "B")]
HORROR_A = {"type": "filter", "name": "Good horror", "library": "A",
            "filter": {"genres": ["Horror"], "min_rating": 7}}


def test_full_run_syncs_and_ignores_other_libraries():
    acts = _build_api_actions(_cfg([HORROR_A]), ITEMS, _tmdb(DETAILS), "en")
    (a,) = [x for x in acts if x.name == "Good horror"]
    assert a.action == "sync"
    assert a.add_ids == ["a1"]  # b3 matches the filter but lives in library B


def test_full_run_with_no_matches_still_emits_sync_so_stale_collection_is_removed():
    coll = {**HORROR_A, "filter": {"min_rating": 9.5}}
    (a,) = [x for x in _build_api_actions(_cfg([coll]), ITEMS, _tmdb(DETAILS), "en")
            if x.name == "Good horror"]
    assert a.action == "sync" and a.add_ids == []


def test_append_mode_stays_additive():
    acts = _build_api_actions(_cfg([HORROR_A], sync_mode="append"), ITEMS, _tmdb(DETAILS), "en")
    (a,) = [x for x in acts if x.name == "Good horror"]
    assert a.action == "create" and a.add_ids == ["a1"]


def test_scoped_run_stays_additive():
    acts = _build_api_actions(_cfg([HORROR_A]), ITEMS[:2], _tmdb(DETAILS), "en", library_name="A")
    (a,) = [x for x in acts if x.name == "Good horror"]
    assert a.action == "create"


def test_list_collection_and_disabled_library_skipped():
    colls = [{"type": "list", "name": "Picks", "library": "A", "tmdb_ids": [2, 3]},
             {"type": "list", "name": "Off", "library": "B", "tmdb_ids": [3]}]
    acts = _build_api_actions(_cfg(colls, b_enabled=False), ITEMS, _tmdb(DETAILS), "en")
    by_name = {a.name: a for a in acts}
    assert by_name["Picks"].add_ids == ["a2"]
    assert "Off" not in by_name


def test_duplicate_collection_names_rejected():
    with pytest.raises(ValidationError, match="duplicate collection name"):
        _cfg([HORROR_A, {**HORROR_A, "library": "B"}])
