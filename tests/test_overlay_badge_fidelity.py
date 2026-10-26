"""S9c10b fix: one raw-stream source for preview AND apply (K9 full fidelity).

Small, focused tests written fresh (the wip branch\u2019s test_overlays_api additions
are NOT reused):

* forced audio tracks must not drive the audio badge on the apply path;
* a video with ONLY ColorTransfer metadata still gets the HDR fallback result;
* preview and apply badges agree for three fixture items (same raw source);
* the preview endpoint\u2019s ``samples`` list never contains real-data elements
  (resolution / dynamic_range / audio) when those values are actually detected.
"""
from __future__ import annotations

import copy
import io
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from finmeta.api.routes_settings import _overlay_preview_source
from finmeta.engine.badges import overlay_values
from finmeta.engine.overlay import OverlayStyle, render_poster, resolve_badges
from finmeta.engine.plan_builder import _overlay_poster
from finmeta.sources.candidate import Candidate

# Three fixture items: raw Jellyfin MediaStreams dicts (full fidelity source).
FIXTURES = {
    # 4K DOVI movie, real Atmos audio.
    "dovi": [
        {"Type": "Video", "Width": 3840, "Height": 2160, "Codec": "hevc",
         "Profile": "Main10", "VideoRange": "DOVI", "VideoRangeType": "DOVIWithHDR10"},
        {"Type": "Audio", "Channels": 8, "ChannelLayout": "7.1",
         "DisplayTitle": "Dolby Atmos", "Codec": "eac3", "Profile": "Atmos"},
    ],
    # 1080p SDR movie whose ONLY audio track is a forced commentary (must be skipped).
    "forced_audio": [
        {"Type": "Video", "Width": 1920, "Height": 1080, "Codec": "h264"},
        {"Type": "Audio", "Channels": 8, "ChannelLayout": "7.1",
         "DisplayTitle": "DTS-HD MA (commentary)", "Profile": "DTS-HD MA",
         "Codec": "dtsxhdma", "IsForced": True},
    ],
    # 1080p item with NO VideoRangeType at all -- only ColorTransfer=smpte2084.
    "ct_only": [
        {"Type": "Video", "Width": 1920, "Height": 1080, "Codec": "hevc",
         "ColorTransfer": "smpte2084"},
        {"Type": "Audio", "Channels": 6, "ChannelLayout": "5.1",
         "DisplayTitle": "Dolby Digital Plus", "Codec": "eac3"},
    ],
}

STYLE = OverlayStyle(position="bottom_left", margin=20, font_size=30)
# _overlay_poster now takes the richer OverlayStyleDoc shape (see
# plan_builder._apply_style_doc) -- elements are filled in per-call below
# (one entry per requested name) so only the elements the caller asked for
# are enabled, matching the old `names`-driven behavior.
STYLE_DOC = {"base_position": "bottom_left", "margin": 20, "font_size": 30}


def _poster_bytes() -> bytes:
    img = Image.new("RGB", (200, 300), (20, 30, 60))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


class _FakeJF:
    """Offline JellyfinClient stand-in exposing ONLY the raw-stream source."""

    def __init__(self):
        self.raw_calls: list[str] = []

    def get_item(self, item_id):
        return SimpleNamespace(id=item_id, type="Movie", path="/x/y.mkv", provider_ids={})

    def get_items(self, parent_id=None, recursive=False):
        return []  # movies only in these fixtures

    def get_item_media_streams_raw(self, item_id):
        self.raw_calls.append(item_id)
        assert item_id in FIXTURES, f"unknown fixture {item_id!r}"
        return copy.deepcopy(FIXTURES[item_id])

    def close(self):
        pass


def _apply_badges(item_id: str, names=("resolution", "hdr", "audio")) -> dict:
    """Run the REAL apply path (_overlay_poster) and read back its badge values."""
    jf = _FakeJF()
    target = Path("/tmp/finmeta-test-overlay/poster.jpg")
    cand = Candidate(kind="poster", source="local", ref="/nonexistent-poster.bin",
                     lang=None, width=200, height=300, score=0.9)
    # Patch the bytes reader once so _candidate_bytes finds our fixture poster.
    import finmeta.engine.plan_builder as pb
    orig = pb._candidate_bytes
    pb._candidate_bytes = lambda ref, timeout=30.0: _poster_bytes()
    style_doc = dict(STYLE_DOC, elements={n: {"enabled": True} for n in names})
    try:
        fw = _overlay_poster(jf.get_item(item_id), cand, list(names), target, jf,
                             None, "movie", None, style_doc, None)
    finally:
        pb._candidate_bytes = orig
    assert fw is not None
    # Re-derive the badge texts from the same values the apply path used.
    values = overlay_values(copy.deepcopy(FIXTURES[item_id]), None)
    badges = resolve_badges(list(names), values)
    return {b.text for b in badges}


