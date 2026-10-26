"""Kodi-style NFO XML generator (SPEC §7, override O4).

Generates deterministic XML for movie, tvshow, season, and episode NFOs.
Only fields listed in the library\'s ``metadata.fields`` are written;
``uniqueid`` and ``lockdata`` are always included.
"""
from __future__ import annotations

import hashlib
from typing import Any, Dict, List, Optional
from xml.etree import ElementTree as ET

# Default metadata field sets used when a library does not pin
# ``metadata.fields``. The plan builder AND the executor must share these:
# the executor re-fetches TMDb and re-generates a "generated" NFO at apply
# time, then verifies its SHA-256 against the value computed at plan time.
# If the two sides ever fell back to different defaults, the NFO bytes would
# differ and apply would report a false hash mismatch (movie vs TV field
# sets differ: TV uses ``status``, movies use ``tagline``/``runtime``/``mpaa``).
DEFAULT_MOVIE_FIELDS = [
    "title", "originaltitle", "sorttitle", "year", "premiered",
    "plot", "tagline", "runtime", "mpaa", "genres", "studios", "rating",
]
DEFAULT_TV_FIELDS = [
    "title", "originaltitle", "sorttitle", "year", "premiered",
    "plot", "status", "genres", "studios", "rating",
]

# Map config field names to NFO XML element names.
# Fields not in this map are always written (uniqueid, lockdata).
_FIELD_TO_ELEMENT = {
    "title": "title",
    "originaltitle": "originaltitle",
    "sorttitle": "sorttitle",
    "year": "year",
    "premiered": "premiered",
    "plot": "plot",
    "overview": "plot",
    "tagline": "tagline",
    "runtime": "runtime",
    "mpaa": "mpaa",
    "genres": "genre",
    "studios": "studio",
    "rating": "ratings",
    "status": "status",
    "season": "season",
    "episode": "episode",
    "aired": "aired",
}

# Root element per media type.
_ROOT = {
    "movie": "movie",
    "tvshow": "tvshow",
    "season": "season",
    "episode": "episodedetails",
}


def _text(value: Any) -> Optional[str]:
    """Convert a value to a stripped string, or None if empty."""
    if value is None:
        return None
    s = str(value).strip()
    return s if s else None


def _add_element(parent: ET.Element, tag: str, value: Any) -> None:
    """Add a text child element if value is non-empty."""
    t = _text(value)
    if t is not None:
        ET.SubElement(parent, tag).text = t


def _add_repeated(parent: ET.Element, tag: str, values: Any) -> None:
    """Add repeated child elements for a list value."""
    if not values:
        return
    if isinstance(values, (list, tuple)):
        for v in values:
            t = _text(v)
            if t is not None:
                ET.SubElement(parent, tag).text = t
    else:
        t = _text(values)
        if t is not None:
            ET.SubElement(parent, tag).text = t


def _add_ratings(parent: ET.Element, value: Any) -> None:
    """Add a ``<ratings>`` block with the TMDb rating."""
    v = _text(value)
    if v is None:
        return
    ratings = ET.SubElement(parent, "ratings")
    rating = ET.SubElement(ratings, "rating", name="themoviedb", max="10")
    rating.text = v


def _add_uniqueid(parent: ET.Element, media_type: str, tmdb_id: Any,
                  imdb_id: Optional[str] = None) -> None:
    """Add uniqueid elements (always included per O4/SPEC §7)."""
    if tmdb_id is not None:
        uid = ET.SubElement(parent, "uniqueid", type="tmdb", default="true")
        uid.text = str(tmdb_id)
    if imdb_id:
        uid2 = ET.SubElement(parent, "uniqueid", type="imdb")
        uid2.text = str(imdb_id)


def _add_lockdata(parent: ET.Element) -> None:
    """Add ``<lockdata>true</lockdata>`` (override O4)."""
    ET.SubElement(parent, "lockdata").text = "true"


def _write_field(parent: ET.Element, field: str, value: Any) -> None:
    """Write a single config field to the NFO root element."""
    if field == "genres":
        _add_repeated(parent, "genre", value)
    elif field == "studios":
        _add_repeated(parent, "studio", value)
    elif field == "rating":
        _add_ratings(parent, value)
    else:
        elem = _FIELD_TO_ELEMENT.get(field, field)
        _add_element(parent, elem, value)


def _serialize(root: ET.Element) -> bytes:
    """Serialize an ElementTree root to deterministic UTF-8 bytes."""
    # Ensure deterministic attribute ordering (Python 3.8+ preserves insertion order)
    ET.indent(root, space="  ")
    raw = ET.tostring(root, encoding="unicode", xml_declaration=False)
    # Prepend XML declaration
    xml = '<?xml version="1.0" encoding="utf-8"?>\n' + raw + "\n"
    return xml.encode("utf-8")


