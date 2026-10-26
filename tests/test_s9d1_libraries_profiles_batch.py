"""S9d1: Jellyfin libraries (K8), per-library overlay profiles, batched streams.

Offline tests: respx for the Jellyfin client, TestClient with fakes for the API.
The live test server (port 8097) exercises /api/jellyfin/libraries in
tests/test_api_live.py (marked ``live``).
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Dict, List

import httpx
import pytest
import respx
import yaml

from finmeta.api import AppContext, create_app
from finmeta.config.models import Config, MovieLibrary, OverlaysConfig
from finmeta.engine.plan import FileWrite, ItemPlan, WritePlan
from finmeta.jellyfin_client.client import JellyfinClient
from fastapi.testclient import TestClient

BASE = "http://jf:8096"


# ---------------------------------------------------------------------------
# K8: libraries from the real Jellyfin API (respx)
# ---------------------------------------------------------------------------

class FakeJf:
    """Minimal stand-in exposing get_libraries like the real client."""

    def __init__(self, libs):
        self._libs = libs
        self.closed = False

    def get_libraries(self):
        from finmeta.jellyfin_client.types import Library
        return [Library.from_dict(d) for d in self._libs]

    def close(self):
        self.closed = True


def _api_context(tmp_path, monkeypatch, jf):
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
  Anime:
    type: movies
    enabled: false
    overlays: []
overlays:
  profiles:
    standard:
      elements: [resolution, hdr, audio]
""")

    def _fake_plan(cfg, jf, tmdb, sources, library=None, item_id=None, config_dir=None, **kwargs):
        return WritePlan(items=[])

    ctx = AppContext(
        str(cfg_file), config_dir=str(cfg_dir),
        db_path=str(cfg_dir / "jobs.db"), overrides_path=str(cfg_dir / "overrides.db"),
        plan_builder=_fake_plan,
    )
    ctx.make_jellyfin = lambda cfg=None: jf
    return ctx


def test_jellyfin_libraries_endpoint_lists_real_libs(tmp_path, monkeypatch):
    jf = FakeJf([
        {"Name": "Movies", "ItemId": "lib-movies", "CollectionType": "movies",
         "Locations": ["/media/movies"]},
        {"Name": "Shows", "ItemId": "lib-shows", "CollectionType": "tvshows",
         "Locations": ["/media/tv"]},
        {"Name": "Anime", "ItemId": "lib-anime", "CollectionType": "mixed",
         "Locations": ["/media/anime"]},
    ])
    ctx = _api_context(tmp_path, monkeypatch, jf)
    with TestClient(create_app(ctx)) as client:
        r = client.get("/api/jellyfin/libraries")
    assert r.status_code == 200
    body = r.json()
    names = [l["name"] for l in body["libraries"]]
    # Any number of libraries, straight from Jellyfin — no Movies/TV assumption.
    assert names == ["Movies", "Shows", "Anime"]
    by_name = {l["name"]: l for l in body["libraries"]}
    assert by_name["Movies"]["id"] == "lib-movies"
    assert by_name["Movies"]["collection_type"] == "movies"
    assert by_name["Movies"]["configured"] is True
    assert by_name["Movies"]["enabled"] is True
    # Shows exists in Jellyfin but NOT in config -> untouched.
    assert by_name["Shows"]["configured"] is False
    assert by_name["Shows"]["enabled"] is False
    assert by_name["Shows"]["overlay_profile"] is None
    # Anime is configured but disabled (v1 behavior: off, untouched).
    assert by_name["Anime"]["configured"] is True
    assert by_name["Anime"]["enabled"] is False


def test_jellyfin_libraries_error_maps_to_502(tmp_path, monkeypatch):
    class BoomJf:
        def get_libraries(self):
            raise RuntimeError("connection refused")

        def close(self):
            pass

    ctx = _api_context(tmp_path, monkeypatch, BoomJf())
    with TestClient(create_app(ctx)) as client:
        r = client.get("/api/jellyfin/libraries")
    assert r.status_code == 502


# ---------------------------------------------------------------------------
# Overlay profiles (config model + plan behavior)
# ---------------------------------------------------------------------------

