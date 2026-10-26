"""Unit + API tests for auth (S9 §A)."""
import pytest
from fastapi.testclient import TestClient

from finmeta.api import AppContext, create_app
from finmeta.api.auth import (
    AuthStore, SessionManager, LoginRateLimiter, auth_enabled,
)


# ---- unit: AuthStore ----
def test_auth_store_first_run(tmp_path, monkeypatch):
    monkeypatch.setenv("FINMETA_AUTH", "on")
    a = AuthStore(tmp_path)
    assert a.first_run() is True and not a.has_password()
    a.set_password("hunter22")
    assert a.has_password() and a.first_run() is False
    assert a.verify("hunter22") and not a.verify("nope")


def test_auth_store_file_0600_and_hashed(tmp_path):
    a = AuthStore(tmp_path)
    a.set_password("hunter22")
    assert (a.path.stat().st_mode & 0o777) == 0o600
    assert "hunter22" not in a.path.read_text()


def test_auth_store_reload(tmp_path):
    AuthStore(tmp_path).set_password("hunter22")
    assert AuthStore(tmp_path).verify("hunter22")


def test_auth_enabled_flag(monkeypatch):
    monkeypatch.setenv("FINMETA_AUTH", "off")
    assert auth_enabled() is False
    monkeypatch.setenv("FINMETA_AUTH", "on")
    assert auth_enabled() is True
    monkeypatch.delenv("FINMETA_AUTH")
    assert auth_enabled() is True
    monkeypatch.setenv("FINMETA_AUTH", "0")
    assert auth_enabled() is False


# ---- unit: sessions + rate limiter ----
def test_session_manager():
    sm = SessionManager()
    t = sm.create()
    assert sm.valid(t) and not sm.valid("bogus")
    sm.destroy(t)
    assert not sm.valid(t)


def test_rate_limiter():
    rl = LoginRateLimiter(max_attempts=3, window=60)
    assert not rl.blocked()
    for _ in range(3):
        rl.record_failure()
    assert rl.blocked()
    rl.reset()
    assert not rl.blocked()


# ---- API: auth on ----
CFG = "dry_run: true\nserver:\n  url: http://fake\nlibraries:\n  Movies:\n    type: movies\n"


@pytest.fixture
def auth_ctx(tmp_path, monkeypatch):
    monkeypatch.setenv("FINMETA_AUTH", "on")
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "config.yml").write_text(CFG)
    return AppContext(str(cfg_dir / "config.yml"), config_dir=str(cfg_dir),
                      db_path=str(cfg_dir / "j.db"), overrides_path=str(cfg_dir / "o.db"),
                      plan_builder=lambda *a, **k: None)


@pytest.fixture
def auth_client(auth_ctx):
    app = create_app(auth_ctx)
    with TestClient(app) as c:
        yield c


def test_api_auth_protects_routes(auth_client):
    assert auth_client.get("/api/health").status_code == 200
    assert auth_client.get("/api/config").status_code == 401
    assert auth_client.get("/api/secrets").status_code == 401
    assert auth_client.get("/api/auth/status").status_code == 200


def test_api_auth_first_run_setup_login(auth_client, auth_ctx):
    assert auth_ctx.auth.first_run() is True
    assert auth_client.post("/api/auth/setup", json={"password": "s3cretpw"}).status_code == 200
    assert auth_client.post("/api/auth/setup", json={"password": "another-pw"}).status_code == 409
    assert auth_client.post("/api/auth/login", json={"password": "bad"}).status_code == 401
    assert auth_client.post("/api/auth/login", json={"password": "s3cretpw"}).status_code == 200
    assert auth_client.get("/api/config").status_code == 200
    assert auth_client.get("/api/auth/me").json() == {"username": "admin"}


def test_api_auth_logout(auth_client):
    auth_client.post("/api/auth/setup", json={"password": "s3cretpw"})
    auth_client.post("/api/auth/login", json={"password": "s3cretpw"})
    assert auth_client.get("/api/config").status_code == 200
    assert auth_client.post("/api/auth/logout").status_code == 200
    assert auth_client.get("/api/config").status_code == 401


