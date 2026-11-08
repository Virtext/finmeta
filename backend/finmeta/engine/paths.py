"""Media-tree locations for one Jellyfin item (SPEC §10, override O6).

Extracted from ``plan_builder`` in S10a step 3 so the health scanner
(SPEC_S10 SC1/SC4) locates artwork in exactly the same place an apply
writes it -- a second copy of these rules would drift, and the dotted
show-folder bug below is proof the rules are not obvious.

Every function returns ``None`` for an item whose Jellyfin path has no
``path_mappings`` entry: an unmapped path is skipped, never guessed.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from finmeta.jellyfin_client.mappings import PathMapper
from finmeta.jellyfin_client.types import Item


def movie_dir(item: Item, mapper: PathMapper) -> Optional[Path]:
    """Local directory containing the movie video file."""
    local = mapper.translate(item.path)
    if not local:
        return None
    return Path(local).parent


def show_dir(item: Item, mapper: PathMapper) -> Optional[Path]:
    """Local directory for a TV series (the show folder)."""
    local = mapper.translate(item.path)
    if not local:
        return None
    p = Path(local)
    # A real existing directory IS the show folder -- trust it outright.
    # Must come before the suffix heuristic below: a show folder NAME can
    # itself contain a '.' (e.g. "A.D. Police Files", "S.H.I.E.L.D.") and
    # Path.suffix has no concept of "this is a directory, not a file" --
    # it just looks for the last '.' in the final path component. Checking
    # suffix first on a real directory like that wrongly climbed two parent
    # levels and wrote posters/NFOs into the TOP of the whole media share
    # instead of the show's own folder (reported live 2026-10-25).
    if p.is_dir():
        return p
    # If path points to a file (first episode), use its parent's parent
    if p.is_file() or p.suffix:
        # /media/Shows/Show/Season 01/ep.mkv -> /media/Shows/Show
        return p.parent.parent
    return p


def season_dir(item: Item, mapper: PathMapper) -> Optional[Path]:
    """Local directory for a season."""
    local = mapper.translate(item.path)
    if not local:
        return None
    return Path(local)


def episode_stem(item: Item, mapper: PathMapper) -> Optional[Path]:
    """Local path stem for an episode file (without extension)."""
    local = mapper.translate(item.path)
    if not local:
        return None
    p = Path(local)
    return p.with_suffix("")


__all__ = ["movie_dir", "show_dir", "season_dir", "episode_stem"]
