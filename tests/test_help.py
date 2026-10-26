"""Help system tests (S9 §B): fail if any config field or provider lacks help."""
from fastapi.testclient import TestClient

from finmeta.api import AppContext, create_app
from finmeta.api.help import (
    iter_config_fields, config_fields_missing_help, providers_missing_help,
    features_missing_help, build_help,
)
from finmeta.config.models import Config


def test_every_config_field_has_help():
    fields = list(iter_config_fields(Config))
    # guard: the walker must actually find the config surface
    assert len(fields) >= 40, f"walker found too few fields: {len(fields)}"
    missing = config_fields_missing_help()
    assert missing == [], f"config fields missing help: {missing}"


def test_every_field_has_short_and_long():
    missing = []
    for path, info in iter_config_fields(Config):
        extra = info.json_schema_extra or {}
        if not (info.description or "").strip():
            missing.append((path, "short"))
        if not (extra.get("help") or "").strip():
            missing.append((path, "long"))
    assert missing == [], f"fields missing short/long help: {missing}"


def test_every_provider_has_help():
    assert providers_missing_help() == []


def test_every_feature_has_help():
    assert features_missing_help() == []


def test_build_help_covers_all_fields():
    cfg = Config(server={"url": "http://x"})
    help_map = build_help(cfg)
    for path, _info in iter_config_fields(Config):
        assert path in help_map, f"help missing for field {path}"


def test_help_route_serves_all(tmp_path, monkeypatch):
    monkeypatch.setenv("FINMETA_AUTH", "off")
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "config.yml").write_text(
        "dry_run: true\nserver:\n  url: http://fake\nlibraries:\n  Movies:\n    type: movies\n")
    ctx = AppContext(str(cfg_dir / "config.yml"), config_dir=str(cfg_dir),
                     db_path=str(cfg_dir / "j.db"), overrides_path=str(cfg_dir / "o.db"),
                     plan_builder=lambda *a, **k: None)
    with TestClient(create_app(ctx)) as c:
        r = c.get("/api/help")
        assert r.status_code == 200
        h = r.json()["help"]
        assert h["dry_run"]["long"].strip()
        assert "provider.tmdb" in h and "provider.fanart" in h
        assert "secret.tmdb_key" in h and "feature.auth" in h
