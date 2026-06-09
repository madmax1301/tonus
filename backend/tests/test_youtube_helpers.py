"""Unit tests for YouTube download helpers (no network)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from services.youtube import YouTubeService


def test_output_base_path_strips_configured_extension() -> None:
    assert YouTubeService._output_base_path("/tmp/a - b.flac", "flac") == "/tmp/a - b"
    assert YouTubeService._output_base_path("/tmp/a - b.FLAC", "flac") == "/tmp/a - b"


def test_output_base_path_fallback_splitext() -> None:
    # Path does not end with .mp3 — strip last extension only (splitext).
    assert YouTubeService._output_base_path("/tmp/foo.bar", "mp3") == "/tmp/foo"


def test_yt_dlp_outtmpl() -> None:
    assert YouTubeService._yt_dlp_outtmpl("/x/base") == "/x/base.%(ext)s"


def test_ffmpeg_extract_preferredcodec_m4a() -> None:
    assert YouTubeService._ffmpeg_extract_preferredcodec("m4a") == "m4a"


def test_ffmpeg_extract_preferredcodec_flac_mapping() -> None:
    s = YouTubeService._ffmpeg_extract_preferredcodec("flac")
    assert s.startswith("m4a>flac/")
    assert s.endswith("/best>flac")
    assert "webm>flac" in s


def test_preferred_quality_for_extract_flac() -> None:
    assert YouTubeService._preferred_quality_for_extract("flac", "320") == "0"


def test_filepaths_from_info_flat() -> None:
    info = {
        "filepath": "/a/b.webm",
        "requested_downloads": [{"filepath": "/a/b.m4a"}],
    }
    paths = YouTubeService._filepaths_from_info(info)
    assert "/a/b.webm" in paths
    assert "/a/b.m4a" in paths


def test_filepaths_from_info_nested_entries() -> None:
    info = {
        "entries": [
            {"filepath": "/nested/track.flac"},
        ]
    }
    assert "/nested/track.flac" in YouTubeService._filepaths_from_info(info)


def test_resolve_downloaded_audio_expected_path(tmp_path) -> None:
    base = tmp_path / "Artist - Song"
    target = base.with_suffix(".flac")
    target.write_bytes(b"\x00")
    svc = YouTubeService()
    ydl = MagicMock()
    out = svc._resolve_downloaded_audio(
        str(base), "flac", False, {}, ydl
    )
    assert out == str(target)


def test_resolve_downloaded_audio_fallback_same_stem_different_ext(tmp_path) -> None:
    base = tmp_path / "Artist - Song"
    mp3 = base.with_suffix(".mp3")
    mp3.write_bytes(b"\x00")
    svc = YouTubeService()
    ydl = MagicMock()
    ydl.prepare_filename.side_effect = Exception("no")
    out = svc._resolve_downloaded_audio(
        str(base), "flac", False, {}, ydl
    )
    assert out == str(mp3)


# ── expand_playlist_url (v0.5.0) ──────────────────────────────────────
# Mocked yt-dlp — kein Netzwerk. Wir patchen services.youtube.yt_dlp.YoutubeDL
# auf einen Context-Manager dessen extract_info ein fixes Info-Dict liefert.


def _mock_ydl(monkeypatch, info) -> None:
    import services.youtube as yt_mod

    class _FakeYDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=False):
            if isinstance(info, Exception):
                raise info
            return info

    monkeypatch.setattr(yt_mod.yt_dlp, "YoutubeDL", _FakeYDL)


def test_expand_playlist_url_basic(monkeypatch) -> None:
    _mock_ydl(monkeypatch, {
        "_type": "playlist",
        "title": "Hardstyle Bangers",
        "entries": [
            {"webpage_url": "https://soundcloud.com/a/t1", "title": "T1", "uploader": "A"},
            {"webpage_url": "https://soundcloud.com/a/t2", "title": "T2", "uploader": "A"},
        ],
    })
    svc = YouTubeService()
    out = svc.expand_playlist_url("https://soundcloud.com/a/sets/x")
    assert out is not None
    assert out["name"] == "Hardstyle Bangers"
    assert len(out["tracks"]) == 2
    assert out["total"] == 2
    assert out["truncated"] is False
    assert out["tracks"][0] == {"url": "https://soundcloud.com/a/t1", "title": "T1", "uploader": "A"}


def test_expand_playlist_url_not_a_playlist(monkeypatch) -> None:
    _mock_ydl(monkeypatch, {"_type": "video", "title": "Single Track"})
    svc = YouTubeService()
    assert svc.expand_playlist_url("https://soundcloud.com/a/track") is None


def test_expand_playlist_url_extract_error(monkeypatch) -> None:
    _mock_ydl(monkeypatch, RuntimeError("boom"))
    svc = YouTubeService()
    assert svc.expand_playlist_url("https://soundcloud.com/a/sets/x") is None


def test_expand_playlist_url_truncation(monkeypatch) -> None:
    entries = [
        {"webpage_url": f"https://soundcloud.com/a/t{i}", "title": f"T{i}", "uploader": "A"}
        for i in range(10)
    ]
    _mock_ydl(monkeypatch, {"_type": "playlist", "title": "Big", "entries": entries})
    svc = YouTubeService()
    out = svc.expand_playlist_url("https://soundcloud.com/a/sets/big", limit=3)
    assert out is not None
    assert len(out["tracks"]) == 3
    assert out["total"] == 10
    assert out["truncated"] is True


def test_expand_playlist_url_filters_api_pseudo_urls(monkeypatch) -> None:
    _mock_ydl(monkeypatch, {
        "_type": "playlist",
        "title": "Mixed",
        "entries": [
            {"webpage_url": "https://api.soundcloud.com/tracks/123", "title": "Pseudo", "uploader": "A"},
            {"webpage_url": "https://soundcloud.com/a/real", "title": "Real", "uploader": "A"},
            None,  # yt-dlp liefert None-Entries bei gelöschten Tracks
        ],
    })
    svc = YouTubeService()
    out = svc.expand_playlist_url("https://soundcloud.com/a/sets/x")
    assert out is not None
    assert len(out["tracks"]) == 1
    assert out["tracks"][0]["title"] == "Real"


def test_expand_playlist_url_all_entries_unusable(monkeypatch) -> None:
    _mock_ydl(monkeypatch, {
        "_type": "playlist",
        "title": "Ghost",
        "entries": [None, {"webpage_url": "", "title": "x"}],
    })
    svc = YouTubeService()
    assert svc.expand_playlist_url("https://soundcloud.com/a/sets/ghost") is None


def test_expand_playlist_url_fallback_name(monkeypatch) -> None:
    _mock_ydl(monkeypatch, {
        "_type": "playlist",
        "title": "",
        "entries": [{"webpage_url": "https://soundcloud.com/a/t1", "title": "T1", "uploader": "A"}],
    })
    svc = YouTubeService()
    out = svc.expand_playlist_url("https://soundcloud.com/a/sets/x")
    assert out is not None
    assert out["name"] == "Imported Playlist"
