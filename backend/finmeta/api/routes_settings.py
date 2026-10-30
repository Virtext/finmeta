"""Settings routes (S9 §A): write-only secrets, service test, help.

Secrets are never returned to the client — only ``{configured, source, hint}``.
``POST /api/services/{name}/test`` returns ``{ok, latency_ms, version, error}``.
``GET /api/help`` serves the merged help registry.
"""
from __future__ import annotations

import os
import time
from typing import Dict, Optional

import base64
import json
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from finmeta.config.loading import ConfigError

import base64
from typing import List

from pydantic import BaseModel, Field
from typing import List

import re as _re

from finmeta.api.deps import get_ctx
from finmeta.api.state import AppContext
from finmeta.api.secrets import secret_specs, secret_status, SecretSpec
from finmeta.config.secretspec import is_arr_secret_name
from finmeta.api.help import build_help
from finmeta.api import proxy as proxy_mod

# (overlay preview renderer is imported inside its route to avoid a cycle.)


router = APIRouter(prefix="/api", tags=["settings"])

#: How many items ``POST /api/overlays/preview`` will render in one request.
#: Previously ``routes_profiles.SAMPLE_SIZE``, shared with the seeded-sample
#: endpoint that was removed on operator request 2026-10-22; this preview cap
#: was its only other use, so it moved to where it is actually enforced.
MAX_PREVIEW_ITEMS = 6


class ArrTestConfigIn(BaseModel):
    """Typed form values for ``POST /api/services/{sonarr|radarr}/test-config``.

    The ``api_key`` is used only for this one request; it is never logged or
    stored by this call."""
    name: str = Field(min_length=1, max_length=200)
    url: str = Field(min_length=1, max_length=500)
    api_key: str = ""
    timeout: float = Field(default=30.0, gt=0, le=600)
    verify_ssl: bool = True


def _spec_or_404(ctx: AppContext, name: str):
    for s in secret_specs(ctx.config):
        if s.name == name:
            return s
    # S9d2-fix2 step A: instance api_keys live in the store under arbitrary
    # operator-chosen names (or <kind>_<inst>_api_key from the form). The store
    # is the source of truth, so accept any name that is not otherwise reserved.
    if is_arr_secret_name(name):
        return SecretSpec(name=name, env_var="", short="Media app API key", long="")
    raise HTTPException(status_code=404, detail=f"unknown secret {name!r}")


def _secret_view(ctx: AppContext, name: str, s) -> dict:
    st = secret_status(ctx.secrets, s)
    return {
        "name": name,
        **st,
        "env_var": s.env_var,
        "locked": st["source"] == "env",
        "short": s.short,
        "help": s.long,
    }


# ---------------------------------------------------------------------------
# secrets (write-only)
# ---------------------------------------------------------------------------

@router.get("/secrets")
def list_secrets(ctx: AppContext = Depends(get_ctx)) -> dict:
    return {
        "encrypted_at_rest": ctx.secrets.encrypted_at_rest,
        "secrets": {
            s.name: _secret_view(ctx, s.name, s) for s in secret_specs(ctx.config)
        },
    }


@router.get("/secrets/{name}")
def get_secret(name: str, ctx: AppContext = Depends(get_ctx)) -> dict:
    return _secret_view(ctx, name, _spec_or_404(ctx, name))


class SecretValueIn(BaseModel):
    value: str = Field(min_length=1)


@router.put("/secrets/{name}")
def put_secret(name: str, body: SecretValueIn, ctx: AppContext = Depends(get_ctx)) -> dict:
    s = _spec_or_404(ctx, name)
    if os.environ.get(s.env_var):
        raise HTTPException(
            status_code=409,
            detail=f"{name} is set via environment variable {s.env_var}; "
                   f"unset it to edit here",
        )
    ctx.secrets.set(name, body.value)
    return {"ok": True, **secret_status(ctx.secrets, s)}


