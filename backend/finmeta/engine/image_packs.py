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
import shutil
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
VALID_MATCHERS = ("format_rule", "format_rule_combo", "filename_regex", "metadata", "rating_tier")


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
    components: Optional[List[str]] = Field(
        default=None,
        description="Ordered K9 category names to combine (e.g. ['resolution', "
                    "'dynamic_range', 'audio']), for the format_rule_combo matcher.")

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
    warnings: List[str] = Field(
        default_factory=list,
        description="Non-fatal issues, e.g. an image that looks poster-sized "
                    "instead of a tightly-cropped badge (see _POSTER_LIKE_HEIGHT).")
    valid: bool = True
    error: Optional[str] = None


def _packs_root(config_dir: Path) -> Path:
    return config_dir / "assets" / "packs"


#: Finmeta's own first-party packs, shipped inside the package (backend/finmeta/
#: assets/bundled_packs/<name>/) -- same pack.yml + images/<category>/<key>.png
#: shape as any user pack, just seeded once instead of hand-dropped.
BUNDLED_PACKS_DIR = Path(__file__).resolve().parent.parent / "assets" / "bundled_packs"

#: Real badges are small, tightly-cropped icons (jmxd's own convention: ~80-150px
#: tall; Finmeta's bundled packs render at up to ~250px for headroom). A pack
#: image taller than this is almost certainly a full poster canvas fed in by
#: mistake -- see docs/HISTORY.md's 2026-10-27 "ichimoku... badges were actually
#: poster-sized" entry, the real bug this threshold catches for future packs.
_POSTER_LIKE_HEIGHT = 400


def seed_bundled_packs(config_dir: Path) -> List[str]:
    """Copy Finmeta's own bundled packs into ``<config_dir>/assets/packs/`` once.

    A marker file (``.bundled_seeded``) records that this has already run, so
    an operator who deletes a seeded pack later (via ``DELETE /api/packs/{name}``)
    keeps it deleted -- re-seeding on every restart would silently undo that.
    Only packs not already present on disk are copied (so a hand-edited
    ``finmeta_clean`` is never overwritten). Returns the names actually copied
    (always empty after the first real run).
    """
    dest_root = _packs_root(config_dir)
    marker = dest_root / ".bundled_seeded"
    if marker.exists():
        return []
    if not BUNDLED_PACKS_DIR.is_dir():
        return []
    dest_root.mkdir(parents=True, exist_ok=True)
    copied: List[str] = []
    for entry in sorted(BUNDLED_PACKS_DIR.iterdir()):
        if not entry.is_dir() or not (entry / "pack.yml").is_file():
            continue
        dest = dest_root / entry.name
        if dest.exists():
            continue
        shutil.copytree(entry, dest)
        copied.append(entry.name)
    marker.touch()
    return copied


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
    """Stat-only fingerprint of the pack definitions under ``root``.

    A pack.yml dir is fingerprinted by that file's own (mtime, size). A
    no-pack.yml dir is only in this signature at all when it's a flat
    auto-pack candidate (see ``_auto_pack_def``) -- fingerprinted by the
    images/ dir's own mtime (changes whenever a file is added/removed/
    renamed) plus the sorted filename list, so a new/removed/renamed PNG
    invalidates the cache the same way editing a pack.yml does.
    """
    sig = []
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return ()
    for entry in entries:
        pack_yml = entry / "pack.yml"
        try:
            st = pack_yml.stat()
            sig.append((entry.name, "yml", st.st_mtime_ns, st.st_size))
            continue
        except OSError:
            pass
        images_dir = entry / "images"
        try:
            st = images_dir.stat()
            names = tuple(sorted(p.name for p in images_dir.iterdir()
                                 if p.is_file() and p.suffix.lower() == ".png"))
        except OSError:
            continue        # not a pack dir at all
        sig.append((entry.name, "flat", st.st_mtime_ns, names))
    return tuple(sig)


#: K9's own format_rule vocabulary (engine.format_rules.DEFAULT_FORMAT_RULES'
#: `image` fields, same ones match_format_rule in image_pack_matchers.py
#: actually reads), inverted to key -> category and category -> full key
#: list. Every key is unique across all three categories, so a bare filename
#: alone is enough to infer which category it belongs to -- the whole point
#: of the flat auto-pack layout below.
def _k9_key_tables() -> Tuple[Dict[str, str], Dict[str, List[str]]]:
    from finmeta.engine.format_rules import DEFAULT_FORMAT_RULES
    key_to_category: Dict[str, str] = {}
    category_keys: Dict[str, List[str]] = {}
    for category, rules in DEFAULT_FORMAT_RULES.items():
        keys = sorted({r["image"].removesuffix(".png") for r in rules})
        category_keys[category] = keys
        for key in keys:
            key_to_category[key] = category
    return key_to_category, category_keys


