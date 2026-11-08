"""Backup & Restore (SPEC_S11 §D).

Export the operator's decisions -- configuration, library profiles, artwork
picks and locks -- as one zip; inspect a bundle before trusting it; restore it
through the existing config path so schema validation, the last-good copy, the
config-undo route and the scheduler hot-reload all keep working.

Secrets are never exported and never restored. See
``engine/backup_bundle`` for why that is structural rather than a default.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response

from finmeta import __version__
from finmeta.api.deps import get_ctx
from finmeta.api.state import AppContext
from finmeta.config.secretspec import secret_specs
from finmeta.engine import scan_store
from finmeta.engine.backup_bundle import (
    EXCLUDED, Bundle, BundleError, build_bundle, bundle_filename,
    installed_packs, safe_bundle_path,
)

logger = logging.getLogger("finmeta.api.backup")

router = APIRouter(prefix="/api", tags=["backup"])

#: Upload cap for a restore. A bundle is config + a JSON dump; anything this
#: large is not one.
MAX_UPLOAD_BYTES = 16 * 1024 * 1024


#: Content types accepted as "the raw bundle is this request's body".
#: Deliberately NOT multipart: that needs python-multipart, and a new runtime
#: dependency for one upload field is not worth it when a raw body works in
#: one line of fetch() on the other side.
_ZIP_TYPES = ("application/zip", "application/x-zip-compressed",
              "application/octet-stream")


def _bundles_dir(ctx: AppContext) -> Path:
    d = ctx.config_dir / "backups" / "bundles"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _secret_names(ctx: AppContext) -> List[str]:
    try:
        return [spec.name for spec in secret_specs(ctx.config)]
    except Exception as exc:          # a broken config must not block a backup
        logger.warning("could not list secret names: %s", exc)
        return []


def _scan_payload(ctx: AppContext) -> Optional[Dict[str, Any]]:
    """The newest scan plus its findings, or None when there is none."""
    scan = scan_store.latest_scan(ctx.db_path, finished_only=True)
    if scan is None:
        return None
    findings, total = scan_store.list_findings(ctx.db_path, scan["scan_id"], limit=500)
    return {"scan": scan, "findings": findings, "total": total}


def _build(ctx: AppContext, include_scan: Optional[bool] = None) -> bytes:
    want_scan = (ctx.config.backup.include_scan if include_scan is None
                 else bool(include_scan))
    return build_bundle(
        config_text=ctx.config_text(),
        overrides=ctx.overrides.export_all(),
        packs=installed_packs(ctx.config_dir),
        secret_names=_secret_names(ctx),
        scan=_scan_payload(ctx) if want_scan else None,
        finmeta_version=__version__,
    )


@router.get("/backup/export")
def export_bundle(ctx: AppContext = Depends(get_ctx),
                  include_scan: Optional[bool] = Query(default=None)) -> Response:
    """Download a bundle without storing anything on the server."""
    data = _build(ctx, include_scan)
    name = bundle_filename(ctx.config.server.url.split("//")[-1].split(":")[0])
    return Response(
        content=data, media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.post("/backup/snapshot")
def snapshot(ctx: AppContext = Depends(get_ctx),
             include_scan: Optional[bool] = Query(default=None)) -> dict:
    """Write a bundle into ``<config>/backups/bundles/`` and prune old ones."""
    data = _build(ctx, include_scan)
    target = _bundles_dir(ctx) / bundle_filename()
    target.write_bytes(data)
    pruned = _prune(ctx)
    return {"bundle": target.name, "bytes": len(data), "pruned": pruned}


def _prune(ctx: AppContext) -> List[str]:
    """Delete snapshots beyond ``backup.keep_bundles`` (0 = keep all)."""
    keep = int(getattr(ctx.config.backup, "keep_bundles", 0) or 0)
    if keep <= 0:
        return []
    files = sorted(_bundles_dir(ctx).glob("finmeta-backup-*.zip"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    dropped = []
    for old in files[keep:]:
        try:
            old.unlink()
            dropped.append(old.name)
        except OSError as exc:
            logger.warning("could not prune %s: %s", old, exc)
    return dropped


@router.get("/backup/bundles")
def list_bundles(ctx: AppContext = Depends(get_ctx)) -> dict:
    """Stored snapshots, newest first."""
    out = []
    for p in sorted(_bundles_dir(ctx).glob("finmeta-backup-*.zip"),
                    key=lambda p: p.stat().st_mtime, reverse=True):
        stat = p.stat()
        out.append({"name": p.name, "bytes": stat.st_size,
                    "modified_at": int(stat.st_mtime)})
    return {"bundles": out, "keep_bundles": ctx.config.backup.keep_bundles,
            "excluded": list(EXCLUDED)}


@router.get("/backup/bundles/{name}")
def download_bundle(name: str, ctx: AppContext = Depends(get_ctx)) -> Response:
    try:
        path = safe_bundle_path(_bundles_dir(ctx), name)
    except BundleError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"no such bundle {name!r}")
    return Response(
        content=path.read_bytes(), media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.delete("/backup/bundles/{name}")
def delete_bundle(name: str, ctx: AppContext = Depends(get_ctx)) -> dict:
    try:
        path = safe_bundle_path(_bundles_dir(ctx), name)
    except BundleError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"no such bundle {name!r}")
    path.unlink()
    return {"deleted": name}


async def _bundle_from_request(request: Request, ctx: AppContext) -> tuple:
    """``(bundle_bytes, options)`` from either request shape.

    A raw zip body carries its options as query parameters; a JSON body names
    a stored snapshot and carries them as fields.
    """
    content_type = (request.headers.get("content-type") or "").split(";")[0].strip()
    body = await request.body()
    if len(body) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"bundle is larger than {MAX_UPLOAD_BYTES} bytes")
    if content_type in _ZIP_TYPES and body:
        params = request.query_params
        return body, {"mode": params.get("mode", "merge"),
                      "confirm": params.get("confirm", "").lower() in ("1", "true", "yes")}
    options: Dict[str, Any] = {"mode": "merge", "confirm": False}
    name: Optional[str] = None
    if body:
        import json
        try:
            payload = json.loads(body)
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail="send the bundle as a zip body (Content-Type: application/zip) "
                       "or a JSON body naming a stored bundle")
        if isinstance(payload, dict):
            name = payload.get("bundle")
            options["mode"] = payload.get("mode", "merge")
            options["confirm"] = bool(payload.get("confirm", False))
    if not name:
        raise HTTPException(status_code=422,
                            detail="upload a bundle file or name a stored one")
    return _load_stored(ctx, name), options


def _load_stored(ctx: AppContext, name: str) -> bytes:
    try:
        path = safe_bundle_path(_bundles_dir(ctx), name)
    except BundleError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"no such bundle {name!r}")
    return path.read_bytes()


def _diff_summary(ctx: AppContext, config_text: Optional[str]) -> Dict[str, Any]:
    """What a restore of this config would change, in broad strokes."""
    if not config_text:
        return {"config": "absent"}
    try:
        incoming = yaml.safe_load(config_text) or {}
    except yaml.YAMLError as exc:
        raise HTTPException(status_code=422,
                            detail=f"bundle config.yml is not valid YAML: {exc}")
    current = ctx.config.model_dump(mode="json")
    cur_libs = set((current.get("libraries") or {}).keys())
    new_libs = set((incoming.get("libraries") or {}).keys())
    cur_profiles = {p.get("name") for p in (current.get("overlays") or {}).get("profiles", [])
                    if isinstance(p, dict)}
    new_profiles = {p.get("name") for p in (incoming.get("overlays") or {}).get("profiles", [])
                    if isinstance(p, dict)}
    return {
        "libraries": {"added": sorted(new_libs - cur_libs),
                      "removed": sorted(cur_libs - new_libs),
                      "kept": sorted(cur_libs & new_libs)},
        "overlay_profiles": {"added": sorted(n for n in new_profiles - cur_profiles if n),
                             "removed": sorted(n for n in cur_profiles - new_profiles if n)},
        "schedules": {"current": len(current.get("schedules") or []),
                      "incoming": len(incoming.get("schedules") or [])},
        "dry_run": {"current": current.get("dry_run"),
                    "incoming": incoming.get("dry_run")},
    }


@router.post("/backup/inspect")
async def inspect_bundle(request: Request,
                         ctx: AppContext = Depends(get_ctx)) -> dict:
    """Read a bundle and report what restoring it WOULD do. Writes nothing."""
    data, _options = await _bundle_from_request(request, ctx)
    try:
        parsed = Bundle(data)
    except BundleError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    try:
        overrides = parsed.overrides
        summary = {
            "manifest": parsed.manifest,
            "members": parsed.names(),
            "overrides": {table: len(rows) for table, rows in overrides.items()},
            "packs": parsed.packs,
            "diff": _diff_summary(ctx, parsed.config_text),
            "secrets_required": parsed.manifest.get("secret_names") or [],
            "note": "Secrets are not in the bundle and will not be restored; "
                    "re-enter the keys listed in secrets_required afterwards.",
        }
    finally:
        parsed.close()
    return summary


@router.post("/backup/restore")
async def restore_bundle(request: Request,
                         ctx: AppContext = Depends(get_ctx)) -> dict:
    """Restore a bundle. Requires ``confirm`` -- this overwrites the config.

    Order matters and the first failure stops everything: the config goes
    first, through ``AppContext.put_config``, so an invalid bundle is rejected
    by the real schema validation before any override row is touched. The
    previous config stays available through the existing config-undo route.
    """
    data, options = await _bundle_from_request(request, ctx)
    mode = str(options.get("mode") or "merge")
    if mode not in ("merge", "replace"):
        raise HTTPException(status_code=422,
                            detail="mode must be 'merge' or 'replace'")
    if not options.get("confirm"):
        raise HTTPException(
            status_code=409,
            detail="restoring overwrites the current configuration -- resend "
                   "with confirm=true (inspect it first if unsure)")
    try:
        parsed = Bundle(data)
    except BundleError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    restored: Dict[str, Any] = {"config": False, "overrides": {}}
    manifest = dict(parsed.manifest)
    try:
        config_text = parsed.config_text
        if config_text:
            try:
                ctx.put_config(config_text)
            except Exception as exc:
                # Schema/validation failure: nothing else has been touched.
                raise HTTPException(
                    status_code=422,
                    detail=ctx.redact(f"bundle config.yml was rejected: {exc}"))
            restored["config"] = True
        overrides = parsed.overrides
        if overrides:
            restored["overrides"] = ctx.overrides.import_all(overrides, mode)
        packs = parsed.packs
    finally:
        parsed.close()

    return {
        "restored": restored,
        "mode": mode,
        "packs_to_reimport": packs,
        "secrets_required": manifest.get("secret_names") or [],
        "note": "Secrets were not restored (they are never in a bundle). "
                "Re-enter them in Settings, and re-import any packs listed.",
    }


__all__ = ["router", "MAX_UPLOAD_BYTES"]
