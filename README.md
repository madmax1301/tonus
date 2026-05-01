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
- `TONUS_API_TOKEN` — Bearer token for `/api/*`
- `NAVIDROME_API_URL`, `NAVIDROME_USER`, `NAVIDROME_PASS`
- `VPN_SOURCE_A`, `VPN_SOURCE_B` (only when running on NAS with dual-VPN)
- Deezer / Spotify credentials per the example file
