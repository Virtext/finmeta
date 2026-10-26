"""Logo pack API tests (SPEC S9c8): install + report, no real network."""
from __future__ import annotations

import io
from pathlib import Path

import httpx
import pytest
import respx
from PIL import Image

from finmeta.api import AppContext, create_app
from finmeta.engine.plan import WritePlan
from fastapi.testclient import TestClient

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
    cfg_dir = tmp_path / "config"; cfg_dir.mkdir()
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


@respx.mock
def test_install_kometa_downloads_files(client, context, respx_mock):
    def handler(request):
        if request.url.path.endswith(".png"):
            return httpx.Response(200, content=_tiny_png())
        return httpx.Response(404)
    respx_mock.route(host="raw.githubusercontent.com").mock(side_effect=handler)

    r = client.post("/api/assets/logo-packs/kometa/install")
    assert r.status_code == 200, r.text
    data = r.json()
    pack_root = Path(context.config_dir) / "assets" / "logos" / "kometa"
    from finmeta.engine.logo_packs import load_pack
    expected = set(load_pack("kometa")["files"])
    have = {f.stem for f in pack_root.iterdir()}
    assert expected <= have, (sorted(expected - have))
    assert data["downloaded"] >= len(expected)
    assert data["skipped"] == 0
    assert "Kometa" in data.get("note", "") or "trademark" in data.get("note","").lower()


@respx.mock
def test_install_idempotent_skips_existing(client, context, respx_mock):
    def ok_handler(request):
        return httpx.Response(200, content=_tiny_png())
    route = respx_mock.route(host="raw.githubusercontent.com")
    route.mock(side_effect=ok_handler)
    n1 = client.post("/api/assets/logo-packs/kometa/install").json()["downloaded"]

    calls = []
    def counting(request):
        calls.append(str(request.url))
        return httpx.Response(200, content=_tiny_png())
    route.reset()
    route.mock(side_effect=counting)
    r2 = client.post("/api/assets/logo-packs/kometa/install").json()
    assert r2["downloaded"] == 0
    assert r2["failed"] == 0
    assert r2["skipped"] == n1
    assert not calls, f"expected no HTTP on second install but got {calls[:3]}"


@respx.mock
def test_unknown_pack_404(client):
    r = client.post("/api/assets/logo-packs/nope/install")
    assert r.status_code == 404


@respx.mock
def test_total_failure_returns_502(client, context, respx_mock):
    from finmeta.engine.logo_packs import load_pack
    total = len(load_pack("kometa")["files"])
    respx_mock.get(url__regex=r".*\.png$").mock(return_value=httpx.Response(500))
    r = client.post("/api/assets/logo-packs/kometa/install")
    assert r.status_code == 502
    detail = r.json()["detail"]
    assert detail["downloaded"] == 0
    assert detail["failed"] == total


def test_pack_status_endpoint(client, context):
    # not installed yet
    r = client.get("/api/assets/logo-packs/kometa/status")
    assert r.status_code == 200
    d = r.json()
    assert d["name"] == "kometa"
    assert d["installed"] is False
    assert d["complete"] is False
    assert d["have"] == 0
    # unknown pack -> 404
    assert client.get("/api/assets/logo-packs/nope/status").status_code == 404


def test_get_reports_installed_packs(context):
    app = create_app(context)
    with TestClient(app) as c:
        r = c.get("/api/assets/logo-packs")
    assert r.status_code == 200
    data = r.json()
    assert "kometa" in data["known"]
    by_name = {d["name"]: d for d in data["detail"]}
    assert by_name["kometa"]["installed"] is False

