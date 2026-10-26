"""Tests for the overlay compositor and plan integration (SPEC \u00a78, O7).

Covers: deterministic output, dimension preservation, non-empty output,
image badges, corner positioning, and the "no stacking" guarantee that an
overlaid poster is always rendered from the original poster.
"""
from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image

from finmeta.config.models import OverlayStyle
from finmeta.engine.badges import (
    format_audio, format_hdr, format_rating, format_resolution, overlay_values,
)
from finmeta.engine.overlay import (
    ImageBadge, TextBadge, overlay_file_write, render_poster, resolve_badges, sha256,
)
from finmeta.jellyfin_client.types import MediaStream

STYLE = OverlayStyle(position="top_left", margin=20, font_size=30,
                     bg="#000000B0", fg="#FFFFFF", radius=12)


def make_poster(width: int = 500, height: int = 750,
                color=(40, 50, 90)) -> bytes:
    img = Image.new("RGB", (width, height), color)
    d = ImageDraw_box(img)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=92)
    return buf.getvalue()


def make_logo() -> bytes:
    img = Image.new("RGBA", (120, 60), (220, 40, 40, 255))
    from PIL import ImageDraw
    d = ImageDraw.Draw(img)
    d.text((20, 20), "HD", fill=(255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _solid_logo(size: tuple, rgba: tuple) -> bytes:
    img = Image.new("RGBA", size, rgba)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def ImageDraw_box(img):
    from PIL import ImageDraw
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, img.width, 120], fill=(110, 120, 160))
    return d


class TestRenderPoster:
    def test_dimensions_preserved(self):
        out = render_poster(make_poster(321, 640), [TextBadge(text="1080p")], STYLE)
        assert Image.open(io.BytesIO(out)).size == (321, 640)

    def test_output_non_empty(self):
        out = render_poster(make_poster(), [TextBadge(text="1080p")], STYLE)
        assert len(out) > 500

    def test_deterministic_hash(self):
        poster = make_poster()
        badges = [TextBadge(text="1080p"), TextBadge(text="\u2605 7.8"), ImageBadge(data=make_logo())]
        h1 = sha256(render_poster(poster, badges, STYLE))
        h2 = sha256(render_poster(poster, badges, STYLE))
        assert h1 == h2

    def test_badges_change_output(self):
        poster = make_poster()
        a = sha256(render_poster(poster, [TextBadge(text="720p")], STYLE))
        b = sha256(render_poster(poster, [TextBadge(text="1080p")], STYLE))
        assert a != b

    def test_empty_badges_reencodes(self):
        poster = make_poster()
        out = render_poster(poster, [], STYLE)
        assert Image.open(io.BytesIO(out)).size == Image.open(io.BytesIO(poster)).size

    def test_image_badge_rendered(self):
        poster = make_poster()
        plain = sha256(render_poster(poster, [], STYLE))
        with_logo = sha256(render_poster(poster, [ImageBadge(data=make_logo())], STYLE))
        assert plain != with_logo

    @pytest.mark.parametrize("pos", ["top_left", "top_right", "bottom_left", "bottom_right"])
    def test_corner_positions_vary(self, pos):
        poster = make_poster()
        base = render_poster(poster, [TextBadge(text="4K")], STYLE)
        styled = OverlayStyle(position=pos, margin=20, font_size=30,
                              bg="#000000B0", fg="#FFFFFF", radius=12)
        out = render_poster(poster, [TextBadge(text="4K")], styled)
        assert sha256(out) != sha256(base) if pos != "top_left" else True

    def test_invalid_color_raises(self):
        bad = OverlayStyle(position="top_left", margin=20, font_size=30,
                           bg="red", fg="#FFFFFF", radius=12)
        with pytest.raises(ValueError):
            render_poster(make_poster(), [TextBadge(text="x")], bad)

    def test_image_badges_stack_using_scaled_not_native_size(self):
        """S9d4c: the stack's total height (used to place the FIRST badge)
        must be computed from the SCALED size _draw_badge actually renders
        at, not the source PNG's native size. A real Kometa pack logo is
        rarely exactly font_size tall (e.g. 38px native scaled to a 120px
        font_size), and the old _badge_size returned the native size --
        under-counting the stack's true height and stranding later badges
        far from the configured margin (found via a real apply once
        S9d4c's logo-pack-fallback fix started producing ImageBadges for
        the first time)."""
        poster = make_poster(400, 600)
        short_logo = _solid_logo((200, 20), (255, 0, 0, 255))   # native h=20
        tall_logo = _solid_logo((40, 100), (0, 255, 0, 255))    # native h=100
        style = OverlayStyle(position="bottom_left", margin=10, font_size=80,
                             bg="#000000B0", fg="#FFFFFF", radius=4)
        out = render_poster(poster, [ImageBadge(data=short_logo), ImageBadge(data=tall_logo)], style)
        im = Image.open(io.BytesIO(out)).convert("RGB")

        def first_y_with_color(rgb):
            px = im.load()
            for y in range(im.height):
                for x in range(im.width):
                    p = px[x, y]
                    if abs(p[0] - rgb[0]) < 20 and abs(p[1] - rgb[1]) < 20 and abs(p[2] - rgb[2]) < 20:
                        return y
            return None

        red_y = first_y_with_color((255, 0, 0))
        green_y = first_y_with_color((0, 255, 0))
        assert red_y is not None and green_y is not None
        assert red_y < green_y  # stacking order preserved (first badge on top)
        # Both scaled to ~font_size (80) tall + padding -- the LAST badge
        # must land close to the configured margin from the poster's
        # bottom edge, not stranded far short of it (the bug's symptom:
        # native sizes 20/100 undercounted the stack vs. the true ~80/80).
        assert im.height - green_y < 140