def test_profile_reference_validates():
    cfg = Config(
        dry_run=True, server={"url": "http://x:8096"},
        libraries={"Movies": MovieLibrary(
            type="movies", overlay_profile="standard",
            overlays=["resolution"])},
        overlays=OverlaysConfig(profiles={"standard": {"elements": ["resolution", "hdr"]}}),
    )
    assert cfg.libraries["Movies"].overlay_profile == "standard"


# pack_overrides shape (S9d3 part B1)


def test_pack_overrides_flat_shape_rejected():
    """Old flat ``pack_overrides.<category>: false`` must fail validation.

    The D3 shape is exactly ``pack_overrides.<pack>.<category>.enabled``;
    the bad key is named in the error, not silently ignored."""
    with pytest.raises(Exception) as ei:
        OverlaysConfig(
            profiles={"standard": {
                "elements": ["resolution"],
                "pack_overrides": {"rating": False}}})
    msg = str(ei.value)
    assert "pack_overrides" in msg
    assert "'rating'" in msg  # names the bad key


def test_pack_overrides_valid_shape_accepted():
    """``pack_overrides.<pack>.<category>.enabled: false`` validates cleanly."""
    cfg = Config(
        dry_run=True, server={"url": "http://x:8096"},
        libraries={"Movies": MovieLibrary(type="movies", overlay_profile="standard")},
        overlays=OverlaysConfig(profiles={"standard": {
            "elements": ["resolution"],
            "packs": ["test_pack"],
            "pack_overrides": {"test_pack": {"rating": {"enabled": False}}}}}),
    )
    ov = cfg.overlays.profiles["standard"]["pack_overrides"]
    assert ov == {"test_pack": {"rating": {"enabled": False}}}


def test_unknown_profile_rejected():
    with pytest.raises(Exception) as ei:
        Config(
            dry_run=True, server={"url": "http://x:8096"},
            libraries={"Movies": MovieLibrary(type="movies", overlay_profile="nope")},
            overlays=OverlaysConfig(),
        )
    assert "unknown overlay profile" in str(ei.value)


def test_library_without_profile_uses_own_overlays():
    from finmeta.engine.plan_builder import _profile_overlay_names
    cfg = Config(
        dry_run=True, server={"url": "http://x:8096"},
        libraries={"Movies": MovieLibrary(type="movies", overlays=["resolution"])},
        overlays=OverlaysConfig(),
    )
    assert _profile_overlay_names(cfg.libraries["Movies"], cfg) == ["resolution"]


def test_profile_elements_override_library_overlays():
    from finmeta.engine.plan_builder import _profile_overlay_names
    cfg = Config(
        dry_run=True, server={"url": "http://x:8096"},
        libraries={"Movies": MovieLibrary(type="movies", overlays=["resolution"],
                                          overlay_profile="minimal")},
        overlays=OverlaysConfig(profiles={"minimal": {"elements": ["hdr"]}}),
    )
    assert _profile_overlay_names(cfg.libraries["Movies"], cfg) == ["hdr"]


def test_profile_style_and_rules_merge():
    from finmeta.engine.plan_builder import (_profile_format_rules,
                                             _profile_style)
    cfg = Config(
        dry_run=True, server={"url": "http://x:8096"},
        libraries={"Movies": MovieLibrary(type="movies", overlays=["resolution"],
                                          overlay_profile="p")},
        overlays=OverlaysConfig(
            profiles={"p": {"style": {"margin": 99},
                            "format_rules": {
                                "resolution": [
                                    {"key": "res_4k", "match": "width>=3200",
                                     "label": "4K", "image": "res_4k.png",
                                     "priority": 1, "enabled": True}]}}}),
    )
    lib = cfg.libraries["Movies"]
    style = _profile_style(lib, cfg)
    assert style.margin == 99
    # Global style fields survive the merge.
    assert style.position == "top_left"
    rules = _profile_format_rules(lib, cfg)
    assert [r["key"] for r in rules["resolution"]] == ["res_4k"]


