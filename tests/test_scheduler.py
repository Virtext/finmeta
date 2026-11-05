"""Tests for api.scheduler job building.

No prior coverage existed for this module. Added while fixing a real bug
(2026-10-22): an "all libraries" schedule (``Schedule.libraries == []``, the
UI's own documented meaning for that field) used to fan out into one
APScheduler job PER configured library, never a single combined
``library=None`` run -- the one scope ``engine.plan_builder._build_api_
actions`` requires to build preset/seasonal collections at all. So
collections could never run on a schedule, mirroring the (separately fixed)
gap where the manual per-library apply button couldn't run them either.
"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

import finmeta.api.scheduler as scheduler_module
from apscheduler.triggers.cron import CronTrigger
from finmeta.api.scheduler import _build_jobs, _run_catch_up
from finmeta.config.models import Config, Metadata, MovieArtwork, MovieLibrary, Schedule, ServerConfig


def _lib():
    return MovieLibrary(type="movies", enabled=True,
                         metadata=Metadata(enabled=False), artwork=MovieArtwork())


def _cfg(schedules, libraries=None):
    return Config(
        dry_run=True, server=ServerConfig(url="http://x:8096"),
        libraries=libraries if libraries is not None else {"Movies": _lib(), "Shows": _lib()},
        schedules=schedules,
    )


def _fake_context(cfg):
    ctx = MagicMock()
    ctx.config = cfg
    return ctx


class TestBuildJobsAllLibraries:
    def test_empty_libraries_is_one_combined_job(self):
        """Schedule.libraries == [] -> exactly one job, library arg None."""
        cfg = _cfg([Schedule(cron="0 4 * * *", action="apply", libraries=[])])
        scheduler = MagicMock()
        _build_jobs(_fake_context(cfg), scheduler)

        assert scheduler.add_job.call_count == 1
        _, kwargs = scheduler.add_job.call_args
        assert kwargs["args"][2] is None  # library
        assert kwargs["args"][1] == "apply"  # action
        assert kwargs["id"] == "sched-0-all"

    def test_explicit_libraries_still_one_job_per_library(self):
        """A non-empty Schedule.libraries list keeps the pre-existing
        per-library fan-out -- only the "empty = all" case changed."""
        cfg = _cfg([Schedule(cron="0 4 * * *", action="plan",
                              libraries=["Movies", "Shows"])])
        scheduler = MagicMock()
        _build_jobs(_fake_context(cfg), scheduler)

        assert scheduler.add_job.call_count == 2
        libs_passed = {c.kwargs["args"][2] for c in scheduler.add_job.call_args_list}
        assert libs_passed == {"Movies", "Shows"}
        ids = {c.kwargs["id"] for c in scheduler.add_job.call_args_list}
        assert ids == {"sched-0-Movies", "sched-0-Shows"}

    def test_multiple_schedules_each_get_their_own_job_id_namespace(self):
        cfg = _cfg([
            Schedule(cron="0 4 * * *", action="apply", libraries=[]),
            Schedule(cron="0 2 * * 1", action="plan", libraries=["Movies"]),
        ])
        scheduler = MagicMock()
        _build_jobs(_fake_context(cfg), scheduler)

        assert scheduler.add_job.call_count == 2
        ids = {c.kwargs["id"] for c in scheduler.add_job.call_args_list}
        assert ids == {"sched-0-all", "sched-1-Movies"}

    def test_no_schedules_adds_no_jobs(self):
        cfg = _cfg([])
        scheduler = MagicMock()
        _build_jobs(_fake_context(cfg), scheduler)
        scheduler.add_job.assert_not_called()


class _FixedNow(datetime):
    """A ``datetime`` subclass whose ``.now()`` always returns a fixed
    instant -- real ``datetime`` otherwise, so comparisons/arithmetic with
    genuine ``datetime`` values (as ``_run_catch_up`` does) still work."""
    _frozen: datetime

    @classmethod
    def now(cls, tz=None):
        return cls._frozen if tz is None else cls._frozen.astimezone(tz)


def _frozen_datetime(value: datetime):
    cls = type("_Frozen", (_FixedNow,), {"_frozen": value})
    return cls


class _FakeJob:
    def __init__(self, job_id, trigger):
        self.id = job_id
        self.trigger = trigger


class _FakeScheduler:
    def __init__(self, timezone, jobs):
        self.timezone = timezone
        self._jobs = jobs
        self.modify_job = MagicMock()

    def get_jobs(self):
        return self._jobs


class TestRunCatchUp:
    """``_run_catch_up`` -- fires a job whose cron occurrence fell due while
    the process wasn't running (see the function's own docstring for why
    ``misfire_grace_time`` alone can't cover this)."""

    def test_missed_occurrence_is_run_now(self, monkeypatch):
        trigger = CronTrigger.from_crontab("0 4 * * *")
        now = datetime(2026, 11, 5, 7, 36, tzinfo=trigger.timezone)  # 04:00 fired 3h36m ago
        monkeypatch.setattr(scheduler_module, "datetime", _frozen_datetime(now))
        scheduler = _FakeScheduler(trigger.timezone, [_FakeJob("sched-0-all", trigger)])

        _run_catch_up(scheduler)

        scheduler.modify_job.assert_called_once_with("sched-0-all", next_run_time=now)

    def test_occurrence_outside_window_is_not_caught_up(self, monkeypatch):
        trigger = CronTrigger.from_crontab("0 4 * * *")
        # 04:00 fired 23h ago -- outside the (default 6h) catch-up window,
        # so the job's own NEXT real occurrence (tonight) must be left alone.
        now = datetime(2026, 11, 6, 3, 0, tzinfo=trigger.timezone)
        monkeypatch.setattr(scheduler_module, "datetime", _frozen_datetime(now))
        scheduler = _FakeScheduler(trigger.timezone, [_FakeJob("sched-0-all", trigger)])

        _run_catch_up(scheduler)

        scheduler.modify_job.assert_not_called()

    def test_no_missed_occurrence_does_nothing(self, monkeypatch):
        trigger = CronTrigger.from_crontab("0 4 * * *")
        now = datetime(2026, 11, 5, 1, 0, tzinfo=trigger.timezone)  # before today's 04:00
        monkeypatch.setattr(scheduler_module, "datetime", _frozen_datetime(now))
        scheduler = _FakeScheduler(trigger.timezone, [_FakeJob("sched-0-all", trigger)])

        _run_catch_up(scheduler)

        scheduler.modify_job.assert_not_called()
