"""Candidate ref policy: the proxy's read side (2026-10-22, SECURITY_REVIEW
"H1") and the override-write side (2026-11-11 audit, "A1"/"B5").

``GET /api/proxy/{token}`` takes a base64 token of ``"<kind>|<ref>"`` that the
UI mints client-side -- it is encoding, not authentication, so the ref cannot
be trusted. Before the 2026-10-22 fix ``fetch`` happily read any absolute
path the caller named (``secrets.yml`` included -- and the redaction
middleware never sees it, because the response is not JSON) and fetched any
URL, giving an authenticated session an arbitrary-file-read plus an SSRF
primitive.

``PUT /api/items/{id}/candidates`` -- the route that stores a manually
picked candidate -- had NO equivalent validation at all until 2026-11-11:
the ref it stores becomes a ``FileWrite.source_ref`` the executor writes to
disk, so the proxy's read-side policy above was defeated by never being
consulted on the way in. ``engine.candidate_ref`` is now the one module
both sides (and ``engine/plan_builder.py``'s own candidate resolution)
share.

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


# ---------------------------------------------------------------------------
# audit 2026-11-11 "A1"/"B5": the SAME policy, enforced at the write side
# ---------------------------------------------------------------------------
#
# Everything above pins the proxy's READ-side policy. Until engine.candidate_ref
# existed, the WRITE side -- PUT /api/items/{id}/candidates, which stores a
# manual override ref -- had no validation at all: an authenticated session
# could store an arbitrary local path (config/secrets.yml included) as a
# poster's source_ref, which the executor would then write into the media
# tree and the proxy would then serve back byte-for-byte, defeating the read
# side's own policy by never going through it on the way in. These tests
# cover the write side plus the shared engine.candidate_ref module directly.

class TestCandidateRefModule:
    """Unit tests for engine.candidate_ref, the module both this proxy and
    api/overrides.py + engine/plan_builder.py now share (one allowed-host
    list, one allowed-root containment check, one size cap -- not three)."""

    def test_check_remote_accepts_an_allowed_host(self):
        from finmeta.engine.candidate_ref import check_remote
        check_remote("https://image.tmdb.org/t/p/original/x.jpg")  # no raise

    def test_check_remote_refuses_a_non_provider_host(self):
        from finmeta.engine.candidate_ref import RefNotAllowed, check_remote
        with pytest.raises(RefNotAllowed):
            check_remote("https://evil.example/x.jpg")

    def test_check_remote_refuses_suffix_confusable_host(self):
        from finmeta.engine.candidate_ref import RefNotAllowed, check_remote
        with pytest.raises(RefNotAllowed):
            check_remote("https://image.tmdb.org.evil.com/x.jpg")

    def test_check_local_refuses_outside_every_root(self, tmp_path):
        from finmeta.engine.candidate_ref import RefNotAllowed, check_local
        allowed = tmp_path / "assets"
        allowed.mkdir()
        victim = tmp_path / "secrets.yml"
        victim.write_text("tmdb_key: CANARY\n")
        with pytest.raises(RefNotAllowed):
            check_local(str(victim), [allowed])

    def test_check_local_accepts_inside_an_allowed_root(self, tmp_path):
        from finmeta.engine.candidate_ref import check_local
        allowed = tmp_path / "assets"
        (allowed / "123").mkdir(parents=True)
        p = check_local(str(allowed / "123" / "poster.jpg"), [allowed])
        assert p == (allowed / "123" / "poster.jpg").resolve()

    def test_default_allowed_roots_never_includes_config_dir_itself(self, tmp_path):
        """The containing config_dir (where secrets.yml/auth.yml/config.yml
        live) must never itself be an allowed root -- only its named
        artwork-lifecycle subdirectories."""
        from finmeta.config.models import Config, ServerConfig
        from finmeta.engine.candidate_ref import default_allowed_roots
        cfg = Config(dry_run=True, server=ServerConfig(url="http://x:8096"))
        config_dir = tmp_path / "config"
        roots = default_allowed_roots(cfg, config_dir)
        resolved_config_dir = config_dir.resolve()
        assert resolved_config_dir not in {r.resolve() for r in roots}

    def test_default_allowed_roots_with_no_config_dir_still_grants_local_source(self, tmp_path):
        """config_dir=None (engine.plan_builder.build_plan's own default)
        must still grant cfg.sources.local.path and path_mappings roots --
        only the four config_dir subdirectories are unavailable without one."""
        from finmeta.config.models import Config, LocalSource, ServerConfig, SourcesConfig
        from finmeta.engine.candidate_ref import check_local, default_allowed_roots
        local_dir = tmp_path / "my-assets"
        local_dir.mkdir()
        cfg = Config(dry_run=True, server=ServerConfig(url="http://x:8096"),
                    sources=SourcesConfig(local=LocalSource(enabled=True, path=str(local_dir))))
        roots = default_allowed_roots(cfg, None)
        check_local(str(local_dir / "poster.jpg"), roots)  # no raise


class TestCandidateOverrideWritePolicy:
    """PUT /api/items/{id}/candidates (the override-write route) now
    validates ``ref`` through the exact same policy as the proxy's read
    side -- fixed 2026-11-11 (audit "A1"). Before this fix there was no
    validation on write at all: any absolute local path, including a config
    secret, was accepted and stored verbatim."""

    @pytest.fixture
    def client_and_ctx(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient
        from finmeta.api import AppContext, create_app
        from finmeta.engine.plan import FileWrite, ItemPlan, WritePlan
        monkeypatch.setenv("FINMETA_AUTH", "off")
        monkeypatch.setenv("TMDB_KEY", "fake-tmdb-key")
        cfg_dir = tmp_path / "config"
        cfg_dir.mkdir()
        assets = tmp_path / "assets"
        assets.mkdir()
        (cfg_dir / "config.yml").write_text(
            "dry_run: true\n"
            "server:\n  url: http://fake:8096\n"
            f"sources:\n  local:\n    enabled: true\n    path: {assets}\n"
            "libraries: {}\n"
        )

        def _fake_plan(cfg, jf, tmdb, sources, library=None, item_id=None,
                      config_dir=None, **kwargs):
            return WritePlan(items=[ItemPlan(
                item_id="item-1", item_name="Item 1", media_type="movie",
                tmdb_id=1, matched_by="provider_id", files=[],
            )])

        ctx = AppContext(str(cfg_dir / "config.yml"), config_dir=str(cfg_dir),
                         db_path=str(cfg_dir / "jobs.db"),
                         overrides_path=str(cfg_dir / "overrides.db"),
                         plan_builder=_fake_plan)
        client = TestClient(create_app(ctx))
        return client, ctx, assets, cfg_dir

    def test_arbitrary_local_path_is_refused_not_stored(self, client_and_ctx):
        """The exact PoC this fix closes: a session user names config's own
        secrets.yml as an override ref. Must be refused with 422, and --
        this is the part that actually matters -- never written to
        overrides.db at all."""
        client, ctx, assets, cfg_dir = client_and_ctx
        canary = cfg_dir / "pretend_secrets.yml"
        canary.write_text("tmdb_key: AUDIT-CANARY-VALUE\n")
        r = client.put("/api/items/item-1/candidates",
                       json={"kind": "poster", "ref": str(canary)})
        assert r.status_code == 422
        assert ctx.overrides.get("item-1", "poster") is None

    def test_disallowed_remote_host_is_refused_not_stored(self, client_and_ctx):
        client, ctx, assets, cfg_dir = client_and_ctx
        r = client.put("/api/items/item-1/candidates",
                       json={"kind": "poster", "ref": "https://evil.example/x.jpg"})
        assert r.status_code == 422
        assert ctx.overrides.get("item-1", "poster") is None

    def test_ref_inside_the_configured_local_source_is_accepted(self, client_and_ctx):
        """The normal, legitimate case must keep working: a local candidate
        inside cfg.sources.local.path is accepted and stored."""
        client, ctx, assets, cfg_dir = client_and_ctx
        legit = assets / "poster.jpg"
        legit.write_bytes(b"JPEGBYTES")
        r = client.put("/api/items/item-1/candidates",
                       json={"kind": "poster", "ref": str(legit)})
        assert r.status_code == 200
        ovr = ctx.overrides.get("item-1", "poster")
        assert ovr is not None
        assert ovr["ref"] == str(legit)

    def test_allowed_provider_host_is_accepted(self, client_and_ctx):
        client, ctx, assets, cfg_dir = client_and_ctx
        r = client.put("/api/items/item-1/candidates",
                       json={"kind": "poster",
                            "ref": "https://image.tmdb.org/t/p/original/x.jpg"})
        assert r.status_code == 200
        ovr = ctx.overrides.get("item-1", "poster")
        assert ovr is not None

    def test_clearing_a_candidate_bypasses_validation_entirely(self, client_and_ctx):
        """ref=None (clear) never touches the ref-validation path at all."""
        client, ctx, assets, cfg_dir = client_and_ctx
        r = client.put("/api/items/item-1/candidates",
                       json={"kind": "poster", "ref": None})
        assert r.status_code == 200
