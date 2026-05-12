#!/usr/bin/env python3
"""Cleanup-Tool für Falsch-Matches die als "single Track" gespeichert wurden
aber eigentlich Festival-Sets / DJ-Mixes / komplette Alben sind.

Hintergrund (siehe v0.3.1 Release-Notes / Burn-in 2026-05-10):

    Wenn der MultiSourceResolver vor v0.3.1 keinen Kandidaten ≥ min_score
    fand, ist er auf legacy `ytsearch1` zurückgefallen. Bei populären
    Hardstyle/EDM-Artists liefert YouTube für `<Artist> <Track> official`
    aber als Top-Result oft das Defqon.1- oder Q-dance-Festival-Set des
    Artists — nicht den gesuchten Track. Diese Sets wurden dann als
    `<requested_artist> - <requested_title>.opus` in die Library gespeichert
    (mit den falschen Tags via mutagen) und sind dadurch unter dem falschen
    Namen "versteckt".

Zwei Betriebsmodi:

  **--logfile <path>**  (rename-Mode, von v0.3.1)
      Parsed ein Tonus-Container-Logfile auf ``YouTube result: 'X' by 'Y'`` +
      ``Looking for: 'T' by 'A' - Match: title=False`` und renamed die in
      der Library liegenden Falsch-Match-Files auf den echten YT-Titel.
      Funktioniert nur wenn die Container-Logs noch da sind.

  **--quarantine**  (quarantine-Mode, neu in v0.3.3)
      Walked die Library, findet Audio-Files > min-duration und verschiebt
      sie in einen Quarantäne-Ordner unter ``<library>/_falsch-matched/``.
      Funktioniert auch wenn die Logs verloren sind (z.B. nach
      ``docker compose pull && up -d`` Container-Recreate). User entscheidet
      später manuell pro File ob er es behalten oder löschen will.

Im --dry-run-Modus (default) wird nur ausgegeben was passieren würde, ohne
irgendwas anzufassen. Mit --apply werden Tags + Filename / Move geändert.

Was es in BEIDEN Modi NICHT tut:

  - Löscht keine Files (Quarantäne != Löschung, Rename != Löschung).
  - Triggert kein Re-Download. Die requested Tracks (z.B. "Sefa - 1527")
    bleiben gemissed; sie können später via /import erneut versucht werden.
  - Berührt keine Files unter der min-duration-Schwelle.

Usage::

    # Modus 1 — log-basiert (wenn die Logs noch da sind):
    docker logs tonus 2>&1 > /tmp/tonus.log
    python3 cleanup_set_mismatches.py \\
        --logfile /tmp/tonus.log \\
        --library /volume1/music \\
        --dry-run
    # … review, dann --apply

    # Modus 2 — Quarantäne (wenn die Logs weg sind):
    python3 cleanup_set_mismatches.py \\
        --quarantine \\
        --library /volume1/music \\
        --dry-run
    # … review, dann --apply

Abhängigkeit: ``mutagen`` (im tonus-Container schon installiert; auf dem
Host außerhalb ggf. `pip install mutagen`).
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

try:
    from mutagen import File as MutagenFile
except ImportError:
    print("ERROR: mutagen not installed. Run: pip install mutagen", file=sys.stderr)
    sys.exit(1)


# Default: 15min — alles drunter wird nicht angepackt (zu riskant für
# legit Extended Mixes etc.). Override via --min-duration.
DEFAULT_MIN_DURATION_S = 900

# Filename-sanitize: ersetze Zeichen die im Filesystem-Pfad Probleme machen
_FORBIDDEN_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# Audio-Formate die wir in der Library erwarten (mutagen-supported)
_AUDIO_EXTS = {".opus", ".mp3", ".m4a", ".ogg", ".flac", ".wav"}


# ──────────────────────────────────────────────────────────────────────
# Log-Parser
# ──────────────────────────────────────────────────────────────────────

# YouTube result: 'ACTUAL TITLE' by 'UPLOADER'
_RE_YT_RESULT = re.compile(r"^YouTube result: '([^']+)' by '([^']*)'\s*$")

# Looking for: 'TITLE' by 'ARTIST' - Match: title=False, artist=True
_RE_LOOKING_FOR = re.compile(
    r"^Looking for: '([^']+)' by '([^']+)' - Match: title=(True|False), artist=(True|False)\s*$"
)


def parse_log(logfile: Path) -> List[Dict[str, str]]:
    """Sammle alle Falsch-Matches (title=False) aus dem Log.

    Returns: liste von dicts mit keys
        - requested_artist
        - requested_title
        - actual_yt_title
        - actual_yt_uploader
    """
    falsch_matches: List[Dict[str, str]] = []
    last_yt_result: Optional[Tuple[str, str]] = None

    with logfile.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m_yt = _RE_YT_RESULT.match(line.rstrip())
            if m_yt:
                last_yt_result = (m_yt.group(1), m_yt.group(2))
                continue

            m_lf = _RE_LOOKING_FOR.match(line.rstrip())
            if m_lf and last_yt_result is not None:
                title_match = m_lf.group(3) == "True"
                if not title_match:
                    falsch_matches.append({
                        "requested_title": m_lf.group(1),
                        "requested_artist": m_lf.group(2),
                        "actual_yt_title": last_yt_result[0],
                        "actual_yt_uploader": last_yt_result[1],
                    })
                last_yt_result = None  # consume

    return falsch_matches


# ──────────────────────────────────────────────────────────────────────
# Library-Walker + Tag-Lookup
# ──────────────────────────────────────────────────────────────────────


def iter_library_audio(library_root: Path) -> Iterator[Path]:
    """Yield audio-files unter dem library-root."""
    for p in library_root.rglob("*"):
        if p.is_file() and p.suffix.lower() in _AUDIO_EXTS:
            yield p


def read_tags(audio_path: Path) -> Optional[Dict[str, object]]:
    """Liest ARTIST + TITLE + DURATION via mutagen. None bei Fehler."""
    try:
        mf = MutagenFile(str(audio_path), easy=True)
        if mf is None:
            return None
        artist = (mf.get("artist") or [""])[0]
        title = (mf.get("title") or [""])[0]
        duration = float(mf.info.length) if getattr(mf, "info", None) else 0.0
        return {"artist": artist, "title": title, "duration": duration}
    except Exception as e:
        print(f"  WARN: cannot read tags from {audio_path}: {type(e).__name__}: {e}")
        return None


def write_title_tag(audio_path: Path, new_title: str) -> bool:
    """Updated nur das TITLE-Tag, ARTIST + ALBUM bleiben unverändert.

    Returns True bei Erfolg, False sonst.
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