def test_profile_style_translates_overlaystyledoc_shape():
    """S9d4c: the Overlays-tab editor saves the richer OverlayStyleDoc shape
    (base_position/elements/ribbon_text) -- _profile_style must translate
    the fields that map (base_position -> position) and drop the
    unsupported ones (elements/ribbon_text), not fail validation and
    silently fall back to the global style, discarding margin/font/colors
    the operator actually set too."""
    from finmeta.engine.plan_builder import _profile_style
    cfg = Config(
        dry_run=True, server={"url": "http://x:8096"},
        libraries={"Movies": MovieLibrary(type="movies", overlays=["resolution"],
                                          overlay_profile="p")},
        overlays=OverlaysConfig(
            profiles={"p": {"style": {
                "base_position": "bottom_left", "margin": 14, "font_size": 80,
                "bg": "#00000099", "fg": "#FFFFFF", "radius": 8,
                "ribbon_text": None,
                "elements": {"resolution": {"enabled": False}},
            }}}),
    )
    lib = cfg.libraries["Movies"]
    style = _profile_style(lib, cfg)
    assert style.position == "bottom_left"
    assert style.margin == 14
    assert style.font_size == 80
    assert style.bg == "#00000099"


def test_no_profile_means_no_overlays_in_plan():
    """A library with a profile whose elements is [] gets no overlay poster."""
    from finmeta.engine.plan_builder import _profile_overlay_names, build_plan
    cfg = Config(
        dry_run=True, server={"url": "http://x:8096"},
        path_mappings=[{"jellyfin": "/media", "local": "/tmp/x"}],
        libraries={"Movies": MovieLibrary(type="movies", overlays=["resolution"],
                                          artwork={"poster": True},
                                          overlay_profile="none")},
        overlays=OverlaysConfig(profiles={"none": {"elements": []}}),
    )
    assert _profile_overlay_names(cfg.libraries["Movies"], cfg) == []


# ---------------------------------------------------------------------------
# Batched stream fetch: N items -> ceil(N / batch) requests
# ---------------------------------------------------------------------------

def _stream_item(iid, n=2):
    return {"Id": iid, "Name": f"Item {iid}", "Type": "Movie",
            "MediaStreams": [
                {"Type": "Video", "Width": 3840, "Height": 2160,
                 "VideoRangeType": "HDR10"},
                {"Type": "Audio", "Codec": "eac3", "Channels": 6},
            ][:n]}


class RecordingClient:
    """Wraps a real client to count /Items?Ids= calls."""

    def __init__(self, inner):
        self._inner = inner
        self.ids_calls: List[str] = []

    def get_items_media_streams_raw(self, item_ids, batch_size=50):
        # Record the batches as they are actually requested.
        size = max(1, int(batch_size))
        ids = [i for i in item_ids if i]
        for start in range(0, len(ids), size):
            self.ids_calls.append(",".join(ids[start:start + size]))
        return self._inner.get_items_media_streams_raw(item_ids, batch_size=batch_size)

    def __getattr__(self, name):
        return getattr(self._inner, name)


@pytest.mark.parametrize("n,batch,expected", [
    (10, 50, 1),
    (50, 50, 1),
    (51, 50, 2),
    (100, 50, 2),
    (101, 50, 3),
    (7, 4, 2),
])
def test_batched_stream_fetch_request_count(n, batch, expected):
    """N items -> ceil(N/batch) /Items?Ids= requests (S9d1 step 3)."""
    ids = [f"i{k}" for k in range(n)]

    def _handler(request: httpx.Request) -> httpx.Response:
        q = dict(httpx.QueryParams(request.url.params))
        got = (q.get("Ids") or "").split(",")
        items = [_stream_item(i) for i in got]
        return httpx.Response(200, json={"Items": items})

    with respx.mock:
        respx.route(method="GET", path="/Items").mock(side_effect=_handler)
        with JellyfinClient(BASE, token="tok", user_id="uid") as c:
            rec = RecordingClient(c)
            out = rec.get_items_media_streams_raw(ids, batch_size=batch)
    assert len(rec.ids_calls) == expected == math.ceil(n / batch)
    # Same raw dicts as the single-item path (K9 fidelity): every item mapped.
    assert set(out) == set(ids)
    for iid in ids:
        assert out[iid][0]["VideoRangeType"] == "HDR10"


