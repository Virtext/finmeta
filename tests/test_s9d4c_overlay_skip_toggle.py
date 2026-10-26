"""S9d4c Fase A step 4: per-item overlay on/off toggle.

Unlike other skip-kinds (S9d4b), "overlay" has no FileWrite of its own to
drop from the plan post-build -- it is baked into the poster's bytes. So
skipping it is checked at BUILD time (plan_builder._overlay_skipped, reading
overrides.db via config_dir -- same best-effort pattern as the existing
reshuffle-counter read) and makes the poster build as a PLAIN image instead
of going through the overlay compositor at all.
"""
from __future__ import annotations

import hashlib
import io

from PIL import Image

from finmeta.api.overrides import OverrideStore
from finmeta.config.models import (
    Config, Metadata, MovieArtwork, MovieLibrary, OverlaysConfig, OverlayStyle, ServerConfig,
)
from finmeta.engine.matching import MatchResult
from finmeta.engine.plan_builder import _build_movie_plan
from finmeta.jellyfin_client.mappings import PathMapper
from finmeta.jellyfin_client.types import Item, MediaStream


def _poster_bytes(color=(10, 20, 30)) -> bytes:
    img = Image.new("RGB", (40, 60), color)
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


class _FakeJfStreams:
    def get_media_streams(self, item_id):
        return [MediaStream(index=0, type="Video", width=1920, height=1080)]


def _fixture(tmp_path):
    media_root = tmp_path / "media"
    movie_dir = media_root / "Movies" / "Test Movie"
    movie_dir.mkdir(parents=True, exist_ok=True)
    (movie_dir / "m.mkv").write_bytes(b"video")

    assets = tmp_path / "assets"
    ad = assets / "155"
    ad.mkdir(parents=True, exist_ok=True)
    candidate = _poster_bytes()
    (ad / "poster.jpg").write_bytes(candidate)

    cfg = Config(
        dry_run=True,
        server=ServerConfig(url="http://x:8096"),
        libraries={"Movies": MovieLibrary(
            type="movies",
            metadata=Metadata(enabled=False),
            artwork=MovieArtwork(poster=True),
            overlays=["resolution"],
            overlay_profile="default",
        )},
        overlays=OverlaysConfig(
            style=OverlayStyle(position="top_left", margin=20, font_size=30),
            profiles={"default": {"elements": ["resolution"]}},
        ),
    )
    mapper = PathMapper.from_config([{"jellyfin": str(media_root), "local": str(media_root)}])
    item = Item(id="mov155", name="Test Movie (2020)", type="Movie",
                path=str(movie_dir / "m.mkv"), provider_ids={"Tmdb": "155"})
    match = MatchResult(item=item, tmdb_id=155, matched_by="provider_id")
    from finmeta.sources.local import LocalAssetSource
    sources = {"local": LocalAssetSource(assets)}
    lib = cfg.libraries["Movies"]
    return cfg, lib, mapper, item, match, sources, candidate


def _build(tmp_path, config_dir):
    cfg, lib, mapper, item, match, sources, candidate = _fixture(tmp_path)
    plan_item = _build_movie_plan(
        item, match, cfg, lib, mapper, tmdb=None, jf=_FakeJfStreams(),
        sources=sources, language="en", lang_fallback=[None], config_dir=config_dir)
    poster = [f for f in plan_item.files if f.kind == "poster"][0]
    return poster, candidate


def test_overlay_active_by_default_poster_bytes_differ_from_candidate(tmp_path):
    poster, candidate = _build(tmp_path, tmp_path / "config")
    assert poster.content_sha256 != hashlib.sha256(candidate).hexdigest()


def test_skipping_overlay_kind_produces_a_plain_poster(tmp_path):
    config_dir = tmp_path / "config"
    OverrideStore(config_dir / "overrides.db").set_skip("mov155", "overlay")
    poster, candidate = _build(tmp_path, config_dir)
    assert poster.content_sha256 == hashlib.sha256(candidate).hexdigest()
    assert poster.source_ref.endswith("155/poster.jpg")


def test_skip_is_per_item_not_global(tmp_path):
    """Skipping overlay for one item must not affect another."""
    config_dir = tmp_path / "config"
    OverrideStore(config_dir / "overrides.db").set_skip("some-other-item", "overlay")
    poster, candidate = _build(tmp_path, config_dir)
    assert poster.content_sha256 != hashlib.sha256(candidate).hexdigest()


def test_unskip_restores_the_overlay(tmp_path):
    config_dir = tmp_path / "config"
    store = OverrideStore(config_dir / "overrides.db")
    store.set_skip("mov155", "overlay")
    poster_skipped, candidate = _build(tmp_path, config_dir)
    assert poster_skipped.content_sha256 == hashlib.sha256(candidate).hexdigest()

    store.clear_skip("mov155", "overlay")
    poster_restored, candidate = _build(tmp_path, config_dir)
    assert poster_restored.content_sha256 != hashlib.sha256(candidate).hexdigest()


def test_no_config_dir_means_overlay_never_considered_skipped(tmp_path):
    poster, candidate = _build(tmp_path, None)
    assert poster.content_sha256 != hashlib.sha256(candidate).hexdigest()
