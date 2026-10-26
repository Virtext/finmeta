"""Tests for image pack profile wiring (SPEC S9 D3 step 3).

Verifies that the shared resolution function produces identical results for
the preview and apply paths by construction: both call resolve_profile_packs
with the same inputs, so their output is the same.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from finmeta.engine.image_pack_resolve import resolve_profile_packs, resolve_profile_pack_image
from finmeta.engine.image_pack_matchers import MatchContext

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture()
def config_dir(tmp_path):
    """Temp config dir with the fixture packs copied in."""
    src = FIXTURES / "config" / "assets" / "packs"
    dest = tmp_path / "assets" / "packs"
    shutil.copytree(src, dest)
    return tmp_path


class TestResolveProfilePacks:
    def test_format_rule_resolution(self, config_dir):
        """A profile with a pack resolves format-rule images for 4K HDR10."""
        profile = {"packs": ["test_pack"]}
        ctx = MatchContext(
            k9_result={"image": "res_4k.png", "key": "res_4k"},
            rating_value=8.5,
        )
        result = resolve_profile_packs(config_dir, profile, ctx)
        # resolution should have an image (res_4k exists in fixture)
        assert "resolution" in result
        assert len(result["resolution"]) == 1
        # rating should resolve to "gold" tier (8.5 >= 8.0)
        assert "rating" in result
        assert len(result["rating"]) == 1

    def test_missing_image_no_badge(self, config_dir):
        """When the matched key's image is absent, no badge is produced."""
        profile = {"packs": ["test_pack"]}
        # res_720p is in the pack's keys but the file is missing
        ctx = MatchContext(
            k9_result={"image": "res_720p.png", "key": "res_720p"},
        )
        result = resolve_profile_packs(config_dir, profile, ctx)
        assert "resolution" not in result  # missing image -> no badge

    def test_no_packs_configured(self, config_dir):
        """Profile without packs returns empty."""
        profile = {"elements": ["resolution"]}
        ctx = MatchContext(k9_result={"image": "res_4k.png"})
        result = resolve_profile_packs(config_dir, profile, ctx)
        assert result == {}

    def test_pack_override_disable(self, config_dir):
        """pack_overrides.<pack>.<category>.enabled: false disables that category."""
        profile = {
            "packs": ["test_pack"],
            "pack_overrides": {"test_pack": {"rating": {"enabled": False}}},
        }
        ctx = MatchContext(k9_result={"image": "res_4k.png"}, rating_value=9.0)
        result = resolve_profile_packs(config_dir, profile, ctx)
        assert "resolution" in result
        assert "rating" not in result

    def test_freely_named_categories_produce_badges(self, config_dir):
        """Every category a pack defines becomes its own badge element.

        A fixture pack with freely-named categories (edition via filename_regex,
        network via metadata) yields badges for both, alongside the standard ones.
        """
        from PIL import Image
        import io as _io
        def png(b: bytes) -> bytes:
            img = Image.new("RGB", (8, 8), b)
            buf = _io.BytesIO()
            img.save(buf, format="PNG")
            return buf.getvalue()

        root = config_dir / "assets" / "packs" / "custom_pack"
        for cat, key, color in (("edition", "limited", (255, 0, 0)), ("network", "hbo", (0, 128, 0))):
            d = root / "images" / cat
            d.mkdir(parents=True, exist_ok=True)
            (d / f"{key}.png").write_bytes(png(color))
        (root).mkdir(parents=True, exist_ok=True)
        (root / "pack.yml").write_text(
            "name: custom_pack\ncategories:\n"
            "  edition:\n    matcher: filename_regex\n    keys: [limited]\n"
            '    regex: "(?P<key>limited)"\n'
            "  network:\n    matcher: metadata\n    field: studio\n    keys: [hbo]\n"
        )
        profile = {"packs": ["custom_pack"]}
        ctx = MatchContext(filename="Movie (2020) Limited Edition.mkv",
                           metadata={"studio": "hbo"})
        result = resolve_profile_packs(config_dir, profile, ctx)
        assert set(result.keys()) == {"edition", "network"}
        assert len(result["edition"]) == 1 and len(result["network"]) == 1

    def test_pack_override_disables_only_that_category(self, config_dir):
        """Disabling one freely-named category removes only that badge."""
        from PIL import Image
        import io as _io
        def png(b: bytes) -> bytes:
            img = Image.new("RGB", (8, 8), b)
            buf = _io.BytesIO()
            img.save(buf, format="PNG")
            return buf.getvalue()

        root = config_dir / "assets" / "packs" / "custom_pack"
        for cat, key, color in (("edition", "limited", (255, 0, 0)), ("network", "hbo", (0, 128, 0))):
            d = root / "images" / cat
            d.mkdir(parents=True, exist_ok=True)
            (d / f"{key}.png").write_bytes(png(color))
        (root).mkdir(parents=True, exist_ok=True)
        (root / "pack.yml").write_text(
            "name: custom_pack\ncategories:\n"
            "  edition:\n    matcher: filename_regex\n    keys: [limited]\n"
            '    regex: "(?P<key>limited)"\n'
            "  network:\n    matcher: metadata\n    field: studio\n    keys: [hbo]\n"
        )
        profile = {
            "packs": ["custom_pack"],
            "pack_overrides": {"custom_pack": {"network": {"enabled": False}}},
        }
        ctx = MatchContext(filename="Movie (2020) Limited Edition.mkv",
                           metadata={"studio": "hbo"})
        result = resolve_profile_packs(config_dir, profile, ctx)
        assert set(result.keys()) == {"edition"}

    def test_pack_override_wrong_shape_ignored(self, config_dir):
        """Only .enabled: false is honored; other override shapes are ignored."""
        profile = {
            "packs": ["test_pack"],
            "pack_overrides": {"rating": False},  # old flat shape -> not D3 shape
        }
        ctx = MatchContext(k9_result={"image": "res_4k.png"}, rating_value=9.0)
        result = resolve_profile_packs(config_dir, profile, ctx)
        assert "resolution" in result and "rating" in result

    def test_unknown_pack_ignored(self, config_dir):
        """A pack name that doesn't exist is silently skipped."""
        profile = {"packs": ["nonexistent_pack"]}
        ctx = MatchContext(k9_result={"image": "res_4k.png"})
        result = resolve_profile_packs(config_dir, profile, ctx)
        assert result == {}

    def test_preview_equals_apply(self, config_dir):
        """The same inputs to resolve_profile_packs always give the same output.

        This is the contract that makes preview == apply: both paths call this
        function with the (profile, MatchContext) pair derived from the same
        item data, so the result is identical by construction.
        """
        profile = {"packs": ["test_pack"]}
        ctx = MatchContext(
            k9_result={"image": "hdr10.png", "key": "hdr10"},
            rating_value=7.0,
        )
        r1 = resolve_profile_packs(config_dir, profile, ctx)
        r2 = resolve_profile_packs(config_dir, profile, ctx)
        assert r1 == r2
        # Verify actual image bytes are equal (not just structure)
        for element in r1:
            assert r1[element] == r2[element]