def test_batched_fetch_missing_items_map_to_empty():
    def _handler(request: httpx.Request) -> httpx.Response:
        q = dict(httpx.QueryParams(request.url.params))
        got = (q.get("Ids") or "").split(",")
        # Server drops one item entirely.
        items = [_stream_item(i) for i in got if i != "ghost"]
        return httpx.Response(200, json={"Items": items})

    with respx.mock:
        respx.route(method="GET", path="/Items").mock(side_effect=_handler)
        with JellyfinClient(BASE, token="tok", user_id="uid") as c:
            out = c.get_items_media_streams_raw(["a", "ghost"], batch_size=50)
    assert out["a"][0]["Type"] == "Video"
    assert out["ghost"] == []


def test_single_item_path_unchanged():
    """get_item_media_streams_raw still returns the same raw dicts as before."""
    with respx.mock:
        respx.get(f"{BASE}/Items").mock(return_value=httpx.Response(200, json={
            "Items": [_stream_item("m1")]})
        )
        with JellyfinClient(BASE, token="tok", user_id="uid") as c:
            streams = c.get_item_media_streams_raw("m1")
    assert streams[0]["VideoRangeType"] == "HDR10"


# ---------------------------------------------------------------------------
# GET /api/format-rules/defaults (single source of truth for the UI)
# ---------------------------------------------------------------------------

def test_format_rules_defaults_endpoint(tmp_path, monkeypatch):
    from finmeta.engine.format_rules import DEFAULT_FORMAT_RULES
    ctx = _api_context(tmp_path, monkeypatch, FakeJf([]))
    with TestClient(create_app(ctx)) as client:
        r = client.get("/api/format-rules/defaults")
    assert r.status_code == 200
    body = r.json()
    assert set(body["rules"]) == set(DEFAULT_FORMAT_RULES)
    # Deep-equal to the backend defaults (fresh copies, not shared state).
    assert body["rules"] == DEFAULT_FORMAT_RULES
    body["rules"]["resolution"][0]["enabled"] = False
    assert DEFAULT_FORMAT_RULES["resolution"][0]["enabled"] is True


# ---------------------------------------------------------------------------
# Live test server (port 8097): real libraries via the API endpoint.
# Marked ``live`` like tests/test_api_live.py; needs .jf-test/creds.json.
# ---------------------------------------------------------------------------

CREDS_PATH = Path(__file__).resolve().parents[1] / ".jf-test" / "creds.json"


def _live_creds():
    if not CREDS_PATH.exists():
        pytest.skip("creds.json not found")
    text = CREDS_PATH.read_text()
    try:
        return yaml.safe_load(text)
    except Exception:
        return json.loads(text)


@pytest.mark.live
def test_live_jellyfin_libraries(tmp_path, monkeypatch):
    creds = _live_creds()
    import os
    os.environ.setdefault("JELLYFIN_API_KEY", creds["api_key"])
    os.environ.setdefault("TMDB_KEY", "fake-key")
    os.environ["FINMETA_AUTH"] = "off"
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    cfg_file = cfg_dir / "config.yml"
    cfg_file.write_text(f"""\
dry_run: true
server:
  url: {creds['url']}
  api_key_env: JELLYFIN_API_KEY
libraries:
  Movies:
    type: movies
    enabled: true
    overlays: []
""")

    def _fake_plan(cfg, jf, tmdb, sources, library=None, item_id=None, config_dir=None, **kwargs):
        return WritePlan(items=[])

    ctx = AppContext(str(cfg_file), config_dir=str(cfg_dir),
                     db_path=str(cfg_dir / "jobs.db"),
                     overrides_path=str(cfg_dir / "overrides.db"),
                     plan_builder=_fake_plan)
    with TestClient(create_app(ctx)) as client:
        r = client.get("/api/jellyfin/libraries")
    assert r.status_code == 200
    libs = r.json()["libraries"]
    assert libs, "test server has at least one library"
    for lib in libs:
        assert lib["id"] and lib["name"] and lib["collection_type"]
    names = {l["name"] for l in libs}
    assert "Movies" in names
    movies = next(l for l in libs if l["name"] == "Movies")
    assert movies["configured"] is True
    assert movies["enabled"] is True


__all__ = []
