"""S9d6: pick which items a library's scheduled poster rotation touches.

Ranking is by Jellyfin's own ``CommunityRating`` (synced from TMDb already,
so this needs no extra TMDb calls just to rank items) -- items with no
rating sort last and are never picked over a rated one.
"""
from __future__ import annotations

from typing import List, Sequence

from finmeta.jellyfin_client.client import JellyfinClient


def top_rated_item_ids(
    jf: JellyfinClient, library: str, item_count: int, item_types: Sequence[str],
) -> List[str]:
    """IDs of the ``item_count`` highest-``CommunityRating`` items in ``library``."""
    jf_lib = jf.get_library(library)
    if jf_lib is None:
        return []
    # Pagination fix (2026-10-30): this used to be an unbounded get_items()
    # call -- the same ReadTimeout risk on a large library as the other
    # library-scale call sites, just not yet reported since scheduled
    # rotation runs in the background. iter_items() pages it the same way.
    items = list(jf.iter_items(
        parent_id=jf_lib.item_id, recursive=True, include_item_types=list(item_types),
        fields="CommunityRating",
    ))
    rated = [it for it in items if it.community_rating is not None]
    rated.sort(key=lambda it: it.community_rating, reverse=True)
    return [it.id for it in rated[:item_count]]


__all__ = ["top_rated_item_ids"]
