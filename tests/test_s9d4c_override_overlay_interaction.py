"""S9d4c: a manual candidate override (S9d2) on "poster" must not silently
discard overlay compositing.

Found while verifying Fase A step 6 end to end: a real apply against
test-Jellyfin on an item with a stored poster override produced a poster
with NO visible badges, despite format-rule matches confirming resolution/
audio data was available. Root cause: ``api.overrides.apply_overrides``
(a post-build patch step with no knowledge of overlays at all) always
re-hashed the override's ref as a PLAIN file and overwrote the FileWrite --
discarding whatever ``_overlay_poster`` had already composited for that
item during the normal plan build.

Fix: ``plan_builder._apply_candidate_overrides`` folds any stored override
into the artwork selection BEFORE files are built, so an overridden poster
goes through the SAME overlay-compositing branch as an auto-selected one;
the resulting FileWrite is marked ``meta["overridden"]`` so
``api.overrides.apply_overrides`` knows not to re-touch it (checking that
marker rather than ``source_ref == ref``, because ``overlay_file_write``
points ``source_ref`` at the latest backup once one exists -- O7 -- not at
the override's ref, so a literal ref comparison stops matching after the
very first apply).
"""
from __future__ import annotations

import hashlib
import io

from PIL import Image

from finmeta.api.overrides import OverrideStore, apply_overrides
from finmeta.config.models import (
    Config, Metadata, MovieArtwork, MovieLibrary, OverlaysConfig, OverlayStyle, ServerConfig,
)
from finmeta.engine.matching import MatchResult
from finmeta.engine.plan import FileWrite, ItemPlan, WritePlan
from finmeta.engine.plan_builder import _build_movie_plan
from finmeta.jellyfin_client.mappings import PathMapper
from finmeta.jellyfin_client.types import Item, MediaStream


def _poster_bytes(color) -> bytes:
    img = Image.new("RGB", (40, 60), color)
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


class _FakeJfStreams:
    def get_media_streams(self, item_id):
        return [MediaStream(index=0, type="Video", width=1920, height=1080)]


def _fixture(tmp_path, assets_dir_name="assets"):
    media_root = tmp_path / "media"
    movie_dir = media_root / "Movies" / "Test Movie"
    movie_dir.mkdir(parents=True, exist_ok=True)
    (movie_dir / "m.mkv").write_bytes(b"video")

    assets = tmp_path / assets_dir_name
    ad = assets / "155"
    ad.mkdir(parents=True, exist_ok=True)
    auto_candidate = _poster_bytes((10, 20, 30))
    (ad / "poster.jpg").write_bytes(auto_candidate)

    override_path = tmp_path / "override_poster.jpg"
    override_bytes = _poster_bytes((200, 100, 50))
    override_path.write_bytes(override_bytes)

    cfg = Config(
        dry_run=True,
        server=ServerConfig(url="http://x:8096"),
        libraries={"Movies": MovieLibrary(
            type="movies",
            metadata=Metadata(enabled=False),
            artwork=MovieArtwork(poster=True),
            overlays=["resolution"],
            overlay_profile="default",
        )},
        overlays=OverlaysConfig(
            style=OverlayStyle(position="top_left", margin=20, font_size=30),
            profiles={"default": {"elements": ["resolution"]}},
        ),
    )
    mapper = PathMapper.from_config([{"jellyfin": str(media_root), "local": str(media_root)}])
    item = Item(id="mov155", name="Test Movie (2020)", type="Movie",
                path=str(movie_dir / "m.mkv"), provider_ids={"Tmdb": "155"})
    match = MatchResult(item=item, tmdb_id=155, matched_by="provider_id")
    from finmeta.sources.local import LocalAssetSource
    sources = {"local": LocalAssetSource(assets)}
    lib = cfg.libraries["Movies"]
    return cfg, lib, mapper, item, match, sources, auto_candidate, override_bytes, str(override_path)


def _build(tmp_path, config_dir, assets_dir_name="assets"):
    cfg, lib, mapper, item, match, sources, auto_candidate, override_bytes, override_ref = \
        _fixture(tmp_path, assets_dir_name)
    plan_item = _build_movie_plan(
        item, match, cfg, lib, mapper, tmdb=None, jf=_FakeJfStreams(),
        sources=sources, language="en", lang_fallback=[None], config_dir=config_dir)
    poster = [f for f in plan_item.files if f.kind == "poster"][0]
    return poster, auto_candidate, override_bytes, override_ref


def test_overridden_poster_is_composited_from_the_override_not_the_auto_pick(tmp_path):
    config_dir = tmp_path / "config"
    _, _, _, _, _, _, _, _, override_ref = _fixture(tmp_path)
    OverrideStore(config_dir / "overrides.db").set("mov155", "poster", override_ref)

    poster, auto_candidate, override_bytes, _ = _build(tmp_path, config_dir)

    # Composited (overlay baked in), so neither matches a plain hash --
    # but it must differ from what compositing the AUTO candidate would give.
    assert poster.content_sha256 != hashlib.sha256(auto_candidate).hexdigest()
    assert poster.content_sha256 != hashlib.sha256(override_bytes).hexdigest()
    assert poster.meta.get("overridden") is True


def test_without_an_override_poster_is_composited_from_the_auto_pick(tmp_path):
    config_dir = tmp_path / "config"
    poster, auto_candidate, override_bytes, _ = _build(tmp_path, config_dir)
    assert not poster.meta.get("overridden")
    assert poster.content_sha256 != hashlib.sha256(auto_candidate).hexdigest()


def test_apply_overrides_skips_a_filewrite_already_marked_overridden():
    """Unit test for the apply_overrides() guard directly: a FileWrite
    plan_builder already folded the override into (meta["overridden"]) must
    be left completely untouched -- re-hashing the plain ref here is exactly
    the bug (it would silently strip the overlay)."""
    composited_sha = "a" * 64
    plan = WritePlan(items=[ItemPlan(
        item_id="mov155", item_name="Test Movie", media_type="movie",
        files=[FileWrite(target_path="/x/poster.jpg", kind="poster",
                         source_ref="/backups/r1/poster.jpg",
                         content_sha256=composited_sha, status="update",
                         meta={"overridden": True})],
    )])
    out = apply_overrides(plan, {"mov155": [{"kind": "poster", "ref": "https://example/new.jpg"}]})
    fw = out.items[0].files[0]
    assert fw.content_sha256 == composited_sha
    assert fw.source_ref == "/backups/r1/poster.jpg"


def test_apply_overrides_still_swaps_when_not_pre_folded():
    """Regression guard: items plan_builder did NOT already fold the override
    into (meta["overridden"] absent -- e.g. a FileWrite built by a fake
    plan_builder in other tests) must still get the override applied by
    apply_overrides(), unchanged from before this fix."""
    import respx
    import httpx
    from finmeta.engine.plan import FileWrite, ItemPlan, WritePlan

    plan = WritePlan(items=[ItemPlan(
        item_id="mov1", item_name="Movie 1", media_type="movie",
        files=[FileWrite(target_path="/x/poster.jpg", kind="poster",
                         source_ref="/old/candidate.jpg",
                         content_sha256="b" * 64, status="update")],
    )])
    with respx.mock:
        respx.get("https://example/new.jpg").mock(
            return_value=httpx.Response(200, content=b"NEWBYTES"))
        out = apply_overrides(plan, {"mov1": [{"kind": "poster", "ref": "https://example/new.jpg"}]})
    fw = out.items[0].files[0]
    assert fw.source_ref == "https://example/new.jpg"
    assert fw.content_sha256 == hashlib.sha256(b"NEWBYTES").hexdigest()
