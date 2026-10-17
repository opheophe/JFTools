# JFTools

A self-hosted web dashboard that sits next to your media stack and drives
Jellyfin over its HTTP API.

Jellyfin itself is **not** part of this repository — JFTools only talks to your
existing server over the API.

## Features

- Web UI with pages for server activity, Jellyfin users, libraries and Movie
  Match (comparing on-disk files to Jellyfin's metadata).
- First-run setup: on first launch you choose your own local login (username +
  password) — no default credentials. The local password works on your local
  network, and any Jellyfin admin account works from anywhere. Failed-login
  lockout included.
- Settings page to change the Jellyfin connection **and** the dashboard login at
  runtime (persisted to a volume; the password is stored as a scrypt hash).
- Backup: export all settings to a JSON file and import them back on another
  instance.
- Jellyfin connection test, library listing, recently added, library stats,
  active sessions and scheduled scans.
- Single small container image, published to GitHub Container Registry for both
  `linux/amd64` and `linux/arm64`.
- `/healthz` healthcheck.

## Quick start (Docker)

The image is built and published to **ghcr.io/opheophe/jftools** by GitHub
Actions. Pulling it needs no login.

```bash
git clone https://github.com/opheophe/JFTools.git
cd JFTools
docker compose pull
docker compose up -d
```

Open `http://<host>:8099` and follow the first-run setup, then set your Jellyfin
URL and API key on the Settings page.

The included `docker-compose.yml` is intentionally minimal — everything else is
configured in-app:

```yaml
services:
  jftools:
    image: ghcr.io/opheophe/jftools:latest
    container_name: jftools
    restart: unless-stopped
    environment:
      PUID: 1000     # owner of the data files (change to your NAS folder owner)
      PGID: 1000
    ports:
      - "8099:8099"
    volumes:
      - /path/to/jftools/data:/data   # change to your NAS data folder
```

Pin a specific version instead of `latest` (e.g. `ghcr.io/opheophe/jftools:1.2.3`)
for reproducible deployments. The container starts as root only to align its
internal user with `PUID:PGID` and `chown` the data folder, then drops privileges
with `gosu` before running the app. See **Data & permissions**.

### Tags

| Tag | Meaning |
| --- | --- |
| `latest` | Newest tagged release |
| `X.Y.Z`, `X.Y` | A specific release |
| `edge` | Latest build from `main` (for testing) |
| `sha-xxxxxxx` | A specific commit |

Pin a version in compose (`image: ghcr.io/opheophe/jftools:1.2.3`) if you prefer
reproducible deployments.

## TerraMaster

TerraMaster TOS ships **Docker Manager** and SSH access.

1. Copy `docker-compose.yml` onto the NAS.
2. Edit the `volumes` path to a real NAS folder, e.g. `/Volume1/public/Docker/Configs/JFTools`,
   and set `PUID`/`PGID` to the owner of that folder (`id -u` / `id -g` over SSH).
3. In **Docker Manager → Project/Compose**, import `docker-compose.yml`, or over
   SSH run:
   ```bash
   docker compose pull && docker compose up -d
   ```
4. Open `http://<nas-ip>:8099`.

To build the image on the NAS instead of pulling it:

```bash
docker compose -f docker-compose.build.yml up -d --build
```

## Configuration

In Docker, configure everything on the **Settings page** in the web UI; values
are persisted to the data directory and take precedence. You can also prefill
any of the variables below by adding them to `environment:` in the compose file.

For **local development** (no Docker), copy `.env.example` to `.env` to prefill
values.

| Variable | Description |
| --- | --- |
| `JELLYFIN_URL` | Jellyfin API root. A trailing `/web` is stripped automatically. |
| `JELLYFIN_API_KEY` | Created in Jellyfin → Dashboard → API Keys. |
| `JFTOOLS_USERNAME` / `JFTOOLS_PASSWORD` | Optional. If both are set, first-run setup is skipped and these become the local login (LAN/localhost only); changeable later on the Settings page (stored hashed). Jellyfin admin accounts can always log in. |
| `JFTOOLS_SECRET_KEY` | Signs session cookies. Auto-generated and persisted to the data dir if unset. |
| `JFTOOLS_COOKIE_SECURE` | Set `true` when serving over HTTPS. |
| `JFTOOLS_DATA_DIR` | Where settings/state are persisted (`/data` in Docker). |
| `JFTOOLS_RADARR_URL` / `JFTOOLS_SONARR_URL` | Default Radarr/Sonarr URLs for Movie Match's "Open" links. |
| `JFTOOLS_TIMEZONE` / `TZ` | Time zone for logs and scheduled jobs (default `Europe/Stockholm`). |
| `PUID` / `PGID` | Container user/group that owns the data files. Set in `docker-compose.yml`. |
| `JFTOOLS_HOST` / `JFTOOLS_PORT` | Bind address/port for `run.bat` locally; the container always listens on `8099`. |

## Data & permissions

All runtime state lives in the data directory (`/data` in the container):
`config.json` (Jellyfin connection + login hash), `.secret_key`, `schedules.json`,
`users.json`, `libraries.json`, `jellyfin_id.json`, and `logs/`.

- **Bind mount:** point the `volumes` path at a NAS folder. The container starts
  as root only to align its internal user with `PUID`/`PGID` and `chown` the data
  folder, then drops privileges with `gosu` before running the app. Set
  `PUID`/`PGID` to the folder's owner (`id -u` / `id -g` over SSH) to avoid
  permission errors.
- **Named volume:** if you prefer, replace the bind mount with
  `jftools-data:/data` and keep `PUID`/`PGID` at `1000`.
- **Single process:** JFTools keeps state in memory (scheduler, caches), so run
  **one container, one uvicorn worker**. Do not scale replicas.

## Updating

```bash
docker compose pull
docker compose up -d
```

Migrating from an older image? Your data is preserved because it lives in the
mounted data folder, not in the container.

## Development

Local development does not need Docker.

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate    Linux/macOS: source .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload
```

On Windows you can also just run `run.bat`, which creates the venv, installs
dependencies and starts the server.

Open `http://localhost:8099`, create your local login on first launch, then set
your Jellyfin URL/API key on the Settings page (or prefill `.env`).

### Tests

```bash
pytest
```

Continuous integration (`.github/workflows/ci.yml`) runs the same suite on every
push to `main` and on every pull request.

## Releases

Images are built and published by `.github/workflows/docker-publish.yml`:

- **Stable release** — run the release script, which tests, tags, pushes and
  creates the GitHub release (requires an authenticated [`gh`](https://cli.github.com/)):
  ```powershell
  .\release.ps1 1.0.0        # Windows
  ```
  ```bash
  ./release.sh 1.0.0         # Linux/macOS
  ```
  Pushing tag `v1.0.0` publishes `1.0.0`, `1.0` and `latest`.
- **Edge build** — every push to `main` publishes `edge`.
- You can also trigger a build manually from the Actions tab
  (`workflow_dispatch`).

## Project layout

```
app/
  main.py            FastAPI app, pages, settings + Jellyfin routes
  config.py          environment + persisted settings
  auth.py            local login, lockout, local-IP checks
  media.py           shared item/image/rescan routes + helpers
  users.py           Users page + watch activity
  libraries.py       Libraries page + recent additions
  jellyfin_id.py     Movie Match (on-disk vs Jellyfin)
  logs.py            per-tab log files
  scheduler.py       scheduled operations (APScheduler)
  jellyfin/client.py shared Jellyfin HTTP client
  templates/         Jinja2 templates
  static/            CSS + vanilla JS
tests/               pytest suite
Dockerfile           container image
docker-compose.yml   pull prebuilt image (NAS)
docker-compose.build.yml  build locally
docker-entrypoint.sh PUID/PGID handling
```

## Security notes

- The Jellyfin API key grants broad access. Keep it out of the repo (`.env` is
  gitignored) and treat it as a secret. No secrets are committed to this
  repository — runtime state (including the API key and login hash) lives only in
  the mounted data directory.
- The Settings page's **Export settings** file contains your Jellyfin API key and
  login hash. Treat it as a secret.
- If you expose the dashboard publicly, serve it over `https://` (reverse proxy +
  TLS) and set `JFTOOLS_COOKIE_SECURE=true`.
- The dashboard is LAN-only by default. For remote access, prefer
  Tailscale/WireGuard or a TLS reverse proxy.

## License

[MIT](LICENSE)