@router.delete("/secrets/{name}")
def delete_secret(name: str, ctx: AppContext = Depends(get_ctx)) -> dict:
    s = _spec_or_404(ctx, name)
    ctx.secrets.delete(name)
    return {"ok": True, **secret_status(ctx.secrets, s)}


# ---------------------------------------------------------------------------
# service test
# ---------------------------------------------------------------------------

@router.post("/services/{app}/test-config")
def test_arr_config(app: str, body: ArrTestConfigIn,
                    ctx: AppContext = Depends(get_ctx)) -> dict:
    """POST /api/services/{sonarr|radarr}/test-config — test the TYPED form
    values (name/url/api_key/timeout/verify_ssl) before saving.

    ``api_key`` is either a literal key (one-shot test) or the NAME of a secret;
    in the latter case it is resolved through :func:`resolve_secret` so an
    existing instance's stored key can be tested without re-entering it.
    Nothing here is logged or stored."""
    if app not in ("sonarr", "radarr"):
        raise HTTPException(status_code=404, detail=f"unknown service {app!r}")
    key = body.api_key
    resolved = ctx.resolve_secret(key) if key else None
    if resolved:
        key = resolved
    return _run_arr_test(app, body.url, key, body.timeout,
                         body.verify_ssl, ctx)


@router.post("/services/{name}/test")
def test_service(name: str, ctx: AppContext = Depends(get_ctx)) -> dict:
    if ctx.service_testers and name in ctx.service_testers:
        return ctx.service_testers[name](ctx.config)
    # S9f1a: Sonarr/Radarr instances are addressed as "sonarr:<name>" / "radarr:<name>".
    if ":" in name:
        app, inst_name = name.split(":", 1)
        if app in ("sonarr", "radarr"):
            return _test_arr_instance(app, inst_name, ctx)
    return _default_service_test(name, ctx)


def _find_media_app(cfg, app: str, name: str):
    for inst in getattr(cfg.media_apps, app, []):
        if inst.name == name:
            return inst
    return None


def _test_arr_instance(app: str, name: str, ctx: AppContext) -> dict:
    """POST /api/services/{sonarr|radarr}:<name>/test — system_status() on the
    configured instance (read-only GET). Returns version + response time."""
    inst = _find_media_app(ctx.config, app, name)
    if inst is None:
        raise HTTPException(status_code=404, detail=f"unknown {app} instance {name!r}")
    if not inst.enabled:
        return {"ok": False, "latency_ms": None, "version": None,
                "error": f"{app} instance {name!r} is disabled"}
    key = ctx.resolve_secret(inst.api_key) or ""
    if not key:
        return {"ok": False, "latency_ms": None, "version": None,
                "error": f"no API key set (secret {inst.api_key!r})"}
    return _run_arr_test(app, inst.url, key, inst.timeout, inst.verify_ssl, ctx)


def _run_arr_test(app: str, url: str, api_key: str, timeout: float,
                  verify_ssl: bool, ctx: AppContext) -> dict:
    """Run a read-only system_status() against the given Sonarr/Radarr URL.

    Shared by the per-instance ``/test`` route and the pre-save
    ``/test-config`` route. The key is only sent as the ``X-Api-Key`` header;
    errors are redacted so it never leaks into responses or logs."""
    from finmeta.arr.sonarr import SonarrClient
    from finmeta.arr.radarr import RadarrClient
    cls = SonarrClient if app == "sonarr" else RadarrClient
    t0 = time.time()
    try:
        with cls(url, api_key=api_key, timeout=timeout, verify_ssl=verify_ssl) as c:
            st = c.system_status()
        ms = int((time.time() - t0) * 1000)
        return {"ok": True, "latency_ms": ms, "version": st.get("version"),
                "error": None}
    except Exception as exc:  # noqa: BLE001
        err = str(exc)
        if api_key and api_key in err:
            err = err.replace(api_key, "[redacted]")
        status = _http_status_of(err)
        if status == 401:
            return {"ok": False, "latency_ms": None, "version": None,
                    "error": (f"HTTP 401: invalid {app} API key or the "
                              f"{app} instance is unreachable")}
        return {"ok": False, "latency_ms": None, "version": None,
                "error": ctx.redact(err)}


