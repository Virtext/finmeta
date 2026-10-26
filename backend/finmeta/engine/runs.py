"""Runs: backup, manifest, and undo (SPEC \u00a710, override O5).

A run writes files into the media tree and records exactly what it did in
``config/runs/<run_id>/manifest.json``. Any existing file that gets
overwritten is first copied to ``config/backups/<run_id>/<relpath>`` (O5).
``undo(run_id)`` restores the originals from those backups and deletes every
file the run created.

This is a library module. Every path is derived from an injectable
``config_dir`` (default: the repo ``config/`` directory), so the executor and
the tests can point it at a temp dir. ``undo`` is idempotent: a second call
restores the same bytes and deletes nothing new.
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from pydantic import BaseModel, Field

# Default config location for the repo checkout. Tests pass their own.
DEFAULT_CONFIG_DIR = Path("config")


class RunFile(BaseModel):
    """One file the run created or overwrote."""

    path: str                                  # absolute local path
    status: str                                # "create" | "overwrite" | "delete" (S9g3)
    backup_relpath: Optional[str] = None       # under backups/<run_id>/


class Manifest(BaseModel):
    """What a run did, recorded for undo (SPEC \u00a710, override O5)."""

    run_id: str
    files: List[RunFile] = Field(default_factory=list)
    # API calls the run made (collections/playlists). Restored in a later
    # session; recorded here so the manifest is complete.
    api_actions: List[dict] = Field(default_factory=list)

    def to_json(self) -> str:
        return self.model_dump_json(indent=2)


@dataclass
class UndoResult:
    restored: List[str] = field(default_factory=list)
    deleted: List[str] = field(default_factory=list)


# -- path helpers -----------------------------------------------------------

def _relpath(path: Path, config_dir: Path) -> str:
    """Filesystem-relative path from the config dir\u2019s parent to ``path``."""
    root = Path(config_dir).resolve().parent
    return os.path.relpath(str(Path(path).resolve()), start=str(root))


def _abs_from_rel(rel: str, config_dir: Path) -> Path:
    root = Path(config_dir).resolve().parent
    return root / rel


def _backups_dir(run_id: str, config_dir: Path) -> Path:
    return Path(config_dir) / "backups" / run_id


def _manifest_path(run_id: str, config_dir: Path) -> Path:
    return Path(config_dir) / "runs" / run_id / "manifest.json"


# -- public API -------------------------------------------------------------

def backup(run_id: str, path: Path, *, config_dir: Path = DEFAULT_CONFIG_DIR) -> Optional[str]:
    """Back up an existing file to ``backups/<run_id>/<relpath>`` (override O5).

    Returns the relpath (under the run\u2019s backup dir) when a copy was made,
    or ``None`` when the file did not exist \u2014 in which case the write is a
    create and there is nothing to back up.
    """
    p = Path(path)
    if not p.is_file():
        return None
    rel = _relpath(p, config_dir)
    dest = _backups_dir(run_id, config_dir) / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(p, dest)
    return rel


def record(manifest: Manifest, *, config_dir: Path = DEFAULT_CONFIG_DIR) -> Path:
    """Persist ``manifest`` to ``config/runs/<run_id>/manifest.json``."""
    mp = _manifest_path(manifest.run_id, config_dir)
    mp.parent.mkdir(parents=True, exist_ok=True)
    mp.write_text(manifest.to_json() + "\n")
    return mp


def load_manifest(run_id: str, *, config_dir: Path = DEFAULT_CONFIG_DIR) -> Manifest:
    mp = _manifest_path(run_id, config_dir)
    if not mp.is_file():
        raise FileNotFoundError(f"no manifest for run {run_id!r}: {mp}")
    return Manifest.model_validate_json(mp.read_text())


def find_original_backup(
    target_path: Path, *, config_dir: Path = DEFAULT_CONFIG_DIR,
) -> Optional[Path]:
    """Return the OLDEST backup of ``target_path`` across all runs -- the
    pre-Finmeta original (override O7).

    Every apply that overwrites an existing file backs it up first (whatever
    is on disk at that moment), so after the FIRST apply, every LATER backup
    is of Finmeta's own prior output, not the true original. The only backup
    that is ever the true pre-Finmeta original is the very first one ever
    made for this path. This function used to be named ``find_latest_backup``
    and returned the most RECENT backup instead -- which silently composited
    new badges on top of old ones on every second-or-later apply, a real O7
    violation (reported by the operator, confirmed by tracing a real
    repeated-apply session: run N's backup is run N-1's already-overlaid
    output). Returns ``None`` when no run has backed this path up yet (the
    on-disk file, if any, IS the original).
    """
    base = Path(config_dir) / "backups"
    if not base.is_dir():
        return None
    rel = _relpath(Path(target_path), config_dir)
    found: List[Path] = []
    for run_dir in base.iterdir():
        if not run_dir.is_dir():
            continue
        bk = run_dir / rel
        if bk.is_file():
            found.append(bk)
    if not found:
        return None
    found.sort(key=lambda pth: pth.stat().st_mtime)
    return found[0]


def undo(run_id: str, *, config_dir: Path = DEFAULT_CONFIG_DIR) -> UndoResult:
    """Undo a run: restore overwritten originals, delete created files.

    Reads the run\u2019s manifest, copies each backed-up original back to its
    path, and unlinks every file the run created. Returns what was restored
    and what was deleted. Idempotent: a second call restores the same bytes
    and deletes nothing new.
    """
    manifest = load_manifest(run_id, config_dir=config_dir)
    bdir = _backups_dir(run_id, config_dir)
    result = UndoResult()
    for entry in manifest.files:
        target = Path(entry.path)
        if entry.status == "overwrite" and entry.backup_relpath:
            bk = bdir / entry.backup_relpath
            if bk.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(bk, target)
                result.restored.append(entry.path)
        elif entry.status == "create":
            if target.is_file():
                target.unlink()
                result.deleted.append(entry.path)
        elif entry.status == "delete":
            # S9g3: a Coming Soon placeholder this run removed because the
            # real file had already arrived -- nothing to restore here
            # (resurrecting a stale placeholder over real content would be
            # wrong), so this is an intentional no-op.
            pass
    return result


__all__ = [
    "Manifest", "RunFile", "UndoResult",
    "backup", "record", "load_manifest", "undo",
    "DEFAULT_CONFIG_DIR",
]
