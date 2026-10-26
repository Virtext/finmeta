"""Tests for the IMDb bulk-dataset source (S9b2) — no real network."""
import gzip
import io
import os
import time

import httpx
import respx

from finmeta.sources.imdb_datasets import ImdbDatasetsClient, DEFAULT_DATASET_URL

TSV = ("tconst\taveragerating\tnumvotes\n"
       "tt0111161\t9.3\t2800000\n"
       "tt1375666\t8.5\t1900000\n"
       "tt1825683\t9.1\t1700000\n")


def _make_tsv_gz(tsv_text: str) -> bytes:
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb") as gz:
        gz.write(tsv_text.encode("utf-8"))
    return buf.getvalue()


def _age_file(path, seconds_ago):
    t = time.time() - seconds_ago
    os.utime(path, (t, t))


# ---------------------------------------------------------------------------
# pre-seeded gz -> no fetch
# ---------------------------------------------------------------------------

def test_preseeded_returns_ratings_no_fetch(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir(parents=True)
    (cache / "title.ratings.tsv.gz").write_bytes(_make_tsv_gz(TSV))
    with respx.mock(assert_all_called=False) as router:
        route = router.get(DEFAULT_DATASET_URL).mock(
            return_value=httpx.Response(200, content=_make_tsv_gz(TSV)))
        with ImdbDatasetsClient(cache_dir=str(cache)) as client:
            assert client.get_ratings("tt0111161") == {"imdb": 9.3, "imdb_votes": 2800000}
            assert client.get_ratings("tt1375666") == {"imdb": 8.5, "imdb_votes": 1900000}
            assert client.get_ratings("tt1825683") == {"imdb": 9.1, "imdb_votes": 1700000}
            # not in the dataset
            assert client.get_ratings("tt0000001") == {"imdb": None, "imdb_votes": None}
    # nothing was downloaded (gz was already fresh)
    assert route.call_count == 0


def test_get_ratings_empty_imdb_id(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir(parents=True)
    (cache / "title.ratings.tsv.gz").write_bytes(_make_tsv_gz(TSV))
    with ImdbDatasetsClient(cache_dir=str(cache)) as client:
        assert client.get_ratings("") == {"imdb": None, "imdb_votes": None}


# ---------------------------------------------------------------------------
# download at most once / 24h
# ---------------------------------------------------------------------------

def test_downloads_once_across_clients(tmp_path):
    cache = tmp_path / "cache"
    gz_bytes = _make_tsv_gz(TSV)
    with respx.mock as router:
        route = router.get(DEFAULT_DATASET_URL).mock(
            return_value=httpx.Response(200, content=gz_bytes))
        with ImdbDatasetsClient(cache_dir=str(cache)) as c1:
            assert c1.get_ratings("tt0111161")["imdb"] == 9.3
        with ImdbDatasetsClient(cache_dir=str(cache)) as c2:
            assert c2.get_ratings("tt0111161")["imdb"] == 9.3
    # second client reused the fresh gz -> only one download
    assert route.call_count == 1


def test_no_redownload_within_ttl(tmp_path):
    cache = tmp_path / "cache"
    gz_bytes = _make_tsv_gz(TSV)
    with respx.mock as router:
        route = router.get(DEFAULT_DATASET_URL).mock(
            return_value=httpx.Response(200, content=gz_bytes))
        with ImdbDatasetsClient(cache_dir=str(cache)):
            pass
        with ImdbDatasetsClient(cache_dir=str(cache)):
            pass
        assert route.call_count == 1


def test_redownloads_when_stale(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir(parents=True)
    (cache / "title.ratings.tsv.gz").write_bytes(_make_tsv_gz(TSV))
    _age_file(cache / "title.ratings.tsv.gz", seconds_ago=25 * 3600)
    gz_bytes = _make_tsv_gz(TSV)
    with respx.mock as router:
        route = router.get(DEFAULT_DATASET_URL).mock(
            return_value=httpx.Response(200, content=gz_bytes))
        with ImdbDatasetsClient(cache_dir=str(cache)) as client:
            assert client.get_ratings("tt0111161")["imdb"] == 9.3
    assert route.call_count == 1  # stale -> re-downloaded


# ---------------------------------------------------------------------------
# injectable http (no network at all)
# ---------------------------------------------------------------------------

def test_injectable_http(tmp_path):
    cache = tmp_path / "cache"
    calls = []
    def fake_http(url):
        calls.append(url)
        return _make_tsv_gz(TSV)
    with ImdbDatasetsClient(cache_dir=str(cache), http=fake_http) as client:
        assert client.get_ratings("tt0111161") == {"imdb": 9.3, "imdb_votes": 2800000}
    assert calls == [DEFAULT_DATASET_URL]
