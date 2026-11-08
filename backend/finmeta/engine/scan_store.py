"""Durable store for library-health scan findings (SPEC_S10 §C).

Two tables in the same SQLite file as ``runs``/``job_log``
(``config/jobs.db``, i.e. ``AppContext.db_path``) because a scan IS a run:
``scans.scan_id`` is the run id of the scan run, so the existing job log and
progress reporting describe the same row.

  scans          -- one row per scan pass (scope, tier, counters, status)
  scan_findings  -- one row per finding, with the fix Finmeta can offer

A scan of a large library can produce thousands of findings, so the rows are
inserted per page (never accumulated in memory) and read back paged/filtered.
Retention is part of the design, not a later cleanup: ``prune_scans`` keeps
only the newest N scans -- the never-pruned ``config/runs/*.json`` plan files
are the cautionary precedent.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

# Visible-to-a-user-browsing-Jellyfin first, "probably deliberate" last.
SEVERITIES: Tuple[str, ...] = ("error", "warning", "info")

# What the UI may offer as a fix for a finding (SPEC_S10 §G).
#   apply_item       -- item-scope apply run (Finmeta writes the file)
#   refresh_item     -- ask Jellyfin to re-read/re-probe what is already there
#   candidate_picker -- a human picks a different artwork candidate
#   external         -- fix lives in Jellyfin/Sonarr/config, deep-link only
#   none             -- reported, never auto-fixed (duplicates)
FIX_KINDS: Tuple[str, ...] = (
    "apply_item", "refresh_item", "candidate_picker", "external", "none",
)

# Kept here (not in config) for S10a; the `scan.keep_scans` config field
# arrives in S10b and will be passed in by the caller.
DEFAULT_KEEP_SCANS = 5

# Severity sort rank, used by list_findings' ORDER BY.
_SEVERITY_RANK = "CASE severity WHEN 'error' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END"


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _conn(db_path: Path) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


@dataclass
class Finding:
    """One thing wrong with one item (or one library-level problem).

    ``check_code`` is ``"<group>.<detail>"`` -- e.g. ``artwork.missing.poster``
    -- so the UI can group by the part before the first dot. ``item_id`` is
    None for library-level findings (an unmapped path prefix, say).
    """

    library: str
    check_code: str
    severity: str
    item_id: Optional[str] = None
    item_name: str = ""
    media_type: str = ""
    detail: Dict[str, Any] = field(default_factory=dict)
    fix_kind: str = "none"

    def __post_init__(self) -> None:
        if self.severity not in SEVERITIES:
            raise ValueError(f"unknown severity {self.severity!r}")
        if self.fix_kind not in FIX_KINDS:
            raise ValueError(f"unknown fix_kind {self.fix_kind!r}")

    @property
    def group(self) -> str:
        """The check group: everything before the first dot of check_code."""
        return self.check_code.split(".", 1)[0]


def init_scan_db(db_path: Path) -> None:
    """Create the scan tables if they do not already exist."""
    with _conn(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS scans (
                scan_id       TEXT PRIMARY KEY,
                started_at    TEXT NOT NULL,
                finished_at   TEXT,
                status        TEXT NOT NULL DEFAULT 'running',
                library       TEXT,
                tier          INTEGER NOT NULL DEFAULT 1,
                items_scanned INTEGER NOT NULL DEFAULT 0,
                finding_count INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS scan_findings (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                scan_id     TEXT NOT NULL,
                library     TEXT NOT NULL,
                item_id     TEXT,
                item_name   TEXT NOT NULL DEFAULT '',
                media_type  TEXT NOT NULL DEFAULT '',
                check_code  TEXT NOT NULL,
                severity    TEXT NOT NULL,
                detail      TEXT NOT NULL DEFAULT '{}',
                fix_kind    TEXT NOT NULL DEFAULT 'none',
                fixed_at    TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_findings_scan
                ON scan_findings(scan_id, severity);
            CREATE INDEX IF NOT EXISTS idx_findings_code
                ON scan_findings(scan_id, check_code);
            CREATE INDEX IF NOT EXISTS idx_findings_item
                ON scan_findings(item_id);
            """
        )


def start_scan(db_path: Path, scan_id: str, *, library: Optional[str] = None,
               tier: int = 1) -> None:
    """Record the start of a scan. ``library`` None = all configured libraries."""
    init_scan_db(db_path)
    with _conn(db_path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO scans (scan_id, started_at, status, library, tier)"
            " VALUES (?,?,?,?,?)",
            (scan_id, _utcnow(), "running", library, int(tier)),
        )


