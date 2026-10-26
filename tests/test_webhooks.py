"""Jellyfin webhook (2026-10-23): token lifecycle, auth split, notification
filtering and the busy-run queue -- offline, same injected-fakes pattern as
test_api.py. ``_resolve_apply_target`` (the one function that talks to a
real JellyfinClient) is monkeypatched at the route-module level so these
tests never attempt a live Jellyfin call; its own resolution logic (Episode
-> Series, disabled-library filtering) is covered separately below with a
FakeJf, same style as test_pagination.py.
"""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from finmeta.api import AppContext, create_app
from finmeta.engine.plan import FileWrite, ItemPlan, WritePlan

CONFIG_YAML = """\
dry_run: true
server:
  url: http://fake:8096
  api_key_env: JELLYFIN_API_KEY
libraries:
  Movies:
    type: movies
    metadata:
      enabled: true
    artwork: {}
    theme_song: false
    overlays: []
"""


def _fake_plan(cfg, jf, tmdb, sources, library=None, item_id=None, config_dir=None, **kwargs):
    fw = FileWrite(target_path="/tmp/fake/Movie1.nfo", kind="metadata",
                   source_ref="generated", content_sha256="a" * 64, status="create")
    item = ItemPlan(item_id=item_id or "item-1", item_name="Movie 1", media_type="movie",
                    tmdb_id=42, matched_by="provider_id", files=[fw])
    return WritePlan(items=[item])


@pytest.fixture
def context(tmp_path, monkeypatch):
    monkeypatch.setenv("JELLYFIN_API_KEY", "fake-key")
    monkeypatch.setenv("FINMETA_AUTH", "on")  # real session gating for this file
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "config.yml").write_text(CONFIG_YAML)
    return AppContext(
        str(cfg_dir / "config.yml"), config_dir=str(cfg_dir),
        db_path=str(cfg_dir / "jobs.db"), overrides_path=str(cfg_dir / "overrides.db"),
        plan_builder=_fake_plan,
    )


@pytest.fixture
def client(context):
    with TestClient(create_app(context)) as c:
        yield c


# ---------------------------------------------------------------------------
# management endpoints require a session; the receive endpoint doesn't
# ---------------------------------------------------------------------------

def test_status_requires_session(client):
    assert client.get("/api/webhooks/jellyfin/status").status_code == 401


def test_regenerate_requires_session(client):
    assert client.post("/api/webhooks/jellyfin/regenerate").status_code == 401


def test_disable_requires_session(client):
    assert client.delete("/api/webhooks/jellyfin").status_code == 401


def test_receive_works_with_no_session_cookie_at_all(client, monkeypatch):
    monkeypatch.setattr("finmeta.api.routes_webhooks._resolve_apply_target",
                        lambda ctx, item_id: None)
    _session(client)
    token = client.post("/api/webhooks/jellyfin/regenerate").json()["url"].rsplit("/", 1)[-1]
    client.cookies.clear()
    r = client.post(f"/api/webhooks/jellyfin/{token}",
                    json={"NotificationType": "ItemAdded", "ItemId": "x"})
    assert r.status_code == 200


def _session(client) -> None:
    # setup creates the admin password but does not log in by itself --
    # login separately to get a real session cookie for management calls
    # (regenerate/disable/status).
    client.post("/api/auth/setup", json={"password": "correct-horse-battery"})
    r = client.post("/api/auth/login",
                    json={"username": "admin", "password": "correct-horse-battery"})
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# token lifecycle
# ---------------------------------------------------------------------------

def test_wrong_token_is_404_not_401(client):
    _session(client)
    client.post("/api/webhooks/jellyfin/regenerate")
    r = client.post("/api/webhooks/jellyfin/not-the-right-token",
                    json={"NotificationType": "ItemAdded", "ItemId": "x"})
    assert r.status_code == 404


def test_no_token_configured_is_404(client):
    _session(client)
    r = client.post("/api/webhooks/jellyfin/anything",
                    json={"NotificationType": "ItemAdded", "ItemId": "x"})
    assert r.status_code == 404


def test_regenerate_then_disable_invalidates_old_token(client):
    _session(client)
    r = client.post("/api/webhooks/jellyfin/regenerate")
    token = r.json()["url"].rsplit("/", 1)[-1]
    assert client.get("/api/webhooks/jellyfin/status").json()["configured"] is True
    client.delete("/api/webhooks/jellyfin")
    assert client.get("/api/webhooks/jellyfin/status").json()["configured"] is False
    r2 = client.post(f"/api/webhooks/jellyfin/{token}",
                     json={"NotificationType": "ItemAdded", "ItemId": "x"})
    assert r2.status_code == 404


def test_regenerate_rotates_the_token(client):
    _session(client)
    first = client.post("/api/webhooks/jellyfin/regenerate").json()["url"].rsplit("/", 1)[-1]
    second = client.post("/api/webhooks/jellyfin/regenerate").json()["url"].rsplit("/", 1)[-1]
    assert first != second
    r = client.post(f"/api/webhooks/jellyfin/{first}",
                    json={"NotificationType": "ItemAdded", "ItemId": "x"})
    assert r.status_code == 404  # old token no longer valid


# ---------------------------------------------------------------------------
# payload handling (token resolution mocked so these are pure route-logic tests)
# ---------------------------------------------------------------------------

def test_non_item_added_notification_is_skipped(client, monkeypatch):
    _session(client)
    called = []
    monkeypatch.setattr("finmeta.api.routes_webhooks._resolve_apply_target",
                        lambda ctx, item_id: called.append(item_id) or "resolved-id")
    token = client.post("/api/webhooks/jellyfin/regenerate").json()["url"].rsplit("/", 1)[-1]
    r = client.post(f"/api/webhooks/jellyfin/{token}",
                    json={"NotificationType": "PlaybackStart", "ItemId": "x"})
    assert r.json() == {"ok": True, "skipped": "ignored notification type 'PlaybackStart'"}
    assert called == []


