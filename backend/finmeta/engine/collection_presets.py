"""S9f2: known preset collections the operator can toggle on/off.

A small, hardcoded catalog of ready-made Jellyfin collections: one
"Adaptive Seasonal Collections" master toggle that drives a whole calendar
of holiday collections (``SEASONAL_CALENDAR`` below), and always-on "chart"
style ones (Top 50 Popular, Highest Rated). Activation state lives in
``Config.collection_presets`` (a list of preset ids); the actual
matching/sorting happens in ``engine.plan_builder._build_api_actions``.

The seasonal calendar mirrors Kometa's real ``defaults/movie/seasonal.yml``
(fetched from Kometa-Team/Kometa on GitHub) for the holidays it covers --
windows and keyword/MDBList values are copied from that file, not guessed.
Only holidays with a source Finmeta can actually reach are included:
Kometa's IMDb-list-based holidays (New Year, St. Patrick's, Easter, Memorial
Day, Independence Day, Labor Day, Veteran's Day) are left out because IMDb's
list pages return an empty bot-protection response (HTTP 202) to a plain
server-side fetch -- confirmed directly, not assumed -- and a weaker
genre-only guess for those would tag the wrong movies. The heritage/
awareness-month holidays (Pride, Black History, Women's History, AAPI
Heritage, Latinx Heritage, Disability Month) are intentionally excluded per
operator request, independent of the IMDb issue.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Literal, Optional, Tuple

LibraryType = Literal["movies", "tvshows"]
PresetKind = Literal["seasonal_adaptive", "chart"]
HolidayMatch = Literal["genre", "keyword", "mdblist"]


@dataclass(frozen=True)
class CollectionPreset:
    id: str
    label: str
    description: str
    library_type: LibraryType
    kind: PresetKind
    min_rating: Optional[float] = None
    sort_by: Optional[str] = None  # TMDb details field, e.g. "popularity"
    limit: Optional[int] = None


@dataclass(frozen=True)
class SeasonalHoliday:
    id: str
    label: str  # Jellyfin collection name
    start: Tuple[int, int]  # (month, day), inclusive
    end: Tuple[int, int]    # (month, day), inclusive
    match: HolidayMatch
    genres: List[str] = field(default_factory=list)
    keywords: List[str] = field(default_factory=list)
    mdblist_url: Optional[str] = None


# Windows/sources copied verbatim from Kometa's defaults/movie/seasonal.yml.
SEASONAL_CALENDAR: List[SeasonalHoliday] = [
    SeasonalHoliday(
        id="valentines", label="Valentine's Day", start=(2, 1), end=(2, 29),
        match="genre", genres=["Romance"],
    ),
    SeasonalHoliday(
        id="mothers_day", label="Mother's Day", start=(5, 5), end=(5, 10),
        match="keyword", keywords=["motherhood", "pregnancy"],
    ),
    SeasonalHoliday(
        id="fathers_day", label="Father's Day", start=(6, 15), end=(6, 20),
        match="keyword", keywords=["fatherhood"],
    ),
    SeasonalHoliday(
        id="halloween", label="Halloween", start=(10, 1), end=(10, 31),
        match="genre", genres=["Horror"],
    ),
    SeasonalHoliday(
        id="thanksgiving", label="Thanksgiving", start=(11, 1), end=(11, 30),
        match="keyword", keywords=["thanksgiving"],
    ),
    SeasonalHoliday(
        id="christmas", label="Christmas", start=(12, 1), end=(12, 31),
        match="mdblist",
        mdblist_url="https://mdblist.com/lists/k0meta/christmas-extravaganza",
    ),
]


KNOWN_PRESETS: dict[str, CollectionPreset] = {
    "adaptive_seasonal": CollectionPreset(
        id="adaptive_seasonal", label="Adaptive Seasonal Collections",
        library_type="movies", kind="seasonal_adaptive",
        description="Automatically creates and removes holiday movie collections through the "
        "year: Valentine's Day, Mother's Day, Father's Day, Halloween, Thanksgiving, Christmas. "
        "Each is only present in Jellyfin during its own window. Christmas additionally needs "
        "MDBList enabled with a saved key (Settings > External services) -- same prerequisite "
        "as Trending.",
    ),
    "top_popular_movies": CollectionPreset(
        id="top_popular_movies", label="Top 50 Popular Movies",
        library_type="movies", kind="chart", sort_by="popularity", limit=50,
        description="The 50 most popular movies in the library (TMDb popularity).",
    ),
    "top_popular_shows": CollectionPreset(
        id="top_popular_shows", label="Top 50 Popular Shows",
        library_type="tvshows", kind="chart", sort_by="popularity", limit=50,
        description="The 50 most popular shows in the library (TMDb popularity).",
    ),
    "highest_rated_movies": CollectionPreset(
        id="highest_rated_movies", label="Highest Rated Movies",
        library_type="movies", kind="chart", sort_by="vote_average",
        min_rating=7.5, limit=50,
        description="Movies rated 7.5+ on TMDb, highest rated first.",
    ),
}


def is_holiday_active_now(holiday: SeasonalHoliday, now: Optional[datetime] = None) -> bool:
    """None of the calendar's windows wrap the year boundary, so a plain
    (month, day) tuple range check is correct."""
    now = now or datetime.now(timezone.utc)
    md = (now.month, now.day)
    return holiday.start <= md <= holiday.end


__all__ = [
    "CollectionPreset", "SeasonalHoliday", "SEASONAL_CALENDAR", "KNOWN_PRESETS",
    "is_holiday_active_now",
]
