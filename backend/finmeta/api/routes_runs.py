"""Run routes: start plan/apply, poll job status + log.

``GET /runs`` (run history) and ``POST /runs/undo`` lived here until
2026-10-22. Neither had ever been called by the UI, and the operator asked
for them to go (docs/SECURITY_REVIEW.md "D2") -- an authenticated but unused
route is still surface to maintain and secure. Undo is unaffected as a
*feature*: it is the CLI's ``finmeta undo``, and ``engine.runs.undo`` /
``load_manifest`` / ``engine.jobs.list_runs`` are all untouched.

``GET /logs/export`` (2026-10-26, operator request) is a deliberately
different shape from the removed ``GET /runs``: a one-shot redacted text
*download* (Sonarr/Radarr's "Log Files" convention) over the most recent
runs, not a persistent live/paginated list endpoint -- there is no ongoing
polling cost, and it reuses ``engine.jobs.list_runs``/``get_job_log``
exactly as the CLI already does.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from finmeta.engine.jobs import get_job_log, get_run, list_runs

from finmeta.api.deps import get_ctx
from finmeta.api.run_service import submit_run
from finmeta.api.state import AppContext

router = APIRouter(prefix="/api", tags=["runs"])

# Sonarr/Radarr-style log export is a bounded, on-demand download -- cap how
# far back it reaches so a long-lived install's full run history can't turn
# one click into an unbounded DB read / multi-MB response.
_MAX_EXPORTED_RUNS = 200


class RunRequest(BaseModel):
    action: str  # "plan" (dry-run) | "apply"
    library: Optional[str] = None
    item: Optional[str] = None


@router.post("/runs", status_code=202)
def start_run(body: RunRequest, ctx: AppContext = Depends(get_ctx)) -> dict:
    if body.action not in ("plan", "apply"):
        raise HTTPException(status_code=422,
                            detail="action must be 'plan' or 'apply'")
    run_id, err = submit_run(ctx, body.action, library=body.library, item_id=body.item)
    if err:
        raise HTTPException(status_code=409, detail=err)
    return {"job_id": run_id, "status": "running", "action": body.action}


@router.get("/logs/export", response_class=PlainTextResponse)
def export_logs(ctx: AppContext = Depends(get_ctx)) -> PlainTextResponse:
    """Download the most recent runs' logs as one redacted text file.

    Every run gets a header line (run_id, status, started/finished, write/
    API-call counts) followed by its job_log lines, oldest run first so the
    file reads top-to-bottom chronologically. ``ctx.redact`` runs over every
    line regardless of whether it was already redacted when first logged
    (most error-path lines are -- see run_service.py -- but this is the one
    place a miss there would actually leave the building, so it's covered
    again here rather than trusted)."""
    runs = list_runs(ctx.db_path)[:_MAX_EXPORTED_RUNS]
    runs.reverse()  # list_runs is newest-first; export oldest-first.
    lines: list[str] = []
    for run in runs:
        lines.append(
            f"===== {run['run_id']}  status={run['status']}  "
            f"started={run['started_at']}  finished={run['finished_at']}  "
            f"files_written={run['files_written']}  "
            f"files_unchanged={run['files_unchanged']}  "
            f"api_calls={run['api_calls']} ====="
        )
        for entry in get_job_log(ctx.db_path, run["run_id"]):
            lines.append(f"{entry['ts']} {entry['level'].upper():5s} {entry['message']}")
        lines.append("")
    body = ctx.redact("\n".join(lines)) if lines else "(no runs logged yet)"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    filename = f"finmeta-logs-{stamp}.txt"
    return PlainTextResponse(
        body, media_type="text/plain",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/jobs/{job_id}")
def job(job_id: str, ctx: AppContext = Depends(get_ctx)) -> dict:
    run = get_run(ctx.db_path, job_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"no such run {job_id!r}")
    log = get_job_log(ctx.db_path, job_id)
    # S9d2-fix2 step 6b: surface provider warnings in the job payload.
    import re
    warnings = []
    for line in log:
        m = re.match(r"provider warning: (\S+) — (.*) \((\d+) item\(s\) affected\)$",
                     line["message"])
        if m:
            warnings.append({"provider": m.group(1), "error": m.group(2),
                             "count": int(m.group(3))})
    return {
        "run_id": run["run_id"], "status": run["status"],
        "started_at": run["started_at"], "finished_at": run["finished_at"],
        "files_written": run["files_written"],
        "files_unchanged": run["files_unchanged"],
        "api_calls": run["api_calls"],
        "log": log,
        "provider_warnings": warnings,
    }




__all__ = ["router"]
