"""Single policy for validating a candidate ref before it is fetched or stored
(audit 2026-11-11, A1/B5).

A candidate ``ref`` (poster/backdrop/logo/NFO source) is either a remote URL
or a local path. It arrives at several places that each need the SAME
answer to "is this ref allowed":

* ``api/proxy.py`` -- the browser-facing image proxy (already fixed 2026-10-22,
  see ``docs/SECURITY_REVIEW.md`` "H1");
* ``api/overrides.py`` -- a manually picked candidate, stored verbatim by
  ``PUT /api/items/{id}/candidates`` with **no validation at all** before
  this module existed: an operator-reachable route that let ANY local path
  become a ``FileWrite.source_ref``, written to the media tree by the
  executor and then readable back through the proxy (which DOES check its
  own policy, but on a ref the executor already wrote to disk under a
  completely different, unchecked path). Confirmed with a PoC against a
  canary file standing in for ``config/secrets.yml``: the ref was accepted,
  hashed, and served back byte-for-byte.
* ``engine/plan_builder.py`` -- the same shape of ref, resolved while
  building a plan.

This module is the one place that decides remote-host and local-root
policy; it is engine-level (no FastAPI import) so ``plan_builder`` and
``overrides.py`` can both use it without a reverse dependency from engine
into api. ``api/proxy.py`` re-exports the two constants from here so there
remains exactly one list of allowed hosts and one cap, not three.
"""
from __future__ import annotations

from pathlib import Path
from typing import List, Optional, Sequence
from urllib.parse import urlparse

import httpx

from finmeta.engine.net import FetchRefused, fetch_bytes

#: Hosts a remote candidate ref may point at: the image hosts of the two
#: artwork providers whose candidate refs are URLs (``TmdbClient.image_base``
#: / ``FanartClient.image_base``). Both are constructor defaults that no
#: config field can change, so this is the complete legitimate set.
ALLOWED_REMOTE_HOSTS = (
    "image.tmdb.org",
    "fanart.tv",
    "assets.fanart.tv",
    "webservice.fanart.tv",
)

#: Hard cap on a fetched candidate so a hostile or compromised upstream
#: cannot balloon memory through an unbounded read. Checked AFTER download
#: (not streamed) -- acceptable here because the host is already restricted
#: to the two allowlisted providers above; this is not a general-purpose
#: SSRF-safe fetcher.
MAX_FETCH_BYTES = 64 * 1024 * 1024


class RefNotAllowed(FetchRefused, ValueError):
    """A ref failed policy validation (never echoed verbatim to a client).

    Also a :class:`engine.net.FetchRefused`: when raised from the
    ``on_response`` hook :func:`fetch` passes to ``engine.net.fetch_bytes``
    (the post-redirect host recheck), this tells ``fetch_bytes`` to give up
    immediately instead of retrying the same disallowed URL. Still a
    ``ValueError`` too, so every existing ``except RefNotAllowed`` /
    ``except ValueError`` catch site is unaffected.
    """


def host_allowed(host: Optional[str]) -> bool:
    """True when ``host`` is one of :data:`ALLOWED_REMOTE_HOSTS` (or a subdomain)."""
    h = (host or "").lower()
    return any(h == a or h.endswith("." + a) for a in ALLOWED_REMOTE_HOSTS)


def check_remote(url: str) -> None:
    """Raise :class:`RefNotAllowed` unless ``url`` is http(s) to an allowed host."""
    parts = urlparse(url)
    if parts.scheme not in ("http", "https"):
        raise RefNotAllowed(f"unsupported scheme: {parts.scheme!r}")
    if not host_allowed(parts.hostname):
        raise RefNotAllowed("remote host is not an allowed provider host")


def check_local(ref: str, allowed_roots: Sequence) -> Path:
    """Resolve ``ref`` and require it inside one of ``allowed_roots``.

    ``resolve()`` runs first so ``..`` segments and symlinks are collapsed
    before the containment test -- comparing raw strings would be
    bypassable. An empty ``allowed_roots`` denies every local ref (fail
    closed): a caller that forgets to pass a policy gets no filesystem
    access rather than unrestricted access.
    """
    p = Path(ref)
    if not p.is_absolute():
        raise RefNotAllowed("local ref must be absolute")
    try:
        rp = p.resolve()
    except OSError as exc:
        raise RefNotAllowed(f"unresolvable local ref: {exc}") from exc
    for root in allowed_roots:
        try:
            base = Path(root).resolve()
        except OSError:
            continue
        if rp == base or base in rp.parents:
            return rp
    raise RefNotAllowed("local ref is outside the allowed roots")


