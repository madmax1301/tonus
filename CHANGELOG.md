# Changelog

All notable changes to Tonus are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Until the first tagged release, everything below lives under `[Unreleased]`.
On a `git tag -a vX.Y.Z`, move the relevant entries into a new dated section.

---

## [0.5.3] — 2026-06-11

Queue-Anzeige-Polish für Playlist-Imports.

### Fixed

- **Queued Playlist-Tracks zeigten rohe URLs** — SoundCloud liefert beim
  flat-extract eines Sets nur für die ersten ~5 Einträge Titel; der Rest
  sind Stubs (api-v2-URL, kein Titel/Cover). Die Queue zeigte deshalb
  `https://api-v2.soundcloud.com/tracks/…` als Track-Name. Jetzt:
  Placeholder „<Playlistname> · Track N/M", Playlist-Owner als Artist
  und das Set-Artwork als Cover. Sobald eine Lane den Track verarbeitet,
  ersetzt der Worker das wie bisher durch die echten Metadaten
  (Full-Extract bei progress=25); in der Library landen Tracks immer
  mit echtem Titel + embedded Cover.
- YouTube-Playlist-Einträge übernehmen ihr per-Entry-Thumbnail (bei
  YT liefert der flat-extract welche) statt des Set-Artworks.

---

## [0.5.2] — 2026-06-10

Performance-/Log-Fix für den Playlist-Reconcile aus v0.5.0.

### Fixed

