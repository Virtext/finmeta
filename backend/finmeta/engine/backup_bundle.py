"""Build and read a Finmeta backup bundle (SPEC_S11 §D).

A bundle is one zip holding the operator's *decisions* -- configuration,
library profiles, artwork picks and locks -- and deliberately nothing else.

**What is never in a bundle**, and why it is a hard rule rather than a
default: ``secrets.yml``, ``auth.yml``, any ``*.bak*``, the caches and the
job database. A backup file is the single most likely thing to be mailed to
someone, synced to a cloud drive or pasted into a bug report. ``config.yml``
references secrets by NAME only (that is already the design), so a restored
config is complete except for the keys -- and the manifest lists exactly
which keys the target install has to re-enter.

Pack/asset BINARIES are excluded too: they are large and re-downloadable.
``packs.json`` records which packs were installed so a restore can say what
to re-import.

Reading is done entirely in memory from named members -- nothing is ever
extracted to a path out of the archive, so a hand-built "zip slip" bundle has
nothing to slip into. Member sizes are capped before reading.
"""
from __future__ import annotations

import io
import json
import logging
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("finmeta.engine.backup")

#: Bumped only when the layout changes incompatibly.
BUNDLE_VERSION = 1

MANIFEST_NAME = "manifest.json"
CONFIG_NAME = "config.yml"
OVERRIDES_NAME = "overrides.json"
PACKS_NAME = "packs.json"
SCAN_NAME = "scan.json"

#: Every member the reader will even look at. Anything else in the zip is
#: ignored rather than trusted.
KNOWN_MEMBERS = (MANIFEST_NAME, CONFIG_NAME, OVERRIDES_NAME, PACKS_NAME, SCAN_NAME)

#: Things a bundle must never contain, repeated in the manifest so the
#: operator can see the promise rather than take it on faith.
EXCLUDED = (
    "secrets.yml", "auth.yml", "*.bak*", "cache.db", "jobs.db",
    "overrides.db (exported as overrides.json instead)",
    "assets/ and packs/ binaries (see packs.json)",
    "media files",
)

#: Per-member read cap (uncompressed). A config or an overrides dump is tiny;
#: anything this big is either corrupt or hostile.
MAX_MEMBER_BYTES = 8 * 1024 * 1024

#: Stored snapshot filenames. Strict, because the name comes from a URL.
BUNDLE_NAME_RE = re.compile(r"^finmeta-backup-[A-Za-z0-9._-]{1,80}\.zip$")


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def bundle_filename(label: str = "", now: Optional[datetime] = None) -> str:
    """``finmeta-backup-<label>-<YYYYMMDD-HHMM>.zip`` (label optional)."""
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%d-%H%M")
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", label).strip("-")
    middle = f"{safe}-{stamp}" if safe else stamp
    return f"finmeta-backup-{middle}.zip"


def installed_packs(config_dir: Path) -> Dict[str, List[str]]:
    """Which image/logo packs are installed, by directory name only.

    Names, never bytes: a restore uses this to tell the operator what to
    re-import, since the packs themselves are large and downloadable.
    """
    out: Dict[str, List[str]] = {}
    for key, rel in (("image_packs", "assets/packs"), ("logo_packs", "assets/logos")):
        base = Path(config_dir) / rel
        try:
            out[key] = sorted(p.name for p in base.iterdir() if p.is_dir())
        except (OSError, FileNotFoundError):
            out[key] = []
    return out


