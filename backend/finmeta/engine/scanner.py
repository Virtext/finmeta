"""Library health scanner — tier 1 checks (SPEC_S10 §A, §B).

Each ``check_*`` function is pure in the sense that matters here: it takes
data someone else already fetched and returns :class:`Finding` objects. No
HTTP happens inside a check, so every rule is unit-testable against
hand-built items and a ``tmp_path`` media tree, and tier 1 provably issues
no provider traffic (TMDb/fanart/OMDb/MDBList/Sonarr) at all.

:func:`scan_library` is the only part that talks to Jellyfin: it pages the
library (bounded pages, never one unbounded ``get_items``) and hands each
page to the checks, flushing findings through an ``emit`` callback so a
large library never accumulates its findings in memory.

S10a implements SC1 (artwork), SC4 (duplicates) and SC7 (unmapped paths).
SC3/SC6 (media streams, artwork resolution) arrive in S10b, SC2/SC5
(metadata, missing episodes) in S10c with tier 2.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import (
    Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple,
)

from finmeta.engine.format_rules import video_facts
from finmeta.engine.matching import match_item
from finmeta.engine.paths import movie_dir, show_dir
from finmeta.engine.scan_store import Finding
from finmeta.jellyfin_client.mappings import PathMapper
from finmeta.jellyfin_client.types import Item

logger = logging.getLogger("finmeta.engine.scanner")

# What the scan pass asks Jellyfin for. Deliberately NOT DEFAULT_ITEM_FIELDS:
# widening that would make every other caller heavier (the 2026-10-30
# pagination timeout is the warning). ImageTags/BackdropImageTags/
# ProductionYear/OfficialRating are not ItemFields values at all -- they are
# always serialized on BaseItemDto, so asking for them would be invalid.
# MediaSourceCount rather than MediaSources: SC4 needs the count, and
# MediaSources drags every MediaStream along with it.
SCAN_ITEM_FIELDS = "Path,ProviderIds,Overview,Genres,Studios,MediaSourceCount"

# One page of a library per Jellyfin request.
DEFAULT_PAGE_SIZE = 200

# Artwork kinds Finmeta can manage, and the sidecar filename each one gets.
# Must stay identical to plan_builder's filename_map -- the scanner has to
# look exactly where an apply writes.
ARTWORK_FILENAMES: Dict[str, str] = {
    "poster": "poster.jpg",
    "backdrop": "backdrop.jpg",
    "logo": "logo.png",
    "landscape": "landscape.jpg",
}

# How Jellyfin reports each kind back: a key in ``ImageTags``, or (backdrop)
# the separate ``BackdropImageTags`` list. Jellyfin maps a local
# landscape.jpg onto its "Thumb" image type.
_IMAGE_TAG_KEY: Dict[str, Optional[str]] = {
    "poster": "Primary",
    "logo": "Logo",
    "landscape": "Thumb",
    "backdrop": None,          # BackdropImageTags, not ImageTags
}

# A missing poster is visible everywhere in Jellyfin; the rest are not.
_MISSING_SEVERITY: Dict[str, str] = {
    "poster": "error",
    "backdrop": "warning",
    "logo": "warning",
    "landscape": "warning",
}

# Provider ids that identify the same title across items (SC4).
_DUPLICATE_PROVIDERS = ("Tmdb", "Imdb")

# How many members of a duplicate group / unmapped-prefix sample to store.
_MAX_MEMBERS = 10
_MAX_SAMPLES = 3

# Severity order, for the `severity_floor` setting.
_SEVERITY_RANK: Dict[str, int] = {"error": 0, "warning": 1, "info": 2}

#: Check groups that can be switched off in config (SC7 is always on).
CHECK_GROUPS = ("artwork", "media", "duplicates", "artwork_quality",
                "metadata", "episodes")

#: Groups that only run in a deep scan (tier 2), because they query a
#: provider per item (TMDb details) or per series (Sonarr episodes).
TIER2_GROUPS = ("episodes",)


@dataclass
class ScanSettings:
    """The `scan:` config block, flattened to plain values.

    Deliberately not the Pydantic model: the checks take plain numbers and
    booleans so every rule is testable without building a whole Config, and
    the scanner never imports the config package.
    """

    checks: Dict[str, bool] = field(default_factory=dict)
    severity_floor: str = "info"
    min_video_height: int = 0
    min_artwork_bytes: int = 0
    min_artwork_width: Dict[str, int] = field(default_factory=dict)
    include_specials: bool = False
    tier: int = 1

    @classmethod
    def from_config(cls, cfg: Any, *, tier: int = 1) -> "ScanSettings":
        """Read `cfg.scan`, tolerating a config that predates the block."""
        scan = getattr(cfg, "scan", None)
        if scan is None:
            return cls(tier=int(tier))
        checks = getattr(scan, "checks", None)
        widths = getattr(scan, "min_artwork_width", None)
        return cls(
            checks={k: bool(getattr(checks, k, True)) for k in CHECK_GROUPS} if checks else {},
            severity_floor=str(getattr(scan, "severity_floor", "info") or "info"),
            min_video_height=int(getattr(scan, "min_video_height", 0) or 0),
            min_artwork_bytes=int(getattr(scan, "min_artwork_bytes", 0) or 0),
            min_artwork_width={k: int(getattr(widths, k, 0) or 0)
                               for k in ARTWORK_FILENAMES} if widths else {},
            include_specials=bool(getattr(scan, "include_specials", False)),
            tier=int(tier),
        )

    def enabled(self, group: str) -> bool:
        """Is this check group on for this scan?

        A tier-2-only group is off in a tier-1 scan however it is configured:
        its cost is a provider request per item or per series, which is the
        whole reason the deep scan is a separate choice.
        """
        if group in TIER2_GROUPS and self.tier < 2:
            return False
        return bool(self.checks.get(group, True))

    @property
    def deep(self) -> bool:
        """Is this a deep (tier 2) scan?"""
        return self.tier >= 2

    def keeps(self, finding: Finding) -> bool:
        """Is this finding at or above the configured severity floor?"""
        floor = _SEVERITY_RANK.get(self.severity_floor, 2)
        return _SEVERITY_RANK.get(finding.severity, 99) <= floor



# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def enabled_artwork_kinds(lib_cfg: Any) -> List[str]:
    """The artwork kinds this library actually manages, in a stable order.

    Reads the same flags the apply path reads, so the scanner never reports a
    kind Finmeta was never asked to write. (TV libraries have no
    ``landscape`` flag; ``getattr`` makes that a plain False.)
    """
    art = getattr(lib_cfg, "artwork", None)
    return [k for k in ("poster", "backdrop", "logo", "landscape")
            if getattr(art, k, False)]


def artwork_base_dir(item: Item, mapper: PathMapper) -> Optional[Path]:
    """Where this item's artwork sidecars live, or None if the path is unmapped."""
    if item.type == "Movie":
        return movie_dir(item, mapper)
    if item.type == "Series":
        return show_dir(item, mapper)
    return None