def default_allowed_roots(cfg, config_dir=None) -> List[Path]:
    """The directories a candidate ref may legitimately live under for one
    config: the local-source root, Finmeta's own artwork-lifecycle
    directories under ``config_dir``, and every ``path_mappings`` local
    root. Deliberately NOT ``config_dir`` itself, which also holds
    ``secrets.yml``/``auth.yml``/``config.yml``.

    Extracted from ``api.state.AppContext.proxy_roots`` (same list, same
    order, same reasoning) so ``plan_builder`` -- which cannot import the
    api package without a reverse dependency -- computes the identical
    policy instead of a second, possibly-drifting copy. ``AppContext.
    proxy_roots`` now delegates here.

    ``config_dir=None`` (``engine.plan_builder.build_plan``'s own default,
    used by callers that have no on-disk config dir at all -- e.g. a plan
    built purely for a preview) still grants the local-source root and
    every ``path_mappings`` root, just not the four ``config_dir`` subdirs,
    which cannot be computed without one.
    """
    roots: List[Path] = []
    local = getattr(getattr(cfg, "sources", None), "local", None)
    if local is not None and getattr(local, "path", None):
        roots.append(Path(local.path))
    if config_dir is not None:
        for sub in ("assets", "backups", "trash", "cache"):
            roots.append(Path(config_dir) / sub)
    for m in getattr(cfg, "path_mappings", []) or []:
        if getattr(m, "local", None):
            roots.append(Path(m.local))
    return roots


def validate(ref: str, *, allowed_roots: Sequence = ()) -> str:
    """Raise :class:`RefNotAllowed` unless ``ref`` is a policy-conforming
    remote URL or local path. Returns the normalized ref (unchanged for a
    remote URL; the resolved absolute path, as a string, for a local ref).

    Does not fetch or check existence -- this is the cheap check used
    BEFORE a ref is stored (``PUT /api/items/{id}/candidates``), so a
    disallowed ref is rejected at write time instead of only failing later
    when something tries to fetch it.
    """
    if ref.startswith(("http://", "https://")):
        check_remote(ref)
        return ref
    return str(check_local(ref, allowed_roots))


def fetch(ref: str, *, timeout: float = 30.0,
          allowed_roots: Sequence = (), attempts: int = 2,
          max_bytes: int = MAX_FETCH_BYTES) -> bytes:
    """Fetch the verified bytes of a policy-conforming candidate ``ref``.

    Remote refs go through ``engine.net.fetch_bytes`` (retry + Content-Length
    + image-end-marker verification -- the same path that fixed the ~360
    silently-dropped backdrops/logos per run, see ``engine.net``'s module
    docstring) with an ``on_response`` hook that re-checks the host AFTER
    redirects: the first hop being allowed says nothing about where it
    pointed. Local refs are read directly once contained.

    Raises :class:`RefNotAllowed` on a policy violation,
    ``FileNotFoundError`` when an allowed local ref does not exist, and the
    underlying ``httpx``/``engine.net`` error for a remote transport failure.
    """
    if ref.startswith(("http://", "https://")):
        check_remote(ref)

        def _recheck_host(resp: httpx.Response) -> None:
            check_remote(str(resp.url))

        data = fetch_bytes(ref, timeout=timeout, attempts=attempts,
                           on_response=_recheck_host)
        if len(data) > max_bytes:
            raise RefNotAllowed(f"response exceeds {max_bytes} bytes")
        return data
    p = check_local(ref, allowed_roots)
    if not p.is_file():
        raise FileNotFoundError(f"candidate ref not found: {ref}")
    data = p.read_bytes()
    if len(data) > max_bytes:
        raise RefNotAllowed(f"local file exceeds {max_bytes} bytes")
    return data


__all__ = [
    "ALLOWED_REMOTE_HOSTS", "MAX_FETCH_BYTES", "RefNotAllowed",
    "host_allowed", "check_remote", "check_local", "default_allowed_roots",
    "validate", "fetch",
]
