import os
import sqlite3
import json
import time
from pathlib import Path
from typing import Any, Dict, Optional

import config

# Persistente App-Daten (Auth-DB, Queue-Jobs, Settings) leben getrennt vom
# Audio-Download-Verzeichnis. /app/data ist ein eigenes Bind-Mount-Volume in
# docker-compose.yml — überlebt damit jeden `docker compose build --no-cache`,
# während DOWNLOAD_DIR nur für temporäre Audio-Files (während Processing) ist.
# JOBS_DB_PATH überschreibbar via env für nicht-Docker-Setups.
JOBS_DB_PATH = os.getenv("JOBS_DB_PATH", "/app/data/jobs.db")
Path(os.path.dirname(JOBS_DB_PATH)).mkdir(parents=True, exist_ok=True)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(JOBS_DB_PATH, timeout=5, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, decl_sql: str) -> None:
    cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl_sql}")


def init_jobs_db() -> None:
    conn = _db()
    try:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS download_jobs (
            job_id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            stage TEXT,
            progress INTEGER,
            message TEXT,
            file_path TEXT,
            download_url TEXT,
            error TEXT,
            album_id TEXT,
            payload_json TEXT,
            created_at_ms INTEGER NOT NULL,
            updated_at_ms INTEGER NOT NULL
        )
        """)

        # If the table existed before album_id was added, migrate in-place.
        _ensure_column(conn, "download_jobs", "album_id", "TEXT")

        conn.execute("CREATE INDEX IF NOT EXISTS idx_download_jobs_status ON download_jobs(status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_download_jobs_updated ON download_jobs(updated_at_ms)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_download_jobs_album_id ON download_jobs(album_id)")

        conn.execute("""
        CREATE TABLE IF NOT EXISTS completed_track_downloads (
            track_id TEXT NOT NULL,
            provider TEXT NOT NULL,
            completed_at_ms INTEGER NOT NULL,
            PRIMARY KEY (track_id, provider)
        )
        """)

        # CSV Import — persistente Jobs (überleben Server-Neustarts)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS csv_import_jobs (
            job_id TEXT PRIMARY KEY,
            status TEXT NOT NULL DEFAULT 'queued',
            total INTEGER NOT NULL DEFAULT 0,
            processed INTEGER NOT NULL DEFAULT 0,
            found INTEGER NOT NULL DEFAULT 0,
            not_found INTEGER NOT NULL DEFAULT 0,
            message TEXT,
            filename TEXT,
            payload_json TEXT,
            created_at_ms INTEGER NOT NULL,
            updated_at_ms INTEGER NOT NULL
        )
        """)
        # Bestehende DBs nachziehen — vor diesen Spalten lebte der Worker mit
        # einem `message`-Feld-Hijack ("provider|limit|pending_raw"), der
        # zwei Zwecke vermischte: User-Status-Anzeige + Job-Payload. Trennen.
        _ensure_column(conn, "csv_import_jobs", "filename", "TEXT")
        _ensure_column(conn, "csv_import_jobs", "payload_json", "TEXT")

        conn.execute("""
        CREATE TABLE IF NOT EXISTS csv_import_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL,
            result_type TEXT NOT NULL,
            original TEXT,
            requested_artist TEXT,
            requested_title TEXT,
            track_json TEXT,
            FOREIGN KEY (job_id) REFERENCES csv_import_jobs(job_id)
        )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_csv_results_job ON csv_import_results(job_id, result_type)")
        # Phase I: Playlist-aware Import. Trägt die Playlist(s) auf denen ein
        # Track im Source-CSV stand durch alle Phasen — Phase 0 library_match,
        # Phase 2 matched, Phase 4 download → reconcile als Subsonic-Playlist.
        # JSON-Liste damit ein Track auf mehreren Playlists landen kann
        # (TuneMyMusic kann multi-playlist-Exports erzeugen).
        _ensure_column(conn, "csv_import_results", "playlist_names_json", "TEXT")

        # ── Phase F: Multi-User-Auth ─────────────────────────────
        # users — registrierte Konten. password_hash = argon2id, totp_secret =
        # base32 (verschlüsselt mit JWT_SECRET als data-at-rest-Schutz, siehe
        # auth_users.py). is_admin steuert wer neue User anlegen darf.
        # last_login_at_ms für Activity-Anzeige in Settings.
        conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            totp_secret TEXT,
            is_admin INTEGER NOT NULL DEFAULT 0,
            created_at_ms INTEGER NOT NULL,
            last_login_at_ms INTEGER
        )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_users_username ON users(username)")

        # pats — Personal Access Tokens für Plugin/CLI/MCP. Plain-Token wird
        # NICHT gespeichert; wir hashen mit sha256 (Tokens sind random-128bit,
        # kein Bcrypt nötig). prefix = sichtbarer Anfang (z.B. "tonus_pat_aB12") als
        # ID für den User in der UI. last_used_at_ms für "rotate stale tokens".
        conn.execute("""
        CREATE TABLE IF NOT EXISTS pats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            prefix TEXT NOT NULL,
            token_hash TEXT NOT NULL UNIQUE,
            scopes TEXT,
            created_at_ms INTEGER NOT NULL,
            last_used_at_ms INTEGER,
            expires_at_ms INTEGER,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_pats_user ON pats(user_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_pats_token_hash ON pats(token_hash)")

        # auth_meta — Server-seitige Auth-Geheimnisse. JWT_SECRET wird beim
        # ersten Start generiert (siehe auth_users.get_or_init_jwt_secret).
        # Key-Value-Schema, einfacher als ein einzelner-Row-Tabellen-Hack.
        conn.execute("""
        CREATE TABLE IF NOT EXISTS auth_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at_ms INTEGER NOT NULL
        )
        """)

        # login_attempts — pro Username + Source-IP, fenster-basiert für das
        # Rate-Limit aus config.LOGIN_RATE_LIMIT_PER_15MIN. Cleanup beim
        # Verify-Aufruf (alle Einträge älter als 15 min werden gelöscht).
        conn.execute("""
        CREATE TABLE IF NOT EXISTS login_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            source_ip TEXT,
            attempted_at_ms INTEGER NOT NULL,
            success INTEGER NOT NULL DEFAULT 0
        )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_login_attempts_username_ts ON login_attempts(username, attempted_at_ms)")

        # refresh_tokens — JWT-Refresh-Tokens werden beim Logout / Rotation
        # invalidiert. Wir speichern den jti-Claim des Refresh-Tokens hier
        # mit user_id und expires_at — bei jedem /api/auth/refresh checken
        # wir ob jti in dieser Tabelle ist UND nicht expired. Logout =
        # DELETE WHERE jti=...
        conn.execute("""
        CREATE TABLE IF NOT EXISTS refresh_tokens (
            jti TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            issued_at_ms INTEGER NOT NULL,
            expires_at_ms INTEGER NOT NULL,
            revoked_at_ms INTEGER,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_refresh_tokens_user ON refresh_tokens(user_id)")

        # app_settings — generischer Key/Value-Store für Runtime-Konfiguration
        # die NICHT in env (deployment-immutable) sondern via UI editierbar
        # sein soll. Provider-Credentials (Spotify Client-ID/Secret, Navidrome
        # User/Pass, etc.) wandern hierhin. encrypted=1 markiert Werte, die
        # mit Fernet verschlüsselt sind (Secrets); plain-text-Werte wie URLs
        # oder Usernames bleiben encrypted=0 für Operability/Debug.
        conn.execute("""
        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            encrypted INTEGER NOT NULL DEFAULT 0,
            updated_at_ms INTEGER NOT NULL
        )
        """)

        # banned_ips — lifetime-Bans nach 5+ Failed-Logins/24h pro IP.
        # PRIMARY KEY auf ip macht Re-Bans idempotent (INSERT OR IGNORE).
        # Loopback-Adressen (127.0.0.1, ::1) werden vom Auto-Bann-Code
        # ausgeschlossen — Container-internal-Calls sollen nie ausgesperrt
        # werden. Admin entfernt Bans manuell über Settings → Brute-Force.
        conn.execute("""
        CREATE TABLE IF NOT EXISTS banned_ips (
            ip TEXT PRIMARY KEY,
            reason TEXT,
            banned_at_ms INTEGER NOT NULL,
            failed_count INTEGER NOT NULL DEFAULT 0
        )
        """)

        conn.commit()
    finally:
        conn.close()


def reset_stale_inflight_jobs() -> int:
    """
    After a process restart, no BackgroundTasks are running. Rows still marked
    queued/processing would block new downloads (duplicate check). Mark them error.
    """
    now = _now_ms()
    msg = "Interrupted — server restarted. Retry the download."
    conn = _db()
    try:
        cur = conn.execute(
            """
            UPDATE download_jobs
            SET status = 'error',
                message = ?,
                stage = NULL,
                progress = 0,
                updated_at_ms = ?
            WHERE status IN ('queued', 'processing')
            """,
            (msg, now),
        )
        n = cur.rowcount or 0
        conn.commit()
        return n
    finally:
        conn.close()


def reset_stale_csv_jobs() -> Dict[str, int]:
    """
    After a process restart, mark any in-flight CSV imports as 'error' and clean up
    their staging data (pending_raw / claimed). Old matched/unmatched results stay
    intact so the user can still browse them. Also handles legacy 'csv-1'-collision
    pollution: stale pending_raw rows belonging to non-completed jobs are removed so
    the new (unique) job_id workflow starts on a clean slate.
    """
    now = _now_ms()
    conn = _db()
    try:
        stale_ids = [r["job_id"] for r in conn.execute(
            "SELECT job_id FROM csv_import_jobs WHERE status IN ('queued', 'processing')"
        ).fetchall()]
        n_jobs = 0
        n_rows = 0
        if stale_ids:
            placeholders = ",".join("?" * len(stale_ids))
            cur = conn.execute(
                f"DELETE FROM csv_import_results WHERE job_id IN ({placeholders}) AND result_type IN ('pending_raw', 'claimed')",
                stale_ids,
            )
            n_rows = cur.rowcount or 0
            cur = conn.execute(
                f"UPDATE csv_import_jobs SET status = 'error', message = 'Interrupted — server restarted', updated_at_ms = ? WHERE job_id IN ({placeholders})",
                (now, *stale_ids),
            )
            n_jobs = cur.rowcount or 0
        conn.commit()
        return {"jobs_reset": n_jobs, "rows_purged": n_rows}
    finally:
        conn.close()


def upsert_job(
    job_id: str,
    *,
    status: str,
    message: str,
    stage: Optional[str] = None,
    progress: Optional[int] = None,
    file_path: Optional[str] = None,
    download_url: Optional[str] = None,
    error: Optional[str] = None,
    album_id: Optional[str] = None,
    payload: Optional[Dict[str, Any]] = None,
) -> None:
    now = _now_ms()
    conn = _db()
    try:
        conn.execute("""
        INSERT INTO download_jobs (
            job_id, status, stage, progress, message, file_path, download_url, error,
            album_id, payload_json, created_at_ms, updated_at_ms
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(job_id) DO UPDATE SET
            status=excluded.status,
            stage=COALESCE(excluded.stage, download_jobs.stage),
            progress=COALESCE(excluded.progress, download_jobs.progress),
            message=excluded.message,
            file_path=COALESCE(excluded.file_path, download_jobs.file_path),
            download_url=COALESCE(excluded.download_url, download_jobs.download_url),
            error=COALESCE(excluded.error, download_jobs.error),
            album_id=COALESCE(excluded.album_id, download_jobs.album_id),
            payload_json=COALESCE(excluded.payload_json, download_jobs.payload_json),
            updated_at_ms=excluded.updated_at_ms
        """, (
            job_id, status, stage, progress, message, file_path, download_url, error,
            album_id,
            json.dumps(payload) if payload is not None else None,
            now, now
        ))
        conn.commit()
    finally:
        conn.close()


def record_completed_download(track_id: str, provider: str) -> None:
    """Mark a catalog track as already downloaded (survives temp file cleanup)."""
    now = _now_ms()
    conn = _db()
    try:
        conn.execute(
            """
            INSERT INTO completed_track_downloads (track_id, provider, completed_at_ms)
            VALUES (?, ?, ?)
            ON CONFLICT(track_id, provider) DO UPDATE SET completed_at_ms = excluded.completed_at_ms
            """,
            (track_id, provider, now),
        )
        conn.commit()
    finally:
        conn.close()


def has_completed_download(track_id: str, provider: str) -> bool:
    conn = _db()
    try:
        row = conn.execute(
            "SELECT 1 FROM completed_track_downloads WHERE track_id = ? AND provider = ?",
            (track_id, provider),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def get_job(job_id: str) -> Optional[Dict[str, Any]]:
    conn = _db()
    try:
        row = conn.execute(
            "SELECT * FROM download_jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        if not row:
            return None

        d = dict(row)
        payload_json = d.pop("payload_json", None)
        if payload_json:
            try:
                d["payload"] = json.loads(payload_json)
            except Exception:
                d["payload"] = None
        else:
            d["payload"] = None

        return d
    finally:
        conn.close()

def get_album_track_jobs(album_id: str, *, exclude_job_id: Optional[str] = None) -> list[dict]:
    conn = _db()
    try:
        sql = """
        SELECT job_id, status, stage, progress, message, file_path, download_url, error, updated_at_ms
        FROM download_jobs
        WHERE album_id = ?
        """
        params = [album_id]
        if exclude_job_id:
            sql += " AND job_id <> ?"
            params.append(exclude_job_id)

        sql += " ORDER BY updated_at_ms DESC"
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_album_aggregate(album_id: str, *, exclude_job_id: Optional[str] = None) -> dict:
    conn = _db()
    try:
        where = "album_id = ?"
        params: list = [album_id]
        if exclude_job_id:
            where += " AND job_id <> ?"
            params.append(exclude_job_id)

        total = conn.execute(f"SELECT COUNT(*) AS n FROM download_jobs WHERE {where}", params).fetchone()["n"]
        completed = conn.execute(
            f"SELECT COUNT(*) AS n FROM download_jobs WHERE {where} AND status = 'completed'",
            params,
        ).fetchone()["n"]
        failed = conn.execute(
            f"SELECT COUNT(*) AS n FROM download_jobs WHERE {where} AND status = 'error'",
            params,
        ).fetchone()["n"]

        current = conn.execute(
            f"""
            SELECT job_id
            FROM download_jobs
            WHERE {where} AND status NOT IN ('completed', 'error')
            ORDER BY updated_at_ms DESC
            LIMIT 1
            """,
            params,
        ).fetchone()
        current_track = current["job_id"] if current else None

        status = "completed" if total > 0 and (completed + failed) >= total else "downloading"

        return {
            "status": status,
            "total_tracks": total,
            "completed_tracks": completed,
            "failed_tracks": failed,
            "current_track": current_track,
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# CSV-Import helpers (persistente Jobs in SQLite)
# ---------------------------------------------------------------------------

def upsert_csv_job(
    job_id: str,
    *,
    status: Optional[str] = None,
    total: Optional[int] = None,
    processed: Optional[int] = None,
    found: Optional[int] = None,
    not_found: Optional[int] = None,
    message: Optional[str] = None,
    filename: Optional[str] = None,
    payload_json: Optional[str] = None,
) -> None:
    """
    Partial upsert: nur Felder mit non-None werden im Update-Pfad
    überschrieben. Dahinter steckt die Lehre aus dem alten "found=0"-
    Bug — wenn der Worker während Phase 2 nur `processed` und `message`
    übergab, wurden `found`/`not_found` stillschweigend auf den default
    (0) zurückgesetzt. Mit COALESCE(excluded.x, table.x) bleibt der
    bisherige DB-Wert stehen, wenn der Caller das Feld nicht explizit
    setzt — semantisch genau das, was Caller erwarten.

    Der Insert-Pfad (erster Aufruf für eine neue job_id) füllt fehlende
    Felder pragmatisch mit sinnvollen Defaults via COALESCE auf der
    VALUES-Seite — sonst gäbe es NOT-NULL-Verletzungen.
    """
    now = _now_ms()
    conn = _db()
    try:
        conn.execute(
            """
            INSERT INTO csv_import_jobs (
                job_id, status, total, processed, found, not_found,
                message, filename, payload_json, created_at_ms, updated_at_ms
            )
            VALUES (
                ?,
                COALESCE(?, 'queued'),
                COALESCE(?, 0),
                COALESCE(?, 0),
                COALESCE(?, 0),
                COALESCE(?, 0),
                COALESCE(?, ''),
                ?, ?, ?, ?
            )
            ON CONFLICT(job_id) DO UPDATE SET
                status        = COALESCE(excluded.status,       csv_import_jobs.status),
                total         = COALESCE(excluded.total,        csv_import_jobs.total),
                processed     = COALESCE(excluded.processed,    csv_import_jobs.processed),
                found         = COALESCE(excluded.found,        csv_import_jobs.found),
                not_found     = COALESCE(excluded.not_found,    csv_import_jobs.not_found),
                message       = COALESCE(excluded.message,      csv_import_jobs.message),
                filename      = COALESCE(excluded.filename,     csv_import_jobs.filename),
                payload_json  = COALESCE(excluded.payload_json, csv_import_jobs.payload_json),
                updated_at_ms = excluded.updated_at_ms
            """,
            (
                job_id, status, total, processed, found, not_found,
                message, filename, payload_json, now, now,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def get_csv_job(job_id: str) -> Optional[Dict[str, Any]]:
    conn = _db()
    try:
        row = conn.execute("SELECT * FROM csv_import_jobs WHERE job_id = ?", (job_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def insert_csv_results(job_id: str, result_type: str, items: list) -> None:
    """Phase I: items dürfen optional `playlist_names: List[str]` enthalten —
    werden als JSON in playlist_names_json gespeichert, damit Reconcile später
    weiß auf welcher Subsonic-Playlist der Track landen soll."""
    conn = _db()
    try:
        conn.executemany(
            "INSERT INTO csv_import_results (job_id, result_type, original, requested_artist, requested_title, track_json, playlist_names_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    job_id,
                    result_type,
                    item.get("original"),
                    item.get("requested_artist"),
                    item.get("requested_title"),
                    json.dumps(item.get("track")) if item.get("track") else None,
                    json.dumps(item.get("playlist_names")) if item.get("playlist_names") else None,
                )
                for item in items
            ],
        )
        conn.commit()
    finally:
        conn.close()


def get_csv_results(job_id: str, offset: int = 0, limit: int = 100) -> Dict[str, Any]:
    """Drei Buckets seit Phase H:
    - matched          → Provider-Lookup hat einen Treffer geliefert (downloadbar)
    - library_match    → bereits in Navidrome-Library, kein Download nötig (Phase H)
    - unmatched        → weder Library noch Provider liefert was
    Backward-Compat: matched/unmatched sind die alten Felder; library_match ist
    additiv (alte Frontends ignorieren das Feld einfach).
    """
    conn = _db()
    try:
        matched = conn.execute(
            "SELECT * FROM csv_import_results WHERE job_id = ? AND result_type = 'matched' ORDER BY id LIMIT ? OFFSET ?",
            (job_id, limit, offset),
        ).fetchall()
        unmatched = conn.execute(
            "SELECT * FROM csv_import_results WHERE job_id = ? AND result_type = 'unmatched' ORDER BY id LIMIT ? OFFSET ?",
            (job_id, limit, offset),
        ).fetchall()
        library_match = conn.execute(
            "SELECT * FROM csv_import_results WHERE job_id = ? AND result_type = 'library_match' ORDER BY id LIMIT ? OFFSET ?",
            (job_id, limit, offset),
        ).fetchall()

        def _row_to_item(row) -> dict:
            track = None
            if row["track_json"]:
                try:
                    track = json.loads(row["track_json"])
                except (json.JSONDecodeError, TypeError):
                    pass
            return {
                "original": row["original"],
                "requested_artist": row["requested_artist"],
                "requested_title": row["requested_title"],
                "track": track,
            }

        return {
            "matched": [_row_to_item(r) for r in matched],
            "unmatched": [_row_to_item(r) for r in unmatched],
            "library_match": [_row_to_item(r) for r in library_match],
        }
    finally:
        conn.close()


def get_csv_library_matches_with_playlists(job_id: str) -> List[Dict[str, Any]]:
    """Phase I-Edge-Case: liefert alle library_match-Rows eines Jobs die
    `playlist_names_json` haben. Wird vom Reconcile gebraucht, weil Library-
    Match-Tracks keinen Download-Job erzeugen und damit aus dem normalen
    Reconcile-Pfad (`_reconcile_imported_playlists`) rausfallen — ohne diesen
    Helper landen sie nicht in den Subsonic-Playlists.

    Returnt list of {requested_artist, requested_title, playlist_names: List[str]}.
    """
    conn = _db()
    try:
        rows = conn.execute(
            """
            SELECT requested_artist, requested_title, playlist_names_json
            FROM csv_import_results
            WHERE job_id = ?
              AND result_type = 'library_match'
              AND playlist_names_json IS NOT NULL
            ORDER BY id
            """,
            (job_id,),
        ).fetchall()
    finally:
        conn.close()
    out: List[Dict[str, Any]] = []
    for r in rows:
        try:
            playlist_names = json.loads(r["playlist_names_json"])
        except (json.JSONDecodeError, TypeError):
            continue
        if not playlist_names:
            continue
        out.append({
            "requested_artist": r["requested_artist"] or "",
            "requested_title": r["requested_title"] or "",
            "playlist_names": [p for p in playlist_names if isinstance(p, str) and p.strip()],
        })
    return out


def count_csv_results(job_id: str) -> Dict[str, int]:
    """Returns Counts für drei Buckets seit Phase H. matched/unmatched
    bleiben backward-kompatibel; library_match ist neu."""
    conn = _db()
    try:
        matched = conn.execute(
            "SELECT COUNT(*) AS n FROM csv_import_results WHERE job_id = ? AND result_type = 'matched'",
            (job_id,),
        ).fetchone()["n"]
        unmatched = conn.execute(
            "SELECT COUNT(*) AS n FROM csv_import_results WHERE job_id = ? AND result_type = 'unmatched'",
            (job_id,),
        ).fetchone()["n"]
        library_match = conn.execute(
            "SELECT COUNT(*) AS n FROM csv_import_results WHERE job_id = ? AND result_type = 'library_match'",
            (job_id,),
        ).fetchone()["n"]
        return {"matched": matched, "unmatched": unmatched, "library_match": library_match}
    finally:
        conn.close()
