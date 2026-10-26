"""A TV show folder whose own name contains a '.' (e.g. "A.B. Example Files",
"S.H.I.E.L.D.") must resolve to itself, not two parents up.

``Path.suffix`` has no concept of file vs. directory -- it just looks for the
last '.' in the final path component, so a real existing show directory with
a dot in its name used to be misread as "this is a file inside a season
folder", climbing two parent levels and writing posters/NFOs into the TOP of
the whole media share instead of the show's own folder. Reported live
2026-10-25 against a real show.
"""
from __future__ import annotations

from pathlib import Path

from finmeta.engine.plan_builder import _show_dir
from finmeta.jellyfin_client.mappings import PathMapper
from finmeta.jellyfin_client.types import Item


def _mapper(tmp_path: Path) -> PathMapper:
    return PathMapper.from_config([{"jellyfin": "/data/tv", "local": str(tmp_path)}])


def test_dotted_show_folder_resolves_to_itself(tmp_path: Path):
    show_dir = tmp_path / "A.B. Example Files {tmdb-10003}"
    show_dir.mkdir()
    item = Item(id="s1", name="A.B. Example Files", type="Series",
               path="/data/tv/A.B. Example Files {tmdb-10003}")
    assert _show_dir(item, _mapper(tmp_path)) == show_dir


def test_plain_show_folder_still_resolves_to_itself(tmp_path: Path):
    """Contrast: a dot-free name already worked -- must keep working."""
    show_dir = tmp_path / "Breaking Bad {tmdb-1396}"
    show_dir.mkdir()
    item = Item(id="s2", name="Breaking Bad", type="Series",
               path="/data/tv/Breaking Bad {tmdb-1396}")
    assert _show_dir(item, _mapper(tmp_path)) == show_dir


def test_episode_file_path_still_climbs_two_levels(tmp_path: Path):
    """Contrast: when Jellyfin reports an episode FILE as the series path
    (no show dir discoverable another way), the old climb-up-two behavior
    for an actual file must be unchanged."""
    show_dir = tmp_path / "Some Show"
    season_dir = show_dir / "Season 01"
    season_dir.mkdir(parents=True)
    ep = season_dir / "Some Show - S01E01.mkv"
    ep.write_bytes(b"x")
    item = Item(id="s3", name="Some Show", type="Series",
               path="/data/tv/Some Show/Season 01/Some Show - S01E01.mkv")
    assert _show_dir(item, _mapper(tmp_path)) == show_dir
