"""Proxy ref policy (2026-10-22 security fix, docs/SECURITY_REVIEW.md "H1").

``GET /api/proxy/{token}`` takes a base64 token of ``"<kind>|<ref>"`` that the
UI mints client-side -- it is encoding, not authentication, so the ref cannot
be trusted. Before this fix ``fetch`` happily read any absolute path the
caller named (``secrets.yml`` included -- and the redaction middleware never
sees it, because the response is not JSON) and fetched any URL, giving an
authenticated session an arbitrary-file-read plus an SSRF primitive.

These tests pin the policy itself: what must be refused, what must keep
working, and that the refusal is not an existence oracle.
"""
from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from finmeta.api import proxy as proxy_mod
from finmeta.api.proxy import MAX_FETCH_BYTES, RefNotAllowed, encode_token, fetch


# ---------------------------------------------------------------------------
# local refs
# ---------------------------------------------------------------------------

class TestLocalRefPolicy:
    def test_ref_inside_an_allowed_root_is_served(self, tmp_path):
        root = tmp_path / "assets"
        (root / "123").mkdir(parents=True)
        poster = root / "123" / "poster.jpg"
        poster.write_bytes(b"JPEGBYTES")
        data, ctype = fetch("poster", str(poster), allowed_roots=[root])
        assert data == b"JPEGBYTES"
        assert ctype == "image/jpeg"

    def test_ref_outside_every_allowed_root_is_refused(self, tmp_path):
        allowed = tmp_path / "assets"
        allowed.mkdir()
        victim = tmp_path / "secrets.yml"
        victim.write_text("tmdb_key: CANARY\n")
        with pytest.raises(RefNotAllowed):
            fetch("metadata", str(victim), allowed_roots=[allowed])

    def test_dotdot_traversal_out_of_an_allowed_root_is_refused(self, tmp_path):
        allowed = tmp_path / "assets"
        allowed.mkdir()
        victim = tmp_path / "secrets.yml"
        victim.write_text("tmdb_key: CANARY\n")
        sneaky = str(allowed / ".." / "secrets.yml")
        with pytest.raises(RefNotAllowed):
            fetch("metadata", sneaky, allowed_roots=[allowed])

    def test_symlink_escape_out_of_an_allowed_root_is_refused(self, tmp_path):
        allowed = tmp_path / "assets"
        allowed.mkdir()
        victim = tmp_path / "secrets.yml"
        victim.write_text("tmdb_key: CANARY\n")
        link = allowed / "innocent.jpg"
        link.symlink_to(victim)
        with pytest.raises(RefNotAllowed):
            fetch("poster", str(link), allowed_roots=[allowed])

    def test_no_allowed_roots_denies_every_local_ref(self, tmp_path):
        """Fail closed: a caller that forgets the policy gets no filesystem
        access, not unrestricted access."""
        f = tmp_path / "poster.jpg"
        f.write_bytes(b"x")
        with pytest.raises(RefNotAllowed):
            fetch("poster", str(f))
        with pytest.raises(RefNotAllowed):
            fetch("poster", str(f), allowed_roots=[])

    def test_relative_ref_is_refused(self, tmp_path):
        with pytest.raises(RefNotAllowed):
            fetch("poster", "relative/poster.jpg", allowed_roots=[tmp_path])

    def test_allowed_but_absent_ref_is_filenotfound_not_policy(self, tmp_path):
        """An allowed-root miss must still be distinguishable from a refusal,
        so the route keeps answering 404 (not 403) for a stale candidate."""
        with pytest.raises(FileNotFoundError):
            fetch("poster", str(tmp_path / "gone.jpg"), allowed_roots=[tmp_path])


# ---------------------------------------------------------------------------
# remote refs
# ---------------------------------------------------------------------------

