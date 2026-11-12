"""Image / audio proxy (SPEC §11: the browser never hits TMDb/fanart directly).

A candidate is addressed by a base64url token of ``"<kind>|<ref>"``. The
proxy endpoint resolves the ref (a local file or a remote URL) and streams the
bytes back, guessing a content type from the kind.

**The token is encoding, not authentication** -- anyone who can reach the
route can mint one for any ``ref`` they like (the UI itself builds tokens
client-side from refs it got out of the plan/candidates API, so server-side
signing would mean reworking both sides). :func:`fetch` therefore validates
the ref against an explicit policy instead of trusting it:

* remote refs: http/https only, and the host must be a known provider image
  host (:data:`ALLOWED_REMOTE_HOSTS`) -- re-checked after redirects;
* local refs: must resolve inside one of the caller-supplied allowed roots.

Without this the route was a full arbitrary-file-read -- ``GET /api/proxy/``
``<base64("metadata|<config>/secrets.yml")>`` returned the file verbatim, and
the redaction middleware never sees it because the response is not JSON --
plus an SSRF primitive against anything the container can reach. Found and
fixed 2026-10-22; see docs/SECURITY_REVIEW.md "H1".

The policy itself (allowed hosts, allowed-root containment, the size cap)
now lives in ``engine.candidate_ref`` and is re-exported below rather than
redefined here -- ``api/overrides.py`` and ``engine/plan_builder.py`` need
the exact same answer to "is this ref allowed" for a candidate a USER picks
via ``PUT /api/items/{id}/candidates``, which had NO validation at all until
``engine.candidate_ref`` was added (audit 2026-11-11, "A1"/"B5": an
operator-reachable route let any local path become a ``FileWrite.source_ref``,
written to the media tree by the executor and then servable back through
this very proxy). This module keeps its own streaming fetch below -- capping
mid-download suits the browser-facing route better than ``candidate_ref``'s
fetch-then-check -- but the policy checks it calls are the shared ones.
"""
from __future__ import annotations

import base64
from pathlib import Path
from typing import Optional, Sequence, Tuple

import httpx

from finmeta.engine.candidate_ref import (
    ALLOWED_REMOTE_HOSTS, MAX_FETCH_BYTES, RefNotAllowed,
    check_local as _check_local, check_remote as _check_remote,
    host_allowed,
)

_KIND_GUESS = {
    "poster": "image/jpeg", "backdrop": "image/jpeg", "landscape": "image/jpeg",
    "logo": "image/png", "season_poster": "image/jpeg", "episode_thumb": "image/jpeg",
    "theme_song": "audio/mpeg", "metadata": "application/xml",
}
_EXT_GUESS = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".webp": "image/webp", ".gif": "image/gif", ".mp3": "audio/mpeg",
    ".nfo": "application/xml", ".xml": "application/xml", ".svg": "image/svg+xml",
}


def encode_token(kind: str, ref: str) -> str:
    raw = f"{kind}|{ref}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_token(token: str) -> Tuple[Optional[str], Optional[str]]:
    """Return ``(kind, ref)`` or ``(None, None)`` if malformed."""
    pad = token + "=" * (-len(token) % 4)
    try:
        raw = base64.urlsafe_b64decode(pad.encode()).decode()
    except Exception:
        return (None, None)
    kind, sep, ref = raw.partition("|")
    if not sep or not ref:
        return (None, None)
    return (kind, ref)


def content_type(kind: Optional[str], ref: str) -> str:
    ext = Path(ref).suffix.lower()
    if ext in _EXT_GUESS:
        return _EXT_GUESS[ext]
    return _KIND_GUESS.get(kind or "", "application/octet-stream")


def fetch(kind: Optional[str], ref: str, timeout: float = 30.0,
          allowed_roots: Optional[Sequence] = None) -> Tuple[bytes, str]:
    """Fetch the candidate bytes for ``ref``. Returns ``(bytes, content_type)``.

    ``allowed_roots`` are the directories a LOCAL ref may live under (see
    ``AppContext.proxy_roots``). ``None`` means "no local refs at all" --
    deliberately fail-closed, so a caller that forgets to pass a policy gets
    no filesystem access rather than unrestricted access.

    Raises :class:`RefNotAllowed` when the ref violates policy,
    ``FileNotFoundError`` when an allowed local ref does not exist, and
    ``httpx.HTTPError`` for upstream failures.
    """
    if ref.startswith(("http://", "https://")):
        _check_remote(ref)
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            with client.stream("GET", ref) as resp:
                # Re-check after redirects: the first hop being allow-listed
                # says nothing about where it pointed us.
                _check_remote(str(resp.url))
                resp.raise_for_status()
                buf = bytearray()
                for chunk in resp.iter_bytes():
                    buf += chunk
                    if len(buf) > MAX_FETCH_BYTES:
                        raise RefNotAllowed(
                            f"upstream response exceeds {MAX_FETCH_BYTES} bytes")
                return bytes(buf), content_type(kind, ref)
    p = _check_local(ref, allowed_roots or ())
    if p.is_file():
        return p.read_bytes(), content_type(kind, ref)
    raise FileNotFoundError(f"candidate ref not found: {ref}")


__all__ = [
    "ALLOWED_REMOTE_HOSTS", "MAX_FETCH_BYTES", "RefNotAllowed",
    "encode_token", "decode_token", "fetch", "content_type", "host_allowed",
]
