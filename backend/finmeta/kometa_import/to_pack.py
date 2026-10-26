"""Convert expanded Kometa overlays into a Finmeta image pack (SPEC_S9 E + D3).

Takes the flat overlay list from :func:`finmeta.kometa_import.expand.expand_file`
and produces:

* ``pack_yml_dict`` — the dict that, dumped as YAML and written to
  ``<config>/assets/packs/<name>/pack.yml``, validates against
  :class:`finmeta.engine.image_packs.PackDef`. One pack category per Kometa
  group (overlays without a group get their own category named by key).
* ``image_copies`` — list of ``(source, target)`` path pairs copying the badge
  images from the Kometa assets tree into ``images/<category>/<key>.png``.

Condition mapping (D3 matchers):

* ``conditions["filepath.regex"]`` -> ``filename_regex`` category with a
  ``filename_regex: {image key: regex}`` rule; ``movies_only: true`` when
  ``conditions["allowed_libraries"] == "movie"``.
* ``conditions["resolution"]`` -> ``format_rule`` category ``resolution`` with a
  ``key_map`` from Finmeta's own :data:`DEFAULT_RESOLUTION_RULES` keys
  (``4k`` -> the 4K rule key, ``1080p`` -> the 1080p rule key).
* ``conditions["audience_rating.gte"]`` with an image file -> ``rating_tier``
  category with ``source: audience`` tiers; a text overlay (``is_text``) at the
  same rating sets ``text: "{value}"`` on that category.
* layout ``horizontal_align``/``vertical_align`` -> ``align`` corner,
  ``[horizontal_offset, vertical_offset]`` -> ``offset``.

Entries that differ only by edition resolution (``IMAX-1080p`` / ``IMAX-4K``)
collapse into one key; the dropped per-resolution offset is noted in the report.
Gradients and other condition-less full-poster overlays are reported as
"not supported in D3" and skipped. Missing source images produce a report entry,
never a crash.
"""
from __future__ import annotations

import re
import os
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from finmeta.engine.format_rules import DEFAULT_RESOLUTION_RULES
from .expand import FlatOverlay
from .report import ImportReport

#: Kometa condition value -> Finmeta resolution rule key (label of the default rule).
_RES_LABEL_TO_RULE_KEY = {r["label"].lower(): r["key"] for r in DEFAULT_RESOLUTION_RULES}


def _res_rule_key(value: Any) -> Optional[str]:
    """Map a Kometa ``resolution`` condition value to a Finmeta rule key."""
    return _RES_LABEL_TO_RULE_KEY.get(str(value).strip().lower())


def _align_corner(horizontal: Any, vertical: Any) -> str:
    h = str(horizontal or "").strip().lower()
    v = str(vertical or "").strip().lower()
    if not h and not v:
        return ""
    if not v:
        v = "center"
    if not h:
        h = "center"
    if h == "center" and v == "center":
        return "center"
    return f"{v}-{h}"


def _offset(layout: Mapping[str, Any]) -> Optional[List[int]]:
    ho = layout.get("horizontal_offset")
    vo = layout.get("vertical_offset")
    if ho is None and vo is None:
        return None
    def num(v):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return 0
    return [num(ho), num(vo)]


def _source_relpath(file: str) -> Optional[str]:
    """Kometa image path as written in the file -> path under the images root.

    Kometa writes ``config/overlays/images/<...>.png``; the part after
    ``images/`` is the relative path inside the Kometa images tree. Unknown
    shapes are used verbatim (minus a leading slash).
    """
    if not file:
        return None
    f = str(file)
    m = re.search(r"(?:^|/)images/(.+)$", f)
    rel = m.group(1) if m else f.lstrip("/")
    return rel or None


def _resolve_source(root: Path, rel: str) -> Optional[Path]:
    """Resolve one Kometa image relative path against ``root`` (S9e4).

    ``root`` may be either the Kometa root (a dir containing ``config/`` — the
    paths in overlay files are full ``config/overlays/images/<...>`` shapes) or
    the images folder itself (containing ``media_info/`` etc.). Try the path
    with the ``config/overlays/images/`` prefix stripped first, then the full
    path. Returns None when neither candidate exists.
    """
    candidates = [root / rel]
    if "config/overlays/images/" in rel:
        candidates.insert(0, root / rel.split("config/overlays/images/", 1)[1])
    for c in candidates:
        try:
            if c.is_file():
                return c
        except OSError:
            continue
    return None


