"""
Parallele Downloads (#92).

Der Download-Worker verteilt Jobs auf Slots: pro Lane so viele, wie die
Einstellung `download.concurrency` erlaubt. Bei 1 muss alles exakt wie der
alte serielle Worker laufen; bei mehr dürfen mehrere Jobs gleichzeitig laden.
"""

from __future__ import annotations

import threading

import pytest

import utils.app_settings as app_settings
import utils.worker as worker_mod
from utils.job_store import get_job, init_jobs_db, upsert_job
from utils.worker import JobWorker


@pytest.fixture(autouse=True)
def _jobs_db():
    init_jobs_db()
    from utils.job_store import _db

    conn = _db()
    try:
        conn.execute("DELETE FROM download_jobs")
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def concurrency(monkeypatch):
    value = {"n": None}

    def fake_get_setting(key, default=None):
        if key == "download.concurrency":
            return None if value["n"] is None else str(value["n"])
        return default

    monkeypatch.setattr(app_settings, "get_setting", fake_get_setting)
    return value


def test_default_is_one_slot(concurrency):
    w = JobWorker(job_type="download")
    slot, wait = w._pick_download_lane()
    assert slot == "default"
    assert wait == 0
    # Slot belegt → kein zweiter Job, solange Concurrency 1 ist.
    w._lane_current_job[slot] = "job-1"
    assert w._pick_download_lane()[0] is None
    assert [l["name"] for l in w.lane_status()["lanes"]] == ["default"]


def test_concurrency_opens_more_slots(concurrency):
    concurrency["n"] = 3
    w = JobWorker(job_type="download")
    picked = []
    for i in range(3):
        slot, _ = w._pick_download_lane()
        assert slot is not None
        w._lane_current_job[slot] = f"job-{i}"
        picked.append(slot)
    assert sorted(picked) == ["default", "default2", "default3"]
    assert w._pick_download_lane()[0] is None

    status = w.lane_status()
    assert status["concurrency"] == 3
    assert [l["label"] for l in status["lanes"]] == ["1", "2", "3"]


def test_value_is_clamped(concurrency):
    concurrency["n"] = 99
    assert worker_mod._load_download_concurrency() == worker_mod._MAX_DOWNLOAD_CONCURRENCY
    concurrency["n"] = 0
    assert worker_mod._load_download_concurrency() == 1


def test_lowered_concurrency_keeps_running_slot_visible(concurrency):
    """Ein Slot, der nach dem Herunterregeln noch lädt, bleibt im UI sichtbar,
    nimmt aber keinen neuen Job mehr an."""
    concurrency["n"] = 2
    w = JobWorker(job_type="download")
    w._lane_current_job["default2"] = "still-running"
    concurrency["n"] = 1
    names = [l["name"] for l in w.lane_status()["lanes"]]
    assert names == ["default", "default2"]
    assert w._pick_download_lane()[0] == "default"


def test_cooldown_blocks_only_its_own_slot(concurrency):
    concurrency["n"] = 2
    w = JobWorker(job_type="download")
    w._lane_ready_at["default"] = worker_mod._now_ms() + 60_000
    assert w._pick_download_lane()[0] == "default2"


def test_two_jobs_download_at_the_same_time(concurrency, monkeypatch):
    concurrency["n"] = 2
    for jid in ("par-1", "par-2"):
        upsert_job(jid, status="queued", message="Queued", payload={})

    release = threading.Event()
    running: set = set()
    both_running = threading.Event()

    def fake_process(self, job, lane="default"):
        running.add(job["job_id"])
        if len(running) == 2:
            both_running.set()
        release.wait(timeout=5)
        upsert_job(job["job_id"], status="completed", message="done")
        self._lane_current_job[lane] = None

    monkeypatch.setattr(JobWorker, "_process_download", fake_process)

    w = JobWorker(job_type="download")
    w.start()
    try:
        assert both_running.wait(timeout=5), "zweiter Job startete nicht parallel"
        assert get_job("par-1")["status"] == "processing"
        assert get_job("par-2")["status"] == "processing"
    finally:
        release.set()
        w.shutdown(timeout=5)

    assert get_job("par-1")["status"] == "completed"
    assert get_job("par-2")["status"] == "completed"


def test_job_is_claimed_only_once(concurrency):
    upsert_job("claim-1", status="queued", message="Queued", payload={})
    w = JobWorker(job_type="download")
    first = w._poll_next_queued_download(lane="default")
    assert first is not None and first["job_id"] == "claim-1"
    assert w._poll_next_queued_download(lane="default2") is None


def test_bot_check_pauses_every_slot_of_the_lane(concurrency):
    """Ein Bot-Check trifft die IP: auch die anderen Slots derselben Lane
    dürfen nicht weiterladen."""
    concurrency["n"] = 3
    w = JobWorker(job_type="download")
    w._throttle_after_bot_check("default", cooldown_s=600)
    slot, wait = w._pick_download_lane()
    assert slot is None
    assert wait > 500_000
    status = w.lane_status()
    assert all(l["remaining_ms"] > 500_000 for l in status["lanes"])


def test_bot_check_throttles_lane_to_one_slot(concurrency, monkeypatch):
    concurrency["n"] = 3
    w = JobWorker(job_type="download")
    w._throttle_after_bot_check("default", cooldown_s=0)
    assert w._pick_download_lane()[0] == "default"
    w._lane_current_job["default"] = "job-1"
    assert w._pick_download_lane()[0] is None
    status = w.lane_status()
    assert [l["name"] for l in status["lanes"]] == ["default"]
    assert status["bot_check"]["throttled_until_ms"] > worker_mod._now_ms()

    # Nach Ablauf der Drossel gilt wieder die eingestellte Concurrency.
    monkeypatch.setattr(worker_mod, "_now_ms", lambda: w._throttled_until["default"] + 1)
    assert w._pick_download_lane()[0] in ("default2", "default3")
    assert w.lane_status()["bot_check"]["throttled_until_ms"] == 0


def test_bot_check_rate_counts_recent_outcomes(concurrency, monkeypatch):
    w = JobWorker(job_type="download")
    for kind in ("ok", "ok", "bot_check", "error"):
        w._record_outcome(kind)
    stats = w.lane_status()["bot_check"]
    assert stats["count"] == 1
    assert stats["finished"] == 4

    later = worker_mod._now_ms() + worker_mod._OUTCOME_WINDOW_MS + 1
    monkeypatch.setattr(worker_mod, "_now_ms", lambda: later)
    stats = w.lane_status()["bot_check"]
    assert stats["count"] == 0
    assert stats["finished"] == 0


def test_bot_check_in_finished_job_throttles_lane(concurrency, monkeypatch):
    """Ende-zu-Ende über _process_download: ein Bot-Check-Fehler wird
    re-queued, sperrt die ganze Lane und landet in der Quote."""
    concurrency["n"] = 2
    upsert_job("bot-1", status="processing", message="", payload={})

    import app as app_mod

    def fake_download(track_id, **_):
        upsert_job(track_id, status="error", message="Download failed",
                   error="Sign in to confirm you're not a bot")

    monkeypatch.setattr(app_mod, "download_and_process", fake_download)
    w = JobWorker(job_type="download")
    w._lane_current_job["default2"] = "bot-1"
    w._process_download({"job_id": "bot-1", "params": {}}, lane="default2")

    assert get_job("bot-1")["status"] == "queued"
    assert w._pick_download_lane()[0] is None
    assert w.lane_status()["bot_check"]["count"] == 1
