"""S9d2: overlay profiles UI backend — profile YAML check, bulk apply.

Offline tests with TestClient + fakes (same pattern as S9d1):
- a library plan covers every item in the library (bulk scope);
- dry_run=true blocks apply via the run API (status "refused").

The seeded-sample tests that put the "sample" in this file's name went away
with GET /api/libraries/{name}/sample itself on 2026-10-22 (operator request,
docs/SECURITY_REVIEW.md "D1"). The filename is kept so the surviving tests'
history stays easy to follow.
"""
from __future__ import annotations

import time

import pytest
import yaml
from fastapi.testclient import TestClient

from finmeta.api import AppContext, create_app
from finmeta.engine.plan import FileWrite, ItemPlan, WritePlan

from test_providers import PERSIST_YAML


def _make_plan(n_items: int) -> WritePlan:
    items = [
        ItemPlan(
            item_id=f"item-{i}", item_name=f"Movie {i}", media_type="movie",
            files=[FileWrite(target_path=f"/media/m{i}.nfo", kind="metadata",
                             source_ref="", content_sha256="0" * 64, status="create")],
        )
        for i in range(n_items)
    ]
    return WritePlan(items=items)


def _api_context(tmp_path, monkeypatch, plan: WritePlan):
    monkeypatch.setenv("JELLYFIN_API_KEY", "fake-key")
    monkeypatch.setenv("TMDB_KEY", "fake-tmdb-key")
    monkeypatch.setenv("FINMETA_AUTH", "off")
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    cfg_file = cfg_dir / "config.yml"
    cfg_file.write_text("""\
dry_run: true
server:
  url: http://fake:8096
  api_key_env: JELLYFIN_API_KEY
libraries:
  Movies:
    type: movies
    enabled: true
    overlays: [resolution]
""")

    def _fake_plan(cfg, jf, tmdb, sources, library=None, item_id=None, config_dir=None, **kwargs):
        return plan

    ctx = AppContext(
        str(cfg_file), config_dir=str(cfg_dir),
        db_path=str(cfg_dir / "jobs.db"), overrides_path=str(cfg_dir / "overrides.db"),
        plan_builder=_fake_plan,
    )
    class FakeJf:
        def get_libraries(self):
            from finmeta.jellyfin_client.types import Library
            return [Library(name="Movies", item_id="lib-movies", collection_type="movies")]
        def close(self):
            pass

    class FakeTmdb:
        def close(self):
            pass

    ctx.make_jellyfin = lambda cfg=None: FakeJf()
    ctx.make_tmdb = lambda cfg=None: FakeTmdb()
    # _do_apply calls make_sources (real build_sources) — stub it out so the
    # worker never touches provider caches during the test.
    ctx.make_sources = lambda cfg=None: {}
    return ctx


# ---------------------------------------------------------------------------
# Profile YAML validation endpoint
# ---------------------------------------------------------------------------

def test_profile_yaml_check_ok_and_errors(tmp_path, monkeypatch):
    ctx = _api_context(tmp_path, monkeypatch, _make_plan(2))
    with TestClient(create_app(ctx)) as client:
        ok = client.post("/api/profiles/standard/yaml",
                         json={"yaml": "elements: [resolution, hdr]\n"})
        assert ok.status_code == 200 and ok.json()["ok"] is True

        bad_key = client.post("/api/profiles/standard/yaml",
                              json={"yaml": "bogus: 1\n"})
        assert bad_key.status_code == 422
        assert "unknown profile keys" in bad_key.json()["detail"]

        bad_els = client.post("/api/profiles/standard/yaml",
                              json={"yaml": "elements: [1, 2]\n"})
        assert bad_els.status_code == 422

        bad_cat = client.post("/api/profiles/standard/yaml",
                              json={"yaml": "format_rules:\n  video: []\n"})
        assert bad_cat.status_code == 422

        bad_yaml = client.post("/api/profiles/standard/yaml",
                               json={"yaml": "elements: [unclosed"})
        assert bad_yaml.status_code == 422


# ---------------------------------------------------------------------------
# Bulk apply: plan covers every item; dry_run blocks apply; one run at a time
# ---------------------------------------------------------------------------

def test_bulk_plan_run_covers_every_item(tmp_path, monkeypatch):
    """Poll tolerating a 404: the worker builds the plan first, which briefly
    leaves no runs row (same race test_dry_run_blocks_apply below already
    handles) -- under CI/load the window is wide enough to flake without
    this, since the very first poll can land before start_run() inserts the
    row at all."""
    ctx = _api_context(tmp_path, monkeypatch, _make_plan(12))
    with TestClient(create_app(ctx)) as client:
        r = client.post("/api/runs", json={"action": "plan", "library": "Movies"})
        assert r.status_code == 202
        job_id = r.json()["job_id"]
        deadline = time.time() + 15
        j = None
        while time.time() < deadline:
            resp = client.get(f"/api/jobs/{job_id}")
            if resp.status_code != 404:
                j = resp.json()
                if j["status"] != "running":
                    break
            time.sleep(0.2)
    assert j is not None, "plan run never appeared in the job table"
    assert j["status"] == "ok"
    # The plan run log records the item count; it must match the library size.
    assert any("items=12" in line["message"] for line in j["log"])
    # files_written/files_unchanged/api_calls are apply-only counters (always
    # 0 for a plan run) -- plan_summary is parsed from that same log line and
    # is the only place the real counts surface to the UI.
    assert j["plan_summary"]["items"] == 12
    assert j["files_written"] == 0


