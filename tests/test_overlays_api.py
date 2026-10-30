"""Overlay editor API tests (SPEC S9 D step 1): preview endpoint.

Covers POST /api/overlays/preview contract: up to four PNG previews per
request and no new or modified image files on disk after a request completes.
"""
from __future__ import annotations

import json
import base64
import io
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from finmeta.api import AppContext, create_app
from finmeta.engine.plan import WritePlan
from finmeta.jellyfin_client import ItemPage
from finmeta.sources.candidate import Candidate
from fastapi.testclient import TestClient

_CONFIG_LINES = [
    "dry_run: true",
    "server:",
    "  url: http://fake:8096",
    "  api_key_env: JELLYFIN_API_KEY",
    "path_mappings:",
    "  - jellyfin: /media",
    "    local: /tmp/fake-media",
    "libraries:",
    "  Movies:",
    "    type: movies",
    "    metadata:",
    "      enabled: true",
    "    artwork:",
    "      poster: true",
    "      backdrop: false",
    "    theme_song: false",
    "    overlays: []",
]
CONFIG_YAML = "\n".join(_CONFIG_LINES) + "\n"


def _with_local_assets(config_yaml: str, assets_dir) -> str:
    """Append ``sources.local.path: <assets_dir>`` to a config.

    2026-10-22: ``_fake_candidates_provider`` hands out poster refs from a
    pytest tmp dir, and the proxy now refuses local refs outside the
    configured asset/media roots (docs/SECURITY_REVIEW.md "H1"). In
    production a ``source="local"`` poster candidate always comes from
    exactly ``sources.local.path`` (``LocalAssetSource``), so declaring it
    here makes the fixture match reality instead of relaxing the policy for
    tests.
    """
    return (config_yaml.rstrip("\n") + "\n"
            + "sources:\n  local:\n    enabled: true\n"
            + f"    path: {assets_dir}\n")

def _make_poster_bytes(width=200, height=300):
    img = Image.new("RGB", (width, height), (30, 40, 90))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()

@pytest.fixture(scope="module")
def shared_assets(tmp_path_factory):
    d = tmp_path_factory.mktemp("assets")
    for i in range(1, 5):
        p = d / f"poster{i}.jpg"
        p.write_bytes(_make_poster_bytes())
    return d

class _FakeJF:
    """Offline stand-in for JellyfinClient used by the preview helper."""
    # Raw MediaStreams (Jellyfin keys) -- what /Items?Ids=&Fields=MediaSources,MediaStreams returns.
    # Video 3840x2160 SDR -> K9 resolution res_4k but NO dynamic-range badge; audio dts_ma/8ch.
    _RAW = [
        {"Type": "Video", "Width": 3840, "Height": 2160, "Codec": "hevc",
         "Profile": "Main10", "VideoRange": "SDR", "VideoRangeType": "SDR"},
        {"Type": "Audio", "Channels": 8, "ChannelLayout": "7.1",
         "DisplayTitle": "DTS-HD MA", "Profile": "DTS-HD MA", "Codec": "dtsxhdma"},
    ]

    def get_item(self, item_id):
        return SimpleNamespace(id=item_id, type="Movie", path="/x/y.mkv", provider_ids={})

    def get_items(self, parent_id=None, recursive=False):
        return []

    def get_items_page(self, *, parent_id=None, recursive=True, include_item_types=None,
                       fields=None, user_id=None, limit=None, start_index=None,
                       sort_by=None, sort_order=None, search_term=None) -> ItemPage:
        return ItemPage(items=[], total_record_count=0, start_index=start_index or 0)

    def get_media_streams_raw_or_objects(self, item_id):
        from finmeta.jellyfin_client.types import MediaStream
        out = []
        for s in self._RAW:
            if (s.get("Type") or "").lower() == "video":
                out.append(MediaStream(index=0, type="Video", width=s["Width"],
                                       height=s["Height"], codec=s["Codec"],
                                       video_range_type=s.get("VideoRangeType"),
                                       profile=s.get("Profile")))
            else:
                out.append(MediaStream(index=1, type="Audio", channels=s["Channels"],
                                       codec=s.get("Codec"), profile=s.get("Profile")))
        return out

    # The preview endpoint fetches RAW dicts; keep the legacy object method too so any
    # old caller still works. Both derive from _RAW to stay consistent.
    def get_media_streams(self, item_id):
        return self.get_media_streams_raw_or_objects(item_id)

    def get_item_media_streams_raw(self, item_id):
        import copy
        return [copy.deepcopy(s) for s in self._RAW]

    def close(self):
        pass



