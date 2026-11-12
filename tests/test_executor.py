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
    DryRunRefused, _atomic_write, _nfo_bytes, apply_plan,
)
from finmeta.engine.jobs import (
    finish_run, get_job_log, get_run, init_db, list_runs, log, log_many, start_run,
)
from finmeta.engine.plan import ApiAction, FileWrite, ItemPlan, WritePlan
from finmeta.engine.runs import load_manifest, undo


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

    def test_log_many_inserts_every_entry_in_one_transaction(self, tmp_path: Path):
        """Audit 2026-11-11 "C4": log_many batches what used to be N
        separate log() calls into one INSERT+COMMIT, preserving each
        entry's own ts/level/message and their relative order."""
        db = tmp_path / "jobs.db"
        init_db(db)
        start_run(db, "r1", "")
        log_many(db, "r1", [
            ("2026-01-01T00:00:00Z", "info", "wrote create poster a.jpg"),
            ("2026-01-01T00:00:01Z", "info", "wrote create backdrop b.jpg"),
            ("2026-01-01T00:00:02Z", "warn", "something odd"),
        ])
        entries = get_job_log(db, "r1")
        assert [(e["ts"], e["level"], e["message"]) for e in entries] == [
            ("2026-01-01T00:00:00Z", "info", "wrote create poster a.jpg"),
            ("2026-01-01T00:00:01Z", "info", "wrote create backdrop b.jpg"),
            ("2026-01-01T00:00:02Z", "warn", "something odd"),
        ]

    def test_log_many_empty_is_a_noop(self, tmp_path: Path):
        db = tmp_path / "jobs.db"
        init_db(db)
        start_run(db, "r1", "")
        log_many(db, "r1", [])
        assert get_job_log(db, "r1") == []

    def test_log_many_interleaves_correctly_with_plain_log_calls(self, tmp_path: Path):
        db = tmp_path / "jobs.db"
        init_db(db)
        start_run(db, "r1", "")
        log(db, "r1", "info", "apply started")
        log_many(db, "r1", [("2026-01-01T00:00:00Z", "info", "wrote x")])
        log(db, "r1", "info", "apply finished")
        messages = [e["message"] for e in get_job_log(db, "r1")]
        assert messages == ["apply started", "wrote x", "apply finished"]


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

