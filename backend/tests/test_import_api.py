"""HTTP-Tests für /api/import/* — CSV- und Spotify-History-Import, Status,
Ergebnis, Abbruch, Löschen und queue-all.

Die Worker laufen in Tests nicht (TONUS_START_WORKERS=0 in conftest), die
Jobs bleiben also in dem Zustand, den der Endpoint hinterlässt. Ergebnisse
eines „fertigen“ Imports legt der Test direkt in der DB an.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from utils.job_store import _db, get_import_job, insert_import_results, upsert_import_job, upsert_job


@pytest.fixture
def client():
    from app import app

    conn = _db()
    try:
        for table in ("import_results", "import_jobs", "download_jobs", "users"):
            conn.execute(f"DELETE FROM {table}")
        conn.commit()
    finally:
        conn.close()
    # Rate-Limiter teilen sich Modul-State über alle Tests.
    from utils import rate_limit
    rate_limit._rate_limit_state.clear()
    return TestClient(app)


def _pending(job_id):
    conn = _db()
    try:
        rows = conn.execute(
            "SELECT requested_artist, requested_title, playlist_names_json FROM import_results "
            "WHERE job_id = ? AND result_type = 'pending_raw' ORDER BY id",
            (job_id,),
        ).fetchall()
        return [tuple(r) for r in rows]
    finally:
        conn.close()


# ── CSV ─────────────────────────────────────────────────────────────────

def test_csv_with_header_and_playlist_column(client):
    csv = "Track Name,Artist Name,Playlist Name\nKickboxer,The Amity Affliction,Gym\nWake Up Calling,Papa Roach,\"Gym;Car\"\n"
    r = client.post("/api/import/csv", json={"csv_text": csv, "filename": "export.csv"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "queued" and body["total"] == 2 and body["filename"] == "export.csv"

    job = get_import_job(body["job_id"])
    assert job["source"] == "csv" and job["mode"] == "full"
    assert job["playlists_total"] == 2
    assert _pending(body["job_id"]) == [
        ("The Amity Affliction", "Kickboxer", '["Gym"]'),
        ("Papa Roach", "Wake Up Calling", '["Gym", "Car"]'),
    ]


def test_csv_semicolon_delimiter(client):
    csv = "Artist;Title\nDayseeker;Forgotten Ghost\n"
    body = client.post("/api/import/csv", json={"csv_text": csv}).json()
    assert _pending(body["job_id"]) == [("Dayseeker", "Forgotten Ghost", None)]


def test_csv_single_column_artist_dash_title(client):
    body = client.post("/api/import/csv", json={"csv_text": "Holding Absence - Modern Life Is Lonely\n"}).json()
    assert _pending(body["job_id"]) == [("Holding Absence", "Modern Life Is Lonely", None)]


def test_csv_playlist_sync_mode_routes_to_its_lane(client):
    csv = "Artist,Track\nA,B\n"
    body = client.post("/api/import/csv", json={"csv_text": csv, "mode": "playlist_sync"}).json()
    job = get_import_job(body["job_id"])
    assert job["mode"] == "playlist_sync" and job["source"] == "playlist_sync"
    assert client.get(f"/api/import/jobs/{body['job_id']}/status").json()["source"] == "playlist_sync"


def test_csv_without_tracks_is_rejected(client):
    r = client.post("/api/import/csv", json={"csv_text": "Artist,Track\n"})
    assert r.status_code == 400


def test_csv_rejects_unknown_provider(client):
    r = client.post("/api/import/csv", json={"csv_text": "A - B", "provider": "napster"})
    assert r.status_code == 400


# ── Spotify History ─────────────────────────────────────────────────────

def _event(artist, title, ts="2024-03-01T10:00:00Z", ms=200_000):
    return {
        "ts": ts,
        "ms_played": ms,
        "master_metadata_track_name": title,
        "master_metadata_album_artist_name": artist,
    }


def test_spotify_history_queues_tracks(client):
    files = [[_event("Papa Roach", "Wake Up Calling"), _event("Papa Roach", "Wake Up Calling")]]
    r = client.post("/api/import/spotify-history", json={"files": files})
    body = r.json()
    assert body["status"] == "queued" and body["total"] == 1
    job = get_import_job(body["job_id"])
    assert job["source"] == "spotify_history"
    assert client.get(f"/api/import/jobs/{body['job_id']}/status").json()["source"] == "spotify_history"


def test_spotify_history_without_music_returns_empty(client):
    r = client.post("/api/import/spotify-history", json={"files": [[{"ts": "2024-01-01T00:00:00Z", "ms_played": 1}]]})
    assert r.status_code == 200
    assert r.json()["status"] == "empty"


# ── Status / Ergebnis / Abbruch / Löschen ───────────────────────────────

def test_status_cancel_and_delete(client):
    job_id = client.post("/api/import/csv", json={"csv_text": "A - B\nC - D\n"}).json()["job_id"]
    st = client.get(f"/api/import/jobs/{job_id}/status").json()
    assert st["status"] == "queued" and st["total"] == 2

    # Ergebnisse gibt es erst nach Abschluss.
    assert client.get(f"/api/import/jobs/{job_id}/result").status_code == 400

    c = client.post(f"/api/import/jobs/{job_id}/cancel").json()
    assert c == {"ok": True, "cancelled": True, "previous_status": "queued"}
    assert client.post(f"/api/import/jobs/{job_id}/cancel").json()["cancelled"] is False

    assert client.delete(f"/api/import/jobs/{job_id}").json()["deleted_jobs"] == 1
    assert client.delete(f"/api/import/jobs/{job_id}").json()["deleted_jobs"] == 0
    assert client.get(f"/api/import/jobs/{job_id}/status").status_code == 404
    assert client.post(f"/api/import/jobs/{job_id}/cancel").status_code == 404


def _completed_job(job_id="csv-done"):
    upsert_import_job(job_id, status="completed", total=3, processed=3, found=2, not_found=1,
                      message="done", payload_json='{"provider": "deezer"}', mode="full", source="csv")
    insert_import_results(job_id, "matched", [
        {"original": "A - B", "requested_artist": "A", "requested_title": "B",
         "track": {"id": "111", "name": "B", "artist": "A", "album": "X"}, "playlist_names": ["Gym"]},
        {"original": "C - D", "requested_artist": "C", "requested_title": "D",
         "track": {"id": "222", "name": "D", "artist": "C", "album": "Y"}},
    ])
    insert_import_results(job_id, "unmatched", [
        {"original": "E - F", "requested_artist": "E", "requested_title": "F"},
    ])
    return job_id


def test_result_of_completed_import(client):
    job_id = _completed_job()
    body = client.get(f"/api/import/jobs/{job_id}/result").json()
    assert body["found"] == 2 and body["not_found"] == 1
    assert [m["track"]["id"] for m in body["matched"]] == ["111", "222"]
    assert client.get(f"/api/import/jobs/{job_id}/result?limit=0").status_code == 422


def test_queue_all_skips_tracks_already_in_queue(client):
    job_id = _completed_job()
    upsert_job("222", status="queued", message="Queued", payload={})

    body = client.post(f"/api/import/jobs/{job_id}/queue-all", json={}).json()
    assert body["queued"] == 1 and body["skipped_duplicate"] == 1 and body["total_matched"] == 2

    from utils.job_store import get_job
    job = get_job("111")
    assert job["status"] == "queued"
    assert job["payload"]["import_playlist_names"] == ["Gym"]
    assert job["payload"]["track"]["name"] == "B"

    # Zweiter Aufruf: beide sind jetzt in der Queue.
    again = client.post(f"/api/import/jobs/{job_id}/queue-all", json={}).json()
    assert again["queued"] == 0 and again["skipped_duplicate"] == 2


def test_queue_all_needs_completed_import(client):
    job_id = client.post("/api/import/csv", json={"csv_text": "A - B"}).json()["job_id"]
    assert client.post(f"/api/import/jobs/{job_id}/queue-all", json={}).status_code == 400
    assert client.post("/api/import/jobs/nope/queue-all", json={}).status_code == 404


def test_status_and_result_need_a_token_once_a_user_exists(client):
    """Status und Ergebnis zeigen Tracks und Playlist-Namen aus der Library —
    ohne Token gibt es sie nicht mehr, sobald ein Account existiert."""
    from utils import auth_users as au

    job_id = _completed_job()
    user = au.create_user("admin", "correct horse battery", is_admin=True)
    try:
        assert client.get(f"/api/import/jobs/{job_id}/status").status_code == 401
        assert client.get(f"/api/import/jobs/{job_id}/result").status_code == 401
        token = au.issue_jwt_pair(user["id"], "admin", True)["access"]
        h = {"Authorization": f"Bearer {token}"}
        assert client.get(f"/api/import/jobs/{job_id}/status", headers=h).status_code == 200
        assert client.get(f"/api/import/jobs/{job_id}/result", headers=h).status_code == 200
    finally:
        conn = _db()
        try:
            conn.execute("DELETE FROM users")
            conn.commit()
        finally:
            conn.close()
