#!/usr/bin/env python3
"""Backfill embedded album art for opus files without cover (Tonus v0.4.2).

Walked die Navidrome-Library, findet opus-Files ohne ``metadata_block_picture``,
sucht Cover via MusicBrainz/Cover-Art-Archive (primary) + Deezer-API-Search
(fallback), und embeddet das gefundene Cover via mutagen.

Pattern wie ``cleanup_set_mismatches.py`` (Phase J.1 v0.3.3):
  - ``--dry-run`` ist Default — kein Schreib-Modus ohne ``--apply``
  - Idempotent: Files mit existierendem cover_block werden geskipped
  - Operator-Tool: läuft im Container per ``docker exec``

Übliche Aufrufe::

    # Dry-run, zeigt nur was passieren würde:
    docker exec tonus python3 /app/backend/scripts/backfill_album_art.py

    # Dry-run mit Limit für initial-Test:
    docker exec tonus python3 /app/backend/scripts/backfill_album_art.py --limit 20

    # Apply für echten Schreib-Modus:
    docker exec tonus python3 /app/backend/scripts/backfill_album_art.py --apply

Sicherheits-Notiz: das Script macht direkte HTTP-Requests an MusicBrainz und
Deezer-APIs — KEINE user-controlled URLs, daher keine SSRF-Allowlist-Pflicht
wie bei ``services.metadata._download_album_art``. URLs sind hardcoded.
"""
from __future__ import annotations

import argparse
import base64
import sys
import time
from pathlib import Path
from typing import Optional, Tuple

# Mutagen wird über das venv des Containers gestellt — kein zusätzlicher Pip.
from mutagen import File as MutagenFile
from mutagen.flac import Picture
import requests


# MusicBrainz Terms-of-Service: mandates eindeutiger User-Agent für Bot-
# Requests, plus max 1 req/s. Wenn der Script gegen Rate-Limit läuft,
# kommt 503 zurück — wir würden re-queryen aber besser polite sein.
_MB_USER_AGENT = "Tonus-Backfill/0.4.2 (https://github.com/madmax1301/tonus)"
_MB_RATE_LIMIT_S = 1.1
_DEEZER_RATE_LIMIT_S = 0.3
_REQUEST_TIMEOUT = 20

_last_mb_call: float = 0.0
_last_deezer_call: float = 0.0


def _throttle_mb() -> None:
    global _last_mb_call
    delta = time.time() - _last_mb_call
    if delta < _MB_RATE_LIMIT_S:
        time.sleep(_MB_RATE_LIMIT_S - delta)
    _last_mb_call = time.time()


def _throttle_deezer() -> None:
    global _last_deezer_call
    delta = time.time() - _last_deezer_call
    if delta < _DEEZER_RATE_LIMIT_S:
        time.sleep(_DEEZER_RATE_LIMIT_S - delta)
    _last_deezer_call = time.time()


def mb_search_release_mbid(artist: str, album: str) -> Optional[str]:
    """MusicBrainz-Search → erste Release-MBID mit höchstem Match-Score."""
    _throttle_mb()
    try:
        params = {
            "query": f'artist:"{artist}" AND release:"{album}"',
            "fmt": "json",
            "limit": 3,
        }
        r = requests.get(
            "https://musicbrainz.org/ws/2/release/",
            params=params,
            headers={"User-Agent": _MB_USER_AGENT},
            timeout=_REQUEST_TIMEOUT,
        )
        if r.status_code != 200:
            return None
        for release in r.json().get("releases", [])[:3]:
            mbid = release.get("id")
            if mbid:
                return mbid
    except Exception as e:
        print(f"  ⚠ MB search error: {type(e).__name__}")
    return None


def fetch_url_bytes(url: str) -> Optional[bytes]:
    """Generic HTTP-fetch mit basic-Validation der Response-Größe."""
    try:
        r = requests.get(url, timeout=_REQUEST_TIMEOUT, allow_redirects=True)
        if r.status_code == 200 and len(r.content) > 1000:
            return r.content
    except Exception as e:
        print(f"  ⚠ Fetch error ({type(e).__name__}): {url[:80]}")
    return None


def deezer_search_album_cover_url(artist: str, album: str) -> Optional[str]:
    """Deezer-API: search/album → cover_xl (1000×1000) wenn verfügbar."""
    _throttle_deezer()
    try:
        params = {"q": f'artist:"{artist}" album:"{album}"', "limit": 3}
        r = requests.get(
            "https://api.deezer.com/search/album",
            params=params,
            timeout=_REQUEST_TIMEOUT,
        )
        if r.status_code != 200:
            return None
        for hit in r.json().get("data", [])[:3]:
            for size in ("cover_xl", "cover_big", "cover_medium", "cover"):
                if hit.get(size):
                    return hit[size]
    except Exception as e:
        print(f"  ⚠ Deezer search error: {type(e).__name__}")
    return None


def extract_artist_album(audio) -> Tuple[Optional[str], Optional[str]]:
    """Liest artist+album aus opus-Tags (Vorbis-Comment-Format)."""
    if not audio.tags:
        return None, None

    def _first(key: str) -> str:
        val = audio.tags.get(key, [""])
        if not val:
            return ""
        if isinstance(val, list):
            return str(val[0]) if val else ""
        return str(val)

    artist = _first("artist").strip()
    album = _first("album").strip()
    # Comma-/Semicolon-getrennt → erste artist
    if artist:
        artist = artist.split(";")[0].split(",")[0].strip()
    return (artist or None), (album or None)


