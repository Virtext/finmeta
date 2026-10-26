"""Tests for the MDBList client (S9b2) — respx-mocked, fake host, no network/sleeps."""

import httpx
import pytest
import respx

from finmeta.sources.mdblist import MdblistClient, RATING_FIELDS, _extract_ratings

FAKE = "http://mdblist.test"


@pytest.fixture
def cache_path(tmp_path):
    return str(tmp_path / "cache.db")


def _mdblist_response(ratings, **extra):
    base = {"id": 1, "imdb_id": "tt0111161", "title": "Test", "year": 1994,
            "type": "movie", "score": 9.0, "ratings": ratings}
    base.update(extra)
    return base


# ---------------------------------------------------------------------------
# _extract_ratings
# ---------------------------------------------------------------------------

def test_extract_ratings_all_fields():
    data = _mdblist_response([
        {"source": "IMDb", "value": 9.3, "score": 9.3, "votes": 1000},
        {"source": "Rotten Tomatoes", "value": 98.0, "score": 98.0},
        {"source": "Rotten Tomatoes (Audience)", "value": 99.0, "score": 99.0},
        {"source": "Metacritic", "value": 82.0, "score": 82.0},
        {"source": "TMDb", "value": 8.5, "score": 8.5},
        {"source": "Trakt", "value": 9.0, "score": 9.0},
        {"source": "Letterboxd", "value": 4.2, "score": 8.4},
    ])
    r = _extract_ratings(data)
    assert r["imdb"] == 9.3
    assert r["rt_critic"] == 98.0
    assert r["rt_audience"] == 99.0
    assert r["metacritic"] == 82.0
    assert r["tmdb"] == 8.5
    assert r["trakt"] == 9.0
    assert r["letterboxd"] == 4.2


def test_extract_ratings_score_fallback_when_value_missing():
    data = _mdblist_response([{"source": "IMDb", "score": 8.0}])
    r = _extract_ratings(data)
    assert r["imdb"] == 8.0


def test_extract_ratings_unknown_source_ignored():
    data = _mdblist_response([{"source": "Roger Ebert", "value": 4.0}])
    r = _extract_ratings(data)
    assert r == {k: None for k in RATING_FIELDS}


def test_extract_ratings_empty_or_missing():
    assert _extract_ratings({}) == {k: None for k in RATING_FIELDS}
    assert _extract_ratings(_mdblist_response([])) == {k: None for k in RATING_FIELDS}


def test_extract_ratings_first_wins_per_field():
    # duplicate IMDb entries: first non-None wins
    data = _mdblist_response([
        {"source": "IMDb", "value": 8.0},
        {"source": "IMDb", "value": 9.5},
    ])
    r = _extract_ratings(data)
    assert r["imdb"] == 8.0


# ---------------------------------------------------------------------------
# get_ratings — respx on a fake host
# ---------------------------------------------------------------------------

@respx.mock
def test_get_ratings_success(cache_path):
    route = respx.get(f"{FAKE}/imdb/any/tt0111161/").mock(
        return_value=httpx.Response(200, json=_mdblist_response([
            {"source": "IMDb", "value": 9.3},
            {"source": "Rotten Tomatoes", "value": 98.0},
        ])))
    with MdblistClient(api_key="key", cache_path=cache_path, base_url=FAKE,
                       min_interval=0.0) as client:
        r = client.get_ratings("tt0111161")
    assert route.called
    assert r["imdb"] == 9.3
    assert r["rt_critic"] == 98.0
    assert r["metacritic"] is None


def test_apikey_sent_as_query_param(cache_path):
    seen = {}
    @respx.mock
    def _run():
        respx.get(f"{FAKE}/imdb/any/tt0111161/").mock(
            return_value=httpx.Response(200, json=_mdblist_response([])))
        def _capture(request, **_):
            seen["apikey"] = request.url.params.get("apikey")
            return httpx.Response(200, json=_mdblist_response([]))
        respx.get(f"{FAKE}/imdb/any/tt0111161/").mock(side_effect=_capture)
        with MdblistClient(api_key="secret-key", cache_path=cache_path,
                           base_url=FAKE, min_interval=0.0) as client:
            client.get_ratings("tt0111161")
    _run()
    assert seen["apikey"] == "secret-key"


