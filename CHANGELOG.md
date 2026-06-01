# Changelog

All notable changes to Tonus are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Until the first tagged release, everything below lives under `[Unreleased]`.
On a `git tag -a vX.Y.Z`, move the relevant entries into a new dated section.

---

## [0.4.3] — 2026-06-01

Security-Audit-Final-Cluster. Schließt die letzten drei verbliebenen Items
aus dem 2026-05-12-Audit (H-3, H-6, M-8). Audit-Status danach: **11/11
closed** (H-8 JWT/Cookie-Refactor bleibt als eigener größerer Frontend-Lift
außerhalb des Original-Clusters).

### Security

- **H-3 (High)** — Subsonic-API nutzt jetzt Token-Auth statt plaintext
  password. `?u=user&s=<random-salt>&t=md5(password+salt)` pro Request.
  Verhindert dass das Plain-Password im Reverse-Proxy-Access-Log /
  Container-stdout landet. Default `NAVIDROME_AUTH_MODE=token`. Fallback
  `plaintext` via env-var falls Subsonic-Server <1.13.0 (unwahrscheinlich,
  Navidrome supportet Token-Auth seit Anfang). Plus `_trigger_scan`
  konsolidiert auf denselben Auth-Pfad (vorher zweite HTTPBasicAuth-
  Schiene).
- **H-6 (High)** — Boot-time-Check für `TONUS_API_TOKEN`. Wenn der Token
  einen bekannten Placeholder-String enthält (`CHANGE_ME`, `replace-with-`,
  `your-token`, …) ODER kürzer als 16 Zeichen ist → **fail-fast mit
  exit 1** und Anweisung zum Fix. Vorher bootete Tonus stillschweigend
  mit einem trivial-bekannten Token. Plus `.env.example` so umgestellt
  dass der default-Marker unmittelbar verdächtig aussieht.
- **M-8 (Medium)** — CI Dependency-Audit + Dependabot. Neuer Workflow
  `.github/workflows/dep-audit.yml` läuft auf push/PR und wöchentlich
  Montag 06:00 UTC. `pip-audit` gegen `backend/requirements.txt` +
  `npm audit --audit-level=high` gegen `frontend/package-lock.json`.
  Fail bei high/critical CVEs. Plus `.github/dependabot.yml` für
  wöchentliche Bump-PRs (minor/patch gruppiert, major separate).

### New env vars

| Var | Default | Purpose |
|---|---|---|
| `NAVIDROME_AUTH_MODE` | `token` | H-3: Subsonic-Auth-Mode (`token` oder `plaintext`) |

### Migration

Keine breaking changes. Bei Bestands-Setups: nichts zu tun. Operator-
Hinweise nur falls:

- **Du hast `TONUS_API_TOKEN=replace-with-strong-random-string`** in der
  `.env`: Container failt jetzt beim Boot mit klarer Fehlermeldung —
  ersetze mit `openssl rand -hex 32` ODER lösche die Zeile (PAT-Auth
  empfohlen).
- **Subsonic-Server ist EIN nicht-Navidrome Server <1.13.0**: setze
  `NAVIDROME_AUTH_MODE=plaintext` in der `.env`.

### Audit-Closing

| Item | Status | Release |
|---|---|---|
| C-1 SSRF album_art | ✅ | v0.4.0 |
| H-1 Docker non-root | ✅ | v0.4.0 |
| H-7 X-Forwarded-For trust | ✅ | v0.4.0 |
| M-6 auth queue + health | ✅ | v0.4.0 |
| H-2 cookies_path allowlist | ✅ | v0.4.1 |
| H-5 path-traversal | ✅ | v0.4.1 |
| M-1 rate-limit bulk | ✅ | v0.4.1 |
| M-2 security-headers | ✅ | v0.4.1 |
| **H-3 Subsonic Token-Auth** | ✅ | **v0.4.3** |
| **H-6 .env.example boot-check** | ✅ | **v0.4.3** |
| **M-8 CI dep-audit** | ✅ | **v0.4.3** |
| H-8 JWT localStorage → HttpOnly | ⬜ | deferred (eigener PR) |

Plus Bonus-Bugs aus dem Audit-Sweep:
- `recovery_keys` NameError (v0.4.0)
- duplicate `download_by_url` + `tv_embedded` (v0.4.0)

---

## [0.4.2] — 2026-06-01

Cover-Art-Robustness-Release. Adressiert silent-fail-Pattern bei intermittent
CDN-Drops und liefert ein Backfill-Tool für Tracks die kein Cover bekommen
haben. Kein Security-Audit-Item — Folge-Hardening nach dem 2026-05-23 NAS-
Routing-Bug der zeitweise viele cover-Downloads geschluckt hat.

