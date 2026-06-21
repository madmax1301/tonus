# LB-Weekly-Mirror Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** LBs vier „Created For You"-Playlists (Weekly/Last Week's Exploration + Jams) nach Navidrome spiegeln — gleiche Namen, wöchentliches Update (replace-in-place) — und die artist-radio-Discovery dabei ersetzen.

**Architecture:** Backend zieht eine LB-`createdfor`-Playlist als Discovery-Quelle (statt artist-radio), dedupt gegen die Library und queued fehlende Tracks mit den **bestehenden** `plugin_sync_playlist_name` + `plugin_sync_navidrome_user`-Markern. Damit funktioniert der **bereits vorhandene** Build-/Reconcile-Pfad (`/api/plugin/finished-tracks` → Plugin `runReconcile` → Subsonic-Playlist) unverändert. Das Plugin bekommt einen Toggle pro User; pro aktivem User werden vier Discovery+Reconcile-Läufe (einer je Playlist) über die Cron-Infrastruktur enqueued.

**Tech Stack:** Backend FastAPI/Python (`backend/app.py`, `backend/services/discovery.py`), Tests pytest (`backend/tests/`). Plugin Go/TinyGo→WASM (Extism), `tonus-navidrome-plugin/`.

## Global Constraints

- Test-venv: `cd backend && /tmp/tonus-test-venv/bin/python -m pytest -q` — Erwartung vor Beginn: 22 passed, 1 skipped.
- `py_compile` vor jedem Backend-Commit: `/tmp/tonus-test-venv/bin/python -m py_compile backend/app.py backend/services/discovery.py`.
- **Bestehende Marker wiederverwenden** (NICHT neue erfinden): `plugin_sync_playlist_name` (Playlist-Name) + `plugin_sync_navidrome_user` (→ Plugin baut user-owned, Backend-Reconcile skippt). Der `finished-tracks`-Endpoint filtert bereits exakt auf diese (`app.py:4200`, `:4221`).
- LB-API: `https://api.listenbrainz.org`, `GET /1/user/{user}/playlists/createdfor`. Sortierung pro `source_patch` nach Top-Level-`date` absteigend → `occurrence` 0/1.
- Navidrome-Owned-Playlists gehen **nur** übers Plugin (`host.SubsonicAPICall` mit User-Kontext) — Backend kann das nicht.
- Commit-Stil: `feat(...)`/`refactor(...)`, Co-Authored-By-Trailer wie im Repo üblich.

---

## Phase 1 — Backend (deploybar + testbar ohne Plugin)

### Task 1: `lb_playlist_tracks` um `occurrence` + `source_patch`-Verifikation

**Files:**
- Modify: `backend/services/discovery.py:214-249` (`lb_playlist_tracks`)
- Test: `backend/tests/test_discovery_lb_playlists.py` (neu)

**Interfaces:**
- Produces: `lb_playlist_tracks(user: str, slug_or_mbid: str, occurrence: int = 0) -> List[Dict]` — Dicts `{"artist": str, "title": str}`. `occurrence=0` = neueste Version des matchenden `source_patch`, `occurrence=1` = zweitneueste (Vorwoche). Leer wenn occurrence nicht existiert.

- [ ] **Step 0: `source_patch`-Strings live verifizieren** (vor Code)

Run (Operator/Executor):
```bash
curl -s "https://api.listenbrainz.org/1/user/madmax1301/playlists/createdfor" \
  | python3 -c "import sys,json; d=json.load(sys.stdin); [print(p['playlist'].get('date',''), '|', (p['playlist'].get('extension',{}).get('https://musicbrainz.org/doc/jspf#playlist',{}).get('additional_metadata',{}).get('algorithm_metadata',{}).get('source_patch','')), '|', p['playlist']['title'][:40]) for p in d.get('playlists',[])]"
```
Expected: vier Zeilen; die `source_patch`-Spalte zeigt für Exploration `weekly-exploration`, für Jams `weekly-jams` (je 2×, unterschiedliche `date`). **Falls die Strings abweichen**, die Konstanten in Task 2 (`_LBWEEKLY_SLOTS`) entsprechend anpassen, bevor weitergeschrieben wird.

- [ ] **Step 1: Failing tests schreiben**

