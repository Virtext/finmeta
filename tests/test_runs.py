"""Tests for run backup / manifest / undo (SPEC \u00a710, override O5).

Verifies the backup\u2192write\u2192undo roundtrip restores identical bytes on a temp
dir, that creates are deleted on undo, and that undo is idempotent.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from finmeta.engine.runs import (
    Manifest, RunFile, backup, find_original_backup, load_manifest, record, undo,
)


@pytest.fixture
def cfg_dir(tmp_path: Path) -> Path:
    """A throwaway config dir; media lives alongside it under tmp_path."""
    return tmp_path / "config"


@pytest.fixture
def media(tmp_path: Path) -> Path:
    d = tmp_path / "library" / "Movies" / "The Matrix"
    d.mkdir(parents=True)
    return d


class TestBackup:
    def test_backup_existing(self, cfg_dir: Path, media: Path):
        p = media / "poster.jpg"
        original = b"ORIGINAL BYTES"
        p.write_bytes(original)
        rel = backup("run1", p, config_dir=cfg_dir)
        assert rel == "library/Movies/The Matrix/poster.jpg"
        bk = cfg_dir / "backups" / "run1" / rel
        assert bk.is_file()
        assert bk.read_bytes() == original

    def test_backup_missing_returns_none(self, cfg_dir: Path, media: Path):
        p = media / "does_not_exist.jpg"
        assert backup("run1", p, config_dir=cfg_dir) is None

    def test_find_original_backup_returns_the_oldest_not_the_newest(self, cfg_dir: Path, media: Path):
        """S9d4c: the OLDEST backup is the true pre-Finmeta original -- run2's
        backup is of run1's own (already-overlaid, for a poster) OUTPUT, not
        the original, so it must never win. Regression test for a real O7
        violation (new badges stacking on old ones every second-or-later
        apply) caused by this function previously returning the newest."""
        p = media / "poster.jpg"
        p.write_bytes(b"v1")  # the true original
        backup("run1", p, config_dir=cfg_dir)
        p.write_bytes(b"v2")  # Finmeta's own output from run1
        backup("run2", p, config_dir=cfg_dir)
        found = find_original_backup(p, config_dir=cfg_dir)
        assert found is not None
        assert found.read_bytes() == b"v1"

    def test_find_original_backup_none(self, cfg_dir: Path, media: Path):
        assert find_original_backup(media / "x.jpg", config_dir=cfg_dir) is None


class TestRecord:
    def test_record_and_load_roundtrip(self, cfg_dir: Path):
        m = Manifest(
            run_id="r1",
            files=[RunFile(path="/a/b.jpg", status="overwrite", backup_relpath="a/b.jpg")],
            api_actions=[{"action": "sync", "kind": "collection", "name": "X"}],
        )
        record(m, config_dir=cfg_dir)
        loaded = load_manifest("r1", config_dir=cfg_dir)
        assert loaded == m

    def test_load_missing_raises(self, cfg_dir: Path):
        with pytest.raises(FileNotFoundError):
            load_manifest("nope", config_dir=cfg_dir)


class TestUndoRoundtrip:
    def test_overwrite_restored_identical(self, cfg_dir: Path, media: Path):
        poster = media / "poster.jpg"
        original = b"\\x00\\x01 original poster bytes"
        poster.write_bytes(original)

        rel = backup("r1", poster, config_dir=cfg_dir)
        m = Manifest(run_id="r1", files=[
            RunFile(path=str(poster), status="overwrite", backup_relpath=rel),
        ])
        record(m, config_dir=cfg_dir)

        # Simulate apply: overwrite the poster.
        poster.write_bytes(b"OVERLAID poster bytes")

        res = undo("r1", config_dir=cfg_dir)
        assert res.restored == [str(poster)]
        assert res.deleted == []
        assert poster.read_bytes() == original

    def test_create_deleted(self, cfg_dir: Path, media: Path):
        nfo = media / "movie.nfo"
        m = Manifest(run_id="r1", files=[
            RunFile(path=str(nfo), status="create"),
        ])
        record(m, config_dir=cfg_dir)
        nfo.write_bytes(b"<movie/>")

        res = undo("r1", config_dir=cfg_dir)
        assert res.deleted == [str(nfo)]
        assert not nfo.exists()

    def test_full_roundtrip_mixed(self, cfg_dir: Path, media: Path):
        """backup \u2192 write (overwrite + create) \u2192 undo restores exactly."""
        poster = media / "poster.jpg"
        nfo = media / "movie.nfo"
        original = b"original poster"
        poster.write_bytes(original)

        rel = backup("r2", poster, config_dir=cfg_dir)
        m = Manifest(run_id="r2", files=[
            RunFile(path=str(poster), status="overwrite", backup_relpath=rel),
            RunFile(path=str(nfo), status="create"),
        ])
        record(m, config_dir=cfg_dir)

        # Apply writes.
        poster.write_bytes(b"overlaid")
        nfo.write_bytes(b"<movie/>")

        res = undo("r2", config_dir=cfg_dir)
        assert poster.read_bytes() == original
        assert not nfo.exists()
        assert sorted(res.restored) == [str(poster)]
        assert res.deleted == [str(nfo)]

    def test_undo_idempotent(self, cfg_dir: Path, media: Path):
        poster = media / "poster.jpg"
        original = b"orig"
        poster.write_bytes(original)
        rel = backup("r3", poster, config_dir=cfg_dir)
        record(Manifest(run_id="r3", files=[
            RunFile(path=str(poster), status="overwrite", backup_relpath=rel),
        ]), config_dir=cfg_dir)
        poster.write_bytes(b"changed")

        undo("r3", config_dir=cfg_dir)
        res2 = undo("r3", config_dir=cfg_dir)
        assert poster.read_bytes() == original
        # Second undo restores the same (persistent) backup but deletes nothing.
        assert res2.restored == [str(poster)]
        assert res2.deleted == []
