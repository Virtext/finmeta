"""S9f2: preset collections -- chart presets + Adaptive Seasonal Collections.

Covers:
  - engine.collection_presets.is_holiday_active_now: seasonal holiday windows.
  - _build_api_actions: chart sorting/limiting, and the adaptive-seasonal
    composite block (genre / keyword / mdblist matching per holiday).
  - fetch_seasonal_mdblist_items: gating (preset off / no secrets / mdblist
    disabled / no key / outside Christmas's window all short-circuit).
  - executor._apply_api_action: the "sync" branch (create/reconcile/delete) --
    unchanged by this session, re-verified for regressions.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from finmeta.config.models import (
    Config, Metadata, MovieArtwork, MovieLibrary, ServerConfig,
)
from finmeta.engine.collection_presets import SEASONAL_CALENDAR, is_holiday_active_now
from finmeta.engine.executor import _apply_api_action
from finmeta.engine.plan import ApiAction, ItemPlan
from finmeta.engine.plan_builder import (
    _build_api_actions, build_plan, fetch_seasonal_mdblist_items,
)
from finmeta.jellyfin_client.types import Item, Library


def _preset_cfg(preset_ids) -> Config:
    return Config(dry_run=True, server=ServerConfig(url="http://x:8096"),
                  collection_presets=list(preset_ids))


def _plan_item(tmdb_id, item_id, media_type="movie"):
    return ItemPlan(item_id=item_id, item_name=item_id, media_type=media_type, tmdb_id=tmdb_id)


def _tmdb_with(details_by_id, keywords_by_id=None):
    tmdb = MagicMock()
    tmdb.movie_details.side_effect = lambda tid, language=None: details_by_id.get(tid, {})
    tmdb.tv_details.side_effect = lambda tid, language=None: details_by_id.get(tid, {})
    kw = keywords_by_id or {}
    tmdb.movie_keywords.side_effect = lambda tid: kw.get(tid, [])
    return tmdb


def _holiday(hid):
    return next(h for h in SEASONAL_CALENDAR if h.id == hid)


# ---------------------------------------------------------------------------
# is_holiday_active_now
# ---------------------------------------------------------------------------

class TestIsHolidayActiveNow:
    def test_genre_holiday_active_in_its_window(self):
        oct_15 = datetime(2026, 10, 15, tzinfo=timezone.utc)
        assert is_holiday_active_now(_holiday("halloween"), now=oct_15) is True

    def test_genre_holiday_inactive_outside_its_window(self):
        nov_1 = datetime(2026, 11, 1, tzinfo=timezone.utc)
        assert is_holiday_active_now(_holiday("halloween"), now=nov_1) is False

    def test_keyword_holiday_window_boundaries(self):
        assert is_holiday_active_now(_holiday("thanksgiving"), now=datetime(2026, 11, 1, tzinfo=timezone.utc)) is True
        assert is_holiday_active_now(_holiday("thanksgiving"), now=datetime(2026, 11, 30, tzinfo=timezone.utc)) is True
        assert is_holiday_active_now(_holiday("thanksgiving"), now=datetime(2026, 12, 1, tzinfo=timezone.utc)) is False

    def test_valentines_day_boundaries(self):
        assert is_holiday_active_now(_holiday("valentines"), now=datetime(2026, 1, 31, tzinfo=timezone.utc)) is False
        assert is_holiday_active_now(_holiday("valentines"), now=datetime(2026, 2, 1, tzinfo=timezone.utc)) is True


# ---------------------------------------------------------------------------
# _build_api_actions: chart presets (unchanged logic, re-verified after the
# preset-loop split)
# ---------------------------------------------------------------------------

class TestChartApiActions:
    def test_unknown_preset_id_is_ignored(self):
        cfg = _preset_cfg(["not_a_real_preset"])
        actions = _build_api_actions(cfg, [], tmdb=MagicMock(), language="en")
        assert actions == []

    def test_no_presets_configured_is_a_noop(self):
        cfg = _preset_cfg([])
        actions = _build_api_actions(cfg, [_plan_item(1, "m1")], tmdb=_tmdb_with({}), language="en")
        assert actions == []

    def test_scoped_library_call_skips_presets_entirely(self):
        cfg = _preset_cfg(["adaptive_seasonal", "top_popular_movies"])
        items = [_plan_item(1, "m1")]
        tmdb = _tmdb_with({1: {"genres": [{"name": "Horror"}]}})
        actions = _build_api_actions(cfg, items, tmdb=tmdb, language="en", library_name="Movies")
        assert actions == []

    def test_chart_preset_sorts_by_popularity_and_limits(self):
        cfg = _preset_cfg(["top_popular_movies"])
        items = [_plan_item(i, f"m{i}") for i in range(1, 4)]
        tmdb = _tmdb_with({
            1: {"popularity": 10.0}, 2: {"popularity": 30.0}, 3: {"popularity": 20.0},
        })
        actions = _build_api_actions(cfg, items, tmdb=tmdb, language="en")
        assert len(actions) == 1
        assert actions[0].action == "sync"
        assert actions[0].add_ids == ["m2", "m3", "m1"]

    def test_highest_rated_applies_min_rating_filter(self):
        cfg = _preset_cfg(["highest_rated_movies"])
        items = [_plan_item(1, "m1"), _plan_item(2, "m2")]
        tmdb = _tmdb_with({
            1: {"vote_average": 8.0}, 2: {"vote_average": 5.0},  # below 7.5 -- excluded
        })
        actions = _build_api_actions(cfg, items, tmdb=tmdb, language="en")
        assert actions[0].add_ids == ["m1"]


# ---------------------------------------------------------------------------
# _build_api_actions: Adaptive Seasonal Collections
# ---------------------------------------------------------------------------

class TestAdaptiveSeasonalApiActions:
    def test_inactive_toggle_produces_no_actions(self):
        cfg = _preset_cfg([])
        items = [_plan_item(1, "m1")]
        tmdb = _tmdb_with({1: {"genres": [{"name": "Horror"}]}})
        actions = _build_api_actions(cfg, items, tmdb=tmdb, language="en")
        assert actions == []

    @patch("finmeta.engine.plan_builder.is_holiday_active_now")
    def test_one_action_per_calendar_holiday(self, mock_active):
        mock_active.return_value = False
        cfg = _preset_cfg(["adaptive_seasonal"])
        actions = _build_api_actions(cfg, [], tmdb=_tmdb_with({}), language="en")
        assert len(actions) == len(SEASONAL_CALENDAR)
        assert {a.name for a in actions} == {h.label for h in SEASONAL_CALENDAR}
        assert all(a.add_ids == [] for a in actions)

    @patch("finmeta.engine.plan_builder.is_holiday_active_now")
    def test_genre_match_halloween(self, mock_active):
        mock_active.side_effect = lambda h: h.id == "halloween"
        cfg = _preset_cfg(["adaptive_seasonal"])
        items = [_plan_item(1, "m1"), _plan_item(2, "m2")]
        tmdb = _tmdb_with({
            1: {"genres": [{"name": "Horror"}]}, 2: {"genres": [{"name": "Comedy"}]},
        })
        actions = _build_api_actions(cfg, items, tmdb=tmdb, language="en")
        halloween = next(a for a in actions if a.name == "Halloween")
        assert halloween.add_ids == ["m1"]
        assert all(a.add_ids == [] for a in actions if a.name != "Halloween")

    @patch("finmeta.engine.plan_builder.is_holiday_active_now")
    def test_keyword_match_thanksgiving(self, mock_active):
        mock_active.side_effect = lambda h: h.id == "thanksgiving"
        cfg = _preset_cfg(["adaptive_seasonal"])
        items = [_plan_item(1, "m1"), _plan_item(2, "m2")]
        tmdb = _tmdb_with({1: {}, 2: {}}, keywords_by_id={
            1: [{"name": "Thanksgiving"}], 2: [{"name": "christmas"}],
        })
        actions = _build_api_actions(cfg, items, tmdb=tmdb, language="en")
        thx = next(a for a in actions if a.name == "Thanksgiving")
        assert thx.add_ids == ["m1"]

    @patch("finmeta.engine.plan_builder.is_holiday_active_now")
    def test_keyword_match_is_case_insensitive_and_cached_per_item(self, mock_active):
        mock_active.side_effect = lambda h: h.id in ("mothers_day", "fathers_day")
        cfg = _preset_cfg(["adaptive_seasonal"])
        items = [_plan_item(1, "m1")]
        tmdb = _tmdb_with({1: {}}, keywords_by_id={1: [{"name": "Motherhood"}]})
        actions = _build_api_actions(cfg, items, tmdb=tmdb, language="en")
        assert next(a for a in actions if a.name == "Mother's Day").add_ids == ["m1"]
        assert next(a for a in actions if a.name == "Father's Day").add_ids == []
        # keywords fetched once per item even though two holidays check it this run
        assert tmdb.movie_keywords.call_count == 1

    @patch("finmeta.engine.plan_builder.is_holiday_active_now")
    def test_mdblist_match_christmas(self, mock_active):
        mock_active.side_effect = lambda h: h.id == "christmas"
        cfg = _preset_cfg(["adaptive_seasonal"])
        items = [_plan_item(1, "m1"), _plan_item(2, "m2")]
        tmdb = _tmdb_with({1: {}, 2: {}})
        fetched = [MagicMock(tmdb_id=1)]
        actions = _build_api_actions(cfg, items, tmdb=tmdb, language="en",
                                     seasonal_mdblist_fetched=fetched)
        xmas = next(a for a in actions if a.name == "Christmas")
        assert xmas.add_ids == ["m1"]

    @patch("finmeta.engine.plan_builder.is_holiday_active_now")
    def test_tvshow_items_never_match_any_holiday(self, mock_active):
        mock_active.return_value = True
        cfg = _preset_cfg(["adaptive_seasonal"])
        items = [_plan_item(1, "s1", media_type="tvshow")]
        tmdb = _tmdb_with({1: {"genres": [{"name": "Horror"}]}},
                          keywords_by_id={1: [{"name": "thanksgiving"}]})
        actions = _build_api_actions(cfg, items, tmdb=tmdb, language="en")
        assert all(a.add_ids == [] for a in actions)


# ---------------------------------------------------------------------------
# fetch_seasonal_mdblist_items
# ---------------------------------------------------------------------------

class TestFetchSeasonalMdblistItems:
    def test_preset_not_active_short_circuits(self):
        cfg = _preset_cfg([])
        assert fetch_seasonal_mdblist_items(cfg, secrets=MagicMock()) == []

    @patch("finmeta.engine.collection_presets.is_holiday_active_now", return_value=False)
    def test_outside_christmas_window_short_circuits(self, _mock):
        cfg = _preset_cfg(["adaptive_seasonal"])
        assert fetch_seasonal_mdblist_items(cfg, secrets=MagicMock()) == []

    @patch("finmeta.engine.collection_presets.is_holiday_active_now", return_value=True)
    def test_no_secrets_short_circuits(self, _mock):
        cfg = _preset_cfg(["adaptive_seasonal"])
        assert fetch_seasonal_mdblist_items(cfg, secrets=None) == []

    @patch("finmeta.engine.collection_presets.is_holiday_active_now", return_value=True)
    def test_no_saved_key_short_circuits(self, _mock):
        cfg = _preset_cfg(["adaptive_seasonal"])
        secrets = MagicMock()
        secrets.get.return_value = None
        assert fetch_seasonal_mdblist_items(cfg, secrets=secrets) == []

    @patch("finmeta.sources.mdblist_lists.fetch_list_items")
    @patch("finmeta.engine.collection_presets.is_holiday_active_now", return_value=True)
    def test_fetches_with_a_saved_key(self, _mock_active, mock_fetch):
        mock_fetch.return_value = ["item"]
        cfg = Config(dry_run=True, server=ServerConfig(url="http://x:8096"),
                     collection_presets=["adaptive_seasonal"])
        cfg.sources.mdblist.enabled = True
        secrets = MagicMock()
        secrets.get.return_value = "a-key"
        result = fetch_seasonal_mdblist_items(cfg, secrets=secrets)
        assert result == ["item"]
        mock_fetch.assert_called_once()


# ---------------------------------------------------------------------------
# executor._apply_api_action: "sync" branch (unchanged this session, kept to
# catch regressions from the plan_builder refactor above)
# ---------------------------------------------------------------------------

class TestSyncApiAction:
    def test_creates_when_no_existing_collection(self):
        jf = MagicMock()
        jf.get_collection_by_name.return_value = None
        action = ApiAction(action="sync", kind="collection", name="Halloween", add_ids=["m1"])
        calls = _apply_api_action(action, jf)
        jf.create_collection.assert_called_once_with("Halloween", ["m1"])
        assert calls == 1

    def test_deletes_when_add_ids_empty_and_collection_exists(self):
        jf = MagicMock()
        existing = MagicMock(id="coll1")
        jf.get_collection_by_name.return_value = existing
        action = ApiAction(action="sync", kind="collection", name="Halloween", add_ids=[])
        calls = _apply_api_action(action, jf)
        jf.delete_collection.assert_called_once_with("coll1")
        jf.create_collection.assert_not_called()
        jf.add_to_collection.assert_not_called()
        assert calls == 1

    def test_noop_when_add_ids_empty_and_no_collection(self):
        jf = MagicMock()
        jf.get_collection_by_name.return_value = None
        action = ApiAction(action="sync", kind="collection", name="Halloween", add_ids=[])
        calls = _apply_api_action(action, jf)
        jf.create_collection.assert_not_called()
        jf.delete_collection.assert_not_called()
        assert calls == 0

    def test_reconciles_membership_add_and_remove(self):
        jf = MagicMock()
        existing = MagicMock(id="coll1")
        jf.get_collection_by_name.return_value = existing
        jf.get_items.return_value = [MagicMock(id="a"), MagicMock(id="b")]
        action = ApiAction(action="sync", kind="collection", name="Halloween",
                          add_ids=["b", "c"])
        calls = _apply_api_action(action, jf)
        jf.remove_from_collection.assert_called_once_with("coll1", ["a"])
        jf.add_to_collection.assert_called_once_with("coll1", ["c"])
        assert calls == 2

    def test_no_api_calls_when_membership_already_matches(self):
        jf = MagicMock()
        existing = MagicMock(id="coll1")
        jf.get_collection_by_name.return_value = existing
        jf.get_items.return_value = [MagicMock(id="a")]
        action = ApiAction(action="sync", kind="collection", name="Halloween", add_ids=["a"])
        calls = _apply_api_action(action, jf)
        jf.remove_from_collection.assert_not_called()
        jf.add_to_collection.assert_not_called()
        assert calls == 0


# ---------------------------------------------------------------------------
# Regression: build_plan's all-libraries ("lib:*") scope must pass
# library_name=None into _build_api_actions so preset/seasonal collections
# actually run on a real full plan/apply. Bug: a fallback meant only to give
# per-item code a non-None default lib_cfg was also overwriting lib_name with
# the first configured library's name, which made _build_api_actions think
# every full run was a scoped single-library call and skip the whole preset-
# collection block -- Top 50 Popular, Highest Rated, Adaptive Seasonal never
# got created/synced, regardless of which items would have qualified.
# ---------------------------------------------------------------------------

class _FakeJfFullScope:
    def __init__(self, libraries, items_by_parent):
        self._libraries = libraries
        self._items_by_parent = items_by_parent

    def get_library(self, name):
        return self._libraries.get(name)

    def get_items(self, parent_id=None, recursive=True):
        return list(self._items_by_parent.get(parent_id, []))

    def get_items_media_streams_raw(self, item_ids, batch_size=50):
        return {}

    def close(self):
        pass


def test_build_plan_full_scope_runs_chart_presets():
    cfg = Config(
        dry_run=True, server=ServerConfig(url="http://x:8096"),
        collection_presets=["top_popular_movies"],
        libraries={"Movies": MovieLibrary(
            type="movies", enabled=True,
            metadata=Metadata(enabled=False), artwork=MovieArtwork(),
        )},
    )
    jf_libs = {"Movies": Library(name="Movies", item_id="lib-movies", collection_type="movies")}
    item = Item(id="m1", name="Movie m1 (2020)", type="Movie",
                path="/media/m1/movie.mkv", provider_ids={"Tmdb": "603"},
                parent_id="lib-movies")
    jf = _FakeJfFullScope(jf_libs, {"lib-movies": [item]})
    tmdb = _tmdb_with({603: {"popularity": 42.0}})

    plan = build_plan(cfg, jf, tmdb, {}, library=None, item_id=None)

    sync_actions = [a for a in plan.api_actions
                    if a.kind == "collection" and a.name == "Top 50 Popular Movies"]
    assert len(sync_actions) == 1
    assert sync_actions[0].add_ids == ["m1"]
