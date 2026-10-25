# JFTools

A self-hosted web dashboard that sits next to your media stack and drives
Jellyfin over its HTTP API.

Jellyfin itself is **not** part of this repository — JFTools only talks to your
existing server over the API.

## Features

- Web UI with pages for server activity, Jellyfin users, libraries and Movie
  Match (comparing on-disk files to Jellyfin's metadata).
- File browser: browse the media folders from Movie Match/Search's
  "Open → File browser" (or `/files`). Browsing is confined to the mapped
  media folders — anything else, including symlink escapes, is refused.
- File management (rename and delete) behind a Settings toggle, **off by
  default**. Deletion is permanent (with a confirmation dialog), every change
  is logged, mutations require a same-origin CSRF header, and after a change
  you can trigger a Jellyfin library rescan with one click.
- First-run setup: on first launch you choose your own local login (username +
  password) — no default credentials. The local password works on your local
  network, and any Jellyfin admin account works from anywhere. Failed-login
  lockout included.
- Settings page to change the Jellyfin connection, the dashboard login and your
  preferred subtitle languages at runtime (persisted to a volume; the password
  is stored as a scrypt hash).
- Backup: export/import settings as JSON, or download and restore a full ZIP of
  settings and runtime data (scan results, Movie Match marks, schedules), with
  scheduled backups and retention.
- Jellyfin connection test, library listing, recently added, library stats,
  active sessions and scheduled scans. **Latest additions** names each item's
  library and carries the same action pills as the other tabs (Open, Sync, Play,
  Subfind, Subtitles, Tracks, Google).
- Subfind: the **Subfind** pill opens the subtitle search with a live player on
  the right half. Downloading a candidate makes the player start it at once
  (later downloads switch the subtitle without restarting the video), and the
  **Sync** and **Delete** buttons act on whichever subtitle is playing in the
  player right now — a success chime plays when they finish. Embedded streams
  are left to the Tracks dialog.
- Tracks: the **Audio** column lists every embedded audio track, a
  filter can match scan state or track count plus a case-insensitive track-name
  regex, and the **Tracks** pill opens a dialog for the audio tracks, the
  embedded subtitles and the external subtitle files — mark several rows for
  deletion with row buttons, choose one
  default of each type, or let **Smart mark** pick every subtitle outside your
  preferred languages plus every forced track, then execute it in a single
  in-place rewrite.
- Player: the **Play** action on a row streams a library file in a small popup
  window — seek, speed, jump to a time — with any subtitle track attached
  (embedded streams extracted, external files converted to WebVTT). Files whose
  audio the browser cannot decode (DTS, TrueHD, often AC-3) get a fast
  audio-only AAC conversion played alongside the muted video instead of a
  full remux; the slow remux is reserved for files whose video itself won't
  play.
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
| `JFTOOLS_OPENSUBTITLES_LANGUAGES` | Preferred subtitle languages, e.g. `en,sv` (default `en`). Used by OpenSubtitles searches and the Tracks dialog's Smart mark. |
| `PUID` / `PGID` | Container user/group that owns the data files. Set in `docker-compose.yml`. |
| `JFTOOLS_HOST` / `JFTOOLS_PORT` | Bind address/port for `run.bat` locally; the container always listens on `8099`. |

## Data & permissions

All runtime state lives in the data directory (`/data` in the container):
`config.json` (Jellyfin connection + login hash), `.secret_key`, `schedules.json`,
`users.json`, `libraries.json`, `jellyfin_id.json`, `scans.json`, `logs/`, the
disposable `play_cache/` and, once backups run, `backups/`. An old `subtitles.json`
is imported automatically the first time the app starts with `scans.json` missing;
it is left on disk as a backup.

- **Bind mount:** point the `volumes` path at a NAS folder. The container starts
  as root only to align its internal user with `PUID`/`PGID` and `chown` the data
  folder, then drops privileges with `gosu` before running the app. Set
  `PUID`/`PGID` to the folder's owner (`id -u` / `id -g` over SSH) to avoid
  permission errors.
- **Named volume:** if you prefer, replace the bind mount with
  `jftools-data:/data` and keep `PUID`/`PGID` at `1000`.
- **Single process:** JFTools keeps state in memory (scheduler, caches), so run
  **one container, one uvicorn worker**. Do not scale replicas.

## Backup & restore

Settings → **Backup** offers two levels:

- **Export settings (JSON)** — configuration only (Jellyfin connection, login
  hash, integrations, path maps, filter profiles, backup settings). Portable
  between installs.
- **Download full backup** — a ZIP with everything needed to rebuild this
  instance: `config.json`, `.secret_key`, `schedules.json`, `libraries.json`,
  `users.json`, `jellyfin_id.json` (Movie Match marks and favourites) and
  `scans.json` (subtitle/audio probe results). `play_cache/` is always excluded;
  logs are optional. Roughly 20 MB raw, a few MB zipped.

Each archive carries a `manifest.json` with the app version and a SHA-256 per
file. **Restore from backup** validates the manifest, refuses while a scan or
fetch is running, and replaces those files after every checksum passes (each
write is atomic). Restart the container afterwards — configuration, schedules
and caches load at startup, and settings writes are refused until then. The
restore also replaces the login and session key, so you may be signed out.

### Automatic backups

**Settings → Automatic backups** sets the folder, how many archives to keep
(`0` keeps all) and whether logs are included. Timing is a **Backup** job on the
Scheduler page (daily 03:30, disabled by default). Each run writes
`jftools-backup-YYYYMMDD-HHMMSS.zip`; archives beyond the keep limit are deleted,
and only files matching that name pattern are touched.

The default folder is `data/backups` inside the data volume — convenient, but it
shares that volume's fate. For a copy on another disk, mount one and point the
folder at it:

```yaml
volumes:
  - /Volume1/public/Docker/Configs/JFTools:/data
  - /Volume1/public/Docker/Backups:/backups
```

Then set the backup folder to `/backups`.

Archives contain the Jellyfin API key, login hash and OpenSubtitles password, so
treat them like the settings export: keep them private (they are written with
mode `0600`).

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

## Media scan

JFTools can probe every media file with **ffprobe** and list its subtitle and
audio tracks — embedded streams plus external `.srt`/`.ass`/… files next to the
video. One probe fills both, cached in `scans.json` and shown in the **Search**
tab: a **Subtitles** column with a **Not scanned** state, a **Subtitles**
filter, a manual **Subtitle Ok** mark/filter, an **Audio** column and an
**Audio** filter. An entry's `scanned` flag means both halves have been probed;
it is set to `true` only when the result is definitive (tracks found or none),
so errors leave it `false` and the next scan retries.

Jellyfin's **series** items are show folders, not media: Movie Match does not
fetch them, because the episodes are rows of their own. If a directory path
still reaches the scan (from an older cache, say), it is recorded as a folder
and both columns show `—` instead of retrying it forever.

A file that genuinely cannot be probed keeps its flag off and is retried by the
next scan; the columns name the reason (**Not reachable** when the path is
missing, **Probe failed** for a damaged file, with ffprobe's own message in the
tooltip). The scan's log line counts those separately, so "complete" never
hides them.

Two scheduler jobs do the work:

- **Media scan (full)** — marks every file unscanned, then re-probes them all.
- **Media scan** — syncs the store with the library (adds new files as
  unscanned, drops removed ones), re-checks the external subtitle files of the
  files already scanned (one directory listing per folder, no ffprobe) and
  re-probes any whose sidecars appeared, changed or went away, then probes every
  file not yet scanned. That is how a `.srt` dropped in by another tool gets
  picked up without a full scan.

The **Media scan** button on the Search page starts the incremental job. The
check compares the sidecar subtitle files' names and classification, not their
contents, so editing an existing `.srt` in place is not detected as a change —
use a row's **Sync → Subtitles & audio** for that.

You can also re-probe a single file from a row's **Sync** menu: **Subtitles &
audio** re-runs ffprobe for both columns (ignoring the `scanned` flag — handy
after dropping a new `.srt` next to a movie), while **Jellyfin library** asks
Jellyfin to rescan the path. Clicking the **Sync** pill itself runs both.

**Requirements:**

- **Media paths must be set** (see above) so JFTools can open the real files.
- **ffprobe** must be available:
  - **Docker:** installed automatically (the image includes `ffmpeg`).
  - **Windows:** drop `ffprobe.exe` into a `Tools/` folder next to the app, or
    set `JFTOOLS_FFPROBE` to its path.

The Search tab's **Subtitles** filter is grouped into four sections, and each
one can cover the whole library when all of its options are selected:

- **Language** — **Preferred languages** (the Settings → Subtitle preferences),
  **Foreign languages**, **Unknown** (including items not scanned yet or whose
  probe failed), and **No subtitles** (a scanned file with no tracks).
- **Acceptable** — **Acceptable** means a normal subtitle in a preferred
  language, AI or not; **Acceptable AI** is the AI-generated subset; **Not
  acceptable** is everything else.
- **Quality** — **Good subtitle** is any normal subtitle in any language, AI or
  not, excluding forced and tiny files. **Forced subtitles** and **Tiny
  subtitles** call out those special kinds; **No subtitles**, **Not scanned**
  and **Failed probe** cover the remaining states.
- **Scan** — **Not scanned**, **Failed probe** and **Scanned**. Series folders
  count as **Not scanned** because there is no media file to probe; their
  episodes are separate rows.

The same value can appear in more than one section (for example **No
subtitles** under Language and Quality); the checkboxes stay in sync. Matching
uses the preferred languages from Settings, not a fixed English/Swedish list.

The **Subtitle regex** box next to the Subtitles filter is case-insensitive and
searches the labels in the Subtitles column plus the underlying track names and
external file names. `forced|srt$` finds forced and `.srt` tracks; `ext` finds
external subtitles. It is combined with the selected subtitle filters.

## Tracks (audio and subtitles)

The **Audio** column lists each file's embedded audio tracks as
`English (E-AC-3 5.1), Swedish (AAC stereo)`; a trailing `*` marks the default
track. The **Audio** filter offers **No track**, **Single track**,
**Multi track**, **Not scanned** (not probed yet) and **Probe failed** (the path
was unreachable, not a file, or ffprobe failed — the column names the exact
reason). The **Audio regex** box next to the Audio filter matches the track
names, case-insensitively (`swe|nor`, `5\.1`, …).

The **Tracks** pill on a row opens the track dialog. It lists the audio
tracks, the embedded subtitle tracks and the external subtitle files. Toggle
any number of rows with **Mark delete** / **Unmark**, use **Set default** to
choose at most one audio and one subtitle default, then press **Execute** — a
success chime plays when it finishes.
The track list scrolls on its own, so **Smart mark** and **Execute** stay
visible no matter how many tracks a file has. **Smart mark** replaces the
subtitle marks with every track or file outside your preferred languages
(Settings → **Subtitle preferences**). Matching is by language, not exact
label, so with English preferred, `English SDH`, `En SDH` and `En` all stay
unmarked; French, `fr`, Czech and similar tracks are marked. A track whose
language tag is missing is read from its title — `Unknown (Català)` is
Catalan, `Unknown (Euskara)` Basque, `Unknown (Norsk Bokmål)` Norwegian, and
the same for Galego, ಕನ್ನಡ, മലയാളം, Bahasa Melayu, தமிழ் and తెలుగు.
**Forced** tracks are marked too, including English forced, because a full
subtitle track normally makes the forced one redundant. Tracks whose language
is **Unknown** are left unmarked unless they are forced, and Smart mark never
changes audio marks. Leaving the preferred list empty defaults to English.

Embedded changes are one ffmpeg rewrite — every other stream is copied as-is —
into a temporary file in the same folder, which replaces the original only if
ffmpeg succeeds; marked external files are then deleted from disk. A new
default replaces the old one; if the default track is deleted without a
replacement, the first remaining track of that type is promoted. The dialog
refuses to delete every audio track, and asks Jellyfin to rescan the file
afterwards.

The dialog needs **File management** enabled in the settings, and free space
equal to the file size when a rewrite is needed. The dialog also flags
commentary audio tracks.

## Subtitles

Missing subtitles can be fetched straight from the table: the **Subtitles**
action on a row (Movie Match and Search tabs). It identifies the video the same
way VLSub does — by its OpenSubtitles "moviehash" (first + last 64 KiB) — and
falls back to a name search when the hash finds nothing. Pick a candidate and
JFTools saves `<video-name>.<language>.srt` next to the video.

The **Subfind** pill is the same dialog with the right column replaced by a
live player: a downloaded subtitle starts playing immediately, later downloads
switch the subtitle without restarting the video, and the **Sync**,
**Delete** and **Rename** buttons under the **Now playing** header act on
whichever subtitle is playing at that moment (a success chime plays when they
finish). **Rename** lists the preferred languages and renames the playing
subtitle to `<video>.<language>.srt`, the same shape the downloader writes.

The dialog's **Search Hash**, **Search Filename** and **Search Jellyfin name**
buttons re-run the search one way instead of the default, for when the automatic
one comes up short: the hash alone (an exact match for that release), the release
name on disk (`Onslaught 2026 1080p WEB H264-YTS`), or the Jellyfin item's name
with its year. The line above the results says which search is on screen. The
dialog is split into two independently scrolling columns: **OpenSubtitles
results** on the left and the tracks already on the video on the right, so a
long result list cannot push the track actions out of view.

The dialog lists **every subtitle track** on the video — the embedded tracks
first (read-only here; use the **Tracks** dialog to delete or make one
default) and then the external files. Each external row has a **Delete** button
(Jellyfin is told the file changed right away) and a **Sync** button that
re-aligns that subtitle to the video with
[ffsubsync](https://github.com/smacke/ffsubsync) — the original is only replaced
once the sync succeeds.

Closing the dialog (clicking outside it, or Escape) re-scans the file with
ffprobe and asks Jellyfin to refresh that path, so the Subtitles column and
Jellyfin's own view update. It only does that when something was downloaded or
deleted; a sync needs no rescan since the filename and language are unchanged.

A download or delete marks the item as **Not scanned** until that close-time
rescan (or the **Media scan** button / scheduled scan) re-probes it. A manual
"Subtitle Ok" mark is cleared when the subtitle files change.

**Requirements:**

- **OpenSubtitles API key** — Settings → OpenSubtitles (create a free consumer
  key at opensubtitles.com). The key alone works anonymously (~5 downloads/day);
  adding your account username/password raises it to the free-account quota
  (~20/day). The dialog shows how many downloads remain.
- **ffsubsync** for the per-subtitle **Sync** button — the Docker image ships
  it (`pip` install). For a native Windows install, drop `ffsubsync.exe` in a
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

The **Play** action on a row (Movie Match, Search and Libraries tabs) opens the
file in a small player window. It streams the original video straight from disk — the
server supports HTTP range requests, so the native timeline seeks normally — and
the row of controls adds a time readout, −30/−5/+5/+30 second jumps, a **Go to**
box (`mm:ss` or `hh:mm:ss`), a speed selector and a **Subtitles** selector.

Every subtitle track the scanner knows about is in that selector: embedded
streams are extracted with ffmpeg, external `.srt`/`.vtt`/`.ass`/`.ssa`/`.sub`
files are converted to WebVTT first. Bitmap subtitles (PGS/DVD, including
VobSub `.sub` files) are listed but marked unavailable, since a browser cannot
render them. **The best track is
already selected when the window opens** — the external file in one of the
default subtitle languages if there is one, otherwise the first usable track —
so subtitles are visible without touching the selector; choose **Off** to hide
them. The same player opens from the subtitle dialog: every track row has a
**Play** button that
starts the file with that track already selected. One player window is reused,
so clicking Play again just re-loads it.

**The player draws the cues itself** rather than leaving them to the browser's
caption renderer, which sizes and places them on its own (and is not always on
at all) — the text appears in a legible bar along the bottom of the video. The
status line also reports what loaded, e.g. `2 cues · English — movie.en.srt`, or
warns that a track holds no cues, so a file with nothing to show says so instead
of looking identical to a player with subtitles switched off. Turning captions
on from the browser's own menu still works: the built-in renderer takes over and
the player's bar steps aside.

Playback starts on its own. Browsers block unmuted autoplay on origins that
have not earned it yet, so the player retries a few times and, if playback is
still refused, shows a **▶** button over the video (and says why in the status
line) — one click starts it.

Playback is read-only, so it needs no File management toggle — only a login.
Paths are resolved through **Media paths** and must land inside the browsable
media folders, exactly like the file browser.

**A browser can only decode what it supports.** A remuxed MKV with DTS or
TrueHD audio plays with picture and *no sound at all* — the video element does
not even fire an error for it. The player therefore probes the file's first
audio stream when it opens, and when the codec is one no browser decodes it
says so and converts **only the audio**: ffmpeg writes the audio track to a
small AAC `.m4a` in `play_cache/` in the data folder (a feature film's track is
a few hundred MB, and the conversion takes a fraction of a full remux because
no video is copied). The player then plays the original video — muted — and the
converted track next to it, keeping them aligned through seeks, speed changes
and a drift check. If the conversion has already been made, the player starts
with it directly. The status line shows which codec is at fault and how far the
conversion is; the first one takes a minute or two for a feature film; later
plays reuse it. The cache is pruned at every startup and before each new
conversion: files older than the configured age go first, then the oldest
files until the folder is under the configured size cap — the newest file is
always kept. The defaults are **7 days / 5 GB** and both are adjustable on the
Settings page (**Player cache**).

ffprobe cannot answer for every codec: AC-3 and E-AC-3 are decoded through the
operating system in some browsers and are silent in others. The player asks the
browser whether it can take AC-3 at all; when it cannot, the audio conversion
starts immediately. When the browser claims support, the player watches whether
audio is actually being decoded — a few seconds of silence with an audio track
present starts the same conversion. The **Fix audio** button stays available
for AC-3/E-AC-3 so a viewer who hears nothing can start it by hand, including in
browsers that expose no decoded-audio counter. Nothing switches until the
conversion is finished — the video keeps playing meanwhile (silently).

A full **browser copy** is only made when the *video itself* will not play —
the container or video codec is something the browser cannot open. Then the
**Make browser copy** button remuxes the file into a browser-friendly MP4
(video copied untouched, audio converted to AAC) and the player switches to it,
keeping the position. This is the slow path, and it is no longer used for audio
problems.

**Requirements:** ffmpeg (bundled in the Docker image, or in `Tools/` next to
the app) for embedded subtitle extraction, audio conversion and browser
copies. Without it the player still streams files the browser can decode, but
the embedded tracks report that they cannot be converted.

## User watching

A **User watching** scheduler job (every 30 s, enabled by default) checks
Jellyfin's live sessions and drives a status dot next to the **Users** nav link:

- **green** — someone is watching
- **orange** — a session is paused
- **red** — nobody watching

The **Currently watching** column on the Users page shows what is playing, and
for episodes it includes the season and episode (`Show — S02E14 — Episode
title`).

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
