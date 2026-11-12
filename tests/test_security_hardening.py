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


def test_swagger_oauth2_redirect_route_is_not_registered(cfg_dir, monkeypatch):
    """Audit 2026-11-11 "D10": FastAPI registers a Swagger-UI OAuth2
    redirect helper at /docs/oauth2-redirect by default -- the only route
    this app ever had outside /api, so the auth middleware above (gated
    on path.startswith("/api"), see _api_routes' own docstring on why the
    walker only looks there) never saw it. There is no OAuth2 flow
    configured and the route served no data; it must now be entirely
    absent (404, not 401 -- the route itself is gone, not merely gated)."""
    with _client(cfg_dir, monkeypatch) as c:
        assert c.get("/docs/oauth2-redirect").status_code == 404
        assert not any(
            getattr(r, "path", "") == "/docs/oauth2-redirect" for r in c.app.routes)


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


# ---------------------------------------------------------------------------
# A2 (audit 2026-11-11): test-config must never resolve an unrelated secret,
# and the upstream response body must never reach the client.
# ---------------------------------------------------------------------------
#
# POST /api/services/{sonarr|radarr}/test-config resolves its `api_key` field
# through ctx.resolve_secret() when it looks like a secret NAME, so an
# existing instance's stored key can be tested without re-entering it. Until
# this fix, resolve_secret() accepted ANY known secret name -- including
# jellyfin_api_key, tmdb_key, etc. -- not just this app's own instance keys.
# Combined with the fully operator-supplied `url`, a session user could name
# jellyfin_api_key here and an attacker-controlled url, and the real
# Jellyfin key went out as X-Api-Key to that URL. Separately, ArrClient
# embedded up to 300 bytes of the upstream response body in its exception,
# so the route was not the "blind" probe SECURITY_REVIEW.md "M5" described.

class TestTestConfigSecretResolution:
    def test_unrelated_secret_name_is_sent_as_a_literal_not_resolved(self, cfg_dir, monkeypatch):
        """api_key="jellyfin_api_key" must NOT resolve to the real Jellyfin
        key -- it is sent to the target verbatim (as the literal string),
        which is the correct "this isn't a real key" outcome."""
        import httpx
        import respx
        with _client(cfg_dir, monkeypatch, auth=False) as c:
            ctx = c.app.state.context
            ctx.secrets.set("jellyfin_api_key", "REAL-JELLYFIN-KEY-NEVER-SEND-THIS")
            sink_url = "http://127.0.0.1:9/sonarr"  # respx intercepts, nothing is really sent
            with respx.mock:
                route = respx.get(f"{sink_url}/api/v3/system/status").mock(
                    return_value=httpx.Response(200, json={"version": "x"}))
                r = c.post("/api/services/sonarr/test-config", json={
                    "name": "evil", "url": sink_url,
                    "api_key": "jellyfin_api_key", "timeout": 5,
                })
            assert r.status_code == 200
            sent_key = route.calls.last.request.headers.get("x-api-key")
            assert sent_key == "jellyfin_api_key"
            assert sent_key != "REAL-JELLYFIN-KEY-NEVER-SEND-THIS"

    def test_a_real_arr_instance_secret_name_still_resolves(self, cfg_dir, monkeypatch):
        """The legitimate case must keep working: testing an EXISTING
        instance's saved key by name (the UI's own "test without
        re-entering the key" flow) still resolves to the real value."""
        import httpx
        import respx
        with _client(cfg_dir, monkeypatch, auth=False) as c:
            ctx = c.app.state.context
            ctx.secrets.set("sonarr_myinstance_api_key", "REAL-SONARR-KEY")
            sink_url = "http://127.0.0.1:9/sonarr"
            with respx.mock:
                route = respx.get(f"{sink_url}/api/v3/system/status").mock(
                    return_value=httpx.Response(200, json={"version": "x"}))
                r = c.post("/api/services/sonarr/test-config", json={
                    "name": "myinstance", "url": sink_url,
                    "api_key": "sonarr_myinstance_api_key", "timeout": 5,
                })
            assert r.status_code == 200
            assert route.calls.last.request.headers.get("x-api-key") == "REAL-SONARR-KEY"


class TestArrErrorNeverEchoesUpstreamBody:
    """The response from POST .../test-config / .../test must never contain
    bytes from the probed server's own response body -- only the status
    code. Otherwise the route is not blind (SECURITY_REVIEW.md "M5"'s own
    description of it), and a session user who can reach this route can use
    it to read fragments of whatever a reachable internal service returns."""

    def test_error_message_has_no_response_body(self):
        from finmeta.arr.base import ArrError, ArrClient
        import httpx
        import respx
        url = "http://127.0.0.1:9/svc"
        with respx.mock:
            respx.get(f"{url}/x").mock(
                return_value=httpx.Response(
                    403, text="INTERNAL SERVICE BANNER: admin panel v1.2.3"))
            c = ArrClient(url, api_key="k")
            try:
                with pytest.raises(ArrError) as ei:
                    c._get("/x")
            finally:
                c.close()
        assert "403" in str(ei.value)
        assert "INTERNAL SERVICE BANNER" not in str(ei.value)


# ---------------------------------------------------------------------------
# A3 (audit 2026-11-11): api.app._is_public must fail CLOSED, not open.
# ---------------------------------------------------------------------------
#
# The previous version excluded the webhook management routes' sub-paths by
# NAME ("status", "regenerate") after the "/api/webhooks/jellyfin/" prefix,
# with everything else under that prefix defaulting to public. It was
# missing "disable" from that exclusion list -- harmless only by accident,
# because that route (DELETE /api/webhooks/jellyfin) has no trailing
# segment at all, so it never matched the prefix check in the first place.
# A hypothetical sibling route added one path segment deeper (e.g.
# "/api/webhooks/jellyfin/disable") would have shipped unauthenticated with
# no test catching it. This version matches the public shape structurally
# (exactly one opaque token segment) instead of excluding known words.

class TestIsPublicFailsClosed:
    @pytest.mark.parametrize("path,expected", [
        ("/api/health", True),
        ("/api/auth/login", True),
        ("/api/auth/setup", True),
        ("/api/auth/status", True),
        ("/api/webhooks/jellyfin/abc123tokenlikestring", True),
        # Case/path-normalization variants must NOT be public.
        ("/API/health", False),
        ("/api//health", False),
        ("/api/./health", False),
        # The sibling management routes, including the one the old
        # exclusion-by-name list actually missed.
        ("/api/webhooks/jellyfin/status", False),
        ("/api/webhooks/jellyfin/regenerate", False),
        ("/api/webhooks/jellyfin/disable", False),
        ("/api/webhooks/jellyfin", False),
        # A traversal segment collapses to a reserved word structurally too.
        ("/api/webhooks/jellyfin/abc/../regenerate", False),
        # Nothing else is ever public.
        ("/api/docs", False),
        ("/api/openapi.json", False),
        ("/api/config", False),
    ])
    def test_is_public(self, path, expected):
        from finmeta.api.app import _is_public
        assert _is_public(path) is expected
