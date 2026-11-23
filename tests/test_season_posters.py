"""Season posters: picked by hand per season, written plain.

2026-11-23 operator decision, replacing the 2026-11-06 behaviour (badges drawn
on every existing season poster): Finmeta never draws on a season poster and
never chooses one. It only writes the poster the operator picked for a season,
and a series has as many seasons as Jellyfin says it has.
"""
from __future__ import annotations

import hashlib
import io
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from PIL import Image

from finmeta.api.overrides import OverrideStore, apply_overrides, apply_skips
from finmeta.config.models import (
    Config, LocalSource, Metadata, OverlaysConfig, OverlayStyle, PathMapping, ServerConfig,
    SourcesConfig, TvArtwork, TvLibrary,
)
from finmeta.engine import plan_builder
from finmeta.engine.diff import annotate_status
from finmeta.engine.executor import apply_plan
from finmeta.engine.matching import MatchResult
from finmeta.engine.plan import WritePlan
from finmeta.engine.plan_builder import _build_tvshow_plan, jellyfin_seasons
from finmeta.jellyfin_client.mappings import PathMapper
from finmeta.jellyfin_client.types import Item
from finmeta.sources.local import LocalAssetSource


def _poster_bytes(color=(10, 20, 30), size=(400, 600)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="JPEG")
    return buf.getvalue()


def _season(number, sid=None):
    return Item(id=sid or f"s{number}", name=f"Season {number}", type="Season", index_number=number)


class _FakeJf:
    def __init__(self, seasons=(0, 1, 2)):
        self.seasons = seasons

    def get_item_media_streams_raw(self, item_id):
        return [{"Type": "Video", "Width": 1920, "Height": 1080}]

    def get_items(self, parent_id=None, recursive=None):
        return []

    def get_items_page(self, *, include_item_types=None, **kw):
        rows = [_season(n) for n in self.seasons] if include_item_types == ["Season"] else []
        return SimpleNamespace(items=rows)


class Show:
    """A show with season files on disk, plus what a build of it needs."""

    def __init__(self, tmp_path: Path, *, season_posters=True, seasons=(0, 1, 2),
                 season_files=("season01-poster.jpg", "season02-poster.jpg",
                               "season-specials-poster.jpg")):
        media_root = tmp_path / "media"
        self.dir = media_root / "Shows" / "Test Show"
        (self.dir / "Season 01").mkdir(parents=True)
        (self.dir / "Season 01" / "ep.mkv").write_bytes(b"video")
        for i, name in enumerate(season_files):
            (self.dir / name).write_bytes(_poster_bytes((40 + i, 60, 90)))
        assets = tmp_path / "assets"
        (assets / "220").mkdir(parents=True)
        (assets / "220" / "poster.jpg").write_bytes(_poster_bytes())
        self.pick_file = assets / "220" / "picked.jpg"
        self.pick_file.write_bytes(_poster_bytes((200, 10, 10)))
        self.cfg = Config(
            dry_run=False,
            server=ServerConfig(url="http://x:8096"),
            sources=SourcesConfig(local=LocalSource(enabled=True, path=str(assets))),
            path_mappings=[PathMapping(jellyfin=str(media_root), local=str(media_root))],
            libraries={"Shows": TvLibrary(
                type="tvshows", metadata=Metadata(enabled=False),
                artwork=TvArtwork(poster=True, season_posters=season_posters),
                overlays=["status"], overlay_profile="default")},
            overlays=OverlaysConfig(
                style=OverlayStyle(position="top_left", margin=20, font_size=60),
                profiles={"default": {"elements": ["status"]}}),
        )
        self.mapper = PathMapper.from_config([{"jellyfin": str(media_root), "local": str(media_root)}])
        self.item = Item(id="show220", name="Test Show", type="Series", path=str(self.dir),
                         provider_ids={"Tmdb": "220", "Tvdb": "111"})
        self.sources = {"local": LocalAssetSource(assets)}
        self.config_dir = tmp_path / "config"
        self.config_dir.mkdir()
        self.store = OverrideStore(self.config_dir / "overrides.db")
        self.jf = _FakeJf(seasons)

    def pick(self, season, ref=None):
        self.store.set("show220", f"season_poster:{season}", str(ref or self.pick_file), "local")

    def build(self):
        plan = WritePlan(items=[_build_tvshow_plan(
            self.item, MatchResult(item=self.item, tmdb_id=220, matched_by="provider_id"),
            self.cfg, self.cfg.libraries["Shows"], self.mapper, tmdb=None, jf=self.jf,
            sources=self.sources, language="en", lang_fallback=[None],
            config_dir=self.config_dir, sonarr_index={})])
        return annotate_status(plan)

    def season_files(self, plan):
        return [f for f in plan.items[0].files if f.kind == "season_poster"]

    def apply(self, run_id):
        jf = MagicMock()
        plan = self.build()
        result = apply_plan(plan, run_id, self.cfg, jf, MagicMock(), config_dir=self.config_dir,
                            db_path=self.config_dir / "jobs.db", poll_timeout=0.1)
        return result, [c.args[0] for c in jf.refresh_item.call_args_list]


