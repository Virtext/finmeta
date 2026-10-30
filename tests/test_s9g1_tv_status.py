"""S9g1 (K7a): TV show status computation from Sonarr data.

Clean-room implementation (SPEC_KOMETA.md K7a's licence note) -- these tests
exercise engine.tv_status.compute_tv_status against synthetic Sonarr-shaped
dicts, never a real Sonarr instance.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from finmeta.engine.tv_status import compute_tv_status

NOW = datetime(2026, 10, 18, tzinfo=timezone.utc)


def _dt(days_from_now: int) -> str:
    return (NOW + timedelta(days=days_from_now)).isoformat().replace("+00:00", "Z")


def _ep(season, num, days_from_now, has_file=None, finale_type=None):
    return {
        "seasonNumber": season, "episodeNumber": num,
        "airDateUtc": _dt(days_from_now),
        "hasFile": has_file if has_file is not None else days_from_now <= 0,
        "monitored": True,
        "finaleType": finale_type,
    }


def test_deleted_returns_none():
    assert compute_tv_status({"status": "deleted"}, [], now=NOW) is None


def test_deleted_case_insensitive():
    assert compute_tv_status({"status": "Deleted"}, [], now=NOW) is None


def test_canceled_detected_from_status_string():
    out = compute_tv_status({"status": "Canceled"}, [], now=NOW)
    assert out.status == "canceled"


def test_upcoming_status_is_new_series():
    series = {"status": "upcoming", "nextAiring": _dt(5)}
    episodes = [_ep(1, 1, 5, has_file=False)]
    out = compute_tv_status(series, episodes, now=NOW)
    assert out.status == "new_series"
    assert out.date == (NOW + timedelta(days=5)).date().isoformat()
    assert "NEW SERIES" in out.label


def test_continuing_but_nothing_ever_aired_is_new_series():
    """Defensive: zero aired episodes overrides a "continuing" status string."""
    series = {"status": "continuing", "nextAiring": _dt(3)}
    episodes = [_ep(1, 1, 3, has_file=False)]
    out = compute_tv_status(series, episodes, now=NOW)
    assert out.status == "new_series"


def test_ended_with_recent_series_finale():
    series = {"status": "ended"}
    episodes = [
        _ep(1, 1, -40), _ep(1, 2, -30),
        _ep(2, 1, -10, finale_type="series"),
    ]
    out = compute_tv_status(series, episodes, now=NOW, recent_days=14)
    assert out.status == "final_episode_aired"
    expected_date = (NOW + timedelta(days=-10)).strftime("%d.%m")
    assert expected_date in out.label
    assert out.date == (NOW + timedelta(days=-10)).date().isoformat()


def test_ended_with_old_series_finale_is_plain_ended():
    series = {"status": "ended"}
    episodes = [_ep(1, 1, -400, finale_type="series")]
    out = compute_tv_status(series, episodes, now=NOW, recent_days=14)
    assert out.status == "ended"
    assert out.date is None


def test_ended_without_finale_type_hint_is_plain_ended():
    """No finaleType hint -> can't PROVE it was the series finale -- stays "ended"."""
    series = {"status": "ended"}
    episodes = [_ep(1, 1, -5)]
    out = compute_tv_status(series, episodes, now=NOW, recent_days=14)
    assert out.status == "ended"


def test_continuing_next_airing_is_new_season_premiere():
    series = {"status": "continuing", "nextAiring": _dt(7)}
    episodes = [
        _ep(1, 1, -300), _ep(1, 8, -290, finale_type="season"),
        _ep(2, 1, 7, has_file=False),
    ]
    out = compute_tv_status(series, episodes, now=NOW)
    assert out.status == "new_season_soon"
    assert out.date is not None


def test_continuing_next_airing_is_finale_via_hint():
    series = {"status": "continuing", "nextAiring": _dt(3)}
    episodes = [
        _ep(2, 1, -20), _ep(2, 2, -13),
        _ep(2, 3, 3, has_file=False, finale_type="season"),
    ]
    out = compute_tv_status(series, episodes, now=NOW)
    assert out.status == "upcoming_finale"


