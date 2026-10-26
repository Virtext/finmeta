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
from typing import Any, Dict, List, Optional

DEFAULT_DB_PATH = Path("config") / "jobs.db"


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _conn(db_path: Path) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: Path = DEFAULT_DB_PATH) -> None:
    """Create tables if they do not already exist."""
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
            """
        )


def start_run(db_path: Path, run_id: str, plan_sha256: str = "") -> None:
    """Record the start of a run.

    Idempotent per run_id (INSERT ... ON CONFLICT DO UPDATE): an apply worker
    records the run up front and :func:`apply_plan` re-records it with the real
    plan sha; both rows must be the same row, never a UNIQUE violation."""
    with _conn(db_path) as conn:
        conn.execute(
            "INSERT INTO runs (run_id, started_at, status, plan_sha256) VALUES (?, ?, 'running', ?) "
            "ON CONFLICT(run_id) DO UPDATE SET "
            "started_at=excluded.started_at, status='running', plan_sha256=excluded.plan_sha256",
            (run_id, _utcnow(), plan_sha256),
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
    "log", "list_runs", "get_run", "get_job_log",
]
