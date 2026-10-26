"""Typed dataclasses for the subset of Jellyfin API responses Finmeta uses.

Jellyfin serializes in PascalCase by default; ``from_dict`` reads those keys.
Only the fields Finmeta actually consumes are modelled — the rest is ignored.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class SystemInfo:
    """Subset of ``GET /System/Info/Public``."""

    server_name: str
    version: str
    startup_wizard_completed: bool

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "SystemInfo":
        return cls(
            server_name=d.get("ServerName", ""),
            version=d.get("Version", ""),
            startup_wizard_completed=bool(d.get("StartupWizardCompleted", False)),
        )


@dataclass
class AuthResult:
    """Subset of ``POST /Users/AuthenticateByName`` result."""

    access_token: str
    user_id: str
    user_name: str


@dataclass
class Library:
    """One virtual folder (``VirtualFolderInfo``)."""

    name: str
    item_id: str
    collection_type: str
    locations: List[str] = field(default_factory=list)
    refresh_status: str = ""
    refresh_progress: float = 0.0

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Library":
        return cls(
            name=d.get("Name", ""),
            item_id=d.get("ItemId", ""),
            collection_type=d.get("CollectionType", ""),
            locations=list(d.get("Locations") or []),
            refresh_status=d.get("RefreshStatus", "") or "",
            refresh_progress=float(d.get("RefreshProgress") or 0.0),
        )


@dataclass
class Item:
    """Subset of ``BaseItemDto`` carrying the fields Finmeta relies on."""

    id: str
    name: str
    type: str
    parent_id: Optional[str] = None
    path: Optional[str] = None
    provider_ids: Dict[str, str] = field(default_factory=dict)
    image_tags: Optional[str] = None
    backdrop_image_tags: List[str] = field(default_factory=list)
    series_id: Optional[str] = None
    season_id: Optional[str] = None
    index_number: Optional[int] = None
    parent_index_number: Optional[int] = None
    community_rating: Optional[float] = None
    critic_rating: Optional[float] = None

    @classmethod
    def _parse_rating(cls, v) -> Optional[float]:
        if v is None or v <= 0:
            return None
        try:
            return round(float(v), 1)
        except (ValueError, TypeError):
            return None

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Item":
        prov = d.get("ProviderIds") or {}
        return cls(
            id=d.get("Id", ""),
            name=d.get("Name", ""),
            type=d.get("Type", ""),
            parent_id=d.get("ParentId"),
            path=d.get("Path"),
            provider_ids={k: v for k, v in prov.items() if v is not None},
            image_tags=d.get("ImageTags"),
            backdrop_image_tags=list(d.get("BackdropImageTags") or []),
            series_id=d.get("SeriesId"),
            season_id=d.get("SeasonId"),
            index_number=d.get("IndexNumber"),
            parent_index_number=d.get("ParentIndexNumber"),
            community_rating=cls._parse_rating(d.get("CommunityRating")),
            critic_rating=cls._parse_rating(d.get("CriticRating")),
        )

    def tmdb_id(self) -> Optional[str]:
        """The TMDb provider id as a string, or None if absent."""
        pid = self.provider_ids.get("Tmdb")
        return str(pid) if pid not in (None, "") else None


@dataclass
class Task:
    """One entry of ``GET /ScheduledTasks`` (``TaskInfo``)."""

    id: str
    name: str
    key: str
    state: str

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Task":
        return cls(
            id=d.get("Id", ""),
            name=d.get("Name", ""),
            key=d.get("Key", ""),
            state=d.get("State", ""),
        )

    @property
    def is_running(self) -> bool:
        return self.state in ("Running", "Cancelling")


@dataclass
class MediaStream:
    """Subset of a Jellyfin ``MediaStream`` (one track of an item).

    Carries the fields the overlay badges (SPEC \u00a78) need: video height and
    HDR range for ``resolution``/``hdr``, and codec + channel count for
    ``audio``.
    """

    index: int
    type: str                                    # "Video" | "Audio" | "Subtitle"
    codec: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    channels: Optional[int] = None
    video_range: Optional[str] = None            # "SDR" | "PQ" | "HLG"
    video_range_type: Optional[str] = None       # "HDR10" | "HDR10Plus" | "HLG" | "DOVI"
    profile: Optional[str] = None
    level: Optional[float] = None
    bit_rate: Optional[int] = None

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "MediaStream":
        return cls(
            index=int(d.get("Index", 0) or 0),
            type=d.get("Type", ""),
            codec=d.get("Codec"),
            width=d.get("Width"),
            height=d.get("Height"),
            channels=d.get("Channels"),
            video_range=d.get("VideoRange"),
            video_range_type=d.get("VideoRangeType"),
            profile=d.get("Profile"),
            level=d.get("Level"),
            bit_rate=d.get("BitRate"),
        )
