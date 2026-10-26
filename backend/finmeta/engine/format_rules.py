"""Format detection rules for conditional overlay badges (SPEC_KOMETA K9).

Each rule is an ordered, editable entry: ``{key, match, label, image, priority,
enabled}``. For one item the first *enabled* rule in each category whose match
condition holds wins; unknown or missing metadata yields no badge (never guess).

Detection facts come from the primary media source's MediaStreams (verified on
the test server via GET /Items?Ids=: video streams carry Codec, Profile, Width,
Height, VideoRange, VideoRangeType and DisplayTitle; audio streams add
Channels + ChannelLayout, ColorTransfer/ColorPrimaries are usually absent).
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Mapping, Optional, Sequence

class RuleMatchError(Exception):
    """A stored rule's match expression could not be evaluated safely."""


_WORD_RE = re.compile(r"[A-Za-z0-9]+(?:[:+][A-Za-z0-9]+)*")


def _split_words(text: str) -> List[str]:
    """Lower-cased word tokens of a DisplayTitle/codec string."""
    return [m.group(0).lower() for m in _WORD_RE.finditer(text)]

# ---------------------------------------------------------------------------
# config defaults (K9) -- stored under overlays.format_rules in config.yml
# ---------------------------------------------------------------------------


def _rule(key: str, match: str, label: str, image: str, priority: int, enabled: bool = True) -> dict:
    return {"key": key, "match": match, "label": label, "image": image,
            "priority": priority, "enabled": enabled}


#: Default dynamic-range rules. First enabled match by ascending priority wins.
DEFAULT_DYNAMIC_RANGE_RULES: List[dict] = [
    # DOVIWithHDR10Plus is a superset of every other DV flavour, so it must be
    # checked first even though its name contains HDR10/HDR10+ fragments.
    _rule("dovi_hdr10plus", "VideoRangeType=DOVIWithHDR10Plus", "DV+HDR10+", "dv_hdr_plus.png", 1),
    _rule("dovi_hdr10", "VideoRangeType=DOVIWithHDR10|VideoRangeType=DOVIWithHLG", "DV+HDR10", "dv_hdr.png", 2),
    _rule("dovi", "VideoRangeType=DOVI|VideoRangeType=DOVIWithSDR", "DV", "dv.png", 3),
    _rule("hdr10plus", "VideoRangeType=HDR10Plus", "HDR10+", "hdr10plus.png", 4),
    _rule("hdr10", "VideoRangeType=HDR10|fallback=smpte2084", "HDR10", "hdr10.png", 5),
    _rule("hlg", "VideoRangeType=HLG|fallback=arib-std-b67", "HLG", "hlg.png", 6),
]

#: Default resolution rules: WIDTH decides first (a 3840x1600 scope film is
#: still 4K), height buckets are only the fallback when width says nothing.
DEFAULT_RESOLUTION_RULES: List[dict] = [
    _rule("res_4k", "width>=3200", "4K", "res_4k.png", 1),
    _rule("res_1080p", "width>=1600|height>=950", "1080p", "res_1080p.png", 2),
    _rule("res_720p", "width>=1200|height>=640", "720p", "res_720p.png", 3),
    _rule("res_576p", "height>=540", "576p", "res_576p.png", 4),
    _rule("res_480p", "height>=420", "480p", "res_480p.png", 5),
]

#: Default audio rules, evaluated against the *best* track (highest channel
#: count among non-forced/external candidates). Codec/profile tokens come from
#: DisplayTitle + Profile; ``other`` is a catch-all that still requires an
#: actual audio stream to exist.
_AUDIO_CODEC_ALIASES = {
    # codec part of a rule -> normalised token(s) that satisfy it. Ordered so
    # specific tokens never leak into parent rules (DTS:X stays DTS:X, etc.).
    "TRUEHD": ("TRUEHD",),
    "MLP": ("MLP",),                # TrueHD's MPEG-Surround sibling spelling
    "EAC3": ("EAC3"),               # eac3 / E-AC-3 / DD+ all normalise here
    "DDPLUS": ("EAC3",),
    "DTSCX": ("DTSCX",),            # profile-carrying multi-channel DTS
    "DTSHDMA": ("DTSHDMA"),         # lossless DTS-HD Master Audio
    "DTSHD": ("DTSHD",),
    "DTS": ("DTS",),                # plain multi-channel DTS only
    "AC3": ("AC3",),                # Dolby Digital after DD+ matched
}


