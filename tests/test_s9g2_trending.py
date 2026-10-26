"""S9g2 (K7b): MDBList trending collections + TOP 10 rank overlay.

Covers:
  - engine.plan_builder.build_trending_data: fetches configured lists via
    the injected secrets store, degrades gracefully.
  - _build_api_actions: per-list trending collections, matched by tmdb_id
    against plan_items (movies and shows).
  - the trending_rank overlay element renders through the same path as
    resolution/hdr/audio/rating/status (badges.overlay_values).
"""
from __future__ import annotations

import io
from pathlib import Path
from unittest.mock import MagicMock, patch

from PIL import Image

from finmeta.config.models import (
    Config, Metadata, MdblistSource, MovieArtwork, MovieLibrary, OverlaysConfig,
    OverlayStyle, ServerConfig, SourcesConfig, TrendingConfig, TrendingListConfig,
)
from finmeta.engine.matching import MatchResult
from finmeta.engine.plan import ItemPlan
from finmeta.engine.plan_builder import _build_api_actions, _build_movie_plan, build_trending_data
from finmeta.jellyfin_client.mappings import PathMapper
from finmeta.jellyfin_client.types import Item
from finmeta.sources.mdblist_lists import MdblistListItem


class _FakeSecrets:
    def __init__(self, values):
        self.values = values

    def get(self, name):
        return self.values.get(name)


LIST_URL = "https://mdblist.com/lists/someuser/trending-movies"


def _trending_cfg(lists, mdblist_enabled=True) -> Config:
    return Config(
        dry_run=True, server=ServerConfig(url="http://x:8096"),
        sources=SourcesConfig(mdblist=MdblistSource(enabled=mdblist_enabled)),
        trending=TrendingConfig(lists=lists),
    )


# ---------------------------------------------------------------------------
# build_trending_data
# ---------------------------------------------------------------------------