def partition_by_path(
    items: Iterable[Item], mapper: PathMapper,
) -> Tuple[List[Tuple[Item, Path]], List[Item]]:
    """Split items into (mapped with their base dir, unmapped).

    An unmapped item is excluded from every disk-side check: whether its
    poster exists is unknowable, and an apply would skip it too. SC7 reports
    those once per path prefix instead of once per item.
    """
    mapped: List[Tuple[Item, Path]] = []
    unmapped: List[Item] = []
    for it in items:
        base = artwork_base_dir(it, mapper)
        if base is None:
            unmapped.append(it)
        else:
            mapped.append((it, base))
    return mapped, unmapped


def _media_type(item: Item) -> str:
    return "movie" if item.type == "Movie" else "tvshow"


def _has_jellyfin_image(item: Item, kind: str) -> bool:
    """Does Jellyfin itself show an image of this kind for the item?"""
    if kind == "backdrop":
        return bool(item.backdrop_image_tags)
    key = _IMAGE_TAG_KEY.get(kind)
    return bool(key and (item.image_tags or {}).get(key))


# ---------------------------------------------------------------------------
# SC1 -- artwork presence
# ---------------------------------------------------------------------------

def check_artwork(
    mapped: Sequence[Tuple[Item, Path]], *, library: str, kinds: Sequence[str],
) -> List[Finding]:
    """SC1: poster/backdrop/logo/landscape present on disk and in Jellyfin.

    Three distinct findings per kind, because the fix differs:

    - ``artwork.missing.<kind>``         neither side has it   -> apply_item
    - ``artwork.stale_jellyfin.<kind>``  file but no Jellyfin  -> refresh_item
    - ``artwork.orphan_jellyfin.<kind>`` Jellyfin but no file  -> apply_item

    The orphan case is an image Finmeta does not own (uploaded through
    Jellyfin, or embedded in the video): an apply writes the sidecar, which
    is what the overlay renderer needs, so it is reported as info rather
    than passed over in silence.
    """
    out: List[Finding] = []
    for item, base in mapped:
        for kind in kinds:
            fname = ARTWORK_FILENAMES.get(kind)
            if not fname:
                continue
            target = base / fname
            try:
                has_file = target.is_file()
            except OSError as exc:            # unreadable mount, permissions
                logger.warning("artwork stat failed for %s: %s", target, exc)
                continue
            has_jf = _has_jellyfin_image(item, kind)
            if has_file and has_jf:
                continue
            if not has_file and not has_jf:
                code, severity, fix = (f"artwork.missing.{kind}",
                                       _MISSING_SEVERITY.get(kind, "warning"),
                                       "apply_item")
            elif has_file:
                code, severity, fix = (f"artwork.stale_jellyfin.{kind}",
                                       "error", "refresh_item")
            else:
                code, severity, fix = (f"artwork.orphan_jellyfin.{kind}",
                                       "info", "apply_item")
            out.append(Finding(
                library=library, check_code=code, severity=severity,
                item_id=item.id, item_name=item.name,
                media_type=_media_type(item), fix_kind=fix,
                detail={"kind": kind, "file": str(target),
                        "file_present": has_file, "jellyfin_image": has_jf},
            ))
    return out




# ---------------------------------------------------------------------------
# SC2 -- metadata
# ---------------------------------------------------------------------------

