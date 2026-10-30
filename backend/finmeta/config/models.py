"""Pydantic v2 models for Finmeta's config (SPEC §3, help per SPEC S9 §B).

Every model uses ``extra="forbid"`` so unknown keys are an error. Library and
collection definitions are discriminated unions keyed on ``type``.

Help (S9 §B): every field carries a one-line ``description`` (short) and a
``json_schema_extra={"help": ...}`` long-form (markdown) so the UI can show an
(i) panel and ``GET /api/help`` can serve them without hardcoding help in the
frontend. A unit test fails if any config field lacks help.
"""
from __future__ import annotations

from typing import Annotated, ClassVar, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class _Strict(BaseModel):
    """Base that rejects unknown keys (spec: unknown keys are an error)."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


# ---------------------------------------------------------------------------
# server + path mappings
# ---------------------------------------------------------------------------

class ServerConfig(_Strict):
    url: str = Field(
        description="Jellyfin server URL (scheme + host + port).",
        json_schema_extra={"help": "The address of your Jellyfin server, e.g. `http://jellyfin:8096` "
        "or `http://192.168.1.10:8096`. Inside a container, `http://localhost:8096` points at the "
        "container itself, not your Jellyfin — use the LAN IP, or the container/service name when "
        "Finmeta and Jellyfin share a Docker network. A trailing slash is stripped automatically. "
        "Changing this (or base_path / verify_ssl / timeout) hot-reloads the client — no restart. "
        "Reverse-proxy example: `url: http://home.example.com` with `base_path: /jellyfin` calls "
        "`http://home.example.com/jellyfin/System/Info`."})
    api_key_env: str = Field(
        default="JELLYFIN_API_KEY",
        description="Name of the environment variable holding the Jellyfin API key.",
        json_schema_extra={"help": "Only the *name* of the env var that holds your Jellyfin API key "
        "(never the key itself). The key is read from that env var or from Settings. Changing this "
        "name does not change where the key comes from — it just renames the lookup."})
    base_path: Optional[str] = Field(
        default=None,
        description="Optional URL prefix when Jellyfin is behind a reverse proxy.",
        json_schema_extra={"help": "Prefix added to every API path when Jellyfin is served under a "
        "sub-path by a reverse proxy. With `url: http://home.example.com` and `base_path: /jellyfin`, "
        "Finmeta calls `http://home.example.com/jellyfin/System/Info`. Leave empty for the normal "
        "case; a leading slash is optional and is normalized."})
    verify_ssl: bool = Field(
        default=True,
        description="Verify TLS certificates for https connections.",
        json_schema_extra={"help": "Verify the server's TLS certificate for `https` URLs (default "
        "on). Turn off only for a self-signed cert you trust (e.g. a home reverse proxy); then the "
        "connection is not protected from tampering."})
    timeout: float = Field(
        default=30.0,
        gt=0,
        description="Per-request timeout for the Jellyfin client, in seconds.",
        json_schema_extra={"help": "How long to wait, in seconds, for each Jellyfin request before "
        "giving up (default 30). Raise it for a slow or distant server."})


class PathMapping(_Strict):
    """One Jellyfin-path -> local-path translation (SPEC override O6)."""
    jellyfin: str = Field(
        description="Path as seen from Jellyfin (starts with its media root).",
        json_schema_extra={"help": "The media path *as Jellyfin sees it*, e.g. `/media`. Every "
        "Jellyfin path Finmeta touches is translated through this mapping before it is read from or "
        "written to disk. A path with no mapping is skipped with an error (never guessed)."})
    local: str = Field(
        description="Corresponding path on the local filesystem.",
        json_schema_extra={"help": "The local path that maps to the Jellyfin path, e.g. "
        "`/media` in the container or `/work/fixture/library` in dev. This is where sidecar files "
        "(NFOs, artwork) are written."})


# ---------------------------------------------------------------------------
# sources
# ---------------------------------------------------------------------------

class TmdbSource(_Strict):
    enabled: bool = Field(
        default=True,
        description="Enable TMDb as a metadata/artwork source.",
        json_schema_extra={"help": "Turn TMDb on/off. TMDb provides metadata, ratings and most "
        "artwork. On by default. Disabling it makes matching/artwork much weaker."})
    api_key_env: str = Field(
        default="TMDB_KEY",
        description="Name of the environment variable holding the TMDb API key.",
        json_schema_extra={"help": "Env var name holding your TMDb API v3 key (not the key itself). "
        "Set the key via that env var or in Settings. Get one at "
        "https://www.themoviedb.org/settings/api."})


class FanartSource(_Strict):
    enabled: bool = Field(
        default=False,
        description="Enable fanart.tv as an artwork source.",
        json_schema_extra={"help": "Turn fanart.tv on/off. Off by default; useful for extra logos and "
        "landscape art. Requires a fanart.tv key to be set."})
    api_key_env: str = Field(
        default="FANART_KEY",
        description="Name of the environment variable holding the fanart.tv API key.",
        json_schema_extra={"help": "Env var name holding your fanart.tv key (not the key itself). "
        "Get one at https://www.fanart.tv/user/apikey. Optional."})


class LocalSource(_Strict):
    enabled: bool = Field(
        default=False,
        description="Enable the local asset directory as an artwork source.",
        json_schema_extra={"help": "Use artwork files you place on disk (e.g. hand-made overlays or "
        "theme songs). Off by default. See `path` for where to put them."})
    path: str = Field(
        default="/assets",
        description="Local directory holding user-supplied artwork.",
        json_schema_extra={"help": "Directory (in the local filesystem) where you drop your own "
        "artwork assets. Only used when the local source is enabled. In the shipped container this "
        "is typically `/config/assets`."})


class OmdbSource(_Strict):
    enabled: bool = Field(
        default=False,
        description="Enable OMDb as a ratings source.",
        json_schema_extra={"help": "Turn OMDb on/off. Off by default. Provides IMDb "
        "rating, Rotten Tomatoes critic score and Metacritic score. Requires an "
        "API key (the `omdb_key` secret or env var OMDB_KEY). Responses are "
        "cached with a 7 day TTL. Get a key at https://www.omdbapi.com/apikey.aspx."})
    api_key_env: str = Field(
        default="OMDB_KEY",
        description="Name of the environment variable holding the OMDb API key.",
        json_schema_extra={"help": "Env var name holding your OMDb API key (not the "
        "key itself). Set the key via that env var or in Settings. Get a free key at "
        "https://www.omdbapi.com/apikey.aspx."})


class MdblistSource(_Strict):
    enabled: bool = Field(
        default=False,
        description="Enable MDBList as a ratings source.",
        json_schema_extra={"help": "Turn MDBList on/off. Off by default. Aggregates IMDb, Rotten "
        "Tomatoes (critic + audience), Metacritic, Trakt, Letterboxd and TMDb ratings in one call. "
        "Requires an API key (the `mdblist_key` secret or env var MDBLIST_KEY). Responses are cached "
        "with a 7 day TTL and the client is rate-limit aware."})
    api_key_env: str = Field(
        default="MDBLIST_KEY",
        description="Name of the environment variable holding the MDBList API key.",
        json_schema_extra={"help": "Env var name holding your MDBList API key (not the key itself). "
        "Set the key via that env var or in Settings. Get a key at https://api.mdblist.com. Free "
        "tier is 1,000 requests/day."})


class ImdbDatasetsSource(_Strict):
    enabled: bool = Field(
        default=False,
        description="Enable IMDb bulk datasets as a ratings source.",
        json_schema_extra={"help": "Turn the IMDb bulk dataset on/off. Off by default. Downloads "
        "IMDb's `title.ratings.tsv.gz` (at most once per 24h), indexes it by tconst and provides the "
        "IMDb rating + vote count with no API key. Note: this is the IMDb non-commercial dataset — "
        "see the IMDb datasets help in the External services tab for the licence."})


class MediaAppInstance(_Strict):
    name: str = Field(
        description="Unique instance name (e.g. `Radarr`, `Sonarr4K`).",
        json_schema_extra={"help": "A unique name for this Sonarr/Radarr instance, e.g. `Radarr` or "
        "`Sonarr4K`. Used to identify the instance and to look up the secret holding its API key."})
    url: str = Field(
        description="Instance URL (scheme + host + port).",
        json_schema_extra={"help": "The address of this Sonarr/Radarr instance, e.g. "
        "`http://192.168.1.10:7878`. Same container/`localhost` caveat as the Jellyfin URL: inside a "
        "container use the LAN IP or the container name."})
    api_key: str = Field(
        description="Name of the secret holding this instance's API key.",
        json_schema_extra={"help": "The *name* of the secret that holds this instance's API key "
        "(e.g. `radarr_key`). The key value is set in Settings and never stored here — only this "
        "name lives in the config."})
    timeout: float = Field(
        default=30.0,
        gt=0,
        description="Per-request timeout, in seconds.",
        json_schema_extra={"help": "How long to wait, in seconds, for each request to this instance "
        "before giving up (default 30)."})
    verify_ssl: bool = Field(
        default=True,
        description="Verify TLS certificates for https connections.",
        json_schema_extra={"help": "Verify the server's TLS certificate for `https` URLs (default "
        "on). Turn off only for a trusted self-signed cert."})
    enabled: bool = Field(
        default=True,
        description="Include this instance in Sonarr/Radarr operations.",
        json_schema_extra={"help": "Turn this instance on/off without removing it. Disabled "
        "instances are skipped by the Sonarr/Radarr integration but still show up in Settings."})


class MediaAppsConfig(_Strict):
    sonarr: List[MediaAppInstance] = Field(
        default_factory=list,
        description="Sonarr instances.",
        json_schema_extra={"help": "Your Sonarr instances (e.g. `Sonarr`, `Sonarr4K`). Each has a "
        "url, the name of the secret holding its API key, timeout, verify_ssl and enabled."})
    radarr: List[MediaAppInstance] = Field(
        default_factory=list,
        description="Radarr instances.",
        json_schema_extra={"help": "Your Radarr instances (e.g. `Radarr`, `Radarr4K`). Each has a "
        "url, the name of the secret holding its API key, timeout, verify_ssl and enabled."})

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_instances(cls, data):
        """Migrate the S9b2 single ``instances`` list (config shape only) into
        the ``sonarr``/``radarr`` lists. Instances whose name mentions Sonarr go
        to sonarr; everything else to radarr. The old key is dropped."""
        if isinstance(data, dict) and "instances" in data:
            inst = data.pop("instances") or []
            sonarr: List[dict] = []
            radarr: List[dict] = []
            for i in inst:
                (sonarr if "sonarr" in str(i.get("name", "")).lower() else radarr).append(i)
            data.setdefault("sonarr", sonarr)
            data.setdefault("radarr", radarr)
        return data

    @model_validator(mode="after")
    def _check_unique_names(self):
        """Names must be unique within each kind (Sonarr / Radarr)."""
        for kind in ("sonarr", "radarr"):
            names = [i.name for i in getattr(self, kind)]
            dupes = sorted({n for n in names if names.count(n) > 1})
            if dupes:
                raise ValueError(
                    f"media_apps.{kind}: duplicate instance name(s): "
                    + ", ".join(repr(d) for d in dupes)
                    + " — names must be unique within a kind")
        return self


class SourcesConfig(_Strict):
    tmdb: TmdbSource = Field(
        default_factory=TmdbSource,
        description="TMDb source settings.",
        json_schema_extra={"help": "Settings for the TMDb source (enabled + which env var holds the "
        "key). See the TMDb help in the External services tab."})
    fanart: FanartSource = Field(
        default_factory=FanartSource,
        description="fanart.tv source settings.",
        json_schema_extra={"help": "Settings for the fanart.tv source (enabled + which env var holds "
        "the key). See the fanart.tv help in the External services tab."})
    local: LocalSource = Field(
        default_factory=LocalSource,
        description="Local asset source settings.",
        json_schema_extra={"help": "Settings for the local on-disk artwork source (enabled + path). "
        "Use this to supply your own artwork files."})
    omdb: OmdbSource = Field(
        default_factory=OmdbSource,
        description="OMDb ratings source settings.",
        json_schema_extra={"help": "Settings for the OMDb ratings source (enabled + which env var "
        "holds the key). See the OMDb help in the External services tab."})
    mdblist: MdblistSource = Field(
        default_factory=MdblistSource,
        description="MDBList ratings source settings.",
        json_schema_extra={"help": "Settings for the MDBList ratings source (enabled + which env var "
        "holds the key). See the MDBList help in the External services tab."})
    imdb_datasets: ImdbDatasetsSource = Field(
        default_factory=ImdbDatasetsSource,
        description="IMDb bulk-dataset ratings source settings.",
        json_schema_extra={"help": "Settings for the IMDb bulk-dataset ratings source (enabled). No "
        "key — it downloads IMDb's public title.ratings dataset. See the IMDb datasets help."})
    field_sources: Dict[str, List[str]] = Field(
        default_factory=lambda: {
            "rating_imdb": ["mdblist", "omdb", "imdb_datasets", "jellyfin"],
            "rating_rt_critic": ["mdblist", "omdb", "jellyfin"],
            "rating_rt_audience": ["mdblist"],
            "rating_metacritic": ["mdblist", "omdb"],
            "rating_tmdb": ["mdblist", "tmdb"],
            "rating_trakt": ["mdblist"],
            "rating_letterboxd": ["mdblist"],
        },
        description=(
            "Per-rating-field precedence: an ordered list of provider names. "
            "The first provider that has a non-None value wins. "
            "Valid field names: rating_imdb, rating_rt_critic, rating_rt_audience, "
            "rating_metacritic, rating_tmdb, rating_trakt, rating_letterboxd. "
            "Valid provider names: mdblist, omdb, imdb_datasets, jellyfin, tmdb."
        ),
        json_schema_extra={"help": (
            "Which provider supplies each rating value, in priority order. The first "
            "provider in the list that has a value for the item wins. Defaults: "
            "`rating_imdb: [mdblist, omdb, imdb_datasets, jellyfin]`, "
            "`rating_rt_critic: [mdblist, omdb, jellyfin]`, `rating_metacritic: "
            "[mdblist, omdb]`, `rating_tmdb: [mdblist, tmdb]`, and `rating_rt_audience`, "
            "`rating_trakt`, `rating_letterboxd` all default to `[mdblist]`. Reorder these in the "
            "Rating sources tab; only providers that can supply a rating appear for that field."
        )},
    )
    precedence: List[str] = Field(
        default_factory=lambda: ["local", "fanart", "tmdb"],
        description="Order in which sources are consulted; first hit wins.",
        json_schema_extra={"help": "For each artwork kind, sources are tried in this order and the "
        "first one that has a candidate wins. Default `[local, fanart, tmdb]`. Only names that are "
        "configured may appear."})

    _VALID: ClassVar[set] = {"local", "fanart", "tmdb", "omdb"}
    _FIELD_SOURCE_FIELDS: ClassVar[set] = {
        "rating_imdb", "rating_rt_critic", "rating_rt_audience", "rating_metacritic",
        "rating_tmdb", "rating_trakt", "rating_letterboxd",
    }
    _FIELD_SOURCE_PROVIDERS: ClassVar[set] = {
        "mdblist", "omdb", "imdb_datasets", "jellyfin", "tmdb",
    }

    @field_validator("field_sources")
    @classmethod
    def _check_field_sources(cls, v: Dict[str, List[str]]) -> Dict[str, List[str]]:
        for field_name, providers in v.items():
            if field_name not in cls._FIELD_SOURCE_FIELDS:
                raise ValueError(
                    f"unknown rating field {field_name!r}; "
                    f"must be one of {sorted(cls._FIELD_SOURCE_FIELDS)}")
            for prov in providers:
                if prov not in cls._FIELD_SOURCE_PROVIDERS:
                    raise ValueError(
                        f"unknown provider {prov!r} in field_sources[{field_name!r}]; "
                        f"must be one of {sorted(cls._FIELD_SOURCE_PROVIDERS)}")
        return v

    @field_validator("precedence")
    @classmethod
    def _check_precedence(cls, v: List[str]) -> List[str]:
        for name in v:
            if name not in cls._VALID:
                raise ValueError(f"unknown source {name!r} in precedence; must be one of {sorted(cls._VALID)}")
        if len(set(v)) != len(v):
            raise ValueError("duplicate source in precedence")
        return v


# ---------------------------------------------------------------------------
# libraries (profiles)
# ---------------------------------------------------------------------------

class Metadata(_Strict):
    enabled: bool = Field(
        default=True,
        description="Write a sidecar .nfo with metadata for this library.",
        json_schema_extra={"help": "When on, Finmeta writes a metadata NFO (title, overview, rating, "
        "genres, etc.) next to each item. On by default. Disable to leave metadata untouched."})
    fields: Optional[List[str]] = Field(
        default=None,
        description="Which metadata fields to write (None = the sensible default set).",
        json_schema_extra={"help": "Restrict which metadata fields go into the NFO, e.g. "
        "`[title, overview, rating]`. Leave empty/null for the default set (title, originaltitle, "
        "overview, tagline, year, genres, studios, rating). Unknown field names are an error."})



class ArtworkSelection(_Strict):
    """Which candidate is picked automatically per artwork kind (S9d4a)."""
    preset: Literal["best_rated", "random_top_n"] = Field(
        default="best_rated",
        description="How the automatic choice is made among candidates.",
        json_schema_extra={"help": "`best_rated` picks the highest-rated candidate (TMDb "
        "`vote_average`, `vote_count` as tiebreak). `random_top_n` picks one of the top N "
        "candidates — deterministically: the same item, kind and reshuffle count always give "
        "the same image; use the Reshuffle button to change it."})
    top_n: int = Field(
        default=3, ge=1, le=50,
        description="How many of the best candidates `random_top_n` may choose from.",
        json_schema_extra={"help": "For the `random_top_n` preset: how many of the best-ranked "
        "candidates are eligible for the (deterministic) pick. Default 3."})


class PosterRotation(_Strict):
    """Automatically reshuffle+reapply the poster of a library's top-rated
    items on a cron schedule (S9d6), so the best items' artwork visibly
    changes over time instead of staying static forever.

    Only has a visible effect once ``artwork_selection`` for the library
    resolves to ``random_top_n`` with ``top_n >= 2`` (there must be more than
    one candidate poster to rotate between) -- enabling this field forces
    that via ``_LibraryBase._apply_poster_rotation_requirements`` below,
    rather than silently doing nothing.
    """
    enabled: bool = Field(
        default=False,
        description="Rotate the poster of this library's top-rated items weekly (or per `cron`).",
        json_schema_extra={"help": "When on, a scheduled job reshuffles and reapplies the poster "
        "of this library's `item_count` highest-rated items on the `cron` schedule. Off by "
        "default -- opt in per library."})
    item_count: int = Field(
        default=5, ge=1, le=50,
        description="How many of the library's highest-rated items get rotated.",
        json_schema_extra={"help": "The N highest-rated items (by Jellyfin's `CommunityRating`) "
        "in this library whose poster gets reshuffled+reapplied each run. Default 5."})
    cron: str = Field(
        default="0 4 * * 0",
        description="Cron expression for when the rotation runs.",
        json_schema_extra={"help": "A 5-field cron expression, default `0 4 * * 0` (Sundays "
        "04:00). Same syntax as the Schedules tab."})

    @field_validator("cron")
    @classmethod
    def _check_cron(cls, v: str) -> str:
        from croniter import croniter
        if not croniter.is_valid(v):
            raise ValueError(f"invalid cron expression: {v!r}")
        return v


class MovieArtwork(_Strict):
    poster: bool = Field(
        default=False,
        description="Write a poster file.",
        json_schema_extra={"help": "Write a `poster.jpg` next to the movie (the 2:3 main artwork). "
        "Off by default; turn on to have Finmeta manage posters."})
    backdrop: bool = Field(
        default=False,
        description="Write a backdrop/landscape file.",
        json_schema_extra={"help": "Write a `fanart.jpg`/backdrop (the 16:9 landscape artwork). Off "
        "by default."})
    logo: bool = Field(
        default=False,
        description="Write a logo file.",
        json_schema_extra={"help": "Write a `logo.png` (transparent title logo). Off by default. "
        "Mostly useful for TV; rarely available for movies."})
    landscape: bool = Field(
        default=False,
        description="Write a separate landscape image.",
        json_schema_extra={"help": "Write a `landscape.jpg`. Distinct from `backdrop` on some "
        "layouts. Off by default."})


class TvArtwork(_Strict):
    poster: bool = Field(
        default=False,
        description="Write a poster file.",
        json_schema_extra={"help": "Write a show `poster.jpg` (2:3). Off by default."})
    backdrop: bool = Field(
        default=False,
        description="Write a backdrop/landscape file.",
        json_schema_extra={"help": "Write a show `fanart.jpg`/backdrop (16:9). Off by default."})
    logo: bool = Field(
        default=False,
        description="Write a logo file.",
        json_schema_extra={"help": "Write a show `logo.png` (transparent title logo). Off by default. "
        "Very useful for TV shows."})
    season_posters: bool = Field(
        default=False,
        description="Write per-season posters.",
        json_schema_extra={"help": "Write `seasonXX.poster.jpg` for each season. Off by default."})
    episode_thumbs: bool = Field(
        default=False,
        description="Write per-episode thumbnails.",
        json_schema_extra={"help": "Write a thumbnail image for each episode. Off by default; "
        "noisy and large for long-running shows."})


class _LibraryBase(_Strict):
    metadata: Metadata = Field(
        default_factory=Metadata,
        description="Metadata (NFO) settings for this library.",
        json_schema_extra={"help": "Controls whether and what metadata NFO is written for this "
        "library (enabled + which fields). See Metadata help."})
    artwork: "MovieArtwork | TvArtwork" = Field(
        default_factory=MovieArtwork,
        description="Artwork settings for this library.",
        json_schema_extra={"help": "Which artwork files to write for this library (poster, backdrop, "
        "logo, ...). The exact available options depend on the library type (movies vs tvshows). "
        "All off by default."})
    theme_song: bool = Field(
        default=False,
        description="Copy the show's theme song from the local asset dir.",
        json_schema_extra={"help": "Place a theme song for each show, sourced from the local asset "
        "directory only (v1 does not download from YouTube — see SPEC override O2). Off by default."})
    enabled: bool = Field(
        default=False,
        description="Whether this library is managed at all.",
        json_schema_extra={"help": "Turn this library on/off. Libraries that appear in Jellyfin but are "
        "not mentioned in config stay off and untouched (anime libraries included). Default `false` so a "
        "newly added Jellyfin library never gets written to until you explicitly enable it."})
    overlay_profile: Optional[str] = Field(
        default=None,
        description="Name of the overlay profile applied to this library's items.",
        json_schema_extra={"help": "Which named overlay profile (from `overlays.profiles`) is applied to "
        "this library's items during plan/apply. A profile supplies the overlay element list plus per-"
        "element style/format-rule overrides. Leave unset for no overlays at all. Unknown names are a "
        "config error."})
    overlays: List[str] = Field(
        default_factory=list,
        description="Deprecated: overlay element names (use overlay_profile instead).",
        json_schema_extra={"deprecated": True,
        "help": "Deprecated (S9d4c). Kept only so older configs keep loading: at config-load time, "
        "a non-empty list here with no profile (or a profile with no `elements`) is migrated into "
        "an auto-generated profile `<library>-auto` holding this list, assigned via `overlay_profile`. "
        "From then on only the profile is read. Set `overlay_profile` + `overlays.profiles` directly "
        "instead of this list in new configs."})


    artwork_selection: Optional[ArtworkSelection] = Field(
        default=None,
        description="Per-library automatic artwork selection (None = inherit global).",
        json_schema_extra={"help": "Override the global `artwork_selection` for this library. "
        "Leave unset to inherit the global preset/top_n."})

    artwork_write_method: Optional[Literal["sidecar"]] = Field(
        default=None,
        description="Per-library artwork write method override (None = inherit global).",
        json_schema_extra={"help": "`sidecar` is the only write method (see the global "
        "`artwork_write_method` for why `jellyfin_api` was removed 2026-10-26) -- this override "
        "can no longer actually differ from the global setting. Kept only so an older "
        "config.yml with this key already set keeps loading."})

    poster_rotation: PosterRotation = Field(
        default_factory=PosterRotation,
        description="Scheduled poster reshuffle for this library's top-rated items.",
        json_schema_extra={"help": "Off by default. See PosterRotation help."})

    @model_validator(mode="after")
    def _apply_poster_rotation_requirements(self) -> "_LibraryBase":
        """Rotation only does anything visible with random_top_n/top_n>=2
        (operator decision 2026-10-25: auto-fix rather than silently no-op)."""
        if self.poster_rotation.enabled:
            sel = self.artwork_selection or ArtworkSelection()
            sel.preset = "random_top_n"
            if sel.top_n < 2:
                sel.top_n = 3
            self.artwork_selection = sel
        return self


class MovieLibrary(_LibraryBase):
    type: Literal["movies"] = Field(
        description="Library type discriminator (must be `movies`).",
        json_schema_extra={"help": "Fixed value `movies`. Tells Finmeta this library is a movie "
        "library and selects the movie artwork options."})
    artwork: MovieArtwork = Field(
        default_factory=MovieArtwork,
        description="Movie artwork settings.",
        json_schema_extra={"help": "Movie artwork options (poster, backdrop, logo, landscape). See "
        "each option's help."})


class TvLibrary(_LibraryBase):
    type: Literal["tvshows"] = Field(
        description="Library type discriminator (must be `tvshows`).",
        json_schema_extra={"help": "Fixed value `tvshows`. Tells Finmeta this library is a TV "
        "library and selects the TV artwork options."})
    artwork: TvArtwork = Field(
        default_factory=TvArtwork,
        description="TV artwork settings.",
        json_schema_extra={"help": "TV artwork options (poster, backdrop, logo, season_posters, "
        "episode_thumbs). See each option's help."})


LibraryConfig = Annotated[
    Union[MovieLibrary, TvLibrary],
    Field(discriminator="type"),
]


# ---------------------------------------------------------------------------
# overlays
# ---------------------------------------------------------------------------

class OverlayStyle(_Strict):
    position: str = Field(
        default="top_left",
        description="Base corner/edge for the overlay block.",
        json_schema_extra={"help": "Default anchor for overlay elements (e.g. `top_left`, "
        "`bottom_left`). Individual elements can override this. The fine-grained layout is managed "
        "in the Overlay editor (later S9 session)."})
    margin: int = Field(
        default=20,
        description="Pixel margin from the poster edge.",
        json_schema_extra={"help": "Distance (in pixels) between the overlay and the poster edge. "
        "Default 20."})
    font_size: int = Field(
        default=42,
        description="Base font size (pixels).",
        json_schema_extra={"help": "Base font size in pixels for overlay text. Default 42. Scale it "
        "up for very wide posters. Per-element overrides live in the Overlay editor."})
    bg: str = Field(
        default="#000000B0",
        description="Backdrop color (hex, with optional alpha).",
        json_schema_extra={"help": "Background/plate color behind overlay text, hex with 8 digits for "
        "alpha (`#RRGGBBAA`). Default `#000000B0` = 69% opaque black."})
    fg: str = Field(
        default="#FFFFFF",
        description="Foreground/text color (hex).",
        json_schema_extra={"help": "Text color for overlays, hex `#RRGGBB`. Default `#FFFFFF` (white)."})
    radius: int = Field(
        default=12,
        description="Corner radius of the backdrop plate (pixels).",
        json_schema_extra={"help": "Corner rounding (pixels) of the semi-transparent plate behind "
        "overlay text. Default 12. 0 = sharp corners."})


_LOGO_PACKS = ("none", "kometa", "custom")


class StatusOverlayConfig(_Strict):
    """S9g1/K7a: TV show status (from Sonarr) -- overlay element + optional collections."""
    recent_days: int = Field(
        default=14,
        ge=0,
        description="How many days a just-aired premiere/finale still counts as 'just happened'.",
        json_schema_extra={"help": "Controls the *_aired statuses (new_season_started, "
        "season_finale_aired, final_episode_aired): an episode that aired more than this many days "
        "ago no longer counts as 'just happened' and the show falls back to a plain status "
        "(returning/ended) instead. Default 14."})
    date_style: Literal["relative", "classic"] = Field(
        default="relative",
        description="How a dated status (e.g. 'NEW SEASON') shows its date on the badge.",
        json_schema_extra={"help": "`relative` (default): TODAY/TOMORROW for the next two days, "
        "the weekday name for days 2-6 out, else DD.MM. `classic`: always DD.MM, regardless of how "
        "far away the date is."})
    create_collections: bool = Field(
        default=False,
        description="Also create a Jellyfin collection per status (e.g. 'New Season Soon').",
        json_schema_extra={"help": "Off by default. When on, every TV show with a computed status "
        "(see the `status` overlay element) is added to a Jellyfin collection named after that "
        "status -- one collection per status value present, created the same way as "
        "`collections` (SPEC override O1, no collection.nfo). Override a status's collection name "
        "via `collection_names`."})
    collection_names: Dict[str, str] = Field(
        default_factory=dict,
        description="Override the default collection name for a status (status key -> name).",
        json_schema_extra={"help": "Only used when `create_collections` is on. Maps a status key "
        "(e.g. `new_season_soon`, `returning`, `ended`) to a custom Jellyfin collection name; any "
        "status not listed here uses a title-cased default (e.g. `new_season_soon` -> "
        "'New Season Soon')."})


class TrendingListConfig(_Strict):
    """S9g2/K7b: one MDBList list to mirror into a Jellyfin collection."""
    url: str = Field(
        description="Public MDBList list URL, e.g. https://mdblist.com/lists/<user>/<slug>.",
        json_schema_extra={"help": "The list's public page URL on mdblist.com (open the list on "
        "mdblist.com and copy the address bar). MDBList's own 'Popular Lists' page has ready-made "
        "Trakt Trending Movies/TV Shows mirrors you can point at directly."})
    collection_name: str = Field(
        description="Jellyfin collection name to create/sync from this list.",
        json_schema_extra={"help": "The Jellyfin collection this list's matched items are added to "
        "(SPEC override O1 -- created via the Jellyfin API, no collection.nfo). Only items already "
        "present in a managed library and matched by TMDb id are added; items MDBList lists that "
        "aren't in your library are skipped (not downloaded -- see K7b)."})
    limit: int = Field(
        default=0,
        ge=0,
        description="Max items to pull from the list (0 = the list's own size).",
        json_schema_extra={"help": "Cap how many of the list's (already-ranked) items to fetch. "
        "0 = no cap."})
    rank_overlay: bool = Field(
        default=False,
        description="Tag this list's top 10 matched items with their rank for the trending_rank element.",
        json_schema_extra={"help": "When on, items ranked 1-10 on this list get a '#<rank>' value for "
        "the `trending_rank` overlay element (e.g. a TOP 10 badge). Off by default. When more than "
        "one list has this on, the FIRST list (in config order) that ranks an item wins."})


class TrendingConfig(_Strict):
    """S9g2/K7b: trending collections + optional TOP 10 rank overlay, via MDBList."""
    lists: List[TrendingListConfig] = Field(
        default_factory=list,
        description="MDBList lists to mirror as Jellyfin collections.",
        json_schema_extra={"help": "Requires `sources.mdblist.enabled` and a saved MDBList API key "
        "(the `mdblist_key` secret). Each entry is one public MDBList list, mirrored into one "
        "Jellyfin collection of matched library items; optionally also tags its top 10 with a rank "
        "for the `trending_rank` overlay element."})


class OverlaysConfig(_Strict):
    logo_packs: List[Literal["none", "kometa", "custom"]] = Field(
        default_factory=list,
        description="Overlay logo packs in priority order (first match wins).",
        json_schema_extra={"help": "Logo sources for rating badges and resolution/HDR/audio image "
        "badges, in priority order — the first pack that has an image for a badge wins. `kometa` "
        "uses the pack installed via the Install-Kometa-logos button into "
        "`<config>/assets/logos/kometa/`. `custom` reads flat files from "
        "`<config>/assets/logos/<key>.png` (your own artwork). Leave empty (or use no sources) to "
        "draw text pills only. Missing images always fall back to the current text pill."})

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_logo_pack(cls, data):
        """Accept legacy single-value ``logo_pack`` (S9c8) as a one-entry list."""
        if isinstance(data, dict) and "logo_packs" not in data and "logo_pack" in data:
            v = data.pop("logo_pack")
            data["logo_packs"] = [v] if v else []
        return data

    @property
    def logo_pack(self) -> str:
        """Legacy accessor: first source or ``none`` (read-only, for old callers)."""
        return self.logo_packs[0] if self.logo_packs else "none"
    format_rules: Dict[str, List[dict]] = Field(
        default_factory=dict,
        description="Per-category K9 overlay detection rules; empty uses K9 defaults.",
        json_schema_extra={
            "help": ("Conditional badge detection (K9): resolution/HDR/audio badges are drawn only when an enabled rule matches the item's MediaStreams; no match means no badge. Leave empty to use built-in Kometa-style defaults. Editable in the Overlay editor's Format rules section: toggle a rule on/off or change its priority; conditions and labels are read-only."),
        })

    @field_validator("format_rules")
    @classmethod
    def _check_format_rules(cls, v: Dict[str, List[dict]]) -> Dict[str, List[dict]]:
        """Server-side validation for overlays.format_rules (K9).

        Categories must be known; each rule needs a unique non-empty key, a
        non-empty match expression and label, an integer priority >= 0 and a
        boolean enabled flag. Raises ValueError -> 400 on PUT /api/config.
        """
        from finmeta.engine.format_rules import DEFAULT_FORMAT_RULES
        allowed = set(DEFAULT_FORMAT_RULES)
        for cat, rules in v.items():
            if cat not in allowed:
                raise ValueError(
                    f"unknown format-rule category {cat!r}; expected one of {sorted(allowed)}")
            if not isinstance(rules, list):
                raise ValueError(f"format_rules[{cat!r}] must be a list")
            seen: set = set()
            for i, rule in enumerate(rules):
                if not isinstance(rule, dict):
                    raise ValueError(f"format_rules[{cat!r}][{i}] must be a mapping")
                key = str(rule.get("key") or "").strip()
                if not key:
                    raise ValueError(f"format_rules[{cat!r}][{i}] has an empty key")
                if key in seen:
                    raise ValueError(f"duplicate rule key {key!r} in format_rules[{cat!r}]")
                seen.add(key)
                if not str(rule.get("match") or "").strip():
                    raise ValueError(f"format_rules[{cat!r}][{i}] ({key}) has an empty match condition")
                if not str(rule.get("label") or "").strip():
                    raise ValueError(f"format_rules[{cat!r}][{i}] ({key}) has an empty label")
                prio = rule.get("priority", 0)
                if isinstance(prio, bool) or not isinstance(prio, int) or prio < 0:
                    raise ValueError(f"format_rules[{cat!r}][{i}] ({key}) priority must be an integer >= 0")
                if not isinstance(rule.get("enabled", True), bool):
                    raise ValueError(f"format_rules[{cat!r}][{i}] ({key}) enabled must be a boolean")
        return v
    style: OverlayStyle = Field(
        default_factory=OverlayStyle,
        description="Base overlay style (position, fonts, colors).",
        json_schema_extra={"help": "Global defaults for the overlay compositor (position, margin, "
        "font size, colors, radius). Per-element layout lives under ``user_styles`` and is edited in "
        "the Overlay editor. The compositor renders from the original poster so re-runs never stack "
        "(SPEC override O7)."})
    user_styles: Dict[str, dict] = Field(
        default_factory=dict,
        description="User-edited overlay style documents, keyed by name.",
        json_schema_extra={"help": "Named overlay styles saved from the Overlay editor. Each value "
        "is a full style document (base settings + per-element overrides); presets are always cloned "
        "into this map before editing so built-in presets stay read-only."})
    profiles: Dict[str, dict] = Field(
        default_factory=dict,
        description="Named overlay profiles; libraries reference one by name.",
        json_schema_extra={"help": "Named overlay profiles (K8/D2). A library's `overlay_profile` "
        "points at one of these names. Each profile may set: `style` (a full style document merged over "
        "`overlays.style`), `format_rules` (per-category K9 rule overrides merged over "
        "`overlays.format_rules`), and `elements` (the overlay element list, falling back to the "
        "library's `overlays` when absent). Libraries with no profile get no overlays. YAML-editable; "
        "unknown keys are an error."})
    stream_batch_size: int = Field(
        default=50, ge=1, le=500,
        description="Items per /Items?Ids= batch when fetching MediaStreams.",
        json_schema_extra={"help": "Apply/plan fetches MediaStreams in batches of this many item IDs "
        "per `GET /Items?Ids=a,b,c` request instead of one request per item. Default 50 (1-500). "
        "N items use ceil(N/batch) requests."})

    @field_validator("profiles")
    @classmethod
    def _check_profiles(cls, v: Dict[str, dict]) -> Dict[str, dict]:
        """Server-side validation for overlays.profiles (K8/D3).

        Each profile may set ``packs`` (list of pack names) and
        ``pack_overrides`` — the D3 shape is exactly
        ``{<pack>: {<category>: {"enabled": <bool>}}}``. Anything else, e.g.
        the old flat ``{<category>: false}`` shape, fails validation with a
        message naming the bad key instead of being silently ignored at
        plan/preview time (SPEC S9 D3).
        """
        for name, prof in v.items():
            if not isinstance(prof, dict):
                raise ValueError(f"overlays.profiles[{name!r}] must be a mapping")
            overrides = prof.get("pack_overrides")
            if overrides is None:
                continue
            if not isinstance(overrides, dict):
                raise ValueError(
                    f"overlays.profiles[{name!r}].pack_overrides must be a "
                    f"mapping of pack -> category -> enabled")
            for pack, cats in overrides.items():
                if not isinstance(cats, dict):
                    raise ValueError(
                        f"overlays.profiles[{name!r}].pack_overrides[{pack!r}] "
                        f"must be a mapping of category -> {{enabled: bool}} "
                        f"(got {type(cats).__name__}); the old flat "
                        f"pack_overrides.<category>.<bool> shape is not valid")
                for cat, ov in cats.items():
                    if not isinstance(ov, dict) or set(ov) != {"enabled"} \
                            or not isinstance(ov.get("enabled"), bool):
                        raise ValueError(
                            f"overlays.profiles[{name!r}].pack_overrides"
                            f"[{pack!r}][{cat!r}] must be exactly "
                            f"{{enabled: bool}}")
        return v



# ---------------------------------------------------------------------------
# collections + playlists
# ---------------------------------------------------------------------------

class Filter(_Strict):
    year_from: Optional[int] = Field(
        default=None,
        description="Minimum release year (inclusive).",
        json_schema_extra={"help": "Only include items released in this year or later. Empty = no "
        "lower bound."})
    year_to: Optional[int] = Field(
        default=None,
        description="Maximum release year (inclusive).",
        json_schema_extra={"help": "Only include items released in this year or earlier. Empty = no "
        "upper bound."})
    min_rating: Optional[float] = Field(
        default=None,
        description="Minimum rating threshold (0-10 scale).",
        json_schema_extra={"help": "Only include items rated at least this high (on a 0-10 scale, "
        "matching the rating Finmeta computes). Empty = no minimum."})
    genres: Optional[List[str]] = Field(
        default=None,
        description="Restrict to these genres (match any).",
        json_schema_extra={"help": "Only include items with any of these genres (case-insensitive, "
        "match any). Empty = any genre."})


class _CollectionBase(_Strict):
    name: str = Field(
        description="Display name of the collection.",
        json_schema_extra={"help": "The name shown in Jellyfin for this collection. Must be unique "
        "among collections you define."})
    library: str = Field(
        description="Library this collection is built from.",
        json_schema_extra={"help": "The library (key from `libraries`) whose items are used to build "
        "this collection. Must reference a configured library."})
    filter: Optional[Filter] = Field(
        default=None,
        description="Optional filter applied to the library items.",
        json_schema_extra={"help": "Optional constraints (year range, min rating, genres) applied on "
        "top of the library. Required for `filter`-type collections; ignored for `tmdb_collection`."})


class TmdbCollection(_CollectionBase):
    type: Literal["tmdb_collection"] = Field(
        description="Collection type discriminator (TMDb collection).",
        json_schema_extra={"help": "Fixed value `tmdb_collection`. Builds a Jellyfin collection from "
        "a TMDb collection (films that share a TMDb collection id). Needs at least `min_items` "
        "matches."})
    min_items: int = Field(
        default=2,
        description="Minimum matched items before the collection is created.",
        json_schema_extra={"help": "Only create the collection if at least this many library items "
        "match it. Default 2 (avoids near-empty collections)."})


class FilterCollection(_CollectionBase):
    type: Literal["filter"] = Field(
        description="Collection type discriminator (filter-based).",
        json_schema_extra={"help": "Fixed value `filter`. Builds a collection from library items that "
        "match the `filter`. The `filter` is required for this type."})
    filter: Filter = Field(
        description="Filter that defines membership.",
        json_schema_extra={"help": "The filter (year range, min rating, genres) that selects which "
        "library items belong to this collection. Required for `filter` collections."})


class ListCollection(_CollectionBase):
    type: Literal["list"] = Field(
        description="Collection type discriminator (explicit TMDb id list).",
        json_schema_extra={"help": "Fixed value `list`. Builds a collection from an explicit list of "
        "TMDb ids. Useful for curated, hand-picked collections."})
    tmdb_ids: List[int] = Field(
        description="List of TMDb item ids to include.",
        json_schema_extra={"help": "Explicit TMDb numeric ids to include, e.g. `[27205, 157336, "
        "155]`. Each is matched against library items; unmatched ids are skipped."})


CollectionConfig = Annotated[
    Union[TmdbCollection, FilterCollection, ListCollection],
    Field(discriminator="type"),
]


class Playlist(_Strict):
    name: str = Field(
        description="Display name of the playlist.",
        json_schema_extra={"help": "The name shown in Jellyfin for this playlist. Must be unique among "
        "playlists you define."})
    user: str = Field(
        description="Jellyfin username the playlist is created for.",
        json_schema_extra={"help": "The Jellyfin user the playlist is created under. Must exist in "
        "Jellyfin. Used for user-scoped playlists."})
    library: str = Field(
        description="Library the playlist is built from.",
        json_schema_extra={"help": "The library (key from `libraries`) whose items fill the playlist. "
        "Must reference a configured library."})
    filter: Optional[Filter] = Field(
        default=None,
        description="Optional filter selecting playlist items.",
        json_schema_extra={"help": "Optional constraints (year range, min rating, genres) selecting "
        "which items fill the playlist. Empty = all items in the library."})
    limit: int = Field(
        default=20,
        description="Maximum number of items in the playlist.",
        json_schema_extra={"help": "Cap on how many items go into the playlist. Default 20."})
    sort: str = Field(
        default="rating_desc",
        description="Sort order for the playlist.",
        json_schema_extra={"help": "How items are ordered in the playlist, e.g. `rating_desc` (best "
        "first), `rating_asc`, `year_desc`, `year_asc`, `title`. Default `rating_desc`."})


# ---------------------------------------------------------------------------
# schedules
# ---------------------------------------------------------------------------

class Schedule(_Strict):
    cron: str = Field(
        description="Cron expression for when the schedule runs.",
        json_schema_extra={"help": "A 5-field cron expression, e.g. `0 4 * * *` (daily at 04:00) or "
        "`0 2 * * 1` (Mondays 02:00). Standard cron minute/hour/day/month/weekday fields."})
    action: Literal["plan", "apply"] = Field(
        default="plan",
        description="What the schedule does: plan (dry-run) or apply.",
        json_schema_extra={"help": "`plan` = build the WritePlan without writing anything (safe, "
        "default); `apply` = actually write files and call the Jellyfin API. `apply` is refused while "
        "`dry_run` is true."})
    libraries: List[str] = Field(
        default_factory=list,
        description="Libraries to include (empty = all configured).",
        json_schema_extra={"help": "Restrict the run to these libraries (keys from `libraries`). "
        "Empty = run against all configured libraries."})

    @field_validator("cron")
    @classmethod
    def _check_cron(cls, v: str) -> str:
        from croniter import croniter
        if not croniter.is_valid(v):
            raise ValueError(f"invalid cron expression: {v!r}")
        return v


# ---------------------------------------------------------------------------
# top-level config
# ---------------------------------------------------------------------------

class Config(_Strict):
    dry_run: bool = Field(
        default=True,
        description="Plan-only mode; refuse to write files or call the write API.",
        json_schema_extra={"help": "Safety switch. While `true` (the shipped default), apply runs "
        "refuse to write anything — the API/CLI only produce a plan. Set to `false` to allow "
        "real writes. See `POST /api/runs`."})
    server: ServerConfig = Field(
        description="Jellyfin server connection settings.",
        json_schema_extra={"help": "Where Finmeta's Jellyfin is and which env var holds its API key. "
        "See ServerConfig help."})
    path_mappings: List[PathMapping] = Field(
        default_factory=list,
        description="Jellyfin-path -> local-path translations.",
        json_schema_extra={"help": "The set of path mappings used to translate every Jellyfin path "
        "to a local path before touching disk (SPEC override O6). An unmapped path is skipped with an "
        "error, never guessed."})
    language: str = Field(
        default="en-US",
        description="Preferred metadata/artwork language.",
        json_schema_extra={"help": "Preferred language for metadata and artwork (BCP-47, e.g. "
        "`en-US`, `en-GB`). Default `en-US`."})
    artwork_language_fallback: List[Optional[str]] = Field(
        default_factory=lambda: [None],
        description="Ordered artwork language fallbacks (null = textless).",
        json_schema_extra={"help": "Ordered fallback languages for artwork, e.g. `[en, null]`. A "
        "`null` entry means 'prefer textless artwork'. Finmeta walks this list and uses the first "
        "language that has a matching image."})
    artwork_selection: ArtworkSelection = Field(
        default_factory=ArtworkSelection,
        description="Global automatic artwork selection settings.",
        json_schema_extra={"help": "How Finmeta automatically chooses among artwork candidates "
        "(preset + top_n). A library can override this under `libraries.<name>.artwork_selection`."})
    artwork_write_method: Literal["sidecar"] = Field(
        default="sidecar",
        description="How poster/backdrop/logo/landscape artwork is written (metadata NFO and "
        "theme songs are always sidecar files; Jellyfin has no API for either).",
        json_schema_extra={"help": "`sidecar` -- the only write method, and the only value this "
        "field accepts. Writes poster.jpg/backdrop.jpg/etc. directly into the media folder next "
        "to the video file. The alternative this field used to also accept, `jellyfin_api` "
        "(uploads straight to Jellyfin via `POST /Items/{id}/Images/{type}`, never touching the "
        "media folder), was removed 2026-10-26: confirmed against a real library that Jellyfin's "
        "own scanning keeps re-asserting a local poster/backdrop/logo/fanart file as "
        "authoritative over an API-uploaded one, so it could never reliably override an item "
        "whose media folder already had local artwork -- which is to say, most real libraries. "
        "Field kept (rather than removed outright) only so an older config.yml with this key "
        "already set to `sidecar` keeps loading."})
    backup_retention_days: int = Field(
        default=14,
        ge=0,
        description="Days a superseded backup copy spends in trash before permanent deletion.",
        json_schema_extra={"help": "S9d5: every file write backs up the previous version "
        "(SPEC override O5). Without cleanup these pile up forever. On each apply, Finmeta "
        "moves every superseded backup copy (i.e. not the very first one ever made for a "
        "path, and not the most recent one) into `<config>/trash/`; a trash entry is then "
        "permanently deleted once it has sat there for this many days. The FIRST backup ever "
        "made for a path (the true pre-Finmeta original, needed for overlay re-renders to "
        "never stack badges -- SPEC override O7, and for a full undo) and the MOST RECENT "
        "backup (needed to undo the latest run) are never touched, regardless of this "
        "setting. Set to `0` to disable cleanup and keep every backup forever."})
    sources: SourcesConfig = Field(
        default_factory=SourcesConfig,
        description="Metadata/artwork source settings + precedence.",
        json_schema_extra={"help": "Which sources (TMDb, fanart.tv, local, OMDb, MDBList, IMDb "
        "datasets) are enabled and in what order they are consulted. See the External services tab "
        "and each source's help."})
    media_apps: MediaAppsConfig = Field(
        default_factory=MediaAppsConfig,
        description="Sonarr and Radarr instances.",
        json_schema_extra={"help": "Your Sonarr (`media_apps.sonarr`) and Radarr (`media_apps.radarr`) "
        "instances. Each has a url, the name of the secret holding its API key, timeout, verify_ssl "
        "and enabled. Finmeta is read-only against these — it never adds, edits or deletes anything."})
    libraries: Dict[str, LibraryConfig] = Field(
        default_factory=dict,
        description="Named library profiles (the main config surface).",
        json_schema_extra={"help": "The libraries Finmeta manages, keyed by a name you choose (e.g. "
        "`Movies`, `Shows`). Each has a type, metadata and artwork options, theme song and overlays. "
        "Collections/playlists/schedules reference these by name."})
    overlays: OverlaysConfig = Field(
        default_factory=OverlaysConfig,
        description="Global overlay compositor style defaults.",
        json_schema_extra={"help": "Global defaults for the poster overlay compositor (position, "
        "fonts, colors, radius). Per-element layout is managed in the Overlay editor (later S9 "
        "session)."})
    status_overlay: StatusOverlayConfig = Field(
        default_factory=StatusOverlayConfig,
        description="TV show status (K7a) settings: recent-days window + optional collections.",
        json_schema_extra={"help": "Settings for the TV show `status` overlay element (computed "
        "from Sonarr -- new series, new season soon, upcoming finale, returning, ended, etc.). The "
        "element itself is enabled per overlay profile like any other (`elements: [status, ...]`); "
        "this section only controls the recent-days window and optional per-status collections."})
    trending: TrendingConfig = Field(
        default_factory=TrendingConfig,
        description="MDBList trending collections (K7b).",
        json_schema_extra={"help": "Lists of MDBList lists mirrored into Jellyfin collections, with "
        "an optional TOP 10 rank overlay element (`trending_rank`). Empty by default."})
    collections: List[CollectionConfig] = Field(
        default_factory=list,
        description="Collections to create in Jellyfin.",
        json_schema_extra={"help": "Collections Finmeta creates and keeps in sync in Jellyfin. Each "
        "is one of `tmdb_collection`, `filter`, or `list`. Created via the Jellyfin API and defined "
        "here (no collection.nfo — SPEC override O1)."})
    collection_presets: List[str] = Field(
        default_factory=list,
        description="Active built-in preset collection ids (S9f2).",
        json_schema_extra={"help": "Ids of built-in preset collections to keep active (e.g. "
        "`adaptive_seasonal`, `top_popular_movies`) — see the Collections tab for the full catalog. "
        "`adaptive_seasonal` drives a whole calendar of holiday collections (Valentine's Day, "
        "Mother's/Father's Day, Halloween, Thanksgiving, Christmas), each only present in Jellyfin "
        "during its own window and deleted automatically outside it (recreated next time it's "
        "active); chart presets (Top 50 Popular, Highest Rated) are always kept in sync. Applies "
        "automatically to every enabled library of the matching type — no per-library selection "
        "needed. Only evaluated on a "
        "full (all-libraries) plan/apply, not a single-library or single-item run."})
    sync_mode: Literal["sync", "append"] = Field(
        default="sync",
        description="Collection/playlist sync behaviour.",
        json_schema_extra={"help": "`sync` = add new members *and* remove ones that no longer match "
        "(default); `append` = only add, never remove. Applies to collections and playlists."})
    playlists: List[Playlist] = Field(
        default_factory=list,
        description="Playlists to create in Jellyfin.",
        json_schema_extra={"help": "Playlists Finmeta creates and keeps in sync in Jellyfin. Each "
        "picks items from a library by an optional filter, up to a limit, in a chosen sort order."})
    schedules: List[Schedule] = Field(
        default_factory=list,
        description="Recurring plan/apply runs (cron).",
        json_schema_extra={"help": "Recurring runs. Each has a cron schedule, an action (plan or "
        "apply) and an optional library subset. `apply` is refused while `dry_run` is true."})

    @model_validator(mode="after")
    def _migrate_overlays_list_to_auto_profile(self) -> "Config":
        """S9d4c step 2b: single source of truth for overlays.

        A library with a non-empty (deprecated) ``overlays`` list and either
        no ``overlay_profile`` or a profile that has no ``elements`` key gets
        an auto-generated profile ``<library>-auto`` holding that list
        (merged with whatever other keys the referenced profile already had,
        e.g. ``style``/``format_rules``), and ``overlay_profile`` is pointed
        at it. From here on, only the profile is consulted anywhere
        (plan_builder, UI) -- the list is a read-only migration source, never
        consulted again directly. A profile that already defines `elements`
        (even an empty list, meaning "intentionally no overlays") is left
        alone: its author already made an explicit choice. A reference to a
        profile that does not exist at all is left alone too -- that is an
        invalid reference, not "no profile"/"profile without elements", and
        must still fail _check_references below instead of being silently
        papered over.
        """
        for name, lc in self.libraries.items():
            overlay_list = list(getattr(lc, "overlays", None) or [])
            if not overlay_list:
                continue
            prof_name = getattr(lc, "overlay_profile", None)
            if prof_name is not None and prof_name not in self.overlays.profiles:
                continue
            prof = self.overlays.profiles.get(prof_name) if prof_name else None
            if isinstance(prof, dict) and "elements" in prof:
                continue
            auto_name = f"{name}-auto"
            self.overlays.profiles[auto_name] = {
                **(prof or {}), "elements": overlay_list,
            }
            lc.overlay_profile = auto_name
        return self

    @model_validator(mode="after")
    def _check_references(self) -> "Config":
        libs = set(self.libraries)
        for c in self.collections:
            if c.library not in libs:
                raise ValueError(f"collection {c.name!r} references unconfigured library {c.library!r}")
        for p in self.playlists:
            if p.library not in libs:
                raise ValueError(f"playlist {p.name!r} references unconfigured library {p.library!r}")
        for s in self.schedules:
            for lib in s.libraries:
                if lib not in libs:
                    raise ValueError(f"schedule references unconfigured library {lib!r}")
        profiles = set(self.overlays.profiles)
        for name, lc in self.libraries.items():
            prof = getattr(lc, "overlay_profile", None)
            if prof is not None and prof not in profiles:
                raise ValueError(
                    f"library {name!r} references unknown overlay profile {prof!r}; "
                    f"defined profiles: {sorted(profiles)}")
        return self


__all__ = [
    "ServerConfig", "PathMapping", "SourcesConfig", "TmdbSource", "FanartSource",
    "LocalSource", "Metadata", "MovieArtwork", "TvArtwork", "MovieLibrary",
    "TvLibrary", "LibraryConfig", "OverlayStyle", "OverlaysConfig", "Filter",
    "TmdbCollection", "FilterCollection", "ListCollection", "CollectionConfig",
    "Playlist", "Schedule", "OmdbSource", "MdblistSource", "ImdbDatasetsSource",
    "MediaAppInstance", "MediaAppsConfig", "Config", "PosterRotation",
]
