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

from unittest.mock import MagicMock

from finmeta.api.scheduler import _build_jobs
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