### Added

- **`backend/scripts/backfill_album_art.py`** — Operator-Tool das die Library
  nach opus-Files ohne `metadata_block_picture` durchsucht und Cover via
  MusicBrainz/Cover-Art-Archive (primary) + Deezer-API (fallback) nachlädt.
  Default `--dry-run`, idempotent, ratelimit-compliant (MB 1.1s/req).
- **YouTube-Thumbnail-Fallback** in `services.metadata._download_album_art`:
  wenn die primary cover-URL failt UND track_info eine yt-video-id enthält
  (`youtube_video_id` oder ableitbar aus `used_url`/`url`), wird
  `i.ytimg.com/vi/<id>/maxresdefault.jpg` als Fallback probiert. Nutzt die
  bestehende C-1 SSRF-Allowlist (ytimg.com via subdomain-match).

### Changed

- **`_download_album_art` mit Retry + Exponential-Backoff**: 3 Versuche
  (1s/3s/9s), Timeout auf 20s erhöht (von 10s). Catched die typischen
  ~5-15s CDN-stalls die nach dem Routing-Fix als residual-noise bleiben.
  Plus 404/410 brechen früh ab (kein retry für permanent-not-found).
- **Logging-Verbesserung**: nach 3 Failures wird die URL + Error-Class
  geprintet — vorher silent return, jetzt sichtbar im docker-log.

### New env vars

| Var | Default | Purpose |
|---|---|---|
| `COVER_DOWNLOAD_RETRIES` | `3` | Anzahl Retries pro URL |
| `COVER_DOWNLOAD_TIMEOUT_S` | `20` | Per-attempt-Timeout |
| `YT_THUMBNAIL_VARIANT` | `maxresdefault` | YT-Thumb-Variant. Auf `hqdefault` umstellen bei vielen 404 |

### Operator-Hinweis

Bestehende Library hat ~1.9% Files ohne embedded cover (Folge der
Routing-Bug-Periode). Backfill-Tool aufrufen für Cleanup:

```bash
# Dry-run first (zeigt nur was gefunden würde):
docker exec tonus python3 /app/backend/scripts/backfill_album_art.py --limit 20

# Wenn die Stats sinnvoll aussehen — full apply:
docker exec tonus python3 /app/backend/scripts/backfill_album_art.py --apply
```

Bei 19000+ tracks dauert das mehrere Stunden wegen MB-Rate-Limit (1 req/s).
Lass es im `screen`/`tmux` laufen.

---

## [0.4.1] — 2026-05-12

Security-Patch — closes 4 of 5 items from the **Bald-Cluster** of the
2026-05-12 audit. **H-3** (Subsonic plaintext password) is deferred
because it needs a live Navidrome test environment.

### Security

- **H-2 (High)** — yt-dlp `cookies_path` arbitrary-file-read blocked.
  Cookie paths must now live inside `config.YOUTUBE_COOKIES_DIR`
  (env-overridable, default `/app/data`), be regular files (no symlinks
  followed), and have a `.txt` / `.netscape` / `.cookies` extension.
  Configurable via *Settings → Verbindungen* but with hard guard-rails.
- **H-5 (High)** — Path-traversal in MP3 target paths fixed.
  `_sanitize_path` and `_sanitize_filename` now replace `..` sequences
  and strip leading dots BEFORE the char-blacklist. Plus defense-in-depth
  containment check via `Path.resolve().relative_to(library_root)` in
  `get_target_path` before `mkdir`.
- **M-1 (Medium)** — Bulk-Endpoint rate limits added: CSV-Import 20/h,
  Spotify-History 10/h, URL-Download 60/h, Track-Download 120/h. Sliding-
  window counter per `(client_ip, route)`, uses the H-7 trusted-proxy-aware
  `client_ip` helper (no Reverse-Proxy collapsing onto the proxy IP).
- **M-2 (Medium)** — Security headers middleware: `X-Content-Type-Options:
  nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy:
  strict-origin-when-cross-origin`, `Permissions-Policy`. CSP intentionally
  deferred — would break SvelteKit inline-styles without a tuning pass.

### Pending

- **H-3 (High)** — Subsonic plaintext password. Refactor to Subsonic
  Token-Auth (`?u=user&s=salt&t=md5(password+salt)`) requires a live
  Navidrome instance for verification. Tracked in the SecondBrain audit
  doc.

### New env vars

