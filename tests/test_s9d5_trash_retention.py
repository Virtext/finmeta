"""S9d5 (CLAUDE.md §4.1): backup trash + retention.

"Gamle versjoner -> papirkurv med oppbevaringstid (f.eks. 14 dager),
deretter slettes de. Originalbildet fra Jellyfin beholdes alltid (undo)."

engine.runs.backup() writes one copy per run under
config/backups/<run_id>/<relpath> and never deletes anything on its own.
engine.trash sweeps that directory: the OLDEST copy per path (the true
pre-Finmeta original, needed for O7 + full undo) and the MOST RECENT copy
(needed to undo the latest run) are permanently exempt; every copy in
between moves to config/trash/<run_id>/<relpath> immediately, then gets
permanently deleted once it has sat there longer than retention_days.
"""
from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path
from unittest.mock import MagicMock

from finmeta.config.models import Config, ServerConfig
from finmeta.engine import trash
from finmeta.engine.executor import apply_plan
from finmeta.engine.plan import FileWrite, ItemPlan, WritePlan
from finmeta.engine.runs import backup


def _mkbackup(config_dir: Path, run_id: str, target: Path, mtime: float) -> Path:
    bk_rel = backup(run_id, target, config_dir=config_dir)
    bk = config_dir / "backups" / run_id / bk_rel
    os.utime(bk, (mtime, mtime))
    return bk


class TestSweepToTrash:
    def test_single_copy_never_moved(self, tmp_path: Path):
        config_dir = tmp_path / "config"
        target = tmp_path / "media" / "poster.jpg"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"v0")
        _mkbackup(config_dir, "run1", target, time.time() - 1000)
        assert trash.sweep_to_trash(config_dir) == 0

    def test_two_copies_never_moved(self, tmp_path: Path):
        """Oldest (original) + newest (latest-run undo) are both exempt."""
        config_dir = tmp_path / "config"
        target = tmp_path / "media" / "poster.jpg"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"v0")
        now = time.time()
        _mkbackup(config_dir, "run1", target, now - 2000)
        target.write_bytes(b"v1")
        _mkbackup(config_dir, "run2", target, now - 1000)
        assert trash.sweep_to_trash(config_dir) == 0

    def test_middle_copy_moved_oldest_and_newest_kept_in_place(self, tmp_path: Path):
        config_dir = tmp_path / "config"
        target = tmp_path / "media" / "poster.jpg"
        target.parent.mkdir(parents=True)
        now = time.time()
        target.write_bytes(b"v0")
        bk1 = _mkbackup(config_dir, "run1", target, now - 3000)
        target.write_bytes(b"v1")
        bk2 = _mkbackup(config_dir, "run2", target, now - 2000)
        target.write_bytes(b"v2")
        bk3 = _mkbackup(config_dir, "run3", target, now - 1000)

        moved = trash.sweep_to_trash(config_dir)
        assert moved == 1
        assert bk1.is_file()   # oldest -- the true original, never touched
        assert bk3.is_file()   # newest -- needed to undo the latest run
        assert not bk2.is_file()
        trashed = [p for p in (config_dir / "trash" / "run2").rglob("*") if p.is_file()]
        assert len(trashed) == 1
        assert trashed[0].read_bytes() == b"v1"

    def test_many_middle_copies_all_moved(self, tmp_path: Path):
        config_dir = tmp_path / "config"
        target = tmp_path / "media" / "poster.jpg"
        target.parent.mkdir(parents=True)
        now = time.time()
        backups = []
        for i in range(5):
            target.write_bytes(f"v{i}".encode())
            backups.append(_mkbackup(config_dir, f"run{i}", target, now - (5 - i) * 1000))
        moved = trash.sweep_to_trash(config_dir)
        assert moved == 3  # all but the oldest and the newest
        assert backups[0].is_file()
        assert backups[-1].is_file()
        for bk in backups[1:-1]:
            assert not bk.is_file()

    def test_multiple_paths_independent(self, tmp_path: Path):
        config_dir = tmp_path / "config"
        now = time.time()
        for name in ("poster.jpg", "backdrop.jpg"):
            target = tmp_path / "media" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            for i, run in enumerate(("run1", "run2", "run3")):
                target.write_bytes(f"{name}-v{i}".encode())
                _mkbackup(config_dir, run, target, now - (3 - i) * 1000)
        assert trash.sweep_to_trash(config_dir) == 2  # one middle copy per path

    def test_no_backups_dir_is_a_noop(self, tmp_path: Path):
        config_dir = tmp_path / "config"
        assert trash.sweep_to_trash(config_dir) == 0


