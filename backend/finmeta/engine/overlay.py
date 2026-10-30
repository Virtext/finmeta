"""Overlay compositor (SPEC \u00a78, overrides O7).

Renders the SPEC v1 overlay subset onto a poster with Pillow: text badges,
image badges, corner position, margin and colour. Output is JPEG quality 92
at the original poster size.

Determinism: the same input poster bytes + badges + style always produce the
same sha256. Pillow\u2019s built-in font and JPEG encoder are byte-stable, and no
timestamps or random seeds are introduced, so repeated renders are identical.

O7: ``render_poster`` always works from the bytes it is given (the *original*
poster, or the backed-up original when a run already overwrote it), so
re-running an apply never stacks badges on top of previous badges.
"""
from __future__ import annotations

import hashlib
import io
import time
from pathlib import Path
from typing import Callable, List, Mapping, Optional, Sequence, Union

from PIL import Image, ImageDraw, ImageFont
from pydantic import BaseModel, ConfigDict

from finmeta.config.models import OverlayStyle
from finmeta.engine.plan import FileWrite, WritePlan
from finmeta.engine.runs import find_original_backup

JPEG_QUALITY = 92
# Layout constants (px).
_BADGE_GAP = 8       # vertical gap between stacked badges
_TEXT_PAD_X = 10     # horizontal padding inside a text badge
_TEXT_PAD_Y = 6      # vertical padding inside a text badge
_IMG_PAD_X = 8       # horizontal padding inside an image badge
_IMG_PAD_Y = 6       # vertical padding inside an image badge

_CORNERS = {"top_left", "top_right", "bottom_left", "bottom_right"}
_BAND_PAD_Y = 10     # extra height (beyond raw text height) for a full-width ribbon band
_BAND_GAP = 2        # vertical gap between stacked full-width bands


class TextBadge(BaseModel):
    """A text overlay badge, e.g. ``1080p`` or ``\u2605 7.8``.

    ``bg``/``fg`` (S9g1/K7a ribbon colours) override the shared ``OverlayStyle``
    backdrop/text colour for just this badge, e.g. the per-status ribbon
    colours in ``engine.tv_status.STATUS_COLORS``. ``None`` (the default)
    means "use the style's own bg/fg", unchanged from before this field
    existed.
    """

    model_config = ConfigDict(frozen=True)

    kind: str = "text"
    text: str
    bg: Optional[str] = None
    fg: Optional[str] = None
    full_width: bool = False


class ImageBadge(BaseModel):
    """An image overlay badge (PNG/JPEG bytes)."""

    model_config = ConfigDict(frozen=True)

    kind: str = "image"
    data: bytes


Badge = Union[TextBadge, ImageBadge]


def sha256(data: bytes) -> str:
    """Hex SHA-256 of ``data``."""
    return hashlib.sha256(data).hexdigest()


def _parse_color(value: str) -> tuple[int, int, int, int]:
    """Parse ``#RRGGBB`` or ``#RRGGBBAA`` into an (r, g, b, a) tuple."""
    v = value.strip().lstrip("#")
    if len(v) == 6:
        r, g, b = int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16)
        return (r, g, b, 255)
    if len(v) == 8:
        r, g, b = int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16)
        a = int(v[6:8], 16)
        return (r, g, b, a)
    raise ValueError(f"unsupported colour {value!r}; use #RRGGBB or #RRGGBBAA")


