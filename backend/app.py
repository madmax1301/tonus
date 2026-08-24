from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request
from fastapi.staticfiles import StaticFiles
from urllib.parse import quote, unquote
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Any, Dict, List, Optional
import os
import re
import sys
import shutil
from pathlib import Path
import time
import random
import threading
_csv_lock = threading.Lock()
_worker: "Optional[JobWorker]" = None  # forward ref — JobWorker imported below

# Plugin-Sync-State: wird von /api/plugin/sync-status gelesen, damit das
# Navidrome-Plugin den Status des letzten Runs anzeigen kann. Module-global
# statt DB, weil es immer nur einen Eintrag gibt (letzter Run) und FastAPI
# mit BackgroundTasks im selben Prozess läuft.
_plugin_sync_lock = threading.Lock()
_plugin_sync_state: Dict[str, Any] = {
    "last_status": None,         # None | "running" | "ok" | "error"
    "last_started_ms": 0,
    "last_finished_ms": 0,
    "last_candidates": 0,
    "last_queued": 0,
    "last_skipped": 0,
    "last_failed": 0,
    "last_error": None,
}

# Add parent directory to path for imports
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

import config
from services.deezer import DeezerService
from services.spotify import SpotifyService
from utils.job_store import (
    _db,
    _now_ms,
    init_jobs_db,
    reset_stale_inflight_jobs,
    reset_stale_import_jobs,
    upsert_job,
    get_job,
    get_album_aggregate,
    record_completed_download,
    has_completed_download,
    upsert_import_job,
    get_import_job,
    insert_import_results,
    get_import_results,
    count_import_results,
    get_import_library_matches_with_playlists,
)
from utils.worker import JobWorker
from utils.auth import require_token, require_admin, auth_required, optional_token
from utils.rate_limit import make_rate_limiter

ALLOWED_METADATA_PROVIDERS = frozenset({"deezer", "spotify"})

# Rate-Limiter (Audit M-1, 2026-05-12). Module-level damit alle Requests
# denselben State teilen — Fresh-Per-Request würde den Counter resetten.
# Werte konservativ-großzügig gewählt: legitime Bulk-User stoßen nicht an,
# automatisierte Bursts werden gebremst.
_rl_csv_import = make_rate_limiter(20, 3600)        # 20 CSV-Imports/h
_rl_spotify_history = make_rate_limiter(10, 3600)   # 10 Spotify-History/h
_rl_url_download = make_rate_limiter(60, 3600)      # 60 URL-Downloads/h
_rl_track_download = make_rate_limiter(120, 3600)   # 120 Track-Downloads/h

# Extra download attempts after the first failure (each failure waits before retrying).
MAX_DOWNLOAD_RETRIES_CAP = 5


def _clamp_download_retries(n: Optional[int]) -> int:
    try:
        v = int(n) if n is not None else 0
    except (TypeError, ValueError):
        v = 0
    return max(0, min(v, MAX_DOWNLOAD_RETRIES_CAP))


def resolve_metadata_provider(raw: Optional[str]) -> str:
    p = (raw or config.DEFAULT_METADATA_PROVIDER or "deezer").lower().strip()
    if p not in ALLOWED_METADATA_PROVIDERS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid metadata provider. Use one of: {', '.join(sorted(ALLOWED_METADATA_PROVIDERS))}",
        )
    return p


def get_metadata_service(provider: str):
    if provider == "deezer":
        return deezer_service
    if provider == "spotify":
        if spotify_service is None:
            raise HTTPException(
                status_code=503,
                detail="Spotify is not configured. Set SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET in your environment.",
            )
        return spotify_service
    raise HTTPException(status_code=400, detail="Unknown metadata provider")


def _slim_track_for_queue(track: Optional[Dict]) -> Optional[Dict]:
    """Reduce a full track dict to the fields the queue UI renders (name/artist/album/cover)."""
    if not track:
        return None
    return {
        "id": str(track.get("id") or ""),
        "name": track.get("name") or "",
        "artist": track.get("artist") or "",
        "album": track.get("album") or "",
        "album_art": track.get("album_art") or "",
    }


def _resolve_track_for_queue(
    track_id: str,
    provider: str,
    hint: Optional[Dict] = None,
) -> Optional[Dict]:
    """Return queue-ready track info. Prefers a frontend-supplied hint, falls back to provider lookup."""
    if hint and (hint.get("name") or hint.get("artist") or hint.get("album_art")):
        slim = _slim_track_for_queue(hint)
        if slim and not slim.get("id"):
            slim["id"] = track_id
        return slim
    try:
        svc = get_metadata_service(provider)
        details = svc.get_track_details(track_id)
        return _slim_track_for_queue(details) if details else None
    except Exception as e:
        print(f"[queue] Could not resolve track metadata for {track_id}: {e}")
        return None


def resolve_navidrome_library_path_optional(raw: Optional[str]) -> str:
    """Return a configured Navidrome music root. Defaults to the first library."""
    if not raw or not str(raw).strip():
        return config.NAVIDROME_MUSIC_PATHS_LIST[0]
    norm = os.path.normpath(os.path.abspath(os.path.expanduser(str(raw).strip())))
    allowed_norms = {os.path.normpath(p) for p in config.NAVIDROME_MUSIC_PATHS_LIST}
    if norm not in allowed_norms:
        raise HTTPException(
            status_code=400,
            detail="Invalid Navidrome library path. Use a path from GET /api/navidrome/libraries.",
        )
    for p in config.NAVIDROME_MUSIC_PATHS_LIST:
        if os.path.normpath(p) == norm:
            return p
    return config.NAVIDROME_MUSIC_PATHS_LIST[0]


def get_system_downloads_folder():
    """Get the user's system Downloads folder"""
    home = Path.home()

    # Check common Downloads folder locations
    if os.name == 'nt':  # Windows
        downloads = home / "Downloads"
    else:  # Linux/Mac
        downloads = home / "Downloads"

    # Create if doesn't exist
    downloads.mkdir(parents=True, exist_ok=True)
    return str(downloads)


from services.youtube import YouTubeService
from services.metadata import MetadataService
from services.navidrome import NavidromeService
from utils.file_handler import get_download_path
from utils.navidrome_library_sync import start_navidrome_library_sync_background

