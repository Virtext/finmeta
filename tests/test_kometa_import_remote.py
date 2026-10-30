"""Tests for POST /api/kometa/import-remote (download+import).

Mocks the actual httpx GET at the transport level (respx) so no real network
access happens; builds a synthetic zip matching the real jmxd/Kometa repo's
layout (Kometa-main/overlays/media_info.yml + overlays/images/...) using the
same real fixture YAML the expand/to_pack unit tests already use, so a clean
import here is good evidence the real jmxd download will also come back
clean (verified live against the real repo separately, not re-verified
against the live network here, only the pipeline around it).
"""
from __future__ import annotations

import io
import zipfile
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from finmeta.api import AppContext, create_app
from finmeta.api.routes_kometa import KNOWN_SOURCES, _safe_extract
from finmeta.engine.plan import WritePlan
from finmeta.kometa_import.expand import expand_file
from finmeta.kometa_import.to_pack import kometa_to_pack

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "kometa"
MEDIA_INFO = FIXTURES / "media_info.yml"

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


def _tiny_png() -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGBA", (12, 8), (255, 0, 0)).save(buf, format="PNG")
    return buf.getvalue()


def _build_jmxd_like_zip() -> bytes:
    """A zip with the same internal layout as the real jmxd/Kometa repo zip,
    using the project's own real media_info.yml fixture so expand_file/
    kometa_to_pack behave exactly as they do for the real download."""
    spec = KNOWN_SOURCES["jmxd"]
    probe_root = Path("/unused-probe-root")
    flat, report = expand_file(MEDIA_INFO)
    _, copies, _ = kometa_to_pack(flat, report, probe_root, "probe")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(f"{spec['root_dir']}/{spec['overlay_file_rel']}", MEDIA_INFO.read_text())
        seen = set()
        for src, _rel in copies:
            # `src` is the fully-resolved SOURCE path (probe_root/<real path
            # under kometa_root_rel in the actual repo> -- e.g. media_info/
            # edition/IMAX.png); strip the probe root to get that real
            # relative path, independent of the pack's own destination
            # naming (which only matches by coincidence for some entries).
            src_rel = Path(src).relative_to(probe_root)
            if src_rel in seen:
                continue
            seen.add(src_rel)
            zf.writestr(f"{spec['root_dir']}/{spec['kometa_root_rel']}/{src_rel.as_posix()}", _tiny_png())
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


def test_sources_endpoint_lists_jmxd(client):
    r = client.get("/api/kometa/sources")
    assert r.status_code == 200
    assert "jmxd" in r.json()["sources"]
    assert "zip_url" in r.json()["sources"]["jmxd"]


def test_unknown_source_400(client):
    r = client.post("/api/kometa/import-remote", json={"source": "nope", "pack_name": "x"})
    assert r.status_code == 400


def test_custom_source_requires_all_fields(client):
    r = client.post("/api/kometa/import-remote",
                     json={"source_url": "https://example.com/x.zip", "pack_name": "x"})
    assert r.status_code == 400


@respx.mock
def test_import_remote_dry_run_clean(client):
    zip_bytes = _build_jmxd_like_zip()
    respx.get(KNOWN_SOURCES["jmxd"]["zip_url"]).mock(
        return_value=httpx.Response(200, content=zip_bytes))
    r = client.post("/api/kometa/import-remote",
                     json={"source": "jmxd", "pack_name": "jmxd-media-info", "dry_run": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["dry_run"] is True
    assert body["missing_images"] == []
    assert len(body["image_copies"]) > 0


@respx.mock
def test_import_remote_writes_pack(client, context):
    zip_bytes = _build_jmxd_like_zip()
    respx.get(KNOWN_SOURCES["jmxd"]["zip_url"]).mock(
        return_value=httpx.Response(200, content=zip_bytes))
    r = client.post("/api/kometa/import-remote",
                     json={"source": "jmxd", "pack_name": "jmxd-media-info", "dry_run": False})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["dry_run"] is False
    assert len(body["written"]) > 0
    target = Path(context.config_dir) / "assets" / "packs" / "jmxd-media-info"
    assert (target / "pack.yml").is_file()
    assert any((target / "images").rglob("*.png"))


@respx.mock
def test_import_remote_existing_pack_needs_overwrite(client):
    zip_bytes = _build_jmxd_like_zip()
    respx.get(KNOWN_SOURCES["jmxd"]["zip_url"]).mock(
        return_value=httpx.Response(200, content=zip_bytes))
    first = client.post("/api/kometa/import-remote",
                         json={"source": "jmxd", "pack_name": "dup-pack", "dry_run": False})
    assert first.status_code == 200
    second = client.post("/api/kometa/import-remote",
                          json={"source": "jmxd", "pack_name": "dup-pack", "dry_run": False})
    assert second.status_code == 409


def test_safe_extract_rejects_zip_slip(tmp_path):
    evil = io.BytesIO()
    with zipfile.ZipFile(evil, "w") as zf:
        zf.writestr("../../etc/evil.txt", "pwned")
    evil.seek(0)
    target = tmp_path / "stage"
    target.mkdir()
    from fastapi import HTTPException
    with zipfile.ZipFile(evil) as zf, pytest.raises(HTTPException):
        _safe_extract(zf, target)