def _badge_size(
    badge: Badge, font: ImageFont.ImageFont, style: "OverlayStyle",
) -> tuple[int, int]:
    """Return (width, height) of a single badge including padding.

    For an ImageBadge this MUST match the scaled size ``_draw_badge`` will
    actually render at (target_h = style.font_size, same aspect-preserving
    scale + padding) -- not the source PNG's native size. Using the native
    size here (the bug this fixes) under-counts the stack's total height
    whenever a logo's native height differs from font_size (true for every
    real pack logo, e.g. a 38px-tall resolution badge scaled up to a
    120px font_size), so _positions() starts the stack in the wrong place
    and later badges overlap/drift far from earlier ones -- found via a
    real Kometa-pack apply once S9d4c's logo-pack-fallback fix started
    actually producing ImageBadges (2026-10-18).
    """
    if isinstance(badge, TextBadge):
        tb = font.getbbox(badge.text)
        tw, th = tb[2] - tb[0], tb[3] - tb[1]
        return (tw + 2 * _TEXT_PAD_X, th + 2 * _TEXT_PAD_Y)
    im = Image.open(io.BytesIO(badge.data))
    target_h = max(style.font_size, 16)
    scale = target_h / im.height
    new_w = max(1, round(im.width * scale))
    return (new_w + 2 * _IMG_PAD_X, target_h + 2 * _IMG_PAD_Y)


