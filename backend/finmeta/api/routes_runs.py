"""Run routes: start plan/apply, poll job status + log.

``GET /runs`` (run history) and ``POST /runs/undo`` lived here until
2026-10-22. Neither had ever been called by the UI, and the operator asked
for them to go (docs/SECURITY_REVIEW.md "D1") -- an authenticated but unused
route is still surface to maintain and secure. Undo is unaffected as a
*feature*: it is the CLI's ``finmeta undo``, and ``engine.runs.undo`` /
``load_manifest`` / ``engine.jobs.list_runs`` are all untouched.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from finmeta.engine.jobs import get_job_log, get_run

from finmeta.api.deps import get_ctx
from finmeta.api.run_service import submit_run
from finmeta.api.state import AppContext

router = APIRouter(prefix="/api", tags=["runs"])


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
