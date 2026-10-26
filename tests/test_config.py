"""Config model tests: valid/invalid configs, schema up-to-date (SPEC §3)."""
from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest
import yaml

from finmeta.config import load_config
from finmeta.config.loading import ConfigError, resolve_env
from finmeta.config.schema import export_schema, schema_is_current
from finmeta.config.models import Config

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = REPO_ROOT / "config" / "config.template.yml"
SCHEMA = REPO_ROOT / "config" / "schema.json"

BASE = {"server": {"url": "http://jellyfin:8096"}}


def _ok(cfg):
    return Config(**cfg)


def _fail(cfg):
    with pytest.raises(Exception):
        Config(**cfg)


# ---------------------------------------------------------------------------
# template + schema
# ---------------------------------------------------------------------------

def test_template_loads_and_dry_run_default():
    cfg = load_config(TEMPLATE)
    assert cfg.dry_run is True
    assert cfg.server.url == "http://jellyfin:8096"
    assert cfg.sync_mode == "sync"


def test_template_validates_against_exported_schema():
    schema = json.loads(SCHEMA.read_text())
    data = yaml.safe_load(TEMPLATE.read_text())
    jsonschema.validate(data, schema)


def test_schema_is_current():
    assert schema_is_current(SCHEMA)


def test_export_schema_is_stable(tmp_path):
    a = tmp_path / "a.json"
    b = tmp_path / "b.json"
    export_schema(a)
    export_schema(b)
    assert a.read_text() == b.read_text()
    assert a.read_text().endswith("\n")


def test_schema_detects_staleness(tmp_path):
    # a hand-edited (wrong) schema file must not be "current"
    p = tmp_path / "schema.json"
    bad = json.loads(SCHEMA.read_text())
    bad["title"] = "tampered"
    p.write_text(json.dumps(bad, indent=2, sort_keys=True) + "\n")
    assert not schema_is_current(p)


# ---------------------------------------------------------------------------
# valid shapes (discriminated unions)
# ---------------------------------------------------------------------------

def test_movies_library_shape():
    c = _ok({**BASE, "libraries": {"M": {"type": "movies",
                                         "artwork": {"poster": True, "landscape": True}}}})
    assert c.libraries["M"].artwork.model_dump() == \
        {"poster": True, "backdrop": False, "logo": False, "landscape": True}


def test_tvshow_library_shape():
    c = _ok({**BASE, "libraries": {"S": {"type": "tvshows",
                                         "artwork": {"season_posters": True}}}})
    assert c.libraries["S"].artwork.season_posters is True
    assert c.libraries["S"].artwork.episode_thumbs is False


def test_collection_types():
    c = _ok({**BASE, "libraries": {"M": {"type": "movies"}}, "collections": [
        {"name": "a", "type": "tmdb_collection", "library": "M", "min_items": 3},
        {"name": "b", "type": "filter", "library": "M",
         "filter": {"year_from": 1990, "min_rating": 7.0}},
        {"name": "c", "type": "list", "library": "M", "tmdb_ids": [1, 2]},
    ]})
    assert [x.type for x in c.collections] == ["tmdb_collection", "filter", "list"]
    assert c.collections[1].filter.year_from == 1990
    assert c.collections[2].tmdb_ids == [1, 2]


def test_collection_presets_defaults_empty():
    assert _ok(BASE).collection_presets == []


def test_collection_presets_roundtrip(tmp_path):
    """S9f2: collection_presets is a plain id list; unknown ids are a config
    concern for the engine to ignore (see _build_api_actions), not rejected
    here -- that lets a preset be removed from KNOWN_PRESETS later without
    turning every existing config into a hard validation error."""
    p = tmp_path / "cfg.yml"
    p.write_text(yaml.safe_dump({
        **BASE, "collection_presets": ["halloween", "top_popular_movies"],
    }))
    cfg = load_config(p)
    assert cfg.collection_presets == ["halloween", "top_popular_movies"]


def test_sources_precedence_defaults():
    c = _ok(BASE)
    assert c.sources.precedence == ["local", "fanart", "tmdb"]
    assert c.sources.tmdb.enabled is True


# ---------------------------------------------------------------------------
# invalid configs (each must raise)
# ---------------------------------------------------------------------------

def test_unknown_top_level_key_rejected():
    _fail({**BASE, "bogus": 1})


def test_unknown_nested_key_rejected():
    _fail({**BASE, "sources": {"tmdb": {"enabled": True, "bogus": 1}}})


def test_movies_artwork_rejects_tv_only_field():
    _fail({**BASE, "libraries": {"M": {"type": "movies",
                                       "artwork": {"poster": True, "season_posters": True}}}})


def test_tv_artwork_rejects_movie_only_field():
    _fail({**BASE, "libraries": {"S": {"type": "tvshows",
                                       "artwork": {"poster": True, "landscape": True}}}})


def test_bad_library_type_rejected():
    _fail({**BASE, "libraries": {"M": {"type": "games"}}})


def test_bad_cron_rejected():
    _fail({**BASE, "schedules": [{"cron": "not a cron", "libraries": []}]})


def test_valid_cron_accepted():
    _ok({**BASE, "schedules": [{"cron": "0 4 * * *", "libraries": []}]})


def test_collection_bad_library_rejected():
    _fail({**BASE, "libraries": {"M": {"type": "movies"}},
           "collections": [{"name": "c", "type": "list", "library": "Nope", "tmdb_ids": [1]}]})


def test_playlist_bad_library_rejected():
    _fail({**BASE, "libraries": {"M": {"type": "movies"}},
           "playlists": [{"name": "p", "user": "u", "library": "Nope"}]})


