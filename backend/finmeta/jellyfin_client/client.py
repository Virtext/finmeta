"""Typed sync HTTP client for the subset of the Jellyfin API Finmeta uses.

Ground truth for every endpoint/param here is the test server's OpenAPI spec
(docs/jellyfin-openapi.json). Query params use the spec's camelCase names;
array-typed params (e.g. ``fields``, ``includeItemTypes``) are sent as
comma-joined strings.
"""
from __future__ import annotations

import base64
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence

import httpx

from .types import AuthResult, Item, Library, MediaStream, SystemInfo, Task

_AUTH_HEADER = (
    'MediaBrowser Client="Finmeta", Device="dev", '
    'DeviceId="finmeta-dev", Version="0.1.0"'
)
DEFAULT_ITEM_FIELDS = "Path,ProviderIds,Overview"


@dataclass
class ItemPage:
    """One page of ``GET /Items``, with Jellyfin's reported total.

    ``total_record_count`` falls back to ``len(items)`` when the server
    omits ``TotalRecordCount`` (seen on some proxies/older servers), so
    callers can always rely on the field being present.
    """

    items: List[Item] = field(default_factory=list)
    total_record_count: int = 0
    start_index: int = 0


class JellyfinError(RuntimeError):
    """A non-2xx response from the Jellyfin API."""

    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


class ServerResetRequired(RuntimeError):
    """The test server is in a state that cannot be repaired via the API.

    A human must reset the server (recreate the container or wipe the
    config/cache folders) before the bootstrap can proceed.
    """


def _join(value: Any) -> Optional[str]:
    """Normalise a scalar or list into a comma-joined query string."""
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return ",".join(str(v) for v in value)
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def normalize_base_path(base_path: Optional[str]) -> str:
    """Normalize a reverse-proxy sub-path to ``"/jellyfin"`` or ``""``.

    Accepts ``None``/empty -> ``""``; adds a leading slash; strips trailing
    slashes. Shared by the client and the service-test button (SPEC S9 §A2).
    """
    if base_path is None:
        return ""
    bp = str(base_path).strip()
    if not bp:
        return ""
    if not bp.startswith("/"):
        bp = "/" + bp
    return bp.rstrip("/") or ""