DEFAULT_AUDIO_RULES: List[dict] = [
    _rule("truehd_atmos", "atmos=truehd|atmos=mlp", "TrueHD Atmos", "truehd_atmos.png", 1),
    _rule("ddplus_atmos", "atmos=eac3|atmos=dd+", "DD+ Atmos", "ddplus_atmos.png", 2),
    _rule("dtsx", "profile=DTS:X", "DTS:X", "dtsx.png", 3),
    _rule("dtshd_ma", "profile=DTS-HD MA", "DTS-HD MA", "dtshd_ma.png", 4),
    _rule("truehd", "codec=TRUEHD", "TrueHD", "truehd.png", 5),
    _rule("ddplus", "codec=EAC3", "DD+", "ddplus.png", 6),
    _rule("dts", "codec=DTS", "DTS", "dts.png", 7),
    _rule("dd", "codec=AC3", "DD", "dd.png", 8),
    _rule("aac", "codec=AAC", "AAC", "aac.png", 9),
    _rule("other", "*", "Audio", "audio_generic.png", 10, enabled=False),
]

#: Per-category defaults. SDR has no dynamic-range rule by design (K9: none for SDR).
DEFAULT_FORMAT_RULES: Dict[str, List[dict]] = {
    "dynamic_range": DEFAULT_DYNAMIC_RANGE_RULES,
    "resolution": DEFAULT_RESOLUTION_RULES,
    "audio": DEFAULT_AUDIO_RULES,
}


def default_format_rules() -> Dict[str, List[dict]]:
    """Fresh deep copy of the K9 default rules."""
    return {cat: [dict(r) for r in rules] for cat, rules in DEFAULT_FORMAT_RULES.items()}


#: Overlay element name -> detection category (K9). Resolution/HDR/audio are rule-driven;
#: rating is a separate per-source value and never goes through format rules.
OVERLAY_ELEMENT_TO_CATEGORY: Dict[str, str] = {
    "resolution": "resolution",
    "hdr": "dynamic_range",
    "audio": "audio",
}


def media_stream_to_dict(stream) -> Dict[str, Any]:
    """Normalise one MediaStream to a dict with JELLYFIN keys for the detectors.

    Accepts either a raw API dict (Jellyfin keys already present) or a
    ``MediaStream`` dataclass object. Objects only carry the fields their
    ``from_dict`` captured -- so pass raw dicts wherever available for full
    fidelity (DisplayTitle / IsForced / ChannelLayout / ColorTransfer matter for
    audio + HDR fallback matching). Missing keys become None (never KeyError).
    """
    if isinstance(stream, Mapping):
        return dict(stream)
    out: Dict[str, Any] = {}
    field_map = [
        ("Type", "type"), ("Codec", "codec"), ("Width", "width"),
        ("Height", "height"), ("Channels", "channels"),
        ("VideoRange", "video_range"), ("VideoRangeType", "video_range_type"),
        ("Profile", "profile"), ("Level", "level"), ("BitRate", "bit_rate"),
    ]
    for jkey, attr in field_map:
        out[jkey] = getattr(stream, attr, None)
    return out


def streams_to_dicts(streams: Sequence[Any]) -> List[Dict[str, Any]]:
    """Convert every stream to a Jellyfin-keyed dict (see media_stream_to_dict)."""
    return [media_stream_to_dict(s) for s in (streams or [])]


#: Human labels per detection category (K9); used by reports/UI.
CATEGORY_LABELS: Dict[str, str] = {
    "dynamic_range": "Dynamic range",
    "resolution": "Resolution",
    "audio": "Audio",
}