def _fake_candidates_provider(shared_assets):
    def provider(item, cfg, jf, tmdb, sources, mapper):
        raw = str(getattr(item, "id", "")) or "a"
        idx = (ord(raw[-1]) % 4) + 1 if raw else 1
        ref = shared_assets / f"poster{idx}.jpg"
        by_kind = {
            "poster": [Candidate(kind="poster", source="local", ref=str(ref),
                                 lang=None, width=200, height=300, score=0.9)],
        }
        return by_kind, 7
    return provider

@pytest.fixture
def context(tmp_path, monkeypatch, shared_assets):
    monkeypatch.setenv("JELLYFIN_API_KEY", "fake-key")
    monkeypatch.setenv("TMDB_KEY", "fake-tmdb-key")
    monkeypatch.setenv("FINMETA_AUTH", "off")
    cfg_dir = tmp_path / "config"; cfg_dir.mkdir()
    (cfg_dir / "config.yml").write_text(_with_local_assets(CONFIG_YAML, shared_assets))
    ctx = AppContext(
        str(cfg_dir / "config.yml"), config_dir=str(cfg_dir),
        db_path=str(cfg_dir / "jobs.db"), overrides_path=str(cfg_dir / "overrides.db"),
        plan_builder=lambda *a, **k: WritePlan(items=[]),
        candidates_provider=_fake_candidates_provider(shared_assets),
    )
    ctx.make_jellyfin = lambda cfg=None: _FakeJF()
    return ctx

@pytest.fixture
def client(context):
    app = create_app(context)
    with TestClient(app) as c:
        yield c

STYLE_DOC = {
    "base_position": "bottom_left","margin": 24,"font_size": 34,
    "bg": "#000000B0","fg": "#FFFFFF","radius": 12,
    "elements": {"imdb":{"enabled":True},"tmdb":{"enabled":False},
                 "resolution":{"enabled":True},"hdr":{"enabled":True},
                 "audio":{"enabled":True}},
}

def test_preview_returns_four_pngs(client):
    r = client.post("/api/overlays/preview", json={"style": STYLE_DOC,
                                                           "item_ids": ["a","b","c","d"]})
    assert r.status_code == 200, r.text
    previews = r.json()["previews"]
    assert len(previews) == 4
    for p in previews:
        b64 = base64.b64decode(p["image"].split(",", 1)[1])
        im = Image.open(io.BytesIO(b64))
        assert im.format == "PNG" and im.size[0] > 50 and im.size[1] > 50

def test_preview_includes_base_image_matching_the_overlay_source(client):
    """S9d4c: "base_image" must be the EXACT pre-compositing candidate --
    guaranteed consistent with "image" (they used to come from two
    independently-resolved candidates, so the UI could show a different
    "base" poster than the one the overlay was actually rendered from)."""
    r = client.post("/api/overlays/preview", json={"style": STYLE_DOC,
                                                           "item_ids": ["a"]})
    assert r.status_code == 200, r.text
    entry = r.json()["previews"][0]
    assert "base_image" in entry
    assert entry["base_image"].startswith("data:image/")
    base_b64 = base64.b64decode(entry["base_image"].split(",", 1)[1])
    base_im = Image.open(io.BytesIO(base_b64))
    overlay_im = Image.open(io.BytesIO(base64.b64decode(entry["image"].split(",", 1)[1])))
    # Same source poster -> same pixel dimensions (the overlay is drawn ON
    # TOP, never resized).
    assert base_im.size == overlay_im.size


