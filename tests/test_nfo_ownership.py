"""NFO ownership + series lock repair (2026-11-21).

Finmeta overwrote Sonarr's tvshow.nfo every night with one carrying
``<lockdata>true``; Jellyfin locked the series and every episode added
afterwards, so new episodes showed only the series name and a file date.

- Finmeta writes an NFO only when it is missing or Finmeta's own.
- tvshow.nfo no longer carries lockdata (movie.nfo still does).
- A Health finding + ``unlock_refresh`` fix repairs series already locked.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import respx

from finmeta.engine.executor import apply_plan
from finmeta.engine.nfo import is_finmeta_nfo, movie_nfo, nfo_owned_elsewhere, tvshow_nfo
from finmeta.engine.plan import FileWrite, ItemPlan, WritePlan
from finmeta.engine.plan_builder import _should_write_nfo
from finmeta.engine.runs import finmeta_ever_wrote
from finmeta.engine.scanner import check_locked_series
from finmeta.jellyfin_client import Item, JellyfinClient

SONARR_TVSHOW = (
    '<?xml version="1.0" encoding="utf-8" standalone="yes"?>\n<tvshow>\n'
    "  <title>Example Show</title>\n"
    '  <uniqueid type="tvdb" default="true">10001</uniqueid>\n</tvshow>\n'
)
# What Finmeta wrote before this change -- must still count as its own.
LEGACY_FINMETA_TVSHOW = (
    '<?xml version="1.0" encoding="utf-8"?>\n<tvshow>\n'
    '  <uniqueid type="tmdb" default="true">20002</uniqueid>\n'
    "  <lockdata>true</lockdata>\n  <title>Example Show</title>\n</tvshow>\n"
)
TV = {"name": "Example Show", "first_air_date": "2020-01-01"}


def _lib(enabled=True):
    return SimpleNamespace(metadata=SimpleNamespace(enabled=enabled))


# -- ownership ---------------------------------------------------------------

def test_generated_nfos_are_recognised_as_own(tmp_path):
    for name, data in (("tvshow.nfo", tvshow_nfo(TV, 1)),
                       ("movie.nfo", movie_nfo({"title": "X"}, 2))):
        p = tmp_path / name
        p.write_bytes(data)
        assert is_finmeta_nfo(p)
        assert not nfo_owned_elsewhere(p)


def test_sonarr_nfo_is_owned_elsewhere_legacy_finmeta_is_not(tmp_path):
    sonarr = tmp_path / "a.nfo"
    sonarr.write_text(SONARR_TVSHOW)
    legacy = tmp_path / "b.nfo"
    legacy.write_text(LEGACY_FINMETA_TVSHOW)
    assert nfo_owned_elsewhere(sonarr)
    assert not nfo_owned_elsewhere(legacy)
    assert not nfo_owned_elsewhere(tmp_path / "missing.nfo")


def test_should_write_nfo_only_when_missing_or_own(tmp_path):
    p = tmp_path / "tvshow.nfo"
    assert _should_write_nfo(_lib(), p, "S")            # missing
    p.write_text(LEGACY_FINMETA_TVSHOW)
    assert _should_write_nfo(_lib(), p, "S")            # Finmeta's own
    p.write_text(SONARR_TVSHOW)
    assert not _should_write_nfo(_lib(), p, "S")        # Sonarr's
    assert not _should_write_nfo(_lib(enabled=False), tmp_path / "x.nfo", "S")


def test_tvshow_nfo_has_no_lockdata_movie_nfo_keeps_it():
    assert b"lockdata" not in tvshow_nfo(TV, 1)
    assert b"<lockdata>true</lockdata>" in movie_nfo({"title": "X"}, 2)


def test_apply_skips_nfo_another_tool_wrote_after_the_plan(tmp_path):
    media = tmp_path / "media"
    media.mkdir()
    target = media / "tvshow.nfo"
    target.write_text(SONARR_TVSHOW)
    cfg = SimpleNamespace(dry_run=False, path_mappings=[], libraries={})
    plan = WritePlan(items=[ItemPlan(
        item_id="s1", item_name="S", media_type="tvshow", tmdb_id=1,
        files=[FileWrite(target_path=str(target), kind="metadata",
                         source_ref="generated", content_sha256="0" * 64)])])
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    tmdb = MagicMock()
    res = apply_plan(plan, "r1", cfg, MagicMock(), tmdb,
                     config_dir=cfg_dir, db_path=tmp_path / "jobs.db", poll_timeout=0.1)
    assert res.files_written == 0 and res.errors == []
    assert target.read_text() == SONARR_TVSHOW
    tmdb.tv_details.assert_not_called()


# -- lock repair -------------------------------------------------------------

def _series(locked=True):
    return Item(id="s1", name="Example Show", type="Series", lock_data=locked)


def test_locked_series_with_finmeta_nfo_is_a_finding(tmp_path):
    (tmp_path / "tvshow.nfo").write_text(LEGACY_FINMETA_TVSHOW)
    [f] = check_locked_series([(_series(), tmp_path)], library="Anime")
    assert (f.check_code, f.fix_kind, f.item_id) == (
        "metadata.locked_by_finmeta", "unlock_refresh", "s1")
    assert check_locked_series([(_series(locked=False), tmp_path)], library="Anime") == []


def test_users_own_lock_without_finmeta_trace_is_left_alone(tmp_path):
    (tmp_path / "tvshow.nfo").write_text(SONARR_TVSHOW)
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    assert check_locked_series([(_series(), tmp_path)], library="Anime",
                               config_dir=cfg_dir) == []


def test_trashed_copy_counts_as_finmeta_trace(tmp_path):
    # Sonarr has since rewritten tvshow.nfo, but Finmeta's trash still holds
    # the copy it replaced -- Finmeta did lock this series.
    show = tmp_path / "media" / "Show"
    show.mkdir(parents=True)
    nfo = show / "tvshow.nfo"
    nfo.write_text(SONARR_TVSHOW)
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    assert not finmeta_ever_wrote(nfo, config_dir=cfg_dir)
    trashed = cfg_dir / "trash" / "run-1-abc" / "media" / "Show" / "tvshow.nfo"
    trashed.parent.mkdir(parents=True)
    trashed.write_text(SONARR_TVSHOW)
    assert finmeta_ever_wrote(nfo, config_dir=cfg_dir)
    assert len(check_locked_series([(_series(), show)], library="Anime",
                                   config_dir=cfg_dir)) == 1



BASE = "http://jf"


@respx.mock
def test_client_lists_children_with_lock_state_and_unlocks_with_full_dto():
    client = JellyfinClient(BASE, token="t", user_id="u1")
    respx.get(f"{BASE}/Items").mock(return_value=httpx.Response(200, json={
        "Items": [{"Id": "e1", "Type": "Episode", "LockData": False},
                  {"Id": "e2", "Type": "Episode", "LockData": True}],
        "TotalRecordCount": 2}))
    dto = {"Id": "e2", "Name": "Example Show", "Overview": "x", "LockData": True}
    respx.get(f"{BASE}/Items/e2").mock(return_value=httpx.Response(200, json=dto))
    post = respx.post(f"{BASE}/Items/e2").mock(return_value=httpx.Response(204))
    respx.get(f"{BASE}/Items/e1").mock(return_value=httpx.Response(
        200, json={"Id": "e1", "LockData": False}))
    post_e1 = respx.post(f"{BASE}/Items/e1").mock(return_value=httpx.Response(204))

    assert [(c.id, c.lock_data) for c in client.series_children("s1")] == [
        ("e1", False), ("e2", True)]
    client.unlock_item("e2")
    client.unlock_item("e1")            # not locked: no write at all

    # the full DTO goes back, only the lock changed
    assert json.loads(post.calls.last.request.content) == {**dto, "LockData": False}
    assert not post_e1.called


# -- the repair run ------------------------------------------------------------

class FakeJf:
    def __init__(self, series, children, fail=()):
        self.series, self.children, self.fail = series, children, set(fail)
        self.unlocked, self.refreshed = [], []

    def get_item(self, item_id):
        if item_id in self.fail:
            raise RuntimeError("boom")
        return self.series[item_id]

    def series_children(self, series_id):
        return list(self.children.get(series_id, []))

    def unlock_item(self, item_id):
        self.unlocked.append(item_id)

    def refresh_item(self, item_id, **kw):
        self.refreshed.append((item_id, kw))


def _setup(tmp_path, nfo_text, *, dry_run=False):
    from finmeta.config.models import PathMapping
    from finmeta.jellyfin_client.mappings import PathMapper
    media = tmp_path / "media"
    show = media / "Example Show (2020)"
    show.mkdir(parents=True)
    (show / "tvshow.nfo").write_text(nfo_text)
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    cfg = SimpleNamespace(dry_run=dry_run,
                          path_mappings=[PathMapping(jellyfin="/data", local=str(media))])
    mapper = PathMapper.from_config([{"jellyfin": "/data", "local": str(media)}])
    series = Item(id="s1", name="Example Show", type="Series",
                  path="/data/Example Show (2020)")
    kids = [Item(id="e1", name="Real Title", type="Episode"),              # fine
            Item(id="e2", name="Pilot", type="Episode", lock_data=True),
            Item(id="e3", name="Example Show", type="Episode", lock_data=True),
            Item(id="e4", name="Example Show", type="Episode")]            # frozen
    jf = FakeJf({"s1": series}, {"s1": kids})
    return jf, cfg, mapper, cfg_dir, show / "tvshow.nfo"


def _run(jf, ids, cfg, mapper, cfg_dir, sleeps=None):
    from finmeta.engine.lock_repair import repair_locked_series
    logs = []
    stats = repair_locked_series(
        jf, ids, mapper=mapper, cfg=cfg, config_dir=cfg_dir, run_id="run-1-abc",
        log=lambda lvl, msg: logs.append((lvl, msg)),
        sleep=(sleeps.append if sleeps is not None else (lambda s: None)))
    return stats, logs


def test_repair_strips_own_nfo_lock_first_then_unlocks_and_refreshes_children(tmp_path):
    from finmeta.engine.runs import undo
    jf, cfg, mapper, cfg_dir, nfo = _setup(tmp_path, LEGACY_FINMETA_TVSHOW)
    sleeps = []
    stats, _ = _run(jf, ["s1"], cfg, mapper, cfg_dir, sleeps)

    assert b"lockdata" not in nfo.read_bytes()
    assert is_finmeta_nfo(nfo)                      # still ours afterwards
    assert jf.unlocked == ["s1", "e2", "e3"]
    # locked + frozen (unlocked but still named like the series) children are
    # re-read; the one with its real title and the series itself are not
    assert [i for i, _ in jf.refreshed] == ["e2", "e3", "e4"]
    assert all(kw["replace_all_metadata"] and not kw["replace_all_images"]
               for _, kw in jf.refreshed)
    assert sleeps == [1.0, 1.0]                      # paced between refreshes
    assert stats == {"series": 1, "items_unlocked": 3, "nfo_fixed": 1,
                     "skipped": 0, "errors": 0}
    # the NFO edit is undoable like any apply write
    undo("run-1-abc", config_dir=cfg_dir)
    assert nfo.read_text() == LEGACY_FINMETA_TVSHOW


def test_repair_under_dry_run_skips_series_whose_nfo_would_relock(tmp_path):
    jf, cfg, mapper, cfg_dir, nfo = _setup(tmp_path, LEGACY_FINMETA_TVSHOW, dry_run=True)
    stats, logs = _run(jf, ["s1"], cfg, mapper, cfg_dir)
    assert nfo.read_text() == LEGACY_FINMETA_TVSHOW
    assert jf.unlocked == [] and stats["skipped"] == 1
    assert logs[0][0] == "warn"


def test_repair_leaves_sonarr_nfo_alone_but_still_unlocks(tmp_path):
    jf, cfg, mapper, cfg_dir, nfo = _setup(tmp_path, SONARR_TVSHOW, dry_run=True)
    stats, _ = _run(jf, ["s1"], cfg, mapper, cfg_dir)
    assert nfo.read_text() == SONARR_TVSHOW
    assert jf.unlocked == ["s1", "e2", "e3"] and stats["nfo_fixed"] == 0


def test_one_failing_series_does_not_stop_the_rest(tmp_path):
    jf, cfg, mapper, cfg_dir, _ = _setup(tmp_path, SONARR_TVSHOW)
    jf.fail = {"bad"}
    sleeps = []
    stats, _ = _run(jf, ["bad", "s1"], cfg, mapper, cfg_dir, sleeps)
    assert stats["errors"] == 1 and stats["series"] == 1
    assert 2.0 in sleeps                             # paced between series