def _auto_pack_def(name: str, pack_dir: Path) -> Optional["PackDef"]:
    """A directory with NO pack.yml but a flat ``images/<key>.png`` layout
    (no per-category subfolder) is auto-detected: each filename (minus
    ``.png``) is looked up directly against K9's own vocabulary to infer its
    category. No pack.yml to write, no subfolder to get the name of wrong --
    just a correctly-named PNG dropped into one folder. Operator-requested
    simplification (2026-11-01): writing pack.yml by hand, and matching its
    category names to the right on-disk subfolder, was the actual blocker on
    a real first attempt (see docs/BADGE_PACKS.md's "Quick start").

    Each resulting category lists K9's FULL key set (not just the files
    present), so ``load_packs()``'s existing missing-images reporting still
    shows what else you could add -- same as a scaffolded pack.yml would.
    Returns None when there's no images/ dir, or zero filenames match any
    known key (so an unrelated folder full of random PNGs is never
    silently adopted as a pack).
    """
    images_dir = pack_dir / "images"
    if not images_dir.is_dir():
        return None
    key_to_category, category_keys = _k9_key_tables()
    found_categories: set = set()
    try:
        for f in images_dir.iterdir():
            if not f.is_file() or f.suffix.lower() != ".png":
                continue
            cat = key_to_category.get(f.stem)
            if cat is not None:
                found_categories.add(cat)
    except OSError:
        return None
    if not found_categories:
        return None
    categories = {
        cat: CategoryDef(matcher="format_rule", keys=category_keys[cat])
        for cat in sorted(found_categories)
    }
    return PackDef(name=name, description="Auto-detected from images/<key>.png (no pack.yml)",
                   categories=categories)


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
            auto = _auto_pack_def(entry.name, entry)
            if auto is not None:
                out.append((entry.name, auto))
            # else: not a pack directory at all -> skip silently
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
        # Check for missing images + flag any that look poster-sized rather
        # than a tightly-cropped badge (a real bug seen on an imported pack).
        missing: List[str] = []
        warnings: List[str] = []
        images_dir = root / dir_name / "images"
        for cat_name, cat_def in pdef.categories.items():
            for key in cat_def.keys:
                img_path = _image_path(images_dir, cat_name, key)
                if not img_path.is_file():
                    missing.append(f"{cat_name}/{key}")
                    continue
                try:
                    from PIL import Image
                    with Image.open(img_path) as im:
                        if im.height > _POSTER_LIKE_HEIGHT:
                            warnings.append(
                                f"{cat_name}/{key} is {im.width}x{im.height}px -- looks "
                                "poster-sized; badges should be a tightly-cropped icon "
                                f"(under {_POSTER_LIKE_HEIGHT}px tall), not a full poster canvas")
                except Exception:
                    pass   # unreadable/corrupt image: resolve_pack_image already treats it as missing
        out.append(PackInfo(
            name=pdef.name, description=pdef.description,
            categories=pdef.categories, missing_images=missing,
            warnings=warnings, valid=True,
        ))
    return out


def get_pack(config_dir: Path, name: str) -> Optional[PackInfo]:
    """Return a single loaded pack by name, or None."""
    for p in load_packs(config_dir):
        if p.name == name and p.valid:
            return p
    return None


def _image_path(images_dir: Path, category: str, key: str) -> Path:
    """Subfoldered ``images/<category>/<key>.png`` if present, else the flat
    ``images/<key>.png`` layout -- the latter is what makes an auto-detected
    (no pack.yml) pack's images resolve at all, see ``_auto_pack_def``."""
    sub = images_dir / category / f"{key}.png"
    if sub.is_file():
        return sub
    return images_dir / f"{key}.png"


def resolve_pack_image(config_dir: Path, pack_name: str, category: str, key: str) -> Optional[bytes]:
    """Read one image from a pack; returns PNG bytes or None when missing."""
    img_path = _image_path(_packs_root(config_dir) / pack_name / "images", category, key)
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
    "seed_bundled_packs", "BUNDLED_PACKS_DIR",
    "LEGACY_CATEGORIES", "VALID_CATEGORIES", "VALID_MATCHERS",
]