# ---- API: account (username + password change) ----
def test_account_change_username_and_password(auth_client, auth_ctx):
    auth_client.post("/api/auth/setup", json={"password": "s3cretpw"})
    assert auth_client.post("/api/auth/login", json={"username": "admin", "password": "s3cretpw"}).status_code == 200
    r = auth_client.post("/api/auth/account", json={
        "current_password": "s3cretpw", "new_username": "operator", "new_password": "n3wpass99"})
    assert r.status_code == 200 and r.json() == {"ok": True, "username": "operator"}
    assert auth_ctx.auth.username == "operator"
    assert auth_client.get("/api/auth/me").json() == {"username": "operator"}
    # old username no longer works, old password no longer works
    assert auth_client.post("/api/auth/login", json={"username": "admin", "password": "n3wpass99"}).status_code == 401
    assert auth_client.post("/api/auth/login", json={"username": "operator", "password": "s3cretpw"}).status_code == 401
    # new username + new password works
    assert auth_client.post("/api/auth/login", json={"username": "operator", "password": "n3wpass99"}).status_code == 200


def test_account_wrong_current_password_is_400_not_401(auth_client):
    auth_client.post("/api/auth/setup", json={"password": "s3cretpw"})
    assert auth_client.post("/api/auth/login", json={"username": "admin", "password": "s3cretpw"}).status_code == 200
    r = auth_client.post("/api/auth/account", json={"current_password": "wrongpw1"})
    assert r.status_code == 400
    assert "current password" in r.json()["detail"]
    # session is still valid (not logged out) and nothing changed
    assert auth_client.get("/api/config").status_code == 200


def test_account_validation(auth_client):
    auth_client.post("/api/auth/setup", json={"password": "s3cretpw"})
    assert auth_client.post("/api/auth/login", json={"username": "admin", "password": "s3cretpw"}).status_code == 200
    # new password too short
    r = auth_client.post("/api/auth/account", json={
        "current_password": "s3cretpw", "new_password": "short"})
    assert r.status_code == 400 and "8 characters" in r.json()["detail"]
    # empty new username rejected
    r = auth_client.post("/api/auth/account", json={
        "current_password": "s3cretpw", "new_username": "   "})
    assert r.status_code == 400
    # password-only change keeps the username
    r = auth_client.post("/api/auth/account", json={
        "current_password": "s3cretpw", "new_password": "n3wpass99"})
    assert r.status_code == 200 and r.json()["username"] == "admin"
    assert auth_client.get("/api/auth/me").json() == {"username": "admin"}


def test_login_requires_username_and_password(auth_client):
    auth_client.post("/api/auth/setup", json={"password": "s3cretpw"})
    # no username at all -> default admin still matches
    assert auth_client.post("/api/auth/login", json={"password": "s3cretpw"}).status_code == 200
    assert auth_client.post("/api/auth/login", json={"username": "nobody", "password": "s3cretpw"}).status_code == 401
    assert auth_client.post("/api/auth/login", json={"username": "admin", "password": "bad"}).status_code == 401


def test_account_requires_auth(auth_client):
    assert auth_client.post("/api/auth/account", json={"current_password": "x"}).status_code == 401


def test_api_auth_rate_limit(auth_client):
    auth_client.post("/api/auth/setup", json={"password": "s3cretpw"})
    for _ in range(5):
        assert auth_client.post("/api/auth/login", json={"password": "bad"}).status_code == 401
    # blocked now, even with the correct password
    assert auth_client.post("/api/auth/login", json={"password": "s3cretpw"}).status_code == 429


def test_api_auth_off_allows_without_login(tmp_path, monkeypatch):
    monkeypatch.setenv("FINMETA_AUTH", "off")
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "config.yml").write_text(CFG)
    ctx = AppContext(str(cfg_dir / "config.yml"), config_dir=str(cfg_dir),
                     db_path=str(cfg_dir / "j.db"), overrides_path=str(cfg_dir / "o.db"),
                     plan_builder=lambda *a, **k: None)
    app = create_app(ctx)
    with TestClient(app) as c:
        assert c.get("/api/config").status_code == 200
        assert c.get("/api/auth/status").json()["auth_enabled"] is False
