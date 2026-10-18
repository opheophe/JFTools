"""Static assets and pages must never be served stale.

A browser that keeps an old app.js after an upgrade posts the Settings forms
natively instead of through fetch, which reloads the page and saves nothing.
"""
import json
import os

import httpx
from starlette.requests import Request

from app import web
from app.config import config
from app.main import _saved, app


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def test_asset_token_follows_the_newest_static_file(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "STATIC_DIR", tmp_path)
    monkeypatch.setenv("JFTOOLS_VERSION", "1.2.3")
    js = tmp_path / "app.js"
    js.write_text("a", encoding="utf-8")

    os.utime(js, (1000, 1000))
    assert web.asset_token() == "1.2.3-1000"

    os.utime(js, (2000, 2000))
    assert web.asset_token() == "1.2.3-2000"


def test_asset_token_survives_a_missing_static_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "STATIC_DIR", tmp_path / "gone")
    assert web.asset_token().endswith("-0")


def test_static_urls_carry_the_asset_token():
    html = web.templates.env.get_template("login.html").render()
    assert "/static/app.css?v=" in html


async def test_static_assets_must_be_revalidated():
    async with _client() as client:
        resp = await client.get("/static/app.js")
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-cache"


async def test_pages_are_not_cached():
    async with _client() as client:
        resp = await client.get("/setup" if config.needs_setup else "/login")
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-store"


def _request(accept: str) -> Request:
    return Request({
        "type": "http",
        "method": "POST",
        "path": "/settings",
        "headers": [(b"accept", accept.encode())],
    })


def test_saved_answers_json_to_the_page_scripts():
    resp = _saved(_request("*/*"), "Jellyfin settings saved.")
    assert resp.status_code == 200
    assert json.loads(resp.body)["message"] == "Jellyfin settings saved."


def test_saved_redirects_a_native_form_submit():
    # The no-script fallback: still saved, but land back on the page instead of
    # a raw JSON view.
    resp = _saved(_request("text/html,application/xhtml+xml"), "saved")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/settings"
