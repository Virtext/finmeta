# unit tests: K9 format detection rules (mocked MediaStreams)
from __future__ import annotations

import pytest

from finmeta.engine.format_rules import (
    RuleMatchError, best_audio_facts, detect_all, detect_category,
    default_format_rules, video_facts)


def _video(**kw):
    d = {"Type": "Video"}
    d.update(kw)
    return [d]


def _audio(codec="aac", channels=2, title="", profile="", forced=False, external=False):
    a = {"Type": "Audio", "Codec": codec, "Channels": channels,
         "DisplayTitle": title, "Profile": profile}
    if forced:
        a["IsForced"] = True
    if external:
        a["IsExternal"] = True
    return [a]


class TestFacts:
    def test_video_facts_missing(self):
        assert video_facts(_audio()) is None

    def test_best_audio_picks_highest_channels_and_skips_forced_external(self):
        streams = (_audio("ac3", 6)[0],
                   _audio("eac3", 8)[0],
                   {**_audio("mlpa", 16, forced=True)[0]},
                   {**_audio("truehd", 20, external=True)[0]})
        f = best_audio_facts(streams)
        # highest channel count among non-forced/non-external (K9); the 16ch
        # forced and 20ch external tracks must be skipped.
        assert f["codec"] == "EAC3" and f["channels"] == 8

    def test_no_audio_streams(self):
        assert best_audio_facts(_video(Width=1920, Height=1080)) is None

    def test_zero_channel_track_has_no_badge_but_still_reports_facts(self):
        # K9: no usable channel count -> matcher must not badge anything, even
        # though the stream exists so facts are still reported for diagnostics.
        f = best_audio_facts(_audio(channels=0))
        assert f["channels"] == 0
        assert detect_category("audio", _audio(channels=0)) is None

    def test_video_facts_fields(self):
        # video_facts takes the MediaStreams list (like detect_category does), not one dict.
        # ColorTransfer is reported as-is; the fallback hint is only *used* when a rule's
        # condition names it (see TestDynamicRange::test_fallback_from_colortransfer_when_no_rangetype), so its value here does not change matching outcomes for an explicit VideoRangeType stream.
        f = video_facts([{"Type": "Video", "Width": 3840, "Height": 1600,
                          "VideoRangeType": "SDR", "ColorTransfer": "smpte2084"}])
        assert f["width"] == 3840 and f["height"] == 1600
        assert f["video_range_type"] == "SDR"
        assert f["fallback"] == "smpte2084"   # transfer field always feeds the hint


class TestDynamicRange:
    @pytest.mark.parametrize("vrt,key,label", [
        ("SDR", None, None),                       # K9: none for SDR
        ("HDR10", "hdr10", "HDR10"),
        ("DOVIWithHLG", "dovi_hdr10", "DV+HDR10"),
        ("DOVIWithHDR10Plus", "dovi_hdr10plus", "DV+HDR10+"),
    ])
    def test_by_videorrangetype(self, vrt, key, label):
        out = detect_category("dynamic_range", _video(VideoRangeType=vrt))
        if key is None:
            assert out is None
        else:
            assert (out or {}).get("key") == key
            assert (out or {}).get("label") == label

    @pytest.mark.parametrize("ct,key", [
        ("smpte2084", "hdr10"),     # PQ transfer without a range type -> HDR10 hint
        ("arib-std-b67", "hlg"),
        ("AribStdB67", "hlg"),      # case/separator variants normalise the same way
        ("srgb", None),             # unknown transfer -> no badge, never guess
        ("", None),                 # absent entirely -> no badge
    ])
    def test_fallback_from_colortransfer_when_no_rangetype(self, ct, key):
        s = {"Type": "Video"}
        if ct:
            s["ColorTransfer"] = ct
        out = detect_category("dynamic_range", [s])
        assert (out or {}).get("key") == key


class TestResolution:
    """K9: width decides first; height buckets are the fallback when width says nothing."""

    @pytest.mark.parametrize("w,h,key", [
        (3840, 2160, "res_4k"),
        (3840, 1600, "res_4k"),   # scope film is still 4K by width alone
        (2560, 1440, "res_1080p"),# K9 has no dedicated 1440p bucket -> next enabled rule down
        (1920, 1080, "res_1080p"),
        (1920, 800, "res_1080p"), # cropped/scope: width below every threshold -> height fallback
        (1280, 720, "res_720p"),
        (720, 576, "res_576p"),
        (720, 480, "res_480p"),
        (None, None, None),       # missing dims never satisfy a comparison
        (640, None, None),
    ])
    def test_buckets(self, w, h, key):
        s = {"Type": "Video"}
        if w is not None:
            s["Width"] = w
        if h is not None:
            s["Height"] = h
        out = detect_category("resolution", [s])
        assert (out or {}).get("key") == key

