"""S9d4c step 2a: library master switch + item-library resolution.

Offline unit/integration tests for the step-2a patch (commit 70b2911):
``build_plan`` must treat a disabled library (``libraries.<lib>.enabled:
false``) as producing an empty plan + a "library disabled" warning at
library- and item-scope, must leave an enabled library's plan unaffected,
and ``_resolve_item_library`` must resolve an item to the config of the
library that actually owns it (not just the first configured library).

No live Jellyfin/TMDb: a minimal fake JellyfinClient + ``tmdb=None`` (every
item carries its own ``Tmdb`` provider id, so ``match_item`` never needs the
TMDb client for provider-id matching).
"""
from __future__ import annotations

from typing import Dict, List, Optional


from finmeta.config.models import Config, Metadata, MovieArtwork, MovieLibrary, ServerConfig
from finmeta.engine.executor import apply_plan
from finmeta.engine.plan import WritePlan
from finmeta.engine.plan_builder import _resolve_item_library, build_plan
from finmeta.jellyfin_client.types import Item, Library


class FakeJf:
    """Minimal JellyfinClient stand-in: libraries, items, parent chains."""

    def __init__(self, libraries: Dict[str, Library],
                 items_by_parent: Dict[str, List[Item]],
                 items_by_id: Dict[str, Item]):
        self._libraries = libraries
        self._items_by_parent = items_by_parent
        self._items_by_id = items_by_id

    def get_library(self, name: str) -> Optional[Library]:
        return self._libraries.get(name)

    def get_libraries(self) -> List[Library]:
        return list(self._libraries.values())

    def get_item(self, item_id: str) -> Item:
        return self._items_by_id[item_id]

    def get_items(self, parent_id: Optional[str] = None, recursive: bool = True) -> List[Item]:
        return list(self._items_by_parent.get(parent_id, []))

    def iter_items(self, *, parent_id=None, recursive=True, include_item_types=None,
                   fields=None, user_id=None, page_size=200):
        pool = self._items_by_parent.get(parent_id, [])
        if include_item_types:
            pool = [it for it in pool if it.type in include_item_types]
        for it in pool:
            yield it

    def get_items_media_streams_raw(self, item_ids, batch_size=50):
        return {}

    def close(self):
        pass


def _lib(enabled: bool, overlays: Optional[List[str]] = None) -> MovieLibrary:
    return MovieLibrary(
        type="movies", enabled=enabled,
        metadata=Metadata(enabled=False), artwork=MovieArtwork(),
        overlays=overlays or [],
    )


def _movie_item(item_id: str, tmdb_id: str, parent_id: str) -> Item:
    return Item(id=item_id, name=f"Movie {item_id} (2020)", type="Movie",
                path=f"/media/{item_id}/movie.mkv",
                provider_ids={"Tmdb": tmdb_id}, parent_id=parent_id)


def _cfg(libraries: Dict[str, MovieLibrary]) -> Config:
    return Config(dry_run=True, server=ServerConfig(url="http://x:8096"),
                  libraries=libraries)


# ---------------------------------------------------------------------------
# library scope
# ---------------------------------------------------------------------------

def test_disabled_library_scope_returns_empty_plan_with_warning():
    cfg = _cfg({
        "Movies": _lib(enabled=True),
        "Anime": _lib(enabled=False),
    })
    jf_libs = {"Movies": Library(name="Movies", item_id="lib-movies", collection_type="movies"),
               "Anime": Library(name="Anime", item_id="lib-anime", collection_type="movies")}
    anime_item = _movie_item("a1", "999", parent_id="lib-anime")
    jf = FakeJf(jf_libs, {"lib-anime": [anime_item]}, {"a1": anime_item})

    plan = build_plan(cfg, jf, None, {}, library="Anime")

    assert plan.items == []
    assert len(plan.provider_warnings) == 1
    assert plan.provider_warnings[0].error == "library disabled: Anime"


def test_enabled_library_scope_is_unaffected():
    cfg = _cfg({
        "Movies": _lib(enabled=True),
        "Anime": _lib(enabled=False),
    })
    jf_libs = {"Movies": Library(name="Movies", item_id="lib-movies", collection_type="movies"),
               "Anime": Library(name="Anime", item_id="lib-anime", collection_type="movies")}
    movie_item = _movie_item("m1", "603", parent_id="lib-movies")
    jf = FakeJf(jf_libs, {"lib-movies": [movie_item]}, {"m1": movie_item})

    plan = build_plan(cfg, jf, None, {}, library="Movies")

    assert plan.provider_warnings == []
    assert [ip.item_id for ip in plan.items] == ["m1"]
    assert plan.items[0].tmdb_id == 603
    assert plan.items[0].matched_by == "provider_id"


# ---------------------------------------------------------------------------
# item scope
# ---------------------------------------------------------------------------

def test_item_scope_resolved_to_disabled_library_returns_empty_plan_with_warning():
    cfg = _cfg({
        "Movies": _lib(enabled=True),
        "Anime": _lib(enabled=False),
    })
    jf_libs = {"Movies": Library(name="Movies", item_id="lib-movies", collection_type="movies",
                                  locations=["/media/movies-root"]),
               "Anime": Library(name="Anime", item_id="lib-anime", collection_type="movies",
                                 locations=["/media/a1"])}
    anime_item = _movie_item("a1", "999", parent_id="lib-anime")
    jf = FakeJf(jf_libs, {}, {"a1": anime_item})

    plan = build_plan(cfg, jf, None, {}, item_id="a1")

    assert plan.items == []
    assert len(plan.provider_warnings) == 1
    assert plan.provider_warnings[0].error == "library disabled: Anime"


