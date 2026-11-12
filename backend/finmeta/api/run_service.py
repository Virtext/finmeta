"""Run execution (SPEC §10): background worker for plan/apply runs.

``submit_run`` claims the single-run slot and spawns a daemon worker thread.
``GET /api/jobs/<id>`` reads the SQLite job log the worker writes.
"""
from __future__ import annotations

import hashlib
import logging
import time
import uuid
from pathlib import Path
from typing import Optional, Tuple

from finmeta.config.models import Config
from finmeta.engine.jobs import finish_run, init_db, log, start_run
from finmeta.engine import scan_store
from finmeta.engine.plan_builder import build_sonarr_index
from finmeta.engine.scanner import ScanSettings, scan_library
from finmeta.engine.executor import DryRunRefused, apply_plan
from finmeta.engine.plan import WritePlan

from finmeta.api.state import AppContext

logger = logging.getLogger("finmeta.api.runs")


def _new_run_id() -> str:
    return f"run-{int(time.time())}-{uuid.uuid4().hex[:6]}"


def submit_run(
    context: AppContext, action: str, *,
    library: Optional[str] = None, item_id: Optional[str] = None,
    tier: int = 1,
) -> Tuple[Optional[str], Optional[str]]:
    """Claim the run slot and start a worker. Returns (run_id, error).

    ``tier`` only means anything for ``action="scan"`` (SPEC_S10 §A): 1 is the
    cheap Jellyfin+disk pass, 2 adds the provider-backed checks.
    """
    run_id = _new_run_id()
    if not context.claim_run(run_id, action=action, library=library, item_id=item_id):
        return None, "a run is already in progress"
    import threading
    t = threading.Thread(
        target=_worker, args=(context, run_id, action, library, item_id, tier),
        daemon=True)
    t.start()
    return run_id, None


def _scope_args(library: Optional[str], item_id: Optional[str]):
    return library, item_id


def scope_label(library: Optional[str], item_id: Optional[str]) -> str:
    """The canonical scope string: "item:<id>" | "lib:<name>" | "all".

    One spelling for the run log AND the ``runs.scope`` column (S11a), so the
    Activity Center can parse a scope back into a retry.
    """
    if item_id:
        return f"item:{item_id}"
    if library:
        return f"lib:{library}"
    return "all"


def scope_parts(scope: Optional[str]) -> tuple:
    """Inverse of :func:`scope_label`: ``(library, item_id)``."""
    if not scope or scope == "all":
        return None, None
    if scope.startswith("item:"):
        return None, scope[len("item:"):] or None
    if scope.startswith("lib:"):
        return scope[len("lib:"):] or None, None
    return None, None


def _write_plan_json(context: AppContext, plan: WritePlan, run_id: str) -> Path:
    """Write ``plan`` to ``config/runs/plan-<epoch>-<hex>.json``.

    Audit 2026-11-11 "D12": this used to name the file
    ``plan-<int(time.time())>.json`` -- two runs starting in the same
    second collided, one plan silently overwriting the other on disk
    (and ``_do_apply`` duplicated this exact logic inline instead of
    calling this function, so the bug existed in two places). Reuses
    ``run_id``'s own ``<epoch>-<hex>`` suffix (``run_id`` is
    ``run-<epoch>-<hex>``, see ``_new_run_id`` -- the hex half is what
    actually makes it collision-free) rather than inventing a new one, so
    the plan file is also directly correlatable to the run it belongs to.
    The epoch stays the leading, fixed-width component so this still
    sorts chronologically wherever something orders these filenames as
    plain strings (``cli/main.py``'s "find the most recent plan" does
    exactly that, not by mtime) -- only what follows the epoch changed.
    """
    runs_dir = context.config_dir / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    suffix = run_id.split("-", 1)[1] if run_id.startswith("run-") else run_id
    p = runs_dir / f"plan-{suffix}.json"
    p.write_text(context.redact(plan.to_json()) + "\n")
    return p


def _set_plan_sha(context: AppContext, run_id: str, plan_sha: str) -> None:
    import sqlite3
    conn = sqlite3.connect(str(context.db_path))
    try:
        conn.execute("UPDATE runs SET plan_sha256=? WHERE run_id=?", (plan_sha, run_id))
        conn.commit()
    finally:
        conn.close()


def _progress_logger(db, run_id: str, label: str, every: int = 5):
    """A build_plan ``progress_cb`` that logs every ``every``-th item (and the
    last one), not every single one -- a plan/apply run used to log exactly
    ONE line ("started") until it finished, however long that took, with no
    cap on how many items a single run processes. A large library-wide run
    with several rating/image sources enabled can legitimately take many
    minutes; with zero visibility in between, that looked indistinguishable
    from a hung job (reported live). Throttled so a huge library doesn't
    turn this into one job-log row per item."""
    def _cb(done: int, total: int) -> None:
        if total and (done == total or done % every == 0):
            log(db, run_id, "info", f"{label} progress {done}/{total}")
    return _cb


