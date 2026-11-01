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


# ---------------------------------------------------------------------------
# Flat auto-detected packs (2026-11-01): a directory with NO pack.yml but a
# flat images/<key>.png layout (no per-category subfolder) is auto-detected
# by matching each filename against K9's own format_rule vocabulary --
# operator-requested simplification after writing pack.yml by hand (and
# getting its category names/subfolders right) turned out to be the actual
# blocker on a real first attempt. See docs/BADGE_PACKS.md.
# ---------------------------------------------------------------------------

class TestFlatAutoPack:
    def _make_flat_pack(self, tmp_path, name: str, keys: list[str], size=(300, 150)):
        from PIL import Image
        d = tmp_path / "assets" / "packs" / name / "images"
        d.mkdir(parents=True)
        for key in keys:
            Image.new("RGBA", size).save(d / f"{key}.png")
        return d

    def test_detected_and_categorised_purely_from_filenames(self, tmp_path):
        self._make_flat_pack(tmp_path, "flat-demo", ["res_4k", "dv_hdr", "dtsx"])
        packs = load_packs(tmp_path)
        p = next(pk for pk in packs if pk.name == "flat-demo")
        assert p.valid is True
        assert set(p.categories.keys()) == {"resolution", "dynamic_range", "audio"}
        for cat_def in p.categories.values():
            assert cat_def.matcher == "format_rule"

    def test_unrecognised_filenames_are_ignored_not_a_category(self, tmp_path):
        self._make_flat_pack(tmp_path, "flat-demo", ["res_4k", "totally_made_up"])
        packs = load_packs(tmp_path)
        p = next(pk for pk in packs if pk.name == "flat-demo")
        assert set(p.categories.keys()) == {"resolution"}

    def test_no_recognised_keys_at_all_is_not_a_pack(self, tmp_path):
        self._make_flat_pack(tmp_path, "random-folder", ["vacation_photo", "poster_v2"])
        packs = load_packs(tmp_path)
        assert "random-folder" not in [p.name for p in packs]

    def test_empty_images_dir_is_not_a_pack(self, tmp_path):
        (tmp_path / "assets" / "packs" / "empty" / "images").mkdir(parents=True)
        packs = load_packs(tmp_path)
        assert "empty" not in [p.name for p in packs]

    def test_missing_images_reports_the_full_k9_vocabulary(self, tmp_path):
        self._make_flat_pack(tmp_path, "flat-demo", ["res_4k"])
        packs = load_packs(tmp_path)
        p = next(pk for pk in packs if pk.name == "flat-demo")
        assert "resolution/res_4k" not in p.missing_images
        assert "resolution/res_1080p" in p.missing_images   # present as a key, just not as a file

    def test_resolve_pack_image_reads_the_flat_layout(self, tmp_path):
        self._make_flat_pack(tmp_path, "flat-demo", ["res_4k"])
        data = resolve_pack_image(tmp_path, "flat-demo", "resolution", "res_4k")
        assert data is not None
        assert resolve_pack_image(tmp_path, "flat-demo", "resolution", "res_1080p") is None

    def test_pack_yml_present_wins_over_flat_detection(self, tmp_path):
        """A real pack.yml always takes priority -- flat auto-detection only
        ever applies when there's no pack.yml at all."""
        d = self._make_flat_pack(tmp_path, "explicit", ["res_4k"])
        (d.parent / "pack.yml").write_text(
            "name: explicit\ndescription: hand-written\ncategories: {}\n"
        )
        packs = load_packs(tmp_path)
        p = next(pk for pk in packs if pk.name == "explicit")
        assert p.description == "hand-written"
        assert p.categories == {}   # the hand-written (empty) categories, not the auto-detected ones

    def test_cache_invalidates_when_a_file_is_added(self, tmp_path, monkeypatch):
        """Same signature-based cache as pack.yml packs -- adding a PNG to a
        flat pack must be picked up once the TTL passes, not stuck forever."""
        import finmeta.engine.image_packs as ip
        monkeypatch.setattr(ip, "_SIG_TTL", 0.0)
        self._make_flat_pack(tmp_path, "flat-demo", ["res_4k"])
        first = load_packs(tmp_path)
        p1 = next(pk for pk in first if pk.name == "flat-demo")
        assert "resolution/res_1080p" in p1.missing_images

        from PIL import Image
        Image.new("RGBA", (300, 150)).save(
            tmp_path / "assets" / "packs" / "flat-demo" / "images" / "res_1080p.png")
        second = load_packs(tmp_path)
        p2 = next(pk for pk in second if pk.name == "flat-demo")
        assert "resolution/res_1080p" not in p2.missing_images


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


