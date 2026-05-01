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
from typing import Any, Dict, List, Optional, Tuple

import requests  # für HTTPError-Detection im 429-Failover (Dual-VPN-Splitting)

from utils.job_store import (
    _db,
    _now_ms,
    upsert_job,
    upsert_csv_job,
    insert_csv_results,
    get_job,
)


# Cooldown-Bereiche
_COOLDOWN_NORMAL = (60, 300)        # 1–5 min nach success / unauffälligem error
_COOLDOWN_429 = (300, 600)          # 5–10 min nach erkanntem 429

# CSV-Match-Tuning. 8 parallele Suchen liegen bei Deezer (~50 req / 5s soft-limit)
# komfortabel drunter. DB-Inserts in 500er Chunks halten SQLite responsiv.
_CSV_SEARCH_CONCURRENCY = 8
_CSV_FLUSH_BATCH = 500
_CSV_PROGRESS_EVERY = 50            # alle N abgeschlossene Unique-Suchen Status updaten

# Dual-VPN-Splitting: gerade Thread-Indizes nutzen Lane A, ungerade Lane B —
# pro Lane bindet services.deezer._get_session(...) eine andere Source-IP.
# Bei VPN_SPLIT_ENABLED=false fällt alles auf "default" zurück (kein Bind).
_CSV_LANES: Tuple[str, str] = ("a", "b")
_VPN_SPLIT_ENABLED = os.environ.get("VPN_SPLIT_ENABLED", "").strip().lower() == "true"

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
    """Generischer Worker — job_type="download" oder "csv"."""

    def __init__(self, job_type: str) -> None:
        super().__init__(daemon=True, name=f"worker-{job_type}")
        self._job_type = job_type
        self._stop: threading.Event = threading.Event()
        self._lock: threading.Lock = threading.Lock()
        # Per-Lane "ready_at" (ms): wann ist die Lane wieder benutzbar (Cooldown vorbei)?
        # 0 = sofort nutzbar. Wird von _process_download nach jedem Job gesetzt.
        self._lane_ready_at: Dict[str, int] = {l: 0 for l in _DOWNLOAD_LANES}
        # Round-robin-Tiebreaker wenn beide Lanes gleichzeitig ready sind.
        self._lane_rr_idx: int = 0

    # ------------------------------------------------------------------
    # Lane selection (Download-Worker, Dual-VPN)
    # ------------------------------------------------------------------

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
        while not self._stop.is_set():
            if self._job_type == "download":
                lane, wait_ms = self._pick_download_lane()
                if lane is None:
                    # Alle Lanes im Cooldown — stoppable-sleep auf die kürzeste
                    # Restwartezeit, dann erneut prüfen.
                    self._stop.wait(timeout=max(0.5, wait_ms / 1000.0))
                    continue
                job = self._poll_next_queued_download()
                if job:
                    self._process_download(job, lane=lane)
                    continue
            else:
                csv_job = self._poll_next_queued_csv()
                if csv_job:
                    self._process_csv_import(csv_job)
                    continue
            self._stop.wait(timeout=2.0)

    def shutdown(self, timeout: Optional[float] = None) -> None:
        self._stop.set()
        self.join(timeout=timeout)

    # ------------------------------------------------------------------
    # Download job polling
    # ------------------------------------------------------------------

    def _poll_next_queued_download(self) -> Optional[Dict[str, Any]]:
        conn = _db()
        try:
            row = conn.execute(
                """
                SELECT job_id, payload_json
                FROM download_jobs
                WHERE status = 'queued'
                ORDER BY created_at_ms ASC
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

    def _poll_next_queued_csv(self) -> Optional[Dict[str, Any]]:
        conn = _db()
        try:
            row = conn.execute(
                """
                SELECT job_id, message, total
                FROM csv_import_jobs
                WHERE status = 'queued'
                ORDER BY created_at_ms ASC
                LIMIT 1
                """
            ).fetchone()
            if not row:
                return None

            conn.execute(
                "UPDATE csv_import_jobs SET status='processing', updated_at_ms=? WHERE job_id=?",
                (_now_ms(), row["job_id"]),
            )
            conn.commit()

            provider = "deezer"
            search_limit = 3
            msg = row["message"] or ""
            parts = msg.split("|")
            if len(parts) >= 2:
                provider = parts[0]
                try:
                    search_limit = int(parts[1])
                except ValueError:
                    pass

            return {
                "job_id": row["job_id"],
                "provider": provider,
                "search_limit": search_limit,
                "total": row["total"],
            }
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # CSV import execution
    # ------------------------------------------------------------------

    def _process_csv_import(self, csv_job: Dict[str, Any]) -> None:
        """Match pending_raw items against Deezer, write results to SQLite.

        Performance-Architektur:
        1. Dedup nach (artist_lc, title_lc) — bei History-Exports oft 30–50%
           Doppelte. Nur eindeutige Keys werden tatsächlich an Deezer gefragt.
        2. Parallele Suche per ThreadPoolExecutor (8 Worker). Deezer hat ein
           weiches Limit von ~50 req / 5 s; 8 Worker mit ~300–500 ms je Call
           landen unter ~24 req/s.
        3. Erst nachdem der Cache vollständig ist, werden alle Original-Rows
           (inkl. Duplikate) materialisiert und in 500er-Chunks geschrieben.
        """
        job_id: str = csv_job["job_id"]
        provider: str = csv_job["provider"]
        search_limit: int = csv_job["search_limit"]
        total: int = csv_job["total"]

        from services.deezer import DeezerService
        from services.spotify import SpotifyService

        if provider == "deezer":
            svc = DeezerService()
        else:
            svc = SpotifyService()
        if svc is None:
            upsert_csv_job(job_id, status="error", message=f"Provider '{provider}' not available")
            return

        # Claim pending raw items (prevent re-processing on restart)
        conn = _db()
        try:
            pending = conn.execute(
                "SELECT id, original, requested_artist, requested_title FROM csv_import_results WHERE job_id = ? AND result_type = 'pending_raw' ORDER BY id",
                (job_id,),
            ).fetchall()
            if pending:
                conn.execute(
                    "UPDATE csv_import_results SET result_type = 'claimed' WHERE job_id = ? AND result_type = 'pending_raw'",
                    (job_id,),
                )
                conn.commit()
        finally:
            conn.close()

        if not pending:
            upsert_csv_job(job_id, status="error", message="No pending items found")
            return

        # ---- Phase 1: Dedup ------------------------------------------------
        # Key = (artist_lc, title_lc). Leere Artists landen als ("", title_lc),
        # was korrekt ist — verschiedene Songs mit gleichem Titel teilen sich
        # dann nur den Lookup wenn Artist gleich (oder beide leer) ist.
        unique_keys: List[Tuple[str, str]] = []
        seen: Dict[Tuple[str, str], None] = {}
        for row in pending:
            key = (
                (row["requested_artist"] or "").strip().lower(),
                (row["requested_title"] or "").strip().lower(),
            )
            if key not in seen:
                seen[key] = None
                unique_keys.append(key)

        unique_total = len(unique_keys)
        upsert_csv_job(
            job_id,
            status="processing",
            total=total,
            processed=0,
            found=0,
            not_found=0,
            message=f"Matching {unique_total} unique tracks (from {total} rows, {total - unique_total} dupes)...",
        )

        # ---- Phase 2: Parallele Suche -------------------------------------
        cache: Dict[Tuple[str, str], Optional[Dict[str, Any]]] = {}

        def _do_search(
            key: Tuple[str, str], lane: str = "default"
        ) -> Tuple[Tuple[str, str], Optional[Dict[str, Any]], str, bool]:
            """Sucht einen einzelnen Track. Bei 429 auf der zugewiesenen Lane wird
            einmalig auf die andere Lane retried (Failover). Returnt
            (key, result, lane_used, was_failover) — lane_used ist die Lane,
            die das Result final geliefert hat (oder die ursprüngliche bei
            finalem Fehlschlag); was_failover=True heißt, dass ein Lane-Wechsel
            wegen 429 stattgefunden hat (relevant fürs Aggregat-Logging).
            """
            artist_lc, title_lc = key
            query = f"{artist_lc} {title_lc}".strip() if artist_lc else title_lc

            def _attempt(this_lane: str) -> Optional[Dict[str, Any]]:
                results = svc.search_tracks(query, limit=search_limit, source=this_lane)
                return results[0] if results else None

            try:
                return key, _attempt(lane), lane, False
            except requests.HTTPError as e:
                status = getattr(e.response, "status_code", None)
                if status == 429 and lane in _CSV_LANES:
                    other = _CSV_LANES[(_CSV_LANES.index(lane) + 1) % len(_CSV_LANES)]
                    try:
                        return key, _attempt(other), other, True
                    except Exception:
                        return key, None, lane, False
                return key, None, lane, False
            except Exception:
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
                    upsert_csv_job(job_id, status="error", message="Interrupted")
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
                    # Geschätzter Gesamtfortschritt: Anteil der gemappten
                    # Unique-Keys, hochgerechnet auf alle Rows. Endgültiger
                    # Wert kommt im Final-Flush unten.
                    est_processed = int(completed_unique * total / max(1, unique_total))
                    if _VPN_SPLIT_ENABLED:
                        lane_str = (
                            f" (lane A: {lane_served.get('a', 0)}, "
                            f"lane B: {lane_served.get('b', 0)}, "
                            f"failovers: {failover_total})"
                        )
                    else:
                        lane_str = ""
                    upsert_csv_job(
                        job_id,
                        status="processing",
                        total=total,
                        processed=min(est_processed, total),
                        message=f"Matched {completed_unique}/{unique_total} unique tracks{lane_str}...",
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
                insert_csv_results(job_id, "matched", batch_matched)
                batch_matched = []
            if batch_unmatched:
                insert_csv_results(job_id, "unmatched", batch_unmatched)
                batch_unmatched = []

        for row in pending:
            artist = row["requested_artist"] or ""
            title = row["requested_title"] or ""
            key = (artist.strip().lower(), title.strip().lower())
            track = cache.get(key)

            if track:
                batch_matched.append({
                    "original": row["original"],
                    "requested_artist": artist,
                    "requested_title": title,
                    "track": track,
                })
                total_matched += 1
            else:
                batch_unmatched.append({
                    "original": row["original"],
                    "requested_artist": artist,
                    "requested_title": title,
                })
                total_unmatched += 1

            processed += 1

            if processed % _CSV_FLUSH_BATCH == 0:
                _flush()
                upsert_csv_job(
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
                "SELECT COUNT(*) AS n FROM csv_import_results WHERE job_id = ? AND result_type = 'matched'",
                (job_id,),
            ).fetchone()["n"]
            unmatched_count = counts_conn.execute(
                "SELECT COUNT(*) AS n FROM csv_import_results WHERE job_id = ? AND result_type = 'unmatched'",
                (job_id,),
            ).fetchone()["n"]
        finally:
            counts_conn.close()

        cleanup_conn = _db()
        try:
            cleanup_conn.execute(
                "DELETE FROM csv_import_results WHERE job_id = ? AND result_type = 'claimed'",
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

        upsert_csv_job(
            job_id,
            status="completed",
            total=total,
            processed=total,
            found=matched_count,
            not_found=unmatched_count,
            message=(
                f"Done: {matched_count} matched, {unmatched_count} not found "
                f"({unique_total} unique queries, {total - unique_total} duplicates skipped)"
                f"{lane_summary}"
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

        # ----- Per-Lane-Cooldown -----
        # Greift IMMER, egal ob success oder error. Bei 429 wird's deutlich länger,
        # damit Retry-Wellen YouTube nicht weiter eskalieren. Im Dual-Lane-Modus
        # läuft der Cooldown pro Lane separat — die andere Lane kann sofort den
        # nächsten Job picken (siehe _pick_download_lane). Das halbiert effektiv
        # die Idle-Zeit zwischen Downloads, ohne parallele yt-dlp-Prozesse.
        finished = get_job(track_id) or {}
        if _looks_like_429(finished.get("message", ""), finished.get("error", "")):
            lo, hi = _COOLDOWN_429
            cooldown = random.uniform(lo, hi)
            print(f"[worker] 429 on '{track_id}' (lane {lane}) — extended cooldown {cooldown:.0f}s")
        else:
            lo, hi = _COOLDOWN_NORMAL
            cooldown = random.uniform(lo, hi)
            tag = finished.get("status", "?")
            print(f"[worker] lane {lane} cooldown {cooldown:.0f}s (last status={tag})")

        # Cooldown am Lane-Slot ablegen — die andere Lane bleibt durch das
        # while-Loop in run() weiterhin pickbar; kein blocking-sleep mehr hier.
        self._lane_ready_at[lane] = _now_ms() + int(cooldown * 1000)
        # Kurze Yield-Pause, damit run() in einem stoppable-sleep landet wenn
        # beide Lanes Cooldown haben (sonst busy-Loop).
        self._stop.wait(timeout=0.1)