def _first_item_streams(item: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Primary media source's MediaStreams as raw dicts ([] when absent)."""
    sources = item.get("MediaSources") or [item]
    for s in sources:
        streams = s.get("MediaStreams")
        if streams:
            return list(streams)
    return []

class JellyfinClient:
    def __init__(self, base_url: str, *, token: Optional[str] = None,
                 user_id: Optional[str] = None, timeout: float = 30.0,
                 base_path: Optional[str] = None, verify_ssl: bool = True,
                 client: Optional[httpx.Client] = None) -> None:
        self.base_url = str(base_url).rstrip("/")
        self.base_path = normalize_base_path(base_path)
        self.token = token
        self.user_id = user_id
        self._external = client is not None
        self._client = client or httpx.Client(base_url=self.base_url, timeout=timeout,
                                              verify=verify_ssl)

    # -- lifecycle ----------------------------------------------------------
    def close(self) -> None:
        if not self._external:
            self._client.close()

    def __enter__(self) -> "JellyfinClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _ensure_user_id(self) -> None:
        """Resolve user_id lazily from the server if not already set.

        Lets callers construct a client without an extra round-trip and
        still call item endpoints that require a userId.
        """
        if self.user_id is None and self.token:
            users = self._request("GET", "/Users") or []
            self.user_id = users[0]["Id"] if users else None


    # -- low level ----------------------------------------------------------
    @staticmethod
    def auth_header(token: str) -> str:
        """The Authorization header value for a Jellyfin API key.

        Single source so the service-test button sends the exact same header
        the client uses (``X-Emby-Token`` is NOT accepted by this server).
        """
        return f'{_AUTH_HEADER}, Token="{token}"'

    def _headers(self) -> dict:
        h = {"Accept": "application/json"}
        if self.token:
            h["Authorization"] = self.auth_header(self.token)
        return h

    @staticmethod
    def _raise_for(resp: httpx.Response) -> None:
        if resp.status_code >= 400:
            raise JellyfinError(
                f"{resp.status_code} for {resp.request.method} "
                f"{resp.request.url.path}: {resp.text[:300]}",
                status_code=resp.status_code,
            )

    def _request(self, method: str, path: str, *,
                 params: Optional[dict] = None, json: Optional[dict] = None):
        full_path = self.base_path + path
        resp = self._client.request(method, full_path, headers=self._headers(),
                                    params=params, json=json)
        self._raise_for(resp)
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    @staticmethod
    def _params(**kw: Any) -> dict:
        out: dict = {}
        for k, v in kw.items():
            if v is None:
                continue
            out[k] = _join(v)
        return out

    # -- system & auth ------------------------------------------------------
    def system_info(self) -> SystemInfo:
        return SystemInfo.from_dict(self._request("GET", "/System/Info/Public"))

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

    # -- startup wizard -----------------------------------------------------
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
                         password: str = "finmeta-test") -> AuthResult:
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

    def get_libraries(self) -> List[Library]:
        arr = self._request("GET", "/Library/VirtualFolders") or []
        return [Library.from_dict(x) for x in arr]

    def get_library(self, name: str) -> Optional[Library]:
        for lib in self.get_libraries():
            if lib.name == name:
                return lib
        return None

    def delete_library(self, name: str, *, refresh_library: bool = False) -> None:
        self._request("DELETE", "/Library/VirtualFolders",
                      params={"name": name, "refreshLibrary": _join(refresh_library)})

    def refresh_library(self) -> None:
        self._request("POST", "/Library/Refresh")

    # -- items --------------------------------------------------------------
    def get_items_page(self, *, parent_id: Optional[str] = None, recursive: bool = True,
                       include_item_types: Optional[Sequence[str]] = None,
                       fields: Optional[str] = DEFAULT_ITEM_FIELDS,
                       user_id: Optional[str] = None, limit: Optional[int] = None,
                       start_index: Optional[int] = None,
                       sort_by: Optional[Sequence[str]] = None,
                       sort_order: Optional[str] = None,
                       search_term: Optional[str] = None) -> ItemPage:
        """One page of ``GET /Items``, with Jellyfin's ``TotalRecordCount``.

        Every library-scale caller should go through this (or :meth:`iter_items`)
        instead of an unbounded ``get_items()`` -- a whole-library fetch with no
        ``Limit``/``IncludeItemTypes`` is what times out on large libraries.
        """
        self._ensure_user_id()
        start = start_index or 0
        params = self._params(
            userId=user_id or self.user_id, parentId=parent_id, recursive=recursive,
            includeItemTypes=include_item_types, fields=fields, limit=limit,
            startIndex=start_index, sortBy=sort_by, sortOrder=sort_order,
            searchTerm=search_term, enableTotalRecordCount=True)
        d = self._request("GET", "/Items", params=params) or {}
        raw_items = d.get("Items", [])
        items = [Item.from_dict(x) for x in raw_items]
        total = d.get("TotalRecordCount")
        return ItemPage(items=items,
                        total_record_count=int(total) if total is not None else len(items),
                        start_index=start)

    def get_items(self, *, parent_id: Optional[str] = None, recursive: bool = True,
                  include_item_types: Optional[Sequence[str]] = None,
                  fields: Optional[str] = DEFAULT_ITEM_FIELDS,
                  user_id: Optional[str] = None, limit: Optional[int] = None,
                  start_index: Optional[int] = None) -> List[Item]:
        """Backward-compatible wrapper around :meth:`get_items_page`.

        Unbounded (no ``limit``) and un-typed calls are exactly the shape that
        times out against a large real library -- prefer :meth:`get_items_page`
        or :meth:`iter_items` for any library-scale enumeration.
        """
        return self.get_items_page(
            parent_id=parent_id, recursive=recursive,
            include_item_types=include_item_types, fields=fields,
            user_id=user_id, limit=limit, start_index=start_index,
        ).items

    def iter_items(self, *, parent_id: Optional[str] = None, recursive: bool = True,
                   include_item_types: Optional[Sequence[str]] = None,
                   fields: Optional[str] = DEFAULT_ITEM_FIELDS,
                   user_id: Optional[str] = None,
                   page_size: int = 200) -> Iterator[Item]:
        """Page through a whole library, de-duplicated, with a stable sort.

        Always sorts by ``SortName,Id`` so pages can't skip or repeat items
        if the library changes mid-walk. Stops on the first of: an empty
        page, ``start_index >= total``, a short page (fewer items than
        requested), or a page containing no item ID not already seen.
        """
        size = max(1, min(int(page_size), 500))
        start = 0
        seen: set = set()
        while True:
            page = self.get_items_page(
                parent_id=parent_id, recursive=recursive,
                include_item_types=include_item_types, fields=fields,
                user_id=user_id, limit=size, start_index=start,
                sort_by=["SortName", "Id"], sort_order="Ascending",
            )
            if not page.items:
                return
            new_items = [it for it in page.items if it.id not in seen]
            if not new_items:
                return
            for it in new_items:
                seen.add(it.id)
                yield it
            start += len(page.items)
            if len(page.items) < size:
                return
            if start >= page.total_record_count:
                return

    def get_item(self, item_id: str) -> Item:
        self._ensure_user_id()
        d = self._request("GET", f"/Items/{item_id}",
                          params=self._params(userId=self.user_id))
        return Item.from_dict(d or {})

    def get_items_by_type(self, item_type: str, *, parent_id: Optional[str] = None,
                          recursive: bool = True) -> List[Item]:
        return self.get_items(parent_id=parent_id, recursive=recursive,
                              include_item_types=[item_type])

    def count_items(self, parent_id: str, *, recursive: bool = True) -> int:
        return self.get_items_page(parent_id=parent_id, recursive=recursive,
                                   fields=None, limit=1).total_record_count

    # -- images (S9d5: artwork_write_method = "jellyfin_api") ---------------
    def get_image_bytes(self, item_id: str, image_type: str) -> Optional[bytes]:
        """Raw bytes of an item's CURRENT image (``GET /Items/{id}/Images/
        {imageType}``), or ``None`` when the item has no image of that type.

        Used to capture the pre-Finmeta original ONCE for ``jellyfin_api``
        write mode, the equivalent of ``find_original_backup`` for sidecar
        files -- there is no local file to back up in this mode, so the
        original has to be fetched from Jellyfin itself before Finmeta ever
        uploads anything.
        """
        full_path = self.base_path + f"/Items/{item_id}/Images/{image_type}"
        resp = self._client.get(full_path, headers=self._headers())
        if resp.status_code == 404:
            return None
        self._raise_for(resp)
        return resp.content

    def upload_image(self, item_id: str, image_type: str, data: bytes,
                     content_type: str = "image/jpeg") -> None:
        """Set (overwrite) an item's image (``POST /Items/{id}/Images/
        {imageType}``). Jellyfin returns 204 on success.

        Despite the OpenAPI spec listing the body as raw ``image/*`` binary,
        the server unconditionally Base64-decodes it
        (``MediaBrowser.Providers.Manager.ImageSaver.SaveImageToLocation``
        pipes the request stream through a ``FromBase64Transform``) -- a
        spec/implementation mismatch confirmed against a real server
        (13.0.0), which raw bytes fail with ``System.FormatException: One of
        the identified items was in an invalid format.`` Content-Type stays
        the real image mime type; only the body needs encoding.
        """
        full_path = self.base_path + f"/Items/{item_id}/Images/{image_type}"
        headers = self._headers()
        headers["Content-Type"] = content_type
        resp = self._client.post(full_path, headers=headers,
                                 content=base64.b64encode(data))
        self._raise_for(resp)

    def refresh_item(self, item_id: str, *,
                     metadata_refresh_mode: str = "FullRefresh",
                     image_refresh_mode: str = "FullRefresh",
                     replace_all_metadata: bool = False,
                     replace_all_images: bool = True) -> None:
        self._request("POST", f"/Items/{item_id}/Refresh", params=self._params(
            metadataRefreshMode=metadata_refresh_mode,
            imageRefreshMode=image_refresh_mode,
            replaceAllMetadata=replace_all_metadata,
            replaceAllImages=replace_all_images))

    # -- scheduled tasks / scan --------------------------------------------
    def get_media_streams(self, item_id: str) -> List[MediaStream]:
        """Return the media streams (video/audio/subtitle tracks) for an item."""
        d = self._request("GET", f"/Items/{item_id}/MediaSources") or []
        if not d:
            return []
        return [MediaStream.from_dict(s) for s in (d[0].get("MediaStreams") or [])]

    def get_item_media_streams_raw(
        self, item_id: str,
    ) -> List[Dict[str, Any]]:
        """Return the primary media source's MediaStreams as RAW API dicts.

        Uses GET /Items?Ids=: with Fields=MediaSources,MediaStreams (the API key is
        accepted here; /Items/{id}/... without a user id can 400 on some servers). Raw
        dicts keep every field -- DisplayTitle, IsForced, ChannelLayout, ColorTransfer
        and friends -- which K9 format-rule detection needs but the trimmed
        :class:`MediaStream` type intentionally drops. Returns [] when absent."""
        d = self._request("GET", "/Items", params=self._params(
            Ids=item_id, Fields="MediaSources,MediaStreams")) or {}
        items = d.get("Items") or []
        if not items:
            return []
        return _first_item_streams(items[0])


    def get_items_media_streams_raw(
        self, item_ids: Sequence[str], batch_size: int = 50,
    ) -> Dict[str, List[Dict[str, Any]]]:
        """Batched raw MediaStreams for many items (S9d1 step 3).

        Fetches ``GET /Items?Ids=a,b,c`` in batches of ``batch_size`` IDs and
        returns ``{item_id: [raw stream dicts]}`` — the SAME raw dicts as
        :meth:`get_item_media_streams_raw` (single source, K9 fidelity), just
        with ceil(N/batch) requests instead of N. Items without streams map to
        []. Raises on any batch failure (caller degrades per-item).
        """
        ids = [i for i in item_ids if i]
        out: Dict[str, List[Dict[str, Any]]] = {i: [] for i in ids}
        size = max(1, int(batch_size))
        for start in range(0, len(ids), size):
            chunk = ids[start:start + size]
            d = self._request("GET", "/Items", params=self._params(
                Ids=",".join(chunk), Fields="MediaSources,MediaStreams")) or {}
            for it in d.get("Items") or []:
                iid = it.get("Id")
                if iid is None:
                    continue
                out[iid] = _first_item_streams(it)
        return out

    def get_scheduled_tasks(self) -> List[Task]:
        arr = self._request("GET", "/ScheduledTasks") or []
        return [Task.from_dict(x) for x in arr]

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

    # -- theme songs --------------------------------------------------------
    def get_theme_songs(self, item_id: str) -> List[Item]:
        self._ensure_user_id()
        d = self._request("GET", f"/Items/{item_id}/ThemeSongs",
                          params=self._params(userId=self.user_id,
                                              inheritFromParent=True)) or {}
        return [Item.from_dict(x) for x in d.get("Items", [])]

    # -- collections --------------------------------------------------------
    def create_collection(self, name: str, ids: Sequence[str]) -> dict:
        return self._request("POST", "/Collections",
                             params={"name": name, "ids": _join(ids)}) or {}

    def get_collections(self) -> List[Item]:
        return self.get_items(include_item_types=["BoxSet"])

    def get_collection_by_name(self, name: str) -> Optional[Item]:
        for it in self.get_collections():
            if it.name == name:
                return it
        return None

    def add_to_collection(self, collection_id: str, ids: Sequence[str]) -> None:
        self._request("POST", f"/Collections/{collection_id}/Items",
                      params={"ids": _join(ids)})

    def remove_from_collection(self, collection_id: str,
                               ids: Sequence[str]) -> None:
        self._request("DELETE", f"/Collections/{collection_id}/Items",
                      params={"ids": _join(ids)})

    def delete_collection(self, collection_id: str) -> None:
        self._request("DELETE", f"/Items/{collection_id}")

    # -- playlists ----------------------------------------------------------
    def create_playlist(self, name: str, ids: Sequence[str], *,
                        user_id: Optional[str] = None,
                        media_type: str = "Video") -> dict:
        return self._request("POST", "/Playlists", params={
            "name": name, "ids": _join(ids), "userId": user_id or self.user_id,
            "mediaType": media_type}) or {}

    def get_playlists(self) -> List[Item]:
        return self.get_items(include_item_types=["Playlist"])

    def get_playlist_by_name(self, name: str) -> Optional[Item]:
        for it in self.get_playlists():
            if it.name == name:
                return it
        return None

    def add_to_playlist(self, playlist_id: str, ids: Sequence[str], *,
                        user_id: Optional[str] = None) -> None:
        self._request("POST", f"/Playlists/{playlist_id}/Items",
                      params={"ids": _join(ids), "userId": user_id or self.user_id})

    def remove_from_playlist(self, playlist_id: str,
                             ids: Sequence[str]) -> None:
        self._request("DELETE", f"/Playlists/{playlist_id}/Items",
                      params={"entryIds": _join(ids)})

    def delete_playlist(self, playlist_id: str) -> None:
        self._request("DELETE", f"/Items/{playlist_id}")
