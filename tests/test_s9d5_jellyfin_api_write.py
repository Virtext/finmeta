"""S9d5 decision point (CLAUDE.md §7.1): artwork_write_method = "jellyfin_api".

Operator-approved alternative to the v1-default "sidecar" write path: upload
images straight to Jellyfin (POST /Items/{id}/Images/{type}) instead of
writing poster.jpg/etc. into the media folder. Covers:

  - engine.jellyfin_originals: capture-once-then-reuse of the pre-Finmeta
    original (the jellyfin_api-mode equivalent of find_original_backup).
  - engine.diff.diff_file: jellyfin_api FileWrites always report "update"
    (no local file to diff against).
  - engine.plan_builder: global + per-library artwork_write_method produces
    FileWrites with the right target_path/write_method; metadata/theme_song
    stay sidecar regardless.
  - engine.executor.apply_plan: uploads via JellyfinClient.upload_image
    instead of touching the filesystem, no backup() call, manifest recorded.
"""
from __future__ import annotations

import hashlib
import io
from pathlib import Path
from unittest.mock import MagicMock

from PIL import Image

from finmeta.config.models import (
    Config, Metadata, MovieArtwork, MovieLibrary, ServerConfig,
)
from finmeta.engine.diff import diff_file
from finmeta.engine.executor import apply_plan
from finmeta.engine.jellyfin_originals import get_or_fetch_original, original_cache_path
from finmeta.engine.matching import MatchResult
from finmeta.engine.plan import FileWrite, ItemPlan, WritePlan
from finmeta.engine.plan_builder import _build_movie_plan, _effective_write_method
from finmeta.jellyfin_client.mappings import PathMapper
from finmeta.jellyfin_client.types import Item


def _poster_bytes(color=(10, 20, 30)) -> bytes:
    img = Image.new("RGB", (400, 600), color)
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# engine.jellyfin_originals
# ---------------------------------------------------------------------------

class TestGetOrFetchOriginal:
    def test_caches_jellyfin_image_on_first_call(self, tmp_path: Path):
        jf = MagicMock()
        jf.get_image_bytes.return_value = b"ORIGINAL BYTES"
        out = get_or_fetch_original(jf, "mov1", "poster", tmp_path)
        assert out == b"ORIGINAL BYTES"
        jf.get_image_bytes.assert_called_once_with("mov1", "Primary")
        assert original_cache_path(tmp_path, "mov1", "poster").read_bytes() == b"ORIGINAL BYTES"

    def test_second_call_uses_cache_not_jellyfin(self, tmp_path: Path):
        jf = MagicMock()
        jf.get_image_bytes.return_value = b"ORIGINAL BYTES"
        get_or_fetch_original(jf, "mov1", "poster", tmp_path)
        jf.get_image_bytes.reset_mock()
        out = get_or_fetch_original(jf, "mov1", "poster", tmp_path)
        assert out == b"ORIGINAL BYTES"
        jf.get_image_bytes.assert_not_called()

    def test_no_existing_jellyfin_image_returns_none_and_caches_nothing(self, tmp_path: Path):
        jf = MagicMock()
        jf.get_image_bytes.return_value = None
        out = get_or_fetch_original(jf, "mov1", "poster", tmp_path)
        assert out is None
        assert not original_cache_path(tmp_path, "mov1", "poster").is_file()

    def test_unknown_kind_returns_none(self, tmp_path: Path):
        jf = MagicMock()
        assert get_or_fetch_original(jf, "mov1", "metadata", tmp_path) is None
        jf.get_image_bytes.assert_not_called()

    def test_logo_kind_maps_to_logo_image_type_and_png_ext(self, tmp_path: Path):
        jf = MagicMock()
        jf.get_image_bytes.return_value = b"LOGO"
        get_or_fetch_original(jf, "mov1", "logo", tmp_path)
        jf.get_image_bytes.assert_called_once_with("mov1", "Logo")
        assert original_cache_path(tmp_path, "mov1", "logo").suffix == ".png"


# ---------------------------------------------------------------------------
# engine.diff
# ---------------------------------------------------------------------------

class TestDiffFileJellyfinApi:
    def test_always_update_regardless_of_disk(self, tmp_path: Path):
        fw = FileWrite(
            target_path="jellyfin-api://mov1/poster",
            kind="poster", source_ref="x", content_sha256="a" * 64,
            write_method="jellyfin_api",
        )
        entry = diff_file(fw)
        assert entry.status == "update"

    def test_sidecar_unaffected(self, tmp_path: Path):
        target = tmp_path / "poster.jpg"
        fw = FileWrite(
            target_path=str(target), kind="poster",
            source_ref="x", content_sha256="a" * 64,
        )
        entry = diff_file(fw)
        assert entry.status == "create"  # file doesn't exist on disk


