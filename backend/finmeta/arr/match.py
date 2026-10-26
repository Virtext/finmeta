"""Match Jellyfin items to Sonarr/Radarr records (S9f1b).

Read-only: fetches ``series()`` / ``movies()`` once per run from every enabled
instance and maps a Jellyfin item to an arr record by ProviderIds, in priority
order Tvdb (shows) / Tmdb / Imdb. First match wins in instance order; the
matched instance name is recorded. Facts only — no overlays or collections.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass
class ArrFacts:
    """What Finmeta knows about a Jellyfin item from an arr instance."""

    instance: str          # configured instance name (e.g. "Sonarr", "Radarr4K")
    kind: str              # "sonarr" | "radarr"
    status: Optional[str] = None
    monitored: Optional[bool] = None
    # movies
    has_file: Optional[bool] = None
    in_cinemas: Optional[str] = None
    digital_release: Optional[str] = None
    physical_release: Optional[str] = None
    # shows
    next_airing: Optional[str] = None
    previous_airing: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "instance": self.instance,
            "kind": self.kind,
            "status": self.status,
            "monitored": self.monitored,
            "has_file": self.has_file,
            "in_cinemas": self.in_cinemas,
            "digital_release": self.digital_release,
            "physical_release": self.physical_release,
            "next_airing": self.next_airing,
            "previous_airing": self.previous_airing,
        }


# ProviderId priority per media type (first non-None wins).
_SHOW_KEYS = ("Tvdb", "Tmdb", "Imdb")
_MOVIE_KEYS = ("Tmdb", "Imdb")

_ARR_FIELD_MAP = {
    "status": "status",
    "monitored": "monitored",
    "has_file": "hasFile",
    "in_cinemas": "inCinemas",
    "digital_release": "digitalRelease",
    "physical_release": "physicalRelease",
    "next_airing": "nextAiring",
    "previous_airing": "previousAiring",
}


class ArrCatalog:
    """Per-run cache of series/movies from every enabled instance.

    ``instances`` is a list of ``(name, app, client)`` tuples in config order;
    only enabled instances should be passed in (see :func:`build_catalog`).
    """

    def __init__(self, instances: List[tuple]) -> None:
        self._order = list(instances)
        self._rows: Dict[str, List[Dict[str, Any]]] = {}
        for name, app, client in instances:
            try:
                rows = client.series() if app == "sonarr" else client.movies()
            except Exception:
                # An unreachable instance must not break matching for the rest.
                rows = []
            self._rows[name] = list(rows)

    def match(self, item_type: str, provider_ids: Dict[str, str]) -> Optional[ArrFacts]:
        """Map a Jellyfin item to an arr record, or ``None`` if no match."""
        keys = _SHOW_KEYS if item_type in ("Series", "Season", "Episode") else _MOVIE_KEYS
        for name, app, _client in self._order:
            pid_keys = [k for k in keys if provider_ids.get(k)]
            for key in pid_keys:
                pid = str(provider_ids[key])
                # rows carry camelCase keys (tvdbId/tmdbId/imdbId); ProviderIds
                # are PascalCase, so translate Tvdb -> tvdbId etc.
                row_key = key[0].lower() + key[1:] + "Id"
                row = next((r for r in self._rows[name]
                            if str(r.get(row_key) or "") == pid), None)
                if row is not None:
                    return _facts(name, app, row)
        return None

    @property
    def instance_names(self) -> List[str]:
        return [n for n, _, _ in self._order]


def build_catalog(cfg, secrets, make_client) -> ArrCatalog:
    """Build an :class:`ArrCatalog` from the config's enabled instances.

    ``make_client(app, inst, api_key)`` returns a Sonarr/Radarr client; it is
    injectable so tests can pass mocks and never touch a real instance.
    """
    out: List[tuple] = []
    for app in ("sonarr", "radarr"):
        for inst in getattr(cfg.media_apps, app, []):
            if not inst.enabled:
                continue
            key = secrets.get(inst.api_key) if secrets is not None else None
            if not key:
                continue
            out.append((inst.name, app, make_client(app, inst, key)))
    return ArrCatalog(out)


def _facts(name: str, app: str, row: Dict[str, Any]) -> ArrFacts:
    kwargs = {dst: row.get(src) for dst, src in _ARR_FIELD_MAP.items()}
    return ArrFacts(instance=name, kind=app, **kwargs)


__all__ = ["ArrFacts", "ArrCatalog", "build_catalog"]
