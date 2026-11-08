"""SPEC_S10 SC5: missing seasons/episodes from Sonarr (tier 2, read-only)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from finmeta.engine.scanner import ScanSettings, check_episodes, scan_library

from _scan_helpers import FakeJellyfin, movie, mapper_for, series, tv_library

NOW = datetime(2026, 11, 8, tzinfo=timezone.utc)


def _ep(season: int, number: int, *, has_file: bool, monitored: bool = True,
        days_ago: int = 30) -> dict:
    return {
        "seasonNumber": season, "episodeNumber": number,
        "airDateUtc": (NOW - timedelta(days=days_ago)).isoformat().replace("+00:00", "Z"),
        "hasFile": has_file, "monitored": monitored, "finaleType": None,
    }


class FakeSonarr:
    url = "http://sonarr:8989"

    def __init__(self, episodes, *, fail: bool = False):
        self._episodes = episodes
        self.fail = fail
        self.calls = []

    def episodes(self, series_id):
        self.calls.append(series_id)
        if self.fail:
            raise RuntimeError("sonarr down")
        return self._episodes


def _index(episodes, *, tvdb="81189", slug="breaking-bad", **kw):
    client = FakeSonarr(episodes, **kw)
    row = {"id": 7, "title": "Breaking Bad", "titleSlug": slug, "tvdbId": int(tvdb)}
    return {tvdb: (client, row)}, client


def _show(tvdb="81189", name="Breaking Bad"):
    return series("s1", name, provider_ids={"Tvdb": tvdb})


def _codes(out):
    return sorted(f.check_code for f in out)


def test_a_complete_series_reports_nothing():
    index, client = _index([_ep(1, 1, has_file=True), _ep(1, 2, has_file=True)])
    assert check_episodes([_show()], library="TV", sonarr_index=index, now=NOW) == []
    assert client.calls == [7]


def test_some_aired_episodes_missing():
    index, _ = _index([
        _ep(1, 1, has_file=True), _ep(1, 2, has_file=False), _ep(1, 3, has_file=False),
    ])
    out = check_episodes([_show()], library="TV", sonarr_index=index, now=NOW)
    assert _codes(out) == ["episodes.missing_episodes"]
    f = out[0]
    assert f.severity == "warning"
    # Sonarr's job: Finmeta has never written to Sonarr and does not start here
    assert f.fix_kind == "external"
    assert f.detail["season"] == 1
    assert f.detail["count"] == 2
    assert f.detail["missing"] == ["S01E02", "S01E03"]
    assert f.detail["sonarr"] == "http://sonarr:8989/series/breaking-bad"


def test_a_season_with_no_files_at_all_is_an_error():
    index, _ = _index([_ep(2, 1, has_file=False), _ep(2, 2, has_file=False)])
    out = check_episodes([_show()], library="TV", sonarr_index=index, now=NOW)
    assert _codes(out) == ["episodes.missing_season"]
    assert out[0].severity == "error"
    assert "season 2" in out[0].detail["note"]


def test_unaired_episodes_are_not_missing():
    """Sonarr knows about unaired episodes; a running season is not half-broken."""
    index, _ = _index([
        _ep(1, 1, has_file=True),
        _ep(1, 2, has_file=False, days_ago=-7),     # airs next week
    ])
    assert check_episodes([_show()], library="TV", sonarr_index=index, now=NOW) == []


def test_an_episode_without_an_air_date_counts_as_unaired():
    index, _ = _index([
        _ep(1, 1, has_file=True),
        {**_ep(1, 2, has_file=False), "airDateUtc": None},
    ])
    assert check_episodes([_show()], library="TV", sonarr_index=index, now=NOW) == []


def test_unmonitored_episodes_are_not_missing():
    index, _ = _index([
        _ep(1, 1, has_file=True),
        _ep(1, 2, has_file=False, monitored=False),
    ])
    assert check_episodes([_show()], library="TV", sonarr_index=index, now=NOW) == []


def test_specials_are_excluded_unless_asked_for():
    index, _ = _index([_ep(0, 1, has_file=False), _ep(1, 1, has_file=True)])
    assert check_episodes([_show()], library="TV", sonarr_index=index, now=NOW) == []
    out = check_episodes([_show()], library="TV", sonarr_index=index, now=NOW,
                         include_specials=True)
    assert _codes(out) == ["episodes.missing_season"]
    assert out[0].detail["season"] == 0


def test_one_finding_per_season():
    index, _ = _index([
        _ep(1, 1, has_file=True), _ep(1, 2, has_file=False),
        _ep(2, 1, has_file=True), _ep(2, 2, has_file=False),
    ])
    out = check_episodes([_show()], library="TV", sonarr_index=index, now=NOW)
    assert [f.detail["season"] for f in out] == [1, 2]


def test_the_missing_list_is_capped():
    eps = [_ep(1, 1, has_file=True)] + [_ep(1, n, has_file=False) for n in range(2, 80)]
    index, _ = _index(eps)
    out = check_episodes([_show()], library="TV", sonarr_index=index, now=NOW)
    assert out[0].detail["count"] == 78
    assert len(out[0].detail["missing"]) == 50
    assert out[0].detail["missing_truncated"] == 28


def test_a_series_sonarr_does_not_know_is_info_only():
    index, _ = _index([_ep(1, 1, has_file=True)])
    out = check_episodes([_show(tvdb="999999")], library="TV",
                         sonarr_index=index, now=NOW)
    assert _codes(out) == ["episodes.not_in_sonarr"]
    assert out[0].severity == "info"


def test_a_series_without_a_tvdb_id_is_also_just_info():
    out = check_episodes([series("s1", "Mystery", provider_ids={"Tmdb": "1"})],
                         library="TV", sonarr_index=_index([])[0], now=NOW)
    assert _codes(out) == ["episodes.not_in_sonarr"]


def test_nothing_is_reported_when_sonarr_is_not_configured():
    """Otherwise the whole library would read as 'not in Sonarr'."""
    assert check_episodes([_show()], library="TV", sonarr_index={}, now=NOW) == []


def test_a_failing_episode_fetch_skips_that_series():
    index, client = _index([_ep(1, 1, has_file=False)], fail=True)
    assert check_episodes([_show()], library="TV", sonarr_index=index, now=NOW) == []
    assert client.calls == [7]


# ---------------------------------------------------------------------------
# the library pass
# ---------------------------------------------------------------------------

def _run(jf, tmp_path, **kw):
    emitted = []
    res = scan_library(jf, library="TV", lib_cfg=tv_library(artwork={}),
                       mapper=mapper_for(tmp_path), emit=emitted.append, **kw)
    return res, [f for b in emitted for f in b]


def test_episodes_only_run_in_a_deep_scan(tmp_path):
    index, client = _index([_ep(1, 1, has_file=False), _ep(1, 2, has_file=False)])
    jf = FakeJellyfin([_show()])

    res, findings = _run(jf, tmp_path, sonarr_index=index)
    assert client.calls == []                       # tier 1: no Sonarr at all
    assert [f for f in findings if f.group == "episodes"] == []

    res, findings = _run(jf, tmp_path, sonarr_index=index,
                         settings=ScanSettings(tier=2, checks={"metadata": False}))
    assert [f.check_code for f in findings] == ["episodes.missing_season"]


def test_movies_are_never_asked_about_episodes(tmp_path):
    index, client = _index([_ep(1, 1, has_file=False)])
    jf = FakeJellyfin([movie("1", "Alpha")])
    _run(jf, tmp_path, sonarr_index=index,
         settings=ScanSettings(tier=2, checks={"metadata": False}))
    assert client.calls == []


def test_episodes_can_be_switched_off_in_a_deep_scan(tmp_path):
    index, client = _index([_ep(1, 1, has_file=False)])
    jf = FakeJellyfin([_show()])
    res, findings = _run(jf, tmp_path, sonarr_index=index,
                         settings=ScanSettings(tier=2, checks={"episodes": False,
                                                               "metadata": False}))
    assert client.calls == []
    assert findings == []
