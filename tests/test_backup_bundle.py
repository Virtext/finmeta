"""SPEC_S11 §D: backup bundles — contents, exclusions, restore, retention."""
from __future__ import annotations

import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

from finmeta.api import AppContext, create_app
from finmeta.engine.backup_bundle import (
    EXCLUDED, Bundle, BundleError, build_bundle, bundle_filename,
    installed_packs, safe_bundle_path,
)

CONFIG_YAML = """\
dry_run: true
server:
  url: http://fake:8096
  api_key_env: JELLYFIN_API_KEY
path_mappings:
  - jellyfin: /media
    local: /tmp/fake-media
libraries:
  Movies:
    type: movies
    enabled: true
    metadata:
      enabled: false
    artwork:
      poster: true
    theme_song: false
    overlays: []
"""


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    monkeypatch.setenv("JELLYFIN_API_KEY", "fake-key")
    monkeypatch.setenv("FINMETA_AUTH", "off")
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "config.yml").write_text(CONFIG_YAML)
    # Plant the files a bundle must never contain, right next to the config,
    # so exclusion is proven deliberate rather than incidental.
    (cfg_dir / "secrets.yml").write_text("tmdb_key: SUPER-SECRET\n")
    (cfg_dir / "auth.yml").write_text("password_hash: $2b$12$whatever\n")
    (cfg_dir / "config.yml.bak-old").write_text("dry_run: false\n")
    (cfg_dir / "assets" / "packs" / "ichimoku").mkdir(parents=True)
    (cfg_dir / "assets" / "logos" / "network").mkdir(parents=True)
    return AppContext(
        str(cfg_dir / "config.yml"), config_dir=str(cfg_dir),
        db_path=str(cfg_dir / "jobs.db"),
        overrides_path=str(cfg_dir / "overrides.db"))


@pytest.fixture
def client(ctx):
    with TestClient(create_app(ctx)) as c:
        yield c


# ---------------------------------------------------------------------------
# the bundle itself
# ---------------------------------------------------------------------------

def test_a_bundle_round_trips():
    data = build_bundle(
        config_text="dry_run: true\n",
        overrides={"overrides": [{"item_id": "i1", "kind": "poster", "ref": "r"}]},
        packs={"image_packs": ["ichimoku"]},
        secret_names=["tmdb_key"], finmeta_version="1.0.0")
    b = Bundle(data)
    assert b.config_text == "dry_run: true\n"
    assert b.overrides["overrides"][0]["item_id"] == "i1"
    assert b.packs == {"image_packs": ["ichimoku"]}
    assert b.manifest["secret_names"] == ["tmdb_key"]
    assert b.manifest["counts"] == {"overrides": 1}
    assert b.manifest["excluded"] == list(EXCLUDED)
    b.close()


def test_a_bundle_from_the_future_is_refused():
    data = build_bundle(config_text="", overrides={})
    buf = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as src, zipfile.ZipFile(buf, "w") as dst:
        for name in src.namelist():
            payload = src.read(name)
            if name == "manifest.json":
                m = json.loads(payload)
                m["bundle_version"] = 99
                payload = json.dumps(m).encode()
            dst.writestr(name, payload)
    with pytest.raises(BundleError, match="not supported"):
        Bundle(buf.getvalue())


def test_garbage_is_not_a_bundle():
    with pytest.raises(BundleError, match="not a readable zip"):
        Bundle(b"this is not a zip")


def test_a_zip_without_a_manifest_is_refused():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("config.yml", "dry_run: true\n")
    with pytest.raises(BundleError, match="no manifest"):
        Bundle(buf.getvalue())


def test_unexpected_members_are_never_read():
    """A bundle is a file the operator may have edited; the reader only ever
    looks at the members it knows."""
    data = build_bundle(config_text="x: 1\n", overrides={})
    buf = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as src, zipfile.ZipFile(buf, "w") as dst:
        for name in src.namelist():
            dst.writestr(name, src.read(name))
        dst.writestr("../../etc/passwd", "root:x:0:0")
    b = Bundle(buf.getvalue())
    assert "../../etc/passwd" in b.names()          # present in the zip...
    with pytest.raises(BundleError, match="unexpected member"):
        b._read("../../etc/passwd")                  # ...but never readable
    b.close()


def test_bundle_names_are_strict():
    assert bundle_filename("b6").startswith("finmeta-backup-b6-")
    assert bundle_filename("a/b c").startswith("finmeta-backup-a-b-c-")