```python
# backend/tests/test_discovery_lb_playlists.py
import services.discovery as disc


def _fake_createdfor(monkeypatch, playlists):
    class _Resp:
        ok = True
        def json(self): return {"playlists": playlists}
    def _get(url, *a, **k): return _Resp()
    monkeypatch.setattr(disc.requests, "get", _get)


def _pl(source_patch, date, tracks, title="t"):
    return {"playlist": {
        "title": title,
        "date": date,
        "identifier": "https://listenbrainz.org/playlist/" + date,
        "extension": {"https://musicbrainz.org/doc/jspf#playlist": {
            "additional_metadata": {"algorithm_metadata": {"source_patch": source_patch}}}},
        "track": [{"creator": a, "title": t} for a, t in tracks],
    }}


def test_occurrence_0_picks_newest_of_source_patch(monkeypatch):
    _fake_createdfor(monkeypatch, [
        _pl("weekly-exploration", "2026-06-14T22:09:00+00:00", [("A", "new")]),
        _pl("weekly-jams",        "2026-06-14T22:05:00+00:00", [("J", "jam")]),
        _pl("weekly-exploration", "2026-06-07T10:35:00+00:00", [("A", "old")]),
    ])
    out = disc.lb_playlist_tracks("u", "weekly-exploration", occurrence=0)
    assert out == [{"artist": "A", "title": "new"}]


def test_occurrence_1_picks_second_newest(monkeypatch):
    _fake_createdfor(monkeypatch, [
        _pl("weekly-exploration", "2026-06-14T22:09:00+00:00", [("A", "new")]),
        _pl("weekly-exploration", "2026-06-07T10:35:00+00:00", [("A", "old")]),
    ])
    out = disc.lb_playlist_tracks("u", "weekly-exploration", occurrence=1)
    assert out == [{"artist": "A", "title": "old"}]


def test_occurrence_missing_returns_empty(monkeypatch):
    _fake_createdfor(monkeypatch, [
        _pl("weekly-exploration", "2026-06-14T22:09:00+00:00", [("A", "new")]),
    ])
    assert disc.lb_playlist_tracks("u", "weekly-exploration", occurrence=1) == []


def test_default_occurrence_is_zero(monkeypatch):
    _fake_createdfor(monkeypatch, [
        _pl("weekly-jams", "2026-06-14T22:05:00+00:00", [("J", "jam")]),
    ])
    assert disc.lb_playlist_tracks("u", "weekly-jams") == [{"artist": "J", "title": "jam"}]
```

- [ ] **Step 2: Tests laufen → FAIL**

Run: `cd backend && /tmp/tonus-test-venv/bin/python -m pytest tests/test_discovery_lb_playlists.py -q`
Expected: FAIL (`lb_playlist_tracks() got an unexpected keyword argument 'occurrence'`)

- [ ] **Step 3: `lb_playlist_tracks` umschreiben**

Ersetze die bestehende Funktion (`discovery.py:214-249`) durch:
```python
def lb_playlist_tracks(user: str, slug_or_mbid: str, occurrence: int = 0) -> List[Dict]:
    """Tracks einer LB-'createdfor'-Playlist (z.B. 'weekly-exploration').

    occurrence=0 → neueste Version des matchenden source_patch,
    occurrence=1 → zweitneueste (Vorwoche, 'Last Week's …').
    Leere Liste, wenn die gewünschte occurrence nicht existiert.
    """
    out: List[Dict] = []
    try:
        r = requests.get(
            f"{LB_API}/user/{user}/playlists/createdfor",
            timeout=20,
            headers={"User-Agent": USER_AGENT},
        )
        if not r.ok:
            return out
        playlists = (r.json().get("playlists") or [])
        # Alle Playlists mit passendem source_patch sammeln, nach date desc sortieren.
        matches = []
        for p in playlists:
            pl = p.get("playlist") or {}
            ext = pl.get("extension", {}).get(
                "https://musicbrainz.org/doc/jspf#playlist", {}
            )
            algo = (
                (ext.get("additional_metadata", {}) or {})
                .get("algorithm_metadata", {})
                .get("source_patch", "")
            )
            if slug_or_mbid in algo or slug_or_mbid in pl.get("identifier", ""):
                matches.append(pl)
        if len(matches) <= occurrence:
            return out
        matches.sort(key=lambda pl: pl.get("date", ""), reverse=True)
        target = matches[occurrence]
        for t in target.get("track") or []:
            artist = t.get("creator", "")
            title = t.get("title", "")
            if artist and title:
                out.append({"artist": artist, "title": title})
    except Exception:
        pass
    return out
```

- [ ] **Step 4: Tests laufen → PASS**

Run: `cd backend && /tmp/tonus-test-venv/bin/python -m pytest tests/test_discovery_lb_playlists.py -q`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add backend/services/discovery.py backend/tests/test_discovery_lb_playlists.py
git commit -m "feat(discovery): lb_playlist_tracks occurrence param for Last Week's variants

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: LB-Weekly-Discovery-Endpoint

**Files:**
- Modify: `backend/app.py` — neue Request-Klasse bei den anderen `Plugin*Request` (~`app.py:704`), neuer Endpoint bei den `/api/plugin/*` (~`app.py:4131`), zwei Helfer bei `_run_plugin_mix_discovery`/`_check_mix_tracks_in_library` (~`app.py:3761`–`3928`)
- Test: `backend/tests/test_plugin_lbweekly.py` (neu)

