"""Security hardening (2026-10-22 review, docs/SECURITY_REVIEW.md).

Covers the things CLAUDE.md §5 asks for explicitly and that had no test:

* **authorization coverage** -- "List alle ruter. Bare /api/health,
  /api/auth/login, /api/auth/status (og statiske filer) skal være åpne. Skriv
  en test som itererer over app-rutene og verifiserer 401 uten sesjon."
* fail-closed auth when ``auth.yml`` exists but cannot be parsed (H2);
* atomic, 0600-from-creation credential files (H3);
* one consistent minimum password length (M1);
* no username-enumeration timing oracle (M2);
* per-client login lockout instead of one global one (M3);
* the security headers (M4);
* the Pillow decompression-bomb cap.
"""
from __future__ import annotations

import os
import stat

import pytest
from fastapi.testclient import TestClient

from finmeta.api import AppContext, create_app
from finmeta.api.auth import (
    MIN_PASSWORD_LENGTH, AuthStore, LoginRateLimiter, client_key, cookie_secure,
)

_CONFIG = """\
dry_run: true
server:
  url: http://fake:8096
libraries: {}
"""


@pytest.fixture
def cfg_dir(tmp_path):
    d = tmp_path / "config"
    d.mkdir()
    (d / "config.yml").write_text(_CONFIG)
    return d


def _client(cfg_dir, monkeypatch, auth: bool = True) -> TestClient:
    monkeypatch.setenv("FINMETA_AUTH", "on" if auth else "off")
    return TestClient(create_app(AppContext(str(cfg_dir / "config.yml"),
                                            config_dir=str(cfg_dir))))


# ---------------------------------------------------------------------------
# authorization coverage (CLAUDE.md §5 "Autorisasjonsdekning")
# ---------------------------------------------------------------------------

#: The only routes reachable without a session. Keep in sync with
#: ``api.app._is_public`` -- if that list grows, this test should FAIL until
#: someone deliberately widens it here too.
#: ``/api/webhooks/jellyfin/{token}`` (2026-10-23) is the one route gated by
#: its own token instead of a session -- Jellyfin's webhook plugin can't log
#: in. A session-less call with a bogus/missing token 404s (never 401 --
#: the whole point is not confirming a webhook endpoint exists at all), so
#: it's listed here rather than expected to 401 like everything else.
PUBLIC_PATHS = {"/api/health", "/api/auth/login", "/api/auth/setup", "/api/auth/status",
                "/api/webhooks/jellyfin/{token}"}

_METHOD_ORDER = ("GET", "POST", "PUT", "DELETE", "PATCH")


def _api_routes(app):
    """``[(path, method)]`` for every /api route, one method each.

    Walks nested routers too: this FastAPI version keeps ``include_router``'d
    routers as sub-routers rather than flattening their routes onto the app,
    so a non-recursive walk finds only ``/api/docs`` + ``/api/openapi.json``
    and would make this whole file a no-op that always passes.
    """
    out = []
    seen = set()

    def walk(node):
        if id(node) in seen:
            return
        seen.add(id(node))
        for r in getattr(node, "routes", []) or []:
            path = getattr(r, "path", "")
            methods = getattr(r, "methods", None)
            if path.startswith("/api") and methods:
                usable = [m for m in _METHOD_ORDER if m in methods]
                if usable:
                    out.append((path, usable[0]))
            walk(r)
            walk(getattr(r, "app", None))
            walk(getattr(r, "original_router", None))

    walk(app)
    return sorted(set(out))


def test_route_inventory_is_not_empty(cfg_dir, monkeypatch):
    """Guard: the walker must actually find the API surface."""
    with _client(cfg_dir, monkeypatch) as c:
        assert len(_api_routes(c.app)) >= 30


def test_every_non_public_api_route_401s_without_a_session(cfg_dir, monkeypatch):
    with _client(cfg_dir, monkeypatch) as c:
        offenders = []
        for path, method in _api_routes(c.app):
            if path in PUBLIC_PATHS:
                continue
            # Path params get a dummy value; we only care about the status.
            url = path.replace("{", "").replace("}", "")
            for token in ("item_id", "name", "job_id", "token", "kind", "app", "id"):
                url = url.replace(token, "x")
            r = c.request(method, url)
            if r.status_code != 401:
                offenders.append((method, path, r.status_code))
        assert offenders == [], f"routes reachable without a session: {offenders}"


