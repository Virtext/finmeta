"""Unit tests for finmeta.arr.match (S9f1b). All arr clients are mocked —
these tests never touch a real Sonarr/Radarr instance."""
from __future__ import annotations

from types import SimpleNamespace


from finmeta.arr.match import ArrCatalog, ArrFacts, build_catalog


class FakeClient:
    def __init__(self, rows):
        self._rows = rows
        self.calls = 0

    def series(self):
        self.calls += 1
        return list(self._rows)

    def movies(self):
        self.calls += 1
        return list(self._rows)


def _cfg(sonarr=None, radarr=None):
    ma = SimpleNamespace(
        sonarr=[SimpleNamespace(name=n, enabled=e) for n, e in (sonarr or [])],
        radarr=[SimpleNamespace(name=n, enabled=e) for n, e in (radarr or [])],
    )
    return SimpleNamespace(media_apps=ma)


def _secrets():
    class S:
        def get(self, name):
            return "k" + name
    return S()


class Ctx:
    """make_client factory that records what it was asked to build."""

    def __init__(self):
        self.built = []

    def make(self, app, inst, key):
        # rows keyed by instance name so tests can control each instance
        rows = getattr(inst, "rows", [])
        c = FakeClient(rows)
        self.built.append((inst.name, app))
        return c


def test_show_matched_by_tvdb():
    son = [FakeClient([{"id": 1, "tvdbId": "12345", "tmdbId": "999",
                        "status": "continuing", "monitored": True,
                        "nextAiring": "2026-01-01T00:00:00Z"}])]
    cat = ArrCatalog([("Sonarr", "sonarr", son[0])])
    f = cat.match("Series", {"Tvdb": "12345", "Tmdb": "999"})
    assert f is not None
    assert isinstance(f, ArrFacts)
    assert (f.instance, f.kind) == ("Sonarr", "sonarr")
    assert f.status == "continuing" and f.monitored is True
    assert f.next_airing == "2026-01-01T00:00:00Z"


def test_movie_matched_by_tmdb():
    rad = [FakeClient([{"id": 2, "tmdbId": "603", "imdbId": "tt0133093",
                        "status": "released", "monitored": False,
                        "hasFile": True, "inCinemas": None,
                        "digitalRelease": "2025-01-01T00:00:00Z"}])]
    cat = ArrCatalog([("Radarr", "radarr", rad[0])])
    f = cat.match("Movie", {"Tmdb": "603"})
    assert f is not None and (f.instance, f.kind) == ("Radarr", "radarr")
    assert f.has_file is True and f.digital_release == "2025-01-01T00:00:00Z"


def test_fallback_to_imdb():
    rad = [FakeClient([{"id": 3, "tmdbId": None, "imdbId": "tt0111161",
                        "status": "tba", "monitored": True, "hasFile": False}])]
    cat = ArrCatalog([("Radarr", "radarr", rad[0])])
    f = cat.match("Movie", {"Imdb": "tt0111161"})
    assert f is not None and f.instance == "Radarr" and f.status == "tba"
    # show fallback: Tvdb missing -> Tmdb missing -> Imdb
    son = [FakeClient([{"id": 4, "tvdbId": None, "tmdbId": None,
                        "imdbId": "tt0111161", "status": "upcoming"}])]
    cat2 = ArrCatalog([("Sonarr", "sonarr", son[0])])
    f2 = cat2.match("Series", {"Imdb": "tt0111161"})
    assert f2 is not None and f2.instance == "Sonarr"


def test_no_match_returns_none():
    cat = ArrCatalog([("Radarr", "radarr", FakeClient(
        [{"id": 5, "tmdbId": "1", "imdbId": "tt0"}]))])
    assert cat.match("Movie", {"Tmdb": "999"}) is None
    assert cat.match("Series", {"Tvdb": "42"}) is None


def test_two_instances_first_wins():
    a = FakeClient([{"id": 1, "tvdbId": "12345", "status": "continuing"}])
    b = FakeClient([{"id": 9, "tvdbId": "12345", "status": "ended"}])
    cat = ArrCatalog([("Sonarr", "sonarr", a), ("Sonarr4K", "sonarr", b)])
    f = cat.match("Series", {"Tvdb": "12345"})
    assert f is not None and f.instance == "Sonarr"  # first in order wins
    assert f.status == "continuing"


def test_disabled_instance_ignored():
    ctx = Ctx()
    inst_on = SimpleNamespace(name="Radarr", enabled=True, api_key="radarr_key",
                              rows=[{"id": 1, "tmdbId": "603", "status": "released"}])
    inst_off = SimpleNamespace(name="RadarrOff", enabled=False, api_key="off_key",
                               rows=[{"id": 2, "tmdbId": "777", "status": "released"}])
    cfg = _cfg(radarr=[("Radarr", True), ("RadarrOff", False)])
    # wire the fake client to each instance's rows
    def make(app, inst, key):
        return FakeClient(getattr(inst, "rows", []))

    class CfgWrap:
        media_apps = SimpleNamespace(
            sonarr=[], radarr=[inst_on, inst_off])

    cat = build_catalog(CfgWrap(), _secrets(), make)
    assert cat.instance_names == ["Radarr"]  # disabled one not fetched
    f = cat.match("Movie", {"Tmdb": "603"})
    assert f is not None and f.instance == "Radarr"
    assert cat.match("Movie", {"Tmdb": "777"}) is None


def test_fetch_happens_once_per_run():
    a = FakeClient([{"id": 1, "tvdbId": "1"}])
    cat = ArrCatalog([("Sonarr", "sonarr", a)])
    cat.match("Series", {"Tvdb": "1"})
    cat.match("Series", {"Tvdb": "2"})
    assert a.calls == 1  # series() fetched once, cached for the run


def test_unreachable_instance_does_not_break_others():
    class Boom:
        def series(self):
            raise RuntimeError("connection refused")

        def movies(self):
            raise RuntimeError("connection refused")

    ok = FakeClient([{"id": 1, "tmdbId": "603", "status": "released"}])
    cat = ArrCatalog([("Bad", "radarr", Boom()), ("Radarr", "radarr", ok)])
    f = cat.match("Movie", {"Tmdb": "603"})
    assert f is not None and f.instance == "Radarr"


def test_facts_to_dict_roundtrip():
    f = ArrFacts(instance="Radarr", kind="radarr", status="released",
                 monitored=True, has_file=False)
    d = f.to_dict()
    assert d["instance"] == "Radarr" and d["kind"] == "radarr"
    assert d["has_file"] is False and d["next_airing"] is None


__all__ = []
