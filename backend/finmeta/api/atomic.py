"""Atomic, private writes for the two credential files (CLAUDE.md §5).

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

:func:`write_private_text` fixes both: create a temp file in the target
directory with mode 0600 from the start, write and ``fsync`` it, then
``os.replace`` it over the target -- an atomic rename within one filesystem,
so a reader sees either the whole old file or the whole new one.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

#: Owner read/write only -- both files hold credentials.
PRIVATE_MODE = 0o600


def write_private_text(path: Path, text: str, mode: int = PRIVATE_MODE) -> None:
    """Atomically write ``text`` to ``path``, never readable by anyone else."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".finmeta_", suffix=".tmp")
    try:
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


__all__ = ["PRIVATE_MODE", "write_private_text"]
