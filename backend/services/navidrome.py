import os
import re
import shutil
import threading
import time
import requests
from typing import Dict, List, Optional, Any, Set, Tuple
from pathlib import Path
import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

# Konstanten für die Subsonic-API
SUBSONIC_API_VERSION = "1.16.1"
SUBSONIC_CLIENT = "tonus"

# Phase H — Library-Match-First Cache.
#
# Der CSV-Import ruft `library_signatures()` einmal pro Job, um festzustellen,
# welche Tracks bereits in Navidromes Library liegen — DANN erst gehen die
# Reste an Deezer/Spotify zur Provider-Suche. Das spart bei wachsender Library
# 80%+ Provider-Calls.
#
# Cache-Strategie: einmaliger Filesystem-Scan pro TTL-Fenster (Default 30 min).
# Build dauert bei 50k Tracks ~30-60s, alle Folge-Aufrufe innerhalb des Fensters
# sind instant. Der Scan blockiert den ersten CSV-Job des Fensters merklich;
# das ist akzeptabel weil es danach für stündliche/tägliche Imports nichts
# kostet.
#
# Module-level Cache statt Instance-Cache, damit alle Worker-Lanes dieselbe
# Snapshot teilen. Lock gegen Race wenn zwei Imports gleichzeitig starten.
#
# Cache-Layout (seit Phase 0 incremental scan, 2026-05-10):
#   _LIBRARY_SIG_CACHE = (
#       last_full_scan_ts,            # float — wann der letzte VOLLE Scan war
#       sigs,                          # Set[Tuple[str,str]] — abgeleitet aus path_map
#       path_map,                      # Dict[str, (mtime, sig_pair_or_none)]
#       last_file_count,               # int — Hint für Phase-0-Progress-Bar
#   )
#
# `_LIBRARY_SIG_CACHE_TTL_S` (default 300s) = wie lange Folge-Calls direkt aus
# `sigs` ohne Re-Scan zurückkommen. Bei TTL-Miss wird inkrementell gescannt
# (stat-mtime-Diff, ~5-10s bei 50k Files), nicht voll. Voller Re-Scan nur:
#   - cold cache (erster Call nach Restart)
#   - last_full_scan_ts älter als `_LIBRARY_SIG_FULLSCAN_S` (default 6h) →
#     drops files die outside-mtime-changed wurden (z.B. Tag-Edit ohne mtime-Touch)
#   - force_refresh=True
_LIBRARY_SIG_TTL_S = int(os.getenv("LIBRARY_SIG_CACHE_TTL_S", "300"))
_LIBRARY_SIG_FULLSCAN_S = int(os.getenv("LIBRARY_SIG_FULLSCAN_HOURS", "6")) * 3600
_LIBRARY_SIG_CACHE: Optional[
    Tuple[
        float,
        Set[Tuple[str, str]],
        Dict[str, Tuple[float, Optional[Tuple[str, str]]]],
        int,
    ]
] = None
_LIBRARY_SIG_LOCK = threading.Lock()


def library_sig_last_file_count() -> Optional[int]:
    """Hint für Phase-0-Progress-Bar: file_count vom letzten Scan, oder None
    wenn noch nie gescannt wurde. Worker nutzt das um die Progress-Ratio
    zu berechnen (file_count / expected) während ein neuer Scan läuft."""
    if _LIBRARY_SIG_CACHE is None:
        return None
    return _LIBRARY_SIG_CACHE[3]


def _normalize_sig(s: str) -> str:
    """Aggressive Normalisierung für Library-Signature-Match.

    Lowercase + alle Nicht-Alphanumerics raus. Damit matchen "The Beatles"
    und "the-beatles!" auf denselben Bucket. Gleiches Schema wie der
    existierende `_norm()` in `navidrome_library_sync.py`, hier zur
    Wiederverwendung im Service.
    """
    if not s:
        return ""
    return re.sub(r"[^a-z0-9]+", "", s.lower())


