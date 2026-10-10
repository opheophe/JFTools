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
