"""SPEC_S10 §D: the `scan:` config block — gating, severity floor, retention."""
from __future__ import annotations

import pytest
import yaml

from finmeta.config.models import Config, ScanConfig
from finmeta.engine.scanner import (
    CHECK_GROUPS, TIER2_GROUPS, ScanSettings, scan_library,
)

#: Groups a plain (tier-1) scan runs.
TIER1_GROUPS = [g for g in CHECK_GROUPS if g not in TIER2_GROUPS]
from finmeta.engine.scan_store import Finding

from _scan_helpers import FakeJellyfin, mapper_for, movie, movie_library


def _cfg(**scan) -> Config:
    return Config(server={"url": "http://x"}, scan=scan)


def test_defaults_are_everything_on_and_nothing_dropped():
    s = ScanSettings.from_config(_cfg())
    assert all(s.enabled(g) for g in TIER1_GROUPS)
    assert s.severity_floor == "info"
    assert s.min_artwork_width["poster"] == 1000
    assert s.min_artwork_bytes == 20480
    assert s.min_video_height == 0          # off: an old SD film is not a defect


def test_a_config_without_a_scan_block_still_works():
    """`scan:` is optional, and a pre-S10 config.yml must keep loading."""
    cfg = Config(**yaml.safe_load("server:\n  url: http://x\n"))
    s = ScanSettings.from_config(cfg)
    assert all(s.enabled(g) for g in TIER1_GROUPS)
    # and so must an object with no scan attribute at all
    assert ScanSettings.from_config(object()).enabled("artwork") is True


def test_unknown_scan_keys_are_rejected():
    with pytest.raises(Exception):
        ScanConfig(keep_scanz=3)


def test_check_groups_can_be_switched_off():
    s = ScanSettings.from_config(_cfg(checks={"duplicates": False}))
    assert s.enabled("duplicates") is False
    assert s.enabled("artwork") is True


def test_severity_floor_filters_findings():
    error = Finding(library="L", check_code="a.b", severity="error")
    warn = Finding(library="L", check_code="a.b", severity="warning")
    info = Finding(library="L", check_code="a.b", severity="info")
    keep_all = ScanSettings.from_config(_cfg(severity_floor="info"))
    assert [keep_all.keeps(f) for f in (error, warn, info)] == [True, True, True]
    warnings_up = ScanSettings.from_config(_cfg(severity_floor="warning"))
    assert [warnings_up.keeps(f) for f in (error, warn, info)] == [True, True, False]
    errors_only = ScanSettings.from_config(_cfg(severity_floor="error"))
    assert [errors_only.keeps(f) for f in (error, warn, info)] == [True, False, False]


def test_a_disabled_group_produces_no_findings(tmp_path):
    jf = FakeJellyfin([
        movie("1", "Alpha", provider_ids={"Tmdb": "603"}),
        movie("2", "Alpha copy", provider_ids={"Tmdb": "603"}),
    ])
    emitted = []
    res = scan_library(
        jf, library="Movies", lib_cfg=movie_library(), mapper=mapper_for(tmp_path),
        emit=emitted.append,
        settings=ScanSettings.from_config(_cfg(checks={"artwork": False})),
    )
    codes = [f.check_code for batch in emitted for f in batch]
    assert codes == ["duplicates.same_provider_id"]
    assert res.findings == 1                 # the count matches what was emitted


def test_the_floor_is_applied_before_emit_so_counts_agree(tmp_path):
    """An info finding below the floor is never stored AND never counted."""
    jf = FakeJellyfin([movie("1", "Alpha", media_source_count=2)])
    emitted = []
    res = scan_library(
        jf, library="Movies", lib_cfg=movie_library(artwork={}),
        mapper=mapper_for(tmp_path), emit=emitted.append,
        settings=ScanSettings.from_config(_cfg(severity_floor="warning")),
    )
    assert emitted == []                     # multi_version is info
    assert res.findings == 0


def test_path_unmapped_has_no_off_switch(tmp_path):
    """It blinds every other disk check, so it is always reported."""
    jf = FakeJellyfin([movie("1", "Alpha", path="/elsewhere/A/A.mkv")])
    emitted = []
    scan_library(
        jf, library="Movies", lib_cfg=movie_library(), mapper=mapper_for(tmp_path),
        emit=emitted.append,
        settings=ScanSettings.from_config(
            _cfg(checks={g: False for g in CHECK_GROUPS})),
    )
    assert [f.check_code for batch in emitted for f in batch] == ["path_unmapped"]


# ---------------------------------------------------------------------------
# S10c: tiers
# ---------------------------------------------------------------------------

def test_tier2_only_groups_are_off_in_a_plain_scan():
    """Their cost is a provider request per item or per series -- which is the
    whole reason the deep scan is a separate choice."""
    plain = ScanSettings.from_config(_cfg(), tier=1)
    deep = ScanSettings.from_config(_cfg(), tier=2)
    for group in TIER2_GROUPS:
        assert plain.enabled(group) is False
        assert deep.enabled(group) is True
    assert plain.deep is False and deep.deep is True


def test_a_tier2_group_switched_off_in_config_stays_off_in_a_deep_scan():
    deep = ScanSettings.from_config(_cfg(checks={"episodes": False}), tier=2)
    assert deep.enabled("episodes") is False


def test_tier1_groups_still_run_in_a_deep_scan():
    deep = ScanSettings.from_config(_cfg(), tier=2)
    assert all(deep.enabled(g) for g in TIER1_GROUPS)