# ─── Logging-Setup ────────────────────────────────────────────────
# Configure root logger from env (LOG_LEVEL) BEFORE any service-Module
# lazy-init kicks in. Format: short timestamp + level + name + message.
# `force=True` weil uvicorn ggf. schon einen Default-Logger registriert
# hat — wir wollen unseren ersetzen, nicht parallel laufen lassen.
import logging as _logging
_logging.basicConfig(
    level=getattr(_logging, config.LOG_LEVEL, _logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
    force=True,
)
# Uvicorn-Access-Logs (das 'GET /api/queue 200 OK'-Spam) optional abdrehen.
# Bei aktivem Polling (UI macht alle 1s einen Queue-Call) überfluten die
# Lines den Container-Log und überdecken echte WARN/ERROR-Meldungen.
if not config.UVICORN_ACCESS_LOG:
    _logging.getLogger("uvicorn.access").disabled = True

app = FastAPI(title="Tonus API", version="1.0.0")

def _migrate_legacy_jobs_db() -> None:
    """Einmalige Migration alter jobs.db-Standorte auf JOBS_DB_PATH (/app/data/).

    Bis Mai 2026 lag jobs.db unter $DOWNLOAD_DIR/jobs.db (relativ zum CWD).
    Der CWD im Container war /app/backend → DB landete in /app/backend/downloads,
    NICHT im gemounteten /app/downloads. Bei jedem `--no-cache` Container-Rebuild
    war damit die Auth-DB weg.

    Neuer Pfad: /app/data/jobs.db, eigenes Bind-Mount-Volume.

    Diese Funktion: wenn neue DB nicht existiert UND alte (an einem von zwei
    Legacy-Pfaden) gefunden wird, einmalig kopieren. Idempotent — Re-Run No-Op."""
    import shutil
    from utils.job_store import JOBS_DB_PATH as new_path

    if os.path.exists(new_path):
        return  # neue DB existiert ⇒ nichts migrieren

    # Mögliche Legacy-Pfade durchprobieren — Reihenfolge nach Wahrscheinlichkeit.
    candidates = [
        os.path.abspath(os.path.join(config.DOWNLOAD_DIR, "jobs.db")),  # "./downloads/jobs.db"
        "/app/backend/downloads/jobs.db",
        "/app/downloads/jobs.db",
    ]
    seen = set()
    for legacy in candidates:
        if legacy in seen or not os.path.isfile(legacy):
            continue
        seen.add(legacy)
        try:
            os.makedirs(os.path.dirname(new_path), exist_ok=True)
            shutil.copy2(legacy, new_path)
            # WAL/SHM mitkopieren falls vorhanden — sonst ginge in-flight WAL verloren
            for ext in ("-wal", "-shm"):
                if os.path.isfile(legacy + ext):
                    shutil.copy2(legacy + ext, new_path + ext)
            print(f"[migrate] Legacy jobs.db von {legacy} → {new_path} kopiert", flush=True)
            return
        except Exception as e:
            print(f"[migrate] Konnte {legacy} → {new_path} nicht kopieren: {e}", flush=True)


_migrate_legacy_jobs_db()
init_jobs_db()
_stale = reset_stale_inflight_jobs()
if _stale:
    print(f"Re-queued {_stale} interrupted download job(s) after server start")
_csv_stale = reset_stale_import_jobs()
if _csv_stale.get("jobs_reset") or _csv_stale.get("rows_purged"):
    print(
        f"Reset {_csv_stale['jobs_reset']} stale CSV import job(s) and purged "
        f"{_csv_stale['rows_purged']} pending/claimed staging rows"
    )


# ───────────────────────────────────────────────────────────────────
# F.5: Legacy-Token-Migration — Deprecation-Warning beim Boot
# H-6 Audit (2026-05-12): Default-/Placeholder-Token-Check beim Boot
# ───────────────────────────────────────────────────────────────────
# Known placeholder strings die der Operator vergessen haben könnte
# umzustellen. Wenn TONUS_API_TOKEN einen davon enthält oder zu kurz
# ist, refuse-to-boot (statt silent als legitimer auth-Mode laufen).
_TOKEN_PLACEHOLDER_MARKERS = (
    "change_me",
    "changeme",
    "replace-with",
    "replace_with",
    "please_set",
    "your-token",
    "your_token",
    "default-token",
    "default_token",
    "xxxxxxxx",
    "todo",
    "fixme",
)
# H-6: Plus minimum entropy requirement. 16 Zeichen ist eine niedrige
# Schwelle die ein zufälliges Hex-32 sauber überschreitet aber typische
# Tippfehler-Werte ("admin", "test123") abfängt.
_TOKEN_MIN_LEN = 16


def _legacy_token_deprecation_notice() -> None:
    """Boot-time-Check für TONUS_API_TOKEN: deprecation + placeholder-guard.

    Drei separate Conditions:
      1. Token leer/nicht gesetzt → nichts zu tun, return.
      2. Token sieht aus wie ein Placeholder ("CHANGE_ME", "replace-with-…")
         ODER ist kürzer als ``_TOKEN_MIN_LEN`` → **refuse-to-boot** mit
         klarer Fehlermeldung (Audit H-6, 2026-05-12). Operator muss
         entweder einen starken random-Wert eintragen ODER die Zeile aus
         der ``.env`` entfernen.
      3. Token ist OK aber gesetzt → Deprecation-Warning damit der
         Operator zur PAT-Migration angestoßen wird (Phase F.5).

    KEIN Auto-User-Create mehr — der erste Admin wird über das Onboarding
    in der UI angelegt (``/api/auth/setup``-Wizard). Das auth.py-Setup-Gate
    öffnet auch wenn Legacy-Token gesetzt ist, solange noch kein User in
    der DB existiert.
    """
    token = config.TONUS_API_TOKEN
    if not token:
        return

    token_lower = token.lower()
    looks_placeholder = any(m in token_lower for m in _TOKEN_PLACEHOLDER_MARKERS)
    too_short = len(token) < _TOKEN_MIN_LEN

    if looks_placeholder or too_short:
        print("=" * 64, flush=True)
        print("✗  TONUS_API_TOKEN FAILED HEALTH-CHECK (Audit H-6)", flush=True)
        if looks_placeholder:
            print(f"   Token looks like an un-substituted placeholder:", flush=True)
            print(f"     {token[:32]}{'…' if len(token) > 32 else ''}", flush=True)
        if too_short:
            print(f"   Token is too short ({len(token)} chars, min {_TOKEN_MIN_LEN}).", flush=True)
        print("", flush=True)
        print("   Fix one of:", flush=True)
        print("     • Generate a real random value:  openssl rand -hex 32", flush=True)
        print("     • Or remove the TONUS_API_TOKEN line from .env entirely.", flush=True)
        print("=" * 64, flush=True)
        # Fail-fast statt mit unsicherer Auth-Config booten.
        raise SystemExit(1)

    print("=" * 64, flush=True)
    print("⚠  TONUS_API_TOKEN is DEPRECATED.", flush=True)
    print("   Migrate the Navidrome plugin to a PAT (Settings → API tokens),", flush=True)
    print("   then remove TONUS_API_TOKEN from backend/.env.", flush=True)
    print("   See CUTOVER.md → 'Plugin-Migration auf PAT-Auth'.", flush=True)
    print("=" * 64, flush=True)


_legacy_token_deprecation_notice()


# ───────────────────────────────────────────────────────────────────
# UI-editierbare Provider-Configs aus app_settings laden
# ───────────────────────────────────────────────────────────────────
# DB-Overrides MÜSSEN vor der Service-Instanziierung (DeezerService,
# SpotifyService, NavidromeService) passieren, weil die Services im
# Constructor config.X-Werte lesen. Nachträgliches Patchen würde sie
# nicht erreichen.
config.apply_db_overrides()


# ----- Dual-VPN-Splitting: Boot-Check -----
# Wenn VPN_SPLIT_ENABLED=true ist, müssen beide Source-IPs (VPN_SOURCE_A,
# VPN_SOURCE_B) auf einem Host-Interface bindbar sein. Schlägt das fehl,
# bricht der Boot mit klarer Fehlermeldung ab — sofortiges Signal, dass die
# NAS-Multi-NIC-Konfig (Phase 1) oder network_mode: host (Phase 3) noch nicht
# durchgezogen ist. Bei VPN_SPLIT_ENABLED=false (Default) kein Check, kein
# Verhaltensänderung gegenüber dem Status quo.
def _verify_vpn_source_bindings() -> None:
    if os.environ.get("VPN_SPLIT_ENABLED", "").strip().lower() != "true":
        return
    import socket
    sources = {
        "VPN_SOURCE_A": (os.environ.get("VPN_SOURCE_A") or "").strip(),
        "VPN_SOURCE_B": (os.environ.get("VPN_SOURCE_B") or "").strip(),
    }
    for var, ip in sources.items():
        if not ip:
            raise RuntimeError(
                f"VPN_SPLIT_ENABLED=true, but {var} is empty. "
                f"Set VPN_SOURCE_A and VPN_SOURCE_B in compose env or disable splitting."
            )
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                s.bind((ip, 0))
            finally:
                s.close()
        except OSError as e:
            raise RuntimeError(
                f"Cannot bind to {var}={ip} on this host (errno {e.errno}: {e.strerror}). "
                f"Verify the NAS interface with this IP is up and that the container runs "
                f"with network_mode: host."
            )
    print(f"[boot] VPN-Split active: lane A={sources['VPN_SOURCE_A']}, lane B={sources['VPN_SOURCE_B']}")


_verify_vpn_source_bindings()

# Background worker — vier unabhängige Threads (User-Wunsch 2026-05-10):
#   - download              → Track-Downloads, eigene Lane
#   - import:csv            → klassischer Bulk-CSV-Import (full pipeline)
#   - import:spotify_history→ JSON-Streaming-History-Import (full pipeline)
#   - import:playlist_sync  → Library-Match + Reconcile only, KEIN Provider
# Lane-Filter in _poll_next_queued_import sorgt für saubere Trennung. Damit
# blockiert ein laufender Bulk-CSV-Import nicht mehr eine startende Playlist-
# Sync — beide laufen parallel.
_download_worker = JobWorker(job_type="download")
_csv_worker = JobWorker(job_type="import", import_lane="csv")
_spotify_history_worker = JobWorker(job_type="import", import_lane="spotify_history")
_playlist_sync_worker = JobWorker(job_type="import", import_lane="playlist_sync")
_download_worker.start()
_csv_worker.start()
_spotify_history_worker.start()
_playlist_sync_worker.start()
print("Worker threads started (download + import:csv + import:spotify_history + import:playlist_sync)")

# Phase 0 Pre-Warm: Library-Signature-Cache beim Container-Boot asynchron
# aufbauen, damit der ERSTE CSV-Import nach Restart nicht 30-60s in Phase 0
# hängt. Daemon-Thread blockiert App-Start nicht — wenn der App-Boot fertig
# ist und kein Import läuft, bauen wir den Cache nebenbei. Falls ein Import
# vorher startet, übernimmt dessen Lock-Acquire die Arbeit.
def _prewarm_library_signatures():
    try:
        import time as _time
        _time.sleep(2.0)  # App + Worker erst stabil hochfahren lassen
        from services.navidrome import NavidromeService
        print("[prewarm] starting library_signatures() warmup...", flush=True)
        t0 = _time.time()
        sigs = NavidromeService().library_signatures()
        elapsed = _time.time() - t0
        print(f"[prewarm] library cache ready: {len(sigs)} signatures, {elapsed:.1f}s", flush=True)
    except Exception as e:
        # Pre-warm-Fehler dürfen App-Boot nicht stören — Worker macht's
        # ggf. später beim ersten Import erneut.
        print(f"[prewarm] failed: {type(e).__name__}: {e} — first import will rebuild cache", flush=True)

_prewarm_thread = threading.Thread(
    target=_prewarm_library_signatures,
    name="library-prewarm",
    daemon=True,
)
_prewarm_thread.start()

@app.on_event("shutdown")
def _shutdown_workers():
    print("Shutting down workers...")
    _download_worker.shutdown(timeout=60)
    _csv_worker.shutdown(timeout=300)
    _spotify_history_worker.shutdown(timeout=300)
    _playlist_sync_worker.shutdown(timeout=60)
    print("Workers stopped")

# CORS middleware (still useful for API endpoints)
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

from starlette.responses import FileResponse
from starlette.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

class NoCacheStaticFiles(StaticFiles):
    """Static files without browser caching for JS/CSS."""
    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        if path.endswith('.js') or path.endswith('.css'):
            response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        return response

deezer_service = DeezerService()
try:
    spotify_service = SpotifyService()
except Exception as e:
    print(f"Spotify not available (optional): {e}")
    spotify_service = None

youtube_service = YouTubeService()
metadata_service = MetadataService()
navidrome_service = NavidromeService()

start_navidrome_library_sync_background(deezer_service, spotify_service)


def _start_playlist_reconcile_background() -> None:
    """Periodischer Playlist-Reconcile (v0.5.0).

    Läuft alle PLAYLIST_RECONCILE_INTERVAL_S (default 15 min) idempotent:
    Playlist-Marker (SC-Playlist-Import, CSV-Import, LB-Weekly) werden zu
    Subsonic-Playlists materialisiert. Bei leerem Marker-Set ist das ein
    einzelnes SQL-LIKE-Query, praktisch kostenlos.

    Daemon-Thread, lazy function-lookup (Funktion ist weiter unten im Modul
    definiert — beim ersten Tick nach dem Sleep längst da)."""
    import threading as _threading

    def _loop() -> None:
        import time as _time
        # Erster Lauf nach 3 min — Boot-Phase (Worker-Start, Stale-Reset,
        # Navidrome-Erreichbarkeit) nicht mit Subsonic-Calls belasten.
        _time.sleep(180)
        while True:
            try:
                recon = _reconcile_imported_playlists()
                if recon.get("tracks_added", 0) > 0:
                    print(
                        f"[playlist-reconcile] {recon['tracks_added']} tracks "
                        f"added across {recon['playlists']} playlists"
                    )
            except Exception as e:
                print(f"[playlist-reconcile] WARN: {type(e).__name__}: {e}")
            _time.sleep(config.PLAYLIST_RECONCILE_INTERVAL_S)

    t = _threading.Thread(target=_loop, daemon=True, name="playlist-reconcile")
    t.start()
    print(
        "Playlist reconcile: background thread started "
        f"(first run after 180s, then every {config.PLAYLIST_RECONCILE_INTERVAL_S}s)"
    )


_start_playlist_reconcile_background()


def physical_track_file_exists(
    track_info: dict,
    location: str,
    output_format: str,
    navidrome_library_path: Optional[str] = None,
) -> bool:
    """True if an audio file for this track already exists at the target location."""
    if location == "local":
        root = get_download_path(track_info, config.DOWNLOAD_DIR, output_format)
        if os.path.isfile(root):
            return True
        temp_dir = os.path.join(config.DOWNLOAD_DIR, "temp")
        temp_p = get_download_path(track_info, temp_dir, output_format)
        return os.path.isfile(temp_p)
    if location == "navidrome":
        root = resolve_navidrome_library_path_optional(navidrome_library_path)
        return navidrome_service.track_file_exists(track_info, output_format, root)
    return False


def get_duplicate_download_reason(
    track_id: str,
    provider: str,
    location: str,
    output_format: str,
    track_info: Optional[dict] = None,
    navidrome_library_path: Optional[str] = None,
) -> Optional[str]:
    """None if download is allowed; otherwise a short message for HTTP 409."""
    job = get_job(track_id)
    if job and job.get("status") in ("queued", "processing"):
        return "A download is already in progress for this track."

    if track_info is None:
        svc = deezer_service if provider == "deezer" else spotify_service
        if svc is None:
            return None
        track_info = svc.get_track_details(track_id)
    if not track_info:
        return None

    if physical_track_file_exists(track_info, location, output_format, navidrome_library_path):
        return "This track is already in your library."

    # Zusätzlich: Navidrome-Subsonic-Check für Tracks die außerhalb von tonus
    # in die Library gekommen sind (manuelle Imports, alte Sync-Aktionen, andere Tools).
    # Best-effort — Network-Errors zählen NICHT als Duplikat.
    try:
        artist = (track_info.get("artist") or "").strip()
        title = (track_info.get("name") or "").strip()
        if artist and title and navidrome_service.library_has_track(artist, title):
            return "This track is already in your Navidrome library."
    except Exception as e:
        print(f"[dup-check] Navidrome lookup skipped due to error: {e}")

    return None


# Request models
class SearchRequest(BaseModel):
    query: str
    limit: Optional[int] = 20
    provider: Optional[str] = None  # "deezer" | "spotify"


class DownloadRequest(BaseModel):
    track_id: str
    location: Optional[str] = "local"  # 'local' or 'navidrome'
    video_id: Optional[str] = None  # YouTube video ID if user selected a specific candidate
    format: Optional[str] = None  # Audio format (mp3, m4a, opus, ogg, flac)
    quality: Optional[str] = None  # Audio quality/bitrate (e.g., "128", "192", "256", "320")
    provider: Optional[str] = None  # "deezer" | "spotify"
    max_retries: Optional[int] = 0  # Extra attempts if YouTube download fails (0–5)
    navidrome_library: Optional[str] = None  # Absolute path; must match server config (see GET /api/navidrome/libraries)
    track_hint: Optional[Dict] = None  # Optional pre-known {name, artist, album, album_art} so the queue can render without an extra provider lookup


class AlbumDownloadRequest(BaseModel):
    album_id: str
    location: Optional[str] = "local"  # 'local' or 'navidrome'
    format: Optional[str] = None  # Audio format (mp3, m4a, opus, ogg, flac)
    quality: Optional[str] = None  # Audio quality/bitrate (e.g., "128", "192", "256", "320")
    provider: Optional[str] = None  # "deezer" | "spotify"
    max_retries: Optional[int] = 0  # Extra attempts per track if YouTube download fails (0–5)
    navidrome_library: Optional[str] = None


class ArtistDownloadRequest(BaseModel):
    artist_id: str
    location: Optional[str] = "local"  # 'local' or 'navidrome'
    format: Optional[str] = None
    quality: Optional[str] = None
    provider: Optional[str] = None  # "deezer" (only provider supported for now)
    max_retries: Optional[int] = 0  # Extra attempts per track if a download fails (0–5)
    navidrome_library: Optional[str] = None
    include_singles: Optional[bool] = False  # include record_type 'single'
    include_compilations: Optional[bool] = False  # include record_type 'compilation' (best-of/samplers)


class ReverseLookupRequest(BaseModel):
    url: str
    provider: Optional[str] = None  # "deezer" | "spotify"


class ReverseDownloadRequest(BaseModel):
    youtube_url: str
    location: Optional[str] = "local"  # 'local' or 'navidrome'
    spotify_track_id: Optional[str] = None  # Catalog track id (Spotify or Deezer depending on provider)
    metadata: Optional[Dict] = None
    provider: Optional[str] = None  # "deezer" | "spotify"
    navidrome_library: Optional[str] = None

class RecommendationRequest(BaseModel):
    track_id: str
    limit: int = 20


class CsvImportRequest(BaseModel):
    csv_text: str
    provider: Optional[str] = None
    limit: Optional[int] = 5  # Deezer search results per track, max 5
    # Optional: original-Filename für UI-Anzeige (Tab zeigt sonst nur job_id).
    # Liefert das Frontend bei File-Upload mit; bei Text-Paste bleibt's null.
    filename: Optional[str] = None
    # Mode-Switch (2026-05-10):
    #   None / "full"          → klassischer Bulk-Import: Phase 0..5
    #   "playlist_sync"        → Library-Match + Playlist-Reconcile only.
    #                            Skip Provider-Phase + Download. Use case:
    #                            User hat Tracks bereits via Bulk-Import,
    #                            will jetzt nur die Playlist-Memberships aus
    #                            einer CSV (TuneMyMusic / Spotify-Export)
    #                            in seine Navidrome-Playlists einreihen.
    mode: Optional[str] = None


class URLDownloadRequest(BaseModel):
    """Phase 1: direkter Download einer beliebigen yt-dlp-URL (YouTube/SoundCloud/...)

    v0.5.0: Playlist-URLs (SoundCloud-Sets, YouTube-Playlists) werden erkannt
    und zu N einzelnen Worker-Jobs expandiert. `as_navidrome_playlist`
    steuert, ob die Tracks zusätzlich einen Playlist-Marker bekommen —
    _reconcile_imported_playlists baut daraus die gleichnamige Subsonic-
    Playlist in Navidrome. Bei Single-Track-URLs wird das Flag ignoriert."""
    url: str
    location: Optional[str] = "local"
    format: Optional[str] = None
    quality: Optional[str] = None
    max_retries: Optional[int] = 0
    navidrome_library: Optional[str] = None
    as_navidrome_playlist: Optional[bool] = True


class URLProbeRequest(BaseModel):
    """Leichtgewichtiger Probe-Call (v0.5.0): Frontend fragt beim Paste, ob
    die URL eine Playlist ist, um die Playlist-UI (Track-Count + Navidrome-
    Toggle) einzublenden. flat-extract, kein Download."""
    url: str


class URLSearchRequest(BaseModel):
    """Phase 2: Multi-Source Smart-Search (Tonus 0.2.0+).

    Hard Cutover ab 0.2.0: `source`-Param ist DEPRECATED und wird ignoriert.
    Alle in `ENABLED_SOURCES` (config.py) konfigurierten Quellen werden
    parallel durchsucht — Default `youtube,soundcloud,bandcamp`. Plugin-
    Konsumenten mit fest verdrahtetem `source` bekommen eine Deprecation-
    Note im Response-Body unter `_deprecation`."""
    query: str
    source: Optional[str] = None  # DEPRECATED 0.2.0: ignored, multi-source-search runs always
    limit: Optional[int] = 10


class PluginMixDiscoveryRequest(BaseModel):
    """Trigger-Body für /api/plugin/mix/discovery — vom Navidrome-Plugin
    (Mix-Discovery-Job) gepostet, einmal pro Genre-Mix.

    Anders als `/api/plugin/sync` (klassischer LB-Top-Artist-Pfad) holt dieser
    Endpoint Tracks per Genre-Tag aus den ListenBrainz-Charts. Workflow:
      1. LB-Top-Recordings pro Genre fetchen (über `count` × Faktor 2 als
         Pool, damit nach Library-Dedup genug übrig bleiben)
      2. Pro Track: in-Library-Check via Navidrome-Subsonic-search3
      3. Existierende Tracks → Liste zurück (für Plugin-Build-Phase)
      4. Fehlende Tracks → in download_jobs queuen mit Mix-Marker im Payload
         (damit `/api/plugin/finished-tracks` sie später dem Mix zuordnen kann)

    `mix_name` + `navidrome_user` werden als Marker im Payload jedes gequeuten
    Tracks gespeichert, damit beim späteren Build-Job nur die richtigen Tracks
    in die Mix-Playlist gehen.
    """
    navidrome_user: str
    mix_name: str
    genre: str
    count: int = 25
    discovery_ratio: float = 0.4   # 0.0 = nur familiars, 1.0 = nur new


class PluginLbWeeklyDiscoveryRequest(BaseModel):
    """Trigger-Body für /api/plugin/lbweekly/discovery — eine der vier
    LB-'createdfor'-Playlists pro Call. Quelle ist eine fertig kuratierte
    LB-Playlist (source_patch + occurrence), nicht artist-radio.

    Gequeute Tracks tragen die BESTEHENDEN sync-Marker
    (plugin_sync_playlist_name + plugin_sync_navidrome_user), damit der
    vorhandene finished-tracks/Build-Pfad sie unverändert verarbeitet.
    """
    navidrome_user: str
    listenbrainz_user: str
    source_patch: str            # "weekly-exploration" | "weekly-jams"
    occurrence: int = 0          # 0 = aktuelle Woche, 1 = "Last Week's …"
    playlist_name: str           # Navidrome-Playlist-Name (z.B. "Weekly Exploration")
    location: str = "navidrome"
    max_tracks: int = 60


# Response models
class TrackResponse(BaseModel):
    id: str
    name: str
    artist: str
    artists: List[str]
    album: str
    duration_ms: int
    external_url: str
    preview_url: Optional[str]
    album_art: Optional[str]
    release_date: str


class DownloadStatusResponse(BaseModel):
    status: str
    message: str
    file_path: Optional[str] = None


def download_and_process(
    track_id: str,
    location: str = "local",
    video_id: str = None,
    output_format: str = None,
    audio_quality: str = None,
    metadata_provider: str = "deezer",
    max_retries: int = 0,
    navidrome_library_path: Optional[str] = None,
    source_lane: Optional[str] = None,
):
    """Background task to download and process a track (called by JobWorker under its lock).

    navidrome_library_path: resolved absolute root when location is navidrome (must be allowlisted).
    source_lane: "a"|"b"|None → bei aktivem Dual-VPN-Splitting wird die Source-IP
        des Containers für Deezer-Detail-Calls und yt-dlp-Downloads gepinnt. None
        bedeutet System-Default-Routing (= Lane B = eth1 im aktuellen Setup).
    """
    # Use provided format/quality or fall back to config defaults
    output_format = output_format or config.OUTPUT_FORMAT
    audio_quality = audio_quality or config.AUDIO_QUALITY

    # Deezer/Spotify-Source-Param ist "default" wenn keine Lane übergeben wurde —
    # bit-identisch zum Status quo. yt-dlp bekommt None (kein source_address).
    deezer_source = source_lane if source_lane in ("a", "b") else "default"

    try:
        svc = deezer_service if metadata_provider == "deezer" else spotify_service
        if svc is None:
            upsert_job(
                track_id,
                status="error",
                message="Spotify is not configured",
                progress=0,
            )
            return

        upsert_job(
            track_id,
            status="processing",
            message="Fetching track info...",
            stage="fetching",
            progress=10,
        )

        # SpotifyService.get_track_details kennt keinen source-Param — nur Deezer
        # ist heute lane-aware. Spotify bleibt auf Default-Routing.
        if metadata_provider == "deezer":
            track_info = svc.get_track_details(track_id, source=deezer_source)
        else:
            track_info = svc.get_track_details(track_id)
        if not track_info:
            upsert_job(track_id,
                status="error",
                message="Could not fetch track information",
                progress=0
                )
            return

        upsert_job(track_id, status="processing", message="Preparing download location...", stage="preparing",
            progress=15)

        # Determine download path based on location preference
        if location == "navidrome":
            # Download directly to Navidrome music directory with proper structure (Artist/Album/)
            # First download to temp location, then copy to Navidrome directory
            temp_dir = os.path.join(config.DOWNLOAD_DIR, "temp")
            Path(temp_dir).mkdir(parents=True, exist_ok=True)
            download_path = get_download_path(track_info, temp_dir, output_format)
            print(f"Downloading track {track_id} for Navidrome: {download_path}")
        else:
            # For local downloads: download to temp folder, then serve via browser download
            # This allows each user's browser to save to their own Downloads folder
            temp_dir = os.path.join(config.DOWNLOAD_DIR, "temp")
            Path(temp_dir).mkdir(parents=True, exist_ok=True)
            download_path = get_download_path(track_info, temp_dir, output_format)
            print(f"Downloading track {track_id} for local browser download: {download_path}")

        upsert_job(track_id,
            status="processing",
            message="Searching YouTube and downloading...",
            stage="downloading",
            progress=30)

        extra = _clamp_download_retries(max_retries)
        max_attempts = 1 + extra
        download_result = None
        last_err = "Unknown error"
        for attempt in range(max_attempts):
            if attempt > 0:
                delay_sec = min(8, 2 ** (attempt - 1))
                upsert_job(
                    track_id,
                    status="processing",
                    message=f"Download failed, retrying in {delay_sec}s (attempt {attempt + 1}/{max_attempts})...",
                    stage="downloading",
                    progress=30,
                )
                time.sleep(delay_sec)
                upsert_job(
                    track_id,
                    status="processing",
                    message="Searching YouTube and downloading...",
                    stage="downloading",
                    progress=30,
                )
            download_result = youtube_service.search_and_download(
                track_info['name'],
                track_info['artist'],
                download_path,
                track_info,
                video_id,
                output_format,
                audio_quality,
                source_lane=source_lane,
            )
            if download_result.get("success"):
                break
            last_err = download_result.get("error", "Unknown error")

        if not download_result or not download_result.get("success"):
            upsert_job(
                track_id,
                status="error",
                message=f"Download failed: {last_err}",
                progress=0,
            )
            return

        # Multi-Source-Resolver hat used_source/used_url/match_score/yt_actual_title
        # in download_result eingehängt — in payload mergen damit das Frontend
        # die genutzte Source in der Origin-Pill anzeigen kann.
        #
        # yt_actual_title wird unabhängig vom used_source-Pfad gemerged (auch
        # der Legacy-Fallback setzt ihn), damit Rename-Tools nach einem
        # Falschmatch den echten YT/SC-Titel haben.
        used_source = download_result.get("used_source")
        yt_actual_title = download_result.get("yt_actual_title")
        if used_source or yt_actual_title:
            existing_job = get_job(track_id) or {}
            merged_payload = dict(existing_job.get("payload") or {})
            if used_source:
                merged_payload["used_source"] = used_source
            if download_result.get("used_url"):
                merged_payload["used_url"] = download_result["used_url"]
            if download_result.get("match_score") is not None:
                merged_payload["match_score"] = download_result["match_score"]
            if yt_actual_title:
                merged_payload["yt_actual_title"] = yt_actual_title
            upsert_job(
                track_id,
                status="processing",
                message="Applying metadata...",
                stage="tagging",
                progress=85,
                payload=merged_payload,
            )
        else:
            upsert_job(track_id,
                status="processing",
                message="Applying metadata...",
                stage="tagging",
                progress=85)

        # Apply metadata to downloaded file
        metadata_service.apply_metadata(download_result['file_path'], track_info)

        # Handle completion based on location
        if location == "navidrome":
            # Copy to Navidrome music directory with proper structure (Artist/Album/)
            upsert_job(track_id,
                status="processing",
                message="Copying to Navidrome library...",
                stage="copying",
                progress=90)

            try:
                # Get target path in Navidrome directory (extension matches chosen format, e.g. .flac)
                target_path = navidrome_service.get_target_path(
                    track_info, output_format, navidrome_library_path
                )

                # Copy file to Navidrome directory
                shutil.copy2(download_result['file_path'], target_path)

                # Clean up temp file
                if os.path.exists(download_result['file_path']):
                    os.remove(download_result['file_path'])

                # Trigger Navidrome scan
                navidrome_result = navidrome_service.finalize_track(str(target_path))

                if navidrome_result.get('success'):
                    upsert_job(track_id,
                        status="completed",
                        message="Track successfully added to Navidrome library",
                        file_path=str(target_path),
                        stage="completed",
                        progress=100
                        )
                    record_completed_download(track_id, metadata_provider)

                else:
                    upsert_job(track_id,
                        status="completed",
                        message=f"Track added to library (scan may need manual trigger): {navidrome_result.get('error', '')}",
                        file_path=str(target_path),
                        stage="completed",
                        progress=100
                        )
                    record_completed_download(track_id, metadata_provider)

            except Exception as e:
                upsert_job(track_id,
                    status="error",
                    message=f"Failed to copy to Navidrome: {str(e)}",
                    progress=0
                    )
        else:
            # For local downloads, provide download URL for browser to handle
            # The file is ready, browser will download it to user's Downloads folder
            filename = os.path.basename(download_result['file_path'])
            # URL encode the filename to handle special characters (use query parameter)
            encoded_filename = quote(filename, safe='')
            download_url = f"api/download/file/{track_id}?filename={encoded_filename}"
            upsert_job(track_id,
                status="completed",
                message="Track ready for download",
                file_path=download_result['file_path'],
                download_url=download_url,  # URL to trigger browser download
                stage="completed",
                progress=100
                )

        # Hinweis: Cool-down zwischen Jobs läuft jetzt im Worker (worker.py),
        # damit auch ERROR-Jobs eine Pause haben — sonst rennt ein Retry direkt
        # in den nächsten 429.

    except Exception as e:
        upsert_job(track_id,
            status="error",
            message=f"Error: {str(e)}",
            progress=0
            )


@app.middleware("http")
async def add_root_path(request: Request, call_next):
    root_path = request.headers.get("X-Forwarded-Prefix", "")
    request.scope["root_path"] = root_path
    return await call_next(request)


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    """Standard-Security-Header für jede Response (Audit M-2, 2026-05-12).

    Vier No-Brainer-Header die nicht die UI brechen können:
      - X-Content-Type-Options: nosniff — blockt MIME-Type-Sniffing-Attacks
      - X-Frame-Options: DENY — blockt Clickjacking via iframe-Embedding
      - Referrer-Policy: strict-origin-when-cross-origin — limitiert Referer-Leak
      - Permissions-Policy — disabled Browser-APIs die Tonus nicht braucht
    CSP bleibt bewusst weg — würde SvelteKit-inline-styles brechen und braucht
    eigenes Tuning (eigenes Backlog-Item). HSTS macht Sinn nur HINTER TLS-Proxy
    (Traefik) und wird vom Proxy selbst gesetzt — hier kein zweites Set.
    """
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault(
        "Permissions-Policy",
        "geolocation=(), microphone=(), camera=(), payment=(), usb=()",
    )
    return response


@app.get("/api/metadata/providers")
async def metadata_providers():
    """Available metadata sources and server default."""
    return {
        "default": config.DEFAULT_METADATA_PROVIDER,
        "providers": [
            {"id": "deezer", "label": "Deezer", "configured": True},
            {
                "id": "spotify",
                "label": "Spotify",
                "configured": spotify_service is not None,
            },
        ],
    }


@app.post("/api/search", response_model=List[TrackResponse])
async def search_tracks(request: SearchRequest):
    """Search for tracks using the selected catalog provider."""
    provider = resolve_metadata_provider(request.provider)
    svc = get_metadata_service(provider)
    try:
        return svc.search_tracks(request.query, request.limit or 20)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Search failed: {str(e)}")


@app.post("/api/reverse/youtube")
async def reverse_lookup_youtube(request: ReverseLookupRequest):
    """Given a YouTube URL, extract title and search the selected catalog."""
    provider = resolve_metadata_provider(request.provider)
    svc = get_metadata_service(provider)
    try:
        yt_info = youtube_service.extract_video_info(request.url)
        if not yt_info.get('success'):
            raise HTTPException(status_code=400,
                                detail=f"Failed to read YouTube URL: {yt_info.get('error', 'Unknown error')}")

        title = (yt_info.get('title') or '').strip()
        if not title:
            raise HTTPException(status_code=400, detail="YouTube title was empty")

        candidates = svc.search_tracks(title, limit=5)

        return {
            "youtube": yt_info,
            "query": title,
            "spotify_candidates": candidates,
            "provider": provider,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Reverse lookup failed: {str(e)}")


@app.post("/api/search/tracks/top")
async def search_tracks_top(request: SearchRequest):
    """Search tracks with a small default limit (pick-lists)."""
    provider = resolve_metadata_provider(request.provider)
    svc = get_metadata_service(provider)
    try:
        limit = request.limit or 5
        limit = max(1, min(int(limit), 10))
        return svc.search_tracks(request.query, limit=limit)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Search failed: {str(e)}")


@app.post("/api/search/albums")
async def search_albums(request: SearchRequest):
    """Search for albums."""
    provider = resolve_metadata_provider(request.provider)
    svc = get_metadata_service(provider)
    try:
        return svc.search_albums(request.query, request.limit or 20)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Album search failed: {str(e)}")


@app.post("/api/search/artists")
async def search_artists(request: SearchRequest):
    """Search for artists (for the artist card / "download all" flow)."""
    provider = resolve_metadata_provider(request.provider)
    svc = get_metadata_service(provider)
    if not hasattr(svc, "search_artists"):
        raise HTTPException(
            status_code=400,
            detail=f"Artist search is not supported for provider '{provider}' yet.",
        )
    try:
        return svc.search_artists(request.query, request.limit or 8)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Artist search failed: {str(e)}")


@app.get("/api/album/{album_id}")
async def get_album(album_id: str, provider: Optional[str] = Query(None)):
    """Get album details including all tracks"""
    p = resolve_metadata_provider(provider)
    svc = get_metadata_service(p)
    try:
        album = svc.get_album_details(album_id)
        if not album:
            raise HTTPException(status_code=404, detail="Album not found")
        return album
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching album: {str(e)}")


@app.get("/api/track/{track_id}", response_model=TrackResponse)
async def get_track(track_id: str, provider: Optional[str] = Query(None)):
    """Get details for a specific track"""
    p = resolve_metadata_provider(provider)
    svc = get_metadata_service(p)
    try:
        track = svc.get_track_details(track_id)
        if not track:
            raise HTTPException(status_code=404, detail="Track not found")
        return track
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching track: {str(e)}")


@app.get("/api/queue/lanes")
async def queue_lanes(_: None = Depends(require_token)):
    """Lane-Cooldown-Status für die Live-Queue-UI.

    Liefert pro Lane (`a`/`b` mit VPN-Split, sonst `default`) den Timestamp
    bis zur nächsten Verfügbarkeit + die Restzeit in ms. Plus die globalen
    Cooldown-Bereiche, damit das Frontend "Random zwischen X und Y Sekunden"
    transparent machen kann.
    """
    return _download_worker.lane_status()


@app.get("/api/queue")
async def list_queue(
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=500, ge=1, le=10000),
    status: Optional[str] = Query(default=None),
    _: None = Depends(require_token),
):
    """Return queued/active/recent jobs (paginiert).

    Default-Limit ist 500 — bei größeren Bulk-Imports (CSV mit 15k+ Tracks)
    laggt das UI sonst weil das Frontend pro Polling-Tick alle Items als
    DOM-Nodes rendert. Die Standard-Frontend-Ansicht ist der `active`-Filter
    (status='queued,processing'), der typischerweise nur 50–200 Items
    enthält. Completed/error-Listen lädt das UI nur on-demand.

    Optionaler offset für echte Pagination, optionaler status-Filter
    (csv-werte 'queued','processing','completed','error' oder kombiniert
    wie 'queued,processing'). `status_counts` zählt IMMER über ALLE
    Statuses, unabhängig vom Filter — so weiß das UI wie viele Jobs in
    jedem Bucket liegen ohne separat /api/queue/stats zu pollen.
    """
    import json as _json
    from utils.job_store import _db as _queue_db

    allowed_statuses = {'queued', 'processing', 'completed', 'error'}
    if status:
        wanted = {s.strip() for s in status.split(',') if s.strip() in allowed_statuses}
    else:
        wanted = allowed_statuses
    if not wanted:
        wanted = allowed_statuses
    placeholders = ",".join("?" * len(wanted))

    conn = _queue_db()
    try:
        total = conn.execute(
            f"SELECT COUNT(*) AS n FROM download_jobs WHERE status IN ({placeholders})",
            tuple(wanted),
        ).fetchone()["n"]

        # Sortierung in zwei Gruppen:
        #   1) processing + queued: FIFO ASC — ältester ist als nächstes zu
        #      pullen (matched Worker-ORDER BY created_at_ms ASC, rowid ASC).
        #      Beim CSV-Bulk-Import landen viele Jobs in derselben Millisekunde,
        #      also MUSS der Tiebreaker rowid ASC sein damit die UI denselben
        #      "next pull" anzeigt den der Worker tatsächlich zieht.
        #   2) completed + error: DESC — neueste Erlebnisse zuerst, klassischer
        #      Newsfeed-Style fürs Aufräumen / Debuggen.
        rows = conn.execute(
            f"""
            SELECT job_id, status, stage, progress, message, download_url,
                   created_at_ms, updated_at_ms, payload_json, rowid AS _rowid
            FROM download_jobs
            WHERE status IN ({placeholders})
            ORDER BY
              CASE status
                WHEN 'processing' THEN 1
                WHEN 'queued'     THEN 2
                WHEN 'completed'  THEN 3
                WHEN 'error'      THEN 4
                ELSE 5
              END ASC,
              CASE WHEN status IN ('queued', 'processing') THEN created_at_ms END ASC,
              CASE WHEN status IN ('queued', 'processing') THEN _rowid END ASC,
              CASE WHEN status IN ('completed', 'error')   THEN created_at_ms END DESC,
              CASE WHEN status IN ('completed', 'error')   THEN _rowid END DESC
            LIMIT ? OFFSET ?
            """,
            (*wanted, limit, offset),
        ).fetchall()

        items = []
        for row in rows:
            payload = None
            if row["payload_json"]:
                try:
                    payload = _json.loads(row["payload_json"])
                except (_json.JSONDecodeError, TypeError):
                    pass

            items.append(
                {
                    "job_id": row["job_id"],
                    "status": row["status"],
                    "stage": row["stage"],
                    "progress": row["progress"],
                    "message": row["message"],
                    "download_url": row["download_url"],
                    "created_at_ms": row["created_at_ms"],
                    "updated_at_ms": row["updated_at_ms"],
                    "payload": payload,
                }
            )
        # Status-Aggregat über ALLE Statuses, nicht nur den gefilterten —
        # das UI braucht alle Counts für die Filter-Pills auch wenn der
        # aktuelle Filter z.B. nur 'queued,processing' anfragt.
        agg_rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM download_jobs GROUP BY status",
        ).fetchall()
        status_counts = {r["status"]: r["n"] for r in agg_rows}

        # Live-Block: alle processing-Jobs + Top-N queued-Jobs (FIFO).
        # Wird IMMER mitgeliefert, unabhängig vom User-Filter — damit die
        # Lane-Strip auch beim "Done"- oder "Error"-Filter sichtbar bleibt
        # ("Was läuft gerade" + "Was kommt als nächstes" sollen nie
        # ausgeblendet werden). N=4 reicht für 2 Lanes (je 1 running +
        # 1 upNext) plus etwas Puffer.
        def _row_to_item(r):
            payload = None
            if r["payload_json"]:
                try:
                    payload = _json.loads(r["payload_json"])
                except (_json.JSONDecodeError, TypeError):
                    pass
            return {
                "job_id": r["job_id"],
                "status": r["status"],
                "stage": r["stage"],
                "progress": r["progress"],
                "message": r["message"],
                "download_url": r["download_url"],
                "created_at_ms": r["created_at_ms"],
                "updated_at_ms": r["updated_at_ms"],
                "payload": payload,
            }

        proc_rows = conn.execute(
            """
            SELECT job_id, status, stage, progress, message, download_url,
                   created_at_ms, updated_at_ms, payload_json
            FROM download_jobs
            WHERE status = 'processing'
            ORDER BY created_at_ms ASC, rowid ASC
            """
        ).fetchall()
        head_rows = conn.execute(
            """
            SELECT job_id, status, stage, progress, message, download_url,
                   created_at_ms, updated_at_ms, payload_json
            FROM download_jobs
            WHERE status = 'queued'
            ORDER BY created_at_ms ASC, rowid ASC
            LIMIT 4
            """
        ).fetchall()
        live = {
            "processing": [_row_to_item(r) for r in proc_rows],
            "queued_head": [_row_to_item(r) for r in head_rows],
        }

        return {
            "items": items,
            "total": total,
            "shown": len(items),
            "offset": offset,
            "limit": limit,
            "status_counts": status_counts,
            "live": live,
        }
    finally:
        conn.close()


