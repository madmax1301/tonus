"""Discovery- und Sync-Pipelines.

Diese Helpers werden an drei Stellen genutzt:

1. CLI-Skript ``scripts/discover_via_artist_radio.py`` —
   "ListenBrainz Top-Artists → Deezer Artist Radio → Queue".
2. CLI-Skript ``scripts/sync_missing_tracks.py`` —
   "Lückenfüller aus N Quellen → Queue".
3. HTTP-Endpoint ``GET /api/plugin/library/missing`` (für das
   Navidrome-Plugin) — dieselbe Discovery-Logik, aber inproc.

Entwurfsgrundsätze:
- Reine Bibliotheks-Funktionen, kein argparse, kein print, kein sys.exit.
- Fehler kommen als leere Listen / None zurück (Skripte logging-en selbst).
- Keine Abhängigkeit auf FastAPI / DB — funktioniert auch in Standalone-Skript.
"""
from __future__ import annotations

import csv
import io
import time
from collections import Counter
from typing import Dict, Iterable, List, Optional, Set

import requests


LB_API = "https://api.listenbrainz.org/1"
DEEZER_BASE = "https://api.deezer.com"
MB_BASE = "https://musicbrainz.org/ws/2"
USER_AGENT = "tonus-discovery/1.0"


# ---------------------------------------------------------------------------
# ListenBrainz helpers
# ---------------------------------------------------------------------------


def _lb_range_from_days(days: int) -> str:
    if days <= 7:
        return "this_week"
    if days <= 31:
        return "this_month"
    if days <= 365:
        return "this_year"
    return "all_time"


def lb_top_artists(user: str, days: int, top_n: int) -> List[str]:
    """Top-N gehörte Artists aus LB-Statistics-API. Fallback über Listens-Counting."""
    rng = _lb_range_from_days(days)
    try:
        r = requests.get(
            f"{LB_API}/stats/user/{user}/artists",
            params={"range": rng, "count": top_n},
            timeout=20,
            headers={"User-Agent": USER_AGENT},
        )
        r.raise_for_status()
        block = (r.json().get("payload") or {}).get("artists") or []
        names = [a.get("artist_name") for a in block if a.get("artist_name")]
        if names:
            return names[:top_n]
    except Exception:
        pass
    return _lb_top_artists_via_listens(user, days, top_n)


def _lb_top_artists_via_listens(user: str, days: int, top_n: int) -> List[str]:
    cutoff = int(time.time() - days * 86400)
    counter: Counter = Counter()
    max_ts: Optional[int] = None
    for _ in range(20):  # max 2000 Listens
        params: Dict[str, int] = {"count": 100}
        if max_ts is not None:
            params["max_ts"] = max_ts
        try:
            r = requests.get(
                f"{LB_API}/user/{user}/listens",
                params=params,
                timeout=15,
                headers={"User-Agent": USER_AGENT},
            )
            if not r.ok:
                break
            listens = ((r.json().get("payload") or {}).get("listens")) or []
        except Exception:
            break
        if not listens:
            break
        for lst in listens:
            meta = lst.get("track_metadata") or {}
            artist = meta.get("artist_name")
            ts = lst.get("listened_at") or 0
            if artist and ts >= cutoff:
                counter[artist] += 1
        min_ts = min(l.get("listened_at", 0) for l in listens)
        if min_ts <= cutoff:
            break
        max_ts = min_ts - 1
    return [a for a, _ in counter.most_common(top_n)]


def lb_listened_track_keys(user: str, days: int, max_listens: int = 5000) -> Set[str]:
    """Skip-Liste 'artist|title' aller Listens der letzten <days> Tage (lowercase)."""
    cutoff = int(time.time() - days * 86400)
    keys: Set[str] = set()
    fetched = 0
    max_ts: Optional[int] = None
    while fetched < max_listens:
        params: Dict[str, int] = {"count": 100}
        if max_ts is not None:
            params["max_ts"] = max_ts
        try:
            r = requests.get(
                f"{LB_API}/user/{user}/listens",
                params=params,
                timeout=15,
                headers={"User-Agent": USER_AGENT},
            )
            if not r.ok:
                break
            listens = ((r.json().get("payload") or {}).get("listens")) or []
        except Exception:
            break
        if not listens:
            break
        for lst in listens:
            meta = lst.get("track_metadata") or {}
            a = (meta.get("artist_name") or "").strip().lower()
            t = (meta.get("track_name") or "").strip().lower()
            ts = lst.get("listened_at") or 0
            if a and t and ts >= cutoff:
                keys.add(f"{a}|{t}")
        fetched += len(listens)
        min_ts = min(l.get("listened_at", 0) for l in listens)
        if min_ts <= cutoff:
            break
        max_ts = min_ts - 1
    return keys


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


def lb_playlist_tracks(user: str, slug_or_mbid: str) -> List[Dict]:
    """Tracks einer LB-'createdfor'-Playlist (z.B. 'daily-jams')."""
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
        target = None
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
                target = pl
                break
        if not target:
            return out
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


def deezer_search_artist_id(artist_name: str) -> Optional[Dict]:
    try:
        r = requests.get(
            f"{DEEZER_BASE}/search/artist",
            params={"q": artist_name, "limit": 1},
            timeout=15,
            headers={"User-Agent": USER_AGENT},
        )
        r.raise_for_status()
        items = r.json().get("data") or []
        return items[0] if items else None
    except Exception:
        return None


def deezer_artist_radio(artist_id: str, limit: int) -> List[Dict]:
    try:
        r = requests.get(
            f"{DEEZER_BASE}/artist/{artist_id}/radio",
            params={"limit": limit},
            timeout=15,
            headers={"User-Agent": USER_AGENT},
        )
        r.raise_for_status()
        return r.json().get("data") or []
    except Exception:
        return []


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


def discover_via_artist_radio(
    *,
    listenbrainz_user: str,
    top_artists: int = 10,
    tracks_per_artist: int = 5,
    history_days: int = 90,
    max_total: int = 50,
    skip_listened: bool = True,
) -> List[Dict]:
    """LB-Top-Artists → Deezer-Artist-Radio → Filter History.

    Output: Liste von Items
        ``{"artist": str, "title": str, "deezer_track": dict}``

    deezer_track ist der vollständige Deezer-Track (mit ``id``, ``album``, …),
    sodass Caller direkt einen Download triggern können.
    """
    artists = lb_top_artists(listenbrainz_user, history_days, top_artists)
    if not artists:
        return []

    skip_keys: Set[str] = (
        lb_listened_track_keys(listenbrainz_user, history_days) if skip_listened else set()
    )

    out: List[Dict] = []
    for artist_name in artists:
        if len(out) >= max_total:
            break
        ainfo = deezer_search_artist_id(artist_name)
        if not ainfo:
            continue
        radio = deezer_artist_radio(str(ainfo["id"]), tracks_per_artist * 4)
        added = 0
        for track in radio:
            if added >= tracks_per_artist or len(out) >= max_total:
                break
            t_artist = (track.get("artist") or {}).get("name", "").strip().lower()
            t_title = (track.get("title", "")).strip().lower()
            if f"{t_artist}|{t_title}" in skip_keys:
                continue
            out.append(
                {
                    "artist": (track.get("artist") or {}).get("name", ""),
                    "title": track.get("title", ""),
                    "deezer_track": track,
                }
            )
            added += 1
    return out


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
