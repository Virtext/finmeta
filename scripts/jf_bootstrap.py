#!/usr/bin/env python3
"""Bootstrap the throwaway Jellyfin test server (S2).

Reads credentials from ``.jf-test/creds.json`` (created by the human).
If the startup wizard is not completed, exits with an error. Uses the
api_key for authentication (same as production Finmeta). Creates the three
test libraries if missing, then waits for the library scan to settle.
``--reset`` deletes and recreates the libraries.

The script only ever talks to the server over its HTTP API — it never
touches the server's container, config or cache folders.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Make the repo importable even if the editable install is not on the path.
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from finmeta.jellyfin_client import JellyfinClient  # noqa: E402

DEFAULT_URL = os.environ.get("JF_TEST_URL", "http://192.168.1.10:8097")
CREDS_PATH = ROOT / ".jf-test" / "creds.json"

# (name, collectionType, server path) — the fixture is mounted at /media.
LIBRARIES = [
    ("Movies", "movies", "/media/Movies"),
    ("Shows", "tvshows", "/media/Shows"),
    ("Untouched", "movies", "/media/Untouched"),
]

DEFAULT_TIMEOUT = 300.0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=None, help="Jellyfin base URL (from creds if omitted)")
    ap.add_argument("--reset", action="store_true",
                    help="delete and recreate the test libraries")
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                    help="scan-settle timeout in seconds")
    args = ap.parse_args(argv)

    # -- load credentials -------------------------------------------------
    if not CREDS_PATH.exists():
        print("ERROR: .jf-test/creds.json not found (human must create it).",
              file=sys.stderr)
        return 2
    creds = json.loads(CREDS_PATH.read_text())
    url = args.url or creds.get("url", DEFAULT_URL)
    api_key = creds.get("api_key")
    username = creds.get("username", "finmeta")
    if not api_key:
        print("ERROR: no api_key in .jf-test/creds.json.", file=sys.stderr)
        return 2

    # -- connect & verify wizard ------------------------------------------
    with JellyfinClient(url, token=api_key) as c:
        info = c.system_info()
        print(f"server={info.server_name} v{info.version} "
              f"wizard_done={info.startup_wizard_completed}")
        if not info.startup_wizard_completed:
            print("ERROR: test server not set up (human). "
                  "StartupWizardCompleted is false; complete the wizard manually.",
                  file=sys.stderr)
            return 3

        # -- get user_id from public user list ----------------------------
        users = c._request("GET", "/Users") or []
        user_id = None
        for u in users:
            if u.get("Name") == username:
                user_id = u.get("Id")
                break
        if not user_id:
            print(f"ERROR: user '{username}' not found in /Users.",
                  file=sys.stderr)
            return 4
        c.user_id = user_id
        print(f"authenticated via api_key; user={username} id={user_id}")

        # -- libraries ------------------------------------------------------
        if args.reset:
            for name, _, _ in LIBRARIES:
                if c.get_library(name) is not None:
                    c.delete_library(name)
                    print(f"deleted library '{name}'")

        created = False
        for name, ctype, path in LIBRARIES:
            if c.get_library(name) is None:
                c.create_library(name, ctype, [path])
                print(f"created library '{name}' ({ctype}) -> {path}")
                created = True
            else:
                print(f"library '{name}' already present")

        item_ids = {name: c.get_library(name).item_id for name, _, _ in LIBRARIES}
        if created or args.reset:
            c.refresh_library()
            print("scan started; waiting for it to settle ...")
            c.wait_for_scan(
                lambda: sum(c.count_items(item_ids[n]) for n in item_ids),
                timeout=args.timeout)
        for name, _, _ in LIBRARIES:
            print(f"  {name}: {c.count_items(item_ids[name])} items")

        print("bootstrap complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
