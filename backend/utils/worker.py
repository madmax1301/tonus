"""Background worker threads for download + CSV-import jobs.

Zwei unabhängige Worker — Downloads und CSV-Import laufen parallel,
blockieren sich nicht. HTTP-Endpoints return instantly after enqueuing.

Cool-down zwischen Jobs (Anti-429): wird hier zentralisiert, gilt sowohl für
erfolgreiche als auch für fehlgeschlagene Jobs. Bei 429-Detection im error-Field
wird ein deutlich längerer Cooldown angewendet (5–10 min), damit Retry-Wellen
das YouTube-Rate-Limit nicht weiter eskalieren.
"""

import json
import os
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Set, Tuple

import requests  # für HTTPError-Detection im 429-Failover (Dual-VPN-Splitting)

from utils.job_store import (
    _db,
    _now_ms,
    upsert_job,
    upsert_import_job,
    insert_import_results,
    get_job,
    is_import_job_cancelled,
)


# Cooldown-Bereiche (defaults — können via Settings → Standard-Verhalten
# überschrieben werden; siehe _load_cooldown_ranges)
_COOLDOWN_NORMAL = (60, 300)        # 1–5 min nach success / unauffälligem error
_COOLDOWN_429 = (300, 600)          # 5–10 min nach erkanntem 429


def _load_cooldown_ranges() -> Tuple[Tuple[int, int], Tuple[int, int]]:
    """Lädt die Cooldown-Ranges aus app_settings, fallback auf Module-Defaults.
    Wird bei jedem Cooldown-Aufruf live aufgerufen — User-Änderungen in der UI
    wirken sofort, kein Container-Restart nötig.

    Bei kaputten Werten (negativ, min > max, nicht-numerisch) → Defaults."""
    from utils.app_settings import get_setting

    def _read(key: str, fallback: int) -> int:
        try:
            v = get_setting(f'cooldown.{key}')
            if v is None:
                return fallback
            n = int(v)
            return n if n >= 0 else fallback
        except (ValueError, TypeError):
            return fallback

    n_min = _read('normal_min_s', _COOLDOWN_NORMAL[0])
    n_max = _read('normal_max_s', _COOLDOWN_NORMAL[1])
    r_min = _read('rl_min_s', _COOLDOWN_429[0])
    r_max = _read('rl_max_s', _COOLDOWN_429[1])
    # Sanity: min ≤ max, sonst die Default-Range nehmen
    if n_min > n_max:
        n_min, n_max = _COOLDOWN_NORMAL
    if r_min > r_max:
        r_min, r_max = _COOLDOWN_429
    return (n_min, n_max), (r_min, r_max)

# CSV-Match-Tuning. Dual-Lane: 8 parallel über 2 Source-IPs (4/Lane) liegen
# bei Deezer (~50 req / 5s soft-limit) komfortabel drunter. Single-Lane: alle
# Threads teilen sich eine IP, daher konservativ auf 2 reduzieren — sonst
# bekommt jede 2. Anfrage 429 und der Single-Lane-Pfad hat keinen Failover.
# DB-Inserts in 500er Chunks halten SQLite responsiv.
_VPN_SPLIT_ENABLED = os.environ.get("VPN_SPLIT_ENABLED", "").strip().lower() == "true"
_CSV_SEARCH_CONCURRENCY = 8 if _VPN_SPLIT_ENABLED else 2
_CSV_FLUSH_BATCH = 500
_CSV_PROGRESS_EVERY = 5             # alle N abgeschlossene Unique-Suchen Status updaten —
                                    # 5 statt 50, damit das Frontend mit svelte/motion `tweened`
                                    # echten Counter-Verlauf bekommt statt 0→34→65-Sprünge.

# Bei einem 429 ohne Failover-Möglichkeit (single-lane) versuchen wir es mit
# exponential backoff erneut. 1.5 s → 3 s → aufgeben. Hält uns über schwankende
# Provider-Limits hinweg, ohne den ThreadPool unbegrenzt zu blockieren.
_CSV_429_RETRY_DELAYS: Tuple[float, ...] = (1.5, 3.0)

# Dual-VPN-Splitting: gerade Thread-Indizes nutzen Lane A, ungerade Lane B —
# pro Lane bindet services.deezer._get_session(...) eine andere Source-IP.
# Bei VPN_SPLIT_ENABLED=false fällt alles auf "default" zurück (kein Bind).
_CSV_LANES: Tuple[str, str] = ("a", "b")

# Download-Worker: zwei Lanes mit getrennten Cooldown-Timern. Single-threaded
# bleibt der Worker (keine parallelen yt-dlp-Prozesse — YouTube-Bot-Detection!),
# aber während Lane A im Cooldown wartet, kann Lane B sofort den nächsten Job
# starten. Effektiv halbiert das die Idle-Zeit zwischen Downloads.
# Bei VPN_SPLIT_ENABLED=false → eine "default"-Lane → 1:1 Verhalten wie zuvor.
_DOWNLOAD_LANES: Tuple[str, ...] = ("a", "b") if _VPN_SPLIT_ENABLED else ("default",)


def _looks_like_429(message: str, error: str) -> bool:
    """Heuristik: zeigt der Job-Status auf YouTube-Rate-Limiting hin?"""
    blob = f"{message or ''} {error or ''}".lower()
    needles = ("429", "too many requests", "rate-limit", "rate limit")
    return any(n in blob for n in needles)


