"""Tests for the MDBList "list items" client (S9g2/K7b) — respx-mocked, fake
host, no network. See sources/mdblist_lists.py's module docstring: the exact
response shape is NOT independently verified against a live MDBList API key
(none available), so these tests exercise the defensive parsing against
several plausible shapes rather than one assumed-correct fixture.
"""
from __future__ import annotations

import httpx
import pytest
import respx

from finmeta.sources.mdblist_lists import (
    MdblistListError, fetch_list_items, parse_list_url,
)

FAKE = "http://mdblist.test"
URL = "https://mdblist.com/lists/someuser/top-watched-movies-of-the-week"


# ---------------------------------------------------------------------------
# parse_list_url
# ---------------------------------------------------------------------------

def test_parse_list_url_extracts_user_and_slug():
    assert parse_list_url(URL) == ("someuser", "top-watched-movies-of-the-week")


def test_parse_list_url_with_query_string():
    out = parse_list_url(URL + "?sort=rank")
    assert out == ("someuser", "top-watched-movies-of-the-week")


def test_parse_list_url_unrecognised_returns_none():
    assert parse_list_url("https://example.com/not-a-list") is None


# ---------------------------------------------------------------------------
# fetch_list_items
# ---------------------------------------------------------------------------

@respx.mock
def test_fetch_bare_list_response():
    respx.get(f"{FAKE}/lists/someuser/top-watched-movies-of-the-week/items").mock(
        return_value=httpx.Response(200, json=[
            {"rank": 1, "title": "Movie A", "imdb_id": "tt0001", "tmdb_id": 11, "mediatype": "movie"},
            {"rank": 2, "title": "Movie B", "imdb_id": "tt0002", "tmdb_id": 22, "mediatype": "movie"},
        ]))
    out = fetch_list_items(URL, "key", base_url=FAKE)
    assert len(out) == 2
    assert out[0].rank == 1
    assert out[0].tmdb_id == 11
    assert out[0].media_type == "movie"


@respx.mock
def test_fetch_items_wrapped_object_response():
    respx.get(f"{FAKE}/lists/someuser/top-watched-movies-of-the-week/items").mock(
        return_value=httpx.Response(200, json={"items": [
            {"rank": 1, "title": "Show A", "tmdb_id": 99, "mediatype": "show"},
        ]}))
    out = fetch_list_items(URL, "key", base_url=FAKE)
    assert len(out) == 1
    assert out[0].media_type == "show"


@respx.mock
def test_fetch_movies_shows_split_response():
    respx.get(f"{FAKE}/lists/someuser/top-watched-movies-of-the-week/items").mock(
        return_value=httpx.Response(200, json={
            "movies": [{"rank": 1, "tmdb_id": 1, "mediatype": "movie"}],
            "shows": [{"rank": 1, "tmdb_id": 2, "mediatype": "show"}],
        }))
    out = fetch_list_items(URL, "key", base_url=FAKE)
    assert {o.tmdb_id for o in out} == {1, 2}


@respx.mock
def test_missing_rank_falls_back_to_position():
    respx.get(f"{FAKE}/lists/someuser/top-watched-movies-of-the-week/items").mock(
        return_value=httpx.Response(200, json=[
            {"title": "No rank field", "tmdb_id": 5, "mediatype": "movie"},
        ]))
    out = fetch_list_items(URL, "key", base_url=FAKE)
    assert out[0].rank == 1


@respx.mock
def test_alternate_field_names_tolerated():
    """imdbid/tmdbid (no underscore) and 'type' instead of 'mediatype'."""
    respx.get(f"{FAKE}/lists/someuser/top-watched-movies-of-the-week/items").mock(
        return_value=httpx.Response(200, json=[
            {"rank": 1, "name": "Alt-shaped row", "imdbid": "tt9999", "tmdbid": 77, "type": "tv"},
        ]))
    out = fetch_list_items(URL, "key", base_url=FAKE)
    assert out[0].title == "Alt-shaped row"
    assert out[0].imdb_id == "tt9999"
    assert out[0].tmdb_id == 77
    assert out[0].media_type == "show"


@respx.mock
def test_missing_ids_degrade_to_none_not_crash():
    respx.get(f"{FAKE}/lists/someuser/top-watched-movies-of-the-week/items").mock(
        return_value=httpx.Response(200, json=[{"rank": 1, "title": "Sparse"}]))
    out = fetch_list_items(URL, "key", base_url=FAKE)
    assert out[0].tmdb_id is None
    assert out[0].imdb_id is None
    assert out[0].media_type is None


def test_unparseable_url_raises():
    with pytest.raises(MdblistListError):
        fetch_list_items("https://example.com/nope", "key", base_url=FAKE)


@respx.mock
def test_http_error_raises():
    respx.get(f"{FAKE}/lists/someuser/top-watched-movies-of-the-week/items").mock(
        return_value=httpx.Response(401, text="unauthorized"))
    with pytest.raises(MdblistListError):
        fetch_list_items(URL, "bad-key", base_url=FAKE)


@respx.mock
def test_apikey_sent_as_query_param():
    seen = {}

    def _capture(request):
        seen["apikey"] = request.url.params.get("apikey")
        return httpx.Response(200, json=[])

    respx.get(f"{FAKE}/lists/someuser/top-watched-movies-of-the-week/items").mock(
        side_effect=_capture)
    fetch_list_items(URL, "secret-key", base_url=FAKE)
    assert seen["apikey"] == "secret-key"


@respx.mock
def test_limit_sent_as_query_param():
    seen = {}

    def _capture(request):
        seen["limit"] = request.url.params.get("limit")
        return httpx.Response(200, json=[])

    respx.get(f"{FAKE}/lists/someuser/top-watched-movies-of-the-week/items").mock(
        side_effect=_capture)
    fetch_list_items(URL, "key", base_url=FAKE, limit=10)
    assert seen["limit"] == "10"
