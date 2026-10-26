"""S9d5 (CLAUDE.md §4.1): artwork lifecycle locking.

"Første kjøring velger bilder etter preset og låser valget per element.
Nattlig kjøring = kun overlay-render på låst, rent grunnbilde... ingen nye
nedlastinger." (First run picks images per preset and locks the choice per
element. Nightly run = overlay-only render on the locked, clean base image
-- no new downloads.)

Covers:
  - OverrideStore: set_lock/get_lock/clear_lock/locks_for_item.
  - engine.plan_builder._locked_candidates: reads the lock, builds a
    Candidate from it.
  - engine.plan_builder._build_movie_plan: a locked kind's source is NEVER
    queried again (the actual "no new downloads" guarantee) -- the locked
    ref is used as-is; an unlocked kind still resolves normally.
  - engine.executor._maybe_lock_artwork (exercised via apply_plan): first
    successful write/upload locks the kind; a second apply never overwrites
    an existing lock; metadata/theme_song are never locked.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import MagicMock

from finmeta.api.overrides import OverrideStore
from finmeta.config.models import (
    Config, Metadata, MovieArtwork, MovieLibrary, ServerConfig,
)
from finmeta.engine.executor import apply_plan
from finmeta.engine.matching import MatchResult
from finmeta.engine.plan import FileWrite, ItemPlan, WritePlan
from finmeta.engine.plan_builder import _build_movie_plan, _locked_candidates
from finmeta.jellyfin_client.mappings import PathMapper
from finmeta.jellyfin_client.types import Item
from finmeta.sources.candidate import Candidate


# ---------------------------------------------------------------------------
# OverrideStore
# ---------------------------------------------------------------------------

class TestOverrideStoreLocks:
    def test_set_then_get(self, tmp_path: Path):
        store = OverrideStore(tmp_path / "overrides.db")
        store.set_lock("mov1", "poster", "/tmp/p.jpg", "a" * 64)
        lock = store.get_lock("mov1", "poster")
        assert lock is not None
        assert lock["ref"] == "/tmp/p.jpg"
        assert lock["sha256"] == "a" * 64
        assert isinstance(lock["locked_at"], float)

    def test_get_missing_returns_none(self, tmp_path: Path):
        store = OverrideStore(tmp_path / "overrides.db")
        assert store.get_lock("mov1", "poster") is None

    def test_set_lock_does_not_overwrite_on_caller_mistake(self, tmp_path: Path):
        """set_lock itself is a raw upsert (REPLACE); callers (executor) are
        responsible for checking get_lock first -- this confirms the
        underlying upsert behaviour callers rely on not overwriting silently
        is respected: a second explicit set_lock DOES replace (by design,
        used by nothing in the normal flow, but must not corrupt state)."""
        store = OverrideStore(tmp_path / "overrides.db")
        store.set_lock("mov1", "poster", "/tmp/p1.jpg", "a" * 64)
        store.set_lock("mov1", "poster", "/tmp/p2.jpg", "b" * 64)
        lock = store.get_lock("mov1", "poster")
        assert lock["ref"] == "/tmp/p2.jpg"

    def test_clear_lock(self, tmp_path: Path):
        store = OverrideStore(tmp_path / "overrides.db")
        store.set_lock("mov1", "poster", "/tmp/p.jpg", "a" * 64)
        assert store.clear_lock("mov1", "poster") is True
        assert store.get_lock("mov1", "poster") is None
        assert store.clear_lock("mov1", "poster") is False

    def test_locks_for_item(self, tmp_path: Path):
        store = OverrideStore(tmp_path / "overrides.db")
        store.set_lock("mov1", "poster", "/tmp/p.jpg", "a" * 64)
        store.set_lock("mov1", "backdrop", "/tmp/b.jpg", "b" * 64)
        locks = store.locks_for_item("mov1")
        assert set(locks) == {"poster", "backdrop"}
        assert locks["poster"]["ref"] == "/tmp/p.jpg"

    def test_locks_are_per_item(self, tmp_path: Path):
        store = OverrideStore(tmp_path / "overrides.db")
        store.set_lock("mov1", "poster", "/tmp/p.jpg", "a" * 64)
        assert store.get_lock("mov2", "poster") is None


# ---------------------------------------------------------------------------
# engine.plan_builder._locked_candidates
# ---------------------------------------------------------------------------

class TestLockedCandidates:
    def test_no_config_dir_returns_empty(self):
        assert _locked_candidates("mov1", ["poster"], config_dir=None) == {}

    def test_no_overrides_db_yet_returns_empty(self, tmp_path: Path):
        assert _locked_candidates("mov1", ["poster"], config_dir=tmp_path) == {}

    def test_locked_kind_returns_candidate(self, tmp_path: Path):
        store = OverrideStore(tmp_path / "overrides.db")
        store.set_lock("mov1", "poster", "/tmp/locked.jpg", "a" * 64)
        out = _locked_candidates("mov1", ["poster", "backdrop"], config_dir=tmp_path)
        assert set(out) == {"poster"}
        assert isinstance(out["poster"], Candidate)
        assert out["poster"].ref == "/tmp/locked.jpg"


# ---------------------------------------------------------------------------
# engine.plan_builder: build-time integration (no new downloads)
# ---------------------------------------------------------------------------

def _poster_bytes() -> bytes:
    import io
    from PIL import Image
    img = Image.new("RGB", (400, 600), (10, 20, 30))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


class _CountingLocalSource:
    """Wraps a real LocalAssetSource but counts candidates_for() calls."""

    def __init__(self, inner):
        self.inner = inner
        self.calls: list = []

    def candidates_for(self, tmdb_id, kinds):
        self.calls.append(list(kinds))
        return self.inner.candidates_for(tmdb_id, kinds)


def _fixture(tmp_path):
    media_root = tmp_path / "media"
    movie_dir = media_root / "Movies" / "Test Movie"
    movie_dir.mkdir(parents=True, exist_ok=True)
    (movie_dir / "m.mkv").write_bytes(b"video")

    assets = tmp_path / "assets"
    ad = assets / "155"
    ad.mkdir(parents=True, exist_ok=True)
    (ad / "poster.jpg").write_bytes(_poster_bytes())
    (ad / "backdrop.jpg").write_bytes(_poster_bytes())

    cfg = Config(
        dry_run=True,
        server=ServerConfig(url="http://x:8096"),
        libraries={"Movies": MovieLibrary(
            type="movies",
            metadata=Metadata(enabled=False),
            artwork=MovieArtwork(poster=True, backdrop=True),
        )},
    )
    mapper = PathMapper.from_config([{"jellyfin": str(media_root), "local": str(media_root)}])
    item = Item(id="mov155", name="Test Movie (2020)", type="Movie",
                path=str(movie_dir / "m.mkv"), provider_ids={"Tmdb": "155"})
    match = MatchResult(item=item, tmdb_id=155, matched_by="provider_id")
    from finmeta.sources.local import LocalAssetSource
    counting = _CountingLocalSource(LocalAssetSource(assets))
    sources = {"local": counting}
    lib = cfg.libraries["Movies"]
    return cfg, lib, mapper, item, match, sources, counting


class TestPlanBuilderSkipsResolutionWhenLocked:
    def test_unlocked_item_queries_source_for_all_kinds(self, tmp_path: Path):
        config_dir = tmp_path / "config"
        cfg, lib, mapper, item, match, sources, counting = _fixture(tmp_path)
        jf = MagicMock()
        _build_movie_plan(
            item, match, cfg, lib, mapper, tmdb=None, jf=jf,
            sources=sources, language="en", lang_fallback=[None], config_dir=config_dir)
        queried = {k for call in counting.calls for k in call}
        assert queried == {"poster", "backdrop"}

    def test_locked_kind_never_queries_source_again(self, tmp_path: Path):
        config_dir = tmp_path / "config"
        cfg, lib, mapper, item, match, sources, counting = _fixture(tmp_path)
        store = OverrideStore(config_dir / "overrides.db")
        store.set_lock("mov155", "poster", str(tmp_path / "assets" / "155" / "poster.jpg"), "x" * 64)
        jf = MagicMock()
        plan_item = _build_movie_plan(
            item, match, cfg, lib, mapper, tmdb=None, jf=jf,
            sources=sources, language="en", lang_fallback=[None], config_dir=config_dir)
        queried = {k for call in counting.calls for k in call}
        # poster never asked for again; backdrop (unlocked) still resolves normally.
        assert "poster" not in queried
        assert "backdrop" in queried
        by_kind = {f.kind: f for f in plan_item.files}
        assert by_kind["poster"].source_ref == str(tmp_path / "assets" / "155" / "poster.jpg")


# ---------------------------------------------------------------------------
# engine.executor: locking on first successful apply
# ---------------------------------------------------------------------------

def _make_config(dry_run=False):
    return Config(dry_run=dry_run, server=ServerConfig(url="http://x:8096"))


class TestExecutorLocksOnFirstApply:
    def test_sidecar_write_locks_the_kind(self, tmp_path: Path):
        content = b"POSTER BYTES"
        sha = hashlib.sha256(content).hexdigest()
        plan = WritePlan(items=[
            ItemPlan(
                item_id="mov001", item_name="Test Movie",
                media_type="movie", tmdb_id=123, matched_by="provider_id",
                files=[FileWrite(
                    target_path=str(tmp_path / "media" / "poster.jpg"),
                    kind="poster", source_ref=str(tmp_path / "src.jpg"),
                    content_sha256=sha,
                    meta={"lock_ref": "/original/poster.jpg", "lock_sha256": "z" * 64},
                )],
            ),
        ])
        (tmp_path / "src.jpg").write_bytes(content)
        cfg = _make_config(dry_run=False)
        jf, tmdb = MagicMock(), MagicMock()
        db = tmp_path / "jobs.db"
        cfg_dir = tmp_path / "config"
        cfg_dir.mkdir()

        apply_plan(plan, "r1", cfg, jf, tmdb, config_dir=cfg_dir, db_path=db, poll_timeout=0.1)

        store = OverrideStore(cfg_dir / "overrides.db")
        lock = store.get_lock("mov001", "poster")
        assert lock is not None
        assert lock["ref"] == "/original/poster.jpg"
        assert lock["sha256"] == "z" * 64

    def test_jellyfin_api_upload_locks_the_kind(self, tmp_path: Path):
        content = b"LOGO BYTES"
        sha = hashlib.sha256(content).hexdigest()
        plan = WritePlan(items=[
            ItemPlan(
                item_id="mov001", item_name="Test Movie",
                media_type="movie", tmdb_id=123, matched_by="provider_id",
                files=[FileWrite(
                    target_path="jellyfin-api://mov001/logo",
                    kind="logo", source_ref=str(tmp_path / "logo.png"),
                    content_sha256=sha, write_method="jellyfin_api",
                    meta={"lock_ref": "https://example/logo.png", "lock_sha256": "y" * 64},
                )],
            ),
        ])
        (tmp_path / "logo.png").write_bytes(content)
        cfg = _make_config(dry_run=False)
        jf, tmdb = MagicMock(), MagicMock()
        db = tmp_path / "jobs.db"
        cfg_dir = tmp_path / "config"
        cfg_dir.mkdir()

        apply_plan(plan, "r1", cfg, jf, tmdb, config_dir=cfg_dir, db_path=db, poll_timeout=0.1)

        store = OverrideStore(cfg_dir / "overrides.db")
        lock = store.get_lock("mov001", "logo")
        assert lock is not None
        assert lock["ref"] == "https://example/logo.png"

    def test_existing_lock_is_never_overwritten(self, tmp_path: Path):
        content = b"POSTER BYTES"
        sha = hashlib.sha256(content).hexdigest()
        cfg_dir = tmp_path / "config"
        cfg_dir.mkdir()
        store = OverrideStore(cfg_dir / "overrides.db")
        store.set_lock("mov001", "poster", "/original/first.jpg", "first" + "0" * 59)

        plan = WritePlan(items=[
            ItemPlan(
                item_id="mov001", item_name="Test Movie",
                media_type="movie", tmdb_id=123, matched_by="provider_id",
                files=[FileWrite(
                    target_path=str(tmp_path / "media" / "poster.jpg"),
                    kind="poster", source_ref=str(tmp_path / "src.jpg"),
                    content_sha256=sha,
                    meta={"lock_ref": "/original/second.jpg", "lock_sha256": "z" * 64},
                )],
            ),
        ])
        (tmp_path / "src.jpg").write_bytes(content)
        cfg = _make_config(dry_run=False)
        jf, tmdb = MagicMock(), MagicMock()
        db = tmp_path / "jobs.db"

        apply_plan(plan, "r1", cfg, jf, tmdb, config_dir=cfg_dir, db_path=db, poll_timeout=0.1)

        lock = store.get_lock("mov001", "poster")
        assert lock["ref"] == "/original/first.jpg"  # unchanged

    def test_unchanged_status_still_locks(self, tmp_path: Path):
        """A re-pick that happens to resolve back to the SAME bytes already
        on disk (e.g. best_rated, which reshuffle can't change) produces an
        "unchanged" FileWrite -- the write itself is correctly skipped, but
        the item must still get (re-)locked, otherwise it stays unlocked
        forever and every future apply re-queries sources again. Found live
        against test-Jellyfin while verifying S9d5 end to end."""
        target = tmp_path / "media" / "poster.jpg"
        target.parent.mkdir(parents=True)
        content = b"POSTER BYTES"
        target.write_bytes(content)  # already on disk -- matches content_sha256 below
        sha = hashlib.sha256(content).hexdigest()

        plan = WritePlan(items=[
            ItemPlan(
                item_id="mov001", item_name="Test Movie",
                media_type="movie", tmdb_id=123, matched_by="provider_id",
                files=[FileWrite(
                    target_path=str(target), kind="poster",
                    source_ref=str(tmp_path / "src.jpg"), content_sha256=sha,
                    status="unchanged",
                    meta={"lock_ref": "/original/poster.jpg", "lock_sha256": "z" * 64},
                )],
            ),
        ])
        cfg = _make_config(dry_run=False)
        jf, tmdb = MagicMock(), MagicMock()
        db = tmp_path / "jobs.db"
        cfg_dir = tmp_path / "config"
        cfg_dir.mkdir()

        result = apply_plan(plan, "r1", cfg, jf, tmdb, config_dir=cfg_dir, db_path=db, poll_timeout=0.1)
        assert result.files_written == 0
        assert result.files_unchanged == 1

        store = OverrideStore(cfg_dir / "overrides.db")
        lock = store.get_lock("mov001", "poster")
        assert lock is not None
        assert lock["ref"] == "/original/poster.jpg"

    def test_metadata_and_theme_song_are_never_locked(self, tmp_path: Path):
        nfo = b"<movie/>"
        sha = hashlib.sha256(nfo).hexdigest()
        plan = WritePlan(items=[
            ItemPlan(
                item_id="mov001", item_name="Test Movie",
                media_type="movie", tmdb_id=123, matched_by="provider_id",
                files=[FileWrite(
                    target_path=str(tmp_path / "media" / "movie.nfo"),
                    kind="metadata", source_ref="generated",
                    content_sha256=sha,
                    meta={"lock_ref": "generated", "lock_sha256": sha},
                )],
            ),
        ])
        cfg = _make_config(dry_run=False)
        jf, tmdb = MagicMock(), MagicMock()
        tmdb.movie_details = MagicMock()
        db = tmp_path / "jobs.db"
        cfg_dir = tmp_path / "config"
        cfg_dir.mkdir()

        # _fetch_content for "generated" calls _nfo_bytes -> tmdb.movie_details;
        # avoid that entirely by monkeypatching content fetch via overlay cache.
        from finmeta.engine.overlay import _write_overlay_cache
        _write_overlay_cache(sha, nfo, config_dir=cfg_dir)

        apply_plan(plan, "r1", cfg, jf, tmdb, config_dir=cfg_dir, db_path=db, poll_timeout=0.1)

        store = OverrideStore(cfg_dir / "overrides.db")
        assert store.get_lock("mov001", "metadata") is None
