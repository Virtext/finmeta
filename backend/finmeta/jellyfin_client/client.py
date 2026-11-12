"""Typed sync HTTP client for the subset of the Jellyfin API Finmeta uses.

Ground truth for every endpoint/param here is the test server's OpenAPI spec
(docs/jellyfin-openapi.json). Query params use the spec's camelCase names;
array-typed params (e.g. ``fields``, ``includeItemTypes``) are sent as
comma-joined strings.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

import httpx

from .types import Item, Library, MediaStream, SystemInfo, Task

_AUTH_HEADER = (
    'MediaBrowser Client="Finmeta", Device="dev", '
    'DeviceId="finmeta-dev", Version="2.0.0"'
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

    # -- libraries ----------------------------------------------------------
    def get_libraries(self) -> List[Library]:
        arr = self._request("GET", "/Library/VirtualFolders") or []
        return [Library.from_dict(x) for x in arr]

    def get_library(self, name: str) -> Optional[Library]:
        for lib in self.get_libraries():
            if lib.name == name:
                return lib
        return None

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

    def get_image_bytes(self, item_id: str, image_type: str, *,
                        index: int = 0,
                        max_width: Optional[int] = None) -> Optional[Tuple[bytes, str]]:
        """Raw bytes of one of an item's OWN current Jellyfin images.

        Deliberately separate from the candidates/resolution machinery
        (``engine/resolution.py``): that path does TMDb matching + provider
        collection per item (~7s/item cold, see routes_core.py's docstring
        on ``/items/{id}/plan``) -- far too slow for a poster grid of
        hundreds of items. This is one plain HTTP GET against Jellyfin's
        own image endpoint, used by the modern UI's Library grid
        (SPEC_S14_UI.md phase P3, decision D8) to show what Jellyfin is
        ACTUALLY serving today. Returns ``None`` on a 404 (no such image)
        rather than raising -- a library grid expects many items to be
        missing one kind or another; that is data, not an error.

        ``max_width`` asks Jellyfin to resize server-side (``?maxWidth=``,
        the same param its own web client uses for thumbnails) -- without
        it, Jellyfin returns the original file, often several MB, for every
        tile in a grid of hundreds (operator-reported: image loading is slow
        throughout the modern UI). Omitted (``None``) for callers that need
        the real full-resolution image (e.g. the single-item detail view).
        """
        path = f"{self.base_path}/Items/{item_id}/Images/{image_type}/{index}"
        params = {"maxWidth": max_width, "quality": 90} if max_width else None
        resp = self._client.get(path, headers=self._headers(), params=params)
        if resp.status_code == 404:
            return None
        self._raise_for(resp)
        return resp.content, resp.headers.get("Content-Type", "application/octet-stream")

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
