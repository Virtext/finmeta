"""Metadata sources: TMDb, Fanart, local asset dir (SPEC §4)."""
from finmeta.sources.candidate import Candidate, Kind, SourceName, KINDS
from finmeta.sources.cache import SqliteCache, cache_key
from finmeta.sources.ranking import rank_candidates, pick_best
from finmeta.sources.imdb_datasets import ImdbDatasetsClient
from finmeta.sources.mdblist import MdblistClient
from finmeta.sources.tmdb import TmdbClient, TmdbError, RateLimiter
from finmeta.sources.local import LocalAssetSource
from finmeta.sources.fanart import FanartSource
from finmeta.sources.factory import build_sources

__all__ = [
    "Candidate", "Kind", "SourceName", "KINDS",
    "SqliteCache", "cache_key",
    "rank_candidates", "pick_best",
    "TmdbClient", "TmdbError", "RateLimiter",
    "MdblistClient", "ImdbDatasetsClient",
    "LocalAssetSource", "FanartSource", "build_sources",
]
