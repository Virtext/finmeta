"""Development / container entry point for the Finmeta API server.

Run directly:  finmeta.api.server  (or  python -m finmeta.api.server)
Config path comes from the ``FINMETA_CONFIG`` env var (default
``config/config.yml``) — no hardcoded paths (D1).
"""
from __future__ import annotations

import logging
import os

import uvicorn

from finmeta.api.app import create_app_from_path
from finmeta.api.state import DEFAULT_CONFIG_PATH


def _configure_logging() -> None:
    """Give every ``finmeta.*`` logger (webhooks, scheduler, run_service, ...)
    a real handler. ``uvicorn.run(..., log_level=...)`` only wires up its own
    ``uvicorn``/``uvicorn.access`` loggers (uvicorn.config.LOGGING_CONFIG has
    no ``root`` entry) -- without this, every ``logger.info``/``.warning``
    call anywhere in the app is silently swallowed (root stays at the
    logging-module default WARNING, with zero handlers), even though the
    code runs. Attached to the "finmeta" parent logger specifically, not
    root, and with ``propagate=False``, so uvicorn's own log lines are never
    duplicated by also bubbling up through a root handler.
    """
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    app_logger = logging.getLogger("finmeta")
    app_logger.setLevel(logging.INFO)
    app_logger.addHandler(handler)
    app_logger.propagate = False


def main() -> None:
    _configure_logging()
    config_path = os.environ.get("FINMETA_CONFIG", DEFAULT_CONFIG_PATH)
    host = os.environ.get("FINMETA_HOST", "0.0.0.0")
    port = int(os.environ.get("FINMETA_PORT", "8765"))
    app = create_app_from_path(config_path)
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
