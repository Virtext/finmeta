"""Kometa overlay expansion against REAL Kometa files (jmxd media_info + audience_rating).

The fixtures in tests/fixtures/kometa/ are unmodified community files. Never edit them
to make a test pass.
"""
from pathlib import Path

import pytest
import yaml

from finmeta.kometa_import import expand_data, expand_file

FIX = Path(__file__).parent / "fixtures" / "kometa"
AUDIENCE = FIX / "audience_rating.yml"
MEDIA = FIX / "media_info.yml"


def _by_key(flat):
    return {f.key: f for f in flat}


@pytest.fixture(scope="module")
def audience():
    flat, rep = expand_file(AUDIENCE)
    return _by_key(flat), rep


@pytest.fixture(scope="module")
def media():
    flat, rep = expand_file(MEDIA)
    return _by_key(flat), rep


def test_fixtures_are_the_real_files():
    assert "Created by: jmxd" in MEDIA.read_text()
    assert "audience_score_high_backdrop" in AUDIENCE.read_text()


@pytest.mark.parametrize("path", [AUDIENCE, MEDIA])
def test_every_overlay_key_is_accounted_for(path):
    data = yaml.safe_load(path.read_text())
    keys = [str(k) for k in data["overlays"]]
    flat, rep = expand_file(path)
    flat_keys = {f.key for f in flat}
    skipped = {i.key for i in rep.items if i.status == "skipped"}
    assert len(rep.items) == len(keys)
    for k in keys:
        assert (k in flat_keys) != (k in skipped), k


# ---------------------------------------------------------------- audience_rating.yml

def test_audience_backdrop_entries(audience):
    by, rep = audience
    hi = by["audience_score_high_backdrop"]
    assert hi.file == "config/overlays/images/audience_score/audience_score_high.png"
    assert hi.conditions["audience_rating.gte"] == 8.0
    assert hi.group == "backdrop"
    assert hi.weight == 500
    assert hi.layout["horizontal_align"] == "right"
    assert hi.layout["vertical_offset"] == 110
    assert by["audience_score_mid_backdrop"].conditions["audience_rating.gte"] == 6.0
    assert by["audience_score_low_backdrop"].conditions["audience_rating.gte"] == 0
    assert rep.by_key("audience_score_high_backdrop").status == "ok"


def test_audience_score_entries_are_text(audience):
    by, rep = audience
    hi = by["audience_score_high"]
    assert hi.group == "score"
    assert hi.is_text and hi.name == "text(<<audience_rating>>)"   # runtime placeholder kept
    assert hi.conditions["audience_rating.gte"] == 8.0
    assert hi.layout["font_size"] == 68 and hi.layout["back_width"] == 140
    # `files` has no matching condition for score keys -> attribute dropped, reported
    assert hi.file is None
    assert rep.by_key("audience_score_high").status == "partial"
    assert any("files" in n for n in rep.by_key("audience_score_high").notes)


def test_audience_optional_builder_level_dropped_silently(audience):
    by, rep = audience
    hi = by["audience_score_high_backdrop"]
    assert "builder_level" not in hi.body
    assert not any("builder_level" in n and "unresolved" in n
                   for n in rep.by_key("audience_score_high_backdrop").notes)


# ---------------------------------------------------------------- media_info.yml

def test_media_codec_combined(media):
    by, rep = media
    o = by["DV-TrueHD-Atmos"]
    assert o.file == "config/overlays/images/media_info/codec/DV-TrueHD-Atmos.png"
    assert o.conditions["filepath.regex"] == r'(?i)\[(?!.*HDR)(DV)\].*\[(TrueHD Atmos( 7\.1)?)\]'
    assert o.group == "combined_codec"
    assert o.weight == 510
    assert o.layout["vertical_align"] == "bottom"
    assert o.layout["horizontal_align"] == "left"
    assert o.layout["horizontal_offset"] == 50
    assert o.layout["vertical_offset"] == 110
    assert "DV" in o.suppress and "TrueHD-Atmos" in o.suppress
    assert o.templates_used == ["codec_info"]
    # optional run_definition var dropped, not reported as unresolved
    assert o.body.get("run_definition") in ([], None)
    item = rep.by_key("DV-TrueHD-Atmos")
    assert item.status == "partial"                      # missing Kometa default "standard"
    assert any("standard" in n for n in item.notes)