# ---------------------------------------------------------------------------
# _resolve_item_library: an item must resolve to the config of the library
# that actually owns it, not "first library in config" (the pre-step-2a bug).
# ---------------------------------------------------------------------------

def test_item_in_second_configured_library_resolves_to_its_own_config():
    lib_a = _lib(enabled=True, overlays=["resolution"])
    lib_b = _lib(enabled=True, overlays=["hdr"])
    # dict order: "First" comes before "Second" -- resolution must not just
    # pick the first entry.
    cfg = _cfg({"First": lib_a, "Second": lib_b})
    jf_libs = {"First": Library(name="First", item_id="lib-first", collection_type="movies",
                                 locations=["/media/first-root"]),
               "Second": Library(name="Second", item_id="lib-second", collection_type="movies",
                                  locations=["/media/m2"])}
    item = _movie_item("m2", "42", parent_id="lib-second")
    jf = FakeJf(jf_libs, {}, {"m2": item})

    name, lib_cfg = _resolve_item_library(item, jf, cfg)

    assert name == "Second"
    assert lib_cfg is lib_b
    assert lib_cfg is not lib_a

    # And the same holds end-to-end through build_plan (item scope).
    plan = build_plan(cfg, jf, None, {}, item_id="m2")
    assert plan.provider_warnings == []
    assert [ip.item_id for ip in plan.items] == ["m2"]


def test_item_path_matching_no_configured_library_does_not_borrow_an_unrelated_one():
    """An item whose real path matches NO configured library's Locations must
    never silently fall back to some unrelated configured library's config --
    reported live as a misleading "library disabled: <unrelated name>" 404 on
    an item that actually belonged to a different, enabled library whose
    path just didn't line up with what Jellyfin reports as its Locations."""
    # "Gamle Serier" is configured (and disabled) but its Locations don't
    # cover where this item's path actually lives -- nothing should match it.
    cfg = _cfg({"Gamle Serier": _lib(enabled=False)})
    jf_libs = {"Gamle Serier": Library(name="Gamle Serier", item_id="lib-old",
                                       collection_type="movies",
                                       locations=["/media/completely-unrelated"])}
    item = _movie_item("x1", "42", parent_id="lib-anime")
    item.path = "/media/anime/x1/movie.mkv"  # not under any configured Locations
    jf = FakeJf(jf_libs, {}, {"x1": item})

    name, lib_cfg = _resolve_item_library(item, jf, cfg)
    assert (name, lib_cfg) == (None, None)

    plan = build_plan(cfg, jf, None, {}, item_id="x1")
    assert plan.items == []
    assert len(plan.provider_warnings) == 1
    assert plan.provider_warnings[0].provider == "config"
    assert "could not determine" in plan.provider_warnings[0].error
    assert "Gamle Serier" not in plan.provider_warnings[0].error


# ---------------------------------------------------------------------------
# all-libraries scope (library=None, item_id=None, a.k.a. "lib:*"): a
# disabled library must not blank the whole plan just because it happens to
# be the first one in the config dict -- other enabled libraries' items
# still need to come through.
# ---------------------------------------------------------------------------

def test_global_scope_disabled_first_library_does_not_blank_other_libraries():
    cfg = _cfg({
        "Anime": _lib(enabled=False),   # first in dict order, disabled
        "Movies": _lib(enabled=True),
    })
    jf_libs = {"Anime": Library(name="Anime", item_id="lib-anime", collection_type="movies"),
               "Movies": Library(name="Movies", item_id="lib-movies", collection_type="movies")}
    anime_item = _movie_item("a1", "999", parent_id="lib-anime")
    movie_item = _movie_item("m1", "603", parent_id="lib-movies")
    jf = FakeJf(jf_libs,
                {"lib-anime": [anime_item], "lib-movies": [movie_item]},
                {"a1": anime_item, "m1": movie_item})

    plan = build_plan(cfg, jf, None, {}, library=None, item_id=None)

    assert [ip.item_id for ip in plan.items] == ["m1"]
    assert plan.provider_warnings == []


# ---------------------------------------------------------------------------
# apply: an empty plan (disabled library, either scope) writes zero files.
# ---------------------------------------------------------------------------

def test_apply_disabled_library_plan_writes_zero_files(tmp_path):
    cfg = Config(dry_run=False, server=ServerConfig(url="http://x:8096"),
                 libraries={"Anime": _lib(enabled=False)})
    plan = WritePlan(items=[], api_actions=[], unmatched=[])

    result = apply_plan(
        plan, "run-test-2a", cfg, jf=FakeJf({}, {}, {}), tmdb=None,
        config_dir=tmp_path / "config", db_path=tmp_path / "config" / "jobs.db",
    )

    assert result.files_written == 0
    assert result.files_unchanged == 0
    assert result.api_calls == 0
    assert result.errors == []
    assert list((tmp_path).rglob("*.nfo")) == []
    assert list((tmp_path).rglob("*.jpg")) == []
