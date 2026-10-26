"""Image pack routes (SPEC S9 D3): list packs with categories and missing images."""
from __future__ import annotations

import re
import shutil

import yaml
from fastapi import APIRouter, Depends, HTTPException

from finmeta.api.deps import get_ctx
from finmeta.api.state import AppContext
from finmeta.config.loading import ConfigError
from finmeta.engine.image_packs import load_packs

router = APIRouter(prefix="/api/packs", tags=["packs"])

_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


@router.get("")
def list_packs(ctx: AppContext = Depends(get_ctx)):
    """List all image packs found under <config>/assets/packs/.

    Each pack reports its categories, expected keys, and which images are
    missing on disk. Invalid packs appear with ``valid: false`` and an error.
    """
    packs = load_packs(ctx.config_dir)
    return {"packs": [p.model_dump() for p in packs]}


@router.delete("/{name}")
def delete_pack(name: str, ctx: AppContext = Depends(get_ctx)):
    """Uninstall an imported/downloaded pack: removes its files and any
    reference to it from every overlay profile (packs/pack_overrides)."""
    # _NAME_RE's charset alone would still accept "." / ".." (both are made
    # of allowed characters) -- reject them explicitly, then re-confirm by
    # resolved-path containment below (defense in depth against traversal).
    if name in (".", "..") or not _NAME_RE.match(name):
        raise HTTPException(status_code=400, detail=f"invalid pack name: {name!r}")

    packs_root = (ctx.config_dir / "assets" / "packs").resolve()
    pack_dir = (packs_root / name).resolve()
    if pack_dir.parent != packs_root:
        raise HTTPException(status_code=400, detail=f"invalid pack name: {name!r}")
    if not pack_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"pack not found: {name}")
    shutil.rmtree(pack_dir)

    # Best-effort: also clear the remote-download staging cache for this pack
    # (config/cache/kometa_downloads/<name>/), if any -- harmless if absent.
    cache_dir = ctx.config_dir / "cache" / "kometa_downloads" / name
    if cache_dir.is_dir():
        shutil.rmtree(cache_dir, ignore_errors=True)

    # Scrub dangling references so no profile still points at a pack that no
    # longer exists on disk.
    cfg_text = yaml.safe_load(ctx.config_text()) or {}
    profiles = ((cfg_text.get("overlays") or {}).get("profiles") or {})
    changed = False
    for prof in profiles.values():
        if not isinstance(prof, dict):
            continue
        packs = prof.get("packs")
        if isinstance(packs, list) and name in packs:
            prof["packs"] = [p for p in packs if p != name]
            changed = True
        overrides = prof.get("pack_overrides")
        if isinstance(overrides, dict) and name in overrides:
            del overrides[name]
            if not overrides:
                del prof["pack_overrides"]
            changed = True
    if changed:
        new_yaml_text = yaml.safe_dump(cfg_text, sort_keys=False, allow_unicode=True)
        try:
            ctx.put_config(new_yaml_text)
        except ConfigError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    return {"ok": True, "name": name}


__all__ = ["router"]
