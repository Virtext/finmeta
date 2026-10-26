"""Image pack loader (SPEC S9 D3).

User-supplied overlay image sets live under ``<config>/assets/packs/<name>/``:

* ``pack.yml`` — pack definition (validated by :class:`PackDef`)
* ``images/<category>/<key>.png`` — the actual badge images

A *category* is any user-defined name (e.g. ``resolution``, ``hdr``,
``edition``, ``network``). Each category declares a *matcher* that decides which image key to use for a given
item, and lists the expected keys so the API can report missing files.

Invalid packs (bad YAML, schema violations) are skipped with a logged error;
they never crash the loader or block other packs from loading.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml
from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger(__name__)

#: Valid category names (mirrors the K9 format-rule categories + rating).
#: Legacy category names (kept for reference; packs may define ANY category name).
LEGACY_CATEGORIES = ("resolution", "hdr", "audio", "rating")

#: Valid matcher types.
VALID_MATCHERS = ("format_rule", "filename_regex", "metadata", "rating_tier")


class CategoryDef(BaseModel):
    """One category within a pack."""
    matcher: str = Field(description="How to select the image key for an item.")
    keys: List[str] = Field(default_factory=list, description="Expected image keys.")
    # Optional matcher-specific config
    regex: Optional[str] = Field(
        default=None,
        description="Single pattern with a (?P<key>...) group, for filename_regex.")
    filename_regex: Optional[Dict[str, str]] = Field(
        default=None,
        description="One pattern PER key, for filename_regex -- the shape real "
                    "Kometa/jmxd-style media_info.yml sources actually use (each "
                    "badge has its own distinct regex, not a shared alternation "
                    "with a named group); kometa_import.to_pack emits this shape. "
                    "Tried before `regex` when both are present.")
    field: Optional[str] = Field(default=None, description="Metadata field for metadata matcher.")
    tiers: Optional[Dict[str, str]] = Field(
        default=None, description="Tier expressions for rating_tier matcher (key -> comparison).")

    @field_validator("matcher")
    @classmethod
    def _check_matcher(cls, v: str) -> str:
        if v not in VALID_MATCHERS:
            raise ValueError(f"unknown matcher {v!r}; must be one of {VALID_MATCHERS}")
        return v


class PackDef(BaseModel):
    """Validated pack.yml contents."""
    name: str = Field(description="Pack name (should match directory name).")
    description: Optional[str] = None
    categories: Dict[str, CategoryDef] = Field(default_factory=dict)

    @field_validator("categories")
    @classmethod
    def _check_categories(cls, v: Dict[str, CategoryDef]) -> Dict[str, CategoryDef]:
        # Packs may define ANY category name (edition, network, codec, ...);
        # each becomes its own badge element in the profile.
        return v


class PackInfo(BaseModel):
    """Runtime info about a loaded pack (returned by the API)."""
    name: str
    description: Optional[str] = None
    categories: Dict[str, CategoryDef] = Field(default_factory=dict)
    missing_images: List[str] = Field(
        default_factory=list,
        description="Dotted paths like 'resolution/res_4k' for expected-but-absent images.")
    valid: bool = True
    error: Optional[str] = None


def _packs_root(config_dir: Path) -> Path:
    return config_dir / "assets" / "packs"


#: Parsed-pack.yml memo: ``{resolved packs root: (checked_at, signature,
#: [(dir name, PackDef | error str)])}``. The signature is a stat-only
#: fingerprint of every ``*/pack.yml`` (name, mtime_ns, size), so an
#: imported/edited/deleted pack invalidates the entry while a steady-state
#: plan/apply run reparses nothing. Keyed on the resolved root so two
#: AppContexts (or a test's tmp_path) can never read each other's entry.
_DEFS_CACHE: Dict[str, Tuple[float, tuple, List[Tuple[str, object]]]] = {}

#: How long a cached entry is trusted without re-stat'ing the signature.
#: Even the stat-only fingerprint is a directory listing plus one stat per
#: pack, which on a network/FUSE-backed config dir (the operator's Unraid
#: shfs mount: ~0.7 ms per listdir, measured 2026-10-22) dominates the cost
#: when the overlay resolver asks thousands of times per run. Human-timescale
#: edits -- importing a pack, editing a pack.yml -- are picked up on the next
#: call past this window, i.e. always by the next run.
_SIG_TTL = 2.0


def _defs_signature(root: Path) -> tuple:
    """Stat-only fingerprint of the pack definitions under ``root``."""
    sig = []
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return ()
    for entry in entries:
        pack_yml = entry / "pack.yml"
        try:
            st = pack_yml.stat()
        except OSError:
            continue        # not a pack dir, or no pack.yml (same as load below)
        sig.append((entry.name, st.st_mtime_ns, st.st_size))
    return tuple(sig)


def _load_pack_defs(root: Path) -> List[Tuple[str, object]]:
    """``[(dir name, PackDef | error message)]`` for every pack under ``root``.

    Cached on :data:`_DEFS_CACHE`. This is the YAML-parsing half of
    :func:`load_packs`, split out because the overlay hot path
    (``engine.image_pack_resolve``) needs only the parsed categories --
    never ``missing_images`` -- and used to re-read and re-parse every
    pack.yml once per overlay element per item (~1 ms each; ~5.5 s of pure
    redundant I/O per 1000-item library, measured 2026-10-22).
    """
    key = str(root)
    now = time.monotonic()
    cached = _DEFS_CACHE.get(key)
    if cached is not None and now - cached[0] < _SIG_TTL:
        return cached[2]
    sig = _defs_signature(root)
    if cached is not None and cached[1] == sig:
        _DEFS_CACHE[key] = (now, sig, cached[2])    # unchanged: refresh the clock
        return cached[2]
    out: List[Tuple[str, object]] = []
    try:
        entries = sorted(root.iterdir())
    except OSError:
        entries = []        # no packs dir yet -> no packs (cached like any other result)
    for entry in entries:
        if not entry.is_dir():
            continue
        pack_yml = entry / "pack.yml"
        if not pack_yml.is_file():
            # No pack.yml -> skip silently (not a pack directory)
            continue
        try:
            raw = yaml.safe_load(pack_yml.read_text()) or {}
            if not isinstance(raw, dict):
                raise ValueError("pack.yml must be a mapping")
            out.append((entry.name, PackDef(**raw)))
        except Exception as exc:
            logger.error("invalid pack %r: %s", entry.name, exc)
            out.append((entry.name, str(exc)))
    _DEFS_CACHE[key] = (now, sig, out)
    return out


def pack_defs(config_dir: Path) -> Dict[str, PackDef]:
    """``{pack name: PackDef}`` for every valid pack -- no image stat'ing.

    The cheap lookup the overlay resolver wants; see :func:`load_packs` for
    the full report (including ``missing_images``) the API serves.
    """
    root = _packs_root(config_dir)
    return {pdef.name: pdef for _, pdef in _load_pack_defs(root)
            if isinstance(pdef, PackDef)}


def load_packs(config_dir: Path) -> List[PackInfo]:
    """Scan ``<config_dir>/assets/packs/*/pack.yml`` and return validated packs.

    Invalid packs are included in the result with ``valid=False`` and an
    ``error`` message so the API can surface them; they do not raise.
    ``missing_images`` is always stat'ed fresh (only the YAML parse is
    cached), so a pack whose images appear or vanish reports correctly.
    """
    root = _packs_root(config_dir)
    if not root.is_dir():
        return []
    out: List[PackInfo] = []
    for dir_name, pdef in _load_pack_defs(root):
        if not isinstance(pdef, PackDef):
            out.append(PackInfo(name=dir_name, valid=False, error=str(pdef)))
            continue
        # Check for missing images
        missing: List[str] = []
        images_dir = root / dir_name / "images"
        for cat_name, cat_def in pdef.categories.items():
            for key in cat_def.keys:
                img_path = images_dir / cat_name / f"{key}.png"
                if not img_path.is_file():
                    missing.append(f"{cat_name}/{key}")
        out.append(PackInfo(
            name=pdef.name, description=pdef.description,
            categories=pdef.categories, missing_images=missing, valid=True,
        ))
    return out


def get_pack(config_dir: Path, name: str) -> Optional[PackInfo]:
    """Return a single loaded pack by name, or None."""
    for p in load_packs(config_dir):
        if p.name == name and p.valid:
            return p
    return None


def resolve_pack_image(config_dir: Path, pack_name: str, category: str, key: str) -> Optional[bytes]:
    """Read one image from a pack; returns PNG bytes or None when missing."""
    img_path = _packs_root(config_dir) / pack_name / "images" / category / f"{key}.png"
    if not img_path.is_file():
        return None
    try:
        data = img_path.read_bytes()
        # Quick sanity check: must be a valid PNG
        from PIL import Image
        import io
        Image.open(io.BytesIO(data)).verify()
        return data
    except Exception:
        return None


#: Backwards-compatible alias (old hardcoded set).
VALID_CATEGORIES = LEGACY_CATEGORIES

__all__ = [
    "PackDef", "CategoryDef", "PackInfo",
    "load_packs", "pack_defs", "get_pack", "resolve_pack_image",
    "LEGACY_CATEGORIES", "VALID_CATEGORIES", "VALID_MATCHERS",
]
