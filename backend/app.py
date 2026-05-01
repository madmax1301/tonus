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

# Plugin-Sync-State: wird vom Background-Task `_run_plugin_sync` befüllt und
# vom Endpoint `/api/plugin/sync-status` gelesen, damit das Navidrome-Plugin
# das Ergebnis seines letzten Triggers anzeigen kann. Module-global statt
# DB, weil es immer nur einen Eintrag gibt (letzter Run) und FastAPI mit
# BackgroundTasks im selben Prozess läuft.
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
    reset_stale_csv_jobs,
    upsert_job,
    get_job,
    get_album_aggregate,
    record_completed_download,
    has_completed_download,
    upsert_csv_job,
    get_csv_job,
    insert_csv_results,
    get_csv_results,
    count_csv_results,
)
from utils.worker import JobWorker
from utils.auth import require_token, auth_required

ALLOWED_METADATA_PROVIDERS = frozenset({"deezer", "spotify"})

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

app = FastAPI(title="Tonus API", version="1.0.0")

init_jobs_db()
_stale = reset_stale_inflight_jobs()
if _stale:
    print(f"Reset {_stale} stale download job(s) (queued/processing) after server start")
_csv_stale = reset_stale_csv_jobs()
if _csv_stale.get("jobs_reset") or _csv_stale.get("rows_purged"):
    print(
        f"Reset {_csv_stale['jobs_reset']} stale CSV import job(s) and purged "
        f"{_csv_stale['rows_purged']} pending/claimed staging rows"
    )


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

# Background worker — zwei unabhängige Threads: Downloads + CSV-Import parallel
_download_worker = JobWorker(job_type="download")
_csv_worker = JobWorker(job_type="csv")
_download_worker.start()
_csv_worker.start()
print("Worker threads started (download + csv)")

@app.on_event("shutdown")
def _shutdown_workers():
    print("Shutting down workers...")
    _download_worker.shutdown(timeout=60)
    _csv_worker.shutdown(timeout=300)
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


class URLDownloadRequest(BaseModel):
    """Phase 1: direkter Download einer beliebigen yt-dlp-URL (YouTube/SoundCloud/...)"""
    url: str
    location: Optional[str] = "local"
    format: Optional[str] = None
    quality: Optional[str] = None
    max_retries: Optional[int] = 0
    navidrome_library: Optional[str] = None


class URLSearchRequest(BaseModel):
    """Phase 2: Suche via yt-dlp ytsearch/scsearch"""
    query: str
    source: Optional[str] = "youtube"  # 'youtube' | 'soundcloud'
    limit: Optional[int] = 10


class PluginSyncRequest(BaseModel):
    """Trigger-Body für /api/plugin/sync — vom Navidrome-Plugin gepostet.

    Ruft die Discovery-Pipeline (LB-Top-Artists → Deezer-Radio) und queut
    fehlende Tracks asynchron in download_jobs. Returnt sofort, damit der
    Plugin-Cron-Callback nicht im 30 s-Hostlimit hängt.

    `playlist_name` (optional): wenn gesetzt, werden alle gequeuten Tracks
    dieses Runs einer gleichnamigen Subsonic-Playlist in Navidrome hinzugefügt
    sobald sie heruntergeladen sind. Der Platzhalter ``{date}`` wird durch
    das aktuelle Datum (YYYY-MM-DD) ersetzt — typischer Wert
    ``"Discovery {date}"``. Leerstring oder None deaktiviert die Playlist.

    `navidrome_user` (optional, Multi-User-Setup): identifiziert den Navidrome-
    User, dem die Tracks dieses Runs gehören. Wird im Track-Payload-Marker
    gespeichert und vom Plugin via /api/plugin/finished-tracks zurückgelesen,
    damit das Plugin im Namen DIESES Users die Subsonic-Playlist erstellen
    kann. Wenn None, übernimmt das Backend selbst die Playlist-Erstellung
    (alter Single-User-Pfad, Admin-Auth)."""
    listenbrainz_user: str
    top_artists: int = 10
    tracks_per_artist: int = 5
    history_days: int = 90
    max_total: int = 50
    location: Optional[str] = "navidrome"
    playlist_name: Optional[str] = None
    navidrome_user: Optional[str] = None

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


