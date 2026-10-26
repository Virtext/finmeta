"""Common Candidate model, local asset source, fanart.tv, ranking, factory (SPEC §4)."""
from __future__ import annotations

import logging
from pathlib import Path

import pytest
import respx
import httpx

from finmeta.config.models import Config
from finmeta.sources import (
    Candidate, KINDS, rank_candidates, pick_best,
    LocalAssetSource, FanartSource, build_sources, TmdbClient,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_ASSETS = REPO_ROOT / "fixture" / "assets"


# ---------------------------------------------------------------------------
# Candidate model
# ---------------------------------------------------------------------------

def test_candidate_fields_and_size():
    c = Candidate(kind="poster", source="tmdb", ref="https://x/a.jpg",
                  lang="en", width=500, height=750, score=3.5)
    assert c.size == (500, 750)
    assert "poster" in str(c) and "tmdb" in str(c)


def test_candidate_size_none_when_missing_dims():
    c = Candidate(kind="theme_song", source="local", ref="/a/theme.mp3")
    assert c.size is None
    assert c.score == 0.0


def test_candidate_kind_is_constrained():
    import pydantic
    with pytest.raises(pydantic.ValidationError):
        Candidate(kind="nope", source="tmdb", ref="x")


def test_kinds_matches_spec():
    assert set(KINDS) == {
        "poster", "backdrop", "logo", "landscape",
        "season_poster", "episode_thumb", "theme_song", "metadata",
    }


# ---------------------------------------------------------------------------
# local asset source (fixture layout)
# ---------------------------------------------------------------------------

def test_local_asset_poster():
    s = LocalAssetSource(FIXTURE_ASSETS)
    cands = s.candidates_for(155, ["poster", "backdrop"])
    assert len(cands) == 1
    assert cands[0].kind == "poster"
    assert cands[0].source == "local"
    assert cands[0].ref.endswith("155/poster.jpg")
    assert cands[0].lang is None


def test_local_asset_theme_song():
    s = LocalAssetSource(FIXTURE_ASSETS)
    kinds = ["poster", "backdrop", "logo", "landscape", "theme_song"]
    cands = s.candidates_for(1396, kinds)
    assert [c.kind for c in cands] == ["theme_song"]
    assert cands[0].ref.endswith("1396/theme.mp3")


def test_local_asset_missing_item():
    s = LocalAssetSource(FIXTURE_ASSETS)
    assert s.candidates_for(999999, ["poster"]) == []


def test_local_asset_season_poster(tmp_path):
    d = tmp_path / "42"
    d.mkdir()
    (d / "season01-poster.jpg").write_bytes(b"x")
    (d / "season02-poster.jpg").write_bytes(b"x")
    s = LocalAssetSource(tmp_path)
    cands = s.candidates_for(42, ["season_poster"])
    assert len(cands) == 2
    assert all(c.kind == "season_poster" for c in cands)


# ---------------------------------------------------------------------------
# fanart.tv (skip when no key; recorded when key present)
# ---------------------------------------------------------------------------

def test_fanart_skip_when_no_key(caplog):
    with caplog.at_level(logging.INFO, logger="finmeta.sources.fanart"):
        f = FanartSource(api_key=None)
        assert f.enabled is False
        assert f.candidates_for("movie", "tt0000001", ["poster", "backdrop"]) == []
    assert any("skipped" in rec.message for rec in caplog.records)
    assert any("FANART_KEY" in rec.message for rec in caplog.records)


@respx.mock
def test_fanart_recorded_with_key():
    # fanart.tv's real URL is "/movies/<id>" (plural) -- not "/movie/<id>".
    f = "https://webservice.fanart.tv/v3"
    respx.get(f + "/movies/tt0468569").mock(
        return_value=httpx.Response(200, json={
            "movieposter": [{"url": "https://fanart.tv/p.jpg", "lang": "en"}],
            "hdmovie": [{"url": "https://fanart.tv/b.jpg", "lang": None}],
            "clearlogo": [{"url": "https://fanart.tv/l.jpg", "lang": "en"}],
        }))
    with FanartSource(api_key="FANART") as s:
        cands = s.candidates_for("movie", "tt0468569", ["poster", "backdrop", "logo"])
    assert [c.kind for c in cands] == ["poster", "backdrop", "logo"]
    assert all(c.source == "fanart" for c in cands)
    assert {c.lang for c in cands} == {"en", None}


@respx.mock
def test_fanart_tvshow_media_type(caplog):
    """media_type="tvshow" (Finmeta's internal string, see plan_builder.py)
    must hit fanart.tv's "/tv/<id>" and use the tv _KIND_KEY entries --
    regression for the mismatch found 2026-10-23 (silently always returned
    [] for every show because "tvshow" matched neither)."""
    f = "https://webservice.fanart.tv/v3"
    respx.get(f + "/tv/121361").mock(
        return_value=httpx.Response(200, json={
            "tvposter": [{"url": "https://fanart.tv/p.jpg", "lang": "en"}],
        }))
    with FanartSource(api_key="FANART") as s:
        cands = s.candidates_for("tvshow", 121361, ["poster"])
    assert [c.kind for c in cands] == ["poster"]


@respx.mock
def test_fanart_rate_limiter_is_actually_called(tmp_path):
    """FanartSource accepted and stored a min_interval constructor arg but
    never enforced it anywhere -- decorative only, every cache-miss request
    went straight to the network. Regression: the limiter must actually be
    waited on before each real fetch, and must NOT fire again on a cache
    hit (no network call happens then). A tmp_path-scoped cache_path is
    required here, not the default "config/cache.db" -- that path is
    relative to the test runner's cwd and persists on disk between runs,
    so a cache hit from an earlier run would silently skip the network
    fetch (and the limiter.wait() call this test exists to check)."""
    f = "https://webservice.fanart.tv/v3"
    respx.get(f + "/movies/603").mock(
        return_value=httpx.Response(200, json={"movieposter": []}))
    with FanartSource(api_key="FANART", cache_path=str(tmp_path / "cache.db")) as s:
        calls = []
        s.limiter.wait = lambda: calls.append(1)  # type: ignore[method-assign]
        s.candidates_for("movie", 603, ["poster"])
        assert calls == [1]
        s.candidates_for("movie", 603, ["poster"])  # cache hit, no new fetch
        assert calls == [1]


# ---------------------------------------------------------------------------
# ranking
# ---------------------------------------------------------------------------

def test_rank_language_beats_score():
    cands = [
        Candidate(kind="poster", source="tmdb", ref="a", lang="fr", width=500, height=750, score=9.0),
        Candidate(kind="poster", source="tmdb", ref="b", lang=None, width=500, height=750, score=8.0),
        Candidate(kind="poster", source="tmdb", ref="c", lang="en", width=500, height=750, score=1.0),
    ]
    ranked = rank_candidates(cands, ["en", None])
    assert [c.ref for c in ranked] == ["c", "b", "a"]


def test_rank_resolution_then_score():
    cands = [
        Candidate(kind="poster", source="tmdb", ref="small", lang="en", width=300, height=450, score=9.0),
        Candidate(kind="poster", source="tmdb", ref="big-low", lang="en", width=500, height=750, score=1.0),
        Candidate(kind="poster", source="tmdb", ref="big-high", lang="en", width=500, height=750, score=5.0),
    ]
    ranked = rank_candidates(cands, ["en"])
    assert [c.ref for c in ranked] == ["big-high", "big-low", "small"]


def test_rank_textless_last_when_prefers_language():
    cands = [
        Candidate(kind="poster", source="tmdb", ref="textless", lang=None, score=9.0),
        Candidate(kind="poster", source="tmdb", ref="en", lang="en", score=1.0),
    ]
    assert pick_best(cands, ["en"]) is not None
    assert pick_best(cands, ["en"]).ref == "en"


# ---------------------------------------------------------------------------
# factory
# ---------------------------------------------------------------------------

def _cfg(**sources_over):
    d = {"server": {"url": "http://x"},
         "sources": {"tmdb": {"enabled": True, "api_key_env": "TMDB_KEY"},
                     "fanart": {"enabled": True, "api_key_env": "FANART_KEY"},
                     "local": {"enabled": True, "path": "/assets"},
                     "precedence": ["local", "fanart", "tmdb"]}}
    return Config(**d)


def test_build_sources_tmdb_requires_key(monkeypatch):
    monkeypatch.delenv("TMDB_KEY", raising=False)
    with pytest.raises(Exception) as ei:
        build_sources(_cfg(), env={})
    assert "TMDB_KEY" in str(ei.value)


def test_build_sources_includes_all_when_keys_present(tmp_path, monkeypatch):
    monkeypatch.setenv("TMDB_KEY", "K")
    monkeypatch.setenv("FANART_KEY", "FK")
    sources = build_sources(_cfg(), env={"TMDB_KEY": "K", "FANART_KEY": "FK"},
                            cache_path=str(tmp_path / "cache.db"))
    assert set(sources) == {"tmdb", "fanart", "local"}
    assert isinstance(sources["tmdb"], TmdbClient)
    assert sources["fanart"].enabled is True
    sources["tmdb"].close()
    sources["fanart"].close()


def _mdblist_cfg(enabled: bool):
    cfg = _cfg()
    return cfg.model_copy(update={"sources": cfg.sources.model_copy(
        update={"mdblist": cfg.sources.mdblist.model_copy(update={"enabled": enabled})})})


def test_build_sources_mdblist_enabled_with_key(tmp_path, monkeypatch):
    from finmeta.sources.mdblist import MdblistClient
    cfg = _mdblist_cfg(True)
    sources = build_sources(cfg, env={"TMDB_KEY": "K", "MDBLIST_KEY": "MK"},
                            cache_path=str(tmp_path / "cache.db"))
    assert isinstance(sources.get("mdblist"), MdblistClient)
    for src in sources.values():
        if hasattr(src, "close"):
            src.close()


def test_build_sources_mdblist_enabled_no_key_absent(tmp_path, monkeypatch):
    cfg = _mdblist_cfg(True)
    sources = build_sources(cfg, env={"TMDB_KEY": "K"},
                            cache_path=str(tmp_path / "cache.db"))
    assert "mdblist" not in sources
    for src in sources.values():
        if hasattr(src, "close"):
            src.close()


def test_build_sources_mdblist_disabled_absent(tmp_path):
    cfg = _mdblist_cfg(False)
    sources = build_sources(cfg, env={"TMDB_KEY": "K", "MDBLIST_KEY": "MK"},
                            cache_path=str(tmp_path / "cache.db"))
    assert "mdblist" not in sources
    for src in sources.values():
        if hasattr(src, "close"):
            src.close()


def test_build_sources_fanart_disabled_not_included(tmp_path):
    cfg = _cfg()
    # disable fanart + local in the built config
    cfg = cfg.model_copy(update={"sources": cfg.sources.model_copy(
        update={"fanart": cfg.sources.fanart.model_copy(update={"enabled": False}),
                "local": cfg.sources.local.model_copy(update={"enabled": False})})})
    sources = build_sources(cfg, env={"TMDB_KEY": "K"}, cache_path=str(tmp_path / "cache.db"))
    assert set(sources) == {"tmdb"}
    sources["tmdb"].close()
