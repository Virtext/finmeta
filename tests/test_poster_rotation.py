"""S9d6: scheduled poster rotation for a library's top-rated items.

Two layers tested: ``engine.poster_rotation.top_rated_item_ids`` (pure
ranking by Jellyfin's own CommunityRating) and
``api.scheduler._poster_rotation_job`` (the guard conditions + the reuse of
the existing single-item webhook queue to chain N items through the
single-run slot).
"""
from __future__ import annotations

from typing import Dict, List, Optional
from unittest.mock import MagicMock

from finmeta.api.scheduler import _poster_rotation_job
from finmeta.config.models import (
    Config, Metadata, MovieArtwork, MovieLibrary, PosterRotation, ServerConfig,
)
from finmeta.engine.poster_rotation import top_rated_item_ids
from finmeta.jellyfin_client.types import Item, Library


class FakeJf:
    def __init__(self, library: Library, items: List[Item]):
        self._library = library
        self._items = items

    def get_library(self, name: str) -> Optional[Library]:
        return self._library if name == self._library.name else None

    def get_items(self, *, parent_id=None, recursive=True, include_item_types=None,
                  fields=None) -> List[Item]:
        return [it for it in self._items if it.type in (include_item_types or [it.type])]

    def iter_items(self, *, parent_id=None, recursive=True, include_item_types=None,
                   fields=None, user_id=None, page_size=200):
        for it in self._items:
            if it.type in (include_item_types or [it.type]):
                yield it


def _movie(item_id: str, rating: Optional[float]) -> Item:
    return Item(id=item_id, name=item_id, type="Movie", community_rating=rating)


class TestTopRatedItemIds:
    def test_ranks_by_community_rating_desc(self):
        jf = FakeJf(
            Library(name="Movies", item_id="lib1", collection_type="movies"),
            [_movie("low", 5.0), _movie("high", 9.0), _movie("mid", 7.0)],
        )
        assert top_rated_item_ids(jf, "Movies", 2, ["Movie"]) == ["high", "mid"]

    def test_unrated_items_never_picked_over_a_rated_one(self):
        jf = FakeJf(
            Library(name="Movies", item_id="lib1", collection_type="movies"),
            [_movie("unrated", None), _movie("rated", 1.0)],
        )
        assert top_rated_item_ids(jf, "Movies", 5, ["Movie"]) == ["rated"]

    def test_unknown_library_returns_empty(self):
        jf = FakeJf(Library(name="Movies", item_id="lib1", collection_type="movies"), [])
        assert top_rated_item_ids(jf, "Nope", 5, ["Movie"]) == []


def _lib(**kw) -> MovieLibrary:
    return MovieLibrary(type="movies", enabled=True, metadata=Metadata(enabled=False),
                        artwork=MovieArtwork(poster=True), **kw)


def _cfg(lib: MovieLibrary, dry_run: bool = False) -> Config:
    return Config(dry_run=dry_run, server=ServerConfig(url="http://x:8096"),
                 libraries={"Movies": lib})


def _context(cfg: Config, item_ids: List[str]):
    ctx = MagicMock()
    ctx.config = cfg
    ctx.make_jellyfin.return_value = FakeJf(
        Library(name="Movies", item_id="lib1", collection_type="movies"),
        [_movie(i, 8.0) for i in item_ids],
    )
    return ctx


class TestPosterRotationJob:
    def test_disabled_library_rotation_does_nothing(self):
        ctx = _context(_cfg(_lib(poster_rotation=PosterRotation(enabled=False))), ["a"])
        _poster_rotation_job(ctx, "Movies")
        ctx.overrides.increment_reshuffle.assert_not_called()

    def test_dry_run_skips_even_when_enabled(self):
        ctx = _context(_cfg(_lib(poster_rotation=PosterRotation(enabled=True)), dry_run=True), ["a"])
        _poster_rotation_job(ctx, "Movies")
        ctx.overrides.increment_reshuffle.assert_not_called()

    def test_artwork_poster_off_skips(self):
        lib = MovieLibrary(type="movies", enabled=True, metadata=Metadata(enabled=False),
                           artwork=MovieArtwork(poster=False),
                           poster_rotation=PosterRotation(enabled=True))
        ctx = _context(_cfg(lib), ["a"])
        _poster_rotation_job(ctx, "Movies")
        ctx.overrides.increment_reshuffle.assert_not_called()

    def test_enabling_rotation_forces_random_top_n_with_at_least_2(self):
        """Config-level invariant (_apply_poster_rotation_requirements) --
        rotation would silently do nothing on best_rated or top_n=1."""
        lib = _lib(poster_rotation=PosterRotation(enabled=True))
        assert lib.artwork_selection.preset == "random_top_n"
        assert lib.artwork_selection.top_n >= 2

    def test_reshuffles_every_picked_item_then_submits_first_and_queues_rest(self):
        lib = _lib(poster_rotation=PosterRotation(enabled=True, item_count=3))
        ctx = _context(_cfg(lib), ["a", "b", "c"])
        ctx.submit_run = None  # not used directly; patched module-level below

        from unittest.mock import patch
        with patch("finmeta.api.scheduler.submit_run", return_value=("run-1", None)) as sub:
            _poster_rotation_job(ctx, "Movies")

        assert ctx.overrides.increment_reshuffle.call_args_list == [
            (("a", "poster"),), (("b", "poster"),), (("c", "poster"),),
        ]
        sub.assert_called_once_with(ctx, "apply", item_id="a")
        assert ctx.queue_webhook_item.call_args_list == [(("b",),), (("c",),)]

    def test_run_busy_queues_the_first_item_too(self):
        lib = _lib(poster_rotation=PosterRotation(enabled=True, item_count=1))
        ctx = _context(_cfg(lib), ["a"])

        from unittest.mock import patch
        with patch("finmeta.api.scheduler.submit_run", return_value=(None, "a run is already in progress")):
            _poster_rotation_job(ctx, "Movies")

        ctx.queue_webhook_item.assert_called_once_with("a")
