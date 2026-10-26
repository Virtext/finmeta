"""S9d4c: the global Kometa-style logo pack (overlays.logo_packs) must also
apply for real, not just in the Overlays-tab preview.

Found while the operator tested Fase A step 6's fix live: "Kometa" logos
showed in the Overlays-tab preview (which reads overlays.logo_packs via
engine.overlay_render.load_pack_image_bytes) but never on an actual apply,
because plan_builder._overlay_poster's pack-substitution only ever
consulted the PER-PROFILE pack.yml system (overlays.profiles.<name>.packs,
engine.image_pack_resolve.resolve_profile_packs) -- a completely separate,
unconfigured-by-default feature with its own file layout
(config/assets/packs/<name>/pack.yml) that has nothing to do with the
global "Kometa installed" flat-file logos under config/assets/logos/<pack>/.

Fix: when the per-profile system has no image for an element,
_overlay_poster now falls back to the SAME load_pack_image_bytes the
preview uses, keyed by the K9-detected key (res_1080p, aac, hdr10, ...) for
resolution/hdr/audio, and "tmdb" for rating (badges.overlay_values' "rating"
is always TMDb vote_average).
"""
from __future__ import annotations

import hashlib
import io

from PIL import Image

from finmeta.config.models import (
    Config, Metadata, MovieArtwork, MovieLibrary, OverlaysConfig, OverlayStyle, ServerConfig,
)
from finmeta.engine.matching import MatchResult
from finmeta.engine.plan_builder import _build_movie_plan
from finmeta.jellyfin_client.mappings import PathMapper
from finmeta.jellyfin_client.types import Item

LOGO_COLOR = (255, 170, 0, 255)


def _poster_bytes(color=(10, 20, 30)) -> bytes:
    img = Image.new("RGB", (400, 600), color)
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


def _write_kometa_logo(config_dir, key: str) -> None:
    d = config_dir / "assets" / "logos" / "kometa"
    d.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGBA", (200, 60), LOGO_COLOR)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    (d / f"{key}.png").write_bytes(buf.getvalue())


class _FakeJfStreams:
    def get_item_media_streams_raw(self, item_id):
        return [{"Type": "Video", "Width": 1920, "Height": 1080}]


def _fixture(tmp_path, logo_packs):
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
            overlays=["resolution"],
            overlay_profile="default",
        )},
        overlays=OverlaysConfig(
            style=OverlayStyle(position="top_left", margin=20, font_size=60),
            profiles={"default": {"elements": ["resolution"]}},
            logo_packs=logo_packs,
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


def _render_poster(tmp_path, logo_packs):
    config_dir = tmp_path / "config"
    cfg, lib, mapper, item, match, sources = _fixture(tmp_path, logo_packs)
    plan_item = _build_movie_plan(
        item, match, cfg, lib, mapper, tmdb=None, jf=_FakeJfStreams(),
        sources=sources, language="en", lang_fallback=[None], config_dir=config_dir)
    poster = [f for f in plan_item.files if f.kind == "poster"][0]
    cache_path = config_dir / "cache" / "overlay" / f"{poster.content_sha256}.jpg"
    return poster, Image.open(cache_path).convert("RGB")


def _orange_pixel_count(im) -> int:
    return sum(
        1 for px in im.get_flattened_data()
        if abs(px[0] - LOGO_COLOR[0]) < 30 and abs(px[1] - LOGO_COLOR[1]) < 30
        and abs(px[2] - LOGO_COLOR[2]) < 30
    )


def test_global_logo_pack_used_when_profile_has_no_packs(tmp_path):
    """No overlays.profiles.<name>.packs configured at all (the real-world
    default) -- the global "kometa" pack must still supply the logo."""
    config_dir = tmp_path / "config"
    _write_kometa_logo(config_dir, "res_1080p")
    poster, rendered = _render_poster(tmp_path, logo_packs=["kometa"])
    assert _orange_pixel_count(rendered) > 200


def test_missing_logo_file_falls_back_to_text(tmp_path):
    """logo_packs set, but no res_1080p.png installed -- text badge, no crash."""
    config_dir = tmp_path / "config"
    poster, rendered = _render_poster(tmp_path, logo_packs=["kometa"])
    assert _orange_pixel_count(rendered) == 0
    # Still composited (not a plain re-encode) -- a text badge was drawn.
    plain = hashlib.sha256(_poster_bytes()).hexdigest()
    assert poster.content_sha256 != plain
