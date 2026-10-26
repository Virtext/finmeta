"""Overlay render cache pruning (fixes the "grows forever" gap found in
review: engine.overlay._write_overlay_cache wrote one file per distinct
rendered output under config/cache/overlay/ and nothing ever deleted from
it).

engine.overlay.prune_overlay_cache deletes a cached render once no plan file
under runs/ still references its digest, except within
_CACHE_PRUNE_GRACE_SECONDS of being written (an unapplied plan may sit on
disk a while before the operator applies it, and the cache entry for an
in-progress plan build lands on disk before that plan's own JSON does)."""
from __future__ import annotations

import os
import time
from pathlib import Path
from unittest.mock import MagicMock

from finmeta.config.models import Config, ServerConfig
from finmeta.engine import overlay
from finmeta.engine.executor import apply_plan
from finmeta.engine.overlay import prune_overlay_cache
from finmeta.engine.plan import FileWrite, ItemPlan, WritePlan


def _cache_file(config_dir: Path, digest: str, mtime: float) -> Path:
    p = config_dir / "cache" / "overlay" / f"{digest}.jpg"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"jpeg-bytes")
    os.utime(p, (mtime, mtime))
    return p


def _write_plan(config_dir: Path, name: str, digest: str, kind: str = "poster") -> Path:
    plan = WritePlan(items=[
        ItemPlan(
            item_id="mov001", item_name="Test Movie",
            media_type="movie", tmdb_id=123, matched_by="provider_id",
            files=[FileWrite(
                target_path="/media/poster.jpg", kind=kind,
                source_ref="/original/poster.jpg", content_sha256=digest,
            )],
        ),
    ])
    runs_dir = config_dir / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    p = runs_dir / name
    p.write_text(plan.to_json() + "\n")
    return p


class TestPruneOverlayCache:
    def test_no_cache_dir_is_a_noop(self, tmp_path: Path):
        assert prune_overlay_cache(tmp_path / "config") == 0

    def test_old_unreferenced_entry_deleted(self, tmp_path: Path):
        config_dir = tmp_path / "config"
        old = time.time() - 2 * overlay._CACHE_PRUNE_GRACE_SECONDS
        f = _cache_file(config_dir, "deadbeef", old)
        assert prune_overlay_cache(config_dir) == 1
        assert not f.is_file()

    def test_recent_unreferenced_entry_kept_within_grace_window(self, tmp_path: Path):
        config_dir = tmp_path / "config"
        f = _cache_file(config_dir, "deadbeef", time.time())
        assert prune_overlay_cache(config_dir) == 0
        assert f.is_file()

    def test_old_entry_referenced_by_a_plan_on_disk_kept(self, tmp_path: Path):
        config_dir = tmp_path / "config"
        old = time.time() - 2 * overlay._CACHE_PRUNE_GRACE_SECONDS
        f = _cache_file(config_dir, "deadbeef", old)
        _write_plan(config_dir, "plan-1.json", "deadbeef")
        assert prune_overlay_cache(config_dir) == 0
        assert f.is_file()

    def test_old_entry_referenced_by_non_poster_write_is_not_protected(self, tmp_path: Path):
        """Only poster FileWrites carry overlay-cache digests -- a plan that
        happens to reuse the same sha256 for a different kind (e.g. a
        metadata file) must not keep the cache entry alive."""
        config_dir = tmp_path / "config"
        old = time.time() - 2 * overlay._CACHE_PRUNE_GRACE_SECONDS
        f = _cache_file(config_dir, "deadbeef", old)
        _write_plan(config_dir, "plan-1.json", "deadbeef", kind="metadata")
        assert prune_overlay_cache(config_dir) == 1
        assert not f.is_file()

    def test_malformed_plan_file_skipped_not_fatal(self, tmp_path: Path):
        config_dir = tmp_path / "config"
        old = time.time() - 2 * overlay._CACHE_PRUNE_GRACE_SECONDS
        f = _cache_file(config_dir, "deadbeef", old)
        runs_dir = config_dir / "runs"
        runs_dir.mkdir(parents=True)
        (runs_dir / "plan-broken.json").write_text("{not json")
        assert prune_overlay_cache(config_dir) == 1
        assert not f.is_file()

    def test_mixed_live_and_dead_entries(self, tmp_path: Path):
        config_dir = tmp_path / "config"
        old = time.time() - 2 * overlay._CACHE_PRUNE_GRACE_SECONDS
        live = _cache_file(config_dir, "live123", old)
        dead = _cache_file(config_dir, "dead456", old)
        _write_plan(config_dir, "plan-1.json", "live123")
        assert prune_overlay_cache(config_dir) == 1
        assert live.is_file()
        assert not dead.is_file()


class TestExecutorIntegration:
    def test_apply_plan_prunes_overlay_cache_without_failing(self, tmp_path: Path):
        config_dir = tmp_path / "config"
        old = time.time() - 2 * overlay._CACHE_PRUNE_GRACE_SECONDS
        dead = _cache_file(config_dir, "stale999", old)

        target = tmp_path / "media" / "poster.jpg"
        target.parent.mkdir(parents=True)
        content = b"poster-bytes"
        import hashlib
        sha = hashlib.sha256(content).hexdigest()
        (tmp_path / "src.jpg").write_bytes(content)
        plan = WritePlan(items=[
            ItemPlan(
                item_id="mov001", item_name="Test Movie",
                media_type="movie", tmdb_id=123, matched_by="provider_id",
                files=[FileWrite(
                    target_path=str(target), kind="poster",
                    source_ref=str(tmp_path / "src.jpg"), content_sha256=sha,
                )],
            ),
        ])
        jf, tmdb = MagicMock(), MagicMock()
        db = tmp_path / "jobs.db"
        cfg = Config(dry_run=False, server=ServerConfig(url="http://x:8096"))

        result = apply_plan(plan, "run1", cfg, jf, tmdb,
                            config_dir=config_dir, db_path=db, poll_timeout=0.1)
        assert result.errors == []
        # The stale, unreferenced entry from before this run is gone; this
        # run's own plan was never written to runs/ by apply_plan itself
        # (only cmd_plan/run_service do that), so nothing protects it.
        assert not dead.is_file()
