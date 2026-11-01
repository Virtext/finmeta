"""Tests for Finmeta's own bundled badge packs (finmeta_clean/finmeta_dynamic)
and the seeding/warnings machinery that ships them (2026-11-01)."""
from __future__ import annotations

from PIL import Image

from finmeta.engine.image_packs import (
    BUNDLED_PACKS_DIR, PackDef, load_packs, seed_bundled_packs,
)

BUNDLED_NAMES = ("finmeta_clean", "finmeta_dynamic")


class TestBundledPackContent:
    """The shipped pack.yml + images themselves -- catches drift between the
    two if either is ever hand-edited without the other."""

    def test_both_bundled_packs_present_on_disk(self):
        for name in BUNDLED_NAMES:
            assert (BUNDLED_PACKS_DIR / name / "pack.yml").is_file()

    def test_both_bundled_packs_valid_and_complete(self):
        import yaml
        for name in BUNDLED_NAMES:
            pack_dir = BUNDLED_PACKS_DIR / name
            data = yaml.safe_load((pack_dir / "pack.yml").read_text())
            pdef = PackDef(**data)
            assert pdef.name == name
            for cat_name, cat_def in pdef.categories.items():
                assert cat_def.matcher == "format_rule"
                for key in cat_def.keys:
                    img = pack_dir / "images" / cat_name / f"{key}.png"
                    assert img.is_file(), f"{name}: missing {cat_name}/{key}.png"

    def test_bundled_images_are_not_poster_sized(self):
        # The exact bug this guards against: see docs/HISTORY.md 2026-10-27
        # ("ichimoku... badges were actually poster-sized").
        for name in BUNDLED_NAMES:
            for png in (BUNDLED_PACKS_DIR / name / "images").rglob("*.png"):
                with Image.open(png) as im:
                    assert im.height <= 400, f"{png} is {im.height}px tall"
                    assert im.mode == "RGBA", f"{png} has no alpha channel"

    def test_resolution_dynamic_range_audio_match_k9_keys(self):
        """The whole point of the `format_rule` matcher: it uses each K9
        rule's own `image` field (not its `key` field!) as the badge key --
        see match_format_rule in engine/image_pack_matchers.py. These must
        line up exactly or a badge silently never resolves."""
        from finmeta.engine.format_rules import DEFAULT_FORMAT_RULES
        k9_image_keys = {
            cat: {r["image"].removesuffix(".png") for r in rules}
            for cat, rules in DEFAULT_FORMAT_RULES.items()
        }
        import yaml
        for name in BUNDLED_NAMES:
            data = yaml.safe_load((BUNDLED_PACKS_DIR / name / "pack.yml").read_text())
            pdef = PackDef(**data)
            for cat_name in ("resolution", "dynamic_range", "audio"):
                pack_keys = set(pdef.categories[cat_name].keys)
                unknown = pack_keys - k9_image_keys[cat_name]
                assert not unknown, f"{name}/{cat_name} has keys K9 never emits: {unknown}"


class TestSeedBundledPacks:
    def test_seeds_both_packs_into_empty_config_dir(self, tmp_path):
        copied = seed_bundled_packs(tmp_path)
        assert set(copied) == set(BUNDLED_NAMES)
        for name in BUNDLED_NAMES:
            assert (tmp_path / "assets" / "packs" / name / "pack.yml").is_file()
        assert (tmp_path / "assets" / "packs" / ".bundled_seeded").is_file()

    def test_second_call_is_a_noop(self, tmp_path):
        seed_bundled_packs(tmp_path)
        copied_again = seed_bundled_packs(tmp_path)
        assert copied_again == []

    def test_deleted_pack_is_not_resurrected(self, tmp_path):
        """Delete must stick (SPEC: 'every pack now only ever arrives by
        hand') -- re-seeding on a later restart must never undo an
        operator's explicit DELETE /api/packs/{name}."""
        import shutil
        seed_bundled_packs(tmp_path)
        shutil.rmtree(tmp_path / "assets" / "packs" / "finmeta_clean")
        seed_bundled_packs(tmp_path)
        assert not (tmp_path / "assets" / "packs" / "finmeta_clean").exists()
        assert (tmp_path / "assets" / "packs" / "finmeta_dynamic").exists()

    def test_seeded_packs_load_cleanly(self, tmp_path):
        seed_bundled_packs(tmp_path)
        packs = load_packs(tmp_path)
        names = {p.name for p in packs}
        assert names == set(BUNDLED_NAMES)
        for p in packs:
            assert p.valid is True
            assert p.missing_images == []
            assert p.warnings == []


class TestPosterSizeWarning:
    def test_oversized_image_reported_in_warnings(self, tmp_path):
        d = tmp_path / "assets" / "packs" / "big-pack"
        (d / "images" / "resolution").mkdir(parents=True)
        (d / "pack.yml").write_text(
            "name: big-pack\ncategories:\n  resolution:\n"
            "    matcher: format_rule\n    keys: [res_4k]\n"
        )
        Image.new("RGBA", (1000, 1500)).save(d / "images" / "resolution" / "res_4k.png")
        packs = load_packs(tmp_path)
        tp = next(p for p in packs if p.name == "big-pack")
        assert tp.missing_images == []
        assert len(tp.warnings) == 1
        assert "resolution/res_4k" in tp.warnings[0]
        assert "poster-sized" in tp.warnings[0]

    def test_normal_sized_image_has_no_warning(self, tmp_path):
        d = tmp_path / "assets" / "packs" / "ok-pack"
        (d / "images" / "resolution").mkdir(parents=True)
        (d / "pack.yml").write_text(
            "name: ok-pack\ncategories:\n  resolution:\n"
            "    matcher: format_rule\n    keys: [res_4k]\n"
        )
        Image.new("RGBA", (300, 200)).save(d / "images" / "resolution" / "res_4k.png")
        packs = load_packs(tmp_path)
        tp = next(p for p in packs if p.name == "ok-pack")
        assert tp.warnings == []
