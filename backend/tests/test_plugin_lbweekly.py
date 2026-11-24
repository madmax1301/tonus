import json
import app as app_mod


def test_lbweekly_discovery_queues_missing_with_sync_markers(monkeypatch):
    # LB liefert 2 Tracks; einer ist in der Library, einer fehlt.
    monkeypatch.setattr(app_mod, "_now_ms", lambda: 1000)
    import services.discovery as disc
    monkeypatch.setattr(disc, "lb_playlist_tracks",
                        lambda u, sp, occurrence=0: [
                            {"artist": "Have", "title": "Inlib"},
                            {"artist": "Need", "title": "Missing"}])

    def fake_find(artist, title):
        return "sub-have" if artist == "Have" else None
    monkeypatch.setattr(app_mod.navidrome_service,
                        "find_track_id_by_artist_title", fake_find)
    monkeypatch.setattr(disc, "deezer_search_track",
                        lambda a, t: {"id": 4242, "title": t,
                                       "artist": {"name": a}, "album": {"title": "Alb"}})
    monkeypatch.setattr(app_mod, "get_duplicate_download_reason",
                        lambda *a, **k: None)
    monkeypatch.setattr(app_mod, "_resolve_track_for_queue",
                        lambda tid, prov, hint: hint)
    captured = {}
    def fake_upsert(job_id, **kw):
        captured["job_id"] = job_id
        captured["payload"] = kw.get("payload")
    monkeypatch.setattr(app_mod, "upsert_job", fake_upsert)
    monkeypatch.setattr(app_mod, "resolve_navidrome_library_path_optional",
                        lambda x: "/music")

    req = app_mod.PluginLbWeeklyDiscoveryRequest(
        navidrome_user="admin", listenbrainz_user="lbuser",
        source_patch="weekly-exploration", occurrence=0,
        playlist_name="Weekly Exploration")
    app_mod._run_plugin_lbweekly_discovery(req)

    # Der fehlende Track wurde mit den BESTEHENDEN sync-Markern gequeued.
    assert captured["job_id"] == "4242"
    p = captured["payload"]
    assert p["plugin_sync_playlist_name"] == "Weekly Exploration"
    assert p["plugin_sync_navidrome_user"] == "admin"


def test_lbweekly_existing_returns_inlibrary_tracks(monkeypatch):
    import services.discovery as disc
    monkeypatch.setattr(disc, "lb_playlist_tracks",
                        lambda u, sp, occurrence=0: [
                            {"artist": "Have", "title": "Inlib"},
                            {"artist": "Need", "title": "Missing"}])
    monkeypatch.setattr(app_mod.navidrome_service,
                        "find_track_id_by_artist_title",
                        lambda a, t: "sub-have" if a == "Have" else None)
    req = app_mod.PluginLbWeeklyDiscoveryRequest(
        navidrome_user="admin", listenbrainz_user="lbuser",
        source_patch="weekly-exploration", occurrence=0,
        playlist_name="Weekly Exploration")
    existing = app_mod._check_lbweekly_tracks_in_library(req)
    assert existing == [{"subsonic_id": "sub-have", "artist": "Have", "title": "Inlib"}]


def test_finished_tracks_since_ms_drops_older_lb_versions(monkeypatch):
    # Zwei fertige Downloads für "Weekly Jams": einer aus der Vorwoche (vor
    # der LB-Rotation gequeued), einer aus der aktuellen Woche. Mit since_ms
    # darf nur der aktuelle zurückkommen — sonst wächst die Playlist.
    from fastapi.testclient import TestClient
    import utils.job_store as job_store

    job_store.init_jobs_db()
    conn = job_store._db()
    try:
        conn.execute("DELETE FROM download_jobs")
        conn.commit()
    finally:
        conn.close()

    now = {"ms": 0}
    monkeypatch.setattr(job_store, "_now_ms", lambda: now["ms"])
    monkeypatch.setattr(app_mod, "_now_ms", lambda: 10 * 24 * 3600 * 1000)
    monkeypatch.setattr(app_mod.navidrome_service,
                        "find_track_id_by_artist_title", lambda a, t: "sub-" + t)

    def add(job_id, created_ms):
        now["ms"] = created_ms
        upsert_job_payload = {
            "track": {"artist": "A", "name": job_id},
            "plugin_sync_playlist_name": "Weekly Jams",
            "plugin_sync_navidrome_user": "admin",
        }
        job_store.upsert_job(job_id, status="queued", message="q",
                             payload=upsert_job_payload)
        job_store.upsert_job(job_id, status="completed", message="done")

    rotated = 7 * 24 * 3600 * 1000
    add("old", rotated - 1000)
    add("new", rotated + 1000)

    client = TestClient(app_mod.app)
    base = "/api/plugin/finished-tracks?navidrome_user=admin&playlist_name=Weekly%20Jams"
    all_ids = {i["deezer_id"] for i in client.get(base).json()["items"]}
    assert all_ids == {"old", "new"}
    cur = client.get(f"{base}&since_ms={rotated}").json()["items"]
    assert [i["deezer_id"] for i in cur] == ["new"]


def test_lbweekly_discovery_returns_rotated_at(monkeypatch):
    from fastapi.testclient import TestClient
    import services.discovery as disc
    monkeypatch.setattr(disc, "lb_playlist_tracks", lambda u, sp, occurrence=0: [])
    monkeypatch.setattr(disc, "lb_playlist_rotated_at_ms", lambda u, sp: 4242)
    monkeypatch.setattr(app_mod, "_run_plugin_lbweekly_discovery", lambda req: None)
    app_mod.app.dependency_overrides[app_mod.require_token] = lambda: None
    try:
        r = TestClient(app_mod.app).post("/api/plugin/lbweekly/discovery", json={
            "navidrome_user": "admin", "listenbrainz_user": "lb",
            "source_patch": "weekly-jams", "occurrence": 1,
            "playlist_name": "Last Week's Jams"})
    finally:
        app_mod.app.dependency_overrides.pop(app_mod.require_token, None)
    assert r.status_code == 200
    assert r.json()["rotated_at_ms"] == 4242
