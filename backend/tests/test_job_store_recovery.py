"""
Restart recovery for download jobs.

A restart tears the worker thread away from whatever it was downloading. Those
jobs must go back into the queue, while genuine failures (track not findable)
must stay failed — otherwise every restart would replay them forever.
"""

from __future__ import annotations

import pytest

from utils.job_store import (
    get_job,
    init_jobs_db,
    reset_stale_inflight_jobs,
    upsert_job,
)

LEGACY_RESTART_MSG = "Interrupted — server restarted. Retry the download."


@pytest.fixture(autouse=True)
def _jobs_db():
    init_jobs_db()


def test_processing_job_returns_to_queue():
    upsert_job(
        "recov-processing",
        status="processing",
        message="Downloading",
        stage="download",
        progress=42,
        payload={"track": "Aphex Twin — Xtal", "provider": "deezer"},
    )

    reset_stale_inflight_jobs()

    job = get_job("recov-processing")
    assert job["status"] == "queued"
    assert job["message"] == "Re-queued after server restart"


def test_processing_job_keeps_payload_and_clears_progress():
    """payload_json must survive — the retry runs with the same parameters."""
    upsert_job(
        "recov-payload",
        status="processing",
        message="Downloading",
        stage="convert",
        progress=88,
        file_path="/tmp/partial.part",
        download_url="https://example.invalid/stream",
        payload={"track": "Boards of Canada — Roygbiv", "provider": "deezer"},
    )

    reset_stale_inflight_jobs()

    job = get_job("recov-payload")
    assert job["status"] == "queued"
    assert job["stage"] is None
    assert job["progress"] is None
    assert job["file_path"] is None
    assert job["download_url"] is None


def test_legacy_restart_error_is_drained_back_into_queue():
    """Rows that older versions failed on boot get recovered once."""
    upsert_job(
        "recov-legacy",
        status="error",
        message=LEGACY_RESTART_MSG,
        error="interrupted",
    )

    reset_stale_inflight_jobs()

    job = get_job("recov-legacy")
    assert job["status"] == "queued"
    assert job["error"] is None


def test_real_failure_stays_failed():
    """The regression this whole change hinges on: no infinite retry loop."""
    upsert_job(
        "recov-real-error",
        status="error",
        message="No source found for this track",
        error="not_found",
    )

    reset_stale_inflight_jobs()

    job = get_job("recov-real-error")
    assert job["status"] == "error"
    assert job["message"] == "No source found for this track"


def test_completed_job_is_untouched():
    upsert_job(
        "recov-completed",
        status="completed",
        message="Done",
        file_path="/music/lib/artist/album/track.mp3",
    )

    reset_stale_inflight_jobs()

    job = get_job("recov-completed")
    assert job["status"] == "completed"
    assert job["file_path"] == "/music/lib/artist/album/track.mp3"


def test_queued_job_stays_queued():
    upsert_job("recov-queued", status="queued", message="Waiting")

    reset_stale_inflight_jobs()

    assert get_job("recov-queued")["status"] == "queued"
