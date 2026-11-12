"""TTL-backed SQLite cache for remote-source responses (SPEC §4).

Stores serialized JSON payloads keyed by an opaque string, with a
per-entry expiry. Used by the TMDb client to cache details (7 day TTL)
and images (30 day TTL).
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Optional

from finmeta.sqlite_conn import shared_connection


class SqliteCache:
    """A tiny key -> JSON cache with per-entry TTL, backed by SQLite."""

    def __init__(self, path):
        self.path = Path(path)
        # Audit 2026-11-11 "C4": one shared WAL connection per resolved
        # path, reused across every SqliteCache instance opened against it
        # -- the TMDb/fanart/OMDb/MDBList clients each construct a fresh
        # instance per plan build or apply, previously each with its own
        # one-off connection with neither WAL nor a busy timeout.
        self._conn = shared_connection(self.path)
        # Each statement + its commit must be ONE `with` block (audit
        # 2026-11-11 "C4"): the lock the shared connection wraps is held
        # for the whole block, which is what keeps another thread from
        # writing in between this statement and its own commit.
        with self._conn as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS cache ("
                " key TEXT PRIMARY KEY,"
                " payload BLOB NOT NULL,"
                " expires_at REAL NOT NULL)"
            )

    def get(self, key: str) -> Optional[Any]:
        """Return the cached value or None if missing/expired."""
        with self._conn as conn:
            row = conn.execute(
                "SELECT payload, expires_at FROM cache WHERE key = ?", (key,)
            ).fetchone()
            if row is None:
                return None
            payload, expires_at = row
            if expires_at < time.time():
                conn.execute("DELETE FROM cache WHERE key = ?", (key,))
                return None
        return json.loads(payload)

    def set(self, key: str, value: Any, ttl: float) -> None:
        with self._conn as conn:
            conn.execute(
                "INSERT OR REPLACE INTO cache (key, payload, expires_at) VALUES (?, ?, ?)",
                (key, json.dumps(value).encode("utf-8"), time.time() + ttl),
            )

    def delete(self, key: str) -> None:
        with self._conn as conn:
            conn.execute("DELETE FROM cache WHERE key = ?", (key,))

    def clear(self) -> None:
        with self._conn as conn:
            conn.execute("DELETE FROM cache")

    def close(self) -> None:
        """No-op (audit 2026-11-11 "C4"): ``self._conn`` is the shared
        connection for this path, not one this instance owns -- every
        metadata-source client constructs a fresh ``SqliteCache`` and
        closes it at the end of EVERY plan build/apply (see
        ``TmdbClient.close`` and friends), so actually closing it here
        would kill the connection the very next client needs. Closing the
        shared connection itself is ``finmeta.sqlite_conn``'s job (LRU
        eviction only)."""

    def __enter__(self) -> "SqliteCache":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def cache_key(path: str, params: Optional[dict] = None) -> str:
    """Stable key for a (path, params) request tuple."""
    if not params:
        return path
    normalized = {k: v for k, v in params.items() if v is not None and v != ""}
    return path + "?" + "&".join(f"{k}={normalized[k]}" for k in sorted(normalized))


__all__ = ["SqliteCache", "cache_key"]
