"""S9g1 (K7a): wiring TV status into plan building (SPEC_KOMETA.md K7a).

Covers the integration layer around the already-unit-tested
engine.tv_status.compute_tv_status:
  - engine.plan_builder.build_sonarr_index: resolves enabled Sonarr
    instances via the injected secrets store, degrades gracefully.
  - engine.plan_builder._tv_status_for_item: matches a Jellyfin item to its
    Sonarr series by Tvdb provider id.
  - engine.plan_builder._build_tvshow_plan: the "status" overlay element
    renders the computed status, and ItemPlan.tv_status is set.
"""
from __future__ import annotations

import io
from pathlib import Path
from unittest.mock import MagicMock, patch

from PIL import Image

from finmeta.config.models import (
    Config, Metadata, MediaAppInstance, MediaAppsConfig, OverlaysConfig,
    OverlayStyle, ServerConfig, TvArtwork, TvLibrary,
)
from finmeta.engine.matching import MatchResult
from finmeta.engine.plan_builder import (
    _build_tvshow_plan, _tv_status_for_item, build_sonarr_index,
)
from finmeta.jellyfin_client.mappings import PathMapper
from finmeta.jellyfin_client.types import Item


def _poster_bytes() -> bytes:
    img = Image.new("RGB", (400, 600), (10, 20, 30))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


class _FakeSecrets:
    def __init__(self, values):
        self.values = values

    def get(self, name):
        return self.values.get(name)


def _sonarr_cfg(enabled=True, api_key="sonarr_key") -> Config:
    return Config(
        dry_run=True, server=ServerConfig(url="http://x:8096"),
        media_apps=MediaAppsConfig(sonarr=[
            MediaAppInstance(name="tv", url="http://sonarr.example.com",
                             api_key=api_key, enabled=enabled),
        ]),
    )


# ---------------------------------------------------------------------------
# build_sonarr_index
# ---------------------------------------------------------------------------

class TestBuildSonarrIndex:
    def test_no_secrets_returns_empty(self):
        assert build_sonarr_index(_sonarr_cfg(), None) == {}

    def test_disabled_instance_skipped(self):
        cfg = _sonarr_cfg(enabled=False)
        secrets = _FakeSecrets({"sonarr_key": "abc"})
        assert build_sonarr_index(cfg, secrets) == {}

    def test_missing_api_key_skipped(self):
        cfg = _sonarr_cfg()
        secrets = _FakeSecrets({})  # no key saved
        assert build_sonarr_index(cfg, secrets) == {}

    @patch("finmeta.arr.sonarr.SonarrClient")
    def test_indexes_by_tvdb_id(self, mock_cls):
        mock_client = MagicMock()
        mock_client.series.return_value = [
            {"id": 1, "tvdbId": 111, "status": "continuing"},
            {"id": 2, "tvdbId": 222, "status": "ended"},
        ]
        mock_cls.return_value = mock_client
        cfg = _sonarr_cfg()
        secrets = _FakeSecrets({"sonarr_key": "abc"})

        index = build_sonarr_index(cfg, secrets)
        assert set(index) == {"111", "222"}
        client, row = index["111"]
        assert client is mock_client
        assert row["id"] == 1

    @patch("finmeta.arr.sonarr.SonarrClient")
    def test_unreachable_instance_degrades_gracefully(self, mock_cls):
        mock_cls.side_effect = RuntimeError("connection refused")
        cfg = _sonarr_cfg()
        secrets = _FakeSecrets({"sonarr_key": "abc"})
        assert build_sonarr_index(cfg, secrets) == {}


# ---------------------------------------------------------------------------
# _tv_status_for_item
# ---------------------------------------------------------------------------