# Provider ids that make an item usable downstream (artwork, NFO, overlays).
_USABLE_PROVIDERS = ("Tmdb", "Imdb", "Tvdb")

# Fields reported together as one "thin metadata" row rather than one row
# each: an item missing all three is one problem, not three.
_SOFT_FIELDS = ("genres", "studios", "official_rating")

# A Jellyfin year this far from TMDb's release year means the item is almost
# certainly matched to the wrong title, not that someone rounded a date.
_YEAR_TOLERANCE = 1


def check_metadata_fields(
    items: Sequence[Item], *, library: str, writes_nfo: bool = False,
) -> List[Finding]:
    """SC2, tier 1: fields that are simply empty on the Jellyfin item.

    - ``metadata.no_provider_id``   no Tmdb/Imdb/Tvdb id at all. severity error:
      nothing downstream works without one, and no apply can fix it (the id
      lives in Jellyfin's own database), so the fix is ``external``.
    - ``metadata.missing_overview`` / ``metadata.missing_year`` -- warnings;
      both are visible to anyone browsing the library.
    - ``metadata.missing_fields``   genres/studio/age rating, as ONE info row
      listing what is empty.

    The three field findings offer an apply only when the library actually
    writes NFOs: that is the only mechanism Finmeta has for metadata, and it
    only reaches Jellyfin if the library reads NFO files.
    """
    fix = "apply_item" if writes_nfo else "external"
    out: List[Finding] = []
    for item in items:
        base = {
            "library": library, "item_id": item.id, "item_name": item.name,
            "media_type": _media_type(item),
        }
        if not any((item.provider_ids or {}).get(k) for k in _USABLE_PROVIDERS):
            out.append(Finding(
                **base, check_code="metadata.no_provider_id", severity="error",
                fix_kind="external",
                detail={"path": item.path or "",
                        "note": "No TMDb/IMDb/TVDb id, so Finmeta cannot look this "
                                "item up at all -- no artwork, no metadata, no "
                                "overlays. Identify it in Jellyfin first."},
            ))
            continue
        if not (item.overview or "").strip():
            out.append(Finding(
                **base, check_code="metadata.missing_overview", severity="warning",
                fix_kind=fix, detail={"writes_nfo": writes_nfo},
            ))
        if not item.production_year:
            out.append(Finding(
                **base, check_code="metadata.missing_year", severity="warning",
                fix_kind=fix, detail={"writes_nfo": writes_nfo},
            ))
        empty = [name for name in _SOFT_FIELDS if not getattr(item, name, None)]
        if empty:
            out.append(Finding(
                **base, check_code="metadata.missing_fields", severity="info",
                fix_kind=fix,
                detail={"fields": empty, "writes_nfo": writes_nfo},
            ))
    return out


def check_metadata_match(
    items: Sequence[Item], *, library: str, tmdb: Any,
    language: Optional[str] = None,
) -> List[Finding]:
    """SC2, tier 2: does the item match a real TMDb title, and the right one?

    - ``metadata.tmdb_unmatched`` -- TMDb has nothing for this item (the
      matcher's own reason is carried in the detail). severity error.
    - ``metadata.year_mismatch``  -- matched, but Jellyfin's year is more than
      a year off TMDb's. This is the practical "wrong metadata" detector: a
      year that far out almost always means the wrong title.

    Both are ``external``: the provider id and the year live in Jellyfin's
    database, and Finmeta has no manual-id override (SPEC_S10 §L D5).

    An item that already carries a TMDb id matches with no network call at
    all; the per-item cost here is the details lookup, and only for items
    that have a year to compare. Everything goes through the normal TMDb
    cache, so a second deep scan is far cheaper than the first.
    """
    out: List[Finding] = []
    for item in items:
        media_type = "movie" if item.type == "Movie" else "tvshow"
        base = {
            "library": library, "item_id": item.id, "item_name": item.name,
            "media_type": _media_type(item),
        }
        try:
            match = match_item(item, tmdb, media_type, language)
        except Exception as exc:
            logger.warning("TMDb match failed for %s: %s", item.name, exc)
            continue
        if match.tmdb_id is None:
            out.append(Finding(
                **base, check_code="metadata.tmdb_unmatched", severity="error",
                fix_kind="external",
                detail={"reason": match.reason, "path": item.path or "",
                        "note": "Set the right provider id on the item in Jellyfin; "
                                "Finmeta never guesses a match."},
            ))
            continue
        if not item.production_year:
            continue          # nothing to compare -- missing_year covers it
        tmdb_year = _tmdb_year(tmdb, media_type, match.tmdb_id, language)
        if tmdb_year is None:
            continue
        if abs(int(item.production_year) - tmdb_year) > _YEAR_TOLERANCE:
            out.append(Finding(
                **base, check_code="metadata.year_mismatch", severity="warning",
                fix_kind="external",
                detail={"jellyfin_year": int(item.production_year),
                        "tmdb_year": tmdb_year, "tmdb_id": match.tmdb_id,
                        "matched_by": match.matched_by,
                        "note": "A year this far out almost always means the item is "
                                "matched to the wrong title."},
            ))
    return out


