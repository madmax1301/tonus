"""Spotify Extended Streaming History Parser.

Spotify bietet einen separaten Datenexport ("Extended Streaming History") der
eine Reihe von JSON-Dateien (`Streaming_History_Audio_<year>[_N].json`)
liefert — pro Datei ein Array von Listening-Events mit Schema:

    {
      "ts": "2024-12-31T23:59:59Z",
      "ms_played": 240000,
      "master_metadata_track_name": "Track Name",
      "master_metadata_album_artist_name": "Artist Name",
      "master_metadata_album_album_name": "Album Name",
      "spotify_track_uri": "spotify:track:...",
      "skipped": false, "shuffle": false, "offline": false, ...
      "episode_name": null,         // != null = Podcast (skip)
      "audiobook_title": null,      // != null = Audiobook (skip)
      ...
    }

Aggregationsstrategie:
    - Group by (artist_lower, title_lower) damit Schreibvarianten nicht
      verloren gehen
    - Filter Podcasts/Audiobooks (track_name == null)
    - Filter zu kurze Plays (ms_played < min_ms_played) — vermutlich Skip
    - Filter Aggregat unter min_play_count nach Aggregation
    - Optional Date-Range-Filter via ts

Auto-Playlists (Phase I-Integration):
    - Pro Track werden Playlist-Names abgeleitet aus den Plays:
        - "Spotify History · 2024"      (pro Jahr in dem der Track gehört wurde)
        - "Spotify History · 2024-12"   (pro Monat)
    - Ein Track der über 5 Jahre verteilt gehört wurde landet auf 5 Jahres-
      Playlists und entsprechend vielen Monats-Playlists. Idempotent dank
      Subsonic-Playlist-Reconcile.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, date
from typing import Any, Dict, List, Optional, Tuple


def parse_streaming_history(
    files_records: List[List[Dict[str, Any]]],
    *,
    # Default 0: alle Music-Tracks rein (auch skipped). User-Policy ist
    # "wenn ich's einmal angeklickt hab, will ich's importieren". Höher
    # setzen filtert Skip-Through-Klicks raus, aber das ist optional.
    # Non-Music (Podcasts/Audiobooks) wird über master_metadata_track_name
    # weiterhin rausgefiltert — das ist orthogonal zum ms_played-Filter.
    min_ms_played: int = 0,
    min_play_count: int = 1,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    auto_playlist_year: bool = True,
    auto_playlist_month: bool = True,
    playlist_prefix: str = "Spotify History",
) -> Dict[str, Any]:
    """Aggregiert eine Liste von Spotify-Streaming-History-Records zu einer
    Liste von eindeutigen Tracks mit Auto-Playlist-Namen.

    Parameters:
        files_records: Liste von Record-Arrays — typisch ein Array pro JSON-
            File. Inhalt wird flach iteriert, also macht's keinen Unterschied
            ob ein Caller mehrere Arrays oder ein Mega-Array übergibt.
        min_ms_played: Pro-Event-Schwelle. <30s = wahrscheinlich Skip-Klick,
            kein echtes Play. Default ist Spotify's eigene Heuristik für
            "Listened-To"-Plays.
        min_play_count: Aggregat-Schwelle. Tracks die weniger als N-mal
            gespielt wurden fallen aus. Default 1 lässt alles durch; User
            kann hochsetzen um nur "favorisierte" Tracks zu importieren.
        date_from / date_to: ISO-Date-Strings (YYYY-MM-DD). None = open-ended.
        auto_playlist_year / auto_playlist_month: ob pro Jahr / Monat eine
            Playlist-Membership angelegt werden soll.
        playlist_prefix: Präfix für Auto-Playlists. "Spotify History · 2024".

    Returns:
        {
          "tracks": [
            {
              "artist": "Warrant",
              "title": "Train Train",
              "album": "Cherry Pie",
              "spotify_uri": "spotify:track:...",
              "play_count": 15,
              "total_ms": 3000000,
              "first_played": "2018-01-17T19:54:41Z",
              "last_played": "2024-03-12T08:22:01Z",
              "playlist_names": ["Spotify History · 2018",
                                 "Spotify History · 2018-01",
                                 "Spotify History · 2024",
                                 "Spotify History · 2024-03"],
            },
            ...
          ],
          "stats": {
            "total_events": 200000,
            "filtered_short": 12000,        # < min_ms_played
            "filtered_non_music": 8000,     # podcasts/audiobooks
            "filtered_out_of_range": 0,
            "unique_tracks_before_count_filter": 15000,
            "unique_tracks_after_count_filter": 5000,
            "skipped_play_count_below_threshold": 10000,
          }
        }
    """
    # Date-Filter aufbereiten — Spotify-Events sind UTC-Z-Strings, einfacher
    # via Substring-Vergleich gegen ISO-Date-Prefix als datetime-Parsing.
    df = (date_from or "").strip()
    dt = (date_to or "").strip()

    stats = {
        "total_events": 0,
        "filtered_short": 0,
        "filtered_non_music": 0,
        "filtered_out_of_range": 0,
        "unique_tracks_before_count_filter": 0,
        "unique_tracks_after_count_filter": 0,
        "skipped_play_count_below_threshold": 0,
    }

    # Bucket nach (artist_lower, title_lower) — case-insensitive Dedup gegen
    # Schreibvarianten. Original-Casing der ersten Begegnung wird behalten.
    buckets: Dict[Tuple[str, str], Dict[str, Any]] = {}

    for records in files_records:
        if not isinstance(records, list):
            continue
        for ev in records:
            stats["total_events"] += 1
            if not isinstance(ev, dict):
                continue

            track_name = (ev.get("master_metadata_track_name") or "").strip()
            if not track_name:
                # Podcast oder Audiobook — kein Track-Name-Feld
                stats["filtered_non_music"] += 1
                continue

            artist_name = (ev.get("master_metadata_album_artist_name") or "").strip()
            if not artist_name:
                stats["filtered_non_music"] += 1
                continue

            ms = int(ev.get("ms_played") or 0)
            if ms < min_ms_played:
                stats["filtered_short"] += 1
                continue

            ts = (ev.get("ts") or "").strip()
            if df and ts < df:
                stats["filtered_out_of_range"] += 1
                continue
            if dt and ts > dt + "T23:59:59Z":
                stats["filtered_out_of_range"] += 1
                continue

            key = (artist_name.lower(), track_name.lower())
            bucket = buckets.get(key)
            if bucket is None:
                bucket = {
                    "artist": artist_name,
                    "title": track_name,
                    "album": (ev.get("master_metadata_album_album_name") or "").strip(),
                    "spotify_uri": (ev.get("spotify_track_uri") or "").strip() or None,
                    "play_count": 0,
                    "total_ms": 0,
                    "first_played": ts,
                    "last_played": ts,
                    # Set damit Doppel-Memberships nicht entstehen
                    "_year_set": set(),
                    "_month_set": set(),
                }
                buckets[key] = bucket

            bucket["play_count"] += 1
            bucket["total_ms"] += ms
            if ts and (not bucket["first_played"] or ts < bucket["first_played"]):
                bucket["first_played"] = ts
            if ts and (not bucket["last_played"] or ts > bucket["last_played"]):
                bucket["last_played"] = ts

            # Year / Month aus dem ISO-Timestamp ablesen — Spotify liefert
            # immer YYYY-MM-DD-Prefix, also Substring-Slice ist safe und
            # billiger als datetime-Parse.
            if len(ts) >= 7:
                if auto_playlist_year:
                    bucket["_year_set"].add(ts[:4])
                if auto_playlist_month:
                    bucket["_month_set"].add(ts[:7])

    stats["unique_tracks_before_count_filter"] = len(buckets)

    out_tracks: List[Dict[str, Any]] = []
    for (_, _), b in buckets.items():
        if b["play_count"] < min_play_count:
            stats["skipped_play_count_below_threshold"] += 1
            continue

        playlist_names: List[str] = []
        if auto_playlist_year:
            for y in sorted(b["_year_set"]):
                playlist_names.append(f"{playlist_prefix} · {y}")
        if auto_playlist_month:
            for m in sorted(b["_month_set"]):
                playlist_names.append(f"{playlist_prefix} · {m}")

        out_tracks.append({
            "artist": b["artist"],
            "title": b["title"],
            "album": b["album"],
            "spotify_uri": b["spotify_uri"],
            "play_count": b["play_count"],
            "total_ms": b["total_ms"],
            "first_played": b["first_played"],
            "last_played": b["last_played"],
            "playlist_names": playlist_names,
        })

    stats["unique_tracks_after_count_filter"] = len(out_tracks)

    # Sortierung: häufigster Track zuerst — UI kann Top-N-Preview anzeigen
    # ohne extra sort.
    out_tracks.sort(key=lambda t: (-t["play_count"], -t["total_ms"]))

    return {"tracks": out_tracks, "stats": stats}