**Interfaces:**
- Consumes: `lb_playlist_tracks(user, source_patch, occurrence)` aus Task 1; bestehende `navidrome_service.find_track_id_by_artist_title`, `deezer_search_track` (`services/discovery.py:320`), `get_duplicate_download_reason`, `_resolve_track_for_queue`, `upsert_job`, `resolve_navidrome_library_path_optional`.
- Produces: `POST /api/plugin/lbweekly/discovery` mit Body `PluginLbWeeklyDiscoveryRequest`; Response `{"started": true, "existing": [{"subsonic_id","artist","title"}, ...]}`. Gequeute Jobs tragen `plugin_sync_playlist_name=<playlist_name>` + `plugin_sync_navidrome_user=<navidrome_user>`.

- [ ] **Step 1: Failing test schreiben** (Library-Dedup + Marker im Payload)

```python
# backend/tests/test_plugin_lbweekly.py
import json
import app as app_mod


def test_lbweekly_discovery_queues_missing_with_sync_markers(monkeypatch):
    # LB liefert 2 Tracks; einer ist in der Library, einer fehlt.
    monkeypatch.setattr(app_mod, "_now_ms", lambda: 1000)
    import services.discovery as disc
    monkeypatch.setattr(disc, "lb_playlist_tracks",
                        lambda u, sp, occurrence=0: [
                            {"artist": "Have", "title": "Inlib"},
                            {"artist": "Need", "title": "Missing"}])

    def fake_find(artist, title):
        return "sub-have" if artist == "Have" else None
    monkeypatch.setattr(app_mod.navidrome_service,
                        "find_track_id_by_artist_title", fake_find)
    monkeypatch.setattr(disc, "deezer_search_track",
                        lambda a, t: {"id": 4242, "title": t,
                                       "artist": {"name": a}, "album": {"title": "Alb"}})
    monkeypatch.setattr(app_mod, "get_duplicate_download_reason",
                        lambda *a, **k: None)
    monkeypatch.setattr(app_mod, "_resolve_track_for_queue",
                        lambda tid, prov, hint: hint)
    captured = {}
    def fake_upsert(job_id, **kw):
        captured["job_id"] = job_id
        captured["payload"] = kw.get("payload")
    monkeypatch.setattr(app_mod, "upsert_job", fake_upsert)
    monkeypatch.setattr(app_mod, "resolve_navidrome_library_path_optional",
                        lambda x: "/music")

    req = app_mod.PluginLbWeeklyDiscoveryRequest(
        navidrome_user="admin", listenbrainz_user="lbuser",
        source_patch="weekly-exploration", occurrence=0,
        playlist_name="Weekly Exploration")
    app_mod._run_plugin_lbweekly_discovery(req)

    # Der fehlende Track wurde mit den BESTEHENDEN sync-Markern gequeued.
    assert captured["job_id"] == "4242"
    p = captured["payload"]
    assert p["plugin_sync_playlist_name"] == "Weekly Exploration"
    assert p["plugin_sync_navidrome_user"] == "admin"


def test_lbweekly_existing_returns_inlibrary_tracks(monkeypatch):
    import services.discovery as disc
    monkeypatch.setattr(disc, "lb_playlist_tracks",
                        lambda u, sp, occurrence=0: [
                            {"artist": "Have", "title": "Inlib"},
                            {"artist": "Need", "title": "Missing"}])
    monkeypatch.setattr(app_mod.navidrome_service,
                        "find_track_id_by_artist_title",
                        lambda a, t: "sub-have" if a == "Have" else None)
    req = app_mod.PluginLbWeeklyDiscoveryRequest(
        navidrome_user="admin", listenbrainz_user="lbuser",
        source_patch="weekly-exploration", occurrence=0,
        playlist_name="Weekly Exploration")
    existing = app_mod._check_lbweekly_tracks_in_library(req)
    assert existing == [{"subsonic_id": "sub-have", "artist": "Have", "title": "Inlib"}]
```

- [ ] **Step 2: Tests laufen → FAIL**

Run: `cd backend && /tmp/tonus-test-venv/bin/python -m pytest tests/test_plugin_lbweekly.py -q`
Expected: FAIL (`module 'app' has no attribute 'PluginLbWeeklyDiscoveryRequest'`)

- [ ] **Step 3: Request-Klasse hinzufügen** (nach `PluginMixDiscoveryRequest`, ~`app.py:725`)

```python
class PluginLbWeeklyDiscoveryRequest(BaseModel):
    """Trigger-Body für /api/plugin/lbweekly/discovery — eine der vier
    LB-'createdfor'-Playlists pro Call. Quelle ist eine fertig kuratierte
    LB-Playlist (source_patch + occurrence), nicht artist-radio.

    Gequeute Tracks tragen die BESTEHENDEN sync-Marker
    (plugin_sync_playlist_name + plugin_sync_navidrome_user), damit der
    vorhandene finished-tracks/Build-Pfad sie unverändert verarbeitet.
    """
    navidrome_user: str
    listenbrainz_user: str
    source_patch: str            # "weekly-exploration" | "weekly-jams"
    occurrence: int = 0          # 0 = aktuelle Woche, 1 = "Last Week's …"
    playlist_name: str           # Navidrome-Playlist-Name (z.B. "Weekly Exploration")
    location: str = "navidrome"
    max_tracks: int = 60
```

