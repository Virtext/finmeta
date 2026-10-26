"""Config routes: GET, validated PUT (keeps config.last-good.yml), POST undo."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from finmeta.config.loading import ConfigError

from finmeta.api.deps import get_ctx
from finmeta.api.state import AppContext

router = APIRouter(prefix="/api", tags=["config"])


@router.get("/config")
def get_config(ctx: AppContext = Depends(get_ctx)) -> dict:
    from finmeta.config.secretspec import valid_api_key_ref

    parsed = ctx.config.model_dump()
    # S9f1b step 4: flag media-app instances whose api_key reference is not a
    # valid secret name (invalid reference — re-save instance), straight on the
    # config data the Settings page renders.
    for kind in ("sonarr", "radarr"):
        for inst in parsed.get("media_apps", {}).get(kind, []):
            inst["api_key_valid"] = valid_api_key_ref(ctx.config, inst.get("api_key") or "")
    return {
        "path": str(ctx.config_path),
        "dry_run": ctx.config.dry_run,
        "gen": ctx.gen,
        "config_error": ctx.config_error,
        "yaml": ctx.config_text(),
        "parsed": parsed,
    }


class ConfigPut(BaseModel):
    yaml: str


@router.put("/config")
def put_config(body: ConfigPut, ctx: AppContext = Depends(get_ctx)) -> dict:
    try:
        ctx.put_config(body.yaml)
    except ConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "gen": ctx.gen, "dry_run": ctx.config.dry_run}


@router.post("/config/undo")
def undo_config(ctx: AppContext = Depends(get_ctx)) -> dict:
    if not ctx.last_good_path().is_file():
        raise HTTPException(status_code=404,
                            detail="no config.last-good.yml to revert to")
    if not ctx.undo_config():
        raise HTTPException(status_code=500, detail="failed to reload reverted config")
    return {"ok": True, "gen": ctx.gen, "dry_run": ctx.config.dry_run}


__all__ = ["router"]
