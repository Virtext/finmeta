"""S12d: per-library statistics gathered by the health scan."""
from __future__ import annotations

from finmeta.engine import scan_store
from finmeta.engine.format_rules import detect_all
from finmeta.engine.scanner import ScanSettings, scan_library

from _scan_helpers import FakeJellyfin, mapper_for, movie, movie_library, series

UHD_HDR10 = {"Type": "Video", "Codec": "hevc", "Width": 3840, "Height": 2160,
             "VideoRange": "HDR", "VideoRangeType": "HDR10"}
SURROUND = {"Type": "Audio", "Codec": "eac3", "Channels": 6}


def _run(jf, tmp_path, **kw):
    return scan_library(jf, library="Movies", lib_cfg=movie_library(artwork={}),
                        mapper=mapper_for(tmp_path), emit=lambda _b: None, **kw)


def test_breakdowns_match_the_badge_rules_and_cover_every_item(tmp_path):
    jf = FakeJellyfin(
        [movie("1", "A", genres=["Horror", "Drama"], production_year=1999, official_rating="R"),
         movie("2", "B", genres=[], production_year=None, official_rating=None),
         series("s1", "Show")],
        streams={"1": [UHD_HDR10, SURROUND], "2": []},
    )
    stats = _run(jf, tmp_path).stats
    badge = detect_all([UHD_HDR10, SURROUND])  # the chart label IS the badge label
    assert stats["resolution"] == {badge["resolution"]["label"]: 1, "No stream data": 1}
    assert stats["dynamic_range"] == {badge["dynamic_range"]["label"]: 1}
    assert stats["audio"] == {badge["audio"]["label"]: 1}
    assert stats["type"] == {"Movie": 2, "Show": 1}
    assert stats["genre"]["Horror"] == 1 and stats["genre"]["(none)"] == 1
    # the series helper defaults to 1999 / rating "15" / genre Action
    assert stats["decade"] == {"1990s": 2, "Unknown": 1}
    assert stats["official_rating"] == {"R": 1, "Unrated": 1, "15": 1}


def test_media_off_means_no_stream_charts_but_metadata_charts_remain(tmp_path):
    jf = FakeJellyfin([movie("1", "A", genres=["Drama"])], streams={"1": [UHD_HDR10]})
    stats = _run(jf, tmp_path, settings=ScanSettings(checks={"media": False})).stats
    assert jf.stream_calls == []
    assert "resolution" not in stats and stats["genre"] == {"Drama": 1}


def test_store_roundtrip_and_cleanup(tmp_path):
    db = tmp_path / "jobs.db"
    scan_store.start_scan(db, "s1")
    scan_store.save_scan_stats(db, "s1", "Movies", {"genre": {"Drama": 2}})
    assert scan_store.get_scan_stats(db, "s1") == {"Movies": {"genre": {"Drama": 2}}}
    scan_store.delete_scan(db, "s1")
    assert scan_store.get_scan_stats(db, "s1") == {}
