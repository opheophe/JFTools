"""OpenSubtitles.com REST client — search and download subtitles.

Ported from the idea behind VLSub (the VLC extension): identify a video by its
"moviehash" (first + last 64 KiB, two 32-bit sums) and download the matching
subtitles next to the video file. VLSub talked to the legacy opensubtitles.org
XML-RPC API, which is being shut down; this client talks to the modern REST
API at api.opensubtitles.com instead.

An API key (created on opensubtitles.com -> Consumers) is required. The key
alone works anonymously with a low daily quota; adding an account
username/password in the Settings raises it to the free-account quota.
"""
from __future__ import annotations

import asyncio
import os
import re

import httpx

API_ROOT = "https://api.opensubtitles.com/api/v1"

# The OpenSubtitles API rejects requests without a meaningful User-Agent
# (the default "python-httpx/…" gets a Cloudflare 403). The docs ask for
# the app name and a way to contact the developer.
USER_AGENT = "JFTools/1.0 (https://github.com/opheophe/JFTools)"

# Transient statuses worth a quick retry, same policy as the Jellyfin client.
_TRANSIENT_STATUSES = {429, 502, 503, 504}
_RETRY_ATTEMPTS = 3
_RETRY_DELAY = 0.6

# Subtitle formats we allow downloading; maps API format -> file extension.
_SUB_FORMATS = {
    "srt": ".srt",
    "vtt": ".vtt",
    "webvtt": ".vtt",
    "ass": ".ass",
    "ssa": ".ssa",
    "sub": ".sub",
    "txt": ".txt",
}


class OpenSubtitlesError(Exception):
    """Raised for configuration or upstream OpenSubtitles errors."""


def opensubtitles_hash(path: str) -> tuple[str, int]:
    """The OpenSubtitles "moviehash" for a file, plus its size in bytes.

    Reads the first 64 KiB and the last 64 KiB (for files smaller than 128 KiB
    the content is read twice, like VLSub and the C reference do), sums every
    complete 8-byte word as two 32-bit little-endian accumulators (lo seeded
    with the file size) and returns ``(f"{hi:08x}{lo:08x}", size)``.
    """
    size = os.path.getsize(path)
    with open(path, "rb") as fh:
        head = fh.read(65536)
        fh.seek(max(0, size - 65536))
        tail = fh.read(65536)
    data = head + tail
    lo = size & 0xFFFFFFFF
    hi = 0
    for i in range(0, len(data) - 7, 8):
        word = data[i:i + 8]
        lo = (lo + int.from_bytes(word[:4], "little")) & 0xFFFFFFFF
        hi = (hi + int.from_bytes(word[4:8], "little")) & 0xFFFFFFFF
    return f"{hi:08x}{lo:08x}", size


def sub_extension(sub_format: str) -> str:
    """File extension for a download format; falls back to ``.srt``."""
    return _SUB_FORMATS.get((sub_format or "").strip().lower(), ".srt")


def normalize_languages(raw: str) -> list[str]:
    """Comma/space separated language codes -> lowercase, deduplicated list."""
    out: list[str] = []
    for part in re.split(r"[\s,;]+", (raw or "").strip().lower()):
        part = re.sub(r"[^a-z0-9]", "", part)
        if not part or part in out:
            continue
        out.append(part)
    return out


def _flatten_candidates(payload: dict) -> list[dict]:
    """Turn a /subtitles search response into one dict per subtitle file."""
    out: list[dict] = []
    for item in payload.get("data") or []:
        if not isinstance(item, dict):
            continue
        a = item.get("attributes") or {}
        if not isinstance(a, dict):
            continue
        feature = a.get("feature_details") or {}
        uploader = a.get("uploader") or {}
        for f in a.get("files") or []:
            if not isinstance(f, dict):
                continue
            out.append(
                {
                    "subtitle_id": str(a.get("subtitle_id") or ""),
                    "file_id": f.get("file_id"),
                    "file_name": f.get("file_name") or "",
                    "language": (a.get("language") or "").lower(),
                    "download_count": a.get("download_count") or 0,
                    "new_download_count": a.get("new_download_count") or 0,
                    "hearing_impaired": bool(a.get("hearing_impaired")),
                    "hd": bool(a.get("hd")),
                    "ratings": a.get("ratings") or 0,
                    "votes": a.get("votes") or 0,
                    "from_trusted": bool(a.get("from_trusted")),
                    "ai_translated": bool(a.get("ai_translated")),
                    "machine_translated": bool(a.get("machine_translated")),
                    "moviehash_match": a.get("moviehash_match"),
                    "upload_date": (a.get("upload_date") or "")[:10],
                    "release": a.get("release") or "",
                    "uploader": uploader.get("name") or "",
                    "title": feature.get("movie_name") or feature.get("title") or "",
                    "year": feature.get("year"),
                }
            )
    return out


