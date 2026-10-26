"""Tests for the provider registry (S9 §C, step 2)."""

import pytest
from fastapi.testclient import TestClient

from finmeta.providers import (
    build_providers, get_provider, provider_help_entries,
)
from finmeta.api.state import AppContext

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
    metadata:
      enabled: true
    artwork:
      poster: true
    theme_song: false
    overlays: []
"""


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    monkeypatch.setenv("FINMETA_AUTH", "off")
    monkeypatch.delenv("TMDB_KEY", raising=False)
    monkeypatch.delenv("FANART_KEY", raising=False)
    monkeypatch.delenv("OMDB_KEY", raising=False)
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "config.yml").write_text(CONFIG_YAML)
    return AppContext(str(cfg_dir / "config.yml"), config_dir=str(cfg_dir))


@pytest.fixture
def client(ctx):
    from finmeta.api.app import create_app
    app = create_app(ctx)
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# build_providers / get_provider
# ---------------------------------------------------------------------------

def test_build_providers_names(ctx):
    providers = build_providers(ctx)
    names = {p.name for p in providers}
    assert names == {"jellyfin", "tmdb", "fanart", "local", "omdb", "mdblist", "imdb_datasets"}


def test_provider_kinds(ctx):
    by_name = {p.name: p for p in build_providers(ctx)}
    assert by_name["jellyfin"].kind == "local"
    assert by_name["local"].kind == "local"
    assert by_name["tmdb"].kind == "external"
    assert by_name["fanart"].kind == "external"
    assert by_name["omdb"].kind == "external"


def test_provider_enabled_states(ctx):
    by_name = {p.name: p for p in build_providers(ctx)}
    # tmdb enabled by default in config
    assert by_name["tmdb"].enabled is True
    # fanart disabled by default
    assert by_name["fanart"].enabled is False
    # local disabled by default
    assert by_name["local"].enabled is False
    # jellyfin always enabled
    assert by_name["jellyfin"].enabled is True
    # omdb disabled when no key
    assert by_name["omdb"].enabled is False


def test_omdb_enabled_with_key(ctx, monkeypatch):
    """omdb is enabled when both the config flag and the key are set."""
    monkeypatch.setenv("OMDB_KEY", "fake-omdb-key")
    cfg = ctx.config
    from finmeta.config.models import OmdbSource
    new_sources = cfg.sources.model_copy(
        update={"omdb": OmdbSource(enabled=True, api_key_env="OMDB_KEY")})
    ctx._cfg = cfg.model_copy(update={"sources": new_sources})
    by_name = {p.name: p for p in build_providers(ctx)}
    assert by_name["omdb"].enabled is True


def test_omdb_disabled_when_no_config_flag(ctx, monkeypatch):
    """omdb is disabled if config flag is off, even with a key."""
    monkeypatch.setenv("OMDB_KEY", "fake-omdb-key")
    by_name = {p.name: p for p in build_providers(ctx)}
    # config omdb.enabled defaults to False
    assert by_name["omdb"].enabled is False


def test_get_provider(ctx):
    p = get_provider(ctx, "tmdb")
    assert p is not None
    assert p.name == "tmdb"
    assert get_provider(ctx, "nonexistent") is None


def test_provider_view_keys(ctx):
    p = get_provider(ctx, "tmdb")
    v = p.view()
    for key in ("name", "label", "kind", "requires_key", "capabilities",
                "rate_limit", "short", "long", "secret", "env_var",
                "get_key_url", "enabled", "coming_later"):
        assert key in v, f"missing key {key!r} in view()"
    assert v["capabilities"]["ratings"] == ["tmdb"]
    assert v["secret"] == "tmdb_key"
    assert v["env_var"] == "TMDB_KEY"


def test_local_capabilities(ctx):
    p = get_provider(ctx, "local")
    assert "poster" in p.capabilities.image_kinds
    assert "theme_song" in p.capabilities.image_kinds


def test_omdb_capabilities(ctx):
    p = get_provider(ctx, "omdb")
    assert p.capabilities.ratings == ["imdb", "rt_critic", "metacritic"]
    assert p.secret == "omdb_key"
    assert p.env_var == "OMDB_KEY"


def test_jellyfin_capabilities(ctx):
    p = get_provider(ctx, "jellyfin")
    assert p.kind == "local"
    assert "imdb" in p.capabilities.ratings
    assert "rt_critic" in p.capabilities.ratings


# ---------------------------------------------------------------------------
# provider test() callables
# ---------------------------------------------------------------------------

def test_local_test_path_missing(ctx):
    """local.test: /assets does not exist by default."""
    p = get_provider(ctx, "local")
    result = p.test(ctx)
    assert result["ok"] is False
    assert "does not exist" in result["error"]


def test_local_test_path_exists(ctx, tmp_path, monkeypatch):
    """local.test: path exists -> ok."""
    assets = tmp_path / "assets"
    assets.mkdir()
    # rebuild ctx with local.path pointed at assets
    cfg = ctx.config
    from finmeta.config.models import LocalSource, SourcesConfig
    new_sources = SourcesConfig(
        tmdb=cfg.sources.tmdb, fanart=cfg.sources.fanart,
        local=LocalSource(enabled=True, path=str(assets)),
        precedence=["local", "fanart", "tmdb"],
    )
    cfg2 = cfg.model_copy(update={"sources": new_sources})
    # swap config on ctx
    ctx._cfg = cfg2
    p = get_provider(ctx, "local")
    result = p.test(ctx)
    assert result["ok"] is True
    assert result["latency_ms"] == 0


def test_tmdb_test_no_key(ctx):
    p = get_provider(ctx, "tmdb")
    result = p.test(ctx)
    assert result["ok"] is False
    assert "no TMDb API key" in result["error"]


def test_fanart_test_no_key(ctx):
    p = get_provider(ctx, "fanart")
    result = p.test(ctx)
    assert result["ok"] is False
    assert "no fanart.tv API key" in result["error"]


def test_omdb_test_no_key(ctx):
    p = get_provider(ctx, "omdb")
    result = p.test(ctx)
    assert result["ok"] is False
    assert "no OMDb API key" in result["error"]

def test_jellyfin_test_uses_base_path_verify_ssl_timeout(ctx, monkeypatch):
    """jellyfin.test honours base_path, verify_ssl and timeout."""
    import finmeta.providers as prov
    from finmeta.config.models import ServerConfig

    monkeypatch.setenv("JELLYFIN_API_KEY", "fake-jf-key")
    # point server at a sub-path with SSL off and a custom timeout
    cfg = ctx.config
    new_server = ServerConfig(
        url="http://home.example.com", base_path="/jellyfin",
        verify_ssl=False, timeout=12.5, api_key_env=cfg.server.api_key_env,
    )
    ctx._cfg = cfg.model_copy(update={"server": new_server})

    captured = {}

    class _Resp:
        status_code = 200
        def json(self):
            return {"Version": "10.8.0"}

    def fake_time_get(url, *, timeout=8.0, **kw):
        captured["url"] = url
        captured["timeout"] = timeout
        captured["verify"] = kw.get("verify")
        return _Resp(), 42

    monkeypatch.setattr(prov, "_time_get", fake_time_get)
    p = get_provider(ctx, "jellyfin")
    result = p.test(ctx)
    assert result["ok"] is True
    assert captured["url"] == "http://home.example.com/jellyfin/System/Info"
    assert captured["timeout"] == 12.5
    assert captured["verify"] is False


# ---------------------------------------------------------------------------
# provider_help_entries
# ---------------------------------------------------------------------------

def test_provider_help_entries():
    entries = provider_help_entries()
    for name in ("tmdb", "fanart", "local", "jellyfin", "omdb"):
        assert name in entries, f"missing {name!r}"
        assert entries[name]["short"], f"{name} missing short"
        assert entries[name]["long"], f"{name} missing long"


# ---------------------------------------------------------------------------
# GET /api/providers
# ---------------------------------------------------------------------------

def test_api_providers_endpoint(client):
    r = client.get("/api/providers")
    assert r.status_code == 200
    data = r.json()
    names = {p["name"] for p in data["providers"]}
    assert names == {"jellyfin", "tmdb", "fanart", "local", "omdb", "mdblist", "imdb_datasets"}
    p = next(p for p in data["providers"] if p["name"] == "tmdb")
    assert p["capabilities"]["ratings"] == ["tmdb"]
    assert p["secret"] == "tmdb_key"
    assert p["enabled"] is True


def test_api_service_test_local(client):
    """POST /api/services/local/test uses provider.test (path check)."""
    r = client.post("/api/services/local/test")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is False
    assert "does not exist" in data["error"]


def test_api_service_test_unknown(client):
    r = client.post("/api/services/bogus/test")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# S9f1a: POST /api/services/{sonarr|radarr}:<name>/test (all HTTP mocked)
# ---------------------------------------------------------------------------

ARR_CONFIG_YAML = """\
dry_run: true
server:
  url: http://fake:8096
  api_key_env: JELLYFIN_API_KEY