@app.get("/api/queue/stats")
async def queue_stats(_: None = Depends(require_token)):
    """Aggregat-Statistik für Dashboards/Health-Checks.

    Liefert:
      - total              Anzahl aller Jobs in non-terminal-und-terminal Statuses
      - by_status          Counter pro Status
      - oldest_queued_age_s   Sekunden seit ältester queued/processing-Job angelegt
      - avg_completed_dur_s   Mittlere Job-Dauer (created→updated) für completed Jobs der letzten 24h
      - last_completed_ms     Timestamp des zuletzt erfolgreichen Downloads
      - last_error_ms         Timestamp des zuletzt gefehlten Jobs
    """
    from utils.job_store import _db as _stats_db
    now = _now_ms()
    conn = _stats_db()
    try:
        agg = conn.execute(
            "SELECT status, COUNT(*) AS n FROM download_jobs GROUP BY status"
        ).fetchall()
        by_status = {r["status"]: r["n"] for r in agg}
        total = sum(by_status.values())

        oldest = conn.execute(
            "SELECT MIN(created_at_ms) AS m FROM download_jobs WHERE status IN ('queued','processing')"
        ).fetchone()
        oldest_age = ((now - oldest["m"]) / 1000.0) if oldest and oldest["m"] else 0

        # Mittlere Dauer der completed Jobs der letzten 24h (in s)
        cutoff = now - 24 * 3600 * 1000
        avg_dur = conn.execute(
            "SELECT AVG((updated_at_ms - created_at_ms) / 1000.0) AS d "
            "FROM download_jobs WHERE status='completed' AND updated_at_ms >= ?",
            (cutoff,),
        ).fetchone()
        avg_completed = float(avg_dur["d"] or 0) if avg_dur else 0.0

        last_ok = conn.execute(
            "SELECT MAX(updated_at_ms) AS m FROM download_jobs WHERE status='completed'"
        ).fetchone()
        last_err = conn.execute(
            "SELECT MAX(updated_at_ms) AS m FROM download_jobs WHERE status='error'"
        ).fetchone()

        return {
            "total": total,
            "by_status": by_status,
            "oldest_queued_age_s": round(oldest_age, 1),
            # Mittlere Wall-Clock-Zeit von Job-Anlage bis Completed (= Queue-Wartezeit + Download).
            # Bei langer Queue oder Re-Tries kann das hoch sein — kein reines "Download-Tempo".
            "avg_created_to_completed_s": round(avg_completed, 1),
            "last_completed_ms": (last_ok["m"] if last_ok else None),
            "last_error_ms": (last_err["m"] if last_err else None),
            "now_ms": now,
        }
    finally:
        conn.close()


class QueueMoveRequest(BaseModel):
    track_id: str
    direction: str  # "up" | "down"


@app.post("/api/queue/move")
async def move_queue_item(req: QueueMoveRequest, _: None = Depends(require_token)):
    """Tauscht Positionen mit Nachbar via created_at_ms (atomarer Swap)."""
    from utils.job_store import _db as _move_db
    conn = _move_db()
    try:
        # Finde den aktuellen Job
        current = conn.execute(
            "SELECT created_at_ms FROM download_jobs WHERE job_id=? AND status IN ('queued','processing')",
            (req.track_id,)
        ).fetchone()
        if not current:
            raise HTTPException(404, "Queued job not found")

        # Sortierung: Queue zeigt DESC nach created_at_ms (neueste oben)
        # "up" = nach oben schieben = created_at_ms erhöhen (neuer)
        # "down" = nach unten schieben = created_at_ms verringern (älter)
        if req.direction == "up":
            neighbor = conn.execute(
                """
                SELECT job_id, created_at_ms FROM download_jobs
                WHERE status IN ('queued','processing')
                  AND job_id != ?
                  AND created_at_ms > ?
                ORDER BY created_at_ms ASC
                LIMIT 1
                """,
                (req.track_id, current["created_at_ms"]),
            ).fetchone()
        else:
            neighbor = conn.execute(
                """
                SELECT job_id, created_at_ms FROM download_jobs
                WHERE status IN ('queued','processing')
                  AND job_id != ?
                  AND created_at_ms < ?
                ORDER BY created_at_ms DESC
                LIMIT 1
                """,
                (req.track_id, current["created_at_ms"]),
            ).fetchone()

        if not neighbor:
            return {"ok": False, "reason": "no neighbor"}

        # Atomarer Swap: beide created_at_ms tauschen
        cur_ts = current["created_at_ms"]
        nei_ts = neighbor["created_at_ms"]
        # Nutze -1 als temporärer Platzhalter, um UNIQUE-Kollision zu vermeiden
        conn.execute(
            "UPDATE download_jobs SET created_at_ms = -1 WHERE job_id = ?",
            (req.track_id,),
        )
        conn.execute(
            "UPDATE download_jobs SET created_at_ms = ? WHERE job_id = ?",
            (cur_ts, neighbor["job_id"]),
        )
        conn.execute(
            "UPDATE download_jobs SET created_at_ms = ? WHERE job_id = ?",
            (nei_ts, req.track_id),
        )
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


@app.post("/api/download")
async def download_track(request: DownloadRequest, background_tasks: BackgroundTasks, _: None = Depends(require_token), __: None = Depends(_rl_track_download)):
    """Start downloading a track"""
    if request.location not in ["local", "navidrome"]:
        request.location = "local"

    provider = resolve_metadata_provider(request.provider)
    get_metadata_service(provider)

    output_format = request.format or config.OUTPUT_FORMAT
    navidrome_path: Optional[str] = None
    if request.location == "navidrome":
        navidrome_path = resolve_navidrome_library_path_optional(request.navidrome_library)

    dup = get_duplicate_download_reason(
        request.track_id,
        provider,
        request.location,
        output_format,
        navidrome_library_path=navidrome_path,
    )
    if dup:
        raise HTTPException(status_code=409, detail=dup)

    location_msg = "local downloads folder" if request.location == "local" else "Navidrome server"
    track_for_queue = _resolve_track_for_queue(request.track_id, provider, request.track_hint)
    upsert_job(
        request.track_id,
        status="queued",
        message=f"Download queued for {location_msg}",
        progress=0,
        stage="queued",
        payload={
            "provider": provider,
            "record_track_id": request.track_id,
            "location": request.location,
            "video_id": request.video_id,
            "output_format": output_format,
            "audio_quality": request.quality,
            "metadata_provider": provider,
            "max_retries": _clamp_download_retries(request.max_retries),
            "navidrome_library_path": navidrome_path,
            "track": track_for_queue,
        },
    )
    # Worker thread picks up the job from SQLite — no background_tasks needed here

    return {
        "status": "queued",
        "message": f"Download started to {location_msg}",
        "track_id": request.track_id,
    }


def reverse_download_and_process(
    job_id: str,
    youtube_url: str,
    location: str,
    track_id: Optional[str],
    metadata: Optional[Dict],
    metadata_provider: str = "deezer",
    navidrome_library_path: Optional[str] = None,
):
    """Background task: download a specific YouTube URL and tag using catalog or manual metadata."""
    try:
        upsert_job(job_id, status="processing", message="Extracting YouTube info...", stage="fetching", progress=10)

        yt_info = youtube_service.extract_video_info(youtube_url)
        if not yt_info.get('success'):
            upsert_job(job_id, status="error",
                       message=f"Failed to read YouTube URL: {yt_info.get('error', 'Unknown error')}", progress=0)
            return

        video_id = yt_info.get('video_id')
        if not video_id:
            upsert_job(job_id, status="error", message="Could not determine YouTube video id", progress=0)
            return

        track_info: Optional[Dict] = None
        if track_id:
            svc = deezer_service if metadata_provider == "deezer" else spotify_service
            if svc is None:
                upsert_job(job_id, status="error", message="Spotify is not configured", progress=0)
                return
            upsert_job(job_id, status="processing", message="Fetching track info...", stage="fetching",
                       progress=20)
            track_info = svc.get_track_details(track_id)
            if not track_info:
                upsert_job(job_id, status="error", message="Could not fetch track information", progress=0)
                return
        else:
            # Kein Spotify/Deezer-Track-ID UND keine manuellen Metadaten →
            # "Direkt laden ohne Match"-Fall. Vorher hat der Code hier hart
            # mit "Manual metadata requires 'name' (song title) and 'artist'"
            # abgebrochen — dabei sind YouTube-Title + Uploader + Thumbnail
            # vom yt-dlp-extract genau das was der User in diesem Pfad will.
            #
            # Logik:
            #   1. Manuelle Metadaten haben Vorrang (User-Eingaben)
            #   2. Falls fehlend → Fallback auf yt-dlp-Werte
            #   3. Falls weder noch → letzter Fallback "YouTube" / "Unknown"
            md = metadata or {}
            name = (
                (md.get('name') or md.get('title') or '').strip()
                or (yt_info.get('title') or '').strip()
                or 'Unknown'
            )
            artist = (
                (md.get('artist') or '').strip()
                or (yt_info.get('uploader') or yt_info.get('channel') or '').strip()
                or 'YouTube'
            )

            # Default album/album_artist: album = "Singles" (Navidrome-
            # konventional für Tracks ohne natürliches Album), album_artist
            # = artist (= YouTube-Channel). Manuelle Tags haben weiterhin
            # Vorrang. Damit Pfad <Channel>/Singles/<Title>.opus statt
            # vorher Doppelnest <Channel>/<Channel>/<Title>.opus.
            album_artist = (md.get('album_artist') or '').strip() or artist
            album = (md.get('album') or md.get('album_name') or '').strip() or 'Singles'

            # If user didn't provide album art, use YouTube thumbnail
            album_art = md.get('album_art') or yt_info.get('thumbnail') or None

            track_info = {
                'id': job_id,
                'name': name,
                'artist': artist,
                'artists': [a.strip() for a in re.split(r"[;,]", artist) if a.strip()],
                'album_artist': album_artist,
                'album': album,
                'track_number': int(md.get('track_number') or 1),
                'release_date': (md.get('release_date') or '').strip(),
                'album_art': album_art,
                'duration_ms': int((yt_info.get('duration') or 0) * 1000),
                'external_url': yt_info.get('webpage_url') or youtube_url,
                'preview_url': None,
            }

        # Job-Payload auf den jetzt-resolved Track umstellen, damit die
        # Queue-UI Title + Artist + Cover statt YouTube-URL + leeres Bild
        # zeigt. Bei Catalog-Match kommt track_info aus Provider, bei
        # Direct-ohne-Match aus yt-dlp — in beiden Fällen ist track_info
        # jetzt vollständig.
        slim_track = {
            "id": job_id,
            "name": track_info.get('name'),
            "artist": track_info.get('artist'),
            "album": track_info.get('album'),
            "album_art": track_info.get('album_art'),
        }
        upsert_job(
            job_id,
            status="processing",
            message="Preparing download location...",
            stage="preparing",
            progress=20,
            payload={
                "kind": "reverse",
                "provider": metadata_provider,
                "record_track_id": track_id,
                "track": slim_track,
            },
        )

        # Idempotenz-Check (siehe url_download_and_process): wenn die
        # Ziel-Datei schon existiert, gleich auf "completed" — kein
        # Re-Download, keine "(1)/(2)"-Counter-Files mehr.
        if physical_track_file_exists(track_info, location, config.OUTPUT_FORMAT, navidrome_library_path):
            upsert_job(
                job_id,
                status="completed",
                message="Track is already in your library",
                stage="completed",
                progress=100,
            )
            return

        # Determine download path
        temp_dir = os.path.join(config.DOWNLOAD_DIR, "temp")
        Path(temp_dir).mkdir(parents=True, exist_ok=True)
        download_path = get_download_path(track_info, temp_dir, config.OUTPUT_FORMAT)

        upsert_job(job_id, status="processing", message="Downloading from YouTube...", stage="downloading", progress=40)
        download_result = youtube_service.download_by_video_id(video_id, download_path)
        if not download_result.get('success'):
            upsert_job(job_id, status="error",
                       message=f"Download failed: {download_result.get('error', 'Unknown error')}", progress=0)
            return

        upsert_job(job_id, status="processing", message="Applying metadata...", stage="tagging", progress=80)
        metadata_service.apply_metadata(download_result['file_path'], track_info)

        # Handle completion based on location
        if location == "navidrome":
            upsert_job(job_id, status="processing", message="Copying to Navidrome library...", stage="copying",
                       progress=90)
            try:
                target_path = navidrome_service.get_target_path(
                    track_info, config.OUTPUT_FORMAT, navidrome_library_path
                )
                shutil.copy2(download_result['file_path'], target_path)
                if os.path.exists(download_result['file_path']):
                    os.remove(download_result['file_path'])

                navidrome_result = navidrome_service.finalize_track(str(target_path))
                upsert_job(job_id,
                           status="completed",
                           message="Track successfully added to Navidrome library" if navidrome_result.get(
                               'success') else f"Track added to library (scan may need manual trigger): {navidrome_result.get('error', '')}",
                           file_path=str(target_path),
                           stage="completed",
                           progress=100,
                           )
                if track_id:
                    record_completed_download(track_id, metadata_provider)
            except Exception as e:
                upsert_job(job_id, status="error", message=f"Failed to copy to Navidrome: {str(e)}", progress=0)
        else:
            filename = os.path.basename(download_result['file_path'])
            encoded_filename = quote(filename, safe='')
            download_url = f"api/download/file/{job_id}?filename={encoded_filename}"
            upsert_job(job_id,
                       status="completed",
                       message="Track ready for download",
                       file_path=download_result['file_path'],
                       download_url=download_url,
                       stage="completed",
                       progress=100,
                       )

    except Exception as e:
        upsert_job(job_id, status="error", message=f"Error: {str(e)}", progress=0)


@app.post("/api/reverse/download")
async def reverse_download(request: ReverseDownloadRequest, background_tasks: BackgroundTasks, _: None = Depends(require_token)):
    """Finalize reverse flow: download YouTube URL and tag with chosen track or manual metadata."""
    provider = resolve_metadata_provider(request.provider)
    get_metadata_service(provider)  # validate Spotify configured if needed

    location = request.location if request.location in ["local", "navidrome"] else "local"
    location_msg = "local downloads folder" if location == "local" else "Navidrome server"

    navidrome_path: Optional[str] = None
    if location == "navidrome":
        navidrome_path = resolve_navidrome_library_path_optional(request.navidrome_library)

    if request.spotify_track_id:
        dup = get_duplicate_download_reason(
            request.spotify_track_id,
            provider,
            location,
            config.OUTPUT_FORMAT,
            navidrome_library_path=navidrome_path,
        )
        if dup:
            raise HTTPException(status_code=409, detail=dup)

    job_id = f"yt-{abs(hash((request.youtube_url, request.spotify_track_id or '', location, provider))) % 10_000_000}"

    track_for_queue: Optional[Dict] = None
    if request.spotify_track_id:
        track_for_queue = _resolve_track_for_queue(request.spotify_track_id, provider, None)
    if not track_for_queue and request.metadata:
        track_for_queue = _slim_track_for_queue(request.metadata)
    if not track_for_queue:
        track_for_queue = {"id": job_id, "name": request.youtube_url, "artist": "YouTube", "album": "", "album_art": ""}

    # status="processing" (siehe URL-Download-Endpoint): JobWorker darf den
    # Job nicht parallel als normalen Spotify/Deezer-Download dispatchen,
    # während die BackgroundTask reverse_download_and_process läuft.
    upsert_job(
        job_id,
        status="processing",
        message=f"Reverse download queued for {location_msg}",
        progress=0,
        stage="queued",
        payload={
            "kind": "reverse",
            "provider": provider,
            "record_track_id": request.spotify_track_id,
            "track": track_for_queue,
        },
    )

    background_tasks.add_task(
        reverse_download_and_process,
        job_id,
        request.youtube_url,
        location,
        request.spotify_track_id,
        request.metadata,
        provider,
        navidrome_path,
    )

    return {
        "status": "queued",
        "message": f"Reverse download started to {location_msg}",
        "job_id": job_id,
    }


@app.get("/api/download/status/{track_id}")
async def get_download_status(track_id: str):
    job = get_job(track_id)
    if not job:
        raise HTTPException(status_code=404, detail="Download not found")
    # Keep response shape compatible with old dict
    return {
        "status": job.get("status"),
        "message": job.get("message"),
        "stage": job.get("stage"),
        "progress": job.get("progress"),
        "file_path": job.get("file_path"),
        "download_url": job.get("download_url"),
        "error": job.get("error"),
    }


@app.post("/api/download/{job_id}/cancel")
async def cancel_download(job_id: str, _: None = Depends(require_token)):
    """Cancel a queued or processing job by deleting from DB."""
    conn = _db()
    try:
        conn.execute("DELETE FROM download_jobs WHERE job_id = ?", (job_id,))
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


@app.post("/api/queue/{job_id}/cancel")
async def cancel_queue_item(job_id: str, _: None = Depends(require_token)):
    """Alias — same as POST /api/download/{id}/cancel."""
    return await cancel_download(job_id)


@app.post("/api/queue/{job_id}/start")
async def start_queued_job(job_id: str, _: None = Depends(require_token)):
    """Restart an error job (set status back to queued)."""
    conn = _db()
    try:
        conn.execute(
            "UPDATE download_jobs SET status='queued', updated_at_ms = ? WHERE job_id = ? AND status = 'error'",
            (_now_ms(), job_id),
        )
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


@app.post("/api/queue/retry-all-errors")
async def retry_all_errors(_: None = Depends(require_token)):
    """Bulk-Retry: setzt ALLE Error-Jobs in einem einzigen UPDATE auf 'queued'.

    Ersetzt die alte clientseitige for-Loop, die wegen Pagination (max 2000)
    nur eine Teilmenge erreichen konnte.
    """
    conn = _db()
    try:
        cur = conn.execute(
            "UPDATE download_jobs SET status='queued', updated_at_ms = ? WHERE status = 'error'",
            (_now_ms(),),
        )
        conn.commit()
        return {"ok": True, "retried": cur.rowcount}
    finally:
        conn.close()


@app.post("/api/queue/clear")
async def clear_queue(
    status: Optional[str] = Query(default="completed"),
    _: None = Depends(require_token),
):
    """Bulk-Delete: löscht Jobs nach Status in einem einzigen DELETE.

    status:
      - "completed"   (Default — sicher, nur fertige Jobs)
      - "error"       (alle fehlerhaften)
      - "queued"      (alle wartenden)
      - "all"         (alles AUSSER processing — laufende Jobs bleiben)
      - csv-Liste     (z.B. "completed,error")

    'processing' wird absichtlich NIE bulk-gelöscht, um Race-Conditions mit
    aktiven Workern zu vermeiden. Wer einzelne laufende Jobs killen will,
    nutzt /api/queue/{job_id}/cancel.
    """
    allowed = {'queued', 'completed', 'error'}  # absichtlich ohne 'processing'
    if not status or status == 'all':
        wanted = allowed
    else:
        wanted = {s.strip() for s in status.split(',') if s.strip() in allowed}
    if not wanted:
        return {"ok": False, "reason": "invalid or empty status filter", "allowed": sorted(allowed) + ["all"]}
    placeholders = ",".join("?" * len(wanted))
    conn = _db()
    try:
        cur = conn.execute(
            f"DELETE FROM download_jobs WHERE status IN ({placeholders})",
            tuple(wanted),
        )
        conn.commit()
        return {"ok": True, "deleted": cur.rowcount, "status_filter": sorted(wanted)}
    finally:
        conn.close()



def _queue_album_tracks(
    album: Dict,
    *,
    provider: str,
    location: str,
    output_format: str,
    quality: Optional[str],
    max_retries: Optional[int],
    navidrome_path: Optional[str],
) -> Dict:
    """Dedup one album's tracks against the library, register the album_meta
    aggregator, and enqueue the missing tracks. Returns a summary dict. Does
    NOT raise on an all-duplicate album — the caller decides (single-album →
    409, artist fan-out → skip). Shared by /api/download/album and
    /api/download/artist.
    """
    album_id = str(album.get("id") or "")
    tracks = album.get("tracks") or []
    to_queue = [
        t for t in tracks
        if get_duplicate_download_reason(
            t["id"], provider, location, output_format,
            navidrome_library_path=navidrome_path,
        ) is None
    ]
    if not to_queue:
        return {
            "album_id": album_id,
            "album_name": album.get("name"),
            "queued": 0,
            "skipped": len(tracks),
            "queued_track_ids": [],
        }

    # Album-Aggregator: NICHT 'queued' — sonst greift ihn der Worker als Track-
    # Download ab, scheitert an svc.get_track_details('album:...') und landet als
    # Geist-Eintrag. Status 'album_meta' wird vom Worker UND /api/queue ignoriert,
    # bleibt nur über get_job() für die Status-Route abrufbar.
    upsert_job(
        f"album:{album_id}",
        status="album_meta",
        message=f"Album '{album.get('name')}' queued",
        stage="queued",
        progress=0,
        album_id=album_id,
        payload={
            "album_id": album_id,
            "album_name": album.get("name"),
            "artist": album.get("artist"),
            "album_art": album.get("album_art"),
            "track_ids": [t["id"] for t in to_queue],
            "total_tracks": len(to_queue),
        },
    )

    album_cover = album.get("album_art")
    for track in to_queue:
        track_for_queue = _slim_track_for_queue(track) or {}
        if not track_for_queue.get("album_art") and album_cover:
            track_for_queue["album_art"] = album_cover
        if not track_for_queue.get("album"):
            track_for_queue["album"] = album.get("name") or ""
        upsert_job(
            track["id"],
            status="queued",
            message=f"Queued (Album: {album.get('name')})",
            progress=0,
            stage="queued",
            album_id=album_id,
            payload={
                "provider": provider,
                "record_track_id": track["id"],
                "location": location,
                "video_id": None,
                "output_format": output_format,
                "audio_quality": quality,
                "metadata_provider": provider,
                "max_retries": _clamp_download_retries(max_retries),
                "navidrome_library_path": navidrome_path,
                "track": track_for_queue,
            },
        )

    return {
        "album_id": album_id,
        "album_name": album.get("name"),
        "queued": len(to_queue),
        "skipped": len(tracks) - len(to_queue),
        "queued_track_ids": [t["id"] for t in to_queue],
    }


@app.post("/api/download/album")
async def download_album(request: AlbumDownloadRequest, background_tasks: BackgroundTasks, _: None = Depends(require_token)):
    """Start downloading all tracks from an album"""
    provider = resolve_metadata_provider(request.provider)
    svc = get_metadata_service(provider)

    album = svc.get_album_details(request.album_id)
    if not album:
        raise HTTPException(status_code=404, detail="Album not found")

    location = request.location if request.location in ["local", "navidrome"] else "local"
    location_msg = "local downloads folder" if location == "local" else "Navidrome server"
    output_format = request.format or config.OUTPUT_FORMAT
    navidrome_path: Optional[str] = None
    if location == "navidrome":
        navidrome_path = resolve_navidrome_library_path_optional(request.navidrome_library)

    summary = _queue_album_tracks(
        album,
        provider=provider,
        location=location,
        output_format=output_format,
        quality=request.quality,
        max_retries=request.max_retries,
        navidrome_path=navidrome_path,
    )
    if summary["queued"] == 0:
        raise HTTPException(
            status_code=409,
            detail="All tracks from this album are already in your library.",
        )

    skipped = summary["skipped"]
    return {
        "status": "queued",
        "message": f"Queued {summary['queued']} track(s) from '{album['name']}' to {location_msg}"
        + (f" ({skipped} skipped — already in library)" if skipped else ""),
        "album_id": request.album_id,
        "total_tracks": summary["queued"],
        "skipped_tracks": skipped,
        "queued_track_ids": summary["queued_track_ids"],
    }