class TestBuildTrendingData:
    def test_no_secrets_returns_empty(self):
        cfg = _trending_cfg([TrendingListConfig(url=LIST_URL, collection_name="Trending")])
        ranks, fetched = build_trending_data(cfg, None)
        assert ranks == {} and fetched == []

    def test_no_lists_configured_returns_empty(self):
        cfg = _trending_cfg([])
        ranks, fetched = build_trending_data(cfg, _FakeSecrets({"mdblist_key": "k"}))
        assert ranks == {} and fetched == []

    def test_mdblist_disabled_returns_empty(self):
        cfg = _trending_cfg([TrendingListConfig(url=LIST_URL, collection_name="Trending")],
                            mdblist_enabled=False)
        ranks, fetched = build_trending_data(cfg, _FakeSecrets({"mdblist_key": "k"}))
        assert ranks == {} and fetched == []

    def test_no_saved_key_returns_empty(self):
        cfg = _trending_cfg([TrendingListConfig(url=LIST_URL, collection_name="Trending")])
        ranks, fetched = build_trending_data(cfg, _FakeSecrets({}))
        assert ranks == {} and fetched == []

    @patch("finmeta.sources.mdblist_lists.fetch_list_items")
    def test_fetches_and_builds_rank_index(self, mock_fetch):
        mock_fetch.return_value = [
            MdblistListItem(rank=1, title="A", imdb_id=None, tmdb_id=10, media_type="movie"),
            MdblistListItem(rank=2, title="B", imdb_id=None, tmdb_id=20, media_type="movie"),
        ]
        cfg = _trending_cfg([TrendingListConfig(
            url=LIST_URL, collection_name="Trending", rank_overlay=True)])
        ranks, fetched = build_trending_data(cfg, _FakeSecrets({"mdblist_key": "k"}))
        assert ranks == {10: 1, 20: 2}
        assert len(fetched) == 1
        lst, items = fetched[0]
        assert lst.collection_name == "Trending"
        assert len(items) == 2

    @patch("finmeta.sources.mdblist_lists.fetch_list_items")
    def test_rank_overlay_off_excludes_from_rank_index_but_still_fetched(self, mock_fetch):
        mock_fetch.return_value = [
            MdblistListItem(rank=1, title="A", imdb_id=None, tmdb_id=10, media_type="movie"),
        ]
        cfg = _trending_cfg([TrendingListConfig(
            url=LIST_URL, collection_name="Trending", rank_overlay=False)])
        ranks, fetched = build_trending_data(cfg, _FakeSecrets({"mdblist_key": "k"}))
        assert ranks == {}
        assert len(fetched) == 1

    @patch("finmeta.sources.mdblist_lists.fetch_list_items")
    def test_rank_above_10_excluded_from_rank_index(self, mock_fetch):
        mock_fetch.return_value = [
            MdblistListItem(rank=11, title="A", imdb_id=None, tmdb_id=10, media_type="movie"),
        ]
        cfg = _trending_cfg([TrendingListConfig(
            url=LIST_URL, collection_name="Trending", rank_overlay=True)])
        ranks, fetched = build_trending_data(cfg, _FakeSecrets({"mdblist_key": "k"}))
        assert ranks == {}

    @patch("finmeta.sources.mdblist_lists.fetch_list_items")
    def test_first_list_wins_a_shared_tmdb_id(self, mock_fetch):
        mock_fetch.side_effect = [
            [MdblistListItem(rank=1, title="A", imdb_id=None, tmdb_id=10, media_type="movie")],
            [MdblistListItem(rank=5, title="A", imdb_id=None, tmdb_id=10, media_type="movie")],
        ]
        cfg = _trending_cfg([
            TrendingListConfig(url=LIST_URL, collection_name="L1", rank_overlay=True),
            TrendingListConfig(url=LIST_URL + "-2", collection_name="L2", rank_overlay=True),
        ])
        ranks, fetched = build_trending_data(cfg, _FakeSecrets({"mdblist_key": "k"}))
        assert ranks == {10: 1}

    @patch("finmeta.sources.mdblist_lists.fetch_list_items")
    def test_one_list_failing_does_not_break_the_others(self, mock_fetch):
        mock_fetch.side_effect = [
            RuntimeError("timeout"),
            [MdblistListItem(rank=1, title="A", imdb_id=None, tmdb_id=10, media_type="movie")],
        ]
        cfg = _trending_cfg([
            TrendingListConfig(url=LIST_URL, collection_name="L1", rank_overlay=True),
            TrendingListConfig(url=LIST_URL + "-2", collection_name="L2", rank_overlay=True),
        ])
        ranks, fetched = build_trending_data(cfg, _FakeSecrets({"mdblist_key": "k"}))
        assert ranks == {10: 1}
        assert len(fetched) == 1


# ---------------------------------------------------------------------------
# _build_api_actions: trending collections
# ---------------------------------------------------------------------------

def _plan_item(tmdb_id, item_id, media_type="movie"):
    return ItemPlan(item_id=item_id, item_name=item_id, media_type=media_type, tmdb_id=tmdb_id)


