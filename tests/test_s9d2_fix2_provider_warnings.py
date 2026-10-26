"""S9d2-fix2 step 6b: provider errors must not be silent.

TMDb 401 degrades to "no candidates" but the plan carries
``provider_warnings: [{provider, error, count}]`` and the run log shows it."""
from __future__ import annotations

import pytest

from finmeta.engine.resolution import _query_source, resolve_candidates
from finmeta.sources.tmdb import TmdbError

# Reuse the API-level fixtures defined in test_api (S9d2-fix2 step 6b).
from tests.test_api import tmp_config  # noqa: F401


class FakeTmdb:
    def movie_images(self, tmdb_id, language=None, language_fallback=None, kinds=None):
        raise TmdbError("TMDb GET /movie/1/images -> HTTP 401")

    tv_images = movie_images


def test_query_source_records_tmdb_401_warning():
    warnings: list = []
    cands = _query_source("tmdb", FakeTmdb(), "movie", 1, "poster", None, [],
                          warnings=warnings)
    assert cands == []
    assert warnings == [{"provider": "tmdb",
                         "error": "TMDb GET /movie/1/images -> HTTP 401",
                         "count": 1}]


def test_query_source_counts_repeated_errors():
    warnings: list = []
    for _ in range(3):
        _query_source("tmdb", FakeTmdb(), "tv", 2, "backdrop", None, [],
                      warnings=warnings)
    assert len(warnings) == 1
    assert warnings[0]["count"] == 3


def test_resolve_candidates_passes_warnings_through():
    warnings: list = []
    result = resolve_candidates(
        {"tmdb": FakeTmdb()}, ["tmdb"], "movie", 1, ["poster", "backdrop"],
        warnings=warnings)
    assert result == {}
    # one failed lookup per kind
    assert sum(w["count"] for w in warnings) == 2


def test_no_warnings_when_source_ok():
    class OkTmdb:
        def movie_images(self, tmdb_id, language=None, language_fallback=None, kinds=None):
            return []

    warnings: list = []
    resolve_candidates({"tmdb": OkTmdb()}, ["tmdb"], "movie", 1, ["poster"],
                       warnings=warnings)
    assert warnings == []


def test_write_plan_carries_provider_warnings():
    from finmeta.engine.plan import ProviderWarning, WritePlan
    plan = WritePlan(provider_warnings=[ProviderWarning(
        provider="tmdb", error="TMDb GET /movie/1/images -> HTTP 401", count=2)])
    dumped = plan.model_dump()
    assert dumped["provider_warnings"] == [
        {"provider": "tmdb",
         "error": "TMDb GET /movie/1/images -> HTTP 401", "count": 2}]


def test_items_endpoint_exposes_provider_warnings(tmp_config, monkeypatch):
    """API level: plan with tmdb 401 warnings -> /api/libraries/{name}/items
    response carries provider_warnings (visible in the UI plan list)."""
    import pytest
    from fastapi.testclient import TestClient
    from finmeta.api.app import create_app
    from finmeta.api.state import AppContext

    ctx = AppContext(
        str(tmp_config / "config.yml"),
        config_dir=str(tmp_config),
        db_path=str(tmp_config / "jobs.db"),
        overrides_path=str(tmp_config / "overrides.db"),
        plan_builder=lambda *a, **k: (_ for _ in ()).throw(AssertionError("unused")),
    )
    # Replace the plan builder with one that returns a plan containing warnings.
    from finmeta.engine.plan import ProviderWarning, WritePlan

    def fake_plan(cfg, jf, tmdb, sources, library=None, item_id=None, config_dir=None, **kwargs):
        return WritePlan(provider_warnings=[ProviderWarning(
            provider="tmdb", error="TMDb GET /movie/1/images -> HTTP 401",
            count=2)])

    ctx._plan_builder = fake_plan
    with TestClient(create_app(ctx)) as client:
        r = client.get("/api/libraries/Movies/items")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["provider_warnings"] == [
        {"provider": "tmdb",
         "error": "TMDb GET /movie/1/images -> HTTP 401", "count": 2}]
