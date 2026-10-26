"""Common Candidate model shared by all metadata sources (SPEC §4).

``ref`` is a URL (remote sources) or a local filesystem path (the local
asset source). ``lang`` is an ISO-639-1 code (e.g. ``"en"``), a fuller
locale for metadata, or ``None`` for textless artwork.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict

# SPEC §4: kind is one of these.
Kind = Literal[
    "poster", "backdrop", "logo", "landscape",
    "season_poster", "episode_thumb", "theme_song", "metadata",
]
# Which source produced the candidate.
SourceName = Literal["tmdb", "fanart", "local"]

KINDS = [
    "poster", "backdrop", "logo", "landscape",
    "season_poster", "episode_thumb", "theme_song", "metadata",
]


class Candidate(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Kind
    source: SourceName
    ref: str
    lang: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    score: float = 0.0

    @property
    def size(self) -> Optional[tuple[int, int]]:
        """(width, height) when both are known, else None."""
        if self.width is not None and self.height is not None:
            return (self.width, self.height)
        return None

    def __str__(self) -> str:  # compact, for logs/tests
        dims = f"{self.width}x{self.height}" if self.size else "?"
        return f"[{self.kind}:{self.source}] {self.ref} lang={self.lang or 'textless'} {dims} score={self.score}"


__all__ = ["Candidate", "Kind", "SourceName", "KINDS"]
