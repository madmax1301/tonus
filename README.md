<p align="center">
  <img src="frontend/static/repo-icon.png" alt="Tonus" width="520" />
</p>

<p align="center">
  <strong>Self-hosted music acquisition. Pairs with Navidrome.</strong><br/>
  Search across Deezer, Spotify and YouTube — drop the result straight into your library.
</p>

<p align="center">
  <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/License-MIT-c8a96a.svg" /></a>
  <img alt="Stack: FastAPI" src="https://img.shields.io/badge/backend-FastAPI-009688.svg" />
  <img alt="Stack: SvelteKit" src="https://img.shields.io/badge/frontend-SvelteKit-ff3e00.svg" />
  <img alt="Container: Docker" src="https://img.shields.io/badge/run-Docker-2496ed.svg" />
</p>

---

## What is Tonus

Tonus is the **acquisition half** of a self-hosted music setup. You search, you queue, Tonus downloads and lands the file in your Navidrome library folder. Navidrome itself stays the **player** — Tonus does not stream, transcode, or expose music to clients. Think of it as the "buy/grab" workflow that sits next to your existing Navidrome instance.

It's a single-tenant tool you self-host on a NAS (or any Docker host). All credentials and audio stay on your machine — no external service calls beyond the metadata APIs you opt into.

## Screenshots

| | |
|---|---|
| ![Library view](docs/screenshots/library.png) | ![Onboarding wizard](docs/screenshots/onboarding.png) |
| **Library** — search across providers, download a single track or a full album with one click | **Onboarding** — pick your providers on first boot; everything else is configured later in *Settings → Verbindungen* |
| ![Album detail](docs/screenshots/album.png) | ![Settings panel](docs/screenshots/settings.png) |
| **Album view** — full track list with queue badge for in-flight downloads | **Settings** — runtime configuration: defaults, providers, users, API tokens, brute-force log |

## Features

