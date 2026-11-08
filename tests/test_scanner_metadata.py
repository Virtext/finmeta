"""SPEC_S10 SC2: missing fields (tier 1) and wrong matches (tier 2)."""
from __future__ import annotations


from finmeta.engine.scanner import (
    ScanSettings, check_metadata_fields, check_metadata_match, scan_library,
)
from finmeta.jellyfin_client.types import Item

from _scan_helpers import FakeJellyfin, mapper_for, movie, movie_library


def _full(item_id="1", name="Alpha", **over) -> Item:
    """An item with every SC2 field populated."""
    payload = {
        "Id": item_id, "Name": name, "Type": "Movie",
        "Path": f"/media/Movies/{name}/{name}.mkv",
        "ProviderIds": {"Tmdb": "603"},
        "Overview": "A film about things.",
        "ProductionYear": 1999,
        "OfficialRating": "15",
        "Genres": ["Action"],
        "Studios": [{"Name": "Warner", "Id": "x"}],
    }
    payload.update(over)
    return Item.from_dict(payload)


def _codes(out):
    return sorted(f.check_code for f in out)


# ---------------------------------------------------------------------------
# tier 1: empty fields
# ---------------------------------------------------------------------------

def test_a_fully_populated_item_reports_nothing():
    assert check_metadata_fields([_full()], library="Movies") == []


def test_no_provider_id_is_an_error_and_not_fixable_here():
    it = _full(ProviderIds={})
    out = check_metadata_fields([it], library="Movies")
    assert _codes(out) == ["metadata.no_provider_id"]
    assert out[0].severity == "error"
    # the id lives in Jellyfin's own database
    assert out[0].fix_kind == "external"


def test_any_usable_provider_id_counts():
    for pids in ({"Tmdb": "1"}, {"Imdb": "tt1"}, {"Tvdb": "9"}):
        assert check_metadata_fields([_full(ProviderIds=pids)], library="Movies") == []


def test_an_unidentified_item_is_reported_once_not_five_times():
    """Every other field is empty too, but the missing id is THE problem."""
    it = Item.from_dict({"Id": "1", "Name": "Ghost", "Type": "Movie"})
    assert _codes(check_metadata_fields([it], library="Movies")) == [
        "metadata.no_provider_id"]


def test_missing_overview_and_year_are_warnings():
    out = check_metadata_fields([_full(Overview="  ", ProductionYear=None)],
                                library="Movies")
    assert _codes(out) == ["metadata.missing_overview", "metadata.missing_year"]
    assert all(f.severity == "warning" for f in out)


def test_thin_soft_fields_are_one_info_row():
    out = check_metadata_fields([_full(Genres=[], Studios=[], OfficialRating=None)],
                                library="Movies")
    assert _codes(out) == ["metadata.missing_fields"]
    assert out[0].severity == "info"
    assert out[0].detail["fields"] == ["genres", "studios", "official_rating"]


def test_the_fix_is_an_apply_only_when_the_library_writes_nfos():
    it = _full(Overview=None)
    assert check_metadata_fields([it], library="Movies")[0].fix_kind == "external"
    out = check_metadata_fields([it], library="Movies", writes_nfo=True)
    assert out[0].fix_kind == "apply_item"
    assert out[0].detail["writes_nfo"] is True


# ---------------------------------------------------------------------------
# tier 2: matching
# ---------------------------------------------------------------------------

class FakeTmdb:
    """Minimal stand-in: provider-id matches never hit the network at all."""

    def __init__(self, *, movie_year="1999-03-31", fail_details=False):
        self.movie_year = movie_year
        self.fail_details = fail_details
        self.details_calls = []
        self.search_calls = []

    def movie_details(self, tmdb_id, language=None):
        self.details_calls.append(tmdb_id)
        if self.fail_details:
            raise RuntimeError("tmdb down")
        return {"release_date": self.movie_year}

    def tv_details(self, tmdb_id, language=None):
        self.details_calls.append(tmdb_id)
        return {"first_air_date": self.movie_year}

    # match_item's title+year fallback goes through these
    def search_movies(self, *a, **kw):
        self.search_calls.append((a, kw))
        return []

    def search_tv(self, *a, **kw):
        self.search_calls.append((a, kw))
        return []

    def find_by_imdb(self, *a, **kw):
        return None


