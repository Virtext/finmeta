"""TMDb API client (SPEC §4).

httpx-based, rate-limited to ~40 req/10 s, with a TTL SQLite cache.
Details use a 7 day TTL; images use a 30 day TTL. Responses are cached
in ``config/cache.db`` (path injectable for tests).
"""
from __future__ import annotations

import time
from typing import Any, Dict, Iterable, List, Optional

import httpx

from finmeta.sources.cache import SqliteCache, cache_key
from finmeta.sources.candidate import Candidate, Kind, SourceName
from finmeta.sources.ranking import rank_candidates

DETAIL_TTL = 7 * 86400      # 7 days
IMAGE_TTL = 30 * 86400      # 30 days
DEFAULT_MIN_INTERVAL = 0.25  # 40 req / 10 s


class TmdbError(RuntimeError):
    """Raised for any TMDb request failure (HTTP error or API error)."""


class RateLimiter:
    """Enforce a minimum interval between calls (simple, injectable for tests)."""

    def __init__(self, min_interval: float = DEFAULT_MIN_INTERVAL):
        self.min_interval = min_interval
        self._last = 0.0

    def wait(self) -> None:
        if self.min_interval <= 0:
            return
        now = time.monotonic()
        gap = now - self._last
        if gap < self.min_interval:
            time.sleep(self.min_interval - gap)
        self._last = time.monotonic()


