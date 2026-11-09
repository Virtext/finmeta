"""Help system tests (S9 §B): fail if any config field or provider lacks help."""
from fastapi.testclient import TestClient

from finmeta.api import AppContext, create_app
from finmeta.api.help import (
    iter_config_fields, config_fields_missing_help, providers_missing_help,
    features_missing_help, build_help, config_help,
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


# --- S13b item 3: reset-to-default, defaults sourced from the Pydantic schema
# itself (never a hand-written list) ------------------------------------------

def test_required_field_has_no_default():
    # server.url has no default -- there's nothing to "reset" it to.
    h = config_help()
    assert h["server.url"]["has_default"] is False
    assert h["server.url"]["default"] is None


def test_scalar_field_default_matches_pydantic():
    h = config_help()
    assert h["dry_run"]["has_default"] is True
    assert h["dry_run"]["default"] is True   # Config.dry_run defaults to True
    assert h["server.timeout"]["has_default"] is True
    assert h["server.timeout"]["default"] == 30


def test_default_factory_field_is_called_and_json_safe():
    h = config_help()
    # trending.lists: List[TrendingListConfig] = Field(default_factory=list)
    assert h["trending.lists"]["has_default"] is True
    assert h["trending.lists"]["default"] == []


def test_tiered_fields_use_one_of_the_three_known_values():
    # S13b item 6: tier is hand-tagged (json_schema_extra={"tier": ...}),
    # unlike has_default/default -- nothing stops a typo, so this is the
    # guard. Untagged fields simply omit the key (never a bare None).
    h = config_help()
    seen_tiers = set()
    for path, entry in h.items():
        if "tier" in entry:
            seen_tiers.add(entry["tier"])
    assert seen_tiers <= {"basic", "advanced", "expert"}, f"unknown tier value(s): {seen_tiers}"
    # At least one real field proposal landed in each tier (sanity, not an
    # exhaustive classification -- most of the 159 fields are untagged).
    assert seen_tiers == {"basic", "advanced", "expert"}


def test_known_field_tiers_match_the_first_proposal():
    h = config_help()
    assert h["dry_run"]["tier"] == "basic"
    assert h["server.url"]["tier"] == "basic"
    assert h["server.timeout"]["tier"] == "advanced"
    assert h["artwork_write_method"]["tier"] == "expert"
    assert "tier" not in h["server.api_key_env"]   # untagged -- no opinion yet


def test_help_route_includes_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("FINMETA_AUTH", "off")
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "config.yml").write_text(
        "dry_run: true\nserver:\n  url: http://fake\nlibraries:\n  Movies:\n    type: movies\n")
    ctx = AppContext(str(cfg_dir / "config.yml"), config_dir=str(cfg_dir),
                     db_path=str(cfg_dir / "j.db"), overrides_path=str(cfg_dir / "o.db"),
                     plan_builder=lambda *a, **k: None)
    with TestClient(create_app(ctx)) as c:
        h = c.get("/api/help").json()["help"]
        assert h["dry_run"]["has_default"] is True
        assert h["server.url"]["has_default"] is False


# --- S10b: the health scanner's help keys the UI reads ----------------------
def test_every_scan_check_group_has_feature_help():
    """HealthPage renders `feature.scan.<group>` per findings group, so a new
    check group must come with help text instead of an empty (i) bubble."""
    from finmeta.engine.scanner import CHECK_GROUPS

    cfg = Config(server={"url": "http://x"})
    help_map = build_help(cfg)
    expected = [f"feature.scan.{g}" for g in CHECK_GROUPS]
    expected.append("feature.scan.path_unmapped")   # always-on check, no config switch
    expected.append("feature.page.health")
    missing = [k for k in expected if not (help_map.get(k, {}).get("long") or "").strip()]
    assert missing == [], f"scanner help missing: {missing}"
