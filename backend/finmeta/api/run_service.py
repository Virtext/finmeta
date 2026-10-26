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
from finmeta.engine.executor import DryRunRefused, apply_plan
from finmeta.engine.plan import WritePlan

from finmeta.api.state import AppContext

logger = logging.getLogger("finmeta.api.runs")


def _new_run_id() -> str:
    return f"run-{int(time.time())}-{uuid.uuid4().hex[:6]}"


def submit_run(
    context: AppContext, action: str, *,
    library: Optional[str] = None, item_id: Optional[str] = None,
) -> Tuple[Optional[str], Optional[str]]:
    """Claim the run slot and start a worker. Returns (run_id, error)."""
    run_id = _new_run_id()
    if not context.claim_run(run_id):
        return None, "a run is already in progress"
    import threading
    t = threading.Thread(
        target=_worker, args=(context, run_id, action, library, item_id), daemon=True)
    t.start()
    return run_id, None


def _scope_args(library: Optional[str], item_id: Optional[str]):
    return library, item_id


def _write_plan_json(context: AppContext, plan: WritePlan) -> Path:
    runs_dir = context.config_dir / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    p = runs_dir / f"plan-{int(time.time())}.json"
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
    saw_any_cat = False
    cat_present = set()
    for ip in plan.items:
        fc = getattr(ip, "format_counts", None) or {}
        if not isinstance(fc, dict):
            continue
        for cat in cats:
            key = fc.get(cat)
            if key is None:
                continue
            saw_any_cat = True
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
    scope = f"item:{item_id}" if item_id else f"lib:{library or '*'}"
    start_run(db, run_id, "")
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
    plan_path = _write_plan_json(context, plan)
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
    start_run(db, run_id, "")   # record the run up front so it is always visible
    jf = context.make_jellyfin(cfg)
    tmdb = context.make_tmdb(cfg)
    try:
        plan = context.get_plan(library=library, item_id=item_id,
                                progress_cb=_progress_logger(db, run_id, "apply"))
        runs_dir = context.config_dir / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        plan_path = runs_dir / f"plan-{int(time.time())}.json"
        plan_path.write_text(context.redact(plan.to_json()) + "\n")
        # Reuse the runs row inserted at the top of this function (start_run is
        # INSERT-only); apply_plan would double-insert, so record the sha here.
        _set_plan_sha(context, run_id,
                      hashlib.sha256(plan.to_json().encode()).hexdigest()[:16])
        log(db, run_id, "info", f"apply started scope=lib:{library or '*'}")
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


def _worker(context: AppContext, run_id: str, action: str,
            library: Optional[str], item_id: Optional[str]) -> None:
    cfg = context.config
    try:
        if action == "plan":
            _do_plan(context, run_id, cfg, library, item_id)
        elif action == "apply":
            _do_apply(context, run_id, cfg, library, item_id)
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


__all__ = ["submit_run"]