@app.get("/api/queue")
async def list_queue(
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=2000, ge=1, le=10000),
    status: Optional[str] = Query(default=None),
):
    """Return queued/active/recent jobs (paginiert).

    Default-Limit erhöht von 200 auf 2000, damit CSV-Bulk-Imports vollständig sichtbar
    sind. Optionaler offset für echte Pagination, optionaler status-Filter (csv-werte
    'queued','processing','completed','error' oder kombiniert wie 'queued,processing').
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

        rows = conn.execute(
            f"""
            SELECT job_id, status, stage, progress, message, download_url,
                   created_at_ms, updated_at_ms, payload_json
            FROM download_jobs
            WHERE status IN ({placeholders})
            ORDER BY created_at_ms DESC, rowid DESC
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
        # Status-Aggregat fürs UI (wie viele queued/processing/completed/error)
        agg_rows = conn.execute(
            f"SELECT status, COUNT(*) AS n FROM download_jobs WHERE status IN ({placeholders}) GROUP BY status",
            tuple(wanted),
        ).fetchall()
        status_counts = {r["status"]: r["n"] for r in agg_rows}
        return {
            "items": items,
            "total": total,
            "shown": len(items),
            "offset": offset,
            "limit": limit,
            "status_counts": status_counts,
        }
    finally:
        conn.close()


