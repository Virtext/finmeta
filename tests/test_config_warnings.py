"""S13b item 2: dependency warnings from api.config_warnings."""
from __future__ import annotations

import pytest

from finmeta.api.config_warnings import config_warnings
from finmeta.api.secrets import SecretsStore, secret_specs
from finmeta.config.models import Config, Metadata, MovieArtwork, MovieLibrary, ServerConfig


def _cfg(**kw) -> Config:
    base = dict(dry_run=True, server=ServerConfig(url="http://x:8096"))
    base.update(kw)
    return Config(**base)


@pytest.fixture
def store(tmp_path, monkeypatch):
    # secret_status also honours env vars -- make sure none leak in from the host.
    for s in secret_specs(_cfg(media_apps={"sonarr": [
            {"name": "main", "url": "http://s:8989", "api_key": "sonarr_main_api_key"}]})):
        monkeypatch.delenv(s.env_var, raising=False)
    return SecretsStore(tmp_path / "secrets.yml", secret_key=None)


def _paths(cfg, store):
    return [w["path"] for w in config_warnings(cfg, store)]


def test_enabled_source_without_key_warns_and_key_clears_it(store):
    cfg = _cfg()
    assert "sources.tmdb.enabled" in _paths(cfg, store)
    store.set("tmdb_key", "x" * 32)
    assert "sources.tmdb.enabled" not in _paths(cfg, store)


def test_disabled_source_without_key_is_quiet(store):
    cfg = _cfg(sources={"fanart": {"enabled": False}})
    assert "sources.fanart.enabled" not in _paths(cfg, store)


def test_trending_needs_mdblist_and_empty_url_warns(store):
    cfg = _cfg(trending={"lists": [{"url": " ", "collection_name": "Hot"}]})
    paths = _paths(cfg, store)
    assert "trending.lists" in paths
    assert "trending.lists[0].url" in paths


def test_status_element_without_sonarr_warns(store):
    cfg = _cfg(overlays={"profiles": {"tv": {"elements": ["status"]}}})
    assert "status_overlay" in _paths(cfg, store)


def test_sonarr_instance_without_key_warns_then_with_key_status_ok(store):
    cfg = _cfg(media_apps={"sonarr": [
        {"name": "main", "url": "http://s:8989", "api_key": "sonarr_main_api_key"}]},
        status_overlay={"create_collections": True})
    paths = _paths(cfg, store)
    assert "media_apps.sonarr" in paths and "status_overlay" in paths
    store.set("sonarr_main_api_key", "k" * 32)
    paths = _paths(cfg, store)
    assert "media_apps.sonarr" not in paths and "status_overlay" not in paths


def test_collection_on_disabled_library_warns(store):
    lib = MovieLibrary(type="movies", enabled=False, metadata=Metadata(enabled=False),
                       artwork=MovieArtwork())
    cfg = _cfg(libraries={"Movies": lib}, collections=[
        {"type": "list", "name": "Picks", "library": "Movies", "tmdb_ids": [1]}])
    assert "collections" in _paths(cfg, store)


def test_no_secret_value_in_output(store):
    store.set("omdb_key", "SUPERSECRETVALUE123")
    cfg = _cfg(sources={"omdb": {"enabled": True}})
    assert "SUPERSECRETVALUE123" not in repr(config_warnings(cfg, store))
