"""Jellyfin webhook (2026-10-23): auto-apply overlays/artwork the moment a
new movie or episode lands in Jellyfin, instead of waiting for the next
scheduled cron run.

Needs the official Jellyfin "Webhook" plugin, pointed at a Generic
destination with "Item Added" enabled for the libraries Finmeta manages,
and -- easy to miss, see below -- **"Send All Properties" ticked**.

A Generic destination renders its Handlebars ``Template`` as the request
body. With "Send All Properties" ON the plugin instead serializes the whole
notification as JSON, which already includes the ``ItemId`` and
``NotificationType`` this route needs, so no template has to be written.
With it OFF *and* no template set (the plugin's own default for a
freshly-added destination) the rendered body is **empty** -- the plugin
still POSTs, Finmeta still answers 200, and nothing whatsoever happens.
This cost a full debugging session on 2026-11-05 because an earlier version
of this docstring claimed no template was required without mentioning the
checkbox that makes that true; ``webhook_receive`` now names the setting in
its own skip message for an empty body.

The receiving path (``POST /api/webhooks/jellyfin/{token}``) is reachable
without a Finmeta session (Jellyfin's webhook plugin can't log in) --
see ``app._is_public``. Its own token is the only gate, so it must never be
guessable or reversible: only a SHA-256 hash of it is ever stored (the
plaintext is shown once, at generation time, and cannot be recovered
afterwards -- same "shown once" pattern as a GitHub/Stripe API key, and it
means the token is immune to the global secret-redaction middleware, which
would otherwise have scrubbed it out of that one response too).
"""
from __future__ import annotations

import hashlib
import json
import logging
import secrets as _secrets_mod
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Tuple

from apscheduler.triggers.date import DateTrigger
from fastapi import APIRouter, Depends, HTTPException, Request

from finmeta.api.deps import get_ctx
from finmeta.api.run_service import submit_run
from finmeta.api.state import AppContext

logger = logging.getLogger("finmeta.api.webhooks")

router = APIRouter(prefix="/api", tags=["webhooks"])

#: Not the token itself -- see module docstring.
_HASH_SECRET_NAME = "jellyfin_webhook_token_hash"

#: Buffer between a webhook ItemAdded hit and actually running the apply.
#: Confirmed live 2026-11-05: Jellyfin's own LibraryMonitor (real-time file
#: watcher) takes ~60s after a file finishes writing before it even starts
#: refreshing the item, before any metadata/image generation happens on top
#: of that -- applying immediately on the webhook POST risks overlaying a
#: poster Jellyfin hasn't actually produced yet. Not configurable (yet);
#: revisit if 90s proves too short/long in practice.
WEBHOOK_APPLY_DELAY = timedelta(seconds=90)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _get_ci(d: Dict[str, Any], *names: str) -> Optional[Any]:
    """Case-insensitive key lookup -- Jellyfin's webhook plugin uses
    PascalCase (``ItemId``), but a custom operator template could send
    anything."""
    lower = {str(k).lower(): v for k, v in d.items()}
    for n in names:
        v = lower.get(n.lower())
        if v is not None:
            return v
    return None


#: Child types whose own id isn't a valid apply scope, but whose ``SeriesId``
#: is. Jellyfin fires a *separate* ``ItemAdded`` for each of these -- adding
#: one season of a show posts the Season and every Episode in it, plus the
#: Series itself if it's new -- so all of them have to land on the series.
_RESOLVE_UP_TO_SERIES = ("Episode", "Season")


def _resolve_apply_target(ctx: AppContext, item_id: str) -> Tuple[Optional[str], Optional[str]]:
    """An Episode's or Season's own id isn't a valid apply scope (build_plan
    only dispatches Movie/Series) -- resolve up to its Series. Returns
    ``(target_id, None)`` on success, or ``(None, reason)`` -- a short,
    specific reason the caller logs -- when the item doesn't exist, isn't a
    Movie/Series(/Episode/Season), or belongs to a library Finmeta isn't
    managing (``enabled: false`` or unconfigured). Collapsing all of these
    into a single silent ``None`` (the original behavior) made every
    real-world webhook skip indistinguishable from any other in the logs."""
    from finmeta.engine.plan_builder import _resolve_item_library

    jf = ctx.make_jellyfin()
    try:
        try:
            item = jf.get_item(item_id)
        except Exception as exc:
            return None, f"could not fetch item from Jellyfin: {exc}"
        if item.type in _RESOLVE_UP_TO_SERIES and item.series_id:
            try:
                item = jf.get_item(item.series_id)
            except Exception as exc:
                return None, f"could not fetch parent series from Jellyfin: {exc}"
        if item.type not in ("Movie", "Series"):
            return None, f"item type {item.type!r} is not Movie/Series(/Episode/Season)"
        try:
            _name, lib_cfg = _resolve_item_library(item, jf, ctx.config)
        except Exception as exc:
            return None, f"library resolution failed: {exc}"
    finally:
        jf.close()
    if lib_cfg is None:
        return None, "item's path matched no configured library"
    if not getattr(lib_cfg, "enabled", False):
        return None, "item's library is configured but not enabled"
    return item.id, None


def _submit_or_queue(context: AppContext, target_id: str) -> None:
    """Runs ``WEBHOOK_APPLY_DELAY`` after the webhook hit, via the scheduler
    -- same single-run-slot path everything else uses, so this just as
    happily queues (``run_service._worker`` drains it) as runs immediately."""
    run_id, err = submit_run(context, "apply", item_id=target_id)
    if run_id:
        logger.info("webhook: delayed apply started for item %s (run %s)", target_id, run_id)
    else:
        context.queue_webhook_item(target_id)
        logger.info("webhook: delayed apply busy (%s), queued item %s", err, target_id)


