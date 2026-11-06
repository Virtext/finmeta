"""A short download must not poison the plan (and silently skip a write).

Operator-reported 2026-11-06: a full apply logged ~360 ``hash mismatch``
warnings -- ~360 backdrops/logos not written, every run, silently.

Every image is downloaded twice: once at plan time (to hash it, for the
create/update/unchanged diff) and once at apply time (to write it). The
executor compared the two and refused the write when they disagreed. Traced
to the PLAN-time download coming back short: the same URL hashed identically
in the Nov 4 and Nov 5 plans and on a fresh re-fetch, but differently in the
Nov 6 plan. httpx returns a body that ends early without complaint, so the
bad hash went into the plan and no later fetch could ever match it.

``engine.net.fetch_bytes`` verifies instead of trusting, and the executor no
longer throws away a verified body just because the plan's hash came from a
broken one. Overlaid kinds keep the strict check -- their bytes come from
the render cache, and the source_ref fetch is the UN-overlaid original.
"""
from __future__ import annotations

import hashlib
import io
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from PIL import Image

from finmeta.engine import net
from finmeta.engine.executor import apply_plan
from finmeta.engine.net import IncompleteDownload, fetch_bytes, verify_body
from finmeta.engine.plan import FileWrite, ItemPlan, WritePlan


