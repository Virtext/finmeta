"""Config models: Pydantic v2 schema for config/config.yml."""
from finmeta.config.models import (
    Config,
    ServerConfig,
    PathMapping,
    SourcesConfig,
    TmdbSource,
    FanartSource,
    LocalSource,
    Metadata,
    MovieArtwork,
    TvArtwork,
    MovieLibrary,
    TvLibrary,
    LibraryConfig,
    OverlayStyle,
    OverlaysConfig,
    Filter,
    TmdbCollection,
    FilterCollection,
    ListCollection,
    CollectionConfig,
    Playlist,
    Schedule,
)
from finmeta.config.loading import load_config, resolve_env
from finmeta.config.schema import export_schema, schema_is_current

__all__ = [
    "Config", "ServerConfig", "PathMapping", "SourcesConfig",
    "TmdbSource", "FanartSource", "LocalSource", "Metadata",
    "MovieArtwork", "TvArtwork", "MovieLibrary", "TvLibrary", "LibraryConfig",
    "OverlayStyle", "OverlaysConfig", "Filter",
    "TmdbCollection", "FilterCollection", "ListCollection", "CollectionConfig",
    "Playlist", "Schedule",
    "load_config", "resolve_env", "export_schema", "schema_is_current",
]
