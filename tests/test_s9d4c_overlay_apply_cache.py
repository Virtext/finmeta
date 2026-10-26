"""S9d4c Fase A step 4: fix the overlay apply bug found while building the
per-element overlay UI.

``engine.overlay.overlay_file_write`` renders the overlaid poster bytes only
to hash them into the plan (SPEC §10 — the plan carries metadata, never raw
bytes) and then threw them away. ``FileWrite.source_ref`` always points at
the PLAIN original poster (needed for O7's "never re-render from an
already-overlaid poster" rule), so at apply time ``executor._fetch_content``
fetched the plain bytes, whose SHA never matches ``content_sha256`` (the
overlaid hash) -- the write was refused as a "hash mismatch" every single
time. No end-to-end apply test had exercised this yet (see CLAUDE.md Fase A
step 6).

Fix: ``overlay_file_write`` also caches the overlaid bytes on disk by their
own SHA-256 (``config_dir/cache/overlay/<sha>.jpg``); ``_fetch_content``
checks that cache first. These tests exercise the real bug scenario.
"""
from __future__ import annotations

import hashlib
import io
from pathlib import Path
from unittest.mock import MagicMock

from PIL import Image

from finmeta.config.models import (
    Config, Metadata, MovieArtwork, MovieLibrary, OverlaysConfig, OverlayStyle, ServerConfig,
)
from finmeta.engine.executor import apply_plan
from finmeta.engine.matching import MatchResult
from finmeta.engine.overlay import (
    TextBadge, overlay_file_write, read_overlay_cache,
)
from finmeta.engine.plan import WritePlan
from finmeta.engine.plan_builder import _build_movie_plan
from finmeta.jellyfin_client.mappings import PathMapper
from finmeta.jellyfin_client.types import Item, MediaStream


def _poster_bytes(color=(10, 20, 30)) -> bytes:
    img = Image.new("RGB", (40, 60), color)
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


def test_overlay_cache_round_trip(tmp_path):
    fw = overlay_file_write(
        target_path=tmp_path / "poster.jpg",
        candidate_bytes=_poster_bytes(), candidate_ref="/src/poster.jpg",
        badges=[TextBadge(text="1080p")], style=OverlayStyle(),
        config_dir=tmp_path / "config",
    )
    cached = read_overlay_cache(fw.content_sha256, config_dir=tmp_path / "config")
    assert cached is not None
    assert hashlib.sha256(cached).hexdigest() == fw.content_sha256
    # Not the plain candidate bytes -- the badge was actually baked in.
    assert cached != _poster_bytes()


def test_overlay_cache_miss_without_config_dir():
    fw = overlay_file_write(
        target_path=Path("/x/poster.jpg"),
        candidate_bytes=_poster_bytes(), candidate_ref="/src/poster.jpg",
        badges=[TextBadge(text="1080p")], style=OverlayStyle(),
        config_dir=None,
    )
    assert read_overlay_cache(fw.content_sha256, config_dir=None) is None


class _FakeJfStreams:
    """Minimal jf for _build_movie_plan: MediaStreams only."""

    def get_media_streams(self, item_id):
        return [MediaStream(index=0, type="Video", width=1920, height=1080)]


def test_overlay_apply_writes_the_exact_overlaid_bytes_plan_hashed(tmp_path):
    """The real bug, end to end: build a plan with an active overlay, apply
    it, and confirm the bytes landing on disk are the OVERLAID bytes the
    plan hashed -- not refused as a hash mismatch, and not the plain
    candidate re-written verbatim."""
    media_root = tmp_path / "media"
    movie_dir = media_root / "Movies" / "Test Movie"
    movie_dir.mkdir(parents=True)
    (movie_dir / "m.mkv").write_bytes(b"video")

    assets = tmp_path / "assets"
    ad = assets / "155"
    ad.mkdir(parents=True)
    candidate = _poster_bytes()
    (ad / "poster.jpg").write_bytes(candidate)

    cfg_dir = tmp_path / "config"
    cfg = Config(
        dry_run=False,
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

    plan_item = _build_movie_plan(
        item, match, cfg, lib, mapper, tmdb=None, jf=_FakeJfStreams(),
        sources=sources, language="en", lang_fallback=[None], config_dir=cfg_dir)
    poster_fw = [f for f in plan_item.files if f.kind == "poster"]
    assert len(poster_fw) == 1
    poster_fw = poster_fw[0]

    plan = WritePlan(items=[plan_item])
    jf, tmdb = MagicMock(), MagicMock()
    result = apply_plan(plan, "r1", cfg, jf, tmdb,
                        config_dir=cfg_dir, db_path=cfg_dir / "jobs.db", poll_timeout=0.1)

    assert result.errors == []
    assert result.files_written == 1
    written = (movie_dir / "poster.jpg").read_bytes()
    assert hashlib.sha256(written).hexdigest() == poster_fw.content_sha256
    assert written != candidate  # the overlay was actually baked in, not skipped
