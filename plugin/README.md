# tonus-navidrome-plugin

A [Navidrome](https://www.navidrome.org/) plugin that triggers the [tonus](https://github.com/madmax1301/tonus) FastAPI backend on a schedule for **per-user music discovery + auto-download** — driven by each user's ListenBrainz history, surfaced as personal Subsonic playlists owned by their Navidrome account.

**Status:** v0.3.0 — multi-user playlists are live. Single-schedule, daily by default. Multi-schedule + listen-tracking are on the v2 roadmap.

## What it does

- Registers a recurring cron in Navidrome (default `0 7 * * *` — every day at 07:00 local time).
- For each Navidrome user you map to a ListenBrainz account, on each cron tick:
  1. Asks the tonus backend to discover "tracks you'd probably like" from that user's LB top-artists (Deezer artist-radio, filtered against their listening history) and queue them for download.
  2. Reconciles already-finished tracks into a Subsonic playlist **owned by that Navidrome user**, named per template (default `Discovery {date}`, also supports `{user}`).
- Idempotent — repeated cron ticks merge new finished tracks into the same dated playlist without creating duplicates. Tracks that take days to finish trickle into the playlist as they become available.

## What it does NOT do

- The plugin does not download anything itself — that's tonus's job. The plugin is a thin trigger + Subsonic-playlist scheduler.
- It does not show a live queue inside Navidrome — plugin UI is limited to the settings page. Use tonus's own web UI for live status (it has a status card under Settings).
- It is single-schedule. Want different schedules for discovery vs cleanup vs maintenance? That's v2.

## Architecture (one paragraph)

Plugin is a **thin trigger**: TaskQueue with concurrency 1 receives one trigger-task and one reconcile-task per Navidrome user per cron tick. The trigger-task fires `POST /api/plugin/sync` at tonus (fire-and-forget, returns in ms). The reconcile-task pulls `GET /api/plugin/finished-tracks` for the user and pushes results into a Subsonic playlist via `host.SubsonicAPICall("/rest/createPlaylist?u=USERNAME")` — Navidrome injects the user's auth automatically because the plugin has the `users` permission. The tonus backend resolves Subsonic track IDs server-side (admin-auth `search3`, cached in payload) so the plugin reconcile stays under the 30 s plugin-callback timeout.

## Requirements

- **Navidrome ≥ 0.60** (plugin system, Extism-based).
- A reachable tonus backend exposing `/api/plugin/health`, `/api/plugin/sync`, `/api/plugin/sync-status`, `/api/plugin/finished-tracks`, plus the existing `/api/import/csv*` endpoints.
- For build: [TinyGo](https://tinygo.org/getting-started/install/) `≥ 0.40` and [Go](https://golang.org/dl/) `≥ 1.23`. The Makefile falls back to plain `go` if TinyGo is missing, but the resulting WASM is much larger.

## Install

### Option 1 — Pre-built release (recommended)

Grab the latest `tonus-navidrome-plugin.ndp` from the [GitHub Releases](https://github.com/madmax1301/tonus-navidrome-plugin/releases) page. Each release is built by the `release.yml` GitHub Action whenever a `v*.*.*` tag is pushed.

### Option 2 — Build from source

```bash
git clone https://github.com/madmax1301/tonus-navidrome-plugin.git
cd tonus-navidrome-plugin
make package    # → tonus-navidrome-plugin.ndp
```

### Install into Navidrome

1. In Navidrome admin → **Plugins** → upload `tonus-navidrome-plugin.ndp`.
2. Enable the plugin. Required permissions:
   - **HTTP** to your tonus host (manifest currently uses `"*"` — Navidrome may prompt you to confirm).
   - **Scheduler** for the cron registration.
   - **KVStore** for per-user status persistence.
   - **SubsonicAPI** for playlist create/update.
   - **Users** — flip on `Allow all users` or pick the users you want discovery for.
   - **TaskQueue** for async per-user pipelines.
3. Open the plugin's settings page and fill in:
   - **Connection**: `tonus_url` (e.g. `http://192.168.1.200:8000`) and `tonus_token` (the `TONUS_API_TOKEN` from your tonus `.env`, optional).
   - **Schedule + Global Discovery Tuning**: cron expression (default `0 7 * * *`), top-N artists, tracks-per-artist, max queue per run.
   - **Users**: click `+` for each Navidrome user. Per entry set `navidrome_username`, `listenbrainz_username`, and the `playlist_name` template (`Discovery {date}` or `Discovery {user} {date}`).

## How it works under the hood

1. **OnInit** registers the cron with `host.SchedulerScheduleRecurring(cron, ...)` and creates the TaskQueue with `host.TaskCreateQueue(...)`.
2. **OnCallback** (cron tick) reads the user-mapping array and enqueues — for each user — one trigger-task and one reconcile-task. Returns in <1 s.
3. **OnTaskExecute** (one of the two task types):
   - `trigger-sync`: health-check + `POST /api/plugin/sync` with the user-specific config. Backend kicks off discovery + download asynchronously.
   - `reconcile`: `GET /api/plugin/finished-tracks?navidrome_user=...&limit=25` returns finished tracks **with backend-resolved Subsonic IDs**. Plugin opens a Subsonic client bound to the Navidrome user (via `host.SubsonicAPICall`) and `findOrCreate` + `addTracksIdempotent` against the playlist.
4. **Status**: per-user state is persisted in `KVStore` under `status:<navidrome_user>`. The tonus web UI also shows a global status card.

## Cold-start note

The first time the plugin runs for a fresh Navidrome user, you may see one "Task execution failed: module closed with context deadline exceeded" warning — that's Navidrome registering a new player context for that user the first time the plugin authenticates as them. The retry (built into the TaskQueue with `MaxRetries=3, BackoffMs=30000`) succeeds in seconds. After 1–2 successful reconciles the cold start is over and tasks complete instantly.

## Architecture decisions worth knowing

- **Plugin = trigger, backend = brain.** All discovery + download stay in tonus where they're easy to debug. Plugin only knows three tonus endpoints + Subsonic.
- **Reconcile pattern, not polling.** Tracks queued today might not finish for hours or days (rate-limited downloads). Each cron tick the reconcile runs again, idempotently picks up newly finished tracks, never duplicates.
- **Subsonic IDs cached in `download_jobs.payload_json`.** Resolves the slow `search3` once per track (admin-auth, server-side) and never again.
- **Per-user playlist ownership** via `host.SubsonicAPICall("?u=USERNAME")`. No user passwords stored in the plugin or backend — Navidrome injects auth based on the plugin's `users` permission.

## License

MIT — see [LICENSE](./LICENSE).
