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
    ) -> None:
        self.base_url = normalize_base_url(base_url)
        self.api_key = (api_key or "").strip()
        self.timeout = timeout
        self._transport = transport

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": (
                'MediaBrowser Client="MedServ", Device="MedServ", '
                'DeviceId="medserv", Version="1.0.0", '
                f'Token="{self.api_key}"'
            ),
            "Accept": "application/json",
        }

    async def _get(self, path: str, params: dict | None = None) -> dict:
        if not self.base_url:
            raise JellyfinError("Jellyfin URL is not configured.")
        if not self.api_key:
            raise JellyfinError("Jellyfin API key is not configured.")
        async with httpx.AsyncClient(timeout=self.timeout, transport=self._transport) as client:
            resp = await client.get(
                self.base_url + path,
                headers=self._headers(),
                params=params,
            )
        if resp.status_code == 401:
            raise JellyfinError("Unauthorized — check the Jellyfin API key.")
        if resp.status_code >= 400:
            raise JellyfinError(f"Jellyfin returned HTTP {resp.status_code} for {path}.")
        return resp.json()

    async def get_system_info(self) -> dict:
        return await self._get("/System/Info")

    async def get_libraries(self) -> list:
        return await self._get("/Library/VirtualFolders")

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
