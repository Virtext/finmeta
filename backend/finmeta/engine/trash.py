"""Backup trash + retention (S9d5, CLAUDE.md §4.1, SPEC override O5/O7).

"Gamle versjoner -> papirkurv med oppbevaringstid (f.eks. 14 dager), deretter
slettes de. Originalbildet fra Jellyfin beholdes alltid (undo)." (Old
versions -> trash with a retention period, then they get deleted. The
original image from Jellyfin is always kept, for undo.)

``engine.runs.backup`` writes one copy per run under
``config/backups/<run_id>/<relpath>`` and NEVER deletes anything -- left
alone, these accumulate forever. Two files per path are permanently exempt
from cleanup, regardless of age:

  - the OLDEST copy across all runs: the true pre-Finmeta original, needed
    by ``engine.runs.find_original_backup`` so overlay re-renders never
    stack badges (O7), and for a full "undo everything" restore.
  - the MOST RECENT copy: needed to undo the latest run.

Every OTHER copy is immediately superseded the moment it is neither the
oldest nor the newest, so it moves to ``config/trash/<run_id>/<relpath>``
right away (its trash-entry mtime is reset to the move time). A second pass
permanently deletes trash entries older than ``retention_days``.
"""
from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from typing import Dict, List, Tuple

from finmeta.engine.runs import DEFAULT_CONFIG_DIR, run_epoch


def _trash_dir(config_dir: Path) -> Path:
    return Path(config_dir) / "trash"


def _backup_copies_by_relpath(config_dir: Path) -> Dict[str, List[Tuple[str, Path]]]:
    """Every backup copy across all run dirs, grouped by relpath, as
    ``(run_id, path)`` pairs -- the run id travels with its path so the
    caller never has to re-derive it from the path later."""
    base = Path(config_dir) / "backups"
    out: Dict[str, List[Tuple[str, Path]]] = {}
    if not base.is_dir():
        return out
    for run_dir in base.iterdir():
        if not run_dir.is_dir():
            continue
        for f in run_dir.rglob("*"):
            if not f.is_file():
                continue
            rel = str(f.relative_to(run_dir))
            out.setdefault(rel, []).append((run_dir.name, f))
    return out


def sweep_to_trash(config_dir: Path = DEFAULT_CONFIG_DIR) -> int:
    """Move every superseded (not-oldest, not-newest) backup copy to trash.

    Ordered by ``engine.runs.run_epoch`` -- the SAME ordering
    ``find_original_backup`` uses to decide which copy is the true
    pre-Finmeta original -- never by ``st_mtime``. Audit 2026-11-11 "B1":
    this previously sorted by ``st_mtime``, which disagrees with
    ``run_epoch`` whenever anything mtime-preserving (``rsync -a``,
    ``cp -p``, a restore, the Unraid mover) touches a backed-up file
    between runs. When the two orderings disagreed, this function could
    trash (and ``purge_expired`` could then permanently delete) the one
    copy ``find_original_backup`` depends on never going away -- after
    which every later overlay re-render composites onto Finmeta's own
    prior output instead of the real original (the exact O7 violation
    this whole module exists to prevent), with no way back.

    Sort key is ``(run_epoch, mtime)`` -- mtime only breaks a tie between
    two run ids that don't match the real ``run-<epoch>-<hex>`` pattern (a
    hand-made run id in a test sorts to ``float("inf")`` and would
    otherwise tie with every other hand-made id, falling back to
    whatever order the filesystem happens to hand directory entries out
    in). This is the exact same two-key sort ``find_original_backup``
    already uses, for the same reason.

    Returns the number of files moved. Best-effort per file: one failure
    (e.g. a permissions issue) is skipped, never aborts the whole sweep.
    """
    moved = 0
    by_rel = _backup_copies_by_relpath(config_dir)
    trash_root = _trash_dir(config_dir)
    for rel, copies in by_rel.items():
        if len(copies) < 3:
            continue  # nothing "in between" the oldest and the newest yet
        copies.sort(key=lambda rp: (run_epoch(rp[0]), rp[1].stat().st_mtime))
        middle = copies[1:-1]
        for run_id, src in middle:
            try:
                dest = trash_root / run_id / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dest))
                os.utime(dest, None)  # retention clock starts now, not at the original mtime
                moved += 1
            except OSError:
                continue
    return moved


def purge_expired(config_dir: Path = DEFAULT_CONFIG_DIR, retention_days: int = 14) -> int:
    """Permanently delete trash entries older than ``retention_days``.

    ``retention_days <= 0`` disables purging entirely (sweep_to_trash still
    moves superseded copies into trash, they just accumulate there).
    Returns the number of files deleted.

    Also prunes the now-empty ``<run_id>/<relpath-dir>/`` directories left
    behind by those deletes (audit 2026-11-11 "D13"): the file-only unlink
    loop never removed them, so ``config/trash/`` accumulated empty
    directory trees forever. Deepest-first so a child directory emptying
    out lets its now-empty parent get removed in the same pass; ``rmdir()``
    only succeeds on an empty directory, so a dir that still holds a
    not-yet-expired entry is left alone, same best-effort-per-entry style
    as the delete loop above.
    """
    if retention_days <= 0:
        return 0
    trash_root = _trash_dir(config_dir)
    if not trash_root.is_dir():
        return 0
    cutoff = time.time() - retention_days * 86400
    deleted = 0
    for f in trash_root.rglob("*"):
        if not f.is_file():
            continue
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
                deleted += 1
        except OSError:
            continue
    dirs = sorted((d for d in trash_root.rglob("*") if d.is_dir()),
                  key=lambda d: len(d.parts), reverse=True)
    for d in dirs:
        try:
            d.rmdir()
        except OSError:
            continue
    return deleted


def sweep(config_dir: Path = DEFAULT_CONFIG_DIR, retention_days: int = 14) -> Dict[str, int]:
    """Run both stages: supersede -> trash, then purge expired trash entries."""
    return {
        "moved_to_trash": sweep_to_trash(config_dir),
        "purged": purge_expired(config_dir, retention_days),
    }


__all__ = ["sweep", "sweep_to_trash", "purge_expired"]