def _http_status_of(err: str) -> Optional[int]:
    m = _re.search(r"HTTP (\d{3})", err)
    return int(m.group(1)) if m else None


def _default_service_test(name: str, ctx: AppContext) -> dict:
    """Dispatch to the provider registry's test() callable."""
    from finmeta.providers import get_provider
    prov = get_provider(ctx, name)
    if prov is None:
        raise HTTPException(status_code=404, detail=f"unknown service {name!r}")
    if prov.test is None:
        raise HTTPException(status_code=404, detail=f"no test for service {name!r}")
    return prov.test(ctx)


# ---------------------------------------------------------------------------
# providers
# ---------------------------------------------------------------------------

@router.get("/providers")
def list_providers(ctx: AppContext = Depends(get_ctx)) -> dict:
    """Return all registered providers as views (for the Settings UI)."""
    from finmeta.providers import build_providers
    return {"providers": [p.view() for p in build_providers(ctx)]}


@router.get("/rating-sources")
def rating_sources(ctx: AppContext = Depends(get_ctx)) -> dict:
    """Per-rating-field provider order (the "Rating sources" tab data).

    ``fields[i].providers`` is the configured order filtered to providers that
    can supply the field; ``fields[i].available`` lists every capable provider
    not yet in the list (for the reorder UI); ``enabled_by_name`` maps each
    provider name to its live enabled flag so the UI can grey out disabled or
    unconfigured providers.
    """
    from finmeta.providers import build_providers, rating_source_matrix
    matrix = rating_source_matrix(ctx)
    providers = {p.name: p for p in build_providers(ctx)}
    key_of = lambda f: f["name"].replace("rating_", "")  # noqa: E731
    for f in matrix["fields"]:
        key = key_of(f)
        capable = [n for n, p in providers.items() if key in p.capabilities.ratings]
        f["available"] = [n for n in capable if n not in f["providers"]]
    return {
        "fields": matrix["fields"],
        "enabled_by_name": {n: p.enabled for n, p in providers.items()},
    }


# ---------------------------------------------------------------------------
# help
# ---------------------------------------------------------------------------

class OverlayPreviewIn(BaseModel):
    """Body of ``POST /api/overlays/preview`` (SPEC S9 §D step 1)."""

    style: dict = Field(default_factory=dict, description="OverlayStyleDoc JSON; merged over the current config's overlays.style.")
    item_ids: List[str] = Field(
        min_length=0, max_length=MAX_PREVIEW_ITEMS,
        description=f"Up to {MAX_PREVIEW_ITEMS} items for side-by-side previews.")
    sample_data: bool = Field(
        default=False,
        description='Explicit "Sample data" toggle from the overlay editor: when true, '
                    'placeholder values are shown for elements the item lacks (and listed in samples[]); '
                    'when false (default) missing values simply have no badge.',
    )
    profile: Optional[str] = Field(
        default=None,
        description="Overlay profile name (overlays.profiles.<name>): when set, the preview "
                    "is built with that profile's style/format_rules merged over the global "
                    "values. Unknown names are rejected with 400.",
    )


