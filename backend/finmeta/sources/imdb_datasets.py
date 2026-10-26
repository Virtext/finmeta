"""IMDb bulk-dataset ratings source (S9b2).

Downloads IMDb's public ``title.ratings.tsv.gz`` (from
``https://datasets.imdbws.com``) at most once per 24h into the cache dir,
indexes it in SQLite by ``tconst``, and returns the IMDb rating + vote count.
No API key required.

``get_ratings(imdb_id)`` returns ``{"imdb": <float|None>,
"imdb_votes": <int|None>}`` — ``imdb`` feeds ``rating_imdb`` (via
``field_sources``) and ``imdb_votes`` is surfaced as an extra value.
"""
from __future__ import annotations

import gzip
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

import httpx

DEFAULT_DATASET_URL = "https://datasets.imdbws.com/title.ratings.tsv.gz"
TTL = 24 * 3600  # 24 hours


class ImdbDatasetsError(RuntimeError):
    """Raised for an unexpected IMDb-dataset failure."""


def _parse_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return round(float(value), 1)
    except (ValueError, TypeError):
        return None


def _parse_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


class ImdbDatasetsClient:
    """IMDb ratings/votes from the bulk ``title.ratings`` dataset."""

    def __init__(self, cache_dir: str = "config/cache",
                 dataset_url: str = DEFAULT_DATASET_URL,
                 ttl: float = TTL,
                 http: Optional[Callable[[str], bytes]] = None,
                 now: Callable[[], float] = time.time) -> None:
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._gz_path = self.cache_dir / "title.ratings.tsv.gz"
        self._db_path = self.cache_dir / "imdb_datasets.db"
        self.dataset_url = dataset_url
        self.ttl = float(ttl)
        self._http = http
        self._now = now
        self._ensure_downloaded()
        self._ensure_indexed()

    # -- download -----------------------------------------------------------
    def _needs_download(self) -> bool:
        if not self._gz_path.exists():
            return True
        age = self._now() - self._gz_path.stat().st_mtime
        return age >= self.ttl

    def _ensure_downloaded(self) -> None:
        if not self._needs_download():
            return
        if self._http is not None:
            data = self._http(self.dataset_url)
        else:
            r = httpx.get(self.dataset_url, timeout=120.0, follow_redirects=True)
            r.raise_for_status()
            data = r.content
        self._gz_path.write_bytes(data)

    # -- index --------------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self._db_path))

    def _index_row_count(self) -> int:
        if not self._db_path.exists():
            return 0
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='imdb_ratings'"
            ).fetchone()
            if not row or row[0] == 0:
                return 0
            return conn.execute("SELECT COUNT(*) FROM imdb_ratings").fetchone()[0]
        finally:
            conn.close()

    def _ensure_indexed(self) -> None:
        if not self._gz_path.exists():
            return
        rebuild = self._index_row_count() == 0
        if not rebuild and self._db_path.exists():
            if self._gz_path.stat().st_mtime > self._db_path.stat().st_mtime:
                rebuild = True
        if rebuild:
            self._reindex()

    def _reindex(self) -> None:
        conn = self._connect()
        try:
            conn.execute("DROP TABLE IF EXISTS imdb_ratings")
            conn.execute(
                "CREATE TABLE imdb_ratings ("
                " tconst TEXT PRIMARY KEY, avg REAL, num_votes INTEGER)"
            )
            rows = []
            with gzip.open(self._gz_path, "rt", encoding="utf-8", errors="replace") as fh:
                fh.readline()   # consume the header: tconst/averageRating/numVotes
                for line in fh:
                    parts = line.rstrip("\n").split("\t")
                    if len(parts) < 3:
                        continue
                    rows.append((parts[0], _parse_float(parts[1]), _parse_int(parts[2])))
            conn.executemany(
                "INSERT OR REPLACE INTO imdb_ratings (tconst, avg, num_votes) VALUES (?, ?, ?)",
                rows,
            )
            conn.commit()
        finally:
            conn.close()

    # -- public -------------------------------------------------------------
    def get_ratings(self, imdb_id: str) -> Dict[str, Optional[float]]:
        """Return ``{"imdb": <float|None>, "imdb_votes": <int|None>}``."""
        if not imdb_id:
            return {"imdb": None, "imdb_votes": None}
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT avg, num_votes FROM imdb_ratings WHERE tconst = ?",
                (imdb_id,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return {"imdb": None, "imdb_votes": None}
        return {"imdb": row[0], "imdb_votes": row[1]}

    def close(self) -> None:
        pass

    def __enter__(self) -> "ImdbDatasetsClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


__all__ = ["ImdbDatasetsClient", "ImdbDatasetsError", "DEFAULT_DATASET_URL"]
