"""SPEC_S11 §B2/§B3: the Activity Center API."""
from __future__ import annotations


import pytest
from fastapi.testclient import TestClient

from finmeta.api import AppContext, create_app
from finmeta.engine.jobs import finish_run, init_db, log, start_run

CONFIG_TEMPLATE = """\
dry_run: {dry_run}
server:
  url: http://fake:8096
  api_key_env: JELLYFIN_API_KEY
path_mappings:
  - jellyfin: /media
    local: /tmp/fake-media
libraries:
  Movies:
    type: movies
    enabled: true
    metadata:
      enabled: false
    artwork:
      poster: true
    theme_song: false
    overlays: []
"""


@pytest.fixture
def ctx_factory(tmp_path, monkeypatch):
    monkeypatch.setenv("JELLYFIN_API_KEY", "fake-key")
    monkeypatch.setenv("FINMETA_AUTH", "off")

    def _make(*, dry_run: bool = True):
        cfg_dir = tmp_path / f"config-{int(dry_run)}"
        cfg_dir.mkdir(exist_ok=True)
        (cfg_dir / "config.yml").write_text(
            CONFIG_TEMPLATE.format(dry_run=str(dry_run).lower()))
        return AppContext(
            str(cfg_dir / "config.yml"), config_dir=str(cfg_dir),
            db_path=str(cfg_dir / "jobs.db"),
            overrides_path=str(cfg_dir / "overrides.db"))

    return _make


def _client(ctx) -> TestClient:
    return TestClient(create_app(ctx))


def _run(ctx, run_id, *, action="apply", scope="all", status="ok",
         tier=None, errors=0, warnings=0):
    init_db(ctx.db_path)
    start_run(ctx.db_path, run_id, "", action=action, scope=scope, tier=tier)
    for i in range(warnings):
        log(ctx.db_path, run_id, "warn", f"warning {i}")
    for i in range(errors):
        log(ctx.db_path, run_id, "error", f"failure {i}")
    if status != "running":
        finish_run(ctx.db_path, run_id, status, files_written=3)


# ---------------------------------------------------------------------------
# the listing
# ---------------------------------------------------------------------------

def test_runs_are_listed_with_what_they_were_and_how_they_went(ctx_factory):
    ctx = ctx_factory()
    _run(ctx, "r1", action="scan", scope="lib:Movies", tier=2,
         status="error", errors=2, warnings=1)
    with _client(ctx) as client:
        body = client.get("/api/activity").json()
    row = body["runs"][0]
    assert row["run_id"] == "r1"
    assert (row["action"], row["scope"], row["tier"]) == ("scan", "lib:Movies", 2)
    assert row["status"] == "error"
    assert (row["errors"], row["warnings"]) == (2, 1)
    assert row["last_error"] == "failure 1"
    assert row["retryable"] is True
    assert row["duration_s"] is not None
    assert body["dry_run"] is True


def test_a_pre_s11_row_is_listed_but_not_retryable(ctx_factory):
    ctx = ctx_factory()
    init_db(ctx.db_path)
    start_run(ctx.db_path, "old", "")        # no action recorded
    finish_run(ctx.db_path, "old", "ok")
    with _client(ctx) as client:
        row = client.get("/api/activity").json()["runs"][0]
        assert row["action"] is None
        assert row["retryable"] is False
        r = client.post("/api/activity/old/retry")
        assert r.status_code == 422
        assert "nothing to resubmit" in r.json()["detail"]


def test_paging_and_filters(ctx_factory):
    ctx = ctx_factory()
    for i in range(4):
        _run(ctx, f"r{i}", action="plan" if i % 2 else "apply",
             status="ok" if i else "error")
    with _client(ctx) as client:
        body = client.get("/api/activity", params={"limit": 2}).json()
        assert (body["total"], body["has_more"], len(body["runs"])) == (4, True, 2)
        assert client.get("/api/activity",
                          params={"action": "plan"}).json()["total"] == 2
        assert client.get("/api/activity",
                          params={"status": "error"}).json()["total"] == 1
        assert client.get("/api/activity",
                          params={"action": "bogus"}).status_code == 422