class TestTrendingCollections:
    def test_matches_movies_and_shows_by_tmdb_id(self):
        cfg = _trending_cfg([TrendingListConfig(url=LIST_URL, collection_name="Trending Now")])
        items = [
            _plan_item(10, "m1", "movie"),
            _plan_item(20, "s1", "tvshow"),
            _plan_item(99, "m2", "movie"),  # not in the list -- excluded
        ]
        fetched = [(cfg.trending.lists[0], [
            MdblistListItem(rank=1, title="A", imdb_id=None, tmdb_id=10, media_type="movie"),
            MdblistListItem(rank=2, title="B", imdb_id=None, tmdb_id=20, media_type="show"),
        ])]
        actions = _build_api_actions(cfg, items, tmdb=MagicMock(), language="en",
                                     trending_fetched=fetched)
        assert len(actions) == 1
        assert actions[0].name == "Trending Now"
        assert set(actions[0].add_ids) == {"m1", "s1"}

    def test_no_matches_produces_no_action(self):
        cfg = _trending_cfg([TrendingListConfig(url=LIST_URL, collection_name="Trending Now")])
        items = [_plan_item(99, "m1")]
        fetched = [(cfg.trending.lists[0], [
            MdblistListItem(rank=1, title="A", imdb_id=None, tmdb_id=10, media_type="movie"),
        ])]
        actions = _build_api_actions(cfg, items, tmdb=MagicMock(), language="en",
                                     trending_fetched=fetched)
        assert actions == []

    def test_no_trending_fetched_is_a_noop(self):
        cfg = _trending_cfg([])
        items = [_plan_item(10, "m1")]
        actions = _build_api_actions(cfg, items, tmdb=MagicMock(), language="en",
                                     trending_fetched=None)
        assert actions == []


# ---------------------------------------------------------------------------
# trending_rank overlay element: end-to-end render
# ---------------------------------------------------------------------------

def _poster_bytes() -> bytes:
    img = Image.new("RGB", (400, 600), (10, 20, 30))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


def _movie_fixture(tmp_path):
    media_root = tmp_path / "media"
    movie_dir = media_root / "Movies" / "Test Movie"
    movie_dir.mkdir(parents=True, exist_ok=True)
    (movie_dir / "m.mkv").write_bytes(b"video")

    assets = tmp_path / "assets"
    ad = assets / "155"
    ad.mkdir(parents=True, exist_ok=True)
    (ad / "poster.jpg").write_bytes(_poster_bytes())

    cfg = Config(
        dry_run=True,
        server=ServerConfig(url="http://x:8096"),
        libraries={"Movies": MovieLibrary(
            type="movies",
            metadata=Metadata(enabled=False),
            artwork=MovieArtwork(poster=True),
            overlays=["trending_rank"],
            overlay_profile="default",
        )},
        overlays=OverlaysConfig(
            style=OverlayStyle(position="top_left", margin=20, font_size=60),
            profiles={"default": {"elements": ["trending_rank"]}},
        ),
    )
    mapper = PathMapper.from_config([{"jellyfin": str(media_root), "local": str(media_root)}])
    item = Item(id="mov155", name="Test Movie (2020)", type="Movie",
                path=str(movie_dir / "m.mkv"), provider_ids={"Tmdb": "155"})
    match = MatchResult(item=item, tmdb_id=155, matched_by="provider_id")
    from finmeta.sources.local import LocalAssetSource
    sources = {"local": LocalAssetSource(assets)}
    lib = cfg.libraries["Movies"]
    return cfg, lib, mapper, item, match, sources


class _FakeJfStreams:
    def get_item_media_streams_raw(self, item_id):
        return [{"Type": "Video", "Width": 1920, "Height": 1080}]


class TestTrendingRankOverlayElement:
    def test_renders_and_differs_from_no_rank(self, tmp_path: Path):
        config_dir = tmp_path / "config"
        cfg, lib, mapper, item, match, sources = _movie_fixture(tmp_path)
        jf = _FakeJfStreams()

        plan_item = _build_movie_plan(
            item, match, cfg, lib, mapper, tmdb=None, jf=jf,
            sources=sources, language="en", lang_fallback=[None],
            config_dir=config_dir, trending_index={155: 3},
        )
        poster = [f for f in plan_item.files if f.kind == "poster"][0]

        plan_item_no_rank = _build_movie_plan(
            item, match, cfg, lib, mapper, tmdb=None, jf=jf,
            sources=sources, language="en", lang_fallback=[None],
            config_dir=config_dir / "nodir", trending_index=None,
        )
        poster_no_rank = [f for f in plan_item_no_rank.files if f.kind == "poster"][0]

        assert poster.content_sha256 != poster_no_rank.content_sha256
