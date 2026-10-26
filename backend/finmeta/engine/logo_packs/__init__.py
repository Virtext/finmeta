"""On-demand logo pack installation (SPEC S9c8).

Rating logos are trademarks and are neither committed to this repo nor shipped
in the Docker image. The user installs a pack on demand; files land in
``<config>/assets/logos/<pack>/`` which is git-ignored. Pack definitions live
next to this module as YAML mapping Finmeta element keys to upstream file paths.

HTTP goes through :class:`httpx.Client`; tests inject a mock transport so no
real network access happens during testing.
"""
from __future__ import annotations

import io
import os
from pathlib import Path
from typing import Dict, List

import yaml

from PIL import Image

# Pack definition YAML files live NEXT TO this module (this package directory),
# e.g. engine/logo_packs/kometa.yml. Do NOT recurse into the *installed* pack
# folder at runtime: user data never defines new packs, and installed logos are
# git-ignored binary blobs that must stay out of any glob here.
PACKS_DIR = Path(__file__).resolve().parent


def _pack_files() -> list:
    try:
        entries = [e.name for e in os.scandir(PACKS_DIR) if e.is_file()]
    except OSError:
        return []
    # only *.yml definitions; exclude __init__ etc. naturally by extension filter
    return [n for n in entries if n.endswith(".yml")]


def available_pack_names() -> List[str]:
    """Names of known packs (one per .yml file next to this module)."""
    return sorted(n[:-4] for n in _pack_files())


def load_pack(name: str) -> dict:
    """Load one pack definition by name; raises KeyError when unknown."""
    p = PACKS_DIR / f"{name}.yml"
    if not p.is_file():
        raise KeyError(f"unknown logo pack {name!r}")
    data = yaml.safe_load(p.read_text()) or {}
    if not isinstance(data.get("files"), dict):
        raise ValueError(f"logo pack {name!r} has no 'files' mapping")
    return data


def pack_url(pack: dict, key: str) -> str:
    base = (pack.get("base_url") or "").format(
        repo=pack.get("repo", ""), ref=pack.get("ref", ""))
    rel = str(pack["files"][key])
    sep = "/" if base.endswith("/") else ""
    return f"{base}{sep}{rel}"


def installed_root(config_dir: Path | str) -> Path:
    return Path(config_dir) / "assets" / "logos"


def install_pack(client, config_dir: Path | str, name: str,
                 timeout: float = 30.0) -> Dict[str, int]:
    """Download every mapped file of ``name`` into the pack folder.

    Idempotent: existing non-empty files are skipped (counted as such). A file
    that fails to download is left absent so the renderer falls back to text;
    failures never abort the rest of the install. Returns counts and errors.
    """
    import httpx  # noqa: F401 - client must be an httpx.Client (documented contract)

    pack = load_pack(name)
    dest = installed_root(config_dir) / name
    dest.mkdir(parents=True, exist_ok=True)
    downloaded = skipped = failed = 0
    errors: List[dict] = []
    for key in sorted(pack["files"]):
        target = dest / f"{key}.png"
        if target.is_file() and target.stat().st_size > 0:
            skipped += 1
            continue
        try:
            resp = client.get(pack_url(pack, key), timeout=timeout)
            if getattr(resp, "status_code", None) != 200:
                raise IOError(f"HTTP {getattr(resp, 'status_code', '?')}")
            blob = bytes(resp.content)
            Image.open(io.BytesIO(blob)).verify()   # reject HTML error pages etc.
            tmp = target.with_suffix(".part")
            tmp.write_bytes(blob)
            os.replace(str(tmp), str(target))
            downloaded += 1
        except Exception as exc:      # one bad file must not stop the rest of the pack
            failed += 1
            errors.append({"file": key, "error": str(exc)})
            stale = target.with_suffix(".part")
            if stale.is_file():
                try:
                    stale.unlink()
                except OSError:
                    pass
    return {"pack": name, "downloaded": downloaded, "skipped": skipped,
            "failed": failed, "errors": errors}


def installed_packs(config_dir: Path | str) -> List[dict]:
    """Report which packs are present on disk and how complete they are."""
    root = installed_root(config_dir)
    out: List[dict] = []
    for name in available_pack_names():
        folder = root / name
        files = load_pack(name)["files"]
        have = [k for k in files if (folder / f"{k}.png").is_file()]
        out.append({
            "name": name,
            "installed": folder.is_dir(),
            "complete": len(have) == len(files),
            "have": len(have),
            "total": len(files),
            "missing": sorted(set(files) - set(have)),
        })
    return out


__all__ = ["available_pack_names", "load_pack", "install_pack",
           "installed_packs", "pack_url", "installed_root"]
