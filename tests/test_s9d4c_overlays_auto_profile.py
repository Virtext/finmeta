"""S9d4c step 2b: single source of truth for overlays.

A library's (deprecated) ``overlays`` list is migrated, at config-load time,
into an auto-generated ``<library>-auto`` profile whenever no profile (or an
elements-less profile) is assigned -- so plan_builder and the UI only ever
need to read ``overlay_profile`` + ``overlays.profiles`` from here on.
"""
from __future__ import annotations


import pytest

from finmeta.config.models import Config, MovieLibrary, OverlaysConfig, ServerConfig
from finmeta.config.schema import current_schema, schema_is_current
from finmeta.engine.plan_builder import _profile_overlay_names, _profile_style


def _cfg(libraries, profiles=None):
    return Config(dry_run=True, server=ServerConfig(url="http://x:8096"),
                  libraries=libraries,
                  overlays=OverlaysConfig(profiles=profiles or {}))


def test_no_profile_gets_auto_profile_with_the_list_elements():
    cfg = _cfg({"Movies": MovieLibrary(type="movies", overlays=["resolution", "hdr"])})
    lib = cfg.libraries["Movies"]
    assert lib.overlay_profile == "Movies-auto"
    assert cfg.overlays.profiles["Movies-auto"]["elements"] == ["resolution", "hdr"]
    # And plan_builder now reads it purely via the profile.
    assert _profile_overlay_names(lib, cfg) == ["resolution", "hdr"]


def test_elementless_profile_gets_elements_merged_into_a_new_auto_profile():
    cfg = _cfg(
        {"Movies": MovieLibrary(type="movies", overlays=["resolution"],
                                 overlay_profile="movies")},
        profiles={"movies": {"style": {"margin": 30}}},
    )
    lib = cfg.libraries["Movies"]
    # Reassigned to the auto profile -- the old "movies" profile is untouched
    # (and now unreferenced), the new one carries both style and elements.
    assert lib.overlay_profile == "Movies-auto"
    assert cfg.overlays.profiles["movies"] == {"style": {"margin": 30}}
    assert cfg.overlays.profiles["Movies-auto"] == {
        "style": {"margin": 30}, "elements": ["resolution"]}
    assert _profile_style(lib, cfg).margin == 30
    assert _profile_overlay_names(lib, cfg) == ["resolution"]


def test_profile_with_elements_already_is_left_alone():
    cfg = _cfg(
        {"Movies": MovieLibrary(type="movies", overlays=["resolution"],
                                 overlay_profile="p")},
        profiles={"p": {"elements": ["hdr"]}},
    )
    lib = cfg.libraries["Movies"]
    assert lib.overlay_profile == "p"
    assert set(cfg.overlays.profiles) == {"p"}
    # The profile's own elements win over the deprecated list (unchanged
    # semantics from before step 2b).
    assert _profile_overlay_names(lib, cfg) == ["hdr"]


def test_profile_with_empty_elements_list_means_explicit_no_overlays():
    """elements: [] is a deliberate choice, not "missing" -- no migration."""
    cfg = _cfg(
        {"Movies": MovieLibrary(type="movies", overlays=["resolution"],
                                 overlay_profile="none")},
        profiles={"none": {"elements": []}},
    )
    lib = cfg.libraries["Movies"]
    assert lib.overlay_profile == "none"
    assert set(cfg.overlays.profiles) == {"none"}
    assert _profile_overlay_names(lib, cfg) == []


def test_empty_overlays_list_never_triggers_migration():
    cfg = _cfg({"Movies": MovieLibrary(type="movies", overlays=[])})
    lib = cfg.libraries["Movies"]
    assert lib.overlay_profile is None
    assert cfg.overlays.profiles == {}


def test_unknown_profile_reference_is_not_silently_migrated_away():
    """A broken/typo'd overlay_profile must still fail validation -- the
    auto-migration must not quietly "fix" it into a valid auto profile."""
    with pytest.raises(Exception) as ei:
        _cfg(
            {"Movies": MovieLibrary(type="movies", overlays=["resolution"],
                                     overlay_profile="ghost")},
        )
    assert "unknown overlay profile" in str(ei.value)
    assert "'ghost'" in str(ei.value)


def test_two_libraries_each_get_their_own_auto_profile():
    cfg = _cfg({
        "Movies": MovieLibrary(type="movies", overlays=["resolution"]),
        "Shows": MovieLibrary(type="movies", overlays=["hdr"]),
    })
    assert cfg.libraries["Movies"].overlay_profile == "Movies-auto"
    assert cfg.libraries["Shows"].overlay_profile == "Shows-auto"
    assert cfg.overlays.profiles["Movies-auto"]["elements"] == ["resolution"]
    assert cfg.overlays.profiles["Shows-auto"]["elements"] == ["hdr"]


# ---------------------------------------------------------------------------
# schema: overlays is marked deprecated, schema.json on disk matches
# ---------------------------------------------------------------------------

def test_overlays_field_marked_deprecated_in_schema():
    schema = current_schema()
    for lib_type in ("MovieLibrary", "TvLibrary"):
        prop = schema["$defs"][lib_type]["properties"]["overlays"]
        assert prop.get("deprecated") is True


def test_schema_json_on_disk_is_current():
    assert schema_is_current("config/schema.json")