# ---------------------------------------------------------------------------
# stream facts extraction
# ---------------------------------------------------------------------------

def _num(value: Any) -> int:
    """Positive integer from a stream field; ``-1`` sentinel never matches."""
    n = _int(value)
    return n if n is not None else -1


def video_facts(streams: Sequence[Mapping]) -> Optional[Dict[str, Any]]:
    """Facts from the first video MediaStream, or None when absent.

    ``fallback`` is a VideoRangeType-less HDR hint derived from color transfer:
    smpte2084 (PQ) implies an HDR10-family range, arib-std-b67 implies HLG.
    """
    v = next((s for s in streams if (s.get("Type") or "").lower() == "video"), None)
    if not v:
        return None
    width = _int(v.get("Width"))
    height = _int(v.get("Height"))
    fallback = ""
    ct = str(v.get("ColorTransfer") or "").strip().lower()
    if ct == "smpte2084":
        fallback = "smpte2084"
    elif "arib" in ct:  # arib-std-b67 / AribStdB67 variants of HLG transfer
        fallback = "arib-std-b67"
    return {
        "width": width,
        "height": height,
        "video_range_type": str(v.get("VideoRangeType") or ""),
        "video_range": str(v.get("VideoRange") or ""),
        "color_transfer": ct,
        "fallback": fallback,
        "display_title": str(v.get("DisplayTitle") or ""),
    }


def best_audio_facts(streams: Sequence[Mapping]) -> Optional[Dict[str, Any]]:
    """Facts of the highest-channel-count audio track (forced/external skipped).

    Returns None when there is no usable audio stream. ``profile`` and
    ``codec`` are normalised upper-case tokens for rule matching; the raw
    DisplayTitle feeds Atmos detection as well.
    """
    audios = [
        s for s in streams
        if (s.get("Type") or "").lower() == "audio"
        and not bool(s.get("IsForced")) and not bool(s.get("IsExternal"))
    ]
    if not audios:
        return None
    def ch(s):
        try:
            return int(s.get("Channels") or 0)
        except (TypeError, ValueError):
            return 0
    best = max(audios, key=ch)
    codec = _token(best.get("Codec"))
    profile = _token(best.get("Profile"))
    title_tokens = set(_tokens(best.get("DisplayTitle"))) | {codec} | ({profile} if profile else set())
    atmos = any(("atmos" in t.lower()) for t in title_tokens)
    return {
        "channels": ch(best),
        "codec": codec,
        "profile": str(best.get("Profile") or ""),
        "display_title": str(best.get("DisplayTitle") or ""),
        "title_tokens": sorted(title_tokens),
        "atmos": atmos,
    }


def _int(value: Any) -> Optional[int]:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _token(value: Any) -> str:
    """Normalise a codec/profile string into an upper-case search token.

    ``eac3`` (Jellyfin's DisplayTitle spelling), ``E-AC3`` and ``DD+`` all
    normalise to ``EAC3`` so the alias table below can match them uniformly.
    Other codecs keep their letter case-insensitive form, minus separators.
    """
    raw = str(value or "")
    s = re.sub(r"[^A-Za-z0-9]+", "", raw).upper()
    if s in ("EAC3", "DDPLUS", "DOLBYDIGITALPLUS"):
        return "EAC3"
    return s


def _tokens(value: Any) -> List[str]:
    parts = _split_words(str(value or ""))
    out: List[str] = []
    for part in parts:
        t = _token(part)
        if t and t not in out:
            out.append(t)
    return out


