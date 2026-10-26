"""Original-image cache for the ``jellyfin_api`` artwork write method (S9d5).

Mirrors ``engine.runs.find_original_backup``'s role when a library's
``artwork_write_method`` is ``"jellyfin_api"``: a sidecar run has
``config/backups/`` to find the pre-Finmeta original in; an API-upload run
never writes a local file at all, so there is nothing to back up. Instead,
the pre-Finmeta original is captured ONCE -- the first time Finmeta touches
an item+kind -- by downloading its CURRENT image straight from Jellyfin.
That capture is cached on local disk forever after and never re-fetched, so
later overlay re-renders (O7, no badge stacking) always start from that same
true original even though Finmeta itself keeps overwriting the image in
Jellyfin via the API in between.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from finmeta.jellyfin_client.client import JellyfinClient

# Only image kinds Jellyfin has an ImageType for (metadata/theme_song/
# season_poster/episode_thumb are always sidecar -- see Config.artwork_write_method).
KIND_TO_IMAGE_TYPE = {
    "poster": "Primary",
    "backdrop": "Backdrop",
    "logo": "Logo",
    "landscape": "Thumb",
}


def original_cache_path(config_dir: Path, item_id: str, kind: str) -> Path:
    ext = "png" if kind == "logo" else "jpg"
    return Path(config_dir) / "jellyfin_originals" / item_id / f"{kind}.{ext}"


def get_or_fetch_original(
    jf: JellyfinClient, item_id: str, kind: str, config_dir: Path,
) -> Optional[bytes]:
    """Pre-Finmeta original bytes for ``(item_id, kind)``, or ``None`` when
    Jellyfin has no image of that type yet -- this item's first run, where
    the freshly resolved candidate itself IS the original (same meaning as
    ``find_original_backup`` returning ``None`` in sidecar mode).

    Captured from Jellyfin's CURRENT image the first time this is called for
    a given item+kind, then cached on disk and never re-fetched -- later
    calls return the cached bytes even after Finmeta's own uploads have
    replaced the image in Jellyfin.
    """
    path = original_cache_path(config_dir, item_id, kind)
    if path.is_file():
        return path.read_bytes()
    image_type = KIND_TO_IMAGE_TYPE.get(kind)
    if image_type is None:
        return None
    data = jf.get_image_bytes(item_id, image_type)
    if data is None:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return data


__all__ = ["KIND_TO_IMAGE_TYPE", "original_cache_path", "get_or_fetch_original"]