@respx.mock
def test_get_ratings_uses_cache(cache_path):
    route = respx.get(f"{FAKE}/imdb/any/tt0133093/").mock(
        return_value=httpx.Response(200, json=_mdblist_response(
            [{"source": "IMDb", "value": 8.7}])))
    with MdblistClient(api_key="key", cache_path=cache_path, base_url=FAKE,
                       min_interval=0.0) as client:
        r1 = client.get_ratings("tt0133093")
        r2 = client.get_ratings("tt0133093")  # from cache
    assert r1 == r2
    assert r1["imdb"] == 8.7
    assert route.call_count == 1


@respx.mock
def test_get_ratings_429_returns_none_not_cached(cache_path):
    route = respx.get(f"{FAKE}/imdb/any/tt1/").mock(
        return_value=httpx.Response(429, text="rate limited"))
    with MdblistClient(api_key="key", cache_path=cache_path, base_url=FAKE,
                       min_interval=0.0) as client:
        r = client.get_ratings("tt1")
    assert r == {k: None for k in RATING_FIELDS}
    assert route.call_count == 1
    # a retry must hit the network again (not served from cache)
    with MdblistClient(api_key="key", cache_path=cache_path, base_url=FAKE,
                       min_interval=0.0) as client:
        client.get_ratings("tt1")
    assert route.call_count == 2


@respx.mock
def test_get_ratings_404_returns_none_not_cached(cache_path):
    respx.get(f"{FAKE}/imdb/any/tt1/").mock(return_value=httpx.Response(404, text="nf"))
    with MdblistClient(api_key="key", cache_path=cache_path, base_url=FAKE,
                       min_interval=0.0) as client:
        r = client.get_ratings("tt1")
    assert r == {k: None for k in RATING_FIELDS}


def test_get_ratings_connection_error_returns_none(cache_path):
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{FAKE}/imdb/any/tt1/").mock(
            side_effect=httpx.ConnectError("conn refused"))
        with MdblistClient(api_key="key", cache_path=cache_path, base_url=FAKE,
                           min_interval=0.0) as client:
            r = client.get_ratings("tt1")
    assert r == {k: None for k in RATING_FIELDS}


def test_empty_imdb_id_returns_none_no_network(cache_path):
    with respx.mock(assert_all_called=False):
        with MdblistClient(api_key="key", cache_path=cache_path, base_url=FAKE,
                           min_interval=0.0) as client:
            r = client.get_ratings("")
    assert r == {k: None for k in RATING_FIELDS}


# ---------------------------------------------------------------------------
# throttle — injectable sleep/now (no real sleeps)
# ---------------------------------------------------------------------------

def _clock(start=100.0):
    state = {"t": start}

    def now():
        return state["t"]

    def sleep(seconds):
        state["t"] += seconds
        return seconds

    return now, sleep


@respx.mock
def test_throttle_enforces_min_interval(cache_path):
    now, sleep = _clock(100.0)
    calls = []
    real_sleep = sleep
    def recording_sleep(seconds):
        calls.append(round(seconds, 4))
        return real_sleep(seconds)
    respx.get(f"{FAKE}/imdb/any/tt1/").mock(
        return_value=httpx.Response(200, json=_mdblist_response([])))
    respx.get(f"{FAKE}/imdb/any/tt2/").mock(
        return_value=httpx.Response(200, json=_mdblist_response([])))
    with MdblistClient(api_key="key", cache_path=cache_path, base_url=FAKE,
                       min_interval=1.0, sleep=recording_sleep, now=now) as client:
        client.get_ratings("tt1")   # first: clock far past t0 -> no sleep
        client.get_ratings("tt2")   # second: must wait min_interval
    # only the second request waits, for exactly min_interval
    assert calls == [1.0]


@respx.mock
def test_cache_hit_does_not_throttle(cache_path):
    now, sleep = _clock(100.0)
    calls = []
    def recording_sleep(seconds):
        calls.append(round(seconds, 4))
        return sleep(seconds)
    route = respx.get(f"{FAKE}/imdb/any/tt1/").mock(
        return_value=httpx.Response(200, json=_mdblist_response([])))
    with MdblistClient(api_key="key", cache_path=cache_path, base_url=FAKE,
                       min_interval=1.0, sleep=recording_sleep, now=now) as client:
        client.get_ratings("tt1")
        client.get_ratings("tt1")   # cache hit: no network, no throttle
    assert route.call_count == 1
    assert calls == []
