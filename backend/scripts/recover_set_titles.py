#!/usr/bin/env python3
"""Recovery-Tool für Falsch-Matches wenn die Container-Logs nicht mehr da sind.

Companion zu ``cleanup_set_mismatches.py``: jenes Script braucht das Tonus-
Log um die echten YT-Titel zu kennen. Wenn der Container recreated wurde
(z.B. via ``docker compose pull && up -d``), ist der alte Log verloren.

Dieses Script geht den anderen Weg:

  1. Walked die Navidrome-Library, findet Audio-Files > MAX_TRACK_DURATION_S
     (default 900s = 15min). Aus dem Pfad ``/music/<Artist>/<Album>/<Title>``
     wird die ursprüngliche Spotify-Anfrage abgeleitet.
  2. Replayed die Legacy-Suche: ``ytsearch1: <Artist> <Title> official audio``
     mit yt-dlp (NO download — nur metadata-extract). Top-Result-Title wird
     als der "echte" Set-Name angenommen.
  3. Plant Rename + Retag (TITLE + ALBUM tags) damit die Datei unter dem
     richtigen Namen in der Library erscheint.

Limitations:

  - YT-Search-Results können sich seit dem Original-Download geändert haben.
    Für stabile Festival-Sets (Defqon.1, Q-dance) mit Millionen Views ist
    das Top-Result aber praktisch deterministisch — daher der Recovery-
    Ansatz hier robust genug.
  - Funktioniert nur für Files die der Tonus-Pfad-Konvention folgen
    (``/music/<Artist>/<Album>/<Title>.<ext>``). File-Drops außerhalb davon
    werden übersprungen.
  - Macht KEINEN echten Download. Nur metadata-extract via yt-dlp — schnell.

Default ``--dry-run`` — zeigt was passieren würde ohne irgendwas anzufassen.
``--apply`` schreibt tatsächlich Tags + File-Rename.

Usage (auf NAS via docker exec)::

    sudo docker exec tonus python3 /app/backend/scripts/recover_set_titles.py \\
        --library /music \\
        --dry-run

    # Nach Review:
    sudo docker exec tonus python3 /app/backend/scripts/recover_set_titles.py \\
        --library /music \\
        --apply

Danach: Navidrome-Library-Scan triggern damit Subsonic-DB die neuen Titel
sieht.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Iterator, List, Optional, Tuple

try:
    from mutagen import File as MutagenFile
except ImportError:
    print("ERROR: mutagen not installed. Run: pip install mutagen", file=sys.stderr)
    sys.exit(1)

try:
    import yt_dlp
except ImportError:
    print("ERROR: yt-dlp not installed. Run: pip install yt-dlp", file=sys.stderr)
    sys.exit(1)


# 15min Default. Wenn die Library auch legitime Extended-Mixes von 12-15min
# hat, lieber höher setzen damit die nicht versehentlich getroffen werden.
DEFAULT_MIN_DURATION_S = 900

_FORBIDDEN_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_AUDIO_EXTS = {".opus", ".mp3", ".m4a", ".ogg", ".flac", ".wav"}

# Folder unter dem Library-Root den wir überspringen — User kann hier
# Falsch-Matches manuell quarantänen ohne dass das Script sie zweimal anpackt.
_SKIP_DIRS = {"_falsch-matched", "_sets", "_quarantine"}


# ──────────────────────────────────────────────────────────────────────
# Library-Walker
# ──────────────────────────────────────────────────────────────────────


def iter_library_audio(library_root: Path) -> Iterator[Path]:
    """Yield audio-files unter dem library-root, skipt Quarantäne-Folder."""
    for p in library_root.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in _AUDIO_EXTS:
            continue
        # Skip quarantäne-ähnliche Folder
        if any(part.lower() in _SKIP_DIRS for part in p.relative_to(library_root).parts):
            continue
        yield p


def parse_request_from_path(path: Path, library_root: Path) -> Optional[Tuple[str, str]]:
    """Parse Tonus' default path convention.

    Erwartete Struktur::

        /music/<Artist>/<Album>/<Track>.<ext>
        /music/<Artist>/<Track>.<ext>        (no album subdir — selten)

    Returns ``(artist, title)`` aus den Pfad-Komponenten. None wenn die
    Struktur nicht passt (z.B. Track im Library-Root, oder verschachtelt
    tiefer als 2 Ebenen).
    """
    try:
        rel = path.relative_to(library_root)
    except ValueError:
        return None
    parts = rel.parts
    # parts: [Artist, Album, Title.ext] oder [Artist, Title.ext]
    if len(parts) not in (2, 3):
        return None
    artist = parts[0].strip()
    title = path.stem.strip()
    if not artist or not title:
        return None
    return artist, title


def get_audio_duration(path: Path) -> float:
    """Read duration via mutagen. 0 bei Fehler."""
    try:
        mf = MutagenFile(str(path))
        return float(mf.info.length) if mf and getattr(mf, "info", None) else 0.0
    except Exception:
        return 0.0


# ──────────────────────────────────────────────────────────────────────
# YT-Search-Replay
# ──────────────────────────────────────────────────────────────────────


def yt_search_top_title(artist: str, title: str, ydl: "yt_dlp.YoutubeDL") -> Optional[str]:
    """Replay den Legacy-Fallback-Query und return den top-result-Title.

    Identische Query-Form wie ``youtube.py::search_and_download`` im
    Legacy-Fallback-Pfad (vor v0.3.1): ``<artist> <title> official audio``.
    """
    query = f"{artist} {title} official audio"
    try:
        info = ydl.extract_info(f"ytsearch1:{query}", download=False)
    except Exception as e:
        print(f"  WARN: ytsearch1 failed: {type(e).__name__}: {e}")
        return None
    if not info:
        return None
    entries = info.get("entries") or []
    if not entries:
        return None
    top = entries[0]
    if not top:
        return None
    return top.get("title")


def sanitize_filename_segment(s: str) -> str:
    cleaned = _FORBIDDEN_FILENAME_CHARS.sub("_", s)
    return cleaned.strip(" .")[:200]


def write_title_tags(audio_path: Path, new_title: str) -> bool:
    """Updated TITLE-Tag. Album-Tag bleibt unverändert — User kann den
    Album-Folder später manuell aufräumen wenn er konsistent benannt sein
    soll.
    """
    try:
        mf = MutagenFile(str(audio_path), easy=True)
        if mf is None:
            return False
        mf["title"] = [new_title]
        mf.save()
        return True
    except Exception as e:
        print(f"  ERROR: cannot write title tag on {audio_path}: {type(e).__name__}: {e}")
        return False


def is_meaningful_change(old_title: str, new_title: str) -> bool:
    """Heuristik: skip wenn der YT-Titel essentiell der gleiche Track ist.

    Beispiel: requested "Lose Yourself" → YT returnt "Lose Yourself (Soundtrack)".
    Das wäre kein Falsch-Match sondern ein echter Track mit Suffix. Wenn der
    requested-Title (case+space-normalized) vollständig im YT-Titel
    enthalten ist UND die Länge nicht dramatisch unterschiedlich ist
    → skip, das war wahrscheinlich legit.
    """
    norm = lambda s: re.sub(r"[^a-z0-9]+", "", s.lower())
    old_norm = norm(old_title)
    new_norm = norm(new_title)
    if old_norm in new_norm:
        # Length-Ratio check: wenn der neue Titel 2x so lang wäre wie
        # der alte oder mehr, ist es kein "nur Suffix" sondern wirklich
        # ein anderer Title (z.B. Festival-Set-Header).
        if len(new_norm) < 2 * len(old_norm):
            return False  # not meaningful, probably legit extended mix
    return True


# ──────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Recover real titles for falsch-matched long files via YT-reverse-search."
    )
    parser.add_argument(
        "--library",
        type=Path,
        required=True,
        help="Navidrome library root (z.B. /music im Container, /volume1/music auf Host)",
    )
    parser.add_argument(
        "--min-duration",
        type=int,
        default=DEFAULT_MIN_DURATION_S,
        help=f"Nur Files mit duration > N seconds anfassen (default: {DEFAULT_MIN_DURATION_S})",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Tatsächlich umbenennen + retaggen (default: dry-run)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Nur die ersten N Kandidaten verarbeiten (für Tests). 0 = unlimitiert.",
    )
    args = parser.parse_args()

    if not args.library.exists() or not args.library.is_dir():
        print(f"ERROR: library not found: {args.library}", file=sys.stderr)
        return 1

    dry_run = not args.apply

    # ── Step 1: Library walken + Kandidaten sammeln ──
    print(f"Scanning {args.library} for files > {args.min_duration}s …")
    candidates: List[Tuple[Path, str, str, float]] = []  # (path, artist, title, duration)
    for audio_path in iter_library_audio(args.library):
        duration = get_audio_duration(audio_path)
        if duration <= args.min_duration:
            continue
        parsed = parse_request_from_path(audio_path, args.library)
        if parsed is None:
            continue
        artist, title = parsed
        candidates.append((audio_path, artist, title, duration))

    print(f"  Found {len(candidates)} candidate file(s)")
    if not candidates:
        print("Nothing to do.")
        return 0

    if args.limit and args.limit < len(candidates):
        print(f"  Limiting to first {args.limit} for this run")
        candidates = candidates[: args.limit]

    # ── Step 2: YT-Search-Replay pro Kandidat ──
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "extract_flat": False,
    }

    rename_plan: List[Tuple[Path, Path, str, str]] = []  # (old, new, old_title, new_title)
    skipped_no_match = 0
    skipped_not_meaningful = 0

    print(f"\nReplaying YT-searches for {len(candidates)} candidate(s) …")
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        for audio_path, artist, title, duration in candidates:
            print(f"  • {artist} — {title}  ({duration:.0f}s)")
            actual_title = yt_search_top_title(artist, title, ydl)
            if not actual_title:
                print("    (no YT result)")
                skipped_no_match += 1
                continue
            if not is_meaningful_change(title, actual_title):
                print(f"    → {actual_title!r}  (skipped: similar to requested)")
                skipped_not_meaningful += 1
                continue
            new_filename = (
                f"{sanitize_filename_segment(artist)} - "
                f"{sanitize_filename_segment(actual_title)}{audio_path.suffix}"
            )
            new_path = audio_path.parent / new_filename
            if new_path == audio_path:
                continue
            rename_plan.append((audio_path, new_path, title, actual_title))
            print(f"    → {actual_title!r}")

    print()
    print(f"Summary: {len(rename_plan)} rename(s) planned, "
          f"{skipped_no_match} skipped (no YT result), "
          f"{skipped_not_meaningful} skipped (not meaningful change)")

    if not rename_plan:
        return 0

    # ── Step 3: Apply oder Dry-Run-Ausgabe ──
    print()
    if dry_run:
        print("=== DRY RUN — no changes will be made ===")
    else:
        print("=== APPLYING CHANGES ===")
    print()

    renamed_ok = 0
    failed = 0
    for old_path, new_path, old_title, new_title in rename_plan:
        rel_old = old_path.relative_to(args.library)
        rel_new = new_path.relative_to(args.library)
        size_mb = old_path.stat().st_size / (1024 * 1024)

        print(f"  {rel_old}  ({size_mb:.1f} MB)")
        print(f"  → {rel_new}")
        print(f"    title-tag: {old_title!r} → {new_title!r}")

        if not dry_run:
            if not write_title_tags(old_path, new_title):
                print("    ❌ tag-write failed, skipping rename")
                failed += 1
                print()
                continue
            if new_path.exists():
                print(f"    ❌ target exists, skipping rename: {new_path.name}")
                failed += 1
                print()
                continue
            try:
                old_path.rename(new_path)
                print("    ✓ renamed + retagged")
                renamed_ok += 1
            except OSError as e:
                print(f"    ❌ rename failed: {e}")
                failed += 1
        print()

    if dry_run:
        print(f"\n{len(rename_plan)} files would be renamed. Re-run with --apply to commit.")
    else:
        print(f"\n✓ {renamed_ok} renamed")
        if failed:
            print(f"❌ {failed} failed")
        print("Trigger a Navidrome library scan to refresh Subsonic-DB.")

    return 0 if failed == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