def _overlay_preview_source(
    ctx: AppContext, item_id: str, sample_data: bool = False,
    profile_rules=None, sonarr_index: Optional[dict] = None,
) -> tuple[Optional[bytes], dict, set, Optional[str], dict, str]:
    """Resolve an item\u2019s best poster candidate plus its media-stream-derived
    overlay values (resolution/HDR/audio; ratings come from per-source fields).

    ``sonarr_index`` (2026-10-23): pass the caller's already-built index --
    MAX_PREVIEW_ITEMS calls into this function previously each rebuilt it
    from scratch (~4s each, found while the operator reported "rendering
    takes extremely long" testing the new network badge). ``None`` builds it
    here as a fallback for any other/future caller.

    Reuses the same provider chain as the candidates endpoint so preview
    posters match what Finmeta would actually apply. Returns ``(None, {})`` when
    no poster is resolvable — the caller reports a per-item error then.
    Overlay *values* never write anything either.
    """
    from finmeta.api.routes_candidates import (
        _default_candidates_provider, _media_type,
    )
    cfg = ctx.config
    jf = ctx.make_jellyfin(cfg)
    tmdb = ctx.make_tmdb(cfg)
    sources = ctx.make_sources(cfg)
    mapper = ctx.make_mapper(cfg)
    status_key: Optional[str] = None
    status_label: Optional[str] = None
    network_name: Optional[str] = None
    try:
        item = jf.get_item(item_id)
        by_kind, _tmdb_id = (ctx._candidates_provider or _default_candidates_provider)(
            item, cfg, jf, tmdb, sources, mapper)
        streams: list = []
        if not isinstance(by_kind, Exception):
            try:
                mt = _media_type(item)
                target_id = item.id
                if mt == "tvshow":
                    first_ep = jf.get_items_page(
                        parent_id=item.id, recursive=True,
                        include_item_types=["Episode"],
                        sort_by=["ParentIndexNumber", "IndexNumber"],
                        sort_order="Ascending", limit=1,
                    ).items
                    if first_ep:
                        target_id = first_ep[0].id
                # RAW MediaStreams keep every field K9 detection needs (DisplayTitle,
                # IsForced, ChannelLayout, ColorTransfer); /Items?Ids= accepts the API key.
                try:
                    streams_raw = list(jf.get_item_media_streams_raw(target_id))
                except Exception:
                    streams_raw = []
                streams = streams_raw
                # S9g1/K7a: same TV-status lookup real applies use, so the
                # preview's "status" ribbon matches what a real apply would
                # draw (the editor used to be unable to preview this at all).
                if mt == "tvshow":
                    from finmeta.engine.plan_builder import _tv_status_for_item, build_sonarr_index
                    if sonarr_index is None:
                        sonarr_index = build_sonarr_index(cfg, ctx.secrets)
                    tv_status_obj = _tv_status_for_item(
                        item, sonarr_index, ctx.config_dir,
                        recent_days=cfg.status_overlay.recent_days)
                    if tv_status_obj:
                        status_key = tv_status_obj.status
                        status_label = tv_status_obj.label
                    # 2026-10-23: the "network" badge needs TMDb's own
                    # `networks` field -- operator-reported "I only ever see
                    # TV status, nothing else" when testing it. A small,
                    # cached, read-only call (TmdbClient's own TTL cache),
                    # same tradeoff "status" above already makes -- preview
                    # couldn't show this at all otherwise.
                    if _tmdb_id:
                        try:
                            details = tmdb.tv_details(_tmdb_id, language=cfg.language)
                            networks = details.get("networks") or []
                            if networks:
                                network_name = networks[0].get("name")
                        except Exception:
                            pass
            except Exception:
                streams = []
    finally:
        jf.close()
        try:
            tmdb.close()
        except Exception:
            pass

    posters = [c for c in by_kind.get("poster", []) if getattr(c, "ref", None)]
    if not posters:
        return None, {}, set(), None, {}, ""
    data, _ctype = proxy_mod.fetch("poster", posters[0].ref,
                                   allowed_roots=ctx.proxy_roots(cfg))
    from finmeta.engine.badges import overlay_values, format_rating_value
    from finmeta.engine.plan_builder import compute_ratings
    from finmeta.engine.format_rules import detect_all as _detect_all
    from pathlib import Path as _Path

    rules = profile_rules if profile_rules is not None else (getattr(cfg.overlays, "format_rules", None) or None)
    # K10: rating values come from the SAME field_sources chain as apply
    # (compute_ratings); no TMDb details are fetched here -- a missing value
    # means NO badge by default.
    computed = compute_ratings(item, None, sources, cfg)
    values = dict(overlay_values(streams, None, categories_rules=rules, status_label=status_label,
                                 network=network_name))
    for key, v in computed["values"].items():
        if v is not None:
            values[f"rating_{key}"] = format_rating_value(key, v)

    # Same raw per-category winning-rule dicts plan_builder._overlay_poster
    # feeds into resolve_profile_pack_image, so a per-profile Kometa-import
    # pack's resolution/hdr/audio badges preview exactly as a real apply
    # would render them (SPEC's "preview == apply" invariant, image_pack_
    # resolve.py's docstring). No TMDb vote_average here (preview never
    # fetches TMDb details -- see note above), so a pack category using the
    # rating_tier matcher simply won't preview; matches existing behavior.
    _det = _detect_all(streams or [], rules)
    k9m = {"resolution": _det.get("resolution"), "hdr": _det.get("dynamic_range"),
           "audio": _det.get("audio")}
    _item_name = getattr(item, "name", None)
    filename = _Path(_item_name).stem if _item_name else ""

    sample: set[str] = set()
    # Preview-only placeholders: ONLY when the editor's explicit "Sample data"
    # toggle is on. For elements the item lacks, synthesize a plausible string
    # so the user can SEE what that badge looks like -- and flag it synthetic
    # in samples[]. Real-data behavior elsewhere is untouched: plan/apply keep
    # "missing data -> no badge". Never overwrite non-empty real values.
    if sample_data:
        _PREVIEW_PLACEHOLDERS = {
            "rating_imdb": "8.5", "rating_rt_critic": "92", "rating_metacritic": "78",
            "rating_tmdb": "7.9", "rating_letterboxd": "4.2", "rating_trakt": "8.1",
            "rating_rt_audience": "88", "resolution": "4K", "hdr": "HDR10",
            "audio": "Atmos",
        }
        for key, ph in _PREVIEW_PLACEHOLDERS.items():
            if not (values.get(key) or "").strip():
                values[key] = ph
                sample.add(key)
    return (data or b""), values, sample, status_key, k9m, filename


