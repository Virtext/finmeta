"""Tests for image pack matchers (SPEC S9 D3 step 2)."""
from __future__ import annotations

import pytest

from finmeta.engine.image_packs import CategoryDef
from finmeta.engine.image_pack_matchers import (
    MatchContext, resolve_pack_key,
    match_format_rule, match_format_rule_combo, match_filename_regex,
    match_metadata, match_rating_tier,
)


class TestFormatRuleMatcher:
    def test_basic(self):
        cat = CategoryDef(matcher="format_rule", keys=["res_4k", "res_1080p"])
        ctx = MatchContext(k9_result={"image": "res_4k.png", "key": "res_4k"})
        assert match_format_rule(cat, ctx) == ["res_4k"]

    def test_no_k9_result(self):
        cat = CategoryDef(matcher="format_rule", keys=["res_4k"])
        ctx = MatchContext(k9_result=None)
        assert match_format_rule(cat, ctx) is None

    def test_key_not_in_pack(self):
        cat = CategoryDef(matcher="format_rule", keys=["res_1080p"])
        ctx = MatchContext(k9_result={"image": "res_4k.png"})
        # res_4k not in pack, and no combine decomposition for it
        assert match_format_rule(cat, ctx) is None

    def test_combine_fallback(self):
        """dv_hdr_plus decomposes into dv + hdr10plus when the combined image is absent."""
        cat = CategoryDef(matcher="format_rule", keys=["dv", "hdr10plus"])
        ctx = MatchContext(k9_result={"image": "dv_hdr_plus.png"})
        result = match_format_rule(cat, ctx)
        assert result == ["dv", "hdr10plus"]

    def test_combine_partial_fallback(self):
        """Only one part available -> return just that part."""
        cat = CategoryDef(matcher="format_rule", keys=["dv"])
        ctx = MatchContext(k9_result={"image": "dv_hdr_plus.png"})
        result = match_format_rule(cat, ctx)
        assert result == ["dv"]

    def test_combine_no_parts_available(self):
        cat = CategoryDef(matcher="format_rule", keys=["hdr10"])
        ctx = MatchContext(k9_result={"image": "dv_hdr_plus.png"})
        # dv not in keys, hdr10plus not in keys -> no match
        assert match_format_rule(cat, ctx) is None