class TestRemoteRefPolicy:
    def test_allowed_provider_host_is_fetched(self, monkeypatch):
        url = "https://image.tmdb.org/t/p/original/abc.jpg"

        def handler(request):
            assert str(request.url) == url
            return httpx.Response(200, content=b"PNGDATA")

        transport = httpx.MockTransport(handler)
        real_client = httpx.Client
        monkeypatch.setattr(
            proxy_mod.httpx, "Client",
            lambda **kw: real_client(**{**kw, "transport": transport}))
        data, _ = fetch("poster", url)
        assert data == b"PNGDATA"

    @pytest.mark.parametrize("url", [
        "http://169.254.169.254/latest/meta-data/",   # cloud metadata
        "http://127.0.0.1:8096/System/Info",          # the real Jellyfin
        "http://192.168.1.10:8989/api/v3/series",     # a LAN Sonarr instance
        "http://evil.example.com/x.jpg",
        "https://image.tmdb.org.evil.com/x.jpg",      # suffix-confusion
    ])
    def test_non_provider_host_is_refused(self, url):
        with pytest.raises(RefNotAllowed):
            fetch("poster", url)

    def test_host_allowed_accepts_subdomains_only_under_the_allowlist(self):
        assert proxy_mod.host_allowed("image.tmdb.org")
        assert proxy_mod.host_allowed("assets.fanart.tv")
        assert not proxy_mod.host_allowed("tmdb.org.attacker.net")
        assert not proxy_mod.host_allowed("")
        assert not proxy_mod.host_allowed(None)

    def test_redirect_off_the_allowlist_is_refused(self, monkeypatch):
        """An allow-listed first hop says nothing about where it points."""
        def handler(request):
            if request.url.host == "image.tmdb.org":
                return httpx.Response(
                    302, headers={"location": "http://169.254.169.254/secret"})
            return httpx.Response(200, content=b"LEAKED")

        transport = httpx.MockTransport(handler)
        real_client = httpx.Client
        monkeypatch.setattr(
            proxy_mod.httpx, "Client",
            lambda **kw: real_client(**{**kw, "transport": transport}))
        with pytest.raises(RefNotAllowed):
            fetch("poster", "https://image.tmdb.org/t/p/original/a.jpg")

    def test_oversized_upstream_response_is_refused(self, monkeypatch):
        def handler(request):
            return httpx.Response(200, content=b"\0" * (MAX_FETCH_BYTES + 1))

        transport = httpx.MockTransport(handler)
        real_client = httpx.Client
        monkeypatch.setattr(
            proxy_mod.httpx, "Client",
            lambda **kw: real_client(**{**kw, "transport": transport}))
        with pytest.raises(RefNotAllowed):
            fetch("poster", "https://image.tmdb.org/t/p/original/huge.jpg")


# ---------------------------------------------------------------------------
# AppContext.proxy_roots: the policy the route actually passes in
# ---------------------------------------------------------------------------

class TestProxyRootsPolicy:
    def _ctx(self, tmp_path):
        from finmeta.api import AppContext
        cfg = tmp_path / "config.yml"
        cfg.write_text(
            "dry_run: true\n"
            "server:\n  url: http://fake:8096\n"
            "sources:\n  local:\n    enabled: true\n    path: "
            f"{tmp_path / 'local-assets'}\n"
            "path_mappings:\n"
            f"  - jellyfin: /media\n    local: {tmp_path / 'media'}\n"
            "libraries: {}\n"
        )
        return AppContext(str(cfg))

    def test_config_dir_itself_is_never_an_allowed_root(self, tmp_path):
        """The whole point: config_dir holds secrets.yml / auth.yml /
        config.yml, so only its artwork SUBdirectories may be served."""
        ctx = self._ctx(tmp_path)
        roots = [Path(r).resolve() for r in ctx.proxy_roots()]
        assert ctx.config_dir.resolve() not in roots

    def test_local_asset_dir_and_media_roots_are_allowed(self, tmp_path):
        ctx = self._ctx(tmp_path)
        roots = [Path(r) for r in ctx.proxy_roots()]
        assert tmp_path / "local-assets" in roots
        assert tmp_path / "media" in roots

    def test_artwork_lifecycle_subdirs_are_allowed(self, tmp_path):
        ctx = self._ctx(tmp_path)
        names = {Path(r).name for r in ctx.proxy_roots()}
        assert {"assets", "backups", "trash", "cache"} <= names

    def test_secrets_file_is_refused_under_the_real_policy(self, tmp_path):
        """End-to-end over the policy the route uses: a config-dir file that
        is NOT under an artwork subdir must be refused."""
        ctx = self._ctx(tmp_path)
        victim = ctx.config_dir / "secrets.yml"
        victim.write_text("tmdb_key: CANARY\n")
        with pytest.raises(RefNotAllowed):
            fetch("metadata", str(victim), allowed_roots=ctx.proxy_roots())


