"""S9d6: scheduled poster rotation for a library's top-rated items.

Three layers tested: ``engine.poster_rotation.top_rated_item_ids`` (pure
ranking by Jellyfin's own CommunityRating), ``api.scheduler.poster_rotation_job``
(the guard conditions + the reuse of the existing single-item webhook queue to
chain N items through the single-run slot), and the manual "Run now" HTTP
route (``POST /api/libraries/{library}/poster-rotation/run``,
routes_runs.py) that calls the same job function on demand.
"""
from __future__ import annotations

from typing import Dict, List, Optional
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from finmeta.api import AppContext, create_app
from finmeta.api.scheduler import poster_rotation_job
from finmeta.config.models import (
    Config, Metadata, MovieArtwork, MovieLibrary, PosterRotation, ServerConfig,
)
from finmeta.engine.plan import WritePlan
from finmeta.engine.poster_rotation import top_rated_item_ids
from finmeta.jellyfin_client.types import Item, Library


class FakeJf:
    def __init__(self, library: Library, items: List[Item]):
        self._library = library
        self._items = items

    def get_library(self, name: str) -> Optional[Library]:
        return self._library if name == self._library.name else None

    def get_items(self, *, parent_id=None, recursive=True, include_item_types=None,
                  fields=None) -> List[Item]:
        return [it for it in self._items if it.type in (include_item_types or [it.type])]

    def iter_items(self, *, parent_id=None, recursive=True, include_item_types=None,
                   fields=None, user_id=None, page_size=200):
        for it in self._items:
            if it.type in (include_item_types or [it.type]):
                yield it


def _movie(item_id: str, rating: Optional[float]) -> Item:
    return Item(id=item_id, name=item_id, type="Movie", community_rating=rating)


class TestTopRatedItemIds:
    def test_ranks_by_community_rating_desc(self):
        jf = FakeJf(
            Library(name="Movies", item_id="lib1", collection_type="movies"),
            [_movie("low", 5.0), _movie("high", 9.0), _movie("mid", 7.0)],
        )
        assert top_rated_item_ids(jf, "Movies", 2, ["Movie"]) == ["high", "mid"]

    def test_unrated_items_never_picked_over_a_rated_one(self):
        jf = FakeJf(
            Library(name="Movies", item_id="lib1", collection_type="movies"),
            [_movie("unrated", None), _movie("rated", 1.0)],
        )
        assert top_rated_item_ids(jf, "Movies", 5, ["Movie"]) == ["rated"]

    def test_unknown_library_returns_empty(self):
        jf = FakeJf(Library(name="Movies", item_id="lib1", collection_type="movies"), [])
        assert top_rated_item_ids(jf, "Nope", 5, ["Movie"]) == []


def _lib(**kw) -> MovieLibrary:
    return MovieLibrary(type="movies", enabled=True, metadata=Metadata(enabled=False),
                        artwork=MovieArtwork(poster=True), **kw)


def _cfg(lib: MovieLibrary, dry_run: bool = False) -> Config:
    return Config(dry_run=dry_run, server=ServerConfig(url="http://x:8096"),
                 libraries={"Movies": lib})


def _context(cfg: Config, item_ids: List[str]):
    ctx = MagicMock()
    ctx.config = cfg
    ctx.make_jellyfin.return_value = FakeJf(
        Library(name="Movies", item_id="lib1", collection_type="movies"),
        [_movie(i, 8.0) for i in item_ids],
    )
    return ctx


class TestPosterRotationJob:
    def test_disabled_library_rotation_does_nothing(self):
        ctx = _context(_cfg(_lib(poster_rotation=PosterRotation(enabled=False))), ["a"])
        result = poster_rotation_job(ctx, "Movies")
        ctx.overrides.increment_reshuffle.assert_not_called()
        assert result == {"ok": False,
                          "reason": "library not found, disabled, or poster rotation is off"}

    def test_dry_run_skips_even_when_enabled(self):
        ctx = _context(_cfg(_lib(poster_rotation=PosterRotation(enabled=True)), dry_run=True), ["a"])
        result = poster_rotation_job(ctx, "Movies")
        ctx.overrides.increment_reshuffle.assert_not_called()
        assert result == {"ok": False, "reason": "dry_run is true"}

    def test_artwork_poster_off_skips(self):
        lib = MovieLibrary(type="movies", enabled=True, metadata=Metadata(enabled=False),
                           artwork=MovieArtwork(poster=False),
                           poster_rotation=PosterRotation(enabled=True))
        ctx = _context(_cfg(lib), ["a"])
        result = poster_rotation_job(ctx, "Movies")
        ctx.overrides.increment_reshuffle.assert_not_called()
        assert result == {"ok": False, "reason": "artwork.poster is off"}

    def test_no_rated_items_returns_ok_true_rotated_zero(self):
        lib = _lib(poster_rotation=PosterRotation(enabled=True))
        ctx = _context(_cfg(lib), [])
        result = poster_rotation_job(ctx, "Movies")
        ctx.overrides.increment_reshuffle.assert_not_called()
        assert result == {"ok": True, "rotated": 0, "reason": "no rated items found"}

    def test_enabling_rotation_forces_random_top_n_with_at_least_2(self):
        """Config-level invariant (_apply_poster_rotation_requirements) --
        rotation would silently do nothing on best_rated or top_n=1."""
        lib = _lib(poster_rotation=PosterRotation(enabled=True))
        assert lib.artwork_selection.preset == "random_top_n"
        assert lib.artwork_selection.top_n >= 2

    def test_reshuffles_every_picked_item_then_submits_first_and_queues_rest(self):
        lib = _lib(poster_rotation=PosterRotation(enabled=True, item_count=3))
        ctx = _context(_cfg(lib), ["a", "b", "c"])
        ctx.submit_run = None  # not used directly; patched module-level below

        from unittest.mock import patch
        with patch("finmeta.api.scheduler.submit_run", return_value=("run-1", None)) as sub:
            result = poster_rotation_job(ctx, "Movies")

        assert ctx.overrides.increment_reshuffle.call_args_list == [
            (("a", "poster"),), (("b", "poster"),), (("c", "poster"),),
        ]
        sub.assert_called_once_with(ctx, "apply", item_id="a")
        assert ctx.queue_webhook_item.call_args_list == [(("b",),), (("c",),)]
        assert result == {"ok": True, "rotated": 3, "run_id": "run-1", "queued": ["b", "c"]}

    def test_run_busy_queues_the_first_item_too(self):
        lib = _lib(poster_rotation=PosterRotation(enabled=True, item_count=1))
        ctx = _context(_cfg(lib), ["a"])

        from unittest.mock import patch
        with patch("finmeta.api.scheduler.submit_run", return_value=(None, "a run is already in progress")):
            result = poster_rotation_job(ctx, "Movies")

        ctx.queue_webhook_item.assert_called_once_with("a")
        assert result == {"ok": True, "rotated": 1, "run_id": None, "queued": ["a"]}