def movie_nfo(data: Dict[str, Any], tmdb_id: int, fields: Optional[List[str]] = None) -> bytes:
    """Generate a movie NFO from TMDb movie details.

    ``data`` is the TMDb ``/movie/{id}`` response dict.
    ``fields`` is the list of field names from config (None = all).
    """
    root = ET.Element("movie")
    # Always include uniqueid + lockdata
    _add_uniqueid(root, "movie", tmdb_id, data.get("imdb_id"))
    _add_lockdata(root)

    if fields is None:
        fields = ["title", "originaltitle", "sorttitle", "year", "premiered",
                  "plot", "tagline", "runtime", "mpaa", "genres", "studios", "rating"]

    for field in fields:
        if field == "title":
            _add_element(root, "title", data.get("title"))
        elif field == "originaltitle":
            _add_element(root, "originaltitle", data.get("original_title") or data.get("title"))
        elif field == "sorttitle":
            _add_element(root, "sorttitle", data.get("title"))
        elif field == "year":
            rd = data.get("release_date") or ""
            if rd:
                _add_element(root, "year", rd[:4])
        elif field == "premiered":
            _add_element(root, "premiered", data.get("release_date"))
        elif field in ("plot", "overview"):
            _add_element(root, "plot", data.get("overview"))
        elif field == "tagline":
            _add_element(root, "tagline", data.get("tagline"))
        elif field == "runtime":
            _add_element(root, "runtime", data.get("runtime"))
        elif field == "mpaa":
            _add_element(root, "mpaa", data.get("release_certifications", {}).get("US") or
                         data.get("certification"))
        elif field == "genres":
            _add_repeated(root, "genre", [g["name"] for g in data.get("genres", [])])
        elif field == "studios":
            _add_repeated(root, "studio", [c["name"] for c in data.get("production_companies", [])])
        elif field == "rating":
            _add_ratings(root, data.get("vote_average"))

    return _serialize(root)


def tvshow_nfo(data: Dict[str, Any], tmdb_id: int, fields: Optional[List[str]] = None) -> bytes:
    """Generate a tvshow NFO from TMDb TV details."""
    root = ET.Element("tvshow")
    _add_uniqueid(root, "tvshow", tmdb_id, data.get("imdb_id") or
                  (data.get("external_ids") or {}).get("imdb_id"))
    _add_lockdata(root)

    if fields is None:
        fields = ["title", "originaltitle", "sorttitle", "year", "premiered",
                  "plot", "status", "genres", "studios", "rating"]

    for field in fields:
        if field == "title":
            _add_element(root, "title", data.get("name"))
        elif field == "originaltitle":
            _add_element(root, "originaltitle", data.get("original_name") or data.get("name"))
        elif field == "sorttitle":
            _add_element(root, "sorttitle", data.get("name"))
        elif field == "year":
            rad = data.get("first_air_date") or ""
            if rad:
                _add_element(root, "year", rad[:4])
        elif field == "premiered":
            _add_element(root, "premiered", data.get("first_air_date"))
        elif field in ("plot", "overview"):
            _add_element(root, "plot", data.get("overview"))
        elif field == "status":
            _add_element(root, "status", data.get("status"))
        elif field == "genres":
            _add_repeated(root, "genre", [g["name"] for g in data.get("genres", [])])
        elif field == "studios":
            _add_repeated(root, "studio", [c["name"] for c in data.get("production_companies", [])])
        elif field == "rating":
            _add_ratings(root, data.get("vote_average"))

    return _serialize(root)


def season_nfo(data: Dict[str, Any], tmdb_id: int, series_name: str,
               fields: Optional[List[str]] = None) -> bytes:
    """Generate a season NFO from TMDb season details."""
    root = ET.Element("season")
    ET.SubElement(root, "tvshow").text = series_name
    _add_uniqueid(root, "season", tmdb_id)
    _add_lockdata(root)

    if fields is None:
        fields = ["season", "plot"]

    for field in fields:
        if field == "season":
            _add_element(root, "season", data.get("season_number"))
        elif field in ("plot", "overview"):
            _add_element(root, "plot", data.get("overview"))

    return _serialize(root)


def episode_nfo(data: Dict[str, Any], tmdb_id: Optional[int],
                fields: Optional[List[str]] = None) -> bytes:
    """Generate an episode NFO from TMDb episode details."""
    root = ET.Element("episodedetails")
    if tmdb_id is not None:
        _add_uniqueid(root, "episode", tmdb_id)
    _add_lockdata(root)

    if fields is None:
        fields = ["title", "plot", "aired", "season", "episode"]

    for field in fields:
        if field == "title":
            _add_element(root, "title", data.get("name"))
        elif field in ("plot", "overview"):
            _add_element(root, "plot", data.get("overview"))
        elif field == "aired":
            _add_element(root, "aired", data.get("air_date"))
        elif field == "season":
            _add_element(root, "season", data.get("season_number"))
        elif field == "episode":
            _add_element(root, "episode", data.get("episode_number"))

    return _serialize(root)


def nfo_sha256(content: bytes) -> str:
    """Return the SHA-256 hex digest of NFO content bytes."""
    return hashlib.sha256(content).hexdigest()


__all__ = [
    "movie_nfo", "tvshow_nfo", "season_nfo", "episode_nfo", "nfo_sha256",
    "DEFAULT_MOVIE_FIELDS", "DEFAULT_TV_FIELDS",
]
