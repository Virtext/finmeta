"""S9d4c Fase A step 5: small cleanups from CLAUDE.md's checklist.

- SAMPLE_SIZE used to be exposed to the UI via GET /api/libraries; that field
  went away with the sample endpoint and SamplePreview.tsx on 2026-10-22
  (tests/test_api.py::test_libraries now asserts its absence instead).
- PUT /api/config tolerates GET /api/config's injected per-instance
  api_key_valid field coming back (round-trip safety).
"""
from __future__ import annotations

import yaml as _yaml

from tests.test_providers import persist_client, persist_ctx  # noqa: F401


def test_config_round_trip_tolerates_injected_api_key_valid(persist_client):  # noqa: F811
    """GET /api/config injects api_key_valid into media_apps instances for
    display (S9f1b step 4); PUT /api/config must tolerate it coming back
    instead of 400ing on an unknown field (MediaAppInstance forbids extras),
    and must never persist it to disk."""
    d = persist_client.get("/api/config").json()
    assert "api_key_valid" in d["parsed"]["media_apps"]["sonarr"][0]

    doc = _yaml.safe_load(d["yaml"])
    # Simulate the UI accidentally round-tripping the injected display field.
    doc["media_apps"]["sonarr"][0]["api_key_valid"] = True
    doc["media_apps"]["radarr"][0]["api_key_valid"] = False
    r = persist_client.put("/api/config", json={"yaml": _yaml.safe_dump(doc)})
    assert r.status_code == 200, r.text

    d2 = persist_client.get("/api/config").json()
    assert "api_key_valid" not in d2["yaml"]
    assert d2["parsed"]["media_apps"]["sonarr"][0]["name"] == "S1"
    assert d2["parsed"]["media_apps"]["radarr"][0]["name"] == "R1"
    # Still correctly recomputed fresh on this GET (not just "absent").
    assert d2["parsed"]["media_apps"]["sonarr"][0]["api_key_valid"] is True
