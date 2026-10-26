"""Network/streaming-service overlay badges (2026-10-23).

Source: Kometa-Team/Default-Images (MIT), the same project the "kometa"
logo pack (engine/logo_packs/kometa.yml) already downloads rating/
resolution/hdr/audio badges from. Network badges are NOT in that pack: it
pre-declares a fixed ~30-entry file list, but TMDb's `networks` field can
be any of 280+ names (Netflix, HBO, Apple TV+, ...) -- too many to
pre-declare and download eagerly. Fetched lazily instead, one file per
distinct network actually seen in the library, and cached on disk forever
after (including a cached "no such badge" miss, so an unknown network
name is never re-fetched on every apply).

Movies have no TMDb `networks` field; this is TV-shows only, matching
Kometa's own network.yml convention.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

_BASE_URL = "https://raw.githubusercontent.com/Kometa-Team/Default-Images/master/network/overlays/standard/{name}.png"


def network_slug(name: str) -> str:
    """A safe, stable local filename for a network's badge.

    Decoupled from Default-Images' own filename (which keeps spaces/"+"/
    "&" verbatim, e.g. "Apple TV+.png") -- this is purely our own on-disk
    cache key.
    """
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def _logo_path(config_dir: Path, name: str) -> Path:
    return Path(config_dir) / "assets" / "logos" / "kometa" / "network" / f"{network_slug(name)}.png"


def ensure_network_logo(config_dir: Path, name: str, *, timeout: float = 10.0) -> Optional[bytes]:
    """Cached bytes for this network's badge, fetching once if not yet cached.

    Returns None when Default-Images has no badge for this exact name (a
    0-byte marker file is cached so the miss is never re-fetched) or on a
    network/transport error (NOT cached, so a transient failure can
    succeed on a later run)."""
    if not name or not name.strip():
        return None
    p = _logo_path(config_dir, name)
    if p.is_file():
        data = p.read_bytes()
        return data or None
    url = _BASE_URL.format(name=quote(name, safe=""))
    try:
        resp = httpx.get(url, timeout=timeout, follow_redirects=True)
    except httpx.HTTPError as exc:
        logger.warning("network logo fetch failed for %r: %s", name, exc)
        return None
    p.parent.mkdir(parents=True, exist_ok=True)
    if resp.status_code == 200 and resp.content:
        p.write_bytes(resp.content)
        return resp.content
    p.write_bytes(b"")  # cache the miss
    return None


__all__ = ["ensure_network_logo", "network_slug"]
