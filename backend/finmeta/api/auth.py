"""Authentication for Finmeta (SPEC S9 §A).

First run: set an admin password (bcrypt hash in ``config/auth.yml``). A session
cookie (HttpOnly, SameSite=Strict) authenticates subsequent requests. Every
``/api`` route except ``/api/health`` and the login/setup routes requires auth.
Login is rate-limited. ``FINMETA_AUTH=off`` disables auth (tests/dev only).
"""
from __future__ import annotations

import logging
import os
import secrets as _secrets
import time
from collections import deque
from pathlib import Path
from typing import Optional

import bcrypt
import yaml

from finmeta.api.atomic import write_private_text

logger = logging.getLogger(__name__)

COOKIE_NAME = "finmeta_session"
SESSION_TTL = 7 * 24 * 3600  # 7 days

#: Minimum admin password length, enforced on BOTH the first-run setup route
#: and later password changes (they disagreed -- 4 vs 8 -- until 2026-10-22,
#: with the weaker rule on the first-run path that actually matters; see
#: docs/SECURITY_REVIEW.md "M1").
MIN_PASSWORD_LENGTH = 8


def cookie_secure() -> bool:
    """Whether the session cookie gets the ``Secure`` flag.

    Off by default because Finmeta is normally reached over plain HTTP on a
    LAN, where ``Secure`` would stop the cookie working entirely. Set
    ``FINMETA_COOKIE_SECURE=1`` when serving behind TLS so the session cookie
    can never be sent over an unencrypted hop (CLAUDE.md §5).
    """
    v = os.environ.get("FINMETA_COOKIE_SECURE", "").strip().lower()
    return v in ("1", "on", "true", "yes")


def auth_enabled() -> bool:
    """Whether authentication is enforced (FINMETA_AUTH=off disables it)."""
    v = os.environ.get("FINMETA_AUTH", "").strip().lower()
    return v not in ("off", "0", "false", "no")


class AuthStore:
    """Stores the admin password hash in ``config/auth.yml`` (mode 0600)."""

    def __init__(self, config_dir):
        self.path = Path(config_dir) / "auth.yml"
        self._hash: Optional[str] = None
        self._username: str = "admin"
        self._load()

    @property
    def username(self) -> str:
        return self._username

    def _load(self) -> None:
        self._hash = None
        self._username = "admin"
        self._unreadable = False
        if self.path.is_file():
            try:
                data = yaml.safe_load(self.path.read_text()) or {}
                if not isinstance(data, dict):
                    raise yaml.YAMLError("auth.yml must be a mapping")
            except (yaml.YAMLError, OSError) as exc:
                # An existing-but-unreadable auth.yml must NOT look like "no
                # password set": that flips first_run() on, and the setup
                # route is unauthenticated by necessity, so anyone who can
                # reach the port could claim the admin account. Fail closed
                # instead -- refuse logins AND refuse setup -- and say so
                # loudly. See docs/SECURITY_REVIEW.md "H2".
                self._unreadable = True
                logger.error(
                    "auth.yml exists but could not be read (%s): refusing every "
                    "login and refusing to re-run first-time setup. Fix or remove "
                    "%s to recover.", exc, self.path)
                return
            u = data.get("username")
            if isinstance(u, str) and u.strip():
                self._username = u.strip()
            h = data.get("password_hash")
            if isinstance(h, str) and h:
                self._hash = h

    @property
    def unreadable(self) -> bool:
        """True when auth.yml exists but could not be parsed (fail closed)."""
        return self._unreadable

    def has_password(self) -> bool:
        return self._hash is not None

    def first_run(self) -> bool:
        """Whether the unauthenticated setup route may set the first password.

        Never true while :attr:`unreadable` -- see :meth:`_load`.
        """
        return auth_enabled() and not self.has_password() and not self._unreadable

    def set_password(self, password: str) -> None:
        self._hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
        self._persist()

    def verify(self, password: str, username: Optional[str] = None) -> bool:
        if not self._hash or self._unreadable:
            return False
        # Always run bcrypt, then combine the results, so a wrong USERNAME
        # costs the same as a wrong password. Returning early on a username
        # mismatch skipped the ~100 ms hash and made the login route a
        # username-enumeration oracle (SECURITY_REVIEW "M2").
        try:
            password_ok = bcrypt.checkpw(
                password.encode("utf-8"), self._hash.encode("utf-8"))
        except ValueError:
            return False
        if username is not None and not _secrets.compare_digest(
                username, self._username):
            return False
        return password_ok

    def set_username(self, username: str) -> None:
        """Change the account's login name (preserves the password hash)."""
        u = username.strip()
        if not u:
            raise ValueError("username must not be empty")
        self._username = u
        self._persist()

    def update_password(self, password: str) -> None:
        self._hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
        self._persist()

    def _persist(self) -> None:
        data = {"username": self._username}
        if self._hash is not None:
            data["password_hash"] = self._hash
        # Atomic + 0600 from creation: a torn write used to lose the admin
        # password (which re-armed the unauthenticated setup route), and the
        # old write-then-chmod left the hash briefly world-readable.
        write_private_text(self.path, yaml.safe_dump(data, sort_keys=True))