class TestPlan:
    def test_nothing_is_planned_without_a_pick(self, tmp_path: Path):
        """Also with overlays on: existing season posters are never drawn on."""
        show = Show(tmp_path)
        plan = show.build()
        assert show.season_files(plan) == []
        assert len([f for f in plan.items[0].files if f.kind == "poster"]) == 1

    def test_a_pick_is_written_plain_and_only_for_its_season(self, tmp_path: Path):
        show = Show(tmp_path)
        show.pick(2)
        [fw] = show.season_files(show.build())
        assert Path(fw.target_path) == show.dir / "season02-poster.jpg"
        assert fw.content_sha256 == hashlib.sha256(show.pick_file.read_bytes()).hexdigest()
        assert fw.status == "update"
        assert fw.meta["refresh_item_id"] == "s2"

    def test_season_without_a_file_and_specials_and_two_digits(self, tmp_path: Path):
        show = Show(tmp_path, season_files=(), seasons=range(0, 15))
        for n in (0, 1, 14):
            show.pick(n)
        files = show.season_files(show.build())
        assert [Path(f.target_path).name for f in files] == [
            "season-specials-poster.jpg", "season01-poster.jpg", "season14-poster.jpg"]
        assert {f.status for f in files} == {"create"}

    def test_existing_png_is_the_target(self, tmp_path: Path):
        # Jellyfin prefers .png, so a .jpg next to it would never be shown
        show = Show(tmp_path, season_files=("season01-poster.png",))
        show.pick(1)
        [fw] = show.season_files(show.build())
        assert Path(fw.target_path).name == "season01-poster.png"

    def test_off_means_off(self, tmp_path: Path):
        show = Show(tmp_path, season_posters=False)
        show.pick(1)
        assert show.season_files(show.build()) == []

    def test_unknown_override_key_and_old_style_key_are_ignored(self, tmp_path: Path):
        show = Show(tmp_path)
        for kind in ("season_poster", "season_poster:x"):
            show.store.set("show220", kind, str(show.pick_file), "local")
        plan = apply_overrides(show.build(), {"show220": show.store.all_for("show220")},
                               allowed_roots=[tmp_path])
        assert show.season_files(plan) == []

    def test_skip_is_per_season(self, tmp_path: Path):
        show = Show(tmp_path)
        show.pick(1)
        show.pick(2)
        plan = apply_skips(show.build(), {"show220": ["season_poster:1"]})
        assert [f.meta["season"] for f in show.season_files(plan)] == [2]

    def test_applied_remote_pick_is_not_downloaded_again(self, tmp_path: Path, monkeypatch):
        show = Show(tmp_path)
        ref = "https://image.tmdb.org/t/p/original/s1.jpg"
        show.pick(1, ref)
        show.store.set_lock("show220", "season_poster:1", ref, "a" * 64)
        monkeypatch.setattr(plan_builder.candidate_ref, "fetch",
                            lambda *a, **k: pytest.fail("downloaded"))
        [fw] = show.season_files(show.build())
        assert fw.content_sha256 == "a" * 64


