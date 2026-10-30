"""WritePlan models — the single source of truth for the executor and UI.

SPEC §10: "The plan must be the ONLY thing the executor and the UI preview
consume." Stable JSON ordering via ``sort_keys=True`` on serialization.
"""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

# File kinds: same vocabulary as Candidate.kind + "metadata" for NFOs.
FileKind = Literal[
    "metadata", "poster", "backdrop", "logo", "landscape",
    "season_poster", "episode_thumb", "theme_song",
]

# Diff status for a single file.
FileStatus = Literal["create", "update", "unchanged"]

# How an item was matched to TMDb.
MatchMethod = Literal["provider_id", "title_year"]


class FileWrite(BaseModel):
    """One file the executor will write (or confirm unchanged)."""
    target_path: str
    kind: FileKind
    source_ref: str
    content_sha256: str
    status: FileStatus = "create"
    write_method: Literal["sidecar"] = Field(
        default="sidecar",
        description="S9d5: writes target_path on disk. The only write method -- "
                    "'jellyfin_api' (API image upload) was removed 2026-10-26: Jellyfin's own "
                    "library scan kept re-asserting any local poster/backdrop/logo file over an "
                    "API-uploaded one, so it could never reliably override an item that already "
                    "had local artwork.")
    meta: Dict[str, Any] = Field(
        default_factory=dict,
        description="Optional metadata (e.g. K9 format-rule detection facts for this file).")


class ApiAction(BaseModel):
    """A collection or playlist mutation to perform via the Jellyfin API."""
    action: str          # "create" | "sync"
    kind: Literal["collection", "playlist"]
    name: str
    target_id: Optional[str] = None
    add_ids: List[str] = Field(default_factory=list)
    remove_ids: List[str] = Field(default_factory=list)


class UnmatchedItem(BaseModel):
    """A library item that could not be matched to TMDb."""
    item_id: str
    name: str
    reason: str


class ItemPlan(BaseModel):
    """Plan entry for one Jellyfin item (movie or TV show)."""
    item_id: str
    item_name: str
    media_type: Literal["movie", "tvshow"]
    tmdb_id: Optional[int] = None
    matched_by: Optional[MatchMethod] = None
    files: List[FileWrite] = Field(default_factory=list)
    ratings: Dict[str, Optional[float]] = Field(
        default_factory=dict,
        description="Computed ratings (first source in field_sources wins; missing -> None).",
    )
    rating_sources: Dict[str, Optional[str]] = Field(
        default_factory=dict,
        description="Which enabled source supplied each rating (K10), e.g. {'imdb': 'mdblist'}; "
                    "None when no enabled source had a value.",
    )
    format_counts: Dict[str, str] = Field(
        default_factory=dict,
        description="K9 rule-key counts per category for this item's overlays.")
    tv_status: Optional[str] = Field(
        default=None,
        description="S9g1/K7a: computed TV show status from Sonarr (e.g. 'new_season_soon'), "
                    "TV shows only -- see engine.tv_status.compute_tv_status.",
    )


class ProviderWarning(BaseModel):
    """S9d2-fix2 step 6b: a provider that failed during plan build.

    ``count`` is the number of affected (item, kind) lookups; the plan is
    still built — this must be visible, never silent."""
    provider: str
    error: str
    count: int


class WritePlan(BaseModel):
    """The complete plan for a run. Serialized with sort_keys for stability."""
    items: List[ItemPlan] = Field(default_factory=list)
    api_actions: List[ApiAction] = Field(default_factory=list)
    unmatched: List[UnmatchedItem] = Field(default_factory=list)
    provider_warnings: List[ProviderWarning] = Field(default_factory=list)
    total_items: Optional[int] = Field(
        default=None,
        description="Count of Movie/Series items before pagination slicing "
                    "(None = not a paginated build -- `items` is complete).")

    def to_json(self, indent: int = 2) -> str:
        """Serialize to stable JSON (sorted keys)."""
        return self.model_dump_json(indent=indent)

    @classmethod
    def from_json(cls, data: str) -> "WritePlan":
        return cls.model_validate_json(data)


__all__ = [
    "FileWrite", "ApiAction", "UnmatchedItem", "ItemPlan",
    "ProviderWarning", "WritePlan",
    "FileKind", "FileStatus", "MatchMethod",
]
