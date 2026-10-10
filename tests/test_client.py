import httpx
import pytest

from app.jellyfin.client import JellyfinClient, JellyfinError, normalize_base_url


def test_normalize_base_url():
    assert normalize_base_url("http://x:8096/web/") == "http://x:8096"
    assert normalize_base_url("http://x:8096/web") == "http://x:8096"
    assert normalize_base_url("http://x:8096/") == "http://x:8096"
    assert normalize_base_url("http://x:8096") == "http://x:8096"
    assert normalize_base_url("") == ""
    assert normalize_base_url(None) == ""


async def test_get_system_info_sends_token():
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("Authorization")
        seen["path"] = request.url.path
        return httpx.Response(200, json={"ServerName": "Test", "Version": "10.9.0"})

    client = JellyfinClient("http://x:8096", "sekrit", transport=httpx.MockTransport(handler))
    info = await client.get_system_info()

    assert info["ServerName"] == "Test"
    assert 'Token="sekrit"' in seen["auth"]
    assert seen["auth"].startswith("MediaBrowser ")
    assert seen["path"] == "/System/Info"


async def test_unauthorized_raises():
    def handler(request):
        return httpx.Response(401, json={})

    client = JellyfinClient("http://x:8096", "bad", transport=httpx.MockTransport(handler))
    with pytest.raises(JellyfinError, match="Unauthorized"):
        await client.get_system_info()


async def test_missing_url_raises():
    client = JellyfinClient("", "key")
    with pytest.raises(JellyfinError, match="URL"):
        await client.get_system_info()


async def test_missing_key_raises():
    client = JellyfinClient("http://x:8096", "")
    with pytest.raises(JellyfinError, match="API key"):
        await client.get_system_info()


async def test_authenticate_user_sends_pw_and_returns_user():
    import json

    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        seen["path"] = request.url.path
        return httpx.Response(
            200, json={"User": {"Name": "admin", "Policy": {"IsAdministrator": True}}}
        )

    client = JellyfinClient("http://x:8096", "unused", transport=httpx.MockTransport(handler))
    user = await client.authenticate_user("admin", "pw")

    assert user["Name"] == "admin"
    assert user["Policy"]["IsAdministrator"] is True
    assert seen["path"] == "/Users/AuthenticateByName"
    assert seen["body"] == {"Username": "admin", "Pw": "pw"}


async def test_scan_all_posts_library_refresh():
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        seen["method"] = request.method
        return httpx.Response(204)

    client = JellyfinClient("http://x:8096", "key", transport=httpx.MockTransport(handler))
    await client.scan_all()

    assert seen["method"] == "POST"
    assert seen["path"] == "/Library/Refresh"


async def test_scan_paths_sends_updates():
    import json

    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(204)

    client = JellyfinClient("http://x:8096", "key", transport=httpx.MockTransport(handler))
    await client.scan_paths(["/Filmer", "/Shows"])

    assert seen["body"] == {
        "Updates": [
            {"Path": "/Filmer", "UpdateType": "Modified"},
            {"Path": "/Shows", "UpdateType": "Modified"},
        ]
    }


async def test_get_scheduled_tasks():
    def handler(request):
        return httpx.Response(200, json=[{"Name": "Scan Media Library", "State": "Idle"}])

    client = JellyfinClient("http://x:8096", "key", transport=httpx.MockTransport(handler))
    tasks = await client.get_scheduled_tasks()

    assert tasks[0]["Name"] == "Scan Media Library"
