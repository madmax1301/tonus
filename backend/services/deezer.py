"""Deezer public API — no API key required for catalog search."""
import os
import sys
import threading
from typing import Dict, List, Optional

import requests
from requests.adapters import HTTPAdapter

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BASE = "https://api.deezer.com"

# ---------------------------------------------------------------------------
# Dual-VPN-Splitting: Source-IP-Bind Layer
# ---------------------------------------------------------------------------
# Wenn VPN_SPLIT_ENABLED=true ist, kann jeder API-Call gezielt über eine von
# zwei Source-IPs (= zwei NAS-Ethernet-Ports = zwei VPN-Tunnel am Router)
# rausgehen. Lane "a" → VPN_SOURCE_A, Lane "b" → VPN_SOURCE_B, Lane "default"
# → System-Routing ohne Bind. Bei deaktiviertem Splitting liefern alle Lanes
# dieselbe Default-Session — Verhalten dann bit-identisch zum Status quo.
# ---------------------------------------------------------------------------

_VPN_SPLIT_ENABLED = os.environ.get("VPN_SPLIT_ENABLED", "").strip().lower() == "true"
_VPN_SOURCE_A = os.environ.get("VPN_SOURCE_A", "").strip() or None
_VPN_SOURCE_B = os.environ.get("VPN_SOURCE_B", "").strip() or None

_session_lock = threading.Lock()
_sessions: Dict[str, "requests.Session"] = {}


class _SourceAddressAdapter(HTTPAdapter):
    """HTTPAdapter that pins outbound connections to a fixed source IP.

    Reicht source_address an den urllib3-PoolManager durch; der ruft am Ende
    socket.bind((ip, 0)) vor dem Connect. Damit landet der Egress-Traffic auf
    dem Host-Interface mit dieser IP — der Router routet dann per Policy.
    """

    def __init__(self, source_ip: str, **kwargs) -> None:
        self._source_ip = source_ip
        super().__init__(**kwargs)

    def init_poolmanager(self, *args, **kwargs):
        kwargs["source_address"] = (self._source_ip, 0)
        return super().init_poolmanager(*args, **kwargs)


def _get_session(source: str = "default") -> "requests.Session":
    """Lazy-init pro Lane. Thread-safe via _session_lock (double-checked)."""
    if not _VPN_SPLIT_ENABLED or source not in ("a", "b"):
        source = "default"

    sess = _sessions.get(source)
    if sess is not None:
        return sess

    with _session_lock:
        sess = _sessions.get(source)
        if sess is not None:
            return sess
        sess = requests.Session()
        bind_ip = _VPN_SOURCE_A if source == "a" else (_VPN_SOURCE_B if source == "b" else None)
        if bind_ip:
            adapter = _SourceAddressAdapter(bind_ip)
            sess.mount("https://", adapter)
            sess.mount("http://", adapter)
        _sessions[source] = sess
        return sess


def _get(path: str, params: Optional[dict] = None, source: str = "default") -> dict:
    url = f"{BASE}{path}" if path.startswith("/") else f"{BASE}/{path}"
    r = _get_session(source).get(url, params=params or {}, timeout=15)
    r.raise_for_status()
    data = r.json()
    if isinstance(data, dict) and data.get("error"):
        err = data["error"]
        raise RuntimeError(err.get("message", str(err)))
    return data


def _track_from_api(t: dict) -> Dict:
    artist = t.get("artist") or {}
    album = t.get("album") or {}
    artists = [artist["name"]] if artist.get("name") else []
    dur = int(t.get("duration") or 0) * 1000
    cover = album.get("cover_xl") or album.get("cover_big") or album.get("cover_medium")
    return {
        "id": str(t.get("id", "")),
        "name": t.get("title", "Unknown"),
        "artists": artists,
        "artist": artist.get("name", "Unknown"),
        "album": album.get("title", "Unknown"),
        "album_id": str(album.get("id", "")),
        "duration_ms": dur,
        "external_url": t.get("link", ""),
        "preview_url": t.get("preview"),
        "album_art": cover,
        "release_date": (t.get("release_date") or "")[:10],
        # Genres werden später durch DeezerService._enrich_with_genres() befüllt — leer
        # bleibt als Default, damit metadata.py silently skippt wenn die API nichts liefert.
        "genres": [],
    }


