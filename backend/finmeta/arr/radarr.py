"""Read-only Radarr v3 client (S9f1a).

GET only, ``X-Api-Key`` header. Exposes system status, the movie list and the
calendar. Never sends POST/PUT/DELETE — see :class:`finmeta.arr.base.ArrClient`.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from finmeta.arr.base import ArrClient


def _movie_view(m: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": m.get("id"),
        "title": m.get("title"),
        "status": m.get("status"),  # tba | announced | inCinemas | released
        "tmdbId": m.get("tmdbId"),
        "imdbId": m.get("imdbId"),
        "hasFile": m.get("hasFile"),
        "monitored": m.get("monitored"),
        "inCinemas": m.get("inCinemas"),
        "digitalRelease": m.get("digitalRelease"),
        "physicalRelease": m.get("physicalRelease"),
    }


class RadarrClient(ArrClient):
    app = "radarr"

    def movies(self) -> List[Dict[str, Any]]:
        """``GET /api/v3/movie`` — one row per movie."""
        data = self._get("/api/v3/movie") or []
        return [_movie_view(m) for m in data]

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

    def root_folders(self) -> List[Dict[str, Any]]:
        """``GET /api/v3/rootfolder`` — this instance's configured root
        folders (S9g3: used to refuse a Coming Soon folder that overlaps one)."""
        return self._get("/api/v3/rootfolder") or []