def test_continuing_next_airing_is_finale_via_no_higher_known_episode():
    """No finaleType hint at all, but no higher episode number is known for
    this season -- the no-hint heuristic treats it as the finale."""
    series = {"status": "continuing", "nextAiring": _dt(3)}
    episodes = [_ep(2, 1, -20), _ep(2, 2, -13), _ep(2, 3, 3, has_file=False)]
    out = compute_tv_status(series, episodes, now=NOW)
    assert out.status == "upcoming_finale"


def test_continuing_next_airing_midseason_finale_counts_as_finale():
    series = {"status": "continuing", "nextAiring": _dt(3)}
    episodes = [_ep(2, 1, -20), _ep(2, 2, 3, has_file=False, finale_type="midseason"),
                _ep(2, 3, 10, has_file=False)]
    out = compute_tv_status(series, episodes, now=NOW)
    assert out.status == "upcoming_finale"


def test_continuing_next_airing_regular_episode():
    series = {"status": "continuing", "nextAiring": _dt(3)}
    episodes = [_ep(2, 1, -20), _ep(2, 2, 3, has_file=False, finale_type=None),
                _ep(2, 3, 10, has_file=False)]
    out = compute_tv_status(series, episodes, now=NOW)
    assert out.status == "upcoming_episode"


def test_continuing_no_next_airing_recent_season_premiere():
    series = {"status": "continuing", "previousAiring": _dt(-2)}
    episodes = [_ep(1, 8, -300, finale_type="season"), _ep(2, 1, -2)]
    out = compute_tv_status(series, episodes, now=NOW, recent_days=14)
    assert out.status == "new_season_started"


def test_continuing_no_next_airing_recent_season_finale():
    series = {"status": "continuing", "previousAiring": _dt(-5)}
    episodes = [_ep(2, 1, -40), _ep(2, 2, -5, finale_type="season")]
    out = compute_tv_status(series, episodes, now=NOW, recent_days=14)
    assert out.status == "season_finale_aired"


def test_continuing_no_next_airing_old_previous_airing_is_returning():
    series = {"status": "continuing", "previousAiring": _dt(-100)}
    episodes = [_ep(2, 1, -100, finale_type="season")]
    out = compute_tv_status(series, episodes, now=NOW, recent_days=14)
    assert out.status == "returning"
    assert out.date is None


def test_continuing_no_airing_dates_at_all_is_returning():
    series = {"status": "continuing"}
    episodes = [_ep(1, 1, -200)]
    out = compute_tv_status(series, episodes, now=NOW)
    assert out.status == "returning"


def test_upcoming_episode_today_says_today():
    series = {"status": "continuing", "nextAiring": _dt(0)}
    episodes = [_ep(2, 1, -20), _ep(2, 2, 0, has_file=False), _ep(2, 3, 10, has_file=False)]
    out = compute_tv_status(series, episodes, now=NOW)
    assert out.status == "upcoming_episode"
    assert out.label == "NEW EPISODE TODAY"


def test_upcoming_episode_tomorrow_says_tomorrow():
    series = {"status": "continuing", "nextAiring": _dt(1)}
    episodes = [_ep(2, 1, -20), _ep(2, 2, 1, has_file=False), _ep(2, 3, 10, has_file=False)]
    out = compute_tv_status(series, episodes, now=NOW)
    assert out.status == "upcoming_episode"
    assert out.label == "NEW EPISODE TOMORROW"


def test_upcoming_episode_within_week_says_weekday_name():
    # NOW = 2026-10-18 (a Sunday); +4 days = 2026-10-22 (Thursday).
    series = {"status": "continuing", "nextAiring": _dt(4)}
    episodes = [_ep(2, 1, -20), _ep(2, 2, 4, has_file=False), _ep(2, 3, 10, has_file=False)]
    out = compute_tv_status(series, episodes, now=NOW)
    assert out.status == "upcoming_episode"
    assert out.label == "NEW EPISODE THURSDAY"