def test_stored_bundle_paths_cannot_escape(tmp_path):
    d = tmp_path / "bundles"
    d.mkdir()
    ok = safe_bundle_path(d, "finmeta-backup-20261108-1200.zip")
    assert ok.parent == d.resolve()
    for bad in ("../secrets.yml", "finmeta-backup-../x.zip", "config.yml",
                "finmeta-backup-x.zip.exe", ""):
        with pytest.raises(BundleError):
            safe_bundle_path(d, bad)


def test_installed_packs_lists_names_only(ctx):
    packs = installed_packs(ctx.config_dir)
    assert packs == {"image_packs": ["ichimoku"], "logo_packs": ["network"]}


# ---------------------------------------------------------------------------
# the API
# ---------------------------------------------------------------------------

def _members(resp) -> list:
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        return sorted(zf.namelist())


def test_export_contains_the_decisions_and_none_of_the_secrets(client, ctx):
    ctx.overrides.set("i1", "poster", "ref-1", "tmdb")
    ctx.overrides.set_lock("i1", "poster", "ref-1", "a" * 64)
    r = client.get("/api/backup/export")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    assert "finmeta-backup-" in r.headers["content-disposition"]
    assert _members(r) == ["config.yml", "manifest.json", "overrides.json", "packs.json"]

    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        blob = b"".join(zf.read(n) for n in zf.namelist())
        assert b"SUPER-SECRET" not in blob
        assert b"password_hash" not in blob
        overrides = json.loads(zf.read("overrides.json"))
        manifest = json.loads(zf.read("manifest.json"))
    assert overrides["overrides"][0]["ref"] == "ref-1"
    assert overrides["artwork_locks"][0]["item_id"] == "i1"
    assert "jellyfin_api_key" in manifest["secret_names"]
    assert manifest["finmeta_version"]


def test_the_scan_is_opt_in(client, ctx):
    from finmeta.engine import scan_store
    scan_store.start_scan(ctx.db_path, "s1", library="Movies", tier=1)
    scan_store.add_findings(ctx.db_path, "s1", [
        scan_store.Finding(library="Movies", check_code="artwork.missing.poster",
                           severity="error", item_id="i1", item_name="A")])
    scan_store.finish_scan(ctx.db_path, "s1", "ok")

    assert "scan.json" not in _members(client.get("/api/backup/export"))
    r = client.get("/api/backup/export", params={"include_scan": True})
    assert "scan.json" in _members(r)
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        scan = json.loads(zf.read("scan.json"))
    assert scan["scan"]["scan_id"] == "s1" and scan["total"] == 1


def test_snapshot_lists_prunes_downloads_and_deletes(client, ctx, monkeypatch):
    import time
    for _ in range(3):
        client.post("/api/backup/snapshot")
        time.sleep(0.01)
    listed = client.get("/api/backup/bundles").json()
    # the filename has minute resolution, so three in one minute is one name
    assert len(listed["bundles"]) >= 1
    assert listed["keep_bundles"] == 10
    assert listed["excluded"] == list(EXCLUDED)

    name = listed["bundles"][0]["name"]
    dl = client.get(f"/api/backup/bundles/{name}")
    assert dl.status_code == 200 and "manifest.json" in _members(dl)
    assert client.delete(f"/api/backup/bundles/{name}").status_code == 200
    assert client.get(f"/api/backup/bundles/{name}").status_code == 404
    assert client.get("/api/backup/bundles/not-a-bundle.zip").status_code == 422


def test_keep_bundles_prunes_old_snapshots(client, ctx):
    d = ctx.config_dir / "backups" / "bundles"
    d.mkdir(parents=True, exist_ok=True)
    for i in range(4):
        (d / f"finmeta-backup-2026110{i}-1200.zip").write_bytes(b"old")
    ctx.put_config(CONFIG_YAML + "backup:\n  keep_bundles: 2\n")
    body = client.post("/api/backup/snapshot").json()
    assert len(body["pruned"]) == 3           # 5 files -> keep 2
    assert len(client.get("/api/backup/bundles").json()["bundles"]) == 2