class TestTvStatusForItem:
    def _item(self, tvdb_id=None):
        pids = {"Tvdb": str(tvdb_id)} if tvdb_id else {}
        return Item(id="s1", name="Show", type="Series", path="/media/Show",
                    provider_ids=pids)

    def test_no_index_returns_none(self):
        assert _tv_status_for_item(self._item(111), None, None) is None

    def test_no_tvdb_id_returns_none(self):
        client = MagicMock()
        index = {"111": (client, {"id": 1, "status": "continuing"})}
        assert _tv_status_for_item(self._item(None), index, None) is None

    def test_tvdb_id_not_in_index_returns_none(self):
        client = MagicMock()
        index = {"111": (client, {"id": 1, "status": "continuing"})}
        assert _tv_status_for_item(self._item(999), index, None) is None

    def test_matched_item_computes_status(self):
        client = MagicMock()
        client.episodes.return_value = [
            {"seasonNumber": 1, "episodeNumber": 1, "airDateUtc": "2020-01-01T00:00:00Z",
             "hasFile": True, "monitored": True, "finaleType": None},
        ]
        index = {"111": (client, {"id": 7, "status": "ended", "tvdbId": 111})}
        out = _tv_status_for_item(self._item(111), index, None)
        assert out is not None
        assert out.status == "ended"
        client.episodes.assert_called_once_with(7)

    def test_client_error_degrades_to_none(self):
        client = MagicMock()
        client.episodes.side_effect = RuntimeError("timeout")
        index = {"111": (client, {"id": 7, "status": "ended", "tvdbId": 111})}
        assert _tv_status_for_item(self._item(111), index, None) is None


# ---------------------------------------------------------------------------
# _build_tvshow_plan: status overlay element + ItemPlan.tv_status
# ---------------------------------------------------------------------------

class _FakeJfStreams:
    def get_item_media_streams_raw(self, item_id):
        return [{"Type": "Video", "Width": 1920, "Height": 1080}]

    def get_items(self, parent_id=None, recursive=None):
        return []


def _tv_fixture(tmp_path, sonarr_index):
    media_root = tmp_path / "media"
    show_dir = media_root / "Shows" / "Test Show"
    show_dir.mkdir(parents=True, exist_ok=True)
    (show_dir / "Season 01" / "ep.mkv").parent.mkdir(parents=True, exist_ok=True)
    (show_dir / "Season 01" / "ep.mkv").write_bytes(b"video")

    assets = tmp_path / "assets"
    ad = assets / "220"
    ad.mkdir(parents=True, exist_ok=True)
    (ad / "poster.jpg").write_bytes(_poster_bytes())

    cfg = Config(
        dry_run=True,
        server=ServerConfig(url="http://x:8096"),
        libraries={"Shows": TvLibrary(
            type="tvshows",
            metadata=Metadata(enabled=False),
            artwork=TvArtwork(poster=True),
            overlays=["status"],
            overlay_profile="default",
        )},
        overlays=OverlaysConfig(
            style=OverlayStyle(position="top_left", margin=20, font_size=60),
            profiles={"default": {"elements": ["status"]}},
        ),
    )
    mapper = PathMapper.from_config([{"jellyfin": str(media_root), "local": str(media_root)}])
    item = Item(id="show220", name="Test Show", type="Series",
                path=str(show_dir), provider_ids={"Tmdb": "220", "Tvdb": "111"})
    match = MatchResult(item=item, tmdb_id=220, matched_by="provider_id")
    from finmeta.sources.local import LocalAssetSource
    sources = {"local": LocalAssetSource(assets)}
    lib = cfg.libraries["Shows"]
    return cfg, lib, mapper, item, match, sources


