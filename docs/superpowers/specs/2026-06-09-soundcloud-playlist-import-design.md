# SoundCloud-Playlist-Import → Navidrome-Playlist-Build

**Datum:** 2026-06-09
**Status:** approved (User-Review im Chat)
**Ziel-Release:** v0.5.0 (neues Feature, minor bump)

## Problem

Tonus kann einzelne URLs (Track) downloaden, aber keine SoundCloud-Playlist
als Ganzes. User will eine SC-Playlist-URL pasten und bekommt:
1. alle Tracks der Playlist als opus in der Library
2. automatisch eine gleichnamige Navidrome-Playlist mit genau diesen Tracks

## Entscheidungen (User-approved)

| Frage | Entscheidung |
|---|---|
| Track-Resolution | **Direkt von SoundCloud** — jeder Track lädt von seiner SC-URL via `download_by_url`. Treu zur Playlist (exakte Version/Mix), kein Resolver-Umweg. |
| Eingabe-UX | **URL-Mode erkennt Playlists automatisch** — kein neuer UI-Mode. Paste einer Playlist-URL → Probe zeigt "Playlist erkannt: N Tracks". |
| Navidrome-Build | **Automatisch mit SC-Namen, Toggle zum Abschalten** — Checkbox "Als Navidrome-Playlist" (default an) im URL-Mode wenn Playlist erkannt. |

## Architektur

Kernidee: **In die bestehende Maschinerie einklinken, nichts Neues bauen.**
Die Playlist-Pipeline existiert seit Phase I komplett:

- `download_jobs.payload_json["import_playlist_names"]` — Playlist-Marker pro Track
- `_reconcile_imported_playlists()` (app.py) — findet completed-Tracks mit
  Marker, baut idempotent Subsonic-Playlists (`find_playlist_by_name` →
  `create_playlist` / `update_playlist`)
- `_reconcile_import_library_matches()` — deckt Tracks ab, die schon in der
  Library sind (kein Download-Job)