def _format_counts_summary(plan) -> str:
    """Aggregate per-rule-key counts across plan items for the run log (K9).

    Returns e.g. "dynamic_range: dovi_hdr10=2 hdr10=5 none=3; resolution: res_4k=3"
    -- only categories present on any item are shown; an empty-string rule key tallies as ``none``.
    Empty string when no overlay detection facts exist at all."""
    from collections import Counter
    cats = ("dynamic_range", "resolution", "audio")
    tallies = {c: Counter() for c in cats}
    cat_present = set()
    for ip in plan.items:
        fc = getattr(ip, "format_counts", None) or {}
        if not isinstance(fc, dict):
            continue
        for cat in cats:
            key = fc.get(cat)
            if key is None:
                continue
            cat_present.add(cat)
            tallies[cat][key if key else "none"] += 1
    parts = []
    for cat in cats:
        cnt = tallies[cat]
        total = sum(cnt.values())
        # Show the category only if it was detected on >=1 item (matched OR explicit none).
        if total == 0 and cat not in cat_present:
            continue
        inner = ", ".join(f"{k}={v}" for k, v in sorted(cnt.items()))
        parts.append(f"{cat}: {inner}")
    return "; ".join(parts)


def _do_plan(context: AppContext, run_id: str, cfg: Config,
             library: Optional[str], item_id: Optional[str]) -> None:
    db = context.db_path
    init_db(db)
    scope = scope_label(library, item_id)
    start_run(db, run_id, "", action="plan", scope=scope)
    log(db, run_id, "info", f"plan started scope={scope}")
    try:
        plan = context.get_plan(library=library, item_id=item_id,
                                progress_cb=_progress_logger(db, run_id, "plan"))
    except Exception as exc:
        log(db, run_id, "error", context.redact(f"plan failed: {exc}"))
        finish_run(db, run_id, "error")
        return
    _set_plan_sha(context, run_id,
                  hashlib.sha256(plan.to_json().encode()).hexdigest()[:16])
    n_files = sum(len(ip.files) for ip in plan.items)
    fmt_part = _format_counts_summary(plan)
    suffix = (" format=" + fmt_part) if fmt_part else ""
    plan_path = _write_plan_json(context, plan, run_id)
    # S9d2-fix2 step 6b: provider errors must be visible in the run log.
    for w in getattr(plan, "provider_warnings", []):
        log(db, run_id, "warn",
            f"provider warning: {w.provider} — {w.error} ({w.count} item(s) affected)")
    log(db, run_id, "info",
        f"plan ok items={len(plan.items)} files={n_files} "
        f"unmatched={len(plan.unmatched)} actions={len(plan.api_actions)}{suffix}"
        f" -> {plan_path}")
    finish_run(db, run_id, "ok")


def _do_apply(context: AppContext, run_id: str, cfg: Config,
              library: Optional[str], item_id: Optional[str]) -> None:
    db = context.db_path
    init_db(db)
    scope = scope_label(library, item_id)
    # Record the run up front so it is always visible, even if the plan build
    # below throws before apply_plan ever re-records it.
    start_run(db, run_id, "", action="apply", scope=scope)
    jf = context.make_jellyfin(cfg)
    tmdb = context.make_tmdb(cfg)
    try:
        plan = context.get_plan(library=library, item_id=item_id,
                                progress_cb=_progress_logger(db, run_id, "apply"))
        plan_path = _write_plan_json(context, plan, run_id)
        # Reuse the runs row inserted at the top of this function (start_run is
        # INSERT-only); apply_plan would double-insert, so record the sha here.
        _set_plan_sha(context, run_id,
                      hashlib.sha256(plan.to_json().encode()).hexdigest()[:16])
        # Was hardcoded to "lib:..." even for an item-scoped apply (every
        # webhook/fix run), which read as a whole-library run in the log.
        log(db, run_id, "info", f"apply started scope={scope}")
        apply_plan(
            plan, run_id, cfg, jf, tmdb,
            config_dir=context.config_dir, db_path=db,
            plan_path=str(plan_path),
        )
    except DryRunRefused as exc:
        log(db, run_id, "warn", context.redact(f"apply refused: {exc}"))
        finish_run(db, run_id, "refused")
    except Exception as exc:
        log(db, run_id, "error", context.redact(f"apply failed: {exc}"))
        finish_run(db, run_id, "error")
    finally:
        jf.close()
        try:
            tmdb.close()
        except Exception:
            pass


