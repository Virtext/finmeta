"""GET /api/items/{id}/plan must say WHY when the item resolves to nothing,
not just "not in plan" -- specifically the disabled-library case (S9d4c
step 2a: build_plan returns an empty plan + a "library disabled: <name>"
ProviderWarning instead of raising). Before this fix routes_core.item_plan
never looked at plan.provider_warnings, so this case was indistinguishable
from a genuinely wrong/unknown item id -- reported live."""
from __future__ import annotations

from fastapi.testclient import TestClient

from finmeta.api import AppContext, create_app
from finmeta.engine.plan import ProviderWarning, WritePlan

CONFIG_YAML = "\n".join([
    "dry_run: true",
    "server:",
    "  url: http://fake:8096",
    "  api_key_env: JELLYFIN_API_KEY",
]) + "\n"


def _disabled_library_plan(*a, **k) -> WritePlan:
    return WritePlan(
        items=[], unmatched=[],
        provider_warnings=[ProviderWarning(
            provider="config", count=1, error="library disabled: Anime")],
    )


def _plain_empty_plan(*a, **k) -> WritePlan:
    return WritePlan(items=[], unmatched=[], provider_warnings=[])


def _make_client(tmp_path, monkeypatch, plan_builder):
    monkeypatch.setenv("JELLYFIN_API_KEY", "fake-key")
    monkeypatch.setenv("TMDB_KEY", "fake-tmdb-key")
    monkeypatch.setenv("FINMETA_AUTH", "off")
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "config.yml").write_text(CONFIG_YAML)
    ctx = AppContext(
        str(cfg_dir / "config.yml"), config_dir=str(cfg_dir),
        db_path=str(cfg_dir / "jobs.db"), overrides_path=str(cfg_dir / "overrides.db"),
        plan_builder=plan_builder,
    )
    return TestClient(create_app(ctx))


def test_disabled_library_404_names_the_reason(tmp_path, monkeypatch):
    client = _make_client(tmp_path, monkeypatch, _disabled_library_plan)
    r = client.get("/api/items/some-item/plan")
    assert r.status_code == 404
    assert "library disabled: Anime" in r.json()["detail"]


def test_genuinely_unknown_item_still_gets_the_generic_message(tmp_path, monkeypatch):
    """No provider_warning at all -> still the plain fallback, not a false
    "library disabled" claim for an item that's just plain unknown."""
    client = _make_client(tmp_path, monkeypatch, _plain_empty_plan)
    r = client.get("/api/items/some-item/plan")
    assert r.status_code == 404
    assert r.json()["detail"] == "item some-item not in plan"