def _preview_streams(ctx: AppContext, item_id: str) -> list:
    """Raw MediaStreams for one item (same targeting as the preview source path)."""
    from finmeta.api.routes_candidates import _media_type
    jf = ctx.make_jellyfin(ctx.config)
    try:
        item = jf.get_item(item_id)
        target_id = item.id
        if _media_type(item) == "tvshow":
            first_ep = jf.get_items_page(
                parent_id=item.id, recursive=True,
                include_item_types=["Episode"],
                sort_by=["ParentIndexNumber", "IndexNumber"],
                sort_order="Ascending", limit=1,
            ).items
            if first_ep:
                target_id = first_ep[0].id
        try:
            return list(jf.get_item_media_streams_raw(target_id))
        except Exception:
            return []
    except Exception:
        return []
    finally:
        jf.close()


def _preview_rating_sources(ctx: AppContext, item_id: str) -> Dict[str, Optional[str]]:
    """Re-run the K10 rating chain for one item (cheap: cached providers)."""
    from finmeta.engine.plan_builder import compute_ratings
    jf = ctx.make_jellyfin(ctx.config)
    try:
        item = jf.get_item(item_id)
    except Exception:
        return {}
    finally:
        jf.close()
    sources = ctx.make_sources(ctx.config)
    try:
        return compute_ratings(item, None, sources, ctx.config)["sources"]
    except Exception:
        return {}