def download_album_track(
    track_id: str,
    location: str,
    album_id: str,
    output_format: str = None,
    audio_quality: str = None,
    metadata_provider: str = "deezer",
    max_retries: int = 0,
    navidrome_library_path: Optional[str] = None,
):
    try:
        download_and_process(
            track_id,
            location,
            None,
            output_format,
            audio_quality,
            metadata_provider,
            max_retries,
            navidrome_library_path,
        )
    except Exception as e:
        print(f"Error downloading album track {track_id}: {e}")



@app.get("/api/download/album/status/{album_id}")
async def get_album_download_status(album_id: str):
    album_job_id = f"album:{album_id}"
    meta_job = get_job(album_job_id)

    agg = get_album_aggregate(album_id, exclude_job_id=album_job_id)

    if not meta_job:
        # fallback: als iemand status opvraagt zonder dat album ooit gestart is
        raise HTTPException(status_code=404, detail="Album download not found")

    payload = meta_job.get("payload") or {}

    return {
        "status": agg["status"],
        "album_name": payload.get("album_name"),
        "artist": payload.get("artist"),
        "total_tracks": payload.get("total_tracks") or agg["total_tracks"],
        "completed_tracks": agg["completed_tracks"],
        "failed_tracks": agg["failed_tracks"],
        "current_track": agg["current_track"],
        "track_ids": payload.get("track_ids") or [],
    }


def _run_artist_queueing(
    artist_id: str,
    albums: List[Dict],
    *,
    provider: str,
    location: str,
    output_format: str,
    quality: Optional[str],
    max_retries: Optional[int],
    navidrome_path: Optional[str],
) -> None:
    """Background fan-out: fetch each album's tracklist and enqueue the missing
    tracks. Runs outside the request so a large discography doesn't block the
    HTTP response. Rewrites the artist_meta aggregator when done."""
    svc = get_metadata_service(provider)
    total_queued = 0
    total_skipped = 0
    all_ids: List[str] = []
    per_album: List[Dict] = []
    artist_name: Optional[str] = None

    for a in albums:
        try:
            detail = svc.get_album_details(a["id"])
            if not detail:
                continue
            artist_name = artist_name or detail.get("artist")
            summary = _queue_album_tracks(
                detail,
                provider=provider,
                location=location,
                output_format=output_format,
                quality=quality,
                max_retries=max_retries,
                navidrome_path=navidrome_path,
            )
        except Exception as e:
            print(f"artist-download {artist_id}: album {a.get('id')} failed: {e}")
            continue
        total_queued += summary["queued"]
        total_skipped += summary["skipped"]
        all_ids.extend(summary["queued_track_ids"])
        per_album.append({
            "album_id": a["id"],
            "album_name": a.get("name") or summary.get("album_name"),
            "queued": summary["queued"],
            "skipped": summary["skipped"],
        })

    upsert_job(
        f"artist:{artist_id}",
        status="artist_meta",
        message=f"Artist '{artist_name or artist_id}': {total_queued} track(s) queued, "
                f"{total_skipped} already in library, across {len(per_album)} album(s)",
        stage="queued",
        progress=100,
        payload={
            "artist_id": artist_id,
            "artist_name": artist_name,
            "albums": per_album,
            "queued_track_ids": all_ids,
            "total_tracks": total_queued,
            "total_skipped": total_skipped,
            "queueing": False,
        },
    )


@app.post("/api/download/artist")
async def download_artist(request: ArtistDownloadRequest, background_tasks: BackgroundTasks, _: None = Depends(require_token)):
    """Queue an artist's whole discography (albums + EPs by default; singles /
    compilations opt-in). The per-album fetch + enqueue runs in the background —
    poll /api/download/artist/status/{artist_id} for progress."""
    provider = resolve_metadata_provider(request.provider)
    svc = get_metadata_service(provider)
    if not hasattr(svc, "get_artist_albums"):
        raise HTTPException(
            status_code=400,
            detail=f"Artist download is not supported for provider '{provider}' yet.",
        )

    albums = svc.get_artist_albums(
        request.artist_id,
        include_singles=bool(request.include_singles),
        include_compilations=bool(request.include_compilations),
    )
    if not albums:
        raise HTTPException(status_code=404, detail="No matching releases found for this artist.")

    location = request.location if request.location in ["local", "navidrome"] else "local"
    location_msg = "local downloads folder" if location == "local" else "Navidrome server"
    output_format = request.format or config.OUTPUT_FORMAT
    navidrome_path: Optional[str] = None
    if location == "navidrome":
        navidrome_path = resolve_navidrome_library_path_optional(request.navidrome_library)

    # Seed the artist_meta aggregator immediately so the status endpoint has
    # something to report while the background fan-out runs.
    upsert_job(
        f"artist:{request.artist_id}",
        status="artist_meta",
        message=f"Queuing {len(albums)} release(s) for artist {request.artist_id}…",
        stage="queued",
        progress=0,
        payload={
            "artist_id": request.artist_id,
            "artist_name": None,
            "albums": [{"album_id": a["id"], "album_name": a.get("name")} for a in albums],
            "queued_track_ids": [],
            "total_tracks": 0,
            "total_skipped": 0,
            "queueing": True,
        },
    )

    background_tasks.add_task(
        _run_artist_queueing,
        request.artist_id,
        albums,
        provider=provider,
        location=location,
        output_format=output_format,
        quality=request.quality,
        max_retries=request.max_retries,
        navidrome_path=navidrome_path,
    )

    return {
        "status": "started",
        "message": f"Queuing {len(albums)} release(s) to {location_msg} — downloads will trickle in.",
        "artist_id": request.artist_id,
        "album_count": len(albums),
    }


@app.get("/api/download/artist/status/{artist_id}")
async def get_artist_download_status(artist_id: str):
    """Aggregate an artist download's progress by summing its albums' aggregates."""
    meta = get_job(f"artist:{artist_id}")
    if not meta:
        raise HTTPException(status_code=404, detail="Artist download not found")
    payload = meta.get("payload") or {}
    albums = payload.get("albums") or []

    total = completed = failed = 0
    current = None
    for alb in albums:
        aid = alb.get("album_id")
        if not aid:
            continue
        agg = get_album_aggregate(aid, exclude_job_id=f"album:{aid}")
        total += agg["total_tracks"]
        completed += agg["completed_tracks"]
        failed += agg["failed_tracks"]
        if current is None and agg.get("current_track"):
            current = agg["current_track"]

    if payload.get("queueing"):
        status = "queueing"
    elif total == 0:
        status = "empty"
    elif (completed + failed) >= total:
        status = "completed" if failed == 0 else "completed_with_errors"
    else:
        status = "downloading"

    return {
        "status": status,
        "artist_name": payload.get("artist_name"),
        "album_count": len(albums),
        "total_tracks": total,
        "completed_tracks": completed,
        "failed_tracks": failed,
        "current_track": current,
        "albums": albums,
    }



@app.get("/api/youtube/candidates/{track_id}")
async def get_youtube_candidates(track_id: str, provider: Optional[str] = Query(None)):
    """Get YouTube candidates for a track to let user choose if confidence is low"""
    p = resolve_metadata_provider(provider)
    svc = get_metadata_service(p)
    try:
        track_info = svc.get_track_details(track_id)
        if not track_info:
            raise HTTPException(status_code=404, detail="Track not found")

        result = youtube_service.search_candidates(
            track_info['name'],
            track_info['artist'],
            track_info
        )

        return {
            "track": {
                "id": track_id,
                "name": track_info['name'],
                "artist": track_info['artist'],
                "album": track_info.get('album', '')
            },
            **result
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error searching YouTube: {str(e)}")


@app.get("/api/download/file/{track_id}")
async def download_file(track_id: str, filename: str = Query(...),
                        background_tasks: BackgroundTasks = BackgroundTasks()):
    """Download a file (for local browser downloads) and delete temp file afterward"""

    job = get_job(track_id)
    if not job:
        raise HTTPException(status_code=404, detail="Download not found")

    if job.get("status") != "completed":
        raise HTTPException(status_code=400, detail="File not ready for download")

    file_path = job.get("file_path")
    if not file_path or not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="File not found")

    # Decode URL-encoded filename for comparison
    decoded_filename = unquote(filename)
    actual_filename = os.path.basename(file_path)

    # Verify filename matches for security (compare decoded vs actual)
    if actual_filename != decoded_filename:
        raise HTTPException(status_code=400,
                            detail=f"Invalid filename. Expected: {actual_filename}, Got: {decoded_filename}")

    # Check if this is a temp file (for local downloads) - delete after serving
    # Normalize paths for comparison
    temp_dir_path = str(Path(config.DOWNLOAD_DIR) / "temp")
    is_temp_file = temp_dir_path in file_path or "temp" in os.path.dirname(file_path)

    # Return file for browser to download (saves to user's Downloads folder)
    # Use RFC 5987 encoding for non-ASCII filenames in Content-Disposition header
    # This handles special characters like ć, č, š, etc.
    ascii_filename = decoded_filename.encode('ascii', 'ignore').decode('ascii') or 'download.mp3'
    encoded_filename = quote(decoded_filename)

    ext = Path(file_path).suffix.lower()
    media_type = {
        ".flac": "audio/flac",
        ".mp3": "audio/mpeg",
        ".m4a": "audio/mp4",
        ".opus": "audio/opus",
        ".ogg": "audio/ogg",
        ".webm": "audio/webm",
    }.get(ext, "application/octet-stream")

    response = FileResponse(
        file_path,
        media_type=media_type,
        filename=ascii_filename,  # Fallback ASCII filename
        headers={
            "Content-Disposition": f"attachment; filename=\"{ascii_filename}\"; filename*=UTF-8''{encoded_filename}"
        }
    )

    # Delete temp file after download completes (only for local downloads)
    if is_temp_file:
        background_tasks.add_task(cleanup_temp_file, file_path, track_id)

    return response


def cleanup_temp_file(file_path: str, _job_id: str):
    """Clean up temporary download file after it's been served (local browser downloads).

    We do not record completed_track_downloads here — that table is for Navidrome
    library copies only (see download_and_process).
    """
    try:
        # Long delay so duplicate requests (extra tabs, extensions, browser retries) still hit the file
        time.sleep(max(2, config.TEMP_FILE_CLEANUP_DELAY_SEC))
        if os.path.exists(file_path):
            os.remove(file_path)
            print(f"Cleaned up temp file: {file_path}")
    except Exception as e:
        print(f"Error cleaning up temp file {file_path}: {e}")


@app.get("/api/navidrome/libraries")
async def list_navidrome_libraries():
    """Configured Navidrome music folder roots (from NAVIDROME_MUSIC_PATHS / labels)."""
    return {"libraries": config.navidrome_libraries_public()}


@app.get("/api/track/{track_id}/exists")
async def check_track_exists(
    track_id: str,
    provider: Optional[str] = Query(None),
    location: str = Query("local"),
    navidrome_library: Optional[str] = Query(None),
):
    """Check if a track is already present (see below).

    - location=local: only files on this server (downloads/temp or downloads root).
      Does not use completed_track_downloads. Local browser saves go to the client PC.
    - location=navidrome: on-disk file under the chosen library (navidrome_library path),
      or any configured library if navidrome_library is omitted; also completion DB / temp.
    """
    p = resolve_metadata_provider(provider)
    svc = get_metadata_service(p)
    try:
        if location not in ("local", "navidrome"):
            location = "local"

        track_info = svc.get_track_details(track_id)
        if not track_info:
            return {"exists": False, "file_path": None}

        ext = config.OUTPUT_FORMAT
        download_path = get_download_path(track_info, config.DOWNLOAD_DIR, ext)
        temp_path = get_download_path(
            track_info, os.path.join(config.DOWNLOAD_DIR, "temp"), ext
        )

        if location == "local":
            if os.path.isfile(download_path):
                return {"exists": True, "file_path": download_path}
            if os.path.isfile(temp_path):
                return {"exists": True, "file_path": temp_path}
            return {"exists": False, "file_path": None}

        # navidrome
        if navidrome_library:
            root = resolve_navidrome_library_path_optional(navidrome_library)
            if navidrome_service.track_file_exists(track_info, ext, root):
                return {"exists": True, "file_path": None}
        else:
            if has_completed_download(track_id, p):
                return {"exists": True, "file_path": None}
            for lib in config.NAVIDROME_MUSIC_PATHS_LIST:
                if navidrome_service.track_file_exists(track_info, ext, lib):
                    return {"exists": True, "file_path": None}
        if os.path.isfile(download_path):
            return {"exists": True, "file_path": download_path}
        if os.path.isfile(temp_path):
            return {"exists": True, "file_path": temp_path}

        return {"exists": False, "file_path": None}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error checking track: {str(e)}")


@app.get("/api/formats")
async def get_available_formats():
    """Get available audio formats and quality options"""
    return {
        "formats": [
            {"value": "mp3", "label": "MP3", "description": "Compatible with most devices"},
            {"value": "m4a", "label": "M4A/AAC", "description": "Better quality, smaller files"},
            {"value": "opus", "label": "Opus", "description": "High quality, efficient compression"},
            {"value": "ogg", "label": "OGG Vorbis", "description": "Open source format"},
            {"value": "flac", "label": "FLAC", "description": "Lossless, larger files"}
        ],
        "qualities": [
            {"value": "96", "label": "96 kbps", "description": "Low quality, small files"},
            {"value": "128", "label": "128 kbps", "description": "Standard quality (default)"},
            {"value": "192", "label": "192 kbps", "description": "Good quality"},
            {"value": "256", "label": "256 kbps", "description": "High quality"},
            {"value": "320", "label": "320 kbps", "description": "Maximum quality"},
            {"value": "lossless", "label": "Lossless", "description": "FLAC only - no quality loss"}
        ],
        "default_format": config.OUTPUT_FORMAT,
        "default_quality": config.AUDIO_QUALITY
    }

@app.get("/api/health")
async def health_check(authed: bool = Depends(optional_token)):
    """Health check endpoint.

    Liefert immer ``status: healthy`` für unauthenticated Monitoring-Tools
    (Uptime-Kuma, Docker healthcheck etc.). Erweiterte Felder mit FS-Pfaden
    und Service-URLs nur für authenticated Clients zurück — verhindert
    Disclosure-Leaks an non-authenticated Probes (Audit-Finding M-6,
    2026-05-12).
    """
    base = {"status": "healthy"}
    if authed:
        base.update({
            "default_metadata_provider": config.DEFAULT_METADATA_PROVIDER,
            "spotify_configured": spotify_service is not None,
            "navidrome_path": config.NAVIDROME_MUSIC_PATH,
            "navidrome_libraries": config.navidrome_libraries_public(),
            "navidrome_api_url": config.NAVIDROME_API_URL,
        })
    return base

@app.post("/api/recommendations")
def get_recommendations(request: RecommendationRequest):
    """
    Deezer Artist Radio — ähnliche Tracks basierend auf einem Deezer track_id
    """
    import requests

    # Erst Track holen um Artist-ID zu bekommen
    track_resp = requests.get(f"https://api.deezer.com/track/{request.track_id}")
    track_data = track_resp.json()
    artist_id = track_data.get("artist", {}).get("id")
    
    if not artist_id:
        return {"tracks": [], "source": "deezer_radio"}

    # Artist-Radio — ähnliche Tracks
    radio_url = f"https://api.deezer.com/artist/{artist_id}/radio"
    resp = requests.get(radio_url, params={"limit": request.limit})
    data = resp.json()

    tracks = []
    for t in data.get("data", []):
        tracks.append({
            "id": str(t["id"]),
            "name": t["title"],
            "artist": t["artist"]["name"],
            "artist_id": str(t["artist"]["id"]),
            "album": t["album"]["title"],
            "album_id": str(t["album"]["id"]),
            "duration_ms": t["duration"] * 1000,
            "album_art": t["album"].get("cover_medium"),
        })

    return {"tracks": tracks, "source": "deezer_radio"}


@app.post("/api/import/csv")
async def import_csv(request: CsvImportRequest, _: None = Depends(require_token), __: None = Depends(_rl_csv_import)):
    """
    CSV-Import (persistent): speichert Job in SQLite, Worker holt ihn ab.
    Gibt sofort eine job_id zurück — Status unter /api/import/jobs/{job_id}/status pollbar.
    """
    import csv, io, re as _re

    provider = resolve_metadata_provider(request.provider)
    search_limit = max(1, min(request.limit or 3, 5))
    lines = request.csv_text.strip().splitlines()

    # Parse CSV to list of (artist, title, raw_line).
    # Delimiter-Heuristik: probiere die üblichen Trennzeichen und nimm den, der über
    # die ersten Zeilen die meisten Spalten ergibt. Frühere Logik prüfte len(rows)<2,
    # was bei einer ,-getrennten Datei mit ;-Versuch nicht aufschlug (jede Zeile blieb
    # als 1-Spalten-Eintrag stehen, weil 778 Zeilen >= 2 sind).
    text = request.csv_text.strip()
    rows: list = []
    best_cols = 0
    for delim in (",", ";", "\t", "|"):
        try:
            candidate = list(csv.reader(io.StringIO(text), delimiter=delim))
            sample = candidate[:5]
            avg_cols = max((len(r) for r in sample), default=0)
            if avg_cols > best_cols:
                best_cols = avg_cols
                rows = candidate
        except Exception:
            continue
    if not rows:
        rows = [[line.strip()] for line in lines]

    # Auto-detect header: look for "artist", "track"/"title" und (Phase I)
    # optional "playlist"-Spalte. TuneMyMusic-Exports haben standardmäßig
    # eine "Playlist Name"-Spalte; andere Exports haben sie nicht — dann
    # bleibt col_playlist=None und der Track hat keine playlist_names.
    col_artist, col_title = 0, 1
    col_playlist: Optional[int] = None
    if rows and len(rows[0]) >= 2:
        first = [c.strip().lower().lstrip('\ufeff') for c in rows[0]]
        artist_cols = [i for i, c in enumerate(first) if 'artist' in c]
        title_cols  = [i for i, c in enumerate(first) if 'track' in c or 'title' in c or 'name' in c]
        playlist_cols = [i for i, c in enumerate(first) if 'playlist' in c]
        if artist_cols and title_cols:
            col_artist, col_title = artist_cols[0], title_cols[0]
            if playlist_cols:
                col_playlist = playlist_cols[0]
            rows = rows[1:]
    elif rows and any(kw in (rows[0][0].strip().lower().lstrip('\ufeff')) for kw in ['track', 'title', 'name']):
        col_artist, col_title = 1, 0
        rows = rows[1:]

    parsed = []
    for row in rows:
        if not row:
            continue
        artist, title, playlist_cell = "", "", ""
        max_col = max(col_artist, col_title, col_playlist if col_playlist is not None else 0)
        if len(row) > max_col:
            artist = row[col_artist].strip().strip('"').strip("'") if col_artist < len(row) else ""
            title  = row[col_title].strip().strip('"').strip("'")  if col_title  < len(row) else ""
            if col_playlist is not None and col_playlist < len(row):
                playlist_cell = row[col_playlist].strip().strip('"').strip("'")
        elif len(row) == 1:
            parts = _re.split(r"\s*[-–—]\s*", row[0].strip(), maxsplit=1)
            if len(parts) == 2:
                artist = parts[0].strip()
                title = parts[1].strip()
            else:
                title = parts[0].strip()
        if title:
            # Phase I: Multi-Playlist-Membership in einer Zelle. TuneMyMusic
            # liefert pro Track einen einzelnen Playlist-Namen, aber wir
            # parsen comma- oder semicolon-separated falls Tools mehrere
            # Memberships in einer Zelle haben.
            playlist_names = [
                p.strip() for p in _re.split(r"[,;]", playlist_cell) if p.strip()
            ] if playlist_cell else []
            parsed.append({
                "artist": artist,
                "title": title,
                "raw": row[0].strip() if row else f"{artist} {title}".strip(),
                "playlist_names": playlist_names,
            })

    # Empty-Guard: wenn der Parser nichts findet, sofort 400 zurück statt
    # einen Stub-Job in DB anzulegen — sonst sieht User nur kryptischen
    # Worker-State ("No pending items found") in der UI.
    if not parsed:
        raise HTTPException(
            status_code=400,
            detail=(
                "CSV-Inhalt konnte nicht erkannt werden — bitte prüfe dass die Datei "
                "Spalten 'Artist' + 'Track Name' enthält (optional 'Playlist Name'). "
                f"Empfangen: {len(request.csv_text)} Bytes, {len(rows)} Roh-Zeilen, "
                f"0 valide Tracks geparsed."
            ),
        )

    # Eindeutige job_id auf Basis der Wall-Clock-Zeit (import_jobs hat keine numeric id-Spalte)
    import time as _time
    from utils.job_store import _db as _csv_db
    job_id = f"csv-{int(_time.time() * 1000)}"

    # Falls eine ältere Session denselben job_id hinterlassen hat (extrem unwahrscheinlich,
    # aber wir wollen keine fremden Results mit den neuen vermischen), zuerst alle Reste löschen.
    conn = _csv_db()
    try:
        conn.execute("DELETE FROM import_results WHERE job_id = ?", (job_id,))
        conn.execute("DELETE FROM import_jobs    WHERE job_id = ?", (job_id,))
        conn.commit()
    finally:
        conn.close()

    # Worker-Payload: provider + search_limit als sauberes JSON in eigener
    # Spalte (vorher: Hijack des `message`-Felds als "provider|limit|pending_raw"-
    # String, was bei verzögertem Worker-Pickup als Status-Message in der UI
    # auftauchte). Filename optional für UI-Anzeige.
    #
    # Mode-Switch + Source-Marker:
    #   - mode="playlist_sync" → Worker skipped Provider/Download, macht nur
    #     Library-Match + Playlist-Reconcile. source="playlist_sync" damit
    #     Frontend Tab-Label "Playlist-Sync" rendert.
    #   - mode=None / "full"   → Default Bulk-Import-Pipeline. source="csv".
    import json as _json
    mode = (request.mode or "full").strip().lower()
    if mode not in ("full", "playlist_sync"):
        mode = "full"
    payload_dict: Dict[str, Any] = {"provider": provider, "search_limit": search_limit}
    if mode == "playlist_sync":
        payload_dict["mode"] = "playlist_sync"
        payload_dict["source"] = "playlist_sync"
    payload = _json.dumps(payload_dict)
    fname = (request.filename or "").strip() or None

    # source-Spalte ist Lane-Routing für die 4-Lane-Worker-Architektur
    # (csv / spotify_history / playlist_sync). mode steuert ob Provider/
    # Download-Phasen ausgeführt werden.
    job_source = "playlist_sync" if mode == "playlist_sync" else "csv"
    # Pre-stats für Result-Card: wieviele unique Playlists wurden in der CSV
    # gefunden. Wird vom Worker als "playlists_total" durchgereicht und vom
    # Frontend als "Playlists in CSV" angezeigt — User soll auf einen Blick
    # sehen ob sein Export überhaupt Playlist-Spalten hatte.
    playlists_in_csv = len({
        name for p in parsed for name in (p.get("playlist_names") or [])
    })
    upsert_import_job(
        job_id,
        status="queued",
        total=len(parsed),
        message=f"Queued — waiting for worker ({len(parsed)} tracks)",
        filename=fname,
        payload_json=payload,
        mode=mode,
        source=job_source,
        playlists_total=playlists_in_csv,
    )

    # Store parsed items in a temp table so the worker can read them
    # (use import_results with result_type='pending' as staging).
    # Phase I: playlist_names werden mit gespeichert, Worker kann sie
    # zu library_match/matched-Buckets durchreichen.
    insert_import_results(job_id, "pending_raw", [
        {
            "original": p["raw"],
            "requested_artist": p["artist"],
            "requested_title": p["title"],
            "playlist_names": p.get("playlist_names") or [],
        }
        for p in parsed
    ])

    return {"status": "queued", "job_id": job_id, "total": len(parsed), "filename": fname}


