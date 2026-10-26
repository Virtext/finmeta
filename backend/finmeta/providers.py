"""Provider registry (S9 §C) — the single source for all providers.

Every source is optional and user-selectable. ``build_providers(ctx)``
returns a list of :class:`Provider` objects; ``get_provider(ctx, name)``
returns a single one by name.

Provider help text (``short`` / ``long``) lives here so ``help.py`` can
build the help registry from the same definitions — single source of truth.

``_default_service_test`` in ``routes_settings.py`` dispatches to
``provider.test(ctx)``.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------


@dataclass
class Capabilities:
    """What a provider can supply."""
    ratings: List[str] = field(default_factory=list)
    metadata_fields: List[str] = field(default_factory=list)
    image_kinds: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


@dataclass
class Provider:
    name: str
    label: str
    kind: str                      # "local" | "external"
    requires_key: bool = False
    capabilities: Capabilities = field(default_factory=Capabilities)
    rate_limit: Optional[str] = None
    short: str = ""
    long: str = ""
    secret: Optional[str] = None   # secret name (e.g. "tmdb_key")
    env_var: Optional[str] = None  # env var name (e.g. "TMDB_KEY")
    get_key_url: Optional[str] = None
    enabled: bool = False
    coming_later: bool = False
    test: Optional[Callable] = None  # callable(ctx) -> dict

    def view(self) -> Dict[str, Any]:
        """JSON-safe dict for ``GET /api/providers``."""
        return {
            "name": self.name,
            "label": self.label,
            "kind": self.kind,
            "requires_key": self.requires_key,
            "capabilities": {
                "ratings": self.capabilities.ratings,
                "metadata_fields": self.capabilities.metadata_fields,
                "image_kinds": self.capabilities.image_kinds,
            },
            "rate_limit": self.rate_limit,
            "short": self.short,
            "long": self.long,
            "secret": self.secret,
            "env_var": self.env_var,
            "get_key_url": self.get_key_url,
            "enabled": self.enabled,
            "coming_later": self.coming_later,
        }


# ---------------------------------------------------------------------------
# test() callables
# ---------------------------------------------------------------------------


def _time_get(url: str, *, timeout: float = 8.0, **kw):
    import httpx
    t0 = time.time()
    r = httpx.get(url, timeout=timeout, **kw)
    return r, int((time.time() - t0) * 1000)


def _test_local(ctx) -> Dict[str, Any]:
    """local.test: check the configured asset path exists."""
    path = ctx.config.sources.local.path
    ok = os.path.isdir(path)
    return {
        "ok": ok,
        "latency_ms": 0,
        "version": None,
        "error": None if ok else f"path {path!r} does not exist",
    }


def _test_tmdb(ctx) -> Dict[str, Any]:
    key = ctx.resolve_secret("tmdb_key")
    if not key:
        return {"ok": False, "latency_ms": None, "version": None,
                "error": "no TMDb API key set"}
    try:
        r, ms = _time_get("https://api.themoviedb.org/3/configuration",
                          params={"api_key": key})
        if r.status_code == 200:
            return {"ok": True, "latency_ms": ms, "version": "tmdb", "error": None}
        return {"ok": False, "latency_ms": ms, "version": None,
                "error": f"HTTP {r.status_code}"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "latency_ms": None, "version": None,
                "error": ctx.redact(str(exc))}


def _test_fanart(ctx) -> Dict[str, Any]:
    key = ctx.resolve_secret("fanart_key")
    if not key:
        return {"ok": False, "latency_ms": None, "version": None,
                "error": "no fanart.tv API key set"}
    try:
        # fanart.tv has no bare "/v3/movies" endpoint -- every lookup needs an
        # id. Fight Club (TMDb 550) is used purely as a fixed, always-present
        # id to validate the key, same pattern as _test_omdb's fixed title.
        r, ms = _time_get("https://webservice.fanart.tv/v3/movies/550",
                          params={"api_key": key})
        if r.status_code == 200:
            return {"ok": True, "latency_ms": ms, "version": "fanart", "error": None}
        return {"ok": False, "latency_ms": ms, "version": None,
                "error": f"HTTP {r.status_code}"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "latency_ms": None, "version": None,
                "error": ctx.redact(str(exc))}


# ---------------------------------------------------------------------------
# Static provider definitions (for help.py — no ctx needed)
# ---------------------------------------------------------------------------

_TMDB_DEF = dict(
    label="TMDb",
    kind="external",
    requires_key=True,
    capabilities=Capabilities(
        ratings=["tmdb"],
        metadata_fields=["title", "originaltitle", "overview", "tagline",
                         "year", "genres", "studios"],
        image_kinds=["poster", "backdrop", "logo"],
    ),
    rate_limit="~20 req/s; responses are cached",
    short="TMDb: metadata, artwork and TMDb rating",
    long=(
        "The Movie Database (TMDb) — metadata (title, overview, genres, ...), "
        "most artwork, and the TMDb audience rating. Enabled by default; it is "
        "Finmeta's main external source. Requires an API key (the `tmdb_key` "
        "secret). Rate limit is ~20 requests/second; responses are cached. "
        "Get a key at https://www.themoviedb.org/settings/api."
    ),
    secret="tmdb_key",
    env_var="TMDB_KEY",
    get_key_url="https://www.themoviedb.org/settings/api",
)

_FANART_DEF = dict(
    label="fanart.tv",
    kind="external",
    requires_key=True,
    capabilities=Capabilities(
        ratings=[],
        metadata_fields=[],
        image_kinds=["logo", "landscape", "clear_logo", "banner"],
    ),
    rate_limit="responses are cached",
    short="fanart.tv: extra logos and artwork",
    long=(
        "fanart.tv — additional artwork (logos, landscape, clear logos, "
        "banners). Disabled by default. Requires an API key (the `fanart_key` "
        "secret). Rate limits are looser than TMDb; responses are cached. "
        "Get a key at https://www.fanart.tv/user/apikey."
    ),
    secret="fanart_key",
    env_var="FANART_KEY",
    get_key_url="https://www.fanart.tv/user/apikey",
)

_LOCAL_DEF = dict(
    label="Local assets",
    kind="local",
    requires_key=False,
    capabilities=Capabilities(
        ratings=[],
        metadata_fields=[],
        image_kinds=["poster", "backdrop", "logo", "landscape", "theme_song"],
    ),
    rate_limit=None,
    short="Local asset directory (your own artwork)",
    long=(
        "Artwork files you place on disk yourself (custom overlays, theme "
        "songs, hand-made logos). Disabled by default. No key required — it "
        "reads from the configured `local.path`. Useful when you want full "
        "control over a specific image."
    ),
    secret=None,
    env_var=None,
    get_key_url=None,
)

_JELLYFIN_DEF = dict(
    label="Jellyfin",
    kind="local",
    requires_key=True,
    capabilities=Capabilities(
        ratings=["imdb", "rt_critic"],
        metadata_fields=[],
        image_kinds=["poster", "backdrop"],
    ),
    rate_limit=None,
    short="Jellyfin's own data (no key)",
    long=(
        "Jellyfin's own stored metadata: CommunityRating, CriticRating and "
        "OfficialRating, plus the item's existing images. No API key required "
        "— it is Finmeta's default source and the fallback when an external "
        "source has no value. This is a local service (it uses the Jellyfin "
        "server connection and API key), shown in the Local services tab."
    ),
    secret="jellyfin_api_key",
    env_var=None,  # env var name comes from cfg.server.api_key_env
    get_key_url=None,
)

_OMDB_DEF = dict(
    label="OMDb",
    kind="external",
    requires_key=True,
    capabilities=Capabilities(
        ratings=["imdb", "rt_critic", "metacritic"],
        metadata_fields=[],
        image_kinds=[],
    ),
    rate_limit="responses are cached (7 day TTL)",
    short="OMDb: IMDb, Rotten Tomatoes and Metacritic ratings",
    long=(
        "OMDb API — IMDb rating, Rotten Tomatoes critic score, and Metacritic "
        "score. Disabled by default. Requires an API key (the `omdb_key` "
        "secret; env var OMDB_KEY). Rate limit is 1000 requests/day; responses "
        "are cached with a 7 day TTL. Get a free key at "
        "https://www.omdbapi.com/apikey.aspx."
    ),
    secret="omdb_key",
    env_var="OMDB_KEY",
    get_key_url="https://www.omdbapi.com/apikey.aspx",
)


_MDBLIST_DEF = dict(
    label="MDBList",
    kind="external",
    requires_key=True,
    capabilities=Capabilities(
        ratings=["imdb", "rt_critic", "rt_audience", "metacritic",
                 "tmdb", "trakt", "letterboxd"],
        metadata_fields=[],
        image_kinds=[],
    ),
    rate_limit="1,000 requests/day (free); responses cached 7 days; rate-limit aware",
    short="MDBList: one call for IMDb, Rotten Tomatoes, Metacritic, Trakt, TMDb and Letterboxd ratings",
    long=(
        "MDBList aggregates IMDb, Rotten Tomatoes (critic + audience), Metacritic, Trakt, "
        "TMDb and Letterboxd ratings in a single request. Disabled by default. Requires an "
        "API key (the `mdblist_key` secret; env var MDBLIST_KEY). Free tier is 1,000 "
        "requests/day; responses are cached with a 7 day TTL and the client is rate-limit "
        "aware. Get a key at https://api.mdblist.com."
    ),
    secret="mdblist_key",
    env_var="MDBLIST_KEY",
    get_key_url="https://api.mdblist.com",
)


_IMDB_DATASETS_DEF = dict(
    label="IMDb datasets",
    kind="external",
    requires_key=False,
    capabilities=Capabilities(
        ratings=["imdb", "imdb_votes"],
        metadata_fields=[],
        image_kinds=[],
    ),
    rate_limit="downloads at most once per 24h; no per-item requests",
    short="IMDb bulk dataset: IMDb rating + vote count, no API key",
    long=(
        "IMDb's public `title.ratings` dataset (bulk TSV, downloaded at most once per 24h) "
        "indexed by tconst. Provides the IMDb rating and vote count with no API key. "
        "Disabled by default. Note: this is IMDb's non-commercial dataset — free to use but "
        "not for commercial purposes. See the IMDb datasets help for the licence."
    ),
    secret=None,
    env_var=None,
    get_key_url=None,
)


def _test_jellyfin(ctx) -> Dict[str, Any]:
    from finmeta.jellyfin_client import JellyfinClient
    from finmeta.jellyfin_client.client import normalize_base_path
    key = ctx.resolve_secret("jellyfin_api_key")
    if not key:
        return {"ok": False, "latency_ms": None, "version": None,
                "error": "no Jellyfin API key set"}
    server = ctx.config.server
    base = (server.url or "").rstrip("/") + normalize_base_path(server.base_path)
    try:
        r, ms = _time_get(
            base + "/System/Info",
            headers={"Authorization": JellyfinClient.auth_header(key)},
            verify=server.verify_ssl,
            timeout=server.timeout,
        )
        if r.status_code == 200:
            return {"ok": True, "latency_ms": ms,
                    "version": r.json().get("Version"), "error": None}
        return {"ok": False, "latency_ms": ms, "version": None,
                "error": f"HTTP {r.status_code}"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "latency_ms": None, "version": None,
                "error": ctx.redact(str(exc))}


def _test_omdb(ctx) -> Dict[str, Any]:
    key = ctx.resolve_secret("omdb_key")
    if not key:
        return {"ok": False, "latency_ms": None, "version": None,
                "error": "no OMDb API key set"}
    try:
        r, ms = _time_get("https://www.omdbapi.com/",
                          params={"apikey": key, "t": "The Shawshank Redemption",
                                  "y": "1994"})
        data = r.json() if r.content else {}
        if r.status_code == 200 and data.get("Response") == "True":
            return {"ok": True, "latency_ms": ms, "version": "omdb", "error": None}
        err = data.get("Response", f"HTTP {r.status_code}")
        return {"ok": False, "latency_ms": ms, "version": None, "error": err}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "latency_ms": None, "version": None,
                "error": ctx.redact(str(exc))}


def _test_mdblist(ctx) -> Dict[str, Any]:
    key = ctx.resolve_secret("mdblist_key")
    if not key:
        return {"ok": False, "latency_ms": None, "version": None,
                "error": "no MDBList API key set"}
    try:
        r, ms = _time_get("https://api.mdblist.com/", params={"apikey": key})
        if r.status_code == 200:
            return {"ok": True, "latency_ms": ms, "version": "mdblist", "error": None}
        return {"ok": False, "latency_ms": ms, "version": None,
                "error": f"HTTP {r.status_code}"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "latency_ms": None, "version": None,
                "error": ctx.redact(str(exc))}


def _test_imdb_datasets(ctx) -> Dict[str, Any]:
    # Lightweight connectivity check (HEAD) against the bulk dataset host.
    import httpx
    url = "https://datasets.imdbws.com/title.ratings.tsv.gz"
    try:
        t0 = time.time()
        r = httpx.head(url, timeout=8.0, follow_redirects=True)
        ms = int((time.time() - t0) * 1000)
        if r.status_code in (200, 206):
            return {"ok": True, "latency_ms": ms,
                    "version": "imdb-datasets", "error": None}
        return {"ok": False, "latency_ms": ms, "version": None,
                "error": f"HTTP {r.status_code}"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "latency_ms": None, "version": None,
                "error": ctx.redact(str(exc))}


# ---------------------------------------------------------------------------
# build_providers / get_provider
# ---------------------------------------------------------------------------

_STATIC_DEFS: Dict[str, Dict[str, Any]] = {
    "jellyfin": _JELLYFIN_DEF,
    "tmdb": _TMDB_DEF,
    "fanart": _FANART_DEF,
    "local": _LOCAL_DEF,
    "omdb": _OMDB_DEF,
    "mdblist": _MDBLIST_DEF,
    "imdb_datasets": _IMDB_DATASETS_DEF,
}

_TESTS: Dict[str, Callable] = {
    "jellyfin": _test_jellyfin,
    "tmdb": _test_tmdb,
    "fanart": _test_fanart,
    "local": _test_local,
    "omdb": _test_omdb,
    "mdblist": _test_mdblist,
    "imdb_datasets": _test_imdb_datasets,
}


def build_providers(ctx) -> List[Provider]:
    """Build all registered providers with their enabled state and test fn."""
    out: List[Provider] = []
    for name, d in _STATIC_DEFS.items():
        cfg = ctx.config
        # determine enabled state
        if name == "jellyfin":
            enabled = True  # always on when there is a connection
        elif name == "tmdb":
            enabled = cfg.sources.tmdb.enabled
        elif name == "fanart":
            enabled = cfg.sources.fanart.enabled
        elif name == "local":
            enabled = cfg.sources.local.enabled
        elif name == "omdb":
            enabled = cfg.sources.omdb.enabled and bool(ctx.resolve_secret("omdb_key"))
        elif name == "mdblist":
            enabled = cfg.sources.mdblist.enabled and bool(ctx.resolve_secret("mdblist_key"))
        elif name == "imdb_datasets":
            enabled = cfg.sources.imdb_datasets.enabled
        else:
            enabled = False

        env_var = d["env_var"]
        if name == "jellyfin":
            env_var = cfg.server.api_key_env

        out.append(Provider(
            name=name,
            label=d["label"],
            kind=d["kind"],
            requires_key=d["requires_key"],
            capabilities=d["capabilities"],
            rate_limit=d["rate_limit"],
            short=d["short"],
            long=d["long"],
            secret=d["secret"],
            env_var=env_var,
            get_key_url=d["get_key_url"],
            enabled=enabled,
            coming_later=d.get("coming_later", False),
            test=_TESTS.get(name),
        ))
    return out


def get_provider(ctx, name: str) -> Optional[Provider]:
    for p in build_providers(ctx):
        if p.name == name:
            return p
    return None


def provider_help_entries() -> Dict[str, Dict[str, str]]:
    """Return ``{name: {short, long}}`` for help.py — no ctx needed."""
    return {
        name: {"short": d["short"], "long": d["long"]}
        for name, d in _STATIC_DEFS.items()
    }


RATING_FIELD_LABELS: Dict[str, str] = {
    "rating_imdb": "IMDb",
    "rating_rt_critic": "Rotten Tomatoes (critic)",
    "rating_rt_audience": "Rotten Tomatoes (audience)",
    "rating_metacritic": "Metacritic",
    "rating_tmdb": "TMDb",
    "rating_trakt": "Trakt",
    "rating_letterboxd": "Letterboxd",
}


def rating_source_matrix(ctx) -> Dict[str, Any]:
    """For each rating field, the ordered list of capable providers.

    Providers come from ``field_sources`` order, filtered to those that can
    actually supply the field (per their ``capabilities.ratings``). This is the
    data behind the "Rating sources" tab.
    """
    cfg = ctx.config
    fs = cfg.sources.field_sources
    providers = {p.name: p for p in build_providers(ctx)}
    fields: List[Dict[str, Any]] = []
    for field_name, label in RATING_FIELD_LABELS.items():
        key = field_name.replace("rating_", "")
        order = fs.get(field_name, [])
        capable = [n for n in order
                   if n in providers and key in providers[n].capabilities.ratings]
        fields.append({"name": field_name, "label": label, "providers": capable})
    return {"fields": fields}


__all__ = [
    "Capabilities", "Provider",
    "build_providers", "get_provider", "provider_help_entries",
    "RATING_FIELD_LABELS", "rating_source_matrix",
]