def sanitize_filename_segment(s: str) -> str:
    """Entferne forbidden Zeichen für plattformsicheren Filenamen."""
    cleaned = _FORBIDDEN_FILENAME_CHARS.sub("_", s)
    return cleaned.strip(" .")[:200]  # cap at 200 chars


# ──────────────────────────────────────────────────────────────────────
# Quarantine-Mode (v0.3.3) — log-loser Pfad
# ──────────────────────────────────────────────────────────────────────


# Name des Quarantäne-Subfolders unter dem Library-Root. Wird beim Scan
# automatisch übersprungen, damit Re-Runs idempotent sind.
QUARANTINE_DIRNAME = "_falsch-matched"


def build_quarantine_target(audio_path: Path, library_root: Path) -> Path:
    """Zielpfad in der Quarantäne. Flach (kein Subfolder pro Artist) damit
    der User auf einen Blick die Quarantäne-Liste sieht. Doppel-Underscore-
    Separator behält die Original-Pfad-Info: ``<Artist>__<Album>__<File>``.
    """
    quarantine_root = library_root / QUARANTINE_DIRNAME
    try:
        rel = audio_path.relative_to(library_root)
    except ValueError:
        # Sollte nicht passieren, aber als safety fallback nimm nur den Filename
        rel = Path(audio_path.name)
    parent_chain = "__".join(rel.parts[:-1]) if len(rel.parts) > 1 else ""
    fname = rel.parts[-1]
    target_name = f"{parent_chain}__{fname}" if parent_chain else fname
    return quarantine_root / target_name


