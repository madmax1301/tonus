#!/usr/bin/env python3
"""Generic Lückenfüller — wanted tracks aus N Quellen → tonus-Queue.

Quellen (--source):

    listenbrainz-recs            ListenBrainz CF-Recommendations
    listenbrainz-playlist <slug> LB-Playlist via Slug oder MBID
    file <path>                  CSV/Text-Datei mit "artist;title" pro Zeile
    navidrome-playlist <id>      Subsonic-Playlist nach [MISSING]-Markern scannen

Source-Reader liegen in ``services/discovery.py``. Dieses Skript ist ein dünner
CLI-Wrapper, der die gefundenen Items per Deezer-Search auflöst und an die
tonus-API queued.

Beispiele:

    python3 sync_missing_tracks.py --source listenbrainz-recs \\
        --listenbrainz-user madmax1301 \\
        --tonus-url http://192.168.1.6:8000

    python3 sync_missing_tracks.py --source file --file ~/wanted.csv

    python3 sync_missing_tracks.py --source navidrome-playlist \\
        --playlist-id 11DKMb... \\
        --navidrome-url https://navidrome.example.com \\
        --navidrome-user admin --navidrome-password $NAVIDROME_PASSWORD
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Dict, Optional

try:
    import requests
except ImportError:
    print("Fehlt: requests (pip install requests)", file=sys.stderr)
    sys.exit(2)

_BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from services.discovery import collect_wanted_tracks, deezer_search_track  # noqa: E402


USER_AGENT = "tonus-sync/1.0"


def tonus_queue(
    track: Dict,
    tonus_url: str,
    location: str,
    fmt: Optional[str],
    quality: Optional[str],
    token: Optional[str],
) -> str:
    """Returns 'queued' | 'duplicate' | 'error'."""
    artist_obj = track.get("artist") or {}
    album_obj = track.get("album") or {}
    track_hint = {
        "id": str(track.get("id", "")),
        "name": track.get("title", ""),
        "artist": artist_obj.get("name", ""),
        "album": album_obj.get("title", ""),
        "album_art": (
            album_obj.get("cover_xl")
            or album_obj.get("cover_big")
            or album_obj.get("cover_medium")
        ),
    }
    body = {
        "track_id": str(track.get("id", "")),
        "location": location,
        "provider": "deezer",
        "track_hint": track_hint,
    }
    if fmt:
        body["format"] = fmt
    if quality:
        body["quality"] = quality
    headers = {"User-Agent": USER_AGENT}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        r = requests.post(
            f"{tonus_url.rstrip('/')}/api/download",
            json=body,
            timeout=30,
            headers=headers,
        )
        if r.status_code == 409:
            return "duplicate"
        r.raise_for_status()
        return "queued"
    except Exception as e:
        print(f"  [queue-err] {track_hint['name']}: {e}", file=sys.stderr)
        return "error"


def main() -> None:
    p = argparse.ArgumentParser(
        description="Generic Lückenfüller — Tracks aus N Quellen → tonus-Queue"
    )
    p.add_argument(
        "--source",
        required=True,
        choices=[
            "listenbrainz-recs",
            "listenbrainz-playlist",
            "file",
            "navidrome-playlist",
        ],
    )
    p.add_argument("--listenbrainz-user")
    p.add_argument(
        "--listenbrainz-slug",
        help="z.B. 'daily-jams' für listenbrainz-playlist",
    )
    p.add_argument("--file", help="Pfad zur Track-Liste (für source=file)")
    p.add_argument("--playlist-id", help="Navidrome Playlist-ID (für navidrome-playlist)")
    p.add_argument("--navidrome-url", help="Navidrome Base-URL (für navidrome-playlist)")
    p.add_argument("--navidrome-user", help="(für navidrome-playlist)")
    p.add_argument("--navidrome-password", help="(für navidrome-playlist)")
    p.add_argument("--tonus-url", default="http://localhost:8000")
    p.add_argument(
        "--tonus-token",
        default=os.environ.get("TONUS_API_TOKEN"),
        help="Bearer-Token (optional, fallback env TONUS_API_TOKEN)",
    )
    p.add_argument("--location", default="navidrome", choices=["local", "navidrome"])
    p.add_argument("--format", default=None)
    p.add_argument("--quality", default=None)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--max-queue", type=int, default=100)
    args = p.parse_args()

    print(f"[1/2] Reading source: {args.source}")
    wanted = collect_wanted_tracks(
        source=args.source,
        listenbrainz_user=args.listenbrainz_user,
        listenbrainz_slug=args.listenbrainz_slug,
        file_path=args.file,
        navidrome_playlist_id=args.playlist_id,
        navidrome_url=args.navidrome_url,
        navidrome_user=args.navidrome_user,
        navidrome_password=args.navidrome_password,
    )
    print(f"  → {len(wanted)} wanted tracks")
    if not wanted:
        print("Nichts zu tun.")
        return

    print(f"\n[2/2] Pipeline: search Deezer → queue (max {args.max_queue}) ...")
    queued = 0
    duplicates = 0
    not_found = 0
    errors = 0
    for w in wanted:
        if queued >= args.max_queue:
            break
        track = deezer_search_track(w["artist"], w["title"])
        if not track:
            not_found += 1
            print(f"  [not-found] {w['artist']} - {w['title']}")
            continue
        if args.dry_run:
            artist_name = (track.get("artist") or {}).get("name", "")
            print(f"  [dry] {track.get('title')} — {artist_name}")
            queued += 1
            continue
        status = tonus_queue(
            track,
            args.tonus_url,
            args.location,
            args.format,
            args.quality,
            args.tonus_token,
        )
        artist_name = (track.get("artist") or {}).get("name", "")
        if status == "queued":
            queued += 1
            print(f"  [ok ] {track.get('title')} — {artist_name}")
        elif status == "duplicate":
            duplicates += 1
            print(f"  [dup] {track.get('title')} — schon in Library/Queue")
        else:
            errors += 1

    print("\n=== Sync done ===")
    print(f"In Queue:           {queued}")
    print(f"Duplikate:          {duplicates}")
    print(f"Auf Deezer nicht:   {not_found}")
    print(f"Fehler:             {errors}")


if __name__ == "__main__":
    main()