class TestStatusOverlayElement:
    def test_status_element_renders_and_item_plan_carries_status(self, tmp_path: Path):
        config_dir = tmp_path / "config"
        client = MagicMock()
        client.episodes.return_value = [
            {"seasonNumber": 1, "episodeNumber": 1, "airDateUtc": "2020-01-01T00:00:00Z",
             "hasFile": True, "monitored": True, "finaleType": None},
        ]
        sonarr_index = {"111": (client, {"id": 9, "status": "ended", "tvdbId": 111})}
        cfg, lib, mapper, item, match, sources = _tv_fixture(tmp_path, sonarr_index)

        plan_item = _build_tvshow_plan(
            item, match, cfg, lib, mapper, tmdb=None, jf=_FakeJfStreams(),
            sources=sources, language="en", lang_fallback=[None],
            config_dir=config_dir, sonarr_index=sonarr_index,
        )
        assert plan_item.tv_status == "ended"
        poster = [f for f in plan_item.files if f.kind == "poster"][0]
        cache_path = config_dir / "cache" / "overlay" / f"{poster.content_sha256}.jpg"
        assert cache_path.is_file()
        # Rendered bytes differ from the plain candidate (a badge was drawn).
        import hashlib
        plain_sha = hashlib.sha256(_poster_bytes()).hexdigest()
        assert poster.content_sha256 != plain_sha

    def test_no_sonarr_match_means_no_status_no_crash(self, tmp_path: Path):
        config_dir = tmp_path / "config"
        cfg, lib, mapper, item, match, sources = _tv_fixture(tmp_path, {})
        plan_item = _build_tvshow_plan(
            item, match, cfg, lib, mapper, tmdb=None, jf=_FakeJfStreams(),
            sources=sources, language="en", lang_fallback=[None],
            config_dir=config_dir, sonarr_index={},
        )
        assert plan_item.tv_status is None


# ---------------------------------------------------------------------------
# status_overlay.create_collections -> per-status ApiActions
# ---------------------------------------------------------------------------

def _plan_item(media_type, tv_status=None, item_id="x"):
    from finmeta.engine.plan import ItemPlan
    return ItemPlan(item_id=item_id, item_name=item_id, media_type=media_type,
                    tmdb_id=1, tv_status=tv_status)


class TestStatusCollections:
    def test_off_by_default_no_actions(self):
        from finmeta.engine.plan_builder import _build_api_actions
        cfg = Config(dry_run=True, server=ServerConfig(url="http://x:8096"))
        items = [_plan_item("tvshow", "returning", "s1")]
        actions = _build_api_actions(cfg, items, tmdb=MagicMock(), language="en")
        assert actions == []

    def test_groups_by_status_with_default_titlecased_name(self):
        from finmeta.config.models import StatusOverlayConfig
        from finmeta.engine.plan_builder import _build_api_actions
        cfg = Config(dry_run=True, server=ServerConfig(url="http://x:8096"),
                     status_overlay=StatusOverlayConfig(create_collections=True))
        items = [
            _plan_item("tvshow", "new_season_soon", "s1"),
            _plan_item("tvshow", "new_season_soon", "s2"),
            _plan_item("tvshow", "ended", "s3"),
            _plan_item("tvshow", None, "s4"),       # no status -> excluded
            _plan_item("movie", "new_season_soon", "m1"),  # movies never have tv_status in practice
        ]
        actions = _build_api_actions(cfg, items, tmdb=MagicMock(), language="en")
        by_name = {a.name: a for a in actions}
        assert set(by_name) == {"New Season Soon", "Ended"}
        assert set(by_name["New Season Soon"].add_ids) == {"s1", "s2"}
        assert by_name["Ended"].add_ids == ["s3"]

    def test_collection_name_override(self):
        from finmeta.config.models import StatusOverlayConfig
        from finmeta.engine.plan_builder import _build_api_actions
        cfg = Config(dry_run=True, server=ServerConfig(url="http://x:8096"),
                     status_overlay=StatusOverlayConfig(
                         create_collections=True,
                         collection_names={"ended": "📺 All Done"}))
        items = [_plan_item("tvshow", "ended", "s1")]
        actions = _build_api_actions(cfg, items, tmdb=MagicMock(), language="en")
        assert actions[0].name == "📺 All Done"