def test_inspect_writes_nothing_and_reports_the_diff(client, ctx):
    other = CONFIG_YAML.replace("dry_run: true", "dry_run: false").replace(
        "  Movies:", "  Serier:")
    bundle = build_bundle(config_text=other, overrides={}, secret_names=["tmdb_key"])
    r = client.post("/api/backup/inspect", content=bundle,
                    headers={"content-type": "application/zip"})
    assert r.status_code == 200
    body = r.json()
    assert body["diff"]["libraries"] == {"added": ["Serier"], "removed": ["Movies"],
                                         "kept": []}
    assert body["diff"]["dry_run"] == {"current": True, "incoming": False}
    assert body["secrets_required"] == ["tmdb_key"]
    # nothing was applied
    assert ctx.config.dry_run is True
    assert "Movies" in ctx.config.libraries


def test_inspect_reports_overlay_profile_additions_and_removals(client, ctx):
    """Audit 2026-11-11 "D7": overlays.profiles is a Dict[str, dict] keyed
    by profile NAME, not a list of {"name": ...} objects -- the old diff
    code iterated the dict's own KEYS (plain strings) through a
    `isinstance(p, dict)` check that was therefore always False, so this
    "what would restoring this change?" preview always reported zero
    overlay-profile changes no matter what the bundle actually held."""
    with_profile = CONFIG_YAML + (
        "overlays:\n"
        "  profiles:\n"
        "    Movies-auto:\n"
        "      elements: [resolution]\n"
    )
    ctx.put_config(with_profile)
    assert "Movies-auto" in ctx.config.overlays.profiles

    # The incoming bundle has a DIFFERENT profile name -- Movies-auto should
    # show up as removed, Shows-auto as added.
    incoming = CONFIG_YAML + (
        "overlays:\n"
        "  profiles:\n"
        "    Shows-auto:\n"
        "      elements: [audio]\n"
    )
    bundle = build_bundle(config_text=incoming, overrides={})
    r = client.post("/api/backup/inspect", content=bundle,
                    headers={"content-type": "application/zip"})
    assert r.status_code == 200
    assert r.json()["diff"]["overlay_profiles"] == {
        "added": ["Shows-auto"], "removed": ["Movies-auto"],
    }
    # still just a preview -- nothing applied
    assert "Movies-auto" in ctx.config.overlays.profiles
    assert "Shows-auto" not in ctx.config.overlays.profiles


def test_restore_requires_confirm(client, ctx):
    bundle = client.get("/api/backup/export").content
    r = client.post("/api/backup/restore", content=bundle,
                    headers={"content-type": "application/zip"})
    assert r.status_code == 409
    assert "confirm" in r.json()["detail"]


def test_restore_applies_config_and_overrides(client, ctx):
    ctx.overrides.set("i1", "poster", "ref-1", "tmdb")
    bundle = client.get("/api/backup/export").content

    # drift away from the backup, then restore it
    ctx.put_config(CONFIG_YAML.replace("dry_run: true", "dry_run: false"))
    ctx.overrides.clear("i1", "poster")
    assert ctx.config.dry_run is False

    r = client.post("/api/backup/restore", content=bundle,
                    params={"confirm": "true"},
                    headers={"content-type": "application/zip"})
    assert r.status_code == 200
    body = r.json()
    assert body["restored"]["config"] is True
    assert body["restored"]["overrides"]["overrides"] == 1
    assert body["secrets_required"]
    assert ctx.config.dry_run is True
    assert ctx.overrides.get("i1", "poster")["ref"] == "ref-1"


def test_restore_merges_by_default_and_can_replace(client, ctx):
    ctx.overrides.set("i1", "poster", "ref-1", "tmdb")
    bundle = client.get("/api/backup/export").content
    ctx.overrides.set("i2", "logo", "ref-2", "tmdb")      # made AFTER the backup

    client.post("/api/backup/restore", content=bundle, params={"confirm": "true"},
                headers={"content-type": "application/zip"})
    assert ctx.overrides.get("i2", "logo") is not None     # merge keeps it

    client.post("/api/backup/restore", content=bundle,
                params={"confirm": "true", "mode": "replace"},
                headers={"content-type": "application/zip"})
    assert ctx.overrides.get("i2", "logo") is None         # replace drops it
    assert ctx.overrides.get("i1", "poster") is not None


