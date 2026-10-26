"""APScheduler wiring: one cron job per config schedule (SPEC §10, §12).

Jobs run the same single-slot run path as the API (so two things can never
apply at once). On every config (re)load the jobs are removed and rebuilt from
the new config — this is the config hot-reload hook.
"""
from __future__ import annotations

import logging
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from finmeta.api.state import AppContext
from finmeta.api.run_service import submit_run
from finmeta.engine.poster_rotation import top_rated_item_ids

logger = logging.getLogger("finmeta.api.scheduler")


def _job(context: AppContext, action: str, library: Optional[str]) -> None:
    run_id, err = submit_run(context, action, library=library)
    label = library or "all-libraries"
    if err:
        logger.warning("schedule skipped (%s/%s): %s", action, label, err)
    else:
        logger.info("schedule started run %s (%s %s)", run_id, action, label)


def _poster_rotation_job(context: AppContext, library: str) -> None:
    """S9d6: reshuffle+reapply the poster of a library's top-rated items.

    Submits the first item directly and queues the rest on the same
    single-item FIFO the webhook route uses (``queue_webhook_item``/
    ``pop_webhook_item``, drained one at a time as each run's worker thread
    finishes) -- reuses the existing single-run-slot chaining instead of a
    bespoke wait/poll loop.
    """
    cfg = context.config
    lib_cfg = cfg.libraries.get(library)
    if lib_cfg is None or not lib_cfg.enabled or not lib_cfg.poster_rotation.enabled:
        return
    if cfg.dry_run:
        logger.warning("poster rotation skipped for %s: dry_run is true", library)
        return
    if not lib_cfg.artwork.poster:
        logger.warning("poster rotation skipped for %s: artwork.poster is off", library)
        return
    jf = context.make_jellyfin(cfg)
    item_types = ["Movie"] if lib_cfg.type == "movies" else ["Series"]
    item_ids = top_rated_item_ids(jf, library, lib_cfg.poster_rotation.item_count, item_types)
    if not item_ids:
        logger.info("poster rotation: no rated items found for %s", library)
        return
    for item_id in item_ids:
        context.overrides.increment_reshuffle(item_id, "poster")
    first, rest = item_ids[0], item_ids[1:]
    run_id, err = submit_run(context, "apply", item_id=first)
    if run_id:
        logger.info("poster rotation: apply started for %s (run %s)", first, run_id)
    else:
        context.queue_webhook_item(first)
        logger.info("poster rotation: run busy (%s), queued %s", err, first)
    for item_id in rest:
        context.queue_webhook_item(item_id)
    logger.info("poster rotation: rotated %d item(s) in %s", len(item_ids), library)


def _build_jobs(context: AppContext, scheduler: BackgroundScheduler) -> None:
    cfg = context.config
    for i, sched in enumerate(cfg.schedules):
        trigger = CronTrigger.from_crontab(sched.cron)
        if sched.libraries:
            # Explicit subset -> one scoped run per listed library, unchanged.
            for lib in sched.libraries:
                scheduler.add_job(
                    _job,
                    trigger=trigger,
                    args=[context, sched.action, lib],
                    id=f"sched-{i}-{lib}",
                    name=f"{sched.action}:{lib}",
                    max_instances=1,
                    coalesce=True,
                    misfire_grace_time=300,
                )
        else:
            # Empty libraries list = "all configured libraries" (the UI's own
            # hint text). Must be ONE combined library=None run, not one job
            # per library -- preset/seasonal collections only ever get built
            # on that true full scope (engine.plan_builder._build_api_actions
            # requires library_name is None), same as the Collections tab's
            # manual "Apply (all libraries)" button added 2026-10-22. Before
            # this fix, an "all libraries" schedule fanned out into one job
            # PER library and could never run collections either.
            scheduler.add_job(
                _job,
                trigger=trigger,
                args=[context, sched.action, None],
                id=f"sched-{i}-all",
                name=f"{sched.action}:all-libraries",
                max_instances=1,
                coalesce=True,
                misfire_grace_time=300,
            )

    for lib_name, lib_cfg in cfg.libraries.items():
        rot = lib_cfg.poster_rotation
        if not rot.enabled:
            continue
        scheduler.add_job(
            _poster_rotation_job,
            trigger=CronTrigger.from_crontab(rot.cron),
            args=[context, lib_name],
            id=f"poster-rotation-{lib_name}",
            name=f"poster_rotation:{lib_name}",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=300,
        )


def reschedule(context: AppContext) -> None:
    """Remove all jobs and rebuild them from the current config."""
    scheduler: Optional[BackgroundScheduler] = context.scheduler
    if scheduler is None:
        return
    for job in list(scheduler.get_jobs()):
        scheduler.remove_job(job.id)
    _build_jobs(context, scheduler)


def start(context: AppContext) -> None:
    if context.scheduler is not None:
        return
    scheduler = BackgroundScheduler()
    _build_jobs(context, scheduler)
    scheduler.start()
    context.scheduler = scheduler
    logger.info("scheduler started with %d job(s)", len(scheduler.get_jobs()))


__all__ = ["start", "reschedule"]