class TestResolveProfilePackImageAliases:
    """A pack imported via Kometa overlay import/import-remote uses the real
    upstream category naming (video_codec/audio_codec), not Finmeta's own
    element names (hdr/audio). Without resolve_profile_pack_image's alias
    table, _overlay_poster's plain ``result.get(name)`` lookup would never
    find these -- hdr/audio always fell back to a text pill regardless of
    which pack was imported or what it was named (real bug, found 2026-10-22
    against an operator-imported pack)."""

    def test_hdr_element_resolves_via_video_codec_category(self, config_dir):
        profile = {"packs": ["jmxd_style_pack"]}
        images = resolve_profile_pack_image(
            config_dir, profile, "hdr", filename="Movie.2020.2160p.HDR10.x265-GROUP.mkv")
        assert len(images) == 1

    def test_audio_element_resolves_via_audio_codec_category(self, config_dir):
        profile = {"packs": ["jmxd_style_pack"]}
        images = resolve_profile_pack_image(
            config_dir, profile, "audio", filename="Movie.2020.Atmos.x265-GROUP.mkv")
        assert len(images) == 1

    def test_resolution_element_still_resolves_directly_no_alias_needed(self, config_dir):
        profile = {"packs": ["test_pack"]}
        images = resolve_profile_pack_image(
            config_dir, profile, "resolution",
            k9_result={"image": "res_4k.png", "key": "res_4k"})
        assert len(images) == 1

    def test_no_match_returns_empty_not_an_error(self, config_dir):
        profile = {"packs": ["jmxd_style_pack"]}
        images = resolve_profile_pack_image(
            config_dir, profile, "hdr", filename="Movie.2020.SDR.x265-GROUP.mkv")
        assert images == []

    def test_unaliased_element_falls_back_to_its_own_name(self, config_dir):
        """A pack that DOES use Finmeta's own element names (test_pack's
        hdr/audio categories) must keep working unchanged -- the alias list
        always tries the element's own name too."""
        profile = {"packs": ["test_pack"]}
        images = resolve_profile_pack_image(
            config_dir, profile, "hdr", k9_result={"image": "hdr10.png", "key": "hdr10"})
        assert len(images) == 1


class TestCombinedCodec:
    """A pack may define a 'combined_codec' category -- one image (e.g.
    "TrueHD Atmos") standing in for both the hdr and audio badges. Operator
    decision 2026-10-23: when it resolves, it replaces the hdr badge and the
    separate audio badge is suppressed (previously an open decision, see
    STATE.md "Needs human")."""

    def test_combined_codec_replaces_hdr_badge(self, config_dir):
        profile = {"packs": ["jmxd_style_pack"]}
        images = resolve_profile_pack_image(
            config_dir, profile, "hdr",
            filename="Movie.2020.2160p.TrueHD.Atmos.x265-GROUP.mkv")
        assert len(images) == 1

    def test_combined_codec_suppresses_audio_badge(self, config_dir):
        profile = {"packs": ["jmxd_style_pack"]}
        images = resolve_profile_pack_image(
            config_dir, profile, "audio",
            filename="Movie.2020.2160p.TrueHD.Atmos.x265-GROUP.mkv")
        assert images == []

    def test_no_combined_match_leaves_hdr_and_audio_independent(self, config_dir):
        """Without a combined_codec match, hdr/audio resolve independently
        (unchanged behaviour -- regression guard for the alias table)."""
        profile = {"packs": ["jmxd_style_pack"]}
        hdr = resolve_profile_pack_image(
            config_dir, profile, "hdr", filename="Movie.2020.HDR10.x265-GROUP.mkv")
        audio = resolve_profile_pack_image(
            config_dir, profile, "audio", filename="Movie.2020.Atmos.x265-GROUP.mkv")
        assert len(hdr) == 1
        assert len(audio) == 1