def _tmdb_year(tmdb: Any, media_type: str, tmdb_id: int,
               language: Optional[str]) -> Optional[int]:
    """Release/first-air year from TMDb details, or None if unavailable."""
    try:
        if media_type == "movie":
            details = tmdb.movie_details(tmdb_id, language=language) or {}
            date = details.get("release_date") or ""
        else:
            details = tmdb.tv_details(tmdb_id, language=language) or {}
            date = details.get("first_air_date") or ""
    except Exception as exc:
        logger.warning("TMDb details failed for %s %s: %s", media_type, tmdb_id, exc)
        return None
    try:
        return int(str(date)[:4])
    except (TypeError, ValueError):
        return None

# ---------------------------------------------------------------------------
# SC3 -- resolution / HDR information
# ---------------------------------------------------------------------------

# Resolution and dynamic-range claims as they appear in release filenames.
# `dv` needs word boundaries or "DVDRip" would read as Dolby Vision.
_RES_TOKEN_RE = re.compile(r"\b(2160p|1440p|1080p|720p|576p|480p|4k|uhd)\b", re.I)
_HDR_TOKEN_RE = re.compile(
    r"(dolby[ ._-]?vision|hdr10\+|hdr10plus|hdr10|hdr|\bdovi\b|\bdv\b)", re.I)

# Actual stream height -> the label a filename would use for it.
_HEIGHT_BUCKETS = (
    (2000, "2160p"), (1300, "1440p"), (900, "1080p"),
    (700, "720p"), (500, "576p"), (300, "480p"),
)
_TOKEN_BUCKETS = {
    "2160p": "2160p", "4k": "2160p", "uhd": "2160p", "1440p": "1440p",
    "1080p": "1080p", "720p": "720p", "576p": "576p", "480p": "480p",
}

# A filename HDR claim -> what VideoRangeType must contain to back it up.
_DOVI_TOKENS = ("dolby vision", "dolbyvision", "dolby.vision", "dolby-vision",
                "dolby_vision", "dovi", "dv")
_HDR10PLUS_TOKENS = ("hdr10+", "hdr10plus")

# Below this height, "no HDR information" is normal rather than suspicious.
_HDR_EXPECTED_HEIGHT = 2160


def _height_bucket(height: Optional[int]) -> Optional[str]:
    """The filename-style label for an actual pixel height."""
    if not height or height <= 0:
        return None
    for floor, label in _HEIGHT_BUCKETS:
        if height >= floor:
            return label
    return "sd"


def _path_tokens(path: Optional[str]) -> Dict[str, Optional[str]]:
    """Resolution / dynamic-range claims made by an item's filename.

    Only the filename is read, never the directory chain above it: a share
    called ``/media/4k-movies`` would otherwise make every file in it claim
    2160p. Returns ``{"resolution": <bucket|None>, "hdr": <token|None>}``.
    """
    if not path:
        return {"resolution": None, "hdr": None}
    name = str(path).replace("\\", "/").rsplit("/", 1)[-1]
    res_m = _RES_TOKEN_RE.search(name)
    hdr_m = _HDR_TOKEN_RE.search(name)
    return {
        "resolution": _TOKEN_BUCKETS.get(res_m.group(1).lower()) if res_m else None,
        "hdr": hdr_m.group(1).lower() if hdr_m else None,
    }


def _hdr_claim_satisfied(token: str, range_type: str, video_range: str) -> bool:
    """Does the stream back up what the filename claims?"""
    rt = (range_type or "").upper()
    vr = (video_range or "").upper()
    norm = token.replace("_", " ").replace(".", " ").replace("-", " ").strip()
    if norm in _DOVI_TOKENS or norm.replace(" ", "") in ("dolbyvision", "dovi", "dv"):
        return "DOVI" in rt
    if norm in _HDR10PLUS_TOKENS:
        return "PLUS" in rt or "HDR10+" in rt
    # plain HDR / HDR10: any non-SDR dynamic range counts
    return bool(rt and rt != "SDR") or (bool(vr) and vr != "SDR")


