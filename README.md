# MedServ

A self-hosted web dashboard that sits next to your media stack and drives
external services over HTTP APIs. It ships with a **Jellyfin** module and a
small plugin framework so new modules (subtitle audit, renaming, duplicates,
notifications, ...) can be added as single files.

Jellyfin itself is **not** part of this repository — MedServ only talks to your
existing server over its API.

## Features

- Web UI (`http://<host>:8099`) with module cards and action buttons.
- Session-based login with failed-login lockout.
- Settings page to change the Jellyfin connection **and** the dashboard login at
  runtime (persisted to a volume; the password is stored as a hash).
- Jellyfin actions: test connection, list libraries, recently added, library
  stats, active sessions.
- Docker image (non-root) + compose file, with a `/healthz` healthcheck.

## Configuration

Copy `.env.example` to `.env` and set real values. The Jellyfin URL/API key in
`.env` are only defaults — changes made on the Settings page are persisted to
`$MEDSERV_DATA_DIR/config.json` and take precedence.

| Variable | Description |
| --- | --- |
| `JELLYFIN_URL` | Jellyfin API root. The web-player `/web` suffix is stripped automatically. |
| `JELLYFIN_API_KEY` | Created in Jellyfin → Dashboard → API Keys. |
| `MEDSERV_USERNAME` / `MEDSERV_PASSWORD` | Dashboard login. Initial defaults; changeable on the Settings page (password stored hashed). |
| `MEDSERV_SECRET_KEY` | Signs session cookies. Auto-generated and persisted if unset. |
| `MEDSERV_COOKIE_SECURE` | Set `true` when serving over HTTPS. |
| `MEDSERV_DATA_DIR` | Where settings/state are persisted (`/data` in Docker). |
| `MEDSERV_PORT` | Web port (default `8099`). |

> **Security notes**
> - The Jellyfin API key grants broad access. Keep it out of the repo (`.env` is
>   gitignored) and treat it as a secret.
> - If you use a public DuckDNS host, serve it over `https://` (reverse proxy +
>   Let's Encrypt). Plain `http://` sends the API key in cleartext.
> - The dashboard itself is LAN-only by default. For remote access, put it
>   behind Tailscale/WireGuard or a TLS reverse proxy with `MEDSERV_COOKIE_SECURE=true`.

## Run locally (development)

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate    Linux/macOS: source .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload
```

Open http://localhost:8099, log in, then set your Jellyfin URL/API key on the
Settings page (or prefill `.env`).

## Run with Docker

```bash
cp .env.example .env   # then edit .env
docker compose up -d --build
```

The image builds on the host machine, so it matches that machine's
architecture (x86_64 or ARM). This is the recommended approach for a NAS where
you build on-device.

### TerraMaster

TerraMaster TOS ships **Docker Manager** (and SSH is available). Two options:

1. **Docker Manager → Project / Compose** (TOS 5/6): import the
   `docker-compose.yml` from this folder. It will read `.env` and build the
   image on the NAS.
2. **SSH**: copy the repo to the NAS, then:
   ```bash
   cd MedServ
   cp .env.example .env && nano .env
   docker compose up -d --build
   ```

Confirm the architecture with `uname -m` (`x86_64` or `aarch64`); building
on-device avoids cross-architecture image issues. Then open
`http://<nas-ip>:8099`.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

## Adding a module

Create a file in `app/modules/`, register a `Module` with `Action`s, and import
it from `app/modules/__init__.py`. The dashboard picks it up automatically.

```python
from .base import Module, Action, register_module, message

async def hello(ctx):
    return message("Hello world", title="Greeting")

register_module(Module(
    id="example",
    name="Example",
    description="An example module.",
    actions=[Action("hello", "Say hello", "Returns a greeting.", hello)],
))
```
