"""Match Jellyfin items to TMDb (SPEC §5).

Strategy:
1. Provider ID from Jellyfin (``ProviderIds.Tmdb``), then IMDb -> TMDb via
   TMDb's find endpoint.
2. Fallback: title + year search on TMDb; accept only if exactly one result
   with matching year (+/-1 for movies).
3. Unmatched items are reported. Never guess.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional

from finmeta.jellyfin_client.types import Item
from finmeta.sources.tmdb import TmdbClient, TmdbError

# Extract year from a title like "The Matrix (1999)" or "Show [2001]".
_YEAR_RE = re.compile(r"[\[\(](\d{4})[\]\)]\s*$")


@dataclass
class MatchResult:
    """Outcome of matching a single Jellyfin item to TMDb."""
    item: Item
    tmdb_id: Optional[int] = None
    matched_by: Optional[str] = None  # "provider_id" | "title_year"
    reason: str = ""  # populated when unmatched


def _extract_year(name: str) -> Optional[int]:
    """Pull a 4-digit year from the end of a title."""
    m = _YEAR_RE.search(name)
    if m:
        year = int(m.group(1))
        if 1900 <= year <= 2100:
            return year
    return None


def _strip_year(name: str) -> str:
    """Remove a trailing year from a title for cleaner search queries."""
    return _YEAR_RE.sub("", name).strip()


def _match_by_provider(item: Item, tmdb: TmdbClient, media_type: str,
                       language: Optional[str] = None) -> Optional[int]:
    """Try provider IDs. Returns TMDb id or None."""
    # Direct TMDb provider ID
    tmdb_pid = item.tmdb_id()
    if tmdb_pid:
        try:
            return int(tmdb_pid)
        except (ValueError, TypeError):
            pass

    # IMDb provider ID -> TMDb find
    imdb_id = item.provider_ids.get("Imdb")
    if imdb_id:
        try:
            result = tmdb.find_by_external_id(media_type, imdb_id, language)
            if result and result.get("id"):
                return int(result["id"])
        except TmdbError:
            pass

    return None


def _match_by_title_year(item: Item, tmdb: TmdbClient, media_type: str,
                         language: Optional[str] = None) -> Optional[int]:
    """Fallback: title + year search. Accept only if exactly one result
    with matching year (+/-1 for movies)."""
    query = _strip_year(item.name)
    year = _extract_year(item.name)

    try:
        if media_type == "movie":
            results = tmdb.search_movies(query, year=year, language=language)
            # Accept exactly one result, or filter to matching year
            if year is not None:
                matching = [r for r in results
                            if r.get("release_date") and
                            abs(int(r["release_date"][:4]) - year) <= 1]
                if len(matching) == 1:
                    return int(matching[0]["id"])
            elif len(results) == 1:
                return int(results[0]["id"])
        else:  # tvshow
            results = tmdb.search_tv(query, year=year, language=language)
            if year is not None:
                matching = [r for r in results
                            if r.get("first_air_date") and
                            abs(int(r["first_air_date"][:4]) - year) <= 1]
                if len(matching) == 1:
                    return int(matching[0]["id"])
            elif len(results) == 1:
                return int(results[0]["id"])
    except TmdbError:
        pass

    return None


def match_item(item: Item, tmdb: TmdbClient, media_type: str,
               language: Optional[str] = None) -> MatchResult:
    """Match a Jellyfin item to TMDb.

    ``media_type`` is "movie" or "tvshow".
    """
    # Step 1: provider ID
    tmdb_id = _match_by_provider(item, tmdb, media_type, language)
    if tmdb_id is not None:
        return MatchResult(item=item, tmdb_id=tmdb_id, matched_by="provider_id")

    # Step 2: title + year
    tmdb_id = _match_by_title_year(item, tmdb, media_type, language)
    if tmdb_id is not None:
        return MatchResult(item=item, tmdb_id=tmdb_id, matched_by="title_year")

    # Step 3: unmatched
    return MatchResult(
        item=item, tmdb_id=None, matched_by=None,
        reason=f"no TMDb match for {item.name!r} (type={item.type})",
    )


def match_items(items: List[Item], tmdb: TmdbClient,
                item_media_type: "dict", language: Optional[str] = None
                ) -> List[MatchResult]:
    """Match a list of items. ``item_media_type`` maps item.id -> "movie"|"tvshow"."""
    results = []
    for item in items:
        mt = item_media_type.get(item.id, "movie")
        results.append(match_item(item, tmdb, mt, language))
    return results


__all__ = ["MatchResult", "match_item", "match_items"]
