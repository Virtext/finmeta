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
