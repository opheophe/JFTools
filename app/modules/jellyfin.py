"""The first MedServ module: live Jellyfin API actions."""
from __future__ import annotations

from ..jellyfin.client import JellyfinClient, JellyfinError
from .base import Action, Context, Module, kv, message, register_module, table


def _client(ctx: Context) -> JellyfinClient:
    return JellyfinClient(ctx.settings.jellyfin_url, ctx.settings.jellyfin_api_key)


async def test_connection(ctx: Context) -> dict:
    info = await _client(ctx).get_system_info()
    items = [
        ["Server name", info.get("ServerName", "?")],
        ["Version", info.get("Version", "?")],
        ["Operating system", info.get("OperatingSystem", "?")],
        ["Server ID", info.get("Id", "?")],
    ]
    return kv(items, title="Connection OK")


async def list_libraries(ctx: Context) -> dict:
    client = _client(ctx)
    libraries = await client.get_libraries()
    rows = []
    for lib in libraries:
        name = lib.get("Name", "?")
        ctype = lib.get("CollectionType") or "mixed"
        item_id = lib.get("ItemId")
        count = ""
        if item_id:
            try:
                count = str(await client.get_library_item_count(item_id))
            except JellyfinError:
                count = "?"
        rows.append([name, ctype, count])
    if not rows:
        return message("No libraries found.", title="Libraries")
    return table(["Name", "Type", "Items"], rows, title="Libraries")


async def recently_added(ctx: Context) -> dict:
    data = await _client(ctx).get_recent(limit=12)
    rows = []
    for item in data.get("Items", []):
        name = item.get("Name", "?")
        parent = item.get("SeriesName") or item.get("Album") or ""
        title = f"{parent} — {name}" if parent else name
        year = item.get("ProductionYear") or ""
        added = (item.get("DateCreated") or "")[:10]
        rows.append([title, item.get("Type", "?"), year, added])
    if not rows:
        return message("No recent items found.", title="Recently added")
    return table(["Title", "Type", "Year", "Added"], rows, title="Recently added")


async def library_stats(ctx: Context) -> dict:
    counts = await _client(ctx).get_counts()
    items = [
        ["Movies", counts.get("MovieCount", 0)],
        ["Series", counts.get("SeriesCount", 0)],
        ["Episodes", counts.get("EpisodeCount", 0)],
        ["Albums", counts.get("AlbumCount", 0)],
        ["Songs", counts.get("SongCount", 0)],
    ]
    return kv(items, title="Library stats")


async def active_sessions(ctx: Context) -> dict:
    sessions = await _client(ctx).get_sessions()
    rows = []
    for s in sessions:
        now_playing = s.get("NowPlayingItem") or {}
        rows.append(
            [
                s.get("UserName") or "?",
                now_playing.get("Name") or "Idle",
                s.get("DeviceName") or "?",
                s.get("Client") or "?",
            ]
        )
    if not rows:
        return message("No active sessions.", title="Active sessions")
    return table(
        ["User", "Now playing", "Device", "Client"],
        rows,
        title="Active sessions",
    )


register_module(
    Module(
        id="jellyfin",
        name="Jellyfin",
        description="Interact with your Jellyfin server over its API.",
        actions=[
            Action("test", "Test connection", "Check connectivity and show server info.", test_connection),
            Action("libraries", "List libraries", "Show libraries with item counts.", list_libraries),
            Action("recent", "Recently added", "Show the most recently added media.", recently_added),
            Action("stats", "Library stats", "Show movie/series/episode counts.", library_stats),
            Action("sessions", "Active sessions", "Show who is watching what now.", active_sessions),
        ],
    )
)