class SpotifyHistoryImportRequest(BaseModel):
    """Phase I.2 — Spotify Extended Streaming History Import.

    files: Liste von parsed JSON-Arrays (jedes Array eine
    Streaming_History_Audio_*.json). Frontend liest Files via FileReader,
    JSON.parse und sendet sie als Top-Level-Array damit Backend keinen
    String→JSON-Roundtrip mehr braucht.

    Filter-Defaults sind Spotifys eigene "Listened-To"-Heuristik (≥30s)
    plus Playlist-Counts pro Jahr/Monat.
    """
    files: List[List[Dict[str, Any]]]
    provider: Optional[str] = None
    limit: Optional[int] = 3
    min_ms_played: Optional[int] = 0
    min_play_count: Optional[int] = 1
    date_from: Optional[str] = None  # YYYY-MM-DD
    date_to: Optional[str] = None
    auto_playlist_year: Optional[bool] = True
    auto_playlist_month: Optional[bool] = True
    playlist_prefix: Optional[str] = "Spotify History"
    filename: Optional[str] = None  # nur für UI-Anzeige (z.B. "Spotify History · 24 Files")


@app.post("/api/import/spotify-history")
async def import_spotify_history(req: SpotifyHistoryImportRequest, _: None = Depends(require_token), __: None = Depends(_rl_spotify_history)):
    """Importiert eine oder mehrere Spotify-Extended-Streaming-History-JSONs.

    Pipeline-Integration:
    1. spotify_history.parse_streaming_history() aggregiert pro (artist, title)
       und leitet Year/Month-Auto-Playlists ab
    2. Aggregat wird in import_jobs/import_results eingereiht
       (gleiches Schema wie /api/import/csv) — der existing Import-Worker
       übernimmt von hier (Phase 0 Library-Match, Phase 2 Provider-Lookup)
    3. import_playlist_names werden via Phase I durch alle Buckets
       durchgereicht und beim queue_all + Reconcile in Subsonic-Playlists
       übersetzt
    """
    from services.spotify_history import parse_streaming_history
    import json as _json
    import time as _time
    from utils.job_store import _db as _hist_db

    provider = resolve_metadata_provider(req.provider)
    search_limit = max(1, min(req.limit or 3, 5))

    # Aggregation. parse_streaming_history is pure — kein DB-Touch, kein
    # Side-Effect — also sicher synchron im Request.
    # None-explizit-Check statt `or` — sonst macht User-eingabe 0 (alle Tracks
    # rein) silent zum Default 30000. Same für min_play_count.
    result = parse_streaming_history(
        req.files,
        min_ms_played=int(req.min_ms_played if req.min_ms_played is not None else 0),
        min_play_count=int(req.min_play_count if req.min_play_count is not None else 1),
        date_from=req.date_from,
        date_to=req.date_to,
        auto_playlist_year=bool(req.auto_playlist_year if req.auto_playlist_year is not None else True),
        auto_playlist_month=bool(req.auto_playlist_month if req.auto_playlist_month is not None else True),
        playlist_prefix=(req.playlist_prefix or "Spotify History").strip() or "Spotify History",
    )
    tracks = result["tracks"]
    stats = result["stats"]

    if not tracks:
        # Aggregat ist leer — keine Tracks die importierbar wären. Wir geben
        # die stats zurück damit User sieht warum (zu strenger Filter,
        # nur Podcasts in den Files, etc.) — aber kein import_job angelegt.
        return {
            "status": "empty",
            "message": "No tracks after filtering",
            "stats": stats,
        }

    job_id = f"spotify-hist-{int(_time.time() * 1000)}"

    # Cleanup analog zu /api/import/csv falls dieser job_id schon existiert
    conn = _hist_db()
    try:
        conn.execute("DELETE FROM import_results WHERE job_id = ?", (job_id,))
        conn.execute("DELETE FROM import_jobs    WHERE job_id = ?", (job_id,))
        conn.commit()
    finally:
        conn.close()

    payload = _json.dumps({"provider": provider, "search_limit": search_limit, "source": "spotify_history"})
    fname = (req.filename or "").strip() or f"Spotify History · {len(req.files)} Files"

    upsert_import_job(
        job_id,
        status="queued",
        total=len(tracks),
        message=f"Queued — waiting for worker ({len(tracks)} tracks from {stats['total_events']} events)",
        filename=fname,
        payload_json=payload,
        mode="full",
        source="spotify_history",
    )

    # Phase I-Schema: artist/title/playlist_names werden direkt durchgereicht.
    # Worker pickt die als pending_raw und läuft die ganze Pipeline (Phase 0
    # Library-Match → Phase 2 Provider-Lookup → Phase 3 materialize).
    insert_import_results(job_id, "pending_raw", [
        {
            "original": f"{t['artist']} - {t['title']}",
            "requested_artist": t["artist"],
            "requested_title": t["title"],
            "playlist_names": t.get("playlist_names") or [],
        }
        for t in tracks
    ])

    return {
        "status": "queued",
        "job_id": job_id,
        "total": len(tracks),
        "filename": fname,
        "stats": stats,
    }


@app.get("/api/import/jobs/{job_id}/status")
async def import_job_status(job_id: str):
    """Poll CSV/JSON import progress (aus SQLite). Liefert:
    - library_match_count (Phase H) damit Frontend den "in library"-Counter live zeigen kann
    - recovery_total + recovery_recovered (Phase 2.5) damit Frontend rechecked-Stats anzeigt
    - source ("csv" | "spotify_history") aus payload_json damit Frontend
      Tab-Label korrekt rendert (vorher hardcoded "CSV" auch bei JSON)
    """
    import json as _json
    job = get_import_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="CSV import job not found")
    counts = count_import_results(job_id)
    # Source aus payload_json ableiten — bei legacy-Jobs ohne source-Feld
    # default "csv". Spotify-History setzt {"source": "spotify_history"}.
    source = "csv"
    payload_raw = job.get("payload_json")
    if payload_raw:
        try:
            payload = _json.loads(payload_raw)
            if isinstance(payload, dict) and isinstance(payload.get("source"), str):
                source = payload["source"]
        except Exception:
            pass
    return {
        "status": job["status"],
        "total": job["total"],
        "processed": job["processed"],
        "found": job["found"],
        "not_found": job["not_found"],
        "library_match_count": counts.get("library_match", 0),
        "recovery_total": job.get("recovery_total", 0) or 0,
        "recovery_recovered": job.get("recovery_recovered", 0) or 0,
        "phase0_progress": job.get("phase0_progress", 0) or 0,
        "playlists_total": job.get("playlists_total", 0) or 0,
        "playlists_synced": job.get("playlists_synced", 0) or 0,
        "playlist_tracks_added": job.get("playlist_tracks_added", 0) or 0,
        "playlist_queue_tagged": job.get("playlist_queue_tagged", 0) or 0,
        "source": source,
        "message": job.get("message", ""),
        "filename": job.get("filename"),
    }


@app.post("/api/import/jobs/{job_id}/cancel")
async def import_job_cancel(job_id: str, _: None = Depends(require_token)):
    """User-Cancel via UI. Setzt status='cancelled' in der DB; der Worker
    pollt diesen Status zwischen Phase-Schritten und beendet den Job
    sauber (max 1 Phase-Step Latenz). Idempotent: bereits terminale Jobs
    (completed/error/cancelled) returnen `cancelled: false` ohne Fehler."""
    from utils.job_store import cancel_import_job
    job = get_import_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="CSV import job not found")
    cancelled = cancel_import_job(job_id)
    return {"ok": True, "cancelled": cancelled, "previous_status": job.get("status")}


@app.get("/api/import/jobs/{job_id}/result")
async def import_job_result(
    job_id: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
):
    """CSV-Import-Ergebnisse paginiert aus SQLite."""
    job = get_import_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="CSV import job not found")
    if job["status"] != "completed":
        raise HTTPException(status_code=400, detail="CSV import not yet completed")

    results = get_import_results(job_id, offset=offset, limit=limit)
    counts = count_import_results(job_id)
    return {
        "total": job["total"],
        "found": counts["matched"],
        "not_found": counts["unmatched"],
        "library_match_count": counts.get("library_match", 0),
        "matched": results["matched"],
        "unmatched": results["unmatched"],
        "library_match": results.get("library_match", []),
    }


@app.delete("/api/import/jobs/{job_id}")
async def delete_import_job(job_id: str, _: None = Depends(require_token)):
    """Löscht einen CSV-Import-Job inkl. aller matched/unmatched Results.

    Greift auf import_jobs UND import_results — der Job verschwindet
    komplett, kann danach mit derselben job_id nicht mehr abgerufen werden.
    Idempotent: löschen eines nicht-existierenden Jobs ist OK (deleted=0).
    """
    conn = _db()
    try:
        conn.execute("DELETE FROM import_results WHERE job_id = ?", (job_id,))
        cur = conn.execute("DELETE FROM import_jobs WHERE job_id = ?", (job_id,))
        n = cur.rowcount or 0
        conn.commit()
        return {"ok": True, "deleted_jobs": n, "job_id": job_id}
    finally:
        conn.close()


class CsvQueueAllRequest(BaseModel):
    provider: Optional[str] = None
    location: Optional[str] = "local"
    format: Optional[str] = None
    quality: Optional[str] = None
    max_retries: Optional[int] = 0
    navidrome_library: Optional[str] = None


@app.post("/api/import/jobs/{job_id}/queue-all")
async def csv_queue_all(job_id: str, req: CsvQueueAllRequest, _: None = Depends(require_token)):
    """Schreibe ALLE matched Tracks aus einem CSV-Import in die Download-Queue.

    Ersetzt den browserseitigen "for each downloadTrack(...)"-Loop, der bei großen
    Imports (30k+ Tracks) entweder am Pagination-Limit hing oder den Browser mit
    tausenden Einzelrequests blockiert hätte. Hier wird alles serverseitig in einer
    DB-Schleife in download_jobs eingefügt.
    """
    import json as _json

    import_job = get_import_job(job_id)
    if not import_job:
        raise HTTPException(status_code=404, detail="CSV import job not found")
    if import_job["status"] != "completed":
        raise HTTPException(status_code=400, detail="CSV import not yet completed")

    provider = resolve_metadata_provider(req.provider)
    location = req.location if req.location in ["local", "navidrome"] else "local"
    output_format = req.format or config.OUTPUT_FORMAT
    audio_quality = req.quality
    max_retries = _clamp_download_retries(req.max_retries)
    navidrome_path: Optional[str] = None
    if location == "navidrome":
        navidrome_path = resolve_navidrome_library_path_optional(req.navidrome_library)

    # ----- Schritt 1: alle matched Track-JSONs + playlist_names aus DB lesen + parsen -----
    # Phase I: playlist_names_json wird mitgelesen damit der Download-Job
    # den Playlist-Marker `import_playlist_names` im Payload trägt — das
    # ist die Quelle für `_reconcile_imported_playlists` nach Download.
    conn = _db()
    try:
        rows = conn.execute(
            "SELECT track_json, playlist_names_json FROM import_results WHERE job_id = ? AND result_type = 'matched'",
            (job_id,),
        ).fetchall()
    finally:
        conn.close()

    candidates: list = []  # list of (track_id, track_dict, playlist_names)
    errors = 0
    for row in rows:
        if not row["track_json"]:
            errors += 1
            continue
        try:
            track = _json.loads(row["track_json"])
        except (_json.JSONDecodeError, TypeError):
            errors += 1
            continue
        tid = str(track.get("id") or "")
        if not tid:
            errors += 1
            continue
        try:
            playlist_names = _json.loads(row["playlist_names_json"]) if row["playlist_names_json"] else []
        except Exception:
            playlist_names = []
        candidates.append((tid, track, playlist_names))

    # ----- Schritt 2: bulk-Lookup auf existierende download_jobs (statt N Einzelqueries) -----
    skipped_dup = 0
    in_flight_ids: set = set()
    completed_provider_pairs: set = set()
    if candidates:
        conn = _db()
        try:
            track_ids = [tid for tid, _, _ in candidates]
            # SQLite-Parameterlimit (~999) → in 500er-Chunks aufteilen
            chunk = 500
            for i in range(0, len(track_ids), chunk):
                slice_ids = track_ids[i:i + chunk]
                placeholders = ",".join("?" * len(slice_ids))
                cur = conn.execute(
                    f"SELECT job_id FROM download_jobs WHERE status IN ('queued', 'processing') AND job_id IN ({placeholders})",
                    slice_ids,
                )
                in_flight_ids.update(r["job_id"] for r in cur.fetchall())
                cur = conn.execute(
                    f"SELECT track_id FROM completed_track_downloads WHERE provider = ? AND track_id IN ({placeholders})",
                    [provider, *slice_ids],
                )
                completed_provider_pairs.update(r["track_id"] for r in cur.fetchall())
        finally:
            conn.close()

    # ----- Schritt 3: candidates filtern + Insert-Tuples vorbereiten -----
    now = _now_ms()
    msg = f"Download queued for {'local downloads folder' if location == 'local' else 'Navidrome server'}"
    payload_template = {
        "provider": provider,
        "location": location,
        "video_id": None,
        "output_format": output_format,
        "audio_quality": audio_quality,
        "metadata_provider": provider,
        "max_retries": max_retries,
        "navidrome_library_path": navidrome_path,
    }

    insert_tuples: list = []
    queued = 0
    for tid, track, playlist_names in candidates:
        if tid in in_flight_ids or tid in completed_provider_pairs:
            skipped_dup += 1
            continue
        # File-System-Existenz-Check: gezielt mit dem bereits geladenen track_info,
        # damit keine Provider-HTTP-Calls erfolgen. Wir tragen den File-Check als
        # "best effort" mit ein — bei tausenden Files ist Stat schnell, aber wir
        # wollen es trotzdem nicht für jeden überflüssigen Track ausführen.
        try:
            if physical_track_file_exists(track, location, output_format, navidrome_path):
                skipped_dup += 1
                continue
        except Exception:
            pass

        track_for_queue = _slim_track_for_queue(track)
        payload = dict(payload_template, record_track_id=tid, track=track_for_queue)
        # Phase I: import_playlist_names im Payload setzen wenn der Track
        # auf mindestens einer Playlist im Source-CSV stand. Reconcile
        # (siehe _reconcile_imported_playlists) liest diesen Marker.
        if playlist_names:
            payload["import_playlist_names"] = playlist_names
        insert_tuples.append((
            tid, "queued", "queued", 0, msg,
            None, None, None,  # file_path, download_url, error
            None,               # album_id
            _json.dumps(payload),
            now, now,
        ))
        queued += 1

    # ----- Schritt 4: Bulk-Insert in EINER Connection / EINER Transaction -----
    if insert_tuples:
        conn = _db()
        try:
            conn.executemany(
                """
                INSERT INTO download_jobs (
                    job_id, status, stage, progress, message,
                    file_path, download_url, error, album_id,
                    payload_json, created_at_ms, updated_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    status=excluded.status,
                    stage=excluded.stage,
                    progress=excluded.progress,
                    message=excluded.message,
                    payload_json=excluded.payload_json,
                    updated_at_ms=excluded.updated_at_ms
                """,
                insert_tuples,
            )
            conn.commit()
        finally:
            conn.close()

    # ----- Schritt 5: Library-Match-Tracks zu Playlists (Phase I-Edge-Case) -----
    # Library-Match-Tracks haben keinen Download-Job (Tracks sind ja schon
    # da), würden aber ohne diesen Schritt nie zu ihren Playlists hinzu-
    # gefügt. Job-scoped Reconcile direkt aus import_results.
    library_recon: Dict[str, int] = {"playlists": 0, "tracks_added": 0, "library_tracks_processed": 0}
    try:
        library_recon = _reconcile_import_library_matches(job_id)
    except Exception as e:
        # Reconcile-Failure soll queue_all nicht killen — Tracks sind in der
        # Queue, das ist der wichtige Teil. Reconcile kann manuell oder
        # beim nächsten Plugin-Sync nachgezogen werden.
        print(f"[csv-queue-all] library-reconcile failed (non-fatal): {type(e).__name__}: {e}")

    return {
        "queued": queued,
        "skipped_duplicate": skipped_dup,
        "errors": errors,
        "total_matched": len(rows),
        "library_playlists_reconciled": library_recon,
    }


# ---------------------------------------------------------------------------
# Phase 1+2: URL-Direct Download / URL-Search (YouTube, SoundCloud, ...)
# ---------------------------------------------------------------------------

def url_download_and_process(
    job_id: str,
    url: str,
    location: str,
    output_format: str,
    audio_quality: Optional[str],
    navidrome_library_path: Optional[str],
    track_hint: Optional[Dict] = None,
    import_playlist_names: Optional[List[str]] = None,
    source_lane: Optional[str] = None,
):
    """Background task: yt-dlp lädt direkt via URL (kein Spotify/Deezer-Match).

    Tags werden aus den yt-dlp-Metadaten gebildet (Title=Track, Uploader=Artist,
    Thumbnail=Cover). Funktioniert für jede yt-dlp-unterstützte Quelle.

    v0.5.0: läuft auch im Worker-Kontext (Playlist-Tracks, kind='url'-Jobs).
    `import_playlist_names` muss durchgereicht werden, weil das payload-Update
    bei progress=25 das komplette payload_json ersetzt — ohne Re-Inject wäre
    der Playlist-Marker weg bevor _reconcile_imported_playlists ihn liest.
    `source_lane` bindet den Download an die VPN-Lane des Workers (dual-lane).
    """
    try:
        upsert_job(job_id, status="processing", message="Reading URL metadata...",
                   stage="fetching", progress=10)

        info = youtube_service.extract_video_info(url)
        if not info.get('success'):
            upsert_job(job_id, status="error",
                       message=f"Failed to read URL: {info.get('error', 'Unknown error')}",
                       progress=0)
            return

        # Track-Info aus yt-dlp-Metadaten ableiten
        title = (info.get('title') or '').strip() or url
        uploader = (info.get('uploader') or '').strip() or "Unknown"
        thumb = info.get('thumbnail') or ''
        webpage = info.get('webpage_url') or url

        # Falls Frontend einen Track-Hint mitgegeben hat (z.B. aus Search-Result),
        # bevorzuge dessen Felder fürs Tagging
        if track_hint:
            title = (track_hint.get('name') or title).strip()
            uploader = (track_hint.get('artist') or uploader).strip()
            thumb = track_hint.get('album_art') or thumb

        # Album = "Singles" statt uploader — vorher führte uploader-als-album
        # zum Doppelnest <Channel>/<Channel>/<Title>.opus, was visuell in
        # Navidrome merkwürdig wirkt (Channel-Name als Album-Bezeichnung).
        # Mit "Singles" gibt's <Channel>/Singles/<Title>.opus — der
        # Navidrome-konventionale Pfad für Tracks ohne natürliches Album-
        # Konzept (YouTube, SoundCloud, Bandcamp-Singles).
        track_info = {
            'id': job_id,
            'name': title,
            'artist': uploader,
            'artists': [uploader] if uploader else [],
            'album_artist': uploader,
            'album': 'Singles',
            'track_number': 1,
            'release_date': '',
            'album_art': thumb,
            'duration_ms': int((info.get('duration') or 0) * 1000),
            'external_url': webpage,
            'preview_url': None,
        }

        # Job-Payload mit den yt-dlp-Metadaten aktualisieren — sonst zeigt
        # die Queue-UI weiterhin die rohe URL als Track-Name (so wurde der
        # Job vom Endpoint mit Placeholder-Track angelegt). Mit dem Update
        # bekommt das Frontend Title + Uploader + Thumbnail in der Live-
        # Ansicht direkt nach dem yt-dlp-Extract.
        slim_track = {
            "id": job_id,
            "name": title,
            "artist": uploader,
            "album": 'Singles',
            "album_art": thumb,
        }
        refreshed_payload = {
            "kind": "url",
            "url": url,
            "location": location,
            "output_format": output_format,
            "audio_quality": audio_quality,
            "navidrome_library_path": navidrome_library_path,
            "track": slim_track,
        }
        # Playlist-Marker erhalten — dieses Update ERSETZT payload_json,
        # ohne Re-Inject würde der Marker verloren gehen (v0.5.0).
        if import_playlist_names:
            refreshed_payload["import_playlist_names"] = import_playlist_names
        upsert_job(
            job_id,
            status="processing",
            message="Preparing download location...",
            stage="preparing",
            progress=25,
            payload=refreshed_payload,
        )

        # Idempotenz-Check: User submittet dieselbe URL nochmal — vorher
        # legte get_target_path bei jedem Run eine "(1)", "(2)"-Variante an.
        # Jetzt: wenn die exakte Ziel-Datei (Title+Artist+Album) schon
        # existiert, abbrechen mit "completed/already present".
        if physical_track_file_exists(track_info, location, output_format, navidrome_library_path):
            upsert_job(
                job_id,
                status="completed",
                message="Track is already in your library",
                stage="completed",
                progress=100,
            )
            return

        temp_dir = os.path.join(config.DOWNLOAD_DIR, "temp")
        Path(temp_dir).mkdir(parents=True, exist_ok=True)
        download_path = get_download_path(track_info, temp_dir, output_format)

        upsert_job(job_id, status="processing", message="Downloading from source...",
                   stage="downloading", progress=40)

        download_result = youtube_service.download_by_url(
            webpage, download_path, output_format, audio_quality,
            source_lane=source_lane,
        )
        if not download_result.get('success'):
            upsert_job(job_id, status="error",
                       message=f"Download failed: {download_result.get('error', 'Unknown error')}",
                       progress=0)
            return

        upsert_job(job_id, status="processing", message="Applying metadata...",
                   stage="tagging", progress=80)
        try:
            metadata_service.apply_metadata(download_result['file_path'], track_info)
        except Exception as e:
            print(f"[url-download] Metadata apply warning: {e}")

        if location == "navidrome":
            upsert_job(job_id, status="processing",
                       message="Copying to Navidrome library...",
                       stage="copying", progress=90)
            try:
                target_path = navidrome_service.get_target_path(
                    track_info, output_format, navidrome_library_path
                )
                shutil.copy2(download_result['file_path'], target_path)
                if os.path.exists(download_result['file_path']):
                    os.remove(download_result['file_path'])
                navidrome_result = navidrome_service.finalize_track(str(target_path))
                upsert_job(
                    job_id,
                    status="completed",
                    message="Track successfully added to Navidrome library"
                            if navidrome_result.get('success')
                            else f"Track added (scan may need manual trigger): {navidrome_result.get('error', '')}",
                    file_path=str(target_path),
                    stage="completed",
                    progress=100,
                )
            except Exception as e:
                upsert_job(job_id, status="error",
                           message=f"Failed to copy to Navidrome: {str(e)}", progress=0)
        else:
            filename = os.path.basename(download_result['file_path'])
            encoded_filename = quote(filename, safe='')
            download_url = f"api/download/file/{job_id}?filename={encoded_filename}"
            upsert_job(
                job_id,
                status="completed",
                message="Track ready for download",
                file_path=download_result['file_path'],
                download_url=download_url,
                stage="completed",
                progress=100,
            )

        # Cool-down läuft im Worker (siehe worker.py) — auch hier kein
        # inline sleep mehr, sonst doppeln sich die Pausen.

    except Exception as e:
        upsert_job(job_id, status="error", message=f"Error: {str(e)}", progress=0)


# Playlist-URL-Heuristik (v0.5.0): nur URLs, die nach Playlist aussehen,
# bekommen den (1-2s teuren) flat-extract-Expand. Single-Track-URLs behalten
# ihre bisherige Latenz. False-positive ist harmlos — wenn der Expand kein
# `_type=playlist` findet, fällt der Handler auf den Single-Pfad zurück.
_PLAYLIST_URL_RE = re.compile(
    r"(soundcloud\.com/[^/]+/sets/"   # SC: /artist/sets/name
    r"|[?&]list="                      # YT: watch?v=..&list=… / playlist?list=…
    r"|youtube\.com/playlist)",
    re.IGNORECASE,
)


