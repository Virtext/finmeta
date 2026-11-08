"""SPEC_S11 §B1: the runs table gains action/scope/tier without losing rows."""
from __future__ import annotations

import sqlite3

from finmeta.engine.jobs import (
    _RUNS_ADDED_COLUMNS, finish_run, get_run, init_db, list_runs,
    list_runs_page, run_log_summary, log, start_run,
)


def _pre_s11_db(path):
    """The runs/job_log schema exactly as it shipped before S11a."""
    with sqlite3.connect(str(path)) as conn:
        conn.executescript(
            """
            CREATE TABLE runs (
                run_id       TEXT PRIMARY KEY,
                started_at   TEXT NOT NULL,
                finished_at  TEXT,
                status       TEXT NOT NULL DEFAULT 'running',
                files_written   INTEGER NOT NULL DEFAULT 0,
                files_unchanged INTEGER NOT NULL DEFAULT 0,
                api_calls       INTEGER NOT NULL DEFAULT 0,
                plan_sha256     TEXT
            );
            CREATE TABLE job_log (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id  TEXT NOT NULL,
                ts      TEXT NOT NULL,
                level   TEXT NOT NULL,
                message TEXT NOT NULL
            );
            """
        )
        conn.execute(
            "INSERT INTO runs (run_id, started_at, status, files_written)"
            " VALUES ('old-run', '2026-01-01T00:00:00Z', 'ok', 42)")
        conn.execute(
            "INSERT INTO job_log (run_id, ts, level, message)"
            " VALUES ('old-run', '2026-01-01T00:00:00Z', 'info', 'apply ok')")


def test_an_existing_database_is_migrated_and_keeps_its_rows(tmp_path):
    db = tmp_path / "jobs.db"
    _pre_s11_db(db)
    init_db(db)

    row = get_run(db, "old-run")
    assert row is not None
    assert row["files_written"] == 42          # nothing was rebuilt or dropped
    for name, _ddl in _RUNS_ADDED_COLUMNS:
        assert name in row                      # the column exists
        assert row[name] is None                # and is empty for an old row
    assert len(list_runs(db)) == 1


def test_migrating_twice_is_a_no_op(tmp_path):
    db = tmp_path / "jobs.db"
    _pre_s11_db(db)
    init_db(db)
    init_db(db)
    assert get_run(db, "old-run")["files_written"] == 42


def test_action_scope_and_tier_are_recorded(tmp_path):
    db = tmp_path / "jobs.db"
    init_db(db)
    start_run(db, "r1", "", action="scan", scope="lib:Movies", tier=2)
    row = get_run(db, "r1")
    assert (row["action"], row["scope"], row["tier"]) == ("scan", "lib:Movies", 2)


def test_a_second_start_run_does_not_blank_the_action(tmp_path):
    """apply_plan re-records the row with the real plan sha and knows neither
    the action nor the scope -- it must not wipe what the worker set."""
    db = tmp_path / "jobs.db"
    init_db(db)
    start_run(db, "r1", "", action="apply", scope="item:abc")
    start_run(db, "r1", "sha-123")
    row = get_run(db, "r1")
    assert (row["action"], row["scope"]) == ("apply", "item:abc")
    assert row["plan_sha256"] == "sha-123"


def test_list_runs_page_is_paged_filtered_and_newest_first(tmp_path):
    db = tmp_path / "jobs.db"
    init_db(db)
    for i in range(5):
        start_run(db, f"r{i}", "", action="plan" if i % 2 else "apply",
                  scope="all")
        finish_run(db, f"r{i}", "ok" if i else "error")
        # started_at has second resolution; force a deterministic order
        with sqlite3.connect(str(db)) as conn:
            conn.execute("UPDATE runs SET started_at=? WHERE run_id=?",
                         (f"2026-11-0{i + 1}T00:00:00Z", f"r{i}"))

    rows, total = list_runs_page(db, limit=2)
    assert total == 5
    assert [r["run_id"] for r in rows] == ["r4", "r3"]
    rows, _ = list_runs_page(db, limit=2, offset=2)
    assert [r["run_id"] for r in rows] == ["r2", "r1"]
    rows, total = list_runs_page(db, action="apply")
    assert total == 3 and all(r["action"] == "apply" for r in rows)
    rows, total = list_runs_page(db, status="error")
    assert (total, rows[0]["run_id"]) == (1, "r0")


def test_run_log_summary_counts_levels_and_keeps_the_last_error(tmp_path):
    db = tmp_path / "jobs.db"
    init_db(db)
    start_run(db, "r1", "", action="apply", scope="all")
    log(db, "r1", "info", "started")
    log(db, "r1", "warn", "something odd")
    log(db, "r1", "error", "first failure")
    log(db, "r1", "error", "second failure")
    start_run(db, "r2", "", action="plan", scope="all")
    log(db, "r2", "info", "all good")

    summary = run_log_summary(db, ["r1", "r2", "missing"])
    assert summary["r1"] == {"errors": 2, "warnings": 1,
                             "last_error": "second failure"}
    assert summary["r2"] == {"errors": 0, "warnings": 0, "last_error": None}
    assert summary["missing"]["errors"] == 0
    assert run_log_summary(db, []) == {}
