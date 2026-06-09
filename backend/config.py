import os
import re
from pathlib import Path
from typing import List, Dict, Any
from dotenv import load_dotenv

load_dotenv()

# Metadata provider: "deezer" (default, no API key) or "spotify" (requires credentials)
_raw_provider = os.getenv("DEFAULT_METADATA_PROVIDER", "deezer").lower().strip()
DEFAULT_METADATA_PROVIDER = _raw_provider if _raw_provider in ("deezer", "spotify") else "deezer"

# Spotify API (optional — only needed when using provider "spotify")
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET", "")
SPOTIFY_REDIRECT_URI = os.getenv("SPOTIFY_REDIRECT_URI", "http://localhost:8000/callback")

# Navidrome Configuration
# Multiple libraries: set NAVIDROME_MUSIC_PATHS to a comma- or newline-separated list of absolute paths.
# Optional NAVIDROME_MUSIC_LABELS: same order, comma/newline-separated labels (defaults to folder basename).
# If NAVIDROME_MUSIC_PATHS is unset, NAVIDROME_MUSIC_PATH (single path, default /music) is used.
def _parse_navidrome_paths() -> List[str]:
    raw = (os.getenv("NAVIDROME_MUSIC_PATHS") or "").strip()
    paths: List[str] = []
    if raw:
        for part in re.split(r"[\n,]", raw):
            p = part.strip()
            if not p:
                continue
            paths.append(os.path.abspath(os.path.expanduser(p)))
    if not paths:
        single = (os.getenv("NAVIDROME_MUSIC_PATH") or "/music").strip()
        paths.append(os.path.abspath(os.path.expanduser(single)))
    # Deduplicate while preserving order
    seen = set()
    out: List[str] = []
    for p in paths:
        np = os.path.normpath(p)
        if np not in seen:
            seen.add(np)
            out.append(p)
    return out


def _parse_navidrome_labels() -> List[str]:
    raw = (os.getenv("NAVIDROME_MUSIC_LABELS") or "").strip()
    if not raw:
        return []
    return [x.strip() for x in re.split(r"[\n,]", raw) if x.strip()]


NAVIDROME_MUSIC_PATHS_LIST = _parse_navidrome_paths()
NAVIDROME_MUSIC_PATH = NAVIDROME_MUSIC_PATHS_LIST[0]
_label_parts = _parse_navidrome_labels()


def navidrome_libraries_public() -> List[Dict[str, Any]]:
    """Configured Navidrome music roots for API/UI (path + short label)."""
    libs = []
    for i, path in enumerate(NAVIDROME_MUSIC_PATHS_LIST):
        if i < len(_label_parts):
            label = _label_parts[i]
        else:
            label = os.path.basename(path.rstrip(os.sep)) or path
        libs.append({"path": path, "label": label})
    return libs


NAVIDROME_API_URL = os.getenv("NAVIDROME_API_URL", "http://localhost:4533")
NAVIDROME_USERNAME = os.getenv("NAVIDROME_USERNAME", "")
NAVIDROME_PASSWORD = os.getenv("NAVIDROME_PASSWORD", "")

# H-3 Audit 2026-05-12: Subsonic-Auth-Mode.
# Default "token" — random salt + md5(password+salt) pro Request. Verhindert
# dass das Plain-Password im Server-Access-Log / Query-String steht. Subsonic
# API spec >=1.13.0 (Navidrome supportet alle Versionen). Backward-Compat-
# Fallback "plaintext" lässt das alte ?u=user&p=pwd-Pattern stehen — nur
# nötig für Sub-Sonic-Server <1.13.0 (theoretisch, praktisch ausgestorben).
NAVIDROME_AUTH_MODE = os.getenv("NAVIDROME_AUTH_MODE", "token").lower().strip()
# Scan music folder periodically and match files to Deezer/Spotify — mark completed_track_downloads
_nav_sync = os.getenv("NAVIDROME_SYNC_ENABLED", "true").lower().strip()
NAVIDROME_SYNC_ENABLED = _nav_sync in ("1", "true", "yes", "on")
NAVIDROME_SYNC_INTERVAL_HOURS = float(os.getenv("NAVIDROME_SYNC_INTERVAL_HOURS", "4"))
NAVIDROME_SYNC_INITIAL_DELAY_SEC = int(os.getenv("NAVIDROME_SYNC_INITIAL_DELAY_SEC", "120"))
NAVIDROME_SYNC_API_DELAY_SEC = float(os.getenv("NAVIDROME_SYNC_API_DELAY_SEC", "0.12"))

