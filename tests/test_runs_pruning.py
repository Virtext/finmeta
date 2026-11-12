"""config/runs/ retention (audit 2026-11-11 "C1").

Before this fix, nothing ever deleted a `plan-<epoch>.json` file or a
`run-<epoch>-<hex>/manifest.json` dir -- `engine.overlay._live_overlay_digests`
reads every plan file on every apply to decide which cached overlay renders
are still needed, so an ever-growing `runs/` directory is the actual root
cause of the overlay cache growing without bound. `prune_runs_dir` keeps the
newest `keep` of each kind and deletes the rest.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from finmeta.engine import overlay
from finmeta.engine.overlay import prune_overlay_cache
from finmeta.engine.plan import FileWrite, ItemPlan, WritePlan
from finmeta.engine.runs import Manifest, RunFile, prune_runs_dir, record, undo


def _plan_file(config_dir: Path, epoch: int, digest: str = "deadbeef") -> Path:
    plan = WritePlan(items=[
        ItemPlan(
            item_id="mov001", item_name="Test Movie",
            media_type="movie", tmdb_id=123, matched_by="provider_id",
            files=[FileWrite(
                target_path="/media/poster.jpg", kind="poster",
                source_ref="/original/poster.jpg", content_sha256=digest,
            )],
        ),
    ])
    runs_dir = config_dir / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    p = runs_dir / f"plan-{epoch}.json"
    p.write_text(plan.to_json() + "\n")
    os.utime(p, (epoch, epoch))
    return p


def _run_dir(config_dir: Path, run_id: str, mtime: float | None = None) -> Path:
    record(Manifest(run_id=run_id, files=[
        RunFile(path="/media/poster.jpg", status="create"),
    ]), config_dir=config_dir)
    d = config_dir / "runs" / run_id
    if mtime is not None:
        os.utime(d / "manifest.json", (mtime, mtime))
    return d


class TestNoopCases:
    def test_no_runs_dir_is_a_noop(self, tmp_path: Path):
        assert prune_runs_dir(tmp_path / "config", keep=50) == 0

    def test_keep_zero_disables_pruning(self, tmp_path: Path):
        config_dir = tmp_path / "config"
        for i in range(5):
            _plan_file(config_dir, 1000 + i)
        assert prune_runs_dir(config_dir, keep=0) == 0
        assert len(list((config_dir / "runs").glob("plan-*.json"))) == 5


class TestPlanFilePruning:
    def test_keeps_newest_n_deletes_rest(self, tmp_path: Path):
        config_dir = tmp_path / "config"
        plans = [_plan_file(config_dir, 1000 + i) for i in range(5)]
        removed = prune_runs_dir(config_dir, keep=2)
        assert removed == 3
        # newest two (1003, 1004) survive
        assert plans[3].is_file()
        assert plans[4].is_file()
        for p in plans[:3]:
            assert not p.is_file()

    def test_fewer_than_keep_is_a_noop(self, tmp_path: Path):
        config_dir = tmp_path / "config"
        plans = [_plan_file(config_dir, 1000 + i) for i in range(3)]
        assert prune_runs_dir(config_dir, keep=50) == 0
        assert all(p.is_file() for p in plans)

    def test_malformed_name_sorts_as_newest_never_pruned(self, tmp_path: Path):
        """A name that doesn't match plan-<epoch>.json sorts LAST (same
        convention as engine.runs.run_epoch for run dirs) -- never pruned,
        rather than guessed at or crashing the sweep."""
        config_dir = tmp_path / "config"
        weird = _plan_file(config_dir, 1)  # will be renamed below
        weird_renamed = weird.with_name("plan-not-a-number.json")
        weird.rename(weird_renamed)
        normal = [_plan_file(config_dir, 2000 + i) for i in range(3)]
        removed = prune_runs_dir(config_dir, keep=1)
        assert weird_renamed.is_file()
        assert removed == 3  # all 3 normal ones pruned; weird occupies the one "keep" slot
        assert all(not p.is_file() for p in normal)


class TestRunDirPruning:
    def test_keeps_newest_n_deletes_rest(self, tmp_path: Path):
        config_dir = tmp_path / "config"
        now = time.time()
        dirs = []
        for i in range(5):
            dirs.append(_run_dir(config_dir, f"run-{1000 + i}-abcdef", mtime=now - (5 - i)))
        removed = prune_runs_dir(config_dir, keep=2)
        assert removed == 3
        assert dirs[3].is_dir()
        assert dirs[4].is_dir()
        for d in dirs[:3]:
            assert not d.is_dir()

    def test_pruned_run_falls_back_to_backups_only_undo(self, tmp_path: Path):
        """Deleting a run's manifest only degrades undo() for that run --
        it already has a defined fallback (audit 2026-11-11 "B2")."""
        from finmeta.engine.runs import backup

        config_dir = tmp_path / "config"
        target = tmp_path / "media" / "poster.jpg"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"ORIGINAL")
        rel = backup("run-1000-aaaaaa", target, config_dir=config_dir)
        record(Manifest(run_id="run-1000-aaaaaa", files=[
            RunFile(path=str(target), status="overwrite", backup_relpath=rel),
        ]), config_dir=config_dir)
        target.write_bytes(b"FINMETA-OUTPUT")

        prune_runs_dir(config_dir, keep=0)  # no-op guard, sanity
        import shutil as _shutil
        _shutil.rmtree(config_dir / "runs" / "run-1000-aaaaaa")

        result = undo("run-1000-aaaaaa", config_dir=config_dir)
        assert result.incomplete is True
        assert target.read_bytes() == b"ORIGINAL"


class TestOverlayCacheIntegration:
    def test_pruning_old_plan_lets_overlay_cache_reclaim_its_render(self, tmp_path: Path):
        """The architectural point of C1: the cache can't shrink because
        _live_overlay_digests reads every plan file still on disk. Once the
        plan referencing a digest is pruned, prune_overlay_cache can free
        the render it protected."""
        config_dir = tmp_path / "config"
        old = time.time() - 2 * overlay._CACHE_PRUNE_GRACE_SECONDS
        cache_file = config_dir / "cache" / "overlay" / "deadbeef.jpg"
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_bytes(b"jpeg-bytes")
        os.utime(cache_file, (old, old))

        newest_plan = _plan_file(config_dir, int(old) + 1000, digest="livecafe")
        _plan_file(config_dir, int(old), digest="deadbeef")  # the only ref to deadbeef

        # Still referenced -> protected.
        assert prune_overlay_cache(config_dir) == 0
        assert cache_file.is_file()

        removed = prune_runs_dir(config_dir, keep=1)
        assert removed == 1
        assert newest_plan.is_file()

        # No plan references "deadbeef" any more -> now reclaimable.
        assert prune_overlay_cache(config_dir) == 1
        assert not cache_file.is_file()