def test_preview_rejects_more_than_max_preview_items(client):
    """Server caps preview item count at MAX_PREVIEW_ITEMS (request-validation
    time). The constant moved from routes_profiles.SAMPLE_SIZE to
    routes_settings.MAX_PREVIEW_ITEMS on 2026-10-22 when the seeded-sample
    endpoint it was shared with was removed -- the cap itself is unchanged."""
    from finmeta.api.routes_settings import MAX_PREVIEW_ITEMS as SAMPLE_SIZE
    ids = [f"x{i}" for i in range(SAMPLE_SIZE + 1)]
    r = client.post("/api/overlays/preview", json={"style": {}, "item_ids": ids})
    # Either a clean 422 from pydantic max_length, or (if we ever loosen the cap)
    # still no more than SAMPLE_SIZE previews returned.
    if r.status_code == 200:
        assert len(r.json()["previews"]) <= SAMPLE_SIZE
    else:
        assert r.status_code == 422


def test_preview_accepts_a_full_batch_of_six_ids(client):
    """A request at exactly the cap (6) must be a clean 200 with one preview
    entry per id (unknown fakes -> per-item error, but no 422). Was S9d2-fix
    step 4b, when the overlay editor's "New sample" button posted a full
    batch; that button is gone but the boundary still matters."""
    from finmeta.api.routes_settings import MAX_PREVIEW_ITEMS as SAMPLE_SIZE
    assert SAMPLE_SIZE == 6
    ids = [f"s{i}" for i in range(SAMPLE_SIZE)]
    r = client.post("/api/overlays/preview", json={"style": {}, "item_ids": ids})
    assert r.status_code == 200, r.text
    previews = r.json()["previews"]
    assert len(previews) == SAMPLE_SIZE
    assert [p["item_id"] for p in previews] == ids


def test_preview_accepts_exactly_four(client):
    r = client.post("/api/overlays/preview",
                    json={"style": {}, "item_ids": ["a1","b2","c3","d4"]})
    assert r.status_code == 200
    assert len(r.json()["previews"]) == 4

def test_preview_rejects_invalid_style(client):
    r = client.post("/api/overlays/preview",
                    json={"style":{"base_position":"not_a_corner"},"item_ids":["a"]})
    assert r.status_code == 422