def test_an_invalid_config_is_rejected_before_anything_is_touched(client, ctx):
    bundle = build_bundle(
        config_text="dry_run: not-a-boolean\nserver:\n  url: http://x\n",
        overrides={"overrides": [{"item_id": "i9", "kind": "poster", "ref": "r"}]})
    r = client.post("/api/backup/restore", content=bundle,
                    params={"confirm": "true"},
                    headers={"content-type": "application/zip"})
    assert r.status_code == 422
    assert "rejected" in r.json()["detail"]
    assert ctx.config.dry_run is True                      # untouched
    assert ctx.overrides.get("i9", "poster") is None       # and no rows written


def test_restore_can_name_a_stored_snapshot(client, ctx):
    name = client.post("/api/backup/snapshot").json()["bundle"]
    ctx.put_config(CONFIG_YAML.replace("dry_run: true", "dry_run: false"))
    r = client.post("/api/backup/restore",
                    json={"bundle": name, "confirm": True})
    assert r.status_code == 200
    assert ctx.config.dry_run is True


def test_restore_rejects_junk_and_bad_modes(client, ctx):
    assert client.post("/api/backup/restore", content=b"nope",
                       params={"confirm": "true"},
                       headers={"content-type": "application/zip"}).status_code == 422
    bundle = client.get("/api/backup/export").content
    assert client.post("/api/backup/restore", content=bundle,
                       params={"confirm": "true", "mode": "sideways"},
                       headers={"content-type": "application/zip"}).status_code == 422
    assert client.post("/api/backup/restore", json={"confirm": True}).status_code == 422


# ---------------------------------------------------------------------------
# A4 (audit 2026-11-11): oversized uploads are refused WHILE streaming, not
# only after being fully buffered into memory.
# ---------------------------------------------------------------------------

class _FakeOversizedRequest:
    """A minimal stand-in for fastapi.Request: a declared content-length and
    an async chunk generator that COUNTS how many chunks it actually hands
    out, so a test can prove the reader stopped pulling chunks once the cap
    was crossed instead of draining the whole thing first."""

    def __init__(self, total_bytes: int, chunk_size: int = 1024,
                declared_length: "int | None" = None):
        self._total = total_bytes
        self._chunk_size = chunk_size
        self.chunks_yielded = 0
        self.headers = ({"content-length": str(declared_length)}
                        if declared_length is not None else {})

    async def stream(self):
        sent = 0
        while sent < self._total:
            n = min(self._chunk_size, self._total - sent)
            self.chunks_yielded += 1
            sent += n
            yield b"x" * n


def test_capped_body_reader_aborts_mid_stream_without_draining_it():
    """A body far larger than the cap, sent with NO (or a lying)
    Content-Length, must be refused as soon as the cap is crossed -- not
    after the whole multi-megabyte body has been pulled into memory.

    Driven with a plain ``asyncio.run`` rather than an async test plugin --
    none is set up for this project (no pytest-asyncio/anyio marker exists
    anywhere else in the suite) and ``_read_capped_body`` is a single
    coroutine with no event-loop dependency worth pulling one in for.
    """
    import asyncio
    from fastapi import HTTPException
    from finmeta.api.routes_backup import _read_capped_body

    limit = 4096
    total = 50 * limit  # would be 200KB fully drained; must not get there
    req = _FakeOversizedRequest(total, chunk_size=1024, declared_length=None)
    with pytest.raises(HTTPException) as ei:
        asyncio.run(_read_capped_body(req, limit))
    assert ei.value.status_code == 413
    # Stopped within a few chunks of crossing the cap, nowhere near the end.
    assert req.chunks_yielded < (total // 1024) // 2


def test_capped_body_reader_rejects_on_declared_length_before_reading_any_chunk():
    import asyncio
    from fastapi import HTTPException
    from finmeta.api.routes_backup import _read_capped_body

    limit = 4096
    req = _FakeOversizedRequest(10 * limit, chunk_size=1024, declared_length=10 * limit)
    with pytest.raises(HTTPException) as ei:
        asyncio.run(_read_capped_body(req, limit))
    assert ei.value.status_code == 413
    assert req.chunks_yielded == 0  # refused before touching the stream at all


def test_restore_oversized_body_is_rejected(client):
    """End-to-end: the real route still answers 413 for a body over the cap
    (this one small enough to actually send in a test)."""
    from finmeta.api.routes_backup import MAX_UPLOAD_BYTES
    oversized = b"x" * (MAX_UPLOAD_BYTES + 1)
    r = client.post("/api/backup/restore", content=oversized,
                    params={"confirm": "true"},
                    headers={"content-type": "application/zip"})
    assert r.status_code == 413
