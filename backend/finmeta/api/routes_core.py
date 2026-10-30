"""Core routes: health, libraries, items (with current plan diff), item plan."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from finmeta import __version__
from finmeta.config.models import Config
from finmeta.engine.diff import diff_file
from finmeta.engine.plan import ItemPlan, WritePlan
from finmeta.engine.plan_builder import _profile_overlay_names
from finmeta.jellyfin_client.client import JellyfinError

from finmeta.api.deps import get_ctx
from finmeta.api.state import AppContext
from finmeta.config.secretspec import valid_api_key_ref

router = APIRouter(prefix="/api", tags=["core"])


@router.get("/format-rules/defaults")
def format_rules_defaults() -> dict:
    """The built-in K9 default format rules (single source of truth).

    The UI loads these from here instead of keeping its own hardcoded copy, so
    the Format rules section can never drift from the backend defaults.
    """
    from finmeta.engine.format_rules import default_format_rules
    return {"rules": default_format_rules()}


def _item_entries(item: ItemPlan) -> tuple:
    """Per-item current diff: file entries + create/update/unchanged counts."""
    entries = []
    counts = {"create": 0, "update": 0, "unchanged": 0}
    for fw in item.files:
        e = diff_file(fw)
        counts[e.status] += 1
        entries.append({
            "kind": e.kind, "status": e.status,
            "target_path": e.target_path, "source_ref": e.source_ref,
        })
    if not item.files:
        # NOT a TMDb match failure: an item that genuinely failed to match
        # never reaches this function at all (plan_builder puts it in
        # plan.unmatched with a reason instead -- see build_plan's Movie/
        # Series branches). Every ItemPlan here was matched; "no files" means
        # nothing was generated for it (path mapping couldn't resolve its
        # folder, or every metadata/artwork/overlay setting is off for its
        # library) -- "unmatched" was a misleading label for that and looked
        # exactly like the real thing (reported live: a library with several
        # genuinely-unmatched shows made every OTHER, successfully-matched-
        # but-nothing-enabled item look identically broken).
        status = "empty"
    elif counts["create"] or counts["update"]:
        status = "changes"
    else:
        status = "clean"
    return entries, counts, status


def _item_view(item: ItemPlan) -> dict:
    entries, counts, status = _item_entries(item)
    return {
        "item_id": item.item_id, "item_name": item.item_name,
        "media_type": item.media_type, "tmdb_id": item.tmdb_id,
        "matched_by": item.matched_by, "files": entries,
        "diff": counts, "status": status,
    }


@router.get("/health")
def health(ctx: AppContext = Depends(get_ctx)) -> dict:
    return {
        "status": "ok",
        "version": __version__,
        "config_loaded": True,
        "config_error": ctx.config_error,
        "dry_run": ctx.config.dry_run,
        "active_run": ctx.active_run,
        "active_run_scope": ctx.active_run_scope,
    }


@router.get("/libraries")
def libraries(ctx: AppContext = Depends(get_ctx)) -> dict:
    cfg = ctx.config
    out = []
    for name, lib in cfg.libraries.items():
        art = lib.artwork
        art_fields = [k for k in art.model_dump() if getattr(art, k, False)]
        out.append({
            "name": name, "type": lib.type,
            "metadata": lib.metadata.enabled,
            "theme_song": lib.theme_song,
            "overlays": list(lib.overlays),
            "artwork_enabled": art_fields,
        })
    # `sample_size` was exposed here (S9d4c Fase A step 5) only so
    # SamplePreview.tsx could stop hardcoding it. That widget and the sample
    # endpoint were both removed on operator request 2026-10-22, so the field
    # went with them instead of lingering as a value nothing reads.
    return {"libraries": out, "dry_run": cfg.dry_run}


@router.get("/jellyfin/libraries")
def jellyfin_libraries(ctx: AppContext = Depends(get_ctx)) -> dict:
    """List the REAL libraries in Jellyfin (K8) via GET /Library/VirtualFolders.

    Any number of libraries — no fixed Movies/TV assumption. Each entry carries
    the Jellyfin id, name and collection type, plus whether Finmeta config knows
    it and whether it is enabled there. Libraries not mentioned in config are
    reported as ``configured: false`` and stay untouched (anime included).
    """
    cfg = ctx.config
    jf = ctx.make_jellyfin(cfg)
    try:
        libs = []
        for lib in jf.get_libraries():
            known = lib.name in cfg.libraries
            lc = cfg.libraries.get(lib.name)
            libs.append({
                "id": lib.item_id,
                "name": lib.name,
                "collection_type": lib.collection_type,
                "locations": list(lib.locations),
                "configured": known,
                "enabled": bool(getattr(lc, "enabled", False)) if lc else False,
                "overlay_profile": getattr(lc, "overlay_profile", None) if lc else None,
            })
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Jellyfin library list failed: {exc}")
    finally:
        jf.close()
    return {"libraries": libs}


def _jellyfin_502_detail(exc: Exception) -> str:
    """Map a plan-build exception to an operator-friendly 502 detail.

    A 401 from Jellyfin means the API key was rejected — say so explicitly
    instead of dumping the raw upstream message."""
    if isinstance(exc, JellyfinError) and exc.status_code == 401:
        return ("Jellyfin rejected the API key (401) — check JELLYFIN_API_KEY "
                "or Settings → Secrets")
    return f"plan build failed: {exc}"


@router.get("/libraries/{name}/items/light")
def library_items_light(
    name: str, ctx: AppContext = Depends(get_ctx),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    search: Optional[str] = Query(default=None),
) -> dict:
    """Jellyfin-only item list: no TMDb/fanart/etc. lookups, no WritePlan.

    2026-10-23: even paginated, a full per-item plan build is ~7s/item
    (cold network calls) -- unusable as the thing that renders the moment
    you open a library. This is what the browser now loads first (names
    only, for the search box); the full diff/status for one specific item
    is still fetched on demand via GET /items/{item_id}/plan the moment you
    select it, same as before.

    Pagination fix (2026-10-30): this used to fetch the WHOLE library with
    no Limit/IncludeItemTypes, recursive -- for a TV library that also
    pulls every season and episode with Path/ProviderIds/Overview. On a
    tester's large library that single call timed out (httpx.ReadTimeout
    at both 30s and 120s). Now bounded (`limit`/`offset`, default 100,
    1-500) and type-filtered (Movie/Series only) at Jellyfin via
    get_items_page(), with minimal fields (no Path/ProviderIds/Overview --
    this endpoint never reads them). `search` forwards to Jellyfin's own
    SearchTerm so the UI's search box keeps working across pages without
    loading everything client-side.
    """
    if name not in ctx.config.libraries:
        raise HTTPException(status_code=404, detail=f"unknown library {name!r}")
    lib_cfg = ctx.config.libraries[name]
    jf = ctx.make_jellyfin()
    try:
        jf_lib = jf.get_library(name)
        if jf_lib is None:
            page_items, total = [], 0
        else:
            page = jf.get_items_page(
                parent_id=jf_lib.item_id, recursive=True,
                include_item_types=["Movie", "Series"], fields=None,
                limit=limit, start_index=offset, search_term=search,
            )
            page_items, total = page.items, page.total_record_count
    except Exception as exc:
        raise HTTPException(status_code=502, detail=_jellyfin_502_detail(exc))
    finally:
        jf.close()
    light = [
        {"item_id": it.id, "item_name": it.name,
         "media_type": "movie" if it.type == "Movie" else "tvshow"}
        for it in page_items
    ]
    overlay_elements = _profile_overlay_names(lib_cfg, ctx.config)
    return {
        "library": name,
        "enabled": bool(getattr(lib_cfg, "enabled", False)),
        "overlay_profile": getattr(lib_cfg, "overlay_profile", None),
        "overlay_element_count": len(overlay_elements),
        "media_apps": {
            kind: [
                {**inst.model_dump(),
                 "api_key_valid": valid_api_key_ref(ctx.config, inst.api_key)}
                for inst in getattr(ctx.config.media_apps, kind)
            ]
            for kind in ("sonarr", "radarr")
        },
        "items": light,
        "total_items": total,
        "limit": limit,
        "offset": offset,
        "has_more": offset + len(light) < total,
    }


@router.get("/libraries/{name}/items")
def library_items(
    name: str, ctx: AppContext = Depends(get_ctx),
    limit: Optional[int] = Query(default=None, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict:
    if name not in ctx.config.libraries:
        raise HTTPException(status_code=404, detail=f"unknown library {name!r}")
    try:
        # limit/offset page the expensive per-item work (TMDb/fanart/etc.) --
        # see plan_builder.build_plan -- so opening a library with hundreds
        # of titles doesn't block on building a plan for every one of them.
        plan: WritePlan = ctx.get_plan(library=name, limit=limit, offset=offset)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=_jellyfin_502_detail(exc))
    items = [_item_view(ip) for ip in plan.items]
    # S9d4c Fase A step 3: single source of truth for "does this library get
    # overlays" (profile wins over the deprecated list, auto-migration
    # already applied at config-load time) -- same helper plan_builder uses.
    lib_cfg = ctx.config.libraries[name]
    overlay_elements = _profile_overlay_names(lib_cfg, ctx.config)
    return {
        "library": name,
        "enabled": bool(getattr(lib_cfg, "enabled", False)),
        "overlay_profile": getattr(lib_cfg, "overlay_profile", None),
        "overlay_element_count": len(overlay_elements),
        # S9d2-fix2 step 6a: flag media-app instances whose api_key names a
        # secret that does not exist (invalid reference — re-save instance).
        "media_apps": {
            kind: [
                {**inst.model_dump(),
                 "api_key_valid": valid_api_key_ref(ctx.config, inst.api_key)}
                for inst in getattr(ctx.config.media_apps, kind)
            ]
            for kind in ("sonarr", "radarr")
        },
        "items": items,
        # None when this request wasn't paginated (limit omitted) -- the UI
        # treats a present total_items as "there may be more pages".
        "total_items": plan.total_items,
        "unmatched": [{"item_id": u.item_id, "name": u.name, "reason": u.reason}
                      for u in plan.unmatched],
        # S9d2-fix2 step 6b: provider errors that degraded to "no candidates".
        "provider_warnings": [w.model_dump() for w in plan.provider_warnings],
        "summary": {
            "items": len(items),
            "create": sum(i["diff"]["create"] for i in items),
            "update": sum(i["diff"]["update"] for i in items),
            "unchanged": sum(i["diff"]["unchanged"] for i in items),
        },
    }


@router.get("/items/{item_id}/plan")
def item_plan(item_id: str, library: Optional[str] = None,
              ctx: AppContext = Depends(get_ctx)) -> dict:
    try:
        plan = ctx.get_plan(library=library, item_id=item_id)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=_jellyfin_502_detail(exc))
    item = next((ip for ip in plan.items if ip.item_id == item_id), None)
    if item is None:
        # unmatched or not in scope
        unmatched = next((u for u in plan.unmatched if u.item_id == item_id), None)
        if unmatched is not None:
            raise HTTPException(status_code=404,
                                detail=f"item {item_id} unmatched: {unmatched.reason}")
        # build_plan() returns an empty plan (no items, no unmatched entries)
        # instead of raising for two cases: the item's resolved library is
        # disabled (S9d4c step 2a), or its library couldn't be resolved at
        # all (its path matched no configured library's Locations). Surface
        # whichever one applies instead of a bare, unhelpful "not in plan"
        # that gave no indication anything was configurable.
        config_warning = next((w for w in plan.provider_warnings if w.provider == "config"), None)
        if config_warning is not None:
            raise HTTPException(status_code=404, detail=f"item {item_id}: {config_warning.error}")
        raise HTTPException(status_code=404, detail=f"item {item_id} not in plan")
    entries, counts, status = _item_entries(item)
    # S9d4b step 4: server is the truth source for skipped kinds.
    skipped = sorted(ctx.overrides.skipped_kinds(item_id))
    return {
        "item_id": item.item_id, "item_name": item.item_name,
        "media_type": item.media_type, "tmdb_id": item.tmdb_id,
        "matched_by": item.matched_by, "files": entries,
        "diff": counts, "status": status, "skipped": skipped,
        "plan": item.model_dump(),
    }


__all__ = ["router"]
