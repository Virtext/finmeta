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


def test_render_reports_real_badge_layout_clamped_inside_and_not_overlapping():
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
    # 2026-11-15 operator decision: +500 used to push these past the bottom
    # edge; now every badge is clamped fully inside and none overlap.
    for e in layout:
        assert 0 <= e["x"] and e["x"] + e["w"] <= 720 and 0 <= e["y"] and e["y"] + e["h"] <= 1080, e
    for i, a in enumerate(layout):
        for b in layout[i + 1:]:
            assert not (a["x"] < b["x"] + b["w"] and b["x"] < a["x"] + a["w"]
                        and a["y"] < b["y"] + b["h"] and b["y"] < a["y"] + a["h"]), (a, b)


def test_render_without_layout_arg_is_unchanged():
    import io

    from PIL import Image

    poster = io.BytesIO()
    Image.new("RGB", (1000, 1500), "black").save(poster, format="JPEG")
    doc = {"elements": {"resolution": {"enabled": True}}}
    a = overlay_render.render_overlay_png(poster.getvalue(), doc, values={"resolution": "4K"})
    b = overlay_render.render_overlay_png(poster.getvalue(), doc, values={"resolution": "4K"}, layout=[])
    assert a == b


def test_resize_writes_integer_px_within_style_bounds():
    # 2026-11-15: the resize handle wrote a 0.4-2.5 factor (e.g. 1.33) into
    # `size`, which is int px; the preview 422'd and the profile was saved broken.
    meta = {type(m).__name__: m for m in ElementStyle.model_fields["size"].metadata}
    lo, hi = meta["Ge"].ge, meta["Le"].le
    assert f"size: Math.min({hi}, Math.max({lo}, Math.round(" in TSX
    assert "Math.min(2.5" not in TSX


def test_badge_positions_do_not_depend_on_which_other_badges_have_data():
    # Operator-reported 2026-11-15: toggling "Simulate all badges" (or an item
    # lacking a rating) moved every badge, since each was a slot in a stack.
    import io

    from PIL import Image

    poster = io.BytesIO()
    Image.new("RGB", (1000, 1500), "black").save(poster, format="JPEG")
    doc = {"base_position": "bottom_left", "elements": {
        "imdb": {"enabled": True, "x_offset": 300, "y_offset": -400},
        "tmdb": {"enabled": True, "x_offset": 0, "y_offset": -900},
        "metacritic": {"enabled": True}, "resolution": {"enabled": True}}}

    def where(vals):
        lay: list = []
        overlay_render.render_overlay_png(poster.getvalue(), doc, values=vals, layout=lay)
        return {e["key"]: (e["x"], e["y"]) for e in lay}

    few = where({"rating_imdb": "8.1", "rating_tmdb": "7.0"})
    many = where({"rating_imdb": "8.1", "rating_tmdb": "7.0", "rating_metacritic": "80", "resolution": "4K"})
    assert few["imdb"] == many["imdb"] and few["tmdb"] == many["tmdb"]


def _layout(doc, vals, size=(1000, 1500)):
    import io

    from PIL import Image

    poster = io.BytesIO()
    Image.new("RGB", size, "black").save(poster, format="JPEG")
    lay: list = []
    overlay_render.render_overlay_png(poster.getvalue(), doc, values=vals, layout=lay)
    return {e["key"]: e for e in lay}


ALL3 = {"rating_imdb": "8", "rating_metacritic": "7", "rating_tmdb": "6"}
ENDS = {"rating_imdb": "8", "rating_tmdb": "6"}


def test_stack_with_a_missing_badge_closes_up_toward_the_nearer_edge():
    # Operator 2026-11-15: three stacked badges where only the top and bottom
    # have data must close up toward the nearest top/bottom edge, no hole.
    els = {k: {"enabled": True} for k in ("imdb", "metacritic", "tmdb")}
    top_all, top = _layout({"base_position": "top_left", "elements": els}, ALL3), \
        _layout({"base_position": "top_left", "elements": els}, ENDS)
    assert top["imdb"]["y"] == top_all["imdb"]["y"]
    assert top["tmdb"]["y"] == top_all["metacritic"]["y"]          # moved up into the hole
    bot_all, bot = _layout({"base_position": "bottom_left", "elements": els}, ALL3), \
        _layout({"base_position": "bottom_left", "elements": els}, ENDS)
    assert bot["tmdb"]["y"] == bot_all["tmdb"]["y"]
    # moved down into the hole (bottom edges line up; badge heights differ)
    assert bot["imdb"]["y"] + bot["imdb"]["h"] == bot_all["metacritic"]["y"] + bot_all["metacritic"]["h"]


def test_layout_reports_the_push_the_studio_folds_into_the_offset():
    # Two badges dropped on one spot: the second is pushed clear, and the
    # layout says by how much so the Studio can save where it is really drawn.
    els = {"imdb": {"enabled": True, "x_offset": 300, "y_offset": 300},
           "metacritic": {"enabled": True, "x_offset": 300, "y_offset": 300}}
    both = _layout({"base_position": "top_left", "elements": els}, {"rating_imdb": "8", "rating_metacritic": "7"})
    assert both["imdb"]["push_y"] == 0 and both["metacritic"]["push_y"] > 0
    assert both["metacritic"]["y"] >= both["imdb"]["y"] + both["imdb"]["h"]


def test_layout_reports_home_spots_and_ghosts_so_simulating_moves_no_handle():
    # Operator-reported 2026-11-16: "Simulate all badges" rearranged the
    # Studio's handles, and badges without data had no real (clamped) handle.
    # Handles sit at (x, home_y); a badge without data is reported as a ghost.
    from finmeta.engine.overlay_render import _GHOST_VALUES

    els = {k: {"enabled": True} for k in ("imdb", "metacritic", "tmdb", "resolution")}
    doc = {"base_position": "top_right", "elements": els}
    real = {"rating_imdb": "8", "rating_tmdb": "6"}
    off = _layout(doc, real)
    on = _layout(doc, {**dict(_GHOST_VALUES.values()), **real})   # what sample_data sends
    assert off["metacritic"]["ghost"] and off["resolution"]["ghost"]
    assert not any(e.get("ghost") for e in on.values())
    assert off["tmdb"]["y"] != off["tmdb"]["home_y"]               # drawn closed up...
    for k in els:                                                  # ...handle stays put
        assert (off[k]["x"], off[k]["home_y"], off[k]["w"], off[k]["h"]) == \
            (on[k]["x"], on[k]["home_y"], on[k]["w"], on[k]["h"]), k
