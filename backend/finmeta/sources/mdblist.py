"""MDBList API client (S9b2).

httpx-based, with a 7-day TTL SQLite cache (``config/cache.db``, path
injectable) and a minimum-interval throttle. Aggregates IMDb, Rotten Tomatoes
(critic + audience), Metacritic, Trakt, TMDb and Letterboxd ratings in one call.

Endpoint: ``GET {base}/imdb/any/{imdb_id}/?apikey=<key>``. The response is a
JSON object whose ``ratings`` key is a *list* of ``{"source", "value",
"score", ...}`` items (one per critic/audience source). See
https://api.mdblist.com/docs/.

Graceful degrade: on a 429, a network error, or any other failure the client
returns all-None and does NOT cache the result (so a later retry can succeed).
"""
from __future__ import annotations

import time
from typing import Any, Callable, Dict, Optional

import httpx

from finmeta.sources.cache import SqliteCache, cache_key

DEFAULT_BASE_URL = "https://api.mdblist.com"
TTL = 7 * 86400  # 7 days
DEFAULT_MIN_INTERVAL = 1.0  # seconds between real (uncached) requests

# The seven rating fields MDBList can supply (keyed as in ``field_sources``).
RATING_FIELDS: list = [
    "imdb", "rt_critic", "rt_audience", "metacritic",
    "tmdb", "trakt", "letterboxd",
]

# Normalised ``ratings[].source`` value -> rating field key.
_SOURCE_TO_FIELD: Dict[str, str] = {
    "imdb": "imdb",
    "rotten tomatoes": "rt_critic",
    "rt": "rt_critic",
    "rotten tomatoes critic": "rt_critic",
    "rt critic": "rt_critic",
    "critic": "rt_critic",
    "rotten tomatoes audience": "rt_audience",
    "rt audience": "rt_audience",
    "audience": "rt_audience",
    "metacritic": "metacritic",
    "tmdb": "tmdb",
    "themoviedb": "tmdb",
    "themoviedb rating": "tmdb",
    "trakt": "trakt",
    "letterboxd": "letterboxd",
}


class MdblistError(RuntimeError):
    """Raised for an unexpected MDBList failure."""


def _normalize_source(source: Any) -> Optional[str]:
    if source is None:
        return None
    s = str(source).strip().lower().replace("-", " ").replace("_", " ")
    s = s.replace("(", " ").replace(")", " ")
    s = " ".join(s.split())
    return s or None


def _parse_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return round(float(value), 1)
    except (ValueError, TypeError):
        return None


def _extract_ratings(data: Dict[str, Any]) -> Dict[str, Optional[float]]:
    """Pull the seven rating values from a parsed MDBList response.

    ``ratings`` is a list of ``{source, value, score, ...}`` items. Each item
    maps to at most one rating field; the first non-None value per field wins.
    """
    out: Dict[str, Optional[float]] = {k: None for k in RATING_FIELDS}
    ratings = data.get("ratings") if isinstance(data, dict) else None
    if not isinstance(ratings, list):
        return out
    for item in ratings:
        if not isinstance(item, dict):
            continue
        norm = _normalize_source(item.get("source"))
        if norm is None:
            continue
        field = _SOURCE_TO_FIELD.get(norm)
        if field is None:
            continue
        value = item.get("value")
        if value is None:
            value = item.get("score")
        v = _parse_float(value)
        if v is not None and out[field] is None:
            out[field] = v
    return out


class MdblistClient:
    """Fetch aggregated ratings from the MDBList API (IMDb ID keyed)."""

    def __init__(self, api_key: str, cache_path: str = "config/cache.db",
                 base_url: str = DEFAULT_BASE_URL, min_interval: float = DEFAULT_MIN_INTERVAL,
                 cache_ttl: float = TTL,
                 sleep: Callable[[float], Any] = time.sleep,
                 now: Callable[[], float] = time.time) -> None:
        self.api_key = api_key
        self.base_url = str(base_url).rstrip("/")
        self.min_interval = float(min_interval)
        self.cache_ttl = float(cache_ttl)
        self._cache = SqliteCache(cache_path)
        self._sleep = sleep
        self._now = now
        self._last_request: float = 0.0

    # -- internal -----------------------------------------------------------
    def _throttle(self) -> None:
        """Enforce at least ``min_interval`` seconds between real requests."""
        if self.min_interval <= 0:
            return
        now = self._now()
        wait = self._last_request + self.min_interval - now
        if wait > 0:
            self._sleep(wait)
            now = self._now()
        self._last_request = now

    # -- public -------------------------------------------------------------
    def get_ratings(self, imdb_id: str) -> Dict[str, Optional[float]]:
        """Return the seven rating fields (missing values -> None).

        A successful response is cached for ``cache_ttl``. A 429, a network
        error, or any other failure returns all-None and is not cached.
        """
        if not imdb_id:
            return {k: None for k in RATING_FIELDS}
        cache_k = cache_key(f"/imdb/any/{imdb_id}")
        cached = self._cache.get(cache_k)
        if cached is not None:
            return _extract_ratings(cached)

        self._throttle()
        try:
            r = httpx.get(
                f"{self.base_url}/imdb/any/{imdb_id}/",
                params={"apikey": self.api_key},
                timeout=10.0,
            )
        except httpx.HTTPError:
            # network failure: do not cache (a retry may succeed)
            return {k: None for k in RATING_FIELDS}

        if r.status_code != 200:
            # 429 / 404 / 401 / ...: graceful degrade, do not cache
            return {k: None for k in RATING_FIELDS}
        try:
            data = r.json()
        except ValueError:
            return {k: None for k in RATING_FIELDS}
        if not isinstance(data, dict):
            return {k: None for k in RATING_FIELDS}

        self._cache.set(cache_k, data, self.cache_ttl)
        return _extract_ratings(data)

    def close(self) -> None:
        self._cache.close()

    def __enter__(self) -> "MdblistClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


__all__ = [
    "MdblistClient", "MdblistError", "RATING_FIELDS",
    "DEFAULT_BASE_URL", "_extract_ratings",
]
