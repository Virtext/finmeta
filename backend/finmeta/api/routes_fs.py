"""Filesystem browse route (S9e4): GET /api/fs/list.

Lists folders and ``*.yml`` files for the import panel's Browse buttons. For
safety the listing root is restricted to ``/import`` plus whatever extra
roots ``FINMETA_FS_BROWSE_ROOTS`` names: anything else is rejected with 400,
and symlink escapes out of the allowed tree are blocked (a symlinked
directory that resolves outside the allowed roots is not listed).

``/import`` is the one root every deployment gets -- the Docker image's
documented mount point (see ``docker-compose.yml``) for a user's own Kometa
export, read-only. Fixed (SECURITY_REVIEW.md L3): ``/work`` -- the dev
container's own checkout path, meaningless in a real deployment -- used to
be hardcoded into this list and shipped as part of the public source. The
dev/test environment now opts into it the same way any other deployment
would opt into an extra root: via the env var, never via source code.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import List, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query

from finmeta.api.deps import get_ctx
from finmeta.api.state import AppContext

router = APIRouter(prefix="/api/fs", tags=["fs"])


def _allowed_roots() -> Tuple[str, ...]:
    """Roots the browser may list. Everything else is a 400 (D1: no other
    host paths are ever exposed through the API).

    ``/import`` is always allowed. ``FINMETA_FS_BROWSE_ROOTS`` (os.pathsep-
    separated absolute paths, e.g. ``/work`` in dev) adds more -- empty by
    default, so a fresh production container only ever exposes ``/import``.
    Read from the environment on every call (not cached at import time) so
    a test can change it per-case via monkeypatch.
    """
    raw = os.environ.get("FINMETA_FS_BROWSE_ROOTS", "")
    extra = tuple(p for p in raw.split(os.pathsep) if p)
    return ("/import",) + extra


def _under_allowed(p: Path) -> bool:
    """True when ``p`` resolves inside one of the allowed roots."""
    try:
        rp = p.resolve()
    except OSError:
        return False
    for root in _allowed_roots():
        base = Path(root).resolve()
        if rp == base or base in rp.parents:
            return True
    return False


@router.get("/list")
def fs_list(path: str = Query(default="/import"), ctx: AppContext = Depends(get_ctx)) -> dict:
    """List subfolders and ``.yml`` files under ``path`` (absolute).

    Only paths under /import (plus any extra root from
    FINMETA_FS_BROWSE_ROOTS) are served; anything else is a 400. Symlinked
    directories that resolve outside the allowed roots are skipped so the
    browser cannot escape the sandbox via symlinks.
    """
    p = Path(path)
    if not p.is_absolute():
        raise HTTPException(status_code=400, detail="path must be absolute")
    if not _under_allowed(p):
        raise HTTPException(
            status_code=400,
            detail=f"path {path!r} is outside the allowed roots {_allowed_roots()}")
    if not p.is_dir():
        raise HTTPException(status_code=400, detail=f"not a directory: {path}")

    folders: List[str] = []
    files: List[str] = []
    try:
        entries = sorted(p.iterdir(), key=lambda e: e.name.lower())
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"cannot list {path}: {exc}") from exc
    for e in entries:
        name = e.name
        try:
            if e.is_dir() and not e.is_symlink():
                folders.append(str(e))
            elif e.is_dir() and e.is_symlink():
                # Symlinked dir: only list it when it stays inside the sandbox.
                if _under_allowed(e):
                    folders.append(str(e))
            elif e.is_file() and name.endswith(".yml"):
                files.append(str(e))
        except OSError:
            continue    # broken symlink / vanished entry -> skip
    return {"path": str(p), "folders": folders, "files": files}


__all__ = ["router"]
