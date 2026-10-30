"""Overlay style presets + per-element layout (SPEC S9 \u00a7D).

The overlay editor works on a *style document*: base compositor settings plus
one entry per element (rating logos, resolution/HDR/audio text badges and an
optional ribbon). Presets are built-in defaults; they are always cloned before
editing so user edits never mutate them ("clone-before-edit").

A posted style merges over the current config\'s ``overlays.style`` for live
previews; saved styles land under ``cfg.overlays.user_styles[name]``.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


# Element keys in render order: rating logos first, then info text, then the
# S9g1/K7a TV-status + S9g2/K7b trending-rank ribbons, ribbon text last.
# "network" (2026-10-23, TV shows only): the streaming/broadcast network from
# TMDb's own `networks` field, badge images lazily fetched from Kometa-Team/
# Default-Images -- see engine.network_logos.
ELEMENT_KEYS: List[str] = [
    "imdb", "rt_critic", "rt_audience", "metacritic", "tmdb", "letterboxd",
    "trakt", "resolution", "hdr", "audio", "network", "status", "trending_rank", "ribbon",
]

GRID_POSITIONS = [
    "top_left", "top_center", "top_right",
    "center_left", "center", "center_right",
    "bottom_left", "bottom_center", "bottom_right",
]

RATING_LABELS = {
    "imdb": "IMDb", "rt_critic": "RT critic", "rt_audience": "RT audience",
    "metacritic": "Metacritic", "tmdb": "TMDb", "letterboxd": "Letterboxd",
    "trakt": "Trakt",
}

_HEX = r"^#[0-9A-Fa-f]{6}([0-9A-Fa-f]{2})?$"


def element_labels() -> Dict[str, str]:
    """Element key \u2192 human label (for the editor UI)."""
    out = dict(RATING_LABELS)
    out.update({
        "resolution": "Resolution", "hdr": "HDR / Dolby Vision",
        "audio": "Audio codec / Atmos",
        "network": "Network / streaming service (TV shows only)",
        "status": "TV status (Sonarr)",
        "trending_rank": "Trending rank (MDBList)",
        "ribbon": "Ribbon text",
    })
    return out


class ElementStyle(_Strict):
    """Per-element placement + look inside an overlay style."""

    enabled: bool = Field(
        default=True, description="Whether this element is rendered at all.")
    position: Optional[str] = Field(
        default=None, pattern=f"^({'|'.join(GRID_POSITIONS)})$",
        description="Override for the base anchor; one of the 3x3 grid positions.",
        json_schema_extra={"help": "Anchor point on a 3\u00d73 grid. Empty follows the global position."})
    x_offset: int = Field(default=0, ge=-500, le=500,
                          description="Pixel offset from the anchor, horizontal.")
    y_offset: int = Field(default=0, ge=-500, le=500,
                          description="Pixel offset from the anchor, vertical.")
    size: Optional[int] = Field(default=None, ge=8, le=240,
                                description="Font/badge height in pixels (default = base font_size).")
    color: Optional[str] = Field(default=None, pattern=_HEX,
                                 description="Text/foreground colour override (#RRGGBB or #RRGGBBAA).")
    back_color: Optional[str] = Field(default=None, pattern=_HEX,
                                      description="Backdrop plate colour (#RRGGBB or #RRGGBBAA; empty = base bg).")
    radius: Optional[int] = Field(default=None, ge=0, le=100,
                                  description="Corner radius of this element\'s backdrop plate.")
    padding_x: int = Field(default=10, ge=0, le=60,
                           description="Horizontal padding inside the backdrop plate (px).")
    padding_y: int = Field(default=6, ge=0, le=60,
                           description="Vertical padding inside the backdrop plate (px).")
    opacity: float = Field(
        default=1.0, ge=0.0, le=1.0,
        description="Opacity multiplier applied to the backdrop plate.")
    group: str = Field(default="main", max_length=40,
                       description="Group name; elements in one group stack as a block.")
    weight: int = Field(default=100, ge=-1000, le=1000,
                        description="Sort key within its position column (lower first).")
    full_width: bool = Field(
        default=True,
        description="TV status / trending rank only: draw as an edge-to-edge band "
                    "flush with the poster (the long-standing default) instead of a "
                    "normal position-anchored pill sized to the text plus padding.")


class OverlayStyleDoc(_Strict):
    """A full overlay style document (base settings + per-element layout)."""

    base_position: str = Field(
        default="top_left", pattern=f"^({'|'.join(GRID_POSITIONS)})$",
        description="Default anchor for all elements without an override.")
    margin: int = Field(default=20, ge=0, le=500)
    font_size: int = Field(default=42, ge=8, le=300)
    bg: str = Field(default="#000000B0", pattern=_HEX)
    fg: str = Field(default="#FFFFFF", pattern=_HEX)
    radius: int = Field(default=12, ge=0, le=100)
    ribbon_text: Optional[str] = Field(
        default=None, max_length=120,
        description="Text of the ribbon element; empty disables its text content "
                    "(the element toggle still controls rendering).")
    elements: Dict[str, ElementStyle] = Field(
        default_factory=dict,
        description="Per-element overrides, keyed by element name.",
        json_schema_extra={"help": "One entry per overlay element (rating logos, resolution/HDR/audio, ribbon)."})
    status_colors: Dict[str, str] = Field(
        default_factory=dict,
        description="Per-status background colour overrides for the TV status ribbon.",
        json_schema_extra={"help": "Fine-tune the TV status ribbon's background colour for one or more "
        "status values (e.g. `ended`, `canceled`, `returning`) -- hex `#RRGGBB` or `#RRGGBBAA`. Any "
        "status left out keeps its built-in default colour."})

    @field_validator("status_colors")
    @classmethod
    def _check_status_colors(cls, v: Dict[str, str]) -> Dict[str, str]:
        from finmeta.engine.tv_status import STATUS_COLORS
        for key, color in v.items():
            if key not in STATUS_COLORS:
                raise ValueError(f"unknown TV status {key!r}; expected one of {sorted(STATUS_COLORS)}")
            if not re.match(_HEX, color):
                raise ValueError(f"status_colors[{key!r}] must be hex #RRGGBB or #RRGGBBAA")
        return v


def preset_ratings_bottom() -> OverlayStyleDoc:
    """All rating logos along the bottom edge."""
    doc = OverlayStyleDoc(base_position="bottom_left")
    for key in RATING_LABELS:
        doc.elements[key] = ElementStyle(enabled=True)
    doc.elements["ribbon"] = ElementStyle(enabled=False)
    return doc


def preset_resolution_audio_top() -> OverlayStyleDoc:
    """Resolution + HDR + audio badges top-left, ratings off."""
    doc = OverlayStyleDoc(base_position="top_left", font_size=34)
    for key in ("resolution", "hdr", "audio"):
        doc.elements[key] = ElementStyle(enabled=True)
    for key in RATING_LABELS:
        doc.elements[key] = ElementStyle(enabled=False)
    doc.elements["ribbon"] = ElementStyle(enabled=False)
    return doc


def preset_ribbon_corner() -> OverlayStyleDoc:
    """A single corner ribbon; everything else off."""
    doc = OverlayStyleDoc(
        base_position="top_right", margin=12, font_size=30, radius=6,
        bg="#E50914B0", fg="#FFFFFF", ribbon_text="FINMETA")
    doc.elements["ribbon"] = ElementStyle(
        enabled=True, position="top_right", back_color=None, padding_x=14, padding_y=8)
    for key in ELEMENT_KEYS[:-1]:
        doc.elements[key] = ElementStyle(enabled=False)
    return doc


def preset_minimal() -> OverlayStyleDoc:
    """Just the TMDb star rating, small and quiet."""
    doc = OverlayStyleDoc(base_position="bottom_left", margin=14, font_size=30,
                          bg="#00000099", radius=8)
    doc.elements["tmdb"] = ElementStyle(enabled=True)
    for key in ELEMENT_KEYS:
        if key != "tmdb":
            doc.elements[key] = ElementStyle(enabled=False)
    return doc


PRESETS: Dict[str, dict] = {
    "Ratings bottom": {"description": "All available rating logos along the bottom edge.",
                       "build": preset_ratings_bottom},
    "Resolution + audio top": {"description": "Resolution / HDR / Audio badges stacked top-left; ratings off.",
                               "build": preset_resolution_audio_top},
    "Ribbon corner": {"description": "A single coloured ribbon text badge in a corner.",
                      "build": preset_ribbon_corner},
    "Minimal": {"description": "Only the TMDb \u2605 rating, small and subtle.",
                "build": preset_minimal},
}

# Rating element keys in display order (also used by models.py).
RATING_ELEMENT_KEYS: List[str] = list(RATING_LABELS.keys())


def all_presets() -> List[dict]:
    """Preset metadata: name, description, style document."""
    out = []
    for name, meta in PRESETS.items():
        out.append({"name": name, "description": meta["description"],
                    "style": meta["build"]().model_dump(mode="json")})
    return out


__all__ = [
    "ELEMENT_KEYS", "GRID_POSITIONS", "ElementStyle", "OverlayStyleDoc",
    "PRESETS", "preset_ratings_bottom", "preset_resolution_audio_top",
    "preset_ribbon_corner", "preset_minimal", "element_labels", "all_presets",
    "RATING_ELEMENT_KEYS", "RATING_LABELS",
]