class TestPurgeExpired:
    def test_old_entry_purged_recent_kept(self, tmp_path: Path):
        config_dir = tmp_path / "config"
        trash_dir = config_dir / "trash" / "run2"
        trash_dir.mkdir(parents=True)
        old = trash_dir / "old.jpg"
        old.write_bytes(b"old")
        os.utime(old, (time.time() - 20 * 86400, time.time() - 20 * 86400))
        recent = trash_dir / "recent.jpg"
        recent.write_bytes(b"recent")
        os.utime(recent, (time.time() - 1 * 86400, time.time() - 1 * 86400))

        deleted = trash.purge_expired(config_dir, retention_days=14)
        assert deleted == 1
        assert not old.is_file()
        assert recent.is_file()

    def test_retention_zero_disables_purge(self, tmp_path: Path):
        config_dir = tmp_path / "config"
        trash_dir = config_dir / "trash" / "run2"
        trash_dir.mkdir(parents=True)
        old = trash_dir / "old.jpg"
        old.write_bytes(b"old")
        os.utime(old, (time.time() - 999 * 86400, time.time() - 999 * 86400))
        assert trash.purge_expired(config_dir, retention_days=0) == 0
        assert old.is_file()

    def test_no_trash_dir_is_a_noop(self, tmp_path: Path):
        config_dir = tmp_path / "config"
        assert trash.purge_expired(config_dir, retention_days=14) == 0


class TestSweepCombined:
    def test_sweep_moves_then_purges_in_one_call(self, tmp_path: Path):
        config_dir = tmp_path / "config"
        target = tmp_path / "media" / "poster.jpg"
        target.parent.mkdir(parents=True)
        now = time.time()
        target.write_bytes(b"v0")
        _mkbackup(config_dir, "run1", target, now - 3000)
        target.write_bytes(b"v1")
        bk2 = _mkbackup(config_dir, "run2", target, now - 2000)
        target.write_bytes(b"v2")
        _mkbackup(config_dir, "run3", target, now - 1000)

        result = trash.sweep(config_dir, retention_days=14)
        assert result["moved_to_trash"] == 1
        assert result["purged"] == 0  # just moved -- mtime reset to "now"
        assert not bk2.is_file()


class TestExecutorIntegration:
    def _make_config(self, retention_days: int = 14) -> Config:
        return Config(dry_run=False, server=ServerConfig(url="http://x:8096"),
                      backup_retention_days=retention_days)

    def test_apply_plan_sweeps_backups_without_failing(self, tmp_path: Path):
        """A pre-existing superseded backup gets swept during a normal apply
        that overwrites the same path again -- best-effort, never breaks
        the apply itself."""
        config_dir = tmp_path / "config"
        target = tmp_path / "media" / "poster.jpg"
        target.parent.mkdir(parents=True)
        now = time.time()
        target.write_bytes(b"v0")
        _mkbackup(config_dir, "run1", target, now - 3000)
        target.write_bytes(b"v1")
        bk2 = _mkbackup(config_dir, "run2", target, now - 2000)
        target.write_bytes(b"v2")  # current on-disk content before this apply

        content = b"v3"
        sha = hashlib.sha256(content).hexdigest()
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
        (tmp_path / "src.jpg").write_bytes(content)
        jf, tmdb = MagicMock(), MagicMock()
        db = tmp_path / "jobs.db"

        result = apply_plan(plan, "run4", self._make_config(), jf, tmdb,
                            config_dir=config_dir, db_path=db, poll_timeout=0.1)
        assert result.errors == []
        # run2's backup (now the "middle" copy once run4's own backup of v2 exists)
        # was superseded and swept.
        assert not bk2.is_file()
        assert target.read_bytes() == content

    def test_retention_zero_still_sweeps_to_trash_but_never_purges(self, tmp_path: Path):
        config_dir = tmp_path / "config"
        target = tmp_path / "media" / "poster.jpg"
        target.parent.mkdir(parents=True)
        now = time.time()
        target.write_bytes(b"v0")
        _mkbackup(config_dir, "run1", target, now - 3000)
        target.write_bytes(b"v1")
        bk2 = _mkbackup(config_dir, "run2", target, now - 2000)
        target.write_bytes(b"v2")

        content = b"v3"
        sha = hashlib.sha256(content).hexdigest()
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
        (tmp_path / "src.jpg").write_bytes(content)
        jf, tmdb = MagicMock(), MagicMock()
        db = tmp_path / "jobs.db"

        apply_plan(plan, "run4", self._make_config(retention_days=0), jf, tmdb,
                  config_dir=config_dir, db_path=db, poll_timeout=0.1)
        assert not bk2.is_file()  # still moved to trash
        assert list((config_dir / "trash").rglob("*.jpg"))  # but nothing purged from it
