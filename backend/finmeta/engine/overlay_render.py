"""Overlay preview rendering (SPEC S9 \u00a7D step 1).

Renders a poster with the *editor's* per-element style document and returns PNG
bytes. This is intentionally separate from :func:`finmeta.engine.overlay.render_poster`
(which serves apply-runs with ``OverlayStyle``) so the editor can lay out one
element at a time on a 3\u00d73 grid without changing run semantics.

Pure function of its inputs; never writes files. Missing rating values or
missing logo assets fall back to text badges ("IMDb 8.7"). Rating logos are NOT
shipped (trademarks): the user drops their own PNGs into ``assets/logos/<key>.png``
next to config.yml — i.e. /config/assets/logos inside the container — which is
exactly where the local asset source already looks."""
from __future__ import annotations

import io
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from PIL import Image, ImageDraw, ImageFont

from finmeta.config.models import Config
from finmeta.engine.style_presets import ELEMENT_KEYS, ElementStyle, OverlayStyleDoc

# Anchor vocabulary. Note that "center" belongs to BOTH the row and column axis,
_ROW_WORDS = ("top", "bottom")          # unambiguous row words
_COL_WORDS = ("left", "right")          # unambiguous column words
_BAND_PAD_Y = 10     # extra height (beyond raw text height) for a full-width ribbon band
_BAND_GAP = 2        # vertical gap between stacked full-width bands
_BAND_SIDE_PAD = 16  # reference-width px of breathing room on each side of band text

# Operator-reported (2026-10-25): every pixel-based style field (font_size,
# margin, padding, radius, x/y offset) was applied as a literal pixel value
# regardless of the actual poster's resolution -- a badge tuned to look right
# on a 1000x1500 poster came out tiny on a larger source image (e.g. a
# 2000x3000 TMDb original) and oversized on a smaller one. SPEC_KOMETA.md
# already documents the intended fix for imported Kometa offsets ("Kometa
# offsets are pixels on a 1000x1500 poster; scale to poster size") but that
# scaling was never actually implemented anywhere -- this constant/helper is
# the first real implementation, applied uniformly to every pixel field (not
# just imported-pack offsets) so native Finmeta elements scale too.
_REFERENCE_WIDTH = 1000.0


def _poster_scale(pw: int) -> float:
    """Multiplier so pixel-based style values read as if tuned for a
    ``_REFERENCE_WIDTH``-wide poster, regardless of this poster's real width.
    Clamped to a sane range so a degenerate (near-zero or huge) source image
    can't blow up badge sizes."""
    return max(0.1, min(pw / _REFERENCE_WIDTH, 10.0))


def _scale_placed(p: "_Placed", scale: float) -> "_Placed":
    """Scale one placed element's pixel fields by ``scale`` (see
    ``_poster_scale``). Floors keep tiny posters from collapsing a badge to
    nothing; ``weight``/anchor/colour/text fields are never pixel-based."""
    return replace(
        p,
        size=max(round(p.size * scale), 8),
        radius=max(round(p.radius * scale), 0),
        pad_x=max(round(p.pad_x * scale), 2),
        pad_y=max(round(p.pad_y * scale), 2),
        x_off=round(p.x_off * scale),
        y_off=round(p.y_off * scale),
    )


def _normalize_anchor(anchor):
    """Normalise an element anchor into full ``(row, col)`` grid words.

    ``ElementStyle.position`` carries no pattern check and the editor's 3x3 grid offers a bare
    "center" cell, so hand-edited or legacy styles can carry single-word anchors like "center".
    Missing axes default to the middle one; any unrecognisable word returns (None, None) so
    callers skip just that element instead of crashing the whole preview render."""
    if not anchor:
        return None, None
    parts = [part for part in str(anchor).strip().lower().split("_")]
    rows = [w for w in parts if w in _ROW_WORDS]
    cols = [w for w in parts if w in _COL_WORDS]
    centers = sum(1 for w in parts if w == "center")
    unknown = len(parts) - len(rows) - len(cols) - centers
    if unknown:
        return None, None      # some other word entirely -> unusable anchor
    # If both axes have explicit words + center is present, prefer the last occurrence order
    row = rows[-1] if rows else ("center" if centers else "top")
    col = cols[-1] if cols else ("center" if centers else "left")
    return (row, col)

