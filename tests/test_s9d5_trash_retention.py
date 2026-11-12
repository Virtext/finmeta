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

from finmeta.config.models import Config, PathMapping, ServerConfig
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

    def test_mtime_disagreeing_with_run_order_does_not_trash_the_true_original(
        self, tmp_path: Path,
    ):
        """Audit 2026-11-11 "B1": sweep_to_trash used to order copies by
        st_mtime while find_original_backup orders by run_epoch (the
        submit-time embedded in a real `run-<epoch>-<hex>` id). The two
        disagree whenever something mtime-preserving (rsync -a, cp -p, a
        restore, the Unraid mover) touches a backup between runs -- here
        simulated directly: the OLDEST run's backup gets the LATEST mtime.

        Before the fix this moved the true original to trash (and
        find_original_backup would then return the middle run's copy
        instead once it was gone). After the fix, ordering follows the
        real run ids and ignores mtime entirely once they disagree."""
        from finmeta.engine.runs import find_original_backup

        config_dir = tmp_path / "config"
        target = tmp_path / "media" / "poster.jpg"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"TRUE-ORIGINAL")
        # run-1000-... is the OLDEST by run id, but its backup copy is given
        # the NEWEST mtime -- the exact disagreement the old mtime-based
        # sort could not tell apart from a genuinely newer run.
        bk_true_original = _mkbackup(
            config_dir, "run-1000-aaaaaa", target, time.time())
        target.write_bytes(b"finmeta-output-1")
        bk_middle = _mkbackup(
            config_dir, "run-2000-bbbbbb", target, time.time() - 5000)
        target.write_bytes(b"finmeta-output-2")
        _mkbackup(config_dir, "run-3000-cccccc", target, time.time() - 4000)

        moved = trash.sweep_to_trash(config_dir)
        assert moved == 1
        assert bk_true_original.is_file(), "the true pre-Finmeta original must survive"
        assert not bk_middle.is_file(), "the middle run's copy is the one superseded"
        orig = find_original_backup(target, config_dir=config_dir)
        assert orig is not None and orig.read_bytes() == b"TRUE-ORIGINAL"


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

    def test_now_empty_directories_are_removed(self, tmp_path: Path):
        """Audit 2026-11-11 "D13": the unlink loop above never removed the
        now-empty <run_id>/<relpath-dir>/ trees it left behind -- only the
        file was deleted, config/trash/ accumulated empty directory trees
        forever. Nested relpath dirs (e.g. a show's season subfolder) must
        be pruned too, deepest first."""
        config_dir = tmp_path / "config"
        old_dir = config_dir / "trash" / "run2" / "Show" / "Season 01"
        old_dir.mkdir(parents=True)
        old = old_dir / "poster.jpg"
        old.write_bytes(b"old")
        os.utime(old, (time.time() - 20 * 86400, time.time() - 20 * 86400))

        deleted = trash.purge_expired(config_dir, retention_days=14)
        assert deleted == 1
        assert not old.is_file()
        assert not old_dir.is_dir()                       # "Season 01"
        assert not old_dir.parent.is_dir()                 # "Show"
        assert not (config_dir / "trash" / "run2").is_dir()  # run dir itself
        assert (config_dir / "trash").is_dir()             # trash root survives

    def test_directory_with_a_surviving_entry_is_not_removed(self, tmp_path: Path):
        config_dir = tmp_path / "config"
        trash_dir = config_dir / "trash" / "run2"
        trash_dir.mkdir(parents=True)
        old = trash_dir / "old.jpg"
        old.write_bytes(b"old")
        os.utime(old, (time.time() - 20 * 86400, time.time() - 20 * 86400))
        recent = trash_dir / "recent.jpg"
        recent.write_bytes(b"recent")
        os.utime(recent, (time.time() - 1 * 86400, time.time() - 1 * 86400))

        trash.purge_expired(config_dir, retention_days=14)
        assert trash_dir.is_dir()  # "recent.jpg" is still in it
        assert recent.is_file()


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
    def _make_config(self, retention_days: int = 14, media_root: str = "/tmp/media") -> Config:
        # media_root (audit 2026-11-11 "B6"): apply_plan now refuses to
        # write outside a configured path_mappings local root.
        return Config(dry_run=False, server=ServerConfig(url="http://x:8096"),
                      backup_retention_days=retention_days,
                      path_mappings=[PathMapping(jellyfin="/media", local=media_root)])

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

        result = apply_plan(plan, "run4", self._make_config(media_root=str(tmp_path / "media")), jf, tmdb,
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

        apply_plan(plan, "run4", self._make_config(retention_days=0, media_root=str(tmp_path / "media")), jf, tmdb,
                  config_dir=config_dir, db_path=db, poll_timeout=0.1)
        assert not bk2.is_file()  # still moved to trash
        assert list((config_dir / "trash").rglob("*.jpg"))  # but nothing purged from it