def test_dry_run_blocks_apply(tmp_path, monkeypatch):
    """With dry_run: true the apply run ends 'refused' and writes nothing.

    The worker builds the plan first (which briefly leaves no runs row), so poll
    tolerating a 404 until the row appears."""
    ctx = _api_context(tmp_path, monkeypatch, _make_plan(3))  # config has dry_run: true
    with TestClient(create_app(ctx)) as client:
        r = client.post("/api/runs", json={"action": "apply", "library": "Movies"})
        assert r.status_code == 202
        job_id = r.json()["job_id"]
        # The apply worker builds the plan first (which can take a moment with
        # fakes) before start_run() inserts the runs row, so poll tolerating 404.
        deadline = time.time() + 15
        j = None
        while time.time() < deadline:
            resp = client.get(f"/api/jobs/{job_id}")
            if resp.status_code != 404:
                j = resp.json()
                if j["status"] != "running":
                    break
            time.sleep(0.2)
        assert j is not None, "apply run never appeared in the job table"
        assert j["status"] == "refused"
        assert any("dry_run" in line["message"].lower() or "refused" in line["message"]
                   for line in j["log"])


def _wait_job(client, job_id: str, timeout: float = 15.0):
    deadline = time.time() + timeout
    j = None
    while time.time() < deadline:
        resp = client.get(f"/api/jobs/{job_id}")
        if resp.status_code != 404:
            j = resp.json()
            if j["status"] != "running":
                break
        time.sleep(0.2)
    return j


def test_two_consecutive_applies_distinct_run_ids_both_stored(tmp_path, monkeypatch):
    """S9d2-fix step 4a: two apply runs back-to-back must get distinct run_ids
    and both rows must be stored (previously the second crashed with
    'UNIQUE constraint failed: runs.run_id' when they landed in the same
    second — the run id was time-based only)."""
    from finmeta.engine.jobs import list_runs

    ctx = _api_context(tmp_path, monkeypatch, _make_plan(2))  # dry_run: true -> refused
    with TestClient(create_app(ctx)) as client:
        r1 = client.post("/api/runs", json={"action": "apply", "library": "Movies"})
        assert r1.status_code == 202
        j1 = _wait_job(client, r1.json()["job_id"])
        assert j1 is not None and j1["status"] == "refused"

        # Second apply immediately after the first finished.
        r2 = client.post("/api/runs", json={"action": "apply", "library": "Movies"})
        assert r2.status_code == 202
        j2 = _wait_job(client, r2.json()["job_id"])
        assert j2 is not None and j2["status"] == "refused"

    assert j1["run_id"] != j2["run_id"], (j1["run_id"], j2["run_id"])
    stored = {r["run_id"] for r in list_runs(ctx.db_path)}
    assert j1["run_id"] in stored and j2["run_id"] in stored

def test_one_run_at_a_time(tmp_path, monkeypatch):
    """Poll tolerating a 404: the worker builds the plan first, which briefly
    leaves no runs row (same race test_dry_run_blocks_apply below already
    handles) -- under CI/load the window is wide enough to flake without
    this, since the very first poll can land before start_run() inserts the
    row at all."""
    ctx = _api_context(tmp_path, monkeypatch, _make_plan(2))
    with TestClient(create_app(ctx)) as client:
        r1 = client.post("/api/runs", json={"action": "plan", "library": "Movies"})
        assert r1.status_code == 202
        # While the first run is in flight, a second must be rejected (409).
        r2 = client.post("/api/runs", json={"action": "plan", "library": "Movies"})
        assert r2.status_code == 409
        job_id = r1.json()["job_id"]
        deadline = time.time() + 15
        while time.time() < deadline:
            resp = client.get(f"/api/jobs/{job_id}")
            if resp.status_code != 404:
                j = resp.json()
                if j["status"] != "running":
                    break
            time.sleep(0.2)
    # After the run finishes, a new one is accepted again.
    with TestClient(create_app(ctx)) as client:
        r3 = client.post("/api/runs", json={"action": "plan", "library": "Movies"})
        assert r3.status_code == 202


def test_profile_assignment_roundtrip_via_config_api(tmp_path, monkeypatch):
    """Assigning a profile per library goes through PUT /api/config (validated)."""
    ctx = _api_context(tmp_path, monkeypatch, _make_plan(2))
    with TestClient(create_app(ctx)) as client:
        d = client.get("/api/config").json()
        doc = yaml.safe_load(d["yaml"])
        doc.setdefault("overlays", {})["profiles"] = {"standard": {"elements": ["resolution"]}}
        doc["libraries"]["Movies"]["overlay_profile"] = "standard"
        r = client.put("/api/config", json={"yaml": yaml.dump(doc)})
        assert r.status_code == 200
        # Unknown profile name is a server-side config error.
        doc["libraries"]["Movies"]["overlay_profile"] = "ghost"
        r2 = client.put("/api/config", json={"yaml": yaml.dump(doc)})
        assert r2.status_code == 400