def test_media_codec_video_and_audio_groups(media):
    by, _ = media
    assert by["DV"].group == "video_codec"
    assert by["DV"].file.endswith("/codec/DV.png")
    assert by["Atmos"].group == "audio_codec"
    assert "DV" in by["Atmos"].suppress            # audio suppresses video-only badges


def test_media_resolution(media):
    by, _ = media
    uhd = by["Ultra-HD"]
    assert uhd.file == "config/overlays/images/media_info/resolution/Ultra-HD.png"
    assert uhd.conditions["resolution"] == "4k"
    assert uhd.group == "resolution"
    assert uhd.layout["vertical_align"] == "top"
    assert by["1080P"].conditions["resolution"] == "1080p"
    assert "plex_search" in uhd.plex_only


def test_media_edition(media):
    by, _ = media
    imax = by["IMAX-4K"]
    assert imax.file == "config/overlays/images/media_info/edition/IMAX.png"
    assert imax.conditions["resolution"] == "4K"
    assert imax.conditions["filepath.regex"] == r'(?i)\{edition-IMAX[^}]*\}'
    assert imax.conditions["allowed_libraries"] == "movie"
    assert imax.layout["horizontal_offset"] == 186
    assert by["IMAX-1080p"].layout["horizontal_offset"] == 202
    assert imax.group == "edition"


def test_media_gradient_backdrop(media):
    by, rep = media
    g = by["gradient_top"]
    assert g.file == "config/overlays/images/gradients/gradient_top.png"   # not the episode variant
    assert g.name == "gradient_top"
    assert rep.by_key("gradient_top").status == "ok"


def test_media_missing_standard_template_reported(media):
    _, rep = media
    assert "standard" in rep.missing_templates
    counts = rep.counts()
    assert counts["skipped"] == 0
    assert counts["ok"] == 2 and counts["partial"] == 79      # 2 gradients ok, 79 use "standard"


# ---------------------------------------------------------------- semantics (small, synthetic)

def test_default_block_alone_produces_no_attributes():
    data = {"templates": {"T": {"default": {"group": "score"}}},
            "overlays": {"x": {"template": [{"name": "T"}]}}}
    flat, _ = expand_data(data)
    assert flat[0].body == {}
    assert flat[0].variables["group"] == "score"          # default supplies a variable value


def test_overlay_variables_beat_conditionals_and_defaults():
    data = {"templates": {"T": {
        "default": {"pos": "left"},
        "conditionals": {"pos": {"conditions": [{"key": "x", "value": "right"}]}},
        "overlay": {"horizontal_align": "<<pos>>"}}},
        "overlays": {
            "x": {"variables": {"key": "x"}, "template": [{"name": "T"}]},
            "y": {"variables": {"key": "y"}, "template": [{"name": "T"}]},
            "z": {"variables": {"key": "x", "pos": "center"}, "template": [{"name": "T"}]},
        }}
    by = _by_key(expand_data(data)[0])
    assert by["x"].layout["horizontal_align"] == "right"   # conditional
    assert by["y"].layout["horizontal_align"] == "left"    # default
    assert by["z"].layout["horizontal_align"] == "center"  # overlay variable wins


def test_nested_variable_names():
    data = {"templates": {"T": {"overlay": {"name": "<<use_<<type>>>>"}}},
            "overlays": {"x": {"variables": {"type": "video", "use_video": "yes"},
                               "template": [{"name": "T"}]}}}
    assert expand_data(data)[0][0].name == "yes"


def test_unresolved_non_optional_is_partial_not_crash():
    data = {"templates": {"T": {"overlay": {"file": "<<missing>>", "group": "g"}}},
            "overlays": {"x": {"template": [{"name": "T"}]}}}
    flat, rep = expand_data(data)
    assert flat[0].file is None and flat[0].group == "g"
    assert rep.by_key("x").status == "partial"
