#!/usr/bin/env python3
"""Genre-Backfill für eine bestehende Music-Library.

Iteriert über alle MP3/FLAC/M4A/Opus-Files unter <library_path>, liest Artist+Title
aus den existierenden Tags, sucht via Deezer das passende Album und schreibt nur
den Genre-Tag zurück. Alle anderen Metadaten (Title, Artist, Album, Cover etc.)
bleiben unverändert.

Designed standalone — keine tonus-Imports — damit du es entweder im tonus-Container
oder direkt auf dem NAS-Host (mit `pip install mutagen requests`) laufen lassen kannst.

Usage:
    python3 backfill_genres.py /path/to/library
    python3 backfill_genres.py /path/to/library --dry-run
    python3 backfill_genres.py /path/to/library --force --limit 50
    python3 backfill_genres.py /path/to/library --rate-limit-ms 500

Optionen:
    --dry-run         Nur zeigen, was geschrieben würde — nichts ändern
    --force           Auch Files mit existierendem Genre-Tag re-taggen
    --limit N         Maximal N Files verarbeiten (für Tests)
    --rate-limit-ms   Pause zwischen Deezer-Calls in ms (Default 200)
    --skip-on-empty   Tracks die Deezer NICHT findet, einfach überspringen statt warnen
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Dict, List, Optional, Tuple

try:
    import requests
except ImportError:
    print("Fehlt: requests (pip install requests)", file=sys.stderr)
    sys.exit(2)

try:
    from mutagen import File as MutagenFile
    from mutagen.id3 import ID3, TCON
    from mutagen.flac import FLAC
    from mutagen.mp4 import MP4
    from mutagen.oggopus import OggOpus
except ImportError:
    print("Fehlt: mutagen (pip install mutagen)", file=sys.stderr)
    sys.exit(2)


DEEZER_BASE = "https://api.deezer.com"
USER_AGENT = "tonus-genre-backfill/1.0"

# Cache: spart pro Album den Re-Lookup. Bei 12 Tracks vom selben Album = 1 statt 12 HTTP-Calls.
_ALBUM_GENRE_CACHE: Dict[str, List[str]] = {}


def _http_get(path: str, params: Optional[Dict] = None) -> Optional[Dict]:
    url = f"{DEEZER_BASE}{path}" if path.startswith("/") else f"{DEEZER_BASE}/{path}"
    try:
        r = requests.get(url, params=params or {}, timeout=15, headers={"User-Agent": USER_AGENT})
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"  [http-err] {url}: {e}", file=sys.stderr)
        return None


def deezer_search_track(artist: str, title: str) -> Optional[Dict]:
    """Sucht einen Track per artist+title und gibt den ersten Treffer zurück."""
    query = f"{artist} {title}".strip()
    if not query:
        return None
    data = _http_get("/search/track", {"q": query, "limit": 1})
    if not data:
        return None
    items = data.get("data") or []
    return items[0] if items else None


def get_album_genres(album_id: str) -> List[str]:
    """Holt Genre-Liste eines Deezer-Albums mit Cache."""
    if not album_id:
        return []
    cached = _ALBUM_GENRE_CACHE.get(album_id)
    if cached is not None:
        return cached
    data = _http_get(f"/album/{album_id}")
    if not data:
        _ALBUM_GENRE_CACHE[album_id] = []
        return []
    block = (data.get("genres") or {}).get("data") or []
    seen = set()
    out: List[str] = []
    for g in block:
        n = (g.get("name") or "").strip()
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    _ALBUM_GENRE_CACHE[album_id] = out
    return out


def read_artist_title(audio, file_ext: str) -> Tuple[str, str, Optional[str]]:
    """Liest Artist, Title und vorhandenen Genre-Tag (falls da) aus einer Audio-Datei."""
    artist = ""
    title = ""
    existing_genre: Optional[str] = None

    try:
        if file_ext == ".mp3":
            tags = audio.tags
            if tags is None:
                return "", "", None
            artist = str(tags.get("TPE1", "")).strip() if tags.get("TPE1") else ""
            title = str(tags.get("TIT2", "")).strip() if tags.get("TIT2") else ""
            tcon = tags.get("TCON")
            if tcon:
                existing_genre = str(tcon).strip() or None
        elif file_ext == ".flac":
            artist = (audio.get("ARTIST", [""])[0] or "").strip()
            title = (audio.get("TITLE", [""])[0] or "").strip()
            g = audio.get("GENRE", [""])[0] or ""
            existing_genre = g.strip() or None
        elif file_ext == ".m4a":
            artist_list = audio.get("\xa9ART", [])
            artist = (artist_list[0] if artist_list else "").strip()
            title_list = audio.get("\xa9nam", [])
            title = (title_list[0] if title_list else "").strip()
            g_list = audio.get("\xa9gen", [])
            existing_genre = (g_list[0].strip() if g_list else None) or None
        elif file_ext in (".opus", ".ogg"):
            artist = (audio.get("ARTIST", [""])[0] or "").strip()
            title = (audio.get("TITLE", [""])[0] or "").strip()
            g = audio.get("GENRE", [""])[0] or ""
            existing_genre = g.strip() or None
    except Exception as e:
        print(f"  [tag-read-err] {e}", file=sys.stderr)

    return artist, title, existing_genre


def write_genre(audio, file_path: str, genre_str: str, file_ext: str) -> bool:
    """Schreibt nur den Genre-Tag in die Datei (alles andere bleibt)."""
    try:
        if file_ext == ".mp3":
            if audio.tags is None:
                audio.add_tags()
            audio.tags["TCON"] = TCON(encoding=3, text=genre_str)
            audio.save()
        elif file_ext == ".flac":
            audio["GENRE"] = genre_str
            audio.save()
        elif file_ext == ".m4a":
            audio["\xa9gen"] = [genre_str]
            audio.save()
        elif file_ext in (".opus", ".ogg"):
            audio["GENRE"] = genre_str
            audio.save()
        else:
            return False
        return True
    except Exception as e:
        print(f"  [tag-write-err] {file_path}: {e}", file=sys.stderr)
        return False


def iter_audio_files(library_path: str):
    """Generator, der über alle relevanten Audio-Dateien iteriert."""
    exts = (".mp3", ".flac", ".m4a", ".opus", ".ogg")
    for root, _, files in os.walk(library_path):
        for f in sorted(files):
            if f.lower().endswith(exts):
                yield os.path.join(root, f), os.path.splitext(f)[1].lower()


def main():
    parser = argparse.ArgumentParser(description="Genre-Backfill für bestehende Music-Library")
    parser.add_argument("library_path", help="Pfad zur Music-Library (z.B. /volume1/docker/music)")
    parser.add_argument("--dry-run", action="store_true", help="Nichts schreiben, nur zeigen")
    parser.add_argument("--force", action="store_true", help="Auch Files mit existierendem Genre re-taggen")
    parser.add_argument("--limit", type=int, default=0, help="Max Files (0=alle)")
    parser.add_argument("--rate-limit-ms", type=int, default=200, help="Pause zwischen Deezer-Calls in ms")
    parser.add_argument("--skip-on-empty", action="store_true", help="Stille bei Not-Found-Tracks")
    args = parser.parse_args()

    if not os.path.isdir(args.library_path):
        print(f"Pfad existiert nicht: {args.library_path}", file=sys.stderr)
        sys.exit(1)

    stats = {
        "scanned": 0,
        "tagged": 0,
        "skipped_existing": 0,
        "skipped_no_artist_title": 0,
        "deezer_no_match": 0,
        "no_genre_in_album": 0,
        "errors": 0,
    }

    rate_sleep = max(0, args.rate_limit_ms) / 1000.0
    started = time.time()

    for file_path, ext in iter_audio_files(args.library_path):
        stats["scanned"] += 1
        if args.limit and stats["scanned"] > args.limit:
            stats["scanned"] -= 1
            break

        try:
            audio = MutagenFile(file_path)
            if audio is None:
                stats["errors"] += 1
                continue

            artist, title, existing = read_artist_title(audio, ext)

            if not artist or not title:
                stats["skipped_no_artist_title"] += 1
                continue

            if existing and not args.force:
                stats["skipped_existing"] += 1
                continue

            track = deezer_search_track(artist, title)
            time.sleep(rate_sleep)
            if not track:
                stats["deezer_no_match"] += 1
                if not args.skip_on_empty:
                    print(f"  [no-match] {artist} - {title}")
                continue

            album_id = str((track.get("album") or {}).get("id") or "")
            genres = get_album_genres(album_id)
            if not album_id or not genres:
                stats["no_genre_in_album"] += 1
                continue

            genre_str = "; ".join(genres)

            if args.dry_run:
                print(f"  [dry] {file_path}\n        → {genre_str}")
            else:
                ok = write_genre(audio, file_path, genre_str, ext)
                if ok:
                    stats["tagged"] += 1
                    print(f"  [ok ] {os.path.basename(file_path)} → {genre_str}")
                else:
                    stats["errors"] += 1

        except KeyboardInterrupt:
            print("\nAbbruch durch User.", file=sys.stderr)
            break
        except Exception as e:
            stats["errors"] += 1
            print(f"  [err] {file_path}: {e}", file=sys.stderr)

    elapsed = time.time() - started
    print("\n=== Backfill done ===")
    print(f"Dauer:               {elapsed:.0f}s")
    print(f"Files gescannt:      {stats['scanned']}")
    print(f"Genre geschrieben:   {stats['tagged']}")
    print(f"Schon getaggt (skip):{stats['skipped_existing']}")
    print(f"Ohne Artist/Title:   {stats['skipped_no_artist_title']}")
    print(f"Deezer kein Match:   {stats['deezer_no_match']}")
    print(f"Album ohne Genre:    {stats['no_genre_in_album']}")
    print(f"Fehler:              {stats['errors']}")
    print(f"Albums abgefragt:    {len(_ALBUM_GENRE_CACHE)}")


if __name__ == "__main__":
    main()
