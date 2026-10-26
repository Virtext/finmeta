"""Tests for kometa_to_pack: expanded Kometa overlays -> image pack (SPEC_S9 E + D3).

Uses the real jmxd fixture files committed in S9e1 (tests/fixtures/kometa/);
the produced pack.yml must validate against the existing PackDef model.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

BACKEND = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from finmeta.engine.image_packs import PackDef  # noqa: E402
from finmeta.kometa_import.expand import expand_file  # noqa: E402
from finmeta.kometa_import.to_pack import kometa_to_pack  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "kometa"
MEDIA_INFO = FIXTURES / "media_info.yml"
AUDIENCE_RATING = FIXTURES / "audience_rating.yml"


def _notes(report, key):
    return [n for it in report.items if it.key == key for n in it.notes]


@pytest.fixture(scope="module")
def media_info_pack():
    flat, report = expand_file(MEDIA_INFO)
    pack, copies, report = kometa_to_pack(flat, report, "/nonexistent/kometa-images", "media_info")
    return pack, copies, report


def test_media_info_categories(media_info_pack):
    pack, copies, report = media_info_pack
    cats = pack["categories"]
    assert set(cats) == {"combined_codec", "video_codec", "audio_codec", "resolution", "edition"}
    for name in ("combined_codec", "video_codec", "audio_codec"):
        assert cats[name]["matcher"] == "filename_regex"
        assert cats[name]["filename_regex"], f"{name} must carry filename_regex rules"
        assert len(cats[name]["keys"]) == len(cats[name]["filename_regex"])
    # codec regexes are the Kometa ones, keyed by overlay key
    assert cats["video_codec"]["filename_regex"]["DV"] == r"(?i)\[(?!.*HDR)(DV)\]"


def test_media_info_resolution_format_rule(media_info_pack):
    pack, _, _ = media_info_pack
    res = pack["categories"]["resolution"]
    assert res["matcher"] == "format_rule"
    # key_map uses Finmeta's own DEFAULT_RESOLUTION_RULES keys
    assert set(res["key_map"]) == {"res_4k", "res_1080p"}
    assert res["key_map"]["res_4k"] == "res_4k"
    assert res["key_map"]["res_1080p"] == "res_1080p"
    assert set(res["keys"]) == {"res_4k", "res_1080p"}


def test_media_info_edition_movies_only_and_collapse(media_info_pack):
    pack, copies, report = media_info_pack
    ed = pack["categories"]["edition"]
    assert ed["matcher"] == "filename_regex"
    assert ed["movies_only"] is True
    assert "IMAX-1080p" not in ed["keys"] and "IMAX-4K" not in ed["keys"]
    assert "IMAX" in ed["keys"]
    assert len(ed["keys"]) == 18, "36 per-resolution entries collapse to 18 edition keys"
    # the dropped per-resolution offset is noted in the report
    notes = _notes(report, "IMAX")
    assert any("dropped per-resolution horizontal offsets" in n for n in notes)
    # one image copy per collapsed key (no -4K/-1080p target names)
    targets = [t for _, t in copies]
    assert "images/edition/IMAX.png" in targets
    assert not any(t.endswith(("-4K.png", "-1080p.png")) for t in targets)


def test_media_info_gradients_skipped(media_info_pack):
    pack, copies, report = media_info_pack
    for key in ("gradient_top", "gradient_bottom"):
        item = next(it for it in report.items if it.key == key)
        assert item.status == "skipped"
        assert any("not supported in D3" in n for n in item.notes)
    assert "gradients" not in pack["categories"]


def test_media_info_position_and_missing_sources(media_info_pack):
    pack, copies, report = media_info_pack
    # layout mapping: align corner + offset [horizontal, vertical]
    assert pack["categories"]["video_codec"]["align"] == "bottom-left"
    assert pack["categories"]["video_codec"]["offset"] == [50, 110]
    assert pack["categories"]["edition"]["align"] == "top-left"
    # missing source files -> report entry per key, no crash (root does not exist)
    n_missing = sum(1 for it in report.items for n in it.notes if n.startswith("missing source image"))
    assert n_missing == len(copies)


def test_media_info_pack_validates(media_info_pack):
    pack, _, _ = media_info_pack
    pdef = PackDef(**yaml.safe_load(yaml.safe_dump(pack)))
    assert pdef.name == "media_info"
    assert set(pdef.categories) == set(pack["categories"])


@pytest.fixture(scope="module")
def audience_pack():
    flat, report = expand_file(AUDIENCE_RATING)
    pack, copies, report = kometa_to_pack(flat, report, "/nonexistent/kometa-images", "audience_rating")
    return pack, copies, report


def test_audience_rating_tier(audience_pack):
    pack, copies, report = audience_pack
    cats = pack["categories"]
    # one rating_tier category (the backdrop group); the text overlays merge into it
    tier_cats = [c for c in cats.values() if c["matcher"] == "rating_tier"]
    assert len(tier_cats) == 1
    cat = tier_cats[0]
    assert cat["source"] == "audience"
    assert set(cat["tiers"]) == {"8.0", "6.0", "0.0"}
    assert list(cat["tiers"].values()) == [
        "audience_score_high", "audience_score_mid", "audience_score_low"]
    assert cat["text"] == "{value}"
    # image copies target the tier keys under the category directory
    targets = {t for _, t in copies}
    assert targets == {
        "images/backdrop/audience_score_high.png",
        "images/backdrop/audience_score_mid.png",
        "images/backdrop/audience_score_low.png"}


def test_audience_pack_validates(audience_pack):
    pack, _, _ = audience_pack
    pdef = PackDef(**yaml.safe_load(yaml.safe_dump(pack)))
    assert pdef.name == "audience_rating"
    cat = list(pdef.categories.values())[0]
    assert cat.matcher == "rating_tier"
    assert cat.tiers and set(cat.tiers) == {"8.0", "6.0", "0.0"}


def test_missing_fixture_stops():
    # Guardrail: the real fixtures must exist; never invent them.
    assert MEDIA_INFO.is_file() and AUDIENCE_RATING.is_file()