@router.post("/overlays/preview")
def overlays_preview(body: OverlayPreviewIn, ctx: AppContext = Depends(get_ctx)) -> dict:
    """Render up to MAX_PREVIEW_ITEMS preview PNGs with the posted style; writes nothing.

    The posted ``style`` document (partial OK — missing keys default) drives a
    server-side compositor pass on each chosen item\u2019s best poster candidate.
    Output images are returned inline as data URLs so no files touch disk and
    previews never leak into apply-run backups.
    """
    from finmeta.engine.overlay_render import render_overlay_png
    from finmeta.engine.style_presets import OverlayStyleDoc

    # S9d2-fix: optional profile — build the preview with that profile's style +
    # format_rules (plan_builder helpers), leaving user_styles/global alone.
    prof = None
    if body.profile:
        prof = (ctx.config.overlays.profiles or {}).get(body.profile)
        if prof is None:
            raise HTTPException(status_code=400, detail=f"unknown overlay profile: {body.profile}")
    try:
        doc_json: dict = {}
        # Merge current config's overlays.style over defaults first, then the
        # posted doc wins field-by-field, matching "posted style merged on top".
        _base = ctx.config.overlays.style.model_dump() if hasattr(ctx.config.overlays, "style") else {}
        for k in ("position", "margin", "font_size", "bg", "fg", "radius"):
            v = _base.get(k)
            if v is not None:
                target_k = "base_position" if k == "position" else k
                doc_json.setdefault(target_k, v)
        if prof and "style" in (prof or {}):
            from finmeta.engine.plan_builder import _profile_style
            class _LibCfg:  # minimal stand-in for a library config carrying the profile name
                overlay_profile = body.profile
            try:
                merged = dict(_profile_style(_LibCfg(), ctx.config).model_dump())
                for k, v in merged.items():
                    if v is not None:
                        target_k = "base_position" if k == "position" else k
                        doc_json[target_k] = v
            except Exception:
                pass
        doc_json.update(body.style or {})
        OverlayStyleDoc(**doc_json)  # validate early → clean 422
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"invalid overlay style: {exc}")

    # 2026-10-23: one Sonarr fetch for the whole request (~4s), not one per
    # preview item -- up to MAX_PREVIEW_ITEMS calls into _overlay_preview_source
    # were each rebuilding it from scratch. Same "one lookup, not per show"
    # rule plan_builder.build_plan already follows for a real apply.
    from finmeta.engine.plan_builder import build_sonarr_index
    sonarr_index = build_sonarr_index(ctx.config, ctx.secrets)

    results = []
    for item_id in body.item_ids[:MAX_PREVIEW_ITEMS]:
        entry: dict = {"item_id": item_id}
        try:
            prof_rules = (prof or {}).get("format_rules") if prof else None
            poster_bytes, values, sample, status_key, k9m, filename = _overlay_preview_source(
                ctx, item_id, sample_data=body.sample_data, profile_rules=prof_rules,
                sonarr_index=sonarr_index)
            if not poster_bytes:
                raise ValueError("no resolvable poster for this item")
            # S9d4c: the UI's "Base image" / "With overlay" comparison must be
            # the SAME candidate -- return the exact pre-compositing bytes
            # this preview is about to render on top of, instead of the UI
            # separately resolving its own "base" via the plan (a DIFFERENT
            # candidate-selection path that can legitimately pick a different
            # image, confusing operators into thinking the overlay "isn't
            # applying" when really two different posters were being shown).
            try:
                import io as _io
                from PIL import Image as _PILImage
                _fmt = (_PILImage.open(_io.BytesIO(poster_bytes)).format or "JPEG").lower()
                _mime = {"jpeg": "image/jpeg", "jpg": "image/jpeg", "png": "image/png",
                        "webp": "image/webp"}.get(_fmt, "image/jpeg")
            except Exception:
                _mime = "image/jpeg"
            entry["base_image"] = f"data:{_mime};base64,{base64.b64encode(poster_bytes).decode('ascii')}"
            # Logos live in <local.path>/logos — same tree as posters/backdrops; fall back
            # to <config dir>/assets/logos so a bare /config layout keeps working too.
            _local_src = getattr(getattr(ctx.config, "sources", None), "local", None)
            _local_root = _local_src.path if _local_src is not None else None
            _packs = list(getattr(getattr(ctx.config, "overlays", None), "logo_packs", []) or [])
            png = render_overlay_png(
                poster_bytes, doc_json, values=values,
                config_dir=ctx.config_dir, assets_root=_local_root, logo_pack=_packs,
                status_key=status_key, packs_profile=prof, k9m=k9m, filename=filename)
            b64 = base64.b64encode(png).decode("ascii")
            entry["image"] = f"data:image/png;base64,{b64}"
            # Which overlay cells are synthetic (no real data) so the UI can tag them.
            if sample:
                entry["samples"] = sorted(sample)
            # K10: which source supplied each rating value, for the UI/report.
            src_map = {k: v for k, v in _preview_rating_sources(ctx, item_id).items() if v}
            if src_map:
                entry["rating_sources"] = src_map
            # K9: which format rule matched per category for THIS item (from the
            # same streams/rules the badges were derived from), else "no match".
            from finmeta.engine.format_rules import detect_all, streams_to_dicts
            _rules = prof_rules if prof_rules is not None else (getattr(ctx.config.overlays, "format_rules", None) or None)
            det = detect_all(streams_to_dicts(_preview_streams(ctx, item_id)), _rules)
            matched: dict = {}
            for cat in ("resolution", "dynamic_range", "audio"):
                d = det.get(cat)
                matched[cat] = {"key": d["key"], "label": d["label"]} if d else None
            entry["format_rule_matches"] = matched
        except Exception as exc:
            entry["error"] = str(exc)[:300]
        results.append(entry)
    return {"previews": results}


