"""Tests for the OMDb client (S9 §C, step 4) — respx-mocked, no network."""

import httpx
import pytest
import respx

from finmeta.sources.omdb import OMDbClient, OMDbError, _extract_ratings


@pytest.fixture
def cache_path(tmp_path):
    return str(tmp_path / "cache.db")


def _omdb_response(imdb_rating="8.5", metascore="78", rt="92", response="True"):
    return {
        "Response": response,
        "Title": "The Matrix",
        "Year": "1999",
        "imdbRating": imdb_rating,
        "Metascore": metascore,
        "RottenTomatoes": rt,
        "imdbID": "tt0133093",
    }


# ---------------------------------------------------------------------------
# _extract_ratings
# ---------------------------------------------------------------------------

def test_extract_ratings_all_present():
    data = _omdb_response("8.5", "78", "92")
    r = _extract_ratings(data)
    assert r["imdb"] == 8.5
    assert r["metacritic"] == 78.0
    assert r["rt_critic"] == 92.0


def test_extract_ratings_na_values():
    data = _omdb_response("N/A", "N/A", "N/A")
    r = _extract_ratings(data)
    assert r["imdb"] is None
    assert r["metacritic"] is None
    assert r["rt_critic"] is None


def test_extract_ratings_partial():
    data = _omdb_response("9.0", "N/A", "N/A")
    r = _extract_ratings(data)
    assert r["imdb"] == 9.0
    assert r["metacritic"] is None
    assert r["rt_critic"] is None


def test_extract_ratings_missing_keys():
    r = _extract_ratings({})
    assert r == {"imdb": None, "rt_critic": None, "metacritic": None}


# ---------------------------------------------------------------------------
# OMDbClient.get_ratings — respx-mocked
# ---------------------------------------------------------------------------

@respx.mock
def test_get_ratings_success(cache_path):
    route = respx.get("https://www.omdbapi.com/").mock(
        return_value=httpx.Response(200, json=_omdb_response("9.3", "82", "98"))
    )
    with OMDbClient(api_key="test-key", cache_path=cache_path) as client:
        r = client.get_ratings("tt0111161")
    assert route.called
    assert r["imdb"] == 9.3
    assert r["metacritic"] == 82.0
    assert r["rt_critic"] == 98.0


@respx.mock
def test_get_ratings_not_found(cache_path):
    respx.get("https://www.omdbapi.com/").mock(
        return_value=httpx.Response(200, json=_omdb_response(response="False"))
    )
    with OMDbClient(api_key="test-key", cache_path=cache_path) as client:
        r = client.get_ratings("tt0000001")
    assert r == {"imdb": None, "rt_critic": None, "metacritic": None}


@respx.mock
def test_get_ratings_http_error(cache_path):
    respx.get("https://www.omdbapi.com/").mock(
        return_value=httpx.Response(500, json={"Response": "False"})
    )
    with OMDbClient(api_key="test-key", cache_path=cache_path) as client:
        # HTTP 500 → graceful degrade, all None
        r = client.get_ratings("tt0000001")
    assert r["imdb"] is None


def test_get_ratings_connection_error(cache_path):
    with pytest.raises(OMDbError):
        # respx with no routes: httpx raises
        with respx.mock(assert_all_called=False) as router:
            router.get("https://www.omdbapi.com/").mock(side_effect=httpx.ConnectError("conn refused"))
            with OMDbClient(api_key="test-key", cache_path=cache_path) as client:
                client.get_ratings("tt0000001")


# ---------------------------------------------------------------------------
# caching (second call does NOT hit the network)
# ---------------------------------------------------------------------------

@respx.mock
def test_get_ratings_uses_cache(cache_path):
    route = respx.get("https://www.omdbapi.com/").mock(
        return_value=httpx.Response(200, json=_omdb_response("7.7", "70", "88"))
    )
    with OMDbClient(api_key="test-key", cache_path=cache_path) as client:
        r1 = client.get_ratings("tt0133093")
        r2 = client.get_ratings("tt0133093")  # should come from cache
    assert r1 == r2
    assert route.call_count == 1  # only one HTTP call


@respx.mock
def test_get_ratings_rate_limiter_is_actually_called(cache_path):
    """OMDbClient had NO throttle at all -- every cache-miss call went
    straight to the network, back to back, as fast as a bulk plan could
    issue them. Regression: the limiter must actually be waited on before
    each real fetch, and must NOT fire again on a cache hit."""
    respx.get("https://www.omdbapi.com/").mock(
        return_value=httpx.Response(200, json=_omdb_response()))
    with OMDbClient(api_key="test-key", cache_path=cache_path) as client:
        calls = []
        client.limiter.wait = lambda: calls.append(1)  # type: ignore[method-assign]
        client.get_ratings("tt0133093")
        assert calls == [1]
        client.get_ratings("tt0133093")  # cache hit, no new fetch
        assert calls == [1]


# ---------------------------------------------------------------------------
# live test (only if OMDB_KEY is set)
# ---------------------------------------------------------------------------

import os

@pytest.mark.live
def test_live_omdb():
    key = os.environ.get("OMDB_KEY")
    if not key:
        pytest.skip("OMDB_KEY not set")
    with OMDbClient(api_key=key, cache_path="/tmp/omdb-test.db") as client:
        r = client.get_ratings("tt0111161")  # The Shawshank Redemption
    assert r["imdb"] is not None
    assert r["imdb"] > 9.0
