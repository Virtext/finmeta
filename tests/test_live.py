"""Live tests against the Jellyfin test server (marked ``live``).

Run with:  pytest -m live
Requires:  the test server to be bootstrapped (scripts/jf_bootstrap.py).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from finmeta.jellyfin_client import JellyfinClient, PathMapper

ROOT = Path(__file__).resolve().parents[1]
CREDS_PATH = ROOT / ".jf-test" / "creds.json"
FIXTURE = ROOT / "fixture" / "library"
MAPPINGS = [{"jellyfin": "/media", "local": str(FIXTURE)}]

# Expected leaf-item counts per library (from fixture structure)
EXPECTED_MOVIES = 10
EXPECTED_UNTOUCHED = 2
EXPECTED_SHOWS_EPISODES = 13
EXPECTED_SHOWS_SERIES = 3


def _creds() -> dict:
    if not CREDS_PATH.exists():
        pytest.skip("creds.json not found; run scripts/jf_bootstrap.py first")
    return json.loads(CREDS_PATH.read_text())


@pytest.fixture(scope="module")
def client() -> JellyfinClient:
    c = _creds()
    jf = JellyfinClient(c["url"], token=c["api_key"])
    # Get user_id from the server
    users = jf._request("GET", "/Users") or []
    user_id = next((u["Id"] for u in users if u.get("Name") == c.get("username", "finmeta")), None)
    if not user_id:
        pytest.skip(f"user '{c.get('username')}' not found on server")
    jf.user_id = user_id
    yield jf
    jf.close()


@pytest.fixture(scope="module")
def mapper() -> PathMapper:
    return PathMapper.from_config(MAPPINGS)


@pytest.fixture(scope="module")
def libraries(client) -> dict:
    """Map library name -> Library object."""
    libs = client.get_libraries()
    return {lib.name: lib for lib in libs}


@pytest.mark.live
class TestLibraryCounts:
    def test_movies_count(self, client, libraries):
        lib = libraries["Movies"]
        items = client.get_items(parent_id=lib.item_id, recursive=True,
                                 include_item_types=["Movie"])
        assert len(items) == EXPECTED_MOVIES, f"got {len(items)}"

    def test_untouched_count(self, client, libraries):
        lib = libraries["Untouched"]
        items = client.get_items(parent_id=lib.item_id, recursive=True,
                                 include_item_types=["Movie"])
        assert len(items) == EXPECTED_UNTOUCHED, f"got {len(items)}"

    def test_shows_episodes_count(self, client, libraries):
        lib = libraries["Shows"]
        items = client.get_items(parent_id=lib.item_id, recursive=True,
                                 include_item_types=["Episode"])
        assert len(items) == EXPECTED_SHOWS_EPISODES, f"got {len(items)}"

    def test_shows_series_count(self, client, libraries):
        lib = libraries["Shows"]
        items = client.get_items(parent_id=lib.item_id, recursive=True,
                                 include_item_types=["Series"])
        assert len(items) == EXPECTED_SHOWS_SERIES, f"got {len(items)}"


@pytest.mark.live
class TestTmdbIds:
    """Every item's TMDb provider ID must match the tmdbid in its folder path."""

    def _check_item(self, client, mapper, item):
        if not item.path:
            return
        local = mapper.translate(item.path)
        assert local is not None, f"unmapped path: {item.path}"
        # Extract tmdbid from the folder name
        m = re.search(r"tmdbid-(\d+)", item.path)
        if m:
            folder_tmdb = m.group(1)
            item_tmdb = item.tmdb_id()
            assert item_tmdb == folder_tmdb, (
                f"TMDb ID mismatch: item={item.name} "
                f"provider={item_tmdb} folder={folder_tmdb}"
            )

    def test_all_movies(self, client, mapper, libraries):
        lib = libraries["Movies"]
        items = client.get_items(parent_id=lib.item_id, recursive=True,
                                 include_item_types=["Movie"])
        assert len(items) > 0
        for item in items:
            self._check_item(client, mapper, item)

    def test_all_untouched(self, client, mapper, libraries):
        lib = libraries["Untouched"]
        items = client.get_items(parent_id=lib.item_id, recursive=True,
                                 include_item_types=["Movie"])
        assert len(items) > 0
        for item in items:
            self._check_item(client, mapper, item)

    def test_all_shows(self, client, mapper, libraries):
        lib = libraries["Shows"]
        # Check series items (folder tmdbid matches series provider ID)
        series = client.get_items(parent_id=lib.item_id, recursive=True,
                                  include_item_types=["Series"])
        for item in series:
            self._check_item(client, mapper, item)
        # Episodes have their own TMDb IDs (not the series folder ID),
        # so only verify they are mapped to a valid local path.
        episodes = client.get_items(parent_id=lib.item_id, recursive=True,
                                    include_item_types=["Episode"])
        assert len(episodes) > 0
        for item in episodes:
            assert item.path is not None
            local = mapper.translate(item.path)
            assert local is not None, f"unmapped path: {item.path}"


@pytest.mark.live
class TestMappedPaths:
    """Every item path that comes back from Jellyfin must map to an existing local file."""

    def _check_paths(self, client, mapper, items):
        checked = 0
        for item in items:
            if not item.path:
                continue
            local = mapper.translate(item.path)
            assert local is not None, f"unmapped path: {item.path}"
            # The path should exist on disk (either as file or directory)
            p = Path(local)
            assert p.exists(), f"mapped path does not exist: {local} (from {item.path})"
            checked += 1
        assert checked > 0, "no items with paths to check"
        return checked

    def test_movies_paths(self, client, mapper, libraries):
        lib = libraries["Movies"]
        items = client.get_items(parent_id=lib.item_id, recursive=True,
                                 include_item_types=["Movie"])
        self._check_paths(client, mapper, items)

    def test_untouched_paths(self, client, mapper, libraries):
        lib = libraries["Untouched"]
        items = client.get_items(parent_id=lib.item_id, recursive=True,
                                 include_item_types=["Movie"])
        self._check_paths(client, mapper, items)

    def test_shows_paths(self, client, mapper, libraries):
        lib = libraries["Shows"]
        # Episodes should have file paths
        episodes = client.get_items(parent_id=lib.item_id, recursive=True,
                                    include_item_types=["Episode"])
        self._check_paths(client, mapper, episodes)