# ---------------------------------------------------------------------------
_AUDIO_CODEC_ALIASES = {
    # rule codec part -> normalised token(s) that satisfy it. Ordered so specific
    # tokens never leak into parent rules (DTS:X stays DTS:X, DTSHDMA != plain DTS).
    "TRUEHD": ("TRUEHD",),
    "MLP": ("MLP",),               # TrueHD's MPEG-Surround sibling spelling
    "EAC3": ("EAC3",),             # eac3 / E-AC-3 / DD+ all normalise here
    "DDPLUS": ("EAC3",),
    "DTSCX": ("DTSCX",),           # profile-carrying multi-channel DTS
    "DTSHDMA": ("DTSHDMA",),       # lossless DTS-HD Master Audio
    "DTSHD": ("DTSHD",),
    "DTS": ("DTS",),               # plain multi-channel DTS only
    "AC3": ("AC3"),                # Dolby Digital after DD+ matched
}



# rule matching -- one function per category, all pure over the extracted facts
# ---------------------------------------------------------------------------


def _norm(value: Any) -> str:
    """Upper-case with separators stripped; used to compare enum-ish values."""
    return re.sub(r"[^A-Za-z0-9]+", "", str(value or "")).upper()


def _match_dynamic_range(rule_match: str, facts: Optional[Mapping]) -> bool:
    """``VideoRangeType=X | fallback=Y`` -- exact match on the normalised value.

    ``fallback`` is the color-transfer hint computed by :func:`video_facts`
    when VideoRangeType itself is missing from the stream. Unknown condition
    names never match (K9: unknown metadata -> no badge, never guess).
    """
    if not facts:
        return False
    vrt = _norm(facts.get("video_range_type"))
    for cond in rule_match.split("|"):
        m = re.match(r"^\s*([A-Za-z_]+)\s*=\s*(.+?)\s*$", cond)
        if not m:
            continue  # malformed condition -> inert
        name, val = m.group(1), m.group(2)
        if name == "VideoRangeType" and _norm(val) == vrt:
            return True
        if name == "fallback" and _norm(val) == _norm(facts.get("fallback")):
            return True
    return False


_RES_COND_RE = re.compile(r"(width|height)\s*(>=|<=|=|>|<)\s*(-?\d+)")


def _match_resolution(rule_match: str, facts: Optional[Mapping]) -> bool:
    """``width>=3200 | height>=1700 ...`` -- first satisfied bucket wins.

    A missing axis (-1 sentinel) can never satisfy a comparison so streams
    without Width/Height simply match no resolution rule (no badge).
    """
    if not facts:
        return False
    width = _num(facts.get("width"))
    height = _num(facts.get("height"))
    for cond in [c.strip() for c in rule_match.replace(",", "|").split("|")]:
        m = _RES_COND_RE.fullmatch(cond)
        if not m:
            continue  # malformed condition -> inert
        actual = width if m.group(1) == "width" else height
        bound = int(m.group(3))
        op = m.group(2)
        ok = {"=": actual == bound, ">": actual > bound, "<": actual < bound,
              ">=": actual >= bound, "<=": actual <= bound}[op]
        if ok and actual != -1:
            return True
    return False


def _norm(value: Any) -> str:
    """Upper-case with separators stripped; used to compare enum-ish values."""
    return re.sub(r"[^A-Za-z0-9]+", "", str(value or "")).upper()


def _blob(facts: Mapping) -> str:
    """Normalised codec+profile blob the audio rules search inside of."""
    return (_norm(facts.get("codec")) + "|" + _norm(facts.get("profile")))


_AUDIO_ALIASES = {
    # rule codec part (normalised) -> tokens accepted on this track. Ordered so a
    # specific token never satisfies its parent's rule (DTS:X stays DTS:X etc.).
    "TRUEHD": ("TRUEHD",),
    "MLP": ("MLP",),                 # TrueHD's MPEG-Surround sibling spelling
    "EAC3": ("EAC3",),               # eac3 / E-AC-3 / DD+ all normalise here
    "DDPLUS": ("EAC3",),
    "DTSCX": ("DTSCX",),             # profile-carrying multi-channel DTS
    "DTSHDMA": ("DTSHDMA"),          # lossless DTS-HD Master Audio
    "DTSHD": ("DTSHD",),
    "DTS": ("DTS",),                 # plain multi-channel DTS only
    "AC3": ("AC3",),                 # Dolby Digital after DD+ matched
}