class TestFormatRuleComboMatcher:
    _COMPONENTS = ["resolution", "hdr", "audio"]
    _KEYS = [
        "res_1080p__sdr", "res_1080p__sdr__truehd_atmos",
        "res_1080p__hdr", "res_1080p__hdr__ddplus_atmos",
        "res_480p__sdr", "res_480p__dv",
    ]

    def _cat(self, **kw):
        return CategoryDef(matcher="format_rule_combo", keys=self._KEYS,
                           components=self._COMPONENTS, **kw)

    def test_full_three_part_match(self):
        ctx = MatchContext(k9_results={
            "resolution": {"key": "res_1080p"},
            "hdr": {"key": "hdr10"},
            "audio": {"key": "ddplus_atmos"},
        })
        assert match_format_rule_combo(self._cat(), ctx) == ["res_1080p__hdr__ddplus_atmos"]

    def test_audio_dropped_when_combo_key_absent(self):
        # dtsx 1080p/hdr combo isn't in self._KEYS -> falls back to resolution+dr only.
        ctx = MatchContext(k9_results={
            "resolution": {"key": "res_1080p"},
            "hdr": {"key": "hdr10"},
            "audio": {"key": "dtsx"},
        })
        assert match_format_rule_combo(self._cat(), ctx) == ["res_1080p__hdr"]

    def test_sdr_bucket_no_hdr_result(self):
        ctx = MatchContext(k9_results={
            "resolution": {"key": "res_1080p"},
            "hdr": None,
            "audio": {"key": "truehd_atmos"},
        })
        assert match_format_rule_combo(self._cat(), ctx) == ["res_1080p__sdr__truehd_atmos"]

    def test_dv_bucket(self):
        ctx = MatchContext(k9_results={
            "resolution": {"key": "res_480p"},
            "hdr": {"key": "dovi"},
            "audio": None,
        })
        assert match_format_rule_combo(self._cat(), ctx) == ["res_480p__dv"]

    def test_plain_dts_has_no_audio_suffix(self):
        # "dts" (plain) is not in _AUDIO_SUFFIXES -> same bucket as no audio.
        ctx = MatchContext(k9_results={
            "resolution": {"key": "res_480p"},
            "hdr": None,
            "audio": {"key": "dts"},
        })
        assert match_format_rule_combo(self._cat(), ctx) == ["res_480p__sdr"]

    def test_no_resolution_result_is_none(self):
        ctx = MatchContext(k9_results={"resolution": None, "hdr": {"key": "hdr10"}})
        assert match_format_rule_combo(self._cat(), ctx) is None

    def test_no_k9_results_at_all_is_none(self):
        ctx = MatchContext(k9_result={"image": "res_4k.png"})  # singular only, no k9_results
        assert match_format_rule_combo(self._cat(), ctx) is None

    def test_no_components_declared_is_none(self):
        cat = CategoryDef(matcher="format_rule_combo", keys=self._KEYS)
        ctx = MatchContext(k9_results={"resolution": {"key": "res_1080p"}})
        assert match_format_rule_combo(cat, ctx) is None

    def test_full_combo_not_in_keys_falls_back(self):
        cat = CategoryDef(matcher="format_rule_combo", keys=["res_1080p__hdr"],
                          components=self._COMPONENTS)
        ctx = MatchContext(k9_results={
            "resolution": {"key": "res_1080p"},
            "hdr": {"key": "hdr10"},
            "audio": {"key": "ddplus_atmos"},
        })
        assert match_format_rule_combo(cat, ctx) == ["res_1080p__hdr"]

    def test_plain_format_rule_matcher_unaffected(self):
        """Existing format_rule (single-category) matcher is untouched by this addition."""
        cat = CategoryDef(matcher="format_rule", keys=["res_4k"])
        ctx = MatchContext(k9_result={"image": "res_4k.png"}, k9_results={"resolution": {"key": "res_4k"}})
        assert match_format_rule(cat, ctx) == ["res_4k"]


class TestFilenameRegexMatcher:
    def test_match(self):
        cat = CategoryDef(matcher="filename_regex", keys=["bluray", "web"],
                          regex=r"(?P<key>bluray|web)")
        ctx = MatchContext(filename="Movie.2024.Bluray.1080p.mkv")
        assert match_filename_regex(cat, ctx) == ["bluray"]

    def test_no_match(self):
        cat = CategoryDef(matcher="filename_regex", keys=["bluray"],
                          regex=r"(?P<key>bluray)")
        ctx = MatchContext(filename="Movie.2024.WEB-DL.mkv")
        assert match_filename_regex(cat, ctx) is None

    def test_empty_filename(self):
        cat = CategoryDef(matcher="filename_regex", keys=["x"], regex=r"(?P<key>x)")
        ctx = MatchContext(filename="")
        assert match_filename_regex(cat, ctx) is None


