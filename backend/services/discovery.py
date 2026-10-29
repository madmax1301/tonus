"""Discovery- und Sync-Pipelines.

Diese Helpers werden an zwei Stellen genutzt:

1. CLI-Skript ``scripts/sync_missing_tracks.py`` —
   "Lückenfüller aus N Quellen → Queue".
2. HTTP-Endpoints (Navidrome-Plugin) — Genre-Mix, LB-Weekly.

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

        # createdfor liefert nur Playlist-Metadaten — das `track`-Array ist
        # dort leer. Die eigentlichen Tracks stehen erst im Einzel-Playlist-
        # Endpoint /1/playlist/<mbid>. MBID aus `identifier` extrahieren
        # (z.B. "https://listenbrainz.org/playlist/<uuid>") und nachladen.
        tracks = target.get("track") or []
        if not tracks:
            import re as _re
            ident = target.get("identifier", "") or ""
            m = _re.search(
                r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
                ident,
            )
            if m:
                pr = requests.get(
                    f"{LB_API}/playlist/{m.group(0)}",
                    timeout=20,
                    headers={"User-Agent": USER_AGENT},
                )
                if pr.ok:
                    tracks = ((pr.json().get("playlist") or {}).get("track")) or []

        for t in tracks:
            artist = t.get("creator", "")
            title = t.get("title", "")
            if artist and title:
                out.append({"artist": artist, "title": title})
    except Exception:
        pass
    return out


# Release-Gruppen-Sekundärtypen, die nicht als "neue Musik" zählen.
_FRESH_SKIP_SECONDARY = {"Compilation", "Live", "DJ-mix", "Mixtape/Street"}


def lb_fresh_releases(
    user: str, days: int = 7, token: Optional[str] = None
) -> List[Dict]:
    """Persönliche LB-Fresh-Releases (bereits erschienen, letzte `days` Tage),
    nach confidence absteigend. Token ist optional — wird mitgeschickt, wenn
    gesetzt."""
    headers = {"User-Agent": USER_AGENT}
    if token:
        headers["Authorization"] = f"Token {token}"
    try:
        r = requests.get(
            f"{LB_API}/user/{user}/fresh_releases",
            params={"days": max(1, min(int(days), 90)), "past": "true",
                    "future": "false", "sort": "confidence"},
            timeout=20,
            headers=headers,
        )
        if not r.ok:
            return []
        data = r.json()
    except Exception:
        return []
    if isinstance(data, dict):
        payload = data.get("payload", data)
        releases = payload.get("releases") if isinstance(payload, dict) else payload
    else:
        releases = data
    out = [
        rel for rel in (releases or [])
        if isinstance(rel, dict)
        and rel.get("artist_credit_name") and rel.get("release_name")
        and rel.get("release_group_secondary_type") not in _FRESH_SKIP_SECONDARY
    ]
    out.sort(key=lambda rel: (rel.get("confidence") or 0,
                              rel.get("release_date") or ""), reverse=True)
    return out


def deezer_album_top_tracks(artist: str, album: str, limit: int = 2) -> List[Dict]:
    """Sucht ein Album bei Deezer und liefert dessen `limit` populärste Tracks
    (nach Deezer-rank) als {artist, title}. Leer, wenn kein Treffer."""
    try:
        r = requests.get(
            f"{DEEZER_BASE}/search/album",
            params={"q": f'artist:"{artist}" album:"{album}"', "limit": 1},
            timeout=15,
            headers={"User-Agent": USER_AGENT},
        )
        r.raise_for_status()
        hits = r.json().get("data") or []
        if not hits:
            return []
        tr = requests.get(
            f"{DEEZER_BASE}/album/{hits[0]['id']}/tracks",
            params={"limit": 100},
            timeout=15,
            headers={"User-Agent": USER_AGENT},
        )
        tr.raise_for_status()
        tracks = tr.json().get("data") or []
    except Exception:
        return []
    tracks.sort(key=lambda t: t.get("rank") or 0, reverse=True)
    out: List[Dict] = []
    for t in tracks[:limit]:
        title = t.get("title", "")
        t_artist = (t.get("artist") or {}).get("name") or artist
        if title:
            out.append({"artist": t_artist, "title": title})
    return out


_FRESH_CACHE: Dict[tuple, tuple] = {}
_FRESH_CACHE_TTL_S = 3600


def lb_fresh_release_tracks(
    user: str,
    days: int = 7,
    token: Optional[str] = None,
    max_releases: int = 25,
    tracks_per_release: int = 2,
) -> List[Dict]:
    """Fresh Releases eines LB-Users als Trackliste ({artist, title}): pro
    Release die populärsten `tracks_per_release` Tracks laut Deezer.

    Kurzer Inproc-Cache, weil der Plugin-Call das Ergebnis zweimal braucht
    (synchroner Library-Check + Background-Queueing) und jede Auflösung
    zwei Deezer-Calls pro Release kostet."""
    import time as _time
    key = (user, days, max_releases, tracks_per_release)
    hit = _FRESH_CACHE.get(key)
    if hit and _time.time() - hit[0] < _FRESH_CACHE_TTL_S:
        return list(hit[1])

    out: List[Dict] = []
    seen = set()
    for rel in lb_fresh_releases(user, days, token)[:max_releases]:
        for t in deezer_album_top_tracks(
            rel["artist_credit_name"], rel["release_name"], tracks_per_release
        ):
            k = (t["artist"].lower(), t["title"].lower())
            if k not in seen:
                seen.add(k)
                out.append(t)
    if out:
        _FRESH_CACHE[key] = (_time.time(), out)
    return list(out)


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
