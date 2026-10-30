"""Kometa import routes (SPEC S9 E): POST /api/kometa/import-remote.

Downloads a known third-party Kometa-style overlay source as a zip, expands
its overlay file, builds the image pack dict with ``kometa_to_pack`` and
either reports what would be written (dry run) or writes
``<config>/assets/packs/<pack_name>/pack.yml`` + copies the badge images.
Nothing is ever written outside the target pack directory.

2026-10-27: the local-path variant (``/import``, requiring the operator to
already have the Kometa files on local disk and type both paths by hand) was
removed on operator decision -- it only ever worked for self-contained
overlay YAML (explicit ``file:``/``template:`` entries, e.g. jmxd's), not the
far more common Kometa-Team/Community-Configs style (``overlay: <name>``
shorthand + Plex collection matching, confirmed incompatible with Finmeta's
own file-metadata-driven detection). Download-on-demand covers the one
source that's actually known to work (jmxd) without the operator needing a
local checkout or typing paths at all.

This repo has no licence file (all rights reserved by default, same as
jmxd/UMTK -- see STATE.md). This import is a user-initiated, on-demand fetch
into the operator's OWN ``config/assets/packs/`` (gitignored, never
committed or shipped with Finmeta) -- not Finmeta redistributing anything
itself.
"""
from __future__ import annotations

import re
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import httpx
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

# Known third-party Kometa-style overlay sources an operator can import by
# name instead of hand-typing a zip URL + the paths inside it. Each entry is
# a direct GitHub codeload zip URL (no generic github.com URL parsing --
# that would be a much larger, fragile surface for marginal benefit) plus
# the paths *inside that zip* expand_file/kometa_to_pack need. Verified
# against the real repo tree (GitHub API) before being added -- never
# guessed. jmxd is the only source confirmed to actually work against
# Finmeta's detection model (self-contained overlay YAML, not Plex-
# collection shorthand) -- see the module docstring.
KNOWN_SOURCES: Dict[str, Dict[str, str]] = {
    "jmxd": {
        "label": "jmxd media info (github.com/jmxd/Kometa)",
        "zip_url": "https://github.com/jmxd/Kometa/archive/refs/heads/main.zip",
        "root_dir": "Kometa-main",  # codeload zip root is "<repo>-<branch>/"
        "overlay_file_rel": "overlays/media_info.yml",
        "kometa_root_rel": "overlays/images",
        # Fixed target pack name (SPEC S9 E follow-up, 2026-10-27): the UI used
        # to ask the operator to type a pack name for a download too, which
        # meant "Download a known overlay pack" and the resulting installed
        # pack were two separately-named things an operator had to mentally
        # connect. One known source -> one fixed pack name removes that
        # typing step entirely and lets the Installed Packs list recognize
        # "this pack IS that source" on sight.
        "default_pack_name": "jmxd-media-info",
    },
}

_MAX_DOWNLOAD_BYTES = 150 * 1024 * 1024  # 150 MB -- these are small overlay repos


class KometaSourcesResp(BaseModel):
    sources: Dict[str, Dict[str, str]]


@router.get("/sources", response_model=KometaSourcesResp)
def kometa_sources():
    """Known downloadable overlay sources (label + default pack name + zip_url, no local paths)."""
    return {"sources": {k: {"label": v["label"], "zip_url": v["zip_url"],
                            "default_pack_name": v["default_pack_name"]}
                        for k, v in KNOWN_SOURCES.items()}}


def _missing(copies: List[Tuple[str, str]]) -> List[str]:
    """Source images that do not exist on disk (reported, never a crash)."""
    return [src for src, _ in copies if not Path(src).is_file()]


def _import_result(pack_yml: Dict[str, Any], copies: List[Tuple[str, str]], report: ImportReport,
                    pack_name: str, dry_run: bool, overwrite: bool, config_dir: Path) -> Dict[str, Any]:
    """Shared dry-run/write logic for the remote-download import endpoint."""
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


class KometaImportRemoteReq(BaseModel):
    source: Optional[str] = Field(default=None, description="Name of a known source in KNOWN_SOURCES (e.g. 'jmxd').")
    source_url: Optional[str] = Field(default=None, description="Direct zip URL (http/https) as an alternative to `source`.")
    root_dir: Optional[str] = Field(default=None, description="Zip-relative dir the other *_rel paths are relative to (required with source_url).")
    overlay_file_rel: Optional[str] = Field(default=None, description="Path to the overlay YAML, relative to root_dir (required with source_url).")
    kometa_root_rel: Optional[str] = Field(default=None, description="Path to the images root, relative to root_dir (required with source_url).")
    pack_name: str = Field(description="Target pack name; slug [a-z0-9-].")
    dry_run: bool = True
    overwrite: bool = False


