"""S9e4: GET /api/fs/list — restricted to /import plus FINMETA_FS_BROWSE_ROOTS,
no symlink escape.

The extra allowed root under test is a throwaway tmp_path directory, opted
in via the env var exactly the way a real deployment would opt in its own
extra root -- never a path hardcoded into the shipped route or into these
tests (SECURITY_REVIEW.md L3). Using tmp_path instead of a fixed dev-host
path also means this suite passes unmodified in CI, not just on the
operator's own checkout."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from finmeta.api import AppContext, create_app
from finmeta.engine.plan import WritePlan

CONFIG_YAML = "\n".join([
    "dry_run: true",
    "server:",
    "  url: http://fake:8096",
    "  api_key_env: JELLYFIN_API_KEY",
]) + "\n"


@pytest.fixture
def extra_root(tmp_path) -> Path:
    root = tmp_path / "browse_root"
    root.mkdir()
    return root


@pytest.fixture
def client(tmp_path, extra_root, monkeypatch):
    monkeypatch.setenv("JELLYFIN_API_KEY", "fake-key")
    monkeypatch.setenv("FINMETA_AUTH", "off")
    monkeypatch.setenv("FINMETA_FS_BROWSE_ROOTS", str(extra_root))
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "config.yml").write_text(CONFIG_YAML)
    ctx = AppContext(
        str(cfg_dir / "config.yml"), config_dir=str(cfg_dir),
        db_path=str(cfg_dir / "jobs.db"), overrides_path=str(cfg_dir / "overrides.db"),
        plan_builder=lambda *a, **k: WritePlan(items=[]),
    )
    app = create_app(ctx)
    with TestClient(app) as c:
        yield c


def test_list_extra_root(client, extra_root):
    d = extra_root / "kometa"
    d.mkdir()
    (d / "media_info.yml").write_text("overlays: {}\n")
    (d / "audience_rating.yml").write_text("overlays: {}\n")
    (d / "notes.txt").write_text("not a yml file\n")
    r = client.get("/api/fs/list", params={"path": str(d)})
    assert r.status_code == 200
    body = r.json()
    assert body["path"] == str(d)
    assert body["folders"] == []
    assert {Path(f).name for f in body["files"]} == {"media_info.yml", "audience_rating.yml"}


def test_list_rejects_outside_allowed_roots(client):
    for bad in ("/etc", "/tmp", "/home"):
        r = client.get("/api/fs/list", params={"path": bad})
        assert r.status_code == 400, f"{bad} must be rejected"


def test_list_rejects_relative_and_missing(client, extra_root):
    assert client.get("/api/fs/list", params={"path": "relative"}).status_code == 400
    missing = extra_root / "definitely-not-here"
    assert client.get("/api/fs/list", params={"path": str(missing)}).status_code == 400


def test_symlink_escape_not_listed(client, extra_root, tmp_path):
    """A symlink under the allowed root pointing outside it must not be listed."""
    outside = tmp_path / "outside"
    outside.mkdir()
    link = extra_root / "fs-escape-link"
    link.symlink_to(outside)
    r = client.get("/api/fs/list", params={"path": str(extra_root)})
    assert r.status_code == 200
    assert str(link) not in r.json()["folders"]


def test_import_root_allowed_when_present(client, tmp_path):
    """/import is allowed when it exists (may be empty on this host)."""
    if not Path("/import").is_dir():
        pytest.skip("/import not present on this host")
    r = client.get("/api/fs/list", params={"path": "/import"})
    assert r.status_code == 200


def test_extra_root_rejected_without_the_env_var(tmp_path, monkeypatch):
    """L3 regression: a deployment that never sets FINMETA_FS_BROWSE_ROOTS
    (i.e. every real production container) must not expose any extra root --
    it used to be hardcoded into the allowed roots regardless of environment."""
    monkeypatch.setenv("JELLYFIN_API_KEY", "fake-key")
    monkeypatch.setenv("FINMETA_AUTH", "off")
    monkeypatch.delenv("FINMETA_FS_BROWSE_ROOTS", raising=False)
    not_allowed = tmp_path / "not-allowed"
    not_allowed.mkdir()
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "config.yml").write_text(CONFIG_YAML)
    ctx = AppContext(
        str(cfg_dir / "config.yml"), config_dir=str(cfg_dir),
        db_path=str(cfg_dir / "jobs.db"), overrides_path=str(cfg_dir / "overrides.db"),
        plan_builder=lambda *a, **k: WritePlan(items=[]),
    )
    app = create_app(ctx)
    with TestClient(app) as c:
        r = c.get("/api/fs/list", params={"path": str(not_allowed)})
    assert r.status_code == 400