class TestAudioRules:
    @pytest.mark.parametrize("streams,key", [
        (_audio("mlpa", 8, title="Dolby TrueHD Atmos"), "truehd_atmos"),
        (_audio("eac3", 8, title="E-AC-3 7.1 Dolby Atmos"), "ddplus_atmos"),
        (_audio("dts", 6, profile="DTS:X"), "dtsx"),
        (_audio("dts", 8, profile="DTS-HD MA"), "dtshd_ma"),
        (_audio("truehd", 8, title="Dolby TrueHD 7.1"), "truehd"),
        (_audio("DD+ (E-AC-3)", 2, title="Dolby Digital Plus"), "ddplus"),
        (_audio("dts", 6, title="DTS 5.1"), "dts"),          # plain DTS stays DTS
        (_audio("ac3", 6, title="Dolby Digital 5.1"), "dd"),
        (_audio("ac3", 2), "dd"),                             # plain AC3 stereo still badges as DD
        (_audio("", 2), None),                           # unknown/absent codec -> free-text miss + catch-all disabled by default -> no badge
        ([], None),                                          # no usable audio stream at all
    ])
    def test_rule_selection(self, streams, key):
        out = detect_category("audio", streams)
        assert (out or {}).get("key") == key


class TestDetectAll:
    def test_all_categories_together(self):
        streams = [
            {"Type": "Video", "Width": 3840, "Height": 1600, "VideoRangeType": "DOVIWithHDR10Plus"},
            _audio("eac3", 8, title="E-AC-3 Atmos")[0],
        ]
        out = detect_all(streams)
        assert out["dynamic_range"]["key"] == "dovi_hdr10plus"
        assert out["resolution"]["key"] == "res_4k"
        assert out["audio"]["key"] == "ddplus_atmos"

    def test_unknown_category_raises_valueerror(self):
        with pytest.raises(ValueError):
            detect_category("bogus_cat", [])


class TestRuleBehaviour:
    def test_disabled_rule_is_inert(self):
        rules = [{"key": "off", "match": "VideoRangeType=SDR", "label": "X",
                  "image": "", "priority": 1, "enabled": False},
                 {"key": "hdr10x", "match": "VideoRangeType=HDR10", "label": "HDR10X",
                  "image": "", "priority": 5, "enabled": True}]
        out = detect_category("dynamic_range", _video(VideoRangeType="HDR10"), rules)
        assert (out or {}).get("key") == "hdr10x"

    def test_malformed_condition_never_matches_instead_of_crashing(self):
        # unparseable match expression is inert for that rule; no exception escapes.
        bad = [{"key": "bad", "match": "width>=oops", "label": "B", "image": "",
                "priority": 1, "enabled": True}]
        streams = [dict(_video()[0], Width=4000)]
        assert detect_category("resolution", streams, bad) is None

    def test_rulermatcherror_is_exception_subclass(self):
        assert issubclass(RuleMatchError, Exception)

    def test_default_rules_are_fresh_copies_each_call(self):
        a, b = default_format_rules(), default_format_rules()
        a["audio"][0]["label"] = "MUTATED"
        assert b["audio"][0]["label"] != "MUTATED"