- Bot-Check-Re-Queue (#51) — fehlgeschlagene Tracks füllen die Playlist
  später automatisch nach (Reconcile ist idempotent + zeitfensterbasiert)

### Datenfluss

```
User pastet SC-Playlist-URL im URL-Mode
   │
   ▼ (Frontend: Probe-Call)
POST /api/download/url/probe → expand_playlist_url() erkennt _type=playlist
   │  Response: {kind: "playlist", name, track_count}
   ▼ (User bestätigt, Checkbox "Als Navidrome-Playlist" default an)
POST /api/download/url {url, as_navidrome_playlist: true}
   │
   ▼
expand_playlist_url(url) → [{url, title, uploader}, ...]  (flat-extract)
   │  Cap: PLAYLIST_MAX_TRACKS (env, default 200)
   ▼
N download_jobs gequeut, payload je Track:
   • download_url = SC-Track-URL  → Worker nutzt download_by_url (direkt SC)
   • import_playlist_names = [<SC-Playlist-Name>]  (wenn Toggle an)
   ▼
Worker-Lanes arbeiten ab (Cooldowns, Bot-Check-Re-Queue greifen normal)
   ▼
_reconcile_imported_playlists (periodisch, bestehend)
   → Navidrome-Playlist "<SC-Name>" entsteht/wächst idempotent
```

## Komponenten

### Backend

1. **`expand_playlist_url(url) -> Optional[Dict]`** — neuer Helper in
   `services/youtube.py`. yt-dlp `extract_info(url, extract_flat=True)`.
   Returns `None` wenn kein `_type == "playlist"` (= einzelner Track,
   Caller fällt auf bisherigen Single-URL-Pfad zurück). Sonst:
   `{name: str, tracks: [{url, title, uploader}], truncated: bool}`.
   - Cap bei `PLAYLIST_MAX_TRACKS` (config.py, env, default 200)
   - SC-Pseudo-URLs (api.soundcloud.com) wie im bestehenden
     `_search_yt_dlp_extractor` filtern
   - Anti-Detection-Opts + Cookies wie bestehende Extract-Pfade

2. **`POST /api/download/url`** (app.py) — erweitert:
   - Neues Request-Feld `as_navidrome_playlist: Optional[bool] = True`
   - Vor dem Single-Download: `expand_playlist_url()` aufrufen
   - Playlist → N Jobs queuen (bestehende queue-Logik je Track,
     `import_playlist_names`-Marker analog Plugin-Sync-Pfad)
   - Response neu: `{kind: "playlist", queued: N, skipped: M,
     playlist_name: str, truncated: bool}` vs. bisheriges
     Single-Track-Response-Format (`kind: "track"`)
   - Dedup-Kriterium: existiert bereits ein `download_jobs`-Eintrag mit
     `status='completed'` und gleicher `download_url` → Track skippen,
     aber für den Playlist-Marker via Library-Match-Pfad registrieren
     (Playlist soll vollständig sein). Queued/processing-Jobs mit gleicher
     URL zählen ebenfalls als Skip (kein Doppel-Queue).

3. **Probe-Endpoint** — `POST /api/download/url/probe` (neu, leichtgewichtig):
   `{url}` → `{kind: "playlist"|"track", name?, track_count?}`.
   Frontend ruft das beim Paste auf, um die Playlist-UI einzublenden.
   Flat-extract ist schnell (~1-2s), kein Download.

### Frontend (URL-Mode in `src/routes/+page.svelte`)

- Nach URL-Eingabe (debounced): Probe-Call
- Bei `kind: "playlist"`: Info-Zeile "📋 Playlist: <Name> — N Tracks"
  + Checkbox "Als Navidrome-Playlist anlegen" (default checked)
- Submit → `as_navidrome_playlist` mitsenden
- Response-Handling: "N Tracks gequeut" Toast statt Single-Track-Karte
- Queue-Ansicht zeigt die Tracks einzeln (bestehende Job-Cards, kein
  neues UI — sie sind normale download_jobs)

## Edge-Cases (festgelegt)

1. **Track schon in Library** → wird geskippt (kein Doppel-Download), aber
   via Library-Match-Reconcile trotzdem zur Navidrome-Playlist hinzugefügt.
2. **Track schlägt fehl (Bot-Check/403)** → Re-Queue via #51; nach späterem
   Erfolg fügt der idempotente Reconcile ihn automatisch zur Playlist hinzu.
   Playlist füllt sich über Zeit, kein manueller Eingriff.
3. **Playlist > 200 Tracks** → Cap, `truncated: true` im Response, Warnung
   im Frontend ("erste 200 von 543 gequeut").
4. **Scope v1:** SoundCloud- und YouTube-Playlists (beide flat-extractable
   via yt-dlp). Spotify/Apple-Playlists explizit out of scope (brauchen
   API-Auth — anderer Import-Pfad, existiert teilweise via CSV).
5. **Leere Playlist / Extract-Fehler** → 422 mit klarer Message, kein Job.
6. **Playlist-Name-Kollision in Navidrome** → bestehende Semantik von
   `find_playlist_by_name`: gleicher Name = Tracks werden zur bestehenden
   Playlist hinzugefügt (idempotent, kein Duplikat-Append durch
   Reconcile-Dedup).

## Konfiguration

| Env | Default | Zweck |
|---|---|---|
| `PLAYLIST_MAX_TRACKS` | `200` | Safety-Cap pro Playlist-Expand |

Kein weiterer .env-Diff. Feature ist per Default aktiv (URL-Mode-Verhalten
für Single-URLs unverändert).

## Testing

- Unit: `expand_playlist_url` mit gemocktem yt-dlp (playlist / track /
  kaputte URL / truncation / SC-Pseudo-URL-Filter)
- Unit: `/api/download/url` Playlist-Branch (Job-Count, Marker-Payload,
  Dedup-Skip, Response-Shape)
- Manuell auf NAS: echte SC-Playlist (~10 Tracks) → Queue beobachten →
  Navidrome-Playlist prüfen; Toggle aus → keine Playlist
- Regression: einzelne SC-Track-URL + YouTube-URL verhalten sich wie vorher

## Nicht-Ziele

- Kein Playlist-Sync (Änderungen an der SC-Playlist später nachziehen) —
  das wäre ein Folge-Feature
- Keine Spotify/Apple-Playlist-URLs
- Kein eigener UI-Mode/Tab