def _queue_playlist_tracks(
    expanded: Dict,
    location: str,
    output_format: str,
    audio_quality: Optional[str],
    navidrome_path: Optional[str],
    as_navidrome_playlist: bool,
) -> Dict:
    """Queut die Tracks einer expandierten Playlist als Worker-Jobs (v0.5.0).

    Anders als der Single-URL-Pfad (BackgroundTask, status='processing')
    werden Playlist-Tracks als status='queued' angelegt — der JobWorker
    arbeitet sie über die Download-Lanes ab. Damit greifen Lane-Cooldowns
    und Bot-Check-Re-Queue (#51); 200 Tracks hämmern SoundCloud nicht
    parallel zu.

    Dedup pro Track über die stabile job_id (hash aus url+location+format):
      - queued/processing  → skip (läuft schon)
      - completed          → skip, aber Playlist-Marker in den bestehenden
                             Job mergen — der Reconcile nimmt ihn dann mit.
                             Grenze: Reconcile filtert auf created_at_ms
                             (60-Tage-Fenster); Jobs älter als das bleiben
                             außen vor (Long-Tail, akzeptiert für v0.5.0).
      - error/absent       → (neu) queuen
    """
    from utils.job_store import merge_playlist_names_into_download_job

    playlist_name = expanded["name"]
    marker = [playlist_name] if as_navidrome_playlist else []
    # Placeholder-Daten für die Queue-Anzeige (v0.5.3): SC-flat-extract
    # liefert für Set-Einträge nach den ersten ~5 nur Stubs (api-v2-URL,
    # kein Titel/Cover) — statt der rohen URL zeigen wir "Playlist · Track
    # N/M" und das Set-Artwork. Der Worker ersetzt das beim Download durch
    # die echten Metadaten (payload-Refresh bei progress=25).
    pl_uploader = expanded.get("uploader") or ""
    pl_artwork = expanded.get("artwork") or ""
    track_count = len(expanded["tracks"])

    queued = 0
    skipped = 0
    for idx, entry in enumerate(expanded["tracks"], start=1):
        track_url = entry["url"]
        job_id = f"url-{abs(hash((track_url, location, output_format))) % 10_000_000}"

        existing = get_job(job_id)
        if existing and existing.get("status") in ("queued", "processing"):
            skipped += 1
            continue
        if existing and existing.get("status") == "completed":
            skipped += 1
            if marker:
                merge_playlist_names_into_download_job(job_id, marker)
            continue

        title = entry.get("title") or f"{playlist_name} · Track {idx}/{track_count}"
        uploader = entry.get("uploader") or pl_uploader or "URL"
        payload = {
            "kind": "url",
            "url": track_url,
            "location": location,
            "output_format": output_format,
            "audio_quality": audio_quality,
            "navidrome_library_path": navidrome_path,
            "track": {
                "id": job_id,
                "name": title,
                "artist": uploader,
                "album": "",
                "album_art": entry.get("thumbnail") or pl_artwork,
            },
        }
        if marker:
            payload["import_playlist_names"] = marker

        upsert_job(
            job_id,
            status="queued",
            message=f"Queued from playlist '{playlist_name}'",
            stage="queued",
            progress=0,
            payload=payload,
        )
        queued += 1

    return {
        "status": "queued",
        "kind": "playlist",
        "playlist_name": playlist_name,
        "queued": queued,
        "skipped": skipped,
        "total": expanded["total"],
        "truncated": expanded["truncated"],
    }


def _playlist_expand_error_detail(raw: str) -> str:
    """Operator-freundliche Message für Playlist-Expand-Fehler (v0.5.1).

    Der häufigste Fall ist 404 auf private SoundCloud-Sets: die normale
    Set-URL ist für Anonym-Zugriff (yt-dlp) unsichtbar — der Share-Link
    trägt einen Secret-Token (…/sets/name/s-XXXXX) der das aufschließt."""
    if "404" in raw:
        return (
            "Playlist nicht lesbar (404). Wenn das Set privat ist: den "
            "SoundCloud-Share-Link nutzen (endet auf /s-XXXXX) oder das Set "
            "öffentlich stellen. Alternativ SC-Cookies in der cookies.txt "
            "hinterlegen."
        )
    return f"Playlist nicht lesbar: {raw[:200]}"


@app.post("/api/url/probe")
async def url_probe(req: URLProbeRequest, _: None = Depends(require_token)):
    """Probe (v0.5.0): ist die URL eine Playlist? flat-extract, kein Download.

    Frontend ruft das debounced beim Paste auf, um die Playlist-UI
    (Track-Count + Navidrome-Toggle) einzublenden. Expand-Fehler (private
    Sets, 404) werden als kind='error' gemeldet, damit der User es schon
    beim Paste sieht statt erst nach dem Submit (v0.5.1).
    """
    url = (req.url or "").strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="Bitte eine vollständige URL angeben (http/https).")
    if not _PLAYLIST_URL_RE.search(url):
        return {"kind": "track"}

    expanded = youtube_service.expand_playlist_url(url)
    if expanded is None:
        return {"kind": "track"}
    if expanded.get("error"):
        return {"kind": "error", "message": _playlist_expand_error_detail(expanded["error"])}
    return {
        "kind": "playlist",
        "name": expanded["name"],
        "track_count": len(expanded["tracks"]),
        "total": expanded["total"],
        "truncated": expanded["truncated"],
    }


@app.post("/api/url/download")
async def url_download(req: URLDownloadRequest, background_tasks: BackgroundTasks, _: None = Depends(require_token), __: None = Depends(_rl_url_download)):
    """Phase 1: Direkter Download via URL ohne Spotify/Deezer-Match.

    Funktioniert für YouTube, SoundCloud, Bandcamp, Vimeo … alles was yt-dlp kennt.
    v0.5.0: Playlist-URLs werden zu N Worker-Jobs expandiert (siehe
    _queue_playlist_tracks); Single-URLs behalten den BackgroundTask-Pfad.
    """
    if not req.url or not req.url.strip().startswith(('http://', 'https://')):
        raise HTTPException(status_code=400, detail="Bitte eine vollständige URL angeben (http/https).")

    location = req.location if req.location in ("local", "navidrome") else "local"
    output_format = req.format or config.OUTPUT_FORMAT
    navidrome_path: Optional[str] = None
    if location == "navidrome":
        navidrome_path = resolve_navidrome_library_path_optional(req.navidrome_library)

    # ── Playlist-Branch (v0.5.0) ────────────────────────────────────
    if _PLAYLIST_URL_RE.search(req.url):
        expanded = youtube_service.expand_playlist_url(req.url.strip())
        # Expand-FEHLER (404 bei privaten Sets etc.) → klarer 422 statt
        # still auf den Single-Pfad zu fallen. Ein Single-Download dieser
        # Playlist-URL würde identisch scheitern — der alte Fall-Through
        # erzeugte Fake-Success "In Queue als url-XXXX" + unsichtbaren
        # Error-Job (v0.5.1-Fix).
        if expanded and expanded.get("error"):
            raise HTTPException(
                status_code=422,
                detail=_playlist_expand_error_detail(expanded["error"]),
            )
        if expanded:
            result = _queue_playlist_tracks(
                expanded,
                location,
                output_format,
                req.quality,
                navidrome_path,
                bool(req.as_navidrome_playlist),
            )
            # Sofort-Reconcile im Hintergrund: Tracks, die schon in der
            # Library sind (Dedup-Skip mit Marker-Merge), landen direkt in
            # der Navidrome-Playlist statt erst beim nächsten periodischen
            # Lauf. Neue Downloads zieht der Background-Thread nach.
            if bool(req.as_navidrome_playlist) and result.get("skipped", 0) > 0:
                background_tasks.add_task(_reconcile_imported_playlists)
            return result
        # Heuristik-Hit aber kein Playlist-Extract → Single-Pfad weiter unten.

    # Stabile job_id aus URL — verhindert Duplikate beim doppelten Klick
    job_id = f"url-{abs(hash((req.url.strip(), location, output_format))) % 10_000_000}"

    track_for_queue = {
        "id": job_id,
        "name": req.url.strip(),  # wird vom Worker mit echtem Title überschrieben
        "artist": "URL",
        "album": "",
        "album_art": "",
    }

    # WICHTIG: status="processing" (nicht "queued") — sonst pickt der
    # JobWorker-Loop den Job zusätzlich auf und ruft download_and_process()
    # mit Deezer-Lookup → "Invalid query" weil track_id eine URL ist.
    # URL-Downloads umgehen die Match-Pipeline komplett, deshalb gehört
    # die ganze Verarbeitung in url_download_and_process (BackgroundTask)
    # und der Worker darf den Job nicht parallel anfassen.
    upsert_job(
        job_id,
        status="processing",
        message=f"URL queued for {'local downloads folder' if location == 'local' else 'Navidrome server'}",
        stage="queued",
        progress=0,
        payload={
            "kind": "url",
            "url": req.url.strip(),
            "location": location,
            "output_format": output_format,
            "audio_quality": req.quality,
            "navidrome_library_path": navidrome_path,
            "track": track_for_queue,
        },
    )

    background_tasks.add_task(
        url_download_and_process,
        job_id,
        req.url.strip(),
        location,
        output_format,
        req.quality,
        navidrome_path,
        None,
    )

    return {"status": "queued", "job_id": job_id}


@app.post("/api/url/search")
async def url_search(req: URLSearchRequest):
    """Phase 2: Multi-Source-Search via yt-dlp.

    Hard Cutover (Tonus 0.2.0+): ignoriert `source`-Param, queryt ALLE
    aktivierten Sources parallel (config.ENABLED_SOURCES), merged Results.
    """
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as _TFE

    deprecation: List[str] = []
    if req.source:
        deprecation.append(
            f"`source` parameter ('{req.source}') is ignored since Tonus 0.2.0 — "
            f"smart multi-source-routing now searches all configured sources "
            f"({','.join(config.ENABLED_SOURCES)}) and merges results. "
            f"Remove the parameter to silence this notice."
        )

    sources = config.ENABLED_SOURCES or ["youtube"]
    aggregated: List[Dict[str, Any]] = []
    per_source_errors: Dict[str, str] = {}

    with ThreadPoolExecutor(max_workers=max(1, len(sources))) as pool:
        futures = {
            src: pool.submit(youtube_service.search_url, req.query, src, req.limit or 10)
            for src in sources
        }
        for src, fut in futures.items():
            try:
                res = fut.result(timeout=config.MULTI_SOURCE_TIMEOUT_S)
            except _TFE:
                per_source_errors[src] = f"timed out after {config.MULTI_SOURCE_TIMEOUT_S}s"
                continue
            except Exception as e:
                per_source_errors[src] = f"{type(e).__name__}: {e}"
                continue
            if res.get("success"):
                aggregated.extend(res.get("results", []) or [])
            else:
                per_source_errors[src] = res.get("error", "search failed")

    response: Dict[str, Any] = {
        "query": req.query,
        "sources_queried": sources,
        "results": aggregated,
    }
    if deprecation:
        response["_deprecation"] = deprecation
    if per_source_errors:
        response["_source_errors"] = per_source_errors
    if not aggregated and per_source_errors:
        # Alle Sources sind gescheitert — gib einen sinnvollen 502 raus damit
        # der Caller (Plugin / UI) den Fehler erkennt
        raise HTTPException(
            status_code=502,
            detail=f"All sources failed: {per_source_errors}",
        )
    return response


# ---------------------------------------------------------------------------
# Plugin-spezifische Endpoints
#
# Diese drei Routen sind die einzige Schnittstelle, die das Navidrome-Plugin
# (separates Repo `tonus-navidrome-plugin`) braucht. Alles andere kann es
# über die bestehenden /api/import/csv + /api/import/jobs + /api/download* Endpoints erreichen.
# Bewusst klein gehalten, damit Plugin-Wartung minimal bleibt.
# ---------------------------------------------------------------------------


@app.get("/api/plugin/health")
async def plugin_health():
    """Liveness-Check für das Plugin. Verrät, ob das Backend einen Bearer-Token
    erzwingt — Plugin nutzt das, um in der Settings-Page klar zu machen ob ein
    Token-Feld nötig ist."""
    return {
        "ok": True,
        "version": "1.0.0",
        "auth_required": auth_required(),
        "now_ms": _now_ms(),
    }


@app.get("/api/plugin/sync-status")
async def plugin_sync_status():
    """Kompaktes Status-Aggregat für den Read-Only-Block in der Plugin-
    Settings-Page (Navidrome-UI). Polling-freundlich: ein DB-Trip, kleine
    Antwort, Error-Snippet truncated. Ergänzt /api/queue/stats um die letzte
    Fehlermeldung als String, damit das Plugin die User direkt anzeigen kann."""
    from utils.job_store import _db as _stats_db

    now = _now_ms()
    conn = _stats_db()
    try:
        agg = conn.execute(
            "SELECT status, COUNT(*) AS n FROM download_jobs GROUP BY status"
        ).fetchall()
        by_status = {r["status"]: r["n"] for r in agg}
        oldest = conn.execute(
            "SELECT MIN(created_at_ms) AS m FROM download_jobs "
            "WHERE status IN ('queued','processing')"
        ).fetchone()
        oldest_age = ((now - oldest["m"]) / 1000.0) if oldest and oldest["m"] else 0
        last_ok = conn.execute(
            "SELECT MAX(updated_at_ms) AS m FROM download_jobs WHERE status='completed'"
        ).fetchone()
        last_err_row = conn.execute(
            "SELECT updated_at_ms, error, message FROM download_jobs "
            "WHERE status='error' ORDER BY updated_at_ms DESC LIMIT 1"
        ).fetchone()

        last_err_ms = last_err_row["updated_at_ms"] if last_err_row else None
        last_err_msg = (
            (last_err_row["error"] or last_err_row["message"])
            if last_err_row
            else None
        )
        if last_err_msg and len(last_err_msg) > 200:
            last_err_msg = last_err_msg[:197] + "..."

        with _plugin_sync_lock:
            plugin_snapshot = dict(_plugin_sync_state)

        return {
            "queue": {
                "queued": by_status.get("queued", 0),
                "processing": by_status.get("processing", 0),
                "completed": by_status.get("completed", 0),
                "error": by_status.get("error", 0),
                "oldest_age_s": round(oldest_age, 1),
            },
            "last_completed_ms": (last_ok["m"] if last_ok else None),
            "last_error_ms": last_err_ms,
            "last_error_message": last_err_msg,
            "now_ms": now,
            "auth_required": auth_required(),
            "plugin_sync": plugin_snapshot,
        }
    finally:
        conn.close()


def _resolve_playlist_name_template(template: Optional[str]) -> Optional[str]:
    """Substituiert ``{date}`` in einem playlist_name-Template gegen das
    aktuelle Datum (YYYY-MM-DD, lokale Server-Zeit). Leerstring/None → None,
    damit der Caller weiß: keine Playlist-Funktion."""
    if not template or not template.strip():
        return None
    from datetime import date
    return template.replace("{date}", date.today().isoformat()).strip()


def _reconcile_imported_playlists(max_age_days: int = 60) -> Dict[str, int]:
    """Findet alle 'completed'-Tracks der letzten N Tage die einen Playlist-
    Marker tragen, und fügt sie idempotent zu ihrer Subsonic-Playlist in
    Navidrome hinzu.

    Phase I: zwei Marker werden akzeptiert:
    - ``plugin_sync_playlist_name`` (Single-String) — vom Navidrome-Plugin
      gesetzt, ein Track gehört zu genau einer Sync-Run-Playlist
    - ``import_playlist_names`` (List[str]) — vom CSV-Import (TuneMyMusic)
      gesetzt, ein Track kann auf mehreren Playlists landen wenn das
      CSV multiple Memberships kommasepariert in einer Zelle hat

    Architektur: Wir tracken keine separate Sync-Run-Tabelle, sondern lesen
    die Marker aus ``download_jobs.payload_json``. Idempotenz greift über
    read-before-write in ``add_tracks_to_playlist`` — der Helper kann beliebig
    oft laufen ohne Duplikate zu erzeugen.

    Tracks deren Subsonic-Index-Eintrag noch nicht existiert (Scanner war
    noch nicht durch) werden beim nächsten Reconcile-Lauf nachgezogen.

    Backward-Compat: alter Funktionsname `_reconcile_plugin_playlists` ist
    unten als Alias verfügbar.
    """
    import json as _json
    from collections import defaultdict

    cutoff_ms = _now_ms() - max_age_days * 24 * 3600 * 1000
    conn = _db()
    try:
        rows = conn.execute(
            """
            SELECT job_id, payload_json FROM download_jobs
            WHERE status = 'completed'
              AND created_at_ms >= ?
              AND (payload_json LIKE '%plugin_sync_playlist_name%'
                   OR payload_json LIKE '%import_playlist_names%')
            """,
            (cutoff_ms,),
        ).fetchall()
    finally:
        conn.close()

    by_playlist: Dict[str, List[Dict[str, str]]] = defaultdict(list)
    for r in rows:
        if not r["payload_json"]:
            continue
        try:
            payload = _json.loads(r["payload_json"])
        except Exception:
            continue

        # Sammle alle Playlist-Memberships dieses Tracks. Plugin-Marker ist
        # Single-String, Import-Marker ist List — wir vereinheitlichen zu
        # einer Liste damit der Track auf alle ihre Playlists landet.
        playlist_names: List[str] = []
        plugin_name = payload.get("plugin_sync_playlist_name")
        if plugin_name:
            # Multi-User-Modus: Tracks mit `plugin_sync_navidrome_user`-Marker
            # werden vom Plugin via Subsonic-API im Namen dieses Users gepushed.
            # Backend-Reconcile skippt diese Tracks, sonst würden Playlists
            # doppelt entstehen (einmal Admin-owned, einmal user-owned).
            if not payload.get("plugin_sync_navidrome_user"):
                playlist_names.append(plugin_name)
        import_names = payload.get("import_playlist_names") or []
        if isinstance(import_names, list):
            playlist_names.extend(p for p in import_names if isinstance(p, str) and p.strip())

        if not playlist_names:
            continue
        track_obj = payload.get("track") or {}
        artist = (track_obj.get("artist") or "").strip()
        title = (track_obj.get("name") or "").strip()
        if not artist or not title:
            continue
        # Memo (v0.5.2): Paare (Job, Playlist) die schon erfolgreich in
        # Navidrome bestätigt wurden, überspringen — sonst hämmern wir bei
        # jedem 15-min-Lauf tausende Subsonic-Lookups für längst
        # reconcilierte Tracks und spammen "+0"-Logzeilen.
        memo = payload.get("reconciled_playlists") or []
        for pl_name in playlist_names:
            if pl_name in memo:
                continue
            by_playlist[pl_name].append(
                {"artist": artist, "title": title, "job_id": r["job_id"]}
            )

    if not by_playlist:
        return {"playlists": 0, "tracks_added": 0}

    total_added = 0
    unresolved_total = 0
    unresolved_playlists = 0
    # Memo-Updates: {job_id: [playlist_name, ...]} — nach dem Loop in einem
    # einzigen Batch in payload_json gemerged (eine Transaction statt N).
    memo_updates: Dict[str, List[str]] = defaultdict(list)
    for playlist_name, items in by_playlist.items():
        existing = navidrome_service.find_playlist_by_name(playlist_name)
        if existing:
            playlist_id = existing.get("id")
        else:
            playlist_id = navidrome_service.create_playlist(playlist_name)
        if not playlist_id:
            print(f"[plugin-reconcile] could not get/create playlist '{playlist_name}'")
            continue

        # Subsonic-IDs auflösen — Misses (Track noch nicht im Index) werden
        # beim nächsten Reconcile-Lauf nachgezogen.
        sub_ids: List[str] = []
        resolved_items: List[Dict[str, str]] = []
        unresolved = 0
        for it in items:
            sid = navidrome_service.find_track_id_by_artist_title(it["artist"], it["title"])
            if sid:
                sub_ids.append(sid)
                resolved_items.append(it)
            else:
                unresolved += 1
        if not sub_ids:
            # Nicht pro Playlist loggen (v0.5.4): dauerhaft unauflösbare
            # Marker (Download fehlte, Tags matchen nicht) würden sonst
            # jede 15 min dutzende Zeilen wiederholen — Summary unten.
            unresolved_total += unresolved
            unresolved_playlists += 1
            continue

        result = navidrome_service.add_tracks_to_playlist(playlist_id, sub_ids)
        added = result.get("added", 0)
        total_added += added
        if added > 0:
            print(
                f"[plugin-reconcile] '{playlist_name}' (pid={playlist_id}): "
                f"+{added} tracks (already in playlist: {result.get('already_present', 0)}, "
                f"unresolved: {unresolved})"
            )
        # Resolved + in der Playlist (frisch oder schon drin) → memoizen.
        # Unresolved Items bleiben ohne Memo und werden nachgezogen.
        # Guard: bei API-Fehler liefert add_tracks_to_playlist added=0 obwohl
        # to_add nicht leer war — dann NICHT memoizen, sonst gehen die Tracks
        # verloren. Erfolg ⇔ added + already_present deckt alle sub_ids ab.
        if added + result.get("already_present", 0) >= len(sub_ids):
            for it in resolved_items:
                jid = it.get("job_id")
                if jid:
                    memo_updates[jid].append(playlist_name)

    if memo_updates:
        from utils.job_store import bulk_merge_reconciled_playlists

        bulk_merge_reconciled_playlists(dict(memo_updates))

    if unresolved_total:
        print(
            f"[plugin-reconcile] {unresolved_total} track(s) across "
            f"{unresolved_playlists} playlist(s) not resolvable in Navidrome yet "
            f"(missing download or tag mismatch) — will retry"
        )

    return {"playlists": len(by_playlist), "tracks_added": total_added}


# Backward-Compat-Alias: existing Plugin-Sync-Code ruft die Funktion unter
# dem alten Namen auf. Beide Namen zeigen auf dieselbe Implementation —
# `_reconcile_imported_playlists` ist der semantisch breitere Name (deckt
# Plugin- und CSV-Import-Pfade ab), `_reconcile_plugin_playlists` bleibt
# als no-op-Alias damit Aufrufer nicht synchron migriert werden müssen.
_reconcile_plugin_playlists = _reconcile_imported_playlists


def _reconcile_import_library_matches(
    job_id: str,
    on_playlist_progress: Optional[Any] = None,
) -> Dict[str, int]:
    """Phase I-Edge-Case-Fix: für library_match-Tracks eines CSV-Imports
    direkt zu den Subsonic-Playlists hinzufügen.

    Hintergrund: `_reconcile_imported_playlists()` liest aus `download_jobs`,
    aber Library-Match-Tracks erzeugen keinen Download-Job (queue_all
    filtert auf result_type='matched'). Sie würden ohne diesen Helper
    nie zu ihren Subsonic-Playlists hinzugefügt — obwohl sie schon in
    Navidrome liegen und der User explizit `playlist`-Memberships im
    CSV angegeben hat.

    Diese Funktion ist job-scoped (kein Cutoff über Zeit, nur dieser
    eine Import) und idempotent (read-before-write in
    `add_tracks_to_playlist`).

    on_playlist_progress: optionaler Callable(idx, total, playlist_name)
    der bei jedem Playlist-Schritt aufgerufen wird — Worker nutzt das
    um die UI-Status-Message live mit dem aktuellen Reconcile-Schritt
    zu aktualisieren ("Reconciling playlist 42/197: Favorite Songs").
    """
    from collections import defaultdict

    rows = get_import_library_matches_with_playlists(job_id)
    if not rows:
        return {"playlists": 0, "tracks_added": 0, "library_tracks_processed": 0}

    by_playlist: Dict[str, List[Dict[str, str]]] = defaultdict(list)
    for r in rows:
        artist = r["requested_artist"].strip()
        title = r["requested_title"].strip()
        if not artist or not title:
            continue
        for pl_name in r["playlist_names"]:
            by_playlist[pl_name].append({"artist": artist, "title": title})

    if not by_playlist:
        return {"playlists": 0, "tracks_added": 0, "library_tracks_processed": len(rows)}

    total_playlists = len(by_playlist)
    total_added = 0
    for idx, (playlist_name, items) in enumerate(by_playlist.items()):
        if on_playlist_progress is not None:
            try:
                on_playlist_progress(idx, total_playlists, playlist_name)
            except Exception:
                pass
        existing = navidrome_service.find_playlist_by_name(playlist_name)
        if existing:
            playlist_id = existing.get("id")
        else:
            playlist_id = navidrome_service.create_playlist(playlist_name)
        if not playlist_id:
            print(f"[csv-library-reconcile] could not get/create playlist '{playlist_name}'")
            continue

        # Subsonic-IDs auflösen — sollten alle existieren (Tracks sind ja
        # bereits in Library), aber falls Subsonic-Index noch nicht durchge-
        # zogen ist (frisch gescant), gibt's Misses wie im normalen Reconcile.
        sub_ids: List[str] = []
        unresolved = 0
        for it in items:
            sid = navidrome_service.find_track_id_by_artist_title(it["artist"], it["title"])
            if sid:
                sub_ids.append(sid)
            else:
                unresolved += 1
        if not sub_ids:
            print(
                f"[csv-library-reconcile] '{playlist_name}': "
                f"no resolvable subsonic IDs ({unresolved} unresolved of {len(items)})"
            )
            continue

        result = navidrome_service.add_tracks_to_playlist(playlist_id, sub_ids)
        added = result.get("added", 0)
        total_added += added
        print(
            f"[csv-library-reconcile] '{playlist_name}' (pid={playlist_id}): "
            f"+{added} library tracks (already in playlist: {result.get('already_present', 0)}, "
            f"unresolved: {unresolved})"
        )

    return {
        "playlists": len(by_playlist),
        "tracks_added": total_added,
        "library_tracks_processed": len(rows),
    }