# ---------------------------------------------------------------------------
# engine.plan_builder: effective write method + plan-build wiring
# ---------------------------------------------------------------------------

class TestEffectiveWriteMethod:
    def test_global_default_sidecar(self):
        cfg = Config(dry_run=True, server=ServerConfig(url="http://x:8096"))
        lib = MovieLibrary(type="movies")
        assert _effective_write_method(cfg, lib) == "sidecar"

    def test_global_jellyfin_api(self):
        cfg = Config(dry_run=True, server=ServerConfig(url="http://x:8096"),
                     artwork_write_method="jellyfin_api")
        lib = MovieLibrary(type="movies")
        assert _effective_write_method(cfg, lib) == "jellyfin_api"

    def test_per_library_override_wins_over_global(self):
        cfg = Config(dry_run=True, server=ServerConfig(url="http://x:8096"),
                     artwork_write_method="sidecar")
        lib = MovieLibrary(type="movies", artwork_write_method="jellyfin_api")
        assert _effective_write_method(cfg, lib) == "jellyfin_api"

    def test_per_library_unset_inherits_global(self):
        cfg = Config(dry_run=True, server=ServerConfig(url="http://x:8096"),
                     artwork_write_method="jellyfin_api")
        lib = MovieLibrary(type="movies")
        assert _effective_write_method(cfg, lib) == "jellyfin_api"


def _build_fixture(tmp_path, write_method: str):
    media_root = tmp_path / "media"
    movie_dir = media_root / "Movies" / "Test Movie"
    movie_dir.mkdir(parents=True, exist_ok=True)
    (movie_dir / "m.mkv").write_bytes(b"video")

    assets = tmp_path / "assets"
    ad = assets / "155"
    ad.mkdir(parents=True, exist_ok=True)
    (ad / "poster.jpg").write_bytes(_poster_bytes())
    (ad / "backdrop.jpg").write_bytes(_poster_bytes((90, 90, 90)))

    cfg = Config(
        dry_run=True,
        server=ServerConfig(url="http://x:8096"),
        artwork_write_method="sidecar",
        libraries={"Movies": MovieLibrary(
            type="movies",
            metadata=Metadata(enabled=False),
            artwork=MovieArtwork(poster=True, backdrop=True),
            artwork_write_method=write_method,
        )},
    )
    mapper = PathMapper.from_config([{"jellyfin": str(media_root), "local": str(media_root)}])
    item = Item(id="mov155", name="Test Movie (2020)", type="Movie",
                path=str(movie_dir / "m.mkv"), provider_ids={"Tmdb": "155"})
    match = MatchResult(item=item, tmdb_id=155, matched_by="provider_id")
    from finmeta.sources.local import LocalAssetSource
    sources = {"local": LocalAssetSource(assets)}
    lib = cfg.libraries["Movies"]
    return cfg, lib, mapper, item, match, sources


class TestPlanBuilderJellyfinApi:
    def test_non_overlay_kinds_get_synthetic_target_and_write_method(self, tmp_path: Path):
        config_dir = tmp_path / "config"
        cfg, lib, mapper, item, match, sources = _build_fixture(tmp_path, "jellyfin_api")
        jf = MagicMock()
        jf.get_image_bytes.return_value = None
        plan_item = _build_movie_plan(
            item, match, cfg, lib, mapper, tmdb=None, jf=jf,
            sources=sources, language="en", lang_fallback=[None], config_dir=config_dir)
        by_kind = {f.kind: f for f in plan_item.files}
        assert by_kind["poster"].write_method == "jellyfin_api"
        assert by_kind["poster"].target_path == "jellyfin-api://mov155/poster"
        assert by_kind["backdrop"].write_method == "jellyfin_api"
        assert by_kind["backdrop"].target_path == "jellyfin-api://mov155/backdrop"

    def test_sidecar_default_unchanged(self, tmp_path: Path):
        config_dir = tmp_path / "config"
        cfg, lib, mapper, item, match, sources = _build_fixture(tmp_path, "sidecar")
        jf = MagicMock()
        plan_item = _build_movie_plan(
            item, match, cfg, lib, mapper, tmdb=None, jf=jf,
            sources=sources, language="en", lang_fallback=[None], config_dir=config_dir)
        by_kind = {f.kind: f for f in plan_item.files}
        assert by_kind["poster"].write_method == "sidecar"
        assert str(Path(by_kind["poster"].target_path).name) == "poster.jpg"
        jf.get_image_bytes.assert_not_called()


