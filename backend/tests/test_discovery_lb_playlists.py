import services.discovery as disc


def _fake_createdfor(monkeypatch, playlists):
    class _Resp:
        ok = True
        def json(self): return {"playlists": playlists}
    def _get(url, *a, **k): return _Resp()
    monkeypatch.setattr(disc.requests, "get", _get)


def _pl(source_patch, date, tracks, title="t"):
    return {"playlist": {
        "title": title,
        "date": date,
        "identifier": "https://listenbrainz.org/playlist/" + date,
        "extension": {"https://musicbrainz.org/doc/jspf#playlist": {
            "additional_metadata": {"algorithm_metadata": {"source_patch": source_patch}}}},
        "track": [{"creator": a, "title": t} for a, t in tracks],
    }}


def test_occurrence_0_picks_newest_of_source_patch(monkeypatch):
    _fake_createdfor(monkeypatch, [
        _pl("weekly-exploration", "2026-06-14T22:09:00+00:00", [("A", "new")]),
        _pl("weekly-jams",        "2026-06-14T22:05:00+00:00", [("J", "jam")]),
        _pl("weekly-exploration", "2026-06-07T10:35:00+00:00", [("A", "old")]),
    ])
    out = disc.lb_playlist_tracks("u", "weekly-exploration", occurrence=0)
    assert out == [{"artist": "A", "title": "new"}]


def test_occurrence_1_picks_second_newest(monkeypatch):
    _fake_createdfor(monkeypatch, [
        _pl("weekly-exploration", "2026-06-14T22:09:00+00:00", [("A", "new")]),
        _pl("weekly-exploration", "2026-06-07T10:35:00+00:00", [("A", "old")]),
    ])
    out = disc.lb_playlist_tracks("u", "weekly-exploration", occurrence=1)
    assert out == [{"artist": "A", "title": "old"}]


def test_occurrence_missing_returns_empty(monkeypatch):
    _fake_createdfor(monkeypatch, [
        _pl("weekly-exploration", "2026-06-14T22:09:00+00:00", [("A", "new")]),
    ])
    assert disc.lb_playlist_tracks("u", "weekly-exploration", occurrence=1) == []


def test_default_occurrence_is_zero(monkeypatch):
    _fake_createdfor(monkeypatch, [
        _pl("weekly-jams", "2026-06-14T22:05:00+00:00", [("J", "jam")]),
    ])
    assert disc.lb_playlist_tracks("u", "weekly-jams") == [{"artist": "J", "title": "jam"}]
