"""Shared, cached SQLite connections (audit 2026-11-11 "C4").

``engine.jobs``, ``api.overrides.OverrideStore`` and ``sources.cache.
SqliteCache`` each used to open a brand-new connection per call / per
request / per instance against a database the scheduler thread, the run
worker and request threads all write to concurrently -- and none of the
three set ``journal_mode=WAL`` or a busy timeout, so sqlite's default
rollback-journal locking (a 5s wait before giving up) made "database is
locked" the ordinary failure mode under write contention, not a wait.
Reconnecting on every call also paid a fresh ``open()`` every time, costly
on the operator's shfs (FUSE) mount -- a 1000-file apply logging one line
per file was ~2000 connect+commit cycles just for that.

``shared_connection`` hands back ONE connection per resolved path, reused
across every later call for that path and across threads.

**Thread safety.** ``check_same_thread=False`` only disables sqlite3's
same-thread CHECK -- it does NOT make a single ``Connection`` object safe
for unsynchronized concurrent use. Two threads interleaving
execute()/commit() on the same connection corrupts the ``sqlite3`` module's
OWN transaction bookkeeping (confirmed live, this first version of the
fix: two apply workers racing on ``jobs.db`` under the fast suite produced
``sqlite3.OperationalError: cannot commit - no transaction is active``).
``_SharedConnection`` wraps the real connection with a per-path
``threading.RLock``: every caller already does ``with _conn(db_path) as
conn: conn.execute(...)`` (or the direct ``self._conn.execute(...)``
equivalent in ``SqliteCache``), so the lock is held for exactly the
granularity that needs to be atomic -- from a block's first write through
its own commit/rollback, nothing else may touch the connection in
between.
"""
from __future__ import annotations

import sqlite3
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Union


class _SharedConnection:
    """Thread-safe wrapper around one shared ``sqlite3.Connection``.

    Two usage styles, both lock-safe:

    - ``with shared_connection(path) as conn: conn.execute(...)`` -- the
      lock is held for the whole block (acquired in ``__enter__``,
      released after an implicit commit/rollback in ``__exit__``), and
      ``conn`` inside the block is the REAL ``sqlite3.Connection`` (so
      every existing call site needed no changes).
    - ``shared_connection(path).execute(...)`` / ``.commit()`` directly --
      each such call acquires the lock for just that one call. Used by
      ``sources.cache.SqliteCache``, which issues its own explicit
      ``execute()`` + ``commit()`` pairs rather than a ``with`` block.

    The lock is an ``RLock`` so a thread already inside a ``with`` block
    (holding the lock) can still call a self-locking method on this same
    wrapper without deadlocking itself -- only a DIFFERENT thread is ever
    made to wait.
    """

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
        self._lock = threading.RLock()

    def __enter__(self) -> sqlite3.Connection:
        self._lock.acquire()
        return self._conn

    def __exit__(self, exc_type, exc, tb) -> bool:
        try:
            if exc_type is None:
                self._conn.commit()
            else:
                self._conn.rollback()
        finally:
            self._lock.release()
        return False

    def execute(self, *args, **kwargs):
        with self._lock:
            return self._conn.execute(*args, **kwargs)

    def executemany(self, *args, **kwargs):
        with self._lock:
            return self._conn.executemany(*args, **kwargs)

    def executescript(self, *args, **kwargs):
        with self._lock:
            return self._conn.executescript(*args, **kwargs)

    def commit(self) -> None:
        with self._lock:
            self._conn.commit()

    def rollback(self) -> None:
        with self._lock:
            self._conn.rollback()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @property
    def row_factory(self):
        return self._conn.row_factory

    @row_factory.setter
    def row_factory(self, value) -> None:
        self._conn.row_factory = value


#: A real deployment only ever touches a handful of distinct database
#: files (jobs.db, overrides.db, one cache db per metadata source). This
#: cap exists so a process that happens to open many distinct paths --
#: chiefly the test suite, which gives nearly every test its own tmp_path
#: database -- doesn't accumulate an unbounded number of open file
#: descriptors. LRU: the least-recently-used connection is closed first.
_MAX_CACHED_CONNECTIONS = 16

_cache: "OrderedDict[str, _SharedConnection]" = OrderedDict()
_lock = threading.Lock()


def shared_connection(path: Union[str, Path]) -> _SharedConnection:
    """Return the shared WAL-mode connection for ``path``, opening one
    (and running its migrations-of-one -- the two pragmas) the first time
    this path is seen."""
    key = str(Path(path).resolve())
    with _lock:
        wrapped = _cache.get(key)
        if wrapped is not None:
            _cache.move_to_end(key)
            return wrapped
        Path(key).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(key, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        wrapped = _SharedConnection(conn)
        _cache[key] = wrapped
        while len(_cache) > _MAX_CACHED_CONNECTIONS:
            _, oldest = _cache.popitem(last=False)
            oldest.close()
        return wrapped


__all__ = ["shared_connection"]
