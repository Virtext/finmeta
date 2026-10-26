"""The real apply renderer now consumes the full per-element style document
(S9-overlay-unify, 2026-10-25): operator-reported "TV status y-offset/radius
do nothing" traced to engine.overlay (apply) only ever understanding one
shared margin/radius/position for the whole badge stack, while the editor
preview (engine.overlay_render) already supported per-element offset/radius
and never carried through to a real written poster. engine.plan_builder.
_overlay_poster now renders via engine.overlay_render.render_overlay_jpeg
(plan_builder._apply_style_doc builds its style document), so these tests
exercise that renderer directly plus the apply-level wiring.
"""
from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from PIL import Image

from finmeta.engine.badges import overlay_values
from finmeta.engine.overlay_render import render_overlay_png
from finmeta.engine.plan_builder import _overlay_poster
from finmeta.engine.style_presets import OverlayStyleDoc
from finmeta.sources.candidate import Candidate

POSTER_W, POSTER_H = 200, 300


def _poster() -> bytes:
    img = Image.new("RGB", (POSTER_W, POSTER_H), (10, 10, 10))
    buf = io.BytesIO(); img.save(buf, format="JPEG")
    return buf.getvalue()


def _poster_sized(w: int, h: int) -> bytes:
    img = Image.new("RGB", (w, h), (10, 10, 10))
    buf = io.BytesIO(); img.save(buf, format="JPEG")
    return buf.getvalue()


def _band_top_row(png_bytes: bytes, bg_rgb: tuple[int, int, int]) -> int:
    """First row (from the top) whose x=5 pixel matches `bg_rgb` (the band)."""
    im = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    for y in range(im.height):
        if im.getpixel((5, y)) == bg_rgb:
            return y
    raise AssertionError(f"band colour {bg_rgb} not found in rendered image")


def _status_doc(status_colors=None, y_offset=0) -> dict:
    doc = {
        "base_position": "top_left", "margin": 20, "font_size": 32,
        "elements": {"status": {"enabled": True, "y_offset": y_offset}},
    }
    if status_colors:
        doc["status_colors"] = status_colors
    return doc


def test_status_band_y_offset_shifts_the_band_down():
    """A y_offset on the 'status' element nudges the full-width band away from
    the poster edge it's otherwise flush with (previously ignored entirely).

    Uses a reference-width poster (1000x1500, see
    overlay_render._REFERENCE_WIDTH) rather than the module's 200x300
    POSTER_W/H -- offsets are pixels-at-reference-width (SPEC_KOMETA.md,
    2026-10-25 poster-scaling fix) and now scale with the poster's actual
    width, so an exact-pixel offset assertion only holds at that width.
    """
    ref_poster_bytes = _poster_sized(1000, 1500)
    values = {"status": "ENDED"}
    # Fully-opaque override (6-digit hex) so the band colour matches exactly
    # regardless of alpha-blending against the poster underneath -- the
    # built-in STATUS_COLORS default carries an alpha channel.
    colors = {"ended": "#445566"}
    flush = render_overlay_png(ref_poster_bytes, _status_doc(colors), values=values, status_key="ended")
    offset = render_overlay_png(ref_poster_bytes, _status_doc(colors, y_offset=30), values=values, status_key="ended")
    bg = _hex_to_rgb(colors["ended"])
    row_flush = _band_top_row(flush, bg)
    row_offset = _band_top_row(offset, bg)
    assert row_flush == 0, row_flush
    assert row_offset == 30, row_offset


def test_status_colors_override_changes_the_band_background():
    """overlays.style.status_colors fine-tunes one status's ribbon colour,
    overriding the hard-coded engine.tv_status.STATUS_COLORS default."""
    values = {"status": "ENDED"}
    custom = _hex_to_rgb("#112233")  # 6-digit hex -> opaque; unique, not the default grey
    png = render_overlay_png(
        _poster(), _status_doc(status_colors={"ended": "#112233"}),
        values=values, status_key="ended")
    assert _band_top_row(png, custom) == 0


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    v = value.lstrip("#")
    return (int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16))


class _FakeJF:
    def get_items(self, parent_id=None, recursive=False):
        return []

    def get_item_media_streams_raw(self, item_id):
        return []


def test_apply_poster_changes_when_style_doc_changes():
    """engine.plan_builder._overlay_poster's rendered bytes actually depend on
    the style document now (previously `style` only drove the coarse shared
    margin/radius and per-element offset/radius/colour were silently dropped
    on the real apply path -- this is the regression guard for that fix)."""
    item = SimpleNamespace(id="item-1", name="Some Show", type="Series", path="/x/Some Show")
    cand = Candidate(kind="poster", source="local", ref="/nonexistent-poster.bin",
                     lang=None, width=POSTER_W, height=POSTER_H, score=0.9)
    jf = _FakeJF()

    import finmeta.engine.plan_builder as pb
    orig = pb._candidate_bytes
    pb._candidate_bytes = lambda ref, timeout=30.0: _poster()
    try:
        fw_a = _overlay_poster(
            item, cand, ["status"], Path("/tmp/finmeta-test-rich-style/poster.jpg"), jf,
            None, "tvshow", None, _status_doc(y_offset=0),
            None, streams=[], status_label="Ended", status_key="ended")
        fw_b = _overlay_poster(
            item, cand, ["status"], Path("/tmp/finmeta-test-rich-style/poster.jpg"), jf,
            None, "tvshow", None, _status_doc(y_offset=60),
            None, streams=[], status_label="Ended", status_key="ended")
    finally:
        pb._candidate_bytes = orig
    assert fw_a is not None and fw_b is not None
    assert fw_a.content_sha256 != fw_b.content_sha256


def test_status_colors_rejects_unknown_status_key():
    with pytest.raises(ValidationError, match="unknown TV status"):
        OverlayStyleDoc(status_colors={"not_a_real_status": "#112233"})


def test_status_colors_rejects_non_hex_value():
    with pytest.raises(ValidationError, match="hex"):
        OverlayStyleDoc(status_colors={"ended": "not-a-colour"})


def test_status_colors_accepts_a_known_status():
    doc = OverlayStyleDoc(status_colors={"ended": "#112233"})
    assert doc.status_colors == {"ended": "#112233"}
