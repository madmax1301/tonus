"""finished_at_ms: wann ein Download fertig wurde (Queue-Karte zeigt es an)."""

from __future__ import annotations

import pytest

import utils.job_store as job_store
from utils.job_store import get_job, init_jobs_db, requeue_for_retry, upsert_job


@pytest.fixture(autouse=True)
def _jobs_db():
    init_jobs_db()
    conn = job_store._db()
    try:
        conn.execute("DELETE FROM download_jobs")
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def clock(monkeypatch):
    now = {"ms": 1_000}
    monkeypatch.setattr(job_store, "_now_ms", lambda: now["ms"])
    return now


def test_set_when_job_finishes_and_kept_on_repeat(clock):
    upsert_job("f-1", status="queued", message="Queued", payload={})
    assert get_job("f-1")["finished_at_ms"] is None

    clock["ms"] = 2_000
    upsert_job("f-1", status="completed", message="done")
    assert get_job("f-1")["finished_at_ms"] == 2_000

    # Späteres Nachschreiben desselben Status verschiebt den Zeitpunkt nicht.
    clock["ms"] = 3_000
    upsert_job("f-1", status="completed", message="done, tagged")
    job = get_job("f-1")
    assert job["finished_at_ms"] == 2_000
    assert job["updated_at_ms"] == 3_000


def test_cleared_when_requeued(clock):
    upsert_job("f-2", status="error", message="boom")
    assert get_job("f-2")["finished_at_ms"] == 1_000
    requeue_for_retry("f-2", 1, "retry")
    assert get_job("f-2")["finished_at_ms"] is None


def test_backfilled_from_updated_at_for_old_rows(clock):
    upsert_job("f-3", status="completed", message="done")
    conn = job_store._db()
    try:
        conn.execute("UPDATE download_jobs SET finished_at_ms = NULL WHERE job_id = 'f-3'")
        conn.commit()
    finally:
        conn.close()
    init_jobs_db()
    assert get_job("f-3")["finished_at_ms"] == 1_000
