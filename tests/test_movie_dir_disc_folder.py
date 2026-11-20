"""A disc-folder movie (BDMV/VIDEO_TS) has the movie FOLDER as its Jellyfin
path. ``movie_dir`` used to take its parent -- the library root -- so every
such movie wrote poster.jpg/movie.nfo into the top of the movies share
(found 2026-11-20 on the public server: three disc-folder movies).
"""
from __future__ import annotations

from pathlib import Path

from finmeta.engine.paths import movie_dir
from finmeta.jellyfin_client.mappings import PathMapper
from finmeta.jellyfin_client.types import Item


def _mapper(tmp_path: Path) -> PathMapper:
    return PathMapper.from_config([{"jellyfin": "/data/movies", "local": str(tmp_path)}])


def test_disc_folder_movie_resolves_to_its_own_folder(tmp_path: Path):
    folder = tmp_path / "Example Disc Movie (2021)"
    (folder / "BDMV").mkdir(parents=True)
    item = Item(id="m1", name="Example Disc Movie", type="Movie", path="/data/movies/Example Disc Movie (2021)")
    assert movie_dir(item, _mapper(tmp_path)) == folder


def test_file_movie_still_resolves_to_the_file_parent(tmp_path: Path):
    folder = tmp_path / "Example Heist (1995)"
    folder.mkdir()
    (folder / "Example Heist (1995).mkv").write_bytes(b"x")
    item = Item(id="m2", name="Example Heist", type="Movie", path="/data/movies/Example Heist (1995)/Example Heist (1995).mkv")
    assert movie_dir(item, _mapper(tmp_path)) == folder