- **Multi-provider search** — Deezer (free, no key) or Spotify (catalog) for metadata; audio stream falls back across Deezer ↔ YouTube as needed
- **First-run onboarding wizard** — admin account, optional 2FA (TOTP), provider connections, all in the browser
- **Per-user accounts with admin separation** — non-admins see a stripped-down settings panel; admins manage users, tokens, providers, bans
- **Personal Access Tokens (PATs)** — for the Navidrome plugin and scripts; revocable, scoped to your account
- **Brute-force protection** — five failed logins from the same IP triggers a lifetime ban (admin can unban from *Settings → Brute-force*)
- **Hot-reloadable defaults** — change worker cooldowns, default provider, audio codec from the UI without a container restart
- **Dual-VPN source-IP splitting** (optional, NAS-mode) — bind alternating download threads to two network interfaces for higher throughput on rate-limited APIs
- **Persistent state on a separate volume** — auth DB and queue jobs live in `/app/data/`; clearing the audio download cache cannot wipe your users
- **Navidrome plugin available separately** — [`tonus-navidrome-plugin`](https://github.com/madmax1301/tonus-navidrome-plugin) (Go), authenticates with a PAT

## Quick Start

### Prerequisites

- Docker and `docker compose` v2
- A Navidrome instance (or any music library folder Tonus can write to)
- ~1 GB free disk for in-flight downloads (cleared on each completed job)

### One-time setup on the host

```bash
# Get the code
git clone https://github.com/madmax1301/tonus.git
cd tonus

# Local data folders next to the compose file (matches default volume paths)
mkdir -p tonus-data downloads

# Point the music volume at your Navidrome library
$EDITOR docker-compose.yml   # replace /path/to/music with your actual library path

# Configure
cp .env.example .env
$EDITOR .env   # at minimum: NAVIDROME_* + a strong TONUS_API_TOKEN
```

On a Synology NAS where absolute paths are nicer, either edit `docker-compose.yml` directly to point at `/volume1/docker/...` or drop a `docker-compose.override.yml` next to it that only overrides the `volumes:` section.

### Boot

```bash
docker compose up -d
# UI: http://<host>:8088
```

The shipped `docker-compose.yml` pulls a prebuilt image from [GitHub Container Registry](https://ghcr.io/madmax1301/tonus). To build from a local checkout instead, see [Building from source](#building-from-source) below.

On first open the **setup wizard** creates your admin account and walks you through 2FA + provider connections. After that everything is configurable from *Settings*.

### macOS / dev hosts (no dual-VPN)

`docker-compose.override.yml` ships with the repo and is auto-merged by `docker compose`. It swaps `network_mode: host` for bridge + port mapping and points volumes at `./test-{music,downloads,data}` so a fresh `git clone` runs on macOS Docker Desktop without changes. **On the NAS, do not commit a local override** — the base file is what's used in production.

## First-Run Onboarding

1. **Setup wizard** — empty user table → setup form → admin account
2. **2FA wizard** — server-rendered SVG QR for any TOTP app (Authy, 1Password, Aegis…); recovery code shown once
3. **Onboarding step 2/2** — pick the providers you want connected; expandable help blocks explain credential setup (Spotify Dashboard, Navidrome admin, etc.)
4. **Settings → Verbindungen** later on — change provider credentials, swap defaults, configure cooldowns

If `TONUS_API_TOKEN` is set in `.env`, the legacy static-token plugin path remains active in parallel until you migrate the plugin to a PAT. See *Authentication → Migration* below.

## Architecture

```
┌─────────── Browser ───────────┐  ┌─────── Navidrome plugin ──────┐
│  SvelteKit (adapter-static)   │  │  Go binary, PAT auth          │
└──────────────┬────────────────┘  └──────────────┬────────────────┘
               │  JWT cookies                     │  Bearer PAT
               ▼                                  ▼
┌──────────────────────────────────────────────────────────────────┐
│                    FastAPI (uvicorn)                             │
│  ┌─────────────┐ ┌─────────────┐ ┌──────────────┐ ┌───────────┐  │
│  │ Auth/User   │ │ Job-Queue   │ │ Workers      │ │ Settings  │  │
│  │ JWT + Argon2│ │ SQLite WAL  │ │ asyncio +    │ │ DB-backed │  │
│  │ TOTP + bans │ │ idempotent  │ │ source-IP    │ │ overrides │  │
│  └─────────────┘ └─────────────┘ └──────────────┘ └───────────┘  │
└──────────────────────────────────────────────────────────────────┘
               │
               ├─── /app/data/jobs.db        (auth + queue + settings, persistent)
               ├─── /app/downloads/          (in-flight audio, ephemeral)
               └─── /music/                  (Navidrome library, final destination)
```

**Why two volumes?** `/app/downloads` is a working area for in-flight files — clearing it during cleanup must not destroy your users or queue history. `/app/data` is the durable store. Bind-mount both separately.

The plugin lives in its own repo: [`tonus-navidrome-plugin`](https://github.com/madmax1301/tonus-navidrome-plugin).

## Authentication

Tonus has three auth paths, each meant for a specific consumer:

| Path | For | Lifetime | Where |
|---|---|---|---|
| **JWT** | Browser sessions | Refresh-rotated, ~30 d | Login page, cookie-bound |
| **PAT** | Navidrome plugin, scripts, curl | User-defined (no expiry possible) | *Settings → API-Tokens* |
| **TOTP** | Optional 2nd factor on top of password | Per-user toggle | *Settings → Sicherheit* |

### Brute-force defense

Five failed logins from the same IP within 24 h → lifetime ban. The ban list lives in `/app/data/jobs.db` (table `banned_ips`) and is checked **before** every authenticated request. The host's loopback range is permanently exempt so localhost/Docker-internal calls cannot self-ban. Admins inspect and unban from *Settings → Brute-force*.

### Migration from `TONUS_API_TOKEN`

The legacy static-token path stays available but is deprecated:

1. With `TONUS_API_TOKEN` set, the onboarding wizard still opens as long as the user table is empty. Create your admin account through the wizard.
2. Configure the Navidrome plugin to use a **PAT** (*Settings → API-Tokens*) instead of `TONUS_API_TOKEN`. Restart the plugin; queue jobs now arrive tagged with your user.
3. Remove `TONUS_API_TOKEN` from `.env` and restart Tonus. The legacy path is closed; all calls require JWT or PAT.

Rollback: revoke the PAT, restore `TONUS_API_TOKEN`, restart.

## Configuration

### Two configuration layers

Tonus reads configuration from two sources, in this priority:

1. **`/app/data/jobs.db` → `app_settings` table** (highest) — what you set via *Settings → Verbindungen / Defaults* in the UI
2. **`.env` file** — bootstrap values, used when the DB has no override

This means once you've configured a provider in the UI, the `.env` value for it is **ignored**. You can keep `.env` for first-boot bootstrap and then forget it.

### `.env` variables

| Variable | Default | Required | Purpose |
|---|---|---|---|
| **Metadata providers** | | | |
| `DEFAULT_METADATA_PROVIDER` | `deezer` | no | `deezer` (free, no key) or `spotify`. Overridable in *Settings → Defaults*. |
| `SPOTIFY_CLIENT_ID` | — | only with `spotify` | Spotify Web API Client ID. Get one at [developer.spotify.com](https://developer.spotify.com/dashboard). |
| `SPOTIFY_CLIENT_SECRET` | — | only with `spotify` | Paired secret. Tonus uses Client-Credentials flow — no user OAuth. |
| `SPOTIFY_REDIRECT_URI` | `http://localhost:8000/callback` | no | Reserved for future user-OAuth flow; not used by current builds. |
| **Navidrome integration** | | | |
| `NAVIDROME_MUSIC_PATH` | `/music` (in container) | yes | Final destination for downloaded tracks. Inside the container; bind-mounted from your Navidrome library. |
| `NAVIDROME_MUSIC_PATHS` | — | no | Comma- or newline-separated list of multiple library paths. Each appears as "Download to" in the UI. |
| `NAVIDROME_MUSIC_LABELS` | — | no | Display labels parallel to `NAVIDROME_MUSIC_PATHS` (e.g. `Library A,Library B`). |
| `NAVIDROME_API_URL` | `http://localhost:4533` | yes (for scan) | Navidrome HTTP endpoint. Tonus posts a scan trigger after each completed download. |
| `NAVIDROME_USERNAME` | `admin` | yes | Navidrome admin user (needed for scan API). |
| `NAVIDROME_PASSWORD` | — | yes | Paired password. Stored encrypted at rest once moved into the DB layer via the UI. |
| `NAVIDROME_SYNC_ENABLED` | `true` | no | Periodic library scan: walk `NAVIDROME_MUSIC_PATH`, mark catalog tracks already present. |
| `NAVIDROME_SYNC_INTERVAL_HOURS` | `4` | no | Hours between background sync passes. |
| `NAVIDROME_SYNC_INITIAL_DELAY_SEC` | `120` | no | Wait at boot before the first sync, so the worker doesn't fight cold-start I/O. |
| `NAVIDROME_SYNC_API_DELAY_SEC` | `0.12` | no | Throttle between Navidrome API calls during sync. |
| **Storage paths** | | | |
| `JOBS_DB_PATH` | `/app/data/jobs.db` | no | Auth DB, job queue, app settings, banned IPs. Override only for non-Docker setups. |
| `DOWNLOAD_DIR` | `./downloads` (`/app/downloads` in container) | no | In-flight working area for audio files. **Separate from `JOBS_DB_PATH`** — clearing this folder must not wipe auth state. |
| **Audio output** | | | |
| `OUTPUT_FORMAT` | `mp3` | no | Container/codec for the final file. |
| `AUDIO_QUALITY` | `128` | no | Bitrate in kbps. `128` is a good size/quality balance. |
| `TEMP_FILE_CLEANUP_DELAY_SEC` | `60` | no | Browser temp-file lifetime; avoids 404s on duplicate GETs. |
| **YouTube** | | | |
| `YOUTUBE_COOKIES_PATH` | — | no | Netscape-format cookies for `yt-dlp` when YouTube triggers bot detection. Export with a browser extension or `yt-dlp --cookies-from-browser`. |
| **API server** | | | |
| `API_HOST` | `0.0.0.0` | no | Bind address for uvicorn. |
| `API_PORT` | `8000` (`8088` in Docker) | no | TCP port. The shipped `docker-compose.yml` forces `8088`. |
| `CORS_ORIGINS` | `http://localhost:3000,http://127.0.0.1:3000` | no | Comma-separated list of allowed origins for the dev frontend. |
| **Authentication (legacy)** | | | |
| `TONUS_API_TOKEN` | — | no | **Deprecated.** Static plugin token; set only during PAT migration. Remove once the plugin uses a PAT. |
| **Network / VPN** | | | |
| `VPN_SPLIT_ENABLED` | `true` | no | Disable for hosts without two bindable interfaces. |
| `VPN_SOURCE_A` | `192.168.1.200` | yes if `VPN_SPLIT_ENABLED=true` | Source-IP for download lane A. Must be locally bindable, otherwise boot aborts. |
| `VPN_SOURCE_B` | `192.168.1.201` | yes if `VPN_SPLIT_ENABLED=true` | Source-IP for download lane B. |

### Sensitive values — encryption at rest

Once you save provider credentials through *Settings → Verbindungen*, Tonus stores them in `app_settings` encrypted with **Fernet** (AES-128-CBC + HMAC-SHA256). The encryption key is derived from a per-installation seed in `/app/data/jobs.db`. You can clear-text-edit `.env` but everything that goes through the UI is encrypted.

### TOTP secrets

User TOTP secrets are stored encrypted with the same Fernet key. Losing `/app/data/jobs.db` means losing 2FA enrollments — back this volume up if you care about recovering accounts.

## Development

### Local dev (no Docker)

```bash
# Backend
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app:app --reload --port 8088

# Frontend (separate shell — Vite dev server proxies API to :8088)
cd frontend
npm install
npm run dev
```

### Tests

```bash
# Python
cd backend && python -m pytest

# Svelte (typecheck + svelte-check)
cd frontend && npm run check
```

### Building from source

The shipped `docker-compose.yml` only references the GHCR image (no `build:` directive). To build a local image — useful when you've forked the repo and want to test changes before pushing:

```bash
# Build and tag with the same name docker-compose expects
docker build -t ghcr.io/madmax1301/tonus:latest .

# Now `docker compose up -d` uses the local image instead of pulling
docker compose up -d
```

The Dockerfile is multi-stage: SvelteKit is built in stage 1 (`node:20-alpine`) and copied into the runtime image (`python:3.11-slim`). No separate frontend deploy needed.

## Updates

```bash
cd /opt/GitHub/tonus
git pull                        # only needed if compose file or .env changed
docker compose pull             # fetch the new image from GHCR
docker compose up -d            # restart with the new image
```

`/app/data/jobs.db` lives on a host bind-mount (the `./tonus-data` folder by default) and survives image swaps. **Do not skip the `mkdir tonus-data downloads` step on first install** — without those bind-mount targets, the DB dies on every container recreation.

## Container images

Images are built and published to GHCR by [`.github/workflows/build.yml`](.github/workflows/build.yml) on every push to `main` and on every `vX.Y.Z` git tag. Multi-arch: `linux/amd64` + `linux/arm64`.

| Tag | Stable? | Points to |
|---|---|---|
| `:latest` | rolling | Most recent commit on `main` |
| `:main` | rolling | Same as `:latest` |
| `:0.1.0` | yes | A specific tagged release (immutable) |
| `:0.1` | rolling within minor | Latest patch in the `0.1.x` series |
| `:sha-abc1234` | yes | A specific commit (immutable) |

### Pin to a release

For a NAS running production, prefer a fixed minor:

```yaml
services:
  tonus:
    image: ghcr.io/madmax1301/tonus:0.1
```

You'll get every patch release automatically (`0.1.0` → `0.1.1` → `0.1.2`) but stay locked out of `0.2.x` until you opt in. Cutting a new release is a `git tag -a v0.1.1 -m "..." && git push origin v0.1.1` away.

## License

Released under the [MIT License](LICENSE) — © 2026 madmax1301.