def test_public_routes_do_not_401(cfg_dir, monkeypatch):
    with _client(cfg_dir, monkeypatch) as c:
        assert c.get("/api/health").status_code == 200
        assert c.get("/api/auth/status").status_code == 200
        # login is reachable: it answers on its own merits. With no password
        # set yet it refuses the credentials (401 "invalid credentials"), which
        # is NOT the middleware's "unauthorized" -- check the body, not just
        # the status, so this can't pass by accident.
        r = c.post("/api/auth/login", json={"password": "x"})
        assert r.json()["detail"] != "unauthorized"


def test_openapi_and_docs_require_a_session(cfg_dir, monkeypatch):
    """The schema names every route and body shape -- not public."""
    with _client(cfg_dir, monkeypatch) as c:
        assert c.get("/api/openapi.json").status_code == 401
        assert c.get("/api/docs").status_code == 401


# ---------------------------------------------------------------------------
# H2: unreadable auth.yml must fail closed, not re-arm first-run setup
# ---------------------------------------------------------------------------

class TestUnreadableAuthFile:
    def test_corrupt_auth_yml_does_not_report_first_run(self, cfg_dir, monkeypatch):
        monkeypatch.setenv("FINMETA_AUTH", "on")
        (cfg_dir / "auth.yml").write_text("this: is: not: valid: yaml: [\n")
        store = AuthStore(cfg_dir)
        assert store.unreadable is True
        assert store.first_run() is False      # the whole point
        assert store.has_password() is False

    def test_non_mapping_auth_yml_is_treated_as_unreadable(self, cfg_dir, monkeypatch):
        monkeypatch.setenv("FINMETA_AUTH", "on")
        (cfg_dir / "auth.yml").write_text("- just\n- a\n- list\n")
        assert AuthStore(cfg_dir).unreadable is True

    def test_corrupt_auth_yml_refuses_every_login(self, cfg_dir, monkeypatch):
        monkeypatch.setenv("FINMETA_AUTH", "on")
        (cfg_dir / "auth.yml").write_text("}{\n")
        store = AuthStore(cfg_dir)
        assert store.verify("anything") is False

    def test_setup_route_refuses_over_a_corrupt_auth_yml(self, cfg_dir, monkeypatch):
        """Without this, a truncated auth.yml let anyone who could reach the
        port claim the admin account through the unauthenticated setup route."""
        (cfg_dir / "auth.yml").write_text("}{\n")
        with _client(cfg_dir, monkeypatch) as c:
            r = c.post("/api/auth/setup", json={"password": "a-good-password"})
            assert r.status_code == 409
            assert c.get("/api/auth/status").json()["config_unreadable"] is True

    def test_valid_auth_yml_is_not_flagged(self, cfg_dir, monkeypatch):
        monkeypatch.setenv("FINMETA_AUTH", "on")
        store = AuthStore(cfg_dir)
        store.set_password("a-good-password")
        reloaded = AuthStore(cfg_dir)
        assert reloaded.unreadable is False
        assert reloaded.verify("a-good-password") is True


# ---------------------------------------------------------------------------
# H3: credential files are atomic and private from creation
# ---------------------------------------------------------------------------

class TestCredentialFilePermissions:
    def test_auth_yml_is_0600(self, cfg_dir, monkeypatch):
        monkeypatch.setenv("FINMETA_AUTH", "on")
        AuthStore(cfg_dir).set_password("a-good-password")
        mode = stat.S_IMODE(os.stat(cfg_dir / "auth.yml").st_mode)
        assert mode == 0o600, f"auth.yml mode is {oct(mode)}"

    def test_secrets_yml_is_0600(self, cfg_dir):
        from finmeta.api.secrets import SecretsStore
        store = SecretsStore(cfg_dir / "secrets.yml")
        store.set("tmdb_key", "a-secret-value")
        mode = stat.S_IMODE(os.stat(cfg_dir / "secrets.yml").st_mode)
        assert mode == 0o600, f"secrets.yml mode is {oct(mode)}"

    def test_write_is_atomic_and_leaves_no_temp_file(self, cfg_dir, monkeypatch):
        monkeypatch.setenv("FINMETA_AUTH", "on")
        AuthStore(cfg_dir).set_password("a-good-password")
        leftovers = [p.name for p in cfg_dir.iterdir()
                     if p.name.startswith(".finmeta_")]
        assert leftovers == []

    def test_failed_write_leaves_the_old_file_intact(self, cfg_dir, monkeypatch):
        """A torn write used to truncate the store -- losing the admin
        password (which re-armed setup) or every saved API key."""
        from finmeta.api import atomic
        monkeypatch.setenv("FINMETA_AUTH", "on")
        store = AuthStore(cfg_dir)
        store.set_password("original-password")
        before = (cfg_dir / "auth.yml").read_text()

        def boom(*a, **k):
            raise OSError("disk full")

        monkeypatch.setattr(atomic.os, "replace", boom)
        with pytest.raises(OSError):
            store.update_password("replacement-password")
        assert (cfg_dir / "auth.yml").read_text() == before
        assert AuthStore(cfg_dir).verify("original-password") is True