# Download Configuration
DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", "./downloads")  # Temporary download location for testing
OUTPUT_FORMAT = os.getenv("OUTPUT_FORMAT", "mp3")
AUDIO_QUALITY = os.getenv("AUDIO_QUALITY", "128")  # kbps (lower = smaller files, 128 is good balance)
# Seconds to keep browser temp files after first serve (stray duplicate GETs then get 200 instead of 404)
TEMP_FILE_CLEANUP_DELAY_SEC = int(os.getenv("TEMP_FILE_CLEANUP_DELAY_SEC", "60"))

# YouTube Configuration
YOUTUBE_COOKIES_PATH = os.getenv("YOUTUBE_COOKIES_PATH", "")  # Path to YouTube cookies file (Netscape format) for yt-dlp

# H-2 Audit 2026-05-12: Allowed-Root für YOUTUBE_COOKIES_PATH.
# yt-dlp liest die Cookies-Datei direkt von Disk. Ohne Restriction konnte
# der Operator via Settings → Verbindungen einen beliebigen Pfad eintragen
# (z.B. /etc/passwd oder /app/data/jobs.db) — yt-dlp würde versuchen die
# Datei als Cookies-Netscape-Format zu parsen. Kombiniert mit H-1 (Container
# als root) wäre das ein arbitrary-file-read; H-1 ist gefixt, dies hier ist
# defense-in-depth. Bind-Mount-Konvention: Operator legt die Cookies-Datei
# in das App-Daten-Volume, das standardmäßig auf /app/data gemountet wird.
YOUTUBE_COOKIES_DIR = os.getenv("YOUTUBE_COOKIES_DIR", "/app/data")

# yt-dlp Anti-Detection / Rate-Limiting
# ratelimit: Bytes/sec für jeden einzelnen Download. 1.5 MB/s ist fast nie
#   für die User wahrnehmbar (3-min Track in 12 s) und reduziert das Profil
#   stark gegenüber YouTube/CDN-Rate-Limit-Triggern.
YOUTUBE_RATELIMIT_BPS = int(os.getenv("YOUTUBE_RATELIMIT_BPS", "1500000"))
# chunk-size random im Range Min..Max bei jedem Download — variiert das
#   HTTP-Range-Pattern, sodass yt-dlp-Downloads sich gegenseitig nicht
#   identisch anhören (keine fixen 10MB-Boundaries mehr).
YOUTUBE_CHUNK_MIN_MB = int(os.getenv("YOUTUBE_CHUNK_MIN_MB", "8"))
YOUTUBE_CHUNK_MAX_MB = int(os.getenv("YOUTUBE_CHUNK_MAX_MB", "16"))
# Sleeps zwischen Requests/Fragments. yt-dlp randomisiert zwischen
#   sleep_interval und max_sleep_interval automatisch — das einzige was
#   wir tun ist den Range konfigurieren.
YOUTUBE_SLEEP_REQUESTS_S = float(os.getenv("YOUTUBE_SLEEP_REQUESTS_S", "1"))
YOUTUBE_SLEEP_MIN_S = float(os.getenv("YOUTUBE_SLEEP_MIN_S", "5"))
YOUTUBE_SLEEP_MAX_S = float(os.getenv("YOUTUBE_SLEEP_MAX_S", "15"))
# impersonate: TLS-Handshake-Fingerprint emulieren (via curl-cffi).
#   "chrome" ist der robusteste, "" deaktiviert den Mechanismus.
YOUTUBE_IMPERSONATE = os.getenv("YOUTUBE_IMPERSONATE", "chrome")
# player_clients: yt-dlp probiert die in Reihenfolge durch falls einer
#   blockt. "default" = web mit po_token-plugin, "web" = browser-fallback,
#   "android_vr" hat aktuell die zuverlässigsten format-streams.
#   Hinweis: "tv_embedded" wurde in yt-dlp 2026 als unsupported markiert
#   ("Skipping unsupported client") — Default angepasst.
YOUTUBE_PLAYER_CLIENTS = [
    c.strip() for c in os.getenv("YOUTUBE_PLAYER_CLIENTS", "default,web,android_vr").split(",") if c.strip()
]

