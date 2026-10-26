"""Shared image-pack resolution for plan/preview/apply (SPEC S9 D3 step 3).

Given a profile's pack configuration and an item's detection context, resolves
which pack images to use for each overlay element. Both the apply path
(plan_builder) and the preview path (overlay_render) call this same function
so their output is guaranteed identical.

Missing image → no badge (the element is simply absent from the result).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from finmeta.engine.image_packs import pack_defs, resolve_pack_image
from finmeta.engine.image_pack_matchers import MatchContext, resolve_pack_key


def _category_disabled(overrides: Mapping[str, Any], pack_name: str, category: str) -> bool:
    """SPEC S9 D3: ``pack_overrides.<pack>.<category>.enabled: false`` disables.

    The override shape is exactly ``{pack_name: {category: {"enabled": false}}}``.
    Anything else (missing entries, non-bool values, other fields) is ignored —
    no merging into CategoryDef, no silent skips of unknown shapes.
    """
    if not isinstance(overrides, Mapping):
        return False
    pack_ov = overrides.get(pack_name)
    if not isinstance(pack_ov, Mapping):
        return False
    cat_ov = pack_ov.get(category)
    if not isinstance(cat_ov, Mapping):
        return False
    return cat_ov.get("enabled") is False


def resolve_profile_packs(
    config_dir: Path,
    profile: Mapping[str, Any],
    ctx: MatchContext,
    only_categories: Optional[Sequence[str]] = None,
) -> Dict[str, List[bytes]]:
    """Resolve pack images for one item under a given profile.

    ``profile`` is the raw dict from ``overlays.profiles.<name>`` and may
    contain:
      * ``packs``: list of pack names (in priority order)
      * ``pack_overrides``: per-pack, per-category overrides; only
        ``pack_overrides.<pack>.<category>.enabled: false`` is honored
        (disables that category for that pack).

    EVERY category a pack defines becomes its own badge element in the result
    (e.g. resolution, hdr, audio, rating, edition, network, codec, ...); each
    keeps the pack's declared position/group/weight downstream. An absent key
    means no badge for that element.

    ``only_categories`` restricts the work to those category names (same
    results, just no matcher runs or image reads for categories the caller is
    about to discard) -- used by :func:`resolve_profile_pack_image`, which
    wants exactly one element's badge.
    """
    pack_names: List[str] = list(profile.get("packs") or [])
    if not pack_names:
        return {}

    overrides: Mapping[str, Any] = profile.get("pack_overrides") or {}
    if not isinstance(overrides, Mapping):
        overrides = {}

    # Parsed pack definitions only (cached; no per-call image stat'ing --
    # missing_images is a load_packs() concern and unused here).
    all_packs = pack_defs(config_dir)

    # Union of every category defined by any referenced pack (pack order).
    wanted = set(only_categories) if only_categories is not None else None
    categories: List[str] = []
    for pname in pack_names:
        pack = all_packs.get(pname)
        if pack is None:
            continue
        for cat in pack.categories:
            if wanted is not None and cat not in wanted:
                continue
            if cat not in categories:
                categories.append(cat)

    result: Dict[str, List[bytes]] = {}
    for category in categories:
        keys: Optional[List[str]] = None
        pack_name_used: Optional[str] = None

        for pname in pack_names:
            pack = all_packs.get(pname)
            if pack is None:
                continue
            cat_def = pack.categories.get(category)
            if cat_def is None:
                continue
            if _category_disabled(overrides, pname, category):
                continue
            resolved = resolve_pack_key(cat_def, ctx)
            if resolved:
                keys = resolved
                pack_name_used = pname
                break

        if not keys or pack_name_used is None:
            continue

        # Load the actual image bytes.
        images: List[bytes] = []
        for key in keys:
            data = resolve_pack_image(config_dir, pack_name_used, category, key)
            if data is not None:
                images.append(data)
        if images:
            result[category] = images

    return result


# Finmeta's overlay ELEMENT names (resolution/hdr/audio) vs. the CATEGORY
# names real-world Kometa-style packs declare (resolution/video_codec/
# audio_codec) are two different vocabularies that happen to agree only on
# "resolution". Without this alias table, any pack imported via Kometa
# overlay import/import-remote -- using the real upstream category naming,
# not a Finmeta-specific one -- could never produce an hdr/audio badge, only
# ever fall back to text, regardless of which pack it is or what it's named.
ELEMENT_CATEGORY_ALIASES: Dict[str, List[str]] = {
    "resolution": ["resolution"],
    "hdr": ["hdr", "dynamic_range", "video_codec"],
    "audio": ["audio", "audio_codec"],
}


def resolve_profile_pack_image(
    config_dir: Path,
    profile: Mapping[str, Any],
    element: str,
    k9_result: Optional[Dict[str, Any]] = None,
    filename: str = "",
    metadata: Optional[Mapping[str, Any]] = None,
    rating_value: Optional[float] = None,
) -> List[bytes]:
    """``resolve_profile_packs`` narrowed to ONE Finmeta overlay element.

    Tries the element's own name first, then its known category-name aliases
    (see ``ELEMENT_CATEGORY_ALIASES``) against the packs' own resolved
    categories -- first alias with any image wins. Same inputs, same
    resolution, usable from both the apply path and the preview path so
    neither one can drift from the other.
    """
    if element in ("hdr", "audio"):
        # A pack may define a "combined_codec" category: a single image
        # (e.g. "TrueHD Atmos") that conveys both HDR and audio info in one
        # badge. When it resolves, it takes the hdr slot's position and the
        # separate audio badge is suppressed entirely -- showing both would
        # be redundant. Operator decision 2026-10-23 (previously flagged as
        # "a layout choice, not a safe default to pick silently").
        combined_ctx = MatchContext(filename=filename, metadata=dict(metadata or {}),
                                    rating_value=rating_value)
        combined = resolve_profile_packs(config_dir, profile, combined_ctx,
                                         only_categories=["combined_codec"]
                                         ).get("combined_codec")
        if combined:
            return combined if element == "hdr" else []

    aliases = ELEMENT_CATEGORY_ALIASES.get(element, [element])
    ctx = MatchContext(k9_result=k9_result, filename=filename,
                       metadata=dict(metadata or {}), rating_value=rating_value)
    resolved = resolve_profile_packs(config_dir, profile, ctx,
                                     only_categories=aliases)
    for candidate in aliases:
        images = resolved.get(candidate)
        if images:
            return images
    return []


__all__ = ["resolve_profile_packs", "resolve_profile_pack_image", "ELEMENT_CATEGORY_ALIASES"]
