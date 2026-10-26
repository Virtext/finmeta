"""Secrets store for Finmeta (SPEC S9 §A).

Secrets live in ``config/secrets.yml`` (mode 0600), never in ``config.yml``.
If ``FINMETA_SECRET_KEY`` is set, values are Fernet-encrypted at rest; otherwise
stored plaintext (the UI then shows a warning banner). The API never returns
secret values — only ``{configured, source, hint}``.

Also provides a :class:`Redactor` used to scrub secret values out of logs,
errors, job logs and plan JSON (D2).
"""
from __future__ import annotations

import base64
import re
import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from finmeta.api.atomic import write_private_text
from finmeta.config.secretspec import (
    SecretSpec, secret_specs, resolve_secret,
)

import yaml

try:  # cryptography is an optional-but-expected dependency for Fernet.
    from cryptography.fernet import Fernet
except Exception:  # pragma: no cover - cryptography missing
    Fernet = None


def _make_fernet(raw: str):
    """Derive a stable, valid Fernet key from an arbitrary secret string."""
    digest = hashlib.sha256(raw.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


class SecretsStore:
    """File-backed secret store with optional Fernet encryption at rest."""

    def __init__(self, path, secret_key: Optional[str] = None):
        self.path = Path(path)
        self._fernet = _make_fernet(secret_key) if (secret_key and Fernet is not None) else None
        self._secrets: Dict[str, str] = {}
        self._encryption = "plaintext"
        self.load()

    # -- lifecycle ----------------------------------------------------------
    @property
    def encrypted_at_rest(self) -> bool:
        return self._fernet is not None

    def load(self) -> None:
        self._secrets = {}
        self._encryption = "plaintext"
        if self.path.is_file():
            data = yaml.safe_load(self.path.read_text()) or {}
            self._encryption = data.get("encryption", "plaintext")
            stored = data.get("secrets", {}) or {}
            for name, val in stored.items():
                if not isinstance(val, str):
                    val = str(val)
                self._secrets[str(name)] = self._decode(val)
        self._chmod()

    def _decode(self, val: str) -> str:
        if self._encryption == "fernet" and self._fernet is not None:
            try:
                return self._fernet.decrypt(val.encode("utf-8")).decode("utf-8")
            except Exception:
                return val
        return val

    def _encode(self, val: str) -> str:
        if self._fernet is not None:
            return self._fernet.encrypt(val.encode("utf-8")).decode("utf-8")
        return val

    def _chmod(self) -> None:
        """Narrow an EXISTING file's mode (e.g. one written before this fix).

        New writes go through :func:`write_private_text`, which creates the
        file 0600 from the start -- there is no window where it is readable
        by anyone else.
        """
        if self.path.is_file():
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass

    def _save(self) -> None:
        stored = {n: self._encode(v) for n, v in self._secrets.items()}
        enc = "fernet" if self._fernet is not None else "plaintext"
        data = {"version": 1, "encryption": enc, "secrets": stored}
        # Atomic + 0600 from creation. The old write-then-chmod created the
        # file world-readable with (in plaintext mode) every API key in it,
        # and a torn write truncated the whole store -- losing every saved
        # key. See api.atomic / docs/SECURITY_REVIEW.md "H3".
        write_private_text(
            self.path,
            yaml.safe_dump(data, sort_keys=True, default_flow_style=False))
        self._encryption = enc

    # -- accessors (plaintext, in-memory) -----------------------------------
    def get(self, name: str) -> Optional[str]:
        return self._secrets.get(name)

    def has(self, name: str) -> bool:
        return name in self._secrets

    def names(self) -> List[str]:
        return sorted(self._secrets)

    def values(self) -> List[str]:
        return [v for v in self._secrets.values() if v]

    def set(self, name: str, value: str) -> None:
        if not name:
            raise ValueError("secret name is required")
        self._secrets[name] = value
        self._save()

    def delete(self, name: str) -> bool:
        if name in self._secrets:
            del self._secrets[name]
            self._save()
            return True
        return False

    # -- hinting ------------------------------------------------------------
    @staticmethod
    def hint_value(value: Optional[str]) -> str:
        if not value:
            return ""
        if len(value) >= 4:
            return "\u2022\u2022\u2022\u2022" + value[-4:]
        return "\u2022\u2022\u2022\u2022"

    def hint(self, name: str) -> str:
        return self.hint_value(self._secrets.get(name))


class Redactor:
    """Redact known secret values from arbitrary text (D2)."""

    def __init__(self, values):
        seen: List[str] = []
        for v in values:
            if v and v not in seen:
                seen.append(v)
        # longest first so overlapping values are handled deterministically
        self._values = sorted(seen, key=len, reverse=True)

    def redact(self, text):
        if text is None:
            return text
        s = text if isinstance(text, str) else str(text)
        if not s:
            return s
        # Fixed string marker (S9d2-fix2): a bare [REDACTED] is ambiguous YAML —
        # it parses as the flow list ['REDACTED'] and breaks PUT /api/config.
        # A plain word can never be misread as a list.
        for v in self._values:
            if v in s:
                # Word-boundary match so a short value (e.g. a single character)
                # does not clobber unrelated words that merely contain it.
                s = re.sub(r'\b' + re.escape(v) + r'\b', "__FINMETA_SECRET__", s)
        return s


def redact(text, values) -> str:
    """Convenience: redact ``values`` out of ``text``."""
    return Redactor(values).redact(text)


def secret_status(store: SecretsStore, spec: SecretSpec) -> dict:
    """Status for one secret: {configured, source, hint}. Never the value."""
    ev = os.environ.get(spec.env_var)
    if ev:
        return {"configured": True, "source": "env", "hint": store.hint_value(ev)}
    val = store.get(spec.name)
    if val:
        return {"configured": True, "source": "ui", "hint": store.hint_value(val)}
    return {"configured": False, "source": "none", "hint": ""}


__all__ = [
    "SecretsStore", "Redactor", "redact", "SecretSpec",
    "secret_specs", "secret_status", "resolve_secret",
]
