"""Network/streaming-service overlay badges (2026-10-23, Kometa-Team/
Default-Images). Offline, respx-mocked -- see test_tmdb.py for the same
pattern.
"""
from __future__ import annotations

import httpx
import respx

from finmeta.engine.network_logos import ensure_network_logo, network_slug

BASE = "https://raw.githubusercontent.com/Kometa-Team/Default-Images/master/network/overlays/standard"


def test_network_slug_normalizes():
    assert network_slug("Netflix") == "netflix"
    assert network_slug("Apple TV+") == "apple_tv"
    assert network_slug("HBO Max") == "hbo_max"
    assert network_slug("  AMC  ") == "amc"


@respx.mock
def test_fetches_and_caches_to_disk(tmp_path):
    respx.get(f"{BASE}/Netflix.png").mock(
        return_value=httpx.Response(200, content=b"fake-png-bytes"))
    data = ensure_network_logo(tmp_path, "Netflix")
    assert data == b"fake-png-bytes"
    cached_path = tmp_path / "assets" / "logos" / "kometa" / "network" / "netflix.png"
    assert cached_path.read_bytes() == b"fake-png-bytes"


@respx.mock
def test_second_call_hits_disk_not_network(tmp_path):
    route = respx.get(f"{BASE}/Netflix.png").mock(
        return_value=httpx.Response(200, content=b"fake-png-bytes"))
    ensure_network_logo(tmp_path, "Netflix")
    ensure_network_logo(tmp_path, "Netflix")
    assert route.call_count == 1


@respx.mock
def test_404_returns_none_and_caches_the_miss(tmp_path):
    route = respx.get(f"{BASE}/Some%20Obscure%20Channel.png").mock(
        return_value=httpx.Response(404))
    assert ensure_network_logo(tmp_path, "Some Obscure Channel") is None
    # Cached as an empty marker file -- a second call must not hit the network again.
    assert ensure_network_logo(tmp_path, "Some Obscure Channel") is None
    assert route.call_count == 1


@respx.mock
def test_network_error_is_not_cached_and_may_succeed_later(tmp_path):
    route = respx.get(f"{BASE}/Netflix.png").mock(
        side_effect=[httpx.ConnectError("boom"), httpx.Response(200, content=b"ok")])
    assert ensure_network_logo(tmp_path, "Netflix") is None
    assert ensure_network_logo(tmp_path, "Netflix") == b"ok"
    assert route.call_count == 2


def test_empty_name_returns_none_without_any_request(tmp_path):
    assert ensure_network_logo(tmp_path, "") is None
    assert ensure_network_logo(tmp_path, "   ") is None


@respx.mock
def test_name_with_special_characters_is_url_encoded(tmp_path):
    """"Apple TV+" has a "+" that must survive as a literal character in the
    URL path, not be interpreted as a space (requote with safe="")."""
    route = respx.get(f"{BASE}/Apple%20TV%2B.png").mock(
        return_value=httpx.Response(200, content=b"apple-tv-logo"))
    data = ensure_network_logo(tmp_path, "Apple TV+")
    assert data == b"apple-tv-logo"
    assert route.call_count == 1
