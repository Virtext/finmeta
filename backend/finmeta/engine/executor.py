"""Executor: apply a WritePlan to disk + Jellyfin (SPEC §10, override O5).

Order of operations:
  1. Refuse when ``cfg.dry_run`` is true.
  2. Back up every existing file that will be overwritten.
  3. Atomic file writes (temp + rename, mode 0644, readable by uid 99).
  4. API actions (collections / playlists, idempotent).
  5. Per-item refresh.
  6. Poll until ImageTags / ThemeSongs reflect the change (timeout).
  7. Write manifest (runs.record) + job log (SQLite).

The executor is pure logic: every dependency is injected.
"""
from __future__ import annotations

import hashlib
import logging
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from finmeta.config.models import Config, MovieLibrary, TvLibrary
from finmeta.engine.jobs import DEFAULT_DB_PATH, finish_run, init_db, log, start_run
from finmeta.engine.nfo import (
    DEFAULT_MOVIE_FIELDS, DEFAULT_TV_FIELDS,
    episode_nfo, movie_nfo, nfo_sha256, season_nfo, tvshow_nfo,
)
from finmeta.engine import trash
from finmeta.engine.overlay import prune_overlay_cache, read_overlay_cache
from finmeta.engine.plan import ApiAction, FileWrite, ItemPlan, WritePlan
from finmeta.engine.runs import (
    DEFAULT_CONFIG_DIR, Manifest, RunFile, backup, record,
)
from finmeta.jellyfin_client.client import JellyfinClient
from finmeta.sources.tmdb import TmdbClient

logger = logging.getLogger("finmeta.executor")


class DryRunRefused(RuntimeError):
    """Raised when ``cfg.dry_run`` is true and an apply is attempted."""

    def __init__(self, plan_path: str = "") -> None:
        msg = "dry_run is true; run 'finmeta plan' first, then set dry_run: false"
        if plan_path:
            msg += f"; plan written to {plan_path}"
        super().__init__(msg)


@dataclass
class ApplyResult:
    run_id: str
    files_written: int = 0
    files_unchanged: int = 0
    api_calls: int = 0
    errors: List[str] = field(default_factory=list)


# -- content fetching -------------------------------------------------------

def _nfo_bytes(item: ItemPlan, cfg: Config, tmdb: TmdbClient) -> bytes:
    """Re-generate NFO bytes for a 'generated' FileWrite."""
    language = cfg.language
    # Pick the right library config (first match on media type).
    lib = None
    for lc in cfg.libraries.values():
        if item.media_type == "movie" and isinstance(lc, MovieLibrary):
            lib = lc
            break
        if item.media_type == "tvshow" and isinstance(lc, TvLibrary):
            lib = lc
            break
    _default = (DEFAULT_MOVIE_FIELDS if item.media_type == "movie"
                else DEFAULT_TV_FIELDS)
    fields = (lib.metadata.fields if lib else None) or _default
    if item.media_type == "movie":
        details = tmdb.movie_details(item.tmdb_id, language=language)
        return movie_nfo(details, item.tmdb_id, fields)
    else:
        details = tmdb.tv_details(item.tmdb_id, language=language)
        return tvshow_nfo(details, item.tmdb_id, fields)


def _fetch_content(
    fw: FileWrite,
    item: ItemPlan,
    cfg: Config,
    tmdb: TmdbClient,
    *,
    config_dir: Path = DEFAULT_CONFIG_DIR,
) -> bytes:
    """Return the bytes to write for one FileWrite.

    An overlaid poster's ``source_ref`` points at the plain (un-overlaid)
    original -- O7 needs that for future re-renders, not the overlaid bytes
    themselves -- so it is checked first against the overlay cache (by its
    own content_sha256, written at plan-build time by
    ``engine.overlay.overlay_file_write``). A cache miss falls through to the
    normal source_ref fetch, same as any other file kind.
    """
    cached = read_overlay_cache(fw.content_sha256, config_dir=config_dir)
    if cached is not None:
        return cached
    if fw.source_ref == "generated":
        return _nfo_bytes(item, cfg, tmdb)
    if fw.source_ref.startswith(("http://", "https://")):
        import httpx
        resp = httpx.get(fw.source_ref, timeout=30, follow_redirects=True)
        resp.raise_for_status()
        return resp.content
    p = Path(fw.source_ref)
    if p.is_file():
        return p.read_bytes()
    raise FileNotFoundError(f"source_ref not found: {fw.source_ref}")