def _make_config(dry_run: bool = False, media_root: str = "/tmp/media"):
    """Minimal config for tests.

    ``media_root`` (audit 2026-11-11 "B6"): apply_plan now refuses to write
    outside a configured path_mappings local root, so every test whose
    FileWrite targets live under its own ``tmp_path`` must pass that same
    directory here -- the old hardcoded "/tmp/media" default only ever
    matched by coincidence (no containment check existed before this).
    """
    from finmeta.config.models import (
        Config, ServerConfig, MovieLibrary, Metadata, MovieArtwork,
        PathMapping, SourcesConfig, TmdbSource, LocalSource,
    )
    return Config(
        dry_run=dry_run,
        server=ServerConfig(url="http://test"),
        path_mappings=[PathMapping(jellyfin="/media", local=media_root)],
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

        cfg = _make_config(dry_run=False, media_root=str(tmp_path / "media"))
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

    def test_per_file_wrote_lines_all_land_despite_batching(self, tmp_path: Path):
        """Audit 2026-11-11 "C4": the "wrote ..." line for each FileWrite is
        now batched per item (one log_many transaction) instead of one
        log() call each -- this must not drop, duplicate, or misattribute
        any line. Two files on ONE item, two items total: 3 "wrote" lines
        expected overall, naming the right kind/path each."""
        assets = tmp_path / "assets"
        assets.mkdir()
        for name, data in (("poster.jpg", b"POSTER"), ("backdrop.jpg", b"BACKDROP")):
            (assets / name).write_bytes(data)

        def _fw(name: str, kind: str, data: bytes) -> FileWrite:
            return FileWrite(
                target_path=str(tmp_path / "media" / name), kind=kind,
                source_ref=str(assets / name),
                content_sha256=hashlib.sha256(data).hexdigest(),
            )

        plan = WritePlan(items=[
            ItemPlan(
                item_id="mov001", item_name="Movie One", media_type="movie",
                tmdb_id=1, matched_by="provider_id",
                files=[_fw("poster.jpg", "poster", b"POSTER"),
                       _fw("backdrop.jpg", "backdrop", b"BACKDROP")],
            ),
            ItemPlan(
                item_id="mov002", item_name="Movie Two", media_type="movie",
                tmdb_id=2, matched_by="provider_id",
                files=[FileWrite(
                    target_path=str(tmp_path / "media2" / "poster.jpg"),
                    kind="poster", source_ref=str(assets / "poster.jpg"),
                    content_sha256=hashlib.sha256(b"POSTER").hexdigest(),
                )],
            ),
        ])
        cfg = _make_config(dry_run=False, media_root=str(tmp_path))  # covers media/ and media2/
        jf, tmdb = MagicMock(), MagicMock()
        db = tmp_path / "jobs.db"
        cfg_dir = tmp_path / "config"
        cfg_dir.mkdir()

        result = apply_plan(plan, "r1", cfg, jf, tmdb,
                            config_dir=cfg_dir, db_path=db, poll_timeout=0.1)
        assert result.files_written == 3
        assert result.errors == []

        wrote_lines = [e["message"] for e in get_job_log(db, "r1")
                       if e["message"].startswith("wrote ")]
        assert len(wrote_lines) == 3
        assert sum("poster.jpg" in m for m in wrote_lines) == 2  # one per item
        assert sum("backdrop.jpg" in m for m in wrote_lines) == 1

    def test_api_action_recorded(self, tmp_path: Path):
        cfg = _make_config(dry_run=False, media_root=str(tmp_path / "media"))
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

        cfg = _make_config(dry_run=False, media_root=str(tmp_path / "media"))
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

    def test_write_outside_path_mappings_is_refused(self, tmp_path: Path):
        """Audit 2026-11-11 "B6": a FileWrite whose target_path falls
        outside every path_mappings local root must never be written --
        regardless of how it got there (a plan_builder bug like the real
        dotted-show-folder-name incident, or a stored candidate override).
        Simulated directly here with a target under a sibling directory
        the configured path_mappings local root does not cover at all."""
        assets = tmp_path / "assets"
        assets.mkdir()
        src = assets / "test.jpg"
        src.write_bytes(b"POSTER DATA")

        cfg = _make_config(dry_run=False, media_root=str(tmp_path / "media"))
        sha = hashlib.sha256(b"POSTER DATA").hexdigest()
        outside_target = tmp_path / "not-the-media-root" / "test.jpg"
        plan = WritePlan(items=[
            ItemPlan(
                item_id="mov001", item_name="Test Movie",
                media_type="movie", tmdb_id=123, matched_by="provider_id",
                files=[FileWrite(
                    target_path=str(outside_target),
                    kind="poster", source_ref=str(src), content_sha256=sha,
                )],
            ),
        ])
        jf, tmdb = MagicMock(), MagicMock()
        db = tmp_path / "jobs.db"
        cfg_dir = tmp_path / "config"
        cfg_dir.mkdir()

        result = apply_plan(plan, "r1", cfg, jf, tmdb,
                            config_dir=cfg_dir, db_path=db, poll_timeout=0.1)
        assert result.files_written == 0
        assert len(result.errors) == 1
        assert "outside every path_mappings local root" in result.errors[0]
        assert not outside_target.exists()

    def test_manifest_reflects_completed_writes_if_the_run_is_interrupted(
        self, tmp_path: Path, monkeypatch,
    ):
        """Audit 2026-11-11 "B2": the on-disk manifest must already name
        every file successfully written SO FAR even if the process stops
        (crash, OOM kill, container restart) partway through a multi-file
        apply -- not only once the whole run finishes. Simulated by raising
        from inside the per-file loop, AFTER the second file's write but
        with nothing to catch it, so the exception propagates straight out
        of apply_plan exactly like a real interrupt would: everything after
        this point in the function simply never ran.

        Regression for: before this fix, `record()` was called only once,
        after the entire loop -- an interrupted run like this one left a
        manifest-less `backups/<run_id>/` directory that `undo()` could not
        read at all, despite both backups being right there on disk.
        """
        import finmeta.engine.executor as executor_mod

        assets = tmp_path / "assets"
        assets.mkdir()
        for name, content in (("a.jpg", b"A-DATA"), ("b.jpg", b"B-DATA")):
            (assets / name).write_bytes(content)

        media_dir = tmp_path / "media"
        media_dir.mkdir()
        target_a = media_dir / "a.jpg"
        target_b = media_dir / "b.jpg"
        # Both are OVERWRITES (pre-existing on-disk originals), the
        # interesting case for undo.
        target_a.write_bytes(b"ORIGINAL-A")
        target_b.write_bytes(b"ORIGINAL-B")

        cfg = _make_config(dry_run=False, media_root=str(tmp_path / "media"))
        plan = WritePlan(items=[
            ItemPlan(
                item_id="mov001", item_name="Test Movie",
                media_type="movie", tmdb_id=123, matched_by="provider_id",
                files=[
                    FileWrite(target_path=str(target_a), kind="poster",
                             source_ref=str(assets / "a.jpg"),
                             content_sha256=hashlib.sha256(b"A-DATA").hexdigest()),
                    FileWrite(target_path=str(target_b), kind="backdrop",
                             source_ref=str(assets / "b.jpg"),
                             content_sha256=hashlib.sha256(b"B-DATA").hexdigest()),
                ],
            ),
        ])
        jf, tmdb = MagicMock(), MagicMock()
        db = tmp_path / "jobs.db"
        cfg_dir = tmp_path / "config"
        cfg_dir.mkdir()

        # engine.runs.backup() runs BEFORE the second file's own write +
        # manifest append + record() -- and, unlike content fetch or the
        # write itself, is NOT wrapped in a try/except in apply_plan's loop
        # -- so crashing here on the SECOND call lands exactly between the
        # first file's write (fully committed, including its manifest
        # entry) and the second file's write (never even attempted).
        real_backup = executor_mod.backup
        calls = {"n": 0}

        def _crash_on_second_file(run_id, path, *, config_dir):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("simulated crash: process dies right here")
            return real_backup(run_id, path, config_dir=config_dir)

        monkeypatch.setattr(executor_mod, "backup", _crash_on_second_file)

        with pytest.raises(RuntimeError, match="simulated crash"):
            apply_plan(plan, "r1", cfg, jf, tmdb,
                      config_dir=cfg_dir, db_path=db, poll_timeout=0.1)

        # The manifest on disk already names the FIRST file's write, even
        # though apply_plan never reached its own final record() call.
        m = load_manifest("r1", config_dir=cfg_dir)
        assert len(m.files) == 1
        assert m.files[0].path == str(target_a)
        assert target_a.read_bytes() == b"A-DATA"  # the write itself did complete

        # And undo() can use that partial manifest to restore what it knows about.
        res = undo("r1", config_dir=cfg_dir)
        assert res.incomplete is False  # a real (partial) manifest was found
        assert res.restored == [str(target_a)]
        assert target_a.read_bytes() == b"ORIGINAL-A"

    def test_undo_recovers_from_backups_with_no_manifest_at_all(self, tmp_path: Path):
        """Audit 2026-11-11 "B2": the even narrower case -- the process died
        before the manifest was ever written even once (between start_run
        and the first file's record() call). undo() must still reconstruct
        an "overwrite" restore from the raw backup tree instead of raising
        FileNotFoundError, and must say the recovery is incomplete."""
        from finmeta.engine.runs import backup

        media_dir = tmp_path / "media"
        media_dir.mkdir()
        target = media_dir / "poster.jpg"
        target.write_bytes(b"ORIGINAL")
        cfg_dir = tmp_path / "config"
        backup("r9", target, config_dir=cfg_dir)  # the backup exists...
        target.write_bytes(b"OVERWRITTEN")
        # ...but NO manifest was ever recorded for "r9" -- simulating a
        # crash before apply_plan's very first record() call.

        res = undo("r9", config_dir=cfg_dir)
        assert res.incomplete is True
        assert res.restored == [str(target)]
        assert target.read_bytes() == b"ORIGINAL"
        assert "no manifest" in res.unrecoverable_note


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
        cfg = _make_config(dry_run=False, media_root=str(tmp_path / "media"))
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
        cfg = _make_config(dry_run=False, media_root=str(tmp_path / "media"))
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
        cfg = _make_config(dry_run=False, media_root=str(tmp_path / "media"))
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
        cfg = _make_config(dry_run=False, media_root=str(tmp_path / "media"))
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



# ---------------------------------------------------------------------------
# _nfo_bytes picks the item's OWN library (audit 2026-11-11 "B4")
# ---------------------------------------------------------------------------

class TestNfoBytesPicksTheRightLibrary:
    """Before this fix, _nfo_bytes re-derived "the library" by scanning
    cfg.libraries for the first entry of the matching media type -- in any
    install with two movie (or two TV) libraries configuring different
    metadata.fields, every item in the library that ISN'T first in config
    order got the WRONG fields regenerated, which then disagreed with what
    plan_builder originally hashed and silently failed the apply's hash
    check (kind="metadata" takes the "never write that" branch)."""

    def _cfg(self) -> "Config":
        from finmeta.config.models import (
            Config, Metadata, MovieArtwork, MovieLibrary, ServerConfig,
        )
        return Config(
            dry_run=False,
            server=ServerConfig(url="http://test"),
            libraries={
                # "Filmer" is FIRST in config order -- the old bug always
                # picked this one's fields, regardless of which library the
                # item actually belongs to.
                "Filmer": MovieLibrary(
                    type="movies",
                    metadata=Metadata(enabled=True, fields=["title", "year"]),
                    artwork=MovieArtwork(poster=False),
                ),
                "Anime": MovieLibrary(
                    type="movies",
                    metadata=Metadata(
                        enabled=True,
                        fields=["title", "year", "overview", "genres"]),
                    artwork=MovieArtwork(poster=False),
                ),
            },
        )

    def _tmdb(self):
        tmdb = MagicMock()
        tmdb.movie_details.return_value = {
            "title": "Test Movie", "release_date": "2020-01-01",
            "overview": "A test overview.", "genres": [{"name": "Action"}],
        }
        return tmdb

    def test_item_in_the_second_library_gets_its_own_fields(self):
        cfg = self._cfg()
        tmdb = self._tmdb()
        item = ItemPlan(
            item_id="mov1", item_name="Test Movie", media_type="movie",
            tmdb_id=42, matched_by="provider_id", library="Anime",
        )
        nfo = _nfo_bytes(item, cfg, tmdb)
        # "Anime"'s fields include overview/genres -- "Filmer"'s (the
        # first-in-config library the old bug always picked) do not.
        assert b"<plot>" in nfo
        assert b"<genre>" in nfo

    def test_item_in_the_first_library_still_gets_its_own_fields(self):
        cfg = self._cfg()
        tmdb = self._tmdb()
        item = ItemPlan(
            item_id="mov2", item_name="Test Movie", media_type="movie",
            tmdb_id=42, matched_by="provider_id", library="Filmer",
        )
        nfo = _nfo_bytes(item, cfg, tmdb)
        assert b"<plot>" not in nfo
        assert b"<genre>" not in nfo

    def test_no_library_recorded_falls_back_to_first_matching_type(self):
        """An ItemPlan with no `library` set (an older cached plan.json, or
        a hand-built test ItemPlan) must not crash -- it degrades to the
        OLD best-effort guess, unchanged from before this fix."""
        cfg = self._cfg()
        tmdb = self._tmdb()
        item = ItemPlan(
            item_id="mov3", item_name="Test Movie", media_type="movie",
            tmdb_id=42, matched_by="provider_id",  # library left unset (None)
        )
        nfo = _nfo_bytes(item, cfg, tmdb)
        # "Filmer" is first in config order -- the old guess.
        assert b"<plot>" not in nfo
        assert b"<genre>" not in nfo
