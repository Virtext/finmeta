"""Logo pack routes (SPEC S9c8): install + report.

Logos are trademarks of their owners; the Kometa project's overlay images are
downloaded at the user's request only and never shipped with Finmeta itself.
HTTP is done through an :class:`httpx.Client` built per call so tests can swap
in a mock transport without any real network access.
"""
from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends, HTTPException

from finmeta.api.deps import get_ctx
from finmeta.api.state import AppContext
from finmeta.engine.logo_packs import (available_pack_names, install_pack,
                                       installed_packs, load_pack)

router = APIRouter(prefix="/api/assets/logo-packs", tags=["assets"])


def _build_client() -> httpx.Client:
    # Fresh client per request; follow redirects to stay resilient against raw.githubusercontent moves.
    return httpx.Client(follow_redirects=True, timeout=30.0)


@router.get("")
def list_logo_packs(ctx: AppContext = Depends(get_ctx)):
    """Installed packs on disk + availability of each known pack."""
    cfg_dir = ctx.config_dir
    return {
        "packs": [p["name"] for p in installed_packs(cfg_dir)],
        "known": available_pack_names(),
        "detail": installed_packs(cfg_dir),
    }


@router.get("/{name}/status")
def logo_pack_status(name: str, ctx: AppContext = Depends(get_ctx)):
    """Is this pack installed (folder present) and complete (all files present)?

    Lets the UI decide between "Install", "Update" (partial install present) and
    "Installed" without re-parsing the full list response.
    """
    try:
        load_pack(name)   # raises KeyError when unknown
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    for d in installed_packs(ctx.config_dir):
        if d["name"] == name:
            return {"name": name, "installed": d["installed"],
                    "complete": d["complete"], "have": d["have"],
                    "total": d["total"]}
    raise HTTPException(status_code=404, detail=f"unknown logo pack {name!r}")


@router.post("/{name}/install")
def install_logo_pack(name: str, ctx: AppContext = Depends(get_ctx)):
    """Download the mapped files into ``<config>/assets/logos/<name>/``.

    Idempotent (existing non-empty files are skipped). Trademark note is echoed
    back so UI can surface it next to the button.
    """
    try:
        pack = load_pack(name)   # raises KeyError when unknown
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    with _build_client() as client:
        result = install_pack(client, ctx.config_dir, name)
    if result["failed"] == len(pack["files"]) and not result["downloaded"]:
        # total failure -> signal 502 so UI shows a retry-able error state
        raise HTTPException(
            status_code=502,
            detail={"error": "all downloads failed", **result},
        )
    return {**result,
            "note": ("Logos are trademarks of their owners; downloaded at your request "
                     "from the Kometa project.")}


__all__ = ["router"]