# ---------------------------------------------------------------------------
# DELETE /api/packs/{name} (2026-10-20, operator request to remove an
# installed pack from the Overlays tab's "Installed packs" list). Moved here
# from the now-deleted local-path Kometa import test file (2026-10-27) --
# these never actually needed the Kometa import pipeline, just *some* pack on
# disk to delete, so they build one directly instead of importing it.
# ---------------------------------------------------------------------------

class _PackApiFixtures:
    """Shared context/client fixtures for the pack-mutation API tests below.

    Deliberately NOT named Test* -- pytest would otherwise collect (and
    re-run) every test method inherited from it under each subclass too.
    """
    CONFIG_YAML = "\n".join([
        "dry_run: true",
        "server:",
        "  url: http://fake:8096",
        "  api_key_env: JELLYFIN_API_KEY",
        "path_mappings:",
        "  - jellyfin: /media",
        "    local: /tmp/fake-media",
        "libraries:",
        "  Movies:",
        "    type: movies",
        "    metadata:",
        "      enabled: true",
    ]) + "\n"

    @pytest.fixture
    def context(self, tmp_path, monkeypatch):
        monkeypatch.setenv("JELLYFIN_API_KEY", "fake-key")
        monkeypatch.setenv("TMDB_KEY", "fake-tmdb-key")
        monkeypatch.setenv("FINMETA_AUTH", "off")
        cfg_dir = tmp_path / "config"
        cfg_dir.mkdir()
        (cfg_dir / "config.yml").write_text(self.CONFIG_YAML)
        from finmeta.api import AppContext
        from finmeta.engine.plan import WritePlan
        ctx = AppContext(
            str(cfg_dir / "config.yml"), config_dir=str(cfg_dir),
            db_path=str(cfg_dir / "jobs.db"), overrides_path=str(cfg_dir / "overrides.db"),
            plan_builder=lambda *a, **k: WritePlan(items=[]),
        )
        return ctx

    @pytest.fixture
    def client(self, context):
        from fastapi.testclient import TestClient
        from finmeta.api import create_app
        app = create_app(context)
        with TestClient(app) as c:
            yield c

    def _write_pack_with_image(self, context, name="media-info-pack"):
        """A real pack on disk: pack.yml + one image, no Kometa import involved."""
        d = context.config_dir / "assets" / "packs" / name
        (d / "images" / "resolution").mkdir(parents=True, exist_ok=True)
        (d / "pack.yml").write_text(
            f"name: {name}\ncategories:\n  resolution:\n    matcher: format_rule\n    keys: [res_4k]\n"
        )
        from PIL import Image
        Image.new("RGB", (4, 4)).save(d / "images" / "resolution" / "res_4k.png")
        return d