| Var | Default | Purpose |
|---|---|---|
| `YOUTUBE_COOKIES_DIR` | `/app/data` | H-2: allowed root for `YOUTUBE_COOKIES_PATH` |

---

## [0.4.0] — 2026-05-12

Security-Hardening-Release. Closes the entire **Sofort-Fix-Cluster** from the
2026-05-12 internal security audit (1 Critical, 2 High, 1 Medium) plus two
latent runtime bugs that surfaced during the same review.

### Security

- **C-1 (Critical)** — SSRF via user-controlled `album_art` URLs blocked.
  `services/metadata._download_album_art` now validates URLs against a
  configurable allowlist (`config.ALBUM_ART_ALLOWED_HOSTS`, env-overridable
  via `ALBUM_ART_ALLOWED_HOSTS`) and rejects bare IP literals plus
  redirects to non-allowlisted hosts. Defaults cover all metadata-provider
  CDNs (Spotify scdn.co, Deezer dzcdn.net, YouTube ytimg/ggpht, SoundCloud
  sndcdn.com, MusicBrainz coverartarchive.org).
- **H-1 (High)** — Container no longer runs as root. New non-login user
  `tonus` with UID/GID 1000:1000 owns `/app`. **Operator action required
  before upgrade**: `sudo chown -R 1000:1000 <host-path-für-docker_data/tonus>`
  plus the Navidrome music path. See migration notes in the GitHub release.
- **H-7 (High)** — `X-Forwarded-For` / `X-Real-IP` now only honored when
  the direct HTTP peer is inside `config.TRUSTED_PROXIES` (env-overridable
  via `TRUSTED_PROXIES`, default: loopback + RFC1918 private ranges). Closes
  the IP-ban-bypass where any caller could forge their source IP via XFF
  to escape brute-force bans or rotate ban tracking.
- **M-6 (Medium)** — `/api/queue`, `/api/queue/lanes`, `/api/queue/stats`
  now require authentication (`require_token`). `/api/health` keeps a
  monitoring-friendly public surface (`{status: "healthy"}`) but only
  exposes filesystem paths and service URLs to authenticated clients
  via the new `optional_token` dependency.

### Fixed

- **`utils/worker.py`** — `NameError` on `recovery_keys` at the end of CSV
  imports when the Recovery-Wave processed any keys. Variable was renamed
  to `initial_recovery_keys` in Phase J (v0.3.0) but the log-aggregate
  branch kept the old name and would crash post-completion.
- **`services/youtube.py`** — duplicate `download_by_url` method
  definition. Python overrode the new Phase-G (v0.2.0) multi-client
  version with the legacy `tv_embedded`-only one (yt-dlp 2026 deprecated
  that client). Dead Phase-G definition removed, `tv_embedded` replaced
  by `config.YOUTUBE_PLAYER_CLIENTS` in both remaining call sites
  (`download_by_url` + `search_and_download`).

### Changed

- Frontend TypeScript interfaces `CsvImport*` renamed to `Import*` to match
  the `csv_import_*` → `import_*` schema rename from v0.3.0. Pure cosmetic
  refactor, no runtime effect. Renamed: `CsvImportStartResponse`,
  `CsvImportStatus`, `CsvImportResult`, `CsvMatched`, `CsvUnmatched`.

### Migration notes

This release contains three breaking operational changes:

1. **Host bind-mounts must be chown'd to UID 1000**. If you mount a host
   directory into `/app/data` or `/app/downloads` (typical Docker Compose
   setup), the container can no longer write to it as root. Pre-upgrade:
   `sudo chown -R 1000:1000 <host-path-für-docker_data/tonus>` plus the
   Navidrome music path. Tonus will fail to start otherwise.
2. **`/api/queue` and its sub-endpoints now require a bearer token**. If
   external tools or monitoring scripts polled the queue API directly,
   they need an authentication token (PAT recommended). `/api/health`
   remains pollable without auth and still returns `200 OK`.
3. **Reverse-proxy IPs must be in `TRUSTED_PROXIES`**. If your reverse
   proxy sits outside the Docker bridge default (172.16.0.0/12) and your
   LAN range (192.168.0.0/16), set `TRUSTED_PROXIES` env var to the
   correct CIDR. Without it, all client IPs collapse onto the proxy IP
   (brute-force bans would lock out the proxy).

### Audit cross-reference

Full audit tracking lives at
`~/SecondBrain/10-Projects/Tonus/security-audit-2026-05-12.md`. Remaining
items (Bald-Cluster: H-2 cookies_path / H-5 path-traversal / H-3 Subsonic
auth / M-1 rate-limit / M-2 security-header) are scheduled for the next
hardening release.

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
