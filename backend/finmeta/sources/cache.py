"""TTL-backed SQLite cache for remote-source responses (SPEC §4).

Stores serialized JSON payloads keyed by an opaque string, with a
per-entry expiry. Used by the TMDb client to cache details (7 day TTL)
and images (30 day TTL).
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional


class SqliteCache:
    """A tiny key -> JSON cache with per-entry TTL, backed by SQLite."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path))
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS cache ("
            " key TEXT PRIMARY KEY,"
            " payload BLOB NOT NULL,"
            " expires_at REAL NOT NULL)"
        )
        self._conn.commit()

    def get(self, key: str) -> Optional[Any]:
        """Return the cached value or None if missing/expired."""
        row = self._conn.execute(
            "SELECT payload, expires_at FROM cache WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        payload, expires_at = row
        if expires_at < time.time():
            self._conn.execute("DELETE FROM cache WHERE key = ?", (key,))
            self._conn.commit()
            return None
        return json.loads(payload)

    def set(self, key: str, value: Any, ttl: float) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO cache (key, payload, expires_at) VALUES (?, ?, ?)",
            (key, json.dumps(value).encode("utf-8"), time.time() + ttl),
        )
        self._conn.commit()

    def delete(self, key: str) -> None:
        self._conn.execute("DELETE FROM cache WHERE key = ?", (key,))
        self._conn.commit()

    def clear(self) -> None:
        self._conn.execute("DELETE FROM cache")
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

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
