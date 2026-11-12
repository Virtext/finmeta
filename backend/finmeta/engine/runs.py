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

import hashlib
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
    #: SHA-256 of the bytes THIS run actually wrote to ``path`` (audit
    #: 2026-11-11 "B3"). ``None`` for older manifests written before this
    #: field existed -- ``undo`` then falls back to its previous
    #: unconditional behaviour for that entry, same as always.
    content_sha256: Optional[str] = None


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
    #: Paths left untouched because their current on-disk content matches
    #: neither what this run wrote nor (for an "overwrite") the backed-up
    #: original -- something ELSE changed them since (audit 2026-11-11
    #: "B3": undoing run N after run N+1, or after the operator/Jellyfin/
    #: Radarr replaced the file by hand, used to silently clobber whatever
    #: was there with no warning). Pass ``force=True`` to undo() to revert
    #: these anyway.
    skipped: List[str] = field(default_factory=list)
    #: Set when there was no manifest to read at all and this is a
    #: best-effort reconstruction from the raw backup tree instead (audit
    #: 2026-11-11 "B2") -- every "create"d file is unrecoverable in that
    #: case (nothing records which paths those were), named here so the
    #: caller can say so rather than silently claiming a complete undo.
    incomplete: bool = False
    unrecoverable_note: Optional[str] = None


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


