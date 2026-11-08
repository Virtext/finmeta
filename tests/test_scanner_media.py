"""SPEC_S10 SC3: resolution / HDR information on the video streams."""
from __future__ import annotations

from finmeta.engine.scanner import ScanSettings, check_media, scan_library

from _scan_helpers import FakeJellyfin, mapper_for, movie, movie_library, series


def _video(**kw):
    s = {"Type": "Video", "Codec": "hevc", "Width": 3840, "Height": 2160,
         "VideoRange": "HDR", "VideoRangeType": "HDR10"}
    s.update(kw)
    return s


def _audio():
    return {"Type": "Audio", "Codec": "eac3", "Channels": 6}


def _codes(out):
    return [f.check_code for f in out]


def test_a_healthy_4k_hdr_item_reports_nothing():
    it = movie("1", "Dune", path="/media/Movies/Dune (2021) 2160p HDR10.mkv")
    assert check_media([(it, [_video(), _audio()])], library="Movies") == []


def test_no_streams_at_all_is_an_error():
    it = movie("1", "Broken")
    out = check_media([(it, [])], library="Movies")
    assert _codes(out) == ["media.no_stream"]
    assert out[0].severity == "error"
    # only Jellyfin can probe the file; Finmeta cannot invent stream data
    assert out[0].fix_kind == "refresh_item"


def test_audio_only_streams_count_as_no_video():
    out = check_media([(movie("1", "Broken"), [_audio()])], library="Movies")
    assert _codes(out) == ["media.no_stream"]


def test_video_without_a_height():
    it = movie("1", "Unprobed", path="/media/Movies/Unprobed.mkv")
    out = check_media([(it, [_video(Height=None, VideoRange="", VideoRangeType="")])],
                      library="Movies")
    assert _codes(out) == ["media.missing_resolution"]
    assert out[0].severity == "warning"


def test_4k_with_no_dynamic_range_information():
    it = movie("1", "Mystery", path="/media/Movies/Mystery.mkv")
    out = check_media([(it, [_video(VideoRange="", VideoRangeType="")])],
                      library="Movies")
    assert _codes(out) == ["media.missing_hdr"]
    assert out[0].detail["height"] == 2160


def test_4k_sdr_is_not_missing_hdr():
    """`VideoRange: SDR` IS information -- the item is simply not HDR."""
    it = movie("1", "Plain", path="/media/Movies/Plain.mkv")
    out = check_media([(it, [_video(VideoRange="SDR", VideoRangeType="SDR")])],
                      library="Movies")
    assert out == []


def test_color_transfer_fallback_counts_as_hdr_information():
    """A stream with no VideoRangeType but smpte2084 transfer is PQ/HDR10."""
    it = movie("1", "Fallback", path="/media/Movies/Fallback.mkv")
    out = check_media([(it, [_video(VideoRange="", VideoRangeType="",
                                    ColorTransfer="smpte2084")])], library="Movies")
    assert out == []


def test_1080p_item_without_hdr_info_is_not_reported():
    """Below 4K, no dynamic-range information is normal."""
    it = movie("1", "Old", path="/media/Movies/Old 1080p.mkv")
    out = check_media([(it, [_video(Height=1080, Width=1920,
                                    VideoRange="", VideoRangeType="")])],
                      library="Movies")
    assert out == []


def test_filename_claims_4k_but_the_stream_is_1080p():
    it = movie("1", "Liar", path="/media/Movies/Liar 2160p/Liar 2160p.mkv")
    out = check_media([(it, [_video(Height=1080, Width=1920)])], library="Movies")
    assert _codes(out) == ["media.format_mismatch"]
    d = out[0].detail
    assert d["aspect"] == "resolution"
    assert d["claimed"] == "2160p"
    assert "1080p" in d["detected"] and "1080px" in d["detected"]


def test_filename_claims_1080p_but_the_stream_is_4k():
    """Both directions: a mislabelled file is worth knowing about either way."""
    it = movie("1", "Understated", path="/media/Movies/Understated 1080p.mkv")
    out = check_media([(it, [_video()])], library="Movies")
    assert _codes(out) == ["media.format_mismatch"]
    assert out[0].detail["claimed"] == "1080p"


def test_filename_claims_dolby_vision_the_stream_denies_it():
    it = movie("1", "Fake DV", path="/media/Movies/Fake DV 2160p DV.mkv")
    out = check_media([(it, [_video(VideoRangeType="HDR10")])], library="Movies")
    assert _codes(out) == ["media.format_mismatch"]
    assert out[0].detail == {
        "aspect": "dynamic_range", "claimed": "dv", "detected": "HDR10",
        "path": "/media/Movies/Fake DV 2160p DV.mkv",
    }


