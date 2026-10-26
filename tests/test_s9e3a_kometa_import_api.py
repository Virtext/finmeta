"""Kometa import API tests (SPEC S9 E): POST /api/kometa/import + image copy.

Uses the real jmxd fixtures in tests/fixtures/kometa/ and tiny generated PNGs
at the exact paths media_info.yml / audience_rating.yml reference, so the
import has real source images to copy into <config>/assets/packs/<name>/.
"""
from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image
from fastapi.testclient import TestClient

from finmeta.api import AppContext, create_app
from finmeta.engine.plan import WritePlan

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "kometa"
MEDIA_INFO = FIXTURES / "media_info.yml"
AUDIENCE_RATING = FIXTURES / "audience_rating.yml"

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


def _tiny_png(color=(255, 0, 0)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGBA", (12, 8), color).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def context(tmp_path, monkeypatch):
    monkeypatch.setenv("JELLYFIN_API_KEY", "fake-key")
    monkeypatch.setenv("TMDB_KEY", "fake-tmdb-key")
    monkeypatch.setenv("FINMETA_AUTH", "off")
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "config.yml").write_text(CONFIG_YAML)
    ctx = AppContext(
        str(cfg_dir / "config.yml"), config_dir=str(cfg_dir),
        db_path=str(cfg_dir / "jobs.db"), overrides_path=str(cfg_dir / "overrides.db"),
        plan_builder=lambda *a, **k: WritePlan(items=[]),
    )
    return ctx


@pytest.fixture
def client(context):
    app = create_app(context)
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def media_root(tmp_path_factory):
    """A kometa images root containing tiny PNGs at the paths media_info.yml expects."""
    root = tmp_path_factory.mktemp("kometa-images")
    from finmeta.kometa_import.expand import expand_file
    from finmeta.kometa_import.to_pack import kometa_to_pack
    flat, report = expand_file(MEDIA_INFO)
    _, copies, _ = kometa_to_pack(flat, report, root, "media-info-pack")
    for src, _ in copies:
        p = Path(src)
        if not p.is_file():
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(_tiny_png())
    return root


@pytest.fixture(scope="module")
def audience_root(tmp_path_factory):
    """A kometa images root with the three audience backdrop PNGs present."""
    root = tmp_path_factory.mktemp("kometa-audience-images")
    for tier in ("high", "mid", "low"):
        p = root / "audience_score" / f"audience_score_{tier}.png"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(_tiny_png())
    return root


@pytest.fixture(scope="module")
def media_root_full(tmp_path_factory, media_root):
    """Same image tree nested under <root>/config/overlays/images/ (full Kometa layout)."""
    full = tmp_path_factory.mktemp("kometa-full") / "config" / "overlays" / "images"
    import shutil
    shutil.copytree(media_root, full)
    return full


def _payload(overlay_file: Path, root: Path, pack_name: str, **kw) -> dict:
    d = {"overlay_file": str(overlay_file), "kometa_root": str(root),
         "pack_name": pack_name}
    d.update(kw)
    return d


def test_dry_run_writes_nothing(client, context, media_root):
    r = client.post("/api/kometa/import", json=_payload(MEDIA_INFO, media_root, "media-info-pack"))
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["dry_run"] is True
    assert data["pack_name"] == "media-info-pack"
    assert data["pack_yml"]["name"] == "media-info-pack"
    assert data["pack_yml"]["categories"], "media_info must produce categories"
    # every planned copy source exists, so nothing is missing
    assert data["missing_images"] == []
    assert len(data["image_copies"]) > 0
    # report: counts present, gradient_top reported as skipped/not supported in D3
    rep = data["report"]
    assert rep["counts"]["ok"] > 0 and rep["counts"]["partial"] > 0
    gt = [it for it in rep["items"] if it["key"] == "gradient_top"]
    assert gt, "gradient_top must appear in the report"
    assert gt[0]["status"] == "skipped"
    assert any("not supported in D3" in n for n in gt[0]["notes"])
    packs_dir = context.config_dir / "assets" / "packs"
    assert not packs_dir.exists() or not (packs_dir / "media-info-pack").exists()


def test_real_import_writes_pack_and_images(client, context, media_root):
    r = client.post("/api/kometa/import",
                    json=_payload(MEDIA_INFO, media_root, "media-info-pack", dry_run=False))
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["dry_run"] is False
    target = context.config_dir / "assets" / "packs" / "media-info-pack"
    assert (target / "pack.yml").is_file()
    written = set(data["written"])
    assert written, "expected at least one image to be copied"
    for rel in written:
        assert (target / rel).is_file(), f"{rel} not on disk after import"
    # nothing outside the pack dir
    packs_dir = context.config_dir / "assets" / "packs"
    extras = [e.name for e in packs_dir.iterdir() if e.name != "media-info-pack"]
    assert extras == []


