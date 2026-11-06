"""O7: a poster Finmeta CREATED must never be treated as its own original.

Operator-reported 2026-11-06 and confirmed on a real movie poster: after
changing the overlay setup, the new badges showed up composited on top of the
OLD ones. ``find_original_backup`` returns the oldest backup as "the
pre-Finmeta original" -- but ``backup()`` copies nothing when the file does
not exist yet, so for a path Finmeta created itself the oldest backup that
ever exists is the SECOND apply's copy of the FIRST apply's overlaid output.
Compositing onto that stacks badges, which is precisely what the
oldest-backup rule exists to prevent.

Real-world shape of the bug (traced through the live plan files):

  run 1  create poster.jpg from the TMDb candidate  -> no backup
  run 2  overlay settings change, overwrite         -> backs up run 1's OUTPUT
  run 3  renders "the original" = run 2's backup    -> old badges + new badges

Movie libraries hit this as the NORMAL case: Radarr writes ``folder.jpg``,
never ``poster.jpg``, so every ``poster.jpg`` is Finmeta-created. TV
libraries do not: Sonarr/Jellyfin already wrote ``poster.jpg``, so their
oldest backup is a genuine pre-Finmeta file.
"""
from __future__ import annotations

import hashlib
import io

from PIL import Image

from finmeta.config.models import OverlayStyle
from finmeta.engine.overlay import TextBadge, overlay_file_write
from finmeta.engine.runs import (
    Manifest, RunFile, backup, created_by_finmeta, find_original_backup, record,
)

STYLE = OverlayStyle(position="bottom_left", margin=10, font_size=20,
                     bg="#000000B0", fg="#FFFFFF", radius=4)


def _poster(color=(10, 20, 30)) -> bytes:
    img = Image.new("RGB", (200, 300), color)
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


def _manifest(run_id: str, path, status: str, config_dir, backup_relpath=None) -> None:
    record(Manifest(run_id=run_id, files=[
        RunFile(path=str(path), status=status, backup_relpath=backup_relpath),
    ]), config_dir=config_dir)


class TestCreatedByFinmeta:
    def test_create_in_the_oldest_run_is_detected(self, tmp_path):
        config_dir = tmp_path / "config"
        target = tmp_path / "media" / "poster.jpg"
        _manifest("run-100-aaaaaa", target, "create", config_dir)
        _manifest("run-200-bbbbbb", target, "overwrite", config_dir,
                  backup_relpath="media/poster.jpg")
        assert created_by_finmeta(target, config_dir=config_dir) is True

    def test_overwrite_in_the_oldest_run_is_not(self, tmp_path):
        config_dir = tmp_path / "config"
        target = tmp_path / "media" / "poster.jpg"
        _manifest("run-100-aaaaaa", target, "overwrite", config_dir,
                  backup_relpath="media/poster.jpg")
        _manifest("run-200-bbbbbb", target, "overwrite", config_dir,
                  backup_relpath="media/poster.jpg")
        assert created_by_finmeta(target, config_dir=config_dir) is False

    def test_run_order_comes_from_the_run_id_not_the_scan_order(self, tmp_path):
        """The LATER run is written first -- the index must still read the
        earlier run's 'create' as the first touch."""
        config_dir = tmp_path / "config"
        target = tmp_path / "media" / "poster.jpg"
        _manifest("run-900-zzzzzz", target, "overwrite", config_dir)
        _manifest("run-100-aaaaaa", target, "create", config_dir)
        assert created_by_finmeta(target, config_dir=config_dir) is True

    def test_unknown_path_is_false(self, tmp_path):
        config_dir = tmp_path / "config"
        _manifest("run-100-aaaaaa", tmp_path / "media" / "other.jpg", "create", config_dir)
        assert created_by_finmeta(tmp_path / "media" / "poster.jpg",
                                  config_dir=config_dir) is False

    def test_no_runs_dir_is_false(self, tmp_path):
        assert created_by_finmeta(tmp_path / "media" / "poster.jpg",
                                  config_dir=tmp_path / "config") is False

    def test_index_picks_up_a_newly_recorded_run(self, tmp_path):
        """The per-config cache must invalidate when a run dir appears."""
        config_dir = tmp_path / "config"
        target = tmp_path / "media" / "poster.jpg"
        _manifest("run-100-aaaaaa", target, "overwrite", config_dir)
        assert created_by_finmeta(target, config_dir=config_dir) is False
        _manifest("run-050-aaaaaa", target, "create", config_dir)
        assert created_by_finmeta(target, config_dir=config_dir) is True


