"""Kometa import routes (SPEC S9 E): POST /api/kometa/import.

Expands a Kometa overlay file, builds the image pack dict with
``kometa_to_pack`` and either reports what would be written (dry run) or
writes ``<config>/assets/packs/<pack_name>/pack.yml`` + copies the badge
images. Nothing is ever written outside the target pack directory.

2026-10-22: the remote-download variant (``/import-remote``, "Download a
known overlay pack" / jmxd in the UI) was removed on operator decision --
the Kometa logo pack + this local-path import cover the badge vocabulary
Finmeta actually renders on their own; see STATE.md for the writeup.
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any, Dict, List, Tuple

import yaml
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from finmeta.api.deps import get_ctx
from finmeta.api.state import AppContext
from finmeta.kometa_import.expand import expand_file
from finmeta.kometa_import.report import ImportReport
from finmeta.kometa_import.to_pack import kometa_to_pack

router = APIRouter(prefix="/api/kometa", tags=["kometa"])

_PACK_NAME_RE = re.compile(r"^[a-z0-9-]+$")


class KometaImportReq(BaseModel):
    overlay_file: str = Field(description="Absolute path to the Kometa overlay YAML file.")
    kometa_root: str = Field(description="Absolute path of the Kometa folder: either the Kometa root (contains config/) or the images folder (contains media_info/ etc.).")
    pack_name: str = Field(description="Target pack name; slug [a-z0-9-].")
    dry_run: bool = True
    overwrite: bool = False


def _abs_path(value: str, field: str) -> Path:
    p = Path(value)
    if not p.is_absolute():
        raise HTTPException(status_code=400, detail=f"{field} must be an absolute path")
    if not p.exists():
        raise HTTPException(status_code=400, detail=f"{field} does not exist: {value}")
    return p


def _build(overlay_file: str, kometa_root: str, pack_name: str) -> Tuple[Dict[str, Any], List[Tuple[str, str]], "ImportReport"]:
    """Expand + translate; returns (pack_yml, copies, report). Raises 400 on invalid input."""
    if not _PACK_NAME_RE.match(pack_name):
        raise HTTPException(status_code=400,
                            detail="pack_name must be a slug of [a-z0-9-]")
    src = _abs_path(overlay_file, "overlay_file")
    root = _abs_path(kometa_root, "kometa_root")
    flat, report = expand_file(src)
    pack_yml, copies, report = kometa_to_pack(flat, report, root, pack_name)
    return pack_yml, copies, report


def _missing(copies: List[Tuple[str, str]]) -> List[str]:
    """Source images that do not exist on disk (reported, never a crash)."""
    return [src for src, _ in copies if not Path(src).is_file()]


def _import_result(pack_yml: Dict[str, Any], copies: List[Tuple[str, str]], report: ImportReport,
                    pack_name: str, dry_run: bool, overwrite: bool, config_dir: Path) -> Dict[str, Any]:
    """Shared dry-run/write logic for the local-path import endpoint."""
    missing = _missing(copies)

    if dry_run:
        return {
            "dry_run": True,
            "pack_name": pack_name,
            "pack_yml": pack_yml,
            "image_copies": [list(c) for c in copies],
            "missing_images": missing,
            "report": report.to_dict(),
        }

    packs_root = config_dir / "assets" / "packs"
    target = packs_root / pack_name
    if target.exists() and not overwrite:
        raise HTTPException(status_code=409,
                            detail=f"pack {pack_name!r} already exists; pass overwrite=true to replace it")

    # Write only inside the target pack directory.
    if target.exists():
        shutil.rmtree(target)  # overwrite=true: drop the old pack fully (e.g. a stale stub with no images)
    target.mkdir(parents=True, exist_ok=True)
    (target / "pack.yml").write_text(
        yaml.safe_dump(pack_yml, sort_keys=False, allow_unicode=True), encoding="utf-8")

    written: List[str] = []
    for src, rel in copies:
        sp = Path(src)
        if not sp.is_file():
            continue  # listed in missing_images; never a crash
        dest = target / rel
        _ensure_inside(dest, target)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(sp, dest)
        written.append(rel)

    return {
        "dry_run": False,
        "pack_name": pack_name,
        "pack_yml": pack_yml,
        "image_copies": [list(c) for c in copies],
        "missing_images": missing,
        "written": written,
        "report": report.to_dict(),
    }


@router.post("/import")
def kometa_import(req: KometaImportReq, ctx: AppContext = Depends(get_ctx)):
    """Import one Kometa overlay file as an image pack.

    dry_run (default) returns the report + pack.yml + planned image copies
    without writing anything. With ``dry_run=false`` the pack is written to
    ``<config>/assets/packs/<pack_name>/``; existing packs require
    ``overwrite=true`` or the request fails with 409. Missing source images
    are listed in ``missing_images`` and skipped, never an error.
    """
    pack_yml, copies, report = _build(req.overlay_file, req.kometa_root, req.pack_name)
    return _import_result(pack_yml, copies, report, req.pack_name, req.dry_run, req.overwrite, ctx.config_dir)


def _ensure_inside(dest: Path, target: Path) -> None:
    """Guarantee a destination stays inside the pack directory (no .. escapes)."""
    try:
        dest.resolve().relative_to(target.resolve())
    except ValueError as exc:
        raise HTTPException(status_code=500, detail=f"internal error: copy target escapes pack dir: {dest}") from exc


__all__ = ["router"]