def test_imported_pack_loads_via_api(client, context, media_root):
    client.post("/api/kometa/import",
                json=_payload(MEDIA_INFO, media_root, "media-info-pack", dry_run=False))
    r = client.get("/api/packs")
    assert r.status_code == 200, r.text
    packs = {p["name"]: p for p in r.json()["packs"]}
    mp = packs["media-info-pack"]
    assert mp["valid"] is True
    assert mp["error"] is None
    # all images were copied, so none missing
    assert mp["missing_images"] == []


def test_existing_pack_conflict_409(client, context, media_root):
    client.post("/api/kometa/import",
                json=_payload(MEDIA_INFO, media_root, "media-info-pack", dry_run=False))
    r = client.post("/api/kometa/import",
                    json=_payload(MEDIA_INFO, media_root, "media-info-pack", dry_run=False))
    assert r.status_code == 409
    # overwrite=true succeeds
    r2 = client.post("/api/kometa/import",
                     json=_payload(MEDIA_INFO, media_root, "media-info-pack",
                                   dry_run=False, overwrite=True))
    assert r2.status_code == 200, r2.text


def test_bad_pack_name_400(client, context, media_root):
    for bad in ("Bad_Name", "pack name", "UPPER", "", "pack.name"):
        r = client.post("/api/kometa/import",
                        json=_payload(MEDIA_INFO, media_root, bad))
        assert r.status_code == 400, f"{bad!r} should be rejected, got {r.status_code}"


def test_bad_paths_400(client, context, media_root):
    # relative path
    r = client.post("/api/kometa/import", json={
        "overlay_file": "relative/path.yml", "kometa_root": str(media_root),
        "pack_name": "ok-pack"})
    assert r.status_code == 400
    # nonexistent path
    r = client.post("/api/kometa/import", json={
        "overlay_file": "/nonexistent/overlay.yml", "kometa_root": str(media_root),
        "pack_name": "ok-pack"})
    assert r.status_code == 400


def test_missing_images_listed_not_crash(client, context, audience_root):
    # Delete one of the three source images to make it missing.
    victim = audience_root / "audience_score" / "audience_score_mid.png"
    victim.unlink()
    r = client.post("/api/kometa/import",
                    json=_payload(AUDIENCE_RATING, audience_root, "audience-pack"))
    assert r.status_code == 200, r.text
    data = r.json()
    assert str(victim) in data["missing_images"]
    # real import: missing image skipped, others copied, no crash
    r2 = client.post("/api/kometa/import",
                     json=_payload(AUDIENCE_RATING, audience_root, "audience-pack", dry_run=False))
    assert r2.status_code == 200, r2.text
    target = context.config_dir / "assets" / "packs" / "audience-pack"
    assert (target / "images" / "backdrop" / "audience_score_high.png").is_file()
    assert not (target / "images" / "backdrop" / "audience_score_mid.png").exists()
    assert (target / "images" / "backdrop" / "audience_score_low.png").is_file()


def test_pack_name_slug_ok(client, context, media_root):
    r = client.post("/api/kometa/import", json=_payload(MEDIA_INFO, media_root, "a-b-123"))
    assert r.status_code == 200, r.text


def test_import_accepts_full_kometa_root_layout(client, context, media_root_full):
    """S9e4: kometa_root may be the Kometa root (config/ layout), not just images folder."""
    r = client.post("/api/kometa/import", json=_payload(MEDIA_INFO, media_root_full, "media-info-pack"))
    assert r.status_code == 200
    body = r.json()
    assert body["dry_run"] is True
    assert not body["missing_images"], f"no images may be missing in full-root layout: {body['missing_images']}"
    for src, _ in body["image_copies"]:
        assert str(media_root_full) in src


def test_import_accepts_images_folder_layout(client, context, media_root):
    """S9e4: kometa_root as the images folder (existing layout) keeps working."""
    r = client.post("/api/kometa/import", json=_payload(MEDIA_INFO, media_root, "media-info-pack"))
    assert r.status_code == 200
    body = r.json()
    assert not body["missing_images"]


# --------------------------------------------------------------------------
# 2026-10-20: DELETE /api/packs/{name} -- operator request to remove an
# installed pack from the Overlays tab's "Installed packs" list.

def test_delete_pack_removes_files_and_listing(client, context, media_root):
    client.post("/api/kometa/import",
               json=_payload(MEDIA_INFO, media_root, "media-info-pack", dry_run=False))
    target = context.config_dir / "assets" / "packs" / "media-info-pack"
    assert target.is_dir()

    r = client.delete("/api/packs/media-info-pack")
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "name": "media-info-pack"}
    assert not target.exists()
    assert client.get("/api/packs").json()["packs"] == []


def test_delete_unknown_pack_404(client, context):
    r = client.delete("/api/packs/does-not-exist")
    assert r.status_code == 404


def test_delete_bad_name_400(client, context):
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


def test_delete_pack_scrubs_profile_references(client, context, media_root):
    client.post("/api/kometa/import",
               json=_payload(MEDIA_INFO, media_root, "media-info-pack", dry_run=False))
    cfg_with_profile = CONFIG_YAML + "\n".join([
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
