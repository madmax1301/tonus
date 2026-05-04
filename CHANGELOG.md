# Changelog

All notable changes to Tonus are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Until the first tagged release, everything below lives under `[Unreleased]`.
On a `git tag -a vX.Y.Z`, move the relevant entries into a new dated section.

---

## [Unreleased]

### Added

#### Authentication & multi-user
- JWT login flow with refresh-token rotation (HS256, Argon2id-hashed passwords)
- Personal Access Tokens (PATs) — admin-revocable, used by the Navidrome plugin and scripts
- TOTP 2FA, optional per user; server-rendered SVG QR (no Pillow dependency)
- Per-user accounts with admin separation; non-admins see a stripped-down settings panel
- Brute-force defense: five failed logins from one IP → lifetime ban (loopback exempt)
- Self-password re-verify — current password required before any change
- Onboarding wizard: empty user table → setup → 2FA → provider selection (step 2/2)
- Admin user-management UI (create / delete / promote / reset password / unban IP)

#### Provider & runtime configuration via UI
- *Settings → Verbindungen* — Spotify, Navidrome, YouTube credentials editable from the browser; values stored encrypted (Fernet) in `app_settings`
- *Settings → Defaults* — default metadata provider, audio codec, worker cooldowns; cooldown changes hot-reload (no restart)
- Spotify setup help block (5-step manual instructions, link to developer dashboard) — shown both in onboarding and in Settings → Verbindungen

#### Acquisition pipeline
- Multi-source download: Deezer (free), Spotify catalog (with API credentials), YouTube fallback
- CSV import mode with smooth progress counter and reload-resume
- URL & reverse-direct download paths (single-track, no catalog match required)
- Idempotency check for URL & reverse workers — prevents duplicate jobs on rapid clicks

#### UI / UX
- Cinematic empty states with rotating vinyl glyph (library + queue), respects `prefers-reduced-motion`
- Fly-to-queue animation when adding tracks
- ConfirmDialog refactored to cinematic glass style; TokenSheet matches the same family
- Album-detail screen with queue badge for in-flight downloads
- i18n DE/EN with live language switcher in *Settings → Sprache*

#### Infrastructure & operations
- Persistent app data on `/app/data/` — separate volume from `/app/downloads`; survives `docker compose build --no-cache`
- Migration helper from legacy job-DB locations on first boot
- `/app/downloads` is treated as ephemeral working area; auth state cannot be wiped by clearing it
- `.env` lives in repo root (no longer `backend/.env`)
- Logo + 4 README screenshots (library, onboarding, album, settings)
- `.gitignore` reorganized by topic; `test-data/`, `.venv/`, pytest outputs, log files now ignored
- GitHub Actions pipeline (`.github/workflows/build.yml`) — multi-arch (amd64+arm64) build & push to GHCR on every push to `main` and every `vX.Y.Z` git tag
- Two-channel release model: `:dev` rolls with `main` (every push triggers a fresh build), `:latest` / `:X.Y.Z` / `:X.Y` move only on semver-tags. Production stays on `:0.1` (rolling within minor) for safe auto-patches; testing uses `:dev` via a separate `docker-compose.dev.yml` (documented in README → Container images)
- `docker-compose.yml` now references `ghcr.io/madmax1301/tonus:latest` — NAS updates become `docker compose pull && up -d` instead of slow `--no-cache` rebuilds
- `.dockerignore` extended (`docs/`, `.github/`, `test-data/`, venvs) — image no longer carries Repo-Metadata or screenshots
- Issue & PR templates (`.github/ISSUE_TEMPLATE/`, `.github/pull_request_template.md`)
- MIT License + comprehensive README with logo, screenshots, ASCII architecture diagram, full `.env` table, two-layer configuration explanation

### Changed
- `.env` location: `backend/.env` → repo root `.env`
- Provider configuration: `.env` is now bootstrap-only — UI values from `Settings → Verbindungen` take priority once set
- README structure: complete rewrite with logo hero, screenshots, ASCII architecture diagram, full `.env` variable table, two-layer configuration explanation
- Onboarding layout aligned with the BLogin specification from `design_handoff_tonus_b`

### Fixed
- Setup wizard now opens after first user creation even with `TONUS_API_TOKEN` active (legacy-active path no longer blocked)
- 500 error in `/api/auth/setup` — Pillow dependency removed by switching QR rendering to SVG
- Auth DB lost on every `--no-cache` rebuild — Plan C: dedicated `/app/data` bind-mount separated from downloads
- Reverse-direct download crashed with metadata-required error
- URL & reverse jobs accidentally routed through Deezer pipeline (race condition)
- CSV import: 500 on duplicate body-stream read; schema-cleanup applied
- Album default for unknown source: `"Singles"` instead of an uploader-doubled nest
- Double download from `yt-dlp` when QR-render path was synchronous

### Security
- Password hashing: Argon2id with sane defaults (`time_cost=3`, `memory_cost=64MB`, `parallelism=4`)
- Provider credentials and TOTP secrets encrypted at rest (Fernet, key derived from per-installation seed)
- Failed login attempts logged (`login_attempts` table); auto-ban triggers from 5 fails / 24 h
- Admin endpoints require `Depends(require_admin)` in addition to `Depends(require_token)`
- Loopback IP range permanently exempt from brute-force bans (prevents Docker-internal calls from self-banning)

---

## [0.0.1] — 2026-05-01

Initial commit. Bootstrap of the Tonus project — FastAPI backend + SvelteKit frontend, basic Deezer/YouTube acquisition flow, single static `TONUS_API_TOKEN` auth.
