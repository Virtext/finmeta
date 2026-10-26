"""Unit tests for the secrets store + redaction (S9 §A)."""
import pytest

from finmeta.api.secrets import (
    SecretsStore, Redactor, redact, secret_specs, secret_status,
)
from finmeta.config.models import Config


@pytest.fixture
def store(tmp_path):
    return SecretsStore(tmp_path / "secrets.yml", secret_key=None)


def test_set_get_roundtrip(store):
    store.set("tmdb_key", "value-0a3f9")
    assert store.get("tmdb_key") == "value-0a3f9"
    assert store.hint("tmdb_key") == "••••a3f9"


def test_file_mode_0600(store):
    store.set("x", "y")
    assert (store.path.stat().st_mode & 0o777) == 0o600


def test_plaintext_at_rest_without_key(store):
    store.set("k", "visible-secret-XYZ")
    assert "visible-secret-XYZ" in store.path.read_text()


def test_reload_roundtrip(tmp_path):
    p = tmp_path / "s.yml"
    SecretsStore(p).set("k", "abc-123")
    assert SecretsStore(p).get("k") == "abc-123"


def test_fernet_encrypts_at_rest(tmp_path):
    p = tmp_path / "s.yml"
    store = SecretsStore(p, secret_key="my-key")
    store.set("k", "top-secret-42")
    assert "top-secret-42" not in p.read_text()
    assert SecretsStore(p, secret_key="my-key").get("k") == "top-secret-42"


def test_fernet_flag(tmp_path):
    assert SecretsStore(tmp_path / "a.yml", secret_key=None).encrypted_at_rest is False
    assert SecretsStore(tmp_path / "b.yml", secret_key="k").encrypted_at_rest is True


def test_delete(store):
    store.set("k", "v")
    assert store.delete("k") is True
    assert store.get("k") is None
    assert store.delete("k") is False


def test_names_values(store):
    store.set("b", "2")
    store.set("a", "1")
    assert store.names() == ["a", "b"]
    assert set(store.values()) == {"1", "2"}


def test_hint_short_value():
    assert SecretsStore.hint_value("") == ""
    assert SecretsStore.hint_value("ab") == "••••"
    assert SecretsStore.hint_value("abcd1234") == "••••1234"


def test_redact_replaces_values():
    r = Redactor(["my-secret", "other"])
    assert r.redact("a my-secret b other c") == "a __FINMETA_SECRET__ b __FINMETA_SECRET__ c"
    assert r.redact("nothing here") == "nothing here"
    assert redact("x other", ["other"]) == "x __FINMETA_SECRET__"


def test_redact_substring():
    r = Redactor(["my-secret"])
    assert r.redact("my-secret") == "__FINMETA_SECRET__"


def test_secret_specs_and_status(store, monkeypatch):
    cfg = Config(server={"url": "http://x"})
    specs = {s.name: s for s in secret_specs(cfg)}
    monkeypatch.delenv(specs["fanart_key"].env_var, raising=False)
    # none set -> source none
    assert secret_status(store, specs["fanart_key"]) == \
        {"configured": False, "source": "none", "hint": ""}
    # ui set
    store.set("fanart_key", "uv-9999")
    assert secret_status(store, specs["fanart_key"]) == \
        {"configured": True, "source": "ui", "hint": "••••9999"}
    # env overrides ui
    monkeypatch.setenv(specs["fanart_key"].env_var, "env-7777")
    st = secret_status(store, specs["fanart_key"])
    assert st["source"] == "env" and st["hint"] == "••••7777"
