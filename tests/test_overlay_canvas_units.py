"""Overlay Studio canvas (ui/src/shell/OverlaysPage.tsx) must speak the
renderer's offset units: 1000px-reference, scaled by poster width, bounded
by the style model. 2026-11-15 bug: the canvas wrote natural-pixel offsets,
passed the +/-500 bound after a short drag, and every preview 422'd silently.
There is no UI test harness, so this pins the TS constants to the backend's.
"""
from __future__ import annotations

import re
from pathlib import Path

from finmeta.engine import overlay_render
from finmeta.engine.style_presets import ElementStyle

TSX = (Path(__file__).resolve().parents[1] / "ui/src/shell/OverlaysPage.tsx").read_text()


def _const(name: str) -> float:
    m = re.search(rf"const {name} = (-?\d+(?:\.\d+)?);", TSX)
    assert m, f"{name} not found in OverlaysPage.tsx"
    return float(m.group(1))


def test_reference_width_matches_renderer():
    assert _const("REFERENCE_WIDTH") == overlay_render._REFERENCE_WIDTH


def test_offset_limit_matches_style_model():
    lim = _const("OFFSET_LIMIT")
    for field in ("x_offset", "y_offset"):
        meta = {type(m).__name__: m for m in ElementStyle.model_fields[field].metadata}
        assert meta["Ge"].ge == -lim and meta["Le"].le == lim


def test_poster_scale_clamp_matches_renderer():
    # same clamp bounds as overlay_render._poster_scale
    assert "Math.max(0.1, Math.min(naturalWidth / REFERENCE_WIDTH, 10))" in TSX
    assert overlay_render._poster_scale(50) == 0.1 and overlay_render._poster_scale(50_000) == 10.0


def test_offset_beyond_old_500_bound_is_accepted_and_moves_the_badge():
    # Operator's profile had a corner stack whose badges needed ~800 to reach
    # the bottom; the old +/-500 bound 422'd every such preview.
    import io

    from PIL import Image

    poster = io.BytesIO()
    Image.new("RGB", (1000, 1500), "black").save(poster, format="JPEG")

    def render(y):
        doc = {"elements": {"resolution": {"enabled": True, "position": "top_left", "y_offset": y}}}
        return overlay_render.render_overlay_png(poster.getvalue(), doc, values={"resolution": "4K"})

    assert render(800) != render(500)


def test_render_reports_real_badge_layout_including_off_poster_badges():
    # The Studio canvas places its drag handles from this; the operator's
    # profile (8 badges stacked bottom_left, y_offset +500) drew most of them
    # below the poster while the old approximate chips looked fine.
    import io

    from PIL import Image

    poster = io.BytesIO()
    Image.new("RGB", (720, 1080), "black").save(poster, format="JPEG")
    keys = ["imdb", "metacritic", "letterboxd", "trakt"]
    doc = {"base_position": "bottom_left", "margin": 14, "font_size": 60,
           "elements": {k: {"enabled": True, "y_offset": 500} for k in keys}}
    vals = {f"rating_{k}": "8.1" for k in keys}
    layout: list = []
    overlay_render.render_overlay_png(poster.getvalue(), doc, values=vals, layout=layout)
    assert sorted(entry["key"] for entry in layout) == sorted(keys)
    assert all({"x", "y", "w", "h"} <= set(entry) for entry in layout)
    assert any(entry["y"] >= 1080 for entry in layout)  # pushed past the bottom edge


def test_render_without_layout_arg_is_unchanged():
    import io

    from PIL import Image

    poster = io.BytesIO()
    Image.new("RGB", (1000, 1500), "black").save(poster, format="JPEG")
    doc = {"elements": {"resolution": {"enabled": True}}}
    a = overlay_render.render_overlay_png(poster.getvalue(), doc, values={"resolution": "4K"})
    b = overlay_render.render_overlay_png(poster.getvalue(), doc, values={"resolution": "4K"}, layout=[])
    assert a == b
