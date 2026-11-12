"""SQLite job log for Finmeta runs (SPEC §10, override O5).

Two tables:
  runs     — one row per apply; started_at / finished_at / status / summary
  job_log  — timestamped log entries per run

The database lives at ``config/jobs.db`` by default. Every function takes
an explicit ``db_path`` so tests can point it at a temp file.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from finmeta.sqlite_conn import shared_connection

DEFAULT_DB_PATH = Path("config") / "jobs.db"


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _conn(db_path: Path) -> sqlite3.Connection:
    """The shared WAL connection for ``db_path`` (audit 2026-11-11 "C4").

    Every call site below uses this as ``with _conn(db_path) as conn:`` --
    sqlite3's context manager only commits-or-rolls-back on exit, it never
    closes, so switching this from "open a fresh connection every call" to
    "reuse one shared connection" needed no changes at any call site."""
    return shared_connection(db_path)


#: Columns added to ``runs`` after the table first shipped. Applied by
#: :func:`_migrate` on every open, guarded by PRAGMA table_info -- never a
#: destructive rebuild, so an existing database keeps every row.
_RUNS_ADDED_COLUMNS = (
    # S11a: what a run actually was. Until now the action and scope existed
    # only inside a log line ("plan started scope=lib:Filmer"), so no list of
    # runs could say what any of them did.
    ("action", "TEXT"),      # "plan" | "apply" | "scan"
    ("scope", "TEXT"),       # "lib:<name>" | "item:<id>" | "all"
    ("tier", "INTEGER"),     # scan tier (NULL for plan/apply)
)


def _migrate(conn: sqlite3.Connection) -> None:
    """Add any column ``runs`` is missing. Idempotent."""
    have = {row["name"] for row in conn.execute("PRAGMA table_info(runs)")}
    for name, ddl in _RUNS_ADDED_COLUMNS:
        if name not in have:
            conn.execute(f"ALTER TABLE runs ADD COLUMN {name} {ddl}")


def init_db(db_path: Path = DEFAULT_DB_PATH) -> None:
    """Create tables if they do not already exist, and migrate old ones."""
    with _conn(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS runs (
                run_id       TEXT PRIMARY KEY,
                started_at   TEXT NOT NULL,
                finished_at  TEXT,
                status       TEXT NOT NULL DEFAULT 'running',
                files_written   INTEGER NOT NULL DEFAULT 0,
                files_unchanged INTEGER NOT NULL DEFAULT 0,
                api_calls       INTEGER NOT NULL DEFAULT 0,
                plan_sha256     TEXT
            );
            CREATE TABLE IF NOT EXISTS job_log (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id  TEXT NOT NULL,
                ts      TEXT NOT NULL,
                level   TEXT NOT NULL,
                message TEXT NOT NULL,
                FOREIGN KEY (run_id) REFERENCES runs(run_id)
            );
            CREATE INDEX IF NOT EXISTS idx_job_log_run
                ON job_log(run_id, level);
            """
        )
        _migrate(conn)


def start_run(db_path: Path, run_id: str, plan_sha256: str = "",
              action: Optional[str] = None, scope: Optional[str] = None,
              tier: Optional[int] = None) -> None:
    """Record the start of a run.

    Idempotent per run_id (INSERT ... ON CONFLICT DO UPDATE): an apply worker
    records the run up front and :func:`apply_plan` re-records it with the real
    plan sha; both rows must be the same row, never a UNIQUE violation.

    ``action``/``scope`` (S11a) are COALESCEd on conflict: the second caller
    (``apply_plan``) does not know them and must not blank out what the worker
    already recorded."""
    with _conn(db_path) as conn:
        conn.execute(
            "INSERT INTO runs (run_id, started_at, status, plan_sha256, action,"
            " scope, tier) VALUES (?, ?, 'running', ?, ?, ?, ?) "
            "ON CONFLICT(run_id) DO UPDATE SET "
            "started_at=excluded.started_at, status='running', "
            "plan_sha256=excluded.plan_sha256, "
            "action=COALESCE(excluded.action, runs.action), "
            "scope=COALESCE(excluded.scope, runs.scope), "
            "tier=COALESCE(excluded.tier, runs.tier)",
            (run_id, _utcnow(), plan_sha256, action, scope, tier),
        )


def finish_run(
    db_path: Path,
    run_id: str,
    status: str,
    *,
    files_written: int = 0,
    files_unchanged: int = 0,
    api_calls: int = 0,
) -> None:
    """Record the end of a run."""
    with _conn(db_path) as conn:
        conn.execute(
            """UPDATE runs SET finished_at=?, status=?, files_written=?,
               files_unchanged=?, api_calls=? WHERE run_id=?""",
            (_utcnow(), status, files_written, files_unchanged, api_calls, run_id),
        )


