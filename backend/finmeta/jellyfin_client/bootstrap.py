"""Bootstrap-only Jellyfin client (audit 2026-11-11 "D8").

``create_library``, ``delete_library``, the startup-wizard flow and
``authenticate`` never had a production caller: Finmeta's own runtime
always constructs :class:`JellyfinClient` with an operator-provided API
key (``token=...``), never logs in itself, and never creates, deletes or
reconfigures a Jellyfin library. Those capabilities existed only for
``scripts/jf_bootstrap.py`` (sets up the throwaway S2 test server) and
the test suite. Shipping a ``DELETE /Library/VirtualFolders`` wrapper and
an admin-user-creating wizard (with a hardcoded default password) in the
production package is capability nobody needs -- the original review
praised :class:`finmeta.arr.base.ArrClient` for being *structurally*
read-only; the base :class:`JellyfinClient` is now the same, and this
subclass carries everything that isn't.

Only ``scripts/jf_bootstrap.py`` and the test suite import this module --
never any production code path.
"""
from __future__ import annotations

import time
from typing import Callable, List, Optional, Sequence

from .client import JellyfinClient, JellyfinError, _join
from .types import AuthResult, Item, Library


class ServerResetRequired(RuntimeError):
    """The test server is in a state that cannot be repaired via the API.

    A human must reset the server (recreate the container or wipe the
    config/cache folders) before the bootstrap can proceed.
    """


class BootstrapJellyfinClient(JellyfinClient):
    """Adds the throwaway-test-server setup surface to :class:`JellyfinClient`:
    the startup wizard, authentication, library create/delete, and
    scan-settle polling."""

    # -- auth -----------------------------------------------------------
    def authenticate(self, username: str, password: str) -> AuthResult:
        d = self._request("POST", "/Users/AuthenticateByName",
                          json={"Username": username, "Pw": password})
        user = (d or {}).get("User") or {}
        res = AuthResult(access_token=d.get("AccessToken", ""),
                         user_id=user.get("Id", ""),
                         user_name=user.get("Name", ""))
        self.token = res.access_token
        self.user_id = res.user_id
        return res

    # -- startup wizard ---------------------------------------------------
    def wizard_configuration(self, ui_culture: str = "en-US",
                             metadata_country: str = "US",
                             preferred_language: str = "en") -> None:
        self._request("POST", "/Startup/Configuration", json={
            "UICulture": ui_culture, "MetadataCountryCode": metadata_country,
            "PreferredMetadataLanguage": preferred_language})

    def wizard_get_user(self) -> Optional[dict]:
        return self._request("GET", "/Startup/User")

    def wizard_create_user(self, name: str, password: str) -> None:
        self._request("POST", "/Startup/User", json={"Name": name, "Password": password})

    def wizard_remote_access(self, enable: bool = True,
                             port_mapping: bool = False) -> None:
        self._request("POST", "/Startup/RemoteAccess", json={
            "EnableRemoteAccess": enable,
            "EnableAutomaticPortMapping": port_mapping})

    def wizard_complete(self) -> None:
        self._request("POST", "/Startup/Complete")

    def complete_wizard(self, username: str = "finmeta",
                         password: str = "finmeta-test") -> AuthResult:  # noqa: S107 -- throwaway dev-only test-server credential, never reachable from production (this whole class only exists for scripts/jf_bootstrap.py and the test suite)
        """Run the startup wizard (only if needed) and authenticate.

        Wizard steps (in order, per test-server rules):
          1. POST /Startup/Configuration
          2. GET  /Startup/User   (initializes the first user)
             POST /Startup/User   (set Name + Password)
          3. GET  /Startup/User   -> assert Name == username; if not, STOP
          4. POST /Startup/RemoteAccess
             POST /Startup/Complete
          5. Authenticate

        If the wizard is already completed but authentication fails, raise
        :class:`ServerResetRequired` instead of trying to repair the state.
        """
        if self.system_info().startup_wizard_completed:
            try:
                return self.authenticate(username, password)
            except JellyfinError:
                raise ServerResetRequired(
                    "test server needs reset (human): wizard is complete "
                    "but authentication failed (no user persisted)"
                ) from None

        # -- wizard not completed: run all steps in order -------------------
        self.wizard_configuration()
        self.wizard_get_user()
        self.wizard_create_user(username, password)
        # Step 3: verify the user was actually set before completing
        user = self.wizard_get_user()
        if not user or user.get("Name") != username:
            raise ServerResetRequired(
                "test server needs reset (human): POST /Startup/User "
                f"returned 2xx but GET /Startup/User Name={user.get('Name')!r} "
                f"!= {username!r}; stopping before /Startup/Complete"
            )
        self.wizard_remote_access()
        self.wizard_complete()
        return self.authenticate(username, password)

    # -- libraries ----------------------------------------------------------
    @staticmethod
    def library_scan_options() -> dict:
        """Test library options: local NFO reader on, saver off, no remote lookups."""
        return {
            "Enabled": True,
            "EnableRealtimeMonitor": False,
            "EnableAutomaticSeriesGrouping": True,
            "SaveLocalMetadata": False,
            "MetadataSavers": [],
            "DisabledLocalMetadataReaders": [],
            "EnableInternetProviders": False,
        }

    def create_library(self, name: str, collection_type: str,
                       paths: Sequence[str], *, options: Optional[dict] = None,
                       refresh_library: bool = True) -> Library:
        params = {"name": name, "collectionType": collection_type,
                  "paths": _join(paths), "refreshLibrary": _join(refresh_library)}
        body = {"LibraryOptions": options if options is not None
                else self.library_scan_options()}
        d = self._request("POST", "/Library/VirtualFolders", params=params, json=body)
        return Library.from_dict(d or {})

    def delete_library(self, name: str, *, refresh_library: bool = False) -> None:
        self._request("DELETE", "/Library/VirtualFolders",
                      params={"name": name, "refreshLibrary": _join(refresh_library)})

    def refresh_library(self) -> None:
        self._request("POST", "/Library/Refresh")

    # -- items --------------------------------------------------------------
    def get_items_by_type(self, item_type: str, *, parent_id: Optional[str] = None,
                          recursive: bool = True) -> List[Item]:
        return self.get_items(parent_id=parent_id, recursive=recursive,
                              include_item_types=[item_type])

    def count_items(self, parent_id: str, *, recursive: bool = True) -> int:
        return self.get_items_page(parent_id=parent_id, recursive=recursive,
                                   fields=None, limit=1).total_record_count

    # -- scan settle ----------------------------------------------------
    def wait_for_scan(self, count_fn: Callable[[], int], *, poll_interval: float = 5.0,
                      timeout: float = 300.0) -> int:
        """Wait until no task is running and item count is stable (two polls apart)."""
        deadline = time.monotonic() + timeout
        prev: Optional[int] = None
        while True:
            running = any(t.is_running for t in self.get_scheduled_tasks())
            cnt = count_fn()
            if not running and prev == cnt:
                return cnt
            prev = cnt
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"scan not settled within {timeout:.0f}s (running={running}, count={cnt})")
            time.sleep(poll_interval)


__all__ = ["BootstrapJellyfinClient", "ServerResetRequired"]
