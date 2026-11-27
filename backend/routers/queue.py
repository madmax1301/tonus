"""Queue-Endpoints: Liste, Zählungen, Lanes, Live-Stream (SSE),
Umsortieren, Abbrechen, Starten, Retry und Aufräumen.

Die Downloads selbst laufen im Download-Worker (core._download_worker);
diese Routen lesen und ändern nur die Tabelle download_jobs.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from core import _download_worker
from utils.auth import require_token
from utils.job_store import _db, _now_ms

router = APIRouter()


@router.get("/api/queue/lanes")
async def queue_lanes(_: None = Depends(require_token)):
    """Lane-Cooldown-Status für die Live-Queue-UI.

    Liefert pro Lane (`a`/`b` mit VPN-Split, sonst `default`) den Timestamp
    bis zur nächsten Verfügbarkeit + die Restzeit in ms. Plus die globalen
    Cooldown-Bereiche, damit das Frontend "Random zwischen X und Y Sekunden"
    transparent machen kann.
    """
    return _download_worker.lane_status()


@router.get("/api/queue")
async def list_queue(
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=500, ge=1, le=10000),
    status: Optional[str] = Query(default=None),
    _: None = Depends(require_token),
):
    """Return queued/active/recent jobs (paginiert).

    Default-Limit ist 500 — bei größeren Bulk-Imports (CSV mit 15k+ Tracks)
    laggt das UI sonst weil das Frontend pro Polling-Tick alle Items als
    DOM-Nodes rendert. Die Standard-Frontend-Ansicht ist der `active`-Filter
    (status='queued,processing'), der typischerweise nur 50–200 Items
    enthält. Completed/error-Listen lädt das UI nur on-demand.

    Optionaler offset für echte Pagination, optionaler status-Filter
    (csv-werte 'queued','processing','completed','error' oder kombiniert
    wie 'queued,processing'). `status_counts` zählt IMMER über ALLE
    Statuses, unabhängig vom Filter — so weiß das UI wie viele Jobs in
    jedem Bucket liegen ohne separat /api/queue/stats zu pollen.
    """
    import json as _json
    from utils.job_store import _db as _queue_db

    allowed_statuses = {'queued', 'processing', 'completed', 'error'}
    if status:
        wanted = {s.strip() for s in status.split(',') if s.strip() in allowed_statuses}
    else:
        wanted = allowed_statuses
    if not wanted:
        wanted = allowed_statuses
    placeholders = ",".join("?" * len(wanted))

    conn = _queue_db()
    try:
        total = conn.execute(
            f"SELECT COUNT(*) AS n FROM download_jobs WHERE status IN ({placeholders})",
            tuple(wanted),
        ).fetchone()["n"]

        # Sortierung in zwei Gruppen:
        #   1) processing + queued: FIFO ASC — ältester ist als nächstes zu
        #      pullen (matched Worker-ORDER BY created_at_ms ASC, rowid ASC).
        #      Beim CSV-Bulk-Import landen viele Jobs in derselben Millisekunde,
        #      also MUSS der Tiebreaker rowid ASC sein damit die UI denselben
        #      "next pull" anzeigt den der Worker tatsächlich zieht.
        #   2) completed + error: DESC — neueste Erlebnisse zuerst, klassischer
        #      Newsfeed-Style fürs Aufräumen / Debuggen.
        rows = conn.execute(
            f"""
            SELECT job_id, status, stage, progress, message, download_url,
                   created_at_ms, updated_at_ms, finished_at_ms, payload_json, rowid AS _rowid
            FROM download_jobs
            WHERE status IN ({placeholders})
            ORDER BY
              CASE status
                WHEN 'processing' THEN 1
                WHEN 'queued'     THEN 2
                WHEN 'completed'  THEN 3
                WHEN 'error'      THEN 4
                ELSE 5
              END ASC,
              CASE WHEN status IN ('queued', 'processing') THEN created_at_ms END ASC,
              CASE WHEN status IN ('queued', 'processing') THEN _rowid END ASC,
              CASE WHEN status IN ('completed', 'error')   THEN created_at_ms END DESC,
              CASE WHEN status IN ('completed', 'error')   THEN _rowid END DESC
            LIMIT ? OFFSET ?
            """,
            (*wanted, limit, offset),
        ).fetchall()

        items = []
        for row in rows:
            payload = None
            if row["payload_json"]:
                try:
                    payload = _json.loads(row["payload_json"])
                except (_json.JSONDecodeError, TypeError):
                    pass

            items.append(
                {
                    "job_id": row["job_id"],
                    "status": row["status"],
                    "stage": row["stage"],
                    "progress": row["progress"],
                    "message": row["message"],
                    "download_url": row["download_url"],
                    "created_at_ms": row["created_at_ms"],
                    "updated_at_ms": row["updated_at_ms"],
                    "finished_at_ms": row["finished_at_ms"],
                    "payload": payload,
                }
            )
        # Status-Aggregat über ALLE Statuses, nicht nur den gefilterten —
        # das UI braucht alle Counts für die Filter-Pills auch wenn der
        # aktuelle Filter z.B. nur 'queued,processing' anfragt.
        agg_rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM download_jobs GROUP BY status",
        ).fetchall()
        status_counts = {r["status"]: r["n"] for r in agg_rows}

        # Live-Block: alle processing-Jobs + Top-N queued-Jobs (FIFO).
        # Wird IMMER mitgeliefert, unabhängig vom User-Filter — damit die
        # Lane-Strip auch beim "Done"- oder "Error"-Filter sichtbar bleibt
        # ("Was läuft gerade" + "Was kommt als nächstes" sollen nie
        # ausgeblendet werden). N=4 reicht für 2 Lanes (je 1 running +
        # 1 upNext) plus etwas Puffer.
        def _row_to_item(r):
            payload = None
            if r["payload_json"]:
                try:
                    payload = _json.loads(r["payload_json"])
                except (_json.JSONDecodeError, TypeError):
                    pass
            return {
                "job_id": r["job_id"],
                "status": r["status"],
                "stage": r["stage"],
                "progress": r["progress"],
                "message": r["message"],
                "download_url": r["download_url"],
                "created_at_ms": r["created_at_ms"],
                "updated_at_ms": r["updated_at_ms"],
                "finished_at_ms": r["finished_at_ms"],
                "payload": payload,
            }

        proc_rows = conn.execute(
            """
            SELECT job_id, status, stage, progress, message, download_url,
                   created_at_ms, updated_at_ms, finished_at_ms, payload_json
            FROM download_jobs
            WHERE status = 'processing'
            ORDER BY created_at_ms ASC, rowid ASC
            """
        ).fetchall()
        head_rows = conn.execute(
            """
            SELECT job_id, status, stage, progress, message, download_url,
                   created_at_ms, updated_at_ms, finished_at_ms, payload_json
            FROM download_jobs
            WHERE status = 'queued'
            ORDER BY created_at_ms ASC, rowid ASC
            LIMIT 4
            """
        ).fetchall()
        live = {
            "processing": [_row_to_item(r) for r in proc_rows],
            "queued_head": [_row_to_item(r) for r in head_rows],
        }

        return {
            "items": items,
            "total": total,
            "shown": len(items),
            "offset": offset,
            "limit": limit,
            "status_counts": status_counts,
            "live": live,
        }
    finally:
        conn.close()


@router.get("/api/queue/stats")
async def queue_stats(_: None = Depends(require_token)):
    """Aggregat-Statistik für Dashboards/Health-Checks.

    Liefert:
      - total              Anzahl aller Jobs in non-terminal-und-terminal Statuses
      - by_status          Counter pro Status
      - oldest_queued_age_s   Sekunden seit ältester queued/processing-Job angelegt
      - avg_completed_dur_s   Mittlere Job-Dauer (created→updated) für completed Jobs der letzten 24h
      - last_completed_ms     Timestamp des zuletzt erfolgreichen Downloads
      - last_error_ms         Timestamp des zuletzt gefehlten Jobs
    """
    from utils.job_store import _db as _stats_db
    now = _now_ms()
    conn = _stats_db()
    try:
        agg = conn.execute(
            "SELECT status, COUNT(*) AS n FROM download_jobs GROUP BY status"
        ).fetchall()
        by_status = {r["status"]: r["n"] for r in agg}
        total = sum(by_status.values())

        oldest = conn.execute(
            "SELECT MIN(created_at_ms) AS m FROM download_jobs WHERE status IN ('queued','processing')"
        ).fetchone()
        oldest_age = ((now - oldest["m"]) / 1000.0) if oldest and oldest["m"] else 0

        # Mittlere Dauer der completed Jobs der letzten 24h (in s)
        cutoff = now - 24 * 3600 * 1000
        avg_dur = conn.execute(
            "SELECT AVG((updated_at_ms - created_at_ms) / 1000.0) AS d "
            "FROM download_jobs WHERE status='completed' AND updated_at_ms >= ?",
            (cutoff,),
        ).fetchone()
        avg_completed = float(avg_dur["d"] or 0) if avg_dur else 0.0

        last_ok = conn.execute(
            "SELECT MAX(updated_at_ms) AS m FROM download_jobs WHERE status='completed'"
        ).fetchone()
        last_err = conn.execute(
            "SELECT MAX(updated_at_ms) AS m FROM download_jobs WHERE status='error'"
        ).fetchone()

        return {
            "total": total,
            "by_status": by_status,
            "oldest_queued_age_s": round(oldest_age, 1),
            # Mittlere Wall-Clock-Zeit von Job-Anlage bis Completed (= Queue-Wartezeit + Download).
            # Bei langer Queue oder Re-Tries kann das hoch sein — kein reines "Download-Tempo".
            "avg_created_to_completed_s": round(avg_completed, 1),
            "last_completed_ms": (last_ok["m"] if last_ok else None),
            "last_error_ms": (last_err["m"] if last_err else None),
            "now_ms": now,
        }
    finally:
        conn.close()


# Server-Sent Events statt Polling (#93): der Stream schickt nur, was sich
# seit dem letzten Tick geändert hat. Fortschritts-Updates eines laufenden
# Downloads kommen als einzelne Job-Zeilen; das Frontend lädt die volle
# Liste nur neu, wenn sich Status-Zählungen oder die Reihenfolge ändern.
_QUEUE_EVENTS_TICK_S = 1.0
_QUEUE_EVENTS_KEEPALIVE_S = 15.0
# Mehr geänderte Zeilen pro Tick (Bulk-Import, Clear) → statt Diff ein
# resync-Signal; das Frontend holt dann einmal /api/queue.
_QUEUE_EVENTS_MAX_ROWS = 200


def _queue_snapshot_since(since_ms: int, seen: set) -> Dict[str, Any]:
    """Geänderte Jobs seit `since_ms` plus Status-Zählungen.

    `seen` enthält die job_ids, die bei exakt `since_ms` schon gemeldet
    wurden — upsert_job schreibt Millisekunden, mehrere Updates können
    denselben Zeitstempel tragen."""
    import json as _json
    from utils.job_store import _db as _events_db

    conn = _events_db()
    try:
        rows = conn.execute(
            """
            SELECT job_id, status, stage, progress, message, download_url,
                   created_at_ms, updated_at_ms, finished_at_ms, payload_json
            FROM download_jobs
            WHERE updated_at_ms >= ?
            ORDER BY updated_at_ms ASC
            LIMIT ?
            """,
            (since_ms, _QUEUE_EVENTS_MAX_ROWS + len(seen) + 1),
        ).fetchall()
        agg = conn.execute(
            "SELECT status, COUNT(*) AS n FROM download_jobs GROUP BY status"
        ).fetchall()
    finally:
        conn.close()

    rows = [r for r in rows if not (r["updated_at_ms"] == since_ms and r["job_id"] in seen)]
    resync = len(rows) > _QUEUE_EVENTS_MAX_ROWS
    jobs = []
    if not resync:
        for r in rows:
            payload = None
            if r["payload_json"]:
                try:
                    payload = _json.loads(r["payload_json"])
                except (_json.JSONDecodeError, TypeError):
                    pass
            jobs.append({
                "job_id": r["job_id"],
                "status": r["status"],
                "stage": r["stage"],
                "progress": r["progress"],
                "message": r["message"],
                "download_url": r["download_url"],
                "created_at_ms": r["created_at_ms"],
                "updated_at_ms": r["updated_at_ms"],
                "finished_at_ms": r["finished_at_ms"],
                "payload": payload,
            })

    if rows:
        new_since = max(r["updated_at_ms"] for r in rows)
        new_seen = {r["job_id"] for r in rows if r["updated_at_ms"] == new_since}
        if new_since == since_ms:
            new_seen |= seen
    else:
        new_since, new_seen = since_ms, seen
    return {
        "jobs": jobs,
        "resync": resync,
        "status_counts": {r["status"]: r["n"] for r in agg},
        "since": new_since,
        "seen": new_seen,
    }


def _lanes_fingerprint(lanes: Dict[str, Any]) -> Any:
    """remaining_ms zählt jede Sekunde runter — das ist keine Änderung,
    die der Client braucht (er zählt selbst). Verglichen wird der Rest."""
    return (
        tuple(
            (l["name"], l["ready_at_ms"], l.get("current_job_id"))
            for l in lanes.get("lanes", [])
        ),
        lanes.get("concurrency"),
        tuple(lanes.get("cooldown", {}).get("normal_seconds", [])),
        tuple(lanes.get("cooldown", {}).get("rate_limited_seconds", [])),
        tuple(sorted(lanes.get("bot_check", {}).items())),
    )


@router.get("/api/queue/events")
async def queue_events(request: Request, _: None = Depends(require_token)):
    """Live-Stream der Queue als text/event-stream.

    Jedes `queue`-Event trägt `status_counts`, geänderte `jobs`, `lanes`
    (nur wenn sich die Lane-Belegung geändert hat) und `resync` (Client
    soll /api/queue neu laden). Das erste Event kommt sofort und enthält
    Zählungen und Lanes, damit der Client nichts separat pollen muss."""
    import asyncio
    import json as _json
    from starlette.concurrency import run_in_threadpool
    from starlette.responses import StreamingResponse

    async def _stream():
        since = _now_ms()
        seen: set = set()
        last_counts = None
        last_lanes_fp = None
        last_sent = 0.0
        yield "retry: 3000\n\n"
        while not await request.is_disconnected():
            snap = await run_in_threadpool(_queue_snapshot_since, since, seen)
            since, seen = snap["since"], snap["seen"]
            lanes = _download_worker.lane_status()
            lanes_fp = _lanes_fingerprint(lanes)

            event: Dict[str, Any] = {}
            if snap["jobs"]:
                event["jobs"] = snap["jobs"]
            if snap["resync"]:
                event["resync"] = True
            if snap["status_counts"] != last_counts or event:
                event["status_counts"] = snap["status_counts"]
                last_counts = snap["status_counts"]
            if lanes_fp != last_lanes_fp:
                event["lanes"] = lanes
                last_lanes_fp = lanes_fp

            now = time.monotonic()
            if event:
                yield f"event: queue\ndata: {_json.dumps(event, separators=(',', ':'))}\n\n"
                last_sent = now
            elif now - last_sent >= _QUEUE_EVENTS_KEEPALIVE_S:
                # Kommentarzeile hält Proxies und mobile Verbindungen offen.
                yield ": keepalive\n\n"
                last_sent = now
            await asyncio.sleep(_QUEUE_EVENTS_TICK_S)

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            # nginx/Traefik puffern sonst den Stream und liefern Events in Brocken.
            "X-Accel-Buffering": "no",
        },
    )


class QueueMoveRequest(BaseModel):
    track_id: str
    direction: str  # "up" | "down"


@router.post("/api/queue/move")
async def move_queue_item(req: QueueMoveRequest, _: None = Depends(require_token)):
    """Tauscht Positionen mit Nachbar via created_at_ms (atomarer Swap)."""
    from utils.job_store import _db as _move_db
    conn = _move_db()
    try:
        # Finde den aktuellen Job
        current = conn.execute(
            "SELECT created_at_ms FROM download_jobs WHERE job_id=? AND status IN ('queued','processing')",
            (req.track_id,)
        ).fetchone()
        if not current:
            raise HTTPException(404, "Queued job not found")

        # Sortierung: Queue zeigt DESC nach created_at_ms (neueste oben)
        # "up" = nach oben schieben = created_at_ms erhöhen (neuer)
        # "down" = nach unten schieben = created_at_ms verringern (älter)
        if req.direction == "up":
            neighbor = conn.execute(
                """
                SELECT job_id, created_at_ms FROM download_jobs
                WHERE status IN ('queued','processing')
                  AND job_id != ?
                  AND created_at_ms > ?
                ORDER BY created_at_ms ASC
                LIMIT 1
                """,
                (req.track_id, current["created_at_ms"]),
            ).fetchone()
        else:
            neighbor = conn.execute(
                """
                SELECT job_id, created_at_ms FROM download_jobs
                WHERE status IN ('queued','processing')
                  AND job_id != ?
                  AND created_at_ms < ?
                ORDER BY created_at_ms DESC
                LIMIT 1
                """,
                (req.track_id, current["created_at_ms"]),
            ).fetchone()

        if not neighbor:
            return {"ok": False, "reason": "no neighbor"}

        # Atomarer Swap: beide created_at_ms tauschen
        cur_ts = current["created_at_ms"]
        nei_ts = neighbor["created_at_ms"]
        # Nutze -1 als temporärer Platzhalter, um UNIQUE-Kollision zu vermeiden
        conn.execute(
            "UPDATE download_jobs SET created_at_ms = -1 WHERE job_id = ?",
            (req.track_id,),
        )
        # updated_at_ms mitziehen, damit /api/queue/events die neue
        # Reihenfolge an andere offene Tabs meldet.
        moved_at = _now_ms()
        conn.execute(
            "UPDATE download_jobs SET created_at_ms = ?, updated_at_ms = ? WHERE job_id = ?",
            (cur_ts, moved_at, neighbor["job_id"]),
        )
        conn.execute(
            "UPDATE download_jobs SET created_at_ms = ?, updated_at_ms = ? WHERE job_id = ?",
            (nei_ts, moved_at, req.track_id),
        )
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


@router.post("/api/download/{job_id}/cancel")
async def cancel_download(job_id: str, _: None = Depends(require_token)):
    """Cancel a queued or processing job by deleting from DB."""
    conn = _db()
    try:
        conn.execute("DELETE FROM download_jobs WHERE job_id = ?", (job_id,))
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


@router.post("/api/queue/{job_id}/cancel")
async def cancel_queue_item(job_id: str, _: None = Depends(require_token)):
    """Alias — same as POST /api/download/{id}/cancel."""
    return await cancel_download(job_id)


@router.post("/api/queue/{job_id}/start")
async def start_queued_job(job_id: str, _: None = Depends(require_token)):
    """Restart an error job (set status back to queued)."""
    conn = _db()
    try:
        conn.execute(
            "UPDATE download_jobs SET status='queued', finished_at_ms = NULL, updated_at_ms = ? WHERE job_id = ? AND status = 'error'",
            (_now_ms(), job_id),
        )
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


@router.post("/api/queue/retry-all-errors")
async def retry_all_errors(_: None = Depends(require_token)):
    """Bulk-Retry: setzt ALLE Error-Jobs in einem einzigen UPDATE auf 'queued'.

    Ersetzt die alte clientseitige for-Loop, die wegen Pagination (max 2000)
    nur eine Teilmenge erreichen konnte.
    """
    conn = _db()
    try:
        cur = conn.execute(
            "UPDATE download_jobs SET status='queued', finished_at_ms = NULL, updated_at_ms = ? WHERE status = 'error'",
            (_now_ms(),),
        )
        conn.commit()
        return {"ok": True, "retried": cur.rowcount}
    finally:
        conn.close()


@router.post("/api/queue/clear")
async def clear_queue(
    status: Optional[str] = Query(default="completed"),
    _: None = Depends(require_token),
):
    """Bulk-Delete: löscht Jobs nach Status in einem einzigen DELETE.

    status:
      - "completed"   (Default — sicher, nur fertige Jobs)
      - "error"       (alle fehlerhaften)
      - "queued"      (alle wartenden)
      - "all"         (alles AUSSER processing — laufende Jobs bleiben)
      - csv-Liste     (z.B. "completed,error")

    'processing' wird absichtlich NIE bulk-gelöscht, um Race-Conditions mit
    aktiven Workern zu vermeiden. Wer einzelne laufende Jobs killen will,
    nutzt /api/queue/{job_id}/cancel.
    """
    allowed = {'queued', 'completed', 'error'}  # absichtlich ohne 'processing'
    if not status or status == 'all':
        wanted = allowed
    else:
        wanted = {s.strip() for s in status.split(',') if s.strip() in allowed}
    if not wanted:
        return {"ok": False, "reason": "invalid or empty status filter", "allowed": sorted(allowed) + ["all"]}
    placeholders = ",".join("?" * len(wanted))
    conn = _db()
    try:
        cur = conn.execute(
            f"DELETE FROM download_jobs WHERE status IN ({placeholders})",
            tuple(wanted),
        )
        conn.commit()
        return {"ok": True, "deleted": cur.rowcount, "status_filter": sorted(wanted)}
    finally:
        conn.close()