class TestResolveBadges:
    def test_order_and_skip_none(self):
        values = {"resolution": "1080p", "hdr": None, "audio": "5.1", "rating": "\u2605 7.8"}
        badges = resolve_badges(["resolution", "hdr", "audio", "rating"], values)
        assert [b.text for b in badges] == ["1080p", "5.1", "\u2605 7.8"]

    def test_all_missing_is_empty(self):
        assert resolve_badges(["resolution", "hdr"], {"resolution": None, "hdr": None}) == []


class TestOverlayValues:
    def test_resolution(self):
        assert format_resolution(2160) == "4K"
        assert format_resolution(1080) == "1080p"
        assert format_resolution(720) == "720p"
        assert format_resolution(480) == "SD"
        assert format_resolution(None) is None

    def test_hdr(self):
        assert format_hdr("PQ", "HDR10") == "HDR10"
        assert format_hdr("PQ", "HDR10Plus") == "HDR10+"
        assert format_hdr("PQ", "DOVI") == "DV"
        assert format_hdr("HLG", "HLG") == "HLG"
        assert format_hdr("SDR", None) is None

    def test_audio(self):
        assert format_audio([MediaStream(index=1, type="Audio", codec="eac3", channels=6)]) == "DD+"
        assert format_audio([MediaStream(index=1, type="Audio", codec="truehd", channels=8)]) == "TrueHD"
        assert format_audio([MediaStream(index=1, type="Audio", codec="aac", channels=2)]) == "Stereo"
        assert format_audio([MediaStream(index=0, type="Video")]) is None

    def test_rating(self):
        assert format_rating(7.83) == "\u2605 7.8"
        assert format_rating(None) is None

    def test_overlay_values_full(self):
        streams = [
            MediaStream(index=0, type="Video", width=3840, height=2160,
                        video_range="PQ", video_range_type="HDR10"),
            MediaStream(index=1, type="Audio", codec="truehd", channels=8),
        ]
        v = overlay_values(streams, 8.1)
        assert v == {"resolution": "4K", "hdr": "HDR10", "audio": "TrueHD", "rating": "\u2605 8.1",
                     "status": None, "trending_rank": None, "network": None}

    def test_overlay_values_status_passthrough(self):
        """S9g1: status_label passes straight through (already-formatted by
        engine.tv_status.compute_tv_status)."""
        v = overlay_values([], None, status_label="NEW SEASON 12.11")
        assert v["status"] == "NEW SEASON 12.11"

    def test_overlay_values_trending_rank_formatted(self):
        """S9g2: trending_rank is formatted as "#<rank>"; 0/None -> no badge."""
        assert overlay_values([], None, trending_rank=3)["trending_rank"] == "#3"
        assert overlay_values([], None, trending_rank=None)["trending_rank"] is None

    def test_overlay_values_network_passthrough(self):
        """2026-10-23: network passes straight through (TMDb's own networks
        field, extracted by plan_builder._overlay_poster)."""
        assert overlay_values([], None, network="Netflix")["network"] == "Netflix"
        assert overlay_values([], None)["network"] is None


