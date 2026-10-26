"""Build a WritePlan from config + Jellyfin + sources (SPEC §10).

This is the core engine: it ties together matching, resolution, NFO
generation, and the diff to produce a complete WritePlan.
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

import httpx

from finmeta.config.models import Config, MovieLibrary, TvLibrary
from finmeta.engine.diff import annotate_status, diff_plan, diff_to_text
from finmeta.engine.matching import MatchResult, match_item
from finmeta.engine.nfo import (
    DEFAULT_MOVIE_FIELDS, DEFAULT_TV_FIELDS,
    episode_nfo, movie_nfo, nfo_sha256, season_nfo, tvshow_nfo,
)
from finmeta.engine.plan import (
    ApiAction, FileWrite, ItemPlan, UnmatchedItem, WritePlan,
)
from finmeta.engine.badges import overlay_values, format_rating_value
from finmeta.engine.format_rules import OVERLAY_ELEMENT_TO_CATEGORY, detect_all, streams_to_dicts
from finmeta.engine.overlay import overlay_file_write
from finmeta.engine.style_presets import ELEMENT_KEYS
from finmeta.engine.resolution import collect_candidates
from finmeta.engine.artwork_selection import (
    effective_selection, select_candidate,
)
from finmeta.engine.runs import DEFAULT_CONFIG_DIR
from finmeta.engine.jellyfin_originals import get_or_fetch_original, original_cache_path
from finmeta.engine.tv_status import TvStatus
from finmeta.engine.collection_presets import (
    KNOWN_PRESETS, SEASONAL_CALENDAR, is_holiday_active_now,
)
from finmeta.jellyfin_client.client import JellyfinClient
from finmeta.jellyfin_client.mappings import PathMapper
from finmeta.jellyfin_client.types import Item, MediaStream
from finmeta.sources.candidate import Candidate
from finmeta.sources.tmdb import TmdbClient

logger = logging.getLogger("finmeta.engine")

def _resolved_format_cats(format_rules: Optional[Dict[str, list]]) -> Dict[str, list]:
    """K9 category rules: profile/global overrides merged onto the built-in defaults."""
    from finmeta.engine.format_rules import default_format_rules
    cats = default_format_rules()
    if isinstance(format_rules, dict):
        for k in list(cats.keys()):
            v = format_rules.get(k)
            if v:
                cats[k] = [dict(r) for r in v]
    return cats


def _item_format_counts(
    streams: List[MediaStream], format_rules: Optional[Dict[str, list]],
    det: Optional[Dict[str, Any]] = None,
) -> Dict[str, str]:
    """Per-category winning rule key for one item's media streams (K9).

    Returns e.g. {"dynamic_range": "dovi_hdr10", "resolution": "res_4k"}; a
    category with no enabled-rule match maps to the empty string (no badge). Compact
    enough to store on the poster FileWrite and aggregate into the plan report.
    ``det`` lets a caller that already ran ``detect_all`` (e.g. to build the
    overlay renderer's ``k9m``) pass it straight in instead of recomputing it.
    """
    if det is None:
        det = detect_all(streams_to_dicts(streams), _resolved_format_cats(format_rules))
    out: Dict[str, str] = {}
    for cat in ("dynamic_range", "resolution", "audio"):
        d = det.get(cat)
        out[cat] = str(d["key"]) if d else ""
    return out


def _aggregate_format_counts(files: List[FileWrite]) -> Dict[str, str]:
    """Merge per-poster format_counts into one item-level {category: key}.

    First non-empty value per category wins across an item's poster files.
    Empty-string values mean no enabled rule matched that category.
    """
    out: Dict[str, str] = {}
    for fw in files:
        fc = (fw.meta or {}).get("format_counts") or {}
        if not isinstance(fc, dict):
            continue
        for cat, val in fc.items():
            if cat not in out:
                out[cat] = val
    return out

def _file_sha256(path: Path) -> str:
    """SHA-256 of a local file."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _http_sha256(url: str, timeout: float = 30.0) -> str:
    """SHA-256 of a remote file (downloads it)."""
    resp = httpx.get(url, timeout=timeout, follow_redirects=True)
    resp.raise_for_status()
    return hashlib.sha256(resp.content).hexdigest()


def _content_sha256(source_ref: str) -> str:
    """Compute the SHA-256 of the content a FileWrite will produce."""
    if source_ref == "generated":
        # Should not be called for generated content; caller passes the hash directly
        raise ValueError("generated content must pass hash explicitly")
    if source_ref.startswith(("http://", "https://")):
        return _http_sha256(source_ref)
    p = Path(source_ref)
    if p.is_file():
        return _file_sha256(p)
    return hashlib.sha256(b"").hexdigest()


def _candidate_bytes(source_ref: str, timeout: float = 30.0) -> bytes:
    """Fetch the raw bytes of a candidate (local file or remote URL)."""
    if source_ref.startswith(("http://", "https://")):
        resp = httpx.get(source_ref, timeout=timeout, follow_redirects=True)
        resp.raise_for_status()
        return resp.content
    p = Path(source_ref)
    if p.is_file():
        return p.read_bytes()
    return b""


def _overlay_poster(
    item: Item, cand: Candidate, overlay_names: List[str], target: Path,
    jf: JellyfinClient, details: Optional[Dict[str, Any]],
    media_type: str, config_dir: Optional[Path], style_doc: dict,
    format_rules: Optional[Dict[str, list]] = None,
    streams: Optional[List[Dict[str, Any]]] = None,
    packs_profile: Optional[dict] = None,
    logo_packs: Optional[List[str]] = None,
    assets_root: Optional[str] = None,
    write_method: str = "sidecar",
    status_label: Optional[str] = None,
    status_key: Optional[str] = None,
    trending_rank: Optional[int] = None,
    is_override: bool = False,
    rating_values: Optional[Dict[str, str]] = None,
) -> Optional[FileWrite]:
    """Build the overlaid-poster FileWrite for one item, or None on failure.

    The poster is always rendered from the original (backup if a prior run
    saved one, else the candidate) so re-runs never stack badges (O7). In
    ``"jellyfin_api"`` write mode (S9d5) that original instead comes from
    ``engine.jellyfin_originals.get_or_fetch_original`` and the FileWrite's
    ``target_path`` is a synthetic ``jellyfin-api://<item_id>/poster``
    identifier -- see ``overlay_file_write``'s docstring.

    Rendering goes through ``engine.overlay_render.render_overlay_jpeg`` --
    the SAME per-element layout engine the editor preview uses, driven by
    ``style_doc`` (see ``_apply_style_doc``) -- instead of this module
    resolving badges/pack images by hand into the older, coarser
    ``engine.overlay`` renderer. A real apply's poster now gets the exact
    per-element offset/radius/colour the preview showed, not just its
    text/logo content (previously the apply pipeline only understood one
    shared margin/radius/position for the whole badge stack).

    ``rating_values`` (2026-10-25, closes the "Badge-visibility gap"): the
    caller's already-computed K10 chain (``compute_ratings``), pre-formatted
    as ``{"rating_imdb": "8.3", "rating_rt_critic": "92%", ...}`` -- the same
    convention ``api.routes_settings``'s preview endpoint has always used.
    Merged into ``values`` below so the imdb/rt_critic/rt_audience/
    metacritic/tmdb/letterboxd/trakt elements -- previously rendered only in
    this tab's own live preview, never in a real apply -- get real data here
    too. ``None`` (the default) renders exactly as before: no rating badges.
    """
    try:
        poster_bytes = _candidate_bytes(cand.ref)
    except Exception as exc:
        logger.warning("overlay poster read failed for %s: %s", item.name, exc)
        return None
    if not poster_bytes:
        logger.warning("empty poster bytes for overlay on %s; skipping overlay", item.name)
        return None
    try:
        # SAME raw-stream source (JellyfinClient.get_item_media_streams_raw) as the preview
        # path -> preview badges always equal plan/apply badges. Raw dicts keep every field
        # K9 detection needs (DisplayTitle, IsForced, ChannelLayout, ColorTransfer).
        if streams is None:
            target_id = item.id
            if media_type == "tvshow":
                eps = [e for e in jf.get_items(parent_id=item.id, recursive=True)
                       if e.type == "Episode"]
                if eps:
                    eps.sort(key=lambda e: ((e.parent_index_number or 0),
                                            (e.index_number or 0)))
                    target_id = eps[0].id
            streams = list(jf.get_item_media_streams_raw(target_id))
    except Exception as exc:
        logger.warning("media-stream fetch failed for %s: %s", item.name, exc)
        streams = []
    vote = (details or {}).get("vote_average")
    networks = (details or {}).get("networks") or [] if media_type == "tvshow" else []
    network_name = networks[0].get("name") if networks else None
    values = dict(overlay_values(streams, vote, categories_rules=format_rules, status_label=status_label,
                            trending_rank=trending_rank, network=network_name))
    if rating_values:
        values.update(rating_values)
    det = detect_all(streams_to_dicts(streams), _resolved_format_cats(format_rules))
    k9m = {"resolution": det.get("resolution"), "hdr": det.get("dynamic_range"), "audio": det.get("audio")}
    item_fc = _item_format_counts(streams, format_rules, det=det)
    fw_target: Any = target
    original_override: Optional[bytes] = None
    original_ref_override: Optional[str] = None
    if write_method == "jellyfin_api":
        fw_target = f"jellyfin-api://{item.id}/poster"
        if config_dir is not None and not is_override:
            orig = get_or_fetch_original(jf, item.id, "poster", config_dir)
            if orig is not None:
                original_override = orig
                original_ref_override = str(original_cache_path(config_dir, item.id, "poster"))
    from finmeta.engine.overlay_render import render_overlay_jpeg
    logo_packs = [p for p in (logo_packs or []) if p and p != "none"]
    _item_name = getattr(item, "name", None)
    filename = Path(_item_name).stem if _item_name else ""

    def _render(poster: bytes) -> bytes:
        return render_overlay_jpeg(
            poster, style_doc, values=values, config_dir=config_dir,
            assets_root=assets_root, logo_pack=logo_packs, status_key=status_key,
            packs_profile=packs_profile, k9m=k9m, filename=filename,
            rating_value=float(vote) if vote is not None else None,
        )

    fw = overlay_file_write(
        target_path=fw_target, candidate_bytes=poster_bytes, candidate_ref=cand.ref,
        render_fn=_render, config_dir=config_dir,
        original_override=original_override, original_ref_override=original_ref_override,
        write_method=write_method, is_override=is_override,
    )
    if item_fc:
        fw.meta["format_counts"] = item_fc
    return fw


def _movie_dir(item: Item, mapper: PathMapper) -> Optional[Path]:
    """Local directory containing the movie video file."""
    local = mapper.translate(item.path)
    if not local:
        return None
    return Path(local).parent


def _show_dir(item: Item, mapper: PathMapper) -> Optional[Path]:
    """Local directory for a TV series (the show folder)."""
    local = mapper.translate(item.path)
    if not local:
        return None
    p = Path(local)
    # A real existing directory IS the show folder -- trust it outright.
    # Must come before the suffix heuristic below: a show folder NAME can
    # itself contain a '.' (e.g. "A.D. Police Files", "S.H.I.E.L.D.") and
    # Path.suffix has no concept of "this is a directory, not a file" --
    # it just looks for the last '.' in the final path component. Checking
    # suffix first on a real directory like that wrongly climbed two parent
    # levels and wrote posters/NFOs into the TOP of the whole media share
    # instead of the show's own folder (reported live 2026-10-25).
    if p.is_dir():
        return p
    # If path points to a file (first episode), use its parent's parent
    if p.is_file() or p.suffix:
        # /media/Shows/Show/Season 01/ep.mkv -> /media/Shows/Show
        return p.parent.parent
    return p


def _season_dir(item: Item, mapper: PathMapper) -> Optional[Path]:
    """Local directory for a season."""
    local = mapper.translate(item.path)
    if not local:
        return None
    return Path(local)


def _episode_stem(item: Item, mapper: PathMapper) -> Optional[Path]:
    """Local path stem for an episode file (without extension)."""
    local = mapper.translate(item.path)
    if not local:
        return None
    p = Path(local)
    return p.with_suffix("")


# -- ratings ----------------------------------------------------------------
# Canonical scale for each rating field (see _compute_ratings docstring).
_RATING_SCALE: Dict[str, int] = {
    "imdb": 10, "tmdb": 10, "trakt": 10,
    "rt_critic": 100, "rt_audience": 100, "metacritic": 100,
    "letterboxd": 5,
}


def _to_scale(value: Any, key: str) -> Optional[float]:
    """Normalise a provider rating value to the field's canonical scale.

    Handles common string forms (``"94%"`` -> 94, ``"82/100"`` -> 82). Values
    that are already on the canonical scale pass through; a 0-5 value for a
    0-100 field is scaled up (Letterboxd-style), and a 0-10 value for a 0-100
    field is not rescaled (IMDb/TMDb style) — providers are expected to report
    on their native scale, so we only rescale the unambiguous small-range case.
    """
    if value is None:
        return None
    # Parse string forms first.
    s = str(value).strip() if not isinstance(value, (int, float)) else None
    if s is not None:
        pct = s.endswith("%")
        if pct:
            s = s[:-1].strip()
        if "/" in s:
            s = s.split("/", 1)[0].strip()
        try:
            f = float(s)
        except (ValueError, TypeError):
            return None
    else:
        try:
            f = float(value)
        except (ValueError, TypeError):
            return None

    scale = _RATING_SCALE.get(key, 10)
    if scale == 100 and f <= 5.0:
        # A 0-5 value reported against a 0-100 field (e.g. Letterboxd).
        f = f * 20.0
    return round(f, 1)



def _compute_ratings(
    item: "Item",
    details: Optional[Dict[str, Any]],
    sources: Dict[str, object],
    cfg: Config,
) -> Dict[str, Optional[float]]:
    """Compute the ratings dict for an item using field_sources precedence.

    Every rating field has ONE canonical scale regardless of provider:

      * ``imdb``, ``tmdb``, ``trakt``            -> 0-10
      * ``rt_critic``, ``rt_audience``, ``metacritic`` -> 0-100
      * ``letterboxd``                           -> 0-5

    Providers are normalised to that scale before precedence is applied. For
    each field in ``cfg.sources.field_sources`` the provider list is walked in
    order; disabled/unconfigured providers (absent from ``sources``) and
    providers that cannot supply the field are skipped, and the first non-None
    value wins. The TMDb rating (``tmdb``) comes from the details when present.

    .. deprecated:: Use :func:`compute_ratings` which also records the source.
    """
    return compute_ratings(item, details, sources, cfg)["values"]


def compute_ratings(
    item: "Item",
    details: Optional[Dict[str, Any]],
    sources: Dict[str, object],
    cfg: Config,
) -> Dict[str, Dict[str, Optional[float]]]:
    """Compute per-field rating values AND their supplying source (K10).

    Same S9b2 field_sources chain as before; the result records which enabled
    source supplied each value so the UI/report can show it::

        {"values": {"imdb": 8.5, ...}, "sources": {"imdb": "mdblist", ...}}

    A field with no value from any enabled source maps to ``None`` in both
    dicts (missing -> NO badge in apply; preview placeholders only when the
    editor's explicit "Sample data" toggle is on).
    """
    provider_ids = getattr(item, "provider_ids", None) or {}
    imdb_id = provider_ids.get("Imdb")

    # 2026-10-23: memoize per (provider, this call) -- field_sources lists
    # most providers first for several fields (e.g. mdblist for 6 of 7), and
    # one get_ratings() call already returns every field a provider has. The
    # provider's OWN cache (SqliteCache) only helps for a response worth
    # caching (a real 200) -- a title with no match there deliberately
    # ISN'T cached (so a transient failure can retry), which without this
    # memo meant every field re-tried the live call, each serialized behind
    # the client's own 1s rate-limit throttle. Found while the operator
    # reported overlay-preview rendering was "extremely slow": ~6s/item for
    # exactly this reason (confirmed: 6+ redundant mdblist calls, no cache
    # hit, ~1s throttle each).
    _call_memo: Dict[str, Optional[Dict[str, Any]]] = {}

    def _call(name: str) -> Optional[Dict[str, Any]]:
        if name in _call_memo:
            return _call_memo[name]
        client = sources.get(name)
        if client is None or not imdb_id:
            _call_memo[name] = None
            return None
        try:
            vals = client.get_ratings(imdb_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s ratings failed for %s: %s", name, getattr(item, "name", item), exc)
            vals = None
        result = vals if isinstance(vals, dict) else None
        _call_memo[name] = result
        return result

    def _fetch(name: str, key: str) -> Optional[float]:
        """Fetch ``key`` from provider ``name`` and normalise to the field scale."""
        vals = _call(name)
        if not vals:
            return None
        raw = vals.get(key)
        if raw is None:
            return None
        return _to_scale(raw, key)

    # jellyfin: from the item's own ratings (already on canonical scale).
    def _jellyfin(key: str) -> Optional[float]:
        if key == "imdb":
            return getattr(item, "community_rating", None)
        if key == "rt_critic":
            return getattr(item, "critic_rating", None)
        return None

    # tmdb: from the fetched details (vote_average is already 0-10).
    def _tmdb(key: str) -> Optional[float]:
        if key != "tmdb" or details is None:
            return None
        v = details.get("vote_average")
        if v is None:
            return None
        try:
            f = float(v)
        except (ValueError, TypeError):
            return None
        return round(f, 1) if f > 0 else None

    def _value_for(prov_name: str, key: str) -> Optional[float]:
        if prov_name == "jellyfin":
            return _jellyfin(key)
        if prov_name == "tmdb":
            return _tmdb(key)
        return _fetch(prov_name, key)

    values: Dict[str, Optional[float]] = {}
    sources_out: Dict[str, Optional[str]] = {}
    for field_name, provider_list in cfg.sources.field_sources.items():
        key = field_name.replace("rating_", "")
        value: Optional[float] = None
        source: Optional[str] = None
        for prov_name in provider_list:
            v = _value_for(prov_name, key)
            if v is not None:
                value = v
                source = prov_name
                break
        values[key] = value
        sources_out[key] = source

    return {"values": values, "sources": sources_out}


# -- per-item plan builders -------------------------------------------------

def _build_movie_plan(
    item: Item, match: MatchResult, cfg: Config, lib: MovieLibrary,
    mapper: PathMapper, tmdb: TmdbClient, jf: JellyfinClient,
    sources: Dict[str, object], language: Optional[str],
    lang_fallback: List[Optional[str]], config_dir: Optional[Path] = None,
    streams: Optional[List[Dict[str, Any]]] = None,
    warnings: Optional[List[Dict[str, object]]] = None,
    trending_index: Optional[Dict[int, int]] = None,
) -> ItemPlan:
    """Build an ItemPlan for a matched movie."""
    if match.tmdb_id is None:
        return ItemPlan(
            item_id=item.id, item_name=item.name,
            media_type="movie", tmdb_id=None, matched_by=None,
        )

    files: List[FileWrite] = []
    base = _movie_dir(item, mapper)
    if base is None:
        return ItemPlan(
            item_id=item.id, item_name=item.name,
            media_type="movie", tmdb_id=match.tmdb_id,
            matched_by=match.matched_by,
        )

    fields = lib.metadata.fields or DEFAULT_MOVIE_FIELDS

    # TMDb details (needed for the NFO and for the "rating" overlay badge).
    details: Optional[Dict[str, Any]] = None
    overlay_names = _profile_overlay_names(lib, cfg)
    if lib.metadata.enabled or "rating" in overlay_names:
        try:
            details = tmdb.movie_details(match.tmdb_id, language=language)
        except Exception as exc:
            logger.warning("TMDb details failed for %s: %s", item.name, exc)

    # K10 rating chain, computed here (not just after the artwork loop below)
    # so the per-provider values can feed the overlay renderer's rating
    # badges (imdb/rt_critic/rt_audience/metacritic/tmdb/letterboxd/trakt) --
    # see rating_values below and _overlay_poster's new parameter.
    computed = compute_ratings(item, details, sources, cfg)
    rating_values = {
        f"rating_{k}": format_rating_value(k, v)
        for k, v in computed["values"].items() if v is not None
    }

    # NFO
    if lib.metadata.enabled and details is not None:
        try:
            nfo_bytes = movie_nfo(details, match.tmdb_id, fields)
            files.append(FileWrite(
                target_path=str(base / "movie.nfo"),
                kind="metadata",
                source_ref="generated",
                content_sha256=nfo_sha256(nfo_bytes),
            ))
        except Exception as exc:
            logger.warning("NFO generation failed for %s: %s", item.name, exc)

    # Artwork
    artwork_kinds: List[str] = []
    art = lib.artwork
    for k in ("poster", "backdrop", "logo", "landscape"):
        if getattr(art, k, False):
            artwork_kinds.append(k)
    if lib.theme_song:
        artwork_kinds.append("theme_song")

    # S9d4a: gather all ranked candidates, then pick per the selection preset.
    sel = _effective_selection(cfg, lib)
    write_method = _effective_write_method(cfg, lib)
    # S9g2/K7b: top-10 rank on a trending list with rank_overlay on, if any.
    trending_rank = (trending_index or {}).get(match.tmdb_id)
    # S9d5: locked image kinds skip source resolution entirely (no new
    # TMDb/fanart queries) -- the locked ref is used as-is.
    locked = _locked_candidates(item.id, artwork_kinds, config_dir)
    kinds_to_resolve = [k for k in artwork_kinds if k not in locked]
    resolved = _select_artwork(
        sources, cfg.sources.precedence, "movie", match.tmdb_id,
        kinds_to_resolve, language=language, lang_fallback=lang_fallback,
        warnings=warnings, item_id=item.id, sel=sel, config_dir=config_dir,
    )
    resolved.update(locked)
    overridden_kinds = _apply_candidate_overrides(resolved, item.id, artwork_kinds, config_dir)
    filename_map = {
        "poster": "poster.jpg", "backdrop": "backdrop.jpg",
        "logo": "logo.png", "landscape": "landscape.jpg",
        "theme_song": "theme.mp3",
    }
    for kind, cand in resolved.items():
        fname = filename_map.get(kind, f"{kind}.jpg")
        if kind == "poster" and overlay_names and not _overlay_skipped(item.id, config_dir):
            fw = _overlay_poster(
                item, cand, list(overlay_names), base / fname, jf,
                details, "movie", config_dir, _apply_style_doc(lib, cfg, overlay_names),
                _profile_format_rules(lib, cfg), streams=streams,
                packs_profile=_profile_for(lib, cfg),
                logo_packs=list(getattr(cfg.overlays, "logo_packs", None) or []),
                assets_root=getattr(getattr(cfg.sources, "local", None), "path", None),
                write_method=write_method,
                trending_rank=trending_rank,
                is_override=kind in overridden_kinds,
                rating_values=rating_values,
            )
            if fw is not None:
                if kind in overridden_kinds:
                    fw.meta["overridden"] = True
                files.append(fw)
            continue
        try:
            sha = _content_sha256(cand.ref)
        except Exception as exc:
            logger.warning("content hash failed for %s (%s): %s", item.name, kind, exc)
            continue
        # S9d5: metadata/theme_song are always sidecar (no Jellyfin API for
        # either) -- only the four image kinds can use jellyfin_api.
        use_api = write_method == "jellyfin_api" and kind in (
            "poster", "backdrop", "logo", "landscape")
        meta: Dict[str, Any] = {"overridden": True} if kind in overridden_kinds else {}
        if kind in ("poster", "backdrop", "logo", "landscape"):
            meta["lock_ref"] = cand.ref
            meta["lock_sha256"] = sha
        files.append(FileWrite(
            target_path=(f"jellyfin-api://{item.id}/{kind}" if use_api
                        else str(base / fname)),
            kind=kind,  # type: ignore[arg-type]
            source_ref=cand.ref,
            content_sha256=sha,
            meta=meta,
            write_method="jellyfin_api" if use_api else "sidecar",
        ))

    format_counts = _aggregate_format_counts(files)
    return ItemPlan(
        item_id=item.id, item_name=item.name,
        media_type="movie", tmdb_id=match.tmdb_id,
        matched_by=match.matched_by, files=files, ratings=computed["values"],
        rating_sources=computed["sources"],
        format_counts=format_counts,
    )


def _build_tvshow_plan(
    item: Item, match: MatchResult, cfg: Config, lib: TvLibrary,
    mapper: PathMapper, tmdb: TmdbClient, jf: JellyfinClient,
    sources: Dict[str, object], language: Optional[str],
    lang_fallback: List[Optional[str]], config_dir: Optional[Path] = None,
    streams: Optional[List[Dict[str, Any]]] = None,
    warnings: Optional[List[Dict[str, object]]] = None,
    sonarr_index: Optional[Dict[str, Any]] = None,
    trending_index: Optional[Dict[int, int]] = None,
) -> ItemPlan:
    """Build an ItemPlan for a matched TV series (show-level only)."""
    if match.tmdb_id is None:
        return ItemPlan(
            item_id=item.id, item_name=item.name,
            media_type="tvshow", tmdb_id=None, matched_by=None,
        )

    files: List[FileWrite] = []
    base = _show_dir(item, mapper)
    if base is None:
        return ItemPlan(
            item_id=item.id, item_name=item.name,
            media_type="tvshow", tmdb_id=match.tmdb_id,
            matched_by=match.matched_by,
        )

    fields = lib.metadata.fields or DEFAULT_TV_FIELDS

    # TMDb details (needed for the NFO, the "rating" overlay badge, and the
    # "network" badge -- TMDb's own `networks` field, TV shows only).
    details: Optional[Dict[str, Any]] = None
    overlay_names = _profile_overlay_names(lib, cfg)
    if lib.metadata.enabled or "rating" in overlay_names or "network" in overlay_names:
        try:
            details = tmdb.tv_details(match.tmdb_id, language=language)
        except Exception as exc:
            logger.warning("TMDb tv_details failed for %s: %s", item.name, exc)

    # K10 rating chain, computed here (see the movie builder's identical
    # comment) so imdb/rt_critic/rt_audience/metacritic/tmdb/letterboxd/trakt
    # overlay badges get real values on a real apply, not just in preview.
    computed = compute_ratings(item, details, sources, cfg)
    rating_values = {
        f"rating_{k}": format_rating_value(k, v)
        for k, v in computed["values"].items() if v is not None
    }

    # NFO
    if lib.metadata.enabled and details is not None:
        try:
            nfo_bytes = tvshow_nfo(details, match.tmdb_id, fields)
            files.append(FileWrite(
                target_path=str(base / "tvshow.nfo"),
                kind="metadata",
                source_ref="generated",
                content_sha256=nfo_sha256(nfo_bytes),
            ))
        except Exception as exc:
            logger.warning("tvshow NFO failed for %s: %s", item.name, exc)

    # S9g1/K7a: TV show status from Sonarr (used by the "status" overlay
    # element below and by status-based collections in _build_api_actions).
    tv_status_obj = _tv_status_for_item(
        item, sonarr_index, config_dir, recent_days=cfg.status_overlay.recent_days)

    # Artwork
    artwork_kinds: List[str] = []
    art = lib.artwork
    for k in ("poster", "backdrop", "logo"):
        if getattr(art, k, False):
            artwork_kinds.append(k)
    if getattr(art, "season_posters", False):
        artwork_kinds.append("season_poster")
    if lib.theme_song:
        artwork_kinds.append("theme_song")

    sel = _effective_selection(cfg, lib)
    write_method = _effective_write_method(cfg, lib)
    trending_rank = (trending_index or {}).get(match.tmdb_id)
    locked = _locked_candidates(item.id, artwork_kinds, config_dir)
    kinds_to_resolve = [k for k in artwork_kinds if k not in locked]
    resolved = _select_artwork(
        sources, cfg.sources.precedence, "tv", match.tmdb_id,
        kinds_to_resolve, language=language, lang_fallback=lang_fallback,
        warnings=warnings, item_id=item.id, sel=sel, config_dir=config_dir,
    )
    resolved.update(locked)
    overridden_kinds = _apply_candidate_overrides(resolved, item.id, artwork_kinds, config_dir)
    filename_map = {
        "poster": "poster.jpg", "backdrop": "backdrop.jpg",
        "logo": "logo.png", "theme_song": "theme.mp3",
    }
    for kind, cand in resolved.items():
        if kind == "season_poster":
            # Season posters go to the show folder as seasonNN-poster.jpg
            continue  # handled per-season
        fname = filename_map.get(kind, f"{kind}.jpg")
        if kind == "poster" and overlay_names and not _overlay_skipped(item.id, config_dir):
            fw = _overlay_poster(
                item, cand, list(overlay_names), base / fname, jf,
                details, "tvshow", config_dir, _apply_style_doc(lib, cfg, overlay_names),
                _profile_format_rules(lib, cfg), streams=streams,
                packs_profile=_profile_for(lib, cfg),
                logo_packs=list(getattr(cfg.overlays, "logo_packs", None) or []),
                assets_root=getattr(getattr(cfg.sources, "local", None), "path", None),
                write_method=write_method,
                status_label=tv_status_obj.label if tv_status_obj else None,
                status_key=tv_status_obj.status if tv_status_obj else None,
                trending_rank=trending_rank,
                is_override=kind in overridden_kinds,
                rating_values=rating_values,
            )
            if fw is not None:
                if kind in overridden_kinds:
                    fw.meta["overridden"] = True
                files.append(fw)
            continue
        try:
            sha = _content_sha256(cand.ref)
        except Exception as exc:
            logger.warning("content hash failed for %s (%s): %s", item.name, kind, exc)
            continue
        use_api = write_method == "jellyfin_api" and kind in (
            "poster", "backdrop", "logo", "landscape")
        meta: Dict[str, Any] = {"overridden": True} if kind in overridden_kinds else {}
        if kind in ("poster", "backdrop", "logo", "landscape"):
            meta["lock_ref"] = cand.ref
            meta["lock_sha256"] = sha
        files.append(FileWrite(
            target_path=(f"jellyfin-api://{item.id}/{kind}" if use_api
                        else str(base / fname)),
            kind=kind,  # type: ignore[arg-type]
            source_ref=cand.ref,
            content_sha256=sha,
            meta=meta,
            write_method="jellyfin_api" if use_api else "sidecar",
        ))

    format_counts = _aggregate_format_counts(files)
    return ItemPlan(
        item_id=item.id, item_name=item.name,
        media_type="tvshow", tmdb_id=match.tmdb_id,
        matched_by=match.matched_by, files=files, ratings=computed["values"],
        rating_sources=computed["sources"],
        format_counts=format_counts,
        tv_status=tv_status_obj.status if tv_status_obj else None,
    )


def _tv_status_for_item(
    item: Item, sonarr_index: Optional[Dict[str, Any]], config_dir: Optional[Path],
    *, recent_days: int = 14,
) -> Optional[TvStatus]:
    """S9g1/K7a: compute this show's TvStatus, or None (no match / no Sonarr
    configured / any failure). Best-effort -- status is a nice-to-have
    overlay element, never worth failing a plan build over."""
    if not sonarr_index:
        return None
    tvdb_id = (item.provider_ids or {}).get("Tvdb")
    if not tvdb_id:
        return None
    entry = sonarr_index.get(str(tvdb_id))
    if entry is None:
        return None
    client, series_row = entry
    try:
        from finmeta.engine.tv_status import compute_tv_status
        episodes = client.episodes(series_row["id"])
        return compute_tv_status(series_row, episodes, recent_days=recent_days)
    except Exception as exc:
        logger.warning("Sonarr status lookup failed for %s: %s", item.name, exc)
        return None


def build_sonarr_index(cfg: Config, secrets) -> Dict[str, Any]:
    """S9g1: one {tvdbId (str): (SonarrClient, series_row)} index across every
    enabled Sonarr instance with a resolvable API key -- built ONCE per plan
    build (not per item) since GET /api/v3/series returns everything in one
    call. ``secrets`` is anything with a ``.get(name) -> Optional[str]``
    (``AppContext.secrets``); ``None`` (CLI/tests without one) -> no Sonarr
    lookups, status overlays/collections silently produce nothing (same
    degrade-gracefully pattern as every other optional provider here).

    The caller is responsible for closing every client in the returned index
    once the build is done (see ``build_plan``).
    """
    index: Dict[str, Any] = {}
    if secrets is None:
        return index
    from finmeta.arr.sonarr import SonarrClient
    for inst in cfg.media_apps.sonarr:
        if not inst.enabled:
            continue
        key = secrets.get(inst.api_key)
        if not key:
            continue
        try:
            client = SonarrClient(inst.url, api_key=key, timeout=inst.timeout,
                                  verify_ssl=inst.verify_ssl)
            for row in client.series():
                tvdb_id = row.get("tvdbId")
                if tvdb_id:
                    index.setdefault(str(tvdb_id), (client, row))
        except Exception as exc:
            logger.warning("Sonarr instance %r unreachable for status lookups: %s", inst.name, exc)
    return index


def build_trending_data(cfg: Config, secrets):
    """S9g2/K7b: fetch every configured MDBList trending list ONCE per plan
    build. Returns ``(rank_by_tmdb_id, fetched)`` where ``rank_by_tmdb_id``
    merges every list with ``rank_overlay`` on (first list in config order
    wins a given tmdb_id, used by the ``trending_rank`` overlay element) and
    ``fetched`` is ``[(TrendingListConfig, [MdblistListItem])]`` for every
    list (used afterward by ``_build_api_actions`` to build collections,
    once the matching tmdb_ids of plan_items are known).

    Best-effort per list: a fetch failure is logged and that list
    contributes nothing, it never fails the whole plan build. ``secrets`` is
    the same duck-typed ``.get(name) -> Optional[str]`` object
    ``build_sonarr_index`` takes; ``None`` or no saved key -> both return
    values are empty.
    """
    rank_by_tmdb: Dict[int, int] = {}
    fetched: List[Any] = []
    if secrets is None or not cfg.trending.lists:
        return rank_by_tmdb, fetched
    if not cfg.sources.mdblist.enabled:
        return rank_by_tmdb, fetched
    api_key = secrets.get("mdblist_key")
    if not api_key:
        return rank_by_tmdb, fetched
    from finmeta.sources.mdblist_lists import fetch_list_items
    for lst in cfg.trending.lists:
        try:
            items = fetch_list_items(lst.url, api_key, limit=lst.limit)
        except Exception as exc:
            logger.warning("trending list fetch failed (%r): %s", lst.url, exc)
            continue
        fetched.append((lst, items))
        if lst.rank_overlay:
            for it in items:
                if it.rank <= 10 and it.tmdb_id and it.tmdb_id not in rank_by_tmdb:
                    rank_by_tmdb[it.tmdb_id] = it.rank
    return rank_by_tmdb, fetched


def fetch_seasonal_mdblist_items(cfg: Config, secrets) -> List[Any]:
    """S9f2: fetch the one hardcoded MDBList URL the Christmas holiday in
    SEASONAL_CALENDAR uses, via the same mechanism/prerequisites as
    build_trending_data above (MDBList enabled + a saved key). Skips the
    fetch entirely outside Christmas's own window and whenever the
    Adaptive Seasonal Collections preset isn't active, so this never does
    network work 11 months of the year.
    """
    from finmeta.engine.collection_presets import SEASONAL_CALENDAR, is_holiday_active_now

    if "adaptive_seasonal" not in cfg.collection_presets:
        return []
    christmas = next(h for h in SEASONAL_CALENDAR if h.id == "christmas")
    if not is_holiday_active_now(christmas):
        return []
    if secrets is None or not cfg.sources.mdblist.enabled:
        return []
    api_key = secrets.get("mdblist_key")
    if not api_key:
        return []
    from finmeta.sources.mdblist_lists import fetch_list_items
    try:
        return fetch_list_items(christmas.mdblist_url, api_key, limit=500)
    except Exception as exc:
        logger.warning("seasonal mdblist fetch failed (%r): %s", christmas.mdblist_url, exc)
        return []


def _effective_write_method(cfg: Config, lib_cfg) -> str:
    """S9d5: global artwork_write_method + per-library override (None = inherit)."""
    return getattr(lib_cfg, "artwork_write_method", None) or cfg.artwork_write_method


def _effective_selection(cfg: Config, lib_cfg):
    """S9d4a: global artwork_selection + per-library override."""
    return effective_selection(
        cfg.artwork_selection.model_dump(),
        (lib_cfg.artwork_selection.model_dump()
         if getattr(lib_cfg, "artwork_selection", None) else None),
    )


def _select_artwork(sources, precedence, media_type, tmdb_id, kinds, *,
                    language=None, lang_fallback=None, warnings=None,
                    item_id: str = "", sel: Optional[dict] = None,
                    config_dir=None) -> Dict[str, Any]:
    """S9d4a: gather all ranked candidates per kind and pick one via the
    configured preset (best_rated default; random_top_n deterministic)."""
    from finmeta.api.overrides import OverrideStore  # noqa: F401  (counter may be absent)
    sel = sel or {"preset": "best_rated", "top_n": 3}
    by_kind = collect_candidates(
        sources, precedence, media_type, tmdb_id, kinds,
        language=language, lang_fallback=lang_fallback, warnings=warnings,
    )
    reshuffles = _reshuffle_counts(item_id, config_dir)
    out: Dict[str, Any] = {}
    for kind, ranked in by_kind.items():
        if not ranked:
            continue
        cand = select_candidate(
            ranked, preset=sel["preset"], top_n=sel["top_n"],
            item_id=item_id, kind=kind,
            reshuffle_count=reshuffles.get(kind, 0),
        )
        if cand is not None:
            out[kind] = cand
    return out


def _overlay_skipped(item_id: str, config_dir=None) -> bool:
    """S9d4c Fase A step 4: has the operator skipped the 'overlay' kind for
    this item (per-element overlay toggle, via the existing skip-kinds API)?

    Same best-effort read of overrides.db as ``_reshuffle_counts`` -- False
    (not skipped) on any failure or missing store (CLI/tests without a
    config dir). Checked at BUILD time (not as a post-filter like other skip
    kinds): unlike e.g. "poster", "overlay" has no FileWrite of its own to
    drop -- it is baked into the poster's bytes, so skipping it means
    building a plain (non-overlaid) poster instead.
    """
    try:
        if config_dir is None:
            return False
        from pathlib import Path as _P
        db = _P(config_dir) / "overrides.db"
        if not db.is_file():
            return False
        from finmeta.api.overrides import OverrideStore
        return "overlay" in OverrideStore(db).skipped_kinds(item_id)
    except Exception:
        return False


def _apply_candidate_overrides(resolved: Dict[str, Any], item_id: str,
                               kinds: List[str], config_dir=None) -> set:
    """Fold any stored manual candidate override (S9d2) into ``resolved`` BEFORE
    files are built, so a kind the operator manually picked goes through the
    SAME downstream path as an auto-selected candidate -- crucially, so an
    overridden poster still gets the overlay compositor applied to it.
    Returns the set of kinds that were overridden (callers mark the
    resulting FileWrite's ``meta["overridden"]`` with this so
    ``api.overrides.apply_overrides`` knows not to re-touch it).

    Bug this fixes (S9d4c, found while verifying Fase A step 6 end to end):
    previously an override only got folded in AFTERWARDS by
    ``api.overrides.apply_overrides``, which unconditionally re-hashes the
    override's ref as a PLAIN file and overwrites the FileWrite -- silently
    discarding any overlay badges ``_overlay_poster`` had already composited
    for that item. Best-effort: any failure/missing store leaves
    ``resolved`` untouched (same safety pattern as ``_reshuffle_counts``).
    """
    overridden: set = set()
    try:
        if config_dir is None:
            return overridden
        from pathlib import Path as _P
        db = _P(config_dir) / "overrides.db"
        if not db.is_file():
            return overridden
        from finmeta.api.overrides import OverrideStore
        store = OverrideStore(db)
        for kind in kinds:
            ov = store.get(item_id, kind)
            if ov and ov.get("ref"):
                # Candidate.source is Literal["tmdb","fanart","local"]; an
                # override's stored source may be absent/other -- "tmdb" is
                # just provenance here, never used to decide how to fetch
                # cand.ref (that's always a plain URL/path read).
                src = ov.get("source")
                if src not in ("tmdb", "fanart", "local"):
                    src = "tmdb"
                resolved[kind] = Candidate(
                    kind=kind, source=src,
                    ref=ov["ref"], lang=None, width=None, height=None, score=0.0,
                )
                overridden.add(kind)
    except Exception:
        pass
    return overridden


def _locked_candidates(item_id: str, kinds: List[str], config_dir=None) -> Dict[str, Candidate]:
    """S9d5: pre-resolved candidates for kinds already locked (first
    successful apply locks whatever was picked -- ``OverrideStore.set_lock``,
    called from ``engine.executor``).

    Callers pass the RETURN VALUE's keys into ``_select_artwork`` as kinds to
    SKIP -- that is the actual "no new downloads" guarantee for a locked
    item+kind: its TMDb/fanart candidate list is never queried again, ever,
    only a manual override (which still wins -- folded in afterwards by
    ``_apply_candidate_overrides``) or an explicit "re-pick" (which clears
    the lock) changes it. Best-effort: any failure/missing store returns {}
    (same safety pattern as ``_reshuffle_counts``/``_apply_candidate_overrides``).
    """
    out: Dict[str, Candidate] = {}
    try:
        if config_dir is None:
            return out
        from pathlib import Path as _P
        db = _P(config_dir) / "overrides.db"
        if not db.is_file():
            return out
        from finmeta.api.overrides import OverrideStore
        store = OverrideStore(db)
        for kind in kinds:
            lock = store.get_lock(item_id, kind)
            if lock and lock.get("ref"):
                out[kind] = Candidate(
                    kind=kind, source="tmdb",
                    ref=str(lock["ref"]), lang=None, width=None, height=None, score=0.0,
                )
    except Exception:
        pass
    return out


def _reshuffle_counts(item_id: str, config_dir=None) -> Dict[str, int]:
    """S9d4a step 2: per-kind reshuffle counters from overrides.db (best effort).

    Reads the same overrides.db the API uses; returns {} when it is absent
    (e.g. CLI use or tests without a config dir), which keeps selection
    deterministic with counter 0.
    """
    try:
        if config_dir is None:
            return {}
        from pathlib import Path as _P
        db = _P(config_dir) / "overrides.db"
        if not db.is_file():
            return {}
        from finmeta.api.overrides import OverrideStore
        return OverrideStore(db).reshuffle_counts(item_id)
    except Exception:
        return {}


# -- top-level plan builder -------------------------------------------------



def _resolve_item_library(item: Item, jf: JellyfinClient, cfg: Config):
    """Find the library config that actually owns ``item``.

    Matches ``item.path`` against each Jellyfin library's configured
    ``locations`` (longest matching prefix wins, for nested locations).
    Returns ``(name, lib_cfg)``, or ``(None, None)`` when ``item.path`` is
    set but matches no configured library's locations (the caller must
    treat that as "couldn't resolve", never substitute an unrelated
    library). Falls back to the first configured library only when the item
    has no path at all (keeps old behavior for items whose path is not
    resolvable, e.g. some test/mocked items).

    Fixed 2026-10-20: this used to walk item.parent_id -> grandparents and
    compare each ancestor id against ``/Library/VirtualFolders``' own
    ItemId. That silently never matched for TV shows on this install:
    a Series' parent_id pointed at a "<Name> Folder" item whose id never
    equalled the VirtualFolders entry for that library (a real Jellyfin
    quirk -- the item tree can insert an extra, differently-ID'd folder
    node above a library's top-level items). Every TV item therefore fell
    through to "the first configured library" (Movies, alphabetically/
    declaration-order first in config.yml) and silently got the Movies
    overlay profile/elements instead of the Shows one -- confirmed live:
    an applied TV poster had Movies-auto's badges (resolution/hdr/audio/
    rating) and no TV-status badge, even though Shows-auto's elements
    list correctly included "status". Path-prefix matching is both
    correct and cheaper (one get_libraries() call, no per-ancestor walk).
    """
    if item.path:
        best_name: Optional[str] = None
        best_cfg = None
        best_len = -1
        for lib in jf.get_libraries():
            lc = cfg.libraries.get(lib.name)
            if lc is None:
                continue
            for loc in lib.locations:
                loc_norm = loc.rstrip("/")
                if item.path == loc_norm or item.path.startswith(loc_norm + "/"):
                    if len(loc_norm) > best_len:
                        best_name, best_cfg, best_len = lib.name, lc, len(loc_norm)
        if best_cfg is not None:
            return best_name, best_cfg
        # A real path that matches NO configured library's Locations is a
        # sign something is structurally wrong -- the item genuinely doesn't
        # belong to any configured library, or Jellyfin reports a different
        # path for the item than for that library's own Locations (a real
        # quirk with symlinked/multi-mount libraries). Never silently borrow
        # an unrelated configured library's settings for it: reported live
        # as an item from one (real, successfully browsed) library landing
        # on a completely different, disabled library and 404ing with a
        # misleading "library disabled: <unrelated name>". The caller
        # (build_plan) must treat None, None as "couldn't resolve", not fall
        # through to its own generic non-None-default fallback.
        return None, None
    if cfg.libraries:
        fallback_name = list(cfg.libraries)[0]
        return fallback_name, cfg.libraries[fallback_name]
    raise ValueError("no library config found")


def _profile_for(lib_cfg, cfg: Config) -> Optional[dict]:
    """The named overlay profile dict for a library config (None when unset)."""
    name = getattr(lib_cfg, "overlay_profile", None)
    if not name:
        return None
    return (cfg.overlays.profiles or {}).get(name)


def _profile_overlay_names(lib_cfg, cfg: Config) -> List[str]:
    """Overlay element list for a library (K8/D2).

    No profile -> the library's own `overlays` list (v1 behavior preserved).
    Profile set -> its `elements` when present, else the library's `overlays`
    list. A profile named `none`-style empty elements disables overlays."""
    prof = _profile_for(lib_cfg, cfg)
    if prof is None:
        return list(getattr(lib_cfg, "overlays", []) or [])
    els = prof.get("elements")
    if els is not None:
        return [str(e) for e in els]
    return list(getattr(lib_cfg, "overlays", []) or [])


def _profile_style(lib_cfg, cfg: Config):
    """Style doc for a library's items: profile `style` merged over the global
    overlays.style (or just the global style when no profile/style override)."""
    base = cfg.overlays.style
    prof = _profile_for(lib_cfg, cfg)
    if not prof or "style" not in prof:
        return base
    from pydantic import TypeAdapter
    try:
        # S9d4c: the Overlays-tab editor saves the richer OverlayStyleDoc
        # shape (engine/style_presets.py: base_position/elements/
        # ribbon_text) for per-element layout; the simpler engine.overlay
        # style this helper returns only has position/margin/font_size/bg/
        # fg/radius. Translate the fields that map 1:1 and drop the rest
        # instead of failing validation and silently discarding the WHOLE
        # style (margin/font/colors included) -- found as part of the S9d4c
        # Fase A step 6 overlay-rendering investigation. Only used for
        # routes_settings.py's coarse style-doc fallback now; the real
        # apply renderer uses ``_apply_style_doc`` below instead, which
        # keeps per-element overrides intact.
        raw = dict(prof.get("style") or {})
        if "base_position" in raw and "position" not in raw:
            raw["position"] = raw.pop("base_position")
        raw.pop("elements", None)
        raw.pop("ribbon_text", None)
        merged = dict(base.model_dump())
        merged.update(raw)
        return TypeAdapter(type(base)).validate_python(merged)
    except Exception as exc:
        logger.warning("library profile style invalid (%s); using global style", exc)
        return base


def _profile_style_doc(lib_cfg, cfg: Config) -> dict:
    """Full per-element style document (OverlayStyleDoc shape) for a library's
    profile: the global ``overlays.style`` fields translated into doc keys
    (``position`` -> ``base_position``, S9g1/K7a's ``status_colors`` included),
    with the profile's own ``style`` document -- ``elements``/``ribbon_text``
    included -- merged on top when present. Unlike ``_profile_style`` above,
    this does NOT drop per-element layout; it's for the real apply renderer
    (``engine.overlay_render``), which understands the full document."""
    base = cfg.overlays.style.model_dump()
    doc: Dict[str, Any] = {}
    for k, v in base.items():
        if v is not None:
            doc["base_position" if k == "position" else k] = v
    prof = _profile_for(lib_cfg, cfg)
    if prof and "style" in prof:
        doc.update(dict(prof.get("style") or {}))
    return doc


def _apply_style_doc(lib_cfg, cfg: Config, overlay_names: Sequence[str]) -> dict:
    """Style document for a real apply render of one library's posters.

    Starts from ``_profile_style_doc`` (so per-element offset/radius/colour/
    status_colors take effect -- the whole point of this helper existing),
    but forces each element's ``enabled`` flag to match ``overlay_names`` --
    the existing, already-correct "which elements does this library show"
    computation (``_profile_overlay_names``) -- rather than trusting the
    style document's own per-element ``enabled`` flags. This keeps apply's
    set of rendered elements identical to before this layout engine changed;
    only HOW an enabled element looks is new, never WHICH elements render.
    """
    doc = _profile_style_doc(lib_cfg, cfg)
    elements = dict(doc.get("elements") or {})
    names = set(overlay_names)
    for key in ELEMENT_KEYS:
        el = dict(elements.get(key) or {})
        el["enabled"] = key in names
        elements[key] = el
    doc["elements"] = elements
    return doc


def _profile_format_rules(lib_cfg, cfg: Config) -> Optional[Dict[str, list]]:
    """Per-category K9 rules for a library: profile `format_rules` merged over
    the global overlays.format_rules; None means built-in defaults."""
    prof = _profile_for(lib_cfg, cfg)
    if not prof or "format_rules" not in prof:
        return cfg.overlays.format_rules or None
    out: Dict[str, list] = {k: [dict(r) for r in v] for k, v in (cfg.overlays.format_rules or {}).items()}
    for cat, rules in (prof.get("format_rules") or {}).items():
        out[cat] = [dict(r) for r in rules]
    return out or None


def _build_api_actions(
    cfg: Config,
    plan_items: List[ItemPlan],
    tmdb: TmdbClient,
    language: Optional[str],
    library_name: Optional[str] = None,
    trending_fetched: Optional[List[Any]] = None,
    seasonal_mdblist_fetched: Optional[List[Any]] = None,
) -> List[ApiAction]:
    """Build ApiActions from config collections + playlists (O1).

    For ``list`` collections: match by explicit TMDb IDs.
    For ``filter`` collections/playlists: match by year/rating/genres.
    ``tmdb_collection`` is skipped in v1 (needs TMDb collection data).
    ``trending_fetched`` (S9g2/K7b) is ``build_trending_data``'s second
    return value: ``[(TrendingListConfig, [MdblistListItem])]``.
    ``seasonal_mdblist_fetched`` (S9f2) is ``fetch_seasonal_mdblist_items``'s
    return value: ``[MdblistListItem]`` for the Christmas holiday's MDBList
    source, or ``[]`` when not applicable.
    """
    from finmeta.config.models import (
        Filter, FilterCollection, ListCollection, TmdbCollection,
    )

    actions: List[ApiAction] = []

    # Pre-fetch TMDb details for filter matching (reuses cache).
    details_cache: Dict[int, Dict[str, Any]] = {}
    for ip in plan_items:
        if ip.tmdb_id is None or ip.tmdb_id in details_cache:
            continue
        try:
            if ip.media_type == "movie":
                details_cache[ip.tmdb_id] = tmdb.movie_details(ip.tmdb_id, language=language)
            else:
                details_cache[ip.tmdb_id] = tmdb.tv_details(ip.tmdb_id, language=language)
        except Exception:
            pass

    def _matches(details: Dict[str, Any], flt) -> bool:
        if flt is None:
            return True
        if flt.year_from is not None or flt.year_to is not None:
            date_str = details.get("release_date") or details.get("first_air_date") or ""
            if not date_str:
                return False
            year = int(date_str[:4])
            if flt.year_from is not None and year < flt.year_from:
                return False
            if flt.year_to is not None and year > flt.year_to:
                return False
        if flt.min_rating is not None:
            if (details.get("vote_average") or 0) < flt.min_rating:
                return False
        if flt.genres:
            item_genres = {g.get("name") for g in (details.get("genres") or [])}
            if not any(g in item_genres for g in flt.genres):
                return False
        return True

    # Collections
    for coll in cfg.collections:
        if library_name and coll.library != library_name:
            continue
        add_ids: List[str] = []
        if isinstance(coll, ListCollection):
            for ip in plan_items:
                if ip.tmdb_id in coll.tmdb_ids:
                    add_ids.append(ip.item_id)
        elif isinstance(coll, FilterCollection):
            for ip in plan_items:
                if ip.tmdb_id is None:
                    continue
                if _matches(details_cache.get(ip.tmdb_id, {}), coll.filter):
                    add_ids.append(ip.item_id)
        elif isinstance(coll, TmdbCollection):
            continue  # v1: skip
        if add_ids:
            actions.append(ApiAction(
                action="create", kind="collection",
                name=coll.name, add_ids=add_ids,
            ))

    # Playlists
    for pl in cfg.playlists:
        if library_name and pl.library != library_name:
            continue
        matched = []
        for ip in plan_items:
            if ip.tmdb_id is None:
                continue
            details = details_cache.get(ip.tmdb_id, {})
            if _matches(details, pl.filter):
                matched.append((ip.item_id, details.get("vote_average") or 0))
        matched.sort(key=lambda x: x[1], reverse=True)
        add_ids = [iid for iid, _ in matched[: pl.limit]]
        if add_ids:
            actions.append(ApiAction(
                action="create", kind="playlist",
                name=pl.name, add_ids=add_ids,
            ))

    # S9g1/K7a: optional per-status collections (e.g. "New Season Soon").
    # Named from the status KEY (title-cased), not STATUS_LABELS -- those are
    # the terse overlay-badge text (e.g. "NEW SEASON", date on the badge
    # itself), not a good collection name on their own.
    if cfg.status_overlay.create_collections:
        by_status: Dict[str, List[str]] = {}
        for ip in plan_items:
            if ip.media_type == "tvshow" and ip.tv_status:
                by_status.setdefault(ip.tv_status, []).append(ip.item_id)
        for status, add_ids in by_status.items():
            name = cfg.status_overlay.collection_names.get(status) or status.replace("_", " ").title()
            actions.append(ApiAction(
                action="create", kind="collection",
                name=name, add_ids=add_ids,
            ))

    # S9g2/K7b: trending collections. Each list's tmdb_ids are matched
    # against plan_items (movies AND shows -- a trending list can mix both);
    # items the list has that aren't in a managed library are simply not
    # matched (not downloaded -- K7b explicitly keeps this phase
    # library-only, "missing items only listed in the report").
    for lst, items in (trending_fetched or []):
        wanted = {it.tmdb_id for it in items if it.tmdb_id}
        add_ids = [ip.item_id for ip in plan_items if ip.tmdb_id in wanted]
        if add_ids:
            actions.append(ApiAction(
                action="create", kind="collection",
                name=lst.collection_name, add_ids=add_ids,
            ))

    # S9f2: preset collections (chart + adaptive seasonal calendar). Only
    # evaluated on a full (all-libraries) plan/apply -- library_name set
    # means this call only saw a subset of the library's items, which would
    # make the executor's membership-sync (see _apply_api_action's "sync"
    # branch) wrongly remove items that belong to a library outside this
    # scoped run.
    if library_name is None:
        # Chart presets: always-on, one collection per configured id.
        for preset_id in cfg.collection_presets:
            preset = KNOWN_PRESETS.get(preset_id)
            if preset is None or preset.kind != "chart":
                continue  # stale/unknown id, or handled by the seasonal block below
            media_type = "movie" if preset.library_type == "movies" else "tvshow"
            candidates = [ip for ip in plan_items if ip.media_type == media_type]
            flt = Filter(min_rating=preset.min_rating) if preset.min_rating else None
            scored = []
            for ip in candidates:
                if ip.tmdb_id is None:
                    continue
                details = details_cache.get(ip.tmdb_id, {})
                if not _matches(details, flt):
                    continue
                scored.append((ip.item_id, details.get(preset.sort_by) or 0))
            scored.sort(key=lambda x: x[1], reverse=True)
            add_ids = [iid for iid, _ in scored[: preset.limit]]
            # "sync" (not "create"): executed via the dedicated branch in
            # _apply_api_action, which reconciles membership and deletes the
            # Jellyfin collection outright if add_ids ever ends up empty --
            # appended even when add_ids is empty so the executor sees that.
            actions.append(ApiAction(
                action="sync", kind="collection",
                name=preset.label, add_ids=add_ids,
            ))

        # Adaptive Seasonal Collections: one master toggle driving a whole
        # calendar of independently date-windowed holidays (SEASONAL_CALENDAR).
        # Each holiday becomes its own Jellyfin collection, synced/deleted the
        # same way as the chart presets above.
        if "adaptive_seasonal" in cfg.collection_presets:
            movie_candidates = [ip for ip in plan_items if ip.media_type == "movie"]
            keyword_cache: Dict[int, set] = {}

            def _item_keyword_names(tmdb_id: int) -> set:
                if tmdb_id not in keyword_cache:
                    try:
                        kws = tmdb.movie_keywords(tmdb_id)
                    except Exception:
                        kws = []
                    keyword_cache[tmdb_id] = {
                        (k.get("name") or "").lower() for k in kws
                    }
                return keyword_cache[tmdb_id]

            mdblist_wanted = {
                it.tmdb_id for it in (seasonal_mdblist_fetched or []) if it.tmdb_id
            }

            for holiday in SEASONAL_CALENDAR:
                add_ids = []
                if is_holiday_active_now(holiday):
                    if holiday.match == "genre":
                        flt = Filter(genres=list(holiday.genres))
                        for ip in movie_candidates:
                            if ip.tmdb_id is not None and _matches(
                                details_cache.get(ip.tmdb_id, {}), flt,
                            ):
                                add_ids.append(ip.item_id)
                    elif holiday.match == "keyword":
                        wanted_kw = {k.lower() for k in holiday.keywords}
                        for ip in movie_candidates:
                            if ip.tmdb_id is not None and _item_keyword_names(ip.tmdb_id) & wanted_kw:
                                add_ids.append(ip.item_id)
                    elif holiday.match == "mdblist":
                        add_ids = [
                            ip.item_id for ip in movie_candidates
                            if ip.tmdb_id in mdblist_wanted
                        ]
                actions.append(ApiAction(
                    action="sync", kind="collection",
                    name=holiday.label, add_ids=add_ids,
                ))

    return actions


def build_plan(
    cfg: Config,
    jf: JellyfinClient,
    tmdb: TmdbClient,
    sources: Dict[str, object],
    library: Optional[str] = None,
    item_id: Optional[str] = None,
    config_dir: Optional[Path] = None,
    secrets: Optional[Any] = None,
    limit: Optional[int] = None,
    offset: int = 0,
    progress_cb: Optional[Callable[[int, int], None]] = None,
) -> WritePlan:
    """Build a WritePlan for the given library or item.

    ``library`` is a config library name (e.g. "Movies").
    ``item_id`` is a Jellyfin item ID (overrides library scope).
    ``secrets`` (S9g1, optional) is anything with ``.get(name) ->
    Optional[str]`` (``AppContext.secrets``) -- used to resolve Sonarr API
    keys for TV show status overlays/collections. ``None`` (CLI/tests) means
    no Sonarr lookups happen; status overlays simply render nothing.
    ``limit``/``offset`` (single-library scope only, ``item_id`` and
    all-libraries scope ignore them) page the per-item work -- see the
    pagination block below. ``WritePlan.total_items`` is set only when
    ``limit`` is given, to the count of Movie/Series items BEFORE slicing.
    ``progress_cb`` (optional), called ``(done, total)`` after each item in
    the main per-item loop below -- a plan/apply run had exactly ONE log
    line ("started") until it finished, however long that took (no cap on
    how many items a single "Plan library"/"Apply library" run processes).
    A large library-wide run with several rating/image sources enabled can
    legitimately take many minutes; with zero visibility in between, that
    looked indistinguishable from a hung job -- reported live.
    """
    mapper = PathMapper.from_config([
        {"jellyfin": m.jellyfin, "local": m.local} for m in cfg.path_mappings
    ])
    language = cfg.language
    lang_fallback = cfg.artwork_language_fallback
    if config_dir is None:
        config_dir = DEFAULT_CONFIG_DIR

    provider_warnings_raw: List[Dict[str, object]] = []  # S9d2-fix2 step 6b
    items: List[Item] = []
    lib_name: Optional[str] = None
    lib_cfg = None

    if item_id:
        item = jf.get_item(item_id)
        items = [item]
        # S9d4c step 2a: resolve the item's ACTUAL library from its parent
        # chain (not "first config entry"). Falls back to the first library
        # config only when no ancestor matches any configured library.
        lib_name, lib_cfg = _resolve_item_library(item, jf, cfg)
        if lib_cfg is None:
            # item.path didn't match any configured library's Locations --
            # return here, before the generic "fill in a non-None default"
            # fallback below gets a chance to silently borrow an unrelated
            # library's config for this item (see _resolve_item_library's
            # docstring; reported live as a misleading "library disabled:
            # <unrelated name>" 404 on an item from a different, enabled
            # library whose path just didn't line up with Jellyfin's
            # reported Locations for it).
            from finmeta.engine.plan import ProviderWarning
            return WritePlan(
                items=[], api_actions=[], unmatched=[],
                provider_warnings=[ProviderWarning(
                    provider="config", count=1,
                    error=f"could not determine which configured library owns item {item_id}")],
            )
    elif library:
        if library not in cfg.libraries:
            raise ValueError(f"unknown library {library!r}")
        lib_name = library
        lib_cfg = cfg.libraries[library]
        jf_lib = jf.get_library(library)
        if jf_lib is None:
            raise ValueError(f"Jellyfin library {library!r} not found")
        items = jf.get_items(parent_id=jf_lib.item_id, recursive=True)
    else:
        # All libraries
        for name, lc in cfg.libraries.items():
            jf_lib = jf.get_library(name)
            if jf_lib is None:
                continue
            lib_items = jf.get_items(parent_id=jf_lib.item_id, recursive=True)
            for it in lib_items:
                it._lib_name = name  # type: ignore[attr-defined]
            items.extend(lib_items)

    # Determine if we have a multi-library plan
    is_multi = library is None and item_id is None

    # Pagination (single-library browse scope only): bound the expensive
    # per-item work below (TMDb/fanart/etc. lookups) to one page, BEFORE any
    # of it runs -- slicing after the fact wouldn't save anything. Profiled
    # 2026-10-23: ~7s/item cold (network-bound), so an unbounded library view
    # does not scale past a handful of titles without this.
    total_items: Optional[int] = None
    if limit is not None and item_id is None and not is_multi:
        items = [it for it in items if it.type in ("Movie", "Series")]
        total_items = len(items)
        items = items[offset:offset + limit]

    if lib_cfg is None and cfg.libraries:
        fallback_name = next(iter(cfg.libraries))
        lib_cfg = cfg.libraries[fallback_name]
        # Bug fix: this fallback only exists to give per-item code a non-None
        # default (see item_lib below). It must NOT set lib_name on a true
        # all-libraries run -- that would make _build_api_actions think this
        # is a scoped single-library call and skip every preset/seasonal
        # collection (Top 50 Popular, Highest Rated, Adaptive Seasonal)
        # on every full plan/apply, which is exactly what was happening.
        if not is_multi:
            lib_name = fallback_name
    if lib_cfg is None:
        raise ValueError("no library config found")

    # S9d4c step 2a: master switch. A disabled library produces an empty plan
    # (with a warning) and no further Jellyfin/TMDb work for its scope.
    # Only applies to a single-library/item scope: in the all-libraries scope
    # (is_multi) ``lib_cfg`` above is just a fallback value for code further
    # down, not necessarily the library being asked about, so bailing out here
    # would wrongly blank the whole multi-library plan whenever the FIRST
    # configured library happens to be disabled. Multi-library scope already
    # skips disabled libraries per item, below.
    if not is_multi and not getattr(lib_cfg, "enabled", False):
        from finmeta.engine.plan import ProviderWarning
        return WritePlan(
            items=[], api_actions=[], unmatched=[],
            provider_warnings=[ProviderWarning(
                provider="config", count=1,
                error=f"library disabled: {lib_name}")],
        )

    plan_items: List[ItemPlan] = []
    unmatched: List[UnmatchedItem] = []

    # S9g1/K7a: one Sonarr lookup for the whole build, not per show.
    sonarr_index = build_sonarr_index(cfg, secrets)
    # S9g2/K7b: one MDBList trending fetch per list for the whole build.
    trending_rank_by_tmdb, trending_fetched = build_trending_data(cfg, secrets)
    # S9f2: Christmas's MDBList source (Adaptive Seasonal Collections).
    seasonal_mdblist_fetched = fetch_seasonal_mdblist_items(cfg, secrets)

    # Batched MediaStreams fetch (S9d1 step 3): one /Items?Ids=a,b,c request per
    # batch_size of items instead of one per item. Items without overlays via
    # their library profile never need streams, so they are excluded up front.
    def _overlay_target_id(it: Item) -> Optional[str]:
        if it.type == "Movie":
            return it.id
        if it.type == "Series":
            eps = [e for e in jf.get_items(parent_id=it.id, recursive=True)
                   if e.type == "Episode"]
            if eps:
                eps.sort(key=lambda e: ((e.parent_index_number or 0),
                                        (e.index_number or 0)))
                return eps[0].id
        return None

    streams_by_id: Dict[str, List[Dict[str, Any]]] = {}
    target_by_item: Dict[str, str] = {}
    need_ids: List[str] = []
    for it in items:
        if it.type not in ("Movie", "Series"):
            continue
        ilib = cfg.libraries.get(getattr(it, "_lib_name", None)) or lib_cfg
        if _profile_overlay_names(ilib, cfg):
            tid = _overlay_target_id(it)
            if tid:
                need_ids.append(tid)
                target_by_item[it.id] = tid
    if need_ids:
        batch_size = max(1, int(getattr(cfg.overlays, "stream_batch_size", 50) or 50))
        try:
            streams_by_id = jf.get_items_media_streams_raw(need_ids, batch_size=batch_size)
        except Exception as exc:
            logger.warning("batched media-stream fetch failed (%s); falling back per item", exc)

    # Group items by their parent library if multi-library
    # For single library, all items use that library config
    total_items_for_progress = len(items)
    for idx, item in enumerate(items, 1):
        if progress_cb:
            # Reports "about to process", not "finished" -- simpler and
            # correct regardless of which of the several `continue`s below
            # a given item hits (disabled library, non-media type, season/
            # episode), no need to track every exit path separately.
            progress_cb(idx, total_items_for_progress)
        # Determine the library config for this item
        if is_multi:
            item_lib_name = getattr(item, "_lib_name", None)
            if item_lib_name and item_lib_name in cfg.libraries:
                item_lib = cfg.libraries[item_lib_name]
            else:
                item_lib = lib_cfg
        else:
            item_lib = lib_cfg

        # S9d4c step 2a: items in a disabled library are skipped entirely.
        if not getattr(item_lib, "enabled", False):
            continue

        # Skip non-media items. (This used to also assign a `media_type` that
        # nothing read -- the branches below re-derive it from item.type.)
        if item.type not in ("Movie", "Series", "Season", "Episode"):
            continue

        # For seasons/episodes, match the parent series
        if item.type == "Series":
            match = match_item(item, tmdb, "tvshow", language)
            if match.tmdb_id is None:
                # Mirror the Movie branch below: a show matching_item couldn't
                # resolve to TMDb must be reported in plan.unmatched (with a
                # reason) exactly like a movie is. Before this fix, only
                # Movies got this treatment -- an unmatched Series fell
                # through to _build_tvshow_plan, which returns an empty,
                # tmdb_id=None ItemPlan that then fails the "if ip.files or
                # ip.tmdb_id" append check below, vanishing from BOTH
                # plan.items and plan.unmatched. GET /api/items/{id}/plan then
                # couldn't find it in either list and raised a bare, reasonless
                # "item {id} not in plan" 404 -- reported live for a TV library
                # where several shows don't have a TMDb match.
                unmatched.append(UnmatchedItem(
                    item_id=item.id, name=item.name, reason=match.reason))
                continue
            ip = _build_tvshow_plan(
                item, match, cfg, item_lib, mapper, tmdb, jf,
                sources, language, lang_fallback, config_dir,
                streams=streams_by_id.get(target_by_item.get(item.id, "")),
                warnings=provider_warnings_raw,
                sonarr_index=sonarr_index,
                trending_index=trending_rank_by_tmdb,
            )
        elif item.type == "Movie":
            match = match_item(item, tmdb, "movie", language)
            if match.tmdb_id is None:
                unmatched.append(UnmatchedItem(
                    item_id=item.id, name=item.name, reason=match.reason))
                continue
            ip = _build_movie_plan(
                item, match, cfg, item_lib, mapper, tmdb, jf,
                sources, language, lang_fallback, config_dir,
                streams=streams_by_id.get(target_by_item.get(item.id, "")),
                warnings=provider_warnings_raw,
                trending_index=trending_rank_by_tmdb,
            )
        else:
            # Season or Episode — skip for v1 (need parent series context)
            continue

        if ip.files or ip.tmdb_id:
            plan_items.append(ip)

    # Close every Sonarr client opened for sonarr_index (deduped -- one
    # client instance is shared across every tvdbId from the same server).
    for client, _ in {id(c): (c, r) for c, r in sonarr_index.values()}.values():
        try:
            client.close()
        except Exception:
            pass

    api_actions = _build_api_actions(
        cfg, plan_items, tmdb, language,
        library_name=lib_name,
        trending_fetched=trending_fetched,
        seasonal_mdblist_fetched=seasonal_mdblist_fetched,
    )

    # S9d2-fix2 step 6b: surface provider errors (e.g. TMDb 401) that degraded
    # to "no candidates" — never silent.
    from finmeta.engine.plan import ProviderWarning
    plan = WritePlan(
        items=plan_items, api_actions=api_actions, unmatched=unmatched,
        provider_warnings=[ProviderWarning(**w) for w in provider_warnings_raw],
        total_items=total_items,
    )
    # Stamp each FileWrite.status against the files already on disk (SPEC §10)
    # so the plan JSON, CLI, API and e2e all see create/update/unchanged and a
    # second apply is a true no-op.
    annotate_status(plan)
    return plan


__all__ = ["build_plan"]
