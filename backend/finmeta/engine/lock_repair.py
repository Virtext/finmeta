"""Repair series an older Finmeta locked in Jellyfin (2026-11-21).

Before 0eeb9b6, Finmeta's tvshow.nfo carried ``<lockdata>true``. Jellyfin
then locked the series and every episode added after it, and a locked
episode never reads its NFO -- new episodes showed only the series name and
a file date. Per series this:

1. strips the lockdata line from Finmeta's OWN tvshow.nfo (backed up and
   recorded in the run manifest, so ``finmeta undo`` reverts it) -- first,
   or the refresh below would re-lock the series from the file;
2. clears the Jellyfin lock on the series and its locked seasons/episodes;
3. refreshes only those unlocked children, one at a time with a pause --
   never a whole-series "replace all": on hundreds of series that floods
   Jellyfin's queue and the remote providers (AniDB bans flooding IPs).
"""
from __future__ import annotations

import hashlib
import logging
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from finmeta.engine.executor import _atomic_write, _write_allowed
from finmeta.engine.nfo import is_finmeta_nfo
from finmeta.engine.paths import show_dir
from finmeta.engine.runs import Manifest, RunFile, backup, record

logger = logging.getLogger("finmeta.engine.lock_repair")

#: The exact line every pre-0eeb9b6 Finmeta tvshow.nfo has (nfo._serialize).
LOCK_LINE = b"  <lockdata>true</lockdata>\n"

#: Seconds between two item refreshes and between two series. Jellyfin
#: queues refreshes and returns at once, so this pacing is what keeps its
#: queue (and the providers behind it) from being flooded.
REFRESH_PAUSE_S = 1.0
SERIES_PAUSE_S = 2.0


def strip_nfo_lock(nfo: Path) -> Optional[bytes]:
    """The NFO's bytes without Finmeta's lockdata line, or None when there is
    nothing of ours to change (missing, someone else's, or already clean)."""
    if not is_finmeta_nfo(nfo):
        return None
    data = nfo.read_bytes()
    return data.replace(LOCK_LINE, b"") if LOCK_LINE in data else None


def repair_locked_series(
    jf: Any, series_ids: List[str], *, mapper: Any, cfg: Any,
    config_dir: Path, run_id: str,
    log: Callable[[str, str], None],
    sleep: Callable[[float], None] = time.sleep,
) -> Dict[str, int]:
    """Repair each series in turn. One series failing never stops the rest."""
    manifest = Manifest(run_id=run_id)
    stats = {"series": 0, "items_unlocked": 0, "nfo_fixed": 0, "skipped": 0, "errors": 0}
    for n, series_id in enumerate(series_ids, 1):
        if n > 1:
            sleep(SERIES_PAUSE_S)
        name = series_id
        try:
            item = jf.get_item(series_id)
            name = item.name or series_id
            base = show_dir(item, mapper)
            nfo = base / "tvshow.nfo" if base else None
            stripped = strip_nfo_lock(nfo) if nfo else None
            if stripped is not None:
                if cfg.dry_run:
                    stats["skipped"] += 1
                    log("warn", f"{name}: skipped -- dry_run is on and its tvshow.nfo still "
                                "locks it (unlocking now would only re-lock on refresh)")
                    continue
                if not _write_allowed(nfo, cfg):
                    stats["errors"] += 1
                    log("error", f"{name}: {nfo} is outside every path_mappings local root")
                    continue
                rel = backup(run_id, nfo, config_dir=config_dir)
                _atomic_write(nfo, stripped)
                manifest.files.append(RunFile(
                    path=str(nfo), status="overwrite", backup_relpath=rel,
                    content_sha256=hashlib.sha256(stripped).hexdigest()))
                stats["nfo_fixed"] += 1
            kids = jf.series_children(series_id)
            locked = [c.id for c in kids if c.lock_data]
            # An episode that is no longer locked can still be frozen at the
            # series name (seen live: unlocked, never re-read). Re-read those
            # too; one that already has its real title is left alone.
            frozen = [c.id for c in kids if not c.lock_data and c.type == "Episode"
                      and item.name and c.name == item.name]
            for item_id in [series_id, *locked]:
                jf.unlock_item(item_id)
            for i, item_id in enumerate(locked + frozen):
                if i:
                    sleep(REFRESH_PAUSE_S)
                # Episode-level replace: its NFO (Sonarr's) is the source of
                # truth for a title that was frozen at "series name + date".
                jf.refresh_item(item_id, image_refresh_mode="Default",
                                replace_all_metadata=True, replace_all_images=False)
            stats["series"] += 1
            stats["items_unlocked"] += 1 + len(locked)
            log("info", f"{name}: unlocked series + {len(locked)} season(s)/episode(s), "
                        f"re-read {len(locked) + len(frozen)}"
                        + (", tvshow.nfo lock removed" if stripped is not None else ""))
        except Exception as exc:
            stats["errors"] += 1
            logger.warning("lock repair failed for %s: %s", name, exc)
            log("error", f"{name}: repair failed: {exc}")
    if manifest.files:
        record(manifest, config_dir=config_dir)
    return stats


__all__ = ["LOCK_LINE", "REFRESH_PAUSE_S", "SERIES_PAUSE_S",
           "strip_nfo_lock", "repair_locked_series"]
