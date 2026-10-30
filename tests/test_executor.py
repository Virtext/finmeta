"""Unit tests for the executor and jobs modules (S6).

Covers:
  - jobs: init_db, start_run, finish_run, log, list_runs
  - executor: DryRunRefused, atomic write, apply_plan (mocked)
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from finmeta.engine.executor import (
    DryRunRefused, _atomic_write, apply_plan,
)
from finmeta.engine.jobs import (
    finish_run, get_job_log, get_run, init_db, list_runs, log, start_run,
)
from finmeta.engine.plan import ApiAction, FileWrite, ItemPlan, WritePlan
from finmeta.engine.runs import load_manifest


# ---------------------------------------------------------------------------
# jobs
# ---------------------------------------------------------------------------

class TestJobs:
    def test_init_and_roundtrip(self, tmp_path: Path):
        db = tmp_path / "jobs.db"
        init_db(db)
        start_run(db, "r1", "abc123")
        log(db, "r1", "info", "started")
        log(db, "r1", "error", "something failed")
        finish_run(db, "r1", "ok", files_written=3, files_unchanged=1, api_calls=2)

        run = get_run(db, "r1")
        assert run is not None
        assert run["status"] == "ok"
        assert run["files_written"] == 3
        assert run["api_calls"] == 2
        assert run["plan_sha256"] == "abc123"

        entries = get_job_log(db, "r1")
        assert len(entries) == 2
        assert entries[0]["level"] == "info"
        assert entries[1]["level"] == "error"

        all_runs = list_runs(db)
        assert len(all_runs) == 1
        assert all_runs[0]["run_id"] == "r1"

    def test_start_run_twice_same_id_is_upsert(self, tmp_path: Path):
        """start_run is idempotent per run_id (apply worker + apply_plan both record)."""
        db = tmp_path / "jobs.db"
        init_db(db)
        start_run(db, "r1", "")            # worker records up front
        start_run(db, "r1", "sha123")      # apply_plan re-records with real sha
        run = get_run(db, "r1")
        assert run is not None
        assert run["status"] == "running"
        assert run["plan_sha256"] == "sha123"
        assert len(list_runs(db)) == 1

    def test_multiple_runs(self, tmp_path: Path):
        db = tmp_path / "jobs.db"
        init_db(db)
        start_run(db, "r1", "h1")
        finish_run(db, "r1", "ok")
        start_run(db, "r2", "h2")
        finish_run(db, "r2", "partial", files_written=1)

        runs = list_runs(db)
        assert len(runs) == 2
        # newest first
        assert runs[0]["run_id"] == "r2"


# ---------------------------------------------------------------------------
# atomic write
# ---------------------------------------------------------------------------

class TestAtomicWrite:
    def test_creates_file(self, tmp_path: Path):
        target = tmp_path / "sub" / "file.txt"
        _atomic_write(target, b"hello world")
        assert target.read_bytes() == b"hello world"
        assert oct(os.stat(target).st_mode & 0o777) == "0o644"

    def test_overwrites_existing(self, tmp_path: Path):
        target = tmp_path / "file.txt"
        target.write_bytes(b"old")
        _atomic_write(target, b"new")
        assert target.read_bytes() == b"new"

    def test_no_temp_left_behind(self, tmp_path: Path):
        target = tmp_path / "file.txt"
        _atomic_write(target, b"data")
        temps = [p for p in tmp_path.iterdir() if p.name.startswith(".finmeta_")]
        assert temps == []


# ---------------------------------------------------------------------------
# apply_plan (mocked)
# ---------------------------------------------------------------------------

def _make_config(dry_run: bool = False):
    """Minimal config for tests."""
    from finmeta.config.models import (
        Config, ServerConfig, MovieLibrary, Metadata, MovieArtwork,
        PathMapping, SourcesConfig, TmdbSource, LocalSource,
    )
    return Config(
        dry_run=dry_run,
        server=ServerConfig(url="http://test"),
        path_mappings=[PathMapping(jellyfin="/media", local="/tmp/media")],
        libraries={"Movies": MovieLibrary(
            type="movies",
            metadata=Metadata(enabled=True, fields=["title", "plot"]),
            artwork=MovieArtwork(poster=True, backdrop=False),
            theme_song=False,
        )},
        sources=SourcesConfig(
            tmdb=TmdbSource(enabled=True),
            local=LocalSource(enabled=True, path="/tmp/assets"),
        ),
    )


def _make_plan(content: bytes = b"test content") -> WritePlan:
    """A plan with one file write (local source)."""
    sha = hashlib.sha256(content).hexdigest()
    return WritePlan(items=[
        ItemPlan(
            item_id="mov001",
            item_name="Test Movie",
            media_type="movie",
            tmdb_id=123,
            matched_by="provider_id",
            files=[
                FileWrite(
                    target_path="/tmp/media/test.jpg",
                    kind="poster",
                    source_ref="/tmp/assets/test.jpg",
                    content_sha256=sha,
                ),
            ],
        ),
    ])


class TestApplyPlan:
    def test_dry_run_refused(self, tmp_path: Path):
        cfg = _make_config(dry_run=True)
        plan = _make_plan()
        jf = MagicMock()
        tmdb = MagicMock()
        db = tmp_path / "jobs.db"
        cfg_dir = tmp_path / "config"
        cfg_dir.mkdir()

        with pytest.raises(DryRunRefused, match="dry_run"):
            apply_plan(
                plan, "r1", cfg, jf, tmdb,
                config_dir=cfg_dir, db_path=db,
            )

    def test_writes_file_and_manifest(self, tmp_path: Path):
        # Set up source file
        assets = tmp_path / "assets"
        assets.mkdir()
        src = assets / "test.jpg"
        src.write_bytes(b"POSTER DATA")

        cfg = _make_config(dry_run=False)
        sha = hashlib.sha256(b"POSTER DATA").hexdigest()
        plan = WritePlan(items=[
            ItemPlan(
                item_id="mov001", item_name="Test Movie",
                media_type="movie", tmdb_id=123, matched_by="provider_id",
                files=[FileWrite(
                    target_path=str(tmp_path / "media" / "test.jpg"),
                    kind="poster",
                    source_ref=str(src),
                    content_sha256=sha,
                )],
            ),
        ])
        jf = MagicMock()
        tmdb = MagicMock()
        db = tmp_path / "jobs.db"
        cfg_dir = tmp_path / "config"
        cfg_dir.mkdir()

        result = apply_plan(
            plan, "r1", cfg, jf, tmdb,
            config_dir=cfg_dir, db_path=db, poll_timeout=0.1,
        )
        assert result.files_written == 1
        assert result.errors == []
        # File was written
        target = tmp_path / "media" / "test.jpg"
        assert target.read_bytes() == b"POSTER DATA"
        # Manifest exists
        m = load_manifest("r1", config_dir=cfg_dir)
        assert len(m.files) == 1
        assert m.files[0].status == "create"

    def test_api_action_recorded(self, tmp_path: Path):
        cfg = _make_config(dry_run=False)
        plan = WritePlan(
            items=[],
            api_actions=[
                ApiAction(action="create", kind="collection",
                          name="Test Coll", add_ids=["id1", "id2"]),
            ],
        )
        jf = MagicMock()
        jf.get_collection_by_name.return_value = None
        jf.create_collection.return_value = {}
        tmdb = MagicMock()
        db = tmp_path / "jobs.db"
        cfg_dir = tmp_path / "config"
        cfg_dir.mkdir()

        result = apply_plan(
            plan, "r1", cfg, jf, tmdb,
            config_dir=cfg_dir, db_path=db, poll_timeout=0.1,
        )
        assert result.api_calls == 1
        m = load_manifest("r1", config_dir=cfg_dir)
        assert len(m.api_actions) == 1
        assert m.api_actions[0]["name"] == "Test Coll"

    def test_hash_mismatch_logged(self, tmp_path: Path):
        assets = tmp_path / "assets"
        assets.mkdir()
        src = assets / "test.jpg"
        src.write_bytes(b"actual data")

        cfg = _make_config(dry_run=False)
        plan = WritePlan(items=[
            ItemPlan(
                item_id="mov001", item_name="Test Movie",
                media_type="movie", tmdb_id=123, matched_by="provider_id",
                files=[FileWrite(
                    target_path=str(tmp_path / "media" / "test.jpg"),
                    kind="poster",
                    source_ref=str(src),
                    content_sha256="deadbeef" * 8,  # wrong hash
                )],
            ),
        ])
        jf = MagicMock()
        tmdb = MagicMock()
        db = tmp_path / "jobs.db"
        cfg_dir = tmp_path / "config"
        cfg_dir.mkdir()

        result = apply_plan(
            plan, "r1", cfg, jf, tmdb,
            config_dir=cfg_dir, db_path=db, poll_timeout=0.1,
        )
        assert result.files_written == 0
        assert len(result.errors) == 1
        assert "hash mismatch" in result.errors[0]


# ---------------------------------------------------------------------------
# no-op apply (idempotent second apply, S6b)
# ---------------------------------------------------------------------------

class TestApplyNoop:
    def _noop_plan(self, tmp_path: Path) -> WritePlan:
        base = tmp_path / "media"
        base.mkdir(parents=True, exist_ok=True)
        (base / "poster.jpg").write_bytes(b"POSTER")
        (base / "theme.mp3").write_bytes(b"THEME")
        return WritePlan(items=[
            ItemPlan(
                item_id="mov001", item_name="T", media_type="movie",
                tmdb_id=1, matched_by="provider_id",
                files=[
                    FileWrite(target_path=str(base / "poster.jpg"), kind="poster",
                              source_ref="/nonexistent/p.jpg",
                              content_sha256=hashlib.sha256(b"POSTER").hexdigest(),
                              status="unchanged"),
                    FileWrite(target_path=str(base / "theme.mp3"), kind="theme_song",
                              source_ref="/nonexistent/t.mp3",
                              content_sha256=hashlib.sha256(b"THEME").hexdigest(),
                              status="unchanged"),
                ],
            ),
        ])

    def test_noop_writes_nothing(self, tmp_path: Path):
        """A no-op apply records a run with written=0, unchanged=N, empty manifest."""
        cfg = _make_config(dry_run=False)
        plan = self._noop_plan(tmp_path)
        jf, tmdb = MagicMock(), MagicMock()
        db, cfg_dir = tmp_path / "jobs.db", tmp_path / "config"
        cfg_dir.mkdir()
        result = apply_plan(plan, "r1", cfg, jf, tmdb,
                            config_dir=cfg_dir, db_path=db, poll_timeout=0.1)
        assert result.files_written == 0
        assert result.files_unchanged == 2
        assert result.errors == []
        m = load_manifest("r1", config_dir=cfg_dir)
        assert m.files == []  # empty manifest
        # undo of an empty-manifest run is a no-op (no error)
        from finmeta.engine.runs import undo
        assert undo("r1", config_dir=cfg_dir).restored == []

    def test_noop_does_not_refresh_or_poll(self, tmp_path: Path):
        """All-unchanged apply must not call refresh_item or enter the poll loop."""
        cfg = _make_config(dry_run=False)
        plan = self._noop_plan(tmp_path)
        jf, tmdb = MagicMock(), MagicMock()
        db, cfg_dir = tmp_path / "jobs.db", tmp_path / "config"
        cfg_dir.mkdir()
        apply_plan(plan, "r1", cfg, jf, tmdb,
                   config_dir=cfg_dir, db_path=db, poll_timeout=0.1)
        jf.refresh_item.assert_not_called()
        # _poll_refresh would call get_theme_songs / get_item repeatedly
        jf.get_theme_songs.assert_not_called()

    def test_written_image_item_still_refreshes(self, tmp_path: Path):
        """Contrast: an item with a poster actually written does refresh."""
        base = tmp_path / "media"
        base.mkdir(parents=True, exist_ok=True)
        assets = tmp_path / "assets"; assets.mkdir()
        src = assets / "p.jpg"; src.write_bytes(b"NEWPOSTER")
        cfg = _make_config(dry_run=False)
        plan = WritePlan(items=[
            ItemPlan(
                item_id="mov001", item_name="T", media_type="movie",
                tmdb_id=1, matched_by="provider_id",
                files=[FileWrite(target_path=str(base / "poster.jpg"), kind="poster",
                                 source_ref=str(src),
                                 content_sha256=hashlib.sha256(b"NEWPOSTER").hexdigest(),
                                 status="create")],
            ),
        ])
        jf, tmdb = MagicMock(), MagicMock()
        db, cfg_dir = tmp_path / "jobs.db", tmp_path / "config"
        cfg_dir.mkdir()
        result = apply_plan(plan, "r1", cfg, jf, tmdb,
                            config_dir=cfg_dir, db_path=db, poll_timeout=0.1)
        assert result.files_written == 1
        jf.refresh_item.assert_called_with("mov001")

    def test_poll_detects_a_synchronous_refresh_without_burning_the_timeout(self, tmp_path: Path):
        """Operator-reported (2026-10-25): every apply with a real image
        write took ~2 extra minutes with nothing visibly happening, even
        though the write itself had already succeeded.

        Jellyfin's single-item ``/Items/{id}/Refresh`` commonly completes
        SYNCHRONOUSLY -- the ImageTag is already updated by the time that
        call returns. The old code captured its "has the tag changed yet"
        baseline *inside* ``_poll_refresh``, which only runs AFTER that
        refresh call -- so the baseline it compared against was already the
        NEW tag, the tag could then never look like it "changed", and every
        apply burned the full ``poll_timeout`` regardless. This fake client
        mimics that synchronous-refresh behaviour (``refresh_item`` mutates
        the tag immediately); a correct implementation detects the already-
        changed tag on its very first check and returns right away."""
        import time as _time

        class _SyncRefreshJF:
            def __init__(self):
                self.tag = "old-tag"
                self.refresh_calls = 0

            def get_item(self, item_id):
                return SimpleNamespace(image_tags=self.tag)

            def refresh_item(self, item_id, **kwargs):
                self.refresh_calls += 1
                self.tag = "new-tag"   # Jellyfin already rescanned by the time this returns

            def get_theme_songs(self, item_id):
                return []

        base = tmp_path / "media"
        base.mkdir(parents=True, exist_ok=True)
        assets = tmp_path / "assets"; assets.mkdir()
        src = assets / "p.jpg"; src.write_bytes(b"NEWPOSTER")
        cfg = _make_config(dry_run=False)
        plan = WritePlan(items=[
            ItemPlan(
                item_id="mov001", item_name="T", media_type="movie",
                tmdb_id=1, matched_by="provider_id",
                files=[FileWrite(target_path=str(base / "poster.jpg"), kind="poster",
                                 source_ref=str(src),
                                 content_sha256=hashlib.sha256(b"NEWPOSTER").hexdigest(),
                                 status="create")],
            ),
        ])
        jf, tmdb = _SyncRefreshJF(), MagicMock()
        db, cfg_dir = tmp_path / "jobs.db", tmp_path / "config"
        cfg_dir.mkdir()

        started = _time.monotonic()
        result = apply_plan(plan, "r1", cfg, jf, tmdb,
                            config_dir=cfg_dir, db_path=db, poll_timeout=30.0)
        elapsed = _time.monotonic() - started

        assert result.files_written == 1
        assert jf.refresh_calls == 1
        assert elapsed < 5.0, f"poll took {elapsed:.1f}s -- baseline captured after the refresh again?"
        entries = get_job_log(db, "r1")
        assert not any("poll timeout" in e["message"] for e in entries), entries

