"""S9g1 (K7a): TV show status from Sonarr data (SPEC_KOMETA.md K7).

Clean-room implementation: built only from the feature list in
SPEC_KOMETA.md K7a and the public Sonarr v3 API (series + episode rows),
never from UMTK's own code -- see SPEC_KOMETA.md K7's licence note.

Status values: new_series, new_season_soon, new_season_started,
upcoming_episode, upcoming_finale, season_finale_aired, final_episode_aired,
returning, ended, canceled.

Sonarr's own ``series.status`` is only one of continuing/ended/upcoming/
deleted -- everything more specific (season vs. series finale, "just
aired" vs. "long over") is derived here from the episode list, which Sonarr
always has even for unaired episodes. ``finaleType`` (series/season/
midseason) exists on newer Sonarr versions but not all -- treated as a
best-effort hint; when absent, a season's last KNOWN episode (highest
episodeNumber Sonarr has on record for that season) stands in for it.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

# Human label + whether a date is shown, per status. The UI/overlay badge
# renders "{label}" or "{label} {date}" per K7a ("text or ribbon with date,
# e.g. 'NEW SEASON 12.11'") -- the date itself is "today"/"tomorrow" for the
# next two days, a weekday name for days 2-6 out, else DD.MM (see
# _fmt_relative).
STATUS_LABELS: Dict[str, str] = {
    "new_series": "NEW SERIES",
    "new_season_soon": "NEW SEASON",
    "new_season_started": "NEW SEASON",
    "upcoming_episode": "NEW EPISODE",
    "upcoming_finale": "FINALE",
    "season_finale_aired": "SEASON FINALE",
    "final_episode_aired": "SERIES FINALE",
    "returning": "RETURNING",
    "ended": "ENDED",
    "canceled": "CANCELED",
}

# Statuses whose badge includes a date (DD.MM, from the driving airDateUtc).
_DATED = {
    "new_season_soon", "new_season_started", "upcoming_episode",
    "upcoming_finale", "season_finale_aired", "final_episode_aired",
}

# Clean-room ribbon colour per status (SPEC_KOMETA.md K7a: "text or ribbon
# with date") -- a plain text pill reads as "nothing happened" next to the
# resolution/audio/rating badges, so each status gets its own accent colour
# instead of the generic overlay backdrop. #RRGGBBAA, ~85% opaque to match
# the existing badge backdrop convention (default bg is #000000B0 = ~69%).
STATUS_COLORS: Dict[str, str] = {
    "new_series": "#16A34ACC",
    "new_season_soon": "#0D9488CC",
    "new_season_started": "#2563EBCC",
    "upcoming_episode": "#4F46E5CC",
    "upcoming_finale": "#D97706CC",
    "season_finale_aired": "#EA580CCC",
    "final_episode_aired": "#DC2626CC",
    "returning": "#0891B2CC",
    "ended": "#475569CC",
    "canceled": "#991B1BCC",
}
# Trending rank badge colour (K7b "TOP 10" rank).
TRENDING_RANK_COLOR = "#CA8A04CC"


@dataclass(frozen=True)
class TvStatus:
    status: str
    label: str            # e.g. "NEW SEASON" or "NEW SEASON 12.11"
    date: Optional[str]    # ISO date driving the label, or None


def _parse_date(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        v = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(v)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _fmt_date(dt: Optional[datetime]) -> Optional[str]:
    return dt.strftime("%d.%m") if dt is not None else None


_WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _fmt_relative(dt: datetime, now: datetime) -> str:
    """Operator request (2026-10-21): "today"/"tomorrow" for the next two
    days, the weekday name for days 2-6 out (within one calendar week, so
    "Monday" is never ambiguous between this week and next), DD.MM beyond
    that or for dates in the past (e.g. *_aired statuses).

    Uppercased (2026-10-25, operator-reported): the band is drawn as one
    plain-text line alongside the ALL-CAPS ``STATUS_LABELS`` prefix (e.g.
    "NEW EPISODE") -- lowercase/title-case here read as noticeably smaller
    next to that prefix purely from case (no cap-height), not an actual
    font-size difference. Matching the case makes the whole label read as
    one size."""
    delta_days = (dt.date() - now.date()).days
    if delta_days == 0:
        return "TODAY"
    if delta_days == 1:
        return "TOMORROW"
    if 2 <= delta_days <= 6:
        return _WEEKDAYS[dt.weekday()].upper()
    return _fmt_date(dt) or ""


def _episode_for(episodes: List[Dict[str, Any]], air_date: Optional[str]) -> Optional[Dict[str, Any]]:
    if not air_date:
        return None
    target = _parse_date(air_date)
    if target is None:
        return None
    for ep in episodes:
        ep_dt = _parse_date(ep.get("airDateUtc"))
        if ep_dt is not None and ep_dt == target:
            return ep
    return None


def _is_season_finale(ep: Dict[str, Any], episodes: List[Dict[str, Any]]) -> bool:
    finale_type = ep.get("finaleType")
    if finale_type in ("season", "series", "midseason"):
        return True
    if finale_type is not None:
        return False  # explicit non-finale hint from Sonarr -- trust it
    # No hint available: this episode is the finale of its season if no
    # OTHER known episode (aired or not) in the same season has a higher
    # episode number.
    season = ep.get("seasonNumber")
    num = ep.get("episodeNumber") or 0
    return not any(
        (e.get("seasonNumber") == season and (e.get("episodeNumber") or 0) > num)
        for e in episodes
    )


def _is_series_finale(ep: Dict[str, Any]) -> bool:
    return ep.get("finaleType") == "series"


def compute_tv_status(
    series: Dict[str, Any],
    episodes: List[Dict[str, Any]],
    *,
    now: Optional[datetime] = None,
    recent_days: int = 14,
) -> Optional[TvStatus]:
    """Compute one :class:`TvStatus` for a Sonarr series, or ``None`` when
    nothing is worth showing (deleted series, or a stable "continuing" show
    with no nearby episode in either direction).

    ``series`` and ``episodes`` are the raw(ish) dicts from
    ``SonarrClient.series()``/``.episodes()``. ``recent_days`` controls the
    "just aired" window for the *_aired statuses (default 14, matching this
    project's other "recent" windows).
    """
    now = now or datetime.now(timezone.utc)
    raw_status = str(series.get("status") or "").lower()
    if raw_status == "deleted":
        return None
    if "cancel" in raw_status:
        return TvStatus("canceled", STATUS_LABELS["canceled"], None)

    next_airing = series.get("nextAiring")
    prev_airing = series.get("previousAiring")

    def _has_aired(ep: Dict[str, Any]) -> bool:
        if ep.get("hasFile"):
            return True
        dt = _parse_date(ep.get("airDateUtc"))
        return dt is not None and dt <= now

    aired = [e for e in episodes if _has_aired(e)]

    if raw_status == "upcoming" or not aired:
        dt = _parse_date(next_airing) or _parse_date(
            min((e.get("airDateUtc") for e in episodes if e.get("airDateUtc")), default=None))
        return TvStatus("new_series", _label("new_series", dt, now), _iso(dt))

    if raw_status == "ended":
        last_aired = max(
            aired, key=lambda e: _parse_date(e.get("airDateUtc")) or datetime.min.replace(tzinfo=timezone.utc))
        if _is_series_finale(last_aired):
            dt = _parse_date(last_aired.get("airDateUtc"))
            if dt is not None and now - dt <= timedelta(days=recent_days):
                return TvStatus("final_episode_aired", _label("final_episode_aired", dt, now), _iso(dt))
        return TvStatus("ended", STATUS_LABELS["ended"], None)

    # raw_status == "continuing"
    if next_airing:
        next_ep = _episode_for(episodes, next_airing)
        dt = _parse_date(next_airing)
        if next_ep is not None:
            season = next_ep.get("seasonNumber") or 0
            num = next_ep.get("episodeNumber") or 0
            if num == 1 and season > 1:
                return TvStatus("new_season_soon", _label("new_season_soon", dt, now), _iso(dt))
            if _is_season_finale(next_ep, episodes):
                return TvStatus("upcoming_finale", _label("upcoming_finale", dt, now), _iso(dt))
        return TvStatus("upcoming_episode", _label("upcoming_episode", dt, now), _iso(dt))

    if prev_airing:
        prev_ep = _episode_for(episodes, prev_airing)
        dt = _parse_date(prev_airing)
        if dt is not None and now - dt <= timedelta(days=recent_days) and prev_ep is not None:
            season = prev_ep.get("seasonNumber") or 0
            num = prev_ep.get("episodeNumber") or 0
            if num == 1 and season > 1:
                return TvStatus("new_season_started", _label("new_season_started", dt, now), _iso(dt))
            if _is_season_finale(prev_ep, episodes):
                return TvStatus("season_finale_aired", _label("season_finale_aired", dt, now), _iso(dt))

    return TvStatus("returning", STATUS_LABELS["returning"], None)


def _label(status: str, dt: Optional[datetime], now: datetime) -> str:
    base = STATUS_LABELS[status]
    if status in _DATED and dt is not None:
        return f"{base} {_fmt_relative(dt, now)}"
    return base


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.date().isoformat() if dt is not None else None


__all__ = ["TvStatus", "STATUS_LABELS", "compute_tv_status"]