class TestFindOriginalBackup:
    def test_created_path_reports_no_original(self, tmp_path):
        config_dir = tmp_path / "config"
        media = tmp_path / "media"
        media.mkdir()
        target = media / "poster.jpg"
        # Run 1 created it (no backup). Run 2 overwrote it, backing up run 1's
        # own overlaid output -- the only backup that exists.
        target.write_bytes(b"RUN1-OVERLAID-OUTPUT")
        _manifest("run-100-aaaaaa", target, "create", config_dir)
        backup("run-200-bbbbbb", target, config_dir=config_dir)
        _manifest("run-200-bbbbbb", target, "overwrite", config_dir,
                  backup_relpath="media/poster.jpg")
        assert find_original_backup(target, config_dir=config_dir) is None

    def test_pre_existing_path_still_returns_the_oldest_backup(self, tmp_path):
        config_dir = tmp_path / "config"
        media = tmp_path / "media"
        media.mkdir()
        target = media / "poster.jpg"
        true_original = b"PRE-FINMETA-ORIGINAL"
        target.write_bytes(true_original)
        backup("run-100-aaaaaa", target, config_dir=config_dir)
        _manifest("run-100-aaaaaa", target, "overwrite", config_dir,
                  backup_relpath="media/poster.jpg")
        target.write_bytes(b"RUN1-OVERLAID-OUTPUT")
        backup("run-200-bbbbbb", target, config_dir=config_dir)
        _manifest("run-200-bbbbbb", target, "overwrite", config_dir,
                  backup_relpath="media/poster.jpg")
        found = find_original_backup(target, config_dir=config_dir)
        assert found is not None and found.read_bytes() == true_original

    def test_oldest_is_decided_by_run_id_not_file_mtime(self, tmp_path):
        """``backup()`` copies with copy2, so a LATE run's backup of an OLD
        file keeps that old mtime -- run order is the only honest key."""
        import os

        config_dir = tmp_path / "config"
        media = tmp_path / "media"
        media.mkdir()
        target = media / "poster.jpg"
        target.write_bytes(b"FIRST-BACKUP")
        backup("run-100-aaaaaa", target, config_dir=config_dir)
        _manifest("run-100-aaaaaa", target, "overwrite", config_dir,
                  backup_relpath="media/poster.jpg")
        target.write_bytes(b"SECOND-BACKUP")
        backup("run-200-bbbbbb", target, config_dir=config_dir)
        # The second backup now claims a much OLDER mtime than the first.
        second = config_dir / "backups" / "run-200-bbbbbb" / "media" / "poster.jpg"
        os.utime(second, (1_000_000, 1_000_000))
        found = find_original_backup(target, config_dir=config_dir)
        assert found is not None and found.read_bytes() == b"FIRST-BACKUP"


def test_three_applies_on_a_finmeta_created_poster_never_stack(tmp_path):
    """End-to-end shape of the operator's report, through overlay_file_write.

    Every apply must render from the TMDb candidate (the only original this
    path has), never from a backup of Finmeta's own output.
    """
    config_dir = tmp_path / "config"
    target = tmp_path / "media" / "poster.jpg"
    target.parent.mkdir(parents=True)

    candidate = _poster((10, 20, 30))
    candidate_sha = hashlib.sha256(candidate).hexdigest()

    # Apply 1: nothing on disk yet -> create, no backup.
    fw1 = overlay_file_write(
        target_path=target, candidate_bytes=candidate, candidate_ref="https://tmdb/x.jpg",
        badges=[TextBadge(text="v1")], style=STYLE, config_dir=config_dir,
    )
    assert fw1.meta["lock_sha256"] == candidate_sha
    target.write_bytes(b"RUN1-OUTPUT-PLACEHOLDER")
    _manifest("run-100-aaaaaa", target, "create", config_dir)

    # Apply 2: overlay settings changed -> overwrite, backs up run 1's output.
    fw2 = overlay_file_write(
        target_path=target, candidate_bytes=candidate, candidate_ref="https://tmdb/x.jpg",
        badges=[TextBadge(text="v2")], style=STYLE, config_dir=config_dir,
    )
    assert fw2.meta["lock_sha256"] == candidate_sha, "run 2 must render from the candidate"
    backup("run-200-bbbbbb", target, config_dir=config_dir)
    target.write_bytes(b"RUN2-OUTPUT-PLACEHOLDER")
    _manifest("run-200-bbbbbb", target, "overwrite", config_dir,
              backup_relpath="media/poster.jpg")

    # Apply 3: the one that used to stack -- a backup now exists, but it is
    # Finmeta's own output, not an original.
    fw3 = overlay_file_write(
        target_path=target, candidate_bytes=candidate, candidate_ref="https://tmdb/x.jpg",
        badges=[TextBadge(text="v2")], style=STYLE, config_dir=config_dir,
    )
    assert fw3.meta["lock_sha256"] == candidate_sha, "run 3 rendered from a prior output"
    assert fw3.source_ref == "https://tmdb/x.jpg"
    # Same badges as run 2 and the same original -> byte-identical output.
    assert fw3.content_sha256 == fw2.content_sha256