class JobWorker(threading.Thread):
    """Generischer Worker.

    job_type="download" → Download-Lane.
    job_type="import"   → Import-Lane.

    import_lane (nur relevant wenn job_type="import"): Filter im Polling.
      None             → pollt alle Import-Jobs (legacy single-worker)
      "csv"            → mode='full'  AND source='csv'
      "spotify_history"→ mode='full'  AND source='spotify_history'
      "playlist_sync"  → mode='playlist_sync' (source ignoriert)

    Mehrere JobWorker-Instanzen mit unterschiedlichen import_lane-Werten
    laufen parallel — User-Wunsch (2026-05-10): Bulk-CSV soll Playlist-Sync
    nicht blockieren.
    """

    def __init__(self, job_type: str, import_lane: Optional[str] = None) -> None:
        thread_name = f"worker-{job_type}" + (f":{import_lane}" if import_lane else "")
        super().__init__(daemon=True, name=thread_name)
        self._job_type = job_type
        self._import_lane = import_lane
        self._stop: threading.Event = threading.Event()
        self._lock: threading.Lock = threading.Lock()
        # Per-Lane "ready_at" (ms): wann ist die Lane wieder benutzbar (Cooldown vorbei)?
        # 0 = sofort nutzbar. Wird von _process_download nach jedem Job gesetzt.
        self._lane_ready_at: Dict[str, int] = {l: 0 for l in _DOWNLOAD_LANES}
        # Round-robin-Tiebreaker wenn beide Lanes gleichzeitig ready sind.
        self._lane_rr_idx: int = 0
        # Welcher Job läuft gerade auf welcher Lane? Nötig damit das UI
        # einen Processing-Job korrekt der visuellen Lane (a/b) zuordnen kann.
        # Vorher hat das Frontend die Reihenfolge nach created_at_ms erraten,
        # was falsch war wenn nur Lane B lief — der Job landete dann in
        # Slot[0] = Lane A, Lane B sah "Ready" obwohl sie aktiv war.
        self._lane_current_job: Dict[str, Optional[str]] = {l: None for l in _DOWNLOAD_LANES}

    # ------------------------------------------------------------------
    # Lane selection (Download-Worker, Dual-VPN)
    # ------------------------------------------------------------------

    def lane_status(self) -> Dict[str, Any]:
        """UI-View auf den Lane-Cooldown-State.

        Wird von /api/queue/lanes für die Live-Queue gepollt — die User-
        sichtbare "noch X:XX bis nächster Job"-Anzeige. Cooldown-Bereiche
        kommen ebenfalls mit, damit das Frontend Range-Hints zeigen kann.
        """
        now = _now_ms()
        lanes = []
        for name in _DOWNLOAD_LANES:
            ready_at = int(self._lane_ready_at.get(name, 0))
            lanes.append({
                "name": name,
                "ready_at_ms": ready_at,
                "remaining_ms": max(0, ready_at - now),
                "current_job_id": self._lane_current_job.get(name),
            })
        # Wenn mind. eine Lane ready: 0 ms bis nächste Lane verfügbar.
        next_ready = min((l["remaining_ms"] for l in lanes), default=0)
        normal_range, rl_range = _load_cooldown_ranges()
        return {
            "lanes": lanes,
            "next_ready_in_ms": next_ready,
            "cooldown": {
                "normal_seconds": list(normal_range),
                "rate_limited_seconds": list(rl_range),
            },
        }

    def _pick_download_lane(self) -> Tuple[Optional[str], int]:
        """Returns (lane_or_None, wait_ms_until_next_ready).

        lane=None heißt: alle Lanes sind im Cooldown — der Caller soll
        wait_ms warten und dann erneut versuchen. Bei nur einer Lane
        ("default") ist das Verhalten 1:1 zum klassischen Single-Lane-Worker.
        """
        now = _now_ms()
        ready = [l for l in _DOWNLOAD_LANES if self._lane_ready_at[l] <= now]
        if ready:
            # Round-robin unter den ready Lanes — gibt fair-share, auch wenn beide
            # immer ready sind (= keine Cooldowns aktiv, z.B. nach Worker-Start).
            chosen = ready[self._lane_rr_idx % len(ready)]
            self._lane_rr_idx = (self._lane_rr_idx + 1) % max(1, len(_DOWNLOAD_LANES))
            return chosen, 0
        # Alle Lanes im Cooldown → kürzeste Restwartezeit zurückgeben.
        wait_ms = min(self._lane_ready_at[l] - now for l in _DOWNLOAD_LANES)
        return None, max(0, wait_ms)

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def run(self) -> None:
        # Boot-Log damit Operator im Container-Log Worker-Lebenszeichen sieht.
        # Hilft bei Diagnose ob alle Lanes (download + 3× import) wirklich laufen.
        lane_suffix = f":{self._import_lane}" if self._import_lane else ""
        print(f"[worker:{self._job_type}{lane_suffix}] loop started, polling for jobs", flush=True)
        consecutive_errors = 0
        while not self._stop.is_set():
            try:
                if self._job_type == "download":
                    lane, wait_ms = self._pick_download_lane()
                    if lane is None:
                        # Alle Lanes im Cooldown — stoppable-sleep auf die kürzeste
                        # Restwartezeit, dann erneut prüfen.
                        self._stop.wait(timeout=max(0.5, wait_ms / 1000.0))
                        continue
                    job = self._poll_next_queued_download(lane=lane)
                    if job:
                        self._process_download(job, lane=lane)
                        consecutive_errors = 0
                        continue
                else:
                    import_job = self._poll_next_queued_import()
                    if import_job:
                        self._process_import_job(import_job)
                        consecutive_errors = 0
                        continue
                self._stop.wait(timeout=2.0)
            except Exception as e:
                # Top-Level-Guard: ohne diesen catch killt jede ungefangene
                # Exception (SQLite-OperationalError, ImportError, kaputter
                # Tag-Read in einer File, etc.) den ganzen Worker-Thread
                # SILENT — User sieht "Queued — waiting for worker" für
                # immer und weiß nicht warum. Stattdessen: Exception loggen
                # mit Traceback, kurzen Backoff machen, weiter pollen.
                # Bei Backoff-Bursts (>10 Errors hintereinander) längere
                # Pause damit DB nicht in Endlosschleife crasht.
                import traceback
                consecutive_errors += 1
                backoff = min(30.0, 1.0 * (2 ** min(consecutive_errors, 5)))
                print(
                    f"[worker:{self._job_type}] EXCEPTION (consecutive={consecutive_errors}, "
                    f"backoff={backoff:.1f}s): {type(e).__name__}: {e}",
                    flush=True,
                )
                traceback.print_exc()
                self._stop.wait(timeout=backoff)
        print(f"[worker:{self._job_type}] loop exited (stop signal received)", flush=True)

    def shutdown(self, timeout: Optional[float] = None) -> None:
        self._stop.set()
        self.join(timeout=timeout)

    # ------------------------------------------------------------------
    # Download job polling
    # ------------------------------------------------------------------

    def _poll_next_queued_download(self, lane: str = "default") -> Optional[Dict[str, Any]]:
        conn = _db()
        try:
            row = conn.execute(
                """
                SELECT job_id, payload_json
                FROM download_jobs
                WHERE status = 'queued'
                ORDER BY created_at_ms ASC, rowid ASC
                LIMIT 1
                """
            ).fetchone()
            if not row:
                return None

            conn.execute(
                "UPDATE download_jobs SET status='processing', updated_at_ms=? WHERE job_id=?",
                (_now_ms(), row["job_id"]),
            )
            conn.commit()
            # Lane-Mapping IM SELBEN STACK-FRAME wie der COMMIT setzen — kein
            # Funktions-Call-Boundary mehr zwischen DB-Transition und In-
            # Memory-Update. Eliminiert das Race-Fenster wo /api/queue X als
            # processing zeigt aber /api/queue/lanes die Lane noch idle
            # reportet (was vorher zu UI-Sprüngen führte: queuedJobs[0]
            # wurde fälschlich auf die "scheinbar idle" Lane gemappt). */
            self._lane_current_job[lane] = row["job_id"]

            params: Dict[str, Any] = {}
            if row["payload_json"]:
                try:
                    params = json.loads(row["payload_json"])
                except (json.JSONDecodeError, TypeError):
                    pass
            return {"job_id": row["job_id"], "params": params}
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # CSV import job polling
    # ------------------------------------------------------------------

    def _poll_next_queued_import(self) -> Optional[Dict[str, Any]]:
        # Lane-Filter (Multi-Worker-Trennung 2026-05-10):
        #   - import_lane=None              → alle Imports
        #   - import_lane="csv"             → mode='full' AND source='csv'
        #   - import_lane="spotify_history" → mode='full' AND source='spotify_history'
        #   - import_lane="playlist_sync"   → mode='playlist_sync'
        # Index idx_import_jobs_lane(status, source, mode, created_at_ms)
        # macht das Polling O(log n) statt full-table-scan.
        #
        # Race-Tolerance: bei klassischem SELECT+UPDATE könnten zwei Worker-
        # Threads in derselben Lane denselben Job picken. Mit klar getrennten
        # Lanes (jeder hat genau einen Worker pro Lane) ist das praktisch
        # ausgeschlossen. Frühere atomare Versuche (BEGIN IMMEDIATE und
        # UPDATE…RETURNING) verursachten Lock-Stau bzw. Silent-Fail im
        # Container — daher zurück zur einfachen zweistufigen Variante.
        conn = _db()
        try:
            if self._import_lane == "playlist_sync":
                where_sql = "status = 'queued' AND mode = 'playlist_sync'"
                params: Tuple[Any, ...] = ()
            elif self._import_lane in ("csv", "spotify_history"):
                where_sql = "status = 'queued' AND mode = 'full' AND source = ?"
                params = (self._import_lane,)
            else:
                where_sql = "status = 'queued'"
                params = ()
            row = conn.execute(
                f"""
                SELECT job_id, payload_json, message, total
                FROM import_jobs
                WHERE {where_sql}
                ORDER BY created_at_ms ASC
                LIMIT 1
                """,
                params,
            ).fetchone()
            if not row:
                return None
            conn.execute(
                "UPDATE import_jobs SET status='processing', updated_at_ms=? WHERE job_id=?",
                (_now_ms(), row["job_id"]),
            )
            conn.commit()

            # Provider/search_limit aus payload_json (sauberer Weg, seit
            # Schema-Migration). Fallback auf den alten message-Hijack
            # ("provider|limit|pending_raw") für Jobs aus der Übergangs-
            # phase, die noch vor dem Schema-Upgrade angelegt wurden.
            provider = "deezer"
            search_limit = 3
            mode = "full"
            payload_raw = row["payload_json"]
            if payload_raw:
                try:
                    payload = json.loads(payload_raw)
                    if isinstance(payload.get("provider"), str):
                        provider = payload["provider"]
                    if isinstance(payload.get("search_limit"), int):
                        search_limit = payload["search_limit"]
                    if isinstance(payload.get("mode"), str):
                        mode = payload["mode"]
                except (json.JSONDecodeError, TypeError):
                    pass
            else:
                msg = row["message"] or ""
                parts = msg.split("|")
                if len(parts) >= 2 and parts[-1] == "pending_raw":
                    provider = parts[0]
                    try:
                        search_limit = int(parts[1])
                    except ValueError:
                        pass

            return {
                "job_id": row["job_id"],
                "provider": provider,
                "search_limit": search_limit,
                "mode": mode,
                "total": row["total"],
            }
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # CSV import execution
    # ------------------------------------------------------------------

    def _process_import_job(self, import_job: Dict[str, Any]) -> None:
        """Match pending_raw items against Deezer, write results to SQLite.

        Performance-Architektur (seit Phase H):
        0. **Library-Match-First** (Phase H): einmaliger Filesystem-Scan via
           NavidromeService.library_signatures(), pre-filter pending → Tracks
           die bereits in Navidrome liegen werden direkt als 'library_match'
           gespeichert (kein Provider-Call). Spart bei wachsenden Libraries
           80%+ Deezer/Spotify-Quota.
        1. Dedup nach (artist_lc, title_lc) — bei History-Exports oft 30–50%
           Doppelte. Nur eindeutige Keys werden tatsächlich an Deezer gefragt.
        2. Parallele Suche per ThreadPoolExecutor (8 Worker). Deezer hat ein
           weiches Limit von ~50 req / 5 s; 8 Worker mit ~300–500 ms je Call
           landen unter ~24 req/s.
        3. Erst nachdem der Cache vollständig ist, werden alle Original-Rows
           (inkl. Duplikate) materialisiert und in 500er-Chunks geschrieben.
        """
        job_id: str = import_job["job_id"]
        provider: str = import_job["provider"]
        search_limit: int = import_job["search_limit"]
        total: int = import_job["total"]
        # Mode aus payload_json (siehe _poll_next_queued_import). "full" =
        # klassischer Bulk-Import-Flow, "playlist_sync" = nur Library-Match
        # + Reconcile (skip Provider/Download). Default "full".
        mode: str = import_job.get("mode", "full") or "full"

        from services.deezer import DeezerService
        from services.spotify import SpotifyService
        from services.navidrome import NavidromeService, _normalize_sig

        # User-Cancel-Polling-Helper. Wird zwischen Phase-Schritten aufgerufen
        # damit Cancel quasi-instant wirkt (max 1 Phase-Step Latenz).
        # Returnt True wenn cancelled — Caller soll dann sofort `return`.
        def _check_user_cancel(processed_so_far: int = 0) -> bool:
            if is_import_job_cancelled(job_id):
                upsert_import_job(
                    job_id,
                    status="cancelled",
                    message=f"Cancelled by user ({processed_so_far}/{total} processed)",
                )
                print(f"[import {job_id}] cancelled by user at {processed_so_far}/{total}", flush=True)
                return True
            return False

        if provider == "deezer":
            svc = DeezerService()
        else:
            svc = SpotifyService()
        if svc is None:
            upsert_import_job(job_id, status="error", message=f"Provider '{provider}' not available")
            return

        # Claim pending raw items (prevent re-processing on restart). Phase I:
        # playlist_names_json wird mitgelesen, damit der Worker die playlist-
        # Membership in matched/library_match Buckets durchreichen kann.
        conn = _db()
        try:
            pending = conn.execute(
                "SELECT id, original, requested_artist, requested_title, playlist_names_json FROM import_results WHERE job_id = ? AND result_type = 'pending_raw' ORDER BY id",
                (job_id,),
            ).fetchall()
            if pending:
                conn.execute(
                    "UPDATE import_results SET result_type = 'claimed' WHERE job_id = ? AND result_type = 'pending_raw'",
                    (job_id,),
                )
                conn.commit()
        finally:
            conn.close()

        if not pending:
            upsert_import_job(job_id, status="error", message="No pending items found")
            return

        # ---- Phase 0: Library-Match-First (Phase H) ----------------------
        # Scan Navidrome's Music-Pfade einmalig → set von normalisierten
        # (artist, title)-Signatures. Pre-filter aller pending Rows: was
        # schon in der Library liegt, wird sofort als 'library_match'
        # eingetragen (skip Provider-Call und Download). Das spart bei
        # rolling-imports den Großteil der Deezer/Spotify-Quota.
        #
        # Library-Scan kann beim ersten Aufruf 30-60s blockieren bei großen
        # Libraries — ohne Live-Progress denkt User der Worker hängt. Daher
        # progress-callback an library_signatures() der jede ~500 Files den
        # CSV-Job-Status updatet.
        upsert_import_job(
            job_id,
            status="processing",
            total=total,
            message=f"Phase 0: scanning Navidrome library (cache miss — first run after restart can take 30-60s)...",
        )

        # Phase-0-Progress-Bar: ratio = file_count / expected. expected ist
        # die file_count vom letzten Scan (in `_LIBRARY_SIG_CACHE` gehalten).
        # Beim allerersten Scan (cold cache) gibt es kein expected → linear-
        # Pseudo via 50000 als grobe Schätzung, capped auf 90% damit der Bar
        # nicht "voll" aussieht obwohl Phase 0 nicht fertig ist.
        from services.navidrome import library_sig_last_file_count
        expected_files = library_sig_last_file_count()

        def _scan_progress(file_count: int, sigs_count: int) -> None:
            if expected_files and expected_files > 0:
                ratio = min(1.0, file_count / expected_files)
            else:
                ratio = min(0.9, file_count / 50000.0)
            phase0_pct = int(round(ratio * 100))
            try:
                upsert_import_job(
                    job_id,
                    status="processing",
                    total=total,
                    phase0_progress=phase0_pct,
                    message=f"Phase 0: scanning library — {file_count:,} files / {sigs_count:,} signatures...",
                )
            except Exception:
                pass

        try:
            nav = NavidromeService()
            library_sigs = nav.library_signatures(on_progress=_scan_progress)
            # Phase 0 fertig — finalen 100% schreiben damit Bar voll bei
            # Übergang zu Phase 2 wenn dort processed=0 noch ist.
            try:
                upsert_import_job(job_id, phase0_progress=100)
            except Exception:
                pass
        except Exception as e:
            print(f"[import] library scan failed: {type(e).__name__}: {e} — skip Phase 0")
            library_sigs = set()
            upsert_import_job(
                job_id,
                status="processing",
                total=total,
                message=f"Phase 0: library scan failed ({type(e).__name__}) — falling back to provider-only match",
            )

        library_hits: List[Dict[str, Any]] = []
        remaining_pending: List[Any] = []
        import json as _wjson
        pending_total = len(pending)
        for idx, row in enumerate(pending):
            artist_orig = (row["requested_artist"] or "").strip()
            title_orig = (row["requested_title"] or "").strip()
            sig = (_normalize_sig(artist_orig), _normalize_sig(title_orig))
            # Phase I: playlist_names aus pending_raw durchreichen damit
            # Reconcile später auch library_match-Tracks zu Subsonic-
            # Playlists hinzufügen kann (siehe app._reconcile_imported_playlists).
            try:
                playlist_names = _wjson.loads(row["playlist_names_json"]) if row["playlist_names_json"] else []
            except Exception:
                playlist_names = []
            if sig in library_sigs and (sig[0] or sig[1]):
                library_hits.append({
                    "original": row["original"],
                    "requested_artist": artist_orig,
                    "requested_title": title_orig,
                    # track-json bleibt None — kein Provider-Track-Objekt,
                    # weil wir keinen Provider-Call gemacht haben. Frontend
                    # rendert library_match-Bucket ohne Track-Detail-Card.
                    "track": None,
                    "playlist_names": playlist_names,
                })
            else:
                remaining_pending.append(row)

            # Live-Progress alle 500 Items im Library-Match-Loop. Bei
            # playlist_sync ist Phase 0 die Hauptarbeit — User soll den
            # Counter steigen sehen. Bei full mode setzen wir nur found/
            # not_found als Hint, processed bleibt 0 weil Phase 2 da der
            # tatsächliche Counter-Treiber ist (vermeidet Zähler-Sprünge
            # bei Phase-Übergang).
            if (idx + 1) % 500 == 0 or (idx + 1) == pending_total:
                try:
                    upsert_import_job(
                        job_id,
                        status="processing",
                        total=total,
                        processed=(idx + 1) if mode == "playlist_sync" else None,
                        found=len(library_hits),
                        not_found=len(remaining_pending),
                        message=(
                            f"Phase 0: matched {idx + 1}/{pending_total} tracks against library "
                            f"— {len(library_hits)} hits, {len(remaining_pending)} pending"
                        ),
                    )
                except Exception:
                    pass

        if library_hits:
            insert_import_results(job_id, "library_match", library_hits)
            print(f"[import] Phase 0: {len(library_hits)} tracks already in library, skipping provider lookup")

        # Erster User-Cancel-Checkpoint nach Phase 0 — Library-Scan kann
        # 30-60s dauern, da soll User abbrechen können bevor Provider-
        # Calls überhaupt anfangen.
        if _check_user_cancel(0):
            return

        # ---- Mode-Switch: playlist_sync ----------------------------------
        # User-Wunsch (2026-05-10): wenn Tracks bereits via Bulk-Import in
        # der Library liegen, nur die Playlist-Memberships aus einer CSV
        # in Navidrome-Playlists übertragen — kein Provider-Lookup, kein
        # Download. Phase 0 hat library_hits + remaining_pending bestimmt;
        # bei playlist_sync schreiben wir die remaining_pending direkt als
        # not_found (User-Hinweis "diese Tracks sind nicht in deiner
        # Library") und triggern den Subsonic-Playlist-Reconcile.
        if mode == "playlist_sync":
            # Phase 0 fertig — UI-Update mit dem Match-Ergebnis bevor wir in
            # die Queue-Tagging-Phase gehen. Sonst sieht User nichts zwischen
            # Library-Scan und Final-Result.
            upsert_import_job(
                job_id,
                status="processing",
                total=total,
                processed=total,
                found=len(library_hits),
                not_found=len(remaining_pending),
                message=(
                    f"Phase 0 done — {len(library_hits)} in library, "
                    f"{len(remaining_pending)} not yet. Tagging Download-Queue..."
                ),
            )

            # Vor dem not_found-Bucket: cross-link gegen die Download-Queue.
            # Tracks die nicht in der Library liegen, aber bereits gequeued
            # sind, bekommen die Playlist-Namen als Marker im Download-Job-
            # Payload (`import_playlist_names`). Nach Download-Complete
            # läuft der existierende _reconcile_imported_playlists und
            # fügt sie zu den richtigen Subsonic-Playlists hinzu.
            from utils.job_store import (
                list_active_download_tracks,
                bulk_merge_playlist_names_into_download_jobs,
            )
            queue_lookup: Dict[Tuple[str, str], List[str]] = {}
            try:
                for entry in list_active_download_tracks():
                    sig = (
                        _normalize_sig(entry["artist"]),
                        _normalize_sig(entry["title"]),
                    )
                    queue_lookup.setdefault(sig, []).append(entry["job_id"])
            except Exception as e:
                print(f"[import {job_id}] queue-lookup failed: {type(e).__name__}: {e}", flush=True)

            # Erst alle merge-Updates in-memory sammeln, dann EIN Batch-
            # Update gegen die DB. Vermeidet 8000× open+commit+close die
            # mit dem parallelen Library-Sync-BG-Thread konkurrieren.
            misses_not_found: List[Dict[str, Any]] = []
            pending_queue_updates: Dict[str, List[str]] = {}
            tracks_with_queue_match: List[Tuple[str, List[str]]] = []  # (dj_id, pl_names) for counting after batch
            for row in remaining_pending:
                try:
                    pl_names = _wjson.loads(row["playlist_names_json"]) if row["playlist_names_json"] else []
                except Exception:
                    pl_names = []
                artist_orig = (row["requested_artist"] or "").strip()
                title_orig = (row["requested_title"] or "").strip()
                sig = (_normalize_sig(artist_orig), _normalize_sig(title_orig))
                queue_jobs = queue_lookup.get(sig, [])
                if queue_jobs and pl_names:
                    # Track ist in der Download-Queue → playlist_names sammeln,
                    # später als Batch ins payload_json mergen.
                    for dj_id in queue_jobs:
                        existing = pending_queue_updates.setdefault(dj_id, [])
                        for name in pl_names:
                            if name and name not in existing:
                                existing.append(name)
                        tracks_with_queue_match.append((dj_id, pl_names))
                else:
                    # Weder Library-Match noch in Queue → echter Miss.
                    misses_not_found.append({
                        "original": row["original"],
                        "requested_artist": artist_orig,
                        "requested_title": title_orig,
                        "track": None,
                        "playlist_names": pl_names,
                    })

            # Single-shot bulk merge — eine Connection, eine Transaction.
            queue_tagged_count = 0
            if pending_queue_updates:
                try:
                    merge_results = bulk_merge_playlist_names_into_download_jobs(pending_queue_updates)
                    # queue_tagged_count = Anzahl unique Tracks (nicht dj_ids)
                    # die wirklich neue Namen erhielten. Tracks mit mehrfach-
                    # match auf verschiedene dj_ids zählen einmal — solange
                    # mind. ein dj_id added > 0 hatte.
                    tagged_dj_ids = {jid for jid, added in merge_results.items() if added > 0}
                    seen_tracks: Set[Tuple[str, ...]] = set()
                    for dj_id, names in tracks_with_queue_match:
                        if dj_id in tagged_dj_ids:
                            track_key = (dj_id,)  # einfach via dj_id; same dj kann nicht doppelt tagged werden
                            if track_key not in seen_tracks:
                                seen_tracks.add(track_key)
                                queue_tagged_count += 1
                    print(
                        f"[import {job_id}] queue-tag batch: "
                        f"{queue_tagged_count} tracks tagged across {len(tagged_dj_ids)} download_jobs",
                        flush=True,
                    )
                except Exception as e:
                    print(f"[import {job_id}] bulk queue-tag failed: {type(e).__name__}: {e}", flush=True)
            if misses_not_found:
                insert_import_results(job_id, "not_found", misses_not_found)

            # Reconcile: library_match-Tracks zu Subsonic-Playlists. Lazy-
            # Import um circular dep (app→worker) zu vermeiden — zur Lauf-
            # zeit ist app.py voll geladen.
            recon_summary = {"playlists": 0, "tracks_added": 0}
            # Pre-Reconcile-Status — User sieht klar dass Phase 0 + Queue-Tag
            # durch sind und jetzt der Subsonic-API-Walk losgeht.
            upsert_import_job(
                job_id,
                status="processing",
                total=total,
                processed=total,
                found=len(library_hits),
                not_found=len(misses_not_found),
                playlist_queue_tagged=queue_tagged_count,
                message=(
                    f"Queue-tagging done ({queue_tagged_count} tracks tagged). "
                    f"Reconciling Subsonic-Playlists..."
                ),
            )

            # Callback für progressive Reconcile-Updates — der Subsonic-API-
            # Walk kann bei vielen Playlists (200+) Minuten dauern. User soll
            # sehen welche Playlist gerade dran ist, nicht raten was passiert.
            def _on_playlist_progress(idx: int, total_pl: int, name: str) -> None:
                try:
                    short = name[:40] + "…" if len(name) > 40 else name
                    upsert_import_job(
                        job_id,
                        status="processing",
                        message=f"Reconcile {idx + 1}/{total_pl}: '{short}'",
                    )
                except Exception:
                    pass

            try:
                from app import _reconcile_import_library_matches
                recon_summary = _reconcile_import_library_matches(
                    job_id, on_playlist_progress=_on_playlist_progress
                )
            except Exception as e:
                print(f"[import {job_id}] playlist_sync reconcile failed: {type(e).__name__}: {e}", flush=True)

            upsert_import_job(
                job_id,
                status="completed",
                total=total,
                processed=total,
                found=len(library_hits),
                not_found=len(misses_not_found),
                phase0_progress=100,
                playlists_synced=int(recon_summary.get("playlists", 0) or 0),
                playlist_tracks_added=int(recon_summary.get("tracks_added", 0) or 0),
                playlist_queue_tagged=queue_tagged_count,
                message=(
                    f"Playlist-Sync done — {recon_summary.get('tracks_added', 0)} "
                    f"tracks added to {recon_summary.get('playlists', 0)} playlist(s), "
                    f"{queue_tagged_count} tagged in queue, "
                    f"{len(misses_not_found)} not in library/queue"
                ),
            )
            return

        # Klare Phase-Trennung im Status — sonst sieht User die ganze
        # Phase 1 (Dedup) noch unter "Phase 0: scanning library..." weil
        # Phase 1 keine eigene Status-Message hat.
        upsert_import_job(
            job_id,
            status="processing",
            total=total,
            message=(
                f"Phase 1: deduplicating {len(remaining_pending)} unique tracks "
                f"({len(library_hits)} already in library)..."
            ),
        )

        pending = remaining_pending
        if not pending:
            # Alle Tracks sind schon in der Library → fertig ohne Provider-Phase
            upsert_import_job(
                job_id,
                status="completed",
                total=total,
                processed=total,
                found=0,
                not_found=0,
                message=f"All {total} tracks already in library (Phase 0 hit-rate 100%)",
            )
            return

        # ---- Phase 1: Dedup ------------------------------------------------
        # Key = (artist_lc, title_lc) für Dedup-Stabilität. Leere Artists
        # landen als ("", title_lc), was korrekt ist — verschiedene Songs
        # mit gleichem Titel teilen sich dann nur den Lookup wenn Artist
        # gleich (oder beide leer) ist.
        # Parallel halten wir ein Mapping zur original-case-Version, weil
        # der Provider-Search mit Mixed-Case oft bessere Treffer liefert
        # als mit lowercased Strings (siehe field-based search unten).
        unique_keys: List[Tuple[str, str]] = []
        seen: Dict[Tuple[str, str], None] = {}
        key_to_original: Dict[Tuple[str, str], Tuple[str, str]] = {}
        for row in pending:
            artist_orig = (row["requested_artist"] or "").strip()
            title_orig = (row["requested_title"] or "").strip()
            key = (artist_orig.lower(), title_orig.lower())
            if key not in seen:
                seen[key] = None
                unique_keys.append(key)
                key_to_original[key] = (artist_orig, title_orig)

        unique_total = len(unique_keys)
        upsert_import_job(
            job_id,
            status="processing",
            total=total,
            processed=0,
            found=0,
            not_found=0,
            message=(
                f"Phase 2: matching {unique_total} unique tracks "
                f"(from {len(pending)} pending after library-hit, "
                f"{len(library_hits)} already in library)..."
            ),
        )

        # ---- Phase 2: Parallele Suche -------------------------------------
        cache: Dict[Tuple[str, str], Optional[Dict[str, Any]]] = {}

        def _is_transient(exc: Exception) -> bool:
            """Klassifiziert Provider-Fehler in transient (retry-würdig)
            vs. permanent (Track existiert wirklich nicht).

            Transient → 429, alle 5xx, ConnectionError, Timeout, ReadTimeout.
            Permanent → 4xx außer 429 (typisch 400/404 für ungültige Query),
            JSON-Decode-Errors, oder None-Result bei 200.

            Vorher hat der Code bei jeder HTTPError != 429 sofort 'unmatched'
            geschrieben — das hat tausende valide Tracks bei Bad-Gateway-
            Bursts oder VPN-Reconnect-Hicksern silent verloren.
            """
            if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
                return True
            if isinstance(exc, requests.HTTPError):
                status = getattr(exc.response, "status_code", None)
                return status == 429 or (status is not None and 500 <= status < 600)
            return False

        def _do_search(
            key: Tuple[str, str], lane: str = "default"
        ) -> Tuple[Tuple[str, str], Optional[Dict[str, Any]], str, bool]:
            """Sucht einen einzelnen Track mit zweistufiger Match-Strategie.

            Stufe 1 (Field-Search): Wenn artist UND title vorhanden sind,
            nutzt Deezers field-syntax `artist:"X" track:"Y"`. Das ist ein
            präziser Match — Deezer filtert auf exakten Artist + Title-Match
            statt Free-Text-Relevance-Roulette. Trifft den richtigen Track
            auch wenn er textlich verschüttet wäre. Mixed-Case (nicht
            lowercased) liefert empirisch bessere Treffer als alles-klein.

            Stufe 2 (Free-Text-Fallback): Wenn Field-Search 0 Treffer liefert
            (z.B. weil der Artist im CSV anders geschrieben ist als bei
            Deezer — "Beatles" vs "The Beatles"), fallback auf Free-Text mit
            höherem Limit. Ranking ist dann Deezers Job.

            Failure-Handling bei transient errors:
            - Dual-Lane: einmalig auf die andere Lane retried (Failover,
              andere Source-IP, kein Backoff)
            - Single-Lane: exponential-backoff via _CSV_429_RETRY_DELAYS

            Permanent-Fehler (4xx außer 429, malformed response, beide
            Stufen 0 Treffer) → unmatched.

            Returnt (key, result, lane_used, was_failover).
            """
            artist_orig, title_orig = key_to_original.get(key, ("", ""))

            def _attempt(this_lane: str) -> Optional[Dict[str, Any]]:
                # Stufe 1: Field-Search wenn beide Felder da sind. Limit 5
                # reicht — Deezer rankt bei field-search nach exact-match.
                if artist_orig and title_orig:
                    field_q = f'artist:"{artist_orig}" track:"{title_orig}"'
                    results = svc.search_tracks(field_q, limit=5, source=this_lane)
                    if results:
                        return results[0]
                # Stufe 2: Free-Text-Fallback. Höheres Limit weil Deezer
                # bei Free-Text Tracks mit ähnlichem Titel hochrankt — der
                # gewünschte Treffer kann auf Position 5-15 liegen wenn
                # der Artist-Name nicht-Standard ist.
                if artist_orig and title_orig:
                    free_q = f"{artist_orig} {title_orig}"
                elif title_orig:
                    free_q = title_orig
                else:
                    free_q = artist_orig
                if not free_q.strip():
                    return None
                results = svc.search_tracks(
                    free_q, limit=max(search_limit, 15), source=this_lane
                )
                return results[0] if results else None

            try:
                return key, _attempt(lane), lane, False
            except Exception as e:
                if not _is_transient(e):
                    # Permanent — z.B. 400/404 bei kaputter Query, oder
                    # JSON-Decode-Fehler im Response. Kein Sinn zu retryen.
                    return key, None, lane, False
                # Transient → Failover-Strategie je nach Setup
                if lane in _CSV_LANES:
                    other = _CSV_LANES[(_CSV_LANES.index(lane) + 1) % len(_CSV_LANES)]
                    try:
                        return key, _attempt(other), other, True
                    except Exception:
                        return key, None, lane, False
                # Single-Lane: exponential backoff in-place.
                for delay in _CSV_429_RETRY_DELAYS:
                    time.sleep(delay)
                    try:
                        return key, _attempt(lane), lane, False
                    except Exception as e2:
                        if not _is_transient(e2):
                            return key, None, lane, False
                        continue
                return key, None, lane, False

        completed_unique = 0
        # Per-Lane-Aggregat: served = wieviele Searches lieferte diese Lane final,
        # failovers_in = wie oft hat diese Lane einen 429-Failover übernommen.
        # Wird in der finalen Status-Message angehängt — gibt Sichtbarkeit, ob
        # beide Lanes wirklich Last bekommen und wie oft Cross-Lane-Retry zog.
        lane_keys = list(_CSV_LANES) if _VPN_SPLIT_ENABLED else ["default"]
        lane_served: Dict[str, int] = {l: 0 for l in lane_keys}
        lane_failover_in: Dict[str, int] = {l: 0 for l in lane_keys}
        failover_total = 0

        with ThreadPoolExecutor(max_workers=_CSV_SEARCH_CONCURRENCY) as pool:
            if _VPN_SPLIT_ENABLED:
                futures = [
                    pool.submit(_do_search, k, _CSV_LANES[idx % len(_CSV_LANES)])
                    for idx, k in enumerate(unique_keys)
                ]
            else:
                futures = [pool.submit(_do_search, k, "default") for k in unique_keys]
            for fut in as_completed(futures):
                # Bei Shutdown laufende Futures abrechen so weit möglich.
                if self._stop.is_set():
                    for f in futures:
                        f.cancel()
                    upsert_import_job(job_id, status="error", message="Interrupted")
                    return
                # User-Cancel-Check zwischen Futures — Phase 2 ist die
                # längste Phase (kann mehrere Minuten dauern bei großen
                # Imports), Cancel muss hier instant wirken.
                if _check_user_cancel(int(0.70 * total * completed_unique / max(1, unique_total))):
                    for f in futures:
                        f.cancel()
                    return
                try:
                    key, result, lane_used, was_failover = fut.result()
                except Exception:
                    continue
                cache[key] = result
                completed_unique += 1
                if lane_used in lane_served:
                    lane_served[lane_used] += 1
                if was_failover:
                    failover_total += 1
                    if lane_used in lane_failover_in:
                        lane_failover_in[lane_used] += 1

                if completed_unique % _CSV_PROGRESS_EVERY == 0:
                    # Phase-Weighting für die Progress-Bar: Initial belegt
                    # 70 % der Bar, Recovery (falls vorhanden) belegt 25 %,
                    # die DB-Materialisierung-Phase 5 %. Damit hängt die Bar
                    # nicht auf 100 %, während Recovery noch 90 s rennt.
                    est_processed = int(0.70 * total * completed_unique / max(1, unique_total))
                    # Live-Counter: cache hat alle bisherigen Lookup-Ergebnisse
                    # (None = nichts gefunden, dict = Treffer). Auf row-count
                    # skalieren, weil die UI matched/not_found als "von total"
                    # interpretiert. Bei perfekter Dedup-Rate stimmt das
                    # exakt überein, sonst ist es eine knappe Estimate, die
                    # in Phase 3 (Materialisierung) auf den echten Row-Count
                    # korrigiert wird.
                    matched_unique = sum(1 for v in cache.values() if v is not None)
                    unmatched_unique = completed_unique - matched_unique
                    scale = total / max(1, unique_total)
                    est_found = int(matched_unique * scale)
                    est_not_found = int(unmatched_unique * scale)
                    if _VPN_SPLIT_ENABLED:
                        lane_str = (
                            f" (lane A: {lane_served.get('a', 0)}, "
                            f"lane B: {lane_served.get('b', 0)}, "
                            f"failovers: {failover_total})"
                        )
                    else:
                        lane_str = ""
                    upsert_import_job(
                        job_id,
                        status="processing",
                        total=total,
                        processed=min(est_processed, total),
                        found=est_found,
                        not_found=est_not_found,
                        message=f"Matched {completed_unique}/{unique_total} unique tracks{lane_str}...",
                    )

        # ---- Phase 2.5: Recovery-Pass (zweistufig) -----------------------
        # Deezer (und ähnliche Music-APIs) reagiert auf Burst-Loads NICHT immer
        # mit 429, sondern oft mit `200 OK + data:[]` als Soft-Throttle. Mein
        # _is_transient-Check fängt das nicht — leeres Array sieht aus wie
        # "Track existiert nicht". Empirisch (User-Re-Check zeigt valide
        # Treffer für Tracks die Phase 2 als unmatched markiert hat) ist das
        # die Hauptursache für ~30 % "False-Negatives" im Initial-Pass.
        #
        # Zweistufige Recovery (seit 2026-05-10):
        #
        # **Phase 2.5a (Fast)** — gleiche Concurrency wie Phase 2 (Thread-Pool
        # + Lane-Splitting), kurzer 5s-Cooldown vorab. Hypothese: viele Misses
        # sind nur temporär (kurze 429-Welle, Network-Glitch, Soft-Throttle
        # der nach 5s vorbei ist). Schnell-Retry fängt 60-80 % davon.
        #
        # **Phase 2.5b (Slow)** — die nach Phase 2.5a noch verbleibenden Keys.
        # Längerer 15s-Cooldown + sequenzieller Re-Search mit 0.4s zwischen
        # Calls. Hypothese: hartnäckige Soft-Throttles brauchen sanfte
        # Behandlung. Fängt die echten Edge-Cases.
        #
        # Bar-Weighting: Phase 2 endet bei 70 %, Fast-Recovery 70-80 %,
        # Slow-Recovery 80-95 %, Phase 3 (DB-write) 95-100 %.
        initial_recovery_keys = [k for k in unique_keys if cache.get(k) is None]
        recovery_recovered = 0
        recovery_total_n = len(initial_recovery_keys)
        phase2_end = int(0.70 * total)
        fast_recovery_end = int(0.80 * total)
        no_recovery_end = int(0.95 * total)

        if initial_recovery_keys:
            # ── Phase 2.5a: Fast Recovery ─────────────────────────────
            fast_cooldown_s = 5
            upsert_import_job(
                job_id,
                status="processing",
                total=total,
                processed=phase2_end,
                recovery_total=recovery_total_n,
                recovery_recovered=0,
                message=(
                    f"Recovery (fast): {recovery_total_n} Initial-Misses werden "
                    f"parallel re-geprüft nach {fast_cooldown_s}s Cooldown..."
                ),
            )
            for _ in range(fast_cooldown_s):
                if self._stop.is_set():
                    upsert_import_job(job_id, status="error", message="Interrupted")
                    return
                if _check_user_cancel(phase2_end):
                    return
                time.sleep(1)

            fast_done = 0
            with ThreadPoolExecutor(max_workers=_CSV_SEARCH_CONCURRENCY) as pool:
                if _VPN_SPLIT_ENABLED:
                    futures = [
                        pool.submit(_do_search, k, _CSV_LANES[idx % len(_CSV_LANES)])
                        for idx, k in enumerate(initial_recovery_keys)
                    ]
                else:
                    futures = [
                        pool.submit(_do_search, k, "default")
                        for k in initial_recovery_keys
                    ]
                for fut in as_completed(futures):
                    if self._stop.is_set():
                        for f in futures:
                            f.cancel()
                        upsert_import_job(job_id, status="error", message="Interrupted")
                        return
                    if _check_user_cancel(phase2_end + fast_done):
                        for f in futures:
                            f.cancel()
                        return
                    try:
                        key, result, _, _ = fut.result()
                    except Exception:
                        continue
                    fast_done += 1
                    if result is not None:
                        cache[key] = result
                        recovery_recovered += 1
                    if fast_done % _CSV_PROGRESS_EVERY == 0:
                        # Fast-Anteil: 10 % der Bar zwischen 70 % und 80 %.
                        fast_share = int(0.10 * total * fast_done / recovery_total_n)
                        matched_unique = sum(1 for v in cache.values() if v is not None)
                        unmatched_unique = unique_total - matched_unique
                        scale = total / max(1, unique_total)
                        upsert_import_job(
                            job_id,
                            status="processing",
                            total=total,
                            processed=min(phase2_end + fast_share, fast_recovery_end),
                            found=int(matched_unique * scale),
                            not_found=int(unmatched_unique * scale),
                            recovery_recovered=recovery_recovered,
                            message=(
                                f"Recovery (fast): {fast_done}/{recovery_total_n} re-checked, "
                                f"+{recovery_recovered} zusätzlich gefunden..."
                            ),
                        )

            # ── Phase 2.5b: Slow Recovery ─────────────────────────────
            slow_recovery_keys = [k for k in initial_recovery_keys if cache.get(k) is None]
            if slow_recovery_keys:
                slow_cooldown_s = 15
                slow_total_n = len(slow_recovery_keys)
                upsert_import_job(
                    job_id,
                    status="processing",
                    total=total,
                    processed=fast_recovery_end,
                    recovery_recovered=recovery_recovered,
                    message=(
                        f"Recovery (slow): {slow_total_n} verbleibende Misses werden "
                        f"nach {slow_cooldown_s}s Cooldown sequenziell re-geprüft..."
                    ),
                )
                for _ in range(slow_cooldown_s):
                    if self._stop.is_set():
                        upsert_import_job(job_id, status="error", message="Interrupted")
                        return
                    if _check_user_cancel(fast_recovery_end):
                        return
                    time.sleep(1)

                # Sequenziell, default lane, 0.4s zwischen Calls.
                for idx, k in enumerate(slow_recovery_keys):
                    if self._stop.is_set():
                        upsert_import_job(job_id, status="error", message="Interrupted")
                        return
                    if _check_user_cancel(fast_recovery_end + idx):
                        return
                    try:
                        _, result, _, _ = _do_search(k, "default")
                    except Exception:
                        result = None
                    if result is not None:
                        cache[k] = result
                        recovery_recovered += 1
                    time.sleep(0.4)
                    if (idx + 1) % 5 == 0:
                        # Slow-Anteil: 15 % der Bar zwischen 80 % und 95 %.
                        slow_share = int(0.15 * total * (idx + 1) / slow_total_n)
                        matched_unique = sum(1 for v in cache.values() if v is not None)
                        unmatched_unique = unique_total - matched_unique
                        scale = total / max(1, unique_total)
                        upsert_import_job(
                            job_id,
                            status="processing",
                            total=total,
                            processed=min(fast_recovery_end + slow_share, no_recovery_end),
                            found=int(matched_unique * scale),
                            not_found=int(unmatched_unique * scale),
                            recovery_recovered=recovery_recovered,
                            message=(
                                f"Recovery (slow): {idx + 1}/{slow_total_n} re-checked, "
                                f"+{recovery_recovered} total recovered..."
                            ),
                        )
        else:
            # Kein Recovery nötig — direkt zur 95 %-Marke springen, Phase 3
            # erledigt den letzten Schritt zu 100 %.
            upsert_import_job(
                job_id,
                status="processing",
                total=total,
                processed=no_recovery_end,
                message=f"Initial-Pass clean, materialisiere {total} Rows...",
            )

        # ---- Phase 3: Materialisieren + DB-Inserts ------------------------
        batch_matched: list = []
        batch_unmatched: list = []
        processed = 0
        total_matched = 0
        total_unmatched = 0

        def _flush() -> None:
            nonlocal batch_matched, batch_unmatched
            if batch_matched:
                insert_import_results(job_id, "matched", batch_matched)
                batch_matched = []
            if batch_unmatched:
                insert_import_results(job_id, "unmatched", batch_unmatched)
                batch_unmatched = []

        for row in pending:
            artist = row["requested_artist"] or ""
            title = row["requested_title"] or ""
            key = (artist.strip().lower(), title.strip().lower())
            track = cache.get(key)
            # Phase I: playlist_names aus pending_raw durchreichen — sowohl in
            # matched (für queue_all → import_playlist_names im Job-Payload)
            # als auch in unmatched (Frontend kann zeigen "auf welcher
            # Playlist hätte dieser Track gestanden").
            try:
                playlist_names = _wjson.loads(row["playlist_names_json"]) if row["playlist_names_json"] else []
            except Exception:
                playlist_names = []

            if track:
                batch_matched.append({
                    "original": row["original"],
                    "requested_artist": artist,
                    "requested_title": title,
                    "track": track,
                    "playlist_names": playlist_names,
                })
                total_matched += 1
            else:
                batch_unmatched.append({
                    "original": row["original"],
                    "requested_artist": artist,
                    "requested_title": title,
                    "playlist_names": playlist_names,
                })
                total_unmatched += 1

            processed += 1

            if processed % _CSV_FLUSH_BATCH == 0:
                _flush()
                # Cancel-Check zwischen Materialize-Batches — Phase 3 ist
                # zwar schnell, aber bei großen Imports (30k+ Rows) lohnt
                # es trotzdem damit Cancel innerhalb 1-2 Sekunden wirkt.
                if _check_user_cancel(processed):
                    return
                upsert_import_job(
                    job_id,
                    status="processing",
                    total=total,
                    processed=processed,
                    found=total_matched,
                    not_found=total_unmatched,
                    message=f"Writing {processed}/{total} rows...",
                )

        _flush()

        counts_conn = _db()
        try:
            matched_count = counts_conn.execute(
                "SELECT COUNT(*) AS n FROM import_results WHERE job_id = ? AND result_type = 'matched'",
                (job_id,),
            ).fetchone()["n"]
            unmatched_count = counts_conn.execute(
                "SELECT COUNT(*) AS n FROM import_results WHERE job_id = ? AND result_type = 'unmatched'",
                (job_id,),
            ).fetchone()["n"]
        finally:
            counts_conn.close()

        cleanup_conn = _db()
        try:
            cleanup_conn.execute(
                "DELETE FROM import_results WHERE job_id = ? AND result_type = 'claimed'",
                (job_id,),
            )
            cleanup_conn.commit()
        finally:
            cleanup_conn.close()

        # Lane-Aggregat in die Final-Message hängen — gibt sofortige Sichtbarkeit
        # über Per-Lane-Verteilung im UI/API ohne in den Container zu schauen.
        if _VPN_SPLIT_ENABLED:
            lane_summary = (
                f" · Lane A served {lane_served.get('a', 0)} "
                f"(failover-in {lane_failover_in.get('a', 0)}), "
                f"Lane B served {lane_served.get('b', 0)} "
                f"(failover-in {lane_failover_in.get('b', 0)}), "
                f"{failover_total} cross-lane failovers"
            )
        else:
            lane_summary = ""

        # Recovery-Aggregat — zeigt wie viel der Burst-Soft-Throttle gekostet
        # hat. Hohe recovery_recovered-Zahl = Burst-Limit war ein echtes
        # Problem im initial-Pass; bei 0 = Initial-Pass war sauber.
        if recovery_keys:
            recovery_summary = (
                f" · Recovery: {recovery_recovered}/{len(recovery_keys)} "
                f"Initial-Misses gerettet"
            )
        else:
            recovery_summary = ""

        # Stdout-Log fürs Aggregat — taucht in `docker logs tonus` auf, ist
        # für Post-Mortem von großen Imports nützlich (UI zeigt nur die letzte
        # Status-Message, der Container-Log behält die Historie).
        print(
            f"[csv-search] job={job_id} matched={matched_count} unmatched={unmatched_count} "
            f"unique={unique_total} dupes={total - unique_total}"
            + (
                f" lane_a_served={lane_served.get('a', 0)} lane_b_served={lane_served.get('b', 0)} "
                f"failover_a={lane_failover_in.get('a', 0)} failover_b={lane_failover_in.get('b', 0)}"
                if _VPN_SPLIT_ENABLED else ""
            )
        )

        upsert_import_job(
            job_id,
            status="completed",
            total=total,
            processed=total,
            found=matched_count,
            not_found=unmatched_count,
            message=(
                f"Done: {matched_count} matched, {unmatched_count} not found "
                f"({unique_total} unique queries, {total - unique_total} duplicates skipped)"
                f"{lane_summary}{recovery_summary}"
            ),
        )

    # ------------------------------------------------------------------
    # Download execution
    # ------------------------------------------------------------------

    def _process_download(self, job: Dict[str, Any], lane: str = "default") -> None:
        track_id: str = job["job_id"]
        params: Dict[str, Any] = job.get("params", {})

        # source_lane an download_and_process: "a"/"b" → yt-dlp source_address +
        # Deezer source-bind. "default" → kein Bind (Status-quo-Verhalten).
        propagated_lane = lane if lane in ("a", "b") else None

        # Lane-Tracking für die UI: _lane_current_job[lane] wurde bereits
        # in _poll_next_queued_download direkt nach dem SQL-COMMIT gesetzt
        # (gleicher Stack-Frame, kein Race-Window). Wir geben es erst nach
        # dem Setzen der Cooldown wieder frei — siehe Reorder-Block unten.
        try:
            with self._lock:
                from app import download_and_process

                download_and_process(
                    track_id=track_id,
                    location=params.get("location", "local"),
                    video_id=params.get("video_id"),
                    output_format=params.get("output_format"),
                    audio_quality=params.get("audio_quality"),
                    metadata_provider=params.get("metadata_provider", "deezer"),
                    max_retries=params.get("max_retries", 0),
                    navidrome_library_path=params.get("navidrome_library_path"),
                    source_lane=propagated_lane,
                )
        finally:
            # ----- Per-Lane-Cooldown -----
            # Greift IMMER, egal ob success oder error. Bei 429 wird's deutlich
            # länger, damit Retry-Wellen YouTube nicht weiter eskalieren. Im
            # Dual-Lane-Modus läuft der Cooldown pro Lane separat — die andere
            # Lane kann sofort den nächsten Job picken (siehe
            # _pick_download_lane). Das halbiert effektiv die Idle-Zeit
            # zwischen Downloads, ohne parallele yt-dlp-Prozesse.
            #
            # Wichtig fürs UI: ready_at MUSS gesetzt werden BEVOR
            # current_job_id auf None geht. Sonst gibt es ein Frame in dem
            # die Lane als "idle, score=0" erscheint (kein Job, keine
            # Cooldown), was das Frontend als "frei → assignt queuedJobs[0]"
            # interpretiert und visuelle Sprünge zwischen Lanes auslöst.
            finished = get_job(track_id) or {}
            normal_range, rl_range = _load_cooldown_ranges()
            if _looks_like_429(finished.get("message", ""), finished.get("error", "")):
                lo, hi = rl_range
                cooldown = random.uniform(lo, hi)
                print(f"[worker] 429 on '{track_id}' (lane {lane}) — extended cooldown {cooldown:.0f}s")
            else:
                lo, hi = normal_range
                cooldown = random.uniform(lo, hi)
                tag = finished.get("status", "?")
                print(f"[worker] lane {lane} cooldown {cooldown:.0f}s (last status={tag})")

            self._lane_ready_at[lane] = _now_ms() + int(cooldown * 1000)
            # ATOMIC für die UI: ready_at ist gesetzt, JETZT erst current_job_id
            # freigeben — Lane geht nahtlos vom 'processing'-State in den
            # 'cooldown'-State, ohne zwischenzeitliches "idle"-Snapshot.
            self._lane_current_job[lane] = None

        # Kurze Yield-Pause, damit run() in einem stoppable-sleep landet wenn
        # beide Lanes Cooldown haben (sonst busy-Loop).
        self._stop.wait(timeout=0.1)
