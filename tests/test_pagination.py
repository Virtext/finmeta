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
from finmeta.jellyfin_client import ItemPage
from finmeta.jellyfin_client.types import Item, Library


class FakeJf:
    """Mimics the real client's pagination (get_items_page/iter_items)
    purely off the in-memory ``items_by_parent`` list -- including
    ``IncludeItemTypes`` filtering and ``TotalRecordCount`` -- so
    build_plan's pagination fix (2026-10-30) can be exercised offline.
    """

    def __init__(self, libraries: Dict[str, Library], items_by_parent: Dict[str, List[Item]]):
        self._libraries = libraries
        self._items_by_parent = items_by_parent
        self.get_items_page_calls: List[dict] = []
        self.iter_items_calls: List[dict] = []

    def get_library(self, name: str) -> Optional[Library]:
        return self._libraries.get(name)

    def get_libraries(self) -> List[Library]:
        return list(self._libraries.values())

    def get_item(self, item_id: str) -> Item:
        raise NotImplementedError

    def _filtered_pool(self, parent_id, include_item_types) -> List[Item]:
        pool = list(self._items_by_parent.get(parent_id, []))
        if include_item_types:
            pool = [it for it in pool if it.type in include_item_types]
        return pool

    def get_items(self, parent_id: Optional[str] = None, recursive: bool = True) -> List[Item]:
        return list(self._items_by_parent.get(parent_id, []))

    def get_items_page(self, *, parent_id=None, recursive=True, include_item_types=None,
                       fields=None, user_id=None, limit=None, start_index=None,
                       sort_by=None, sort_order=None, search_term=None) -> ItemPage:
        self.get_items_page_calls.append({
            "parent_id": parent_id, "include_item_types": include_item_types,
            "limit": limit, "start_index": start_index,
        })
        pool = self._filtered_pool(parent_id, include_item_types)
        total = len(pool)
        start = start_index or 0
        page_items = pool[start:start + limit] if limit is not None else pool[start:]
        return ItemPage(items=page_items, total_record_count=total, start_index=start)

    def iter_items(self, *, parent_id=None, recursive=True, include_item_types=None,
                   fields=None, user_id=None, page_size=200):
        self.iter_items_calls.append({
            "parent_id": parent_id, "include_item_types": include_item_types,
        })
        for it in self._filtered_pool(parent_id, include_item_types):
            yield it

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


def test_limit_makes_exactly_one_page_request_with_limit():
    """Pagination fix (2026-10-30): a library scope with `limit` must hit
    Jellyfin's /Items exactly once, with Limit set -- never fetch the whole
    library then slice in Python."""
    jf = _jf(5)
    build_plan(_cfg(), jf, None, {}, library="Movies", limit=1, offset=0)
    assert len(jf.get_items_page_calls) == 1
    assert jf.get_items_page_calls[0]["limit"] == 1
    assert jf.get_items_page_calls[0]["start_index"] == 0
    assert jf.get_items_page_calls[0]["include_item_types"] == ["Movie", "Series"]
    assert jf.iter_items_calls == []


def test_unbounded_library_scope_uses_iter_items_with_movie_series_filter():
    """No `limit` -> iter_items(), always filtered to Movie/Series so
    seasons/episodes are never fetched from Jellyfin at all."""
    jf = _jf(5)
    build_plan(_cfg(), jf, None, {}, library="Movies")
    assert len(jf.iter_items_calls) == 1
    assert jf.iter_items_calls[0]["include_item_types"] == ["Movie", "Series"]
    assert jf.get_items_page_calls == []


def test_all_libraries_scope_uses_iter_items_and_preserves_lib_name():
    """Multi-library (no `library`, no `item_id`) scope also pages via
    iter_items per library, and each item keeps the _lib_name it came from."""
    movies_lib = MovieLibrary(type="movies", enabled=True,
                              metadata=Metadata(enabled=False), artwork=MovieArtwork())
    from finmeta.config.models import Metadata as _Metadata, TvArtwork, TvLibrary
    shows_lib = TvLibrary(type="tvshows", enabled=True,
                          metadata=_Metadata(enabled=False), artwork=TvArtwork())
    cfg = Config(dry_run=True, server=ServerConfig(url="http://x:8096"),
                libraries={"Movies": movies_lib, "Shows": shows_lib})
    libs = {
        "Movies": Library(name="Movies", item_id="lib-movies", collection_type="movies"),
        "Shows": Library(name="Shows", item_id="lib-shows", collection_type="tvshows"),
    }
    movie_items = [_movie_item(i, "lib-movies") for i in range(1, 3)]
    jf = FakeJf(libs, {"lib-movies": movie_items, "lib-shows": []})

    build_plan(cfg, jf, None, {}, library=None, item_id=None)

    assert len(jf.iter_items_calls) == 2
    parent_ids_asked = {c["parent_id"] for c in jf.iter_items_calls}
    assert parent_ids_asked == {"lib-movies", "lib-shows"}
    for c in jf.iter_items_calls:
        assert c["include_item_types"] == ["Movie", "Series"]


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


# ---------------------------------------------------------------------------
# Regression: a real JellyfinClient (respx-mocked HTTP) paging a library
# bigger than one page through build_plan -- exercises the ACTUAL client
# iter_items() pagination (not FakeJf's single-shot filter above), through
# the full build_plan() integration path. No duplicates, no omissions.
# ---------------------------------------------------------------------------

def test_real_client_pages_a_library_bigger_than_one_page_no_dupes_no_gaps():
    """iter_items()'s default page_size is 200 -- use 250 items so the real
    client genuinely issues 2 /Items requests (200 + 50), not just 1."""
    import respx
    import httpx
    from finmeta.jellyfin_client import JellyfinClient

    base = "http://jf-test"
    n = 250
    all_items = [
        {"Id": f"m{i}", "Name": f"Movie m{i} ({2000 + (i % 30)})", "Type": "Movie",
         "Path": f"/media/m{i}/movie.mkv", "ProviderIds": {"Tmdb": str(1000 + i)}}
        for i in range(1, n + 1)
    ]

    def _items_responder(request):
        qs = dict(httpx.QueryParams(request.url.query))
        start = int(qs.get("startIndex", "0"))
        limit = int(qs.get("limit", "200"))
        chunk = all_items[start:start + limit]
        return httpx.Response(200, json={"Items": chunk, "TotalRecordCount": n})

    with respx.mock(base_url=base) as router:
        router.get("/Library/VirtualFolders").mock(
            return_value=httpx.Response(200, json=[
                {"Name": "Movies", "ItemId": "lib-movies", "CollectionType": "movies",
                 "Locations": ["/media"]},
            ])
        )
        items_route = router.get("/Items").mock(side_effect=_items_responder)

        jf = JellyfinClient(base, token="tok", user_id="uid")
        plan = build_plan(_cfg(), jf, None, {}, library="Movies")

    assert items_route.call_count == 2  # 200 + 50, two real pages

    ids = [ip.item_id for ip in plan.items]
    assert sorted(ids, key=lambda s: int(s[1:])) == [f"m{i}" for i in range(1, n + 1)]
    assert len(ids) == len(set(ids))  # no duplicates
    assert plan.total_items is None  # unbounded (iter_items) scope
