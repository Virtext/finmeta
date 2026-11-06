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

import json
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


def _run_epoch(run_id: str) -> float:
    """The submit-time epoch embedded in ``run-<epoch>-<hex>``.

    Used to order runs chronologically. A directory that does not follow the
    pattern (``verify-run-1`` and friends) sorts LAST, so a real run always
    wins over a hand-made one. Never the backup file's ``st_mtime``:
    ``backup()`` copies with ``shutil.copy2``, which preserves the SOURCE
    file's mtime -- so mtime says when the backed-up content was written,
    not which run saved it, and the two can disagree.
    """
    parts = run_id.split("-")
    if len(parts) >= 3 and parts[0] == "run":
        try:
            return float(parts[1])
        except ValueError:
            pass
    return float("inf")


#: ``{resolved config_dir: (signature, {path: status})}`` -- see
#: ``_first_touch_index``. Run dirs are only ever added, so the signature is
#: just how many there are plus the highest name.
_FIRST_TOUCH_CACHE: dict = {}


def _first_touch_index(config_dir: Path) -> dict:
    """``{absolute path: status}`` of the EARLIEST manifest entry per path.

    Scans every ``runs/<run_id>/manifest.json`` once and remembers, for each
    path any run has ever written, what the oldest of those runs did to it
    (``"create"`` or ``"overwrite"``). Cached per config dir and rebuilt when
    a new run dir shows up -- a plan build asks this once per overlaid
    poster, and re-reading every manifest each time would be quadratic.

    Best-effort by design: a missing/malformed manifest is skipped, and an
    empty index simply means ``find_original_backup`` behaves exactly as it
    did before this index existed.
    """
    runs_dir = Path(config_dir) / "runs"
    if not runs_dir.is_dir():
        return {}
    try:
        names = sorted(d.name for d in runs_dir.iterdir() if d.is_dir())
    except OSError:
        return {}
    signature = (len(names), names[-1] if names else "")
    key = str(Path(config_dir).resolve())
    cached = _FIRST_TOUCH_CACHE.get(key)
    if cached is not None and cached[0] == signature:
        return cached[1]
    oldest: dict = {}
    for name in names:
        mp = runs_dir / name / "manifest.json"
        if not mp.is_file():
            continue
        try:
            data = json.loads(mp.read_text())
        except Exception:
            continue
        epoch = _run_epoch(name)
        for entry in data.get("files") or []:
            path, status = entry.get("path"), entry.get("status")
            if not path or not status:
                continue
            norm = str(Path(path))
            prev = oldest.get(norm)
            if prev is None or epoch < prev[0]:
                oldest[norm] = (epoch, status)
    index = {path: status for path, (_epoch, status) in oldest.items()}
    _FIRST_TOUCH_CACHE[key] = (signature, index)
    return index


def created_by_finmeta(
    target_path: Path, *, config_dir: Path = DEFAULT_CONFIG_DIR,
) -> bool:
    """True when the oldest run that ever touched this path CREATED it.

    Such a path has no pre-Finmeta original at all: ``backup()`` only copies
    a file that already exists, so the first apply saved nothing and every
    backup that exists is of Finmeta's own output. See
    ``find_original_backup``.
    """
    return _first_touch_index(config_dir).get(str(Path(target_path))) == "create"


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

    **"Oldest backup" is only the original when the file predates Finmeta**
    (fixed 2026-11-06, operator-reported, confirmed on a real movie poster):
    ``backup()`` copies nothing when the file does not exist yet, so a path
    Finmeta CREATED itself gets no backup on its first apply -- and the
    oldest backup that eventually exists is the one the SECOND apply made,
    i.e. a copy of the first apply's own overlaid output. Compositing onto
    that stacks old badges under new ones, exactly the bug the oldest-backup
    rule was introduced to prevent. Movie libraries hit this as the normal
    case: Radarr writes ``folder.jpg``, never ``poster.jpg``, so Finmeta
    creates every ``poster.jpg`` itself. ``created_by_finmeta`` (the run
    manifests' own ``status`` field) detects it, and ``None`` here sends the
    caller back to the freshly resolved candidate -- which is the correct
    original for such a path.
    """
    base = Path(config_dir) / "backups"
    if not base.is_dir():
        return None
    if created_by_finmeta(target_path, config_dir=config_dir):
        return None
    rel = _relpath(Path(target_path), config_dir)
    found: List[tuple] = []
    for run_dir in base.iterdir():
        if not run_dir.is_dir():
            continue
        bk = run_dir / rel
        if bk.is_file():
            found.append((_run_epoch(run_dir.name), bk.stat().st_mtime, bk))
    if not found:
        return None
    found.sort(key=lambda row: (row[0], row[1]))
    return found[0][2]


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
    "created_by_finmeta", "find_original_backup",
    "DEFAULT_CONFIG_DIR",
]
