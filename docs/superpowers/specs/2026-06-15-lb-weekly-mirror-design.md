# LB-Weekly-Mirror — Design Spec

**Date:** 2026-06-15
**Status:** Approved (brainstorming) → ready for implementation plan
**Touches:** Backend (Python, `services/discovery.py`, `app.py`) + `tonus-navidrome-plugin` (Go/TinyGo/WASM)

## Context

Die bisherige Discovery-Quelle des Plugins ist artist-radio: LB-Top-Artists →
Deezer-Artist-Radio → Filter gegen Hör-History. In der Praxis liefert das bei
einer fokussierten Library kaum Neues — ein Live-Lauf zeigte
`candidates: 30, skipped: 26, queued: 4`: 26 von 30 Kandidaten waren bereits in
der Library, weil Deezer-Artist-Radio dieselben Szene-Hits recycelt.

ListenBrainz kuratiert jedem User wöchentlich vier „Created For You"-Playlists,
die algorithmisch deutlich besser sind:
**Weekly Exploration**, **Weekly Jams**, **Last Week's Exploration**,
**Last Week's Jams**. Ziel: diese vier in Navidrome spiegeln — gleiche Namen,
wöchentliche Aktualisierung wie auf LB (replace-in-place) — und die
artist-radio-Discovery dabei ersetzen.

## Scope

- **Ersetzt** die artist-radio-Default-Discovery (nicht parallel, nicht Toggle).
- **Genre-Mixes bleiben** unangetastet (separates, optionales Feature).
- Vorerst Single-User (admin), aber das Plugin-Modell bleibt multi-user-fähig
  (Playlists owned by Navidrome-User via `host.SubsonicAPICall`).

## Approach (gewählt: A — Reuse der Plugin-Pipeline)

Die LB-Weekly-Playlists durchlaufen dieselbe erprobte Discovery→Build→Replace-
Mechanik wie die Genre-Mixes, nur mit einer **LB-createdfor-Playlist als
Quelle** statt eines Genre-Tags. Verworfen: ein Backend-only-Daemon (C) —
könnte nur admin-owned Playlists und müsste die User-Mappings aus dem Plugin
duplizieren.

## Architecture

### Quelle (Backend, `services/discovery.py`)

`lb_playlist_tracks(user, slug_or_mbid)` zieht bereits eine createdfor-Playlist
über `GET /user/{user}/playlists/createdfor` und matcht per `source_patch`
(z.B. `weekly-exploration`, `weekly-jams`).

**Erweiterung** für „Last Week's": der createdfor-Endpoint liefert mehrere
Versionen desselben Algorithmus (aktuelle + Vorwoche). Die Funktion wählt
aktuell die *erste* gematchte. Neu: ein `occurrence`-Parameter (0 = neueste,
1 = zweitneueste), wobei die Kandidaten nach `created`/`date` absteigend
sortiert werden. Mapping:

| Navidrome-Playlist        | source_patch         | occurrence |
|---------------------------|----------------------|------------|
| Weekly Exploration        | `weekly-exploration` | 0          |
| Last Week's Exploration   | `weekly-exploration` | 1          |
| Weekly Jams               | `weekly-jams`        | 0          |
| Last Week's Jams          | `weekly-jams`        | 1          |

Liefert ein Slot keine Daten (LB hat die Vorwoche noch nicht), wird er in
diesem Lauf still übersprungen (kein Fehler).

### Discovery-Endpoint (Backend, `app.py`)

Neuer Endpoint `POST /api/plugin/lbweekly/discovery`, analog zu
`/api/plugin/mix/discovery`. Request (`PluginLbWeeklyDiscoveryRequest`):
`navidrome_user`, `listenbrainz_user`, `playlist_kind`
(`weekly-exploration` | `weekly-jams`), `occurrence` (0/1),
`playlist_name` (Anzeigename), `location`, optional `max_tracks`.