- [ ] **Step 4: Helfer `_check_lbweekly_tracks_in_library` hinzufügen** (neben `_check_mix_tracks_in_library`, ~`app.py:3800`)

```python
def _check_lbweekly_tracks_in_library(
    req: "PluginLbWeeklyDiscoveryRequest",
) -> List[Dict[str, str]]:
    """Synchroner Library-Lookup für die existing-Liste eines LB-Weekly-Calls.
    Liefert die schon vorhandenen Tracks mit Subsonic-ID (Plugin persistiert
    sie im KVStore für die Build-Phase)."""
    from services.discovery import lb_playlist_tracks
    items = lb_playlist_tracks(req.listenbrainz_user, req.source_patch, req.occurrence)
    existing: List[Dict[str, str]] = []
    for it in items[: req.max_tracks]:
        try:
            sid = navidrome_service.find_track_id_by_artist_title(
                it.get("artist", ""), it.get("title", "")
            )
            if sid:
                existing.append({
                    "subsonic_id": sid,
                    "artist": it.get("artist", ""),
                    "title": it.get("title", ""),
                })
        except Exception:
            continue
    return existing
```

- [ ] **Step 5: Background-Task `_run_plugin_lbweekly_discovery` hinzufügen** (neben `_run_plugin_mix_discovery`, ~`app.py:3928`)

```python
def _run_plugin_lbweekly_discovery(req: "PluginLbWeeklyDiscoveryRequest") -> None:
    """Background-Task hinter POST /api/plugin/lbweekly/discovery.

    Zieht die LB-Playlist (source_patch+occurrence), dedupliziert gegen
    Library, queued fehlende Tracks als download_jobs mit den BESTEHENDEN
    sync-Markern (plugin_sync_playlist_name + plugin_sync_navidrome_user),
    sodass /api/plugin/finished-tracks + der Plugin-Reconcile sie unverändert
    der user-owned Subsonic-Playlist zuordnen."""
    from services.discovery import lb_playlist_tracks, deezer_search_track

    started = _now_ms()
    items = lb_playlist_tracks(req.listenbrainz_user, req.source_patch, req.occurrence)
    if not items:
        print(f"[plugin-lbweekly] no LB tracks for {req.source_patch!r} occ={req.occurrence}")
        return

    location = req.location if req.location in ("local", "navidrome") else "navidrome"
    output_format = config.OUTPUT_FORMAT
    provider = "deezer"
    navidrome_path = resolve_navidrome_library_path_optional(None)
    run_id = f"plugin-lbweekly-{req.navidrome_user}-{req.source_patch}-{occ_tag(req.occurrence)}-{started}"

    queued = skipped = failed = 0
    for it in items[: req.max_tracks]:
        artist = (it.get("artist") or "").strip()
        title = (it.get("title") or "").strip()
        if not artist or not title:
            continue
        try:
            if navidrome_service.find_track_id_by_artist_title(artist, title):
                skipped += 1
                continue
        except Exception:
            pass
        deezer_track = deezer_search_track(artist, title)
        if not deezer_track:
            failed += 1
            continue
        track_id = str(deezer_track.get("id", ""))
        if not track_id:
            failed += 1
            continue
        artist_obj = deezer_track.get("artist") or {}
        album_obj = deezer_track.get("album") or {}
        track_hint = {
            "id": track_id, "name": deezer_track.get("title", ""),
            "artist": artist_obj.get("name", ""), "album": album_obj.get("title", ""),
            "album_art": (album_obj.get("cover_xl") or album_obj.get("cover_big")
                          or album_obj.get("cover_medium")),
        }
        try:
            if get_duplicate_download_reason(track_id, provider, location,
                                             output_format,
                                             navidrome_library_path=navidrome_path):
                skipped += 1
                continue
            track_for_queue = _resolve_track_for_queue(track_id, provider, track_hint)
            payload_extra = {
                "provider": provider, "record_track_id": track_id,
                "location": location, "video_id": None,
                "output_format": output_format, "audio_quality": None,
                "metadata_provider": provider, "max_retries": 0,
                "navidrome_library_path": navidrome_path,
                "track": track_for_queue,
                # BESTEHENDE sync-Marker — finished-tracks filtert exakt darauf.
                "plugin_sync_run_id": run_id,
                "plugin_sync_playlist_name": req.playlist_name,
                "plugin_sync_navidrome_user": req.navidrome_user,
            }
            upsert_job(track_id, status="queued",
                       message=f"Download queued (lbweekly={req.playlist_name})",
                       progress=0, stage="queued", payload=payload_extra)
            queued += 1
        except Exception as e:
            failed += 1
            print(f"[plugin-lbweekly] queue fail {track_id}: {e}")

    print(f"[plugin-lbweekly] {req.playlist_name!r} done in {_now_ms()-started}ms — "
          f"pool={len(items)} queued={queued} skipped={skipped} failed={failed}")


def occ_tag(occurrence: int) -> str:
    return "cur" if occurrence == 0 else f"occ{occurrence}"
```