class TestApply:
    def test_pick_repick_and_reset(self, tmp_path: Path):
        show = Show(tmp_path)
        target = show.dir / "season01-poster.jpg"
        original = target.read_bytes()

        show.pick(1)
        result, refreshed = show.apply("run-100-aaaaaa")
        assert result.errors == []
        assert target.read_bytes() == show.pick_file.read_bytes()
        assert "s1" in refreshed, "Jellyfin must be told about the season itself"

        other = show.pick_file.with_name("other.jpg")
        other.write_bytes(_poster_bytes((5, 200, 5)))
        show.pick(1, other)
        show.apply("run-200-bbbbbb")
        assert target.read_bytes() == other.read_bytes()
        assert show.store.get_lock("show220", "season_poster:1")["ref"] == str(other)

        show.store.clear("show220", "season_poster:1")
        result, refreshed = show.apply("run-300-cccccc")
        assert target.read_bytes() == original
        assert "s1" in refreshed
        # Given back: from here on the file is not Finmeta's business.
        target.write_bytes(_poster_bytes((1, 1, 1)))
        assert show.season_files(show.build()) == []

    def test_second_apply_writes_nothing(self, tmp_path: Path):
        show = Show(tmp_path)
        show.pick(1)
        show.apply("run-100-aaaaaa")
        [fw] = show.season_files(show.build())
        assert fw.status == "unchanged"


def test_seasons_come_from_jellyfin_specials_last():
    jf = _FakeJf(seasons=(0, 3, 1, None, 2, 12))
    assert [s.index_number for s in jellyfin_seasons(jf, "x")] == [1, 2, 3, 12, 0]


class TestRoutes:
    @pytest.fixture
    def ctx(self, tmp_path, monkeypatch):
        from finmeta.api import routes_candidates as rc
        jf = MagicMock()
        jf.get_item.return_value = Item(id="show1", name="Example Show", type="Series")
        jf.get_items_page.side_effect = _FakeJf(seasons=(0, *range(1, 13))).get_items_page
        lib = SimpleNamespace(artwork=TvArtwork(season_posters=True))
        monkeypatch.setattr(rc, "_resolve_item_library", lambda *a: ("Shows", lib))
        monkeypatch.setattr(rc, "match_item", lambda *a: SimpleNamespace(tmdb_id=7))
        monkeypatch.setattr(rc, "_artwork_langs", lambda *a: ("en", [None]))
        tmdb = MagicMock()
        ctx = SimpleNamespace(
            config=SimpleNamespace(language="en", artwork_language_fallback=[]),
            make_jellyfin=lambda cfg=None: jf, make_tmdb=lambda cfg=None: tmdb,
            make_sources=lambda cfg=None: {}, overrides=OverrideStore(tmp_path / "overrides.db"),
            jf=jf, tmdb=tmdb, lib=lib)
        return ctx

    def test_every_season_is_listed_with_its_pick(self, ctx):
        from finmeta.api.routes_candidates import seasons
        ctx.overrides.set("show1", "season_poster:3", "/a/x.jpg", "local")
        rows = seasons("show1", ctx)["seasons"]
        assert [r["number"] for r in rows] == [*range(1, 13), 0]
        assert [r["number"] for r in rows if r["picked"]] == [3]

    def test_none_for_a_movie_or_with_the_setting_off(self, ctx):
        from finmeta.api.routes_candidates import seasons
        ctx.lib.artwork = TvArtwork(season_posters=False)
        assert seasons("show1", ctx) == {"seasons": []}
        ctx.jf.get_item.return_value = Item(id="m", name="Example Movie", type="Movie")
        assert seasons("m", ctx) == {"seasons": []}

    def test_candidates_are_for_that_season_and_mark_the_pick(self, ctx):
        from finmeta.api.routes_candidates import season_candidates
        from finmeta.sources.candidate import Candidate
        refs = [f"https://image.tmdb.org/t/p/original/{n}.jpg" for n in "ab"]
        ctx.tmdb.season_images.return_value = [
            Candidate(kind="poster", source="tmdb", ref=r, lang="en", score=1.0) for r in refs]
        ctx.overrides.set("show1", "season_poster:4", refs[1], "tmdb")
        out = season_candidates("show1", 4, ctx)["candidates"]
        assert ctx.tmdb.season_images.call_args.args == (7, 4)
        assert {c["kind"] for c in out} == {"season_poster:4"}
        assert [c["ref"] for c in out if c["selected"]] == [refs[1]]

    def test_a_season_tmdb_does_not_have_is_empty_not_an_error(self, ctx):
        from finmeta.api.routes_candidates import season_candidates
        from finmeta.sources.tmdb import TmdbError
        ctx.tmdb.season_images.side_effect = TmdbError("HTTP 404")
        assert season_candidates("show1", 9, ctx) == {"candidates": []}


def test_live_overlay_digests_count_season_posters(tmp_path: Path):
    """Plans saved before 2026-11-23 hold rendered season posters:
    ``prune_overlay_cache`` must not delete one while a plan still references it -- that would turn into an apply
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