def test_a_matching_year_reports_nothing():
    tmdb = FakeTmdb(movie_year="1999-03-31")
    assert check_metadata_match([_full(ProductionYear=1999)], library="Movies",
                                tmdb=tmdb) == []
    assert tmdb.details_calls == [603]      # one details lookup, no search


def test_one_year_off_is_tolerated():
    """Release-date rollovers across new year are normal, not a wrong match."""
    tmdb = FakeTmdb(movie_year="2000-01-05")
    assert check_metadata_match([_full(ProductionYear=1999)], library="Movies",
                                tmdb=tmdb) == []


def test_a_year_far_off_means_the_wrong_title():
    tmdb = FakeTmdb(movie_year="2021-10-22")
    out = check_metadata_match([_full(ProductionYear=1999)], library="Movies",
                               tmdb=tmdb)
    assert _codes(out) == ["metadata.year_mismatch"]
    assert out[0].severity == "warning"
    assert out[0].fix_kind == "external"
    assert out[0].detail["jellyfin_year"] == 1999
    assert out[0].detail["tmdb_year"] == 2021
    assert out[0].detail["matched_by"] == "provider_id"


def test_an_unmatchable_item_is_reported_with_the_matchers_reason():
    tmdb = FakeTmdb()
    it = _full(ProviderIds={}, Name="Nothing Like This (1899)")
    out = check_metadata_match([it], library="Movies", tmdb=tmdb)
    assert _codes(out) == ["metadata.tmdb_unmatched"]
    assert out[0].severity == "error"
    assert "no TMDb match" in out[0].detail["reason"]


def test_an_item_without_a_year_skips_the_details_lookup():
    tmdb = FakeTmdb()
    assert check_metadata_match([_full(ProductionYear=None)], library="Movies",
                                tmdb=tmdb) == []
    assert tmdb.details_calls == []


def test_a_failing_details_lookup_reports_nothing_rather_than_a_mismatch():
    tmdb = FakeTmdb(fail_details=True)
    assert check_metadata_match([_full(ProductionYear=1999)], library="Movies",
                                tmdb=tmdb) == []


# ---------------------------------------------------------------------------
# the library pass
# ---------------------------------------------------------------------------

def _run(jf, tmp_path, **kw):
    emitted = []
    res = scan_library(jf, library="Movies",
                       lib_cfg=movie_library(artwork={}), mapper=mapper_for(tmp_path),
                       emit=emitted.append, **kw)
    return res, [f for b in emitted for f in b]


def test_field_checks_run_in_a_plain_scan(tmp_path):
    jf = FakeJellyfin([movie("1", "Alpha", overview=None)])
    res, findings = _run(jf, tmp_path)
    assert "metadata.missing_overview" in [f.check_code for f in findings]


def test_match_checks_only_run_in_a_deep_scan(tmp_path):
    jf = FakeJellyfin([_full(ProductionYear=1999)])
    tmdb = FakeTmdb(movie_year="2021-10-22")
    res, findings = _run(jf, tmp_path, tmdb=tmdb)
    assert tmdb.details_calls == []                  # tier 1: no TMDb at all
    assert "metadata.year_mismatch" not in [f.check_code for f in findings]

    res, findings = _run(jf, tmp_path, tmdb=tmdb,
                         settings=ScanSettings(tier=2))
    assert "metadata.year_mismatch" in [f.check_code for f in findings]


def test_a_deep_scan_without_a_tmdb_client_still_does_the_field_checks(tmp_path):
    jf = FakeJellyfin([movie("1", "Alpha", overview=None)])
    res, findings = _run(jf, tmp_path, settings=ScanSettings(tier=2))
    assert "metadata.missing_overview" in [f.check_code for f in findings]


def test_metadata_can_be_switched_off(tmp_path):
    jf = FakeJellyfin([movie("1", "Alpha", overview=None)])
    res, findings = _run(jf, tmp_path,
                         settings=ScanSettings(checks={"metadata": False}))
    assert [f for f in findings if f.group == "metadata"] == []