@app.get("/api/queue/stats")
async def queue_stats():
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
async def download_track(request: DownloadRequest, background_tasks: BackgroundTasks, _: None = Depends(require_token)):
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
            # Validate manual metadata (name + artist required)
            md = metadata or {}
            name = (md.get('name') or md.get('title') or '').strip()
            artist = (md.get('artist') or '').strip()
            if not name or not artist:
                upsert_job(job_id, status="error", message="Manual metadata requires 'name' (song title) and 'artist'",
                           progress=0)
                return

            # Default album/album_artist to "YouTube" if not provided
            album_artist = (md.get('album_artist') or '').strip() or "YouTube"
            album = (md.get('album') or md.get('album_name') or '').strip() or "YouTube"

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
                'duration_ms': 0,
                'external_url': yt_info.get('webpage_url') or youtube_url,
                'preview_url': None,
            }

        upsert_job(job_id, status="processing", message="Preparing download location...", stage="preparing",
                   progress=20)

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

    upsert_job(
        job_id,
        status="queued",
        message=f"Reverse download queued for {location_msg}",
        progress=0,
        stage="queued",
        payload={
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



@app.post("/api/download/album")
async def download_album(request: AlbumDownloadRequest, background_tasks: BackgroundTasks, _: None = Depends(require_token)):
    """Start downloading all tracks from an album"""

    provider = resolve_metadata_provider(request.provider)
    svc = get_metadata_service(provider)
    album_job_id = f"album:{request.album_id}"

    album = svc.get_album_details(request.album_id)

    if not album:
        raise HTTPException(status_code=404, detail="Album not found")

    # Validate location
    location = request.location if request.location in ["local", "navidrome"] else "local"
    location_msg = "local downloads folder" if location == "local" else "Navidrome server"

    output_format = request.format or config.OUTPUT_FORMAT
    navidrome_path: Optional[str] = None
    if location == "navidrome":
        navidrome_path = resolve_navidrome_library_path_optional(request.navidrome_library)

    to_queue = []
    for track in album["tracks"]:
        if (
            get_duplicate_download_reason(
                track["id"],
                provider,
                location,
                output_format,
                navidrome_library_path=navidrome_path,
            )
            is None
        ):
            to_queue.append(track)

    if not to_queue:
        raise HTTPException(
            status_code=409,
            detail="All tracks from this album are already in your library.",
        )

    # Album-Aggregator: NICHT 'queued' — sonst greift ihn der Worker als Track-Download
    # ab, scheitert an svc.get_track_details('album:...') und landet als Geist-Eintrag
    # mit Cover aber ohne Track-Name in der Queue. Eigener Status 'album_meta' wird
    # vom Worker UND von /api/queue ignoriert, bleibt nur für /api/download/album/status
    # via get_job() abrufbar.
    upsert_job(
        album_job_id,
        status="album_meta",
        message=f"Album '{album['name']}' queued",
        stage="queued",
        progress=0,
        album_id=request.album_id,
        payload={
            "album_id": request.album_id,
            "album_name": album["name"],
            "artist": album["artist"],
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
            message=f"Queued (Album: {album['name']})",
            progress=0,
            stage="queued",
            album_id=request.album_id,
            payload={
                "provider": provider,
                "record_track_id": track["id"],
                "location": location,
                "video_id": None,
                "output_format": output_format,
                "audio_quality": request.quality,
                "metadata_provider": provider,
                "max_retries": _clamp_download_retries(request.max_retries),
                "navidrome_library_path": navidrome_path,
                "track": track_for_queue,
            },
        )
    # All tracks enqueued in SQLite — worker picks them up one by one

    skipped = len(album["tracks"]) - len(to_queue)
    return {
        "status": "queued",
        "message": f"Queued {len(to_queue)} track(s) from '{album['name']}' to {location_msg}"
        + (f" ({skipped} skipped — already in library)" if skipped else ""),
        "album_id": request.album_id,
        "total_tracks": len(to_queue),
        "skipped_tracks": skipped,
        "queued_track_ids": [t["id"] for t in to_queue],
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
async def health_check():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "default_metadata_provider": config.DEFAULT_METADATA_PROVIDER,
        "spotify_configured": spotify_service is not None,
        "navidrome_path": config.NAVIDROME_MUSIC_PATH,
        "navidrome_libraries": config.navidrome_libraries_public(),
    }

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
async def import_csv(request: CsvImportRequest, _: None = Depends(require_token)):
    """
    CSV-Import (persistent): speichert Job in SQLite, Worker holt ihn ab.
    Gibt sofort eine job_id zurück — Status unter /api/import/csv/status/{job_id} pollbar.
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

    # Auto-detect header: look for "artist" and "track"/"title" columns
    col_artist, col_title = 0, 1
    if rows and len(rows[0]) >= 2:
        first = [c.strip().lower().lstrip('\ufeff') for c in rows[0]]
        artist_cols = [i for i, c in enumerate(first) if 'artist' in c]
        title_cols  = [i for i, c in enumerate(first) if 'track' in c or 'title' in c or 'name' in c]
        if artist_cols and title_cols:
            col_artist, col_title = artist_cols[0], title_cols[0]
            rows = rows[1:]
    elif rows and any(kw in (rows[0][0].strip().lower().lstrip('\ufeff')) for kw in ['track', 'title', 'name']):
        col_artist, col_title = 1, 0
        rows = rows[1:]

    parsed = []
    for row in rows:
        if not row:
            continue
        artist, title = "", ""
        max_col = max(col_artist, col_title)
        if len(row) > max_col:
            artist = row[col_artist].strip().strip('"').strip("'") if col_artist < len(row) else ""
            title  = row[col_title].strip().strip('"').strip("'")  if col_title  < len(row) else ""
        elif len(row) == 1:
            parts = _re.split(r"\s*[-–—]\s*", row[0].strip(), maxsplit=1)
            if len(parts) == 2:
                artist = parts[0].strip()
                title = parts[1].strip()
            else:
                title = parts[0].strip()
        if title:
            parsed.append({"artist": artist, "title": title, "raw": row[0].strip() if row else f"{artist} {title}".strip()})

    # Eindeutige job_id auf Basis der Wall-Clock-Zeit (csv_import_jobs hat keine numeric id-Spalte)
    import time as _time
    from utils.job_store import _db as _csv_db
    job_id = f"csv-{int(_time.time() * 1000)}"

    # Falls eine ältere Session denselben job_id hinterlassen hat (extrem unwahrscheinlich,
    # aber wir wollen keine fremden Results mit den neuen vermischen), zuerst alle Reste löschen.
    conn = _csv_db()
    try:
        conn.execute("DELETE FROM csv_import_results WHERE job_id = ?", (job_id,))
        conn.execute("DELETE FROM csv_import_jobs    WHERE job_id = ?", (job_id,))
        conn.commit()
    finally:
        conn.close()

    upsert_csv_job(
        job_id,
        status="queued",
        total=len(parsed),
        message=f"Processing {len(parsed)} tracks...",
    )

    # Store parsed items in a temp table so the worker can read them
    # (use csv_import_results with result_type='pending' as staging)
    insert_csv_results(job_id, "pending_raw", [
        {"original": p["raw"], "requested_artist": p["artist"], "requested_title": p["title"]}
        for p in parsed
    ])

    # Add payload to csv_import_jobs so worker knows provider + search_limit
    conn = _csv_db()
    try:
        conn.execute(
            "UPDATE csv_import_jobs SET message = ? WHERE job_id = ?",
            (f"{provider}|{search_limit}|pending_raw", job_id),
        )
        conn.commit()
    finally:
        conn.close()

    return {"status": "queued", "job_id": job_id, "total": len(parsed)}


@app.get("/api/import/csv/status/{job_id}")
async def csv_import_status(job_id: str):
    """Poll CSV import progress (aus SQLite)."""
    job = get_csv_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="CSV import job not found")
    return {
        "status": job["status"],
        "total": job["total"],
        "processed": job["processed"],
        "found": job["found"],
        "not_found": job["not_found"],
        "message": job.get("message", ""),
    }


@app.get("/api/import/csv/result/{job_id}")
async def csv_import_result(
    job_id: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
):
    """CSV-Import-Ergebnisse paginiert aus SQLite."""
    job = get_csv_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="CSV import job not found")
    if job["status"] != "completed":
        raise HTTPException(status_code=400, detail="CSV import not yet completed")

    results = get_csv_results(job_id, offset=offset, limit=limit)
    counts = count_csv_results(job_id)
    return {
        "total": job["total"],
        "found": counts["matched"],
        "not_found": counts["unmatched"],
        "matched": results["matched"],
        "unmatched": results["unmatched"],
    }


@app.delete("/api/import/csv/{job_id}")
async def delete_csv_import(job_id: str, _: None = Depends(require_token)):
    """Löscht einen CSV-Import-Job inkl. aller matched/unmatched Results.

    Greift auf csv_import_jobs UND csv_import_results — der Job verschwindet
    komplett, kann danach mit derselben job_id nicht mehr abgerufen werden.
    Idempotent: löschen eines nicht-existierenden Jobs ist OK (deleted=0).
    """
    conn = _db()
    try:
        conn.execute("DELETE FROM csv_import_results WHERE job_id = ?", (job_id,))
        cur = conn.execute("DELETE FROM csv_import_jobs WHERE job_id = ?", (job_id,))
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


@app.post("/api/import/csv/queue-all/{job_id}")
async def csv_queue_all(job_id: str, req: CsvQueueAllRequest, _: None = Depends(require_token)):
    """Schreibe ALLE matched Tracks aus einem CSV-Import in die Download-Queue.

    Ersetzt den browserseitigen "for each downloadTrack(...)"-Loop, der bei großen
    Imports (30k+ Tracks) entweder am Pagination-Limit hing oder den Browser mit
    tausenden Einzelrequests blockiert hätte. Hier wird alles serverseitig in einer
    DB-Schleife in download_jobs eingefügt.
    """
    import json as _json

    csv_job = get_csv_job(job_id)
    if not csv_job:
        raise HTTPException(status_code=404, detail="CSV import job not found")
    if csv_job["status"] != "completed":
        raise HTTPException(status_code=400, detail="CSV import not yet completed")

    provider = resolve_metadata_provider(req.provider)
    location = req.location if req.location in ["local", "navidrome"] else "local"
    output_format = req.format or config.OUTPUT_FORMAT
    audio_quality = req.quality
    max_retries = _clamp_download_retries(req.max_retries)
    navidrome_path: Optional[str] = None
    if location == "navidrome":
        navidrome_path = resolve_navidrome_library_path_optional(req.navidrome_library)

    # ----- Schritt 1: alle matched Track-JSONs aus DB lesen + parsen -----
    conn = _db()
    try:
        rows = conn.execute(
            "SELECT track_json FROM csv_import_results WHERE job_id = ? AND result_type = 'matched'",
            (job_id,),
        ).fetchall()
    finally:
        conn.close()

    candidates: list = []  # list of (track_id, track_dict)
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
        candidates.append((tid, track))

    # ----- Schritt 2: bulk-Lookup auf existierende download_jobs (statt N Einzelqueries) -----
    skipped_dup = 0
    in_flight_ids: set = set()
    completed_provider_pairs: set = set()
    if candidates:
        conn = _db()
        try:
            track_ids = [tid for tid, _ in candidates]
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
    for tid, track in candidates:
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

    return {
        "queued": queued,
        "skipped_duplicate": skipped_dup,
        "errors": errors,
        "total_matched": len(rows),
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
):
    """Background task: yt-dlp lädt direkt via URL (kein Spotify/Deezer-Match).

    Tags werden aus den yt-dlp-Metadaten gebildet (Title=Track, Uploader=Artist,
    Thumbnail=Cover). Funktioniert für jede yt-dlp-unterstützte Quelle.
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

        track_info = {
            'id': job_id,
            'name': title,
            'artist': uploader,
            'artists': [uploader] if uploader else [],
            'album_artist': uploader,
            'album': uploader,  # SoundCloud/YouTube haben selten Album-Konzept
            'track_number': 1,
            'release_date': '',
            'album_art': thumb,
            'duration_ms': int((info.get('duration') or 0) * 1000),
            'external_url': webpage,
            'preview_url': None,
        }

        upsert_job(job_id, status="processing", message="Preparing download location...",
                   stage="preparing", progress=25)

        temp_dir = os.path.join(config.DOWNLOAD_DIR, "temp")
        Path(temp_dir).mkdir(parents=True, exist_ok=True)
        download_path = get_download_path(track_info, temp_dir, output_format)

        upsert_job(job_id, status="processing", message="Downloading from source...",
                   stage="downloading", progress=40)

        download_result = youtube_service.download_by_url(
            webpage, download_path, output_format, audio_quality
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


@app.post("/api/url/download")
async def url_download(req: URLDownloadRequest, background_tasks: BackgroundTasks, _: None = Depends(require_token)):
    """Phase 1: Direkter Download via URL ohne Spotify/Deezer-Match.

    Funktioniert für YouTube, SoundCloud, Bandcamp, Vimeo … alles was yt-dlp kennt.
    """
    if not req.url or not req.url.strip().startswith(('http://', 'https://')):
        raise HTTPException(status_code=400, detail="Bitte eine vollständige URL angeben (http/https).")

    location = req.location if req.location in ("local", "navidrome") else "local"
    output_format = req.format or config.OUTPUT_FORMAT
    navidrome_path: Optional[str] = None
    if location == "navidrome":
        navidrome_path = resolve_navidrome_library_path_optional(req.navidrome_library)

    # Stabile job_id aus URL — verhindert Duplikate beim doppelten Klick
    job_id = f"url-{abs(hash((req.url.strip(), location, output_format))) % 10_000_000}"

    track_for_queue = {
        "id": job_id,
        "name": req.url.strip(),  # wird vom Worker mit echtem Title überschrieben
        "artist": "URL",
        "album": "",
        "album_art": "",
    }

    upsert_job(
        job_id,
        status="queued",
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
    """Phase 2: Suche via yt-dlp (ytsearchN: oder scsearchN:)."""
    src = (req.source or "youtube").lower().strip()
    if src not in ("youtube", "soundcloud"):
        raise HTTPException(status_code=400, detail="source muss 'youtube' oder 'soundcloud' sein")

    result = youtube_service.search_url(req.query, source=src, limit=req.limit or 10)
    if not result.get('success'):
        raise HTTPException(status_code=502, detail=result.get('error', 'Search failed'))
    return {
        "source": src,
        "query": req.query,
        "results": result.get('results', []),
    }


# ---------------------------------------------------------------------------
# Plugin-spezifische Endpoints
#
# Diese drei Routen sind die einzige Schnittstelle, die das Navidrome-Plugin
# (separates Repo `tonus-navidrome-plugin`) braucht. Alles andere kann es
# über die bestehenden /api/import/csv* + /api/download* Endpoints erreichen.
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


@app.get("/api/plugin/library/missing")
async def plugin_library_missing(
    listenbrainz_user: str = Query(..., min_length=1),
    top_artists: int = Query(10, ge=1, le=50),
    tracks_per_artist: int = Query(5, ge=1, le=20),
    history_days: int = Query(90, ge=1, le=3650),
    max_total: int = Query(50, ge=1, le=500),
):
    """Discovery-Quelle für das Plugin: liefert eine kuratierte Track-Liste
    auf Basis der ListenBrainz-Top-Artists des Users (Deezer-Artist-Radio,
    gefiltert gegen die eigene Hör-History).

    Antwort enthält Deezer-Track-IDs und ein schlankes track_hint-Objekt pro
    Item, sodass das Plugin direkt /api/download pro Item triggern kann
    (statt den langsamen CSV-Import-Pfad mit Polling — Navidrome killt
    Plugin-Callbacks die länger als ~30 s laufen)."""
    from services.discovery import discover_via_artist_radio

    items = discover_via_artist_radio(
        listenbrainz_user=listenbrainz_user,
        top_artists=top_artists,
        tracks_per_artist=tracks_per_artist,
        history_days=history_days,
        max_total=max_total,
    )

    out_items = []
    for it in items:
        track = it.get("deezer_track") or {}
        track_id = str(track.get("id", ""))
        if not track_id:
            continue
        artist_obj = track.get("artist") or {}
        album_obj = track.get("album") or {}
        out_items.append({
            "artist": it["artist"],
            "title": it["title"],
            "track_id": track_id,
            "track_hint": {
                "id": track_id,
                "name": track.get("title", ""),
                "artist": artist_obj.get("name", ""),
                "album": album_obj.get("title", ""),
                "album_art": (
                    album_obj.get("cover_xl")
                    or album_obj.get("cover_big")
                    or album_obj.get("cover_medium")
                ),
            },
        })

    return {
        "items": out_items,
        "count": len(out_items),
        "source": "listenbrainz+deezer-radio",
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


def _reconcile_plugin_playlists(max_age_days: int = 60) -> Dict[str, int]:
    """Findet alle 'completed'-Tracks der letzten N Tage, die zu einem
    plugin-sync-Run gehören (Marker ``plugin_sync_playlist_name`` im payload),
    und fügt sie idempotent zu ihrer Subsonic-Playlist in Navidrome hinzu.

    Architektur: Wir tracken keine separate Sync-Run-Tabelle, sondern lesen
    den Marker aus ``download_jobs.payload_json``. Idempotenz greift über
    read-before-write in ``add_tracks_to_playlist`` — der Helper kann beliebig
    oft laufen ohne Duplikate zu erzeugen.

    Wird automatisch am Anfang/Ende jedes ``_run_plugin_sync`` aufgerufen.
    Tracks deren Subsonic-Index-Eintrag noch nicht existiert (Scanner war
    noch nicht durch) werden beim nächsten Reconcile-Lauf nachgezogen.
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
              AND payload_json LIKE '%plugin_sync_playlist_name%'
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
        playlist_name = payload.get("plugin_sync_playlist_name")
        if not playlist_name:
            continue
        # Multi-User-Modus: Tracks mit `plugin_sync_navidrome_user`-Marker
        # werden vom Plugin via Subsonic-API im Namen dieses Users gepushed.
        # Backend-Reconcile skippt diese Tracks, sonst würden Playlists
        # doppelt entstehen (einmal Admin-owned, einmal user-owned).
        if payload.get("plugin_sync_navidrome_user"):
            continue
        track_obj = payload.get("track") or {}
        artist = (track_obj.get("artist") or "").strip()
        title = (track_obj.get("name") or "").strip()
        if not artist or not title:
            continue
        by_playlist[playlist_name].append({"artist": artist, "title": title})

    if not by_playlist:
        return {"playlists": 0, "tracks_added": 0}

    total_added = 0
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
        unresolved = 0
        for it in items:
            sid = navidrome_service.find_track_id_by_artist_title(it["artist"], it["title"])
            if sid:
                sub_ids.append(sid)
            else:
                unresolved += 1
        if not sub_ids:
            print(
                f"[plugin-reconcile] '{playlist_name}': "
                f"no resolvable subsonic IDs yet ({unresolved} unresolved of {len(items)})"
            )
            continue

        result = navidrome_service.add_tracks_to_playlist(playlist_id, sub_ids)
        added = result.get("added", 0)
        total_added += added
        print(
            f"[plugin-reconcile] '{playlist_name}' (pid={playlist_id}): "
            f"+{added} tracks (already in playlist: {result.get('already_present', 0)}, "
            f"unresolved: {unresolved})"
        )

    return {"playlists": len(by_playlist), "tracks_added": total_added}


def _run_plugin_sync(req: PluginSyncRequest) -> None:
    """Background-Task hinter POST /api/plugin/sync.

    Macht in einem Schwung:
      1. Reconcile vorheriger Plugin-Runs (Tracks die inzwischen completed sind
         werden ihrer Playlist hinzugefügt)
      2. Discovery via services.discovery.discover_via_artist_radio
      3. Pro Track: Dup-Check + upsert_job ('queued') mit Plugin-Run-Markern
      4. Reconcile am Ende (nimmt Tracks mit, die schon in der Library waren
         und im Dup-Check geskipped wurden — die haben aber keine Marker, also
         streng genommen nutzlos hier; bleibt drin als safety net falls ein
         Worker-Track zwischenzeitlich fertig wurde)

    Schreibt während des Laufs in `_plugin_sync_state`, damit
    /api/plugin/sync-status den Fortschritt sehen kann.
    """
    from services.discovery import discover_via_artist_radio

    started = _now_ms()
    run_id = f"plugin-{started}"
    resolved_playlist = _resolve_playlist_name_template(req.playlist_name)
    with _plugin_sync_lock:
        _plugin_sync_state.update({
            "last_status": "running",
            "last_started_ms": started,
            "last_finished_ms": 0,
            "last_candidates": 0,
            "last_queued": 0,
            "last_skipped": 0,
            "last_failed": 0,
            "last_error": None,
            "last_run_id": run_id,
            "last_playlist_name": resolved_playlist,
        })

    def _set_final(status: str, error: Optional[str] = None) -> None:
        with _plugin_sync_lock:
            _plugin_sync_state["last_status"] = status
            _plugin_sync_state["last_finished_ms"] = _now_ms()
            if error is not None:
                _plugin_sync_state["last_error"] = error[:500] if error else None

    # Erst aufräumen: Tracks früherer Runs, die inzwischen heruntergeladen +
    # vom Scanner gefunden wurden, in ihre Playlist aufnehmen. Greift z.B. wenn
    # gestern 30 Tracks getriggert wurden, von denen heute 10 fertig sind.
    try:
        recon = _reconcile_plugin_playlists()
        if recon["tracks_added"] > 0 or recon["playlists"] > 0:
            print(
                f"[plugin-sync] pre-reconcile: {recon['tracks_added']} tracks "
                f"added across {recon['playlists']} playlists"
            )
    except Exception as e:
        # Reconcile darf den Sync-Trigger nicht blockieren — bei Fehler nur loggen.
        print(f"[plugin-sync] pre-reconcile failed (continuing): {e}")

    try:
        items = discover_via_artist_radio(
            listenbrainz_user=req.listenbrainz_user,
            top_artists=req.top_artists,
            tracks_per_artist=req.tracks_per_artist,
            history_days=req.history_days,
            max_total=req.max_total,
        )
    except Exception as e:
        _set_final("error", f"discovery failed: {e}")
        print(f"[plugin-sync] discovery error: {e}")
        return

    with _plugin_sync_lock:
        _plugin_sync_state["last_candidates"] = len(items)

    if not items:
        _set_final("ok", None)
        return

    location = req.location if req.location in ("local", "navidrome") else "navidrome"
    output_format = config.OUTPUT_FORMAT
    provider = "deezer"
    navidrome_path: Optional[str] = None
    if location == "navidrome":
        navidrome_path = resolve_navidrome_library_path_optional(None)

    queued = 0
    skipped = 0
    failed = 0
    location_msg = "local downloads folder" if location == "local" else "Navidrome server"

    for it in items:
        track = it.get("deezer_track") or {}
        track_id = str(track.get("id", ""))
        if not track_id:
            failed += 1
            continue

        artist_obj = track.get("artist") or {}
        album_obj = track.get("album") or {}
        track_hint = {
            "id": track_id,
            "name": track.get("title", ""),
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
                skipped += 1
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
                "plugin_sync_run_id": run_id,
            }
            if resolved_playlist:
                # Marker, anhand dessen _reconcile_plugin_playlists die Tracks
                # später ihrer Playlist zuordnet.
                payload_extra["plugin_sync_playlist_name"] = resolved_playlist
            if req.navidrome_user:
                # Multi-User-Modus: Plugin übernimmt Subsonic-Playlist-Erstellung
                # via /api/plugin/finished-tracks + host.SubsonicAPICall im Namen
                # dieses Users. Backend-Reconcile skippt Tracks mit diesem Marker.
                payload_extra["plugin_sync_navidrome_user"] = req.navidrome_user
            upsert_job(
                track_id,
                status="queued",
                message=f"Download queued for {location_msg} (plugin-sync)",
                progress=0,
                stage="queued",
                payload=payload_extra,
            )
            queued += 1
        except Exception as e:
            failed += 1
            print(f"[plugin-sync] queue fail for track {track_id}: {e}")

        if (queued + skipped + failed) % 5 == 0:
            with _plugin_sync_lock:
                _plugin_sync_state["last_queued"] = queued
                _plugin_sync_state["last_skipped"] = skipped
                _plugin_sync_state["last_failed"] = failed

    with _plugin_sync_lock:
        _plugin_sync_state["last_queued"] = queued
        _plugin_sync_state["last_skipped"] = skipped
        _plugin_sync_state["last_failed"] = failed

    elapsed_ms = _now_ms() - started
    print(
        f"[plugin-sync] done in {elapsed_ms} ms — candidates={len(items)} "
        f"queued={queued} skipped={skipped} failed={failed}"
    )

    # Post-Reconcile: greift hauptsächlich für Tracks die im Dup-Check
    # geskipped wurden (= waren schon in der Library), damit auch die in der
    # heutigen Playlist landen. Tracks die wir gerade neu gequeued haben sind
    # noch nicht 'completed', die kommen erst beim nächsten Pre-Reconcile rein.
    if resolved_playlist:
        try:
            recon = _reconcile_plugin_playlists()
            print(
                f"[plugin-sync] post-reconcile: {recon['tracks_added']} tracks "
                f"added across {recon['playlists']} playlists"
            )
        except Exception as e:
            print(f"[plugin-sync] post-reconcile failed: {e}")

    _set_final("ok", None)


@app.post("/api/plugin/sync")
async def plugin_sync(
    req: PluginSyncRequest,
    background_tasks: BackgroundTasks,
    _: None = Depends(require_token),
):
    """Plugin-Trigger: feuere-und-vergiss. Startet die Discovery+Queue-
    Pipeline im Hintergrund und returnt sofort, damit das Navidrome-Plugin
    nicht in seinem ~30 s-Callback-Timeout hängt. Status-Polling über
    /api/plugin/sync-status (Feld plugin_sync.last_status)."""
    background_tasks.add_task(_run_plugin_sync, req)
    return {"started": True, "message": "discovery+queue running in background"}


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


# SPA-Frontend (SvelteKit-Build) — MUSS als letzter Mount stehen, sonst
# werden alle danach definierten Routes von der StaticFiles-Catch-All
# verschluckt. html=True liefert index.html für unbekannte Pfade
# (Client-Side-Routing-Fallback) — ergänzt durch fallback in svelte.config.js.
class SpaStaticFiles(StaticFiles):
    """SvelteKit-Build mit korrekten Cache-Headern.

    - HTML (index.html, Fallback-Pfade): kein Cache → User sieht nach Build-Update
      sofort die neue Version, weil die hashed JS/CSS-URLs neu darin stehen.
    - Hashed Assets (/_app/immutable/*): aggressiv gecached (1 Jahr, immutable),
      weil ihr Inhalt durch den Hash im Pfad eindeutig identifiziert ist.
    """

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
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

