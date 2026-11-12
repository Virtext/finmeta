"""Atomic text writes (CLAUDE.md §5).

``config/secrets.yml`` and ``config/auth.yml`` were written with
``Path.write_text()`` followed by a ``chmod(0o600)``, which has two problems
the engine's own media writes (``engine.executor._atomic_write``) already
avoid:

* **a permissions window** -- the file is created with the process umask
  (typically 0644) and only narrowed afterwards, so plaintext API keys and
  the admin password hash are briefly world-readable;
* **no atomicity** -- ``write_text`` truncates first, so a crash or a full
  disk mid-write leaves a truncated or empty file. For ``secrets.yml`` that
  silently loses every API key; for ``auth.yml`` it loses the admin password,
  which used to re-arm the unauthenticated first-run setup route (see
  ``AuthStore._load``).

:func:`write_text_atomic` fixes both, generically: create a temp file in the
target directory (``mode`` applied before any content is written, if given),
write and ``fsync`` it, then ``os.replace`` it over the target -- an atomic
rename within one filesystem, so a reader sees either the whole old file or
the whole new one. :func:`write_private_text` is the 0600 case this was
originally written for (``secrets.yml``/``auth.yml``); ``config.yml`` (audit
2026-11-11 "B7": the same ``write_text``-then-nothing pattern, so a torn
write there loses the live config and silently reverts to whatever
``config.last-good.yml`` or the template says on the next restart) uses
:func:`write_text_atomic` directly with the normal 0644 a non-secret file
should have.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Optional

#: Owner read/write only -- both files hold credentials.
PRIVATE_MODE = 0o600


def write_text_atomic(path: Path, text: str, mode: Optional[int] = None) -> None:
    """Atomically write ``text`` to ``path`` (temp + fsync + ``os.replace``).

    ``mode`` (if given) is applied to the temp file via ``fchmod`` BEFORE
    any content is written, so there is never a window where partial
    content is readable at a wider mode than intended. ``None`` leaves the
    temp file at the process umask's default, appropriate for a
    non-secret file like ``config.yml``.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".finmeta_", suffix=".tmp")
    try:
        if mode is not None:
            os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_private_text(path: Path, text: str, mode: int = PRIVATE_MODE) -> None:
    """Atomically write ``text`` to ``path``, never readable by anyone else."""
    write_text_atomic(path, text, mode=mode)


__all__ = ["PRIVATE_MODE", "write_text_atomic", "write_private_text"]