def _parse_hex(value: str) -> tuple[int, int, int, int]:
    v = value.strip().lstrip("#")
    if len(v) == 6:
        r, g, b = int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16)
        return (r, g, b, 255)
    r, g, b = int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16)
    a = int(v[6:8], 16)
    return (r, g, b, a)


LOGO_PACK_NAMES = ("none", "kometa", "custom")


def _pack_root(config_dir: Optional[str | os.PathLike], assets_root: Optional[str | os.PathLike]) -> Path:
    """Base directory that holds ``logos/<...>``, preferring the local asset source."""
    if config_dir is not None:
        return Path(config_dir) / "assets"
    if assets_root is not None:
        return Path(assets_root)
    raise ValueError("logo_pack needs a config_dir or assets_root to resolve from")


def load_pack_image_bytes(key: str, pack_name: str,
                          config_dir: Optional[str | os.PathLike] = None,
                          assets_root: Optional[str | os.PathLike] = None) -> Optional[bytes]:
    """Read one image from an installed/custom logo pack by its Finmeta key.

    * ``custom`` (or legacy flat layout): ``<root>/logos/<key>.png`` — this also keeps
      working for packs whose files happen to be named after our keys directly under
      ``logos/``, which is what pre-S9c8 users already had on disk.
    * any other name (e.g. ``kometa``): ``<root>/logos/<pack>/<key>.png``.

    Returns raw PNG bytes or None when missing/corrupt; callers fall back to text.
    """
    if pack_name in ("", "none"):
        return None
    try:
        root = _pack_root(config_dir, assets_root)
    except ValueError:
        return None
    rel = ["logos"]
    if pack_name != "custom":
        rel.append(pack_name)
    rel.append(f"{key}.png")
    p = root.joinpath(*rel)
    # Kometa-style RT variants are keyed with a suffix (fresh/rotten etc.); the caller
    # passes the FULL variant key so no special-casing happens here beyond that one lookup.
    try:
        if p.is_file():
            with open(p, "rb") as fh:
                Image.open(fh).verify()
            return p.read_bytes()
    except Exception:
        pass
    # fall back to the legacy flat layout (<root>/logos/<key>.png) so pre-pack user-dropped logos keep working for any pack name (see _collect_placed).
    legacy = root / "logos" / f"{key}.png"
    try:
        if legacy.is_file() and legacy != p:
            with open(legacy, "rb") as fh:
                Image.open(fh).verify()
            return legacy.read_bytes()
    except Exception:
        pass
    return None


def logo_path(key: str, assets_root: Optional[str | os.PathLike],
              config_dir: Optional[str | os.PathLike] = None) -> Optional[Path]:
    """Filesystem path of the user-provided rating logo for ``key``.

    Resolution order (SPEC S9 §D): an explicit ``assets_root`` first
    (the local asset source's root), then ``<config_dir>/assets``; in both
    cases under ``logos/``. Returns None when no candidate file exists.
    """
    roots: list[Path] = []
    if assets_root is not None:
        roots.append(Path(assets_root))
    if config_dir is not None:
        roots.append(Path(config_dir) / "assets")
    for r in roots:
        p = r / "logos" / f"{key}.png"
        try:
            if p.is_file():
                return p
        except OSError:
            continue
    return None


def load_logo_bytes(key: str, assets_root: Optional[str | os.PathLike],
                    config_dir: Optional[str | os.PathLike] = None) -> Optional[bytes]:
    """Read raw PNG bytes from ``<root>/logos/<key>.png``, or None on miss.

    Corrupt/non-image files are treated as missing so previews never crash on
    a half-saved logo drop. Callers pass the local asset source's configured
    path and/or their config directory; see :func:`logo_path`.
    """
    p = logo_path(key, assets_root, config_dir=config_dir)
    if p is None:
        return None
    try:
        # verify() consumes its stream to EOF — reopen before returning bytes.
        with open(p, "rb") as fh:
            Image.open(fh).verify()
        return p.read_bytes()
    except Exception:
        return None


_RATING_TEXT_LABELS = {
    "imdb": "IMDb", "rt_critic": "RT critic", "rt_audience": "RT audience",
    "metacritic": "Metacritic", "tmdb": "\u2605 TMDb", "letterboxd": "Letterboxd",
    "trakt": "Trakt",
}