- [ ] **Step 6: Endpoint hinzufügen** (nach `plugin_mix_discovery`, ~`app.py:4160`)

```python
@app.post("/api/plugin/lbweekly/discovery")
async def plugin_lbweekly_discovery(
    req: PluginLbWeeklyDiscoveryRequest,
    background_tasks: BackgroundTasks,
    _: None = Depends(require_token),
):
    """Plugin-Trigger für eine LB-'createdfor'-Playlist. Queued fehlende
    Tracks im Hintergrund (mit sync-Markern) und liefert synchron die
    bereits-in-Library-Liste für die Plugin-Build-Phase."""
    background_tasks.add_task(_run_plugin_lbweekly_discovery, req)
    existing = _check_lbweekly_tracks_in_library(req)
    return {"started": True,
            "message": "lbweekly discovery + queueing missing tracks in background",
            "existing": existing}
```

- [ ] **Step 7: Tests + py_compile → PASS**

Run: `cd backend && /tmp/tonus-test-venv/bin/python -m py_compile app.py && /tmp/tonus-test-venv/bin/python -m pytest tests/test_plugin_lbweekly.py -q`
Expected: 2 passed

- [ ] **Step 8: Commit**

```bash
git add backend/app.py backend/tests/test_plugin_lbweekly.py
git commit -m "feat(plugin-api): /api/plugin/lbweekly/discovery — LB createdfor playlist as source

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: artist-radio-Discovery entfernen (Backend)

**Files:**
- Modify: `backend/app.py` — `_run_plugin_sync` (~`app.py:3929-4115`), `POST /api/plugin/sync` (~`app.py:4117-4129`), `PluginSyncRequest` (~`app.py:675-702`)
- Modify: `backend/services/discovery.py` — `discover_via_artist_radio` (~`discovery.py:435-485`) + nur noch dort genutzte Helfer (`lb_top_artists`, `lb_listened_track_keys`, `deezer_artist_radio`, `deezer_search_artist_id`) **nur wenn nirgends sonst referenziert**
- Test: bestehende Suite (`test_youtube_helpers.py` etc.) muss grün bleiben

**Interfaces:**
- Consumes: nichts Neues.
- Produces: `/api/plugin/sync` + `_run_plugin_sync` + `discover_via_artist_radio` existieren nicht mehr. `sync-status` (`_plugin_sync_state`) bleibt (wird von LB-Weekly-Runs nicht mehr befüllt — Feld bleibt für Kompat, Plugin liest es nicht mehr).

- [ ] **Step 1: Referenz-Check** (was hängt noch dran?)

Run:
```bash
cd backend && grep -rn "discover_via_artist_radio\|_run_plugin_sync\|/api/plugin/sync\b\|lb_top_artists\|deezer_artist_radio\|lb_listened_track_keys" --include="*.py" .
```
Expected: nur Treffer in `app.py` (`_run_plugin_sync`, Endpoint) und `services/discovery.py` (Definitionen). Jeder Treffer in einem **anderen** Modul → diesen Helfer NICHT löschen.

- [ ] **Step 2: Endpoint + Background-Task entfernen**

Lösche in `app.py`: den `@app.post("/api/plugin/sync")`-Block (`:4117-4129`), die Funktion `_run_plugin_sync` (`:3929-4115`), und die Klasse `PluginSyncRequest` (`:675-702`). Den globalen State `_plugin_sync_state` + `/api/plugin/sync-status` **behalten** (read-only-Status, kein Bezug zu artist-radio-Quelle).

- [ ] **Step 3: artist-radio-Quelle in discovery.py entfernen**

Lösche `discover_via_artist_radio` (`:435-485`). Lösche `lb_top_artists`, `_lb_top_artists_via_listens`, `lb_listened_track_keys`, `deezer_artist_radio`, `deezer_search_artist_id` **nur**, falls Step 1 keine anderen Nutzer fand. `lb_genre_top_recordings` + `lb_playlist_tracks` + `deezer_search_track` BLEIBEN (Mix + LB-Weekly).

- [ ] **Step 4: py_compile + volle Suite → PASS**

Run: `cd backend && /tmp/tonus-test-venv/bin/python -m py_compile app.py services/discovery.py && /tmp/tonus-test-venv/bin/python -m pytest -q`
Expected: alle grün (die neuen LB-Weekly-Tests + die Bestandstests; keine `_run_plugin_sync`-Importfehler)

- [ ] **Step 5: Commit**

```bash
git add backend/app.py backend/services/discovery.py
git commit -m "refactor(plugin-api): remove artist-radio discovery (replaced by LB-Weekly)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Phase 2 — Plugin (Go/TinyGo → WASM)

