"""Network overlay badge (2026-10-23): wiring TMDb's own `networks` field
into a real _build_tvshow_plan call. Same fixture pattern as
test_s9g1_plan_integration.py's TestStatusOverlayElement.
"""
from __future__ import annotations

import hashlib
import io
from pathlib import Path

from PIL import Image

from finmeta.config.models import (
    Config, Metadata, OverlaysConfig, OverlayStyle, ServerConfig, TvArtwork, TvLibrary,
)
from finmeta.engine.matching import MatchResult
from finmeta.engine.plan_builder import _build_tvshow_plan
from finmeta.jellyfin_client.mappings import PathMapper
from finmeta.jellyfin_client.types import Item


def _poster_bytes() -> bytes:
    img = Image.new("RGB", (400, 600), (10, 20, 30))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


class _FakeJfStreams:
    def get_item_media_streams_raw(self, item_id):
        return [{"Type": "Video", "Width": 1920, "Height": 1080}]

    def get_items(self, parent_id=None, recursive=None):
        return []


class _FakeTmdb:
    """tv_details returns a fixed `networks` list; nothing else is called."""
    def __init__(self, networks):
        self._networks = networks

    def tv_details(self, tmdb_id, language=None):
        return {"vote_average": 7.5, "networks": self._networks}


def _tv_fixture(tmp_path: Path, elements):
    media_root = tmp_path / "media"
    show_dir = media_root / "Shows" / "Test Show"
    (show_dir / "Season 01").mkdir(parents=True, exist_ok=True)
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
            overlays=elements,
            overlay_profile="default",
        )},
        overlays=OverlaysConfig(
            style=OverlayStyle(position="top_left", margin=20, font_size=60),
            profiles={"default": {"elements": elements}},
        ),
    )
    mapper = PathMapper.from_config([{"jellyfin": str(media_root), "local": str(media_root)}])
    item = Item(id="show220", name="Test Show", type="Series",
               path=str(show_dir), provider_ids={"Tmdb": "220"})
    match = MatchResult(item=item, tmdb_id=220, matched_by="provider_id")
    from finmeta.sources.local import LocalAssetSource
    sources = {"local": LocalAssetSource(assets)}
    lib = cfg.libraries["Shows"]
    return cfg, lib, mapper, item, match, sources


class TestNetworkOverlayElement:
    def test_network_badge_renders_from_tmdb_networks_field(self, tmp_path: Path):
        cfg, lib, mapper, item, match, sources = _tv_fixture(tmp_path, ["network"])
        plan_item = _build_tvshow_plan(
            item, match, cfg, lib, mapper,
            tmdb=_FakeTmdb([{"name": "Netflix"}, {"name": "Second Network"}]),
            jf=_FakeJfStreams(), sources=sources, language="en", lang_fallback=[None],
            config_dir=tmp_path / "config",
        )
        poster = [f for f in plan_item.files if f.kind == "poster"][0]
        plain_sha = hashlib.sha256(_poster_bytes()).hexdigest()
        assert poster.content_sha256 != plain_sha  # a badge was drawn

    def test_first_network_wins_when_several_are_listed(self, tmp_path: Path, monkeypatch):
        """Kometa's own convention: the FIRST entry in TMDb's networks list,
        not e.g. the last or all of them. Verified by capturing the real
        overlay_values() call _build_tvshow_plan makes, not by re-deriving
        the extraction logic in the test."""
        import finmeta.engine.plan_builder as pb
        captured = {}
        real_overlay_values = pb.overlay_values

        def _spy(*args, **kwargs):
            captured["network"] = kwargs.get("network")
            return real_overlay_values(*args, **kwargs)

        monkeypatch.setattr(pb, "overlay_values", _spy)
        cfg, lib, mapper, item, match, sources = _tv_fixture(tmp_path, ["network"])
        _build_tvshow_plan(
            item, match, cfg, lib, mapper,
            tmdb=_FakeTmdb([{"name": "Netflix"}, {"name": "Hulu"}]),
            jf=_FakeJfStreams(), sources=sources, language="en", lang_fallback=[None],
            config_dir=tmp_path / "config",
        )
        assert captured["network"] == "Netflix"

    def test_no_networks_means_no_badge_value_no_crash(self, tmp_path: Path, monkeypatch):
        """An empty `networks` list must produce network=None (-> "missing
        data -> no badge", SPEC §8), not raise an IndexError on networks[0]."""
        import finmeta.engine.plan_builder as pb
        captured = {}
        real_overlay_values = pb.overlay_values

        def _spy(*args, **kwargs):
            captured["network"] = kwargs.get("network")
            return real_overlay_values(*args, **kwargs)

        monkeypatch.setattr(pb, "overlay_values", _spy)
        cfg, lib, mapper, item, match, sources = _tv_fixture(tmp_path, ["network"])
        plan_item = _build_tvshow_plan(
            item, match, cfg, lib, mapper, tmdb=_FakeTmdb([]),
            jf=_FakeJfStreams(), sources=sources, language="en", lang_fallback=[None],
            config_dir=tmp_path / "config",
        )
        assert captured["network"] is None
        assert any(f.kind == "poster" for f in plan_item.files)  # still applied, no crash

    def test_network_not_in_elements_never_fetches_tmdb_details(self, tmp_path: Path):
        """A library/profile that doesn't use "network" (and has metadata off
        and doesn't use "rating" either) must not pay for a TMDb details call
        it has no use for."""
        class _ExplodingTmdb:
            def tv_details(self, *a, **kw):
                raise AssertionError("tv_details should not have been called")

        cfg, lib, mapper, item, match, sources = _tv_fixture(tmp_path, ["resolution"])
        plan_item = _build_tvshow_plan(
            item, match, cfg, lib, mapper, tmdb=_ExplodingTmdb(),
            jf=_FakeJfStreams(), sources=sources, language="en", lang_fallback=[None],
            config_dir=tmp_path / "config",
        )
        assert plan_item.tmdb_id == 220  # got this far without raising
