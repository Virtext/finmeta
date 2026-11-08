"""Library-health scan results and fixes (SPEC_S10 §E).

A scan itself is started through ``POST /api/runs`` with ``action: "scan"``
(it is a run: one slot, one job log, one progress stream). These routes read
what a scan found and submit the fixes Finmeta can actually perform.
"""
from __future__ import annotations

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from finmeta.api.deps import get_ctx
from finmeta.api.run_service import submit_run
from finmeta.api.state import AppContext
from finmeta.engine import scan_store

logger = logging.getLogger("finmeta.api.scans")

router = APIRouter(prefix="/api", tags=["scans"])

# Fix kinds this API can act on; everything else is informational (the fix
# lives in Jellyfin/Sonarr/config, or there is deliberately no fix at all).
_ACTIONABLE = ("apply_item", "refresh_item")

# An apply is one run per item. Fixing a whole library's worth of findings in
# one click would queue thousands of runs, so a single request is capped and
# the UI is expected to work through a filtered list instead.
MAX_FIX_ITEMS = 50


class FixRequest(BaseModel):
    finding_ids: List[int]


@router.get("/scans")
def list_scans(ctx: AppContext = Depends(get_ctx),
               limit: int = Query(default=20, ge=1, le=100)) -> dict:
    """Scans, newest first."""
    return {"scans": scan_store.list_scans(ctx.db_path, limit=limit)}


@router.get("/scans/latest")
def latest_scan(ctx: AppContext = Depends(get_ctx)) -> dict:
    """The newest scan plus its finding counts, or ``{"scan": null}``.

    Includes a still-running scan: the UI shows live counts while it works.
    """
    scan = scan_store.latest_scan(ctx.db_path)
    if scan is None:
        return {"scan": None, "counts": {"by_severity": {}, "by_group": {},
                                         "by_check": {}}}
    return {"scan": scan, "counts": scan_store.scan_counts(ctx.db_path,
                                                           scan["scan_id"])}


@router.get("/scans/{scan_id}")
def get_scan(scan_id: str, ctx: AppContext = Depends(get_ctx)) -> dict:
    scan = scan_store.get_scan(ctx.db_path, scan_id)
    if scan is None:
        raise HTTPException(status_code=404, detail=f"unknown scan {scan_id!r}")
    return {"scan": scan,
            "counts": scan_store.scan_counts(ctx.db_path, scan_id)}


