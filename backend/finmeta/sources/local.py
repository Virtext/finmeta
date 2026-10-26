"""Local asset source (SPEC §4).

Layout: ``<assets>/<tmdbid>/{poster.jpg, backdrop.jpg, logo.png,
landscape.jpg, theme.mp3, season01-poster.jpg, ...}``.
Local files are always language-agnostic (``lang=None``) and carry a high
score so they win automatic ranking when listed first in precedence.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable, List

from finmeta.sources.candidate import Candidate

_FILE_FOR_KIND = {
    "poster": "poster.jpg",
    "backdrop": "backdrop.jpg",
    "logo": "logo.png",
    "landscape": "landscape.jpg",
    "theme_song": "theme.mp3",
}


class LocalAssetSource:
    def __init__(self, path):
        self.path = Path(path)

    def item_dir(self, tmdb_id) -> Path:
        return self.path / str(tmdb_id)

    def candidates_for(self, tmdb_id, kinds: Iterable[str]) -> List[Candidate]:
        d = self.item_dir(tmdb_id)
        if not d.is_dir():
            return []
        out: List[Candidate] = []
        for kind in kinds:
            if kind in _FILE_FOR_KIND:
                f = d / _FILE_FOR_KIND[kind]
                if f.is_file():
                    out.append(Candidate(kind=kind, source="local", ref=str(f), lang=None, score=1.0))
            elif kind == "season_poster":
                for f in sorted(d.glob("season*-poster.jpg")):
                    out.append(Candidate(kind="season_poster", source="local", ref=str(f), lang=None, score=1.0))
            elif kind == "episode_thumb":
                for f in sorted(d.glob("season*-episode-*.jpg")):
                    out.append(Candidate(kind="episode_thumb", source="local", ref=str(f), lang=None, score=1.0))
        return out


__all__ = ["LocalAssetSource"]