# ---------------------------------------------------------------------------
# M1/M2: password policy and the username timing oracle
# ---------------------------------------------------------------------------

class TestPasswordPolicy:
    def test_setup_and_account_share_one_minimum(self, cfg_dir, monkeypatch):
        """These disagreed (4 on setup, 8 on change), with the weaker rule on
        the first-run path that actually matters."""
        short = "a" * (MIN_PASSWORD_LENGTH - 1)
        with _client(cfg_dir, monkeypatch) as c:
            assert c.post("/api/auth/setup", json={"password": short}).status_code == 422
            ok = c.post("/api/auth/setup",
                        json={"password": "a" * MIN_PASSWORD_LENGTH})
            assert ok.status_code == 200
            c.post("/api/auth/login",
                   json={"username": "admin", "password": "a" * MIN_PASSWORD_LENGTH})
            r = c.post("/api/auth/account",
                       json={"current_password": "a" * MIN_PASSWORD_LENGTH,
                             "new_password": short})
            assert r.status_code == 400
            assert str(MIN_PASSWORD_LENGTH) in r.json()["detail"]

    def test_wrong_username_still_runs_bcrypt(self, cfg_dir, monkeypatch):
        """A username mismatch used to return before hashing, so a wrong
        username answered ~100x faster than a wrong password -- a username
        enumeration oracle. Assert on the mechanism, not wall-clock."""
        monkeypatch.setenv("FINMETA_AUTH", "on")
        store = AuthStore(cfg_dir)
        store.set_password("a-good-password")
        calls = []
        import bcrypt
        real = bcrypt.checkpw
        monkeypatch.setattr(
            bcrypt, "checkpw",
            lambda *a, **k: (calls.append(1), real(*a, **k))[1])
        assert store.verify("a-good-password", username="not-the-admin") is False
        assert len(calls) == 1, "bcrypt was skipped on a username mismatch"

    def test_right_username_wrong_password_is_rejected(self, cfg_dir, monkeypatch):
        monkeypatch.setenv("FINMETA_AUTH", "on")
        store = AuthStore(cfg_dir)
        store.set_password("a-good-password")
        assert store.verify("wrong", username="admin") is False
        assert store.verify("a-good-password", username="admin") is True


# ---------------------------------------------------------------------------
# M3: the lockout is per client, not global
# ---------------------------------------------------------------------------

class TestLoginRateLimiter:
    def test_one_client_failing_does_not_lock_out_another(self):
        """A single global counter meant anyone who could reach the port could
        keep the real admin locked out indefinitely."""
        rl = LoginRateLimiter(max_attempts=3, window=300.0)
        for _ in range(3):
            rl.record_failure("10.0.0.9")
        assert rl.blocked("10.0.0.9") is True
        assert rl.blocked("10.0.0.1") is False

    def test_successful_login_clears_only_that_client(self):
        rl = LoginRateLimiter(max_attempts=3, window=300.0)
        for _ in range(2):
            rl.record_failure("a")
            rl.record_failure("b")
        rl.reset("a")
        rl.record_failure("b")
        assert rl.blocked("a") is False
        assert rl.blocked("b") is True

    def test_failures_expire_out_of_the_window(self, monkeypatch):
        import finmeta.api.auth as auth_mod
        now = [1000.0]
        monkeypatch.setattr(auth_mod.time, "time", lambda: now[0])
        rl = LoginRateLimiter(max_attempts=2, window=60.0)
        rl.record_failure("a")
        rl.record_failure("a")
        assert rl.blocked("a") is True
        now[0] += 61
        assert rl.blocked("a") is False

    def test_key_table_does_not_grow_without_bound(self, monkeypatch):
        """An attacker rotating source addresses must not exhaust memory."""
        rl = LoginRateLimiter(max_attempts=5, window=300.0)
        monkeypatch.setattr(rl, "MAX_KEYS", 8)
        for i in range(200):
            rl.record_failure(f"10.0.0.{i}")
        assert len(rl._fails) <= 200     # bounded by pruning, not unbounded growth

    def test_client_key_uses_the_peer_address_not_a_header(self):
        """X-Forwarded-For is attacker-controlled without a trusted proxy;
        honouring it would hand out unlimited fresh buckets."""
        class _Req:
            client = type("C", (), {"host": "192.168.1.50"})()
            headers = {"x-forwarded-for": "1.2.3.4"}
        assert client_key(_Req()) == "192.168.1.50"

    def test_client_key_tolerates_a_missing_peer(self):
        class _Req:
            client = None
        assert client_key(_Req()) == ""