def _check_mix_tracks_in_library(req: PluginMixDiscoveryRequest) -> List[Dict[str, str]]:
    """Synchroner Library-Lookup für die existing-Liste eines Mix-Discovery-Calls.

    Holt LB-Top-Recordings für das Genre, fragt pro Track navidrome_service
    ab, returnt nur die in-Library-vorhandenen Tracks mit ihrer Subsonic-ID.
    Diese Liste kommt direkt im Endpoint-Response zurück und wird vom Plugin
    im KVStore persistiert für die Build-Phase.

    Performance: ~25 Tracks × ~50 ms Subsonic-search3 = ~1.5 s. Bleibt damit
    klar im 30 s-Plugin-Hostlimit.
    """
    from services.discovery import lb_genre_top_recordings

    # Pool 2× count, weil viele Tracks NICHT in Library sind und wir trotzdem
    # genug existing für die Mix-Build-Phase brauchen wollen.
    pool_size = max(req.count * 2, 50)
    items = lb_genre_top_recordings(req.genre, count=pool_size)
    if not items:
        return []

    existing: List[Dict[str, str]] = []
    target_existing = int(req.count * (1.0 - req.discovery_ratio))
    for it in items:
        if len(existing) >= target_existing:
            break
        try:
            sid = navidrome_service.find_track_id_by_artist_title(
                it.get("artist", ""), it.get("title", "")
            )
            if sid:
                existing.append({
                    "subsonic_id": sid,
                    "artist": it.get("artist", ""),
                    "title": it.get("title", ""),
                })
        except Exception:
            continue
    return existing


def _run_plugin_mix_discovery(req: PluginMixDiscoveryRequest) -> None:
    """Background-Task hinter POST /api/plugin/mix/discovery.

    Holt LB-Top-Recordings pro Genre, dedupliziert gegen Library, queued
    fehlende Tracks (= discovery_ratio-Anteil) als download_jobs mit
    Mix-Markern (mix_name + navidrome_user) im Payload, sodass
    /api/plugin/finished-tracks sie später dem Mix zuordnen kann.

    Schreibt sich nicht in _plugin_sync_state, weil Mix-Runs unabhängig
    vom Default-Discovery-Pfad laufen — ein Mix-Run hat seinen eigenen
    Lebenszyklus (Wed-Discovery → Fr-Build).
    """
    from services.discovery import lb_genre_top_recordings, deezer_search_track

    started = _now_ms()
    pool_size = max(req.count * 2, 50)
    target_missing = int(req.count * req.discovery_ratio)

    try:
        items = lb_genre_top_recordings(req.genre, count=pool_size)
    except Exception as e:
        print(f"[plugin-mix-discovery] LB fetch failed: {e}")
        return

    if not items:
        print(f"[plugin-mix-discovery] no LB results for genre={req.genre!r}")
        return

    location = "navidrome"
    output_format = config.OUTPUT_FORMAT
    provider = "deezer"
    navidrome_path = resolve_navidrome_library_path_optional(None)
    run_id = f"plugin-mix-{req.navidrome_user}-{req.mix_name}-{started}"

    queued = 0
    skipped_existing = 0
    failed = 0

    for it in items:
        if queued >= target_missing:
            break
        artist = it.get("artist", "")
        title = it.get("title", "")
        if not artist or not title:
            continue

        # In-Library? Dann skip — der Track gehört zur "existing"-Liste, die der
        # Endpoint synchron zurückgegeben hat.
        try:
            if navidrome_service.find_track_id_by_artist_title(artist, title):
                skipped_existing += 1
                continue
        except Exception:
            pass

        # Deezer-Track auflösen, sodass wir eine ID für den Download-Worker haben.
        deezer_track = deezer_search_track(artist, title)
        if not deezer_track:
            failed += 1
            continue
        track_id = str(deezer_track.get("id", ""))
        if not track_id:
            failed += 1
            continue

        artist_obj = deezer_track.get("artist") or {}
        album_obj = deezer_track.get("album") or {}
        track_hint = {
            "id": track_id,
            "name": deezer_track.get("title", ""),
            "artist": artist_obj.get("name", ""),
            "album": album_obj.get("title", ""),
            "album_art": (
                album_obj.get("cover_xl")
                or album_obj.get("cover_big")
                or album_obj.get("cover_medium")
            ),
        }

        try:
            dup = get_duplicate_download_reason(
                track_id, provider, location, output_format,
                navidrome_library_path=navidrome_path,
            )
            if dup:
                skipped_existing += 1
                continue

            track_for_queue = _resolve_track_for_queue(track_id, provider, track_hint)
            payload_extra: Dict[str, Any] = {
                "provider": provider,
                "record_track_id": track_id,
                "location": location,
                "video_id": None,
                "output_format": output_format,
                "audio_quality": None,
                "metadata_provider": provider,
                "max_retries": 0,
                "navidrome_library_path": navidrome_path,
                "track": track_for_queue,
                # Mix-spezifische Marker — gelesen von /api/plugin/finished-tracks
                # wenn ein Mix-Build-Job die Tracks für seine Playlist sammelt.
                "plugin_mix_run_id": run_id,
                "plugin_mix_name": req.mix_name,
                "plugin_mix_navidrome_user": req.navidrome_user,
                "plugin_mix_genre": req.genre,
            }
            upsert_job(
                track_id,
                status="queued",
                message=f"Download queued (mix={req.mix_name})",
                progress=0,
                stage="queued",
                payload=payload_extra,
            )
            queued += 1
        except Exception as e:
            failed += 1
            print(f"[plugin-mix-discovery] queue fail track={track_id}: {e}")

    elapsed_ms = _now_ms() - started
    print(
        f"[plugin-mix-discovery] {req.mix_name!r} (genre={req.genre}) "
        f"done in {elapsed_ms}ms — pool={len(items)} "
        f"queued={queued} skipped_existing={skipped_existing} failed={failed}"
    )


def _check_lbweekly_tracks_in_library(
    req: "PluginLbWeeklyDiscoveryRequest",
) -> List[Dict[str, str]]:
    """Synchroner Library-Lookup für die existing-Liste eines LB-Weekly-Calls.
    Liefert die schon vorhandenen Tracks mit Subsonic-ID (Plugin persistiert
    sie im KVStore für die Build-Phase)."""
    from services.discovery import lb_playlist_tracks
    items = lb_playlist_tracks(req.listenbrainz_user, req.source_patch, req.occurrence)
    existing: List[Dict[str, str]] = []
    for it in items[: req.max_tracks]:
        try:
            sid = navidrome_service.find_track_id_by_artist_title(
                it.get("artist", ""), it.get("title", "")
            )
            if sid:
                existing.append({
                    "subsonic_id": sid,
                    "artist": it.get("artist", ""),
                    "title": it.get("title", ""),
                })
        except Exception:
            continue
    return existing


def occ_tag(occurrence: int) -> str:
    return "cur" if occurrence == 0 else f"occ{occurrence}"


def _run_plugin_lbweekly_discovery(req: "PluginLbWeeklyDiscoveryRequest") -> None:
    """Background-Task hinter POST /api/plugin/lbweekly/discovery.

    Zieht die LB-Playlist (source_patch+occurrence), dedupliziert gegen
    Library, queued fehlende Tracks als download_jobs mit den BESTEHENDEN
    sync-Markern (plugin_sync_playlist_name + plugin_sync_navidrome_user),
    sodass /api/plugin/finished-tracks + der Plugin-Reconcile sie unverändert
    der user-owned Subsonic-Playlist zuordnen."""
    from services.discovery import lb_playlist_tracks, deezer_search_track

    started = _now_ms()
    items = lb_playlist_tracks(req.listenbrainz_user, req.source_patch, req.occurrence)
    if not items:
        print(f"[plugin-lbweekly] no LB tracks for {req.source_patch!r} occ={req.occurrence}")
        return

    location = req.location if req.location in ("local", "navidrome") else "navidrome"
    output_format = config.OUTPUT_FORMAT
    provider = "deezer"
    navidrome_path = resolve_navidrome_library_path_optional(None)
    run_id = f"plugin-lbweekly-{req.navidrome_user}-{req.source_patch}-{occ_tag(req.occurrence)}-{started}"

    queued = skipped = failed = 0
    for it in items[: req.max_tracks]:
        artist = (it.get("artist") or "").strip()
        title = (it.get("title") or "").strip()
        if not artist or not title:
            continue
        try:
            if navidrome_service.find_track_id_by_artist_title(artist, title):
                skipped += 1
                continue
        except Exception:
            pass
        deezer_track = deezer_search_track(artist, title)
        if not deezer_track:
            failed += 1
            continue
        track_id = str(deezer_track.get("id", ""))
        if not track_id:
            failed += 1
            continue
        artist_obj = deezer_track.get("artist") or {}
        album_obj = deezer_track.get("album") or {}
        track_hint = {
            "id": track_id, "name": deezer_track.get("title", ""),
            "artist": artist_obj.get("name", ""), "album": album_obj.get("title", ""),
            "album_art": (album_obj.get("cover_xl") or album_obj.get("cover_big")
                          or album_obj.get("cover_medium")),
        }
        try:
            if get_duplicate_download_reason(track_id, provider, location,
                                             output_format,
                                             navidrome_library_path=navidrome_path):
                skipped += 1
                continue
            track_for_queue = _resolve_track_for_queue(track_id, provider, track_hint)
            payload_extra = {
                "provider": provider, "record_track_id": track_id,
                "location": location, "video_id": None,
                "output_format": output_format, "audio_quality": None,
                "metadata_provider": provider, "max_retries": 0,
                "navidrome_library_path": navidrome_path,
                "track": track_for_queue,
                # BESTEHENDE sync-Marker — finished-tracks filtert exakt darauf.
                "plugin_sync_run_id": run_id,
                "plugin_sync_playlist_name": req.playlist_name,
                "plugin_sync_navidrome_user": req.navidrome_user,
            }
            upsert_job(track_id, status="queued",
                       message=f"Download queued (lbweekly={req.playlist_name})",
                       progress=0, stage="queued", payload=payload_extra)
            queued += 1
        except Exception as e:
            failed += 1
            print(f"[plugin-lbweekly] queue fail {track_id}: {e}")

    print(f"[plugin-lbweekly] {req.playlist_name!r} done in {_now_ms()-started}ms — "
          f"pool={len(items)} queued={queued} skipped={skipped} failed={failed}")


@app.post("/api/plugin/mix/discovery")
async def plugin_mix_discovery(
    req: PluginMixDiscoveryRequest,
    background_tasks: BackgroundTasks,
    _: None = Depends(require_token),
):
    """Plugin-Trigger für Genre-basierte Mixes (Wed-Discovery-Cron-Pfad).

    Returnt sofort mit der Liste der bereits-in-Library-vorhandenen Tracks
    (für die Plugin-Build-Phase). Fehlende Tracks werden im Hintergrund in
    die Download-Queue gelegt, mit Mix-Markern im Payload (mix_name +
    navidrome_user). Beim Build-Cron (Fr) holt das Plugin sich die
    inzwischen-fertigen Tracks via /api/plugin/finished-tracks und mischt
    sie mit den existing tracks zur finalen Playlist.

    Trotz feuer-und-vergiss: die existing-Liste muss synchron berechnet
    werden, weil das Plugin sie für die Build-Phase im KVStore speichern
    muss. Library-Lookup per Navidrome-search3 ist schnell genug
    (~50ms/Track × 25 = 1-2 s) — bleibt im 30-s-Plugin-Limit.
    """
    background_tasks.add_task(_run_plugin_mix_discovery, req)
    # Synchroner Library-Check für die existing-Liste — Plugin braucht das
    # für KVStore-Persistierung
    existing = _check_mix_tracks_in_library(req)
    return {
        "started": True,
        "message": "mix discovery + queueing missing tracks in background",
        "existing": existing,
    }


@app.post("/api/plugin/lbweekly/discovery")
async def plugin_lbweekly_discovery(
    req: PluginLbWeeklyDiscoveryRequest,
    background_tasks: BackgroundTasks,
    _: None = Depends(require_token),
):
    """Plugin-Trigger für eine LB-'createdfor'-Playlist. Queued fehlende
    Tracks im Hintergrund (mit sync-Markern) und liefert synchron die
    bereits-in-Library-Liste für die Plugin-Build-Phase."""
    background_tasks.add_task(_run_plugin_lbweekly_discovery, req)
    existing = _check_lbweekly_tracks_in_library(req)
    return {"started": True,
            "message": "lbweekly discovery + queueing missing tracks in background",
            "existing": existing}


@app.get("/api/plugin/finished-tracks")
async def plugin_finished_tracks(
    navidrome_user: str = Query(..., min_length=1),
    since_days: int = Query(60, ge=1, le=365),
    playlist_name: Optional[str] = Query(None),
    limit: int = Query(25, ge=1, le=200),
):
    """Liefert die heruntergeladenen Tracks eines Plugin-Sync-Runs für einen
    bestimmten Navidrome-User — wird vom Plugin im Reconcile-Task abgefragt
    um die User-eigene Subsonic-Playlist zu erstellen.

    Filter:
      - status='completed' (Worker hat den Track erfolgreich runtergeladen)
      - payload.plugin_sync_navidrome_user == ``navidrome_user``
      - optional: payload.plugin_sync_playlist_name == ``playlist_name``
      - created_at_ms innerhalb der letzten ``since_days``
      - max ``limit`` Tracks pro Call (Default 25), damit der Plugin-
        Reconcile-Task nicht im 30 s-Hostlimit hängt. Reconcile holt sich
        beim nächsten Cron-Trigger den Rest.

    **Subsonic-ID-Auflösung passiert hier serverseitig** mit Admin-Auth: für
    jeden Track wird ``find_track_id_by_artist_title`` einmalig aufgerufen,
    das Ergebnis im payload gecached und in folgenden Calls direkt geliefert.
    Damit muss das Plugin keine search3-Calls mehr pro Track machen, was
    sonst den 30 s-Plugin-Timeout sprengen würde.

    Read-only — kein Token erforderlich (analog /api/plugin/sync-status)."""
    import json as _json

    cutoff_ms = _now_ms() - since_days * 24 * 3600 * 1000
    conn = _db()
    try:
        rows = conn.execute(
            """
            SELECT job_id, payload_json, updated_at_ms
              FROM download_jobs
             WHERE status = 'completed'
               AND created_at_ms >= ?
               AND payload_json LIKE '%plugin_sync_navidrome_user%'
             ORDER BY updated_at_ms DESC
            """,
            (cutoff_ms,),
        ).fetchall()
    finally:
        conn.close()

    items = []
    # Track die noch keine cached subsonic_id haben — nach dem Loop einmal
    # zurück in download_jobs.payload_json schreiben.
    payload_updates: List[tuple] = []
    for r in rows:
        if len(items) >= limit:
            break
        if not r["payload_json"]:
            continue
        try:
            payload = _json.loads(r["payload_json"])
        except Exception:
            continue
        if payload.get("plugin_sync_navidrome_user") != navidrome_user:
            continue
        if playlist_name and payload.get("plugin_sync_playlist_name") != playlist_name:
            continue
        track_obj = payload.get("track") or {}
        artist = (track_obj.get("artist") or "").strip()
        title = (track_obj.get("name") or "").strip()
        if not artist or not title:
            continue

        # Subsonic-ID aus dem Cache (= payload-Feld). Wenn nicht da, jetzt
        # auflösen und in payload speichern für den nächsten Reconcile.
        sub_id = payload.get("subsonic_track_id") or ""
        if not sub_id:
            try:
                resolved = navidrome_service.find_track_id_by_artist_title(artist, title)
                if resolved:
                    sub_id = resolved
                    payload["subsonic_track_id"] = resolved
                    payload_updates.append((_json.dumps(payload), r["job_id"]))
            except Exception as e:
                # Subsonic-Probleme sind kein Showstopper — Track kommt beim
                # nächsten Reconcile-Lauf wieder dran.
                print(f"[finished-tracks] subsonic lookup failed for {r['job_id']}: {e}")

        items.append({
            "deezer_id": r["job_id"],
            "artist": artist,
            "title": title,
            "playlist_name": payload.get("plugin_sync_playlist_name"),
            "completed_at_ms": r["updated_at_ms"],
            "subsonic_track_id": sub_id,
        })

    # Cache-Updates persistieren (idempotent, ein einziger Trip).
    if payload_updates:
        upd_conn = _db()
        try:
            upd_conn.executemany(
                "UPDATE download_jobs SET payload_json = ? WHERE job_id = ?",
                payload_updates,
            )
            upd_conn.commit()
        finally:
            upd_conn.close()

    return {
        "items": items,
        "count": len(items),
        "navidrome_user": navidrome_user,
        "since_days": since_days,
        "limit": limit,
    }


# ──────────────────────────────────────────────────────────────────────
# Phase F.2 — Auth-Endpoints
#
# /api/auth/setup-status  — public, sagt ob noch ein Initial-Setup nötig ist
# /api/auth/setup         — public, legt den ersten Admin an (nur einmal)
# /api/auth/login         — public, Username + Password (+ optional TOTP) → JWT pair
# /api/auth/refresh       — public, Refresh-Token → neues Access-Token
# /api/auth/logout        — auth, revoke aktuelles Refresh-Token
# /api/auth/me            — auth, gibt den aktuell auth'd User zurück
# ──────────────────────────────────────────────────────────────────────

class AuthSetupRequest(BaseModel):
    username: str
    password: str
    enable_totp: bool = False


class AuthLoginRequest(BaseModel):
    username: str
    password: str
    totp_code: Optional[str] = None


class AuthRefreshRequest(BaseModel):
    refresh_token: str


class AuthLogoutRequest(BaseModel):
    refresh_token: Optional[str] = None


def _client_ip(request: Request) -> Optional[str]:
    """Extract client IP from request, falls hinter Reverse-Proxy via
    X-Forwarded-For / X-Real-IP. Best-effort — null wenn keiner gesetzt."""
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        # X-Forwarded-For: client, proxy1, proxy2 — wir wollen client
        return fwd.split(",")[0].strip()
    real = request.headers.get("x-real-ip")
    if real:
        return real.strip()
    return request.client.host if request.client else None


@app.get("/api/auth/setup-status")
async def auth_setup_status():
    """Public — Frontend nutzt das beim ersten Page-Load um zu erkennen,
    ob ein Setup-Wizard gezeigt werden muss. Auth-aktiv wenn entweder
    ein User registriert ist oder Legacy-Token konfiguriert."""
    from utils import auth_users as au
    return {
        "setup_required": au.setup_required(),
        "auth_active": auth_required(),
        "legacy_token_active": bool(config.TONUS_API_TOKEN),
    }


@app.post("/api/auth/setup")
async def auth_setup(req: AuthSetupRequest):
    """Bootstrap: legt den ersten Admin-User an. Nur einmal aufrufbar —
    sobald ein User existiert, gibt's 409."""
    from utils import auth_users as au
    if not au.setup_required():
        raise HTTPException(status_code=409, detail="Setup bereits abgeschlossen")
    try:
        user = au.create_user(req.username, req.password, is_admin=True)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Optional: TOTP-Setup vorbereiten — aber NICHT direkt scharf machen!
    # Frontend zeigt QR + Code-Input → POST /api/auth/totp-confirm verifies
    # den ersten Code BEVOR das Secret in die DB landet. Sonst kann ein
    # User mit kaputtem QR-Scan sich selbst aussperren (TOTP aktiv, aber
    # er hat keinen funktionierenden Authenticator).
    totp_uri: Optional[str] = None
    totp_secret: Optional[str] = None
    totp_qr_data_url: Optional[str] = None
    if req.enable_totp:
        import qrcode as _qrcode
        import qrcode.image.svg as _qrcode_svg
        import base64 as _b64
        from io import BytesIO as _BytesIO

        totp_secret = au.generate_totp_secret()
        # NICHT au.set_totp_secret hier — erst nach Verify-Step.
        totp_uri = au.totp_provisioning_uri(totp_secret, req.username)

        # QR server-side rendern (SVG, nicht PNG) — vermeidet Pillow-
        # Dependency, ist scharfer als PNG und kleiner. Der otpauth-URI
        # enthält das Klartext-Secret, also nicht an externe QR-Services.
        _img = _qrcode.make(totp_uri, image_factory=_qrcode_svg.SvgImage)
        _buf = _BytesIO()
        _img.save(_buf)
        totp_qr_data_url = "data:image/svg+xml;base64," + _b64.b64encode(_buf.getvalue()).decode("ascii")

    # Direkt-Login nach Setup — User soll nicht extra einloggen müssen.
    pair = au.issue_jwt_pair(user["id"], req.username, is_admin=True)
    au.touch_last_login(user["id"])

    return {
        "user": {"id": user["id"], "username": req.username, "is_admin": True},
        "tokens": pair,
        "totp_secret": totp_secret,  # nur einmal sichtbar
        "totp_uri": totp_uri,
        "totp_qr_data_url": totp_qr_data_url,
    }