def _error_message(resp: httpx.Response, fallback: str) -> str:
    """Extract the API's ``message`` field when present, else the fallback."""
    try:
        data = resp.json()
        if isinstance(data, dict) and data.get("message"):
            return str(data["message"])
    except (ValueError, TypeError):
        pass
    return fallback


class OpenSubtitlesClient:
    """Async client for api.opensubtitles.com (REST v1)."""

    def __init__(
        self,
        api_key: str,
        username: str = "",
        password: str = "",
        timeout: float = 20.0,
        transport: httpx.AsyncBaseTransport | None = None,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self.api_key = (api_key or "").strip()
        self.username = (username or "").strip()
        self.password = password or ""
        self.timeout = timeout
        self._transport = transport
        self._http = http
        self._token = ""
        self._user: dict = {}

    # ---- plumbing ------------------------------------------------------------

    def _use(self) -> tuple[httpx.AsyncClient, bool]:
        if self._http is not None:
            return self._http, False
        return httpx.AsyncClient(timeout=self.timeout, transport=self._transport), True

    def _headers(self) -> dict[str, str]:
        headers = {
            "Api-Key": self.api_key,
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _check(self) -> None:
        if not self.api_key:
            raise OpenSubtitlesError(
                "OpenSubtitles is not configured. Add an API key on the Settings page."
            )

    def _transport_error(self, exc: httpx.HTTPError) -> OpenSubtitlesError:
        if isinstance(exc, httpx.ConnectError):
            return OpenSubtitlesError(
                "Could not connect to api.opensubtitles.com. Check the network."
            )
        if isinstance(exc, httpx.TimeoutException):
            return OpenSubtitlesError("OpenSubtitles request timed out.")
        return OpenSubtitlesError(f"OpenSubtitles request failed: {exc}")

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict | None = None,
        params: dict | None = None,
    ) -> httpx.Response:
        self._check()
        client, close = self._use()
        try:
            for attempt in range(_RETRY_ATTEMPTS):
                try:
                    resp = await client.request(
                        method,
                        API_ROOT + path,
                        headers=self._headers(),
                        json=json,
                        params=params,
                    )
                except httpx.HTTPError as exc:
                    raise self._transport_error(exc)
                if resp.status_code in _TRANSIENT_STATUSES and attempt < _RETRY_ATTEMPTS - 1:
                    await asyncio.sleep(_RETRY_DELAY * (attempt + 1))
                    continue
                return resp
        finally:
            if close:
                await client.aclose()
        raise OpenSubtitlesError(f"OpenSubtitles returned HTTP 503 for {path}.")

    def _raise_for_status(self, resp: httpx.Response, what: str) -> None:
        if resp.status_code == 401:
            raise OpenSubtitlesError(
                _error_message(
                    resp,
                    "OpenSubtitles rejected the request — check the API key and login.",
                )
            )
        if resp.status_code == 403:
            raise OpenSubtitlesError(
                "OpenSubtitles rejected the request (HTTP 403). This usually means "
                "an invalid API key or the request being blocked — check the key "
                "on opensubtitles.com → Consumers."
            )
        if resp.status_code == 406:
            raise OpenSubtitlesError(_error_message(resp, "OpenSubtitles refused the request."))
        if resp.status_code >= 400:
            raise OpenSubtitlesError(
                f"OpenSubtitles returned HTTP {resp.status_code} for {what}."
            )

    # ---- API calls -----------------------------------------------------------

    @property
    def logged_in(self) -> bool:
        return bool(self._token)

    @property
    def user(self) -> dict:
        return self._user

    def remaining(self) -> int | None:
        """Downloads still available today, or None when not logged in."""
        if not self._user:
            return None
        allowed = self._user.get("allowed_downloads")
        used = self._user.get("downloads_count")
        if isinstance(allowed, int) and isinstance(used, int):
            return max(0, allowed - used)
        return None

    async def login(self) -> dict:
        """Log in with the stored credentials; returns the user dict.

        Without credentials the client stays anonymous (lower quota). An
        anonymous "login" just records that nothing was attempted.
        """
        if not self.username:
            self._token = ""
            self._user = {}
            return {}
        resp = await self._request(
            "POST",
            "/login",
            json={"username": self.username, "password": self.password},
        )
        if resp.status_code == 200:
            try:
                data = resp.json()
            except (ValueError, TypeError):
                raise OpenSubtitlesError("OpenSubtitles login failed: bad response.")
            self._token = str(data.get("token") or "")
            self._user = data.get("user") or {}
            if not self._token:
                raise OpenSubtitlesError("OpenSubtitles login failed.")
            return self._user
        if resp.status_code == 401:
            raise OpenSubtitlesError(
                "OpenSubtitles login failed — check the username and password."
            )
        self._raise_for_status(resp, "login")
        raise OpenSubtitlesError("OpenSubtitles login failed.")

    async def _ensure_login(self) -> None:
        """Log in once (credentials only); re-login if the token was rejected."""
        if not self.username:
            return
        if not self._token:
            await self.login()

    async def test(self) -> dict:
        """Cheap probe: /infos/languages when anonymous, else a real login."""
        if self.username:
            return await self.login()
        resp = await self._request("GET", "/infos/languages")
        if resp.status_code == 200:
            return {}
        self._raise_for_status(resp, "API key test")
        raise OpenSubtitlesError("OpenSubtitles API key test failed.")

    async def search(self, **params) -> dict:
        """Search subtitles; returns ``{"total_count", "data": [candidate...]}``.

        Supported params: ``languages`` (list), ``moviehash``, ``query``,
        ``type``, ``season_number``, ``episode_number``, ``imdb_id``, ``year``.
        """
        query: dict = {}
        for key, value in params.items():
            if value in (None, "", []):
                continue
            if key == "languages" and isinstance(value, (list, tuple)):
                query["languages"] = ",".join(value)
            else:
                query[key] = value
        if not query:
            raise OpenSubtitlesError("No search terms supplied.")
        retried_login = False
        while True:
            resp = await self._request("GET", "/subtitles", params=query)
            if resp.status_code == 200:
                try:
                    data = resp.json()
                except (ValueError, TypeError):
                    raise OpenSubtitlesError("OpenSubtitles search failed: bad response.")
                return {
                    "total_count": int(data.get("total_count") or 0),
                    "data": _flatten_candidates(data),
                }
            if resp.status_code == 401 and not retried_login and self.username:
                retried_login = True
                await self.login()
                continue
            self._raise_for_status(resp, "search")
            raise OpenSubtitlesError("OpenSubtitles search failed.")

    async def download(self, file_id, sub_format: str = "srt") -> dict:
        """Request a subtitle file; returns
        ``{"content": bytes, "file_name": str, "remaining": int|None}``."""
        fmt = (sub_format or "srt").strip().lower()
        if fmt not in _SUB_FORMATS:
            raise OpenSubtitlesError(f"Unsupported subtitle format: {sub_format}")
        await self._ensure_login()
        retried_login = False
        while True:
            resp = await self._request(
                "POST",
                "/download",
                json={"file_id": int(file_id), "sub_format": fmt},
            )
            if resp.status_code == 200:
                try:
                    data = resp.json()
                except (ValueError, TypeError):
                    raise OpenSubtitlesError("OpenSubtitles download failed: bad response.")
                link = str(data.get("link") or "").strip()
                if not link:
                    raise OpenSubtitlesError("OpenSubtitles returned no download link.")
                content, filename = await self._fetch_link(link)
                remaining = data.get("remaining")
                if not isinstance(remaining, int):
                    remaining = self.remaining()
                return {
                    "content": content,
                    "file_name": str(data.get("file_name") or filename),
                    "remaining": remaining,
                }
            if resp.status_code == 401 and not retried_login and self.username:
                retried_login = True
                await self.login()
                continue
            self._raise_for_status(resp, "download")
            raise OpenSubtitlesError("OpenSubtitles download failed.")

    async def _fetch_link(self, link: str) -> tuple[bytes, str]:
        client, close = self._use()
        try:
            resp = await client.get(link, headers={"User-Agent": USER_AGENT})
        except httpx.HTTPError as exc:
            raise self._transport_error(exc)
        finally:
            if close:
                await client.aclose()
        if resp.status_code != 200:
            raise OpenSubtitlesError(
                f"Subtitle file download failed (HTTP {resp.status_code})."
            )
        content = resp.content
        if not content:
            raise OpenSubtitlesError("Subtitle file download was empty.")
        name = ""
        disp = resp.headers.get("content-disposition") or ""
        m = re.search(r"filename=\"?([^\";]+)", disp)
        if m:
            name = m.group(1)
        return content, name


# ---- shared client ----------------------------------------------------------
# One process-wide client (and HTTP connection pool) rebuilt when the
# OpenSubtitles settings change, mirroring the shared Jellyfin client.

_shared_http: httpx.AsyncClient | None = None
_shared_client: "OpenSubtitlesClient | None" = None
_shared_key: tuple[str, str, str] | None = None


def get_opensubtitles() -> "OpenSubtitlesClient":
    global _shared_http, _shared_client, _shared_key
    from .config import config

    key = (
        config.settings.opensubtitles_api_key,
        config.settings.opensubtitles_username,
        config.settings.opensubtitles_password,
    )
    if _shared_client is None or _shared_key != key:
        if _shared_http is None:
            _shared_http = httpx.AsyncClient(timeout=20.0)
        _shared_client = OpenSubtitlesClient(key[0], key[1], key[2], http=_shared_http)
        _shared_key = key
    return _shared_client


async def close_shared_client() -> None:
    global _shared_http, _shared_client, _shared_key
    if _shared_http is not None:
        await _shared_http.aclose()
    _shared_http = None
    _shared_client = None
    _shared_key = None


def invalidate() -> None:
    """Forget the cached token/user (e.g. after settings change).

    The shared HTTP client keeps its connection pool; only the client object
    (which holds the token and credentials) is dropped.
    """
    global _shared_client, _shared_key
    _shared_client = None
    _shared_key = None