# ─── Multi-Source-Resolver (Phase 0.2.0) ──────────────────────────
# Smart Source-Routing: pro Track werden alle aktivierten Quellen parallel
# pre-searched, der best-scorende Treffer wird gepullt. Bei Download-Fail
# (z.B. YouTube Age-Gate) automatisch nächst-bester aus dem Ranking. Reduziert
# Failure-Rate signifikant ohne Login/Cookies — z.B. Age-gated YT-Tracks
# werden auf SoundCloud ausweichen wenn dort verfügbar.
#
# Bandcamp ist hier NICHT default: yt-dlp hat keinen 'bcsearch'-Prefix
# (anders als ytsearch/scsearch) — Bandcamp-Suche müsste über die eigene
# Bandcamp-Website-Search-API gescraped werden, das ist nicht implementiert.
# Bandcamp-URLs als Direct-Download funktionieren weiter (yt-dlp's Bandcamp-
# Extractor). Wenn man's trotzdem aktiviert, kommt nur "NoSupportingHandlers"
# raus und der Resolver verschwendet pro Track 1 Round-Trip.
ENABLED_SOURCES = [
    s.strip() for s in os.getenv("ENABLED_SOURCES", "youtube,soundcloud").split(",") if s.strip()
]
# Timeout pro Source-Pre-Search. Verhindert dass eine langsame Quelle
# das ganze Resolve blockiert. Tonus' Worker hat 5-15s Cooldown, also
# darf das Resolve Latency hinzufügen, aber nicht unbegrenzt.
MULTI_SOURCE_TIMEOUT_S = float(os.getenv("MULTI_SOURCE_TIMEOUT_S", "10"))
# Minimum-Match-Score (0-1). Treffer unter dem Wert werden verworfen.
# Mit dem existing scoring-helper liegt 0.65 etwa "title + artist passen
# halbwegs". Bei zu niedrigem Wert kommen falsche Tracks rein.
MULTI_SOURCE_MIN_SCORE = float(os.getenv("MULTI_SOURCE_MIN_SCORE", "0.65"))
# Wie viele Kandidaten pro Source vor dem Ranking gezogen werden. Mehr =
# bessere Auswahl aber langsamer Pre-Search. 3 hat in Tests gut balanciert.
MULTI_SOURCE_CANDIDATES_PER_SOURCE = int(os.getenv("MULTI_SOURCE_CANDIDATES_PER_SOURCE", "3"))
# Maximum-Track-Duration in Sekunden. Kandidaten oberhalb werden verworfen
# weil sie typisch Festival-Sets, DJ-Mixe oder ganze Alben statt einzelner
# Tracks sind. Greift nur im Resolver / Auto-Track-Pfad — URL-Downloads
# und Direct-Video-ID umgehen den Filter komplett. 900s = 15min ist eine
# komfortable Obergrenze für legitime Extended Mixes (5-12min typ.) ohne
# 30-60-90min Sets durchzulassen. Aus dem 2026-05-10 Burn-in: alle
# Falschmatches waren >15min.
MAX_TRACK_DURATION_S = int(os.getenv("MAX_TRACK_DURATION_S", "900"))
# Safety-Cap für Playlist-Expand (v0.5.0): wie viele Tracks aus einer
# SoundCloud/YouTube-Playlist-URL maximal gequeut werden. Schützt vor
# Runaway-Imports bei 1000+-Track-Playlists — Response markiert
# truncated=true, Frontend zeigt eine Warnung.
PLAYLIST_MAX_TRACKS = int(os.getenv("PLAYLIST_MAX_TRACKS", "200"))
# Intervall des Playlist-Reconcile-Background-Threads (v0.5.0): materialisiert
# Playlist-Marker (SC-Import, CSV-Import) zu Subsonic-Playlists, auch ohne
# Navidrome-Plugin-Syncs. Idempotent, bei leerem Marker-Set ~kostenlos.
PLAYLIST_RECONCILE_INTERVAL_S = int(os.getenv("PLAYLIST_RECONCILE_INTERVAL_S", "900"))