def _jpeg(color=(10, 20, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (40, 60), color).save(buf, format="JPEG")
    return buf.getvalue()


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGBA", (10, 10), (1, 2, 3, 4)).save(buf, format="PNG")
    return buf.getvalue()


class _Resp:
    def __init__(self, content: bytes, headers=None, status=200):
        self.content = content
        self.headers = headers or {}
        self._status = status

    def raise_for_status(self):
        if self._status >= 400:
            raise RuntimeError(f"HTTP {self._status}")


def _serve(monkeypatch, *responses):
    """Hand out one response per httpx.get call; the last one repeats."""
    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        idx = min(len(calls) - 1, len(responses) - 1)
        return responses[idx]

    monkeypatch.setattr(net.httpx, "get", fake_get)
    return calls


class TestVerifyBody:
    def test_complete_jpeg_passes(self):
        data = _jpeg()
        assert verify_body(_Resp(data, {"content-length": str(len(data))}), data) is None

    def test_short_body_is_caught_by_content_length(self):
        data = _jpeg()
        assert "declared" in verify_body(
            _Resp(data[:-50], {"content-length": str(len(data))}), data[:-50])

    def test_truncated_jpeg_without_content_length_is_still_caught(self):
        data = _jpeg()[:-40]
        assert "end-of-image" in verify_body(_Resp(data), data)

    def test_truncated_png_is_caught(self):
        data = _png()[:-40]
        assert "IEND" in verify_body(_Resp(data), data)

    def test_complete_png_passes(self):
        data = _png()
        assert verify_body(_Resp(data), data) is None

    def test_compressed_response_skips_the_length_check(self):
        """Content-Length counts the COMPRESSED bytes; httpx hands back the
        decoded ones, so comparing them would fail every gzip response."""
        data = _jpeg()
        resp = _Resp(data, {"content-length": "17", "content-encoding": "gzip"})
        assert verify_body(resp, data) is None

    def test_non_image_payload_has_no_marker_opinion(self):
        data = b"ID3 some audio payload"
        assert verify_body(_Resp(data), data) is None


class TestFetchBytes:
    def test_returns_a_complete_body(self, monkeypatch):
        data = _jpeg()
        _serve(monkeypatch, _Resp(data, {"content-length": str(len(data))}))
        assert fetch_bytes("https://example/x.jpg") == data

    def test_retries_once_and_succeeds(self, monkeypatch):
        """A transient short read is exactly what the live incident looked
        like -- the very next fetch of the same URL was fine."""
        good = _jpeg()
        calls = _serve(
            monkeypatch,
            _Resp(good[:-50], {"content-length": str(len(good))}),
            _Resp(good, {"content-length": str(len(good))}),
        )
        assert fetch_bytes("https://example/x.jpg") == good
        assert len(calls) == 2

    def test_raises_when_every_attempt_is_short(self, monkeypatch):
        good = _jpeg()
        _serve(monkeypatch, _Resp(good[:-50], {"content-length": str(len(good))}))
        with pytest.raises(IncompleteDownload):
            fetch_bytes("https://example/x.jpg")

    def test_http_error_is_retried_then_raised(self, monkeypatch):
        calls = _serve(monkeypatch, _Resp(b"", status=500))
        with pytest.raises(RuntimeError):
            fetch_bytes("https://example/x.jpg")
        assert len(calls) == 2


def _config(tmp_path):
    from finmeta.config.models import (
        Config, LocalSource, Metadata, MovieArtwork, MovieLibrary, PathMapping,
        ServerConfig, SourcesConfig, TmdbSource,
    )
    return Config(
        dry_run=False,
        server=ServerConfig(url="http://test"),
        path_mappings=[PathMapping(jellyfin="/media", local=str(tmp_path / "media"))],
        libraries={"Movies": MovieLibrary(
            type="movies", metadata=Metadata(enabled=False),
            artwork=MovieArtwork(poster=True), theme_song=False)},
        sources=SourcesConfig(tmdb=TmdbSource(enabled=True),
                              local=LocalSource(enabled=True, path=str(tmp_path / "assets"))),
    )


def _plan(target: Path, kind: str, source_ref: str, sha: str) -> WritePlan:
    return WritePlan(items=[ItemPlan(
        item_id="mov001", item_name="Test Movie", media_type="movie",
        tmdb_id=123, matched_by="provider_id",
        files=[FileWrite(target_path=str(target), kind=kind,
                         source_ref=source_ref, content_sha256=sha)],
    )])


class TestExecutorHashMismatch:
    """What the apply does when the plan's hash disagrees with the source."""

    def test_stale_plan_hash_on_a_url_still_writes_the_verified_bytes(
            self, tmp_path: Path, monkeypatch):
        real = _jpeg((1, 2, 3))
        _serve(monkeypatch, _Resp(real, {"content-length": str(len(real))}))
        cfg_dir = tmp_path / "config"
        cfg_dir.mkdir()
        target = tmp_path / "media" / "backdrop.jpg"
        plan = _plan(target, "backdrop", "https://example/b.jpg", "f" * 64)

        result = apply_plan(plan, "r1", _config(tmp_path), MagicMock(), MagicMock(),
                            config_dir=cfg_dir, db_path=tmp_path / "jobs.db",
                            poll_timeout=0.1)

        assert target.read_bytes() == real
        assert result.files_written == 1
        assert result.errors == []  # a warning, not a failed file

    def test_a_poster_with_no_cached_render_is_still_refused(
            self, tmp_path: Path, monkeypatch):
        """The overlaid bytes only ever live in the render cache. On a cache
        miss the source_ref fetch is the ORIGINAL -- writing it would strip
        the badges off the poster, so this one must keep failing loudly."""
        original = _jpeg((9, 9, 9))
        _serve(monkeypatch, _Resp(original, {"content-length": str(len(original))}))
        cfg_dir = tmp_path / "config"
        cfg_dir.mkdir()
        target = tmp_path / "media" / "poster.jpg"
        plan = _plan(target, "poster", "https://example/p.jpg", "e" * 64)

        result = apply_plan(plan, "r1", _config(tmp_path), MagicMock(), MagicMock(),
                            config_dir=cfg_dir, db_path=tmp_path / "jobs.db",
                            poll_timeout=0.1)

        assert not target.exists()
        assert result.files_written == 0
        assert any("hash mismatch" in e for e in result.errors)

    def test_a_local_source_mismatch_is_still_refused(self, tmp_path: Path):
        """Only a verified HTTP body earns the benefit of the doubt."""
        assets = tmp_path / "assets"
        assets.mkdir()
        src = assets / "b.jpg"
        src.write_bytes(_jpeg())
        cfg_dir = tmp_path / "config"
        cfg_dir.mkdir()
        target = tmp_path / "media" / "backdrop.jpg"
        plan = _plan(target, "backdrop", str(src), "d" * 64)

        result = apply_plan(plan, "r1", _config(tmp_path), MagicMock(), MagicMock(),
                            config_dir=cfg_dir, db_path=tmp_path / "jobs.db",
                            poll_timeout=0.1)

        assert not target.exists()
        assert any("hash mismatch" in e for e in result.errors)

    def test_a_matching_hash_is_untouched_by_any_of_this(self, tmp_path: Path, monkeypatch):
        real = _jpeg((4, 5, 6))
        _serve(monkeypatch, _Resp(real, {"content-length": str(len(real))}))
        cfg_dir = tmp_path / "config"
        cfg_dir.mkdir()
        target = tmp_path / "media" / "backdrop.jpg"
        plan = _plan(target, "backdrop", "https://example/b.jpg",
                     hashlib.sha256(real).hexdigest())

        result = apply_plan(plan, "r1", _config(tmp_path), MagicMock(), MagicMock(),
                            config_dir=cfg_dir, db_path=tmp_path / "jobs.db",
                            poll_timeout=0.1)

        assert target.read_bytes() == real
        assert result.files_written == 1
        assert result.errors == []
