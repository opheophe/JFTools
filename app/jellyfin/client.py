"""A thin async client for the Jellyfin HTTP API.

Authenticates with an API key (Jellyfin -> Dashboard -> API Keys) sent via the
``Authorization: MediaBrowser Token="..."`` header. (Newer Jellyfin releases no
longer accept the legacy ``X-Emby-Token`` header.)
"""
from __future__ import annotations

import httpx


def normalize_base_url(url: str) -> str:
    """Return the API root, dropping any trailing '/web' (the web player path)."""
    u = (url or "").strip().rstrip("/")
    if u.endswith("/web"):
        u = u[: -len("/web")].rstrip("/")
    return u


class JellyfinError(Exception):
    """Raised for configuration or upstream Jellyfin errors."""


class JellyfinClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = normalize_base_url(base_url)
        self.api_key = (api_key or "").strip()
        self.timeout = timeout
        self._transport = transport
        self._http = http  # optional shared client; reused across calls when set

    def _use(self) -> tuple[httpx.AsyncClient, bool]:
        """Return (client, should_close). Reuse the shared client when set."""
        if self._http is not None:
            return self._http, False
        return httpx.AsyncClient(timeout=self.timeout, transport=self._transport), True

    def _headers(self, with_token: bool = True) -> dict[str, str]:
        auth = 'MediaBrowser Client="JFTools", Device="JFTools", DeviceId="jftools", Version="1.0.0"'
        if with_token:
            auth += f', Token="{self.api_key}"'
        return {"Authorization": auth, "Accept": "application/json"}

    def _check(self) -> None:
        if not self.base_url:
            raise JellyfinError("Jellyfin URL is not configured.")
        if not self.api_key:
            raise JellyfinError("Jellyfin API key is not configured.")

    async def _get(self, path: str, params: dict | None = None) -> dict:
        self._check()
        client, close = self._use()
        try:
            resp = await client.get(
                self.base_url + path,
                headers=self._headers(),
                params=params,
            )
        finally:
            if close:
                await client.aclose()
        if resp.status_code == 401:
            raise JellyfinError("Unauthorized — check the Jellyfin API key.")
        if resp.status_code >= 400:
            raise JellyfinError(f"Jellyfin returned HTTP {resp.status_code} for {path}.")
        return resp.json()

    async def _post(self, path: str, json: dict | None = None) -> dict | None:
        self._check()
        client, close = self._use()
        try:
            resp = await client.post(
                self.base_url + path,
                headers=self._headers(),
                json=json,
            )
        finally:
            if close:
                await client.aclose()
        if resp.status_code == 401:
            raise JellyfinError("Unauthorized — check the Jellyfin API key.")
        if resp.status_code >= 400:
            raise JellyfinError(f"Jellyfin returned HTTP {resp.status_code} for {path}.")
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    async def get_system_info(self) -> dict:
        return await self._get("/System/Info")

    async def authenticate_user(self, username: str, password: str) -> dict:
        """Authenticate a Jellyfin user by username/password; return the User object."""
        if not self.base_url:
            raise JellyfinError("Jellyfin URL is not configured.")
        client, close = self._use()
        try:
            resp = await client.post(
                self.base_url + "/Users/AuthenticateByName",
                headers=self._headers(with_token=False),
                json={"Username": username, "Pw": password},
            )
        finally:
            if close:
                await client.aclose()
        if resp.status_code == 401:
            raise JellyfinError("Invalid Jellyfin username or password.")
        if resp.status_code >= 400:
            raise JellyfinError(f"Jellyfin returned HTTP {resp.status_code} for authentication.")
        return resp.json().get("User") or {}

    async def get_libraries(self) -> list:
        return await self._get("/Library/VirtualFolders")

    async def get_users(self) -> list:
        return await self._get("/Users")

    async def get_user_items(self, user_id: str, params: dict) -> dict:
        return await self._get(f"/Users/{user_id}/Items", params=params)

    async def get_items(self, params: dict) -> dict:
        return await self._get("/Items", params=params)

    async def get_library_item_count(self, parent_id: str) -> int:
        data = await self._get(
            "/Items",
            params={
                "ParentId": parent_id,
                "Recursive": "true",
                "Limit": "0",
                "EnableTotalRecordCount": "true",
            },
        )
        return int(data.get("TotalRecordCount", 0) or 0)

    async def get_counts(self) -> dict:
        return await self._get("/Items/Counts")

    async def get_recent(self, limit: int = 12) -> dict:
        return await self._get(
            "/Items",
            params={
                "SortBy": "DateCreated",
                "SortOrder": "Descending",
                "IncludeItemTypes": "Movie,Series,Episode",
                "Recursive": "true",
                "Limit": str(limit),
                "Fields": "DateCreated,ProductionYear,SeriesName,Album",
                "Filters": "IsNotFolder",
            },
        )

    async def get_sessions(self) -> list:
        return await self._get("/Sessions")

    async def get_item(self, user_id: str, item_id: str, fields: str = "") -> dict:
        """Fetch one item's full metadata as a specific user sees it."""
        params = {"Fields": fields} if fields else None
        return await self._get(f"/Users/{user_id}/Items/{item_id}", params=params)

    async def get_item_any(self, item_id: str, fields: str = "", user_id: str = "") -> dict:
        """Fetch one item's metadata. Jellyfin requires ``userId`` when an API key
        is used (the user-less form 400s), so callers should supply one."""
        params: dict = {}
        if fields:
            params["Fields"] = fields
        if user_id:
            params["userId"] = user_id
        return await self._get(f"/Items/{item_id}", params=params or None)

    async def get_image(
        self,
        item_id: str,
        tag: str = "",
        kind: str = "Primary",
        max_width: int = 240,
    ) -> tuple[bytes, str]:
        """Fetch an item's image (Primary/Backdrop/Thumb/Logo); returns (bytes, content_type)."""
        if not self.base_url:
            raise JellyfinError("Jellyfin URL is not configured.")
        if not self.api_key:
            raise JellyfinError("Jellyfin API key is not configured.")
        params = {"maxWidth": str(max_width), "quality": "90"}
        if tag:
            params["tag"] = tag
        headers = self._headers()
        headers["Accept"] = "image/*"
        client, close = self._use()
        try:
            resp = await client.get(
                f"{self.base_url}/Items/{item_id}/Images/{kind}",
                headers=headers,
                params=params,
            )
        finally:
            if close:
                await client.aclose()
        if resp.status_code == 401:
            raise JellyfinError("Unauthorized — check the Jellyfin API key.")
        if resp.status_code >= 400:
            raise JellyfinError(f"Jellyfin returned HTTP {resp.status_code} for image.")
        return resp.content, resp.headers.get("content-type", "image/jpeg")

    async def scan_all(self) -> None:
        """Start a full library scan (all libraries)."""
        await self._post("/Library/Refresh")

    async def scan_paths(self, locations: list[str]) -> None:
        """Scan specific folder paths (targeted per-library scan)."""
        updates = [{"Path": loc, "UpdateType": "Modified"} for loc in locations]
        await self._post("/Library/Media/Updated", json={"Updates": updates})

    async def get_scheduled_tasks(self) -> list:
        return await self._get("/ScheduledTasks")

    async def get_activity_log(self, limit: int = 10) -> list:
        data = await self._get("/System/ActivityLog/Entries", params={"Limit": str(limit)})
        if isinstance(data, dict):
            return data.get("Items", [])
        return data


# ---- shared client ---------------------------------------------------------
# One process-wide JellyfinClient (and one HTTP connection pool) reused by every
# request, instead of opening a fresh AsyncClient per call.

_shared_http: httpx.AsyncClient | None = None
_shared_client: "JellyfinClient | None" = None
_shared_key: tuple[str, str] | None = None


def get_client() -> "JellyfinClient":
    global _shared_http, _shared_client, _shared_key
    from ..config import config

    key = (config.settings.jellyfin_url, config.settings.jellyfin_api_key)
    if _shared_client is None or _shared_key != key:
        if _shared_http is None:
            _shared_http = httpx.AsyncClient(timeout=15.0)
        _shared_client = JellyfinClient(key[0], key[1], http=_shared_http)
        _shared_key = key
    return _shared_client


async def close_shared_client() -> None:
    global _shared_http, _shared_client, _shared_key
    if _shared_http is not None:
        await _shared_http.aclose()
    _shared_http = None
    _shared_client = None
    _shared_key = None