def run_quarantine_mode(args: argparse.Namespace, dry_run: bool) -> int:
    """Walked die Library, findet Files > min-duration, plant Quarantäne-Move."""
    quarantine_root = args.library / QUARANTINE_DIRNAME

    print(f"Scanning {args.library} for files > {args.min_duration}s …")
    plan: List[Tuple[Path, Path, float]] = []  # (src, dst, duration)
    scanned = 0
    for audio_path in iter_library_audio(args.library):
        # Skip Files die bereits in der Quarantäne liegen
        try:
            rel = audio_path.relative_to(args.library)
            if rel.parts and rel.parts[0] == QUARANTINE_DIRNAME:
                continue
        except ValueError:
            continue
        scanned += 1
        tags = read_tags(audio_path)
        if tags is None:
            continue
        duration = float(tags["duration"])
        if duration <= args.min_duration:
            continue
        target = build_quarantine_target(audio_path, args.library)
        plan.append((audio_path, target, duration))

    print(f"  Scanned {scanned} files")
    print(f"  Quarantine candidates: {len(plan)}")
    if not plan:
        print("Nothing to do.")
        return 0

    print()
    if dry_run:
        print("=== DRY RUN — no changes will be made ===")
    else:
        print("=== APPLYING QUARANTINE MOVES ===")
    print()

    if not dry_run:
        quarantine_root.mkdir(parents=True, exist_ok=True)

    moved_ok = 0
    failed = 0
    for src, dst, duration in plan:
        rel_src = src.relative_to(args.library)
        size_mb = src.stat().st_size / (1024 * 1024) if src.exists() else 0

        print(f"  {rel_src}  ({size_mb:.1f} MB, {duration:.0f}s)")
        print(f"  → {QUARANTINE_DIRNAME}/{dst.name}")

        if not dry_run:
            if dst.exists():
                print(f"    ❌ target exists, skipping: {dst.name}")
                failed += 1
                print()
                continue
            try:
                src.rename(dst)
                print("    ✓ moved")
                moved_ok += 1
            except OSError as e:
                print(f"    ❌ move failed: {e}")
                failed += 1
        print()

    if dry_run:
        print(f"\n{len(plan)} files would be moved. Re-run with --apply to commit.")
    else:
        print(f"\n✓ {moved_ok} moved to {QUARANTINE_DIRNAME}/")
        if failed:
            print(f"❌ {failed} failed")
        print(
            "\nNext steps:\n"
            "  1. find /music -type d -empty -not -path '*/" + QUARANTINE_DIRNAME + "/*' -delete  "
            "(empty album/artist folders aufräumen, ggf. 2x)\n"
            "  2. Navidrome-Library-Scan triggern damit Subsonic-DB die "
            "neuen Files vergisst\n"
            "  3. " + QUARANTINE_DIRNAME + "/ manuell durchgehen — Files behalten, "
            "umbenennen oder löschen pro Case"
        )

    return 0 if failed == 0 else 2


