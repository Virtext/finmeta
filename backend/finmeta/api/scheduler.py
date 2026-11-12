"""APScheduler wiring: one cron job per config schedule (SPEC §10, §12).

Jobs run the same single-slot run path as the API (so two things can never
apply at once). On every config (re)load the jobs are removed and rebuilt from
the new config — this is the config hot-reload hook.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from finmeta.api.state import AppContext
from finmeta.api.run_service import submit_run
from finmeta.engine.poster_rotation import top_rated_item_ids

logger = logging.getLogger("finmeta.api.scheduler")

#: How far back to look, on a fresh process start, for a cron occurrence
#: that should have fired but never did -- see ``_run_catch_up``.
_CATCH_UP_WINDOW = timedelta(hours=6)


def _job(context: AppContext, action: str, library: Optional[str],
         tier: int = 1) -> None:
    """One cron occurrence for a schedule with NO explicit library subset
    (``library=None``, "all configured libraries" -- the only scope that
    also builds preset/seasonal collections, see ``_build_jobs``).

    Audit 2026-11-11 "B9": when the run slot was busy, this used to log
    "schedule skipped" and drop the occurrence with no retry at all --
    the exact bug ``_libraries_job`` below was already fixed for
    (2026-11-06) on the explicit-subset path, but this sibling scope was
    missed. ``library=None`` is a perfectly representable
    ``queue_library_run`` key (``pop_library_run``'s drain in
    ``run_service._worker`` already forwards it to ``submit_run`` as
    ``library=None`` without special-casing it -- it only needed this
    caller to actually queue instead of giving up).
    """
    run_id, err = submit_run(context, action, library=library, tier=tier)
    label = library or "all-libraries"
    if err:
        context.queue_library_run(action, library, tier)
        logger.warning("schedule queued (%s/%s): %s", action, label, err)
    else:
        logger.info("schedule started run %s (%s %s)", run_id, action, label)


def _libraries_job(context: AppContext, action: str, libraries: list,
                   tier: int = 1) -> None:
    """One cron occurrence for a schedule that names several libraries.

    Starts the first library and QUEUES the rest, drained one at a time by
    ``run_service._worker`` as each run releases the single run slot.

    Before this (fixed 2026-11-06, operator-reported): ``_build_jobs`` added
    one cron job PER library on the same trigger, so they all fired in the
    same second, the first claimed the run slot and every other library was
    dropped with "a run is already in progress" -- no retry, no queue, and
    nothing about it in the UI. Live proof from the operator's deployment,
    whose nightly schedule lists three libraries::

        schedule skipped (apply/Anime): a run is already in progress
        schedule started run run-... (apply Filmer)
        schedule skipped (apply/Tv serier): a run is already in progress

    Two of its three libraries had silently never been applied on schedule.

    Deliberately NOT collapsed into one combined ``library=None`` run: that
    scope also builds preset/seasonal collections (see ``_build_jobs``'s
    ``else`` branch), which an explicit library subset must not trigger.
    """
    if not libraries:
        return
    first, rest = libraries[0], list(libraries[1:])
    run_id, err = submit_run(context, action, library=first, tier=tier)
    if run_id:
        logger.info("schedule started run %s (%s %s)", run_id, action, first)
    else:
        context.queue_library_run(action, first, tier)
        logger.info("schedule queued %s/%s (%s)", action, first, err)
    for lib in rest:
        context.queue_library_run(action, lib, tier)
        logger.info("schedule queued %s/%s behind the active run", action, lib)


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
            # Explicit subset -> ONE job that runs the listed libraries one
            # after another on the single run slot. One job per library (what
            # this used to be) silently dropped every library but the first --
            # see _libraries_job.
            libs = list(sched.libraries)
            scheduler.add_job(
                _libraries_job,
                trigger=trigger,
                args=[context, sched.action, libs, sched.tier],
                id=f"sched-{i}-libs",
                name=f"{sched.action}:{','.join(libs)}",
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
                args=[context, sched.action, None, sched.tier],
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


def _run_catch_up(scheduler: BackgroundScheduler) -> None:
    """Fire any job whose cron occurrence fell due in the last
    ``_CATCH_UP_WINDOW`` but was never actually run.

    ``BackgroundScheduler``'s default in-memory jobstore has no memory of
    anything from before this process existed: on every fresh start,
    ``add_job`` computes each job's next fire time starting from *now*, so a
    cron time that passed while the container was down (an operator restart,
    an image update, a crash) is never scheduled in the first place --
    ``misfire_grace_time`` only covers a *delay* within an already-running
    process, not this case. Confirmed live 2026-11-05: ``finmeta-public-
    test``'s nightly 04:00 schedule was silently skipped after an operator
    restart that landed in this exact gap, with zero error anywhere.

    Re-running an apply/plan schedule that, unbeknownst to this fresh
    process, actually already completed moments before the restart is a
    harmless no-op -- apply is idempotent by design (STATE.md: two
    consecutive applies with no changes write 0 files). The one non-
    idempotent job, poster rotation, could in that same edge case reshuffle
    once more than intended; accepted as a minor cosmetic cost against the
    alternative of silently dropping real runs.
    """
    now = datetime.now(scheduler.timezone)
    window_start = now - _CATCH_UP_WINDOW
    for job in scheduler.get_jobs():
        missed_at = job.trigger.get_next_fire_time(None, window_start)
        if missed_at is not None and missed_at <= now:
            logger.warning(
                "scheduler: %s missed its %s occurrence (process was not "
                "running) -- running it now as catch-up",
                job.id, missed_at.isoformat(),
            )
            scheduler.modify_job(job.id, next_run_time=now)


#: Job-id prefixes _build_jobs itself creates from config (schedules +
#: per-library poster rotation) -- these are what reschedule() removes and
#: rebuilds. Anything else on the scheduler is a one-shot job some OTHER
#: part of the app queued directly (e.g. routes_webhooks' debounced
#: "webhook-apply-<item_id>"), not derived from config at all.
_CONFIG_DERIVED_JOB_PREFIXES = ("sched-", "poster-rotation-")


def reschedule(context: AppContext) -> None:
    """Remove config-derived jobs and rebuild them from the current config.

    Audit 2026-11-11 "B8": this used to remove and rebuild EVERY job on the
    scheduler, including one-shot jobs nothing here created --
    routes_webhooks.webhook_receive debounces an auto-apply by scheduling
    a one-shot "webhook-apply-<item_id>" job up to 90s in the future.
    reschedule() runs on every config save (PUT /api/config and the
    config-undo route both call it, via AppContext._apply_config), so
    saving ANY setting while a webhook apply was pending silently
    cancelled it -- the newly imported title then got no overlays until
    the next scheduled run, with nothing in the log naming what happened.
    Only jobs whose id matches a prefix config itself produces are
    touched now; a pending one-shot job survives a config save untouched.
    """
    scheduler: Optional[BackgroundScheduler] = context.scheduler
    if scheduler is None:
        return
    for job in list(scheduler.get_jobs()):
        if job.id.startswith(_CONFIG_DERIVED_JOB_PREFIXES):
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
    # Only on a genuinely fresh process -- reschedule() (config hot-reload)
    # must never re-trigger a job that correctly already ran minutes ago
    # under the just-replaced config.
    _run_catch_up(scheduler)


__all__ = ["start", "reschedule"]