def test_schedule_bad_library_rejected():
    _fail({**BASE, "libraries": {"M": {"type": "movies"}},
           "schedules": [{"cron": "0 4 * * *", "libraries": ["Nope"]}]})


def test_bad_precedence_rejected():
    _fail({**BASE, "sources": {"precedence": ["local", "bogus"]}})


def test_duplicate_precedence_rejected():
    _fail({**BASE, "sources": {"precedence": ["local", "local"]}})


def test_bad_sync_mode_rejected():
    _fail({**BASE, "sync_mode": "overwrite"})


# ---------------------------------------------------------------------------
# file loading + env resolution
# ---------------------------------------------------------------------------

def test_load_config_roundtrip(tmp_path):
    p = tmp_path / "cfg.yml"
    p.write_text(yaml.safe_dump({"server": {"url": "http://x"}, "dry_run": False}))
    cfg = load_config(p)
    assert cfg.dry_run is False
    assert cfg.server.url == "http://x"


def test_load_config_unknown_key_raises(tmp_path):
    p = tmp_path / "cfg.yml"
    p.write_text(yaml.safe_dump({"server": {"url": "http://x"}, "bogus": 1}))
    with pytest.raises(ConfigError):
        load_config(p)


def test_resolve_env_missing_names_var(monkeypatch):
    monkeypatch.delenv("SOME_MISSING_VAR", raising=False)
    with pytest.raises(ConfigError) as ei:
        resolve_env("SOME_MISSING_VAR")
    assert "SOME_MISSING_VAR" in str(ei.value)
    assert "not set" in str(ei.value)


def test_resolve_env_returns_value(monkeypatch):
    monkeypatch.setenv("SOME_SET_VAR", "secret-value")
    assert resolve_env("SOME_SET_VAR") == "secret-value"


# ---------------------------------------------------------------------------
# S9b2: ServerConfig base_path/verify_ssl/timeout + media_apps
# ---------------------------------------------------------------------------

def test_server_config_connection_defaults():
    cfg = _ok(BASE)
    assert cfg.server.base_path is None
    assert cfg.server.verify_ssl is True
    assert cfg.server.timeout == 30.0


def test_server_config_accepts_connection_fields():
    cfg = _ok({"server": {"url": "http://home.example.com",
                          "base_path": "/jellyfin", "verify_ssl": False,
                          "timeout": 10.0}})
    assert cfg.server.base_path == "/jellyfin"
    assert cfg.server.verify_ssl is False
    assert cfg.server.timeout == 10.0


def test_server_config_timeout_must_be_positive():
    _fail({"server": {"url": "http://x", "timeout": 0}})
    _fail({"server": {"url": "http://x", "timeout": -1}})


def test_media_apps_default_empty():
    cfg = _ok(BASE)
    assert cfg.media_apps.sonarr == []
    assert cfg.media_apps.radarr == []


def test_media_apps_valid_instances():
    cfg = _ok({"server": {"url": "http://jellyfin:8096"},
               "media_apps": {
                   "sonarr": [{"name": "Sonarr", "url": "http://192.168.1.10:8989",
                               "api_key": "sonarr_key"}],
                   "radarr": [
                       {"name": "Radarr", "url": "http://192.168.1.10:7878",
                        "api_key": "radarr_key"},
                       {"name": "Radarr4K", "url": "https://radarr.example.com",
                        "verify_ssl": False, "timeout": 15.0, "enabled": False,
                        "api_key": "radarr4k_key"},
                   ]}})
    assert [i.name for i in cfg.media_apps.radarr] == ["Radarr", "Radarr4K"]
    r4k = cfg.media_apps.radarr[1]
    assert r4k.verify_ssl is False and r4k.timeout == 15.0 and r4k.enabled is False


def test_media_apps_migrates_legacy_instances():
    """The S9b2 single ``instances`` list migrates into sonarr/radarr by name."""
    cfg = _ok({"server": {"url": "http://jellyfin:8096"},
               "media_apps": {"instances": [
                   {"name": "Sonarr", "url": "http://192.168.1.10:8989",
                    "api_key": "sonarr_key"},
                   {"name": "Radarr4K", "url": "https://radarr.example.com",
                    "api_key": "radarr4k_key"},
               ]}})
    assert [i.name for i in cfg.media_apps.sonarr] == ["Sonarr"]
    assert [i.name for i in cfg.media_apps.radarr] == ["Radarr4K"]


def test_media_apps_rejects_unknown_instance_key():
    _fail({"server": {"url": "http://x"},
           "media_apps": {"radarr": [
               {"name": "R", "url": "http://x", "bogus": 1}]}})


def test_media_apps_rejects_missing_required():
    _fail({"server": {"url": "http://x"},
           "media_apps": {"sonarr": [{"name": "S"}]}})


def test_media_apps_rejects_duplicate_name_within_kind():
    # S9f1a-fix: uniqueness is required WITHIN a kind (sonarr vs radarr may reuse names).
    _fail({"server": {"url": "http://x"},
           "media_apps": {
               "sonarr": [{"name": "X", "url": "http://a", "api_key": "k"},
                          {"name": "X", "url": "http://b", "api_key": "k2"}]}})


def test_media_apps_allows_same_name_across_kinds():
    # A Sonarr and a Radarr instance may share a name (they are different kinds).
    from finmeta.config.models import Config
    cfg = Config(server={"url": "http://x"},
                 media_apps={
                     "sonarr": [{"name": "X", "url": "http://a", "api_key": "k"}],
                     "radarr": [{"name": "X", "url": "http://b", "api_key": "k2"}]})
    assert [i.name for i in cfg.media_apps.sonarr] == ["X"]
    assert [i.name for i in cfg.media_apps.radarr] == ["X"]