class TestOverlayFileWriteNoStack:
    """O7: an overlaid poster FileWrite is rendered from the original, so
    running twice does not stack badges."""

    def _build_fw(self, tmp_path: Path, config_dir: Path, candidate: bytes, ref: str):
        target = tmp_path / "media" / "Movies" / "M" / "poster.jpg"
        return overlay_file_write(
            target_path=target, candidate_bytes=candidate, candidate_ref=ref,
            badges=[TextBadge(text="1080p"), TextBadge(text="\u2605 7.8")],
            style=STYLE, config_dir=config_dir,
        )

    def test_first_run_uses_candidate(self, tmp_path: Path):
        cfg_dir = tmp_path / "config"
        candidate = make_poster()
        ref = str(tmp_path / "candidate.jpg")
        fw = self._build_fw(tmp_path, cfg_dir, candidate, ref)
        assert fw.source_ref == ref
        assert fw.content_sha256

    def test_rerun_uses_backup_no_stack(self, tmp_path: Path):
        from finmeta.engine.runs import backup
        cfg_dir = tmp_path / "config"
        candidate = make_poster()
        target = tmp_path / "media" / "Movies" / "M" / "poster.jpg"
        target.parent.mkdir(parents=True)
        target.write_bytes(candidate)
        # First render from candidate (no backup yet).
        fw1 = overlay_file_write(
            target_path=target, candidate_bytes=candidate,
            candidate_ref=str(tmp_path / "c.jpg"),
            badges=[TextBadge(text="1080p")], style=STYLE, config_dir=cfg_dir,
        )
        # A run backs up the on-disk original.
        backup("r1", target, config_dir=cfg_dir)
        # Re-render: must use the backup, giving an identical hash.
        fw2 = overlay_file_write(
            target_path=target, candidate_bytes=candidate,
            candidate_ref=str(tmp_path / "c.jpg"),
            badges=[TextBadge(text="1080p")], style=STYLE, config_dir=cfg_dir,
        )
        assert fw2.source_ref != fw1.source_ref
        assert fw2.content_sha256 == fw1.content_sha256