class TmdbClient:
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.themoviedb.org/3",
        image_base: str = "https://image.tmdb.org/t/p",
        cache: Optional[SqliteCache] = None,
        cache_path=None,
        detail_ttl: float = DETAIL_TTL,
        image_ttl: float = IMAGE_TTL,
        min_interval: float = DEFAULT_MIN_INTERVAL,
        http_client: Optional[httpx.Client] = None,
    ):
        if not api_key:
            raise TmdbError("TMDB_KEY is empty")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.image_base = image_base.rstrip("/")
        self.detail_ttl = detail_ttl
        self.image_ttl = image_ttl
        self.limiter = RateLimiter(min_interval)
        self._http = http_client if http_client is not None else httpx.Client()
        self._owns_http = http_client is None
        self._cache = cache
        if self._cache is None and cache_path is not None:
            self._cache = SqliteCache(cache_path)
        self._http_calls = 0

    # -- low-level GET with cache ------------------------------------------
    def _get(self, path: str, params: Optional[dict], ttl: float) -> dict:
        clean = {k: v for k, v in (params or {}).items() if v is not None and v != ""}
        key = cache_key(path, clean)
        if self._cache is not None:
            hit = self._cache.get(key)
            if hit is not None:
                return hit
        self.limiter.wait()
        self._http_calls += 1
        resp = self._http.get(self.base_url + path, params={**clean, "api_key": self.api_key})
        if resp.status_code != 200:
            raise TmdbError(f"TMDb GET {path} -> HTTP {resp.status_code}")
        data: dict = resp.json()
        if self._cache is not None:
            self._cache.set(key, data, ttl)
        return data

    # -- details -----------------------------------------------------------
    def movie_details(self, tmdb_id, language: Optional[str] = None) -> dict:
        return self._get(f"/movie/{tmdb_id}", {"language": language}, self.detail_ttl)

    def tv_details(self, tmdb_id, language: Optional[str] = None) -> dict:
        return self._get(f"/tv/{tmdb_id}", {"language": language}, self.detail_ttl)

    def movie_keywords(self, tmdb_id) -> List[dict]:
        """S9f2: TMDb movie keywords, e.g. ``[{"id": 9663, "name": "christmas"}]`` --
        used to match seasonal holidays (Mother's/Father's Day, Thanksgiving) the
        same way Kometa's own ``keyword.any`` builder does, scoped to items
        already in the library instead of discovering across all of TMDb."""
        data = self._get(f"/movie/{tmdb_id}/keywords", None, self.detail_ttl)
        return data.get("keywords") or []

    def season_details(self, series_tmdb_id, season_number, language=None) -> dict:
        return self._get(f"/tv/{series_tmdb_id}/season/{season_number}",
                         {"language": language}, self.detail_ttl)

    def episode_details(self, series_tmdb_id, season_number, episode_number, language=None) -> dict:
        return self._get(f"/tv/{series_tmdb_id}/season/{season_number}/episode/{episode_number}",
                         {"language": language}, self.detail_ttl)

    # -- images ------------------------------------------------------------
    def movie_images(self, tmdb_id, language=None, language_fallback: Optional[Iterable[Optional[str]]] = None,
                     kinds: Iterable[str] = ("poster", "backdrop", "logo")) -> List[Candidate]:
        data = self._get(f"/movie/{tmdb_id}/images", {"language": language}, self.image_ttl)
        return self._to_candidates(data, kinds, language, language_fallback)

    def tv_images(self, tmdb_id, language=None, language_fallback=None,
                  kinds=("poster", "backdrop", "logo")) -> List[Candidate]:
        data = self._get(f"/tv/{tmdb_id}/images", {"language": language}, self.image_ttl)
        return self._to_candidates(data, kinds, language, language_fallback)

    def season_images(self, series_tmdb_id, season_number, language=None, language_fallback=None,
                      kinds=("poster", "backdrop", "logo")) -> List[Candidate]:
        data = self._get(f"/tv/{series_tmdb_id}/season/{season_number}/images",
                         {"language": language}, self.image_ttl)
        return self._to_candidates(data, kinds, language, language_fallback)

    # -- search / find (for matching) --------------------------------------
    def find_by_external_id(self, media_type: str, external_id: str,
                            language: Optional[str] = None) -> Optional[dict]:
        """Use TMDb's /find endpoint with IMDb id. Returns the media dict or None."""
        data = self._get("/find/external", {
            "external_id": external_id,
            "external_source": "imdb_id",
            "language": language,
        }, self.detail_ttl)
        key = f"{media_type}_results"
        results = data.get(key) or []
        return results[0] if results else None

    def search_movies(self, query: str, year: Optional[int] = None,
                      language: Optional[str] = None) -> List[dict]:
        """Search TMDb movies by title."""
        params: Dict[str, Any] = {"query": query, "language": language}
        if year:
            params["primary_release_year"] = year
        data = self._get("/search/movie", params, self.detail_ttl)
        return data.get("results") or []

    def search_tv(self, query: str, year: Optional[int] = None,
                  language: Optional[str] = None) -> List[dict]:
        """Search TMDb TV shows by name."""
        params: Dict[str, Any] = {"query": query, "language": language}
        if year:
            params["first_air_date_year"] = year
        data = self._get("/search/tv", params, self.detail_ttl)
        return data.get("results") or []

    _KIND_KEY = {"poster": "posters", "backdrop": "backdrops", "logo": "logos"}

    @staticmethod
    def _clean(fp: str) -> str:
        return fp.lstrip("/")

    def _to_candidates(self, data: dict, kinds, language, language_fallback) -> List[Candidate]:
        prefs = self._prefs(language, language_fallback)
        out: List[Candidate] = []
        for kind in kinds:
            key = self._KIND_KEY.get(kind)
            if key is None:
                continue
            for item in data.get(key) or []:
                fp = item.get("file_path")
                if not fp:
                    continue
                out.append(Candidate(
                    kind=kind, source="tmdb",
                    ref=f"{self.image_base}/original/{self._clean(fp)}",
                    lang=item.get("iso_639_1"),
                    width=item.get("width"), height=item.get("height"),
                    score=float(item.get("vote_average") or 0.0),
                ))
        return rank_candidates(out, prefs)

    @staticmethod
    def _prefs(language, language_fallback):
        prefs: List[Optional[str]] = []
        if language:
            prefs.append(language)
        if language_fallback:
            prefs.extend(language_fallback)
        if not prefs:
            prefs = [None]
        # de-dupe preserving order
        seen, out = set(), []
        for p in prefs:
            if p not in seen:
                seen.add(p)
                out.append(p)
        return out

    # -- lifecycle ---------------------------------------------------------
    @property
    def http_calls(self) -> int:
        """Number of real HTTP GETs made (excludes cache hits)."""
        return self._http_calls

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def __enter__(self) -> "TmdbClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


__all__ = ["TmdbClient", "TmdbError", "RateLimiter", "DETAIL_TTL", "IMAGE_TTL"]