def test_an_empty_history_is_not_an_error(ctx_factory):
    ctx = ctx_factory()
    with _client(ctx) as client:
        body = client.get("/api/activity").json()
    assert body["runs"] == [] and body["total"] == 0
    assert body["active"] is None
    assert body["queued"] == {"items": [], "libraries": []}


# ---------------------------------------------------------------------------
# what is happening now
# ---------------------------------------------------------------------------

def test_the_active_run_and_both_queues_are_visible(ctx_factory):
    """Pending work existed with no way to see it before S11a."""
    ctx = ctx_factory()
    _run(ctx, "live", action="apply", scope="lib:Movies", status="running")
    assert ctx.claim_run("live", action="apply", library="Movies")
    ctx.queue_webhook_item("item-9")
    ctx.queue_library_run("scan", "Movies", 2)
    with _client(ctx) as client:
        body = client.get("/api/activity").json()
    assert body["active"]["run_id"] == "live"
    assert body["active"]["scope"] == "lib:Movies"
    assert body["queued"]["items"] == ["item-9"]
    assert body["queued"]["libraries"] == [
        {"action": "scan", "library": "Movies", "tier": 2}]
    ctx.release_run("live")


def test_queued_entries_can_be_dropped_before_they_start(ctx_factory):
    ctx = ctx_factory()
    ctx.queue_webhook_item("item-9")
    ctx.queue_library_run("apply", "Movies", 1)
    with _client(ctx) as client:
        assert client.delete("/api/activity/queued/items/item-9").status_code == 200
        assert client.delete("/api/activity/queued/items/item-9").status_code == 404
        assert client.delete(
            "/api/activity/queued/libraries/apply/Movies").status_code == 200
        assert client.delete(
            "/api/activity/queued/libraries/apply/Movies").status_code == 404
        body = client.get("/api/activity").json()
    assert body["queued"] == {"items": [], "libraries": []}


def test_clear_queued_drops_everything_but_not_the_active_run(ctx_factory):
    """2026-11-10: a bulk fix can queue far more than is realistic to drop
    one at a time -- DELETE /api/activity/queued clears both queues in
    one call, and never touches whatever is actually running."""
    ctx = ctx_factory()
    _run(ctx, "live", action="apply", scope="item:item-1", status="running")
    assert ctx.claim_run("live", action="apply", item_id="item-1")
    ctx.queue_webhook_item("item-9")
    ctx.queue_webhook_item("item-10")
    ctx.queue_library_run("apply", "Movies", 1)
    with _client(ctx) as client:
        r = client.delete("/api/activity/queued")
        assert r.status_code == 200
        assert r.json() == {"dropped": 3}
        body = client.get("/api/activity").json()
    assert body["queued"] == {"items": [], "libraries": []}
    assert body["active"]["run_id"] == "live"      # untouched
    ctx.release_run("live")


def test_cancel_active_run_releases_the_slot_and_marks_it_error(ctx_factory):
    """2026-11-10: a stuck apply left the operator with no way past it --
    clear_queued() only ever covered the queue, never the run actually in
    progress. POST /api/activity/active/cancel releases the slot and
    marks the run's own row 'error' instead of leaving it 'running'
    forever; a second submission can then claim the slot immediately."""
    ctx = ctx_factory(dry_run=False)
    _run(ctx, "stuck", action="apply", scope="item:item-1", status="running")
    assert ctx.claim_run("stuck", action="apply", item_id="item-1")
    with _client(ctx) as client:
        assert client.post("/api/activity/active/cancel").json() == {"cancelled": "stuck"}
        # idempotent-ish: nothing active now, so a second call 404s
        assert client.post("/api/activity/active/cancel").status_code == 404
        body = client.get("/api/activity").json()
        assert body["active"] is None
        row = next(r for r in body["runs"] if r["run_id"] == "stuck")
        assert row["status"] == "error"
        # the slot is free -- something else can claim it right away
        assert ctx.claim_run("next-one", action="apply", item_id="item-2")
    ctx.release_run("next-one")