class TestFilenameRegexMatcherPerKeyDict:
    """The per-key dict shape (``filename_regex: {key: pattern}``) real
    Kometa/jmxd-style media_info.yml sources use and kometa_import.to_pack
    emits -- one independent regex per key, no shared named group. Before
    this shape was supported, CategoryDef silently dropped the field
    (pydantic default extra="ignore"), so any imported pack's filename_regex
    category could never match anything (real bug, found 2026-10-22)."""

    def test_match_first_matching_key_wins(self):
        cat = CategoryDef(
            matcher="filename_regex", keys=["DV", "HDR"],
            filename_regex={"DV": r"(?i)\bDV\b(?!.*HDR)", "HDR": r"(?i)\bHDR10\b"})
        ctx = MatchContext(filename="Movie.2020.2160p.HDR10.x265-GROUP.mkv")
        assert match_filename_regex(cat, ctx) == ["HDR"]

    def test_no_key_matches(self):
        cat = CategoryDef(
            matcher="filename_regex", keys=["DV", "HDR"],
            filename_regex={"DV": r"(?i)\bDV\b", "HDR": r"(?i)\bHDR10\b"})
        ctx = MatchContext(filename="Movie.2020.2160p.SDR.x265-GROUP.mkv")
        assert match_filename_regex(cat, ctx) is None

    def test_key_not_in_declared_keys_is_ignored(self):
        """A filename_regex entry for a key outside `keys` (e.g. no image on
        disk for it) is never returned, even if its pattern matches."""
        cat = CategoryDef(
            matcher="filename_regex", keys=["HDR"],  # DV deliberately not listed
            filename_regex={"DV": r"(?i)\bDV\b", "HDR": r"(?i)\bHDR10\b"})
        ctx = MatchContext(filename="Movie.2020.DV.mkv")
        assert match_filename_regex(cat, ctx) is None

    def test_filename_regex_dict_takes_priority_over_regex(self):
        """When both shapes are present, the per-key dict wins."""
        cat = CategoryDef(
            matcher="filename_regex", keys=["bluray", "web"],
            regex=r"(?P<key>bluray|web)",
            filename_regex={"web": r"(?i)\bWEB\b"})
        ctx = MatchContext(filename="Movie.2024.Bluray.WEB.mkv")
        # Only "web" is in the dict shape -- "bluray" is reachable only via
        # the single-regex shape, which must NOT be consulted once the dict
        # shape is present.
        assert match_filename_regex(cat, ctx) == ["web"]


class TestMetadataMatcher:
    def test_match(self):
        cat = CategoryDef(matcher="metadata", keys=["en", "ja"], field="original_language")
        ctx = MatchContext(metadata={"original_language": "ja"})
        assert match_metadata(cat, ctx) == ["ja"]

    def test_no_field(self):
        cat = CategoryDef(matcher="metadata", keys=["en"], field="original_language")
        ctx = MatchContext(metadata={})
        assert match_metadata(cat, ctx) is None

    def test_value_not_in_keys(self):
        cat = CategoryDef(matcher="metadata", keys=["en"], field="lang")
        ctx = MatchContext(metadata={"lang": "fr"})
        assert match_metadata(cat, ctx) is None


class TestRatingTierMatcher:
    def test_gold(self):
        cat = CategoryDef(matcher="rating_tier", keys=["gold", "silver", "bronze"],
                          tiers={"gold": ">=8.0", "silver": ">=6.0", "bronze": "<6.0"})
        ctx = MatchContext(rating_value=9.1)
        assert match_rating_tier(cat, ctx) == ["gold"]

    def test_silver(self):
        cat = CategoryDef(matcher="rating_tier", keys=["gold", "silver", "bronze"],
                          tiers={"gold": ">=8.0", "silver": ">=6.0", "bronze": "<6.0"})
        ctx = MatchContext(rating_value=7.2)
        assert match_rating_tier(cat, ctx) == ["silver"]

    def test_bronze(self):
        cat = CategoryDef(matcher="rating_tier", keys=["gold", "silver", "bronze"],
                          tiers={"gold": ">=8.0", "silver": ">=6.0", "bronze": "<6.0"})
        ctx = MatchContext(rating_value=4.5)
        assert match_rating_tier(cat, ctx) == ["bronze"]

    def test_no_rating(self):
        cat = CategoryDef(matcher="rating_tier", keys=["gold"], tiers={"gold": ">=8.0"})
        ctx = MatchContext(rating_value=None)
        assert match_rating_tier(cat, ctx) is None


class TestResolvePackKey:
    def test_dispatch(self):
        cat = CategoryDef(matcher="format_rule", keys=["res_4k"])
        ctx = MatchContext(k9_result={"image": "res_4k.png"})
        assert resolve_pack_key(cat, ctx) == ["res_4k"]

    def test_unknown_matcher(self):
        # Create a category with an invalid matcher (bypass validation for test)
        cat = CategoryDef.model_construct(matcher="unknown", keys=[])
        ctx = MatchContext()
        assert resolve_pack_key(cat, ctx) is None