# -- atomic write -----------------------------------------------------------

def _atomic_write(target: Path, content: bytes) -> None:
    """Write ``content`` to ``target`` atomically (temp + rename, mode 0644)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(target.parent), prefix=".finmeta_")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(content)
        os.chmod(tmp_path, 0o644)
        os.rename(tmp_path, str(target))
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _maybe_lock_artwork(fw: FileWrite, item_id: str, config_dir: Path) -> None:
    """S9d5: on an image kind's first successful write/upload, pin the
    candidate it used (``fw.meta["lock_ref"]``/``"lock_sha256"``, set by
    ``engine.plan_builder``/``engine.overlay.overlay_file_write``) so later
    runs never re-query TMDb/fanart for this item+kind again -- only a
    manual override or an explicit "re-pick" (``OverrideStore.clear_lock``,
    via ``POST /api/items/{id}/reshuffle``) changes it after that.

    Never overwrites an existing lock (that is the whole point -- "first
    run locks it"). Best-effort: any failure is swallowed, same safety
    pattern as the other overrides.db helpers in ``engine.plan_builder``.
    """
    if fw.kind not in ("poster", "backdrop", "logo", "landscape"):
        return
    ref = fw.meta.get("lock_ref")
    sha = fw.meta.get("lock_sha256")
    if not ref or not sha:
        return
    try:
        from finmeta.api.overrides import OverrideStore
        store = OverrideStore(Path(config_dir) / "overrides.db")
        if store.get_lock(item_id, fw.kind) is None:
            store.set_lock(item_id, fw.kind, str(ref), str(sha))
    except Exception:
        logger.warning("artwork lock write failed for %s/%s", item_id, fw.kind, exc_info=True)


# -- API actions (idempotent) -------------------------------------------------

def _apply_api_action(action: ApiAction, jf: JellyfinClient) -> int:
    """Execute one ApiAction idempotently. Returns the number of API calls made."""
    calls = 0
    if action.kind == "collection" and action.action == "sync":
        # S9f2: full membership reconciliation, used by preset collections.
        # Unlike the "create" branch below (additive-only), this removes
        # members that no longer belong and deletes the Jellyfin collection
        # outright once it would have zero members (e.g. a seasonal preset
        # outside its active month) instead of leaving an empty collection
        # behind -- recreated automatically next time add_ids is non-empty.
        existing = jf.get_collection_by_name(action.name)
        wanted = set(action.add_ids)
        if existing is None:
            if wanted:
                jf.create_collection(action.name, action.add_ids)
                calls += 1
        else:
            if not wanted:
                jf.delete_collection(existing.id)
                calls += 1
            else:
                current = {it.id for it in jf.get_items(parent_id=existing.id, recursive=False)}
                remove_ids = current - wanted
                add_ids = wanted - current
                if remove_ids:
                    jf.remove_from_collection(existing.id, list(remove_ids))
                    calls += 1
                if add_ids:
                    jf.add_to_collection(existing.id, list(add_ids))
                    calls += 1
    elif action.kind == "collection":
        existing = jf.get_collection_by_name(action.name)
        if existing is None and action.action == "create":
            if action.add_ids:
                jf.create_collection(action.name, action.add_ids)
                calls += 1
        elif existing is not None:
            cid = existing.id
            if action.remove_ids:
                jf.remove_from_collection(cid, action.remove_ids)
                calls += 1
            if action.add_ids:
                jf.add_to_collection(cid, action.add_ids)
                calls += 1
    elif action.kind == "playlist":
        existing = jf.get_playlist_by_name(action.name)
        if existing is None and action.action == "create":
            if action.add_ids:
                jf.create_playlist(action.name, action.add_ids)
                calls += 1
        elif existing is not None:
            pid = existing.id
            if action.remove_ids:
                jf.remove_from_playlist(pid, action.remove_ids)
                calls += 1
            if action.add_ids:
                jf.add_to_playlist(pid, action.add_ids)
                calls += 1
    return calls


# -- poll for refresh -------------------------------------------------------

def _poll_refresh(
    jf: JellyfinClient,
    item_ids: Sequence[str],
    theme_song_ids: Sequence[str],
    *,
    poll_interval: float = 2.0,
    timeout: float = 120.0,
    pre_tags: Optional[Dict[str, Optional[str]]] = None,
) -> bool:
    """Poll until ImageTags are set for item_ids and ThemeSongs exist for
    theme_song_ids. Returns True when all checks pass, False on timeout.

    ``pre_tags`` (2026-10-25, operator-reported "every apply with an image
    change takes ~2 extra minutes"): the baseline ImageTag to compare
    against, captured by the CALLER before it triggers the refresh. Must not
    be captured in here -- Jellyfin's single-item ``/Items/{id}/Refresh``
    commonly completes synchronously (the tag is already updated by the time
    that HTTP call returns), so capturing the baseline after the refresh (as
    this function used to do internally) reads the ALREADY-NEW tag as the
    baseline -- the tag then never looks like it "changed" and every apply
    with a real image write burned the full ``timeout`` for no reason, even
    though the write had already succeeded. ``None`` (the default) falls
    back to capturing it here for any caller that genuinely polls before
    triggering its own refresh elsewhere."""
    deadline = time.monotonic() + timeout
    if pre_tags is None:
        # Capture the set of items that already have image tags (should be
        # none after a clean bootstrap, but be safe).
        pre_tags = {}
        for iid in item_ids:
            try:
                it = jf.get_item(iid)
                pre_tags[iid] = it.image_tags
            except Exception:
                pre_tags[iid] = None

    while True:
        ok_images = True
        for iid in item_ids:
            try:
                it = jf.get_item(iid)
                if it.image_tags is None or it.image_tags == pre_tags.get(iid):
                    ok_images = False
            except Exception:
                ok_images = False
        ok_songs = True
        for iid in theme_song_ids:
            try:
                songs = jf.get_theme_songs(iid)
                if not songs:
                    ok_songs = False
            except Exception:
                ok_songs = False
        if ok_images and ok_songs:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll_interval)


# -- main entry point ---------------------------------------------------------

def apply_plan(
    plan: WritePlan,
    run_id: str,
    cfg: Config,
    jf: JellyfinClient,
    tmdb: TmdbClient,
    *,
    config_dir: Path = DEFAULT_CONFIG_DIR,
    db_path: Path = DEFAULT_DB_PATH,
    poll_timeout: float = 120.0,
    plan_path: str = "",
) -> ApplyResult:
    """Apply a WritePlan: backups, file writes, API actions, refresh, poll,
    manifest + job log. Returns an :class:`ApplyResult`.

    Raises :class:`DryRunRefused` when ``cfg.dry_run`` is true.
    """
    if cfg.dry_run:
        raise DryRunRefused(plan_path)

    init_db(db_path)
    plan_sha = hashlib.sha256(plan.to_json().encode()).hexdigest()[:16]
    start_run(db_path, run_id, plan_sha)
    log(db_path, run_id, "info", f"apply started plan_sha={plan_sha}")

    result = ApplyResult(run_id=run_id)
    manifest = Manifest(run_id=run_id)
    item_by_id: Dict[str, ItemPlan] = {ip.item_id: ip for ip in plan.items}

    # -- 1+2: backups + atomic file writes ----------------------------------
    for item in plan.items:
        for fw in item.files:
            if fw.status == "unchanged":
                result.files_unchanged += 1
                # S9d5: the on-disk/Jellyfin content already matches this
                # FileWrite's candidate -- safe (and necessary) to (re-)lock
                # here too, not just on an actual write. Otherwise a re-pick
                # that happens to resolve back to the same bytes (e.g.
                # best_rated, which reshuffle can't change) would leave the
                # item permanently unlocked -- "unchanged" never reaching the
                # write branch below where locking normally happens.
                _maybe_lock_artwork(fw, item.item_id, config_dir)
                continue
            # Fetch content
            try:
                content = _fetch_content(fw, item, cfg, tmdb, config_dir=config_dir)
            except Exception as exc:
                msg = f"fetch failed for {fw.target_path}: {exc}"
                logger.warning(msg)
                result.errors.append(msg)
                log(db_path, run_id, "error", msg)
                continue
            # Validate hash
            actual_sha = hashlib.sha256(content).hexdigest()
            if actual_sha != fw.content_sha256:
                msg = (f"hash mismatch for {fw.target_path}: "
                       f"expected={fw.content_sha256[:12]}… actual={actual_sha[:12]}…")
                logger.warning(msg)
                result.errors.append(msg)
                log(db_path, run_id, "warn", msg)
                continue

            target = Path(fw.target_path)
            is_overwrite = target.is_file()
            rel = backup(run_id, target, config_dir=config_dir) if is_overwrite else None
            try:
                _atomic_write(target, content)
            except Exception as exc:
                msg = f"write failed for {fw.target_path}: {exc}"
                logger.error(msg)
                result.errors.append(msg)
                log(db_path, run_id, "error", msg)
                continue
            status = "overwrite" if is_overwrite else "create"
            manifest.files.append(RunFile(
                path=str(target), status=status,
                backup_relpath=rel,
            ))
            result.files_written += 1
            log(db_path, run_id, "info", f"wrote {status} {fw.kind} {target}")
            _maybe_lock_artwork(fw, item.item_id, config_dir)

    # -- 3: API actions -----------------------------------------------------
    for action in plan.api_actions:
        try:
            n = _apply_api_action(action, jf)
            result.api_calls += n
            manifest.api_actions.append(action.model_dump())
            log(db_path, run_id, "info",
                f"api {action.action} {action.kind} {action.name!r} calls={n}")
        except Exception as exc:
            msg = f"api action failed {action.name!r}: {exc}"
            logger.error(msg)
            result.errors.append(msg)
            log(db_path, run_id, "error", msg)

    # -- 4+5: per-item refresh + poll --------------------------------------
    # Only refresh/poll items that actually had an image or theme-song file
    # written in this run. A no-op apply (every FileWrite status ==
    # "unchanged") must not refresh or poll (no 120s timeout).
    image_items: List[str] = []
    theme_items: List[str] = []
    for item in plan.items:
        # Only files actually written this run (status != "unchanged") matter.
        written = [fw for fw in item.files if fw.status != "unchanged"]
        image_writes = [fw for fw in written
                        if fw.kind in ("poster", "backdrop", "logo", "landscape")]
        has_theme = any(fw.kind == "theme_song" for fw in written)
        if image_writes:
            image_items.append(item.item_id)
        if has_theme:
            theme_items.append(item.item_id)

    # Baseline ImageTags, captured BEFORE the refresh calls below -- see
    # _poll_refresh's docstring for why this must happen first, not inside
    # _poll_refresh after the (often-synchronous) refresh already ran.
    pre_image_tags: Dict[str, Optional[str]] = {}
    for iid in image_items:
        try:
            pre_image_tags[iid] = jf.get_item(iid).image_tags
        except Exception:
            pre_image_tags[iid] = None

    for iid in set(image_items + theme_items):
        try:
            jf.refresh_item(iid)
            log(db_path, run_id, "info", f"refresh {iid}")
        except Exception as exc:
            logger.warning("refresh failed for %s: %s", iid, exc)

    if image_items or theme_items:
        settled = _poll_refresh(
            jf, image_items, theme_items, timeout=poll_timeout,
            pre_tags=pre_image_tags,
        )
        if not settled:
            log(db_path, run_id, "warn",
                f"poll timeout: ImageTags/ThemeSongs not confirmed after {poll_timeout:.0f}s")

    # -- 7: manifest + job log ---------------------------------------------
    record(manifest, config_dir=config_dir)
    # S9d5: trash + retention (CLAUDE.md §4.1) -- best-effort, never fails
    # an otherwise-successful apply.
    try:
        swept = trash.sweep(config_dir, cfg.backup_retention_days)
        pruned_cache = prune_overlay_cache(config_dir)
        if swept["moved_to_trash"] or swept["purged"] or pruned_cache:
            log(db_path, run_id, "info",
                f"backup trash: moved={swept['moved_to_trash']} purged={swept['purged']}"
                f" overlay_cache_pruned={pruned_cache}")
    except Exception as exc:
        logger.warning("backup trash sweep failed: %s", exc)
    status = "ok" if not result.errors else "partial"
    finish_run(
        db_path, run_id, status,
        files_written=result.files_written,
        files_unchanged=result.files_unchanged,
        api_calls=result.api_calls,
    )
    log(db_path, run_id, "info",
        f"apply finished status={status} written={result.files_written} "
        f"unchanged={result.files_unchanged} api={result.api_calls} errors={len(result.errors)}")
    return result


__all__ = ["ApplyResult", "DryRunRefused", "apply_plan"]