# ─── Security: Album-Art-URL-Allowlist (Audit C-1, 2026-05-12) ────
# SSRF-Schutz für services.metadata._download_album_art. Ohne diese
# Allowlist konnte ein User-controlled album_art Wert (aus Spotify/
# Deezer-Provider-Response, Track-Hint oder Reverse-Lookup) jede HTTP-
# URL erreichen — inkl. Cloud-Metadata-Endpoints (169.254.169.254),
# Internal-Hosts in Docker-Netz oder LAN-Range.
#
# Default deckt die Cover-Art-CDNs aller unterstützten Metadata-Provider
# und Download-Sources ab. Override via ENV `ALBUM_ART_ALLOWED_HOSTS`
# als CSV von Hostnamen (Subdomain-Match aktiv: `sndcdn.com` matched
# auch `i1.sndcdn.com`). Leerer Override → Default. IP-Literals werden
# IMMER abgelehnt (kein bare-IP-Bypass).
_default_album_art_hosts = [
    # Spotify cover-art
    "scdn.co", "spotifycdn.com",
    # Deezer cover-art
    "dzcdn.net", "deezer.com",
    # YouTube thumbnails (Multi-Source-Resolver-Tracks)
    "ytimg.com", "ggpht.com", "googleusercontent.com",
    # SoundCloud cover-art
    "sndcdn.com",
    # MusicBrainz / Cover-Art-Archive
    "coverartarchive.org", "archive.org",
]
_raw_aa_hosts = (os.getenv("ALBUM_ART_ALLOWED_HOSTS", "") or "").strip()
if _raw_aa_hosts:
    ALBUM_ART_ALLOWED_HOSTS = tuple(h.strip().lower().lstrip(".") for h in _raw_aa_hosts.split(",") if h.strip())
else:
    ALBUM_ART_ALLOWED_HOSTS = tuple(_default_album_art_hosts)

# ─── Security: Trusted-Proxy-CIDRs (Audit H-7, 2026-05-12) ─────────
# utils.auth.client_ip vertraut X-Forwarded-For nur dann, wenn der
# direkte HTTP-Peer (request.client.host) in einem dieser CIDRs liegt.
# Ohne den Check konnte jeder Caller seine eigene Source-IP via XFF-
# Header fälschen und den Brute-Force-Ban auf eine fremde IP umlenken.
#
# Default deckt typische Container-/LAN-Setups ab (Tonus läuft hinter
# Traefik im Docker-Netz). Bei direct-Internet-Expose ohne Reverse-Proxy
# muss `TRUSTED_PROXIES=""` (leer) gesetzt werden — dann wird XFF
# komplett ignoriert und nur der direkte Peer als Source-IP genutzt.
_default_trusted_proxies = [
    "127.0.0.1/32", "::1/128",
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
]
_raw_proxies_env = os.getenv("TRUSTED_PROXIES")
if _raw_proxies_env is None:
    TRUSTED_PROXIES = tuple(_default_trusted_proxies)
else:
    # Auch leerer String "" ist eine gültige Wahl: "vertraue keinem Proxy".
    TRUSTED_PROXIES = tuple(p.strip() for p in _raw_proxies_env.split(",") if p.strip())

# ─── Logging ──────────────────────────────────────────────────────
# DEBUG / INFO / WARNING / ERROR / CRITICAL — kontrolliert tonus' eigene
# logger (resolver, worker, app). yt-dlp's verbosity ist davon getrennt
# konfigurierbar (siehe YT_DLP_QUIET).
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
# Uvicorn HTTP-Access-Logs ('GET /api/queue 200 OK'-Spam). Default OFF
# weil die Queue-Page jede Sekunde polled — Access-Logs überfluten den
# Container-Log und überscrollen die echten WARN/ERROR-Lines.
UVICORN_ACCESS_LOG = os.getenv("UVICORN_ACCESS_LOG", "false").lower() == "true"
# yt-dlp's eigenes Logging: True = nur Errors, False = Info-Level mit
# allen [youtube] / [soundcloud] -Lines. Bei Debug-Sessions auf False
# setzen, in Production auf True für sauberen Container-Log.
YT_DLP_QUIET = os.getenv("YT_DLP_QUIET", "false").lower() == "true"