> Go-Plugin-Code lässt sich nicht sinnvoll unit-testen (Extism-Host-Funktionen). Verifikation = `make` (TinyGo-Build muss durchlaufen) + Live-Test in Navidrome (Cron vorstellen).

### Task 4: Config-Schema umbauen (artist-radio raus, LB-Weekly-Toggle rein)

**Files:**
- Modify: `tonus-navidrome-plugin/manifest.json` (`config.schema.properties`, `config.uiSchema`)

**Interfaces:**
- Produces: pro User-Mapping ein Bool-Feld `lb_weekly_mirror` (Default `true`). Entfernte Felder: `top_artists`, `tracks_per_artist`, `max_queue_per_run`, per-User `playlist_name`.

- [ ] **Step 1: Schema editieren**

In `manifest.json`:
- Aus `config.schema.properties` entfernen: `top_artists`, `tracks_per_artist`, `max_queue_per_run`.
- In `config.schema.properties.users.items.properties` entfernen: `playlist_name`. Hinzufügen:
```json
"lb_weekly_mirror": {
  "type": "boolean",
  "title": "LB Weekly Mirror",
  "description": "Spiegelt die vier ListenBrainz 'Created For You'-Playlists (Weekly/Last Week's Exploration + Jams) als Navidrome-Playlists dieses Users und hält sie aktuell.",
  "default": true
}
```
- `version` auf `0.5.0` erhöhen.
- In `uiSchema`: die Controls für die drei globalen artist-radio-Felder + `playlist_name` entfernen, ein Control `#/properties/lb_weekly_mirror` im User-Detail ergänzen.

- [ ] **Step 2: JSON validieren**

Run: `cd tonus-navidrome-plugin && python3 -c "import json; json.load(open('manifest.json')); print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
cd tonus-navidrome-plugin
git add manifest.json
git commit -m "feat: config schema — LB-Weekly toggle, drop artist-radio tuning"
```

---

### Task 5: Job-Types + Client-Methode für LB-Weekly

**Files:**
- Modify: `tonus-navidrome-plugin/internal/tasks/tasks.go` (Job struct, JobType-Konstanten, `loadUsers`/`userMapping`)
- Modify: `tonus-navidrome-plugin/internal/client/tonus.go` (neue Methode `LbWeeklyDiscovery`)
- Create: `tonus-navidrome-plugin/internal/tasks/lbweekly.go` (Discovery-Task; Build = bestehender `runReconcile`)

**Interfaces:**
- Consumes: `client.New`, bestehender `runReconcile(j *Job)` (`tasks.go:141`+) — funktioniert bereits playlist_name-basiert über `cl.FinishedTracks(user, since, playlistName)`.
- Produces: `JobLbWeeklyDiscovery` JobType; `Job`-Felder `LbSourcePatch string`, `LbOccurrence int`; `client.LbWeeklyDiscovery(req) (*LbWeeklyDiscoveryResponse, error)` → `POST /api/plugin/lbweekly/discovery`; vier feste Slots (Konstante `LbWeeklySlots`).

- [ ] **Step 1: Slot-Konstanten + Job-Felder** (in `tasks.go`)

```go
// LbWeeklySlot beschreibt eine der vier gespiegelten LB-Playlists.
type LbWeeklySlot struct {
	PlaylistName string // Navidrome-Name
	SourcePatch  string // LB source_patch
	Occurrence   int    // 0 = aktuelle Woche, 1 = Vorwoche
}

// LbWeeklySlots — fixe Reihenfolge, je User gespiegelt.
var LbWeeklySlots = []LbWeeklySlot{
	{"Weekly Exploration", "weekly-exploration", 0},
	{"Weekly Jams", "weekly-jams", 0},
	{"Last Week's Exploration", "weekly-exploration", 1},
	{"Last Week's Jams", "weekly-jams", 1},
}
```
In `type Job struct` ergänzen:
```go
	LbSourcePatch string `json:"lb_source_patch,omitempty"`
	LbOccurrence  int    `json:"lb_occurrence,omitempty"`
```
In den `JobType`-Konstanten ergänzen: `JobLbWeeklyDiscovery`. In `userMapping` (loadUsers) das Feld `LbWeeklyMirror bool` mit JSON-Tag `lb_weekly_mirror` ergänzen.

- [ ] **Step 2: Client-Methode** (in `client/tonus.go`, analog `MixDiscovery`)