_INFO_KEYS = ("resolution", "hdr", "audio", "network")
# S9g1/K7a + S9g2/K7b: TV status and trending rank -- always plain text (no
# pack image ever exists for these), each with its own default ribbon colour
# (engine.tv_status.STATUS_COLORS / TRENDING_RANK_COLOR) unless the editor's
# per-element style explicitly overrides back_color/color.
_RIBBON_KEYS = ("status", "trending_rank")


def _parse_score(value: str) -> Optional[float]:
    """First number found anywhere in the value ("8.7", "61%", "9 / 10"), else None."""
    if not isinstance(value, str):
        return None
    import re as _re
    m = _re.search(r"[0-9]+(?:[.]?[0-9]+)?", value)
    if not m:
        return None
    try:
        return float(m.group())
    except ValueError:      # pragma: no cover - regex guarantees digits only
        return None


def rating_pack_key(key: str, value: str):
    """Map a rating element + its value to the logo pack file key.

    RT critic >= 60 maps to ``rt_fresh``, below to ``rt_rotten`` (user-dropped
    logos; missing -> text fallback "RT 87%"); RT audience uses the same
    threshold with *upright*/*spilled*. Other sources are single logos and
    always map regardless of score (the score is drawn next to them anyway)."""
    if not value.strip():
        return None
    v = _parse_score(value)
    if key == "rt_critic":
        # K10 step 2: RT critic >= 60 -> fresh, < 60 -> rotten (no Certified Fresh).
        # User logos are rt_fresh.png / rt_rotten.png (trademarks, NOT shipped);
        # a missing logo falls back to text ("RT 87%").
        if v is None:
            return None
        return "rt_fresh" if v >= 60 else "rt_rotten"
    if key == "rt_audience":
        if v is None:
            return None
        return "rt_audience_upright" if v >= 60 else "rt_audience_spilled"
    simple = {"imdb": "imdb", "tmdb": "tmdb", "metacritic": "metacritic",
              "letterboxd": "letterboxd", "trakt": "trakt"}
    return simple.get(key)


def info_pack_key(element: str, value: str):
    """Map a resolution/HDR/audio element + its rendered text to an image-badge key.

    Returns None when the value does not match anything in the pack vocabulary;
    callers then fall back to drawing the current plain-text pill."""
    s = (value or "").strip().upper()
    if not s:
        return None
    if element == "resolution":
        table = [("4K", "res_4k"), ("2160P", "res_4k"),
                 ("1080P", "res_1080p"), ("720P", "res_720p"), ("480P", "res_480p")]
        for needle, k in table:
            if needle in s:
                return k
        return None
    if element == "hdr":
        # order matters: most specific first so DV+HDR wins over bare HDR/DV.
        rules = [
            (("DV+HDR",), "dv_hdr"),
            (("DOLBY VISION", "DV"), "dv"),
            (("HDR10+",), "hdr10plus"),
            (("HDR",), "hdr"),
        ]
        for needles, k in rules:
            if any(n in s for n in needles):
                return k
        return None
    if element == "audio":
        rules = [
            (("ATMOS",), "atmos"),
            (("DTS:X", "DTSCX"), "dtsx"),
            (("TRUEHD",), "truehd"),
            (("DTS-HD MA", "DTHDMA"), "dtshd_ma"),
            (("DD+", "E-AC3"), "dd_plus"),
            (("AAC",), "aac"),
            (("DOLBY DIGITAL", "AC3"), "dd"),
        ]
        for needles, k in rules:
            if any(n in s for n in needles):
                return k
        return None
    return None


@dataclass(frozen=True)
class _Placed:
    el_key: str
    kind: str                # "text" | "rating" | "image"
    text: Optional[str]      # rendered label / value string (None when image-only)
    value: Optional[str]     # the item's own value for this element
    logo: Optional[bytes]    # pack image bytes, or None -> fall back to a text pill
    size: int
    fg: tuple[int, int, int, int]
    bg: tuple[int, int, int, int]
    radius: int
    pad_x: int
    pad_y: int
    anchor: str              # one of GRID_POSITIONS
    x_off: int
    y_off: int
    weight: int = 100


def _logo_pack_list(logo_pack: str | Sequence[str]) -> List[str]:
    """Normalise legacy single-name or new list-of-names into an ordered list.

    "none"/empty entries are dropped; order is the priority order (first match wins)."""
    if isinstance(logo_pack, str):
        seq = [logo_pack]
    else:
        seq = list(logo_pack or [])
    return [p for p in seq if p and p != "none"]