class SessionManager:
    """In-memory session tokens (single-instance dev tool; not multi-process safe)."""

    def __init__(self):
        self._sessions = {}

    def _prune(self) -> None:
        now = time.time()
        for t in [t for t, ts in self._sessions.items() if now - ts > SESSION_TTL]:
            del self._sessions[t]

    def create(self) -> str:
        self._prune()
        token = _secrets.token_urlsafe(32)
        self._sessions[token] = time.time()
        return token

    def valid(self, token: Optional[str]) -> bool:
        if not token:
            return False
        self._prune()
        return token in self._sessions

    def destroy(self, token: str) -> None:
        self._sessions.pop(token, None)


class LoginRateLimiter:
    """Fail-count rate limiter for login attempts, bucketed per client.

    Counts are kept per ``key`` (the client IP) rather than globally. With a
    single shared counter, five failures from anywhere locked out *everyone*
    including the real admin, so an attacker who could reach the port could
    keep the operator permanently locked out by failing a login every minute
    -- a trivial denial of service. Per-client buckets keep the brute-force
    protection while confining the lockout to whoever is failing
    (SECURITY_REVIEW "M3").

    Still in-memory and single-process, like :class:`SessionManager`: both
    reset on restart, which is documented and acceptable for a
    single-instance tool.
    """

    #: Don't let an attacker rotating source addresses grow this unbounded.
    MAX_KEYS = 2048

    def __init__(self, max_attempts: int = 5, window: float = 300.0):
        self.max = max_attempts
        self.window = window
        self._fails: dict = {}

    def _prune(self, key: str) -> deque:
        now = time.time()
        q = self._fails.get(key)
        if q is None:
            if len(self._fails) >= self.MAX_KEYS:
                # Drop whatever is already fully expired before giving up on
                # tracking a new client; failing open here is the lesser evil
                # (a wrong lockout is worse than a missed count).
                for k in [k for k, v in self._fails.items()
                          if not v or now - v[-1] > self.window]:
                    del self._fails[k]
            q = self._fails.setdefault(key, deque())
        while q and now - q[0] > self.window:
            q.popleft()
        if not q:
            self._fails.pop(key, None)
        return q

    def blocked(self, key: str = "") -> bool:
        return len(self._prune(key)) >= self.max

    def record_failure(self, key: str = "") -> None:
        self._prune(key)
        self._fails.setdefault(key, deque()).append(time.time())

    def reset(self, key: str = "") -> None:
        """Clear one client's failures (called after a successful login)."""
        self._fails.pop(key, None)


def client_key(request) -> str:
    """Rate-limit bucket for a request: the peer address, or "" if unknown.

    Deliberately NOT ``X-Forwarded-For``: that header is attacker-controlled
    unless a trusted proxy sets it, and trusting it would hand an attacker an
    unlimited supply of fresh buckets. Behind a reverse proxy every request
    therefore shares the proxy's bucket -- correct but coarse; revisit with an
    explicit trusted-proxy setting if that becomes a problem.
    """
    client = getattr(request, "client", None)
    return getattr(client, "host", None) or ""


__all__ = [
    "COOKIE_NAME", "SESSION_TTL", "MIN_PASSWORD_LENGTH",
    "auth_enabled", "cookie_secure", "client_key",
    "AuthStore", "SessionManager", "LoginRateLimiter",
]