class TestOverlayValuesWiring:
    """S9c10b step 1: overlay_values routes resolution/hdr/audio through K9 rules.

    No match -> None (element omitted). Uses raw Jellyfin-key dicts AND
    MediaStream objects to prove the adapter handles both shapes."""

    def test_dv_item_gets_dv_badge(self):
        from finmeta.engine.badges import overlay_values
        streams = [
            {"Type": "Video", "Width": 3840, "Height": 2160,
             "VideoRangeType": "DOVIWithHDR10"},
            {"Type": "Audio", "Codec": "truehd", "Channels": 8},
        ]
        v = overlay_values(streams, vote_average=None)
        assert v["hdr"] == "DV+HDR10"          # dynamic_range rule fired
        assert v["resolution"] == "4K"

    def test_sdr_item_gets_no_range_badge_but_resolution_still(self):
        from finmeta.engine.badges import overlay_values
        streams = [
            {"Type": "Video", "Width": 1920, "Height": 1080,
             "VideoRangeType": "SDR"},
            {"Type": "Audio", "Codec": "aac", "Channels": 2},
        ]
        v = overlay_values(streams, vote_average=None)
        assert v["hdr"] is None                 # SDR -> no dynamic-range badge (K9)
        assert v["resolution"] == "1080p"       # resolution still detected by width

    def test_3840x1600_scope_is_4k_width_first(self):
        from finmeta.engine.badges import overlay_values
        streams = [{"Type": "Video", "Width": 3840, "Height": 1600}]
        v = overlay_values(streams, vote_average=7.5)
        assert v["resolution"] == "4K"          # width>=3200 wins over height bucket
        assert v["rating"].endswith("7.5")      # rating path unchanged

    def test_media_stream_objects_via_adapter(self):
        from finmeta.engine.badges import overlay_values
        from finmeta.jellyfin_client.types import MediaStream
        streams = [MediaStream(index=0, type="Video", width=3840, height=2160,
                               video_range_type="DOVI"),
                   MediaStream(index=1, type="Audio", codec="eac3", channels=6)]
        v = overlay_values(streams, vote_average=None)
        assert v["hdr"] == "DV+HDR10" or v["hdr"] in ("DV", "DV+HDR10", "DV+SDR")
        assert v["resolution"] == "4K"


class TestItemFormatCounts:
    """S9c10b step 2 helper: per-item category -> winning rule key."""

    def test_counts_map_categories_to_keys_or_empty(self):
        from finmeta.engine.plan_builder import _item_format_counts
        dv = [{"Type": "Video", "Width": 3840, "Height": 2160,
               "VideoRangeType": "DOVIWithHDR10"},
              {"Type": "Audio", "Codec": "truehd", "Channels": 8}]
        fc = _item_format_counts(dv, None)
        assert fc["dynamic_range"] == "dovi_hdr10"
        assert fc["resolution"] == "res_4k"

    def test_sdr_has_empty_dynamic_range_key(self):
        from finmeta.engine.plan_builder import _item_format_counts
        sdr = [{"Type": "Video", "Width": 1920, "Height": 1080,
                "VideoRangeType": "SDR"}]
        fc = _item_format_counts(sdr, None)
        assert fc["dynamic_range"] == ""          # no badge for SDR (K9)
        assert fc["resolution"] == "res_1080p"


class TestAggregateFormatCounts:
    """S9c10b step 2: merge per-poster format_counts into item-level counts."""

    def test_merge_first_nonempty_wins_and_skips_other_kinds(self):
        from finmeta.engine.plan import FileWrite
        from finmeta.engine.plan_builder import _aggregate_format_counts
        poster = FileWrite(target_path="/x/poster.jpg", kind="poster",
                           source_ref="r", content_sha256="s",
                           meta={"format_counts": {"dynamic_range": "hdr10",
                                                   "resolution": "res_4k"}})
        backdrop = FileWrite(target_path="/x/backdrop.jpg", kind="backdrop",
                             source_ref="r", content_sha256="t")
        out = _aggregate_format_counts([backdrop, poster])
        assert out == {"dynamic_range": "hdr10", "resolution": "res_4k"}



class TestFormatCountsSummary:
    """S9c10b step 2: per-rule-key counts shown in the plan run log."""

    def _plan(self, items):
        from finmeta.engine.plan import ItemPlan, WritePlan
        ips = [
            ItemPlan(item_id=f"i{n}", item_name="m", media_type="movie",
                     format_counts=dict(fc))
            for n, fc in enumerate(items)
        ]
        return WritePlan(items=ips)

    def test_tallies_per_rule_key_and_none(self):
        from finmeta.api.run_service import _format_counts_summary
        plan = self._plan([
            {"dynamic_range": "dovi_hdr10", "resolution": "res_4k"},
            {"dynamic_range": "hdr10",       "resolution": "res_4k"},
            {"dynamic_range": "",            "resolution": ""},   # SDR + no res badge
        ])
        s = _format_counts_summary(plan)
        assert "dynamic_range:" in s and "resolution:" in s
        assert "dovi_hdr10=1" in s and "hdr10=1" in s and "none=1" in s
        assert "res_4k=2" in s

    def test_empty_when_no_facts(self):
        from finmeta.api.run_service import _format_counts_summary
        assert _format_counts_summary(self._plan([])) == ""
