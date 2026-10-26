"""Jellyfin webhook (2026-10-23): auto-apply overlays/artwork the moment a
new movie or episode lands in Jellyfin, instead of waiting for the next
scheduled cron run.

Needs the official Jellyfin "Webhook" plugin, pointed at a Generic
destination with "Item Added" enabled for the libraries Finmeta manages.
Its default payload already includes ``ItemId``/``NotificationType`` --
no custom template required.

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
import logging
import secrets as _secrets_mod
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request

from finmeta.api.deps import get_ctx
from finmeta.api.run_service import submit_run
from finmeta.api.state import AppContext

logger = logging.getLogger("finmeta.api.webhooks")

router = APIRouter(prefix="/api", tags=["webhooks"])

#: Not the token itself -- see module docstring.
_HASH_SECRET_NAME = "jellyfin_webhook_token_hash"


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


def _resolve_apply_target(ctx: AppContext, item_id: str) -> Optional[str]:
    """An Episode's own id isn't a valid apply scope (build_plan only
    dispatches Movie/Series) -- resolve up to its Series. Returns None when
    the item doesn't exist, isn't a Movie/Series(/Episode), or belongs to a
    library Finmeta isn't managing (``enabled: false`` or unconfigured)."""
    from finmeta.engine.plan_builder import _resolve_item_library

    jf = ctx.make_jellyfin()
    try:
        item = jf.get_item(item_id)
        if item.type == "Episode" and item.series_id:
            item = jf.get_item(item.series_id)
        if item.type not in ("Movie", "Series"):
            return None
        _name, lib_cfg = _resolve_item_library(item, jf, ctx.config)
    except Exception:
        logger.warning("webhook: failed to resolve item %s", item_id, exc_info=True)
        return None
    finally:
        jf.close()
    if lib_cfg is None or not getattr(lib_cfg, "enabled", False):
        return None
    return item.id


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

    try:
        body = await request.json()
    except Exception:
        return {"ok": True, "skipped": "invalid JSON body"}
    if not isinstance(body, dict):
        return {"ok": True, "skipped": "unexpected payload shape"}

    notif = _get_ci(body, "NotificationType")
    if not isinstance(notif, str) or notif.lower() != "itemadded":
        return {"ok": True, "skipped": f"ignored notification type {notif!r}"}

    item_id = _get_ci(body, "ItemId")
    if not item_id:
        return {"ok": True, "skipped": "no ItemId in payload"}

    target_id = _resolve_apply_target(ctx, str(item_id))
    if target_id is None:
        return {"ok": True, "skipped": "item not in a Finmeta-managed library"}

    run_id, err = submit_run(ctx, "apply", item_id=target_id)
    if run_id:
        logger.info("webhook: apply started for item %s (run %s)", target_id, run_id)
        return {"ok": True, "run_id": run_id}
    # A run is already active -- queue it, drained after that run finishes
    # (run_service._worker) instead of dropping it.
    ctx.queue_webhook_item(target_id)
    logger.info("webhook: run busy (%s), queued item %s", err, target_id)
    return {"ok": True, "queued": True}


__all__ = ["router"]