def add_findings(db_path: Path, scan_id: str, findings: Sequence[Finding], *,
                 redact: Optional[Callable[[str], str]] = None) -> int:
    """Insert a batch of findings. Returns how many rows were written.

    ``redact`` (``AppContext.redact``) is applied to the serialized detail
    JSON: paths may legitimately contain titles, secrets may not.
    """
    if not findings:
        return 0
    init_scan_db(db_path)
    rows = []
    for f in findings:
        detail = json.dumps(f.detail, ensure_ascii=False, sort_keys=True, default=str)
        if redact is not None:
            detail = redact(detail)
        rows.append((scan_id, f.library, f.item_id, f.item_name, f.media_type,
                     f.check_code, f.severity, detail, f.fix_kind))
    with _conn(db_path) as conn:
        conn.executemany(
            "INSERT INTO scan_findings (scan_id, library, item_id, item_name,"
            " media_type, check_code, severity, detail, fix_kind)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            rows,
        )
        conn.execute(
            "UPDATE scans SET finding_count = finding_count + ? WHERE scan_id = ?",
            (len(rows), scan_id),
        )
    return len(rows)


def bump_items_scanned(db_path: Path, scan_id: str, count: int) -> None:
    """Add to the scan's items_scanned counter (called once per page)."""
    with _conn(db_path) as conn:
        conn.execute(
            "UPDATE scans SET items_scanned = items_scanned + ? WHERE scan_id = ?",
            (int(count), scan_id),
        )


def finish_scan(db_path: Path, scan_id: str, status: str) -> None:
    """Mark a scan finished with ``status`` ("ok" | "error")."""
    with _conn(db_path) as conn:
        conn.execute(
            "UPDATE scans SET finished_at = ?, status = ? WHERE scan_id = ?",
            (_utcnow(), status, scan_id),
        )


def get_scan(db_path: Path, scan_id: str) -> Optional[Dict[str, Any]]:
    init_scan_db(db_path)
    with _conn(db_path) as conn:
        row = conn.execute("SELECT * FROM scans WHERE scan_id = ?", (scan_id,)).fetchone()
    return dict(row) if row else None


