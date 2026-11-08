"""SPEC_S10 SC6: resolution and sanity of the artwork files on disk."""
from __future__ import annotations

import pytest

from finmeta.engine.scanner import (
    ScanSettings, check_artwork_quality, partition_by_path, scan_library,
)

from _scan_helpers import FakeJellyfin, mapper_for, movie, movie_library

PIL = pytest.importorskip("PIL.Image")

WIDTHS = {"poster": 1000, "backdrop": 1920, "logo": 400, "landscape": 1280}


def _png(path, width: int, height: int, *, pad: int = 0) -> None:
    """A real image of the given size (plus optional padding to pass min_bytes)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    img = PIL.new("RGB", (width, height), (90, 110, 130))
    img.save(path, format="JPEG" if path.suffix == ".jpg" else "PNG")
    if pad:
        with path.open("ab") as fh:
            fh.write(b"\x00" * pad)


def _mapped(tmp_path, item=None):
    mapped, _ = partition_by_path([item or movie("1", "Alpha")], mapper_for(tmp_path))
    return mapped


def _codes(out):
    return sorted(f.check_code for f in out)


def test_a_big_healthy_poster_reports_nothing(tmp_path):
    _png(tmp_path / "Movies" / "Alpha" / "poster.jpg", 2000, 3000, pad=30000)
    out = check_artwork_quality(_mapped(tmp_path), library="Movies",
                                kinds=["poster"], min_width=WIDTHS, min_bytes=20480)
    assert out == []


def test_a_small_poster_is_low_resolution(tmp_path):
    _png(tmp_path / "Movies" / "Alpha" / "poster.jpg", 500, 750, pad=30000)
    out = check_artwork_quality(_mapped(tmp_path), library="Movies",
                                kinds=["poster"], min_width=WIDTHS, min_bytes=20480)
    assert _codes(out) == ["artwork_quality.low_resolution.poster"]
    f = out[0]
    assert f.severity == "warning"
    # ranking already prefers resolution: a human has to pick something else
    assert f.fix_kind == "candidate_picker"
    assert (f.detail["width"], f.detail["height"], f.detail["floor"]) == (500, 750, 1000)


def test_each_kind_has_its_own_floor(tmp_path):
    d = tmp_path / "Movies" / "Alpha"
    _png(d / "poster.jpg", 1200, 1800, pad=30000)        # ok (>=1000)
    _png(d / "backdrop.jpg", 1200, 675, pad=30000)       # too narrow (<1920)
    _png(d / "logo.png", 600, 200, pad=30000)            # ok (>=400)
    out = check_artwork_quality(_mapped(tmp_path), library="Movies",
                                kinds=["poster", "backdrop", "logo"],
                                min_width=WIDTHS, min_bytes=20480)
    assert _codes(out) == ["artwork_quality.low_resolution.backdrop"]


def test_a_zero_floor_disables_the_check_for_that_kind(tmp_path):
    _png(tmp_path / "Movies" / "Alpha" / "poster.jpg", 200, 300, pad=30000)
    out = check_artwork_quality(_mapped(tmp_path), library="Movies",
                                kinds=["poster"], min_width={"poster": 0},
                                min_bytes=20480)
    assert out == []


def test_a_tiny_file_is_reported_and_short_circuits(tmp_path):
    """A 2 kB 'poster' is a placeholder; also calling it unreadable would be
    two rows for one problem."""
    p = tmp_path / "Movies" / "Alpha" / "poster.jpg"
    p.parent.mkdir(parents=True)
    p.write_bytes(b"not really an image")
    out = check_artwork_quality(_mapped(tmp_path), library="Movies",
                                kinds=["poster"], min_width=WIDTHS, min_bytes=20480)
    assert _codes(out) == ["artwork_quality.tiny_file.poster"]
    assert out[0].fix_kind == "apply_item"          # re-download it
    assert out[0].detail["bytes"] == len(b"not really an image")


def test_a_big_unreadable_file_is_an_error(tmp_path):
    p = tmp_path / "Movies" / "Alpha" / "poster.jpg"
    p.parent.mkdir(parents=True)
    p.write_bytes(b"\xff\xd8\xff" + b"\x00" * 40000)    # JPEG magic, then garbage
    out = check_artwork_quality(_mapped(tmp_path), library="Movies",
                                kinds=["poster"], min_width=WIDTHS, min_bytes=20480)
    assert _codes(out) == ["artwork_quality.unreadable.poster"]
    assert out[0].severity == "error"
    assert out[0].fix_kind == "apply_item"


def test_a_truncated_jpeg_is_unreadable(tmp_path):
    good = tmp_path / "Movies" / "Alpha" / "poster.jpg"
    _png(good, 1200, 1800, pad=30000)
    data = good.read_bytes()
    good.write_bytes(b"\x00" * 64 + data[64:])          # corrupt the header
    out = check_artwork_quality(_mapped(tmp_path), library="Movies",
                                kinds=["poster"], min_width=WIDTHS, min_bytes=20480)
    assert _codes(out) == ["artwork_quality.unreadable.poster"]


def test_a_missing_file_is_sc1s_business_not_this_checks(tmp_path):
    (tmp_path / "Movies" / "Alpha").mkdir(parents=True)
    out = check_artwork_quality(_mapped(tmp_path), library="Movies",
                                kinds=["poster"], min_width=WIDTHS, min_bytes=20480)
    assert out == []


def test_min_bytes_zero_disables_the_tiny_check(tmp_path):
    p = tmp_path / "Movies" / "Alpha" / "poster.jpg"
    p.parent.mkdir(parents=True)
    p.write_bytes(b"tiny")
    out = check_artwork_quality(_mapped(tmp_path), library="Movies",
                                kinds=["poster"], min_width=WIDTHS, min_bytes=0)
    # not "tiny" any more, but still unreadable -- which is the honest answer
    assert _codes(out) == ["artwork_quality.unreadable.poster"]


def test_the_library_pass_includes_quality_findings(tmp_path):
    _png(tmp_path / "Movies" / "Alpha" / "poster.jpg", 400, 600, pad=30000)
    jf = FakeJellyfin([movie("1", "Alpha", image_tags={"Primary": "tag"})])
    emitted = []
    res = scan_library(
        jf, library="Movies", lib_cfg=movie_library(artwork={"poster": True}),
        mapper=mapper_for(tmp_path), emit=emitted.append,
        settings=ScanSettings(min_artwork_width=WIDTHS, min_artwork_bytes=20480),
    )
    codes = sorted(f.check_code for b in emitted for f in b)
    assert codes == ["artwork_quality.low_resolution.poster"]
    assert res.findings == 1


def test_artwork_quality_can_be_switched_off(tmp_path):
    _png(tmp_path / "Movies" / "Alpha" / "poster.jpg", 400, 600, pad=30000)
    jf = FakeJellyfin([movie("1", "Alpha", image_tags={"Primary": "tag"})])
    emitted = []
    scan_library(
        jf, library="Movies", lib_cfg=movie_library(artwork={"poster": True}),
        mapper=mapper_for(tmp_path), emit=emitted.append,
        settings=ScanSettings(checks={"artwork_quality": False},
                              min_artwork_width=WIDTHS, min_artwork_bytes=20480),
    )
    assert emitted == []