def _audio_alias_hit(alias: str, facts: Mapping) -> bool:
    """Does the track satisfy ``alias`` via :data:`_AUDIO_ALIASES`?"""
    key = re.sub(r"[^A-Za-z0-9]+", "", alias.upper())
    tokens = _AUDIO_ALIASES.get(key, (key,) if len(re.sub("[^A-Za-z0-9]+", "", alias)) >= 3 else ())
    blob = _blob(facts).replace("|", "").lower()
    hit = any(
        len(t.lower()) >= 3 and t.lower() in blob
        # plain-DTS rule must never fire on DTS:X / DTS-HD tracks; those have their own rules.
        and not (t == "DTS" and ("dtscx" in blob or "dtshd" in blob))
        for t in tokens
    )
    return hit



def _atmos_on_track(facts: Mapping) -> bool:
    """Atmos anywhere on the track: codec token, profile string or title words."""
    joined = "|".join([t.lower() for t in (facts.get("title_tokens") or [])] + [
        str(facts.get("codec") or "").lower(), str(facts.get("profile") or "").lower()])
    return "atmos" in re.sub(r"[^a-z]", "", joined)


def _match_dynamic_range(rule_match: str, facts: Optional[Mapping]) -> bool:
    """``VideoRangeType=X | fallback=Y`` conditions, ``|`` separated.

    Exact match against normalised values only -- unknown condition names never
    match (K9: missing/unknown metadata yields no badge, never a guess). The
    ``fallback`` value is the color-transfer hint computed by :func:`video_facts`
    when VideoRangeType itself is absent from the stream.
    """
    if not facts:
        return False
    vrt = _norm(facts.get("video_range_type"))
    for cond in rule_match.split("|"):
        m = re.match(r"^\s*([A-Za-z_]+)\s*=\s*(.+?)\s*$", cond)
        if not m:
            continue  # malformed condition -> inert
        name, val = m.group(1), m.group(2)
        if name == "VideoRangeType" and _norm(val) == vrt:
            return True
        if name == "fallback" and _norm(val) == _norm(facts.get("fallback")):
            return True
    return False


_RES_COND_RE = re.compile(r"(width|height)\s*(>=|<=|=|>|<)\s*(-?\d+)")


def _match_resolution(rule_match: str, facts: Optional[Mapping]) -> bool:
    """Width-first K9 bucket matching; height only as fallback when width says nothing.

    Each condition names exactly one axis (``width>=N`` / ``height>=N`` ...). Width
    comparisons are evaluated first across all of them -- per K9 "by WIDTH first", so a
    scope/cropped file like 1920x800 still reads as 1080p via its height while any
    >=3200-wide stream is unconditionally 4K even at reduced height (e.g. 3840x1600).
    Only if no width condition holds do the height conditions get a chance to match. A
    missing/zero axis yields the -1 sentinel and can never satisfy a comparison, which is
    what makes pure-height streams (no Width reported) fall through correctly instead of
    always mis-bucketing by whatever partial data happened to be present.
    """
    if not facts:
        return False
    width = _num(facts.get("width"))
    height = _num(facts.get("height"))
    width_hits = []
    height_hits = []
    for cond in [c.strip() for c in rule_match.replace(",", "|").split("|")]:
        m = _RES_COND_RE.fullmatch(cond)
        if not m:
            continue  # malformed condition -> inert
        actual = width if m.group(1) == "width" else height
        bound = int(m.group(3))
        op = m.group(2)
        ok = {"=": actual == bound, ">": actual > bound, "<": actual < bound,
              ">=": actual >= bound, "<=": actual <= bound}[op]
        hit = bool(ok and actual != -1)
        (width_hits if m.group(1) == "width" else height_hits).append(hit)
    if any(width_hits):
        return True
    return any(height_hits)
