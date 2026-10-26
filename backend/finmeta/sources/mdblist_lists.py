"""MDBList "list items" client (S9g2/K7b, SPEC_KOMETA.md).

Fetches the items of a public MDBList list (e.g. a Trakt-trending mirror)
by its web URL, for the trending-collections feature.

NOT independently verified against a live MDBList API key -- none was
available in this environment and MDBList's API docs page is JS-rendered
(no static reference to confirm exact field names against). Built from:
  - MDBList's documented list-reference URL format
    (``https://mdblist.com/lists/<user>/<slug>``), confirmed via Kometa's
    own ``mdblist_list`` builder docs (a sibling project with the same
    integration need).
  - This project's own already-working ratings client
    (``sources/mdblist.py``) for the base URL/auth convention
    (``https://api.mdblist.com``, ``apikey`` query param).
Parses the response defensively (several plausible field-name variants per
value, same style as ``mdblist.py``'s ``_normalize_source``) so a minor
field-naming mismatch degrades that one field to ``None`` instead of
crashing the whole fetch. Flag this for live verification with a real
MDBList API key before relying on it in production.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import httpx

DEFAULT_BASE_URL = "https://api.mdblist.com"

_LIST_URL_RE = re.compile(r"mdblist\.com/lists/([^/?#]+)/([^/?#]+)")


class MdblistListError(RuntimeError):
    """Raised for an unparseable list URL or a non-2xx MDBList response."""


@dataclass(frozen=True)
class MdblistListItem:
    rank: int
    title: Optional[str]
    imdb_id: Optional[str]
    tmdb_id: Optional[int]
    media_type: Optional[str]  # "movie" | "show", best-effort normalised


def parse_list_url(url: str) -> Optional[tuple]:
    """``(username, slug)`` from a public MDBList list URL, or ``None``."""
    m = _LIST_URL_RE.search(url)
    if not m:
        return None
    return m.group(1), m.group(2)


def _first(d: Dict[str, Any], *keys: str) -> Any:
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return v
    return None


def _as_int(v: Any) -> Optional[int]:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _media_type(v: Any) -> Optional[str]:
    s = str(v or "").strip().lower()
    if s in ("movie", "movies"):
        return "movie"
    if s in ("show", "shows", "tv", "series"):
        return "show"
    return None


def _rows_from_response(data: Any) -> List[Dict[str, Any]]:
    """Tolerate a few plausible top-level response shapes: a bare list, or
    an object with an "items" list, or separate "movies"/"shows" lists."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        if isinstance(data.get("items"), list):
            return data["items"]
        rows: List[Dict[str, Any]] = []
        for key in ("movies", "shows"):
            if isinstance(data.get(key), list):
                rows.extend(data[key])
        return rows
    return []


def fetch_list_items(
    url: str, api_key: str, *, limit: int = 0,
    base_url: str = DEFAULT_BASE_URL, timeout: float = 30.0,
) -> List[MdblistListItem]:
    """Fetch a public MDBList list's items by its web URL.

    Raises :class:`MdblistListError` on an unparseable URL or a non-2xx
    response. ``limit`` (0 = no limit) caps how many rows are requested.
    """
    parsed = parse_list_url(url)
    if parsed is None:
        raise MdblistListError(f"not a recognisable MDBList list URL: {url!r}")
    username, slug = parsed
    params: Dict[str, Any] = {"apikey": api_key}
    if limit:
        params["limit"] = limit
    resp = httpx.get(f"{base_url}/lists/{username}/{slug}/items",
                     params=params, timeout=timeout)
    if resp.status_code >= 400:
        raise MdblistListError(
            f"GET {resp.request.url.path}: HTTP {resp.status_code}: {resp.text[:300]}")
    rows = _rows_from_response(resp.json())
    out: List[MdblistListItem] = []
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        rank = _as_int(_first(row, "rank", "position")) or (i + 1)
        out.append(MdblistListItem(
            rank=rank,
            title=_first(row, "title", "name"),
            imdb_id=_first(row, "imdb_id", "imdbid", "imdb"),
            tmdb_id=_as_int(_first(row, "tmdb_id", "tmdbid", "tmdb")),
            media_type=_media_type(_first(row, "mediatype", "media_type", "type")),
        ))
    return out


__all__ = ["MdblistListItem", "MdblistListError", "parse_list_url", "fetch_list_items"]
