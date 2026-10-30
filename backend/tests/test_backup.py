"""Snapshots von jobs.db (utils/backup.py) und die Admin-Endpoints."""

from __future__ import annotations

import sqlite3

import pytest

import utils.backup as backup
from utils.job_store import JOBS_DB_PATH, get_job, init_jobs_db, upsert_job


@pytest.fixture(autouse=True)
def _backup_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(backup, "BACKUP_DIR", str(tmp_path / "backups"))
    init_jobs_db()


def test_snapshot_is_a_readable_copy():
    upsert_job("bk-1", status="completed", message="done")
    name = backup.snapshot("manual")
    assert backup.is_valid_name(name)

    conn = sqlite3.connect(backup.path_for(name))
    try:
        row = conn.execute("SELECT status FROM download_jobs WHERE job_id = 'bk-1'").fetchone()
    finally:
        conn.close()
    assert row == ("completed",)
    # Das Original bleibt unangetastet.
    assert get_job("bk-1")["status"] == "completed"


def test_each_kind_keeps_its_own_last_n(monkeypatch):
    monkeypatch.setattr(backup, "BACKUP_KEEP", 2)
    for _ in range(3):
        backup.snapshot("startup")
    backup.snapshot("daily")
    kinds = [b["kind"] for b in backup.list_backups()]
    assert kinds.count("startup") == 2
    assert kinds.count("daily") == 1


def test_no_database_no_snapshot(monkeypatch, tmp_path):
    monkeypatch.setattr(backup, "JOBS_DB_PATH", str(tmp_path / "missing.db"))
    assert backup.snapshot("manual") is None


@pytest.mark.parametrize("name", [
    "../jobs.db",
    "jobs.db",
    "jobs-20261030-221500-manual.db/../../etc/passwd",
    "jobs-20261030-221500-other.db",
])
def test_path_for_rejects_foreign_names(name):
    assert backup.path_for(name) is None


def test_unknown_kind_is_rejected():
    with pytest.raises(ValueError):
        backup.snapshot("weekly")


@pytest.fixture
def admin_client():
    from fastapi.testclient import TestClient

    from app import _require_real_admin, app
    from utils.auth import require_admin, require_token

    app.dependency_overrides[require_token] = lambda: None
    app.dependency_overrides[require_admin] = lambda: None
    app.dependency_overrides[_require_real_admin] = lambda: None
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def test_endpoints_create_list_download(admin_client):
    created = admin_client.post("/api/admin/backups")
    assert created.status_code == 200
    name = created.json()["name"]

    listed = admin_client.get("/api/admin/backups").json()
    assert name in [b["name"] for b in listed["backups"]]

    dl = admin_client.get(f"/api/admin/backups/{name}")
    assert dl.status_code == 200
    assert dl.content.startswith(b"SQLite format 3")

    assert admin_client.get("/api/admin/backups/jobs.db").status_code == 404


def test_endpoints_closed_in_setup_mode():
    """Ohne angelegten User lässt require_token jeden als Admin durch —
    Backups (mit JWT-Secret) bleiben trotzdem zu."""
    from fastapi.testclient import TestClient

    from app import app
    from utils import auth_users

    assert auth_users.setup_required()
    assert TestClient(app).get("/api/admin/backups").status_code in (401, 403)