class TestDeletePackApi(_PackApiFixtures):
    def test_delete_pack_removes_files_and_listing(self, client, context):
        target = self._write_pack_with_image(context)
        assert target.is_dir()

        r = client.delete("/api/packs/media-info-pack")
        assert r.status_code == 200, r.text
        assert r.json() == {"ok": True, "name": "media-info-pack"}
        assert not target.exists()
        # Not an empty list any more: AppContext.start() (which TestClient's
        # lifespan triggers) seeds Finmeta's own bundled packs on first run --
        # this test only asserts the ONE pack it made is gone, not that every
        # pack is gone (see engine.image_packs.seed_bundled_packs).
        assert "media-info-pack" not in [p["name"] for p in client.get("/api/packs").json()["packs"]]

    def test_delete_unknown_pack_404(self, client, context):
        r = client.delete("/api/packs/does-not-exist")
        assert r.status_code == 404

    def test_delete_bad_name_400(self, client, context):
        # A literal "/" in the path segment never reaches our handler at all --
        # the HTTP layer normalizes "/api/packs/.." away before routing, and
        # Starlette's {name} path param doesn't match an embedded "/" either.
        # Either way the real property holds: no traversal ever succeeds (never
        # a 200, nothing outside assets/packs/ is touched).
        for bad in ("/api/packs/..%2F..%2Fetc", "/api/packs/.."):
            r = client.delete(bad)
            assert r.status_code in (400, 404, 405)
        # "%2e%2e" (urlencoded ".." with no literal slash) DOES reach the
        # handler unnormalized -- rejected there by the explicit "." / ".." check.
        r2 = client.delete("/api/packs/%2e%2e")
        assert r2.status_code == 400
        assert context.config_dir.is_dir(), "traversal must never touch anything outside assets/packs/"

    def test_delete_pack_scrubs_profile_references(self, client, context):
        self._write_pack_with_image(context)
        cfg_with_profile = self.CONFIG_YAML + "\n".join([
            "overlays:",
            "  profiles:",
            "    Movies-auto:",
            "      packs: [media-info-pack]",
            "      pack_overrides:",
            "        media-info-pack:",
            "          resolution: {enabled: false}",
            "",
        ])
        r = client.put("/api/config", json={"yaml": cfg_with_profile})
        assert r.status_code == 200, r.text

        r = client.delete("/api/packs/media-info-pack")
        assert r.status_code == 200, r.text

        prof = client.get("/api/config").json()["parsed"]["overlays"]["profiles"]["Movies-auto"]
        assert prof["packs"] == []
        assert "pack_overrides" not in prof or prof["pack_overrides"] in (None, {})


# ---------------------------------------------------------------------------
# POST /api/packs/scaffold (2026-11-01): the only path to a new pack now that
# the Kometa overlay-import feature is gone -- generates a pack.yml with K9's
# own format_rule keys pre-filled, so the operator only has to drop images in.
# ---------------------------------------------------------------------------

class TestScaffoldPackApi(_PackApiFixtures):
    def test_scaffold_creates_pack_with_all_categories_by_default(self, client, context):
        r = client.post("/api/packs/scaffold", json={"name": "my-new-pack"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["name"] == "my-new-pack"
        assert set(body["categories"]) == {"resolution", "dynamic_range", "audio"}
        assert len(body["missing_images"]) == 5 + 6 + 9   # every key, no images yet
        pack_dir = context.config_dir / "assets" / "packs" / "my-new-pack"
        assert (pack_dir / "pack.yml").is_file()
        assert (pack_dir / "images" / "resolution").is_dir()

    def test_scaffold_resolves_via_format_rule_once_images_dropped_in(self, client, context):
        from PIL import Image
        r = client.post("/api/packs/scaffold", json={"name": "p2", "categories": ["resolution"]})
        assert r.status_code == 200, r.text
        img_dir = context.config_dir / "assets" / "packs" / "p2" / "images" / "resolution"
        Image.new("RGBA", (200, 150)).save(img_dir / "res_4k.png")
        body = client.get("/api/packs").json()
        p2 = next(p for p in body["packs"] if p["name"] == "p2")
        assert "resolution/res_4k" not in p2["missing_images"]
        assert "resolution/res_1080p" in p2["missing_images"]

    def test_scaffold_rejects_existing_pack_name(self, client, context):
        self._write_pack_with_image(context, name="taken")
        r = client.post("/api/packs/scaffold", json={"name": "taken"})
        assert r.status_code == 409

    def test_scaffold_rejects_unknown_category(self, client, context):
        r = client.post("/api/packs/scaffold", json={"name": "p3", "categories": ["not_a_real_category"]})
        assert r.status_code == 400

    def test_scaffold_rejects_bad_name(self, client, context):
        r = client.post("/api/packs/scaffold", json={"name": "../etc"})
        assert r.status_code == 400