# ---------------------------------------------------------------------------
# S9d2-fix step 4: two libraries with different profiles round-trip
# independently via PUT/GET /api/config (the exact shape the Overlays tab
# writes when a library scope is selected).
# ---------------------------------------------------------------------------

def test_two_libraries_distinct_profiles_roundtrip_independently(tmp_path, monkeypatch):
    """PUT profile A for Movies and profile B for Shows; GET shows both intact.

    Mirrors the UI: overlays.profiles.<name> = {style: ...} plus
    libraries.<lib>.overlay_profile = <name>, one PUT per library.
    """
    ctx = _api_context(tmp_path, monkeypatch, _make_plan(2))
    with TestClient(create_app(ctx)) as client:
        d = client.get("/api/config").json()
        doc = yaml.safe_load(d["yaml"])

        # Add a second library (Shows) to the config.
        doc["libraries"]["Shows"] = {
            "type": "tvshows",
            "enabled": True,
            "overlays": ["resolution"],
        }
        r = client.put("/api/config", json={"yaml": yaml.dump(doc)})
        assert r.status_code == 200

        # UI save for Movies -> profile "movies" with its own style.
        doc.setdefault("overlays", {})["profiles"] = {
            "movies": {"style": {"position": "bottom_left", "margin": 30,
                                 "font_size": 48, "fg": "#FFCC00"}},
        }
        doc["libraries"]["Movies"]["overlay_profile"] = "movies"
        r1 = client.put("/api/config", json={"yaml": yaml.dump(doc)})
        assert r1.status_code == 200

        # UI save for Shows -> profile "shows", different style, Movies intact.
        doc["overlays"]["profiles"]["shows"] = {
            "style": {"position": "top_right", "margin": 10,
                      "font_size": 36, "fg": "#00FF88"},
        }
        doc["libraries"]["Shows"]["overlay_profile"] = "shows"
        r2 = client.put("/api/config", json={"yaml": yaml.dump(doc)})
        assert r2.status_code == 200

        # GET: both profiles present, each library points at its own profile.
        d2 = client.get("/api/config").json()
        doc2 = yaml.safe_load(d2["yaml"])
        profs = doc2["overlays"]["profiles"]
        assert set(profs) == {"movies", "shows"}
        assert profs["movies"]["style"]["position"] == "bottom_left"
        assert profs["movies"]["style"]["fg"] == "#FFCC00"
        assert profs["shows"]["style"]["position"] == "top_right"
        assert profs["shows"]["style"]["font_size"] == 36
        assert doc2["libraries"]["Movies"]["overlay_profile"] == "movies"
        assert doc2["libraries"]["Shows"]["overlay_profile"] == "shows"

        # Independence: re-saving only Movies' profile leaves Shows untouched.
        doc2["overlays"]["profiles"]["movies"]["style"]["margin"] = 99
        r3 = client.put("/api/config", json={"yaml": yaml.dump(doc2)})
        assert r3.status_code == 200
        d3 = client.get("/api/config").json()
        doc3 = yaml.safe_load(d3["yaml"])
        assert doc3["overlays"]["profiles"]["movies"]["style"]["margin"] == 99
        assert doc3["overlays"]["profiles"]["shows"]["style"]["position"] == "top_right"
        assert doc3["libraries"]["Shows"]["overlay_profile"] == "shows"


@pytest.fixture
def persist_ctx(tmp_path, monkeypatch):
    monkeypatch.setenv("FINMETA_AUTH", "off")
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "config.yml").write_text(PERSIST_YAML)
    return AppContext(str(cfg_dir / "config.yml"), config_dir=str(cfg_dir))


@pytest.fixture
def persist_client(persist_ctx):
    from finmeta.api.app import create_app
    app = create_app(persist_ctx)
    with TestClient(app) as c:
        yield c


def test_general_tab_language_and_path_mapping_round_trip(persist_client):
    """S9d2-fix2 step 5d: General-tab edits (language + path_mappings) go through
    PUT /api/config and other fields stay untouched."""
    import yaml as _yaml
    data = _yaml.safe_load(PERSIST_YAML)
    r = persist_client.put("/api/config", json={"yaml": _yaml.safe_dump({**data,
        "language": "nb-NO",
        "path_mappings": [{"jellyfin": "/media", "local": "/srv/media"}]})})
    assert r.status_code == 200, r.text
    parsed = persist_client.get("/api/config").json()["parsed"]
    assert parsed["language"] == "nb-NO"
    assert parsed["path_mappings"] == [{"jellyfin": "/media", "local": "/srv/media"}]
    # untouched fields survive the round trip
    assert parsed["dry_run"] is True
    assert parsed["server"]["url"] == "http://fake:8096"
    assert [i["name"] for i in parsed["media_apps"]["sonarr"]] == ["S1"]
