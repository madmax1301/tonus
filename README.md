# Tonus

Self-hosted music search, download and library tool. Pairs with Navidrome via a custom plugin.

- **Backend:** FastAPI + SQLite job-store, async download workers
- **Frontend:** SvelteKit + Tailwind v4 (Apple-Music-/Glass design language)
- **Plugin:** separate Go-Repo `tonus-navidrome-plugin`

## Stack

```
backend/   FastAPI app, services for Deezer/Spotify/YouTube/Navidrome
frontend/  SvelteKit (adapter-static), built into backend/frontend/build/
```

## Develop locally

```bash
# Frontend (Vite dev with API proxy to :8088)
cd frontend && npm install && npm run dev

# Backend (separately, in another shell)
cd backend && python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app:app --reload --port 8088
```

## Build & run via Docker

```bash
# First time: bind-mount target must exist before compose up
mkdir -p downloads

docker compose up --build -d
# → http://<host>:8088
```

The base `docker-compose.yml` uses `network_mode: host` for dual-VPN source-IP binding (NAS production). On macOS Docker Desktop, `docker-compose.override.yml` swaps that to bridge mode with port mapping.

### Hosts without dual-VPN (dev VMs, generic Linux)

If `VPN_SOURCE_A`/`VPN_SOURCE_B` aren't bindable on the current host, the boot-time check in `app.py:_verify_vpn_source_bindings` aborts with `errno 99: Cannot assign requested address`. Disable splitting in those environments:

```yaml
environment:
  - VPN_SPLIT_ENABLED=false
```

## Configuration

Copy `backend/env.example` → `backend/.env` and fill in:
- `NAVIDROME_API_URL`, `NAVIDROME_USER`, `NAVIDROME_PASS`
- `VPN_SOURCE_A`, `VPN_SOURCE_B` (only when running on NAS with dual-VPN)
- Deezer / Spotify credentials per the example file
- `TONUS_API_TOKEN` — **deprecated**; only set during the plugin-PAT migration window (see below)

## Authentication

Tonus uses **JWT login** for the browser and **PATs (Personal Access Tokens)** for service-to-service auth (Navidrome plugin, scripts).

### First boot

Open the UI → first-run setup wizard creates your admin account. From then on, every browser tab uses the JWT-cookie pair. 2FA can be enabled at any time under Settings → Sicherheit.

### Plugin/service auth

Settings → API-Tokens → *Neuen Token anlegen* (name + expiry). The plain token is shown **once** — copy it directly into the plugin config (`tonus_token` field). Revoke any time via the same screen.

### Migration from `TONUS_API_TOKEN`

The legacy static-token path stays available for backward compatibility, but is deprecated:

1. **Open the Tonus UI** — even with `TONUS_API_TOKEN` set, the onboarding wizard opens as long as the user table is empty. Create your admin account through the wizard (set a strong password, enable 2FA if you want). Plugin auth via the legacy token continues to work in parallel.
2. Configure your Navidrome plugin to use a **PAT** instead of `TONUS_API_TOKEN` (Settings → API-Tokens). Restart the plugin and verify queue jobs come through with the user-tagged origin.
3. Remove `TONUS_API_TOKEN` from `backend/.env` and restart Tonus. The legacy path is now closed; all calls require JWT or PAT.

Rollback: revoke the PAT, set `TONUS_API_TOKEN` back, restart. The static path resumes.

## License

Released under the [MIT License](LICENSE) — © 2026 madmax1301.