def _do_scan(context: AppContext, run_id: str, cfg: Config,
             library: Optional[str], tier: int = 1) -> None:
    """Run the health scanner over one or every configured library (SPEC_S10).

    A scan is a run like any other -- same slot, same ``runs``/``job_log``
    rows, same progress logging -- so ``scan_id`` IS this ``run_id``. It is
    read-only: nothing is written to the media tree, Jellyfin, Sonarr or
    Radarr, which is why it ignores ``dry_run`` entirely.

    One library failing (unreachable, renamed in Jellyfin) does not abort the
    rest: the failure is logged and the scan carries on, ending "error" so the
    UI can say the results are partial.
    """
    db = context.db_path
    init_db(db)
    scope = scope_label(library, None)
    start_run(db, run_id, "", action="scan", scope=scope, tier=int(tier))
    scan_store.start_scan(db, run_id, library=library, tier=int(tier))
    log(db, run_id, "info", f"scan started scope={scope} tier={tier}")

    names = [library] if library else list(cfg.libraries)
    settings = ScanSettings.from_config(cfg, tier=int(tier))
    jf = context.make_jellyfin(cfg)
    mapper = context.make_mapper(cfg)
    # Tier 2 only: SC2's match checks need TMDb and SC5 needs Sonarr. A
    # tier-1 scan must not even build those clients -- "no provider traffic"
    # is the tier-1 contract, not merely a cost target.
    tmdb = context.make_tmdb(cfg) if settings.deep else None
    sonarr_index: dict = {}
    if settings.deep and settings.enabled("episodes"):
        try:
            sonarr_index = build_sonarr_index(cfg, context.secrets)
        except Exception as exc:
            log(db, run_id, "warn",
                context.redact(f"scan: Sonarr lookup unavailable: {exc}"))
    status = "ok"
    scanned = 0
    findings = 0
    try:
        for name in names:
            lib_cfg = cfg.libraries.get(name)
            if lib_cfg is None:
                log(db, run_id, "warn", f"scan: unknown library {name!r} -- skipped")
                status = "error"
                continue

            def _emit(batch, _name=name):
                scan_store.add_findings(db, run_id, batch, redact=context.redact)

            try:
                res = scan_library(
                    jf, library=name, lib_cfg=lib_cfg, mapper=mapper, emit=_emit,
                    settings=settings, tmdb=tmdb, sonarr_index=sonarr_index,
                    language=cfg.language,
                    progress_cb=_progress_logger(db, run_id, f"scan {name}", every=1),
                )
            except Exception as exc:
                log(db, run_id, "error", context.redact(f"scan failed for {name}: {exc}"))
                status = "error"
                continue
            if res.skipped_reason:
                log(db, run_id, "info", f"scan {name}: skipped ({res.skipped_reason})")
                continue
            scan_store.bump_items_scanned(db, run_id, res.items_scanned)
            scanned += res.items_scanned
            findings += res.findings
            log(db, run_id, "info",
                f"scan {name}: {res.items_scanned} items, {res.findings} finding(s)")
    finally:
        jf.close()
        if tmdb is not None:
            try:
                tmdb.close()
            except Exception:
                pass
        # One client instance is shared across every tvdbId from the same
        # server, so dedupe before closing (same as build_plan does).
        for client, _row in {id(c): (c, r)
                             for c, r in sonarr_index.values()}.values():
            try:
                client.close()
            except Exception:
                pass

    scan_store.finish_scan(db, run_id, status)
    pruned = scan_store.prune_scans(db, keep=getattr(cfg.scan, "keep_scans",
                                                      scan_store.DEFAULT_KEEP_SCANS))
    log(db, run_id, "info",
        f"scan {status} items={scanned} findings={findings}"
        + (f" (pruned {pruned} old scan(s))" if pruned else ""))
    finish_run(db, run_id, status)


def _worker(context: AppContext, run_id: str, action: str,
            library: Optional[str], item_id: Optional[str],
            tier: int = 1) -> None:
    cfg = context.config
    try:
        if action == "plan":
            _do_plan(context, run_id, cfg, library, item_id)
        elif action == "apply":
            _do_apply(context, run_id, cfg, library, item_id)
        elif action == "scan":
            _do_scan(context, run_id, cfg, library, tier)
        else:
            raise ValueError(f"unknown action {action!r}")
    except Exception as exc:  # last-resort guard
        logger.exception("run %s crashed", run_id)
        try:
            finish_run(context.db_path, run_id, "error")
            log(context.db_path, run_id, "error", context.redact(f"run crashed: {exc}"))
        except Exception:
            pass
    finally:
        context.release_run(run_id)
        # Drain one queued webhook item (see state.AppContext.queue_webhook_item)
        # now that the slot is free -- chains until the queue is empty, one
        # run at a time, same single-run-slot rule as everything else. If
        # something else (a human click, the scheduler) wins the slot first,
        # put the item back rather than drop it -- it tries again after
        # THAT run releases the slot too.
        next_item = context.pop_webhook_item()
        if next_item is not None:
            new_run_id, err = submit_run(context, "apply", item_id=next_item)
            if new_run_id is None:
                context.queue_webhook_item(next_item)
        else:
            # Nothing from the webhook -- take the next library a multi-library
            # schedule left queued (state.AppContext.queue_library_run). Webhook
            # items go first: each is one newly added title and finishes in
            # seconds, while a library-wide run can take many minutes.
            queued = context.pop_library_run()
            if queued is not None:
                q_action, q_library, q_tier = queued
                new_run_id, err = submit_run(context, q_action, library=q_library,
                                             tier=q_tier)
                if new_run_id is None:
                    context.queue_library_run(q_action, q_library, q_tier)
                else:
                    logger.info("queued schedule started run %s (%s %s)",
                                new_run_id, q_action, q_library)


__all__ = ["submit_run", "scope_label", "scope_parts"]