# ---------------------------------------------------------------------------
# the route itself
# ---------------------------------------------------------------------------

class TestProxyRoute:
    @pytest.fixture
    def client(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient
        from finmeta.api import AppContext, create_app
        monkeypatch.setenv("FINMETA_AUTH", "off")
        cfg = tmp_path / "config.yml"
        cfg.write_text(
            "dry_run: true\n"
            "server:\n  url: http://fake:8096\n"
            "sources:\n  local:\n    enabled: true\n    path: "
            f"{tmp_path / 'local-assets'}\n"
            "libraries: {}\n"
        )
        return TestClient(create_app(AppContext(str(cfg))))

    def test_disallowed_local_ref_returns_403_without_echoing_the_path(
            self, client, tmp_path):
        victim = tmp_path / "secrets.yml"
        victim.write_text("tmdb_key: CANARY\n")
        r = client.get(f"/api/proxy/{encode_token('metadata', str(victim))}")
        assert r.status_code == 403
        assert "CANARY" not in r.text
        assert str(victim) not in r.text      # no path echo / existence oracle

    def test_absent_file_in_an_allowed_root_still_returns_404(
            self, client, tmp_path):
        gone = tmp_path / "local-assets" / "1" / "poster.jpg"
        r = client.get(f"/api/proxy/{encode_token('poster', str(gone))}")
        assert r.status_code == 404

    def test_allowed_local_ref_is_served(self, client, tmp_path):
        p = tmp_path / "local-assets" / "1" / "poster.jpg"
        p.parent.mkdir(parents=True)
        p.write_bytes(b"REALPOSTER")
        r = client.get(f"/api/proxy/{encode_token('poster', str(p))}")
        assert r.status_code == 200
        assert r.content == b"REALPOSTER"

    def test_refusal_is_indistinguishable_for_existing_and_missing_paths(
            self, client, tmp_path):
        """Both outside the roots -> same status and body, so the route cannot
        be used to probe which host paths exist."""
        exists = tmp_path / "real.yml"
        exists.write_text("x\n")
        missing = tmp_path / "not-real.yml"
        r1 = client.get(f"/api/proxy/{encode_token('metadata', str(exists))}")
        r2 = client.get(f"/api/proxy/{encode_token('metadata', str(missing))}")
        assert r1.status_code == r2.status_code == 403
        assert r1.json() == r2.json()


class TestOverlayProxyRoute:
    """GET /api/proxy/overlay/{digest}: the read side of the overlay cache
    engine.overlay.overlay_file_write writes during build_plan -- the only
    way the UI's poster preview can show the overlaid render instead of
    source_ref's deliberately-pre-overlay original (see routes_candidates.py
    proxy_overlay's docstring)."""

    @pytest.fixture
    def client(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient
        from finmeta.api import AppContext, create_app
        monkeypatch.setenv("FINMETA_AUTH", "off")
        cfg = tmp_path / "config.yml"
        cfg.write_text(
            "dry_run: true\n"
            "server:\n  url: http://fake:8096\n"
            "libraries: {}\n"
        )
        return TestClient(create_app(AppContext(str(cfg))))

    def test_malformed_digest_is_404(self, client):
        r = client.get("/api/proxy/overlay/not-a-sha256")
        assert r.status_code == 404

    def test_uncached_digest_is_404(self, client):
        r = client.get("/api/proxy/overlay/" + "0" * 64)
        assert r.status_code == 404

    def test_cached_digest_is_served(self, client, tmp_path):
        from finmeta.engine.overlay import _write_overlay_cache
        digest = "a" * 64
        _write_overlay_cache(digest, b"OVERLAID-JPEG-BYTES", config_dir=tmp_path)
        r = client.get(f"/api/proxy/overlay/{digest}")
        assert r.status_code == 200
        assert r.content == b"OVERLAID-JPEG-BYTES"
        assert r.headers["content-type"] == "image/jpeg"
