"""Discovery- und Sync-Pipelines.

Diese Helpers werden an zwei Stellen genutzt:

1. CLI-Skript ``scripts/sync_missing_tracks.py`` —
   "Lückenfüller aus N Quellen → Queue".
2. HTTP-Endpoints (Navidrome-Plugin) — Genre-Mix, LB-Weekly, Library-Missing.

Entwurfsgrundsätze:
- Reine Bibliotheks-Funktionen, kein argparse, kein print, kein sys.exit.
- Fehler kommen als leere Listen / None zurück (Skripte logging-en selbst).
- Keine Abhängigkeit auf FastAPI / DB — funktioniert auch in Standalone-Skript.
"""
from __future__ import annotations

import csv
import io
from typing import Dict, Iterable, List, Optional

import requests


LB_API = "https://api.listenbrainz.org/1"
DEEZER_BASE = "https://api.deezer.com"
MB_BASE = "https://musicbrainz.org/ws/2"
USER_AGENT = "tonus-discovery/1.0"


# ---------------------------------------------------------------------------
# ListenBrainz helpers
# ---------------------------------------------------------------------------


def lb_recommendations(user: str, count: int = 100) -> List[Dict]:
    """LB Collaborative-Filter-Empfehlungen, MBIDs aufgelöst zu artist+title."""
    out: List[Dict] = []
    try:
        r = requests.get(
            f"{LB_API}/cf/recommendation/user/{user}/recording",
            params={"count": count},
            timeout=20,
            headers={"User-Agent": USER_AGENT},
        )
        if not r.ok or not r.text.strip():
            return out
        try:
            data = r.json()
        except ValueError:
            return out
        recs = ((data.get("payload") or {}).get("mbids")) or []
        for entry in recs:
            mbid = entry.get("recording_mbid")
            if not mbid:
                continue
            meta = mbid_to_meta(mbid)
            if meta:
                out.append({"artist": meta["artist"], "title": meta["title"], "mbid": mbid})
    except Exception:
        pass
    return out


def lb_genre_top_recordings(genre: str, count: int = 50) -> List[Dict]:
    """Top-Recordings eines Genres aus ListenBrainz Charts.

    Verwendet den `popular/release-groups`-Endpoint mit Genre-Filter, holt
    dann pro Release die Tracklist via MB. Der Genre-String muss ein
    LB/MB-Tag sein (z.B. 'metalcore', 'hip-hop', 'shoegaze').

    Output: Liste von ``{"artist": str, "title": str, "mbid": str}``.
    Leer falls Genre unbekannt oder LB API down.
    """
    out: List[Dict] = []
    try:
        r = requests.get(
            f"{LB_API}/popular/release-groups",
            params={"genre": genre, "count": min(count, 100)},
            timeout=20,
            headers={"User-Agent": USER_AGENT},
        )
        if not r.ok or not r.text.strip():
            return out
        try:
            data = r.json()
        except ValueError:
            return out
        rgs = ((data.get("payload") or {}).get("release_groups")) or []
        # Pro Release-Group den ersten Recording als "repräsentativen" Track
        # nehmen — vermeidet, dass eine Library mit dem gleichen Album
        # mehrfach matched. Wer mehr Tiefe will, kann pro RG mehr Recordings
        # ausweiten (kostet aber MB-Lookups).
        for rg in rgs:
            artist = (rg.get("artist_credit_name") or "").strip()
            title = (rg.get("release_group_name") or "").strip()
            mbid = rg.get("release_group_mbid") or ""
            if artist and title:
                out.append({"artist": artist, "title": title, "mbid": mbid})
            if len(out) >= count:
                break
    except Exception:
        pass
    return out


def lb_playlist_tracks(user: str, slug_or_mbid: str, occurrence: int = 0) -> List[Dict]:
    """Tracks einer LB-'createdfor'-Playlist (z.B. 'weekly-exploration').

    occurrence=0 → neueste Version des matchenden source_patch,
    occurrence=1 → zweitneueste (Vorwoche, 'Last Week's …').
    Leere Liste, wenn die gewünschte occurrence nicht existiert.
    """
    out: List[Dict] = []
    try:
        r = requests.get(
            f"{LB_API}/user/{user}/playlists/createdfor",
            timeout=20,
            headers={"User-Agent": USER_AGENT},
        )
        if not r.ok:
            return out
        playlists = (r.json().get("playlists") or [])
        # Alle Playlists mit passendem source_patch sammeln, nach date desc sortieren.
        matches = []
        for p in playlists:
            pl = p.get("playlist") or {}
            ext = pl.get("extension", {}).get(
                "https://musicbrainz.org/doc/jspf#playlist", {}
            )
            algo = (
                (ext.get("additional_metadata", {}) or {})
                .get("algorithm_metadata", {})
                .get("source_patch", "")
            )
            if slug_or_mbid in algo or slug_or_mbid in pl.get("identifier", ""):
                matches.append(pl)
        if len(matches) <= occurrence:
            return out
        matches.sort(key=lambda pl: pl.get("date", ""), reverse=True)
        target = matches[occurrence]
        for t in target.get("track") or []:
            artist = t.get("creator", "")
            title = t.get("title", "")
            if artist and title:
                out.append({"artist": artist, "title": title})
    except Exception:
        pass
    return out


# ---------------------------------------------------------------------------
# MusicBrainz MBID lookup
# ---------------------------------------------------------------------------


_MB_CACHE: Dict[str, Dict] = {}


