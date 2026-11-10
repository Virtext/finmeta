"""Activity Center: what is running, what is queued, what already ran (SPEC_S11 §B).

Until this existed, a run was only visible to the browser tab that started it
-- ``GET /api/jobs/{id}`` needs an id you can only have if you were there.
A run from a schedule, a webhook or another tab left no trace anyone could
find, and the two work queues held pending work with no way to see it.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from finmeta.api.deps import get_ctx
from finmeta.api.run_service import scope_parts, submit_run
from finmeta.api.state import AppContext
from finmeta.engine.jobs import finish_run, get_run, list_runs_page, log as job_log, run_log_summary

logger = logging.getLogger("finmeta.api.activity")

router = APIRouter(prefix="/api", tags=["activity"])

_ACTIONS = ("plan", "apply", "scan")


def _duration(row: Dict[str, Any]) -> Optional[float]:
    """Seconds between started_at and finished_at, or None while running."""
    from datetime import datetime

    started, finished = row.get("started_at"), row.get("finished_at")
    if not started or not finished:
        return None
    try:
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        return (datetime.strptime(finished, fmt)
                - datetime.strptime(started, fmt)).total_seconds()
    except (ValueError, TypeError):
        return None


@router.get("/activity")
def activity(
    ctx: AppContext = Depends(get_ctx),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    status: Optional[str] = Query(default=None),
    action: Optional[str] = Query(default=None),
) -> dict:
    """Everything the Activity page polls, in one call."""
    if action and action not in _ACTIONS:
        raise HTTPException(status_code=422,
                            detail=f"action must be one of {', '.join(_ACTIONS)}")
    rows, total = list_runs_page(ctx.db_path, limit=limit, offset=offset,
                                 status=status, action=action)
    summary = run_log_summary(ctx.db_path, [r["run_id"] for r in rows])
    runs = []
    for row in rows:
        extra = summary.get(row["run_id"], {})
        runs.append({
            "run_id": row["run_id"],
            "action": row.get("action"),
            "scope": row.get("scope"),
            "tier": row.get("tier"),
            "status": row.get("status"),
            "started_at": row.get("started_at"),
            "finished_at": row.get("finished_at"),
            "duration_s": _duration(row),
            "files_written": row.get("files_written") or 0,
            "files_unchanged": row.get("files_unchanged") or 0,
            "api_calls": row.get("api_calls") or 0,
            "errors": extra.get("errors", 0),
            "warnings": extra.get("warnings", 0),
            "last_error": ctx.redact(extra["last_error"])
                          if extra.get("last_error") else None,
            # A row from before S11a has no recorded action, so there is
            # nothing to resubmit -- the UI disables Retry on these.
            "retryable": bool(row.get("action")),
        })

    active_id = ctx.active_run
    active = None
    if active_id:
        row = get_run(ctx.db_path, active_id) or {}
        active = {
            "run_id": active_id,
            "action": row.get("action"),
            "scope": row.get("scope"),
            "tier": row.get("tier"),
            "started_at": row.get("started_at"),
            "reported_scope": ctx.active_run_scope,
        }

    return {
        "active": active,
        "queued": {
            "items": ctx.peek_webhook_queue(),
            "libraries": [{"action": a, "library": lib, "tier": tier}
                          for a, lib, tier in ctx.peek_library_queue()],
        },
        "schedules": _schedules(ctx),
        "runs": runs,
        "total": total, "limit": limit, "offset": offset,
        "has_more": offset + len(runs) < total,
        "dry_run": ctx.config.dry_run,
    }


def _schedules(ctx: AppContext) -> List[Dict[str, Any]]:
    """Each scheduled job and when it next fires (empty if no scheduler)."""
    scheduler = getattr(ctx, "scheduler", None)
    if scheduler is None:
        return []
    out: List[Dict[str, Any]] = []
    try:
        for job in scheduler.get_jobs():
            nxt = getattr(job, "next_run_time", None)
            out.append({"id": job.id, "name": job.name,
                        "next_run_at": nxt.isoformat() if nxt else None})
    except Exception as exc:          # a scheduler mid-reload must not 500
        logger.warning("could not list scheduled jobs: %s", exc)
        return []
    return sorted(out, key=lambda j: (j["next_run_at"] or "~"))


@router.post("/activity/active/cancel")
def cancel_active_run(ctx: AppContext = Depends(get_ctx)) -> dict:
    """Force-release the single run slot (operator-requested 2026-11-10: a
    stuck apply left the operator with no way to get past it -- the queue
    had a bulk clear already, but the run actually IN PROGRESS did not).

    This is NOT a real cancellation -- the worker thread has no
    cancellation point (same limitation ``clear_queues()`` documents) and
    keeps running in the background until it finishes or errors on its
    own; this only releases the slot so the next queued/submitted run can
    start, and marks the run's own row ``error`` so Activity stops
    showing it as perpetually "running". If the abandoned worker DOES
    eventually finish for real, it will call ``finish_run``/
    ``release_run`` again itself -- harmless (release_run no-ops once a
    different run holds the slot; finish_run just overwrites this row's
    status with whatever actually happened).
    """
    run_id = ctx.active_run
    if not run_id:
        raise HTTPException(status_code=404, detail="no run is active")
    job_log(ctx.db_path, run_id, "warning",
           "Cancelled by operator: the run slot was force-released. The "
           "in-flight worker is not actually killed and may still be "
           "writing in the background.")
    finish_run(ctx.db_path, run_id, "error")
    ctx.release_run(run_id)
    return {"cancelled": run_id}


@router.post("/activity/{run_id}/retry")
def retry_run(run_id: str, ctx: AppContext = Depends(get_ctx)) -> dict:
    """Resubmit a finished run with the SAME action and scope.

    Goes through ``submit_run``, so it takes the one run slot, obeys the
    dry_run refusal and queues like anything else. Never automatic: a failed
    run is something a human looks at and decides to repeat.
    """
    row = get_run(ctx.db_path, run_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no such run {run_id!r}")
    action = row.get("action")
    if not action:
        raise HTTPException(
            status_code=422,
            detail="this run predates action/scope recording, so there is "
                   "nothing to resubmit -- start it from its own page instead")
    if row.get("status") == "running":
        raise HTTPException(status_code=409, detail="that run is still running")
    if action == "apply" and ctx.config.dry_run:
        raise HTTPException(
            status_code=409,
            detail="dry_run is true -- set it to false in config before "
                   "retrying an apply")
    library, item_id = scope_parts(row.get("scope"))
    new_run_id, err = submit_run(ctx, action, library=library, item_id=item_id,
                                 tier=int(row.get("tier") or 1))
    if err:
        raise HTTPException(status_code=409, detail=err)
    return {"job_id": new_run_id, "action": action, "scope": row.get("scope"),
            "status": "running"}


@router.delete("/activity/queued")
def clear_queued(ctx: AppContext = Depends(get_ctx)) -> dict:
    """Drop every pending item/library run (operator-requested 2026-11-10).

    A bulk fix can queue far more single-item runs than dropping them one
    at a time is a realistic way to undo. The run already in progress is
    untouched -- there is no cancellation point inside a running apply,
    only the backlog behind it.
    """
    return {"dropped": ctx.clear_queues()}


@router.delete("/activity/queued/items/{item_id}")
def drop_queued_item(item_id: str, ctx: AppContext = Depends(get_ctx)) -> dict:
    """Drop a pending single-item run that has not started yet."""
    if not ctx.drop_webhook_item(item_id):
        raise HTTPException(status_code=404,
                            detail=f"{item_id!r} is not queued")
    return {"dropped": item_id}


@router.delete("/activity/queued/libraries/{action}/{library}")
def drop_queued_library(action: str, library: str,
                        tier: int = Query(default=1, ge=1, le=2),
                        ctx: AppContext = Depends(get_ctx)) -> dict:
    """Drop a pending library run that has not started yet."""
    if not ctx.drop_library_run(action, library, tier):
        raise HTTPException(
            status_code=404,
            detail=f"{action} on {library!r} (tier {tier}) is not queued")
    return {"dropped": {"action": action, "library": library, "tier": tier}}


__all__ = ["router"]
