"""A multi-library schedule must run every library, not just the first.

Operator-reported 2026-11-06. ``_build_jobs`` registered one cron job per
listed library on the SAME trigger; Finmeta allows one active run at a time,
so the first job claimed the slot and the others were dropped on the spot::

    schedule skipped (apply/Anime): a run is already in progress
    schedule started run run-... (apply Filmer)
    schedule skipped (apply/Tv serier): a run is already in progress

Nothing retried them, so two of the operator's three libraries had never
been applied by the nightly schedule -- which is why their posters kept the
pre-change overlay long after a settings change.

Covered here: the FIFO on ``AppContext`` and the drain in
``run_service._worker``. ``_libraries_job`` itself is covered in
``test_scheduler.py``.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from finmeta.api import run_service
from finmeta.api.state import AppContext

CONFIG_YAML = """
dry_run: true
server:
  url: http://fake:8096
  api_key_env: JELLYFIN_API_KEY
libraries:
  Movies:
    type: movies
    metadata:
      enabled: true
    artwork: {}
    theme_song: false
    overlays: []
"""


@pytest.fixture
def context(tmp_path, monkeypatch):
    monkeypatch.setenv("JELLYFIN_API_KEY", "fake-key")
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "config.yml").write_text(CONFIG_YAML)
    return AppContext(
        str(cfg_dir / "config.yml"), config_dir=str(cfg_dir),
        db_path=str(cfg_dir / "jobs.db"), overrides_path=str(cfg_dir / "overrides.db"),
    )


class TestLibraryQueue:
    def test_fifo_order(self, context):
        context.queue_library_run("apply", "Anime")
        context.queue_library_run("apply", "Tv serier")
        assert context.pop_library_run() == ("apply", "Anime", 1)
        assert context.pop_library_run() == ("apply", "Tv serier", 1)
        assert context.pop_library_run() is None

    def test_same_library_is_not_queued_twice(self, context):
        """A schedule firing again while its own earlier occurrence is still
        waiting must not pile up two identical library runs."""
        context.queue_library_run("apply", "Anime")
        context.queue_library_run("apply", "Anime")
        assert context.pop_library_run() == ("apply", "Anime", 1)
        assert context.pop_library_run() is None

    def test_the_scan_tier_is_part_of_the_key(self, context):
        """S10c: a queued deep scan must not come back out as a plain scan."""
        context.queue_library_run("scan", "Anime", 2)
        context.queue_library_run("scan", "Anime", 1)
        assert context.pop_library_run() == ("scan", "Anime", 2)
        assert context.pop_library_run() == ("scan", "Anime", 1)

    def test_action_is_part_of_the_key(self, context):
        context.queue_library_run("apply", "Anime")
        context.queue_library_run("plan", "Anime")
        assert context.pop_library_run() == ("apply", "Anime", 1)
        assert context.pop_library_run() == ("plan", "Anime", 1)

    def test_requeue_after_a_pop_is_allowed(self, context):
        """``_worker`` puts a library back when something else won the slot."""
        context.queue_library_run("apply", "Anime")
        assert context.pop_library_run() == ("apply", "Anime", 1)
        context.queue_library_run("apply", "Anime")
        assert context.pop_library_run() == ("apply", "Anime", 1)


class TestWorkerDrain:
    """``run_service._worker``'s finally-block hands the slot on."""

    @staticmethod
    def _ctx(tmp_path=None):
        ctx = MagicMock()
        ctx.pop_webhook_item.return_value = None
        ctx.pop_library_run.return_value = None
        # A real path: the crash branch writes to context.db_path, and a
        # MagicMock there makes sqlite create junk directories in the cwd.
        ctx.db_path = str((tmp_path or Path(".")) / "jobs.db")
        ctx.redact = lambda text: text
        return ctx

    def test_queued_library_starts_when_the_slot_frees_up(self, monkeypatch):
        monkeypatch.setattr(run_service, "_do_apply", lambda *a, **k: None)
        submitted = []
        monkeypatch.setattr(
            run_service, "submit_run",
            lambda ctx, action, *, library=None, item_id=None, tier=1: (
                submitted.append((action, library, item_id)) or ("run-2", None)))
        ctx = self._ctx()
        ctx.pop_library_run.return_value = ("apply", "Anime", 1)

        run_service._worker(ctx, "run-1", "apply", "Filmer", None)

        assert submitted == [("apply", "Anime", None)]

    def test_webhook_items_drain_before_queued_libraries(self, monkeypatch):
        """One newly added title finishes in seconds; a library-wide run can
        take many minutes -- so the item goes first and the library stays
        queued for the next release."""
        monkeypatch.setattr(run_service, "_do_apply", lambda *a, **k: None)
        submitted = []
        monkeypatch.setattr(
            run_service, "submit_run",
            lambda ctx, action, *, library=None, item_id=None, tier=1: (
                submitted.append((action, library, item_id)) or ("run-2", None)))
        ctx = self._ctx()
        ctx.pop_webhook_item.return_value = "item-9"
        ctx.pop_library_run.return_value = ("apply", "Anime", 1)

        run_service._worker(ctx, "run-1", "apply", "Filmer", None)

        assert submitted == [("apply", None, "item-9")]
        ctx.pop_library_run.assert_not_called()

    def test_a_lost_race_puts_the_library_back_on_the_queue(self, monkeypatch):
        monkeypatch.setattr(run_service, "_do_apply", lambda *a, **k: None)
        monkeypatch.setattr(
            run_service, "submit_run",
            lambda *a, **k: (None, "a run is already in progress"))
        ctx = self._ctx()
        ctx.pop_library_run.return_value = ("apply", "Anime", 1)

        run_service._worker(ctx, "run-1", "apply", "Filmer", None)

        ctx.queue_library_run.assert_called_once_with("apply", "Anime", 1)

    def test_a_crashed_run_still_hands_the_slot_on(self, monkeypatch, tmp_path):
        """The drain lives in a finally-block: a failing run must not strand
        the rest of the schedule."""
        def boom(*a, **k):
            raise RuntimeError("apply exploded")

        monkeypatch.setattr(run_service, "_do_apply", boom)
        submitted = []
        monkeypatch.setattr(
            run_service, "submit_run",
            lambda ctx, action, *, library=None, item_id=None, tier=1: (
                submitted.append((action, library)) or ("run-2", None)))
        ctx = self._ctx(tmp_path)
        ctx.pop_library_run.return_value = ("apply", "Anime", 1)

        run_service._worker(ctx, "run-1", "apply", "Filmer", None)

        assert submitted == [("apply", "Anime")]
