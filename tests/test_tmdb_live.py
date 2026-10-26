"""Live TMDb smoke test. Skipped unless TMDB_KEY is set in the environment."""
from __future__ import annotations

import os

import pytest

from finmeta.sources import TmdbClient

pytestmark = pytest.mark.tmdb

requires_key = pytest.mark.skipif(
    not os.environ.get("TMDB_KEY"), reason="TMDB_KEY not set"
)


@requires_key
def test_live_movie_details():
    with TmdbClient(api_key=os.environ["TMDB_KEY"], min_interval=0.25) as c:
        d = c.movie_details(155, language="en-US")
    assert d.get("id") == 155
    assert d.get("title") == "The Dark Knight"
    assert d.get("release_date", "").startswith("2008")


@requires_key
def test_live_movie_images_has_poster():
    with TmdbClient(api_key=os.environ["TMDB_KEY"], min_interval=0.25) as c:
        cands = c.movie_images(155, language="en-US", language_fallback=["en", None])
    posters = [x for x in cands if x.kind == "poster"]
    assert posters, "expected at least one poster from TMDb"
    assert posters[0].ref.startswith("https://image.tmdb.org/t/p/")
    assert posters[0].width and posters[0].height