def _atmos_codec_hit(codec_part: str, facts: Mapping) -> bool:
    """Codec part of an ``atmos=<...>`` condition matched over codec tokens."""
    return _audio_alias_hit(re.sub(r"[^A-Za-z0-9+]", "", codec_part), facts)


def _match_audio(rule_match: str, facts: Optional[Mapping]) -> bool:
    """First audio condition that holds wins; conditions are ``|`` separated.

    Condition grammar per rule entry (first hit returns):
      * ``*``                 -- catch-all: any usable audio stream exists
      * ``atmos=CODEC``       -- Atmos on track AND CODEC matches the alias table
                                 or a free-text token (e.g. ``atmos=truehd``,
                                 ``atmos=eac3|atmos=dd+``)
      * bare ``atmos``        -- any Atmos track of any codec (not shipped by default)
      * ``codec=ALIAS``       -- accepted tokens from :data:`_AUDIO_ALIASES`
      * ``profile=X``         -- normalised profile string compare (DTS:X / DTS-HD MA)
      * free text             -- substring over the normalised codec/profile blob
    A missed explicit condition always falls through to the next one so users can
    list alternatives left-to-right in priority order.
    """
    if not facts or (facts.get("channels") in (0, None)):
        return False
    for cond in [c.strip() for c in rule_match.split("|")]:
        if not cond:
            continue
        low = re.sub(r"[^a-z0-9]+", "", cond.lower())

        m_prof = re.match(r"^\s*profile=(.+)$", cond, flags=re.IGNORECASE)
        if m_prof:
            want = _norm(m_prof.group(1))
            have = _norm(facts.get("profile"))
            if want and have == want:
                return True
            continue  # explicit condition missed -> try the next alternative

        m_atmos = re.match(r"^\s*atmos=\s*(.+)$", cond, flags=re.IGNORECASE)
        if m_atmos:
            if not _atmos_on_track(facts):
                continue
            if _audio_alias_hit(re.sub("[+ ]+", "+", m_atmos.group(1)), facts):
                return True
            continue

        if low == "atmos":
            if _atmos_on_track(facts):
                return True
            continue

        m_codec = re.match(r"^\s*codec=([A-Za-z0-9+ ]+)$", cond, flags=re.IGNORECASE)
        if m_codec:
            if _audio_alias_hit(m_codec.group(1), facts):
                return True
            continue

        needle = low.replace("atmos", "").strip()
        if len(needle) >= 3 and needle in _blob(facts).replace("|","").lower():
            return True
    return False


_MATCHERS = {
    "dynamic_range": _match_dynamic_range,
    "resolution": _match_resolution,
    "audio": _match_audio,
}



def detect_category(category: str, streams: Sequence[Mapping],
                    rules: Optional[Sequence[Mapping]] = None) -> Optional[Dict[str, Any]]:
    """First enabled rule whose match holds wins; None => no badge (K9).

    ``rules`` defaults to the K9 default list for ``category``. Disabled rules
    are skipped without erroring so UI-toggled-off entries stay inert. A rule
    with an unparseable match expression raises :class:`RuleMatchError` -- a
    misconfigured stored rule is a config bug, not missing data.
    """
    matcher = _MATCHERS.get(category)
    if matcher is None:
        raise ValueError(f"unknown format-rule category {category!r}")
    if rules is None:
        rules = DEFAULT_FORMAT_RULES[category]
    facts_kwargs = {"streams": streams}
    if category == "dynamic_range":
        facts = video_facts(streams)
    elif category == "resolution":
        facts = video_facts(streams)
    else:
        facts = best_audio_facts(streams)
    ordered = sorted((dict(r) for r in rules), key=lambda r: (r.get("priority", 0), str(r.get("key"))))
    for rule in ordered:
        if not rule.get("enabled", True):
            continue
        try:
            hit = matcher(str(rule.get("match") or ""), facts)
        except RuleMatchError:
            # malformed user-edited condition -> treat as never matching and keep going? No: K9 says unknown => no badge; but a bad *rule* should be visible. We skip it so one typo cannot kill the whole list.
            continue
        if hit:
            return dict(rule, matched=True)
    return None