def check_media(
    items_with_streams: Sequence[Tuple[Item, Sequence[Dict[str, Any]]]], *,
    library: str, min_video_height: int = 0,
) -> List[Finding]:
    """SC3: is Jellyfin's resolution/HDR information there, and is it right?

    Four findings, all fixable only by Jellyfin re-probing the file (Finmeta
    cannot invent stream data):

    - ``media.no_stream``           no media source / no video stream
    - ``media.missing_resolution``  video stream with no height
    - ``media.missing_hdr``         4K with no dynamic-range information at all
    - ``media.format_mismatch``     the filename claims what the streams deny
    - ``media.low_resolution``      below ``min_video_height`` (opt-in, info)

    ``media.missing_hdr`` deliberately does NOT fire on a 4K SDR file:
    ``VideoRange: SDR`` IS information. It fires when both the range and the
    range type are empty, i.e. Jellyfin knows nothing either way.

    The mismatch check reads the filename in both directions for resolution
    (a file named 1080p that is really 2160p is just as mislabelled), but for
    HDR only when the NAME claims something -- a stream that reports Dolby
    Vision while the filename stays quiet is normal, not a defect.
    """
    out: List[Finding] = []
    for item, streams in items_with_streams:
        facts = video_facts(list(streams or []))
        base = {
            "library": library, "item_id": item.id, "item_name": item.name,
            "media_type": _media_type(item),
        }
        if facts is None:
            out.append(Finding(
                **base, check_code="media.no_stream", severity="error",
                fix_kind="refresh_item",
                detail={"path": item.path or "",
                        "note": "Jellyfin reports no video stream for this item -- "
                                "it has never been probed, or the file is unreadable."},
            ))
            continue

        height = facts.get("height") or 0
        if height <= 0:
            out.append(Finding(
                **base, check_code="media.missing_resolution", severity="warning",
                fix_kind="refresh_item",
                detail={"path": item.path or "",
                        "note": "The video stream exists but has no height, so the "
                                "resolution badge has nothing to show."},
            ))
        elif min_video_height and height < min_video_height:
            out.append(Finding(
                **base, check_code="media.low_resolution", severity="info",
                fix_kind="external",
                detail={"height": height, "floor": min_video_height,
                        "path": item.path or ""},
            ))

        range_type = facts.get("video_range_type") or ""
        video_range = facts.get("video_range") or ""
        if (height >= _HDR_EXPECTED_HEIGHT and not range_type and not video_range
                and not facts.get("fallback")):
            out.append(Finding(
                **base, check_code="media.missing_hdr", severity="warning",
                fix_kind="refresh_item",
                detail={"height": height, "path": item.path or "",
                        "note": "4K item with no dynamic-range information at all "
                                "(not even SDR), so the HDR badge silently disappears."},
            ))

        claims = _path_tokens(item.path)
        actual_bucket = _height_bucket(height)
        if claims["resolution"] and actual_bucket and claims["resolution"] != actual_bucket:
            out.append(Finding(
                **base, check_code="media.format_mismatch", severity="warning",
                fix_kind="refresh_item",
                detail={"aspect": "resolution", "claimed": claims["resolution"],
                        "detected": f"{actual_bucket} ({height}px)",
                        "path": item.path or ""},
            ))
        if claims["hdr"] and not _hdr_claim_satisfied(claims["hdr"], range_type, video_range):
            out.append(Finding(
                **base, check_code="media.format_mismatch", severity="warning",
                fix_kind="refresh_item",
                detail={"aspect": "dynamic_range", "claimed": claims["hdr"],
                        "detected": range_type or video_range or "nothing",
                        "path": item.path or ""},
            ))
    return out

# ---------------------------------------------------------------------------
# SC4 -- duplicates
# ---------------------------------------------------------------------------

@dataclass
class DuplicateIndex:
    """Cross-page accumulator for SC4 (a duplicate pair can straddle pages).

    Holds only what a finding needs per item (id, name, path, folder), so a
    5000-item library costs a few hundred kB, not a page of plans.
    """

    library: str
    _by_provider: Dict[Tuple[str, str], List[Dict[str, Any]]] = field(default_factory=dict)
    _by_folder: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)
    _multi_version: List[Dict[str, Any]] = field(default_factory=list)

    def add(self, item: Item, base: Optional[Path]) -> None:
        member = {"item_id": item.id, "name": item.name,
                  "path": item.path or "", "media_type": _media_type(item)}
        for prov in _DUPLICATE_PROVIDERS:
            value = (item.provider_ids or {}).get(prov)
            if value:
                self._by_provider.setdefault((prov, str(value)), []).append(member)
        # Only movies: every episode of a series shares the show folder by
        # design, and two series items never share one.
        if item.type == "Movie" and base is not None:
            self._by_folder.setdefault(str(base), []).append(member)
        if (item.media_source_count or 0) > 1:
            self._multi_version.append(
                {**member, "versions": item.media_source_count})

    def findings(self) -> List[Finding]:
        out: List[Finding] = []
        seen_groups: set = set()
        for (prov, value), members in sorted(self._by_provider.items()):
            if len(members) < 2:
                continue
            key = frozenset(m["item_id"] for m in members)
            if key in seen_groups:
                # The same set of items already reported under another
                # provider id -- one finding, not two.
                continue
            seen_groups.add(key)
            first = members[0]
            out.append(Finding(
                library=self.library, check_code="duplicates.same_provider_id",
                severity="warning", item_id=first["item_id"],
                item_name=first["name"], media_type=first["media_type"],
                fix_kind="none",
                detail={"provider": prov, "provider_id": value,
                        "count": len(members), **_members_detail(members)},
            ))
        for folder, members in sorted(self._by_folder.items()):
            if len(members) < 2:
                continue
            first = members[0]
            out.append(Finding(
                library=self.library, check_code="duplicates.same_folder",
                severity="warning", item_id=first["item_id"],
                item_name=first["name"], media_type=first["media_type"],
                fix_kind="none",
                detail={"folder": folder, "count": len(members),
                        **_members_detail(members)},
            ))
        for m in self._multi_version:
            out.append(Finding(
                library=self.library, check_code="duplicates.multi_version",
                severity="info", item_id=m["item_id"], item_name=m["name"],
                media_type=m["media_type"], fix_kind="none",
                detail={"versions": m["versions"], "path": m["path"],
                        "note": "Jellyfin treats these as versions of one "
                                "item; usually deliberate."},
            ))
        return out


