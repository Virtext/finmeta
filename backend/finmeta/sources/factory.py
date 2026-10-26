"""Build source objects from a Config, resolving API keys from the env."""
from __future__ import annotations

import os
from typing import Dict, Optional

from finmeta.config.models import Config
from finmeta.config.loading import ConfigError
from finmeta.config.secretspec import resolve_secret
from finmeta.sources.fanart import FanartSource
from finmeta.sources.local import LocalAssetSource
from finmeta.sources.mdblist import MdblistClient
from finmeta.sources.omdb import OMDbClient
from finmeta.sources.tmdb import TmdbClient


def build_sources(cfg: Config, env: Optional[Dict[str, str]] = None,
                  cache_path: str = "config/cache.db", secrets=None) -> Dict[str, object]:
    """Return ``{source_name: source_obj}`` for the enabled sources.

    Keys are resolved via :func:`resolve_secret` (env var first, then the
    Settings store) so a key saved in Settings works without an env var. TMDB
    is required (raises if neither env nor store has its key). fanart is only
    active when a key is present, otherwise it logs a skip and yields nothing.
    omdb and mdblist are included only when enabled AND their key resolves,
    otherwise they are simply absent from the result (no crash).
    ``secrets`` is an optional :class:`SecretsStore`; ``env`` is an injectable
    environment mapping for tests.
    """
    env = os.environ if env is None else env
    sources: Dict[str, object] = {}
    if cfg.sources.tmdb.enabled:
        tmdb_key = resolve_secret(cfg, secrets, "tmdb_key", env=env)
        if not tmdb_key:
            raise ConfigError(
                f"TMDB key is required (env {cfg.sources.tmdb.api_key_env} or the "
                f"'tmdb_key' secret) but is not set")
        sources["tmdb"] = TmdbClient(api_key=tmdb_key, cache_path=cache_path)
    if cfg.sources.fanart.enabled:
        fanart_key = resolve_secret(cfg, secrets, "fanart_key", env=env)
        sources["fanart"] = FanartSource(api_key=fanart_key, cache_path=cache_path)
    if cfg.sources.local.enabled:
        sources["local"] = LocalAssetSource(cfg.sources.local.path)
    if cfg.sources.omdb.enabled:
        omdb_key = resolve_secret(cfg, secrets, "omdb_key", env=env)
        if omdb_key:
            sources["omdb"] = OMDbClient(api_key=omdb_key, cache_path=cache_path)
    if cfg.sources.mdblist.enabled:
        mdblist_key = resolve_secret(cfg, secrets, "mdblist_key", env=env)
        if mdblist_key:
            sources["mdblist"] = MdblistClient(api_key=mdblist_key, cache_path=cache_path)
    return sources


__all__ = ["build_sources"]
