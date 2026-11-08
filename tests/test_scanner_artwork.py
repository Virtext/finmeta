"""SPEC_S10 SC1: missing / stale / orphan artwork."""
from __future__ import annotations


from finmeta.engine.scanner import (
    check_artwork, enabled_artwork_kinds, partition_by_path, scan_library,
)

from _scan_helpers import (
    FakeJellyfin, mapper_for, movie, movie_library, series, tv_library,
)


def _codes(findings):
    return sorted(f.check_code for f in findings)


def _by_code(findings):
    return {f.check_code: f for f in findings}


def test_enabled_kinds_follow_the_library_config():
    assert enabled_artwork_kinds(movie_library(artwork={"poster": True})) == ["poster"]
    assert enabled_artwork_kinds(
        movie_library(artwork={"poster": True, "backdrop": True, "logo": True,
                               "landscape": True})
    ) == ["poster", "backdrop", "logo", "landscape"]
    # TV has no landscape flag at all -- must not blow up, must not report it
    assert enabled_artwork_kinds(
        tv_library(artwork={"poster": True, "logo": True})) == ["poster", "logo"]
    assert enabled_artwork_kinds(movie_library(artwork={})) == []


def test_missing_on_both_sides_is_reported_per_kind(tmp_path):
    (tmp_path / "Movies" / "Alpha").mkdir(parents=True)
    it = movie("1", "Alpha")
    mapped, unmapped = partition_by_path([it], mapper_for(tmp_path))
    assert unmapped == []
    out = check_artwork(mapped, library="Movies", kinds=["poster", "logo"])
    assert _codes(out) == ["artwork.missing.logo", "artwork.missing.poster"]
    by = _by_code(out)
    # a missing poster is visible everywhere in Jellyfin; a logo is not
    assert by["artwork.missing.poster"].severity == "error"
    assert by["artwork.missing.logo"].severity == "warning"
    assert all(f.fix_kind == "apply_item" for f in out)
    assert by["artwork.missing.poster"].detail["file"].endswith("Alpha/poster.jpg")
    assert by["artwork.missing.poster"].item_id == "1"
    assert by["artwork.missing.poster"].media_type == "movie"


def test_only_configured_kinds_are_checked(tmp_path):
    (tmp_path / "Movies" / "Alpha").mkdir(parents=True)
    mapped, _ = partition_by_path([movie("1", "Alpha")], mapper_for(tmp_path))
    assert check_artwork(mapped, library="Movies", kinds=[]) == []
    assert _codes(check_artwork(mapped, library="Movies", kinds=["backdrop"])) == [
        "artwork.missing.backdrop"]


def test_file_on_disk_but_no_jellyfin_image_is_stale(tmp_path):
    d = tmp_path / "Movies" / "Alpha"
    d.mkdir(parents=True)
    (d / "poster.jpg").write_bytes(b"x")
    mapped, _ = partition_by_path([movie("1", "Alpha")], mapper_for(tmp_path))
    out = check_artwork(mapped, library="Movies", kinds=["poster"])
    assert len(out) == 1
    assert out[0].check_code == "artwork.stale_jellyfin.poster"
    assert out[0].severity == "error"
    # only Jellyfin can fix this -- it has not rescanned the folder
    assert out[0].fix_kind == "refresh_item"
    assert out[0].detail["file_present"] is True
    assert out[0].detail["jellyfin_image"] is False


def test_jellyfin_image_but_no_file_is_an_orphan(tmp_path):
    (tmp_path / "Movies" / "Alpha").mkdir(parents=True)
    it = movie("1", "Alpha", image_tags={"Primary": "tag"})
    mapped, _ = partition_by_path([it], mapper_for(tmp_path))
    out = check_artwork(mapped, library="Movies", kinds=["poster"])
    assert len(out) == 1
    assert out[0].check_code == "artwork.orphan_jellyfin.poster"
    assert out[0].severity == "info"
    assert out[0].fix_kind == "apply_item"


def test_both_present_is_silent(tmp_path):
    d = tmp_path / "Movies" / "Alpha"
    d.mkdir(parents=True)
    for fname in ("poster.jpg", "logo.png", "backdrop.jpg", "landscape.jpg"):
        (d / fname).write_bytes(b"x")
    it = movie("1", "Alpha",
               image_tags={"Primary": "a", "Logo": "b", "Thumb": "c"},
               backdrops=["d"])
    mapped, _ = partition_by_path([it], mapper_for(tmp_path))
    assert check_artwork(mapped, library="Movies",
                         kinds=["poster", "backdrop", "logo", "landscape"]) == []


def test_backdrop_comes_from_backdrop_image_tags(tmp_path):
    """Jellyfin reports backdrops in their own list, not in ImageTags."""
    d = tmp_path / "Movies" / "Alpha"
    d.mkdir(parents=True)
    (d / "backdrop.jpg").write_bytes(b"x")
    mapped, _ = partition_by_path(
        [movie("1", "Alpha", backdrops=["tag"])], mapper_for(tmp_path))
    assert check_artwork(mapped, library="Movies", kinds=["backdrop"]) == []
    mapped, _ = partition_by_path(
        [movie("1", "Alpha", image_tags={"Primary": "tag"})], mapper_for(tmp_path))
    out = check_artwork(mapped, library="Movies", kinds=["backdrop"])
    assert [f.check_code for f in out] == ["artwork.stale_jellyfin.backdrop"]


def test_landscape_maps_to_jellyfins_thumb(tmp_path):
    d = tmp_path / "Movies" / "Alpha"
    d.mkdir(parents=True)
    (d / "landscape.jpg").write_bytes(b"x")
    mapped, _ = partition_by_path(
        [movie("1", "Alpha", image_tags={"Thumb": "tag"})], mapper_for(tmp_path))
    assert check_artwork(mapped, library="Movies", kinds=["landscape"]) == []


def test_series_artwork_is_looked_for_in_the_show_folder(tmp_path):
    show = tmp_path / "Shows" / "Beta"
    show.mkdir(parents=True)
    (show / "logo.png").write_bytes(b"x")
    it = series("s1", "Beta", image_tags={"Logo": "tag"})
    mapped, _ = partition_by_path([it], mapper_for(tmp_path))
    out = check_artwork(mapped, library="TV", kinds=["poster", "logo"])
    assert [f.check_code for f in out] == ["artwork.missing.poster"]
    assert out[0].media_type == "tvshow"
    assert out[0].detail["file"] == str(show / "poster.jpg")


def test_a_disabled_library_yields_nothing(tmp_path):
    """Same rule as the apply path: Finmeta reports only what it manages."""
    jf = FakeJellyfin([movie("1", "Alpha")])
    emitted = []
    res = scan_library(
        jf, library="Movies", lib_cfg=movie_library(enabled=False),
        mapper=mapper_for(tmp_path), emit=emitted.append)
    assert res.skipped_reason == "library disabled"
    assert (res.items_scanned, res.findings, emitted) == (0, 0, [])
    assert jf.calls == []       # not even a page was fetched
