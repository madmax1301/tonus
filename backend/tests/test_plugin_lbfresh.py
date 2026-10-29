import app as app_mod
import services.discovery as disc


class _Resp:
    def __init__(self, data, ok=True):
        self._data = data
        self.ok = ok

    def json(self):
        return self._data

    def raise_for_status(self):
        if not self.ok:
            raise RuntimeError("http error")


def test_fresh_releases_filters_and_sorts_by_confidence(monkeypatch):
    seen = {}

    def fake_get(url, params=None, headers=None, **kw):
        seen["url"], seen["params"], seen["headers"] = url, params, headers
        return _Resp({"payload": {"releases": [
            {"artist_credit_name": "Low", "release_name": "A", "confidence": 1,
             "release_date": "2026-10-02"},
            {"artist_credit_name": "High", "release_name": "B", "confidence": 5,
             "release_date": "2026-10-01"},
            {"artist_credit_name": "Comp", "release_name": "C", "confidence": 9,
             "release_group_secondary_type": "Compilation"},
        ]}})
    monkeypatch.setattr(disc.requests, "get", fake_get)

    out = disc.lb_fresh_releases("lbuser", days=7, token="tok")
    assert [r["release_name"] for r in out] == ["B", "A"]
    assert seen["url"].endswith("/user/lbuser/fresh_releases")
    assert seen["params"]["future"] == "false"
    assert seen["headers"]["Authorization"] == "Token tok"


def test_fresh_release_tracks_takes_top_ranked_and_dedupes(monkeypatch):
    disc._FRESH_CACHE.clear()
    monkeypatch.setattr(disc, "lb_fresh_releases", lambda u, d, t: [
        {"artist_credit_name": "X", "release_name": "R1"},
        {"artist_credit_name": "X", "release_name": "R1 (Deluxe)"},
    ])

    def fake_get(url, params=None, **kw):
        if url.endswith("/search/album"):
            return _Resp({"data": [{"id": 7}]})
        return _Resp({"data": [
            {"title": "Filler", "rank": 10, "artist": {"name": "X"}},
            {"title": "Hit", "rank": 900, "artist": {"name": "X"}},
            {"title": "Second", "rank": 500, "artist": {"name": "X"}},
        ]})
    monkeypatch.setattr(disc.requests, "get", fake_get)

    out = disc.lb_fresh_release_tracks("lbuser", tracks_per_release=2)
    assert out == [{"artist": "X", "title": "Hit"}, {"artist": "X", "title": "Second"}]


def test_lbfresh_discovery_queues_missing_with_sync_markers(monkeypatch):
    monkeypatch.setattr(app_mod, "_now_ms", lambda: 1000)
    monkeypatch.setattr(disc, "lb_fresh_release_tracks", lambda *a, **k: [
        {"artist": "Have", "title": "Inlib"},
        {"artist": "Need", "title": "Missing"}])
    monkeypatch.setattr(app_mod.navidrome_service, "find_track_id_by_artist_title",
                        lambda a, t: "sub-have" if a == "Have" else None)
    monkeypatch.setattr(disc, "deezer_search_track",
                        lambda a, t: {"id": 4242, "title": t,
                                      "artist": {"name": a}, "album": {"title": "Alb"}})
    monkeypatch.setattr(app_mod, "get_duplicate_download_reason", lambda *a, **k: None)
    monkeypatch.setattr(app_mod, "_resolve_track_for_queue", lambda tid, prov, hint: hint)
    monkeypatch.setattr(app_mod, "resolve_navidrome_library_path_optional",
                        lambda x: "/music")
    captured = []
    monkeypatch.setattr(app_mod, "upsert_job",
                        lambda job_id, **kw: captured.append((job_id, kw["payload"])))

    req = app_mod.PluginLbFreshDiscoveryRequest(
        navidrome_user="admin", listenbrainz_user="lbuser")
    app_mod._run_plugin_lbfresh_discovery(req)

    assert [c[0] for c in captured] == ["4242"]
    p = captured[0][1]
    assert p["plugin_sync_playlist_name"] == "Fresh Releases"
    assert p["plugin_sync_navidrome_user"] == "admin"

    assert app_mod._existing_in_library(app_mod._lbfresh_items(req), 60) == [
        {"subsonic_id": "sub-have", "artist": "Have", "title": "Inlib"}]
