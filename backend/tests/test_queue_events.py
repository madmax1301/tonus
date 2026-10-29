"""
Queue-Stream statt Polling (#93).

/api/queue/events schickt nur Änderungen. Der Diff hängt an updated_at_ms;
mehrere Updates in derselben Millisekunde dürfen weder verloren gehen noch
doppelt gemeldet werden.
"""

from __future__ import annotations

import json

import pytest

from utils.job_store import _db, init_jobs_db, upsert_job


@pytest.fixture(autouse=True)
def _jobs_db():
    init_jobs_db()
    conn = _db()
    try:
        conn.execute("DELETE FROM download_jobs")
        conn.commit()
    finally:
        conn.close()


def _set_updated(job_id: str, ts: int) -> None:
    conn = _db()
    try:
        conn.execute("UPDATE download_jobs SET updated_at_ms=? WHERE job_id=?", (ts, job_id))
        conn.commit()
    finally:
        conn.close()


def test_snapshot_reports_only_changes():
    from app import _queue_snapshot_since

    upsert_job("ev-old", status="completed", message="done")
    _set_updated("ev-old", 1_000)
    upsert_job("ev-new", status="processing", message="Downloading", progress=30)
    _set_updated("ev-new", 2_000)

    snap = _queue_snapshot_since(1_500, set())
    assert [j["job_id"] for j in snap["jobs"]] == ["ev-new"]
    assert snap["jobs"][0]["progress"] == 30
    assert snap["status_counts"] == {"completed": 1, "processing": 1}
    assert snap["resync"] is False

    # Nächster Tick ohne Änderung: nichts Neues, auch nicht ev-new erneut.
    again = _queue_snapshot_since(snap["since"], snap["seen"])
    assert again["jobs"] == []


def test_snapshot_keeps_updates_in_the_same_millisecond():
    from app import _queue_snapshot_since

    upsert_job("ev-a", status="processing", message="a")
    _set_updated("ev-a", 5_000)
    first = _queue_snapshot_since(4_000, set())
    assert [j["job_id"] for j in first["jobs"]] == ["ev-a"]

    # Zweiter Job landet in derselben Millisekunde, nachdem ev-a gemeldet war.
    upsert_job("ev-b", status="queued", message="b")
    _set_updated("ev-b", 5_000)
    second = _queue_snapshot_since(first["since"], first["seen"])
    assert [j["job_id"] for j in second["jobs"]] == ["ev-b"]


def test_snapshot_signals_resync_for_bulk_changes(monkeypatch):
    import app as app_mod

    monkeypatch.setattr(app_mod, "_QUEUE_EVENTS_MAX_ROWS", 2)
    for i in range(3):
        upsert_job(f"ev-bulk-{i}", status="queued", message="q")
    snap = app_mod._queue_snapshot_since(0, set())
    assert snap["resync"] is True
    assert snap["jobs"] == []


def test_lane_fingerprint_ignores_countdown():
    from app import _lanes_fingerprint

    base = {
        "lanes": [{"name": "default", "ready_at_ms": 10, "remaining_ms": 900, "current_job_id": None}],
        "concurrency": 1,
        "cooldown": {"normal_seconds": [60, 300], "rate_limited_seconds": [300, 600]},
    }
    ticked = json.loads(json.dumps(base))
    ticked["lanes"][0]["remaining_ms"] = 100
    assert _lanes_fingerprint(base) == _lanes_fingerprint(ticked)

    busy = json.loads(json.dumps(base))
    busy["lanes"][0]["current_job_id"] = "job-1"
    assert _lanes_fingerprint(base) != _lanes_fingerprint(busy)


def test_stream_sends_counts_and_lanes_first():
    """Erstes Event kommt sofort und trägt Zählungen und Lanes.

    Über den TestClient endet ein endloser Stream nie sauber, daher wird
    der Generator direkt mit einer Request-Attrappe getrieben, die nach dem
    ersten Tick die Verbindung als getrennt meldet."""
    import asyncio

    from app import queue_events

    upsert_job("ev-stream", status="queued", message="q")

    class _Req:
        def __init__(self):
            self.calls = 0

        async def is_disconnected(self):
            self.calls += 1
            return self.calls > 1

    async def _collect():
        resp = await queue_events(_Req(), None)
        assert resp.media_type == "text/event-stream"
        return [chunk async for chunk in resp.body_iterator]

    chunks = asyncio.run(_collect())
    data_lines = [
        line for chunk in chunks for line in chunk.splitlines() if line.startswith("data: ")
    ]
    assert len(data_lines) == 1
    data = json.loads(data_lines[0][len("data: "):])
    assert data["status_counts"] == {"queued": 1}
    assert "lanes" in data
