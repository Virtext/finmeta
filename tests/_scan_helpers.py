"""Shared fixtures for the SPEC_S10 scanner tests (no network, no real server)."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from finmeta.config.models import MovieLibrary, TvLibrary
from finmeta.jellyfin_client.client import ItemPage
from finmeta.jellyfin_client.mappings import PathMapper
from finmeta.jellyfin_client.types import Item, Library


def mapper_for(tmp_path) -> PathMapper:
    return PathMapper.from_config([{"jellyfin": "/media", "local": str(tmp_path)}])


#: Metadata every helper item carries by default, so SC2's field checks
#: contribute nothing unless a test deliberately empties one out (same
#: principle as HEALTHY_VIDEO above).
def _metadata_defaults(overview, production_year, official_rating, genres, studios):
    return {
        "Overview": "A description." if overview is _KEEP else overview,
        "ProductionYear": 1999 if production_year is _KEEP else production_year,
        "OfficialRating": "15" if official_rating is _KEEP else official_rating,
        "Genres": ["Action"] if genres is _KEEP else (genres or []),
        "Studios": ([{"Name": "A Studio"}] if studios is _KEEP
                    else [{"Name": s} for s in (studios or [])]),
    }


class _Keep:
    """Sentinel: "leave this field at the helper default"."""


_KEEP = _Keep()


def movie(item_id: str, name: str, *, path: Optional[str] = None,
          image_tags: Optional[Dict[str, str]] = None,
          backdrops: Optional[List[str]] = None,
          provider_ids: Optional[Dict[str, str]] = None,
          media_source_count: Optional[int] = None,
          overview: Any = _KEEP, production_year: Any = _KEEP,
          official_rating: Any = _KEEP, genres: Any = _KEEP,
          studios: Any = _KEEP) -> Item:
    return Item.from_dict({
        "Id": item_id, "Name": name, "Type": "Movie",
        "Path": path if path is not None else f"/media/Movies/{name}/{name}.mkv",
        "ImageTags": image_tags or {},
        "BackdropImageTags": backdrops or [],
        "ProviderIds": provider_ids or {"Tmdb": item_id},
        "MediaSourceCount": media_source_count,
        **_metadata_defaults(overview, production_year, official_rating,
                             genres, studios),
    })


def series(item_id: str, name: str, *, path: Optional[str] = None,
           image_tags: Optional[Dict[str, str]] = None,
           backdrops: Optional[List[str]] = None,
           provider_ids: Optional[Dict[str, str]] = None,
           overview: Any = _KEEP, production_year: Any = _KEEP,
           official_rating: Any = _KEEP, genres: Any = _KEEP,
           studios: Any = _KEEP) -> Item:
    return Item.from_dict({
        "Id": item_id, "Name": name, "Type": "Series",
        "Path": path if path is not None else f"/media/Shows/{name}",
        "ImageTags": image_tags or {},
        "BackdropImageTags": backdrops or [],
        "ProviderIds": provider_ids or {"Tmdb": item_id},
        **_metadata_defaults(overview, production_year, official_rating,
                             genres, studios),
    })


def movie_library(**kw) -> MovieLibrary:
    kw.setdefault("type", "movies")
    kw.setdefault("enabled", True)
    kw.setdefault("artwork", {"poster": True})
    return MovieLibrary(**kw)


def tv_library(**kw) -> TvLibrary:
    kw.setdefault("type", "tvshows")
    kw.setdefault("enabled", True)
    kw.setdefault("artwork", {"poster": True})
    return TvLibrary(**kw)


#: A healthy 1080p SDR video stream -- the FakeJellyfin default, so SC3 only
#: fires in tests that deliberately ask for broken streams.
HEALTHY_VIDEO = {"Type": "Video", "Codec": "h264", "Width": 1920, "Height": 1080,
                 "VideoRange": "SDR", "VideoRangeType": "SDR"}


class FakeJellyfin:
    """Enough of JellyfinClient for scan_library: paging, libraries, streams.

    Records every get_items_page kwarg set so tests can assert the pass is
    bounded, type-filtered and deterministically sorted. ``streams`` maps
    item id -> raw stream dicts; the default is one healthy stream per movie
    so SC3 contributes nothing unless a test opts into brokenness.
    """

    def __init__(self, items: List[Item], *, library_name: str = "Movies",
                 library_id: str = "lib-1", exists: bool = True,
                 streams: Optional[Dict[str, List[Dict[str, Any]]]] = None,
                 fail_streams: bool = False):
        self.items = list(items)
        self.library_name = library_name
        self.library_id = library_id
        self.exists = exists
        self.streams = streams
        self.fail_streams = fail_streams
        self.calls: List[Dict[str, Any]] = []
        self.stream_calls: List[List[str]] = []

    def get_items_media_streams_raw(self, item_ids, batch_size: int = 50):
        self.stream_calls.append(list(item_ids))
        if self.fail_streams:
            raise RuntimeError("jellyfin hiccup")
        if self.streams is None:
            return {i: [dict(HEALTHY_VIDEO)] for i in item_ids}
        return {i: self.streams.get(i, []) for i in item_ids}

    def get_media_streams(self, item_id):
        raise AssertionError(
            "the scan must use the BATCHED get_items_media_streams_raw, "
            "never one request per item")

    def get_library(self, name: str) -> Optional[Library]:
        if not self.exists:
            return None
        return Library.from_dict({
            "Name": name, "ItemId": self.library_id, "CollectionType": "movies",
        })

    def get_items_page(self, **kw: Any) -> ItemPage:
        self.calls.append(kw)
        start = kw.get("start_index") or 0
        limit = kw.get("limit") or len(self.items)
        chunk = self.items[start:start + limit]
        return ItemPage(items=chunk, total_record_count=len(self.items),
                        start_index=start)


class ExplodingSource:
    """Any attribute access or call fails -- proves tier 1 queries no provider."""

    def __init__(self, name: str = "source"):
        self._name = name

    def __getattr__(self, attr: str):
        raise AssertionError(
            f"tier 1 must not touch provider {self._name!r} (attr {attr!r})")

    def __call__(self, *a: Any, **kw: Any):
        raise AssertionError(f"tier 1 must not call provider {self._name!r}")