# ---------------------------------------------------------------------------
# engine.executor: apply_plan uploading via jellyfin_api
# ---------------------------------------------------------------------------

def _make_config(dry_run=False):
    return Config(dry_run=dry_run, server=ServerConfig(url="http://x:8096"))


class TestApplyPlanJellyfinApi:
    def test_uploads_via_jellyfin_client_not_filesystem(self, tmp_path: Path):
        content = b"POSTER BYTES"
        sha = hashlib.sha256(content).hexdigest()
        plan = WritePlan(items=[
            ItemPlan(
                item_id="mov001", item_name="Test Movie",
                media_type="movie", tmdb_id=123, matched_by="provider_id",
                files=[FileWrite(
                    target_path="jellyfin-api://mov001/poster",
                    kind="poster",
                    source_ref=str(tmp_path / "src.jpg"),
                    content_sha256=sha,
                    write_method="jellyfin_api",
                )],
            ),
        ])
        (tmp_path / "src.jpg").write_bytes(content)
        cfg = _make_config(dry_run=False)
        jf = MagicMock()
        tmdb = MagicMock()
        db = tmp_path / "jobs.db"
        cfg_dir = tmp_path / "config"
        cfg_dir.mkdir()

        result = apply_plan(
            plan, "r1", cfg, jf, tmdb,
            config_dir=cfg_dir, db_path=db, poll_timeout=0.1,
        )
        assert result.files_written == 1
        assert result.errors == []
        jf.upload_image.assert_called_once_with("mov001", "Primary", content, content_type="image/jpeg")
        # Refresh/poll still triggered (status != "unchanged"), but WITHOUT
        # replaceAllImages=true -- the upload already set the new ImageTag
        # directly; asking Jellyfin to also re-scan images here would let a
        # local poster/fanart file still sitting next to the media win over
        # what was just uploaded (see TestRefreshDoesNotReplaceApiImages).
        jf.refresh_item.assert_called_with("mov001", image_refresh_mode="None", replace_all_images=False)

    def test_logo_uses_png_content_type(self, tmp_path: Path):
        content = b"LOGO BYTES"
        sha = hashlib.sha256(content).hexdigest()
        plan = WritePlan(items=[
            ItemPlan(
                item_id="mov001", item_name="Test Movie",
                media_type="movie", tmdb_id=123, matched_by="provider_id",
                files=[FileWrite(
                    target_path="jellyfin-api://mov001/logo",
                    kind="logo",
                    source_ref=str(tmp_path / "logo.png"),
                    content_sha256=sha,
                    write_method="jellyfin_api",
                )],
            ),
        ])
        (tmp_path / "logo.png").write_bytes(content)
        cfg = _make_config(dry_run=False)
        jf = MagicMock()
        tmdb = MagicMock()
        db = tmp_path / "jobs.db"
        cfg_dir = tmp_path / "config"
        cfg_dir.mkdir()

        apply_plan(plan, "r1", cfg, jf, tmdb, config_dir=cfg_dir, db_path=db, poll_timeout=0.1)
        jf.upload_image.assert_called_once_with("mov001", "Logo", content, content_type="image/png")

    def test_upload_failure_recorded_as_error_not_raised(self, tmp_path: Path):
        content = b"POSTER BYTES"
        sha = hashlib.sha256(content).hexdigest()
        plan = WritePlan(items=[
            ItemPlan(
                item_id="mov001", item_name="Test Movie",
                media_type="movie", tmdb_id=123, matched_by="provider_id",
                files=[FileWrite(
                    target_path="jellyfin-api://mov001/poster",
                    kind="poster",
                    source_ref=str(tmp_path / "src.jpg"),
                    content_sha256=sha,
                    write_method="jellyfin_api",
                )],
            ),
        ])
        (tmp_path / "src.jpg").write_bytes(content)
        cfg = _make_config(dry_run=False)
        jf = MagicMock()
        jf.upload_image.side_effect = RuntimeError("jellyfin down")
        tmdb = MagicMock()
        db = tmp_path / "jobs.db"
        cfg_dir = tmp_path / "config"
        cfg_dir.mkdir()

        result = apply_plan(plan, "r1", cfg, jf, tmdb, config_dir=cfg_dir, db_path=db, poll_timeout=0.1)
        assert result.files_written == 0
        assert len(result.errors) == 1
        assert "jellyfin down" in result.errors[0]
