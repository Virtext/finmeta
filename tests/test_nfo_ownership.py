"""NFO ownership + series lock repair (2026-11-21, Devils' Crest).

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
    "  <title>Devils' Crest</title>\n"
    '  <uniqueid type="tvdb" default="true">471128</uniqueid>\n</tvshow>\n'
)
# What Finmeta wrote before this change -- must still count as its own.
LEGACY_FINMETA_TVSHOW = (
    '<?xml version="1.0" encoding="utf-8"?>\n<tvshow>\n'
    '  <uniqueid type="tmdb" default="true">328010</uniqueid>\n'
    "  <lockdata>true</lockdata>\n  <title>Devils' Crest</title>\n</tvshow>\n"
)
TV = {"name": "Devils' Crest", "first_air_date": "2026-11-06"}


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
    return Item(id="s1", name="Devils' Crest", type="Series", lock_data=locked)


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
def test_unlock_series_unlocks_locked_children_and_refreshes():
    client = JellyfinClient(BASE, token="t", user_id="u1")
    respx.get(f"{BASE}/Items").mock(return_value=httpx.Response(200, json={
        "Items": [{"Id": "e1", "Type": "Episode", "LockData": False},
                  {"Id": "e2", "Type": "Episode", "LockData": True}],
        "TotalRecordCount": 2}))
    dtos = {"s1": {"Id": "s1", "Name": "Devils' Crest", "LockData": True},
            "e2": {"Id": "e2", "Name": "Devils' Crest", "LockData": True}}
    for item_id, dto in dtos.items():
        respx.get(f"{BASE}/Items/{item_id}").mock(return_value=httpx.Response(200, json=dto))
    posts = {i: respx.post(f"{BASE}/Items/{i}").mock(return_value=httpx.Response(204))
             for i in dtos}
    refresh = respx.post(f"{BASE}/Items/s1/Refresh").mock(return_value=httpx.Response(204))

    assert client.unlock_series("s1") == 2

    for item_id, route in posts.items():
        body = json.loads(route.calls.last.request.content)
        # full DTO sent back, only the lock changed
        assert body == {**dtos[item_id], "LockData": False}
    q = refresh.calls.last.request.url.params
    assert q["replaceAllMetadata"] == "true" and q["replaceAllImages"] == "false"

