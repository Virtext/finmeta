"""Candidate routes: list candidates (with proxy URLs), select, and the proxy."""
from __future__ import annotations

import logging
import re
from typing import Optional, Tuple

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from finmeta.config.models import Config, TvArtwork
from finmeta.engine.matching import match_item
from finmeta.engine.plan_builder import _resolve_item_library
from finmeta.engine.resolution import collect_candidates
from finmeta.jellyfin_client.types import Item

from finmeta.api import proxy as proxy_mod
from finmeta.api.deps import get_ctx
from finmeta.api.state import AppContext

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["candidates"])


def _media_type(item: Item) -> str:
    return "movie" if item.type == "Movie" else "tvshow"


def _artwork_kinds(lib) -> list:
    kinds = []
    art = lib.artwork
    for k in ("poster", "backdrop", "logo", "landscape"):
        if getattr(art, k, False):
            kinds.append(k)
    if isinstance(art, TvArtwork):
        if art.season_posters:
            kinds.append("season_poster")
        if art.episode_thumbs:
            kinds.append("episode_thumb")
    if lib.theme_song:
        kinds.append("theme_song")
    return kinds


def _default_candidates_provider(
    item, cfg: Config, jf, tmdb, sources, mapper,
) -> Tuple[dict, Optional[int]]:
    mt = _media_type(item)
    match = match_item(item, tmdb, mt, cfg.language)
    if match.tmdb_id is None:
        return {}, None
    # Must be the SAME resolution plan_builder.build_plan uses (path-prefix
    # match against this item's real Jellyfin library, see
    # _resolve_item_library's docstring) -- this used to be a much cruder
    # "first configured library of the right movies/tvshows type", which
    # silently picked the WRONG library's artwork-kind settings whenever
    # more than one library of that type was configured (not a contrived
    # case: reported live with three tvshows-type libraries configured at
    # once). Candidates for a kind that library doesn't actually enable
    # (or candidates computed using a DISABLED library's empty kind list)
    # never show up -- looked exactly like "poster/backdrop/logo won't load
    # at all" for any library that wasn't the one this happened to pick.
    _, lib = _resolve_item_library(item, jf, cfg)
    kinds = _artwork_kinds(lib) if lib is not None else []
    by_kind = collect_candidates(
        sources, cfg.sources.precedence, mt, match.tmdb_id, kinds,
        cfg.language, cfg.artwork_language_fallback)
    return by_kind, match.tmdb_id


@router.get("/items/{item_id}/candidates")
def candidates(item_id: str, ctx: AppContext = Depends(get_ctx)) -> dict:
    cfg = ctx.config
    jf = ctx.make_jellyfin(cfg)
    tmdb = ctx.make_tmdb(cfg)
    sources = ctx.make_sources(cfg)
    mapper = ctx.make_mapper(cfg)
    try:
        item = jf.get_item(item_id)
        provider = ctx._candidates_provider or _default_candidates_provider
        by_kind, tmdb_id = provider(item, cfg, jf, tmdb, sources, mapper)
    finally:
        jf.close()
        try:
            tmdb.close()
        except Exception:
            pass

    out = []
    # S9d5: kinds the plan/apply pipeline has locked (first successful
    # apply) -- it skips source resolution for these entirely; shown here so
    # the operator knows reshuffle/override is the only way to change them.
    locked_kinds = sorted(k for k in by_kind if ctx.overrides.get_lock(item_id, k))
    for kind, lst in by_kind.items():
        ovr = ctx.overrides.get(item_id, kind)
        sel_ref = ovr["ref"] if ovr else (lst[0].ref if lst else None)
        for c in lst:
            out.append({
                "kind": c.kind, "source": c.source, "ref": c.ref,
                "proxy_url": f"/api/proxy/{proxy_mod.encode_token(c.kind, c.ref)}",
                "lang": c.lang, "width": c.width, "height": c.height,
                "score": c.score, "selected": (c.ref == sel_ref),
            })
    return {"item_id": item_id, "tmdb_id": tmdb_id, "candidates": out, "locked_kinds": locked_kinds}


class CandidateSelection(BaseModel):
    kind: str
    ref: Optional[str] = None
    source: Optional[str] = None


@router.put("/items/{item_id}/candidates")
def select_candidate(item_id: str, body: CandidateSelection,
                     ctx: AppContext = Depends(get_ctx)) -> dict:
    ctx.set_candidate(item_id, body.kind, body.ref, body.source)
    # Without this, ctx.get_plan() right below returns whatever this item's
    # plan was cached as BEFORE the override above was set (AppContext.get_plan
    # keys its cache on (generation, scope) and a bare override write never
    # bumps the generation) -- the response (and so the UI, which renders
    # straight from it) silently kept showing the old selection. The three
    # sibling override endpoints (skip_kind, unskip_kind, reshuffle, all
    # below) already call this; this one was the one missing it. Reported
    # live: "clicking poster/backdrop/logo candidates doesn't switch
    # anything, even with dry_run off."
    ctx.invalidate_plans()
    try:
        plan = ctx.get_plan(item_id=item_id)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"plan recompute failed: {exc}")
    item = next((ip for ip in plan.items if ip.item_id == item_id), None)
    return {
        "ok": True, "item_id": item_id, "kind": body.kind,
        "selected": body.ref,
        "item_plan": item.model_dump() if item is not None else None,
    }


