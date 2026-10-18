# JFTools

A self-hosted web dashboard that sits next to your media stack and drives
Jellyfin over its HTTP API.

Jellyfin itself is **not** part of this repository — JFTools only talks to your
existing server over the API.

## Features

- Web UI with pages for server activity, Jellyfin users, libraries and Movie
  Match (comparing on-disk files to Jellyfin's metadata).
- File browser: browse the media folders from Movie Match/Search's
  "Actions → Browse files" (or `/files`). Browsing is confined to the mapped
  media folders — anything else, including symlink escapes, is refused.
- File management (rename and delete) behind a Settings toggle, **off by
  default**. Deletion is permanent (with a confirmation dialog), every change
  is logged, mutations require a same-origin CSRF header, and after a change
  you can trigger a Jellyfin library rescan with one click.
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
- Player: **Actions → Play** streams a library file in a small popup window —
  seek, speed, jump to a time — with any subtitle track attached (embedded
  streams extracted, external files converted to WebVTT), and a one-click
  ffmpeg remux for files the browser cannot decode.
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
| `JFTOOLS_PATH_MAP` | Optional. Semicolon-separated `from=to` path mappings, e.g. `/Filmer=\\NAS\Filmer;/Ero=Z:\Ero`. Maps Jellyfin's container paths to paths this machine can open. Also editable in-app (Settings → Media paths). |
| `JFTOOLS_OPENSUBTITLES_API_KEY` | Optional. OpenSubtitles API key (opensubtitles.com → Consumers) used by subtitle search/download. Also editable in-app. |
| `JFTOOLS_OPENSUBTITLES_USERNAME` / `JFTOOLS_OPENSUBTITLES_PASSWORD` | Optional. OpenSubtitles account login — raises the daily download quota. |
| `JFTOOLS_OPENSUBTITLES_LANGUAGES` | Languages searched by default, e.g. `en,sv` (default `en,sv`). |
| `PUID` / `PGID` | Container user/group that owns the data files. Set in `docker-compose.yml`. |
| `JFTOOLS_HOST` / `JFTOOLS_PORT` | Bind address/port for `run.bat` locally; the container always listens on `8099`. |

## Data & permissions

All runtime state lives in the data directory (`/data` in the container):
`config.json` (Jellyfin connection + login hash), `.secret_key`, `schedules.json`,
`users.json`, `libraries.json`, `jellyfin_id.json`, `subtitles.json`, and `logs/`.

- **Bind mount:** point the `volumes` path at a NAS folder. The container starts
  as root only to align its internal user with `PUID`/`PGID` and `chown` the data
  folder, then drops privileges with `gosu` before running the app. Set
  `PUID`/`PGID` to the folder's owner (`id -u` / `id -g` over SSH) to avoid
  permission errors.
- **Named volume:** if you prefer, replace the bind mount with
  `jftools-data:/data` and keep `PUID`/`PGID` at `1000`.
- **Single process:** JFTools keeps state in memory (scheduler, caches), so run
  **one container, one uvicorn worker**. Do not scale replicas.

## Media paths

By default JFTools only talks to the Jellyfin **API** and never touches your
media files. To let it read (and write) the real files — for example to run
`ffmpeg` — it needs to be able to find them, because Jellyfin reports each
item's path from *inside its own container* (e.g. `/Filmer/Movie/movie.mkv`).

There are two situations:

**1. JFTools and Jellyfin mount the same folders (Docker).** If the JFTools
container mounts the *same* `source:`/`target:` pairs Jellyfin uses, the paths
already resolve as-is — **leave Media paths empty**. Example `docker-compose.yml`:

```yaml
services:
  jftools:
    volumes:
      - /path/to/jftools/data:/data            # existing
      - /Volume2/Media/Filmer:/Filmer          # same target Jellyfin uses
      - /Volume2/Media/Serier:/Serier
      # append :ro to mount read-only, e.g.  - /Volume2/Media/Filmer:/Filmer:ro
```

**2. JFTools runs elsewhere (e.g. Windows, or with different mounts).** Add a
mapping per library on the **Settings → Media paths** page: the **Jellyfin path**
on the left, and the **path on this machine** on the right.

```
/Filmer  →  \\NAS\Filmer     (or Z:\Filmer)
```

Click **Fill with library paths** to add your libraries (both sides pre-filled
with the Jellyfin path); then edit the right-hand path where it differs and
**Save**. The same mappings can be supplied headless via `JFTOOLS_PATH_MAP`
(`/Filmer=\\NAS\Filmer;/Ero=Z:\Ero`).

Notes:
- **Permissions:** the app runs as `PUID:PGID` in Docker (or your user when run
  locally); those credentials need read (and, for writes, write) access to the
  mapped folders.
- **Spaces / special characters** in a path: quote it, or use the long form in
  compose (`type: bind` / `source:` / `target:`).
- **Verify:** use the **Test** button on a mapping, or in Docker
  `docker exec jftools ls "/Filmer"`.

## Subtitle scan

JFTools can probe every media file with **ffprobe** and list its subtitle
tracks — embedded streams plus external `.srt`/`.ass`/… files next to the video.
Results are cached to `subtitles.json` and shown in the **Search** tab (a
**Subtitles** column with a **Not scanned** state, a **Subtitles** filter, and a
manual **Subtitle Ok** mark/filter). Each file carries a `scanned` flag:
probing sets it to `true` only when the result is definitive (subs found or
none); errors leave it `false` and are retried.

Two scheduler jobs do the work:

- **Subtitle scan (full)** — marks every file unscanned, then re-probes them all.
- **Subtitle scan** — syncs the store with the library (adds new files as
  unscanned, drops removed ones), then probes every file not yet scanned.

You can also re-probe a single file from its hover popup (**Rescan sub**),
which ignores the `scanned` flag — handy after dropping a new `.srt` next to a
movie.

**Requirements:**

- **Media paths must be set** (see above) so JFTools can open the real files.
- **ffprobe** must be available:
  - **Docker:** installed automatically (the image includes `ffmpeg`).
  - **Windows:** drop `ffprobe.exe` into a `Tools/` folder next to the app, or
    set `JFTOOLS_FFPROBE` to its path.

Classification matches Subber's Movie List: `acceptable` (English/Swedish/
Unknown), `AI Sub`, `Forced`, `Foreign`, `No subs`.