def _note(report: ImportReport, key: str, text: str) -> None:
    for item in report.items:
        if item.key == key:
            item.notes.append(text)
            return
    from .report import ReportItem
    it = ReportItem(key=key)
    it.notes.append(text)
    report.items.append(it)


def kometa_to_pack(
    flat: Sequence[FlatOverlay],
    report: ImportReport,
    kometa_images_root: str | os.PathLike,
    pack_name: str,
) -> Tuple[Dict[str, Any], List[Tuple[str, str]], ImportReport]:
    """Build the pack dict + image copy list from expanded overlays.

    ``kometa_images_root`` may be either the Kometa root (a dir containing
    ``config/``) or the images folder itself (containing ``media_info/`` etc.);
    each image is resolved by trying the path with the
    ``config/overlays/images/`` prefix stripped and then the full path. The
    returned copies are ``(source path under root, images/<category>/<key>.png)``.
    """
    root = Path(kometa_images_root)
    categories: Dict[str, Dict[str, Any]] = {}
    copies: List[Tuple[str, str]] = []

    def category(name: str) -> Dict[str, Any]:
        if name not in categories:
            categories[name] = {"matcher": "filename_regex", "keys": [], "rules": {}}
        return categories[name]

    def add_key(cat: Dict[str, Any], key: str) -> None:
        if key not in cat["keys"]:
            cat["keys"].append(key)

    # ---- edition overlays collapse across resolution ----------------------
    # IMAX-1080p / IMAX-4K (same base key, same regex) become one entry; the
    # per-resolution horizontal offsets differ and are dropped with a note.
    editions: Dict[Tuple[str, str], List[FlatOverlay]] = {}
    for fo in flat:
        if "filepath.regex" in fo.conditions and "resolution" in fo.conditions:
            base = re.sub(r"-(1080p|4K)$", "", fo.key, flags=re.IGNORECASE)
            editions.setdefault((base, str(fo.conditions["filepath.regex"])), []).append(fo)

    consumed: set[int] = set()
    for (base, regex), group in editions.items():
        group.sort(key=lambda g: str(g.conditions.get("resolution")))
        if len(group) > 1:
            offsets = {str(g.layout.get("horizontal_offset")) for g in group}
            if len(offsets) > 1:
                _note(report, base, "collapsed edition resolutions share one image; "
                                    f"dropped per-resolution horizontal offsets {sorted(offsets)}")
        first = group[0]
        for g in group:
            consumed.add(id(g))
        cat = category(first.group or base)
        add_key(cat, base)
        cat["matcher"] = "filename_regex"
        cat["rules"][base] = regex
        if first.conditions.get("allowed_libraries") == "movie":
            cat["movies_only"] = True
        corner = _align_corner(first.layout.get("horizontal_align"), first.layout.get("vertical_align"))
        off = _offset(first.layout)
        if corner:
            cat["align"] = corner
        if off is not None:
            cat["offset"] = off
        # Use the 1080p variant's image when present (4K-only files would leak a
        # per-resolution name); prefer whichever source actually exists.
        rels = [_source_relpath(g.file or "") for g in group]
        rels = [r for r in rels if r]
        chosen = next((r for r in rels if _resolve_source(root, r) is not None),
                      rels[0] if rels else None)
        if chosen:
            src = _resolve_source(root, chosen) or (root / chosen)
            tgt = f"images/{first.group or base}/{base}.png"
            copies.append((str(src), tgt))
            if not src.is_file():
                _note(report, base, f"missing source image {src}")

    # ---- remaining overlays -------------------------------------------------
    for fo in flat:
        if id(fo) in consumed:
            continue
        cond = fo.conditions
        name = fo.group or fo.key

        if "audience_rating.gte" in cond and fo.is_text:
            # Text overlay at the same rating position: mark the rating_tier
            # category as text (the image overlays carry only the backdrop).
            tier_cat = None
            for cdata in categories.values():
                if cdata.get("matcher") == "rating_tier":
                    tier_cat = cdata
                    break
            if tier_cat is not None:
                tier_cat["text"] = "{value}"
            else:
                cat = category(name)
                cat["matcher"] = "rating_tier"
                cat["tiers"] = {str(float(cond["audience_rating.gte"])): fo.key}
                cat["source"] = "audience"
                cat["text"] = "{value}"
            continue

        if "audience_rating.gte" in cond and not fo.is_text:
            cat = category(name)
            tier_key = re.sub(r"_backdrop$", "", fo.key)
            add_key(cat, tier_key)
            cat["matcher"] = "rating_tier"
            tiers = cat.setdefault("tiers", {})
            try:
                val = float(cond["audience_rating.gte"])
            except (TypeError, ValueError):
                val = 0.0
            tiers[str(val)] = tier_key
            cat["source"] = "audience"
            corner = _align_corner(fo.layout.get("horizontal_align"), fo.layout.get("vertical_align"))
            off = _offset(fo.layout)
            if corner:
                cat["align"] = corner
            if off is not None:
                cat["offset"] = off
            rel = _source_relpath(fo.file or "")
            if rel:
                src = _resolve_source(root, rel) or (root / rel)
                tgt = f"images/{name}/{tier_key}.png"
                copies.append((str(src), tgt))
                if not src.is_file():
                    _note(report, fo.key, f"missing source image {src}")
            continue


        if cond.get("resolution") is not None and "filepath.regex" not in cond:
            rule_key = _res_rule_key(cond["resolution"])
            cat = category(name)
            add_key(cat, rule_key or str(cond["resolution"]).lower())
            cat["matcher"] = "format_rule"
            key_map = cat.setdefault("key_map", {})
            if rule_key:
                key_map[str(rule_key)] = rule_key
            else:
                _note(report, fo.key, f"unknown resolution {cond['resolution']!r}; no Finmeta rule key")
            corner = _align_corner(fo.layout.get("horizontal_align"), fo.layout.get("vertical_align"))
            off = _offset(fo.layout)
            if corner:
                cat["align"] = corner
            if off is not None:
                cat["offset"] = off
            rel = _source_relpath(fo.file or "")
            key_used = rule_key or str(cond["resolution"]).lower()
            if rel:
                src = _resolve_source(root, rel) or (root / rel)
                tgt = f"images/{name}/{key_used}.png"
                copies.append((str(src), tgt))
                if not src.is_file():
                    _note(report, fo.key, f"missing source image {src}")
            continue

        if "filepath.regex" in cond:
            cat = category(name)
            add_key(cat, fo.key)
            cat["matcher"] = "filename_regex"
            cat["rules"][fo.key] = str(cond["filepath.regex"])
            if cond.get("allowed_libraries") == "movie":
                cat["movies_only"] = True
            corner = _align_corner(fo.layout.get("horizontal_align"), fo.layout.get("vertical_align"))
            off = _offset(fo.layout)
            if corner:
                cat["align"] = corner
            if off is not None:
                cat["offset"] = off
            rel = _source_relpath(fo.file or "")
            if rel:
                src = _resolve_source(root, rel) or (root / rel)
                tgt = f"images/{name}/{fo.key}.png"
                copies.append((str(src), tgt))
                if not src.is_file():
                    _note(report, fo.key, f"missing source image {src}")
            continue

        # No condition: full-poster backdrops (gradients) & friends.
        _note(report, fo.key, "not supported in D3 (no condition / full-poster backdrop)")
        for item in report.items:
            if item.key == fo.key:
                item.status = "skipped"

    # ---- assemble pack dict --------------------------------------------------
    cats_out: Dict[str, Dict[str, Any]] = {}
    for name, cat in categories.items():
        out: Dict[str, Any] = {"matcher": cat["matcher"], "keys": list(cat["keys"])}
        if cat.get("rules"):
            out["filename_regex"] = dict(cat["rules"])
        if cat.get("key_map"):
            out["key_map"] = dict(cat["key_map"])
        if cat.get("tiers"):
            out["tiers"] = dict(cat["tiers"])
        for extra in ("movies_only", "source", "text", "align", "offset"):
            if cat.get(extra) is not None:
                out[extra] = cat[extra]
        cats_out[name] = out

    pack_yml = {"name": pack_name, "description": f"Imported from Kometa overlays ({report.source})",
                "categories": cats_out}
    return pack_yml, copies, report


__all__ = ["kometa_to_pack"]
