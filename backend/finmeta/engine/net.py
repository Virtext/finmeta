"""Validated HTTP fetches for artwork candidates.

Every image Finmeta writes is downloaded TWICE: once while the plan is built
(to hash it, so the diff can say create/update/unchanged) and once at apply
time (to write it). The executor refuses to write when the two disagree --
which is the right instinct, except that it was comparing one unverified
download against another.

Operator-reported 2026-11-06: a full run logged ~360 ``hash mismatch``
warnings, i.e. ~360 backdrops/logos silently not written, every single run.
Traced to a *plan-time* download that came back short: the same URL hashed
identically in the Nov 4 and Nov 5 plans and on a re-fetch today, but
differently in the Nov 6 plan -- so the plan recorded the hash of a
truncated body and the apply could never match it. ``httpx`` returns what it
got without complaint when a connection ends cleanly mid-body, so nothing
upstream noticed.

``fetch_bytes`` verifies the body instead of trusting it: the declared
``Content-Length`` must match, and an image must carry its own end-of-file
marker. A failed check retries once (a transient short read almost always
succeeds on the next attempt) before raising.
"""
from __future__ import annotations

import logging
from typing import Callable, Optional

import httpx

logger = logging.getLogger("finmeta.engine.net")

DEFAULT_TIMEOUT = 30.0
DEFAULT_ATTEMPTS = 2

_JPEG_MAGIC = b"\xff\xd8\xff"
_JPEG_EOI = b"\xff\xd9"
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_PNG_IEND = b"IEND"


class IncompleteDownload(RuntimeError):
    """A response body was shorter than it should be (truncated transfer)."""


class FetchRefused(Exception):
    """Raise from an ``on_response`` hook to refuse a response outright.

    Unlike a transport/HTTP error -- a genuinely transient condition worth
    retrying -- a refusal from the hook (e.g. ``engine.candidate_ref``'s
    host/path policy, audit 2026-11-11 "A1"/"B5") is a decision, not a
    failure: retrying the SAME URL would reach the SAME decision every
    time, so :func:`fetch_bytes` lets this propagate immediately instead of
    burning its retry budget on a doomed request. Subclass this (rather
    than raising it directly) to keep a caller's own exception type for
    everyone who already catches it.
    """


def _length_mismatch(resp: httpx.Response, data: bytes) -> Optional[str]:
    """Reason string when Content-Length disagrees with what arrived.

    Skipped when the body was transfer-encoded (``Content-Length`` then
    describes the COMPRESSED bytes, while ``data`` is already decoded).
    """
    encoding = (resp.headers.get("content-encoding") or "identity").lower()
    if encoding not in ("", "identity"):
        return None
    declared = resp.headers.get("content-length")
    if not declared:
        return None
    try:
        expected = int(declared)
    except ValueError:
        return None
    if expected != len(data):
        return f"declared {expected} bytes, received {len(data)}"
    return None


def _truncated_image(data: bytes) -> Optional[str]:
    """Reason string when image bytes lack their own end marker.

    Deliberately a marker check, not a decode: this runs once per candidate
    on a library-wide plan build, and fully decoding every 4K backdrop to
    prove it is intact would cost minutes per run. A truncated transfer
    loses the tail, which is exactly where both markers live.
    """
    if data.startswith(_JPEG_MAGIC):
        # Some encoders pad after EOI; look in the tail rather than the
        # very last two bytes.
        if _JPEG_EOI not in data[-16:]:
            return "JPEG has no end-of-image marker"
    elif data.startswith(_PNG_MAGIC):
        if _PNG_IEND not in data[-16:]:
            return "PNG has no IEND chunk"
    return None


def verify_body(resp: httpx.Response, data: bytes) -> Optional[str]:
    """Reason the body looks incomplete, or ``None`` when it looks whole."""
    return _length_mismatch(resp, data) or _truncated_image(data)


def fetch_bytes(
    url: str, *, timeout: float = DEFAULT_TIMEOUT,
    attempts: int = DEFAULT_ATTEMPTS,
    on_response: Optional[Callable[[httpx.Response], None]] = None,
) -> bytes:
    """GET ``url`` and return a verified-complete body.

    ``on_response`` (2026-11-11, audit A1/B5): called with the raw response
    immediately after the GET, before ``raise_for_status`` or the body is
    read -- the hook a caller uses to re-validate the URL it actually landed
    on (``resp.url``, after redirects) against an SSRF allowlist. Raising
    :class:`FetchRefused` (or a subclass) propagates immediately without
    being retried -- a policy rejection is not a transient failure, and
    retrying the same URL would reach the same refusal every time. Any
    OTHER exception from the hook is treated exactly like a transport/HTTP
    error below (caught, retried). ``None`` (the default) means no extra
    check, unchanged from before this parameter existed. Only
    ``engine.candidate_ref`` passes one; this module stays provider-policy
    agnostic -- it only needs to recognise :class:`FetchRefused`, not any
    particular policy's exception type.

    Raises the underlying ``httpx`` error on a transport/HTTP failure, or
    ``IncompleteDownload`` when every attempt came back short.
    """
    last_error: Optional[Exception] = None
    for attempt in range(1, max(1, attempts) + 1):
        try:
            resp = httpx.get(url, timeout=timeout, follow_redirects=True)
            if on_response is not None:
                on_response(resp)
            resp.raise_for_status()
            data = resp.content
            reason = verify_body(resp, data)
            if reason is None:
                return data
            last_error = IncompleteDownload(f"{reason} ({url})")
        except IncompleteDownload:
            raise
        except FetchRefused:
            raise
        except Exception as exc:  # transport error, HTTP error
            last_error = exc
        if attempt < attempts:
            logger.warning("retrying download (attempt %d failed: %s)",
                           attempt, last_error)
    assert last_error is not None
    raise last_error


__all__ = ["fetch_bytes", "verify_body", "IncompleteDownload", "FetchRefused",
           "DEFAULT_TIMEOUT", "DEFAULT_ATTEMPTS"]
