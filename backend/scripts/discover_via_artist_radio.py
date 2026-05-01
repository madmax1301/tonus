#!/usr/bin/env python3
"""Discovery-Pipeline: ListenBrainz-Top-Artists → Deezer Artist Radio → tonus-Queue.

Sucht für jeden deiner meist-gehörten Künstler (LB History) Empfehlungs-Tracks
(Deezer Artist Radio), filtert solche raus die du schon gehört hast, und queut
sie über die tonus-API. Funktioniert auch von einem anderen Host als tonus,
sofern die tonus-API erreichbar ist.

Discovery-Logik selbst liegt in ``services/discovery.py`` — dieses Skript ist
ein dünner CLI-Wrapper drumherum (auch nutzbar vom Plugin-Endpoint inproc).

Usage:
    python3 discover_via_artist_radio.py \\
        --listenbrainz-user madmax1301 \\
        --tonus-url http://192.168.1.6:8000 \\
        --top-artists 10 \\
        --tracks-per-artist 5 \\
        --location navidrome \\
        --dry-run
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

# Skript wird typischerweise als ``python3 scripts/discover_via_artist_radio.py``
# aus ``code/backend/`` gestartet — Parent-Pfad in sys.path packen, damit der
# ``services``-Import funktioniert. Funktioniert auch wenn das Skript via
# Symlink aus einem anderen Cwd aufgerufen wird.
_BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from services.discovery import discover_via_artist_radio  # noqa: E402


USER_AGENT = "tonus-discovery/1.0"


def tonus_queue_track(
    tonus_url: str,
    track: Dict,
    location: str,
    fmt: Optional[str],
    quality: Optional[str],
    token: Optional[str],
) -> bool:
    """Postet einen Deezer-Track an /api/download. Track-Format = Deezer-API."""
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
            detail = r.json().get("detail", "duplicate")
            print(f"  [skip] {track_hint['name']} by {track_hint['artist']}: {detail}")
            return False
        r.raise_for_status()
        return True
    except Exception as e:
        print(f"  [queue-err] {track_hint['name']}: {e}", file=sys.stderr)
        return False


def main() -> None:
    p = argparse.ArgumentParser(
        description="Discovery via LB-History + Deezer Artist Radio"
    )
    p.add_argument("--listenbrainz-user", required=True)
    p.add_argument("--tonus-url", default="http://localhost:8000")
    p.add_argument(
        "--tonus-token",
        default=os.environ.get("TONUS_API_TOKEN"),
        help="Bearer-Token (optional, fallback env TONUS_API_TOKEN)",
    )
    p.add_argument("--top-artists", type=int, default=10)
    p.add_argument("--tracks-per-artist", type=int, default=5)
    p.add_argument("--history-days", type=int, default=90)
    p.add_argument("--location", default="navidrome", choices=["local", "navidrome"])
    p.add_argument("--format", default=None)
    p.add_argument("--quality", default=None)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument(
        "--max-queue",
        type=int,
        default=50,
        help="Sicherheitsdeckel — max Tracks pro Run",
    )
    args = p.parse_args()

    print(
        f"[1/2] Discovery (LB-Top-{args.top_artists}, "
        f"{args.tracks_per_artist} Tracks/Artist, Hardcap {args.max_queue})..."
    )
    candidates = discover_via_artist_radio(
        listenbrainz_user=args.listenbrainz_user,
        top_artists=args.top_artists,
        tracks_per_artist=args.tracks_per_artist,
        history_days=args.history_days,
        max_total=args.max_queue,
    )
    if not candidates:
        print(
            "  Keine Discovery-Kandidaten gefunden — ist deine LB-History indexiert?",
            file=sys.stderr,
        )
        sys.exit(1)
    print(f"  → {len(candidates)} Kandidaten")

    print(f"\n[2/2] Queue an {args.tonus_url} ...")
    queued = 0
    duplicates = 0
    for item in candidates:
        track = item.get("deezer_track")
        if not track:
            continue
        if args.dry_run:
            print(f"  [dry] {item['title']} — {item['artist']}")
            queued += 1
            continue
        ok = tonus_queue_track(
            args.tonus_url,
            track,
            args.location,
            args.format,
            args.quality,
            args.tonus_token,
        )
        if ok:
            queued += 1
            print(f"  [ok ] {item['title']} — {item['artist']}")
        else:
            duplicates += 1

    print("\n=== Discovery done ===")
    print(f"In Queue:           {queued}")
    print(f"Skipped (Dup/Lib):  {duplicates}")
    if args.dry_run:
        print("(Dry-Run — nichts wirklich gequeued)")


if __name__ == "__main__":
    main()