Ablauf (wie mix/discovery):
1. `lb_playlist_tracks(lb_user, playlist_kind, occurrence)` → Track-Liste
   (artist, title).
2. Pro Track: Library-Check via Navidrome-`search3` →
   `existing[]` (mit serverseitig aufgelöster Subsonic-ID) zurück.
3. Fehlende Tracks → in `download_jobs` queuen, mit **LB-Weekly-Marker** im
   Payload: `lbweekly_navidrome_user`, `lbweekly_playlist_name`. (Analog zu den
   bestehenden `plugin_sync_*` / Mix-Markern.)
4. Synchrone `existing[]`-Antwort, damit das Plugin sie im KVStore für die
   Build-Phase persistieren kann (bleibt im 30-s-Host-Timeout).

### Build (Plugin → `finished-tracks`)

`GET /api/plugin/finished-tracks` wird um den LB-Weekly-Marker erweitert
(Filter auf `lbweekly_navidrome_user` + `lbweekly_playlist_name`, analog zum
bestehenden `plugin_sync_navidrome_user`-Pfad). Die serverseitige
Subsonic-ID-Auflösung + Payload-Cache bleibt unverändert.

Der Plugin-Build-Task replaced die Subsonic-Playlist mit `existing[]` (KVStore)
+ frisch fertige Tracks — identisch zur Mix-Build-Logik (`runMixBuild`),
inklusive **replace-in-place** (`createPlaylist`/`updatePlaylist`), sodass die
Playlist sich wie auf LB aktualisiert.

### Plugin (Go/WASM)

Neue Config-Option **„LB Weekly Mirror" (Toggle) pro User-Mapping**. An = alle
vier Playlists werden gespiegelt.

- **OnInit:** registriert pro User mit aktivem Toggle vier
  Discovery+Build-Cron-Paare (analog zu den Mix-Crons
  `mix-discovery/<user>/<mix>`), bzw. ein gemeinsames Cron-Paar, das alle vier
  Slots in einem TaskQueue-Durchlauf abarbeitet (Detail in der Plan-Phase —
  Ziel: minimale Cron-Anzahl, da concurrency 1).
- **OnCallback:** enqueued Discovery- + Build-Tasks pro aktivem User.
- **OnTaskExecute:** Discovery-Task ruft `/api/plugin/lbweekly/discovery`,
  Build-Task ruft `/api/plugin/finished-tracks` + replaced die Subsonic-
  Playlist mit dem korrekten Namen.

### Config-Schema-Änderungen (`manifest.json`) — artist-radio raus

- **Entfernt:** `top_artists`, `tracks_per_artist`, `max_queue_per_run`
  (artist-radio-Tuning) und das per-User `playlist_name`-Template
  (`Discovery {date}`).
- **Neu:** pro User-Mapping ein Bool `lb_weekly_mirror` (Default an).
- Der globale `cron_expression` triggert künftig den LB-Weekly-Mirror.
- **Bleibt:** Connection (`tonus_url`, `tonus_token`), `cron_expression`,
  User-Mappings (`navidrome_username`, `listenbrainz_username`),
  Genre-`mixes` (unverändert).

### Backend-Aufräumen (artist-radio-Pfad)

