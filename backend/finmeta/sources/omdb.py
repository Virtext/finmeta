"""OMDb API client (S9 §C, step 4).

httpx-based, with a 7-day TTL SQLite cache. Provides IMDb rating, Rotten
Tomatoes critic score, and Metacritic score for a given IMDb ID.
Responses are cached in ``config/cache.db`` (path injectable for tests).
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import httpx

from finmeta.sources.cache import SqliteCache, cache_key
from finmeta.sources.tmdb import RateLimiter

TTL = 7 * 86400  # 7 days
BASE_URL = "https://www.omdbapi.com/"


class OMDbError(RuntimeError):
    """Raised for any OMDb request failure."""


def _parse_float(value: Any) -> Optional[float]:
    """Parse an OMDb rating string to a float (None if missing/invalid)."""
    if value is None or value == "N/A":
        return None
    try:
        return round(float(value), 1)
    except (ValueError, TypeError):
        return None


def _extract_ratings(data: Dict[str, Any]) -> Dict[str, Optional[float]]:
    """Pull the three rating values from a parsed OMDb response."""
    imdb = _parse_float(data.get("imdbRating"))
    metacritic = _parse_float(data.get("Metascore"))
    rt = _parse_float(data.get("RottenTomatoes"))
    return {"imdb": imdb, "rt_critic": rt, "metacritic": metacritic}


class OMDbClient:
    """Fetch ratings from the OMDb API (IMDb ID keyed)."""

    def __init__(self, api_key: str, cache_path: str = "config/cache.db",
                 base_url: str = BASE_URL, min_interval: float = 1.0):
        self.api_key = api_key
        self.base_url = base_url
        self._cache = SqliteCache(cache_path)
        # Unlike TmdbClient/MdblistClient/FanartSource, this had no throttle
        # at all -- a large bulk plan issues one call per item here on every
        # cache miss, back to back, as fast as the network allows. OMDb's
        # free tier is a 1000/day quota, not a published burst limit, but an
        # unthrottled burst of hundreds of sequential requests is still the
        # kind of thing that gets an IP flagged by API abuse detection.
        self.limiter = RateLimiter(min_interval)

    def get_ratings(self, imdb_id: str) -> Dict[str, Optional[float]]:
        """Return ``{imdb, rt_critic, metacritic}`` (missing values -> None).

        Uses the IMDb ID (``ttXXXX``) to look up the title. If the title is
        not found or the API returns an error, all values are None.
        """
        cache_k = cache_key(f"/imdb/{imdb_id}")
        cached = self._cache.get(cache_k)
        if cached is not None:
            return _extract_ratings(cached)

        try:
            self.limiter.wait()
            r = httpx.get(
                self.base_url,
                params={"apikey": self.api_key, "i": imdb_id},
                timeout=8.0,
            )
        except httpx.HTTPError as exc:
            raise OMDbError(f"OMDb request failed: {exc}") from exc

        data = r.json()
        if r.status_code != 200 or data.get("Response") != "True":
            # Title not found or API error — return all None (graceful degrade)
            return {"imdb": None, "rt_critic": None, "metacritic": None}

        self._cache.set(cache_k, data, TTL)
        return _extract_ratings(data)

    def close(self) -> None:
        self._cache.close()

    def __enter__(self) -> "OMDbClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


__all__ = ["OMDbClient", "OMDbError", "_extract_ratings"]