def test_upcoming_episode_far_out_says_date():
    series = {"status": "continuing", "nextAiring": _dt(30)}
    episodes = [_ep(2, 1, -20), _ep(2, 2, 30, has_file=False), _ep(2, 3, 40, has_file=False)]
    out = compute_tv_status(series, episodes, now=NOW)
    assert out.status == "upcoming_episode"
    expected_date = (NOW + timedelta(days=30)).strftime("%d.%m")
    assert out.label == f"NEW EPISODE {expected_date}"


def test_season_finale_aired_recently_still_uses_date_not_relative():
    """Past dates (the *_aired statuses) never say "tomorrow" or a weekday
    name -- those only make sense for a countdown to something upcoming."""
    series = {"status": "continuing", "previousAiring": _dt(-5)}
    episodes = [_ep(2, 1, -40), _ep(2, 2, -5, finale_type="season")]
    out = compute_tv_status(series, episodes, now=NOW, recent_days=14)
    assert out.status == "season_finale_aired"
    expected_date = (NOW + timedelta(days=-5)).strftime("%d.%m")
    assert out.label == f"SEASON FINALE {expected_date}"


def test_classic_date_style_always_uses_date_even_for_today_tomorrow():
    """Operator request (2026-10-26): date_style='classic' opts back into the
    plain DD.MM wording the relative TODAY/TOMORROW/weekday format replaced,
    for today/tomorrow/within-the-week cases too, not just the far-out case
    (which already prints a date either way)."""
    series = {"status": "continuing", "nextAiring": _dt(0)}
    episodes = [_ep(2, 1, -20), _ep(2, 2, 0, has_file=False), _ep(2, 3, 10, has_file=False)]
    out = compute_tv_status(series, episodes, now=NOW, date_style="classic")
    assert out.status == "upcoming_episode"
    expected_date = NOW.strftime("%d.%m")
    assert out.label == f"NEW EPISODE {expected_date}"

    series2 = {"status": "continuing", "nextAiring": _dt(4)}
    episodes2 = [_ep(2, 1, -20), _ep(2, 2, 4, has_file=False), _ep(2, 3, 10, has_file=False)]
    out2 = compute_tv_status(series2, episodes2, now=NOW, date_style="classic")
    expected_date2 = (NOW + timedelta(days=4)).strftime("%d.%m")
    assert out2.label == f"NEW EPISODE {expected_date2}"


def test_weekday_date_style_always_uses_a_day_name():
    """Operator request (2026-10-30): date_style='weekday' always shows the
    weekday name -- never TODAY/TOMORROW, never a numeric date, regardless
    of how near or far the date is (unlike 'relative', which falls back to
    DD.MM beyond a week)."""
    import calendar

    series = {"status": "continuing", "nextAiring": _dt(0)}
    episodes = [_ep(2, 1, -20), _ep(2, 2, 0, has_file=False), _ep(2, 3, 10, has_file=False)]
    out = compute_tv_status(series, episodes, now=NOW, date_style="weekday")
    assert out.label == f"NEW EPISODE {calendar.day_name[NOW.weekday()].upper()}"

    far = NOW + timedelta(days=20)
    series2 = {"status": "continuing", "nextAiring": _dt(20)}
    episodes2 = [_ep(2, 1, -20), _ep(2, 2, 20, has_file=False), _ep(2, 3, 30, has_file=False)]
    out2 = compute_tv_status(series2, episodes2, now=NOW, date_style="weekday")
    assert out2.label == f"NEW EPISODE {calendar.day_name[far.weekday()].upper()}"


def test_weekday_date_style_also_applies_to_past_aired_dates():
    """Unlike 'relative' (test_season_finale_aired_recently_still_uses_date_not_relative
    above, DD.MM for past dates), 'weekday' names every date including the
    *_aired statuses -- no numeric-date exception."""
    import calendar

    series = {"status": "continuing", "previousAiring": _dt(-5)}
    episodes = [_ep(2, 1, -40), _ep(2, 2, -5, finale_type="season")]
    out = compute_tv_status(series, episodes, now=NOW, recent_days=14, date_style="weekday")
    assert out.status == "season_finale_aired"
    past = NOW + timedelta(days=-5)
    assert out.label == f"SEASON FINALE {calendar.day_name[past.weekday()].upper()}"