class SkipKindsRequest(BaseModel):
    kind: str


@router.post("/items/{item_id}/skip-kinds")
def skip_kind(item_id: str, body: SkipKindsRequest,
              ctx: AppContext = Depends(get_ctx)) -> dict:
    """S9d4b per-row handler: skip a file kind for this item.

    The kind is dropped from the plan (no file written) until unskipped.
    """
    ctx.overrides.set_skip(item_id, body.kind)
    ctx.invalidate_plans()
    return {"ok": True, "item_id": item_id, "kind": body.kind,
            "skipped": ctx.overrides.skipped_kinds(item_id)}


@router.delete("/items/{item_id}/skip-kinds/{kind}")
def unskip_kind(item_id: str, kind: str,
                ctx: AppContext = Depends(get_ctx)) -> dict:
    """S9d4b per-row handler: undo a skip for this item/kind."""
    removed = ctx.overrides.clear_skip(item_id, kind)
    ctx.invalidate_plans()
    return {"ok": True, "item_id": item_id, "kind": kind,
            "removed": removed,
            "skipped": ctx.overrides.skipped_kinds(item_id)}


class ReshuffleRequest(BaseModel):
    kinds: list[str]


@router.post("/items/{item_id}/reshuffle")
def reshuffle(item_id: str, body: ReshuffleRequest,
              ctx: AppContext = Depends(get_ctx)) -> dict:
    """S9d4a step 2: bump per-kind reshuffle counters so the deterministic
    ``random_top_n`` preset picks a different candidate on re-plan.

    S9d5 ("re-pick once", built on top of this existing endpoint per
    CLAUDE.md): also clears any artwork lock for the kind. Once an item+kind
    has been successfully applied, it is locked (``OverrideStore.set_lock``)
    and ``engine.plan_builder`` skips source resolution for it entirely --
    bumping the counter alone would have no visible effect without also
    breaking that lock so the next plan/apply actually re-resolves.
    """
    counts = {}
    for kind in body.kinds:
        counts[kind] = ctx.overrides.increment_reshuffle(item_id, kind)
        ctx.overrides.clear_lock(item_id, kind)
    ctx.invalidate_plans()
    return {"ok": True, "item_id": item_id, "counts": counts}


@router.get("/proxy/{token}")
def proxy(token: str, ctx: AppContext = Depends(get_ctx)) -> Response:
    kind, ref = proxy_mod.decode_token(token)
    if not ref:
        raise HTTPException(status_code=404, detail="bad proxy token")
    try:
        data, ctype = proxy_mod.fetch(kind, ref, allowed_roots=ctx.proxy_roots())
    except proxy_mod.RefNotAllowed as exc:
        # The token is client-minted (see api.proxy's module docstring), so a
        # disallowed ref is a policy rejection, not a server fault. The reason
        # is logged; the client is told only that it was refused, so the route
        # is not a path/host existence oracle.
        logger.warning("proxy refused ref %r: %s", ref, exc)
        raise HTTPException(status_code=403, detail="candidate ref not allowed")
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="candidate not found")
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"upstream fetch failed: {exc}")
    # A token is a deterministic encoding of (kind, ref) -- the same token
    # always resolves to the same bytes, so it's safe (and was previously
    # missing entirely) to let the browser cache it instead of re-fetching
    # on every render. Candidate grids can carry 50-100+ live-proxied
    # thumbnails (see CandidatePicker.tsx); with zero cache headers, any
    # re-render that remounts an <img> (switching tabs, selecting another
    # candidate and getting the list back, etc.) re-fetched every one of
    # them from scratch -- reported live as thumbnails visibly reloading/
    # "jumping" every time something nearby changed.
    return Response(content=data, media_type=ctype,
                    headers={"Cache-Control": "public, max-age=86400"})


@router.get("/proxy/overlay/{digest}")
def proxy_overlay(digest: str, ctx: AppContext = Depends(get_ctx)) -> Response:
    """Serve the rendered-overlay bytes for a poster ``FileWrite``'s content_sha256.

    The plain ``/proxy/{token}`` route above serves ``source_ref``, which
    ``engine.overlay.overlay_file_write`` deliberately keeps pointed at the
    PRE-overlay original (O7: so a later re-run never composites badges onto
    an already-overlaid image). That means the UI's poster preview was
    showing the un-overlaid candidate even right after a real Apply had
    written the overlaid version to disk -- reported live 2026-10-31. The
    overlaid bytes themselves are already cached by this exact digest during
    ``build_plan`` (every poster ``FileWrite``'s ``content_sha256`` IS an
    overlay-cache key); this route is the read side of that same cache, not
    a new render path.
    """
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise HTTPException(status_code=404, detail="bad digest")
    from finmeta.engine.overlay import read_overlay_cache
    data = read_overlay_cache(digest, config_dir=ctx.config_dir)
    if data is None:
        # Pruned, or the plan was built before the cache existed -- the
        # caller (PreviewPane) falls back to the plain candidate proxy.
        raise HTTPException(status_code=404, detail="overlay not cached")
    return Response(content=data, media_type="image/jpeg",
                    headers={"Cache-Control": "public, max-age=86400, immutable"})


__all__ = ["router"]
