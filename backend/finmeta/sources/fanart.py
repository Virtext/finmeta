"""fanart.tv source (SPEC §4, optional).

Only active when an API key is present; otherwise it is skipped with a log
line and yields no candidates.
"""
from __future__ import annotations

import logging
from typing import Iterable, List, Optional

import httpx

from finmeta.sources.candidate import Candidate
from finmeta.sources.cache import SqliteCache, cache_key
from finmeta.sources.tmdb import RateLimiter

logger = logging.getLogger("finmeta.sources.fanart")

# Same TTL as TmdbClient's image cache (sources/tmdb.py) -- this is the same
# kind of data (artwork references). Unlike every other source, fanart had
# NO cache at all until 2026-10-23: resolve_candidates (engine/resolution.py)
# queries each configured artwork kind separately, so one item with e.g.
# poster+backdrop+logo configured issued 3 identical uncached HTTP calls to
# the SAME endpoint, every single plan build, forever -- found while
# profiling library-load latency after fixing the URL bug above (that fix
# made fanart actually succeed, which is what made this cost visible).
TTL = 30 * 86400

# (kind) -> fanart.tv response key, per media type.
_KIND_KEY = {
    "movie": {"poster": "movieposter", "backdrop": "hdmovie", "logo": "clearlogo"},
    "tvshow": {"poster": "tvposter", "backdrop": "tvbackground", "logo": "clearlogo"},
}

# fanart.tv's own URL convention is inconsistent (movies plural, tv singular)
# and doesn't match Finmeta's internal media_type strings ("movie"/"tvshow"
# everywhere else, e.g. plan_builder.py) -- found 2026-10-23 while profiling
# library-load latency: every movie candidate lookup was 404ing against
# "/movie/<id>" (should be "/movies/<id>") and every show lookup both hit the
# wrong URL ("/tvshow/<id>") AND matched no _KIND_KEY entry, always [].
_URL_SEGMENT = {"movie": "movies", "tvshow": "tv"}


class FanartSource:
    def __init__(self, api_key: Optional[str] = None,
                 base_url: str = "https://webservice.fanart.tv/v3",
                 image_base: str = "https://fanart.tv/fanart",
                 http_client: Optional[httpx.Client] = None,
                 min_interval: float = 1.0,
                 cache_path: str = "config/cache.db"):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.image_base = image_base.rstrip("/")
        self._http = http_client if http_client is not None else httpx.Client()
        self._owns_http = http_client is None
        self.min_interval = min_interval
        # min_interval was accepted and stored here but never actually
        # enforced anywhere -- every cache-miss request went straight to the
        # network with no throttle at all, decorative only. Operator asked
        # for real rate limiting across sources after a large bulk plan ran
        # for a while; this is the one that looked like it already had it.
        self.limiter = RateLimiter(min_interval)
        self.enabled = bool(api_key)
        self._cache = SqliteCache(cache_path)
        if not self.enabled:
            logger.info("fanart.tv skipped: FANART_KEY not set (no candidates from this source)")

    def _endpoint(self, media_type: str, external_id) -> str:
        return f"{self.base_url}/{_URL_SEGMENT.get(media_type, media_type)}/{external_id}"

    def candidates_for(self, media_type: str, external_id, kinds: Iterable[str]) -> List[Candidate]:
        if not self.enabled:
            return []
        cache_k = cache_key(f"/fanart/{media_type}/{external_id}")
        data = self._cache.get(cache_k)
        if data is None:
            self.limiter.wait()
            resp = self._http.get(self._endpoint(media_type, external_id), params={"api_key": self.api_key})
            if resp.status_code != 200:
                logger.warning("fanart.tv GET %s -> HTTP %s", self._endpoint(media_type, external_id), resp.status_code)
                return []
            data = resp.json()
            self._cache.set(cache_k, data, TTL)
        keymap = _KIND_KEY.get(media_type, {})
        out: List[Candidate] = []
        for kind in kinds:
            rk = keymap.get(kind)
            if rk is None:
                continue
            for item in data.get(rk) or []:
                url = item.get("url")
                if not url:
                    continue
                out.append(Candidate(kind=kind, source="fanart", ref=url,
                                     lang=item.get("lang"), score=0.5))
        return out

    def close(self) -> None:
        if self._owns_http:
            self._http.close()
        self._cache.close()

    def __enter__(self) -> "FanartSource":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


__all__ = ["FanartSource"]