class OverlayStylePut(BaseModel):
    """Body of ``PUT /api/overlays/{name}`` — a full style document."""

    name: str = Field(min_length=1, max_length=80)
    style: dict


def _clone_before_edit(style_doc_json: dict) -> dict:
    """Return a fresh copy of the posted style so callers can't alias into it."""
    return json.loads(json.dumps(dict(style_doc_json or {})))


@router.get("/overlays")
def overlays_list(ctx: AppContext = Depends(get_ctx)) -> dict:
    """List built-in presets (with their full style documents) plus user-saved styles."""
    from finmeta.engine.style_presets import all_presets
    # Full docs included so the UI can clone-before-edit without a second round trip.
    presets = [dict(p) for p in all_presets()]
    user_styles = [
        {"name": k, "style": v} for k, v in ctx.config.overlays.user_styles.items()
    ]
    base_style = ctx.config.overlays.style.model_dump(mode="json") if hasattr(
        ctx.config.overlays, "style") else None
    return {
        "presets": presets,
        "user_styles": user_styles,
        "base_style": base_style,
    }


@router.put("/overlays/{name}")
def overlays_save(name: str, body: OverlayStylePut, ctx: AppContext = Depends(get_ctx)) -> dict:
    """Save (or overwrite) one named user-edited overlay style.

    The posted document is validated as an :class:`OverlayStyleDoc` first; on
    success a fresh clone lands under ``cfg.overlays.user_styles[name]`` and the
    config file is rewritten + hot-reloaded via the standard put_config path so
    Advanced-tab YAML stays in sync with what the editor just saved. Presets are
    never touched here — they live only in code and are always re-cloned from
    scratch by the UI before editing ("clone-before-edit").
    """
    import yaml as _yaml
    from finmeta.engine.style_presets import OverlayStyleDoc

    try:
        doc_json = _clone_before_edit(body.style)
        OverlayStyleDoc(**doc_json)  # validate → clean error if malformed
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"invalid overlay style: {exc}")

    cfg_text = _yaml.safe_load(ctx.config_text()) or {}
    ov = cfg_text.setdefault("overlays", {})
    us = ov.setdefault("user_styles", {})
    existing_key = next((k for k in list(us.keys()) if k == name), None)
    if existing_key is not None:
        del us[existing_key]
    us[name] = doc_json
    new_yaml_text = _yaml.safe_dump(cfg_text, sort_keys=False, allow_unicode=True)
    try:
        ctx.put_config(new_yaml_text)
    except ConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "name": name, "gen": ctx.gen}


@router.get("/help")
def help_(ctx: AppContext = Depends(get_ctx)) -> dict:
    return {"help": build_help(ctx.config)}


__all__ = ["router"]