def _draw_badge(
    work: Image.Image, badge: Badge, font: ImageFont.ImageFont,
    style: OverlayStyle, x: int, y: int,
    bg: tuple[int, int, int, int], fg: tuple[int, int, int, int],
) -> None:
    """Composite one badge onto the full-size RGBA ``work`` image."""
    ov = Image.new("RGBA", work.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    if isinstance(badge, TextBadge):
        w, h = _badge_size(badge, font, style)
        d.rounded_rectangle([x, y, x + w, y + h], radius=style.radius, fill=bg)
        tb = font.getbbox(badge.text)
        d.text((x + _TEXT_PAD_X - tb[0], y + _TEXT_PAD_Y - tb[1]),
               badge.text, font=font, fill=fg)
    else:
        im = Image.open(io.BytesIO(badge.data))
        # Scale the badge to the configured font height (its visual height).
        target_h = max(style.font_size, 16)
        scale = target_h / im.height
        new_w = max(1, round(im.width * scale))
        new_h = max(1, target_h)
        im = im.resize((new_w, new_h))
        w, h = new_w + 2 * _IMG_PAD_X, new_h + 2 * _IMG_PAD_Y
        d.rounded_rectangle([x, y, x + w, y + h], radius=style.radius, fill=bg)
        if im.mode != "RGBA":
            im = im.convert("RGBA")
        ov.paste(im, (x + _IMG_PAD_X, y + _IMG_PAD_Y))
    work.alpha_composite(ov)


def _draw_band(
    work: Image.Image, text: str, font: ImageFont.ImageFont,
    y: int, height: int, width: int,
    bg: tuple[int, int, int, int], fg: tuple[int, int, int, int],
) -> None:
    """Draw one full-width ribbon band (S9g1/K7a: "ribbon ... e.g. NEW SEASON
    12.11") spanning the whole poster width, flush with the top/bottom edge —
    unlike a normal badge, this ignores the style's corner/margin/radius."""
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


def _positions(
    sizes: Sequence[tuple[int, int]], style: OverlayStyle,
    width: int, height: int,
) -> List[tuple[int, int]]:
    """Top-left corner for each badge in the configured corner, in order."""
    margin = style.margin
    pos = style.position
    if pos not in _CORNERS:
        raise ValueError(f"unsupported overlay position {pos!r}")
    right = pos in ("top_right", "bottom_right")
    bottom = pos in ("bottom_left", "bottom_right")

    if bottom:
        total = sum(h for _, h in sizes) + _BADGE_GAP * (len(sizes) - 1)
        y_cursor = height - margin - total
    else:
        y_cursor = margin

    result: List[tuple[int, int]] = []
    for w, h in sizes:
        x = (width - margin - w) if right else margin
        result.append((x, y_cursor))
        y_cursor += h + _BADGE_GAP
    return result


def render_poster(
    poster: bytes, badges: Sequence[Badge], style: OverlayStyle,
) -> bytes:
    """Render ``badges`` onto ``poster`` and return deterministic JPEG bytes.

    The returned image is the same size as the original poster. With no
    badges it returns the poster re-encoded to JPEG quality 92.
    """
    base = Image.open(io.BytesIO(poster))
    base = base.convert("RGBA")
    font = ImageFont.load_default(size=max(style.font_size, 8))
    bg = _parse_color(style.bg)
    fg = _parse_color(style.fg)

    # S9g1/K7a: "status"/"trending_rank" render as full-width ribbon bands
    # (TextBadge.full_width) instead of joining the normal corner stack —
    # pulled out and drawn separately, flush with the top/bottom edge.
    band_badges = [b for b in badges if isinstance(b, TextBadge) and b.full_width]
    stack_badges = [b for b in badges if not (isinstance(b, TextBadge) and b.full_width)]

    if stack_badges:
        sizes = [_badge_size(b, font, style) for b in stack_badges]
        corners = _positions(sizes, style, base.width, base.height)
        for badge, (x, y) in zip(stack_badges, corners):
            b_bg = _parse_color(badge.bg) if isinstance(badge, TextBadge) and badge.bg else bg
            b_fg = _parse_color(badge.fg) if isinstance(badge, TextBadge) and badge.fg else fg
            _draw_badge(base, badge, font, style, x, y, b_bg, b_fg)

    if band_badges:
        heights = []
        for b in band_badges:
            tb = font.getbbox(b.text)
            heights.append((tb[3] - tb[1]) + _BAND_PAD_Y)
        top = style.position.startswith("top")
        y = 0 if top else base.height
        for badge, h in zip(band_badges, heights):
            if not top:
                y -= h
            b_bg = _parse_color(badge.bg) if badge.bg else bg
            b_fg = _parse_color(badge.fg) if badge.fg else fg
            _draw_band(base, badge.text, font, y, h, base.width, b_bg, b_fg)
            y += (h + _BAND_GAP) if top else -_BAND_GAP

    final = base.convert("RGB")
    buf = io.BytesIO()
    final.save(buf, format="JPEG", quality=JPEG_QUALITY)
    return buf.getvalue()


# Element names that render as a full-width ribbon band (see _draw_band)
# instead of joining the normal corner-stacked badges.
FULL_WIDTH_ELEMENTS = {"status", "trending_rank"}


def ribbon_colors(name: str) -> tuple[Optional[str], Optional[str]]:
    """``(bg, fg)`` ribbon colour override for an element, or ``(None, None)``.

    S9g1/K7a ("text or ribbon with date"): ``status`` gets its own colour per
    status value (engine.tv_status.STATUS_COLORS) and ``trending_rank`` gets
    a single gold colour -- everything else keeps the shared style bg/fg.
    """
    if name == "trending_rank":
        from finmeta.engine.tv_status import TRENDING_RANK_COLOR
        return TRENDING_RANK_COLOR, "#FFFFFF"
    return None, None


def status_ribbon_colors(status_key: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """``(bg, fg)`` for a specific computed status value (e.g. ``"ended"``)."""
    if not status_key:
        return None, None
    from finmeta.engine.tv_status import STATUS_COLORS
    color = STATUS_COLORS.get(status_key)
    return (color, "#FFFFFF") if color else (None, None)


def resolve_badges(
    names: Sequence[str], values: Mapping[str, Optional[str]],
    status_key: Optional[str] = None,
) -> List[Badge]:
    """Build the ordered list of badges for the configured overlay names.

    Badges are emitted in ``names`` order; any name whose value is ``None``
    or missing is skipped (SPEC \u00a78: missing data \u2192 no badge). ``status``
    and ``trending_rank`` get their own ribbon colour (``status_key`` is the
    raw status value, e.g. ``"ended"``, driving which colour -- the
    already-formatted label in ``values["status"]`` isn't enough on its own).
    """
    out: List[Badge] = []
    for name in names:
        value = values.get(name)
        if value:
            bg, fg = status_ribbon_colors(status_key) if name == "status" else ribbon_colors(name)
            out.append(TextBadge(text=value, bg=bg, fg=fg, full_width=name in FULL_WIDTH_ELEMENTS))
    return out


def _overlay_cache_path(digest: str, config_dir: Path) -> Path:
    return Path(config_dir) / "cache" / "overlay" / f"{digest}.jpg"


def read_overlay_cache(digest: str, *, config_dir: Optional[Path]) -> Optional[bytes]:
    """Previously-rendered overlay bytes for ``digest`` (its own sha256), or
    ``None`` on a miss. The executor's ``_fetch_content`` uses this so an
    applied overlaid poster is the SAME bytes the plan hashed -- see the
    "plan-build-time render is thrown away" note on ``overlay_file_write``.
    """
    if config_dir is None:
        return None
    p = _overlay_cache_path(digest, Path(config_dir))
    if p.is_file():
        return p.read_bytes()
    return None


def _write_overlay_cache(digest: str, data: bytes, *, config_dir: Path) -> None:
    p = _overlay_cache_path(digest, Path(config_dir))
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


def overlay_file_write(
    *,
    target_path: Path,
    candidate_bytes: bytes,
    candidate_ref: str,
    badges: Sequence[Badge] = (),
    style: Optional[OverlayStyle] = None,
    render_fn: Optional[Callable[[bytes], bytes]] = None,
    config_dir: Optional[Path] = None,
    is_override: bool = False,
) -> FileWrite:
    """Produce the overlaid-poster ``FileWrite`` for an item (SPEC \u00a78, O7).

    ``render_fn`` (preferred): called with the resolved *original* poster
    bytes (after the O7 backup/override lookup below) to produce the
    overlaid bytes -- used by ``plan_builder._overlay_poster`` to render via
    ``engine.overlay_render``'s full per-element style document (offset,
    radius, per-element colour) instead of this module's own simpler
    ``badges``/``style`` renderer. ``badges``/``style`` stay supported (used
    directly by older tests and any caller that only needs the coarse
    shared-margin/radius engine) when ``render_fn`` is omitted.

    The rendered poster is always built from the *original* poster, so
    re-runs never stack badges (O7): that original is the oldest backup of
    ``target_path`` when a prior run saved one (via ``find_original_backup``),
    otherwise the freshly resolved candidate.

    ``is_override`` (fixed 2026-10-20, operator-reported "picking a new
    poster candidate does nothing"): the stored backup/cache is the
    pre-Finmeta original of whichever candidate was selected BACK THEN --
    reusing it for a manual candidate override silently re-composited
    badges onto the OLD image forever, discarding every newly picked
    candidate (confirmed live: content_sha256 stayed byte-identical after
    overriding poster to a different TMDb image). When the caller just
    folded in a manual override this run, the freshly fetched
    ``candidate_bytes`` themselves ARE the correct original to composite
    onto -- skip the backup/cache lookup entirely.

    The returned ``FileWrite`` points ``source_ref`` at that original
    (never at the overlaid bytes -- O7 needs the original for the *next*
    re-render) and carries the SHA-256 of the rendered overlaid bytes.

    The overlaid bytes themselves are cached on disk by that SHA-256 (when
    ``config_dir`` is given) so the executor can retrieve the EXACT bytes the
    plan hashed at apply time via ``read_overlay_cache`` -- without this, the
    executor would re-fetch ``source_ref`` (the plain, un-overlaid original),
    which never matches ``content_sha256`` and the write would be refused as
    a hash mismatch every time (the bug this cache fixes).
    """
    original_bytes = candidate_bytes
    original_ref = candidate_ref
    if is_override:
        pass   # freshly fetched candidate_bytes already ARE the original
    elif config_dir is not None:
        bk = find_original_backup(Path(target_path), config_dir=config_dir)
        if bk is not None:
            original_bytes = bk.read_bytes()
            original_ref = str(bk)

    if render_fn is not None:
        overlaid = render_fn(original_bytes)
    else:
        assert style is not None, "overlay_file_write needs `style` when `render_fn` is omitted"
        overlaid = render_poster(original_bytes, badges, style)
    digest = sha256(overlaid)
    if config_dir is not None:
        _write_overlay_cache(digest, overlaid, config_dir=config_dir)
    return FileWrite(
        target_path=str(target_path),
        kind="poster",
        source_ref=original_ref,
        content_sha256=digest,
        # S9d5: the CLEAN (non-overlaid) base this was rendered from -- what
        # engine.executor locks on first successful apply so later runs never
        # re-query sources for this item+kind (see OverrideStore.set_lock).
        meta={"lock_ref": original_ref, "lock_sha256": sha256(original_bytes)},
    )


# Never prune a cache file younger than this, even if no on-disk plan
# references it: build_plan() writes the cache entry *during* plan
# construction (inside overlay_file_write), but the plan's own JSON file
# only lands on disk once the whole plan finishes building and the caller
# (cli/run_service) saves it -- without this grace window, a prune running
# concurrently with a slow plan build could delete an entry moments before
# its plan file appears, which this plan's later apply needs intact.
_CACHE_PRUNE_GRACE_SECONDS = 3600


def _live_overlay_digests(config_dir: Path) -> set[str]:
    """content_sha256 of every poster ``FileWrite`` in a plan still on disk.

    A plan written by ``finmeta plan`` may not be applied for a while (the
    operator reviews the dry-run diff first), so any overlay cache entry it
    references must survive until that apply happens -- see
    ``prune_overlay_cache``. A plan file that fails to parse (e.g. mid-write,
    or from an incompatible older version) is skipped rather than treated as
    an error: pruning is best-effort cleanup, never something that should
    block on a malformed leftover.
    """
    runs_dir = Path(config_dir) / "runs"
    digests: set[str] = set()
    if not runs_dir.is_dir():
        return digests
    for p in runs_dir.glob("plan-*.json"):
        try:
            plan = WritePlan.from_json(p.read_text())
        except Exception:
            continue
        for item in plan.items:
            for fw in item.files:
                if fw.kind == "poster":
                    digests.add(fw.content_sha256)
    return digests


def prune_overlay_cache(config_dir: Path) -> int:
    """Delete cached overlay renders no longer referenced by any plan on disk.

    ``read_overlay_cache`` is the only reader, and only ever looks up a
    digest that this run's OWN plan already carries (a miss there is a hard
    apply failure -- see ``_fetch_content``'s hash-mismatch check) -- so an
    entry is safe to delete once no plan file under ``runs/`` still
    references its digest, modulo ``_CACHE_PRUNE_GRACE_SECONDS``. Left
    unpruned, this directory grows forever: ``_write_overlay_cache`` writes
    one file per distinct rendered output and nothing ever deleted from it
    before this function existed.

    ``render_poster`` is a pure function of its inputs, so wrongly pruning a
    live entry is not a correctness bug, just a wasted re-render -- the next
    thing that needs it re-renders and re-caches on the spot. Best-effort
    per file, like ``trash.sweep_to_trash``: one failure is skipped, never
    aborts the rest. Returns the number of files deleted.
    """
    cache_dir = Path(config_dir) / "cache" / "overlay"
    if not cache_dir.is_dir():
        return 0
    live = _live_overlay_digests(config_dir)
    cutoff = time.time() - _CACHE_PRUNE_GRACE_SECONDS
    deleted = 0
    for f in cache_dir.glob("*.jpg"):
        if f.stem in live:
            continue
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
                deleted += 1
        except OSError:
            continue
    return deleted


__all__ = [
    "TextBadge", "ImageBadge", "Badge", "render_poster", "sha256",
    "resolve_badges", "overlay_file_write", "read_overlay_cache",
    "prune_overlay_cache", "JPEG_QUALITY",
]
