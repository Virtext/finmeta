"""Diff a WritePlan against the local disk (SPEC §10).

For each ``FileWrite`` in the plan:
- file absent on disk  -> "create"
- file present, same sha256 -> "unchanged"
- file present, different sha256 -> "update"

Also produces a human-readable text summary.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from finmeta.engine.plan import FileWrite, ItemPlan, WritePlan


@dataclass
class DiffEntry:
    """One file's diff result."""
    target_path: str
    kind: str
    status: str  # "create" | "update" | "unchanged"
    source_ref: str


def _sha256_file(path: Path) -> Optional[str]:
    """Return the SHA-256 hex of a local file, or None if unreadable."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except (OSError, PermissionError):
        return None


def diff_file(fw: FileWrite) -> DiffEntry:
    """Diff one FileWrite against disk."""
    p = Path(fw.target_path)
    if not p.exists():
        return DiffEntry(
            target_path=fw.target_path, kind=fw.kind,
            status="create", source_ref=fw.source_ref,
        )
    existing = _sha256_file(p)
    if existing == fw.content_sha256:
        return DiffEntry(
            target_path=fw.target_path, kind=fw.kind,
            status="unchanged", source_ref=fw.source_ref,
        )
    return DiffEntry(
        target_path=fw.target_path, kind=fw.kind,
        status="update", source_ref=fw.source_ref,
    )


def diff_plan(plan: WritePlan) -> List[DiffEntry]:
    """Diff every FileWrite in the plan against disk."""
    entries: List[DiffEntry] = []
    for item in plan.items:
        for fw in item.files:
            entries.append(diff_file(fw))
    return entries


def diff_to_text(plan: WritePlan) -> str:
    """Produce a human-readable diff summary."""
    all_entries: List[DiffEntry] = []
    item_entries: List[tuple] = []  # (item, [DiffEntry])

    for item in plan.items:
        entries = [diff_file(fw) for fw in item.files]
        all_entries.extend(entries)
        item_entries.append((item, entries))

    c = sum(1 for e in all_entries if e.status == "create")
    u = sum(1 for e in all_entries if e.status == "update")
    n = sum(1 for e in all_entries if e.status == "unchanged")

    lines: List[str] = []
    lines.append(f"Files: {c} create, {u} update, {n} unchanged ({len(all_entries)} total)")

    for item, entries in item_entries:
        if not entries:
            continue
        lines.append(f"\n{item.item_name}:")
        for e in entries:
            if e.status == "create":
                lines.append(f"  + create    {e.kind:16s} {e.target_path}")
            elif e.status == "update":
                lines.append(f"  ~ update    {e.kind:16s} {e.target_path}")
            else:
                lines.append(f"  = unchanged {e.kind:16s} {e.target_path}")

    if plan.unmatched:
        lines.append("\nUnmatched items:")
        for u_item in plan.unmatched:
            lines.append(f"  ? {u_item.name} ({u_item.reason})")

    if plan.api_actions:
        lines.append("\nAPI actions:")
        for a in plan.api_actions:
            lines.append(f"  {a.action} {a.kind} {a.name!r}")

    return "\n".join(lines) + "\n"


def _annotate_item(item: "ItemPlan") -> None:
    """Set ``fw.status`` for every FileWrite in ``item`` (diff vs disk)."""
    for fw in item.files:
        fw.status = diff_file(fw).status


def annotate_items(items: List["ItemPlan"]) -> None:
    """Set ``fw.status`` for every FileWrite in ``items`` (diff vs disk).

    Mutates the items in place. Used to (re)stamp a plan or a subset of its
    items after the underlying FileWrites have changed (e.g. an override
    swaps ``source_ref`` + ``content_sha256``).
    """
    for item in items:
        _annotate_item(item)


def annotate_status(plan: WritePlan) -> WritePlan:
    """Set ``fw.status`` for every FileWrite by diffing against disk.

    Persists the create/update/unchanged classification (same rules as
    :func:`diff_file`) onto the plan so the plan JSON, CLI, API and e2e all
    see the real status instead of the default "create". Mutates and returns
    ``plan``.
    """
    annotate_items(list(plan.items))
    return plan


__all__ = [
    "DiffEntry", "diff_file", "diff_plan", "diff_to_text",
    "annotate_status", "annotate_items",
]
