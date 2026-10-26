"""Read-only Sonarr/Radarr client tests on respx-mocked HTTP (S9f1a).

All requests are mocked — no test ever talks to a real Sonarr/Radarr. The
mocking style mirrors tests/test_tmdb.py (respx + httpx.Response).
"""
from __future__ import annotations

import inspect

import pytest
import respx
import httpx

from finmeta.arr import ArrClient, ArrError
from finmeta.arr.sonarr import SonarrClient
from finmeta.arr.radarr import RadarrClient

SONARR = "http://sonarr.example.com"
RADARR = "http://radarr.example.com"


def _sonarr(**kw):
    kw.setdefault("api_key", "SONARRKEY")
    return SonarrClient(SONARR, **kw)


def _radarr(**kw):
    kw.setdefault("api_key", "RADARRKEY")
    return RadarrClient(RADARR, **kw)


# ---------------------------------------------------------------------------
# read-only API surface (no method may send POST/PUT/DELETE)
# ---------------------------------------------------------------------------

def test_clients_expose_no_write_methods():
    """Neither client class exposes anything beyond the GET-based base API."""
    for cls in (SonarrClient, RadarrClient):
        methods = [n for n, v in vars(cls).items() if callable(v)]
        # public + protected methods defined on the subclass itself
        # S9g3: root_folders() added (GET /api/v3/rootfolder) for the Coming
        # Soon folder-overlap safety check.
        assert set(methods) <= {"series", "calendar", "episodes", "root_folders"} | {"movies"}, \
            f"{cls.__name__} defines {methods}"
        base_methods = [n for n, v in vars(ArrClient).items() if callable(v)]
        # the only request primitive is _get; no post/put/delete anywhere
        assert not any(m.lower() in {"post", "put", "delete"}
                       for m in methods + base_methods), \
            f"{cls.__name__} exposes a write method"


# ---------------------------------------------------------------------------
# system_status (shared)
# ---------------------------------------------------------------------------

@respx.mock
def test_sonarr_system_status():
    respx.get(SONARR + "/api/v3/system/status").mock(
        return_value=httpx.Response(200, json={
            "version": "4.0.14.1875", "name": "Sonarr",
            "uptime": "1d 2h", "state": "running"}))
    with _sonarr() as c:
        st = c.system_status()
    assert st["version"] == "4.0.14.1875"
    assert st["name"] == "Sonarr"


@respx.mock
def test_radarr_system_status():
    respx.get(RADARR + "/api/v3/system/status").mock(
        return_value=httpx.Response(200, json={
            "version": "5.17.0.9456", "name": "Radarr",
            "uptime": "3h", "state": "running"}))
    with _radarr() as c:
        st = c.system_status()
    assert st["version"] == "5.17.0.9456"


# ---------------------------------------------------------------------------
# series / movies field mapping
# ---------------------------------------------------------------------------

@respx.mock
def test_sonarr_series_mapping():
    respx.get(SONARR + "/api/v3/series").mock(
        return_value=httpx.Response(200, json=[{
            "id": 1, "title": "Breaking Bad", "status": "ended",
            "tvdbId": 81158, "tmdbId": 1396, "imdbId": "tt0903747",
            "nextAiring": None, "previousAiring": "2013-09-15T20:00:00Z",
            "monitored": True, "somethingElse": 42}]))
    with _sonarr() as c:
        rows = c.series()
    assert rows == [{
        "id": 1, "title": "Breaking Bad", "status": "ended",
        "tvdbId": 81158, "tmdbId": 1396, "imdbId": "tt0903747",
        "nextAiring": None, "previousAiring": "2013-09-15T20:00:00Z",
        "monitored": True}]


@respx.mock
def test_radarr_movies_mapping():
    respx.get(RADARR + "/api/v3/movie").mock(
        return_value=httpx.Response(200, json=[{
            "id": 7, "title": "Dune: Part Two", "status": "released",
            "tmdbId": 693134, "imdbId": "tt15239678", "hasFile": True,
            "monitored": False, "inCinemas": "2024-03-05",
            "digitalRelease": "2024-05-14", "physicalRelease": "2024-04-16",
            "noise": 1}]))
    with _radarr() as c:
        rows = c.movies()
    assert rows == [{
        "id": 7, "title": "Dune: Part Two", "status": "released",
        "tmdbId": 693134, "imdbId": "tt15239678", "hasFile": True,
        "monitored": False, "inCinemas": "2024-03-05",
        "digitalRelease": "2024-05-14", "physicalRelease": "2024-04-16"}]


# ---------------------------------------------------------------------------
# calendar (start/end params)
# ---------------------------------------------------------------------------

@respx.mock
def test_sonarr_calendar_params():
    route = respx.get(SONARR + "/api/v3/calendar").mock(
        return_value=httpx.Response(200, json=[{"seriesTitle": "X"}]))
    with _sonarr() as c:
        rows = c.calendar("2025-01-01", "2025-12-31")
    assert rows == [{"seriesTitle": "X"}]
    assert route.calls.last.request.url.params["start"] == "2025-01-01"
    assert route.calls.last.request.url.params["end"] == "2025-12-31"


@respx.mock
def test_radarr_calendar_params():
    route = respx.get(RADARR + "/api/v3/calendar").mock(
        return_value=httpx.Response(200, json=[{"movieTitle": "Y"}]))
    with _radarr() as c:
        rows = c.calendar("2025-06-01")
    assert rows == [{"movieTitle": "Y"}]
    assert route.calls.last.request.url.params["start"] == "2025-06-01"
    assert "end" not in route.calls.last.request.url.params


# ---------------------------------------------------------------------------
# root_folders (S9g3: Coming Soon folder-overlap safety check)
# ---------------------------------------------------------------------------

@respx.mock
def test_sonarr_root_folders():
    respx.get(SONARR + "/api/v3/rootfolder").mock(
        return_value=httpx.Response(200, json=[{"id": 1, "path": "/tv"}]))
    with _sonarr() as c:
        rows = c.root_folders()
    assert rows == [{"id": 1, "path": "/tv"}]


@respx.mock
def test_radarr_root_folders():
    respx.get(RADARR + "/api/v3/rootfolder").mock(
        return_value=httpx.Response(200, json=[{"id": 1, "path": "/movies"}]))
    with _radarr() as c:
        rows = c.root_folders()
    assert rows == [{"id": 1, "path": "/movies"}]


# ---------------------------------------------------------------------------
# auth header + error handling
# ---------------------------------------------------------------------------

@respx.mock
def test_api_key_header_sent():
    route = respx.get(SONARR + "/api/v3/system/status").mock(
        return_value=httpx.Response(200, json={}))
    with _sonarr() as c:
        c.system_status()
    assert route.calls.last.request.headers["X-Api-Key"] == "SONARRKEY"


@respx.mock
def test_non_2xx_raises_arr_error():
    respx.get(RADARR + "/api/v3/movie").mock(
        return_value=httpx.Response(401, text="unauthorized"))
    with _radarr() as c:
        with pytest.raises(ArrError) as ei:
            c.movies()
    assert "401" in str(ei.value)


@respx.mock
def test_timeout_and_verify_ssl_applied():
    respx.get(SONARR + "/api/v3/series").mock(
        return_value=httpx.Response(200, json=[]))
    with _sonarr(timeout=7.5, verify_ssl=False) as c:
        assert c.series() == []
