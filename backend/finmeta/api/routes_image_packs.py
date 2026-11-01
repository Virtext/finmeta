"""Image pack routes (SPEC S9 D3): list packs with categories and missing images."""
from __future__ import annotations

import re
import shutil
from typing import List

import yaml
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from finmeta.api.deps import get_ctx
from finmeta.api.state import AppContext
from finmeta.config.loading import ConfigError
from finmeta.engine.image_packs import get_pack, load_packs

router = APIRouter(prefix="/api/packs", tags=["packs"])

_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")

#: format_rule keys for a scaffolded pack's categories -- exactly K9's own
#: vocabulary (engine/format_rules.py's DEFAULT_FORMAT_RULES `image` fields),
#: so a scaffolded pack.yml resolves with zero further edits once the
#: operator drops matching PNGs in. See docs/BADGE_PACKS.md.
_SCAFFOLD_CATEGORIES = {
    "resolution": ["res_4k", "res_1080p", "res_720p", "res_576p", "res_480p"],
    "dynamic_range": ["dv_hdr_plus", "dv_hdr", "dv", "hdr10plus", "hdr10", "hlg"],
    "audio": ["truehd_atmos", "ddplus_atmos", "truehd", "ddplus", "dd",
              "dtsx", "dtshd_ma", "dts", "aac"],
}


class ScaffoldPackReq(BaseModel):
    name: str
    categories: List[str] = Field(default_factory=lambda: list(_SCAFFOLD_CATEGORIES))


@router.get("")
def list_packs(ctx: AppContext = Depends(get_ctx)):
    """List all image packs found under <config>/assets/packs/.

    Each pack reports its categories, expected keys, and which images are
    missing on disk. Invalid packs appear with ``valid: false`` and an error.
    """
    packs = load_packs(ctx.config_dir)
    return {"packs": [p.model_dump() for p in packs]}


@router.post("/scaffold")
def scaffold_pack(req: ScaffoldPackReq, ctx: AppContext = Depends(get_ctx)):
    """Create a new pack.yml pre-filled with K9's own format_rule keys for
    the requested categories, plus empty images/<category>/ directories.

    No image files are written -- the operator drops their own PNGs in
    afterwards (GET /api/packs then reports exactly which keys are still
    missing, same as any other pack). This is the only thing a hand-made
    pack needs to get right to resolve automatically; see docs/BADGE_PACKS.md.
    """
    name = req.name.strip()
    if name in (".", "..") or not _NAME_RE.match(name):
        raise HTTPException(status_code=400, detail=f"invalid pack name: {name!r}")
    categories = req.categories or list(_SCAFFOLD_CATEGORIES)
    unknown = [c for c in categories if c not in _SCAFFOLD_CATEGORIES]
    if unknown:
        raise HTTPException(status_code=400, detail=f"unknown categories: {unknown}")

    pack_dir = (ctx.config_dir / "assets" / "packs" / name)
    if pack_dir.exists():
        raise HTTPException(status_code=409, detail=f"pack already exists: {name}")

    lines = [f"name: {name}", "categories:"]
    for cat in categories:
        keys = ", ".join(_SCAFFOLD_CATEGORIES[cat])
        lines += [f"  {cat}:", "    matcher: format_rule", f"    keys: [{keys}]"]
    pack_dir.mkdir(parents=True)
    (pack_dir / "pack.yml").write_text("\n".join(lines) + "\n")
    for cat in categories:
        (pack_dir / "images" / cat).mkdir(parents=True)

    info = get_pack(ctx.config_dir, name)
    return info.model_dump() if info is not None else {"name": name}


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