`discover_via_artist_radio`, `_run_plugin_sync` und `POST /api/plugin/sync`
werden entfernt bzw. der Sync-Endpoint auf den LB-Weekly-Pfad umgestellt
(genaue Schnittmenge in der Plan-Phase; Ziel laut Scope: „sauberer Code, eine
Quelle"). Die Marker-/Reconcile-Hilfen für plugin-owned Playlists bleiben, da
LB-Weekly dieselbe Build-/Ownership-Mechanik nutzt.

## Data Flow (ein User, ein Cron-Tick)

```
Plugin OnCallback
  └─ pro aktivem User, pro Slot (4×):
       Discovery-Task → POST /api/plugin/lbweekly/discovery
            Backend: lb_playlist_tracks(kind, occurrence)
                     → search3 dedup
                     → existing[] zurück (KVStore)
                     → fehlende Tracks queuen (LB-Weekly-Marker)
       Build-Task    → GET /api/plugin/finished-tracks (LB-Weekly-Marker)
            Backend: completed-Jobs + Subsonic-ID-Auflösung
            Plugin:  Playlist replace (existing + finished), Name = LB-Name
Worker (asynchron): lädt fehlende Tracks → /mnt/nas/music → Navidrome-Scan
                    → beim nächsten Cron-Lauf via finished-tracks nachgezogen
```

## Error Handling

- **Slot ohne Daten** (LB hat Vorwoche/aktuelle Woche noch nicht): still
  überspringen, kein Fehler, keine leere Playlist anlegen.
- **Track nicht auflösbar** (noch nicht in Navidrome): bleibt ohne Subsonic-ID,
  wird beim nächsten Lauf nachgezogen — trickle-in, wie bei den Mixes.
- **LB-API-Fehler:** Lauf bricht für diesen Slot ab, Log-Eintrag, nächster Cron
  versucht es erneut. Bestehende Playlist bleibt unangetastet.
- **Replace-Schutz:** Playlist wird nur ge-replaced, wenn mindestens ein Track
  auflösbar ist — verhindert, dass eine volle Playlist durch einen leeren Lauf
  geleert wird.

## Testing

- Backend-Unit-Tests für `lb_playlist_tracks(..., occurrence)` mit gemocktem
  createdfor-Response (mehrere Versionen je source_patch, Sortierung,
  occurrence 0/1, leerer Slot).
- Backend-Test für `/api/plugin/lbweekly/discovery`: dedup gegen Library,
  existing[]-Format, Marker im Payload.
- Backend-Test für `finished-tracks` mit LB-Weekly-Marker-Filter.
- Plugin: manueller Live-Test (Cron vorstellen) — vier Playlists erscheinen in
  Navidrome mit korrekten Namen, owned by admin; zweiter Lauf replaced statt zu
  duplizieren.

## Verifiziert gegen die Live-LB-API (2026-06-15)

`GET /1/user/madmax1301/playlists/createdfor` liefert **vier** Einträge — je
zwei Exploration und zwei Jams (aktuelle Woche + Vorwoche). Das
`occurrence`-Mapping hält.

**Bestätigte Implementierungs-Details:**
- Die Liste ist **nicht** global chronologisch sortiert (Reihenfolge im Test:
  Exploration-aktuell, Jams-aktuell, Jams-Vorwoche, Exploration-Vorwoche).
  Daher: **erst pro `source_patch` filtern, dann nach `date` absteigend
  sortieren, dann `occurrence`-Index nehmen** — nicht den globalen Listen-Index.
- Sortier-Schlüssel ist das Top-Level-`date`-Feld jeder Playlist
  (ISO-Timestamp, z.B. `2026-06-14T22:09:46…`).
- Die LB-Titel enthalten Datum („…week of 2026-06-15 Mon") — irrelevant für die
  Navidrome-Namen (die kommen aus dem Plugin-Config). Titel/`source_patch`
  dienen nur der Quell-Identifikation.
- **Offen für die Plan-Phase:** exakte `source_patch`-Strings im
  `extension`-Block bestätigen (Annahme `weekly-exploration` / `weekly-jams`) —
  ein zweiter curl, der `p['playlist']['extension']` ausgibt, vor dem ersten
  Code.

## Deployment

- Backend: normaler `:dev`-Image-Release (CHANGELOG, Tag, GHCR).
- Plugin: `make` (TinyGo) → `tonus-navidrome-plugin.ndp` → GitHub-Release →
  in Navidrome installieren. Config migrieren (artist-radio-Felder entfallen,
  `lb_weekly_mirror` an). Alte „Discovery"-Navidrome-Playlist manuell löschen.