class TestPlanIntegration:
    """The plan builder emits an overlaid-poster FileWrite whose input is the
    original (backup if present, else the candidate) and that does not stack."""

    def _fixture(self, tmp_path: Path):
        from finmeta.config.models import (
            Config, ServerConfig, MovieLibrary, MovieArtwork, Metadata,
            OverlaysConfig, OverlayStyle,
        )
        from finmeta.engine.matching import MatchResult
        from finmeta.jellyfin_client.mappings import PathMapper
        from finmeta.jellyfin_client.types import Item, MediaStream
        from finmeta.sources.local import LocalAssetSource

        media_root = tmp_path / "media"
        movie_dir = media_root / "Movies" / "Test Movie"
        movie_dir.mkdir(parents=True)
        (movie_dir / "m.mkv").write_bytes(b"video")

        assets = tmp_path / "assets"
        ad = assets / "155"
        ad.mkdir(parents=True)
        (ad / "poster.jpg").write_bytes(make_poster())
        candidate_bytes = (ad / "poster.jpg").read_bytes()

        class FakeJf:
            def get_media_streams(self, item_id):
                return [
                    MediaStream(index=0, type="Video", width=1920, height=1080,
                                video_range="PQ", video_range_type="HDR10"),
                    MediaStream(index=1, type="Audio", codec="eac3", channels=6),
                ]

        cfg = Config(
            dry_run=True,
            server=ServerConfig(url="http://x:8096"),
            libraries={"Movies": MovieLibrary(
                type="movies",
                metadata=Metadata(enabled=False),
                artwork=MovieArtwork(poster=True),
                overlays=["resolution", "hdr", "audio", "rating"],
                overlay_profile="default",
            )},
            overlays=OverlaysConfig(
                style=OverlayStyle(position="top_left", margin=20, font_size=30),
                profiles={"default": {"elements": ["resolution", "hdr", "audio", "rating"]}},
            ),
        )
        mapper = PathMapper.from_config([
            {"jellyfin": str(media_root), "local": str(media_root)},
        ])
        item = Item(id="mov155", name="Test Movie (2020)", type="Movie",
                    path=str(movie_dir / "m.mkv"), provider_ids={"Tmdb": "155"})
        match = MatchResult(item=item, tmdb_id=155, matched_by="provider_id")
        return cfg, mapper, item, match, FakeJf(), assets, candidate_bytes, movie_dir

    def test_overlaid_poster_filewrite(self, tmp_path: Path):
        from finmeta.engine.plan_builder import _build_movie_plan
        cfg, mapper, item, match, jf, assets, candidate, movie_dir = self._fixture(tmp_path)
        from finmeta.sources.local import LocalAssetSource
        sources = {"local": LocalAssetSource(assets)}
        lib = cfg.libraries["Movies"]
        plan = _build_movie_plan(item, match, cfg, lib, mapper, tmdb=None, jf=jf,
                                 sources=sources, language="en", lang_fallback=[None],
                                 config_dir=tmp_path / "config")
        poster = [f for f in plan.files if f.kind == "poster"]
        assert len(poster) == 1
        fw = poster[0]
        # Input is the chosen candidate (no backup yet); sha is of the overlaid bytes.
        assert fw.source_ref.endswith("155/poster.jpg")
        assert fw.content_sha256 != __import__("hashlib").sha256(candidate).hexdigest()
        assert fw.target_path == str(movie_dir / "poster.jpg")

    def test_no_stack_after_backup(self, tmp_path: Path):
        from finmeta.engine.plan_builder import _build_movie_plan
        from finmeta.engine.runs import backup
        from finmeta.sources.local import LocalAssetSource
        cfg, mapper, item, match, jf, assets, candidate, movie_dir = self._fixture(tmp_path)
        sources = {"local": LocalAssetSource(assets)}
        lib = cfg.libraries["Movies"]
        cfg_dir = tmp_path / "config"

        plan1 = _build_movie_plan(item, match, cfg, lib, mapper, tmdb=None, jf=jf,
                                  sources=sources, language="en", lang_fallback=[None],
                                  config_dir=cfg_dir)
        sha1 = [f for f in plan1.files if f.kind == "poster"][0].content_sha256

        # A prior run backed up the on-disk original poster.
        target = movie_dir / "poster.jpg"
        target.write_bytes(candidate)
        backup("r1", target, config_dir=cfg_dir)

        plan2 = _build_movie_plan(item, match, cfg, lib, mapper, tmdb=None, jf=jf,
                                  sources=sources, language="en", lang_fallback=[None],
                                  config_dir=cfg_dir)
        fw2 = [f for f in plan2.files if f.kind == "poster"][0]
        assert fw2.source_ref.startswith(str(cfg_dir))
        assert fw2.content_sha256 == sha1