def mbid_to_meta(mbid: str) -> Optional[Dict]:
    """recording_mbid → {artist, title}. Kleiner Inproc-Cache verhindert MB-Rate-Limit."""
    if mbid in _MB_CACHE:
        return _MB_CACHE[mbid]
    try:
        r = requests.get(
            f"{MB_BASE}/recording/{mbid}",
            params={"inc": "artists", "fmt": "json"},
            timeout=15,
            headers={"User-Agent": USER_AGENT},
        )
        if not r.ok:
            return None
        data = r.json()
        title = data.get("title", "")
        ac = data.get("artist-credit") or []
        artist = " ".join(c.get("name", "") for c in ac).strip() if ac else ""
        if not artist or not title:
            return None
        meta = {"artist": artist, "title": title}
        _MB_CACHE[mbid] = meta
        return meta
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Deezer helpers
# ---------------------------------------------------------------------------


def deezer_search_track(artist: str, title: str) -> Optional[Dict]:
    try:
        r = requests.get(
            f"{DEEZER_BASE}/search/track",
            params={"q": f"{artist} {title}", "limit": 1},
            timeout=15,
            headers={"User-Agent": USER_AGENT},
        )
        r.raise_for_status()
        items = r.json().get("data") or []
        return items[0] if items else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Subsonic / Navidrome playlist
# ---------------------------------------------------------------------------


def navidrome_playlist_missing_tracks(
    playlist_id: str, base_url: str, user: str, password: str
) -> List[Dict]:
    """Liest eine Navidrome-Playlist via Subsonic-API. Tracks mit '[MISSING]'-Marker
    im Titel werden als 'wanted' (artist+title) zurückgegeben."""
    out: List[Dict] = []
    params = {
        "u": user,
        "p": password,
        "v": "1.16.1",
        "c": "tonus-sync",
        "f": "json",
        "id": playlist_id,
    }
    try:
        r = requests.get(
            f"{base_url.rstrip('/')}/rest/getPlaylist.view",
            params=params,
            timeout=20,
            headers={"User-Agent": USER_AGENT},
        )
        r.raise_for_status()
        data = r.json().get("subsonic-response", {})
        if data.get("status") != "ok":
            return out
        pl = data.get("playlist") or {}
        for entry in (pl.get("entry") or []):
            t = entry.get("title", "")
            a = entry.get("artist", "")
            if "[MISSING]" in t or t.startswith("MISSING:"):
                clean = t.replace("[MISSING]", "").replace("MISSING:", "").strip()
                if clean and a:
                    out.append({"artist": a, "title": clean})
    except Exception:
        pass
    return out


# ---------------------------------------------------------------------------
# CSV-/Text-File-Reader
# ---------------------------------------------------------------------------


def read_text_file_tracks(path: str) -> List[Dict]:
    """Liest 'artist;title' (oder ',', '\\t') aus einer Text-Datei.
    Header-Auto-Detect; tolerant gegenüber Anführungszeichen."""
    out: List[Dict] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except Exception:
        return out

    best_rows: List[List[str]] = []
    best_cols = 0
    for delim in (";", ",", "\t"):
        try:
            cand = list(csv.reader(io.StringIO(text), delimiter=delim))
            cols = max((len(r) for r in cand[:5]), default=0)
            if cols > best_cols:
                best_cols = cols
                best_rows = cand
        except Exception:
            continue

    col_artist, col_title = 0, 1
    if best_rows:
        first = [c.strip().lower() for c in best_rows[0]]
        if any("artist" in c for c in first) and any(
            "title" in c or "track" in c for c in first
        ):
            col_artist = next((i for i, c in enumerate(first) if "artist" in c), 0)
            col_title = next(
                (i for i, c in enumerate(first) if "title" in c or "track" in c), 1
            )
            best_rows = best_rows[1:]

    for row in best_rows:
        if not row:
            continue
        try:
            a = row[col_artist].strip().strip('"').strip("'")
            t = row[col_title].strip().strip('"').strip("'")
        except IndexError:
            continue
        if a and t:
            out.append({"artist": a, "title": t})
    return out


# ---------------------------------------------------------------------------
# High-Level Pipelines
# ---------------------------------------------------------------------------


def collect_wanted_tracks(
    *,
    source: str,
    listenbrainz_user: Optional[str] = None,
    listenbrainz_slug: Optional[str] = None,
    file_path: Optional[str] = None,
    navidrome_playlist_id: Optional[str] = None,
    navidrome_url: Optional[str] = None,
    navidrome_user: Optional[str] = None,
    navidrome_password: Optional[str] = None,
) -> List[Dict]:
    """Dispatcher für Lückenfüller-Quellen.

    source ∈ {"listenbrainz-recs", "listenbrainz-playlist", "file", "navidrome-playlist"}
    Output: Liste von ``{"artist", "title", "mbid"?}``.
    """
    if source == "listenbrainz-recs":
        if not listenbrainz_user:
            return []
        return lb_recommendations(listenbrainz_user)
    if source == "listenbrainz-playlist":
        if not listenbrainz_user or not listenbrainz_slug:
            return []
        return lb_playlist_tracks(listenbrainz_user, listenbrainz_slug)
    if source == "file":
        if not file_path:
            return []
        return read_text_file_tracks(file_path)
    if source == "navidrome-playlist":
        if not all(
            [navidrome_playlist_id, navidrome_url, navidrome_user, navidrome_password]
        ):
            return []
        return navidrome_playlist_missing_tracks(
            navidrome_playlist_id,  # type: ignore[arg-type]
            navidrome_url,  # type: ignore[arg-type]
            navidrome_user,  # type: ignore[arg-type]
            navidrome_password,  # type: ignore[arg-type]
        )
    return []
