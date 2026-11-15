"""SPEC_S13.md S13b item 2: dependency warnings -- a setting is on but what it
needs is missing (a key, a Sonarr instance, a URL), so it silently does
nothing on every run. Pure over (config, secret *status*); never reads a
secret value into the result.
"""
from __future__ import annotations

from typing import Any, Dict, List

from finmeta.api.secrets import secret_specs, secret_status

_KEYED_SOURCES = (("tmdb", "TMDb", "tmdb_key"), ("fanart", "fanart.tv", "fanart_key"),
                  ("omdb", "OMDb", "omdb_key"), ("mdblist", "MDBList", "mdblist_key"))


def config_warnings(cfg: Any, secrets: Any) -> List[Dict[str, str]]:
    specs = {s.name: s for s in secret_specs(cfg)}

    def has(name: str) -> bool:
        spec = specs.get(name)
        return spec is not None and bool(secret_status(secrets, spec)["configured"])

    out: List[Dict[str, str]] = []

    def warn(path: str, message: str) -> None:
        out.append({"path": path, "message": message})

    for attr, label, key in _KEYED_SOURCES:
        if getattr(cfg.sources, attr).enabled and not has(key):
            warn(f"sources.{attr}.enabled",
                 f"{label} is on but no {key} is saved, so it is skipped on every run. "
                 f"Add the key under Secrets, or turn {label} off.")

    mdblist_ok = cfg.sources.mdblist.enabled and has("mdblist_key")
    if cfg.trending.lists and not mdblist_ok:
        warn("trending.lists", "Trending lists need MDBList turned on with a saved key; "
                               "no list is fetched right now.")
    for i, lst in enumerate(cfg.trending.lists):
        if not (lst.url or "").strip():
            warn(f"trending.lists[{i}].url",
                 f"Trending list \"{lst.collection_name}\" has no URL and is skipped.")
    if "adaptive_seasonal" in cfg.collection_presets and not mdblist_ok:
        warn("collection_presets", "Adaptive Seasonal's Christmas collection comes from MDBList; "
                                   "without MDBList on and a key it is skipped.")

    for kind in ("sonarr", "radarr"):
        for inst in getattr(cfg.media_apps, kind):
            if inst.enabled and not has(inst.api_key or ""):
                warn(f"media_apps.{kind}", f"{kind.title()} instance \"{inst.name}\" is on but has "
                                           "no saved API key, so it is never contacted.")
    uses_status = cfg.status_overlay.create_collections or any(
        "status" in (p or {}).get("elements", []) for p in cfg.overlays.profiles.values())
    sonarr_ok = any(i.enabled and has(i.api_key or "") for i in cfg.media_apps.sonarr)
    if uses_status and not sonarr_ok:
        warn("status_overlay", "TV status badges/collections come from Sonarr, but no enabled "
                               "Sonarr instance has a key -- they produce nothing.")

    for c in cfg.collections:
        lib = cfg.libraries.get(c.library)
        if lib is not None and not getattr(lib, "enabled", False):
            warn("collections", f"Collection \"{c.name}\" uses library \"{c.library}\", which is "
                                "not managed by Finmeta (disabled), so it is never built.")
    return out


__all__ = ["config_warnings"]