def _collect_placed(
    doc: OverlayStyleDoc, values: Mapping[str, Optional[str]],
    config_dir: Optional[Path] = None,
    assets_root: Optional[os.PathLike | str] = None,
    logo_pack: str | Sequence[str] = "none",
    status_key: Optional[str] = None,
    packs_profile: Optional[Mapping[str, Any]] = None,
    k9m: Optional[Mapping[str, Any]] = None,
    filename: str = "",
    rating_value: Optional[float] = None,
    sample_keys: Optional[Set[str]] = None,
) -> List[_Placed]:
    """Turn style document + item values into placed badges in render order.

    ``logo_pack`` selects where pack images come from, in priority order when a
    list is given (first pack with an image wins; "none" disables them;
    "custom" reads flat files under ``logos/`` as before; any other name such as
    "kometa" reads the installed subfolder). Missing images always fall back to
    a plain text pill for that element only. ``status_key`` is the raw
    computed TV-status value (e.g. ``"ended"``, from
    ``engine.tv_status.compute_tv_status``) driving the default "status"
    ribbon colour; the already-formatted label lives in ``values["status"]``.

    ``packs_profile``/``k9m``/``filename``/``rating_value`` mirror
    ``plan_builder._overlay_poster``'s per-profile Kometa-import pack lookup
    (``engine.image_pack_resolve.resolve_profile_pack_image``) for resolution/
    hdr/audio, tried BEFORE the global ``logo_pack`` fallback below -- same
    priority order as apply, so preview never diverges from what a real apply
    would draw."""
    packs = _logo_pack_list(logo_pack)
    out: List[_Placed] = []
    for key in ELEMENT_KEYS:
        el = doc.elements.get(key)
        if el is None or not el.enabled:
            continue

        value = ""
        if key == "ribbon":
            value = (doc.ribbon_text or "").strip()
        elif key in _INFO_KEYS or key in _RIBBON_KEYS:
            value = (values.get(key) or "").strip()
        else:  # rating elements carry their own per-source value
            value = (values.get(f"rating_{key}") or "").strip()

        kind = "text"; logo: Optional[bytes] = None; text: Optional[str] = None
        default_bg: Optional[str] = None
        if key == "ribbon":
            if not value:
                continue
            text = value
        elif key in _RIBBON_KEYS:
            if not value:
                continue  # missing data -> no badge (SPEC §8)
            text = value
            # full_width (default True, preserves prior behaviour): a flush
            # edge-to-edge band, drawn separately below. When off, this falls
            # through to the normal position-anchored text pill instead
            # (padding_x/padding_y/radius/position already apply to that path).
            kind = "band" if el.full_width else "text"
            if key == "status":
                from finmeta.engine.tv_status import STATUS_COLORS
                default_bg = (doc.status_colors or {}).get(status_key or "") or STATUS_COLORS.get(status_key or "")
            else:
                from finmeta.engine.tv_status import TRENDING_RANK_COLOR
                default_bg = TRENDING_RANK_COLOR
        elif key in _INFO_KEYS:
            if not value:
                continue  # missing data -> no badge (SPEC §8)
            kind = "image"
            if packs_profile and config_dir is not None:
                from finmeta.engine.image_pack_resolve import resolve_profile_pack_image
                _imgs = resolve_profile_pack_image(
                    config_dir, packs_profile, key,
                    k9_result=(k9m or {}).get(key), filename=filename,
                    rating_value=rating_value,
                )
                logo = _imgs[0] if _imgs else None
            if logo is None and key in (sample_keys or ()) and packs_profile and config_dir is not None:
                # "Sample data" placeholder (e.g. "HDR10") has no real k9/filename
                # match to resolve against -- pick a representative image from the
                # active pack by name instead, same illustration purpose the legacy
                # logo_pack fallback below already serves for real (non-sample)
                # values. Never reached by apply or by a real, non-sample preview.
                from finmeta.engine.image_pack_resolve import resolve_profile_pack_sample_image
                _imgs = resolve_profile_pack_sample_image(config_dir, packs_profile, key, value)
                logo = _imgs[0] if _imgs else None
            if logo is None and key == "network" and config_dir is not None:
                # Open vocabulary (280+ possible names) -- no flat logo_pack
                # entry exists for this; fetched/cached on demand instead.
                # See engine.network_logos's module docstring.
                from finmeta.engine.network_logos import ensure_network_logo
                logo = ensure_network_logo(config_dir, value)
            pack_key = info_pack_key(key, value)
            if logo is None and pack_key is not None and packs:
                for _pn in packs:
                    logo = load_pack_image_bytes(pack_key, _pn, config_dir=config_dir, assets_root=assets_root)
                    if logo is not None:
                        break
            if logo is None:      # image missing/unmapped -> current text pill fallback
                kind = "text"
                text = value
        else:
            label = _RATING_TEXT_LABELS[key]
            pack_key = rating_pack_key(key, value)
            if pack_key is not None and packs:
                for _pn in packs:
                    logo = load_pack_image_bytes(
                        pack_key, _pn, config_dir=config_dir, assets_root=assets_root)
                    if logo is not None:
                        break
            # Legacy flat layout (<root>/logos/<key>.png), the pre-pack lookup: it keeps
            # working for any pack name so existing user-dropped logos still render.
            if logo is None:
                logo = load_logo_bytes(key, assets_root, config_dir=config_dir)
            if logo is not None:
                kind = "rating"
                if not value:      # badge always carries its score -> nothing to draw without one
                    continue
                text = value
            else:                  # no pack image for this element/value -> current text pill
                text = f"{label} {value}" if value else None
                if text is None:   # neither score nor usable logo -> skip
                    continue

        anchor = el.position or doc.base_position
        out.append(_Placed(
            el_key=key, kind=kind, text=text, value=value, logo=logo,
            size=max(int(el.size or doc.font_size), 8),
            fg=_parse_hex(el.color or ("#FFFFFF" if default_bg else doc.fg)),
            bg=_apply_opacity(_parse_hex(el.back_color or default_bg or doc.bg), el.opacity),
            radius=(el.radius if el.radius is not None else doc.radius),
            pad_x=max(el.padding_x, 4), pad_y=max(el.padding_y, 3),
            anchor=anchor, x_off=int(el.x_offset), y_off=int(el.y_offset),
            weight=el.weight,
        ))
    return out