def _rotation_api_context(tmp_path, monkeypatch, jf, *, rotation_enabled: bool):
    """Real AppContext (not a MagicMock) backed by an on-disk config.yml, so
    the route's own ``ctx.config.libraries`` lookup and ``submit_run`` are
    exercised for real -- same pattern as
    tests/test_s9d1_libraries_profiles_batch.py's ``_api_context``."""
    monkeypatch.setenv("JELLYFIN_API_KEY", "fake-key")
    monkeypatch.setenv("FINMETA_AUTH", "off")
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    cfg_file = cfg_dir / "config.yml"
    cfg_file.write_text(f"""\
dry_run: false
server:
  url: http://fake:8096
  api_key_env: JELLYFIN_API_KEY
libraries:
  Movies:
    type: movies
    enabled: true
    artwork:
      poster: true
    poster_rotation:
      enabled: {str(rotation_enabled).lower()}
      item_count: 2
      cron: "0 4 * * 0"
""")

    def _fake_plan(cfg, jf, tmdb, sources, library=None, item_id=None, config_dir=None, **kwargs):
        return WritePlan(items=[])

    ctx = AppContext(
        str(cfg_file), config_dir=str(cfg_dir),
        db_path=str(cfg_dir / "jobs.db"), overrides_path=str(cfg_dir / "overrides.db"),
        plan_builder=_fake_plan,
    )
    ctx.make_jellyfin = lambda cfg=None: jf
    return ctx


def _rotation_fake_jf(items: List[Item]) -> FakeJf:
    return FakeJf(Library(name="Movies", item_id="lib1", collection_type="movies"), items)


class TestPosterRotationRunRoute:
    def test_unknown_library_404(self, tmp_path, monkeypatch):
        ctx = _rotation_api_context(tmp_path, monkeypatch, _rotation_fake_jf([]),
                                    rotation_enabled=True)
        client = TestClient(create_app(ctx))
        r = client.post("/api/libraries/Nope/poster-rotation/run")
        assert r.status_code == 404

    def test_disabled_rotation_returns_200_ok_false(self, tmp_path, monkeypatch):
        ctx = _rotation_api_context(tmp_path, monkeypatch, _rotation_fake_jf([]),
                                    rotation_enabled=False)
        client = TestClient(create_app(ctx))
        r = client.post("/api/libraries/Movies/poster-rotation/run")
        assert r.status_code == 200
        assert r.json()["ok"] is False

    def test_enabled_rotation_submits_a_run(self, tmp_path, monkeypatch):
        import time

        jf = _rotation_fake_jf([_movie("a", 9.0), _movie("b", 7.0)])
        ctx = _rotation_api_context(tmp_path, monkeypatch, jf, rotation_enabled=True)
        with TestClient(create_app(ctx)) as client:
            r = client.post("/api/libraries/Movies/poster-rotation/run")
            assert r.status_code == 200
            body = r.json()
            assert body["ok"] is True
            assert body["rotated"] == 2
            run_id = body["run_id"]
            assert run_id

            # Poll tolerating a 404 -- the worker thread inserts the runs row
            # after submit_run() already returned, same race documented in
            # tests/test_s9d2_profiles_sample_bulk.py's job-polling tests.
            deadline = time.time() + 15
            job = None
            while time.time() < deadline:
                resp = client.get(f"/api/jobs/{run_id}")
                if resp.status_code != 404:
                    job = resp
                    break
                time.sleep(0.2)
        assert job is not None, "poster-rotation run never appeared in the job table"
        assert job.status_code == 200
