"""Golden-file tests for NFO XML generation (SPEC §7, O4).

2 movies + 1 TV show. Compares generated NFO bytes against golden files.
"""
from __future__ import annotations

from pathlib import Path


from finmeta.engine.nfo import movie_nfo, tvshow_nfo

GOLDEN = Path(__file__).parent / "golden" / "nfo"

MOVIE_FIELDS = [
    "title", "originaltitle", "sorttitle", "year", "premiered",
    "plot", "tagline", "runtime", "mpaa", "genres", "studios", "rating",
]
TV_FIELDS = [
    "title", "originaltitle", "sorttitle", "year", "premiered",
    "plot", "status", "genres", "studios", "rating",
]

# ---------------------------------------------------------------------------
# Fixture data (mirrors TMDb API response structure)
# ---------------------------------------------------------------------------

MATRIX = {
    "title": "The Matrix",
    "original_title": "The Matrix",
    "release_date": "1999-03-30",
    "overview": (
        "Set in the 22nd century, The Matrix tells the story of a computer "
        "hacker who joins a group of rebels who fight against the machines "
        "that have enslaved humanity."
    ),
    "tagline": "Welcome to the Real World",
    "runtime": 136,
    "vote_average": 8.2,
    "imdb_id": "tt0133093",
    "genres": [{"id": 28, "name": "Action"}, {"id": 878, "name": "Science Fiction"}],
    "production_companies": [
        {"name": "Village Roadshow Pictures"},
        {"name": "Warner Bros. Pictures"},
    ],
}

PULP = {
    "title": "Pulp Fiction",
    "original_title": "Pulp Fiction",
    "release_date": "1994-09-14",
    "overview": (
        "A burger-loving hit man, his pilot partner, and their fixer "
        "associate perform odd jobs for their mysterious employer."
    ),
    "tagline": "The world is yours",
    "runtime": 154,
    "vote_average": 8.5,
    "imdb_id": "tt0110912",
    "genres": [{"id": 53, "name": "Crime"}, {"id": 18, "name": "Drama"}],
    "production_companies": [
        {"name": "A Band Apart"},
        {"name": "Dimension Films"},
    ],
}

BREAKING_BAD = {
    "name": "Breaking Bad",
    "original_name": "Breaking Bad",
    "first_air_date": "2008-01-20",
    "overview": (
        "A high school chemistry teacher diagnosed with inoperable lung "
        "cancer turns to manufacturing and selling methamphetamine."
    ),
    "status": "Ended",
    "vote_average": 8.9,
    "imdb_id": "tt0903747",
    "genres": [{"id": 18, "name": "Drama"}, {"id": 80, "name": "Crime"}],
    "production_companies": [{"name": "Sony Pictures Television"}],
}


def _compare(golden_name: str, actual: bytes) -> None:
    golden = (GOLDEN / golden_name).read_bytes()
    assert actual == golden, (
        f"NFO mismatch for {golden_name}:\n"
        f"--- expected ---\n{golden.decode()}\n"
        f"--- actual ---\n{actual.decode()}"
    )


class TestMovieNfo:
    def test_matrix(self):
        actual = movie_nfo(MATRIX, 603, MOVIE_FIELDS)
        _compare("movie_matrix.xml", actual)

    def test_pulp_fiction(self):
        actual = movie_nfo(PULP, 680, MOVIE_FIELDS)
        _compare("movie_pulp_fiction.xml", actual)

    def test_lockdata_present(self):
        nfo = movie_nfo(MATRIX, 603, MOVIE_FIELDS)
        assert b"<lockdata>true</lockdata>" in nfo

    def test_uniqueid_tmdb(self):
        nfo = movie_nfo(MATRIX, 603, MOVIE_FIELDS)
        assert b'<uniqueid type="tmdb" default="true">603</uniqueid>' in nfo

    def test_uniqueid_imdb(self):
        nfo = movie_nfo(MATRIX, 603, MOVIE_FIELDS)
        assert b'<uniqueid type="imdb">tt0133093</uniqueid>' in nfo


class TestTvshowNfo:
    def test_breaking_bad(self):
        actual = tvshow_nfo(BREAKING_BAD, 1396, TV_FIELDS)
        _compare("tvshow_breaking_bad.xml", actual)

    def test_lockdata_present(self):
        nfo = tvshow_nfo(BREAKING_BAD, 1396, TV_FIELDS)
        assert b"<lockdata>true</lockdata>" in nfo

    def test_status_field(self):
        nfo = tvshow_nfo(BREAKING_BAD, 1396, TV_FIELDS)
        assert b"<status>Ended</status>" in nfo

    def test_uniqueid_tmdb(self):
        nfo = tvshow_nfo(BREAKING_BAD, 1396, TV_FIELDS)
        assert b'<uniqueid type="tmdb" default="true">1396</uniqueid>' in nfo