class DeezerService:
    def __init__(self):
        # Album-Genre-Cache: {album_id: [genre1, genre2, ...]}.
        # Spart pro Track 1 HTTP-Call wenn mehrere Tracks dasselbe Album teilen
        # (z.B. CSV-Import von ganzen Alben oder Album-Downloads).
        self._album_genre_cache: Dict[str, List[str]] = {}

    def _get_album_genres(self, album_id: str, source: str = "default") -> List[str]:
        """Holt Genre-Liste eines Albums per /album/{id} mit Cache."""
        if not album_id:
            return []
        cached = self._album_genre_cache.get(album_id)
        if cached is not None:
            return cached
        try:
            album = _get(f"/album/{album_id}", source=source)
            genres_block = (album.get("genres") or {}).get("data") or []
            names = [g.get("name", "").strip() for g in genres_block if g.get("name")]
            # Deduplizieren, Reihenfolge erhalten
            seen = set()
            result = [g for g in names if not (g in seen or seen.add(g))]
        except Exception as e:
            print(f"Deezer album genre lookup error ({album_id}): {e}")
            result = []
        self._album_genre_cache[album_id] = result
        return result

    def _enrich_with_genres(self, track: Dict, source: str = "default") -> Dict:
        """Hängt genres-Liste an einen Track-Dict an (in-place, returnt das Dict)."""
        if track and track.get("album_id"):
            track["genres"] = self._get_album_genres(track["album_id"], source=source)
        return track

    def search_tracks(self, query: str, limit: int = 20, source: str = "default") -> List[Dict]:
        limit = max(1, min(int(limit), 100))
        data = _get("/search/track", {"q": query, "limit": limit}, source=source)
        items = data.get("data") or []
        # Bei Search KEIN Genre-Lookup — würde N HTTP-Calls pro Suche bedeuten.
        # Genres werden erst beim get_track_details (für den eigentlichen Download) angereichert.
        return [_track_from_api(t) for t in items]

    def get_track_details(self, track_id: str, source: str = "default") -> Optional[Dict]:
        try:
            t = _get(f"/track/{track_id}", source=source)
        except Exception as e:
            print(f"Deezer track lookup error: {e}")
            return None
        if not t or not t.get("id"):
            return None
        out = _track_from_api(t)
        out["album_artist"] = out["artist"]
        out["album_artists"] = list(out["artists"])
        out["track_number"] = int(t.get("track_position") or 1)
        self._enrich_with_genres(out, source=source)
        return out

    def search_albums(self, query: str, limit: int = 20) -> List[Dict]:
        limit = max(1, min(int(limit), 100))
        data = _get("/search/album", {"q": query, "limit": limit})
        items = data.get("data") or []
        albums = []
        for a in items:
            artist = a.get("artist") or {}
            artists = [artist["name"]] if artist.get("name") else []
            cover = a.get("cover_xl") or a.get("cover_big") or a.get("cover_medium")
            albums.append({
                "id": str(a.get("id", "")),
                "name": a.get("title", "Unknown"),
                "artist": artist.get("name", "Unknown"),
                "artists": artists,
                "release_date": (a.get("release_date") or "")[:10],
                "total_tracks": int(a.get("nb_tracks") or 0),
                "album_art": cover,
                "external_url": a.get("link", ""),
            })
        return albums

    def search_artists(self, query: str, limit: int = 5) -> List[Dict]:
        """Sucht Künstler per Name. Gibt Liste mit {id, name, picture, ...} zurück."""
        limit = max(1, min(int(limit), 50))
        data = _get("/search/artist", {"q": query, "limit": limit})
        items = data.get("data") or []
        out = []
        for a in items:
            out.append({
                "id": str(a.get("id", "")),
                "name": a.get("name", ""),
                "picture": a.get("picture_xl") or a.get("picture_big") or a.get("picture_medium"),
                "nb_album": int(a.get("nb_album") or 0),
                "nb_fan": int(a.get("nb_fan") or 0),
                "external_url": a.get("link", ""),
            })
        return out

    def get_artist_radio(self, artist_id: str, limit: int = 25) -> List[Dict]:
        """Holt Tracks aus dem Deezer Artist Radio (= Empfehlungen rund um den Künstler).

        Ideal als Mix-Quelle / Genre-Topup-Fallback wenn ListenBrainz für ein Genre
        keine Recommendations liefert.

        Endpoint: /artist/{id}/radio  (gibt ~25 Tracks per default zurück)
        Anreichert die Tracks gleich mit Genres aus ihrem Album-Cache.
        """
        if not artist_id:
            return []
        limit = max(1, min(int(limit), 100))
        data = _get(f"/artist/{artist_id}/radio", {"limit": limit})
        items = data.get("data") or []
        out: List[Dict] = []
        for t in items:
            track = _track_from_api(t)
            self._enrich_with_genres(track)
            out.append(track)
        return out

    def get_album_details(self, album_id: str, source: str = "default") -> Optional[Dict]:
        try:
            album = _get(f"/album/{album_id}", source=source)
        except Exception as e:
            print(f"Deezer album lookup error: {e}")
            return None
        if not album or not album.get("id"):
            return None
        artist = album.get("artist") or {}
        artists = [artist["name"]] if artist.get("name") else []
        cover = album.get("cover_xl") or album.get("cover_big") or album.get("cover_medium")
        release_date = (album.get("release_date") or "")[:10]

        tracks = []
        track_list = album.get("tracks") or {}
        items = track_list.get("data") or []
        for t in items:
            tracks.append(_track_from_api(t))

        next_url = track_list.get("next")
        while next_url:
            try:
                # Pagination über dieselbe Source-Lane wie der initiale /album/{id}-Call —
                # sonst zerreißt ein einzelner Album-Lookup auf zwei verschiedene VPN-IPs.
                page = _get_session(source).get(next_url, timeout=15)
                page.raise_for_status()
                chunk = page.json()
                for t in chunk.get("data") or []:
                    tracks.append(_track_from_api(t))
                next_url = chunk.get("next")
            except Exception as e:
                print(f"Deezer album tracks pagination: {e}")
                break

        # Album-Genres einmal extrahieren — gilt für alle Tracks
        album_genres_block = (album.get("genres") or {}).get("data") or []
        album_genre_names = []
        seen_g = set()
        for g in album_genres_block:
            n = (g.get("name") or "").strip()
            if n and n not in seen_g:
                seen_g.add(n)
                album_genre_names.append(n)
        # Cache füllen, damit spätere get_track_details(track_id) ohne extra HTTP-Call auskommen
        if album_genre_names:
            self._album_genre_cache[str(album.get("id", ""))] = album_genre_names

        for i, tr in enumerate(tracks, start=1):
            tr["track_number"] = i
            tr["album"] = album.get("title", tr.get("album", "Unknown"))
            tr["album_id"] = str(album.get("id", ""))
            tr["album_art"] = cover
            tr["release_date"] = release_date
            tr["genres"] = list(album_genre_names)

        return {
            "id": str(album["id"]),
            "name": album.get("title", "Unknown"),
            "artist": artist.get("name", "Unknown"),
            "artists": artists,
            "release_date": release_date,
            "total_tracks": len(tracks),
            "album_art": cover,
            "external_url": album.get("link", ""),
            "tracks": tracks,
        }