def detect_all(
    streams: Sequence[Mapping],
    categories_rules: Optional[Mapping[str, Sequence[Mapping]]] = None,
) -> Dict[str, Any]:
    """Detect every category at once plus per-rule counts helpers.

    Returns ``{"dynamic_range": rule|None, "resolution": ..., "audio": ...}``.
    """
    cats = categories_rules or DEFAULT_FORMAT_RULES
    out: Dict[str, Any] = {}
    for cat in ("dynamic_range", "resolution", "audio"):
        rules = cats.get(cat, []) if isinstance(cats, Mapping) else []
        out[cat] = detect_category(cat, streams, rules or None)
    return out



def format_rule_value_for_element(element: str, streams: Sequence[Any],
                                  categories_rules: Optional[Mapping[str, Any]] = None):
    """Detect one overlay element's label via its K9 rule category.

    ``element`` is an overlay name ("resolution"/"hdr"/"audio"). Returns the
    winning rule's human label (str), or ``None`` when no enabled rule matched --
    which means NO badge for that element (K9). Streams may be raw dicts or
    MediaStream objects; they are normalised first. Unknown elements raise ValueError.
    """
    cat = OVERLAY_ELEMENT_TO_CATEGORY.get(element)
    if cat is None:
        raise ValueError(f"no format-rule category for overlay element {element!r}")
    cats = default_format_rules()
    if isinstance(categories_rules, Mapping):
        # merge caller rules over defaults per-category so missing cats keep defaults
        merged = dict(cats)
        for k in list(merged.keys()):
            v = categories_rules.get(k)
            if v:
                merged[k] = [dict(r) for r in v]
        cats = merged
    det = detect_category(cat, streams_to_dicts(streams), cats.get(cat))
    return str(det["label"]) if det else None


def count_rule_matches(elements_streams_pairs: Sequence[Mapping[str, Any]],
                       categories_rules: Optional[Mapping[str, Any]] = None) -> Dict[str, List[int]]:
    """Aggregate per-rule-key match counts across many items.

    Each entry of ``elements_streams_pairs`` carries {"streams": [...]} plus the
    item's detected values are NOT required -- we re-detect from its "streams".
    Returns e.g. {"dynamic_range": ["dovi_hdr10=2", ...], ...} keyed by category;
    a trailing "none=N" tallies items where that category matched no rule.
    """
    cats = default_format_rules()
    if isinstance(categories_rules, Mapping):
        for k in list(cats.keys()):
            v = categories_rules.get(k)
            if v:
                cats[k] = [dict(r) for r in v]
    key_counts: Dict[str, Dict[str, int]] = {c: {} for c in ("dynamic_range", "resolution", "audio")}
    none_counts = {c: 0 for c in key_counts}
    for pair in elements_streams_pairs or []:
        streams = (pair or {}).get("streams") or []
        det_all = detect_all(streams_to_dicts(streams), cats)
        for cat in key_counts:
            d = det_all.get(cat)
            if d:
                kk = str(d.get("key"))
                key_counts[cat][kk] = key_counts[cat].get(kk, 0) + 1
            else:
                none_counts[cat] += 1
    out: Dict[str, List[int]] = {}
    for cat in ("dynamic_range", "resolution", "audio"):
        parts = [f"{k}={v}" for k, v in sorted(key_counts[cat].items())]
        if none_counts[cat]:
            parts.append(f"none={none_counts[cat]}")
        out[cat] = parts
    return out



__all__ = [
    "DEFAULT_FORMAT_RULES", "default_format_rules", "video_facts",
    "best_audio_facts", "detect_category", "detect_all", "RuleMatchError",
    "media_stream_to_dict", "streams_to_dicts",
    "OVERLAY_ELEMENT_TO_CATEGORY", "format_rule_value_for_element",
    "count_rule_matches", "CATEGORY_LABELS",
]