@router.get("/scans/{scan_id}/findings")
def scan_findings(
    scan_id: str, ctx: AppContext = Depends(get_ctx),
    library: Optional[str] = Query(default=None),
    severity: Optional[str] = Query(default=None),
    check: Optional[str] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict:
    """One page of findings. ``check`` takes a group or a full check code."""
    if scan_store.get_scan(ctx.db_path, scan_id) is None:
        raise HTTPException(status_code=404, detail=f"unknown scan {scan_id!r}")
    if severity and severity not in scan_store.SEVERITIES:
        raise HTTPException(
            status_code=422,
            detail=f"severity must be one of {', '.join(scan_store.SEVERITIES)}")
    findings, total = scan_store.list_findings(
        ctx.db_path, scan_id, library=library, severity=severity, check=check,
        limit=limit, offset=offset)
    return {"scan_id": scan_id, "findings": findings, "total": total,
            "limit": limit, "offset": offset,
            "has_more": offset + len(findings) < total}


@router.post("/scans/{scan_id}/fix")
def fix_findings(scan_id: str, body: FixRequest,
                 ctx: AppContext = Depends(get_ctx)) -> dict:
    """Submit the fixes for the named findings (SPEC_S10 §E, §G).

    - ``apply_item``   -> one item-scope apply run per distinct item, through
      the normal single-slot/FIFO mechanics (the first starts, the rest queue).
      Refused as a whole while ``dry_run`` is true: half-applying a selection
      because the flag is set would be worse than doing nothing.
    - ``refresh_item`` -> ask Jellyfin to re-read what is already on disk. No
      run, no media write, so this is allowed under ``dry_run``.
    - anything else    -> reported back in ``not_fixable``; the UI should not
      have offered a button for it.

    ``fixed_at`` is stamped on what was submitted. The fix is NOT re-verified
    here -- the next scan is the source of truth.
    """
    if scan_store.get_scan(ctx.db_path, scan_id) is None:
        raise HTTPException(status_code=404, detail=f"unknown scan {scan_id!r}")
    if not body.finding_ids:
        raise HTTPException(status_code=422, detail="finding_ids must not be empty")

    rows = scan_store.findings_by_ids(ctx.db_path, scan_id, body.finding_ids)
    known = {r["id"] for r in rows}
    unknown = [i for i in body.finding_ids if i not in known]

    actionable = [r for r in rows if r["fix_kind"] in _ACTIONABLE and r["item_id"]]
    actionable_ids = {r["id"] for r in actionable}
    not_fixable = [r["id"] for r in rows if r["id"] not in actionable_ids]

    apply_rows = [r for r in actionable if r["fix_kind"] == "apply_item"]
    refresh_rows = [r for r in actionable if r["fix_kind"] == "refresh_item"]

    apply_items = _distinct([r["item_id"] for r in apply_rows])
    refresh_items = _distinct([r["item_id"] for r in refresh_rows])

    if len(apply_items) + len(refresh_items) > MAX_FIX_ITEMS:
        raise HTTPException(
            status_code=422,
            detail=f"too many items in one fix request "
                   f"({len(apply_items) + len(refresh_items)} > {MAX_FIX_ITEMS}); "
                   "narrow the selection and fix in batches")

    if apply_items and ctx.config.dry_run:
        raise HTTPException(
            status_code=409,
            detail="dry_run is true -- set it to false in config before fixing "
                   "artwork/metadata (refresh-only fixes still work)")

    submitted: List[dict] = []
    fixed_ids: List[int] = []

    if refresh_items:
        jf = ctx.make_jellyfin()
        try:
            for item_id in refresh_items:
                try:
                    jf.refresh_item(
                        item_id, metadata_refresh_mode="FullRefresh",
                        image_refresh_mode="FullRefresh",
                        replace_all_metadata=False, replace_all_images=False)
                    submitted.append({"item_id": item_id, "action": "refresh_item"})
                    fixed_ids.extend(r["id"] for r in refresh_rows
                                     if r["item_id"] == item_id)
                except Exception as exc:
                    logger.warning("refresh failed for %s: %s", item_id, exc)
                    submitted.append({"item_id": item_id, "action": "refresh_item",
                                      "error": ctx.redact(str(exc))})
        finally:
            jf.close()

    for item_id in apply_items:
        run_id, err = submit_run(ctx, "apply", item_id=item_id)
        if run_id:
            submitted.append({"item_id": item_id, "action": "apply_item",
                              "run_id": run_id})
        else:
            # The slot is busy: queue it on the same FIFO the webhook uses,
            # drained by run_service._worker as each run finishes.
            ctx.queue_webhook_item(item_id)
            submitted.append({"item_id": item_id, "action": "apply_item",
                              "queued": True, "detail": err})
        fixed_ids.extend(r["id"] for r in apply_rows if r["item_id"] == item_id)

    scan_store.mark_fixed(ctx.db_path, fixed_ids)
    return {"submitted": submitted, "not_fixable": not_fixable,
            "unknown": unknown}


@router.delete("/scans/{scan_id}")
def delete_scan(scan_id: str, ctx: AppContext = Depends(get_ctx)) -> dict:
    if not scan_store.delete_scan(ctx.db_path, scan_id):
        raise HTTPException(status_code=404, detail=f"unknown scan {scan_id!r}")
    return {"deleted": scan_id}


def _distinct(values: List[Optional[str]]) -> List[str]:
    """Order-preserving unique, dropping empties."""
    out: List[str] = []
    for v in values:
        if v and v not in out:
            out.append(v)
    return out


__all__ = ["router", "MAX_FIX_ITEMS"]
