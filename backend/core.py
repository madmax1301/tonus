"""Gemeinsamer Zustand und Helfer für app.py und die Router.

Hier liegen die Service-Singletons (Deezer, Spotify, YouTube, Metadata,
Navidrome), die Rate-Limiter und die Helfer, die mehrere Endpoint-Gruppen
brauchen. Router importieren von hier statt aus app.py, damit es keinen
Zirkelimport gibt.

WICHTIG: erst importieren, nachdem ``config.apply_db_overrides()`` gelaufen
ist. Die Services lesen config.X im Konstruktor; app.py ruft die Overrides
deshalb direkt vor ``import core`` auf.
"""

from __future__ import annotations

import os
from typing import Dict, Optional

from fastapi import HTTPException

import config
from services.deezer import DeezerService
from services.metadata import MetadataService
from services.navidrome import NavidromeService
from services.spotify import SpotifyService
from services.youtube import YouTubeService
from utils.file_handler import get_download_path
from utils.job_store import get_job
from utils.rate_limit import make_rate_limiter

# ── Services ────────────────────────────────────────────────────────────
deezer_service = DeezerService()
try:
    spotify_service = SpotifyService()
except Exception as e:
    print(f"Spotify not available (optional): {e}")
    spotify_service = None

youtube_service = YouTubeService()
metadata_service = MetadataService()
navidrome_service = NavidromeService()


# ── Provider, Limits, Queue-Helfer ──────────────────────────────────────

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



# ── Duplikat-Prüfung ────────────────────────────────────────────────────

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