def run_epoch(run_id: str) -> float:
    """The submit-time epoch embedded in ``run-<epoch>-<hex>``.

    Used to order runs chronologically. A directory that does not follow the
    pattern (``verify-run-1`` and friends) sorts LAST, so a real run always
    wins over a hand-made one. Never the backup file's ``st_mtime``:
    ``backup()`` copies with ``shutil.copy2``, which preserves the SOURCE
    file's mtime -- so mtime says when the backed-up content was written,
    not which run saved it, and the two can disagree.

    Public (audit 2026-11-11, "B1"): ``engine.trash.sweep_to_trash`` used to
    order the SAME backup copies by ``st_mtime`` instead -- exactly the
    mistake this function's docstring already warned against -- so it could
    disagree with :func:`find_original_backup` about which copy is oldest
    and move the true pre-Finmeta original to trash (and eventually purge
    it) while a later, already-overlaid copy survived. One ordering
    function, used by both.
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
        epoch = run_epoch(name)
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
            found.append((run_epoch(run_dir.name), bk.stat().st_mtime, bk))
    if not found:
        return None
    found.sort(key=lambda row: (row[0], row[1]))
    return found[0][2]


def _undo_from_backups_only(run_id: str, *, config_dir: Path) -> UndoResult:
    """Best-effort undo with NO manifest to read (audit 2026-11-11 "B2").

    ``apply_plan`` now persists the manifest after every file it writes
    (see ``engine.executor``), so this path is only reached when a run
    crashed before its FIRST write ever completed -- a far narrower window
    than before that fix, but not zero (the process could still die
    between ``start_run`` and the first ``record()`` call).

    ``backup()`` only ever copies a file that already existed (an
    "overwrite" write) -- a "create" write has no backup at all, by
    construction -- so every file found under ``backups/<run_id>/`` can be
    safely restored as an overwrite. What CANNOT be recovered this way is
    which paths the run *created*: nothing on disk records that without
    the manifest, so those are left in place and ``incomplete`` is set so
    the caller can say so rather than claiming a complete undo.
    """
    bdir = _backups_dir(run_id, config_dir)
    result = UndoResult(incomplete=True)
    if not bdir.is_dir():
        result.unrecoverable_note = (
            f"no manifest and no backups found for run {run_id!r}; nothing to undo")
        return result
    for bk in bdir.rglob("*"):
        if not bk.is_file():
            continue
        rel = str(bk.relative_to(bdir))
        target = _abs_from_rel(rel, config_dir)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(bk, target)
        result.restored.append(str(target))
    result.unrecoverable_note = (
        f"run {run_id!r} had no manifest -- restored every backed-up "
        "original found on disk, but any file this run CREATED (rather "
        "than overwrote) cannot be identified without the manifest and "
        "was left in place")
    return result


def _sha256_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def undo(run_id: str, *, config_dir: Path = DEFAULT_CONFIG_DIR,
         force: bool = False) -> UndoResult:
    """Undo a run: restore overwritten originals, delete created files.

    Reads the run\u2019s manifest, copies each backed-up original back to its
    path, and unlinks every file the run created. Returns what was restored
    and what was deleted. Idempotent: a second call restores the same bytes
    and deletes nothing new.

    Content-checked (audit 2026-11-11 "B3"): a manifest entry records no
    more than a path and a status, with no way to tell whether the file at
    that path is still what THIS run left there. Undoing run N after run
    N+1 overwrote the same path, or after the operator/Jellyfin/Radarr
    replaced the file by hand, used to silently clobber whatever was there
    with no warning. Before touching a path, the current on-disk content is
    compared against ``RunFile.content_sha256`` (what this run wrote) and,
    for an "overwrite", also against the backup's own hash (so re-running
    undo after a PRIOR successful undo -- the file now holds the ORIGINAL,
    not what this run wrote -- still counts as "this run's own territory",
    preserving idempotency). A path matching neither is left alone and
    named in ``UndoResult.skipped`` instead of being touched. Older
    manifests with no recorded hash (``content_sha256 is None``) skip this
    check entirely, same as before this fix. ``force=True`` reverts
    unconditionally, bypassing the check.

    Falls back to :func:`_undo_from_backups_only` (``UndoResult.incomplete
    = True``) when there is no manifest at all, instead of raising -- see
    that function's docstring (no content check there: nothing records
    what the run itself wrote without the manifest).
    """
    try:
        manifest = load_manifest(run_id, config_dir=config_dir)
    except FileNotFoundError:
        return _undo_from_backups_only(run_id, config_dir=config_dir)
    bdir = _backups_dir(run_id, config_dir)
    result = UndoResult()
    for entry in manifest.files:
        target = Path(entry.path)
        if entry.status == "overwrite" and entry.backup_relpath:
            bk = bdir / entry.backup_relpath
            if not bk.is_file():
                continue
            if not force and entry.content_sha256 and target.is_file():
                current = _sha256_file(target)
                if current not in (entry.content_sha256, _sha256_file(bk)):
                    result.skipped.append(entry.path)
                    continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(bk, target)
            result.restored.append(entry.path)
        elif entry.status == "create":
            if not target.is_file():
                continue
            if not force and entry.content_sha256:
                current = _sha256_file(target)
                if current != entry.content_sha256:
                    result.skipped.append(entry.path)
                    continue
            target.unlink()
            result.deleted.append(entry.path)
        elif entry.status == "delete":
            # S9g3: a Coming Soon placeholder this run removed because the
            # real file had already arrived -- nothing to restore here
            # (resurrecting a stale placeholder over real content would be
            # wrong), so this is an intentional no-op.
            pass
    return result


def _plan_epoch(p: Path) -> float:
    """Epoch embedded in a plan filename -- the second ``-``-separated
    token, always the epoch whether the name is the legacy
    ``plan-<epoch>.json`` (``cli/main.py``, and ``api/run_service.py``
    before audit 2026-11-11 "D12") or the current ``plan-<epoch>-<hex>.
    json`` (``api/run_service.py`` since D12, reusing its run's own
    ``<epoch>-<hex>`` suffix so two runs in the same second can't
    collide). A name matching neither sorts LAST, same convention as
    :func:`run_epoch`."""
    try:
        return float(p.stem.split("-")[1])
    except (IndexError, ValueError):
        return float("inf")


def prune_runs_dir(config_dir: Path = DEFAULT_CONFIG_DIR, keep: int = 50) -> int:
    """Delete plan files and run dirs beyond the newest ``keep`` of each
    (audit 2026-11-11 "C1").

    ``config/runs/`` holds two artifact kinds that both only ever
    accumulate: ``plan-<epoch>.json`` files (one per ``finmeta plan``, read
    by ``engine.overlay._live_overlay_digests`` to decide which rendered
    posters the overlay cache still needs) and ``run-<epoch>-<hex>/
    manifest.json`` dirs (one per apply, read by :func:`undo`). Left
    unpruned, this is the actual root cause of the overlay cache's
    unbounded growth: a cache entry survives as long as ANY plan file still
    references it, and no plan file was ever deleted.

    Each kind is pruned independently, newest ``keep`` survive and
    everything older is deleted outright -- no trash/retention-days grace
    period, unlike ``engine.trash`` (these aren't originals anyone needs to
    recover; they're plan/manifest bookkeeping). Ordered the same way the
    rest of this module orders runs: a name that doesn't match the real
    ``plan-<epoch>.json`` / ``run-<epoch>-<hex>`` pattern sorts as newest
    (never pruned), same convention as :func:`run_epoch`.

    Pruning a run dir only affects :func:`undo` for that one run -- it
    falls back to :func:`_undo_from_backups_only`, which already handles
    "no manifest" -- and never touches ``config/backups/``, which has its
    own independent retention (``engine.trash``, keyed on supersession and
    age, not on this).

    ``keep <= 0`` disables pruning for both (same convention as
    ``backup_retention_days``). Best-effort per entry, like
    ``trash.sweep_to_trash``. Returns the total number removed.
    """
    runs_dir = Path(config_dir) / "runs"
    if keep <= 0 or not runs_dir.is_dir():
        return 0
    removed = 0

    plans = sorted(
        (p for p in runs_dir.glob("plan-*.json") if p.is_file()),
        key=lambda p: (_plan_epoch(p), p.stat().st_mtime), reverse=True,
    )
    for p in plans[keep:]:
        try:
            p.unlink()
            removed += 1
        except OSError:
            continue

    run_dirs = sorted(
        (d for d in runs_dir.iterdir() if d.is_dir()),
        key=lambda d: (run_epoch(d.name), d.stat().st_mtime), reverse=True,
    )
    for d in run_dirs[keep:]:
        try:
            shutil.rmtree(d)
            removed += 1
        except OSError:
            continue

    return removed


__all__ = [
    "Manifest", "RunFile", "UndoResult",
    "backup", "record", "load_manifest", "undo",
    "created_by_finmeta", "find_original_backup", "run_epoch",
    "prune_runs_dir", "DEFAULT_CONFIG_DIR",
]
