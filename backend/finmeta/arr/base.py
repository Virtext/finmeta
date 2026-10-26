"""Shared base for the read-only Sonarr/Radarr clients (S9f1a).

Both apps speak the same v3 API shape: JSON over ``GET`` with an ``X-Api-Key``
header. The base client enforces read-only behaviour structurally — its only
request method is :meth:`ArrClient._get` and it raises on any other verb, so a
subclass cannot accidentally issue a write.
"""
from __future__ import annotations

from typing import Any, Dict, Optional
from urllib.parse import urlparse

import httpx


class ArrError(RuntimeError):
    """A non-2xx response from a Sonarr/Radarr API."""


class ArrClient:
    """Base read-only v3 API client (Sonarr and Radarr)."""

    app = "arr"  # overridden by subclasses ("sonarr" | "radarr")

    def __init__(self, url: str, *, api_key: str = "", timeout: float = 30.0,
                 verify_ssl: bool = True,
                 client: Optional[httpx.Client] = None) -> None:
        self.url = str(url).rstrip("/")
        self.api_key = api_key or ""
        # The instance URL is operator-supplied and arrives here straight from
        # POST /api/services/{sonarr|radarr}/test-config, so restrict the
        # scheme (CLAUDE.md §5 SSRF). Redirects stay off (httpx's default), so
        # an upstream cannot bounce the request elsewhere either. This remains
        # an admin-only BLIND request -- only {ok, latency_ms, version} comes
        # back, never a response body; see docs/SECURITY_REVIEW.md "M5".
        scheme = urlparse(self.url).scheme
        if scheme not in ("http", "https"):
            raise ValueError(
                f"{self.app} URL must be http:// or https:// (got {scheme!r})")
        self._external = client is not None
        self._client = client or httpx.Client(
            base_url=self.url, timeout=timeout, verify=verify_ssl)

    # -- lifecycle ----------------------------------------------------------
    def close(self) -> None:
        if not self._external:
            self._client.close()

    def __enter__(self) -> "ArrClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- low level (GET only) -----------------------------------------------
    def _headers(self) -> Dict[str, str]:
        return {"Accept": "application/json", "X-Api-Key": self.api_key}

    @staticmethod
    def _raise_for(resp: httpx.Response) -> None:
        if resp.status_code >= 400:
            raise ArrError(
                f"{resp.request.method} {resp.request.url.path}: "
                f"HTTP {resp.status_code}: {resp.text[:300]}")

    def _get(self, path: str, *, params: Optional[dict] = None) -> Any:
        """The ONLY request method. Refuses to be used for any write verb."""
        resp = self._client.request("GET", path, headers=self._headers(),
                                    params=params)
        self._raise_for(resp)
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    # -- shared endpoint ----------------------------------------------------
    def system_status(self) -> Dict[str, Any]:
        """``GET /api/v3/system/status`` — version + uptime of the instance."""
        data = self._get("/api/v3/system/status") or {}
        return {
            "version": data.get("version"),
            "name": data.get("name"),
            "uptime": data.get("uptime"),
            "state": data.get("state"),
        }
