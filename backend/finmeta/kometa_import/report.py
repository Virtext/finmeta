"""Import report for Kometa overlay files: what was translated, partly translated or skipped."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Set


@dataclass
class ReportItem:
    key: str
    status: str = "ok"            # ok | partial | skipped
    notes: List[str] = field(default_factory=list)


@dataclass
class ImportReport:
    source: str = ""
    items: List[ReportItem] = field(default_factory=list)
    missing_templates: Set[str] = field(default_factory=set)
    errors: List[str] = field(default_factory=list)

    def by_key(self, key: str) -> ReportItem:
        for it in self.items:
            if it.key == key:
                return it
        raise KeyError(key)

    def counts(self) -> Dict[str, int]:
        out = {"ok": 0, "partial": 0, "skipped": 0}
        for it in self.items:
            out[it.status] = out.get(it.status, 0) + 1
        return out

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "counts": self.counts(),
            "missing_templates": sorted(self.missing_templates),
            "errors": list(self.errors),
            "items": [{"key": i.key, "status": i.status, "notes": list(i.notes)} for i in self.items],
        }