# ──────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Clean up falsch-matched Festival-Sets either by renaming "
                    "(--logfile) or by quarantining them (--quarantine)."
    )
    # Modi sind mutually exclusive — entweder log-basiertes Rename ODER
    # log-loser Quarantine-Move, nicht beides.
    mode_group = parser.add_mutually_exclusive_group(required=True)
    mode_group.add_argument(
        "--logfile",
        type=Path,
        help="Modus 1 (Rename): Pfad zu docker logs output (z.B. /tmp/tonus.log "
             "oder /dev/stdin via pipe). Files werden auf den echten YT-Title "
             "umbenannt + retagged.",
    )
    mode_group.add_argument(
        "--quarantine",
        action="store_true",
        help="Modus 2 (Quarantine): walked Library, verschiebt Files > "
             "min-duration nach <library>/_falsch-matched/. Funktioniert "
             "ohne Logs.",
    )
    parser.add_argument(
        "--library",
        type=Path,
        required=True,
        help="Root of Navidrome library (e.g., /volume1/music)",
    )
    parser.add_argument(
        "--min-duration",
        type=int,
        default=DEFAULT_MIN_DURATION_S,
        help=f"Only touch files with duration > N seconds (default: {DEFAULT_MIN_DURATION_S})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Print actions, don't apply (default ON for safety)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually rename + retag files / move to quarantine "
             "(overrides --dry-run)",
    )
    args = parser.parse_args()

    if not args.library.exists() or not args.library.is_dir():
        print(f"ERROR: library directory not found: {args.library}", file=sys.stderr)
        return 1

    dry_run = not args.apply

    # ── Quarantäne-Mode: log-loser Pfad ──
    if args.quarantine:
        return run_quarantine_mode(args, dry_run)

    # ── Log-Mode: existing rename flow ──
    if not args.logfile.exists():
        print(f"ERROR: logfile not found: {args.logfile}", file=sys.stderr)
        return 1

    # ── Step 1: Parse log → falsch-match-Liste ──
    print(f"Parsing {args.logfile} …")
    matches = parse_log(args.logfile)
    print(f"  Found {len(matches)} falsch-matches (title=False) in log")
    if not matches:
        print("Nothing to do.")
        return 0

    # Build lookup: (artist_lc, title_lc) → list of actual_yt_titles
    # (kann theoretisch mehrere Versuche pro Track geben — wir nehmen das
    # erste was wir finden und renamen den Library-File entsprechend)
    by_request: Dict[Tuple[str, str], str] = {}
    for m in matches:
        key = (m["requested_artist"].lower().strip(), m["requested_title"].lower().strip())
        if key not in by_request:  # first-write-wins
            by_request[key] = m["actual_yt_title"]

    print(f"  Unique requests: {len(by_request)}")

    # ── Step 2: Walk Library + Match gegen Tags ──
    print(f"\nScanning {args.library} for audio files …")
    rename_plan: List[Tuple[Path, Path, str, str]] = []  # (old_path, new_path, old_title, new_title)
    skipped_short = 0
    scanned = 0
    for audio_path in iter_library_audio(args.library):
        scanned += 1
        tags = read_tags(audio_path)
        if tags is None:
            continue
        artist_lc = str(tags["artist"]).lower().strip()
        title_lc = str(tags["title"]).lower().strip()
        duration = float(tags["duration"])

        actual_yt_title = by_request.get((artist_lc, title_lc))
        if not actual_yt_title:
            continue  # not a falsch-match

        if duration <= args.min_duration:
            skipped_short += 1
            continue  # too short to be a Festival-Set

        # Plan: new filename = "<Artist> - <ActualTitle><ext>"
        new_title = actual_yt_title.strip()
        artist_segment = sanitize_filename_segment(str(tags["artist"]))
        title_segment = sanitize_filename_segment(new_title)
        new_filename = f"{artist_segment} - {title_segment}{audio_path.suffix}"
        new_path = audio_path.parent / new_filename

        if new_path == audio_path:
            continue  # already correctly named

        rename_plan.append((audio_path, new_path, str(tags["title"]), new_title))

    print(f"  Scanned {scanned} files")
    print(f"  Skipped {skipped_short} short candidates (≤ {args.min_duration}s)")
    print(f"  Renames planned: {len(rename_plan)}")

    if not rename_plan:
        print("\nNothing to rename.")
        return 0

    # ── Step 3: Dry-run-Ausgabe oder Apply ──
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
        size_mb = old_path.stat().st_size / (1024 * 1024) if old_path.exists() else 0

        print(f"  {rel_old}  ({size_mb:.1f} MB)")
        print(f"  → {rel_new}")
        print(f"    title-tag: {old_title!r}  →  {new_title!r}")

        if not dry_run:
            # Title-Tag first, dann Rename. Wenn Tag-Write failed, abort
            # für dieses File ohne Rename — bessere Konsistenz.
            tag_ok = write_title_tag(old_path, new_title)
            if not tag_ok:
                print("    ❌ tag-write failed, skipping rename")
                failed += 1
                print()
                continue
            try:
                if new_path.exists():
                    print(f"    ❌ target exists, skipping rename: {new_path.name}")
                    failed += 1
                    print()
                    continue
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
