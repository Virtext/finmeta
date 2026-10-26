"""Tests for image pack loader (SPEC S9 D3 step 1)."""
from __future__ import annotations

from pathlib import Path

import pytest

from finmeta.engine.image_packs import (
    PackDef, CategoryDef, load_packs, get_pack, resolve_pack_image,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _config_dir_with_fixtures() -> Path:
    """Return a path whose assets/packs/ contains the fixture packs."""
    # Structure: tests/fixtures/config/assets/packs/<name>/pack.yml
    return FIXTURES / "config"


class TestPackLoader:
    def test_load_valid_pack(self):
        packs = load_packs(_config_dir_with_fixtures())
        names = [p.name for p in packs]
        assert "test_pack" in names
        tp = next(p for p in packs if p.name == "test_pack")
        assert tp.valid is True
        assert tp.error is None
        assert set(tp.categories.keys()) == {"resolution", "hdr", "audio", "rating"}
        assert tp.description == "Test fixture pack for S9d3"

    def test_missing_images_reported(self):
        packs = load_packs(_config_dir_with_fixtures())
        tp = next(p for p in packs if p.name == "test_pack")
        # res_720p and hdr10plus are intentionally missing
        assert "resolution/res_720p" in tp.missing_images
        assert "hdr/hdr10plus" in tp.missing_images
        # Present images should NOT be listed
        assert "resolution/res_4k" not in tp.missing_images
        assert "rating/gold" not in tp.missing_images

    def test_invalid_pack_flagged(self):
        packs = load_packs(_config_dir_with_fixtures())
        bp = next(p for p in packs if p.name == "bad_pack")
        assert bp.valid is False
        assert bp.error is not None
        assert "bogus_matcher" in bp.error

    def test_non_pack_dir_skipped(self):
        """A directory without pack.yml is silently skipped."""
        packs = load_packs(_config_dir_with_fixtures())
        names = [p.name for p in packs]
        assert "not_a_pack" not in names

    def test_empty_dir(self, tmp_path):
        """No packs directory -> empty list."""
        packs = load_packs(tmp_path)
        assert packs == []


class TestPackDef:
    def test_valid_def(self):
        d = PackDef(
            name="x",
            categories={
                "resolution": CategoryDef(matcher="format_rule", keys=["res_4k"]),
                "rating": CategoryDef(matcher="rating_tier", keys=["gold"],
                                       tiers={"gold": ">=8.0"}),
            },
        )
        assert d.categories["resolution"].matcher == "format_rule"

    def test_freely_named_category_accepted(self):
        # S9d3 part A: packs may define ANY category name; each becomes its own badge.
        d = PackDef(
            name="x",
            categories={
                "edition": CategoryDef(matcher="filename_regex", keys=["limited"], regex=r"(?P<key>limited)"),
                "network": CategoryDef(matcher="metadata", field="studio", keys=["hbo"]),
            },
        )
        assert set(d.categories.keys()) == {"edition", "network"}

    def test_bad_matcher(self):
        with pytest.raises(Exception, match="unknown matcher"):
            CategoryDef(matcher="nope")


class TestResolvePackImage:
    def test_existing_image(self):
        data = resolve_pack_image(_config_dir_with_fixtures(), "test_pack", "resolution", "res_4k")
        assert data is not None
        assert len(data) > 8  # non-trivial PNG

    def test_missing_image(self):
        data = resolve_pack_image(_config_dir_with_fixtures(), "test_pack", "resolution", "res_720p")
        assert data is None

    def test_unknown_pack(self):
        data = resolve_pack_image(_config_dir_with_fixtures(), "nonexistent", "resolution", "x")
        assert data is None


class TestGetPack:
    def test_found(self):
        p = get_pack(_config_dir_with_fixtures(), "test_pack")
        assert p is not None
        assert p.valid

    def test_not_found(self):
        assert get_pack(_config_dir_with_fixtures(), "nope") is None


class TestApiPacks:
    """Integration test for GET /api/packs."""

    @pytest.fixture()
    def client(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FINMETA_AUTH", "off")
        from fastapi.testclient import TestClient
        from finmeta.api.app import create_app_from_path

        # Create a minimal config so the app boots
        cfg = tmp_path / "config.yml"
        cfg.write_text(
            "dry_run: true\n"
            "server:\n"
            "  url: http://fake:8096\n"
            "  api_key_env: JELLYFIN_API_KEY\n"
            "libraries:\n"
            "  Movies:\n"
            "    type: movies\n"
            "    enabled: true\n"
            "    overlays: [resolution]\n"
        )
        # Symlink or copy fixture packs into the expected location
        packs_src = FIXTURES / "config" / "assets" / "packs"
        packs_dest = tmp_path / "assets" / "packs"
        import shutil
        shutil.copytree(packs_src, packs_dest)

        app = create_app_from_path(str(cfg))
        return TestClient(app)

    def test_list_packs(self, client):
        r = client.get("/api/packs")
        assert r.status_code == 200
        data = r.json()
        assert "packs" in data
        names = [p["name"] for p in data["packs"]]
        assert "test_pack" in names
        tp = next(p for p in data["packs"] if p["name"] == "test_pack")
        assert tp["valid"] is True
        assert len(tp["missing_images"]) >= 2


# ---------------------------------------------------------------------------
# Parsed-pack.yml cache (2026-10-22). load_packs/pack_defs used to re-read and
# re-parse every pack.yml on EVERY call, which the overlay resolver makes once
# per element per item (~5.5 s of pure redundant I/O per 1000-item library on
# the operator's FUSE-backed config dir). Now memoised on a stat-only
# signature behind a short TTL -- so these tests pin the thing that would
# actually hurt if the memo were wrong: staleness.
# ---------------------------------------------------------------------------

class TestPackDefsCache:
    def _write_pack(self, root, name, category):
        d = root / "assets" / "packs" / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "pack.yml").write_text(
            f"name: {name}\ncategories:\n"
            f"  {category}:\n    matcher: format_rule\n    keys: [k1]\n"
        )

    def _bust_ttl(self, monkeypatch):
        """Force every call past the TTL so only signature checking decides."""
        from finmeta.engine import image_packs
        monkeypatch.setattr(image_packs, "_SIG_TTL", -1.0)

    def test_repeated_calls_parse_once(self, tmp_path, monkeypatch):
        from finmeta.engine.image_packs import pack_defs
        self._write_pack(tmp_path, "p1", "resolution")
        calls = []
        import yaml as _yaml
        real_load = _yaml.safe_load
        monkeypatch.setattr(_yaml, "safe_load",
                            lambda *a, **k: (calls.append(1), real_load(*a, **k))[1])
        for _ in range(5):
            assert set(pack_defs(tmp_path)) == {"p1"}
        assert len(calls) == 1, f"pack.yml re-parsed {len(calls)} times"

    def test_edited_pack_yml_is_picked_up(self, tmp_path, monkeypatch):
        from finmeta.engine.image_packs import pack_defs
        self._bust_ttl(monkeypatch)
        self._write_pack(tmp_path, "p1", "resolution")
        assert set(pack_defs(tmp_path)["p1"].categories) == {"resolution"}
        # Same path, different content/size -> signature must change.
        self._write_pack(tmp_path, "p1", "video_codec")
        assert set(pack_defs(tmp_path)["p1"].categories) == {"video_codec"}

    def test_new_pack_is_picked_up(self, tmp_path, monkeypatch):
        from finmeta.engine.image_packs import pack_defs
        self._bust_ttl(monkeypatch)
        self._write_pack(tmp_path, "p1", "resolution")
        assert set(pack_defs(tmp_path)) == {"p1"}
        self._write_pack(tmp_path, "p2", "audio_codec")
        assert set(pack_defs(tmp_path)) == {"p1", "p2"}

    def test_deleted_pack_is_picked_up(self, tmp_path, monkeypatch):
        import shutil
        from finmeta.engine.image_packs import pack_defs
        self._bust_ttl(monkeypatch)
        self._write_pack(tmp_path, "p1", "resolution")
        self._write_pack(tmp_path, "p2", "audio_codec")
        assert set(pack_defs(tmp_path)) == {"p1", "p2"}
        shutil.rmtree(tmp_path / "assets" / "packs" / "p2")
        assert set(pack_defs(tmp_path)) == {"p1"}

    def test_two_config_dirs_do_not_share_an_entry(self, tmp_path, monkeypatch):
        from finmeta.engine.image_packs import pack_defs
        self._bust_ttl(monkeypatch)
        a, b = tmp_path / "a", tmp_path / "b"
        self._write_pack(a, "only_a", "resolution")
        self._write_pack(b, "only_b", "audio_codec")
        assert set(pack_defs(a)) == {"only_a"}
        assert set(pack_defs(b)) == {"only_b"}
        assert set(pack_defs(a)) == {"only_a"}   # b's call must not have evicted a

    def test_missing_packs_dir_is_empty_not_an_error(self, tmp_path, monkeypatch):
        from finmeta.engine.image_packs import load_packs, pack_defs
        self._bust_ttl(monkeypatch)
        assert pack_defs(tmp_path / "nope") == {}
        assert load_packs(tmp_path / "nope") == []

    def test_missing_images_still_stat_fresh_despite_cached_parse(self, tmp_path, monkeypatch):
        """Only the YAML parse is memoised -- an image appearing on disk must
        change load_packs()'s missing_images without a pack.yml edit."""
        from finmeta.engine.image_packs import load_packs
        self._bust_ttl(monkeypatch)
        self._write_pack(tmp_path, "p1", "resolution")
        pack = next(p for p in load_packs(tmp_path) if p.name == "p1")
        assert pack.missing_images == ["resolution/k1"]
        img = tmp_path / "assets" / "packs" / "p1" / "images" / "resolution" / "k1.png"
        img.parent.mkdir(parents=True, exist_ok=True)
        from PIL import Image
        Image.new("RGB", (4, 4)).save(img)
        pack = next(p for p in load_packs(tmp_path) if p.name == "p1")
        assert pack.missing_images == []