def list_scans(db_path: Path, limit: int = 20) -> List[Dict[str, Any]]:
    """Scans, newest first."""
    init_scan_db(db_path)
    with _conn(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM scans ORDER BY started_at DESC, rowid DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
    return [dict(r) for r in rows]


def latest_scan(db_path: Path, *, finished_only: bool = False) -> Optional[Dict[str, Any]]:
    """The newest scan, optionally restricted to finished ones."""
    init_scan_db(db_path)
    where = "WHERE finished_at IS NOT NULL" if finished_only else ""
    with _conn(db_path) as conn:
        row = conn.execute(
            f"SELECT * FROM scans {where} ORDER BY started_at DESC, rowid DESC LIMIT 1"
        ).fetchone()
    return dict(row) if row else None


def scan_counts(db_path: Path, scan_id: str) -> Dict[str, Dict[str, int]]:
    """Finding counts for one scan, by severity and by check group/code."""
    init_scan_db(db_path)
    out: Dict[str, Dict[str, int]] = {"by_severity": {}, "by_group": {}, "by_check": {}}
    with _conn(db_path) as conn:
        for row in conn.execute(
            "SELECT severity, COUNT(*) n FROM scan_findings WHERE scan_id = ?"
            " GROUP BY severity", (scan_id,),
        ):
            out["by_severity"][row["severity"]] = row["n"]
        for row in conn.execute(
            "SELECT check_code, COUNT(*) n FROM scan_findings WHERE scan_id = ?"
            " GROUP BY check_code", (scan_id,),
        ):
            code = row["check_code"]
            out["by_check"][code] = row["n"]
            group = code.split(".", 1)[0]
            out["by_group"][group] = out["by_group"].get(group, 0) + row["n"]
    return out


def list_findings(
    db_path: Path, scan_id: str, *,
    library: Optional[str] = None,
    severity: Optional[str] = None,
    check: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> Tuple[List[Dict[str, Any]], int]:
    """One page of findings plus the total matching count.

    ``check`` matches either a full check_code ("artwork.missing.poster") or a
    whole group ("artwork"). Ordering is severity, then code, then item name,
    so the page boundary is stable across requests.
    """
    init_scan_db(db_path)
    where = ["scan_id = ?"]
    args: List[Any] = [scan_id]
    if library:
        where.append("library = ?")
        args.append(library)
    if severity:
        where.append("severity = ?")
        args.append(severity)
    if check:
        where.append("(check_code = ? OR check_code LIKE ?)")
        args.extend([check, f"{check}.%"])
    clause = " AND ".join(where)
    with _conn(db_path) as conn:
        total = conn.execute(
            f"SELECT COUNT(*) n FROM scan_findings WHERE {clause}", args
        ).fetchone()["n"]
        rows = conn.execute(
            f"SELECT * FROM scan_findings WHERE {clause}"
            f" ORDER BY {_SEVERITY_RANK}, check_code, item_name, id"
            " LIMIT ? OFFSET ?",
            [*args, int(limit), int(offset)],
        ).fetchall()
    return [_finding_row(r) for r in rows], total


def findings_by_ids(db_path: Path, scan_id: str,
                    ids: Sequence[int]) -> List[Dict[str, Any]]:
    """The named findings of one scan, in the order the ids were given."""
    if not ids:
        return []
    init_scan_db(db_path)
    marks = ",".join("?" for _ in ids)
    with _conn(db_path) as conn:
        rows = conn.execute(
            f"SELECT * FROM scan_findings WHERE scan_id = ? AND id IN ({marks})",
            [scan_id, *[int(i) for i in ids]],
        ).fetchall()
    by_id = {r["id"]: _finding_row(r) for r in rows}
    return [by_id[int(i)] for i in ids if int(i) in by_id]


def mark_fixed(db_path: Path, ids: Sequence[int]) -> None:
    """Stamp fixed_at on findings whose fix was submitted.

    Informational only: the fix is NOT re-verified here -- the next scan is
    the source of truth, and the UI says so.
    """
    if not ids:
        return
    ts = _utcnow()
    with _conn(db_path) as conn:
        conn.executemany(
            "UPDATE scan_findings SET fixed_at = ? WHERE id = ?",
            [(ts, int(i)) for i in ids],
        )


def delete_scan(db_path: Path, scan_id: str) -> bool:
    """Drop one scan and its findings. True if the scan existed."""
    init_scan_db(db_path)
    with _conn(db_path) as conn:
        cur = conn.execute("DELETE FROM scans WHERE scan_id = ?", (scan_id,))
        conn.execute("DELETE FROM scan_findings WHERE scan_id = ?", (scan_id,))
    return cur.rowcount > 0


def prune_scans(db_path: Path, keep: int = DEFAULT_KEEP_SCANS) -> int:
    """Keep only the newest ``keep`` scans; delete the rest and their findings.

    A still-running scan is never pruned (it would orphan the rows the worker
    is still inserting). ``keep <= 0`` disables pruning entirely.
    """
    if keep <= 0:
        return 0
    init_scan_db(db_path)
    with _conn(db_path) as conn:
        doomed = [r["scan_id"] for r in conn.execute(
            "SELECT scan_id FROM scans WHERE status != 'running'"
            " ORDER BY started_at DESC, rowid DESC LIMIT -1 OFFSET ?",
            (int(keep),),
        ).fetchall()]
        if not doomed:
            return 0
        marks = ",".join("?" for _ in doomed)
        conn.execute(f"DELETE FROM scan_findings WHERE scan_id IN ({marks})", doomed)
        conn.execute(f"DELETE FROM scans WHERE scan_id IN ({marks})", doomed)
    return len(doomed)


def _finding_row(row: sqlite3.Row) -> Dict[str, Any]:
    """One DB row as the API shape (detail parsed back into an object)."""
    d = dict(row)
    try:
        d["detail"] = json.loads(d.get("detail") or "{}")
    except (ValueError, TypeError):
        d["detail"] = {"raw": d.get("detail")}
    d["group"] = str(d.get("check_code", "")).split(".", 1)[0]
    return d


__all__ = [
    "SEVERITIES", "FIX_KINDS", "DEFAULT_KEEP_SCANS", "Finding",
    "init_scan_db", "start_scan", "add_findings", "bump_items_scanned",
    "finish_scan", "get_scan", "list_scans", "latest_scan", "scan_counts",
    "list_findings", "findings_by_ids", "mark_fixed", "delete_scan",
    "prune_scans",
]