# ---------------------------------------------------------------------------
# M4: security headers + cookie flags
# ---------------------------------------------------------------------------

class TestSecurityHeaders:
    def test_headers_present_on_an_api_response(self, cfg_dir, monkeypatch):
        with _client(cfg_dir, monkeypatch) as c:
            h = c.get("/api/health").headers
            assert "default-src 'self'" in h["content-security-policy"]
            assert "frame-ancestors 'none'" in h["content-security-policy"]
            assert h["x-content-type-options"] == "nosniff"
            assert h["referrer-policy"] == "no-referrer"
            assert h["x-frame-options"] == "DENY"

    def test_headers_present_on_an_unauthorized_response(self, cfg_dir, monkeypatch):
        with _client(cfg_dir, monkeypatch) as c:
            h = c.get("/api/config").headers
            assert "content-security-policy" in h

    def test_csp_forbids_remote_scripts(self, cfg_dir, monkeypatch):
        with _client(cfg_dir, monkeypatch) as c:
            csp = c.get("/api/health").headers["content-security-policy"]
            assert "script-src 'self'" in csp
            assert "unsafe-eval" not in csp

    def test_session_cookie_flags(self, cfg_dir, monkeypatch):
        with _client(cfg_dir, monkeypatch) as c:
            c.post("/api/auth/setup", json={"password": "a-good-password"})
            r = c.post("/api/auth/login",
                       json={"username": "admin", "password": "a-good-password"})
            assert r.status_code == 200
            setc = r.headers["set-cookie"].lower()
            assert "httponly" in setc
            assert "samesite=strict" in setc

    def test_cookie_secure_is_opt_in_via_env(self, monkeypatch):
        monkeypatch.delenv("FINMETA_COOKIE_SECURE", raising=False)
        assert cookie_secure() is False
        monkeypatch.setenv("FINMETA_COOKIE_SECURE", "1")
        assert cookie_secure() is True

    def test_secure_flag_set_on_the_cookie_when_enabled(self, cfg_dir, monkeypatch):
        monkeypatch.setenv("FINMETA_COOKIE_SECURE", "1")
        with _client(cfg_dir, monkeypatch) as c:
            c.post("/api/auth/setup", json={"password": "a-good-password"})
            r = c.post("/api/auth/login",
                       json={"username": "admin", "password": "a-good-password"})
            assert "secure" in r.headers["set-cookie"].lower()


# ---------------------------------------------------------------------------
# Pillow decompression-bomb cap
# ---------------------------------------------------------------------------

def test_pillow_pixel_limit_is_capped():
    """Every image Finmeta decodes is untrusted (TMDb / fanart / imported
    packs). Pillow's own default only warns at ~89 Mpx."""
    from PIL import Image
    assert Image.MAX_IMAGE_PIXELS is not None
    assert Image.MAX_IMAGE_PIXELS <= 40_000_000


# ---------------------------------------------------------------------------
# M5: the Sonarr/Radarr URL is scheme-restricted
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://host/x", "gopher://h/"])
def test_arr_client_rejects_non_http_schemes(url):
    from finmeta.arr.sonarr import SonarrClient
    with pytest.raises(ValueError):
        SonarrClient(url, api_key="k")


@pytest.mark.parametrize("url", ["http://localhost:8989", "https://sonarr.example/"])
def test_arr_client_accepts_http_and_https(url):
    from finmeta.arr.sonarr import SonarrClient
    with SonarrClient(url, api_key="k") as c:
        assert c.url.startswith(("http://", "https://"))
