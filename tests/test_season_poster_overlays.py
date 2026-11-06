"""Season posters must carry the same overlay badges as the series poster.

Operator-reported 2026-11-06: "on movies the overlay always shows up in
Recently Added, on series and anime it only shows up sometimes -- but if I
open the library the file does have its overlay."

Cause: Finmeta only ever overlaid the SERIES poster. jellyfin-web's Latest
row builds its cards with ``preferParentPoster``, and for an Episode card it
prefers ``ParentPrimaryImageItemId`` -- the SEASON -- over ``SeriesId``. The
season posters are written by Sonarr/Jellyfin and Finmeta never touched
them, so a single newly added episode was drawn with an un-badged image
while the library view (series poster) looked right.

``_season_poster_overlays`` badges the season posters that already exist in
the show folder. Seasons with no poster of their own fall back to the series
poster in Jellyfin, which is overlaid already -- so either way the card has
badges. Fetching per-season artwork is a separate feature and deliberately
out of scope here.
"""
from __future__ import annotations

import hashlib
import io
from pathlib import Path

from PIL import Image

from finmeta.config.models import (
    Config, Metadata, OverlaysConfig, OverlayStyle, ServerConfig, TvArtwork, TvLibrary,
)
from finmeta.engine.matching import MatchResult
from finmeta.engine.plan_builder import _build_tvshow_plan
from finmeta.engine.runs import Manifest, RunFile, backup, record
from finmeta.jellyfin_client.mappings import PathMapper
from finmeta.jellyfin_client.types import Item
from finmeta.sources.local import LocalAssetSource


def _poster_bytes(color=(10, 20, 30), size=(400, 600)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="JPEG")
    return buf.getvalue()


class _FakeJf:
    def get_item_media_streams_raw(self, item_id):
        return [{"Type": "Video", "Width": 1920, "Height": 1080}]

    def get_items(self, parent_id=None, recursive=None):
        return []


def _fixture(tmp_path: Path, *, season_posters=True, elements=("status",),
             season_files=("season01-poster.jpg", "season02-poster.jpg",
                           "season-specials-poster.jpg")):
    media_root = tmp_path / "media"
    show_dir = media_root / "Shows" / "Test Show"
    (show_dir / "Season 01").mkdir(parents=True, exist_ok=True)
    (show_dir / "Season 01" / "ep.mkv").write_bytes(b"video")
    for i, name in enumerate(season_files):
        (show_dir / name).write_bytes(_poster_bytes((40 + i, 60, 90)))

    assets = tmp_path / "assets"
    (assets / "220").mkdir(parents=True, exist_ok=True)
    (assets / "220" / "poster.jpg").write_bytes(_poster_bytes())

    cfg = Config(
        dry_run=True,
        server=ServerConfig(url="http://x:8096"),
        libraries={"Shows": TvLibrary(
            type="tvshows",
            metadata=Metadata(enabled=False),
            artwork=TvArtwork(poster=True, season_posters=season_posters),
            overlays=list(elements),
            overlay_profile="default",
        )},
        overlays=OverlaysConfig(
            style=OverlayStyle(position="top_left", margin=20, font_size=60),
            profiles={"default": {"elements": list(elements)}},
        ),
    )
    mapper = PathMapper.from_config(
        [{"jellyfin": str(media_root), "local": str(media_root)}])
    item = Item(id="show220", name="Test Show", type="Series",
                path=str(show_dir), provider_ids={"Tmdb": "220", "Tvdb": "111"})
    match = MatchResult(item=item, tmdb_id=220, matched_by="provider_id")
    return (cfg, cfg.libraries["Shows"], mapper, item, match,
            {"local": LocalAssetSource(assets)}, show_dir)


def _build(tmp_path, config_dir, fixture):
    cfg, lib, mapper, item, match, sources, show_dir = fixture
    return _build_tvshow_plan(
        item, match, cfg, lib, mapper, tmdb=None, jf=_FakeJf(),
        sources=sources, language="en", lang_fallback=[None],
        config_dir=config_dir, sonarr_index={},
    )


