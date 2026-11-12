"""Jellyfin HTTP API client (items, images, collections, refresh, playlists).

``BootstrapJellyfinClient`` (audit 2026-11-11 "D8": library create/delete,
the startup wizard, authenticate) is deliberately NOT re-exported here --
import it from ``finmeta.jellyfin_client.bootstrap`` directly. It exists
only for ``scripts/jf_bootstrap.py`` and the test suite; keeping it out of
this package's default surface is the point.
"""
from .client import JellyfinClient, JellyfinError, ItemPage, normalize_base_path
from .mappings import PathMapper, PathMapping, translate_path
from .types import AuthResult, Item, Library, SystemInfo, Task

__all__ = [
    "JellyfinClient", "JellyfinError", "ItemPage", "normalize_base_path",
    "PathMapper", "PathMapping", "translate_path",
    "AuthResult", "Item", "Library", "SystemInfo", "Task",
]