def _apply_opacity(c: tuple[int, int, int, int], opacity: float):
    r, g, b, a = c
    return (r, g, b, round(a * max(0.0, min(opacity, 1.0))))


@dataclass(frozen=True)
class _SizedBadge:
    placed: _Placed
    w: int
    h: int
    font: ImageFont.ImageFont


def _render_overlay_image(poster_bytes: bytes, style_doc_json: Mapping,
                          values: Optional[Mapping[str, Optional[str]]] = None,
                          config_dir: Optional[Path] = None,
                          assets_root: Optional[os.PathLike | str] = None,
                          logo_pack: str | Sequence[str] = "none",
                          status_key: Optional[str] = None,
                          packs_profile: Optional[Mapping[str, Any]] = None,
                          k9m: Optional[Mapping[str, Any]] = None,
                          filename: str = "",
                          rating_value: Optional[float] = None,
                          sample_keys: Optional[Set[str]] = None) -> "Image.Image":
    """Render ``poster_bytes`` with the posted style document; RGBA PIL image.

    The shared core behind both :func:`render_overlay_png` (editor preview,
    PNG) and :func:`render_overlay_jpeg` (real apply, JPEG) -- same per-element
    layout engine for both, so a style change (offset/radius/colour) affects
    the real written poster exactly as it does the preview (previously the
    apply pipeline used a separate, coarser renderer -- ``engine.overlay`` --
    that only understood one shared margin/radius for the whole badge stack
    and ignored per-element offset/radius entirely).

    Deterministic in its inputs and never writes to disk. Missing item values
    simply omit their badges ("missing data → no badge", SPEC §8).

    ``packs_profile``/``k9m``/``filename``/``rating_value``/``sample_keys`` are
    passed straight through to ``_collect_placed`` -- see its docstring.
    """
    doc = OverlayStyleDoc(**(dict(style_doc_json or {})))
    base = Image.open(io.BytesIO(poster_bytes)).convert("RGBA")
    pw, ph = base.size
    work = base.copy()

    fonts: Dict[int, object] = {}

    def font(size: int) -> ImageFont.ImageFont:
        f = fonts.get(size)
        if f is None:
            f = ImageFont.load_default(size=max(int(size), 8))
            fonts[size] = f
        return f  # type: ignore[return-value]

    placed = _collect_placed(doc, dict(values or {}), config_dir, assets_root,
                          logo_pack=(logo_pack or "none"), status_key=status_key,
                          packs_profile=packs_profile, k9m=k9m, filename=filename,
                          rating_value=rating_value, sample_keys=sample_keys)

    # Scale every pixel-based field to this poster's actual width (see
    # _poster_scale) so a style tuned at _REFERENCE_WIDTH looks the same
    # proportionally on any source resolution, instead of a fixed pixel size
    # that reads tiny on a large poster and oversized on a small one.
    poster_scale = _poster_scale(pw)
    placed = [_scale_placed(p, poster_scale) for p in placed]

    # S9g1/K7a: "status"/"trending_rank" are full-width ribbon bands, not
    # part of the normal corner-grouped badge stack (see _draw_band below).
    band_placed = [p for p in placed if p.kind == "band"]
    placed = [p for p in placed if p.kind != "band"]

    groups: "Dict[Tuple[str, str], List[_Placed]]" = {}
    for p in sorted(placed, key=lambda q: (q.anchor, ELEMENT_KEYS.index(q.el_key), q.weight)):
        # NOTE: _normalize_anchor returns (row, col); keep that order everywhere below.
        row, col = _normalize_anchor(p.anchor)
        if col is None or row is None:
            continue      # unparseable anchor from a hand-edited style -> skip this element only
        groups.setdefault((col, row), []).append(p)

    margin = max(round(doc.margin * poster_scale), 0)
    gap = lambda size: max(size // 6, 4)

    for (col, row), items in groups.items():
        sized: List[_SizedBadge] = []
        for it in items:
            fnt = font(it.size)
            w, h = _badge_size(it, fnt)
            sized.append(_SizedBadge(placed=it, w=w, h=h, font=fnt))
        g_x = sum(gap(s.placed.size) for s in sized[:-1]) if len(sized) > 1 else 0
        total_w = sum(s.w for s in sized) + g_x
        total_h = sum(s.h + gap(s.placed.size) for s in sized) - (gap(sized[-1].placed.size) if sized else 0)

        # Origin of the *first* badge's top-left corner.
        first = sized[0]
        ox = x_origin(col, margin, pw, total_w)
        oy = y_origin(row, margin, ph, total_h)
        cursor_y = oy
        for sb in sized:
            x = int(round(ox + sb.placed.x_off))
            y = int(round(cursor_y + sb.placed.y_off))
            _draw_badge(work, sb.placed, sb.font, sb.w, sb.h, x, y)
            cursor_y += sb.h + gap(sb.placed.size)

    if band_placed:
        band_placed = sorted(band_placed, key=lambda q: ELEMENT_KEYS.index(q.el_key))
        band_pad_y = max(round(_BAND_PAD_Y * poster_scale), 1)
        band_gap = max(round(_BAND_GAP * poster_scale), 0)
        band_side_pad = max(round(_BAND_SIDE_PAD * poster_scale), 4)
        max_text_w = max(pw - 2 * band_side_pad, 10)
        # Operator-reported (2026-10-26): a band's font_size was applied raw --
        # raising it enough on a long label (e.g. "SEASON FINALE SATURDAY")
        # made the text wider than the poster itself, running off both edges.
        # _fit_band_font shrinks that one element's font just enough to fit,
        # instead of ever drawing text wider than the poster.
        fonts_fitted = [_fit_band_font(font, p.size, p.text or "", max_text_w) for p in band_placed]
        heights = []
        for p, fnt in zip(band_placed, fonts_fitted):
            tb = fnt.getbbox(p.text or "")
            heights.append((tb[3] - tb[1]) + band_pad_y)
        row0, _col0 = _normalize_anchor(band_placed[0].anchor)
        top = row0 == "top"
        y = 0 if top else ph
        for p, h, fnt in zip(band_placed, heights, fonts_fitted):
            if not top:
                y -= h
            # p.y_off nudges this band away from the edge it's flush with
            # (down-positive, same convention as the normal badge stack
            # above) -- the stacking cursor itself (y) is unaffected, so
            # multiple bands keep their relative order regardless of offset.
            _draw_band(work, p.text or "", fnt, y + p.y_off, h, pw, p.bg, p.fg)
            y += (h + band_gap) if top else -band_gap

    return work


def render_overlay_png(poster_bytes: bytes, style_doc_json: Mapping,
                       values: Optional[Mapping[str, Optional[str]]] = None,
                       config_dir: Optional[Path] = None,
                       assets_root: Optional[os.PathLike | str] = None,
                       logo_pack: str | Sequence[str] = "none",
                       status_key: Optional[str] = None,
                       packs_profile: Optional[Mapping[str, Any]] = None,
                       k9m: Optional[Mapping[str, Any]] = None,
                       filename: str = "",
                       rating_value: Optional[float] = None,
                       sample_keys: Optional[Set[str]] = None) -> bytes:
    """Render ``poster_bytes`` with the posted style document; PNG output.

    Thin encoding wrapper around :func:`_render_overlay_image` -- see its
    docstring for the shared layout engine this and ``render_overlay_jpeg``
    both use. ``sample_keys`` (preview's "Sample data" toggle only --
    ``render_overlay_jpeg``/a real apply never has this) names which
    elements carry a synthetic placeholder value with no real detection to
    back it; those get a representative pack image by name instead of
    silently falling to plain text, see ``image_pack_resolve.
    resolve_profile_pack_sample_image``.
    """
    work = _render_overlay_image(
        poster_bytes, style_doc_json, values=values, config_dir=config_dir,
        assets_root=assets_root, logo_pack=logo_pack, status_key=status_key,
        packs_profile=packs_profile, k9m=k9m, filename=filename, rating_value=rating_value,
        sample_keys=sample_keys)
    buf = io.BytesIO()
    work.convert("RGB").save(buf, format="PNG")
    return buf.getvalue()


def render_overlay_jpeg(poster_bytes: bytes, style_doc_json: Mapping,
                        values: Optional[Mapping[str, Optional[str]]] = None,
                        config_dir: Optional[Path] = None,
                        assets_root: Optional[os.PathLike | str] = None,
                        logo_pack: str | Sequence[str] = "none",
                        status_key: Optional[str] = None,
                        packs_profile: Optional[Mapping[str, Any]] = None,
                        k9m: Optional[Mapping[str, Any]] = None,
                        filename: str = "",
                        rating_value: Optional[float] = None,
                        quality: int = 92) -> bytes:
    """Render ``poster_bytes`` with the posted style document; JPEG output.

    Used by the real apply pipeline (``engine.plan_builder._overlay_poster``)
    so a saved/uploaded poster gets the exact same per-element layout as the
    editor preview -- see :func:`_render_overlay_image`. Same JPEG quality as
    the legacy ``engine.overlay.render_poster`` it replaces, so output size
    and determinism are unchanged for callers/tests that only check format.
    """
    work = _render_overlay_image(
        poster_bytes, style_doc_json, values=values, config_dir=config_dir,
        assets_root=assets_root, logo_pack=logo_pack, status_key=status_key,
        packs_profile=packs_profile, k9m=k9m, filename=filename, rating_value=rating_value)
    buf = io.BytesIO()
    work.convert("RGB").save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def x_origin(col: str, margin: int, width: int, block_width: int) -> float:
    """Top-left X for a horizontal position column."""
    if col == "left":
        return float(margin)
    if col == "right":
        return max(float(width - margin - block_width), 0.0)
    return max((width - block_width) / 2.0, float(margin))


def y_origin(row: str, margin: int, height: int, block_height: int) -> float:
    """Top Y for a vertical position row (stacks grow downward from here)."""
    if row == "top":
        return float(margin)
    if row == "bottom":
        return max(float(height - margin - block_height), 0.0)
    # center: whole block vertically centred inside the poster.
    return max((float(height) - block_height) / 2.0, float(margin))


def _logo_scaled_width(logo_bytes: bytes, height: int) -> int:
    """Width of ``logo_bytes`` scaled to exactly ``height`` px tall."""
    im = Image.open(io.BytesIO(logo_bytes))
    return max(1, round(im.width * height / max(im.height, 1)))


def _badge_size(p: _Placed, font) -> Tuple[int, int]:
    if p.kind == "image" and p.logo is not None:
        # image-only badge: pack image scaled to the element size, no text.
        target_h = max(int(p.size), 16)
        new_w = _logo_scaled_width(p.logo, target_h)
        return (new_w + 2 * p.pad_x, target_h + 2 * p.pad_y)
    if p.kind == "rating":
        assert p.logo is not None      # invariant set by _collect_placed
        logo_h = max(int(p.size), 16)
        gap = max(int(p.size * 0.4), 8)   # spacing between logo and value text
        tb = font.getbbox(p.text or "")
        tw, th = tb[2] - tb[0], tb[3] - tb[1]
        w_img = _logo_scaled_width(p.logo, logo_h)
        h_content = max(logo_h, th)
        total_w = w_img + gap + tw
        return (total_w + 2 * p.pad_x, h_content + 2 * p.pad_y)
    # plain text pill ("text" kind, including all fallbacks)
    tb = font.getbbox(p.text or "")
    tw, th = tb[2] - tb[0], tb[3] - tb[1]
    return (tw + 2 * p.pad_x, th + 2 * p.pad_y)


def _fit_band_font(font_loader, size: int, text: str, max_width: int) -> ImageFont.ImageFont:
    """Largest font no bigger than ``size`` whose rendering of ``text`` fits
    within ``max_width`` px, down to the same 8px floor the rest of the
    engine uses (see ``_scale_placed``). Raising a band's configured size
    beyond what the poster can hold now shrinks the text back down instead
    of letting it run off the poster's edges."""
    size = max(int(size), 8)
    fnt = font_loader(size)
    if not text:
        return fnt
    while size > 8:
        tb = fnt.getbbox(text)
        if (tb[2] - tb[0]) <= max_width:
            return fnt
        size -= 1
        fnt = font_loader(size)
    return fnt


def _draw_band(work: Image.Image, text: str, font, y: int, height: int,
              width: int, bg: tuple[int, int, int, int],
              fg: tuple[int, int, int, int]) -> None:
    """Draw one full-width ribbon band (S9g1/K7a), flush with the poster edge
    -- unlike a normal badge, this ignores radius/padding/left-right anchor."""
    ov = Image.new("RGBA", work.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    d.rectangle([0, y, width, y + height], fill=bg)
    tb = font.getbbox(text)
    text_w = tb[2] - tb[0]
    text_h = tb[3] - tb[1]
    tx = max(0, (width - text_w) // 2)
    ty = y + (height - text_h) // 2
    d.text((tx - tb[0], ty - tb[1]), text, font=font, fill=fg)
    work.alpha_composite(ov)


def _draw_badge(work: Image.Image, p: _Placed, font, w: int, h: int,
                x: int, y: int) -> None:
    """Draw one badge onto ``work`` at ``(x, y)`` with size ``(w, h)``.

    Two image-backed kinds share ONE rounded backdrop per element: rating badges
    put the pack logo on the left and the value text beside it inside that same
    box; image badges draw only the scaled pack image (no text). Everything else
    falls through to the plain-text-pill path."""
    ov = Image.new("RGBA", work.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    d.rounded_rectangle([x, y, x + w, y + h], radius=p.radius, fill=p.bg)
    if p.logo is not None:
        im = Image.open(io.BytesIO(p.logo)).convert("RGBA")
        target_h = max(int(p.size), 16)
        scale = target_h / max(im.height, 1)
        new_w = max(1, round(im.width * scale))
        im = im.resize((new_w, target_h))
        ox = x + p.pad_x
        oy = y + ((h - target_h) // 2)
        ov.paste(im, (ox, oy), mask=im)
        text_origin_x = ox + new_w
        gap_after_logo = True
    else:
        text_origin_x = x + p.pad_x
        gap_after_logo = False
    text = p.text or ""
    if text and p.kind != "image":   # image badges never draw their value as text
        tb = font.getbbox(text)
        th = tb[3] - tb[1]
        tx = text_origin_x - tb[0]
        ty = y + ((h - th) // 2) - tb[1]
        if gap_after_logo:           # rating badge: space between logo and value
            tx += max(int(p.size * 0.4), 8)
        d.text((tx, ty), text, font=font, fill=p.fg)
    work.alpha_composite(ov)


__all__ = [
    "render_overlay_png", "render_overlay_jpeg", "load_logo_bytes", "logo_path",
    "load_pack_image_bytes", "rating_pack_key", "info_pack_key",
]