@router.get("/webhooks/jellyfin/status")
def webhook_status(ctx: AppContext = Depends(get_ctx)) -> dict:
    configured = ctx.secrets is not None and ctx.secrets.has(_HASH_SECRET_NAME)
    return {"configured": configured}


@router.post("/webhooks/jellyfin/regenerate")
def webhook_regenerate(request: Request, ctx: AppContext = Depends(get_ctx)) -> dict:
    """Generate a new token, store only its hash, and return the full
    webhook URL (built from the request's own host, D1: never hardcoded) --
    this is the ONLY time the plaintext is ever available. Invalidates any
    previously issued URL (old token's hash is overwritten)."""
    if ctx.secrets is None:
        raise HTTPException(status_code=500, detail="secrets store unavailable")
    token = _secrets_mod.token_urlsafe(32)
    ctx.secrets.set(_HASH_SECRET_NAME, _hash(token))
    url = f"{str(request.base_url).rstrip('/')}/api/webhooks/jellyfin/{token}"
    return {"url": url}


@router.delete("/webhooks/jellyfin")
def webhook_disable(ctx: AppContext = Depends(get_ctx)) -> dict:
    deleted = ctx.secrets.delete(_HASH_SECRET_NAME) if ctx.secrets is not None else False
    return {"deleted": deleted}


@router.post("/webhooks/jellyfin/{token}")
async def webhook_receive(
    token: str, request: Request, ctx: AppContext = Depends(get_ctx),
) -> dict:
    stored_hash = ctx.secrets.get(_HASH_SECRET_NAME) if ctx.secrets is not None else None
    # 404, not 401/403: a wrong path segment should look identical to a path
    # that never existed, not confirm "a webhook endpoint lives here".
    if not stored_hash or not _secrets_mod.compare_digest(_hash(token), stored_hash):
        raise HTTPException(status_code=404, detail="not found")

    raw = await request.body()
    if not raw.strip():
        # By far the most common real-world misconfiguration -- the plugin's
        # Generic destination defaults to no template and "Send All
        # Properties" off, which renders to nothing at all. Name the fix in
        # the message: a bare "invalid JSON body" sent a 2026-11-05 session
        # digging through the plugin's own config XML to work this out.
        logger.info(
            "webhook: skipped (empty body) -- tick \"Send All Properties\" on this "
            "destination in Jellyfin's Webhook plugin, or give it a payload template"
        )
        return {"ok": True, "skipped":
                "empty body: enable \"Send All Properties\" in Jellyfin's Webhook plugin"}
    try:
        body = json.loads(raw)
    except Exception as exc:
        logger.info("webhook: skipped (body is not valid JSON: %s)", exc)
        return {"ok": True, "skipped": "invalid JSON body"}
    if not isinstance(body, dict):
        logger.info("webhook: skipped (unexpected payload shape)")
        return {"ok": True, "skipped": "unexpected payload shape"}

    notif = _get_ci(body, "NotificationType")
    if not isinstance(notif, str) or notif.lower() != "itemadded":
        logger.info("webhook: skipped (ignored notification type %r)", notif)
        return {"ok": True, "skipped": f"ignored notification type {notif!r}"}

    item_id = _get_ci(body, "ItemId")
    if not item_id:
        logger.info("webhook: skipped (no ItemId in payload)")
        return {"ok": True, "skipped": "no ItemId in payload"}

    target_id, reason = _resolve_apply_target(ctx, str(item_id))
    if target_id is None:
        logger.info("webhook: skipped for item %s (%s)", item_id, reason)
        return {"ok": True, "skipped": reason or "item not in a Finmeta-managed library"}

    if ctx.scheduler is None:
        # Shouldn't happen outside of a context that never went through the
        # normal app lifespan -- run it now rather than drop it silently.
        logger.warning("webhook: no scheduler available, applying item %s immediately", target_id)
        _submit_or_queue(ctx, target_id)
        return {"ok": True, "scheduled": False}

    # Debounce, don't queue up one apply per notification: Jellyfin fires a
    # separate ItemAdded for the Series, its Season and every single Episode,
    # so importing one 12-episode season posts 13+ times within seconds --
    # all resolving to the same series. A stable per-target job id plus
    # replace_existing collapses that burst into ONE apply, and because each
    # hit pushes the run time out again, it only fires once the burst has
    # actually gone quiet for WEBHOOK_APPLY_DELAY.
    run_at = datetime.now(ctx.scheduler.timezone) + WEBHOOK_APPLY_DELAY
    job_id = f"webhook-apply-{target_id}"
    replaced = ctx.scheduler.get_job(job_id) is not None
    ctx.scheduler.add_job(
        _submit_or_queue, trigger=DateTrigger(run_date=run_at), args=[ctx, target_id],
        id=job_id, misfire_grace_time=300, replace_existing=True,
    )
    logger.info("webhook: apply for item %s %s for %s (delay %ds)",
                target_id, "rescheduled" if replaced else "scheduled",
                run_at.isoformat(), WEBHOOK_APPLY_DELAY.total_seconds())
    return {"ok": True, "scheduled": True, "run_at": run_at.isoformat(), "debounced": replaced}


__all__ = ["router"]