class NavidromeService:
    def __init__(self):
        self.music_path = config.NAVIDROME_MUSIC_PATH
        self.api_url = config.NAVIDROME_API_URL
        self.username = config.NAVIDROME_USERNAME
        self.password = config.NAVIDROME_PASSWORD

    # ---------------------------------------------------------------
    # Subsonic-API-Helfer (Phase B Vorbereitung)
    # ---------------------------------------------------------------

    def _subsonic_params(self, **extra: Any) -> Dict[str, Any]:
        """Baut die Pflicht-Query-Parameter für jeden Subsonic-Endpoint."""
        base = {
            "u": self.username,
            "p": self.password,
            "v": SUBSONIC_API_VERSION,
            "c": SUBSONIC_CLIENT,
            "f": "json",
        }
        base.update({k: v for k, v in extra.items() if v is not None})
        return base

    def _subsonic_call(self, endpoint: str, **params: Any) -> Optional[Dict[str, Any]]:
        """Ruft einen Subsonic-Endpoint auf und gibt 'subsonic-response' zurück.

        Subsonic-Konvention: Antwort ist ein Wrapper-Objekt mit 'status' = 'ok' / 'failed'.
        Bei Fehler returnt die Methode None und loggt den Fehler.
        """
        if not self.api_url or not self.username or not self.password:
            print("[navidrome] API credentials not configured")
            return None
        url = f"{self.api_url.rstrip('/')}/rest/{endpoint}"
        try:
            resp = requests.get(url, params=self._subsonic_params(**params), timeout=15)
            resp.raise_for_status()
            data = resp.json().get("subsonic-response") or {}
            if data.get("status") != "ok":
                err = (data.get("error") or {}).get("message", "unknown error")
                print(f"[navidrome] {endpoint} failed: {err}")
                return None
            return data
        except Exception as e:
            print(f"[navidrome] {endpoint} HTTP error: {e}")
            return None

    # ---------------------------------------------------------------
    # Playlist-Reader — wird vom Lückenfüller genutzt um Plugin-Playlists abzugrasen
    # ---------------------------------------------------------------

    def get_playlists(self) -> List[Dict[str, Any]]:
        """Alle Playlists abrufen.

        Returns: list of dicts wie [{id, name, songCount, comment, owner, public, ...}, ...]
        """
        data = self._subsonic_call("getPlaylists.view")
        if not data:
            return []
        block = (data.get("playlists") or {}).get("playlist") or []
        # Subsonic kann hier ein Single-Object liefern statt List
        if isinstance(block, dict):
            block = [block]
        return block

    def get_playlist(self, playlist_id: str) -> Optional[Dict[str, Any]]:
        """Details einer Playlist inkl. Tracks. Returns None bei Fehler."""
        data = self._subsonic_call("getPlaylist.view", id=playlist_id)
        if not data:
            return None
        return data.get("playlist") or None

    def get_playlist_tracks(self, playlist_id: str) -> List[Dict[str, Any]]:
        """Convenience: nur die Track-Liste einer Playlist (entry-Block)."""
        pl = self.get_playlist(playlist_id)
        if not pl:
            return []
        entries = pl.get("entry") or []
        if isinstance(entries, dict):
            entries = [entries]
        return entries

    # ---------------------------------------------------------------
    # Playlist-Writer — wird vom Plugin-Sync-Reconcile genutzt
    #
    # Idempotenz-Strategie: wir filtern Track-IDs gegen die existierende
    # Playlist (read-before-write), damit wiederholtes Aufrufen der gleichen
    # Reconcile-Funktion keine Duplikate produziert. Subsonic selbst dedupt
    # nicht beim updatePlaylist-Aufruf.
    # ---------------------------------------------------------------

    def find_playlist_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        """Sucht eine Playlist nach exaktem Name-Match. Returns None wenn nicht
        gefunden — Caller muss dann create_playlist aufrufen."""
        if not name:
            return None
        for pl in self.get_playlists():
            if (pl.get("name") or "") == name:
                return pl
        return None

    def create_playlist(self, name: str, song_ids: Optional[List[str]] = None) -> Optional[str]:
        """Legt eine Playlist an. Returns die neue Playlist-ID oder None bei Fehler.

        song_ids ist optional — Subsonic erlaubt das Anlegen leerer Playlists
        und nachträgliches Befüllen via updatePlaylist."""
        if not name:
            return None
        params: Dict[str, Any] = {"name": name}
        if song_ids:
            # requests.get serialisiert lists als ?songId=a&songId=b automatisch.
            params["songId"] = list(song_ids)
        data = self._subsonic_call("createPlaylist.view", **params)
        if not data:
            return None
        pl = data.get("playlist") or {}
        return pl.get("id")

    def add_tracks_to_playlist(
        self, playlist_id: str, song_ids: List[str]
    ) -> Dict[str, int]:
        """Fügt Tracks idempotent zu einer existierenden Playlist hinzu.

        - Liest erst die aktuellen Tracks der Playlist und filtert IDs raus,
          die schon drin sind.
        - Returnt {"added": N, "already_present": M} damit Caller loggen kann.
        """
        if not playlist_id or not song_ids:
            return {"added": 0, "already_present": 0}
        existing = {e.get("id") for e in self.get_playlist_tracks(playlist_id) if e.get("id")}
        to_add = [sid for sid in song_ids if sid and sid not in existing]
        if not to_add:
            return {"added": 0, "already_present": len(song_ids)}
        data = self._subsonic_call(
            "updatePlaylist.view",
            playlistId=playlist_id,
            songIdToAdd=list(to_add),
        )
        if not data:
            return {"added": 0, "already_present": len(existing & set(song_ids))}
        return {"added": len(to_add), "already_present": len(existing & set(song_ids))}

    # ---------------------------------------------------------------
    # Library-Lookup — fragt ob ein Track schon vorhanden ist
    # ---------------------------------------------------------------

    def library_has_track(self, artist: str, title: str) -> bool:
        """Heuristik: True wenn Subsonic-Search den Track findet.

        Wir prüfen, ob mindestens ein Search-Result 'song' den Titel enthält UND
        der Artist passt. Subsonic-Search ist tokenisiert/fuzzy — daher case-insensitive
        Substring-Match auf beide Felder.
        """
        if not artist or not title:
            return False
        query = f"{artist} {title}".strip()
        data = self._subsonic_call(
            "search3.view",
            query=query,
            songCount=10,
            albumCount=0,
            artistCount=0,
        )
        if not data:
            return False
        result = data.get("searchResult3") or {}
        songs = result.get("song") or []
        if isinstance(songs, dict):
            songs = [songs]
        a_lo = artist.lower().strip()
        t_lo = title.lower().strip()
        for s in songs:
            s_artist = (s.get("artist") or "").lower()
            s_title = (s.get("title") or "").lower()
            if (a_lo in s_artist or s_artist in a_lo) and (t_lo in s_title or s_title in t_lo):
                return True
        return False

    def library_signatures(
        self,
        force_refresh: bool = False,
        on_progress: Optional[Any] = None,
    ) -> Set[Tuple[str, str]]:
        """Returns ein Set aus (artist_norm, title_norm) für alle Tracks in
        Navidromes Music-Pfaden. Aggressiv normalisiert via `_normalize_sig`
        (lowercase + non-alphanumerics raus), damit Schreibvarianten matchen.

        Verwendet Filesystem-Scan + Tag-Read aus `navidrome_library_sync.py`,
        nicht Subsonic-API — bei 50k Tracks wären das sonst 50k API-Calls.

        Cache-Strategie (Phase 0 incremental, 2026-05-10):
          - cold cache → full scan (~30-60s @ 50k Tracks)
          - cache hit innerhalb TTL (300s) → instant return
          - TTL miss aber path_map vorhanden → INKREMENTELLER scan (stat-mtime
            pro Pfad, ~5-10s @ 50k Tracks). Tag-Read NUR für neue/changed Files.
          - last_full_scan älter als FULLSCAN_S (6h) → erneut full scan
            (drops Files die outside-mtime modifiziert wurden, z.B. tag-edit
            ohne Datei-mtime-Touch)
          - force_refresh=True → full scan immer

        Lock-protected gegen parallele Builds (zwei CSV-Imports gleichzeitig).

        on_progress: optionaler Callable(file_count, sigs_count) der alle ~500
        Files aufgerufen wird damit Caller live-Status-Updates schreiben kann
        (Worker → upsert_import_job message). Cache-Hits triggern den Callback
        nicht — der wäre da auch sinnlos (instant return).
        """
        global _LIBRARY_SIG_CACHE
        now = time.time()
        if not force_refresh and _LIBRARY_SIG_CACHE is not None:
            ts, sigs, _path_map, _fc = _LIBRARY_SIG_CACHE
            if now - ts < _LIBRARY_SIG_TTL_S:
                return sigs

        with _LIBRARY_SIG_LOCK:
            # Re-check nach Lock — zwei Threads könnten denselben miss sehen
            if not force_refresh and _LIBRARY_SIG_CACHE is not None:
                ts, sigs, _path_map, _fc = _LIBRARY_SIG_CACHE
                if now - ts < _LIBRARY_SIG_TTL_S:
                    return sigs

            # Decide scan mode: full vs incremental.
            #   - Cold cache: full
            #   - Cache age > FULLSCAN_S: full (drops outside-mtime tag-edits)
            #   - force_refresh: full
            #   - Else: incremental (mtime-based skip of unchanged files)
            do_full_scan = True
            prev_path_map: Dict[str, Tuple[float, Optional[Tuple[str, str]]]] = {}
            if not force_refresh and _LIBRARY_SIG_CACHE is not None:
                prev_ts, _prev_sigs, prev_path_map, _prev_fc = _LIBRARY_SIG_CACHE
                if now - prev_ts < _LIBRARY_SIG_FULLSCAN_S:
                    do_full_scan = False

            # Lazy-import — der Sync-Module hat schwere Dependencies (mutagen)
            # die wir nicht beim Import von navidrome.py laden wollen.
            from utils.navidrome_library_sync import iter_audio_files, read_artist_title

            new_path_map: Dict[str, Tuple[float, Optional[Tuple[str, str]]]] = {}
            scan_start = time.time()
            file_count = 0
            tag_reads = 0  # Diagnostik: wie viele Tag-Reads incremental gespart
            # Progress-Tick alle 500 Files — bei 50k Tracks = 100 Updates,
            # bei 1k Tracks = 2 Updates. Genug für gefühlten Live-Progress
            # ohne DB-Spam.
            PROGRESS_EVERY = 500
            for music_root in config.NAVIDROME_MUSIC_PATHS_LIST:
                root = Path(music_root)
                if not root.is_dir():
                    print(f"[library_signatures] skip non-dir: {root}")
                    continue
                for path in iter_audio_files(root):
                    file_count += 1
                    path_str = str(path)
                    try:
                        mtime = path.stat().st_mtime
                    except OSError:
                        # File während Scan verschwunden — skippen
                        continue

                    # Incremental-Path: wenn mtime unverändert, alten sig-pair
                    # wiederverwenden (kein Tag-Read). Bei full_scan immer
                    # neu lesen.
                    cached = prev_path_map.get(path_str) if not do_full_scan else None
                    if cached is not None and cached[0] >= mtime:
                        # mtime unchanged — reuse cached sig pair (incl. None)
                        new_path_map[path_str] = cached
                    else:
                        # New file or mtime increased → re-read tags
                        pair = read_artist_title(path)
                        tag_reads += 1
                        if pair:
                            artist, title = pair
                            sig_pair = (_normalize_sig(artist), _normalize_sig(title))
                            if not (sig_pair[0] or sig_pair[1]):
                                sig_pair = None
                        else:
                            sig_pair = None
                        new_path_map[path_str] = (mtime, sig_pair)

                    if on_progress and (file_count % PROGRESS_EVERY == 0):
                        # sigs_count ist hier ein Live-Approximator: zähle sigs
                        # in new_path_map (cheap weil dict-iteration). Nicht
                        # exakt = final sigs (deduped Set), aber gut genug
                        # für Progress-Anzeige.
                        try:
                            approx_sigs = sum(1 for _, p in new_path_map.values() if p is not None)
                            on_progress(file_count, approx_sigs)
                        except Exception:
                            pass

            # sigs aus path_map aggregieren — gelöschte Files fehlen automatisch
            # in new_path_map und fallen damit aus den Signatures raus.
            sigs: Set[Tuple[str, str]] = set()
            for _mtime, sig_pair in new_path_map.values():
                if sig_pair is not None:
                    sigs.add(sig_pair)

            elapsed = time.time() - scan_start
            mode = "full" if do_full_scan else "incremental"
            print(
                f"[library_signatures] {mode} scan: {file_count} files, "
                f"{tag_reads} tag-reads, {len(sigs)} unique signatures, "
                f"{elapsed:.1f}s"
            )
            if on_progress:
                try:
                    on_progress(file_count, len(sigs))
                except Exception:
                    pass

            # Bei incremental scan bleibt last_full_scan_ts vom vorherigen Cache
            # — sonst würden wir den 6h-Trigger nie erreichen weil incremental
            # ihn ständig zurücksetzt.
            if do_full_scan:
                last_full_ts = now
            else:
                last_full_ts = _LIBRARY_SIG_CACHE[0] if _LIBRARY_SIG_CACHE else now
            _LIBRARY_SIG_CACHE = (last_full_ts, sigs, new_path_map, file_count)
            return sigs

    def library_match(
        self, artist: str, title: str, signatures: Optional[Set[Tuple[str, str]]] = None
    ) -> bool:
        """Bulk-fähiger Counterpart zu `library_has_track()`.

        Statt einem Subsonic-Call pro Track (was bei 30k-Imports nicht
        skaliert) prüft diese Methode gegen ein vorab geladenes Set aus
        `library_signatures()`. Caller, die viele Tracks prüfen, sollten
        das Set einmal holen und durchreichen — sonst wird's per call
        gebaut (oder aus Cache gezogen, was OK ist aber ein Lock-Acquire
        kostet).
        """
        if not artist or not title:
            return False
        if signatures is None:
            signatures = self.library_signatures()
        sig = (_normalize_sig(artist), _normalize_sig(title))
        return sig in signatures

    def find_track_id_by_artist_title(self, artist: str, title: str) -> Optional[str]:
        """Wie library_has_track, gibt aber die Subsonic-Track-ID zurück (für Playlist-Manipulation)."""
        if not artist or not title:
            return None
        query = f"{artist} {title}".strip()
        data = self._subsonic_call(
            "search3.view",
            query=query,
            songCount=10,
            albumCount=0,
            artistCount=0,
        )
        if not data:
            return None
        result = data.get("searchResult3") or {}
        songs = result.get("song") or []
        if isinstance(songs, dict):
            songs = [songs]
        a_lo = artist.lower().strip()
        t_lo = title.lower().strip()
        for s in songs:
            s_artist = (s.get("artist") or "").lower()
            s_title = (s.get("title") or "").lower()
            if (a_lo in s_artist or s_artist in a_lo) and (t_lo in s_title or s_title in t_lo):
                return str(s.get("id") or "") or None
        return None

    def _library_root(self, library_root: Optional[str]) -> Path:
        return Path(library_root or self.music_path)

    def get_target_path(self, track_info: Dict, file_extension: str, library_root: Optional[str] = None) -> Path:
        """Get the target path for a track under a configured Navidrome music root."""

        # Ensure artist names are joined with semicolons
        if 'artist' in track_info:
            track_info['artist'] = track_info['artist'].replace(',', ';')
            # Extract only the first artist
            artist_name = track_info['artist'].split(';')[0].strip()
        else:
            artist_name = 'Unknown Artist'

        if 'album_artist' in track_info:
            track_info['album_artist'] = track_info['album_artist'].replace(',', ';')
            # Extract only the first artist
            artist_name = track_info['album_artist'].split(';')[0].strip()

        # Create artist directory structure
        artist_name = self._sanitize_path(artist_name)
        album_name = self._sanitize_path(track_info.get('album', 'Unknown Album'))
        
        # Create directory: <root>/Artist/Album/
        target_dir = self._library_root(library_root) / artist_name / album_name
        target_dir.mkdir(parents=True, exist_ok=True)
        
        # Build filename
        filename = self._sanitize_filename(f"{track_info['name']}.{file_extension}")
        target_path = target_dir / filename
        
        # If file exists, add number suffix
        if target_path.exists():
            base_name = target_path.stem
            counter = 1
            while target_path.exists():
                target_path = target_dir / f"{base_name} ({counter}).{file_extension}"
                counter += 1
        
        return target_path

    def track_file_exists(self, track_info: Dict, file_extension: str, library_root: Optional[str] = None) -> bool:
        """True if a file for this track already exists under the given root (incl. numbered duplicates)."""
        ti = dict(track_info)
        if 'artist' in ti:
            ti['artist'] = ti['artist'].replace(',', ';')
            artist_name = ti['artist'].split(';')[0].strip()
        else:
            artist_name = 'Unknown Artist'

        if 'album_artist' in ti:
            ti['album_artist'] = ti['album_artist'].replace(',', ';')
            artist_name = ti['album_artist'].split(';')[0].strip()

        artist_name = self._sanitize_path(artist_name)
        album_name = self._sanitize_path(ti.get('album', 'Unknown Album'))
        target_dir = self._library_root(library_root) / artist_name / album_name
        if not target_dir.is_dir():
            return False

        stem = self._sanitize_filename(ti['name'])
        ext = file_extension.lstrip('.')
        p0 = target_dir / f"{stem}.{ext}"
        if p0.is_file():
            return True
        for n in range(1, 100):
            p = target_dir / f"{stem} ({n}).{ext}"
            if p.is_file():
                return True
        return False

    def finalize_track(self, file_path: str) -> Dict:
        """Finalize track by triggering Navidrome scan"""
        try:
            # Trigger Navidrome scan to pick up the new file
            self._trigger_scan()
            
            return {
                'success': True,
                'target_path': file_path,
                'message': 'Track successfully added to Navidrome'
            }
        
        except Exception as e:
            print(f"Navidrome finalization error: {e}")
            return {
                'success': False,
                'error': str(e)
            }
    
    def upload_to_navidrome(self, file_path: str, track_info: Dict) -> Dict:
        """Copy file to Navidrome music directory (legacy method, kept for compatibility)"""
        try:
            target_path = self.get_target_path(track_info, Path(file_path).suffix[1:])
            shutil.copy2(file_path, target_path)
            return self.finalize_track(str(target_path))
        except Exception as e:
            print(f"Navidrome upload error: {e}")
            return {
                'success': False,
                'error': str(e)
            }
    
    def _trigger_scan(self) -> bool:
        """Trigger Navidrome library scan via Subsonic API"""
        if not self.api_url or not self.username or not self.password:
            print("Navidrome API credentials not configured, skipping scan trigger")
            return False
        
        try:
            # Subsonic API endpoint for starting scan
            # Note: This requires admin credentials
            import requests.auth
            
            auth = requests.auth.HTTPBasicAuth(self.username, self.password)
            url = f"{self.api_url}/rest/startScan.view"
            params = {
                'u': self.username,
                'p': self.password,
                'v': '1.16.1',
                'c': SUBSONIC_CLIENT,
                'f': 'json'
            }
            
            response = requests.get(url, params=params, auth=auth, timeout=10)
            return response.status_code == 200
        
        except Exception as e:
            print(f"Error triggering Navidrome scan: {e}")
            return False
    
    def _sanitize_path(self, path: str) -> str:
        """Remove invalid characters from path"""
        import re
        # Remove invalid characters
        path = re.sub(r'[<>:"/\\|?*]', '', path)
        # Replace multiple spaces with single space
        path = re.sub(r'\s+', ' ', path)
        # Trim
        return path.strip()
    
    def _sanitize_filename(self, filename: str) -> str:
        """Remove invalid characters from filename"""
        import re
        # Remove invalid characters
        filename = re.sub(r'[<>:"/\\|?*]', '', filename)
        # Replace multiple spaces with single space
        filename = re.sub(r'\s+', ' ', filename)
        # Trim
        return filename.strip()

