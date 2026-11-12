"""finmeta.sqlite_conn.shared_connection (audit 2026-11-11 "C4").

engine.jobs, api.overrides.OverrideStore and sources.cache.SqliteCache each
used to open a brand-new sqlite3 connection per call/request/instance, with
neither WAL nor a busy timeout, against databases several threads write to
concurrently. shared_connection replaces all three with one cached,
WAL-mode, busy-timeout connection per resolved path.
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from finmeta import sqlite_conn
from finmeta.sqlite_conn import shared_connection


@pytest.fixture(autouse=True)
def _clear_cache():
    """Each test gets its own view of the module-level cache -- tests use
    distinct tmp_path databases anyway, but clearing keeps LRU-cap tests
    from being affected by whatever earlier tests already cached."""
    with sqlite_conn._lock:
        for conn in sqlite_conn._cache.values():
            try:
                conn.close()
            except sqlite3.Error:
                pass
        sqlite_conn._cache.clear()
    yield


class TestSharedConnection:
    def test_same_path_returns_the_same_connection_object(self, tmp_path: Path):
        db = tmp_path / "a.db"
        c1 = shared_connection(db)
        c2 = shared_connection(db)
        assert c1 is c2

    def test_different_paths_return_different_connections(self, tmp_path: Path):
        c1 = shared_connection(tmp_path / "a.db")
        c2 = shared_connection(tmp_path / "b.db")
        assert c1 is not c2

    def test_relative_and_absolute_forms_of_the_same_path_share_a_connection(
        self, tmp_path: Path, monkeypatch,
    ):
        monkeypatch.chdir(tmp_path)
        c1 = shared_connection("same.db")
        c2 = shared_connection(tmp_path / "same.db")
        assert c1 is c2

    def test_wal_and_busy_timeout_are_set(self, tmp_path: Path):
        conn = shared_connection(tmp_path / "a.db")
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        assert mode.lower() == "wal"
        assert timeout == 5000

    def test_connection_is_actually_usable(self, tmp_path: Path):
        conn = shared_connection(tmp_path / "a.db")
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.execute("INSERT INTO t VALUES (1)")
        conn.commit()
        assert conn.execute("SELECT x FROM t").fetchone()[0] == 1

    def test_creates_the_parent_directory(self, tmp_path: Path):
        db = tmp_path / "nested" / "deep" / "a.db"
        shared_connection(db)
        assert db.parent.is_dir()

    def test_usable_from_a_different_thread(self, tmp_path: Path):
        """check_same_thread=False is the whole point -- the scheduler
        thread, the run worker and request threads all share this."""
        conn = shared_connection(tmp_path / "a.db")
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.commit()
        errors = []

        def _write():
            try:
                conn.execute("INSERT INTO t VALUES (1)")
                conn.commit()
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        t = threading.Thread(target=_write)
        t.start()
        t.join(timeout=5)
        assert errors == []
        assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 1

    def test_concurrent_with_blocks_from_many_threads_never_corrupt_state(
        self, tmp_path: Path,
    ):
        """Regression: the first version of this fix returned the raw
        sqlite3.Connection straight out of the cache. Two threads both
        doing `with shared_connection(path) as conn: conn.execute(...);`
        -- the pattern every real call site uses -- raced on the
        connection's own transaction bookkeeping and failed live under the
        fast suite with `sqlite3.OperationalError: cannot commit - no
        transaction is active`. The lock that `with` now holds for the
        whole block (not just one call) is what this test exercises:
        every one of N threads' M writes must land, none dropped, no
        exception, regardless of how they interleave."""
        db = tmp_path / "a.db"
        with shared_connection(db) as conn:
            conn.execute("CREATE TABLE t (x INTEGER)")
        errors: list = []
        threads_n, writes_per_thread = 8, 20

        def _writer(n: int):
            try:
                for i in range(writes_per_thread):
                    with shared_connection(db) as conn:
                        conn.execute("INSERT INTO t VALUES (?)", (n * 1000 + i,))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=_writer, args=(n,)) for n in range(threads_n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert errors == []
        with shared_connection(db) as conn:
            count = conn.execute("SELECT COUNT(*) FROM t").fetchone()[0]
        assert count == threads_n * writes_per_thread


class TestLRUEviction:
    def test_oldest_connection_closed_once_cap_exceeded(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(sqlite_conn, "_MAX_CACHED_CONNECTIONS", 2)
        c1 = shared_connection(tmp_path / "a.db")
        shared_connection(tmp_path / "b.db")
        shared_connection(tmp_path / "c.db")  # evicts a.db (least-recently-used)

        with pytest.raises(sqlite3.ProgrammingError):
            c1.execute("SELECT 1")
        assert len(sqlite_conn._cache) == 2

    def test_reaccessing_a_path_protects_it_from_eviction(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(sqlite_conn, "_MAX_CACHED_CONNECTIONS", 2)
        c1 = shared_connection(tmp_path / "a.db")
        shared_connection(tmp_path / "b.db")
        shared_connection(tmp_path / "a.db")  # touch a.db again
        shared_connection(tmp_path / "c.db")  # must evict b.db, not a.db now

        c1.execute("SELECT 1")  # a.db's connection is still open
        assert len(sqlite_conn._cache) == 2

    def test_reopening_an_evicted_path_works(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(sqlite_conn, "_MAX_CACHED_CONNECTIONS", 1)
        shared_connection(tmp_path / "a.db")
        shared_connection(tmp_path / "b.db")  # evicts a.db (cap=1)
        c3 = shared_connection(tmp_path / "a.db")  # reopened fresh, not a dead handle
        c3.execute("SELECT 1")
