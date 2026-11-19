"""Artwork already in the media folder under Jellyfin's own names, and
original-language-first artwork ranking ("112 Aina", 2026-11-19)."""
from __future__ import annotations

from finmeta.api.routes_candidates import _folder_candidates
from finmeta.engine.paths import existing_artwork
from finmeta.engine.plan_builder import _artwork_langs
from finmeta.engine.scanner import check_artwork, partition_by_path

from _scan_helpers import mapper_for, movie


def _touch(d, *names):
    d.mkdir(parents=True, exist_ok=True)
    for n in names:
        (d / n).write_bytes(b"x")


def test_existing_artwork_knows_jellyfin_names(tmp_path):
    _touch(tmp_path, "folder.jpg", "fanart.jpg", "backdrop2.jpg", "clearlogo.png",
           "season01-poster.jpg", "notes.txt", "fanart.nfo", "Thumb.JPG")
    names = lambda kind: [p.name for p in existing_artwork(tmp_path, kind)]  # noqa: E731
    assert names("poster") == ["folder.jpg"]
    assert names("backdrop") == ["backdrop2.jpg", "fanart.jpg"]
    assert names("logo") == ["clearlogo.png"]
    assert names("landscape") == ["Thumb.JPG"]
    assert names("season_poster") == []
    assert existing_artwork(tmp_path / "missing", "poster") == []


def test_scan_counts_fanart_jpg_as_a_backdrop_file(tmp_path):
    _touch(tmp_path / "Movies" / "Alpha", "fanart.jpg")
    mapped, _ = partition_by_path([movie("1", "Alpha", backdrops=["t"])], mapper_for(tmp_path))
    assert check_artwork(mapped, library="Movies", kinds=["backdrop"]) == []
    # file but Jellyfin has none: stale (refresh), not "missing"
    mapped, _ = partition_by_path([movie("1", "Alpha")], mapper_for(tmp_path))
    out = check_artwork(mapped, library="Movies", kinds=["backdrop"])
    assert [f.check_code for f in out] == ["artwork.stale_jellyfin.backdrop"]


def test_folder_candidates_skip_finmetas_own_output(tmp_path):
    _touch(tmp_path, "poster.jpg", "folder.jpg")
    cands = _folder_candidates(tmp_path, "poster")
    assert [(c.source, c.ref) for c in cands] == [("folder", str(tmp_path / "folder.jpg"))]


class _Tmdb:
    def __init__(self, details=None, boom=False):
        self.details, self.boom, self.calls = details, boom, 0

    def tv_details(self, tmdb_id, language=None):
        self.calls += 1
        if self.boom:
            raise RuntimeError("down")
        return self.details

    movie_details = tv_details


def test_original_language_ranks_first():
    t = _Tmdb({"original_language": "sv"})
    assert _artwork_langs(t, "tv", 1, None, "en-US", ["en", None]) == ("sv", ["en-US", "en", None])
    # already-fetched details are reused, no second request
    t2 = _Tmdb()
    assert _artwork_langs(t2, "movie", 1, {"original_language": "fi"}, "en-US", None) == ("fi", ["en-US"])
    assert t2.calls == 0


def test_no_original_language_keeps_configured_order():
    assert _artwork_langs(_Tmdb({}), "tv", 1, None, "en-US", ["en"]) == ("en-US", ["en"])
    assert _artwork_langs(_Tmdb(boom=True), "tv", 1, None, "en-US", ["en"]) == ("en-US", ["en"])