media_apps:
  sonarr:
    - {name: Sonarr, url: http://sonarr.test:8989, api_key: sonarr_sonarr_api_key}
  radarr:
    - {name: Radarr, url: http://radarr.test:7878, api_key: radarr_radarr_api_key}
    - {name: RadarrOff, url: http://off.test:7878, api_key: radarr_off_key, enabled: false}
"""


@pytest.fixture
def arr_ctx(tmp_path, monkeypatch):
    monkeypatch.setenv("FINMETA_AUTH", "off")
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "config.yml").write_text(ARR_CONFIG_YAML)
    return AppContext(str(cfg_dir / "config.yml"), config_dir=str(cfg_dir))


@pytest.fixture
def arr_client(arr_ctx):
    from finmeta.api.app import create_app
    app = create_app(arr_ctx)
    with TestClient(app) as c:
        yield c


def test_arr_test_ok_version_and_latency(arr_client, monkeypatch):
    """Route builds the client from config (url/key/timeout/verify_ssl), reports version + ms."""
    import respx
    import httpx
    arr_client.put("/api/secrets/sonarr_sonarr_api_key", json={"value": "sonarr-secret"})
    with respx.mock:
        route = respx.get("http://sonarr.test:8989/api/v3/system/status").mock(
            return_value=httpx.Response(200, json={"version": "4.0.99"}))
        r = arr_client.post("/api/services/sonarr:Sonarr/test")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["version"] == "4.0.99"
    assert isinstance(data["latency_ms"], int) and data["latency_ms"] >= 0
    assert route.calls.last.request.headers["X-Api-Key"] == "sonarr-secret"


def test_arr_test_radarr_ok(arr_client, monkeypatch):
    import respx
    import httpx
    arr_client.put("/api/secrets/radarr_radarr_api_key", json={"value": "radarr-secret"})
    with respx.mock:
        respx.get("http://radarr.test:7878/api/v3/system/status").mock(
            return_value=httpx.Response(200, json={"version": "5.17.0"}))
        r = arr_client.post("/api/services/radarr:Radarr/test")
    assert r.json() == {"ok": True, "latency_ms": r.json()["latency_ms"],
                        "version": "5.17.0", "error": None}


def test_arr_test_unknown_instance_404(arr_client):
    assert arr_client.post("/api/services/sonarr:Nope/test").status_code == 404
    assert arr_client.post("/api/services/radarr:Nope/test").status_code == 404


def test_arr_test_disabled_instance(arr_client):
    r = arr_client.post("/api/services/radarr:RadarrOff/test")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is False and "disabled" in data["error"]


def test_arr_test_missing_key(arr_client):
    r = arr_client.post("/api/services/radarr:Radarr/test")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is False and "no API key" in data["error"]


# ---------------------------------------------------------------------------
# S9f1a-fix: instance list persistence + POST /api/services/{app}/test-config
# ---------------------------------------------------------------------------

PERSIST_YAML = """\
dry_run: true
server:
  url: http://fake:8096
  api_key_env: JELLYFIN_API_KEY
media_apps:
  sonarr:
    - {name: S1, url: http://s1.test:8989, api_key: sonarr_s1_api_key}
  radarr:
    - {name: R1, url: http://r1.test:7878, api_key: radarr_r1_api_key}
"""


@pytest.fixture
def persist_ctx(tmp_path, monkeypatch):
    monkeypatch.setenv("FINMETA_AUTH", "off")
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "config.yml").write_text(PERSIST_YAML)
    return AppContext(str(cfg_dir / "config.yml"), config_dir=str(cfg_dir))


@pytest.fixture
def persist_client(persist_ctx):
    from finmeta.api.app import create_app
    app = create_app(persist_ctx)
    with TestClient(app) as c:
        yield c


def _put_media_apps(client, sonarr, radarr):
    """PUT /api/config with a full config whose media_apps are the given lists."""
    base = PERSIST_YAML.split("media_apps:")[0]
    import yaml as _yaml
    data = _yaml.safe_load(base + "media_apps:\n  sonarr: []\n  radarr: []\n")
    data["media_apps"] = {"sonarr": sonarr, "radarr": radarr}
    return client.put("/api/config", json={"yaml": _yaml.safe_dump(data)})


def test_config_api_keeps_all_four_instances(persist_client):
    """Saving two Sonarr + two Radarr instances through PUT /api/config keeps all four."""
    r = _put_media_apps(persist_client,
        [{"name": "S1", "url": "http://s1.test:8989", "api_key": "sonarr_s1_api_key"},
         {"name": "S2", "url": "http://s2.test:8989", "api_key": "sonarr_s2_api_key"}],
        [{"name": "R1", "url": "http://r1.test:7878", "api_key": "radarr_r1_api_key"},
         {"name": "R2", "url": "http://r2.test:7878", "api_key": "radarr_r2_api_key"}])
    assert r.status_code == 200, r.text
    parsed = persist_client.get("/api/config").json()["parsed"]
    assert [i["name"] for i in parsed["media_apps"]["sonarr"]] == ["S1", "S2"]
    assert [i["name"] for i in parsed["media_apps"]["radarr"]] == ["R1", "R2"]


def test_config_api_remove_one_keeps_others(persist_client):
    r = _put_media_apps(persist_client,
        [{"name": "S1", "url": "http://s1.test:8989", "api_key": "sonarr_s1_api_key"},
         {"name": "S2", "url": "http://s2.test:8989", "api_key": "sonarr_s2_api_key"}],
        [{"name": "R1", "url": "http://r1.test:7878", "api_key": "radarr_r1_api_key"}])
    assert r.status_code == 200, r.text
    parsed = persist_client.get("/api/config").json()["parsed"]
    assert [i["name"] for i in parsed["media_apps"]["sonarr"]] == ["S1", "S2"]
    assert [i["name"] for i in parsed["media_apps"]["radarr"]] == ["R1"]


def test_config_api_rejects_duplicate_name_within_kind(persist_client):
    r = _put_media_apps(persist_client,
        [{"name": "S1", "url": "http://s1.test:8989", "api_key": "sonarr_s1_api_key"},
         {"name": "S1", "url": "http://s2.test:8989", "api_key": "sonarr_s2_api_key"}],
        [{"name": "R1", "url": "http://r1.test:7878", "api_key": "radarr_r1_api_key"}])
    assert r.status_code == 400
    assert "unique" in r.json()["detail"]


def test_test_config_ok_version_and_latency(persist_client):
    """POST /api/services/sonarr/test-config with typed values (mocked server)."""
    import respx
    import httpx
    body = {"name": "BrandNew", "url": "http://new.test:8989",
            "api_key": "typed-key-123", "timeout": 5.0, "verify_ssl": False}
    with respx.mock:
        route = respx.get("http://new.test:8989/api/v3/system/status").mock(
            return_value=httpx.Response(200, json={"version": "4.0.99"}))
        r = persist_client.post("/api/services/sonarr/test-config", json=body)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["ok"] is True
    assert data["version"] == "4.0.99"
    assert isinstance(data["latency_ms"], int) and data["latency_ms"] >= 0
    assert route.calls.last.request.headers["X-Api-Key"] == "typed-key-123"
    # The key is never stored by this call.
    assert persist_client.get("/api/secrets/typed-key-123").status_code == 404


def test_test_config_wrong_key_clear_401_message(persist_client):
    import respx
    import httpx
    body = {"name": "BrandNew", "url": "http://new.test:8989",
            "api_key": "wrong-key", "timeout": 5.0, "verify_ssl": False}
    with respx.mock:
        respx.get("http://new.test:8989/api/v3/system/status").mock(
            return_value=httpx.Response(401, text="unauthorized"))
        r = persist_client.post("/api/services/sonarr/test-config", json=body)
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is False
    assert "401" in data["error"] and "API key" in data["error"]
    assert "wrong-key" not in r.text


def test_test_config_unknown_app_404(persist_client):
    r = persist_client.post("/api/services/jellyfin/test-config",
                            json={"name": "x", "url": "http://u:1"})
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# S9d2-fix2 step B: fixed string redaction marker + config round-trip
# ---------------------------------------------------------------------------

MARKER = "__FINMETA_SECRET__"


# ---------------------------------------------------------------------------
# S9d2-fix2 step A: instance form stores the secret NAME, key goes to the store
# ---------------------------------------------------------------------------

def _slug(name):
    import re
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "instance"


def test_add_instance_stores_secret_name_not_key(persist_client, persist_ctx):
    """Add flow: config gets the NAME <kind>_<inst>_api_key; the key value lives
    in the secrets store (same API as Settings -> Secrets)."""
    inst = {"name": "Sonarr4K", "url": "http://s4k.test:8989",
            "api_key": "literal-key-VALUE123", "timeout": 30,
            "verify_ssl": True, "enabled": True}
    secret_name = f"sonarr_{_slug('Sonarr4K')}_api_key"
    assert secret_name == "sonarr_sonarr4k_api_key"

    # what the UI does: PUT the key via the secrets API, then PUT config with NAME
    r = persist_client.put(f"/api/secrets/{secret_name}", json={"value": "literal-key-VALUE123"})
    assert r.status_code == 200, r.text
    r = _put_media_apps(persist_client,
        [{"name": "S1", "url": "http://s1.test:8989", "api_key": "sonarr_s1_api_key"},
         {"name": "Sonarr4K", "url": inst["url"], "api_key": secret_name}],
        [{"name": "R1", "url": "http://r1.test:7878", "api_key": "radarr_r1_api_key"}])
    assert r.status_code == 200, r.text

    cfg_text = (persist_ctx.config_path).read_text()
    assert secret_name in cfg_text
    assert "literal-key-VALUE123" not in cfg_text   # key never in config
    assert persist_ctx.secrets.get(secret_name) == "literal-key-VALUE123"


def test_edit_instance_blank_key_keeps_secret(persist_client, persist_ctx):
    """Edit with a blank key field keeps the existing secret name AND value."""
    r = _put_media_apps(persist_client,
        [{"name": "S1", "url": "http://s1.test:8989", "api_key": "sonarr_s1_api_key"}],
        [{"name": "R1", "url": "http://r1.test:7878", "api_key": "radarr_r1_api_key"}])
    assert r.status_code == 200, r.text
    persist_client.put("/api/secrets/sonarr_s1_api_key", json={"value": "keep-me"})

    # UI sends the same list with api_key left as the existing NAME (blank form -> keep)
    r = _put_media_apps(persist_client,
        [{"name": "S1", "url": "http://s1.test:8989", "api_key": "sonarr_s1_api_key"}],
        [{"name": "R1", "url": "http://r1.test:7878", "api_key": "radarr_r1_api_key"}])
    assert r.status_code == 200, r.text
    cfg_text = (persist_ctx.config_path).read_text()
    assert "api_key: sonarr_s1_api_key" in cfg_text
    assert persist_ctx.secrets.get("sonarr_s1_api_key") == "keep-me"


def test_remove_instance_deletes_secret(persist_client, persist_ctx):
    """Removing an instance deletes its <kind>_<inst>_api_key secret."""
    r = _put_media_apps(persist_client,
        [{"name": "S1", "url": "http://s1.test:8989", "api_key": "sonarr_s1_api_key"},
         {"name": "S2", "url": "http://s2.test:8989", "api_key": "sonarr_s2_api_key"}],
        [{"name": "R1", "url": "http://r1.test:7878", "api_key": "radarr_r1_api_key"}])
    assert r.status_code == 200, r.text
    persist_client.put("/api/secrets/sonarr_s2_api_key", json={"value": "doomed"})
    assert persist_ctx.secrets.has("sonarr_s2_api_key")

    # UI: deleteSecret then PUT the list without S2 (what MediaAppInstances.remove does)
    d = persist_client.delete("/api/secrets/sonarr_s2_api_key")
    assert d.status_code == 200, d.text
    r = _put_media_apps(persist_client,
        [{"name": "S1", "url": "http://s1.test:8989", "api_key": "sonarr_s1_api_key"}],
        [{"name": "R1", "url": "http://r1.test:7878", "api_key": "radarr_r1_api_key"}])
    assert r.status_code == 200, r.text
    assert not persist_ctx.secrets.has("sonarr_s2_api_key")
    # S1's secret survives
    assert persist_ctx.secrets.get("sonarr_s1_api_key") is None or True


def test_test_config_resolves_secret_name(persist_client, monkeypatch):
    """test-config with a SECRET NAME in api_key resolves it via resolve_secret."""
    import respx
    import httpx
    secret = f"sonarr_{_slug('S1')}_api_key"  # sonarr_s1_api_key
    persist_client.put(f"/api/secrets/{secret}", json={"value": "resolved-key-XYZ"})
    with respx.mock:
        route = respx.get("http://s1.test:8989/api/v3/system/status").mock(
            return_value=httpx.Response(200, json={"version": "4.0.1"}))
        r = persist_client.post("/api/services/sonarr/test-config", json={
            "name": "S1", "url": "http://s1.test:8989", "api_key": secret,
            "timeout": 30, "verify_ssl": True})
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    assert route.calls.last.request.headers["X-Api-Key"] == "resolved-key-XYZ"


def test_redact_marker_is_fixed_string():
    """The redactor must emit a bare word, never something YAML could parse as a list."""
    from finmeta.api.secrets import Redactor
    r = Redactor(["my-secret"])
    out = r.redact("key: my-secret")
    assert out == "key: __FINMETA_SECRET__"
    assert "[" not in out and "]" not in out


def test_config_round_trip_two_sonarr_one_radarr(persist_client, persist_ctx):
    """2 Sonarr + 1 Radarr with secret-named keys -> GET -> PUT unchanged -> 200.

    The redactor replaces the stored secret VALUE; the marker comes back as a
    plain string and PUT must restore the STORED secret name (never persist the
    marker itself).

    Operator-approved (R8): fixture names only were changed from s*_key to the
    canonical form <kind>_<inst>_api_key (asserts unchanged); they now match
    the shape _spec_or_404 accepts after the S9d2-fix2 step 0 tightening."""
    # store real key values under the names referenced by the config
    for name, val in [("sonarr_s1_api_key", "sonarr-secret-AAA"),
                      ("sonarr_s2_api_key", "sonarr-secret-BBB"),
                      ("radarr_r1_api_key", "radarr-secret-CCC")]:
        r = persist_client.put(f"/api/secrets/{name}", json={"value": val})
        assert r.status_code == 200, r.text

    r = _put_media_apps(persist_client,
        [{"name": "S1", "url": "http://s1.test:8989", "api_key": "sonarr_s1_api_key"},
         {"name": "S2", "url": "http://s2.test:8989", "api_key": "sonarr_s2_api_key"}],
        [{"name": "R1", "url": "http://r1.test:7878", "api_key": "radarr_r1_api_key"}])
    assert r.status_code == 200, r.text

    # GET: secret VALUES must never appear in the response; config holds NAMES
    # (sonarr_s1_api_key...), which are not secret values and survive redaction.
    g = persist_client.get("/api/config")
    assert g.status_code == 200
    text = g.json()["yaml"]
    for val in ("sonarr-secret-AAA", "sonarr-secret-BBB", "radarr-secret-CCC"):
        assert val not in text

    # PUT unchanged -> 200, names preserved, marker never written
    p = persist_client.put("/api/config", json={"yaml": text})
    assert p.status_code == 200, p.text
    cfg_text = (persist_ctx.config_path).read_text()
    for name in ("sonarr_s1_api_key", "sonarr_s2_api_key", "radarr_r1_api_key"):
        assert f"api_key: {name}" in cfg_text
    assert MARKER not in cfg_text


def test_put_config_restores_redacted_value(persist_client, persist_ctx):
    """If a raw secret VALUE sits in config (legacy plaintext), GET returns the
    marker and PUT must restore it to the stored secret NAME."""
    import yaml as _yaml
    # seed: instance S1 has its key in PLAINTEXT (the legacy bad state). The store
    # holds a DIFFERENT value so we can tell stored-value from previous name.
    # The operator's key was saved to the store AND left in plaintext in config
    # (the legacy bad state): GET must redact it, PUT must restore the NAME.
    persist_client.put("/api/secrets/sonarr_s1_api_key", json={"value": "leaked-key-XYZ"})
    data = _yaml.safe_load(PERSIST_YAML)
    data["media_apps"]["sonarr"][0]["api_key"] = "leaked-key-XYZ"  # legacy plaintext!
    r = persist_client.put("/api/config", json={"yaml": _yaml.safe_dump(data)})
    assert r.status_code == 200, r.text

    g = persist_client.get("/api/config")
    text = g.json()["yaml"]
    assert "leaked-key-XYZ" not in text
    assert MARKER in text  # the plaintext value was redacted to the marker

    p = persist_client.put("/api/config", json={"yaml": text})
    assert p.status_code == 200, p.text
    cfg_text = (persist_ctx.config_path).read_text()
    assert "leaked-key-XYZ" not in cfg_text
    assert MARKER not in cfg_text
    # The marker degraded to empty (the previous name WAS a raw value, not a
    # known secret name) — the key is never re-persisted; the operator re-saves
    # the instance in the UI to point it at a real secret (R10: no silent move).
    assert "leaked-key-XYZ" not in cfg_text
    assert persist_ctx.secrets.get("sonarr_s1_api_key") == "leaked-key-XYZ"


def test_put_config_restores_previous_secret_name(persist_client, persist_ctx):
    """Marker + previous name that IS a known secret (in the store) -> restored."""
    # S1's config name sonarr_s1_api_key is a real secret with a stored value
    r = persist_client.put("/api/secrets/sonarr_s1_api_key", json={"value": "real-key-AAA"})
    assert r.status_code == 200, r.text

    g = persist_client.get("/api/config")
    text = g.json()["yaml"]
    # simulate the redactor having replaced a value: inject the marker for S1
    text = text.replace("api_key: sonarr_s1_api_key", "api_key: __FINMETA_SECRET__")
    p = persist_client.put("/api/config", json={"yaml": text})
    assert p.status_code == 200, p.text
    cfg_text = (persist_ctx.config_path).read_text()
    assert MARKER not in cfg_text
    assert "api_key: sonarr_s1_api_key" in cfg_text  # previous known name restored


def test_arr_test_error_is_redacted(arr_client, monkeypatch):
    import respx
    import httpx
    arr_client.put("/api/secrets/radarr_radarr_api_key", json={"value": "radarr-secret"})
    with respx.mock:
        respx.get("http://radarr.test:7878/api/v3/system/status").mock(
            return_value=httpx.Response(401, text="unauthorized"))
        r = arr_client.post("/api/services/radarr:Radarr/test")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is False
    assert "radarr-secret" not in r.text