## Subtitles

Missing subtitles can be fetched straight from the table: **Actions → Search
subs** (Movie Match and Search tabs). It identifies the video the same way
VLSub does — by its OpenSubtitles "moviehash" (first + last 64 KiB) — and falls
back to a name search when the hash finds nothing. Pick a candidate and JFTools
saves `<video-name>.<language>.srt` next to the video.

The dialog lists **every subtitle track** on the video — the embedded tracks
first (buttons greyed out, since they cannot be changed) and then the external
files. Each external row has a **Delete** button (Jellyfin is told the file
changed right away) and a **Sync** button that re-aligns that subtitle to the
video with [ffsubsync](https://github.com/smacke/ffsubsync) — the original is
only replaced once the sync succeeds.

Closing the dialog (clicking outside it, or Escape) re-scans the file with
ffprobe and asks Jellyfin to refresh that path, so the Subtitles column and
Jellyfin's own view update. It only does that when something was downloaded or
deleted; a sync needs no rescan since the filename and language are unchanged.

A download or delete marks the item as **Not scanned** until that close-time
rescan (or the **Subtitle scan** button / scheduled scan) re-probes it. A manual
"Subtitle Ok" mark is cleared when the subtitle files change.

**Requirements:**

- **OpenSubtitles API key** — Settings → OpenSubtitles (create a free consumer
  key at opensubtitles.com). The key alone works anonymously (~5 downloads/day);
  adding your account username/password raises it to the free-account quota
  (~20/day). The dialog shows how many downloads remain.
- **ffsubsync** for the per-subtitle **Sync** button — drop `ffsubsync.exe` in a
  `Tools` folder next to the app, put it on PATH, or set `JFTOOLS_FFSUBSYNC`.
  (ffmpeg/ffprobe are found the same way.) Without it the button reports that
  ffsubsync is missing.
- **Media paths** (above) so JFTools can open the video and write next to it.
- **File management** enabled on the Settings page — subtitle downloads and
  deletions write into the media folders and are gated behind the same toggle as
  rename/delete. Existing subtitle files are never overwritten or deleted
  without confirmation.

Downloads talk to the modern OpenSubtitles.com REST API (`api.opensubtitles.com`),
not the legacy XML-RPC API that VLSub used (that one is being shut down).

## Player

**Actions → Play** (Movie Match and Search tabs) opens the file in a small
player window. It streams the original video straight from disk — the server
supports HTTP range requests, so the native timeline seeks normally — and the
row of controls adds a time readout, −30/−5/+5/+30 second jumps, a **Go to**
box (`mm:ss` or `hh:mm:ss`), a speed selector and a **Subtitles** selector.

Every subtitle track the scanner knows about is in that selector: embedded
streams are extracted with ffmpeg, external `.srt`/`.vtt`/`.ass`/`.ssa` files
are converted to WebVTT first. Embedded bitmap subtitles (PGS/DVD) are listed
but marked unavailable, since a browser cannot render them. The same player
opens from the subtitle dialog: every track row has a **Play** button that
starts the file with that track already selected. One player window is reused,
so clicking Play again just re-loads it.

Playback is read-only, so it needs no File management toggle — only a login.
Paths are resolved through **Media paths** and must land inside the browsable
media folders, exactly like the file browser.

**A browser can only decode what it supports.** A remuxed MKV with AC3/DTS
audio, for example, plays silently (or not at all). When that happens the
player offers **Make browser copy**: ffmpeg remuxes the file into
`play_cache/` in the data folder — video stream copied untouched, audio
converted to AAC — and the player switches to it, keeping the position. The
first copy takes a minute or two for a feature film; later plays reuse it. The
cache is pruned at startup (older than a week, and oldest-first above ~5 GB).

**Requirements:** ffmpeg (bundled in the Docker image, or in `Tools/` next to
the app) for embedded subtitle extraction and browser copies. Without it the
player still streams files the browser can decode, but the embedded tracks
report that they cannot be converted.

## User watching

A **User watching** scheduler job (every 30 s, enabled by default) checks
Jellyfin's live sessions and drives a status dot next to the **Users** nav link:

- **green** — someone is watching
- **orange** — a session is paused
- **red** — nobody watching

Disable the job in the Scheduler to hide the dot entirely.

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