# API Configuration
API_HOST = os.getenv("API_HOST", "0.0.0.0")
API_PORT = int(os.getenv("API_PORT", "8000"))
CORS_ORIGINS = os.getenv("CORS_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000").split(",")

# Phase F: Multi-User-Auth mit JWT + 2FA. Drei Auth-Pfade:
#   1) Browser-Session: POST /api/auth/login → JWT (Access + Refresh)
#   2) Plugin/CLI: Personal Access Token (PAT) im "Authorization: Bearer tonus_pat_…" Header
#   3) Legacy-Compat: TONUS_API_TOKEN aus env, deprecated, wird in einer späteren
#      Version entfernt. Wenn gesetzt → Backwards-Compat aktiv (alte Plugin-Configs
#      funktionieren weiter).
TONUS_API_TOKEN = (os.getenv("TONUS_API_TOKEN") or "").strip()

# JWT-Konfiguration
# JWT_SECRET wird beim ersten Start automatisch generiert und in jobs.db persistiert
# (Tabelle "auth_meta"). Per Env überschreibbar — sinnvoll wenn man mehrere
# Tonus-Instanzen mit demselben Secret betreiben will (Replikation).
JWT_SECRET_OVERRIDE = (os.getenv("JWT_SECRET") or "").strip()
JWT_ALGORITHM = "HS256"
JWT_ACCESS_TTL_MIN = int(os.getenv("JWT_ACCESS_TTL_MIN", "15"))
JWT_REFRESH_TTL_DAYS = int(os.getenv("JWT_REFRESH_TTL_DAYS", "30"))

# TOTP-Issuer-Name (erscheint in Authenticator-Apps)
TOTP_ISSUER = (os.getenv("TOTP_ISSUER") or "Tonus").strip()

# Login Rate-Limit (failed attempts)
LOGIN_RATE_LIMIT_PER_15MIN = int(os.getenv("LOGIN_RATE_LIMIT_PER_15MIN", "5"))

# Create directories if they don't exist
Path(DOWNLOAD_DIR).mkdir(parents=True, exist_ok=True)
for _nav_root in NAVIDROME_MUSIC_PATHS_LIST:
    Path(_nav_root).mkdir(parents=True, exist_ok=True)


# ───────────────────────────────────────────────────────────────────
# DB-Overrides für UI-editierbare Provider-Configs
# ───────────────────────────────────────────────────────────────────
# env-Werte sind die Defaults; die UI (Settings → Verbindungen) kann sie
# in der app_settings-Tabelle überschreiben. apply_db_overrides() patcht
# die Module-Globals, sodass nachfolgend instanziierte Services
# (SpotifyService, NavidromeService) die UI-Werte sehen.
#
# WICHTIG: muss zwischen init_jobs_db() und der Service-Instanziierung in
# app.py aufgerufen werden — sonst Henne-Ei.

# Mapping zwischen DB-Key und Modul-Variable. encrypted=True für Felder
# die als Secret gespeichert werden (Passwords, Client-Secrets).
_DB_OVERRIDE_MAP = [
    # Spotify
    ("spotify.client_id", "SPOTIFY_CLIENT_ID", False),
    ("spotify.client_secret", "SPOTIFY_CLIENT_SECRET", True),
    ("spotify.redirect_uri", "SPOTIFY_REDIRECT_URI", False),
    # Navidrome
    ("navidrome.api_url", "NAVIDROME_API_URL", False),
    ("navidrome.username", "NAVIDROME_USERNAME", False),
    ("navidrome.password", "NAVIDROME_PASSWORD", True),
    # YouTube
    ("youtube.cookies_path", "YOUTUBE_COOKIES_PATH", False),
]


def apply_db_overrides() -> None:
    """Patcht Module-Globals mit Werten aus app_settings, falls gesetzt.
    Idempotent — kann mehrfach aufgerufen werden, wirkt aber nur beim
    ersten Mal vor Service-Instanziierung."""
    from utils.app_settings import get_setting

    g = globals()
    for db_key, attr, _encrypted in _DB_OVERRIDE_MAP:
        v = get_setting(db_key)
        if v is not None and v != "":
            g[attr] = v

