"""Manual candidate selection: SQLite store + plan application (SPEC §11).

A user can pick a specific artwork candidate for an item (or clear the
choice). Choices are persisted in ``overrides.db`` under the config dir and
applied on top of the computed WritePlan so the plan/preview reflects them.
"""
from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional

from finmeta.engine.plan import FileWrite, ItemPlan, WritePlan

_FILENAME_FOR_KIND = {
    "poster": "poster.jpg", "backdrop": "backdrop.jpg", "logo": "logo.png",
    "landscape": "landscape.jpg", "season_poster": "seasonNN-poster.jpg",
    "episode_thumb": "seasonNN-episodeNN.jpg", "theme_song": "theme.mp3",
}


def _content_sha256(source_ref: str, timeout: float = 30.0) -> str:
    """SHA-256 of a candidate ref (local file or remote URL)."""
    if source_ref.startswith(("http://", "https://")):
        import httpx
        resp = httpx.get(source_ref, timeout=timeout, follow_redirects=True)
        resp.raise_for_status()
        return hashlib.sha256(resp.content).hexdigest()
    p = Path(source_ref)
    if p.is_file():
        return hashlib.sha256(p.read_bytes()).hexdigest()
    return hashlib.sha256(b"").hexdigest()


class OverrideStore:
    """SQLite store of manual candidate selections, keyed by (item_id, kind)."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        with self._conn() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS overrides (
                    item_id TEXT NOT NULL,
                    kind    TEXT NOT NULL,
                    ref     TEXT NOT NULL,
                    source  TEXT,
                    PRIMARY KEY (item_id, kind)
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS reshuffle_counts (
                    item_id TEXT NOT NULL,
                    kind    TEXT NOT NULL,
                    count   INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (item_id, kind)
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS skip_kinds (
                    item_id TEXT NOT NULL,
                    kind    TEXT NOT NULL,
                    PRIMARY KEY (item_id, kind)
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS artwork_locks (
                    item_id   TEXT NOT NULL,
                    kind      TEXT NOT NULL,
                    ref       TEXT NOT NULL,
                    sha256    TEXT NOT NULL,
                    locked_at REAL NOT NULL,
                    PRIMARY KEY (item_id, kind)
                )"""
            )

    def set(self, item_id: str, kind: str, ref: str, source: Optional[str] = None) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO overrides (item_id, kind, ref, source) VALUES (?,?,?,?)",
                (item_id, kind, ref, source),
            )

    def clear(self, item_id: str, kind: str) -> bool:
        with self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM overrides WHERE item_id=? AND kind=?", (item_id, kind))
        return cur.rowcount > 0

    def get(self, item_id: str, kind: str) -> Optional[Dict[str, str]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT ref, source FROM overrides WHERE item_id=? AND kind=?",
                (item_id, kind)).fetchone()
        return dict(row) if row else None

    def all_for(self, item_id: str) -> List[Dict[str, str]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT item_id, kind, ref, source FROM overrides WHERE item_id=?",
                (item_id,)).fetchall()
        return [dict(r) for r in rows]

    def all_by_item(self, item_ids: List[str]) -> Dict[str, List[Dict[str, str]]]:
        out: Dict[str, List[Dict[str, str]]] = {}
        for iid in item_ids:
            rows = self.all_for(iid)
            if rows:
                out[iid] = rows
        return out

    # -- reshuffle counters (S9d4a step 2) -------------------------------
    def increment_reshuffle(self, item_id: str, kind: str) -> int:
        """Bump the per-(item, kind) counter; returns the new value."""
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO reshuffle_counts (item_id, kind, count) VALUES (?,?,1)
                   ON CONFLICT(item_id, kind) DO UPDATE SET count = count + 1""",
                (item_id, kind))
            row = conn.execute(
                "SELECT count FROM reshuffle_counts WHERE item_id=? AND kind=?",
                (item_id, kind)).fetchone()
        return int(row["count"]) if row else 0

    def reshuffle_counts(self, item_id: str) -> Dict[str, int]:
        """All per-kind counters for an item ({} when none)."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT kind, count FROM reshuffle_counts WHERE item_id=?",
                (item_id,)).fetchall()
        return {r["kind"]: int(r["count"]) for r in rows}

    # -- per-row plan handlers (S9d4b): skip a kind entirely ---------------
    def set_skip(self, item_id: str, kind: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO skip_kinds (item_id, kind) VALUES (?,?)",
                (item_id, kind))

    def clear_skip(self, item_id: str, kind: str) -> bool:
        with self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM skip_kinds WHERE item_id=? AND kind=?", (item_id, kind))
        return cur.rowcount > 0

    def skipped_kinds(self, item_id: str) -> List[str]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT kind FROM skip_kinds WHERE item_id=?", (item_id,)).fetchall()
        return [r["kind"] for r in rows]

    def skipped_by_item(self, item_ids: List[str]) -> Dict[str, List[str]]:
        out: Dict[str, List[str]] = {}
        for iid in item_ids:
            kinds = self.skipped_kinds(iid)
            if kinds:
                out[iid] = kinds
        return out

    # -- artwork locks (S9d5): pin the candidate picked on an item+kind's --
    # first successful apply, so later runs never re-query sources for it.
    def set_lock(self, item_id: str, kind: str, ref: str, sha256: str) -> None:
        import time
        with self._conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO artwork_locks
                   (item_id, kind, ref, sha256, locked_at) VALUES (?,?,?,?,?)""",
                (item_id, kind, ref, sha256, time.time()))

    def get_lock(self, item_id: str, kind: str) -> Optional[Dict[str, object]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT ref, sha256, locked_at FROM artwork_locks WHERE item_id=? AND kind=?",
                (item_id, kind)).fetchone()
        return dict(row) if row else None

    def clear_lock(self, item_id: str, kind: str) -> bool:
        with self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM artwork_locks WHERE item_id=? AND kind=?", (item_id, kind))
        return cur.rowcount > 0

    def locks_for_item(self, item_id: str) -> Dict[str, Dict[str, object]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT kind, ref, sha256, locked_at FROM artwork_locks WHERE item_id=?",
                (item_id,)).fetchall()
        return {r["kind"]: {"ref": r["ref"], "sha256": r["sha256"], "locked_at": r["locked_at"]}
                for r in rows}


# -- plan application -------------------------------------------------------

def apply_overrides(plan: WritePlan, overrides_by_item: Dict[str, List[Dict[str, str]]]) -> WritePlan:
    """Replace/append FileWrites in ``plan`` per stored override (mutates items).

    Each override carries a ``ref``; the FileWrite for that kind is pointed at
    the ref with a freshly computed content hash. Existing FileWrites (and thus
    their ``target_path``) are reused when present so the plan stays in sync.

    Because an override swaps ``source_ref`` + ``content_sha256``, the affected
    FileWrites no longer match their on-disk bytes; the changed items are
    re-annotated so their ``status`` reflects the new content (SPEC §10).
    """
    from finmeta.engine.diff import annotate_items

    changed_items: List[ItemPlan] = []
    for item in plan.items:
        ovs = overrides_by_item.get(item.item_id)
        if not ovs:
            continue
        item_changed = False
        by_kind = {fw.kind: fw for fw in item.files}
        for ov in ovs:
            kind, ref = ov["kind"], ov["ref"]
            if not ref:
                continue
            existing = by_kind.get(kind)
            if existing is not None and existing.meta.get("overridden"):
                # S9d4c: plan_builder already folded this override into the
                # candidate selection at build time (engine.plan_builder.
                # _apply_candidate_overrides, marked via meta["overridden"])
                # -- for "poster" specifically, that lets the overlay
                # compositor render on top of the OVERRIDDEN candidate.
                # Nothing left to do here; re-hashing the plain ref below
                # would silently discard that overlay (the actual bug this
                # guard fixes -- see commit history). Checking meta rather
                # than source_ref==ref because overlay_file_write points
                # source_ref at the latest BACKUP once one exists (O7), not
                # at the override's ref, so a literal ref comparison would
                # stop matching after the very first apply.
                continue
            try:
                sha = _content_sha256(ref)
            except Exception:
                continue
            if existing is not None:
                existing.source_ref = ref
                existing.content_sha256 = sha
                item_changed = True
            else:
                # Rare: a kind not in the base plan. Reuse a sibling's dir.
                base = None
                for fw in item.files:
                    if fw.target_path:
                        base = Path(fw.target_path).parent
                        break
                target = (base / _FILENAME_FOR_KIND.get(kind, f"{kind}.jpg")) if base else None
                if target is None:
                    continue
                fw = FileWrite(target_path=str(target), kind=kind,  # type: ignore[arg-type]
                               source_ref=ref, content_sha256=sha, status="create")
                item.files.append(fw)
                by_kind[kind] = fw
                item_changed = True
        if item_changed:
            changed_items.append(item)
    # The overridden FileWrites now point at different bytes; re-stamp status
    # (create/update/unchanged) against disk so the executor and preview agree.
    if changed_items:
        annotate_items(changed_items)
    return plan


def apply_skips(plan: WritePlan, skipped_by_item: Dict[str, List[str]]) -> WritePlan:
    """S9d4b: drop FileWrites for kinds the user skipped per row.

    A skipped kind is removed from the item's plan entirely (no file written,
    preview shows no row). Re-stamps status so counts stay honest.
    """
    from finmeta.engine.diff import annotate_items

    changed_items: List[ItemPlan] = []
    for item in plan.items:
        kinds = set(skipped_by_item.get(item.item_id) or [])
        if not kinds:
            continue
        before = len(item.files)
        item.files = [fw for fw in item.files if fw.kind not in kinds]
        if len(item.files) != before:
            changed_items.append(item)
    if changed_items:
        annotate_items(changed_items)
    return plan


__all__ = ["OverrideStore", "apply_overrides", "apply_skips", "_content_sha256"]