def _members_detail(members: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Member list for a group finding, capped so one row stays small."""
    detail: Dict[str, Any] = {"members": list(members[:_MAX_MEMBERS])}
    if len(members) > _MAX_MEMBERS:
        detail["members_truncated"] = len(members) - _MAX_MEMBERS
    return detail




# ---------------------------------------------------------------------------
# SC5 -- missing seasons / episodes
# ---------------------------------------------------------------------------

# How many episode labels one finding carries before it just counts them.
_MAX_MISSING_LISTED = 50


def _aired(row: Mapping, now: datetime) -> bool:
    """Has this episode aired? An unknown air date counts as NOT aired.

    Sonarr knows about unaired episodes too, so without this every currently
    running season would look half-missing.
    """
    raw = row.get("airDateUtc")
    if not raw:
        return False
    try:
        when = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when <= now


def check_episodes(
    series_items: Sequence[Item], *, library: str, sonarr_index: Mapping[str, Any],
    include_specials: bool = False, now: Optional[datetime] = None,
) -> List[Finding]:
    """SC5 (tier 2): which monitored episodes has Sonarr not got a file for?

    - ``episodes.missing_season``   a season with NO files at all. severity error.
    - ``episodes.missing_episodes`` some aired, monitored episodes have no file.
    - ``episodes.not_in_sonarr``    a Jellyfin series Sonarr does not know, info --
      it may be deliberately unmanaged.

    Every finding is ``external``: this is Sonarr's job. Finmeta has never
    written to Sonarr and does not start here (operator decision D2), so the
    UI deep-links to the series instead of triggering a search.

    Nothing is reported at all when no Sonarr instance is configured -- the
    whole library would otherwise read as "not in Sonarr". Specials (season 0)
    are excluded unless asked for: they are rarely something you collect.

    Costs one Sonarr request per matched series, which is why this is tier 2.
    """
    if not sonarr_index:
        return []
    moment = now or datetime.now(timezone.utc)
    out: List[Finding] = []
    for item in series_items:
        base = {
            "library": library, "item_id": item.id, "item_name": item.name,
            "media_type": _media_type(item),
        }
        tvdb = (item.provider_ids or {}).get("Tvdb")
        entry = sonarr_index.get(str(tvdb)) if tvdb else None
        if entry is None:
            out.append(Finding(
                **base, check_code="episodes.not_in_sonarr", severity="info",
                fix_kind="external",
                detail={"note": "No Sonarr series matches this show's TVDb id, so "
                                "Finmeta cannot tell what is missing. Deliberately "
                                "unmanaged shows are fine to ignore."},
            ))
            continue
        client, row = entry
        try:
            episodes = client.episodes(row.get("id"))
        except Exception as exc:
            logger.warning("Sonarr episode fetch failed for %s: %s", item.name, exc)
            continue
        link = _sonarr_link(client, row)
        by_season: Dict[int, List[Mapping]] = {}
        for ep in episodes:
            season = ep.get("seasonNumber")
            if season is None:
                continue
            if season == 0 and not include_specials:
                continue
            by_season.setdefault(int(season), []).append(ep)

        for season in sorted(by_season):
            eps = by_season[season]
            missing = [e for e in eps
                       if e.get("monitored") and not e.get("hasFile")
                       and _aired(e, moment)]
            if not missing:
                continue
            labels = [f"S{season:02d}E{int(e.get('episodeNumber') or 0):02d}"
                      for e in sorted(missing,
                                      key=lambda e: int(e.get("episodeNumber") or 0))]
            detail: Dict[str, Any] = {
                "season": season, "count": len(missing),
                "missing": labels[:_MAX_MISSING_LISTED],
                "sonarr": link,
            }
            if len(labels) > _MAX_MISSING_LISTED:
                detail["missing_truncated"] = len(labels) - _MAX_MISSING_LISTED
            if not any(e.get("hasFile") for e in eps):
                out.append(Finding(
                    **base, check_code="episodes.missing_season", severity="error",
                    fix_kind="external",
                    detail={**detail,
                            "note": f"Nothing from season {season} is on disk."},
                ))
            else:
                out.append(Finding(
                    **base, check_code="episodes.missing_episodes",
                    severity="warning", fix_kind="external", detail=detail,
                ))
    return out


def _sonarr_link(client: Any, row: Mapping) -> str:
    """Sonarr's own UI URL for a series, or "" when the slug is unknown."""
    slug = row.get("titleSlug")
    url = str(getattr(client, "url", "") or "").rstrip("/")
    return f"{url}/series/{slug}" if (url and slug) else ""

# ---------------------------------------------------------------------------
# SC6 -- artwork quality
# ---------------------------------------------------------------------------

def _image_size(path: Path) -> Optional[Tuple[int, int]]:
    """``(width, height)`` from an image's header, or None if unreadable.

    ``Image.open`` parses the header only -- no decode, no pixel pass -- so
    this stays cheap enough to run over every artwork file in a library.
    Pillow's decompression-bomb cap is already lowered package-wide
    (``finmeta._harden_pillow``), so a hostile image raises here instead of
    allocating gigabytes.
    """
    try:
        from PIL import Image
        with Image.open(path) as im:
            return int(im.size[0]), int(im.size[1])
    except Exception:
        return None


def check_artwork_quality(
    mapped: Sequence[Tuple[Item, Path]], *, library: str, kinds: Sequence[str],
    min_width: Optional[Dict[str, int]] = None, min_bytes: int = 0,
) -> List[Finding]:
    """SC6: are the artwork files Finmeta manages actually usable?

    - ``artwork_quality.tiny_file.<kind>``      suspiciously small file
    - ``artwork_quality.unreadable.<kind>``     no image library can open it
    - ``artwork_quality.low_resolution.<kind>`` narrower than the configured floor

    A missing file is SC1's business, not this check's. A tiny file short-
    circuits the rest: a 2 kB "poster" is almost certainly a placeholder or a
    truncated download, and also reporting it as unreadable would be two rows
    for one problem.

    The low-resolution fix is ``candidate_picker``, not an apply: artwork
    ranking already prefers resolution (``sources/ranking.py``), so a small
    poster usually means the source has nothing better and a human has to
    choose. ``tiny_file``/``unreadable`` DO map to an apply -- re-downloading
    a truncated file is exactly the right move.
    """
    widths = min_width or {}
    out: List[Finding] = []
    for item, base in mapped:
        for kind in kinds:
            fname = ARTWORK_FILENAMES.get(kind)
            if not fname:
                continue
            target = base / fname
            try:
                if not target.is_file():
                    continue
                size = target.stat().st_size
            except OSError as exc:
                logger.warning("artwork stat failed for %s: %s", target, exc)
                continue
            base_fields = {
                "library": library, "item_id": item.id, "item_name": item.name,
                "media_type": _media_type(item),
            }
            if min_bytes and size < min_bytes:
                out.append(Finding(
                    **base_fields, check_code=f"artwork_quality.tiny_file.{kind}",
                    severity="warning", fix_kind="apply_item",
                    detail={"kind": kind, "file": str(target), "bytes": size,
                            "floor_bytes": min_bytes,
                            "note": "Almost certainly a placeholder or a truncated "
                                    "download rather than a real image."},
                ))
                continue
            dims = _image_size(target)
            if dims is None:
                out.append(Finding(
                    **base_fields, check_code=f"artwork_quality.unreadable.{kind}",
                    severity="error", fix_kind="apply_item",
                    detail={"kind": kind, "file": str(target), "bytes": size,
                            "note": "No image library can open this file, so neither "
                                    "Jellyfin nor the overlay renderer can use it."},
                ))
                continue
            width, height = dims
            floor = int(widths.get(kind) or 0)
            if floor and width < floor:
                out.append(Finding(
                    **base_fields,
                    check_code=f"artwork_quality.low_resolution.{kind}",
                    severity="warning", fix_kind="candidate_picker",
                    detail={"kind": kind, "file": str(target),
                            "width": width, "height": height, "floor": floor,
                            "note": "Artwork ranking already prefers resolution, so "
                                    "the source probably has nothing larger -- pick a "
                                    "different candidate."},
                ))
    return out

# ---------------------------------------------------------------------------
# SC7 -- unmapped paths
# ---------------------------------------------------------------------------

def check_path_mapping(unmapped: Sequence[Item], *, library: str) -> List[Finding]:
    """SC7: items whose Jellyfin path has no ``path_mappings`` entry.

    One finding per distinct path prefix, not per item: a missing mapping is
    one config mistake affecting thousands of items, and the fix is the same
    for all of them. Every disk-side check is blind for these items, which is
    why this is an error even though nothing is wrong with the media itself.
    """
    if not unmapped:
        return []
    groups: Dict[str, List[Item]] = {}
    for it in unmapped:
        groups.setdefault(_path_prefix(it.path), []).append(it)
    out: List[Finding] = []
    for prefix, items in sorted(groups.items()):
        out.append(Finding(
            library=library, check_code="path_unmapped", severity="error",
            item_id=None, item_name=prefix, media_type="",
            fix_kind="external",
            detail={"prefix": prefix, "count": len(items),
                    "samples": [i.path or "" for i in items[:_MAX_SAMPLES]],
                    "note": "Add a path mapping in Settings so Finmeta can "
                            "find this library on disk."},
        ))
    return out


def _path_prefix(path: Optional[str]) -> str:
    """The first two components of a path, as a mapping-shaped prefix."""
    if not path:
        return "(no path)"
    parts = [p for p in str(path).replace("\\", "/").split("/") if p]
    if not parts:
        return "/"
    return "/" + "/".join(parts[:2])


# ---------------------------------------------------------------------------
# orchestration
# ---------------------------------------------------------------------------


def _media_findings(
    jf: Any, items: Sequence[Item], *, library: str, min_video_height: int,
) -> List[Finding]:
    """SC3 for one page: ONE batched stream request, movies only.

    A series carries no streams of its own -- standing in its first episode
    costs one request per series, which is a tier-2 price, so TV items are
    left to a future episode-level pass rather than reported as "no stream".

    If the batch request itself fails, the page contributes no media findings
    at all: reporting "no video stream" for every item because Jellyfin
    hiccuped would be a false alarm of the worst kind.
    """
    movies = [it for it in items if it.type == "Movie"]
    if not movies:
        return []
    try:
        by_id = jf.get_items_media_streams_raw([it.id for it in movies])
    except Exception as exc:
        logger.warning("media-stream fetch failed for %s: %s", library, exc)
        return []
    return check_media(
        [(it, by_id.get(it.id) or []) for it in movies],
        library=library, min_video_height=min_video_height,
    )

@dataclass
class ScanLibraryResult:
    """What one library contributed to a scan."""

    library: str
    items_scanned: int = 0
    findings: int = 0
    skipped_reason: Optional[str] = None


def scan_library(
    jf: Any, *, library: str, lib_cfg: Any, mapper: PathMapper,
    emit: Callable[[List[Finding]], None],
    settings: Optional[ScanSettings] = None,
    tmdb: Any = None,
    sonarr_index: Optional[Mapping[str, Any]] = None,
    language: Optional[str] = None,
    page_size: int = DEFAULT_PAGE_SIZE,
    progress_cb: Optional[Callable[[int, int], None]] = None,
) -> ScanLibraryResult:
    """Run the tier-1 checks over one library, flushing findings per page.

    A library with ``enabled: false`` is skipped entirely -- same rule the
    apply path follows (S9d4c): Finmeta reports only on what it manages.
    Pages are sorted by SortName so a page boundary means the same thing on
    every request.

    ``settings`` gates the check groups and applies the severity floor: a
    group that is off does no work at all (SC3 skips its media-stream fetch),
    and a finding below the floor is never emitted, so the stored count and
    the reported count always agree.

    ``tmdb`` is only used by a deep (tier-2) scan, for SC2's match checks;
    without it those are skipped and the tier-1 field checks still run. The
    same goes for ``sonarr_index`` and SC5.
    """
    result = ScanLibraryResult(library=library)
    cfg = settings or ScanSettings()
    if not getattr(lib_cfg, "enabled", False):
        result.skipped_reason = "library disabled"
        return result

    jf_lib = jf.get_library(library)
    if jf_lib is None:
        result.skipped_reason = "library not found in Jellyfin"
        return result

    kinds = enabled_artwork_kinds(lib_cfg)
    writes_nfo = bool(getattr(getattr(lib_cfg, "metadata", None), "enabled", False))
    dup = DuplicateIndex(library=library)
    unmapped_all: List[Item] = []
    offset = 0
    total = 0

    def _flush(findings: List[Finding]) -> None:
        kept = [f for f in findings if cfg.keeps(f)]
        if kept:
            emit(kept)
            result.findings += len(kept)

    while True:
        page = jf.get_items_page(
            parent_id=jf_lib.item_id, recursive=True,
            include_item_types=["Movie", "Series"],
            fields=SCAN_ITEM_FIELDS, limit=page_size, start_index=offset,
            sort_by=["SortName"],
        )
        items = list(page.items)
        total = page.total_record_count or len(items)
        if not items:
            break
        mapped, unmapped = partition_by_path(items, mapper)
        unmapped_all.extend(unmapped)
        for item, base in mapped:
            dup.add(item, base)
        for item in unmapped:
            dup.add(item, None)
        page_findings: List[Finding] = []
        if kinds and cfg.enabled("artwork"):
            page_findings.extend(check_artwork(mapped, library=library, kinds=kinds))
        if kinds and cfg.enabled("artwork_quality"):
            page_findings.extend(check_artwork_quality(
                mapped, library=library, kinds=kinds,
                min_width=cfg.min_artwork_width, min_bytes=cfg.min_artwork_bytes))
        if cfg.enabled("media"):
            page_findings.extend(_media_findings(
                jf, items, library=library, min_video_height=cfg.min_video_height))
        if cfg.enabled("metadata"):
            page_findings.extend(check_metadata_fields(
                items, library=library, writes_nfo=writes_nfo))
            if cfg.deep and tmdb is not None:
                page_findings.extend(check_metadata_match(
                    items, library=library, tmdb=tmdb, language=language))
        if cfg.enabled("episodes") and sonarr_index:
            page_findings.extend(check_episodes(
                [it for it in items if it.type == "Series"], library=library,
                sonarr_index=sonarr_index,
                include_specials=cfg.include_specials))
        _flush(page_findings)
        result.items_scanned += len(items)
        offset += len(items)
        if progress_cb is not None:
            progress_cb(result.items_scanned, total)
        if offset >= total:
            break

    tail: List[Finding] = []
    if cfg.enabled("duplicates"):
        tail.extend(dup.findings())
    # SC7 has no switch: an unmapped path blinds every other disk check.
    tail.extend(check_path_mapping(unmapped_all, library=library))
    _flush(tail)
    return result


__all__ = [
    "SCAN_ITEM_FIELDS", "DEFAULT_PAGE_SIZE", "ARTWORK_FILENAMES", "CHECK_GROUPS",
    "TIER2_GROUPS",
    "Finding", "DuplicateIndex", "ScanLibraryResult", "ScanSettings",
    "enabled_artwork_kinds", "artwork_base_dir", "partition_by_path",
    "check_artwork", "check_artwork_quality", "check_media",
    "check_episodes", "check_metadata_fields", "check_metadata_match",
    "check_path_mapping", "scan_library",
]
