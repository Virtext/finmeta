"""Read-only Sonarr v3 client (S9f1a).

GET only, ``X-Api-Key`` header. Exposes system status, the series list and the
calendar. Never sends POST/PUT/DELETE — see :class:`finmeta.arr.base.ArrClient`.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from finmeta.arr.base import ArrClient


def _series_view(s: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": s.get("id"),
        "title": s.get("title"),
        "status": s.get("status"),  # continuing | ended | upcoming | deleted
        "tvdbId": s.get("tvdbId"),
        "tmdbId": s.get("tmdbId"),
        "imdbId": s.get("imdbId"),
        "nextAiring": s.get("nextAiring"),
        "previousAiring": s.get("previousAiring"),
        "monitored": s.get("monitored"),
    }


def _episode_view(e: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "seasonNumber": e.get("seasonNumber"),
        "episodeNumber": e.get("episodeNumber"),
        "airDateUtc": e.get("airDateUtc"),
        "hasFile": e.get("hasFile"),
        "monitored": e.get("monitored"),
        # Not present on every Sonarr version -- "series" | "season" |
        # "midseason" | None. engine.tv_status treats it as a best-effort
        # hint, never a hard requirement (see its own docstring).
        "finaleType": e.get("finaleType"),
    }


class SonarrClient(ArrClient):
    app = "sonarr"

    def series(self) -> List[Dict[str, Any]]:
        """``GET /api/v3/series`` — one row per series (root series only)."""
        data = self._get("/api/v3/series", params={"recursive": False}) or []
        return [_series_view(s) for s in data]

    def calendar(self, start: Optional[str] = None,
                 end: Optional[str] = None) -> List[Dict[str, Any]]:
        """``GET /api/v3/calendar`` — upcoming releases in [start, end].

        ``start``/``end`` are ISO-8601 strings (e.g. ``2025-01-01``); both may
        be omitted for the default window. Returns raw calendar entries.
        """
        params: Dict[str, Any] = {}
        if start is not None:
            params["start"] = start
        if end is not None:
            params["end"] = end
        return self._get("/api/v3/calendar", params=params or None) or []

    def episodes(self, series_id: int) -> List[Dict[str, Any]]:
        """``GET /api/v3/episode?seriesId=<id>`` — every episode Sonarr knows
        about for one series (aired and unaired). Used by ``engine.tv_status``
        to tell a season premiere/finale apart from a regular episode."""
        data = self._get("/api/v3/episode", params={"seriesId": series_id}) or []
        return [_episode_view(e) for e in data]

    def root_folders(self) -> List[Dict[str, Any]]:
        """``GET /api/v3/rootfolder`` — this instance's configured root
        folders (S9g3: used to refuse a Coming Soon folder that overlaps one)."""
        return self._get("/api/v3/rootfolder") or []
