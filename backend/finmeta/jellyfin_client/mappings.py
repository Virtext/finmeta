"""Jellyfin -> local path mapping (SPEC override O6).

Every path that comes back from the Jellyfin API is a *server* path
(e.g. ``/media/Movies/The Matrix (1999) [tmdbid-603]/...``). Before Finmeta
touches the local disk it must translate it through ``path_mappings``. A path
that matches no mapping is *unmapped* and must be skipped (never guessed).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence


@dataclass(frozen=True)
class PathMapping:
    """A single ``jellyfin -> local`` prefix mapping."""

    jellyfin: str
    local: str

    def _prefix(self) -> str:
        """Normalized jellyfin prefix ('' means filesystem root)."""
        return self.jellyfin.rstrip("/")


def _from_dict(d: Dict[str, str]) -> PathMapping:
    return PathMapping(jellyfin=d["jellyfin"], local=d["local"])


class PathMapper:
    """Translate server paths to local paths using ordered prefix mappings.

    Longer (more specific) jellyfin prefixes win, so
    ``[/media -> A]`` and ``[/media/Movies -> B]`` both work.
    """

    def __init__(self, mappings: Sequence[PathMapping]) -> None:
        self._mappings: List[PathMapping] = sorted(
            mappings, key=lambda m: len(m._prefix()), reverse=True
        )

    @classmethod
    def from_config(cls, entries: Sequence[Dict[str, str]]) -> "PathMapper":
        return cls([_from_dict(e) for e in entries])

    @property
    def mappings(self) -> List[PathMapping]:
        return list(self._mappings)

    def is_mapped(self, path: Optional[str]) -> bool:
        return self.translate(path) is not None

    def translate(self, path: Optional[str]) -> Optional[str]:
        """Return the local path for a server path, or None if unmapped."""
        if not path:
            return None
        for m in self._mappings:
            prefix = m._prefix()
            local_base = m.local.rstrip("/")
            if prefix == "":
                if path.startswith("/"):
                    return local_base + path
                continue
            if path == prefix or path.startswith(prefix + "/"):
                rest = path[len(prefix):]  # '' or '/Movies/...'
                return local_base + rest
        return None


def translate_path(path: Optional[str], entries: Sequence[Dict[str, str]]) -> Optional[str]:
    """Convenience wrapper building a :class:`PathMapper` from config dicts."""
    return PathMapper.from_config(entries).translate(path)