def log(db_path: Path, run_id: str, level: str, message: str) -> None:
    """Append one log entry."""
    with _conn(db_path) as conn:
        conn.execute(
            "INSERT INTO job_log (run_id, ts, level, message) VALUES (?, ?, ?, ?)",
            (run_id, _utcnow(), level, message),
        )


def log_many(db_path: Path, run_id: str, entries: Sequence[Tuple[str, str, str]]) -> None:
    """Append several log entries in one transaction (audit 2026-11-11
    "C4"): the executor used to call :func:`log` once per file written --
    one INSERT+COMMIT each -- so a 1000-file apply was 1000 of them just
    for the routine "wrote ..." line. ``entries`` is ``(ts, level,
    message)`` tuples, timestamped by the CALLER at the moment each line
    was generated (not here), so batching the commit never shifts what a
    line's own ``ts`` says actually happened. A no-op for an empty
    sequence (the common case: most items write without needing any of
    this)."""
    if not entries:
        return
    with _conn(db_path) as conn:
        conn.executemany(
            "INSERT INTO job_log (run_id, ts, level, message) VALUES (?, ?, ?, ?)",
            [(run_id, ts, level, message) for ts, level, message in entries],
        )


def list_runs(db_path: Path) -> List[Dict[str, Any]]:
    """Return all runs, newest first."""
    try:
        with _conn(db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM runs ORDER BY rowid DESC"
            ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [dict(r) for r in rows]


def list_runs_page(
    db_path: Path, *, limit: int = 50, offset: int = 0,
    status: Optional[str] = None, action: Optional[str] = None,
) -> tuple:
    """``(rows, total)`` of runs, newest first, paged and filtered (S11a).

    :func:`list_runs` returns EVERY row and is kept for its existing callers
    (the log export caps itself separately). A long-lived install has
    thousands of runs, so the Activity Center reads through this instead.
    """
    init_db(db_path)
    where, args = [], []
    if status:
        where.append("status = ?")
        args.append(status)
    if action:
        where.append("action = ?")
        args.append(action)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    with _conn(db_path) as conn:
        # clause is built from hardcoded "status = ?"/"action = ?" strings; the
        # actual values are bound params in args, never interpolated.
        total = conn.execute(
            f"SELECT COUNT(*) n FROM runs {clause}", args).fetchone()["n"]  # noqa: S608
        rows = conn.execute(
            f"SELECT * FROM runs {clause} ORDER BY started_at DESC, rowid DESC"  # noqa: S608
            " LIMIT ? OFFSET ?",
            [*args, int(limit), int(offset)],
        ).fetchall()
    return [dict(r) for r in rows], total


def run_log_summary(db_path: Path, run_ids: Sequence[str]) -> Dict[str, Dict[str, Any]]:
    """``{run_id: {errors, warnings, last_error}}`` for several runs at once.

    One grouped query plus one for the newest error line per run, rather than
    opening each run's log: a failure has to be visible in the list itself,
    and N queries per page would make that cost real money on a long history.
    """
    ids = [i for i in run_ids if i]
    out: Dict[str, Dict[str, Any]] = {
        i: {"errors": 0, "warnings": 0, "last_error": None} for i in ids}
    if not ids:
        return out
    init_db(db_path)
    marks = ",".join("?" for _ in ids)
    with _conn(db_path) as conn:
        # marks is a generated "?" placeholder list sized by len(ids); ids are
        # bound params, never interpolated.
        for row in conn.execute(
            f"SELECT run_id, level, COUNT(*) n FROM job_log"  # noqa: S608
            f" WHERE run_id IN ({marks}) GROUP BY run_id, level", ids,
        ):
            entry = out.get(row["run_id"])
            if entry is None:
                continue
            if row["level"] == "error":
                entry["errors"] = row["n"]
            elif row["level"] == "warn":
                entry["warnings"] = row["n"]
        # same marks as above, same reason.
        for row in conn.execute(
            f"SELECT run_id, message, MAX(id) FROM job_log"  # noqa: S608
            f" WHERE run_id IN ({marks}) AND level = 'error' GROUP BY run_id", ids,
        ):
            entry = out.get(row["run_id"])
            if entry is not None:
                entry["last_error"] = row["message"]
    return out


def get_run(db_path: Path, run_id: str) -> Optional[Dict[str, Any]]:
    try:
        with _conn(db_path) as conn:
            row = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    except sqlite3.OperationalError:
        return None
    return dict(row) if row else None


def get_job_log(db_path: Path, run_id: str) -> List[Dict[str, Any]]:
    try:
        with _conn(db_path) as conn:
            rows = conn.execute(
                "SELECT ts, level, message FROM job_log WHERE run_id=? ORDER BY id",
                (run_id,),
            ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [dict(r) for r in rows]


__all__ = [
    "DEFAULT_DB_PATH", "init_db", "start_run", "finish_run",
    "list_runs_page", "run_log_summary",
    "log", "log_many", "list_runs", "get_run", "get_job_log",
]
