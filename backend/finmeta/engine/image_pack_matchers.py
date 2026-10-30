"""Image pack matchers (SPEC S9 D3 step 2).

Each pack category declares one matcher type that decides which image key(s)
to use for a given item. Matchers consume existing K9/K10 detection results —
they never re-detect streams or ratings themselves.

* ``format_rule`` — the winning K9 rule's ``image`` field (minus ``.png``) is
  the key. When the combined image is absent, fall back to its constituent
  parts (e.g. ``dv_hdr_plus`` → ``dv`` + ``hdr10plus``).
* ``filename_regex`` — the item's filename is matched against the category
  config's regex(es): either one independent pattern PER key
  (``filename_regex: {key: pattern}``, what real Kometa/jmxd-style imports
  use), or a single ``regex`` with a ``(?P<key>...)`` named group/capture.
* ``metadata`` — a metadata field value is looked up in the pack's key list.
* ``rating_tier`` — the TMDb vote_average is bucketed into tiers defined in
  the category config (e.g. ``gold: ">=8.0"``).

Missing image → no badge (the caller simply skips when the file is absent).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence

from finmeta.engine.image_packs import CategoryDef


@dataclass
class MatchContext:
    """Everything a matcher needs to decide the key for one item."""
    #: K9 detection result for this category (the winning rule dict or None).
    k9_result: Optional[Dict[str, Any]] = None
    #: ALL available K9 results at once, keyed by Finmeta overlay ELEMENT name
    #: -- "resolution"/"hdr"/"audio" -- NOT the raw K9 rule-category name
    #: ("dynamic_range" for hdr; see plan_builder.py's/routes_settings.py's
    #: own `k9m = {"resolution": ..., "hdr": det.get("dynamic_range"), ...}`
    #: construction, which is what callers actually pass as all_k9m). A
    #: pack's `components` list must use this element vocabulary, not the K9
    #: category vocabulary, or the hdr component silently never resolves
    #: (hit this for real: pete2583's first pack.yml declared `dynamic_range`
    #: and every HDR/DV item silently misclassified as SDR). For
    #: format_rule_combo, which needs more than one category's result. Every
    #: other matcher ignores this and uses the singular k9_result above.
    k9_results: Optional[Mapping[str, Dict[str, Any]]] = None
    #: The item's filename (basename without extension), for filename_regex.
    filename: str = ""
    #: Arbitrary metadata (TMDb details, etc.), for the metadata matcher.
    metadata: Dict[str, Any] = None  # type: ignore[assignment]
    #: TMDb vote_average (0-10 float or None), for rating_tier.
    rating_value: Optional[float] = None

    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {}


def _strip_ext(name: str) -> str:
    """Strip a trailing .png/.jpg/.jpeg from an image filename."""
    for ext in (".png", ".jpg", ".jpeg"):
        if name.lower().endswith(ext):
            return name[: -len(ext)]
    return name


# ---------------------------------------------------------------------------
# format_rule matcher (with combine fallback)
# ---------------------------------------------------------------------------

#: Known "combined" image keys that decompose into parts.
_COMBINE_PARTS: Dict[str, List[str]] = {
    "dv_hdr_plus": ["dv", "hdr10plus"],
    "dv_hdr": ["dv", "hdr10"],
}


def match_format_rule(cat_def: CategoryDef, ctx: MatchContext) -> Optional[List[str]]:
    """Use the K9 rule's image field as the pack key.

    If the combined key is not in the pack's declared keys, decompose into
    parts (if known) and return those instead. Returns None when there is no
    K9 result or no usable key.
    """
    if ctx.k9_result is None:
        return None
    image = ctx.k9_result.get("image", "")
    key = _strip_ext(image)
    if not key:
        return None
    # Check if the exact key is available in this category
    if key in cat_def.keys:
        return [key]
    # Combine fallback: try decomposing
    parts = _COMBINE_PARTS.get(key)
    if parts:
        available = [p for p in parts if p in cat_def.keys]
        if available:
            return available
    return None


# ---------------------------------------------------------------------------
# format_rule_combo matcher -- resolution + dynamic_range + audio, all from
# real K9 detection (never filename guessing), into ONE badge image per
# combination (SPEC follow-up: pete2583-style combined overlay packs).
# ---------------------------------------------------------------------------

#: K9 dynamic_range rule key -> this matcher's own bucket vocabulary. Every
#: DV flavour (plain or stacked with HDR10/HDR10+) buckets to "dv"; every
#: HDR flavour buckets to "hdr"; no result (SDR, by K9 design -- see
#: format_rules.py) buckets to "sdr".
_DYNAMIC_RANGE_BUCKETS: Dict[Optional[str], str] = {
    None: "sdr",
    "hdr10": "hdr", "hdr10plus": "hdr", "hlg": "hdr",
    "dovi": "dv", "dovi_hdr10": "dv", "dovi_hdr10plus": "dv",
}

#: K9 audio rule key -> this matcher's own suffix vocabulary. Plain "dts",
#: "aac", "other" and no-result all count as "no special audio codec" --
#: same bucket as absent, i.e. no suffix at all (omitted from this map).
_AUDIO_SUFFIXES: Dict[str, str] = {
    "dd": "dd", "ddplus": "ddplus", "ddplus_atmos": "ddplus_atmos",
    "truehd": "truehd", "truehd_atmos": "truehd_atmos",
    "dtshd_ma": "dtshd_ma", "dtsx": "dtsx",
}


def match_format_rule_combo(cat_def: CategoryDef, ctx: MatchContext) -> Optional[List[str]]:
    """Combine multiple K9 category results into one badge key.

    ``cat_def.components`` names the K9 categories to combine, in order
    (e.g. ``["resolution", "dynamic_range", "audio"]``). Builds candidate
    keys ``"{resolution}__{dynamic_range_bucket}__{audio_suffix}"`` then,
    falling back, ``"{resolution}__{dynamic_range_bucket}"`` (no audio
    suffix) -- the first candidate present in ``cat_def.keys`` wins.
    Returns None when ``components``/``k9_results`` is unset, or when the
    resolution component has no K9 result (nothing to badge).
    """
    if not cat_def.components or not ctx.k9_results:
        return None
    components = list(cat_def.components)
    if not components:
        return None

    res_result = ctx.k9_results.get(components[0])
    if not res_result:
        return None
    res_key = res_result.get("key")
    if not res_key:
        return None

    dr_key = None
    if len(components) > 1:
        dr_result = ctx.k9_results.get(components[1])
        dr_key = dr_result.get("key") if dr_result else None
    dr_bucket = _DYNAMIC_RANGE_BUCKETS.get(dr_key)
    if dr_bucket is None:
        return None  # an unrecognised dynamic_range key -- don't guess

    audio_suffix = None
    if len(components) > 2:
        audio_result = ctx.k9_results.get(components[2])
        audio_key = audio_result.get("key") if audio_result else None
        audio_suffix = _AUDIO_SUFFIXES.get(audio_key)  # None for dts/aac/other/absent

    if audio_suffix:
        full_key = f"{res_key}__{dr_bucket}__{audio_suffix}"
        if full_key in cat_def.keys:
            return [full_key]
    base_key = f"{res_key}__{dr_bucket}"
    if base_key in cat_def.keys:
        return [base_key]
    return None


# ---------------------------------------------------------------------------
# filename_regex matcher
# ---------------------------------------------------------------------------

def match_filename_regex(cat_def: CategoryDef, ctx: MatchContext) -> Optional[List[str]]:
    """Match the item's filename against the category's regex(es).

    Two supported shapes (``cat_def.filename_regex`` tried first):

    * ``filename_regex: {key: pattern, ...}`` -- one independent pattern PER
      key, tried in declaration order; the first key whose own pattern
      matches wins. This is the shape real Kometa/jmxd-style media_info.yml
      sources use (each badge has its own regex, not a shared alternation),
      and what ``kometa_import.to_pack`` actually emits -- without this, no
      category using this shape (real-world imports almost always do) could
      ever match anything, imported pack or not (found 2026-10-22 against an
      operator-imported pack where resolution worked but every
      filename_regex category silently never matched).
    * ``regex: pattern`` with a single ``(?P<key>...)`` named group (or the
      first capture group) -- the original, still-supported shape for a
      hand-written pack.yml.

    Returns ``[key]`` on match, None otherwise.
    """
    if not ctx.filename:
        return None
    if cat_def.filename_regex:
        for key, pattern in cat_def.filename_regex.items():
            if key not in cat_def.keys:
                continue
            try:
                if re.search(pattern, ctx.filename, re.IGNORECASE):
                    return [key]
            except re.error:
                continue
        return None
    if not cat_def.regex:
        return None
    try:
        m = re.search(cat_def.regex, ctx.filename, re.IGNORECASE)
    except re.error:
        return None
    if m is None:
        return None
    key = m.group("key") if "key" in m.groupdict() else (m.group(1) if m.groups() else None)
    if key is None:
        return None
    # Case-insensitive comparison against the declared keys
    for k in cat_def.keys:
        if k.lower() == key.lower():
            return [k]
    return None


# ---------------------------------------------------------------------------
# metadata matcher
# ---------------------------------------------------------------------------

def match_metadata(cat_def: CategoryDef, ctx: MatchContext) -> Optional[List[str]]:
    """Look up a metadata field value in the category's key list.

    ``cat_def.field`` names the metadata key (e.g. ``"original_language"``).
    The corresponding value (stringified) must be one of the declared keys.
    """
    if not cat_def.field:
        return None
    val = ctx.metadata.get(cat_def.field)
    if val is None:
        return None
    key = str(val).strip()
    if key in cat_def.keys:
        return [key]
    return None


# ---------------------------------------------------------------------------
# rating_tier matcher
# ---------------------------------------------------------------------------

def _tier_match(expression: str, value: float) -> bool:
    """Evaluate a simple comparison expression like '>=8.0' or '<6.0'."""
    expr = expression.strip()
    for op in (">=", "<=", ">", "<", "=="):
        if expr.startswith(op):
            threshold = float(expr[len(op):])
            if op == ">=":
                return value >= threshold
            if op == "<=":
                return value <= threshold
            if op == ">":
                return value > threshold
            if op == "<":
                return value < threshold
            if op == "==":
                return value == threshold
    return False


def match_rating_tier(cat_def: CategoryDef, ctx: MatchContext) -> Optional[List[str]]:
    """Bucket the rating into tiers defined in the category config.

    Tiers are evaluated in the order they appear in ``cat_def.tiers`` (dict
    insertion order). The first tier whose expression matches wins. Returns
    None when there is no rating or no tier matches.
    """
    if ctx.rating_value is None:
        return None
    tiers = cat_def.tiers or {}
    for key, expr in tiers.items():
        if key not in cat_def.keys:
            continue
        try:
            if _tier_match(expr, ctx.rating_value):
                return [key]
        except (ValueError, TypeError):
            continue
    return None


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

_MATCHERS = {
    "format_rule": match_format_rule,
    "format_rule_combo": match_format_rule_combo,
    "filename_regex": match_filename_regex,
    "metadata": match_metadata,
    "rating_tier": match_rating_tier,
}


def resolve_pack_key(cat_def: CategoryDef, ctx: MatchContext) -> Optional[List[str]]:
    """Dispatch to the category's matcher and return image key(s) or None."""
    fn = _MATCHERS.get(cat_def.matcher)
    if fn is None:
        return None
    return fn(cat_def, ctx)


__all__ = [
    "MatchContext", "resolve_pack_key",
    "match_format_rule", "match_format_rule_combo", "match_filename_regex",
    "match_metadata", "match_rating_tier",
]