def _resolve_remote_spec(req: KometaImportRemoteReq) -> Dict[str, str]:
    if req.source:
        spec = KNOWN_SOURCES.get(req.source)
        if spec is None:
            raise HTTPException(status_code=400, detail=f"unknown source {req.source!r}; see GET /api/kometa/sources")
        return spec
    if not (req.source_url and req.root_dir and req.overlay_file_rel and req.kometa_root_rel):
        raise HTTPException(status_code=400,
                            detail="pass either `source` or all of source_url/root_dir/overlay_file_rel/kometa_root_rel")
    return {"zip_url": req.source_url, "root_dir": req.root_dir,
            "overlay_file_rel": req.overlay_file_rel, "kometa_root_rel": req.kometa_root_rel}


@router.post("/import-remote")
def kometa_import_remote(req: KometaImportRemoteReq, ctx: AppContext = Depends(get_ctx)):
    """Download a known (or custom) Kometa-style overlay source and import it.

    Stages the zip under ``<config>/cache/kometa_downloads/<pack_name>/``
    (gitignored, re-downloaded fresh on every call) and delegates to the
    expand_file/kometa_to_pack pipeline. dry_run (default) returns the
    report + pack.yml + planned image copies without writing anything; with
    ``dry_run=false`` the pack is written to
    ``<config>/assets/packs/<pack_name>/``, ``overwrite=true`` required to
    replace an existing one (409 otherwise). Missing source images are
    listed in ``missing_images`` and skipped, never an error.
    """
    if not _PACK_NAME_RE.match(req.pack_name):
        raise HTTPException(status_code=400, detail="pack_name must be a slug of [a-z0-9-]")
    spec = _resolve_remote_spec(req)
    staged = _stage_source(spec["zip_url"], req.pack_name, ctx.config_dir)
    root = staged / spec["root_dir"]
    if not root.is_dir():
        raise HTTPException(status_code=400,
                            detail=f"downloaded archive has no top-level dir {spec['root_dir']!r} "
                                   f"(found: {[p.name for p in staged.iterdir()]})")
    overlay_file = root / spec["overlay_file_rel"]
    kometa_root = root / spec["kometa_root_rel"]
    if not overlay_file.is_file():
        raise HTTPException(status_code=400, detail=f"overlay_file_rel not found in archive: {spec['overlay_file_rel']}")
    if not kometa_root.is_dir():
        raise HTTPException(status_code=400, detail=f"kometa_root_rel not found in archive: {spec['kometa_root_rel']}")

    flat, report = expand_file(overlay_file)
    pack_yml, copies, report = kometa_to_pack(flat, report, kometa_root, req.pack_name)
    return _import_result(pack_yml, copies, report, req.pack_name, req.dry_run, req.overwrite, ctx.config_dir)


def _download_zip(url: str, dest: Path) -> None:
    """Download ``url`` to ``dest``, http/https only, size-capped (SPEC §5 SSRF guidance).

    httpx, built fresh per call -- same pattern as routes_logo_packs.py's
    _build_client(), so tests can swap in a mock transport without any real
    network access.
    """
    scheme = urlparse(url).scheme
    if scheme not in ("http", "https"):
        raise HTTPException(status_code=400, detail=f"unsupported URL scheme: {scheme!r} (http/https only)")
    try:
        with httpx.Client(follow_redirects=True, timeout=30.0) as client:
            with client.stream("GET", url) as resp:
                resp.raise_for_status()
                written = 0
                with open(dest, "wb") as f:
                    for chunk in resp.iter_bytes(chunk_size=1024 * 256):
                        written += len(chunk)
                        if written > _MAX_DOWNLOAD_BYTES:
                            raise HTTPException(status_code=400, detail="download exceeded 150 MB limit")
                        f.write(chunk)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=400, detail=f"download failed: {exc}") from exc


def _safe_extract(zf: zipfile.ZipFile, target_dir: Path) -> None:
    """Extract ``zf`` into ``target_dir``, refusing any entry that would escape it (zip slip)."""
    target_dir = target_dir.resolve()
    for member in zf.infolist():
        dest = (target_dir / member.filename).resolve()
        try:
            dest.relative_to(target_dir)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"refusing unsafe zip entry: {member.filename!r}")
    zf.extractall(target_dir)


def _stage_source(source_url: str, cache_key: str, config_dir: Path) -> Path:
    """Download+extract a zip URL into config/cache/kometa_downloads/<cache_key>/ (gitignored); return the staged root."""
    stage_root = config_dir / "cache" / "kometa_downloads" / cache_key
    if stage_root.exists():
        shutil.rmtree(stage_root)
    stage_root.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        _download_zip(source_url, tmp_path)
        with zipfile.ZipFile(tmp_path) as zf:
            _safe_extract(zf, stage_root)
    finally:
        tmp_path.unlink(missing_ok=True)
    return stage_root


def _ensure_inside(dest: Path, target: Path) -> None:
    """Guarantee a destination stays inside the pack directory (no .. escapes)."""
    try:
        dest.resolve().relative_to(target.resolve())
    except ValueError as exc:
        raise HTTPException(status_code=500, detail=f"internal error: copy target escapes pack dir: {dest}") from exc


__all__ = ["router"]