def detect_image_mime(data: bytes) -> str:
    """Sniffs MIME-Type aus den ersten Bytes."""
    if not data:
        return "image/jpeg"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"GIF8"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def build_picture_block_b64(data: bytes, mime: str) -> str:
    """FLAC-Picture-Block bauen und base64-encoded zurückgeben (Opus-Format)."""
    pic = Picture()
    pic.type = 3   # Cover (front)
    pic.mime = mime
    pic.desc = ""
    pic.data = data
    return base64.b64encode(pic.write()).decode("ascii")


def embed_cover_in_opus(file_path: Path, cover_bytes: bytes) -> bool:
    """Embed cover via mutagen — atomic save."""
    audio = MutagenFile(str(file_path))
    if audio is None:
        return False
    if not audio.tags:
        audio.add_tags()
    mime = detect_image_mime(cover_bytes)
    block = build_picture_block_b64(cover_bytes, mime)
    audio.tags["metadata_block_picture"] = [block]
    audio.save()
    return True


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Backfill embedded album art for opus files without cover.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--library", default="/music",
        help="Library-Root-Path (default: /music)",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Tatsächlich embedden. Ohne diesen Flag wird nur dry-run gemacht.",
    )
    parser.add_argument(
        "--limit", type=int, default=0,
        help="Max files to process (0 = no limit, default: 0)",
    )
    parser.add_argument(
        "--skip-no-tags", action="store_true",
        help="Skip files ohne artist+album tags (default: warn-and-skip).",
    )
    args = parser.parse_args()

    library = Path(args.library)
    if not library.is_dir():
        print(f"ERROR: Library directory not found: {library}", file=sys.stderr)
        return 1

    print("=" * 60)
    print("Tonus Backfill Album Art (v0.4.2)")
    print("=" * 60)
    print(f"Library: {library}")
    print(f"Mode:    {'APPLY (writing changes)' if args.apply else 'DRY-RUN (use --apply to embed)'}")
    if args.limit:
        print(f"Limit:   {args.limit} files")
    print()

    stats = {
        "scanned": 0,
        "already_has_cover": 0,
        "no_tags": 0,
        "no_match": 0,
        "found_mb": 0,
        "found_deezer": 0,
        "embedded": 0,
        "errors": 0,
    }

    files = sorted(library.rglob("*.opus"))
    if args.limit:
        files = files[: args.limit]

    for f in files:
        stats["scanned"] += 1
        try:
            audio = MutagenFile(str(f))
            if audio is None:
                stats["errors"] += 1
                continue
            if audio.tags and audio.tags.get("metadata_block_picture"):
                stats["already_has_cover"] += 1
                continue

            artist, album = extract_artist_album(audio)
            if not artist or not album:
                stats["no_tags"] += 1
                if not args.skip_no_tags:
                    print(f"⚠ No artist/album tags: {f.relative_to(library)}")
                continue

            rel_path = f.relative_to(library)
            print(f"→ {rel_path}")
            print(f"  artist={artist!r}  album={album!r}")

            # Primary: MusicBrainz → CoverArtArchive
            cover_bytes: Optional[bytes] = None
            mbid = mb_search_release_mbid(artist, album)
            if mbid:
                cover_bytes = fetch_url_bytes(
                    f"https://coverartarchive.org/release/{mbid}/front"
                )
                if cover_bytes:
                    stats["found_mb"] += 1
                    print(f"  ✓ MB+CAA hit (mbid={mbid[:8]}…) cover={len(cover_bytes)}b")

            # Fallback: Deezer
            if not cover_bytes:
                deezer_url = deezer_search_album_cover_url(artist, album)
                if deezer_url:
                    cover_bytes = fetch_url_bytes(deezer_url)
                    if cover_bytes:
                        stats["found_deezer"] += 1
                        print(f"  ✓ Deezer hit cover={len(cover_bytes)}b")

            if not cover_bytes:
                stats["no_match"] += 1
                print("  ✗ No cover found")
                continue

            if args.apply:
                try:
                    if embed_cover_in_opus(f, cover_bytes):
                        stats["embedded"] += 1
                        print("  ✓ Embedded")
                    else:
                        stats["errors"] += 1
                        print("  ✗ Embed failed (mutagen)")
                except Exception as e:
                    stats["errors"] += 1
                    print(f"  ✗ Embed error: {type(e).__name__}: {e}")
            else:
                print("  (dry-run — not embedding)")

        except KeyboardInterrupt:
            print("\nInterrupted by user")
            break
        except Exception as e:
            stats["errors"] += 1
            print(f"  ⚠ {type(e).__name__}: {e}")

    print()
    print("=" * 60)
    print("Summary")
    print("=" * 60)
    for k, v in stats.items():
        print(f"  {k:20}  {v}")
    if not args.apply and stats["found_mb"] + stats["found_deezer"] > 0:
        print()
        print("Run again with --apply to actually embed the found covers.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