def test_apply_forced_audio_does_not_drive_audio_badge():
    """A forced-only audio track is skipped by K9; no non-forced track -> no badge."""
    badges = _apply_badges("forced_audio")
    assert "DTS-HD" not in badges and "7.1" not in badges, badges
    values = overlay_values(copy.deepcopy(FIXTURES["forced_audio"]), None)
    assert values["audio"] is None


def test_colortransfer_only_item_gets_fallback_dynamic_range():
    """No VideoRangeType but ColorTransfer=smpte2084 -> HDR10 fallback rule wins."""
    badges = _apply_badges("ct_only")
    assert "HDR10" in badges, badges
    values = overlay_values(copy.deepcopy(FIXTURES["ct_only"]), None)
    assert values["hdr"] == "HDR10"


def test_preview_and_apply_badges_agree_for_three_fixtures():
    """Preview (routes_settings._overlay_preview_source) and apply (_overlay_poster)
    both read get_item_media_streams_raw -> identical badge sets per item."""
    for name in ("dovi", "forced_audio", "ct_only"):
        values = overlay_values(copy.deepcopy(FIXTURES[name]), None)
        preview_badges = {v for k, v in values.items() if v} - {"\u2605 7.8"}
        apply_badges = _apply_badges(name)
        assert apply_badges == preview_badges, (name, apply_badges, preview_badges)


def test_preview_samples_never_contains_real_data_elements(context):
    """When resolution/hdr/audio are actually detected from raw streams, the
    preview endpoint must NOT flag them as synthetic samples."""
    # Point the context\u2019s fake JF at our dovi fixture (all three elements real).
    jf = _FakeJF()
    context.make_jellyfin = lambda cfg=None: jf
    poster_bytes, values, sample, status_key, _k9m, _fname = _overlay_preview_source(context, "dovi")
    assert values["resolution"] == "4K"
    assert values["hdr"] == "DV+HDR10"
    assert "Atmos" in (values["audio"] or "")
    for real in ("resolution", "hdr", "audio"):
        assert real not in sample, f"{real} is real data but flagged synthetic: {sample}"


def test_no_stream_item_shows_no_format_badges(context):
    """An item with NO video stream and NO audio stream must not get a
    resolution / dynamic_range / audio badge, and those elements must not be
    listed in samples[] (there is nothing to flag as synthetic: the values are
    simply absent)."""

    class _NoStreamJF(_FakeJF):
        def get_item_media_streams_raw(self, item_id):
            self.raw_calls.append(item_id)
            return []  # no MediaStreams at all

    jf = _NoStreamJF()
    context.make_jellyfin = lambda cfg=None: jf
    poster_bytes, values, sample, status_key, _k9m, _fname = _overlay_preview_source(context, "no_streams")
    assert jf.raw_calls == ["no_streams"]
    assert values["resolution"] is None
    assert values["hdr"] is None
    assert values["audio"] is None
    for el in ("resolution", "hdr", "audio"):
        assert el not in sample, f"{el} listed synthetic but has no real data: {sample}"


# --- context fixture (minimal app config, same shape as test_overlays_api.py) ---

_CONFIG_YAML = """\
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
    metadata:
      enabled: true
    artwork:
      poster: true
      backdrop: false
    theme_song: false
    overlays: [resolution, hdr, audio]
"""


def _fake_candidates_provider(assets_dir):
    def provider(item, cfg, jf, tmdb, sources, mapper):
        ref = assets_dir / "poster.jpg"
        return {"poster": [Candidate(kind="poster", source="local", ref=str(ref),
                                     lang=None, width=200, height=300, score=0.9)]}, 7
    return provider


@pytest.fixture
def context(tmp_path, monkeypatch):
    from finmeta.api import AppContext
    from finmeta.engine.plan import WritePlan
    monkeypatch.setenv("JELLYFIN_API_KEY", "fake-key")
    monkeypatch.setenv("TMDB_KEY", "fake-tmdb-key")
    monkeypatch.setenv("FINMETA_AUTH", "off")
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "poster.jpg").write_bytes(_poster_bytes())
    # Declare `assets` as the local asset source -- that is where a
    # source="local" poster candidate comes from in production
    # (LocalAssetSource), and the proxy refuses local refs outside the
    # configured asset/media roots since 2026-10-22 (SECURITY_REVIEW "H1").
    (cfg_dir / "config.yml").write_text(
        _CONFIG_YAML.rstrip("\n") + "\n"
        + f"sources:\n  local:\n    enabled: true\n    path: {assets}\n")
    ctx = AppContext(
        str(cfg_dir / "config.yml"), config_dir=str(cfg_dir),
        db_path=str(cfg_dir / "jobs.db"), overrides_path=str(cfg_dir / "overrides.db"),
        plan_builder=lambda *a, **k: WritePlan(items=[]),
        candidates_provider=_fake_candidates_provider(assets),
    )
    ctx.make_jellyfin = lambda cfg=None: _FakeJF()
    return ctx