class TestSeasonPosterOverlays:
    def test_every_existing_season_poster_gets_one(self, tmp_path: Path):
        config_dir = tmp_path / "config"
        fixture = _fixture(tmp_path)
        show_dir = fixture[-1]

        plan_item = _build(tmp_path, config_dir, fixture)

        season = [f for f in plan_item.files if f.kind == "season_poster"]
        assert {Path(f.target_path).name for f in season} == {
            "season01-poster.jpg", "season02-poster.jpg", "season-specials-poster.jpg",
        }
        # Each is written back over the file it was rendered from.
        for fw in season:
            assert Path(fw.target_path).parent == show_dir
            assert fw.source_ref == fw.target_path

    def test_rendered_bytes_differ_from_the_plain_file(self, tmp_path: Path):
        """A badge really is drawn -- not a copy of the untouched season art."""
        config_dir = tmp_path / "config"
        fixture = _fixture(tmp_path, season_files=("season01-poster.jpg",))
        show_dir = fixture[-1]
        plain = hashlib.sha256((show_dir / "season01-poster.jpg").read_bytes()).hexdigest()

        plan_item = _build(tmp_path, config_dir, fixture)

        fw = [f for f in plan_item.files if f.kind == "season_poster"][0]
        assert fw.content_sha256 != plain

    def test_rendered_bytes_are_cached_for_the_executor(self, tmp_path: Path):
        """The executor can only get overlaid bytes from the cache -- its
        source_ref is the un-overlaid original (see _fetch_content)."""
        config_dir = tmp_path / "config"
        plan_item = _build(tmp_path, config_dir,
                           _fixture(tmp_path, season_files=("season01-poster.jpg",)))
        fw = [f for f in plan_item.files if f.kind == "season_poster"][0]
        assert (config_dir / "cache" / "overlay" / f"{fw.content_sha256}.jpg").is_file()

    def test_season_posters_off_writes_none(self, tmp_path: Path):
        config_dir = tmp_path / "config"
        plan_item = _build(tmp_path, config_dir, _fixture(tmp_path, season_posters=False))
        assert [f for f in plan_item.files if f.kind == "season_poster"] == []

    def test_no_season_files_is_a_no_op(self, tmp_path: Path):
        """Jellyfin falls back to the (already overlaid) series poster."""
        config_dir = tmp_path / "config"
        plan_item = _build(tmp_path, config_dir, _fixture(tmp_path, season_files=()))
        assert [f for f in plan_item.files if f.kind == "season_poster"] == []
        assert [f for f in plan_item.files if f.kind == "poster"]

    def test_no_overlay_elements_writes_none(self, tmp_path: Path):
        """Nothing to draw -> the season files must be left alone entirely."""
        config_dir = tmp_path / "config"
        plan_item = _build(tmp_path, config_dir, _fixture(tmp_path, elements=()))
        assert [f for f in plan_item.files if f.kind == "season_poster"] == []

    def test_series_poster_is_still_built(self, tmp_path: Path):
        config_dir = tmp_path / "config"
        plan_item = _build(tmp_path, config_dir, _fixture(tmp_path))
        assert len([f for f in plan_item.files if f.kind == "poster"]) == 1

    def test_reapply_renders_from_the_original_backup_not_the_badged_file(
            self, tmp_path: Path):
        """O7 for season posters: the second run must composite onto the
        pre-Finmeta season art, never onto its own previous output."""
        config_dir = tmp_path / "config"
        fixture = _fixture(tmp_path, season_files=("season01-poster.jpg",))
        show_dir = fixture[-1]
        target = show_dir / "season01-poster.jpg"
        true_original = target.read_bytes()

        # Run 1 overwrote the pre-existing season art: backup + manifest.
        backup("run-100-aaaaaa", target, config_dir=config_dir)
        record(Manifest(run_id="run-100-aaaaaa", files=[
            RunFile(path=str(target), status="overwrite",
                    backup_relpath="x"),
        ]), config_dir=config_dir)
        target.write_bytes(_poster_bytes((99, 99, 99)))  # run 1's badged output

        plan_item = _build(tmp_path, config_dir, fixture)

        fw = [f for f in plan_item.files if f.kind == "season_poster"][0]
        assert fw.source_ref != str(target), "rendered from its own output"
        assert Path(fw.source_ref).read_bytes() == true_original


def test_live_overlay_digests_count_season_posters(tmp_path: Path):
    """``prune_overlay_cache`` must not delete a season poster's cached
    render while a plan still references it -- that would turn into an apply
    hash mismatch and a silently skipped write."""
    from finmeta.engine.overlay import _live_overlay_digests
    from finmeta.engine.plan import FileWrite, ItemPlan, WritePlan

    config_dir = tmp_path / "config"
    runs = config_dir / "runs"
    runs.mkdir(parents=True)
    plan = WritePlan(items=[ItemPlan(
        item_id="s1", item_name="Show", media_type="tvshow", tmdb_id=1,
        files=[FileWrite(target_path="/m/season01-poster.jpg", kind="season_poster",
                         source_ref="/m/season01-poster.jpg", content_sha256="b" * 64)],
    )])
    (runs / "plan-1.json").write_text(plan.to_json())

    assert "b" * 64 in _live_overlay_digests(config_dir)