def test_unresolvable_item_is_skipped(client, monkeypatch):
    _session(client)
    monkeypatch.setattr("finmeta.api.routes_webhooks._resolve_apply_target",
                        lambda ctx, item_id: None)
    token = client.post("/api/webhooks/jellyfin/regenerate").json()["url"].rsplit("/", 1)[-1]
    r = client.post(f"/api/webhooks/jellyfin/{token}",
                    json={"NotificationType": "ItemAdded", "ItemId": "x"})
    assert r.json() == {"ok": True, "skipped": "item not in a Finmeta-managed library"}


def test_item_added_triggers_a_run(client, monkeypatch):
    _session(client)
    monkeypatch.setattr("finmeta.api.routes_webhooks._resolve_apply_target",
                        lambda ctx, item_id: "resolved-id")
    token = client.post("/api/webhooks/jellyfin/regenerate").json()["url"].rsplit("/", 1)[-1]
    r = client.post(f"/api/webhooks/jellyfin/{token}",
                    json={"NotificationType": "ItemAdded", "ItemId": "x"})
    body = r.json()
    assert body["ok"] is True
    assert body["run_id"]
    time.sleep(0.2)  # let the background worker release the run slot


def test_case_insensitive_payload_keys(client, monkeypatch):
    """Jellyfin's plugin sends PascalCase, but a custom template could send
    anything -- the lookup must not care."""
    _session(client)
    monkeypatch.setattr("finmeta.api.routes_webhooks._resolve_apply_target",
                        lambda ctx, item_id: "resolved-id")
    token = client.post("/api/webhooks/jellyfin/regenerate").json()["url"].rsplit("/", 1)[-1]
    r = client.post(f"/api/webhooks/jellyfin/{token}",
                    json={"notificationtype": "itemadded", "itemid": "x"})
    assert r.json()["ok"] is True
    assert "run_id" in r.json()
    time.sleep(0.2)


def test_busy_run_queues_instead_of_dropping(client, monkeypatch):
    _session(client)
    monkeypatch.setattr("finmeta.api.routes_webhooks._resolve_apply_target",
                        lambda ctx, item_id: "resolved-id")
    # Occupy the single run slot directly (no real run needed for this check).
    assert client.app.state.context.claim_run("held-by-test")
    token = client.post("/api/webhooks/jellyfin/regenerate").json()["url"].rsplit("/", 1)[-1]
    r = client.post(f"/api/webhooks/jellyfin/{token}",
                    json={"NotificationType": "ItemAdded", "ItemId": "x"})
    assert r.json() == {"ok": True, "queued": True}
    client.app.state.context.release_run("held-by-test")


# ---------------------------------------------------------------------------
# _resolve_apply_target: Episode -> Series, disabled-library filtering
# ---------------------------------------------------------------------------

from typing import Dict, List, Optional
from finmeta.jellyfin_client.types import Item, Library
from finmeta.api.routes_webhooks import _resolve_apply_target
from finmeta.config.models import Config, Metadata, MovieArtwork, ServerConfig, TvArtwork, TvLibrary


class FakeJf:
    def __init__(self, items: Dict[str, Item], libraries: Dict[str, Library]):
        self._items = items
        self._libs = libraries

    def get_item(self, item_id: str) -> Item:
        return self._items[item_id]

    def get_libraries(self) -> List[Library]:
        return list(self._libs.values())

    def close(self):
        pass


class FakeCtx:
    def __init__(self, jf: FakeJf, cfg: Config):
        self._jf = jf
        self.config = cfg

    def make_jellyfin(self):
        return self._jf


def _tv_cfg(enabled: bool) -> Config:
    lib = TvLibrary(type="tvshows", enabled=enabled,
                    metadata=Metadata(enabled=False), artwork=TvArtwork())
    return Config(dry_run=True, server=ServerConfig(url="http://x:8096"),
                  libraries={"Shows": lib})


def test_episode_resolves_to_its_series():
    series = Item(id="s1", name="Show", type="Series", path="/media/shows/Show",
                  provider_ids={})
    episode = Item(id="e1", name="S01E01", type="Episode", path="/media/shows/Show/e1.mkv",
                  provider_ids={}, series_id="s1")
    jf = FakeJf({"e1": episode, "s1": series},
               {"Shows": Library(name="Shows", item_id="lib-shows", collection_type="tvshows",
                                  locations=["/media/shows"])})
    target = _resolve_apply_target(FakeCtx(jf, _tv_cfg(enabled=True)), "e1")
    assert target == "s1"


def test_disabled_library_is_ignored():
    series = Item(id="s1", name="Show", type="Series", path="/media/shows/Show",
                  provider_ids={})
    jf = FakeJf({"s1": series},
               {"Shows": Library(name="Shows", item_id="lib-shows", collection_type="tvshows",
                                  locations=["/media/shows"])})
    target = _resolve_apply_target(FakeCtx(jf, _tv_cfg(enabled=False)), "s1")
    assert target is None


def test_unknown_item_id_returns_none():
    jf = FakeJf({}, {})
    target = _resolve_apply_target(FakeCtx(jf, _tv_cfg(enabled=True)), "missing")
    assert target is None


def test_season_item_type_returns_none():
    season = Item(id="se1", name="Season 1", type="Season", path="/media/shows/Show/Season 1",
                 provider_ids={})
    jf = FakeJf({"se1": season}, {})
    target = _resolve_apply_target(FakeCtx(jf, _tv_cfg(enabled=True)), "se1")
    assert target is None
