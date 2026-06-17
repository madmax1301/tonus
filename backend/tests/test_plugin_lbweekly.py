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