- **Reconcile-Memo gegen Lookup-/Log-Spam** — der 15-min-Reconcile-Thread
  prüfte bei jedem Lauf ALLE Playlist-Marker im 60-Tage-Fenster erneut
  (nach dem ersten Materialisieren: ~5 300 Subsonic-Lookups + 218
  „+0 tracks"-Logzeilen pro Lauf, für 60 Tage). Jetzt merkt sich jeder
  Job pro Playlist, dass er erfolgreich reconciled wurde
  (`reconciled_playlists`-Liste in `payload_json`) und wird übersprungen.
  Unresolved Tracks (noch nicht im Navidrome-Index) bleiben ohne Memo und
  werden weiter nachgezogen; bei Subsonic-API-Fehlern wird nicht memoized.
- **Leise Steady-State-Logs** — die per-Playlist-Logzeile erscheint nur
  noch bei `added > 0`. Sind alle Paare memoized, returnt der Lauf früh
  ohne jede Ausgabe.

### Changed

- **Nebeneffekt (gewollt):** Tracks, die manuell aus einer
  Navidrome-Playlist entfernt wurden, werden vom Reconcile nicht mehr
  bei jedem Lauf wieder hinzugefügt — das Memo gilt als „war schon drin".

---

## [0.5.1] — 2026-06-10

Follow-up aus dem v0.5.0-Live-Test: Fehler-Transparenz + Auffindbarkeit.

### Added

- **Eigener Library-Tab „Playlist · SC/YT"** — der Playlist-Import war im
  URL-Mode versteckt (Auto-Detection ohne sichtbaren Einstieg). Jetzt:
  dedizierter Mode-Tab mit eigenem Eingabefeld, Live-Probe-Status
  („prüfe Playlist …" → „Playlist: ‚Name' — N Tracks"), immer sichtbarem
  Navidrome-Toggle und erklärendem Hint (inkl. Share-Link-Hinweis für
  private Sets). Der URL-Tab behält die Auto-Detection als Convenience.

### Fixed

- **Expand-Fehler erzeugte Fake-Success** — schlug der Playlist-Extract
  fehl (z.B. HTTP 404 bei privaten SC-Sets), fiel der Handler still auf
  den Single-URL-Pfad zurück: „✓ In Queue als url-XXXX", der Job starb
  unsichtbar im Error-Haufen. Jetzt: **422 mit Operator-Hinweis**
  („Playlist nicht lesbar (404). Wenn das Set privat ist: den
  SoundCloud-Share-Link nutzen (endet auf /s-XXXXX) …").
  `expand_playlist_url` unterscheidet jetzt „keine Playlist" (None →
  Single-Pfad) von „Extract-Fehler" (`{'error': …}` → 422).
- **Probe meldet Fehler sofort** — `POST /api/url/probe` liefert
  `kind='error'` + Message; das Frontend zeigt das Problem schon beim
  Paste (rot), nicht erst nach dem Submit.

### Operator Notes

- Private SoundCloud-Sets: Share → Link kopieren → der Link mit
  `/s-XXXXX`-Secret-Token macht das Set für yt-dlp lesbar. Alternativ
  SC-Cookies in der cookies.txt hinterlegen oder Set öffentlich stellen.

---

## [0.5.0] — 2026-06-09

**SoundCloud-Playlist-Import + automatischer Navidrome-Playlist-Build.**
Eine SC-Set-URL (oder YouTube-Playlist-URL) im URL-Mode pasten → alle
Tracks werden gequeut und landen nach Download automatisch in einer
gleichnamigen Navidrome-Playlist.

Design-Doc: `docs/superpowers/specs/2026-06-09-soundcloud-playlist-import-design.md`

### Added

- **Playlist-Erkennung im URL-Mode** — `POST /api/url/download` erkennt
  Playlist-URLs (SC `/sets/`, YT `list=`/`/playlist`) und expandiert sie
  via yt-dlp flat-extract zu N einzelnen Download-Jobs. Single-URLs
  verhalten sich exakt wie bisher (gleiche Latenz, BackgroundTask-Pfad).
- **`POST /api/url/probe`** — leichtgewichtiger Probe-Endpoint; das
  Frontend zeigt beim Paste „Playlist: ‚Name' — N Tracks" + Toggle
  „Als Navidrome-Playlist anlegen" (default an).
- **Worker-Dispatch für `kind='url'`-Jobs** — Playlist-Tracks laufen als
  `status='queued'` durch die Download-Lanes statt als parallele
  BackgroundTasks. Damit greifen Lane-Cooldowns, VPN-Lane-Binding und
  Bot-Check-Re-Queue (#51) — 200 Tracks hämmern SoundCloud nicht zu.
- **Periodischer Playlist-Reconcile-Thread** — `_reconcile_imported_playlists`
  lief bisher nur bei Plugin-Syncs; ohne Navidrome-Plugin wurden
  Playlist-Marker nie zu Subsonic-Playlists. Jetzt alle 15 min
  (`PLAYLIST_RECONCILE_INTERVAL_S`) + sofort nach Playlist-Submit für
  Tracks, die schon in der Library sind.
- **Dedup mit Playlist-Vollständigkeit** — Tracks die schon
  queued/processing/completed sind, werden nicht doppelt geladen; bei
  completed wird der Playlist-Marker in den bestehenden Job gemerged,
  damit die Navidrome-Playlist trotzdem vollständig wird.

### Env (neu, optional)

| Var | Default | Zweck |
|---|---|---|
| `PLAYLIST_MAX_TRACKS` | `200` | Safety-Cap pro Playlist-Expand |
| `PLAYLIST_RECONCILE_INTERVAL_S` | `900` | Reconcile-Thread-Intervall |

### Operator Notes

- Tracks werden **direkt von SoundCloud** geladen (treu zur Playlist —
  exakt die Version, die der Kurator gewählt hat), kein Resolver-Umweg.
  Wenn SC einen Track blockt, greift Bot-Check-Re-Queue; die Playlist
  füllt sich über Zeit nach (idempotenter Reconcile).
- Tags wie bei URL-Downloads üblich: Artist = SC-Uploader, Album =
  „Singles". Die Tracks liegen also unter `<Uploader>/Singles/`.
- Scope v1: SoundCloud + YouTube-Playlists. Spotify/Apple-Playlist-URLs
  bewusst nicht (brauchen API-Auth → CSV-Import nutzen).
- 7 neue Unit-Tests (`expand_playlist_url`, gemocktes yt-dlp), Suite 21✓.

---

## [0.4.10] — 2026-06-07

Frontend-Major-Migrationen. Schließt die zwei in v0.4.4 deferred
Dependabot-PRs (#23, #25) plus den damit gekoppelten vite-Major (#36).

### Changed

- **lucide-svelte** 0.453 → 1.0.1. Breaking: 4 Icon-Renames/Entfernungen
  über 6 Komponenten:
  - `Loader2` → `LoaderCircle`
  - `AlertTriangle` → `TriangleAlert`
  - `Youtube` → `Play` (Brand-Icon in 1.0 entfernt — Trademark; `Play`
    passt semantisch zum YouTube-Match-Mode)
  - `Link2` unverändert (existiert weiter)
- **vite** 5.4 → 8.0.16 + **@sveltejs/vite-plugin-svelte** 4.0 → 7.1.2
  (gekoppelt — plugin-svelte 7 verlangt vite ^8). vite 8 nutzt
  **rolldown** als Bundler statt rollup/esbuild → package-lock ~836
  Zeilen kleiner. SvelteKit 2.61 + svelte 5.56 sind kompatibel, keine
  vite.config-Änderung nötig.
- **Dockerfile**: Frontend-Build-Stage auf `--platform=$BUILDPLATFORM`
  gepinnt. rolldowns natives Rust-Binary hängt unter QEMU-arm64-
  Emulation → der erste multi-arch-Build mit vite 8 lief >25min ohne
  Ende. Der FE-Build läuft jetzt nativ auf der Builder-Arch; das
  statische Output wird arch-unabhängig in beide Runtime-Images kopiert.
  Build danach wieder ~2min.

### Verifiziert

- `svelte-check` 0 Errors (14 vorbestehende Warnings unverändert)
- `vite build` grün, `vite preview` liefert HTTP 200 mit korrektem
  `<title>Tonus</title>`
- npm audit: 6 low, kein high/critical → dep-audit-Baseline bleibt clean

---

## [0.4.9] — 2026-06-07

Resolver-Fix für Compilation-Alben. Konkretes Failing-Beispiel aus
Production: "The Screech" von JUNIVERZ (Album "HAMBURG BALLERT ANDERS").

### Fixed

- **Album-Suffix killt Suche bei Compilation-Namen** — `album_suffix_for_query()`
  hängt den Album-Namen an die Such-Query für mehr Spezifität. Bei
  Compilation-Alben deren Name nichts mit dem Track zu tun hat
  (`"JUNIVERZ The Screech HAMBURG BALLERT ANDERS official"`) liefert
  YouTube 0 Treffer — obwohl `"JUNIVERZ The Screech"` den Track als
  Top-Hit findet.
  - **Resolver fährt jetzt 2 Pässe**: Pass 1 mit Album-Suffix (Spezifität
    bei generischen Track-Namen bleibt erhalten), Pass 2 ohne Album wenn
    Pass 1 leer ausgeht. `duration_ms` etc. bleiben im Retry erhalten
    (Duration-Filter funktioniert weiter).
  - **Legacy-ytsearch1-Fallback ohne Album-Suffix** — wenn er erreicht
    wird, haben beide Resolver-Pässe YouTube schon mit und ohne Album
    befragt; die Query soll maximal breit sein. Plus Brackets-Strip
    (#52-Pfad) auch hier.

### Operator Notes

- Kein .env-Diff, keine Schema-Änderung.
- Log zeigt den Retry explizit:
  `INFO: no candidates with album-suffix query (album='…') — retrying without album`

---

## [0.4.8] — 2026-06-07

Revert von v0.4.7. Deploy-Smoke zeigte: die curl-cffi-0.15-Bridge lädt
mit dem aktuellen yt-dlp-Release nicht.

### Fixed

- **curl-cffi zurück auf `>=0.10,<0.15`** — der v0.4.7-Bump auf 0.15
  basierte fälschlich auf yt-dlp-**master**-Stand (`<0.16` erlaubt).
  Das aktuellste yt-dlp-**Release** 2026.03.17 erlaubt aber nur
  `0.5.10 + 0.10.x–0.14.x` (`yt_dlp/networking/_curlcffi.py` raised
  ImportError bei 0.15). Folge auf dem NAS:
  `Impersonate target "chrome" is not available` → Impersonation wieder
  silent-disabled.
- dep-audit `--ignore-vuln CVE-2026-33752` wieder rein (dokumentiert
  mit Revert-Begründung)
- Dependabot-ignore zurück auf `>=0.15`

### Lessons

- **master ≠ latest release.** Supported-Ranges immer gegen den
  installierten/installierbaren Release-Tag checken
  (`raw.githubusercontent.com/yt-dlp/yt-dlp/<TAG>/...`), nicht gegen
  master.
- Der v0.4.6-Fix (ImpersonateTarget-Objekt) bleibt korrekt — die
  WARN-Message hat sich von `AssertionError` zu `Impersonate target not
  available` geändert, was die Bridge-Diagnose erst möglich machte.

### Follow-up

- Sobald yt-dlp **>2026.03.17** released ist (master-Range <0.16 landet
  im Release): v0.4.7-Bump wiederholen. Tracking: Boot-Log-WARN
  verschwindet dann mit curl-cffi 0.15.

---

## [0.4.7] — 2026-06-07

curl-cffi-Pin-Auflösung. Schließt die Diagnose-Kette aus v0.4.6 ab.

### Changed

- **curl-cffi** `>=0.10,<0.15` → `>=0.15,<0.16`. yt-dlp ≥2026.x supportet
  curl-cffi bis <0.16 und pinnt selbst 0.15.0 — das alte Tonus-Pin <0.15
  (aus der yt-dlp-2024er-Bridge-Ära) war obsolet. Floor auf 0.15 wegen
  CVE-2026-33752 (betrifft 0.14.x).
- Damit lösen sich **drei Workarounds gleichzeitig auf**:
  1. `--ignore-vuln CVE-2026-33752` aus dep-audit.yml entfernt —
     pip-audit-Baseline ist jetzt wirklich clean (keine ignores)
  2. Dependabot-ignore von `>=0.15` auf `>=0.16` verschoben (echte
     Upstream-Grenze statt Workaround-Grenze)
  3. requirements-Kommentar dokumentiert jetzt die yt-dlp-Range-Kopplung
     statt der alten Bridge-Inkompatibilität

### Operator Notes

- Kein .env-Diff. Image-Rebuild zieht curl-cffi 0.15.x automatisch.
- Nach Deploy: Boot-Log checken — `WARN: Impersonate-Probe` darf NICHT
  auftauchen (wäre Indiz dass die 0.15-Bridge doch klemmt; dann Issue
  aufmachen und Pin temporär zurück auf `>=0.10,<0.15` + ignore-vuln).

---

## [0.4.6] — 2026-06-07

Hotfix für eine Regression aus dem v0.4.4-yt-dlp-Bump, gefunden beim
v0.4.5-Deploy-Smoke auf dem NAS.

### Fixed

- **Impersonation silent-disabled seit v0.4.4** — yt-dlp ≥2026.x verlangt
  den `impersonate`-Wert als `ImpersonateTarget`-Objekt; der rohe String
  `'chrome'` aus `YOUTUBE_IMPERSONATE` triggerte einen `AssertionError`
  in der Boot-Probe (`networking/impersonate.py:is_supported_target`).
  Folge: TLS-Fingerprint-Spoofing war seit dem yt-dlp-Bump auf 2026.03.17
  deaktiviert → schwächere Anti-Bot-Detection → mehr Bot-Checks (die
  zwar seit v0.4.5 re-queued werden, aber vermeidbar sind).
  Fix: `_parse_impersonate_target()` konvertiert den env-String einmal
  beim Module-Import via `ImpersonateTarget.from_str()`.
  Boot-Log zeigt jetzt keine `WARN: Impersonate-Probe fehlgeschlagen`
  mehr.

### Operator Notes

- Kein .env-Diff — `YOUTUBE_IMPERSONATE=chrome` bleibt als String
  konfiguriert, die Konversion passiert intern.
- Side-Note aus derselben Diagnose: yt-dlp master erlaubt inzwischen
  curl-cffi bis <0.16 (pin selbst auf 0.15.0). Das Tonus-Pin <0.15 kann
  in einem späteren Cycle gelockert werden → würde `--ignore-vuln
  CVE-2026-33752`, den Dependabot-ignore und das requirements-Pin
  gleichzeitig auflösen. Separater Task, braucht Bridge-Smoke-Test.

---

## [0.4.5] — 2026-06-07

Resolver- und Worker-Robustheit. Zwei Operator-Pain-Points aus dem v0.4.4-
Backlog adressiert.

### Fixed

- **Bot-Check Re-Queue (#51)** — yt-dlp's "Sign in to confirm you're not a
  bot"-Error landete bisher als permanenter `error` in der Queue. Jetzt
  erkennt der Worker das Pattern und re-queued den Job mit
  `retry_count++` und langem Lane-Cooldown (10–20 min, IP-Wechsel-
  Window). Cap bei `BOT_CHECK_MAX_RETRIES` (default 5) — danach
  permanent mit Operator-Hinweis (Cookies setzen / VPN wechseln).
  Im dual-lane-Setup landet das re-queued Job typischerweise auf der
  anderen VPN-IP, was bot-check oft auto-fixt.
  - Neue Spalte `download_jobs.retry_count` (auto-Migration via
    `_ensure_column`)
  - Neue Helper `_looks_like_bot_check()` neben dem bestehenden
    `_looks_like_429()`
  - Neue Function `requeue_for_retry()` in `utils/job_store.py`
- **Search mit Brackets/Sonderzeichen (#52)** — Tracks wie
  `"Bitches [Mix Cut] (Original Mix)"` lieferten 0 Treffer auf YouTube +
  SoundCloud weil die Bracket-Tokens als hartes Match-Constraint
  interpretiert wurden. Neuer Helper `strip_search_decorations()`
  entfernt `[…]`- und `(…)`-Inhalte für den Search-Fetch. Original-
  `track_name` bleibt für `calculate_match_score()` unverändert, damit
  die Score-Diskriminierung gegen Result-Titles weiterhin scharf ist.
  - Eingesetzt in 3 Query-Build-Sites: YTMusic-Search, yt-dlp-ytsearch-
    Fallback, MultiSourceResolver-SoundCloud-Pfad

### Operator Notes

- Neue env-vars (optional):
  - `BOT_CHECK_MAX_RETRIES` (default `5`) — Cap für transient retry
- Keine .env-Migration nötig, Defaults sind gewählt für die Mehrheit der
  Setups
- Schema-Migration läuft beim ersten Boot automatisch (idempotent)

### Deferred → eigene PRs

- **lucide-svelte 1.0 + vite-plugin-svelte 7.x** — siehe v0.4.4 Notes,
  bleiben für eigenen Frontend-Migration-Sprint
- **15 file-ACL-Errors auf NAS** — Operator-Task außerhalb des Code-Scope

---

## [0.4.4] — 2026-06-07

Dependency-Bump-Sweep. Erste planmäßige Cleanup-Welle nach Aktivierung
des `dep-audit` Workflows + Dependabot in v0.4.3. Baseline ist danach
clean → dep-audit wechselt von `report-only` auf `strict`-Mode.

### Bumped (Backend)

- **fastapi** 0.104.1 → 0.136.3 (CVE-Cluster der 0.10x-Linie geschlossen,
  Underscore-Header-Validation, SSE-Field-Validation)
- **uvicorn[standard]** 0.24.0 → 0.48.0
- **pydantic** 2.5.0 → 2.13.4
- **spotipy** 2.23.0 → 2.26.0
- **python-multipart** 0.0.26 → 0.0.30
- **requests** 2.33.0 → 2.34.2
- **ytmusicapi** 1.11.4 → 1.12.0
- **PyJWT** 2.12.0 → 2.13.0
- **argon2-cffi** 23.1.0 → 25.1.0
- **yt-dlp** ≥2024.12.20 → ≥2026.3.17 (Bot-Check-Resilience, Format-Fixes)
- **cryptography** 46.0.5 → 48.0.0 (bundled-OpenSSL bumps)

### Bumped (Frontend)

- **typescript** 5.9.3 → 6.0.3
- **bits-ui** 1.8.0 → 2.18.1 (nur eine Verwendung — Dialog in TokenSheet)
- **@sveltejs/kit** 2.59.0 → 2.61.1
- **svelte** 5.55.5 → 5.56.0
- **svelte-check** 4.4.7 → 4.5.0
- **tailwindcss** + **@tailwindcss/vite** 4.2.4 → 4.3.0

### Bumped (CI)

- **actions/setup-python** 5 → 6
- **docker/setup-buildx-action** 3 → 4
- **docker/metadata-action** 5 → 6

### Changed

- **dep-audit Workflow** auf `strict`-Mode umgestellt. Bisher `report-only`
  mit `continue-on-error: true` (baseline-cleanup-Phase). Jetzt:
  pip-audit oder npm audit findings (high/critical) **failen das CI**.
  Bei Dependabot-Bump-PRs heißt ein roter Check aktiv: "der Bump ist
  nötig" — kein stilles "FYI" mehr.
- **Dependabot** ignoriert ab jetzt `curl-cffi >=0.15`. Pin ist
  absichtlich (yt-dlp-Bridge Inkompatibilität). Dependabot probierte
  in PR #27 zu bumpen — wurde geschlossen, Konfig-Eintrag verhindert
  Wiederholung.

### Deferred (eigene v0.4.5-PR)

- `lucide-svelte` 0.453 → 1.0.1 — Build-FAILURE, 9 Verwendungsstellen
  mit Named-Imports. Tree-Shaking-API-Migration nötig.
- `@sveltejs/vite-plugin-svelte` 4.0.4 → 7.1.2 — Major-Major-Jump
  (v5/v6 übersprungen), Build + npm audit FAILURE. Vite-Config-Migration
  zusammen mit eventuellem SvelteKit 3.x ziehen.

### Operator Notes

- Kein .env-Diff. Backend lädt fastapi 0.136 ohne Code-Anpassung
  (CI Backend (Python 3.11) Check grün).
- yt-dlp >=2026.3.17 ist relevant für Audit-Item #51 (Bot-Check-Pfad) —
  prüft sich von selbst beim nächsten v0.4.4-Cycle ob die Symptome
  besser werden.

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