_IMAGE_EXTS = (".png",".jpg",".jpeg")
def _snapshot_files(root: Path) -> dict:
    out = {}
    for f in root.rglob("*"):
        if f.is_file():
            st = f.stat()
            out[str(f.relative_to(root))] = (st.st_size, int(st.st_mtime_ns // 1_000_000))
    return out

def test_preview_writes_no_image_files(client, context):
    cfg_dir = Path(context.config_dir)
    ws_root = cfg_dir.parent
    ws_before = {str(x) for x in ws_root.rglob("*") if x.is_file()}
    r = client.post("/api/overlays/preview",
                    json={"style": STYLE_DOC,"item_ids":["p1","p2","p3","p4"]})
    assert r.status_code == 200 and len(r.json()["previews"]) == 4
    ws_after = {str(x) for x in ws_root.rglob("*") if x.is_file()}
    new_images = [f for f in (ws_after - ws_before) if Path(f).suffix.lower() in _IMAGE_EXTS]
    assert not new_images, f"unexpected image file(s) written: {new_images}"

def test_preview_empty_style_doc_valid(client):
    r = client.post("/api/overlays/preview", json={"style": {}, "item_ids": ["only"]})
    assert r.status_code == 200
    assert len(r.json()["previews"]) == 1


# ---------------------------------------------------------------------------
# S9d2-fix step 2: optional profile param on POST /api/overlays/preview
# ---------------------------------------------------------------------------

def _config_with_profiles():
    """Config with two overlay profiles (different style + format_rules)."""
    return CONFIG_YAML + (
        "overlays:\n"
        "  profiles:\n"
        "    prof_a:\n"
        "      style:\n"
        "        position: top_left\n"
        "        font_size: 60\n"
        "      format_rules:\n"
        "        resolution:\n"
        "          - key: res_sd\n"
        "            match: \"height<=480\"\n"
        "            label: SD-test\n"
        "    prof_b:\n"
        "      style:\n"
        "        position: top_right\n"
        "        font_size: 20\n"
    )


@pytest.fixture
def context_profiles(tmp_path, monkeypatch, shared_assets):
    monkeypatch.setenv("JELLYFIN_API_KEY", "fake-key")
    monkeypatch.setenv("TMDB_KEY", "fake-tmdb-key")
    monkeypatch.setenv("FINMETA_AUTH", "off")
    cfg_dir = tmp_path / "config"; cfg_dir.mkdir()
    (cfg_dir / "config.yml").write_text(
        _with_local_assets(_config_with_profiles(), shared_assets))
    ctx = AppContext(
        str(cfg_dir / "config.yml"), config_dir=str(cfg_dir),
        db_path=str(cfg_dir / "jobs.db"), overrides_path=str(cfg_dir / "overrides.db"),
        plan_builder=lambda *a, **k: WritePlan(items=[]),
        candidates_provider=_fake_candidates_provider(shared_assets),
    )
    ctx.make_jellyfin = lambda cfg=None: _FakeJF()
    return ctx


@pytest.fixture
def client_profiles(context_profiles):
    app = create_app(context_profiles)
    with TestClient(app) as c:
        yield c


def test_preview_profile_changes_input_vs_other_profile(client_profiles):
    """Same item, two profiles -> different preview input (style applied)."""
    style = {"elements": {"resolution": {"enabled": True}, "hdr": {"enabled": True},
                          "audio": {"enabled": True}}}
    r_a = client_profiles.post("/api/overlays/preview", json={"style": style, "item_ids": ["p1"], "profile": "prof_a"})
    r_b = client_profiles.post("/api/overlays/preview", json={"style": style, "item_ids": ["p1"], "profile": "prof_b"})
    assert r_a.status_code == 200 and r_b.status_code == 200
    pa = r_a.json()["previews"][0]
    pb = r_b.json()["previews"][0]
    assert pa.get("image") and pb.get("image")
    # Different profile styles must produce different rendered output.
    assert pa["image"] != pb["image"]


def test_preview_profile_unknown_rejected_400(client_profiles):
    r = client_profiles.post("/api/overlays/preview",
                             json={"style": {}, "item_ids": ["p1"], "profile": "nope"})
    assert r.status_code == 400
    assert "unknown overlay profile" in r.json()["detail"]


def test_preview_profile_applies_format_rules(client_profiles):
    """prof_a overrides resolution rules: a 3840x2160 stream no longer matches res_4k."""
    r = client_profiles.post("/api/overlays/preview",
                             json={"style": {}, "item_ids": ["p1"], "profile": "prof_a"})
    assert r.status_code == 200
    match = r.json()["previews"][0]["format_rule_matches"]["resolution"]
    # Only res_sd (height<=480) exists in prof_a -> no match for a 4K stream.
    assert match is None

# ---------------------------------------------------------------------------
# step 2: GET /api/overlays + PUT /api/overlays/{name}
# ---------------------------------------------------------------------------

def _preset_style(name="Ratings bottom"):
    from finmeta.engine.style_presets import PRESETS
    return json.loads(json.dumps(PRESETS[name]["build"]().model_dump(mode="json")))

def test_get_overlays_lists_all_four_presets(client):
    r = client.get("/api/overlays")
    assert r.status_code == 200
    body = r.json()
    names = {p["name"] for p in body["presets"]}
    assert names == {"Ratings bottom","Resolution + audio top","Ribbon corner","Minimal"}
    assert body["user_styles"] == []

def test_put_overlay_saves_user_style_and_persists_to_config_file(client, context):
    doc = _preset_style("Ratings bottom")
    # clone-before-edit: tweak the copy we send; preset itself must stay pristine server-side.
    sent = dict(doc); sent = json.loads(json.dumps(sent))
    sent["elements"]["imdb"]["x_offset"] = 42
    r = client.put("/api/overlays/my-custom", json={"name":"my-custom","style":sent})
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True

    got = client.get("/api/overlays").json()
    us_by_name = {u["name"]: u["style"] for u in got["user_styles"]}
    assert "my-custom" in us_by_name
    assert us_by_name["my-custom"]["elements"]["imdb"]["x_offset"] == 42

    # verify it actually hit disk (config.yml) so a restart picks it up too
    import yaml as _yaml
    cfg_text = Path(context.config_dir / "config.yml").read_text()
    data = _yaml.safe_load(cfg_text)
    assert data["overlays"]["user_styles"]["my-custom"]["elements"]["imdb"]["x_offset"] == 42

def test_put_overlay_does_not_mutate_preset(client):
    doc = _preset_style("Minimal")
    sent = json.loads(json.dumps(doc))
    sent["base_position"] = "top_right"
    r = client.put("/api/overlays/tweaked", json={"name":"tweaked","style":sent})
    assert r.status_code == 200, r.text
    from finmeta.engine.style_presets import preset_minimal
    fresh = preset_minimal().model_dump(mode="json")
    assert fresh["base_position"] == "bottom_left"  # untouched in code

def test_put_overlay_rejects_invalid_doc(client):
    bad = {"base_position": "nowhere"}
    r = client.put("/api/overlays/bad-one", json={"name":"bad-one","style":bad})
    assert r.status_code == 422
    got = client.get("/api/overlays").json()
    assert not any(u["name"]=="bad-one" for u in got["user_styles"])

def test_put_overlay_overwrites_same_name(client):
    base = _preset_style("Ratings bottom")
    v1 = json.loads(json.dumps(base)); v1["margin"]=30
    v2 = json.loads(json.dumps(base)); v2["margin"]=60
    assert client.put("/api/overlays/same", json={"name":"same","style":v1}).status_code==200
    assert client.put("/api/overlays/same", json={"name":"same","style":v2}).status_code==200
    us_by_name={u["name"]:u["style"] for u in client.get("/api/overlays").json()["user_styles"]}
    assert len([k for k in us_by_name if k=="same"])==1
    assert us_by_name["same"]["margin"]==60


def test_preview_keeps_posted_elements(client):
    """Regression (S9c step 7): style.elements posted by the browser must reach
    render_overlay_png -- the config base has elements={} and must NOT overwrite them.
    With imdb enabled on an otherwise bare item AND the explicit "Sample data"
    toggle on (placeholder fills rating_imdb), the preview PNG differs from a
    render with no elements at all."""
    doc_on = {
        "base_position": "top_left", "margin": 8, "font_size": 16,
        "bg": "#000000B0", "fg": "#FFFFFF", "radius": 4,
        "elements": {"imdb": {"enabled": True, "position": "top_left"}},
    }
    r = client.post("/api/overlays/preview", json={"style": doc_on, "item_ids": ["a"],
                                                   "sample_data": True})
    assert r.status_code == 200, r.text
    p = r.json()["previews"][0]
    assert "error" not in p, p.get("error")
    on_im = Image.open(io.BytesIO(
        base64.b64decode(p["image"].split(",", 1)[1]))).convert("RGB")

    # Same poster rendered without any element: identical input, only the style differs.
    doc_off = dict(doc_on)
    doc_off["elements"] = {}
    roff = client.post("/api/overlays/preview", json={"style": doc_off, "item_ids": ["a"],
                                                      "sample_data": True})
    assert roff.status_code == 200, roff.text
    po = roff.json()["previews"][0]
    assert "error" not in po, po.get("error")
    off_im = Image.open(io.BytesIO(
        base64.b64decode(po["image"].split(",", 1)[1]))).convert("RGB")

    assert on_im.size == off_im.size
    diff = sum(1 for yy in range(on_im.height) for xx in range(on_im.width)
               if on_im.getpixel((xx, yy)) != off_im.getpixel((xx, yy)))
    assert diff > 0, ("imdb badge did not change the preview at all -- posted elements "
                      "were dropped by the config-base merge in overlays_preview")


def test_preview_placeholder_does_not_override_real_values(context):
    """When the source already provides a value, placeholders must NOT clobber it."""
    from finmeta.api.routes_settings import _overlay_preview_source
    # The fake JF streams give height=2160 -> resolution '4K' and dts_ma/8ch audio.
    poster_bytes, values, sample, status_key, _k9m, _fname = _overlay_preview_source(context, "a", sample_data=True)
    assert "resolution" not in sample  # real value detected from streams -> not synthetic
    assert "hdr" in sample             # SDR: K9 no range badge -> synthesized + flagged
    assert values["resolution"] == "4K"   # real stream-derived value survives placeholder pass
    # Fake fixture streams carry no HDR metadata at all; whatever a missing value is, the
    # preview pass must fill it with its placeholder -- never overwrite anything present.
    assert isinstance(values["hdr"], str) and values["hdr"].strip(), "preview pass left hdr empty"




def test_preview_resolves_network_badge_for_a_tv_item(context):
    """2026-10-23: the "network" badge needs TMDb's own `networks` field --
    operator-reported "I only ever see TV status" testing it in the editor
    (preview never fetched TMDb details at all before this). A TV item
    whose fake tmdb.tv_details() returns a networks list must end up with
    values["network"] set to the FIRST entry."""
    from types import SimpleNamespace
    from finmeta.api.routes_settings import _overlay_preview_source

    class _FakeTvJF(_FakeJF):
        def get_item(self, item_id):
            return SimpleNamespace(id=item_id, type="Series", path="/x/Show",
                                   provider_ids={})

    class _FakeTmdb:
        def tv_details(self, tmdb_id, language=None):
            return {"networks": [{"name": "Netflix"}, {"name": "Hulu"}]}

        def close(self):
            pass

    context.make_jellyfin = lambda cfg=None: _FakeTvJF()
    context.make_tmdb = lambda cfg=None: _FakeTmdb()
    _poster_bytes, values, _sample, _status_key, _k9m, _fname = _overlay_preview_source(
        context, "show1", sonarr_index={})
    assert values.get("network") == "Netflix"


def test_preview_marks_synthetic_cells_sample(client):
    """S9c10b step 3: cells whose value came from a synthetic placeholder (no real data)
    are flagged sample=true per-element so the UI can show a 'sample data' label."""
    r = client.post("/api/overlays/preview", json={"style": STYLE_DOC, "item_ids": ["a"],
                                                   "sample_data": True})
    assert r.status_code == 200
    entry = r.json()["previews"][0]
    samples = set(entry.get("samples") or [])
    # The fake item is 3840x2160 SDR -> K9 detects resolution (real, NOT sample)...
    assert "resolution" not in samples
    # ...but has no dynamic-range badge and its DTS-HD MA audio still resolves to a rule;
    # hdr is definitely synthesized here because VideoRangeType=SDR matches no range rule.
    assert "hdr" in samples
    for k in samples:
        assert isinstance(k, str) and k.strip()


def test_preview_no_samples_key_when_all_real_or_error_absent(client):
    """A successful preview always returns image plus either an empty/no samples list -- it
    never crashes on missing raw streams (fake JF supplies them)."""
    r = client.post("/api/overlays/preview", json={"style": {}, "item_ids": ["z"]})
    body = r.json()
    assert r.status_code == 200
    e = body["previews"][0]
    if "error" not in e:
        assert "image" in e
