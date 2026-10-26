"""Library-browse pagination (2026-10-23).

Profiled: a cold single-library plan build costs ~7s/item (TMDb/fanart
network round trips), so an unbounded ``build_plan(library=...)`` does not
scale past a handful of titles -- opening a library of hundreds would block
the UI for minutes. ``limit``/``offset`` bound the expensive per-item work
to one page; ``WritePlan.total_items`` carries the full (unsliced) Movie/
Series count so the caller knows whether there are more pages.

Same FakeJf/Config pattern as test_s9d4c_library_master_switch.py: offline,
tmdb=None (provider-id matching never needs the TMDb client).
"""
from __future__ import annotations

from typing import Dict, List, Optional

from finmeta.config.models import Config, Metadata, MovieArtwork, MovieLibrary, ServerConfig
from finmeta.engine.plan_builder import build_plan
from finmeta.jellyfin_client.types import Item, Library


class FakeJf:
    def __init__(self, libraries: Dict[str, Library], items_by_parent: Dict[str, List[Item]]):
        self._libraries = libraries
        self._items_by_parent = items_by_parent

    def get_library(self, name: str) -> Optional[Library]:
        return self._libraries.get(name)

    def get_libraries(self) -> List[Library]:
        return list(self._libraries.values())

    def get_item(self, item_id: str) -> Item:
        raise NotImplementedError

    def get_items(self, parent_id: Optional[str] = None, recursive: bool = True) -> List[Item]:
        return list(self._items_by_parent.get(parent_id, []))

    def get_items_media_streams_raw(self, item_ids, batch_size=50):
        return {}

    def close(self):
        pass


def _movie_item(n: int, parent_id: str) -> Item:
    return Item(id=f"m{n}", name=f"Movie {n} ({2000 + n})", type="Movie",
                path=f"/media/m{n}/movie.mkv",
                provider_ids={"Tmdb": str(1000 + n)}, parent_id=parent_id)


def _cfg() -> Config:
    lib = MovieLibrary(type="movies", enabled=True,
                       metadata=Metadata(enabled=False), artwork=MovieArtwork())
    return Config(dry_run=True, server=ServerConfig(url="http://x:8096"),
                  libraries={"Movies": lib})


def _jf(count: int) -> FakeJf:
    libs = {"Movies": Library(name="Movies", item_id="lib-movies", collection_type="movies")}
    items = [_movie_item(i, "lib-movies") for i in range(1, count + 1)]
    return FakeJf(libs, {"lib-movies": items})


def test_no_limit_returns_everything_and_total_items_is_none():
    plan = build_plan(_cfg(), _jf(5), None, {}, library="Movies")
    assert [ip.item_id for ip in plan.items] == ["m1", "m2", "m3", "m4", "m5"]
    assert plan.total_items is None


def test_limit_slices_and_reports_total():
    plan = build_plan(_cfg(), _jf(5), None, {}, library="Movies", limit=2, offset=0)
    assert [ip.item_id for ip in plan.items] == ["m1", "m2"]
    assert plan.total_items == 5


def test_offset_moves_the_window():
    plan = build_plan(_cfg(), _jf(5), None, {}, library="Movies", limit=2, offset=2)
    assert [ip.item_id for ip in plan.items] == ["m3", "m4"]
    assert plan.total_items == 5


def test_last_partial_page():
    plan = build_plan(_cfg(), _jf(5), None, {}, library="Movies", limit=2, offset=4)
    assert [ip.item_id for ip in plan.items] == ["m5"]
    assert plan.total_items == 5


def test_offset_past_the_end_is_empty_not_an_error():
    plan = build_plan(_cfg(), _jf(5), None, {}, library="Movies", limit=2, offset=10)
    assert plan.items == []
    assert plan.total_items == 5


def test_progress_cb_called_once_per_item_with_running_total():
    """A plan/apply run used to produce exactly ONE log line ("started")
    until it finished, however long that took, with no visibility into how
    far along a large library-wide run actually was -- looked
    indistinguishable from a hung job (reported live). progress_cb is the
    hook the job-log progress lines (api/run_service.py's
    _progress_logger) are built on."""
    seen = []
    build_plan(_cfg(), _jf(5), None, {}, library="Movies",
              progress_cb=lambda done, total: seen.append((done, total)))
    assert seen == [(1, 5), (2, 5), (3, 5), (4, 5), (5, 5)]


def test_item_scope_ignores_pagination_params():
    """item_id scope has exactly one item regardless of limit/offset -- make
    sure passing them through doesn't change that (they must only apply to
    the single-library browse scope)."""
    libs = {"Movies": Library(name="Movies", item_id="lib-movies", collection_type="movies",
                              locations=["/media"])}
    item = _movie_item(1, "lib-movies")
    jf = FakeJf(libs, {"lib-movies": [item]})
    jf.get_item = lambda item_id: item  # type: ignore[method-assign]
    plan = build_plan(_cfg(), jf, None, {}, item_id="m1", limit=1, offset=5)
    assert [ip.item_id for ip in plan.items] == ["m1"]
    assert plan.total_items is None