def test_scheduled_jobs_and_their_next_run_are_listed(ctx_factory):
    ctx = ctx_factory()

    class _Job:
        id = "sched-0-all"
        name = "scan:all-libraries"

        class _When:
            @staticmethod
            def isoformat():
                return "2026-11-09T04:00:00+00:00"

        next_run_time = _When()

    class _Sched:
        @staticmethod
        def get_jobs():
            return [_Job()]

        @staticmethod
        def shutdown(wait=False):       # AppContext.shutdown calls this
            pass

    ctx.scheduler = _Sched()
    with _client(ctx) as client:
        body = client.get("/api/activity").json()
    assert body["schedules"] == [{
        "id": "sched-0-all", "name": "scan:all-libraries",
        "next_run_at": "2026-11-09T04:00:00+00:00"}]


def test_a_broken_scheduler_does_not_break_the_page(ctx_factory):
    ctx = ctx_factory()

    class _Sched:
        @staticmethod
        def get_jobs():
            raise RuntimeError("mid-reload")

        @staticmethod
        def shutdown(wait=False):
            pass

    ctx.scheduler = _Sched()
    with _client(ctx) as client:
        assert client.get("/api/activity").json()["schedules"] == []


# ---------------------------------------------------------------------------
# retry
# ---------------------------------------------------------------------------

def test_retry_resubmits_the_same_action_and_scope(ctx_factory, monkeypatch):
    ctx = ctx_factory(dry_run=False)
    _run(ctx, "r1", action="scan", scope="lib:Movies", tier=2)
    submitted = {}

    from finmeta.api import routes_activity

    def fake_submit(context, action, *, library=None, item_id=None, tier=1):
        submitted.update(action=action, library=library, item_id=item_id, tier=tier)
        return "run-new", None

    monkeypatch.setattr(routes_activity, "submit_run", fake_submit)
    with _client(ctx) as client:
        body = client.post("/api/activity/r1/retry").json()
    assert body["job_id"] == "run-new"
    assert submitted == {"action": "scan", "library": "Movies",
                         "item_id": None, "tier": 2}


def test_retry_of_an_item_scoped_run_keeps_the_item(ctx_factory, monkeypatch):
    ctx = ctx_factory(dry_run=False)
    _run(ctx, "r1", action="apply", scope="item:abc123")
    submitted = {}

    from finmeta.api import routes_activity
    monkeypatch.setattr(
        routes_activity, "submit_run",
        lambda ctx_, action, *, library=None, item_id=None, tier=1: (
            submitted.update(action=action, library=library, item_id=item_id)
            or ("run-new", None)))
    with _client(ctx) as client:
        client.post("/api/activity/r1/retry")
    assert submitted == {"action": "apply", "library": None, "item_id": "abc123"}


def test_retry_is_refused_while_dry_run_blocks_an_apply(ctx_factory):
    ctx = ctx_factory(dry_run=True)
    _run(ctx, "r1", action="apply", scope="all")
    with _client(ctx) as client:
        r = client.post("/api/activity/r1/retry")
    assert r.status_code == 409
    assert "dry_run" in r.json()["detail"]


def test_a_scan_retry_works_under_dry_run(ctx_factory, monkeypatch):
    """A scan writes nothing, so the flag is irrelevant to it."""
    ctx = ctx_factory(dry_run=True)
    _run(ctx, "r1", action="scan", scope="all", tier=1)
    from finmeta.api import routes_activity
    monkeypatch.setattr(routes_activity, "submit_run",
                        lambda *a, **k: ("run-new", None))
    with _client(ctx) as client:
        assert client.post("/api/activity/r1/retry").status_code == 200


def test_retry_conflicts_with_a_run_in_progress(ctx_factory):
    ctx = ctx_factory(dry_run=False)
    _run(ctx, "r1", action="plan", scope="all")
    assert ctx.claim_run("blocking", action="apply")
    with _client(ctx) as client:
        r = client.post("/api/activity/r1/retry")
    assert r.status_code == 409
    assert "already in progress" in r.json()["detail"]
    ctx.release_run("blocking")


def test_retry_of_a_still_running_run_is_refused(ctx_factory):
    ctx = ctx_factory(dry_run=False)
    _run(ctx, "r1", action="plan", scope="all", status="running")
    with _client(ctx) as client:
        r = client.post("/api/activity/r1/retry")
    assert r.status_code == 409
    assert "still running" in r.json()["detail"]


def test_retry_of_an_unknown_run_is_404(ctx_factory):
    ctx = ctx_factory()
    with _client(ctx) as client:
        assert client.post("/api/activity/nope/retry").status_code == 404
