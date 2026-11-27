"""Import-Endpoints: CSV-Import, Spotify-History, Import-Jobs
(Status, Ergebnis, Abbruch, Löschen, „alle in die Queue“).

Die eigentliche Arbeit machen die Import-Worker (utils/worker.py); diese
Routen legen Jobs an und lesen ihren Zustand aus jobs.db.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

import config
from core import (
    _clamp_download_retries,
    _rl_csv_import,
    _rl_spotify_history,
    _slim_track_for_queue,
    physical_track_file_exists,
    resolve_metadata_provider,
    resolve_navidrome_library_path_optional,
)
from utils.auth import require_token
from utils.job_store import (
    _db,
    _now_ms,
    count_import_results,
    get_import_job,
    get_import_results,
    insert_import_results,
    upsert_import_job,
)

router = APIRouter()


class CsvImportRequest(BaseModel):
    csv_text: str
    provider: Optional[str] = None
    limit: Optional[int] = 5  # Deezer search results per track, max 5
    # Optional: original-Filename für UI-Anzeige (Tab zeigt sonst nur job_id).
    # Liefert das Frontend bei File-Upload mit; bei Text-Paste bleibt's null.
    filename: Optional[str] = None
    # Mode-Switch (2026-05-10):
    #   None / "full"          → klassischer Bulk-Import: Phase 0..5
    #   "playlist_sync"        → Library-Match + Playlist-Reconcile only.
    #                            Skip Provider-Phase + Download. Use case:
    #                            User hat Tracks bereits via Bulk-Import,
    #                            will jetzt nur die Playlist-Memberships aus
    #                            einer CSV (TuneMyMusic / Spotify-Export)
    #                            in seine Navidrome-Playlists einreihen.
    mode: Optional[str] = None


@router.post("/api/import/csv")
async def import_csv(request: CsvImportRequest, _: None = Depends(require_token), __: None = Depends(_rl_csv_import)):
    """
    CSV-Import (persistent): speichert Job in SQLite, Worker holt ihn ab.
    Gibt sofort eine job_id zurück — Status unter /api/import/jobs/{job_id}/status pollbar.
    """
    import csv, io, re as _re

    provider = resolve_metadata_provider(request.provider)
    search_limit = max(1, min(request.limit or 3, 5))
    lines = request.csv_text.strip().splitlines()

    # Parse CSV to list of (artist, title, raw_line).
    # Delimiter-Heuristik: probiere die üblichen Trennzeichen und nimm den, der über
    # die ersten Zeilen die meisten Spalten ergibt. Frühere Logik prüfte len(rows)<2,
    # was bei einer ,-getrennten Datei mit ;-Versuch nicht aufschlug (jede Zeile blieb
    # als 1-Spalten-Eintrag stehen, weil 778 Zeilen >= 2 sind).
    text = request.csv_text.strip()
    rows: list = []
    best_cols = 0
    for delim in (",", ";", "\t", "|"):
        try:
            candidate = list(csv.reader(io.StringIO(text), delimiter=delim))
            sample = candidate[:5]
            avg_cols = max((len(r) for r in sample), default=0)
            if avg_cols > best_cols:
                best_cols = avg_cols
                rows = candidate
        except Exception:
            continue
    if not rows:
        rows = [[line.strip()] for line in lines]

    # Auto-detect header: look for "artist", "track"/"title" und (Phase I)
    # optional "playlist"-Spalte. TuneMyMusic-Exports haben standardmäßig
    # eine "Playlist Name"-Spalte; andere Exports haben sie nicht — dann
    # bleibt col_playlist=None und der Track hat keine playlist_names.
    col_artist, col_title = 0, 1
    col_playlist: Optional[int] = None
    if rows and len(rows[0]) >= 2:
        first = [c.strip().lower().lstrip('\ufeff') for c in rows[0]]
        artist_cols = [i for i, c in enumerate(first) if 'artist' in c]
        title_cols  = [i for i, c in enumerate(first) if 'track' in c or 'title' in c or 'name' in c]
        playlist_cols = [i for i, c in enumerate(first) if 'playlist' in c]
        if artist_cols and title_cols:
            col_artist, col_title = artist_cols[0], title_cols[0]
            if playlist_cols:
                col_playlist = playlist_cols[0]
            rows = rows[1:]
    elif rows and any(kw in (rows[0][0].strip().lower().lstrip('\ufeff')) for kw in ['track', 'title', 'name']):
        col_artist, col_title = 1, 0
        rows = rows[1:]

    parsed = []
    for row in rows:
        if not row:
            continue
        artist, title, playlist_cell = "", "", ""
        max_col = max(col_artist, col_title, col_playlist if col_playlist is not None else 0)
        if len(row) > max_col:
            artist = row[col_artist].strip().strip('"').strip("'") if col_artist < len(row) else ""
            title  = row[col_title].strip().strip('"').strip("'")  if col_title  < len(row) else ""
            if col_playlist is not None and col_playlist < len(row):
                playlist_cell = row[col_playlist].strip().strip('"').strip("'")
        elif len(row) == 1:
            parts = _re.split(r"\s*[-–—]\s*", row[0].strip(), maxsplit=1)
            if len(parts) == 2:
                artist = parts[0].strip()
                title = parts[1].strip()
            else:
                title = parts[0].strip()
        if title:
            # Phase I: Multi-Playlist-Membership in einer Zelle. TuneMyMusic
            # liefert pro Track einen einzelnen Playlist-Namen, aber wir
            # parsen comma- oder semicolon-separated falls Tools mehrere
            # Memberships in einer Zelle haben.
            playlist_names = [
                p.strip() for p in _re.split(r"[,;]", playlist_cell) if p.strip()
            ] if playlist_cell else []
            parsed.append({
                "artist": artist,
                "title": title,
                "raw": row[0].strip() if row else f"{artist} {title}".strip(),
                "playlist_names": playlist_names,
            })

    # Empty-Guard: wenn der Parser nichts findet, sofort 400 zurück statt
    # einen Stub-Job in DB anzulegen — sonst sieht User nur kryptischen
    # Worker-State ("No pending items found") in der UI.
    if not parsed:
        raise HTTPException(
            status_code=400,
            detail=(
                "CSV-Inhalt konnte nicht erkannt werden — bitte prüfe dass die Datei "
                "Spalten 'Artist' + 'Track Name' enthält (optional 'Playlist Name'). "
                f"Empfangen: {len(request.csv_text)} Bytes, {len(rows)} Roh-Zeilen, "
                f"0 valide Tracks geparsed."
            ),
        )

    # Eindeutige job_id auf Basis der Wall-Clock-Zeit (import_jobs hat keine numeric id-Spalte)
    import time as _time
    from utils.job_store import _db as _csv_db
    job_id = f"csv-{int(_time.time() * 1000)}"

    # Falls eine ältere Session denselben job_id hinterlassen hat (extrem unwahrscheinlich,
    # aber wir wollen keine fremden Results mit den neuen vermischen), zuerst alle Reste löschen.
    conn = _csv_db()
    try:
        conn.execute("DELETE FROM import_results WHERE job_id = ?", (job_id,))
        conn.execute("DELETE FROM import_jobs    WHERE job_id = ?", (job_id,))
        conn.commit()
    finally:
        conn.close()

    # Worker-Payload: provider + search_limit als sauberes JSON in eigener
    # Spalte (vorher: Hijack des `message`-Felds als "provider|limit|pending_raw"-
    # String, was bei verzögertem Worker-Pickup als Status-Message in der UI
    # auftauchte). Filename optional für UI-Anzeige.
    #
    # Mode-Switch + Source-Marker:
    #   - mode="playlist_sync" → Worker skipped Provider/Download, macht nur
    #     Library-Match + Playlist-Reconcile. source="playlist_sync" damit
    #     Frontend Tab-Label "Playlist-Sync" rendert.
    #   - mode=None / "full"   → Default Bulk-Import-Pipeline. source="csv".
    import json as _json
    mode = (request.mode or "full").strip().lower()
    if mode not in ("full", "playlist_sync"):
        mode = "full"
    payload_dict: Dict[str, Any] = {"provider": provider, "search_limit": search_limit}
    if mode == "playlist_sync":
        payload_dict["mode"] = "playlist_sync"
        payload_dict["source"] = "playlist_sync"
    payload = _json.dumps(payload_dict)
    fname = (request.filename or "").strip() or None

    # source-Spalte ist Lane-Routing für die 4-Lane-Worker-Architektur
    # (csv / spotify_history / playlist_sync). mode steuert ob Provider/
    # Download-Phasen ausgeführt werden.
    job_source = "playlist_sync" if mode == "playlist_sync" else "csv"
    # Pre-stats für Result-Card: wieviele unique Playlists wurden in der CSV
    # gefunden. Wird vom Worker als "playlists_total" durchgereicht und vom
    # Frontend als "Playlists in CSV" angezeigt — User soll auf einen Blick
    # sehen ob sein Export überhaupt Playlist-Spalten hatte.
    playlists_in_csv = len({
        name for p in parsed for name in (p.get("playlist_names") or [])
    })
    upsert_import_job(
        job_id,
        status="queued",
        total=len(parsed),
        message=f"Queued — waiting for worker ({len(parsed)} tracks)",
        filename=fname,
        payload_json=payload,
        mode=mode,
        source=job_source,
        playlists_total=playlists_in_csv,
    )

    # Store parsed items in a temp table so the worker can read them
    # (use import_results with result_type='pending' as staging).
    # Phase I: playlist_names werden mit gespeichert, Worker kann sie
    # zu library_match/matched-Buckets durchreichen.
    insert_import_results(job_id, "pending_raw", [
        {
            "original": p["raw"],
            "requested_artist": p["artist"],
            "requested_title": p["title"],
            "playlist_names": p.get("playlist_names") or [],
        }
        for p in parsed
    ])

    return {"status": "queued", "job_id": job_id, "total": len(parsed), "filename": fname}


class SpotifyHistoryImportRequest(BaseModel):
    """Phase I.2 — Spotify Extended Streaming History Import.

    files: Liste von parsed JSON-Arrays (jedes Array eine
    Streaming_History_Audio_*.json). Frontend liest Files via FileReader,
    JSON.parse und sendet sie als Top-Level-Array damit Backend keinen
    String→JSON-Roundtrip mehr braucht.

    Filter-Defaults sind Spotifys eigene "Listened-To"-Heuristik (≥30s)
    plus Playlist-Counts pro Jahr/Monat.
    """
    files: List[List[Dict[str, Any]]]
    provider: Optional[str] = None
    limit: Optional[int] = 3
    min_ms_played: Optional[int] = 0
    min_play_count: Optional[int] = 1
    date_from: Optional[str] = None  # YYYY-MM-DD
    date_to: Optional[str] = None
    auto_playlist_year: Optional[bool] = True
    auto_playlist_month: Optional[bool] = True
    playlist_prefix: Optional[str] = "Spotify History"
    filename: Optional[str] = None  # nur für UI-Anzeige (z.B. "Spotify History · 24 Files")


@router.post("/api/import/spotify-history")
async def import_spotify_history(req: SpotifyHistoryImportRequest, _: None = Depends(require_token), __: None = Depends(_rl_spotify_history)):
    """Importiert eine oder mehrere Spotify-Extended-Streaming-History-JSONs.

    Pipeline-Integration:
    1. spotify_history.parse_streaming_history() aggregiert pro (artist, title)
       und leitet Year/Month-Auto-Playlists ab
    2. Aggregat wird in import_jobs/import_results eingereiht
       (gleiches Schema wie /api/import/csv) — der existing Import-Worker
       übernimmt von hier (Phase 0 Library-Match, Phase 2 Provider-Lookup)
    3. import_playlist_names werden via Phase I durch alle Buckets
       durchgereicht und beim queue_all + Reconcile in Subsonic-Playlists
       übersetzt
    """
    from services.spotify_history import parse_streaming_history
    import json as _json
    import time as _time
    from utils.job_store import _db as _hist_db

    provider = resolve_metadata_provider(req.provider)
    search_limit = max(1, min(req.limit or 3, 5))

    # Aggregation. parse_streaming_history is pure — kein DB-Touch, kein
    # Side-Effect — also sicher synchron im Request.
    # None-explizit-Check statt `or` — sonst macht User-eingabe 0 (alle Tracks
    # rein) silent zum Default 30000. Same für min_play_count.
    result = parse_streaming_history(
        req.files,
        min_ms_played=int(req.min_ms_played if req.min_ms_played is not None else 0),
        min_play_count=int(req.min_play_count if req.min_play_count is not None else 1),
        date_from=req.date_from,
        date_to=req.date_to,
        auto_playlist_year=bool(req.auto_playlist_year if req.auto_playlist_year is not None else True),
        auto_playlist_month=bool(req.auto_playlist_month if req.auto_playlist_month is not None else True),
        playlist_prefix=(req.playlist_prefix or "Spotify History").strip() or "Spotify History",
    )
    tracks = result["tracks"]
    stats = result["stats"]

    if not tracks:
        # Aggregat ist leer — keine Tracks die importierbar wären. Wir geben
        # die stats zurück damit User sieht warum (zu strenger Filter,
        # nur Podcasts in den Files, etc.) — aber kein import_job angelegt.
        return {
            "status": "empty",
            "message": "No tracks after filtering",
            "stats": stats,
        }

    job_id = f"spotify-hist-{int(_time.time() * 1000)}"

    # Cleanup analog zu /api/import/csv falls dieser job_id schon existiert
    conn = _hist_db()
    try:
        conn.execute("DELETE FROM import_results WHERE job_id = ?", (job_id,))
        conn.execute("DELETE FROM import_jobs    WHERE job_id = ?", (job_id,))
        conn.commit()
    finally:
        conn.close()

    payload = _json.dumps({"provider": provider, "search_limit": search_limit, "source": "spotify_history"})
    fname = (req.filename or "").strip() or f"Spotify History · {len(req.files)} Files"

    upsert_import_job(
        job_id,
        status="queued",
        total=len(tracks),
        message=f"Queued — waiting for worker ({len(tracks)} tracks from {stats['total_events']} events)",
        filename=fname,
        payload_json=payload,
        mode="full",
        source="spotify_history",
    )

    # Phase I-Schema: artist/title/playlist_names werden direkt durchgereicht.
    # Worker pickt die als pending_raw und läuft die ganze Pipeline (Phase 0
    # Library-Match → Phase 2 Provider-Lookup → Phase 3 materialize).
    insert_import_results(job_id, "pending_raw", [
        {
            "original": f"{t['artist']} - {t['title']}",
            "requested_artist": t["artist"],
            "requested_title": t["title"],
            "playlist_names": t.get("playlist_names") or [],
        }
        for t in tracks
    ])

    return {
        "status": "queued",
        "job_id": job_id,
        "total": len(tracks),
        "filename": fname,
        "stats": stats,
    }


@router.get("/api/import/jobs/{job_id}/status")
async def import_job_status(job_id: str, _: None = Depends(require_token)):
    """Poll CSV/JSON import progress (aus SQLite). Liefert:
    - library_match_count (Phase H) damit Frontend den "in library"-Counter live zeigen kann
    - recovery_total + recovery_recovered (Phase 2.5) damit Frontend rechecked-Stats anzeigt
    - source ("csv" | "spotify_history") aus payload_json damit Frontend
      Tab-Label korrekt rendert (vorher hardcoded "CSV" auch bei JSON)
    """
    import json as _json
    job = get_import_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="CSV import job not found")
    counts = count_import_results(job_id)
    # Source aus payload_json ableiten — bei legacy-Jobs ohne source-Feld
    # default "csv". Spotify-History setzt {"source": "spotify_history"}.
    source = "csv"
    payload_raw = job.get("payload_json")
    if payload_raw:
        try:
            payload = _json.loads(payload_raw)
            if isinstance(payload, dict) and isinstance(payload.get("source"), str):
                source = payload["source"]
        except Exception:
            pass
    return {
        "status": job["status"],
        "total": job["total"],
        "processed": job["processed"],
        "found": job["found"],
        "not_found": job["not_found"],
        "library_match_count": counts.get("library_match", 0),
        "recovery_total": job.get("recovery_total", 0) or 0,
        "recovery_recovered": job.get("recovery_recovered", 0) or 0,
        "phase0_progress": job.get("phase0_progress", 0) or 0,
        "playlists_total": job.get("playlists_total", 0) or 0,
        "playlists_synced": job.get("playlists_synced", 0) or 0,
        "playlist_tracks_added": job.get("playlist_tracks_added", 0) or 0,
        "playlist_queue_tagged": job.get("playlist_queue_tagged", 0) or 0,
        "source": source,
        "message": job.get("message", ""),
        "filename": job.get("filename"),
    }


@router.post("/api/import/jobs/{job_id}/cancel")
async def import_job_cancel(job_id: str, _: None = Depends(require_token)):
    """User-Cancel via UI. Setzt status='cancelled' in der DB; der Worker
    pollt diesen Status zwischen Phase-Schritten und beendet den Job
    sauber (max 1 Phase-Step Latenz). Idempotent: bereits terminale Jobs
    (completed/error/cancelled) returnen `cancelled: false` ohne Fehler."""
    from utils.job_store import cancel_import_job
    job = get_import_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="CSV import job not found")
    cancelled = cancel_import_job(job_id)
    return {"ok": True, "cancelled": cancelled, "previous_status": job.get("status")}


@router.get("/api/import/jobs/{job_id}/result")
async def import_job_result(
    job_id: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    _: None = Depends(require_token),
):
    """CSV-Import-Ergebnisse paginiert aus SQLite."""
    job = get_import_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="CSV import job not found")
    if job["status"] != "completed":
        raise HTTPException(status_code=400, detail="CSV import not yet completed")

    results = get_import_results(job_id, offset=offset, limit=limit)
    counts = count_import_results(job_id)
    return {
        "total": job["total"],
        "found": counts["matched"],
        "not_found": counts["unmatched"],
        "library_match_count": counts.get("library_match", 0),
        "matched": results["matched"],
        "unmatched": results["unmatched"],
        "library_match": results.get("library_match", []),
    }


@router.delete("/api/import/jobs/{job_id}")
async def delete_import_job(job_id: str, _: None = Depends(require_token)):
    """Löscht einen CSV-Import-Job inkl. aller matched/unmatched Results.

    Greift auf import_jobs UND import_results — der Job verschwindet
    komplett, kann danach mit derselben job_id nicht mehr abgerufen werden.
    Idempotent: löschen eines nicht-existierenden Jobs ist OK (deleted=0).
    """
    conn = _db()
    try:
        conn.execute("DELETE FROM import_results WHERE job_id = ?", (job_id,))
        cur = conn.execute("DELETE FROM import_jobs WHERE job_id = ?", (job_id,))
        n = cur.rowcount or 0
        conn.commit()
        return {"ok": True, "deleted_jobs": n, "job_id": job_id}
    finally:
        conn.close()


class CsvQueueAllRequest(BaseModel):
    provider: Optional[str] = None
    location: Optional[str] = "local"
    format: Optional[str] = None
    quality: Optional[str] = None
    max_retries: Optional[int] = 0
    navidrome_library: Optional[str] = None


@router.post("/api/import/jobs/{job_id}/queue-all")
async def csv_queue_all(job_id: str, req: CsvQueueAllRequest, _: None = Depends(require_token)):
    """Schreibe ALLE matched Tracks aus einem CSV-Import in die Download-Queue.

    Ersetzt den browserseitigen "for each downloadTrack(...)"-Loop, der bei großen
    Imports (30k+ Tracks) entweder am Pagination-Limit hing oder den Browser mit
    tausenden Einzelrequests blockiert hätte. Hier wird alles serverseitig in einer
    DB-Schleife in download_jobs eingefügt.
    """
    import json as _json

    import_job = get_import_job(job_id)
    if not import_job:
        raise HTTPException(status_code=404, detail="CSV import job not found")
    if import_job["status"] != "completed":
        raise HTTPException(status_code=400, detail="CSV import not yet completed")

    provider = resolve_metadata_provider(req.provider)
    location = req.location if req.location in ["local", "navidrome"] else "local"
    output_format = req.format or config.OUTPUT_FORMAT
    audio_quality = req.quality
    max_retries = _clamp_download_retries(req.max_retries)
    navidrome_path: Optional[str] = None
    if location == "navidrome":
        navidrome_path = resolve_navidrome_library_path_optional(req.navidrome_library)

    # ----- Schritt 1: alle matched Track-JSONs + playlist_names aus DB lesen + parsen -----
    # Phase I: playlist_names_json wird mitgelesen damit der Download-Job
    # den Playlist-Marker `import_playlist_names` im Payload trägt — das
    # ist die Quelle für `_reconcile_imported_playlists` nach Download.
    conn = _db()
    try:
        rows = conn.execute(
            "SELECT track_json, playlist_names_json FROM import_results WHERE job_id = ? AND result_type = 'matched'",
            (job_id,),
        ).fetchall()
    finally:
        conn.close()

    candidates: list = []  # list of (track_id, track_dict, playlist_names)
    errors = 0
    for row in rows:
        if not row["track_json"]:
            errors += 1
            continue
        try:
            track = _json.loads(row["track_json"])
        except (_json.JSONDecodeError, TypeError):
            errors += 1
            continue
        tid = str(track.get("id") or "")
        if not tid:
            errors += 1
            continue
        try:
            playlist_names = _json.loads(row["playlist_names_json"]) if row["playlist_names_json"] else []
        except Exception:
            playlist_names = []
        candidates.append((tid, track, playlist_names))

    # ----- Schritt 2: bulk-Lookup auf existierende download_jobs (statt N Einzelqueries) -----
    skipped_dup = 0
    in_flight_ids: set = set()
    completed_provider_pairs: set = set()
    if candidates:
        conn = _db()
        try:
            track_ids = [tid for tid, _, _ in candidates]
            # SQLite-Parameterlimit (~999) → in 500er-Chunks aufteilen
            chunk = 500
            for i in range(0, len(track_ids), chunk):
                slice_ids = track_ids[i:i + chunk]
                placeholders = ",".join("?" * len(slice_ids))
                cur = conn.execute(
                    f"SELECT job_id FROM download_jobs WHERE status IN ('queued', 'processing') AND job_id IN ({placeholders})",
                    slice_ids,
                )
                in_flight_ids.update(r["job_id"] for r in cur.fetchall())
                cur = conn.execute(
                    f"SELECT track_id FROM completed_track_downloads WHERE provider = ? AND track_id IN ({placeholders})",
                    [provider, *slice_ids],
                )
                completed_provider_pairs.update(r["track_id"] for r in cur.fetchall())
        finally:
            conn.close()

    # ----- Schritt 3: candidates filtern + Insert-Tuples vorbereiten -----
    now = _now_ms()
    msg = f"Download queued for {'local downloads folder' if location == 'local' else 'Navidrome server'}"
    payload_template = {
        "provider": provider,
        "location": location,
        "video_id": None,
        "output_format": output_format,
        "audio_quality": audio_quality,
        "metadata_provider": provider,
        "max_retries": max_retries,
        "navidrome_library_path": navidrome_path,
    }

    insert_tuples: list = []
    queued = 0
    for tid, track, playlist_names in candidates:
        if tid in in_flight_ids or tid in completed_provider_pairs:
            skipped_dup += 1
            continue
        # File-System-Existenz-Check: gezielt mit dem bereits geladenen track_info,
        # damit keine Provider-HTTP-Calls erfolgen. Wir tragen den File-Check als
        # "best effort" mit ein — bei tausenden Files ist Stat schnell, aber wir
        # wollen es trotzdem nicht für jeden überflüssigen Track ausführen.
        try:
            if physical_track_file_exists(track, location, output_format, navidrome_path):
                skipped_dup += 1
                continue
        except Exception:
            pass

        track_for_queue = _slim_track_for_queue(track)
        payload = dict(payload_template, record_track_id=tid, track=track_for_queue)
        # Phase I: import_playlist_names im Payload setzen wenn der Track
        # auf mindestens einer Playlist im Source-CSV stand. Reconcile
        # (siehe _reconcile_imported_playlists) liest diesen Marker.
        if playlist_names:
            payload["import_playlist_names"] = playlist_names
        insert_tuples.append((
            tid, "queued", "queued", 0, msg,
            None, None, None,  # file_path, download_url, error
            None,               # album_id
            _json.dumps(payload),
            now, now,
        ))
        queued += 1

    # ----- Schritt 4: Bulk-Insert in EINER Connection / EINER Transaction -----
    if insert_tuples:
        conn = _db()
        try:
            conn.executemany(
                """
                INSERT INTO download_jobs (
                    job_id, status, stage, progress, message,
                    file_path, download_url, error, album_id,
                    payload_json, created_at_ms, updated_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    status=excluded.status,
                    stage=excluded.stage,
                    progress=excluded.progress,
                    message=excluded.message,
                    payload_json=excluded.payload_json,
                    updated_at_ms=excluded.updated_at_ms
                """,
                insert_tuples,
            )
            conn.commit()
        finally:
            conn.close()

    # ----- Schritt 5: Library-Match-Tracks zu Playlists (Phase I-Edge-Case) -----
    # Library-Match-Tracks haben keinen Download-Job (Tracks sind ja schon
    # da), würden aber ohne diesen Schritt nie zu ihren Playlists hinzu-
    # gefügt. Job-scoped Reconcile direkt aus import_results.
    library_recon: Dict[str, int] = {"playlists": 0, "tracks_added": 0, "library_tracks_processed": 0}
    try:
        # Liegt noch beim Plugin-Code in app.py; lazy, sonst Zirkelimport.
        from app import _reconcile_import_library_matches
        library_recon = _reconcile_import_library_matches(job_id)
    except Exception as e:
        # Reconcile-Failure soll queue_all nicht killen — Tracks sind in der
        # Queue, das ist der wichtige Teil. Reconcile kann manuell oder
        # beim nächsten Plugin-Sync nachgezogen werden.
        print(f"[csv-queue-all] library-reconcile failed (non-fatal): {type(e).__name__}: {e}")

    return {
        "queued": queued,
        "skipped_duplicate": skipped_dup,
        "errors": errors,
        "total_matched": len(rows),
        "library_playlists_reconciled": library_recon,
    }