```go
type LbWeeklyDiscoveryRequest struct {
	NavidromeUser    string `json:"navidrome_user"`
	ListenBrainzUser string `json:"listenbrainz_user"`
	SourcePatch      string `json:"source_patch"`
	Occurrence       int    `json:"occurrence"`
	PlaylistName     string `json:"playlist_name"`
}

type LbWeeklyDiscoveryResponse struct {
	Started  bool                    `json:"started"`
	Existing []tasks.MixExistingTrack `json:"existing"`
}
// Reuse tasks.MixExistingTrack (tasks/mix.go:38) — Felder {subsonic_id,
// artist, title} matchen die existing[]-Items des Backends 1:1.

func (c *Client) LbWeeklyDiscovery(req LbWeeklyDiscoveryRequest) (*LbWeeklyDiscoveryResponse, error) {
	out := &LbWeeklyDiscoveryResponse{}
	if err := c.request("POST", "/api/plugin/lbweekly/discovery", req, out); err != nil {
		return nil, err
	}
	return out, nil
}
```
(Falls `ExistingTrack` noch nicht existiert: den Typ aus dem Mix-Pfad wiederverwenden — `MixExistingTrack` in `tasks/mix.go` — bzw. den dort vorhandenen Struct referenzieren.)

- [ ] **Step 3: Discovery-Task** (`tasks/lbweekly.go`)

```go
package tasks

import (
	"encoding/json"
	"fmt"
	"github.com/extism/go-pdk"
	"tonus-navidrome-plugin/internal/client"
)

// runLbWeeklyDiscovery triggert einen LB-Weekly-Slot: ruft das Backend,
// persistiert die existing-Liste im KVStore für die Build-Phase (Reuse von
// runReconcile, das playlist_name-basiert baut).
func runLbWeeklyDiscovery(j *Job) (string, error) {
	cl := client.New(j.TonusURL, j.TonusToken)
	h, err := cl.Health()
	if err != nil {
		return "", fmt.Errorf("lbweekly %q/%q: health: %w", j.NavidromeUser, j.PlaylistName, err)
	}
	if h.AuthRequired && j.TonusToken == "" {
		return fmt.Sprintf("lbweekly %q: backend requires token but none configured", j.NavidromeUser), nil
	}
	resp, err := cl.LbWeeklyDiscovery(client.LbWeeklyDiscoveryRequest{
		NavidromeUser:    j.NavidromeUser,
		ListenBrainzUser: j.ListenBrainzUser,
		SourcePatch:      j.LbSourcePatch,
		Occurrence:       j.LbOccurrence,
		PlaylistName:     j.PlaylistName,
	})
	if err != nil {
		return "", fmt.Errorf("lbweekly %q/%q: %w", j.NavidromeUser, j.PlaylistName, err)
	}
	// existing-Liste im KVStore ablegen (Build-Phase liest sie); exakt wie
	// runMixDiscovery (mix.go:95-103): json.Marshal + host.KVStoreSet.
	kvKey := fmt.Sprintf("lbweekly:%s:%s", j.NavidromeUser, j.PlaylistName)
	if data, mErr := json.Marshal(resp.Existing); mErr == nil {
		if err := host.KVStoreSet(kvKey, data); err != nil {
			pdk.Log(pdk.LogWarn, fmt.Sprintf("lbweekly KVStoreSet %s: %s", kvKey, err.Error()))
		}
	}
	pdk.Log(pdk.LogInfo, fmt.Sprintf("lbweekly %q/%q accepted: existing=%d",
		j.NavidromeUser, j.PlaylistName, len(resp.Existing)))
	return "ok", nil
}
```
(`host` ist `github.com/extism/go-pdk`-Host-Import wie in `mix.go`. KV-Key-Format `lbweekly:<user>:<playlist>` — falls `runReconcile` existing aus KV liest, denselben Key-Stil dort spiegeln.)

- [ ] **Step 4: Build via bestehendem `runReconcile`**

Kein neuer Build-Task. `runReconcile(j)` baut bereits die Subsonic-Playlist aus `cl.FinishedTracks(j.NavidromeUser, 60, j.PlaylistName)` + den KVStore-existing. Für LB-Weekly wird derselbe Reconcile-Job pro Slot enqueued (siehe Task 6). Sicherstellen, dass `runReconcile` den KVStore-Key-Stil von Step 3 liest, falls es existing aus KV zieht — sonst genügt der finished-tracks-Pfad allein.

- [ ] **Step 5: Build (TinyGo)**

Run: `cd tonus-navidrome-plugin && make`
Expected: erzeugt `plugin.wasm` ohne Fehler.

- [ ] **Step 6: Commit**

```bash
cd tonus-navidrome-plugin
git add internal/tasks/tasks.go internal/tasks/lbweekly.go internal/client/tonus.go
git commit -m "feat: LB-Weekly discovery job + client method"
```

---

### Task 6: Cron-Registrierung + Dispatch

**Files:**
- Modify: `tonus-navidrome-plugin/main.go` (`OnInit` Cron-Registrierung, `OnCallback` Enqueue, `OnTaskExecute` Dispatch, Schedule-Key-Prefixe)

**Interfaces:**
- Consumes: `LbWeeklySlots`, `JobLbWeeklyDiscovery`, `runLbWeeklyDiscovery`, `runReconcile`, `loadUsers().LbWeeklyMirror`.
- Produces: globaler `cron_expression` enqueued pro User mit `LbWeeklyMirror==true` vier (Discovery+Reconcile)-Paare — eines je Slot.