@app.post("/api/auth/login")
async def auth_login(req: AuthLoginRequest, request: Request):
    """Username + Password (+ TOTP wenn aktiv) → JWT pair. Wirft 401 bei
    falschen Creds. 429 bei Rate-Limit nach 5 Failed-Attempts/15min.
    403 wenn die IP bereits gebannt ist (Lifetime-Ban nach 5+ Fails/24h)."""
    from utils import auth_users as au
    from utils.auth import assert_ip_not_banned

    # Pre-Auth IP-Ban-Check — gebannte IPs erst gar nicht in den Username/
    # Password-Pfad lassen. Verhindert auch dass record_login_attempt sie
    # weiter mitzählt und u.U. ein Recovery via "alter ttl" möglich wäre.
    assert_ip_not_banned(request)

    username = req.username.strip()
    if not username or not req.password:
        raise HTTPException(status_code=400, detail="Username und Passwort erforderlich")

    if au.is_rate_limited(username):
        raise HTTPException(
            status_code=429,
            detail="Zu viele Fehlversuche. Bitte 15 Minuten warten.",
        )

    user = au.get_user_by_username(username)
    ip = _client_ip(request)

    if not user or not au.verify_password(req.password, user["password_hash"]):
        au.record_login_attempt(username, ip, success=False)
        raise HTTPException(
            status_code=401,
            detail="Falsche Anmeldedaten",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # TOTP-Check wenn aktiviert
    if user["totp_secret"]:
        secret = au.get_user_totp_secret(user["id"])
        if not secret or not au.verify_totp_code(secret, req.totp_code or ""):
            au.record_login_attempt(username, ip, success=False)
            raise HTTPException(
                status_code=401,
                detail="2FA-Code fehlt oder falsch",
                headers={"X-Auth-Required-2FA": "true"},
            )

    pair = au.issue_jwt_pair(user["id"], user["username"], bool(user["is_admin"]))
    au.touch_last_login(user["id"])
    au.record_login_attempt(username, ip, success=True)

    return {
        "user": {
            "id": user["id"],
            "username": user["username"],
            "is_admin": bool(user["is_admin"]),
            "totp_enabled": bool(user["totp_secret"]),
        },
        "tokens": pair,
    }


@app.post("/api/auth/refresh")
async def auth_refresh(req: AuthRefreshRequest):
    """Tausche Refresh-Token gegen neues Access-Token (+ rotiertem Refresh).
    Old refresh wird invalidiert (token-rotation pattern)."""
    from utils import auth_users as au
    claims = au.decode_jwt(req.refresh_token, expected_type="refresh")
    if not claims:
        raise HTTPException(status_code=401, detail="Refresh-Token ungültig oder abgelaufen")

    user_id = int(claims.get("sub", 0))
    user = au.get_user_by_id(user_id)
    if not user:
        raise HTTPException(status_code=401, detail="User nicht gefunden")

    # Rotation: altes refresh-jti revoken, neues pair ausstellen.
    au.revoke_refresh_token(claims["jti"])
    pair = au.issue_jwt_pair(user["id"], user["username"], bool(user["is_admin"]))
    return {"tokens": pair}


@app.post("/api/auth/logout")
async def auth_logout(req: AuthLogoutRequest, request: Request,
                      _: None = Depends(require_token)):
    """Invalidiert das übergebene Refresh-Token. Access-Token läuft sowieso
    nach JWT_ACCESS_TTL_MIN ab — Logout-Sicherheit kommt vom Refresh-Revoke.
    Wenn refresh_token nicht im Body: revoke ALLE refresh-tokens des Users
    (Logout-everywhere)."""
    from utils import auth_users as au
    user = request.state.user
    user_id = int(user.get("id", 0))
    if user_id <= 0:
        # Legacy/Setup-Auth → nichts zu logouten
        return {"ok": True, "revoked": 0}

    if req.refresh_token:
        claims = au.decode_jwt(req.refresh_token, expected_type="refresh")
        if claims and claims.get("jti"):
            au.revoke_refresh_token(claims["jti"])
            return {"ok": True, "revoked": 1}
        return {"ok": True, "revoked": 0}

    n = au.revoke_all_user_refresh_tokens(user_id)
    return {"ok": True, "revoked": n}


class TotpConfirmRequest(BaseModel):
    secret: str
    code: str


class TotpDisableRequest(BaseModel):
    password: str
    totp_code: Optional[str] = None


@app.post("/api/auth/totp-init")
async def auth_totp_init(request: Request, _: None = Depends(require_token)):
    """Generiert ein frisches TOTP-Secret + Provisioning-URI + QR-PNG (data-URL)
    für nachträgliches 2FA-Setup aus den Settings. Wird NICHT in der DB
    persistiert — der Aufrufer muss den ersten Code via /api/auth/totp-confirm
    verifizieren, das aktiviert das Secret dann scharf.
    Verify-First-Activate-Second.

    QR wird hier serverseitig als SVG gerendert + base64-data-URL, damit
    das Secret nicht an einen externen QR-Service raussickert — der otpauth-URI
    enthält das Secret im Klartext. SVG vermeidet Pillow-Dependency.

    409 wenn TOTP für den User schon aktiv ist — erst /totp-disable nötig."""
    from utils import auth_users as au
    import qrcode
    import qrcode.image.svg as qrcode_svg
    import base64
    from io import BytesIO

    user = request.state.user
    user_id = int(user.get("id", 0))
    if user_id <= 0:
        raise HTTPException(status_code=403, detail="TOTP-Init nur für eingeloggte User")
    db_user = au.get_user_by_id(user_id)
    if not db_user:
        raise HTTPException(status_code=401, detail="User nicht mehr gefunden")
    if db_user["totp_secret"]:
        raise HTTPException(
            status_code=409,
            detail="TOTP ist bereits aktiv — erst deaktivieren, dann neu einrichten.",
        )
    secret = au.generate_totp_secret()
    uri = au.totp_provisioning_uri(secret, db_user["username"])

    img = qrcode.make(uri, image_factory=qrcode_svg.SvgImage)
    buf = BytesIO()
    img.save(buf)
    qr_data_url = "data:image/svg+xml;base64," + base64.b64encode(buf.getvalue()).decode("ascii")

    return {"secret": secret, "uri": uri, "qr_data_url": qr_data_url}


@app.post("/api/auth/totp-confirm")
async def auth_totp_confirm(req: TotpConfirmRequest, request: Request,
                             _: None = Depends(require_token)):
    """Verifiziert den ersten TOTP-Code BEVOR das Secret scharf in der DB
    landet. So kann sich ein User mit kaputtem Authenticator-Setup nicht
    aussperren — TOTP wird nur aktiviert wenn er nachweisen kann dass
    seine App den Code korrekt generiert."""
    from utils import auth_users as au
    user = request.state.user
    user_id = int(user.get("id", 0))
    if user_id <= 0:
        raise HTTPException(status_code=403, detail="TOTP-Confirm nur für eingeloggte User")

    if not au.verify_totp_code(req.secret, req.code):
        raise HTTPException(status_code=401, detail="TOTP-Code falsch oder abgelaufen")

    au.set_totp_secret(user_id, req.secret)
    return {"ok": True}


@app.post("/api/auth/totp-disable")
async def auth_totp_disable(req: TotpDisableRequest, request: Request,
                             _: None = Depends(require_token)):
    """Deaktiviert TOTP für den eingeloggten User. Verlangt Password-Re-Verify
    plus aktuellen TOTP-Code (wenn 2FA gerade aktiv ist) — sonst könnte ein
    geklauter Access-Token allein genügen, um 2FA auszuhebeln. Defense gegen
    Session-Hijack."""
    from utils import auth_users as au
    user = request.state.user
    user_id = int(user.get("id", 0))
    if user_id <= 0:
        raise HTTPException(status_code=403, detail="TOTP-Disable nur für eingeloggte User")
    db_user = au.get_user_by_id(user_id)
    if not db_user:
        raise HTTPException(status_code=401, detail="User nicht mehr gefunden")

    if not au.verify_password(req.password, db_user["password_hash"]):
        raise HTTPException(status_code=401, detail="Passwort falsch")

    if db_user["totp_secret"]:
        secret = au.get_user_totp_secret(user_id)
        if not secret or not au.verify_totp_code(secret, req.totp_code or ""):
            raise HTTPException(
                status_code=401,
                detail="TOTP-Code fehlt oder ist falsch",
            )

    au.disable_totp(user_id)
    return {"ok": True}


class PatCreateRequest(BaseModel):
    name: str
    expires_in_days: Optional[int] = None  # None ⇒ unbegrenzt


def _user_id_from_request(request: Request) -> int:
    """Extrahiert die User-ID aus dem authentifizierten Request. Wirft 403
    für legacy/setup-Auth (kein User-Datensatz dahinter — PATs gehören zu
    einem konkreten User)."""
    user = request.state.user
    user_id = int(user.get("id", 0))
    if user_id <= 0:
        raise HTTPException(
            status_code=403,
            detail="API-Tokens sind nur für eingeloggte User mit Account verfügbar.",
        )
    return user_id


@app.post("/api/auth/pats")
async def auth_pats_create(req: PatCreateRequest, request: Request,
                            _: None = Depends(require_token),
                            __: None = Depends(require_admin)):
    """Erstellt einen neuen Personal Access Token. Plain-Token wird NUR
    in dieser Antwort zurückgegeben — Backend speichert nur den sha256-Hash.
    Caller (Frontend) muss den Plain-Token einmalig anzeigen + verwerfen.

    Admin-only: PATs sind System-zu-System-Credentials (Plugin-Auth) und
    sollten nur vom Admin angelegt werden. Reguläre User haben keinen
    Use-Case für eigene Long-Lived-Tokens."""
    from utils import auth_users as au
    user_id = _user_id_from_request(request)
    name = (req.name or "").strip()
    if not name or len(name) > 64:
        raise HTTPException(status_code=400, detail="Name muss 1–64 Zeichen lang sein.")

    expires_at_ms: Optional[int] = None
    if req.expires_in_days and req.expires_in_days > 0:
        expires_at_ms = int(time.time() * 1000) + req.expires_in_days * 24 * 60 * 60 * 1000

    try:
        pat = au.issue_pat(user_id, name, expires_at_ms=expires_at_ms)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return pat


@app.get("/api/auth/pats")
async def auth_pats_list(request: Request,
                          _: None = Depends(require_token),
                          __: None = Depends(require_admin)):
    """Liste aller PATs des eingeloggten Admins — ohne Plain-Token (gibt's nicht
    mehr, nur prefix für Identifikation)."""
    from utils import auth_users as au
    user_id = _user_id_from_request(request)
    return {"pats": au.list_pats(user_id)}


@app.delete("/api/auth/pats/{pat_id}")
async def auth_pats_revoke(pat_id: int, request: Request,
                            _: None = Depends(require_token),
                            __: None = Depends(require_admin)):
    """Hard-Delete eines PATs. Owner-Check ist im SQL — der Admin kann nur
    seine eigenen Tokens widerrufen, fremde IDs returnieren 404."""
    from utils import auth_users as au
    user_id = _user_id_from_request(request)
    ok = au.revoke_pat(pat_id, user_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Token nicht gefunden.")
    return {"ok": True}


# ─────────────────────────────────────────────────────────────────────
# Provider-Configs (Admin-only) — UI-editierbar statt env
# ─────────────────────────────────────────────────────────────────────

# Schema einer Provider-Config: name, list of fields. secret-Felder werden
# bei GET nicht im Klartext zurückgegeben, sondern nur als bool "is_set".
# Frontend zeigt "••••••" wenn is_set=true, leer wenn false. PUT erwartet
# alle Felder; leere Strings ⇒ Setting löschen (zurück zu env-Default).
_PROVIDER_DEFINITIONS: Dict[str, Dict[str, Any]] = {
    "spotify": {
        "label": "Spotify",
        "fields": [
            {"key": "spotify.client_id", "label": "Client ID", "secret": False},
            {"key": "spotify.client_secret", "label": "Client Secret", "secret": True},
            {"key": "spotify.redirect_uri", "label": "Redirect URI", "secret": False},
        ],
    },
    "navidrome": {
        "label": "Navidrome",
        "fields": [
            {"key": "navidrome.api_url", "label": "API URL", "secret": False},
            {"key": "navidrome.username", "label": "Username", "secret": False},
            {"key": "navidrome.password", "label": "Password", "secret": True},
        ],
    },
    "youtube": {
        "label": "YouTube",
        "fields": [
            {"key": "youtube.cookies_path", "label": "Cookies File Path", "secret": False},
        ],
    },
}


class ProviderUpdateRequest(BaseModel):
    # Felder als dict {key: value} — keys müssen zur Provider-Definition passen.
    # Leere Strings ⇒ delete_setting (zurück zum env-Default).
    fields: Dict[str, str]


@app.get("/api/providers/config")
async def providers_config_get(_: None = Depends(require_token),
                                 __: None = Depends(require_admin)):
    """Liefert alle Provider mit ihrem aktuellen Konfigurations-Stand.
    Secret-Felder werden NICHT im Klartext rausgegeben — nur ein Flag
    ``is_set`` damit das UI ein masked Placeholder rendern kann."""
    from utils.app_settings import get_setting

    out = []
    for name, defn in _PROVIDER_DEFINITIONS.items():
        fields_out = []
        for f in defn["fields"]:
            val = get_setting(f["key"])
            fields_out.append({
                "key": f["key"],
                "label": f["label"],
                "secret": f["secret"],
                "is_set": bool(val),
                # Plain-text-Wert nur für nicht-secret-Felder
                "value": val if not f["secret"] and val else "",
            })
        out.append({"name": name, "label": defn["label"], "fields": fields_out})
    return {"providers": out}


@app.put("/api/providers/{provider_name}")
async def providers_config_put(provider_name: str, req: ProviderUpdateRequest,
                                 _: None = Depends(require_token),
                                 __: None = Depends(require_admin)):
    """Schreibt Provider-Config. Leere Werte ⇒ delete_setting (zurück zu
    env-Defaults). Änderungen werden erst beim nächsten Container-Restart
    aktiv — Service-Instanzen werden nicht hot-reloaded."""
    from utils.app_settings import set_setting, delete_setting

    defn = _PROVIDER_DEFINITIONS.get(provider_name)
    if not defn:
        raise HTTPException(status_code=404, detail="Unbekannter Provider.")

    valid_keys = {f["key"]: f for f in defn["fields"]}
    for key, value in req.fields.items():
        if key not in valid_keys:
            raise HTTPException(status_code=400, detail=f"Unbekanntes Feld: {key}")
        secret = valid_keys[key]["secret"]
        if value == "":
            delete_setting(key)
        else:
            set_setting(key, value, encrypted=secret)

    return {"ok": True, "restart_required": True}


# ─────────────────────────────────────────────────────────────────────
# Worker-Cooldown-Konfig (Admin-only) — UI-editierbar
# ─────────────────────────────────────────────────────────────────────

# DB-Keys synchron mit utils/worker.py:_load_cooldown_ranges
_COOLDOWN_KEYS = ('normal_min_s', 'normal_max_s', 'rl_min_s', 'rl_max_s')


class CooldownUpdateRequest(BaseModel):
    normal_min_s: int
    normal_max_s: int
    rl_min_s: int
    rl_max_s: int


@app.get("/api/settings/cooldown")
async def settings_cooldown_get(_: None = Depends(require_token),
                                  __: None = Depends(require_admin)):
    """Liefert aktuelle Cooldown-Werte + Defaults (für Reset-Buttons)."""
    from utils.app_settings import get_setting
    from utils.worker import _COOLDOWN_NORMAL, _COOLDOWN_429

    defaults = {
        'normal_min_s': _COOLDOWN_NORMAL[0],
        'normal_max_s': _COOLDOWN_NORMAL[1],
        'rl_min_s': _COOLDOWN_429[0],
        'rl_max_s': _COOLDOWN_429[1],
    }
    current = {}
    for k in _COOLDOWN_KEYS:
        v = get_setting(f'cooldown.{k}')
        try:
            current[k] = int(v) if v is not None else defaults[k]
        except (ValueError, TypeError):
            current[k] = defaults[k]
    return {'current': current, 'defaults': defaults}


@app.put("/api/settings/cooldown")
async def settings_cooldown_put(req: CooldownUpdateRequest,
                                  _: None = Depends(require_token),
                                  __: None = Depends(require_admin)):
    """Schreibt die 4 Cooldown-Werte. Sanity-Checks: alle non-negative,
    min ≤ max in beiden Ranges. Hot-Reload — Worker liest beim nächsten
    Cooldown-Tick die neuen Werte."""
    from utils.app_settings import set_setting

    if any(v < 0 for v in (req.normal_min_s, req.normal_max_s, req.rl_min_s, req.rl_max_s)):
        raise HTTPException(status_code=400, detail='Werte müssen ≥ 0 sein.')
    if req.normal_min_s > req.normal_max_s:
        raise HTTPException(status_code=400, detail='Normal-Min darf nicht größer als Normal-Max sein.')
    if req.rl_min_s > req.rl_max_s:
        raise HTTPException(status_code=400, detail='Rate-Limit-Min darf nicht größer als Rate-Limit-Max sein.')

    set_setting('cooldown.normal_min_s', str(req.normal_min_s))
    set_setting('cooldown.normal_max_s', str(req.normal_max_s))
    set_setting('cooldown.rl_min_s', str(req.rl_min_s))
    set_setting('cooldown.rl_max_s', str(req.rl_max_s))
    return {'ok': True}


# ─────────────────────────────────────────────────────────────────────
# User-Management (Admin-only)
# ─────────────────────────────────────────────────────────────────────

class UserCreateRequest(BaseModel):
    username: str
    password: str
    is_admin: bool = False


class UserPatchRequest(BaseModel):
    is_admin: Optional[bool] = None
    password: Optional[str] = None
    # Pflichtfeld bei Self-Password-Change. Bei Admin-Reset auf einen
    # anderen User wird es ignoriert (Admin-Privileg). Backend entscheidet
    # anhand user_id == me.id ob es required ist.
    current_password: Optional[str] = None


def _public_user_record(u: Dict[str, Any]) -> Dict[str, Any]:
    """Filter sensitive Felder (password_hash, totp_secret) bevor wir Records
    ans Frontend geben. totp_enabled wird als bool surfacet (statt das
    Secret selbst zu enthüllen)."""
    return {
        "id": u.get("id"),
        "username": u.get("username"),
        "is_admin": bool(u.get("is_admin")),
        "totp_enabled": bool(u.get("totp_enabled")) if "totp_enabled" in u else bool(u.get("totp_secret")),
        "created_at_ms": u.get("created_at_ms"),
        "last_login_at_ms": u.get("last_login_at_ms"),
    }


@app.get("/api/auth/users")
async def auth_users_list(_: None = Depends(require_token),
                           __: None = Depends(require_admin)):
    """Liste aller User. Admin-only."""
    from utils import auth_users as au
    return {"users": [_public_user_record(u) for u in au.list_users()]}


@app.post("/api/auth/users")
async def auth_users_create(req: UserCreateRequest,
                              _: None = Depends(require_token),
                              __: None = Depends(require_admin)):
    """Legt neuen User an. Admin-only.
    400 wenn Username schon existiert oder Validation fehlschlägt."""
    from utils import auth_users as au
    try:
        user = au.create_user(req.username, req.password, is_admin=req.is_admin)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"user": _public_user_record(user)}


@app.delete("/api/auth/users/{user_id}")
async def auth_users_delete(user_id: int, request: Request,
                              _: None = Depends(require_token),
                              __: None = Depends(require_admin)):
    """Hard-Delete. Schutz:
    - Self-Delete: nicht erlaubt (Self-Lockout-Risiko)
    - Last-Admin-Delete: nicht erlaubt (System wäre unverwaltbar)
    Cascade: User's PATs + refresh_tokens werden mit gelöscht."""
    from utils import auth_users as au
    me = request.state.user
    if int(me.get("id", 0)) == user_id:
        raise HTTPException(status_code=400, detail="Du kannst dich nicht selbst löschen.")
    target = au.get_user_by_id(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User nicht gefunden.")
    if bool(target.get("is_admin")) and au.admin_count() <= 1:
        raise HTTPException(
            status_code=400,
            detail="Letzter Admin — kann nicht gelöscht werden.",
        )
    au.delete_user(user_id)
    return {"ok": True}


@app.patch("/api/auth/users/{user_id}")
async def auth_users_patch(user_id: int, req: UserPatchRequest, request: Request,
                             _: None = Depends(require_token),
                             __: None = Depends(require_admin)):
    """Toggle Admin-Flag oder reset Password. Admin-only.
    Schutz: Last-Admin-Demotion (eigener oder fremder Account) wird verhindert."""
    from utils import auth_users as au
    target = au.get_user_by_id(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User nicht gefunden.")

    if req.is_admin is not None:
        # Demotion eines Admins blockieren wenn er der letzte ist (egal ob
        # Self oder von anderem Admin getriggert).
        if not req.is_admin and bool(target.get("is_admin")) and au.admin_count() <= 1:
            raise HTTPException(
                status_code=400,
                detail="Letzter Admin — kann nicht demoted werden.",
            )
        au.set_user_admin(user_id, req.is_admin)

    if req.password is not None:
        # Self-Password-Change verlangt Re-Verify mit current_password —
        # Defense gegen Session-Hijack, ein gestohlener Access-Token soll
        # nicht direkt das Master-Credential ändern können. Admin-Reset
        # auf einen anderen User braucht das nicht (Admin-Privileg).
        me = request.state.user
        is_self = int(me.get("id", 0)) == user_id
        if is_self:
            if not req.current_password:
                raise HTTPException(
                    status_code=400,
                    detail="Aktuelles Passwort ist erforderlich für eigene Passwort-Änderung.",
                )
            if not au.verify_password(req.current_password, target["password_hash"]):
                raise HTTPException(status_code=401, detail="Aktuelles Passwort ist falsch.")
        try:
            au.update_user_password(user_id, req.password)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    return {"ok": True}


# ─────────────────────────────────────────────────────────────────────
# Banned-IPs (Admin-only)
# ─────────────────────────────────────────────────────────────────────

@app.get("/api/auth/banned-ips")
async def auth_banned_ips_list(request: Request,
                                _: None = Depends(require_token),
                                __: None = Depends(require_admin)):
    """Liste aller lifetime-gebannten IPs (5+ Failed-Logins/24h). Admin-only."""
    from utils import auth_users as au
    return {"banned": au.list_banned_ips()}


@app.delete("/api/auth/banned-ips/{ip:path}")
async def auth_banned_ips_unban(ip: str, request: Request,
                                  _: None = Depends(require_token),
                                  __: None = Depends(require_admin)):
    """Entbannt eine IP. Path-Parameter mit `:path`-Converter, weil IPv6-Adressen
    Doppelpunkte enthalten und sonst von FastAPI's Default-Path-Matcher
    falsch geparst würden. 404 wenn IP nicht gebannt war."""
    from utils import auth_users as au
    ok = au.unban_ip(ip)
    if not ok:
        raise HTTPException(status_code=404, detail="IP war nicht gebannt.")
    return {"ok": True}


@app.get("/api/auth/me")
async def auth_me(request: Request, _: None = Depends(require_token)):
    """Currently authenticated user — Frontend nutzt das beim Mount um zu
    erkennen ob die Session noch valid ist und welche Berechtigungen
    der User hat."""
    user = request.state.user
    if user.get("auth_method") in ("legacy", "setup"):
        return {
            "id": user["id"],
            "username": user["username"],
            "is_admin": user["is_admin"],
            "auth_method": user["auth_method"],
            "totp_enabled": False,
        }
    from utils import auth_users as au
    db_user = au.get_user_by_id(user["id"])
    if not db_user:
        raise HTTPException(status_code=401, detail="User nicht mehr gefunden")
    return {
        "id": db_user["id"],
        "username": db_user["username"],
        "is_admin": bool(db_user["is_admin"]),
        "auth_method": user["auth_method"],
        "totp_enabled": bool(db_user["totp_secret"]),
        "last_login_at_ms": db_user["last_login_at_ms"],
    }


# SPA-Frontend (SvelteKit-Build) — MUSS als letzter Mount stehen, sonst
# werden alle danach definierten Routes von der StaticFiles-Catch-All
# verschluckt. html=True liefert index.html für unbekannte Pfade
# (Client-Side-Routing-Fallback) — ergänzt durch fallback in svelte.config.js.
class SpaStaticFiles(StaticFiles):
    """SvelteKit-Build mit korrekten Cache-Headern + SPA-Routing-Fallback.

    - HTML (index.html, Fallback-Pfade): kein Cache → User sieht nach Build-Update
      sofort die neue Version, weil die hashed JS/CSS-URLs neu darin stehen.
    - Hashed Assets (/_app/immutable/*): aggressiv gecached (1 Jahr, immutable),
      weil ihr Inhalt durch den Hash im Pfad eindeutig identifiziert ist.
    - SPA-Routes (z.B. /album/123, /queue, /settings): wenn 404 kommt und der
      Pfad keine Datei-Extension hat und nicht unter /api/ liegt → index.html
      servieren, damit der Client-Router die Route aufnimmt. Starlettes
      `html=True` macht das nur für directory paths, nicht für deep links.
    """

    async def get_response(self, path: str, scope):
        # Starlettes StaticFiles wirft bei 404 eine StarletteHTTPException —
        # nicht ein Response-Objekt mit status 404. FastAPI's HTTPException ist
        # eine Subklasse, deckt die Starlette-Parent NICHT ab. Wir fangen
        # explicit die Starlette-Variante.
        try:
            response = await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code != 404:
                raise
            last_segment = path.rsplit("/", 1)[-1] if path else ""
            looks_like_file = "." in last_segment
            is_api = path.startswith("api")
            if looks_like_file or is_api:
                raise
            response = await super().get_response("index.html", scope)

        if path.endswith('.html') or path == '' or path == '/':
            response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        elif '_app/immutable/' in path:
            response.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
        return response

_FRONTEND_BUILD_DIR = os.path.join(os.path.dirname(__file__), "frontend", "build")
if os.path.isdir(_FRONTEND_BUILD_DIR):
    app.mount("/", SpaStaticFiles(directory=_FRONTEND_BUILD_DIR, html=True), name="ui")
else:
    print(f"[ui] frontend build not found at {_FRONTEND_BUILD_DIR} — UI disabled")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=config.API_HOST, port=config.API_PORT)

