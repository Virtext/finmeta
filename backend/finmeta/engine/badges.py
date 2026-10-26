"""Overlay badge values (SPEC \u00a78).

Turns Jellyfin MediaStreams + a TMDb vote score into the short strings the
compositor draws as text badges:

* resolution  \u2192 4K / 1080p / 720p / SD   (video Height)
* hdr         \u2192 DV / HDR10+ / HDR10 / HLG  (VideoRange / VideoRangeType)
* audio       \u2192 Atmos / TrueHD / DTS-HD / DTS / DD+ / 5.1 / 7.1 / Stereo
* rating      \u2192 \u2605 7.8                    (TMDb vote_average)

Missing data yields ``None`` so the caller omits the badge (SPEC \u00a78:
"Missing data \u2192 no badge, never a wrong badge").
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from finmeta.jellyfin_client.types import MediaStream


def format_resolution(height: Optional[int]) -> Optional[str]:
    """Map a video Height to a resolution label."""
    if not height:
        return None
    if height >= 2160:
        return "4K"
    if height >= 1080:
        return "1080p"
    if height >= 720:
        return "720p"
    return "SD"


def format_hdr(video_range: Optional[str], video_range_type: Optional[str]) -> Optional[str]:
    """Map VideoRange / VideoRangeType to an HDR label (``None`` for SDR)."""
    rng = (video_range or "").upper()
    rtype = (video_range_type or "").upper()
    if rng in ("HDR", "DOVI") or rtype in ("HDR10", "HDR10PLUS", "HLG", "DOVI"):
        if "DOVI" in rtype or rng == "DOVI":
            return "DV"
        if "PLUS" in rtype:
            return "HDR10+"
        if rtype == "HLG":
            return "HLG"
        return "HDR10"
    return None


def format_audio(streams: Sequence[MediaStream]) -> Optional[str]:
    """Pick the best audio stream and label its codec + channel layout.

    Codec wins when it is Atmos/TrueHD/DTS-HD/DD+; otherwise the channel
    layout (7.1 > 5.1 > Stereo) is shown. Missing audio \u2192 ``None``.
    """
    audio = [s for s in streams if s.type == "Audio"]
    if not audio:
        return None
    # Best = highest channel count, then codec.
    audio = sorted(audio, key=lambda s: (s.channels or 0), reverse=True)
    best = audio[0]
    codec = (best.codec or "").upper().replace("_", "").replace("-", "")
    if "ATMOS" in codec:
        return "Atmos"
    if "TRUEHD" in codec:
        return "TrueHD"
    if "DTS" in codec and "HD" in codec:
        return "DTS-HD"
    if "DTS" in codec:
        return "DTS"
    if "DOLBYDIGITALPLUS" in codec or "EAC3" in codec or "DDPLUS" in codec:
        return "DD+"
    ch = best.channels or 0
    if ch >= 8:
        return "7.1"
    if ch >= 6:
        return "5.1"
    if ch >= 2:
        return "Stereo"
    return "Stereo"


def format_rating(vote_average: Optional[float]) -> Optional[str]:
    """TMDb vote_average to a \u2605 label (``None`` when no vote data)."""
    if vote_average is None:
        return None
    return f"\u2605 {vote_average:.1f}"


def _rule_value(element: str, streams: Sequence[Any], rules=None) -> Optional[str]:
    """Label for one rule-driven element via K9 format_rules (None => no badge)."""
    from finmeta.engine.format_rules import format_rule_value_for_element
    try:
        return format_rule_value_for_element(element, streams, rules)
    except Exception:
        # A misconfigured stored rule must not crash a whole plan; degrade to no badge.
        return None


def overlay_values(
    streams: Sequence[Any], vote_average: Optional[float],
    categories_rules: Optional[Dict[str, Any]] = None,
    status_label: Optional[str] = None,
    trending_rank: Optional[int] = None,
    network: Optional[str] = None,
) -> Dict[str, Optional[str]]:
    """Build the ``{overlay_name: value}`` map for all SPEC v1 overlays.

    resolution/hdr(dynamic range)/audio are detected by K9 format_rules against
    the item's MediaStreams -- an element with NO matching enabled rule yields
    ``None`` so it is never drawn (K9). rating stays TMDb-derived.
    ``status_label`` (S9g1/K7a, TV shows only) is the already-formatted label
    from ``engine.tv_status.compute_tv_status`` (e.g. ``"NEW SEASON 12.11"``),
    or ``None`` for movies / shows with nothing worth showing.
    ``trending_rank`` (S9g2/K7b) is a 1-10 rank from a configured MDBList
    trending list with ``rank_overlay`` on, formatted as ``"#<rank>"``, or
    ``None`` when the item isn't in any such list's top 10. ``network``
    (2026-10-23, TV shows only) is the first entry of TMDb's own `networks`
    field (e.g. "Netflix"), or ``None`` for movies / shows TMDb has none for.
    Pass ``categories_rules`` (config overlays.format_rules) to override
    defaults.
    """
    values: Dict[str, Optional[str]] = {
        "resolution": _rule_value("resolution", streams, categories_rules),
        "hdr": _rule_value("hdr", streams, categories_rules),
        "audio": _rule_value("audio", streams, categories_rules),
        "rating": format_rating(vote_average),
        "status": status_label,
        "trending_rank": f"#{trending_rank}" if trending_rank else None,
        "network": network,
    }
    return values


def overlay_names_to_labels() -> Dict[str, str]:
    """Config overlay name \u2192 human label (for error messages/tests)."""
    return {"resolution": "resolution", "hdr": "hdr", "audio": "audio", "rating": "rating",
            "status": "status", "trending_rank": "trending_rank", "network": "network"}


def format_rating_value(key: str, value: float) -> str:
    """Canonical-scale K10 rating value (``engine.plan_builder.compute_ratings``)
    -> badge display string. Percent for the three 0-100 fields, one decimal
    otherwise (0-10 scale: imdb/tmdb/trakt; 0-5 scale: letterboxd, already
    rescaled to 0-10 by ``_to_scale`` before this is ever called).

    Single source of truth for both the apply renderer (``plan_builder.
    _overlay_poster``) and the editor's live preview (``api.routes_settings``)
    -- previously only the preview had this, so a real apply never rendered
    an IMDb/RT/Metacritic/TMDb/Letterboxd/Trakt rating badge at all (SPEC \u00a78
    "missing data -> no badge" made this look like silently-missing data
    rather than the real cause: the value was never computed for apply)."""
    if key in ("rt_critic", "rt_audience", "metacritic"):
        return f"{int(round(value))}%"
    return f"{value:.1f}"


__all__ = [
    "format_resolution", "format_hdr", "format_audio", "format_rating",
    "overlay_values", "overlay_names_to_labels", "format_rating_value",
]