def test_dolby_vision_claim_satisfied_by_a_dovi_range_type():
    it = movie("1", "Real DV", path="/media/Movies/Real DV 2160p DV HDR10.mkv")
    out = check_media([(it, [_video(VideoRangeType="DOVIWithHDR10")])], library="Movies")
    assert out == []


def test_hdr_claim_denied_by_an_sdr_stream():
    it = movie("1", "Fake HDR", path="/media/Movies/Fake HDR 1080p HDR.mkv")
    out = check_media([(it, [_video(Height=1080, Width=1920, VideoRange="SDR",
                                    VideoRangeType="SDR")])], library="Movies")
    assert _codes(out) == ["media.format_mismatch"]
    assert out[0].detail["claimed"] == "hdr"


def test_a_quiet_filename_over_an_hdr_stream_is_not_a_finding():
    """Release names routinely omit HDR; that is not a defect."""
    it = movie("1", "Quiet", path="/media/Movies/Quiet.mkv")
    out = check_media([(it, [_video(VideoRangeType="DOVI")])], library="Movies")
    assert out == []


def test_dvdrip_does_not_read_as_dolby_vision():
    it = movie("1", "Old", path="/media/Movies/Old.DVDRip.480p.mkv")
    out = check_media([(it, [_video(Height=480, Width=720, VideoRange="SDR",
                                    VideoRangeType="SDR")])], library="Movies")
    assert out == []


def test_a_parent_folder_named_4k_does_not_make_every_file_claim_4k():
    it = movie("1", "Inherited", path="/media/4k-movies/Inherited/Inherited.mkv")
    out = check_media([(it, [_video(Height=1080, Width=1920, VideoRange="SDR",
                                    VideoRangeType="SDR")])], library="Movies")
    assert out == []


def test_low_resolution_is_opt_in():
    it = movie("1", "SD", path="/media/Movies/SD 480p.mkv")
    streams = [_video(Height=480, Width=720, VideoRange="SDR", VideoRangeType="SDR")]
    assert check_media([(it, streams)], library="Movies") == []
    out = check_media([(it, streams)], library="Movies", min_video_height=720)
    assert _codes(out) == ["media.low_resolution"]
    assert out[0].severity == "info"
    assert out[0].detail == {"height": 480, "floor": 720,
                             "path": "/media/Movies/SD 480p.mkv"}


# ---------------------------------------------------------------------------
# the library pass: one batched request, movies only, failure-tolerant
# ---------------------------------------------------------------------------

def _run(jf, tmp_path, **kw):
    emitted = []
    res = scan_library(jf, library="Movies", lib_cfg=movie_library(artwork={}),
                       mapper=mapper_for(tmp_path), emit=emitted.append, **kw)
    return res, [f for b in emitted for f in b]


def test_streams_are_fetched_once_per_page_for_movies_only(tmp_path):
    jf = FakeJellyfin(
        [movie("1", "A"), series("s1", "Show"), movie("2", "B")],
        streams={"1": [_video()], "2": [_video()]},
    )
    res, findings = _run(jf, tmp_path, page_size=10)
    assert jf.stream_calls == [["1", "2"]]      # one batch, no series
    assert findings == []
    assert res.items_scanned == 3


def test_a_series_is_never_reported_as_having_no_stream(tmp_path):
    """A series carries no streams of its own; standing in its first episode
    costs one request per series, which is a tier-2 price."""
    jf = FakeJellyfin([series("s1", "Show")])
    res, findings = _run(jf, tmp_path)
    assert jf.stream_calls == []
    assert findings == []


def test_a_failed_stream_batch_reports_nothing_rather_than_everything(tmp_path):
    jf = FakeJellyfin([movie("1", "A"), movie("2", "B")], fail_streams=True)
    res, findings = _run(jf, tmp_path)
    assert findings == []
    assert res.items_scanned == 2


def test_media_check_can_be_switched_off(tmp_path):
    jf = FakeJellyfin([movie("1", "A")], streams={"1": []})
    res, findings = _run(jf, tmp_path,
                         settings=ScanSettings(checks={"media": False}))
    assert jf.stream_calls == []                # off means no work, not just no rows
    assert findings == []


def test_media_findings_reach_the_emit_callback(tmp_path):
    jf = FakeJellyfin([movie("1", "A")], streams={"1": []})
    res, findings = _run(jf, tmp_path)
    assert [f.check_code for f in findings] == ["media.no_stream"]
    assert res.findings == 1
