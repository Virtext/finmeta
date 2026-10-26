"""Development / container entry point for the Finmeta API server.

Run directly:  finmeta.api.server  (or  python -m finmeta.api.server)
Config path comes from the ``FINMETA_CONFIG`` env var (default
``config/config.yml``) — no hardcoded paths (D1).
"""
from __future__ import annotations

import os

import uvicorn

from finmeta.api.app import create_app_from_path
from finmeta.api.state import DEFAULT_CONFIG_PATH


def main() -> None:
    config_path = os.environ.get("FINMETA_CONFIG", DEFAULT_CONFIG_PATH)
    host = os.environ.get("FINMETA_HOST", "0.0.0.0")
    port = int(os.environ.get("FINMETA_PORT", "8765"))
    app = create_app_from_path(config_path)
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