def build_bundle(
    *, config_text: str, overrides: Dict[str, Any],
    packs: Optional[Dict[str, List[str]]] = None,
    secret_names: Optional[List[str]] = None,
    scan: Optional[Dict[str, Any]] = None,
    finmeta_version: str = "",
) -> bytes:
    """Serialize a bundle to zip bytes."""
    contents = [MANIFEST_NAME, CONFIG_NAME, OVERRIDES_NAME, PACKS_NAME]
    if scan is not None:
        contents.append(SCAN_NAME)
    manifest = {
        "bundle_version": BUNDLE_VERSION,
        "finmeta_version": finmeta_version,
        "created_at": _utcnow(),
        "contents": contents,
        "excluded": list(EXCLUDED),
        # What the TARGET install must re-enter by hand after a restore.
        "secret_names": list(secret_names or []),
        "counts": {table: len(rows) for table, rows in (overrides or {}).items()},
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(MANIFEST_NAME, json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        zf.writestr(CONFIG_NAME, config_text)
        zf.writestr(OVERRIDES_NAME,
                    json.dumps(overrides or {}, indent=2, sort_keys=True) + "\n")
        zf.writestr(PACKS_NAME,
                    json.dumps(packs or {}, indent=2, sort_keys=True) + "\n")
        if scan is not None:
            zf.writestr(SCAN_NAME, json.dumps(scan, indent=2, default=str) + "\n")
    return buf.getvalue()


class BundleError(ValueError):
    """A bundle that cannot be trusted or understood."""


class Bundle:
    """A parsed bundle, read from memory."""

    def __init__(self, data: bytes):
        try:
            self._zip = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile as exc:
            raise BundleError(f"not a readable zip file: {exc}") from exc
        self.manifest: Dict[str, Any] = json.loads(self._read(MANIFEST_NAME) or "{}")
        version = self.manifest.get("bundle_version")
        if version != BUNDLE_VERSION:
            raise BundleError(
                f"bundle version {version!r} is not supported by this Finmeta "
                f"(expected {BUNDLE_VERSION})")

    def _read(self, name: str) -> Optional[str]:
        """One known member as text, or None when absent."""
        if name not in KNOWN_MEMBERS:
            raise BundleError(f"refusing to read unexpected member {name!r}")
        try:
            info = self._zip.getinfo(name)
        except KeyError:
            if name == MANIFEST_NAME:
                raise BundleError("bundle has no manifest.json") from None
            return None
        if info.file_size > MAX_MEMBER_BYTES:
            raise BundleError(
                f"{name} is {info.file_size} bytes, over the "
                f"{MAX_MEMBER_BYTES}-byte limit")
        with self._zip.open(info) as fh:
            return fh.read(MAX_MEMBER_BYTES + 1).decode("utf-8")

    @property
    def config_text(self) -> Optional[str]:
        return self._read(CONFIG_NAME)

    def json_member(self, name: str) -> Dict[str, Any]:
        raw = self._read(name)
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise BundleError(f"{name} is not valid JSON: {exc}") from exc
        return data if isinstance(data, dict) else {}

    @property
    def overrides(self) -> Dict[str, Any]:
        return self.json_member(OVERRIDES_NAME)

    @property
    def packs(self) -> Dict[str, Any]:
        return self.json_member(PACKS_NAME)

    def names(self) -> List[str]:
        """Every member actually present in the zip."""
        return sorted(self._zip.namelist())

    def close(self) -> None:
        self._zip.close()


def safe_bundle_path(bundles_dir: Path, name: str) -> Path:
    """Resolve a stored bundle name inside ``bundles_dir``, or raise.

    Belt and braces: the name is matched against a strict pattern AND the
    resolved path is checked to still be inside the directory, so neither a
    traversal nor a symlink games the lookup.
    """
    if not BUNDLE_NAME_RE.match(name or ""):
        raise BundleError(f"invalid bundle name {name!r}")
    base = Path(bundles_dir).resolve()
    target = (base / name).resolve()
    if target.parent != base:
        raise BundleError(f"invalid bundle name {name!r}")
    return target


__all__ = [
    "BUNDLE_VERSION", "EXCLUDED", "KNOWN_MEMBERS", "MAX_MEMBER_BYTES",
    "Bundle", "BundleError", "build_bundle", "bundle_filename",
    "installed_packs", "safe_bundle_path",
]