- [ ] **Step 1: OnCallback — globaler Cron enqueued LB-Weekly statt artist-radio**

Im `OnCallback`-Zweig für `scheduleName` (globaler Cron): den bisherigen `JobTriggerSync`+`JobReconcile`-Enqueue ersetzen durch eine Schleife über Users mit `LbWeeklyMirror` und `LbWeeklySlots`:
```go
for _, u := range users {
	if u.NavidromeUsername == "" || !u.LbWeeklyMirror {
		continue
	}
	for _, slot := range tasks.LbWeeklySlots {
		base := tasks.Job{
			NavidromeUser:    u.NavidromeUsername,
			ListenBrainzUser: u.ListenBrainzUsername,
			PlaylistName:     slot.PlaylistName,
			LbSourcePatch:    slot.SourcePatch,
			LbOccurrence:     slot.Occurrence,
			TonusURL:         tonusURL,
			TonusToken:       tonusToken,
		}
		disc := base; disc.Type = tasks.JobLbWeeklyDiscovery
		rec := base; rec.Type = tasks.JobReconcile
		enqueue(disc)  // gleicher Enqueue-Helfer wie bisher
		enqueue(rec)   // Reconcile zieht inzwischen fertige Tracks nach (trickle-in)
	}
}
```

- [ ] **Step 2: OnTaskExecute — Dispatch ergänzen**

Im Type-Switch: `case tasks.JobLbWeeklyDiscovery: return runLbWeeklyDiscovery(&j)`. `JobReconcile` bleibt unverändert (baut playlist_name-basiert).

- [ ] **Step 3: Tote artist-radio-Pfade entfernen**

`JobTriggerSync` + `runTriggerSync` + die `TopArtists/TracksPerArtist/MaxQueuePerRun`-Felder entfernen (werden nicht mehr enqueued). `client.TriggerSync` + `MixDiscoveryRequest`-Bezug nur, falls nicht vom Mix-Pfad genutzt.

- [ ] **Step 4: Build (TinyGo) + Vet**

Run: `cd tonus-navidrome-plugin && go vet ./... && make`
Expected: Build grün, `plugin.wasm` aktualisiert.

- [ ] **Step 5: Commit**

```bash
cd tonus-navidrome-plugin
git add main.go internal/tasks/tasks.go internal/client/tonus.go
git commit -m "feat: global cron mirrors 4 LB-Weekly playlists per user; drop artist-radio jobs"
```

---

### Task 7: Release Plugin + Backend

- [ ] **Step 1: Backend-Release** — CHANGELOG-Eintrag (Feature: LB-Weekly-Mirror ersetzt artist-radio), dev→main PR, Tag (z.B. `v0.6.0`, da Feature + Plugin-API-Bruch), GHCR-Build.
- [ ] **Step 2: Plugin-Release** — Tag `v0.5.0` im `tonus-navidrome-plugin`-Repo pushen → `release.yml`-Action baut `tonus-navidrome-plugin.ndp`.

---

## Verification (end-to-end, auf dem Homeserver)

1. **Backend deployen:** `cd /opt/docker_compose_files/tonus && sudo docker compose pull tonus && sudo docker compose up -d` → `docker logs tonus` zeigt sauberen Boot (kein `_run_plugin_sync`-Importfehler).
2. **Plugin installieren:** neues `.ndp` in Navidrome (`/data/plugins`), Settings: `lb_weekly_mirror` an, artist-radio-Felder sind weg.
3. **Discovery-Smoke (kein Worker nötig):**
   ```bash
   curl -s -X POST -H "Authorization: Bearer <PAT>" -H "Content-Type: application/json" \
     -d '{"navidrome_user":"admin","listenbrainz_user":"madmax1301","source_patch":"weekly-exploration","occurrence":0,"playlist_name":"Weekly Exploration"}' \
     http://192.168.1.6:8088/api/plugin/lbweekly/discovery | python3 -m json.tool
   ```
   Erwartung: `started:true`, `existing:[…]` (bereits vorhandene Tracks mit Subsonic-IDs); im Hintergrund werden fehlende gequeued (Queue zeigt `Plugin · admin → Playlist · Weekly Exploration`).
4. **Build-Test:** Plugin-Cron auf die nächste Minute stellen → nach ~2 min erscheinen in Navidrome bis zu vier Playlists („Weekly Exploration", „Weekly Jams", „Last Week's Exploration", „Last Week's Jams"), owned by admin, gefüllt mit den schon-vorhandenen + inzwischen-fertigen Tracks. Cron zurückstellen (`7 23 * * *`).
5. **Update-Test (replace-in-place):** zweiter Lauf dupliziert nicht; sobald LB eine neue Woche generiert, zieht der nächste Lauf den neuen Inhalt (alte Playlist wird ersetzt).
6. **Regression:** `finished-tracks` für `playlist_name="Weekly Exploration"` liefert nur die Tracks dieser Playlist (Marker-Isolation gegen die anderen drei Slots).
