import yt_dlp
from ytmusicapi import YTMusic
import os
import random
import re
import math
from difflib import SequenceMatcher
from typing import Optional, Dict, List, Any, Tuple
from pathlib import Path
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

# Confidence threshold - below this, show candidates to user
CONFIDENCE_THRESHOLD = 0.65

# How strongly to trust YTMusic ordering (bigger = trust rank deeper)
# (tuned to match the debug script's improved model)
DEFAULT_RANK_STRENGTH = float(os.getenv("YTMUSIC_RANK_STRENGTH", "6.0"))

# ---------------------------------------------------------------------------
# Dual-VPN-Splitting: yt-dlp Source-IP-Bind
# ---------------------------------------------------------------------------
# Wenn VPN_SPLIT_ENABLED=true ist, kann yt-dlp pro Download eine Source-IP an
# den Socket binden — der UniFi-Router routet dann pro Source-IP über einen
# anderen VPN-Tunnel. Damit verhalten sich Downloads wie die CSV-Search-Calls:
# alterniert zwischen Lane A und B, halbiert effektiv den per-IP-Cooldown.
# ---------------------------------------------------------------------------
_YT_VPN_SPLIT_ENABLED = os.environ.get("VPN_SPLIT_ENABLED", "").strip().lower() == "true"
_YT_VPN_SOURCE_A = os.environ.get("VPN_SOURCE_A", "").strip() or None
_YT_VPN_SOURCE_B = os.environ.get("VPN_SOURCE_B", "").strip() or None


def _resolve_lane_ip(lane: Optional[str]) -> Optional[str]:
    """Lane → Host-IP. Returnt None bei deaktiviertem Split oder unbekannter Lane
    (in dem Fall wird kein source_address gesetzt → System-Default-Routing)."""
    if not _YT_VPN_SPLIT_ENABLED or not lane:
        return None
    if lane == "a":
        return _YT_VPN_SOURCE_A
    if lane == "b":
        return _YT_VPN_SOURCE_B
    return None


def _apply_source_lane(ydl_opts: Dict, lane: Optional[str]) -> Dict:
    """In-place: setzt yt-dlp source_address falls Lane bindbar."""
    ip = _resolve_lane_ip(lane)
    if ip:
        ydl_opts["source_address"] = ip
    return ydl_opts


# Probe ob curl_cffi und yt-dlp's Impersonate-Backend tatsächlich nutzbar
# sind. Wenn das Probe failt (curl-cffi fehlt im Image, falsche Version,
# Plattform ohne libcurl-impersonate-Builds), schalten wir die impersonate-
# Option für die ganze Session ab — sonst bricht jeder Download mit
# "Impersonate target 'chrome' is not available". Probe einmal beim
# Module-Import, Cache als Modulvariable.
def _probe_impersonate(target: str) -> bool:
    if not target:
        return False
    try:
        import curl_cffi  # noqa: F401
    except ImportError:
        print(f"WARN: curl-cffi nicht installiert — YOUTUBE_IMPERSONATE='{target}' wird ignoriert")
        return False
    try:
        # yt-dlp's eigene Validierung: wenn der target-string nicht in der
        # verfügbaren Liste ist, raised es ValueError schon beim YoutubeDL-init.
        with yt_dlp.YoutubeDL({'impersonate': target, 'quiet': True, 'no_warnings': True}):
            pass
        return True
    except Exception as e:
        print(f"WARN: Impersonate-Probe fehlgeschlagen ({type(e).__name__}: {e}) — YOUTUBE_IMPERSONATE='{target}' wird ignoriert")
        return False


_IMPERSONATE_OK = _probe_impersonate(config.YOUTUBE_IMPERSONATE)


def _apply_anti_detection_opts(ydl_opts: Dict) -> Dict:
    """Hardening gegen YouTube-Bot-Detection und Rate-Limit-Trigger.

    - ratelimit: cap auf YOUTUBE_RATELIMIT_BPS (default 1.5 MB/s) — ein
      "menschlicher" 3-min-Track ist in 12 s durch, aber der Burst-Pattern
      verschwindet (CDN-Detection looks for full-bandwidth-rips).
    - http_chunk_size: PRO DOWNLOAD eine neue zufällige Größe zwischen
      MIN_MB und MAX_MB. Variiert das HTTP-Range-Pattern, sodass mehrere
      Tonus-Downloads sich nicht identisch fingerprinten lassen.
    - sleep_interval / max_sleep_interval: yt-dlp randomisiert intern
      zwischen den beiden — natürliche Pausen zwischen Fragmenten.
    - sleep_interval_requests: Pause zwischen API-Calls innerhalb
      eines Downloads (Metadata-Fetch, Format-Lookup etc.).
    - impersonate: TLS-Handshake-Spoofing via curl-cffi falls aktiv UND
      beim Module-Import probe-bestanden — sonst silent skip.

    Pro Aufruf einmal — bei Multi-Download-Sessions hat jeder Download
    seine eigene zufällige Chunk-Size und damit eigenen Range-Pattern.
    """
    ydl_opts['ratelimit'] = config.YOUTUBE_RATELIMIT_BPS
    chunk_mb = random.randint(config.YOUTUBE_CHUNK_MIN_MB, config.YOUTUBE_CHUNK_MAX_MB)
    ydl_opts['http_chunk_size'] = chunk_mb * 1024 * 1024
    ydl_opts['sleep_interval_requests'] = config.YOUTUBE_SLEEP_REQUESTS_S
    ydl_opts['sleep_interval'] = config.YOUTUBE_SLEEP_MIN_S
    ydl_opts['max_sleep_interval'] = config.YOUTUBE_SLEEP_MAX_S
    if _IMPERSONATE_OK:
        ydl_opts['impersonate'] = config.YOUTUBE_IMPERSONATE
    return ydl_opts


class YouTubeService:
    def __init__(self):
        self.output_format = config.OUTPUT_FORMAT
        self.audio_quality = config.AUDIO_QUALITY
        self.cookies_path = config.YOUTUBE_COOKIES_PATH
        try:
            self.ytmusic = YTMusic()
        except Exception as e:
            print(f"Failed to initialize YTMusic: {e}")
            self.ytmusic = None

    @staticmethod
    def _preferred_quality_for_extract(output_format: str, audio_quality: str) -> str:
        """yt-dlp FFmpegExtractAudio: bitrate for lossy; '0' is typical for lossless FLAC."""
        fmt = (output_format or "").lower()
        if fmt == "flac" or (audio_quality or "").lower() == "lossless":
            return "0"
        return audio_quality or config.AUDIO_QUALITY

    @staticmethod
    def _ffmpeg_extract_preferredcodec(target_format: str) -> str:
        """Build FFmpegExtractAudio preferredcodec string.

        A bare codec name (e.g. 'flac') usually works, but yt-dlp may treat preferredcodec
        as 'best' if unset, or hit filecodec KeyError and fall back to MP3. An explicit
        ext->target mapping (m4a>flac/webm>flac/...) forces conversion to the requested format.
        """
        t = (target_format or "mp3").strip().lower()
        if t == "m4a":
            return "m4a"
        # YouTube audio commonly arrives as m4a (AAC) or webm (opus); cover common containers.
        sources = (
            "m4a", "webm", "opus", "ogg", "mp3", "aac", "wav", "flac", "mp4", "best"
        )
        return "/".join(f"{s}>{t}" for s in sources)

    @staticmethod
    def _output_base_path(output_path: str, output_format: str) -> str:
        """Strip trailing .ext from our target path (case-insensitive)."""
        ext = (output_format or "mp3").lower()
        suf = f".{ext}"
        if output_path.lower().endswith(suf):
            return output_path[: -len(suf)]
        return os.path.splitext(output_path)[0]

    @staticmethod
    def _yt_dlp_outtmpl(base_path: str) -> str:
        """yt-dlp requires %(ext)s or it may name files after the video title instead of base_path."""
        return f"{base_path}.%(ext)s"

    @staticmethod
    def _filepaths_from_info(info: Optional[Dict]) -> List[str]:
        """Collect candidate paths yt-dlp may attach after download/postprocess."""
        out: List[str] = []
        if not info:
            return out
        fp = info.get("filepath")
        if fp:
            out.append(fp)
        for req in info.get("requested_downloads") or []:
            if req.get("filepath"):
                out.append(req["filepath"])
        for ent in info.get("entries") or []:
            if ent:
                out.extend(YouTubeService._filepaths_from_info(ent))
        return out

    def _resolve_downloaded_audio(
        self,
        base_path: str,
        output_format: str,
        wants_m4a_passthrough: bool,
        info: dict,
        ydl: Any,
    ) -> Optional[str]:
        """Find the file on disk: expected name, yt-dlp metadata, or same-dir fallback."""
        ext = (output_format or "mp3").lower()
        expected = f"{base_path}.m4a" if wants_m4a_passthrough else f"{base_path}.{ext}"

        if os.path.exists(expected):
            return expected

        for p in self._filepaths_from_info(info):
            if p and os.path.exists(p):
                if p.lower().endswith(f".{ext}") or wants_m4a_passthrough and p.lower().endswith(".m4a"):
                    return p
        for p in self._filepaths_from_info(info):
            if p and os.path.exists(p):
                return p

        try:
            fn = ydl.prepare_filename(info)
            if os.path.exists(fn):
                return fn
            if fn.endswith(".webm") and os.path.exists(fn.replace(".webm", f".{ext}")):
                return fn.replace(".webm", f".{ext}")
            if fn.endswith(".m4a") and os.path.exists(fn.replace(".m4a", f".{ext}")):
                return fn.replace(".m4a", f".{ext}")
        except Exception:
            pass

        d = os.path.dirname(base_path)
        stem = os.path.basename(base_path)
        audio_suffixes = (
            f".{ext}",
            ".flac",
            ".mp3",
            ".m4a",
            ".opus",
            ".ogg",
            ".webm",
            ".wav",
        )
        if os.path.isdir(d):
            try:
                # Prefer exact extension, then any known audio file with same stem (yt-dlp quirk).
                candidates: List[str] = []
                for name in os.listdir(d):
                    low = name.lower()
                    if not any(low.endswith(s) for s in audio_suffixes):
                        continue
                    full = os.path.join(d, name)
                    if not os.path.isfile(full):
                        continue
                    if not (name.startswith(stem) or stem in name):
                        continue
                    candidates.append(full)
                for want in (f".{ext}", ".flac", ".mp3", ".m4a", ".opus", ".ogg", ".webm", ".wav"):
                    for full in candidates:
                        if full.lower().endswith(want):
                            return full
            except OSError:
                pass
        return None

    def _add_cookies_to_opts(self, ydl_opts: Dict) -> Dict:
        """Add cookies to yt-dlp options if configured."""
        if self.cookies_path and os.path.exists(self.cookies_path):
            ydl_opts['cookiefile'] = self.cookies_path
            print(f"Using YouTube cookies from: {self.cookies_path}")
        elif self.cookies_path:
            print(f"Warning: Cookie file specified but not found: {self.cookies_path}")
        return ydl_opts
    
    def calculate_similarity(self, str1: str, str2: str) -> float:
        """Calculate similarity between two strings using SequenceMatcher."""
        str1 = (str1 or "").lower().strip()
        str2 = (str2 or "").lower().strip()
        return SequenceMatcher(None, str1, str2).ratio()

    def normalize_text(self, s: str) -> str:
        """Normalize text for cross-source matching (best-effort, multilingual-safe)."""
        s = (s or "").lower()

        # unify separators
        s = s.replace("–", " ").replace("—", " ").replace("-", " ").replace(":", " ")

        # remove common meta tokens
        meta_tokens = [
            "official audio",
            "official video",
            "official music video",
            "lyrics",
            "lyric video",
            "audio",
            "mv",
            "hd",
            "4k",
            "official",
            "music video",
        ]
        for t in meta_tokens:
            s = s.replace(t, " ")

        # remove bracketed meta (best-effort)
        s = re.sub(r"\((official|mv|music video|lyrics|lyric video|audio|hd|4k)[^)]*\)", " ", s)
        s = re.sub(r"\[(official|mv|music video|lyrics|lyric video|audio|hd|4k)[^\]]*\]", " ", s)

        # normalize feat tokens
        s = re.sub(r"\b(feat\.|feat|ft\.|ft)\b", "feat", s)

        # collapse whitespace
        s = re.sub(r"\s+", " ", s).strip()
        return s

    def tokens(self, s: str) -> List[str]:
        s = self.normalize_text(s)
        return [p for p in re.split(r"\s+", s) if p]

    def title_score(self, spotify_title: str, yt_title: str) -> float:
        a = self.normalize_text(spotify_title)
        b = self.normalize_text(yt_title)

        sim = self.calculate_similarity(a, b)

        ts = [t for t in self.tokens(a) if len(t) >= 2 and t not in {"feat"}]
        if ts:
            hits = sum(1 for t in ts if t in b)
            contain = hits / len(ts)
            sim = max(sim, 0.55 * sim + 0.45 * contain)

        if a and a in b:
            sim = max(sim, 0.85)

        return max(0.0, min(sim, 1.0))

    def artist_score(self, spotify_artists: List[str], yt_artists_text: str, yt_title: str) -> Tuple[float, int]:
        """Score artist match against ANY Spotify artist. Returns (score, matched_count)."""
        yt_blob = self.normalize_text(yt_artists_text) + " " + self.normalize_text(yt_title)

        per: List[float] = []
        for a in (spotify_artists or []):
            a_norm = self.normalize_text(a)
            sim = self.calculate_similarity(a_norm, yt_blob)
            if a_norm and a_norm in yt_blob:
                sim = max(sim, 0.95)
            per.append(max(0.0, min(sim, 1.0)))

        if not per:
            return 0.0, 0

        best = max(per)
        matched = sum(1 for s in per if s >= 0.75)

        bonus = 0.0
        if matched >= 2:
            bonus = 0.08
        elif matched == 1:
            bonus = 0.02

        return max(0.0, min(best + bonus, 1.0)), matched

    def parse_duration_to_seconds(self, duration_str: str) -> Optional[int]:
        if not duration_str:
            return None
        parts = duration_str.strip().split(":")
        try:
            if len(parts) == 2:
                return int(parts[0]) * 60 + int(parts[1])
            if len(parts) == 3:
                return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        except Exception:
            return None
        return None

    def duration_score(self, spotify_duration_ms: Optional[int], yt_duration_seconds: Optional[int], yt_duration_str: str = "") -> float:
        """Score duration similarity. Accepts either parsed seconds or a duration string."""
        if not spotify_duration_ms:
            return 0.5

        yt_sec = yt_duration_seconds
        if yt_sec is None and yt_duration_str:
            yt_sec = self.parse_duration_to_seconds(yt_duration_str)
        if yt_sec is None:
            return 0.5

        sp_sec = max(1.0, spotify_duration_ms / 1000.0)
        delta = abs(sp_sec - float(yt_sec))

        if delta <= 5:
            return 1.0
        if delta <= 15:
            return 0.85
        if delta <= 30:
            return 0.65
        if delta <= 60:
            return 0.35
        return 0.0

    def rank_prior(self, rank: int, strength: float) -> float:
        r = max(1, rank)
        return math.exp(-(r - 1) / max(1e-6, strength))

    def heuristic_adjustment(self, spotify_title: str, yt_title: str) -> float:
        sp = self.normalize_text(spotify_title)
        yt = self.normalize_text(yt_title)

        adj = 0.0

        sp_live = any(k in sp for k in ["live", "现场", "現場"])
        yt_live = any(k in yt for k in ["live", "现场", "現場"])
        if sp_live and yt_live:
            adj += 0.05

        if ("cover" in yt or "翻唱" in yt) and ("cover" not in sp and "翻唱" not in sp):
            adj -= 0.12

        if "remix" in yt and "remix" not in sp:
            adj -= 0.10

        return adj

    def calculate_match_score(
        self,
        youtube_title: str,
        youtube_channel: str,
        track_name: str,
        artist: str,
        track_info: Optional[Dict] = None,
        rank: int = 1,
        source: str = "ytmusic",
        yt_duration_seconds: Optional[int] = None,
        yt_duration_str: str = "",
    ) -> float:
        """Improved multi-signal match score.

        Mirrors the scoring you validated in [`debug_ytmusic_scoring.py`](debug_ytmusic_scoring.py:1).
        """
        spotify_title = track_name
        spotify_artists = []
        spotify_duration_ms: Optional[int] = None

        if track_info:
            spotify_title = track_info.get("name") or track_name
            spotify_artists = track_info.get("artists") or []
            spotify_duration_ms = track_info.get("duration_ms")

        # Fallback if track_info wasn't provided
        if not spotify_artists:
            spotify_artists = [a.strip() for a in (artist or "").split(",") if a.strip()]

        t_s = self.title_score(spotify_title, youtube_title)
        a_s, _matched = self.artist_score(spotify_artists, youtube_channel, youtube_title)
        d_s = self.duration_score(spotify_duration_ms, yt_duration_seconds, yt_duration_str)

        # Trust YTMusic ordering more than a raw YouTube web search.
        rank_strength = DEFAULT_RANK_STRENGTH
        if source != "ytmusic":
            rank_strength = max(3.0, DEFAULT_RANK_STRENGTH * 0.6)
        r_s = self.rank_prior(rank, rank_strength)

        heur = self.heuristic_adjustment(spotify_title, youtube_title)

        # Combine (same weights as debug script)
        final = (0.45 * t_s) + (0.25 * a_s) + (0.20 * d_s) + (0.10 * r_s) + heur
        final = max(0.0, min(final, 1.0))
        return final
    
    def search_candidates(self, track_name: str, artist: str, track_info: Dict = None, num_results: int = 5) -> Dict:
        """Search YouTube and return top candidates with confidence scores."""
        candidates = []
        yt_dlp_blocked = False  # Track if yt-dlp was blocked

        # Try YTMusic first
        if self.ytmusic:
            try:
                search_query = f"{artist} {track_name}"
                if track_info and track_info.get('album'):
                    search_query += f" {track_info.get('album')}"

                results = self.ytmusic.search(search_query, filter="songs", limit=num_results)

                for idx, res in enumerate(results, start=1):
                    video_id = res.get('videoId')
                    if not video_id:
                        continue

                    title = res.get('title', '')
                    artists_list = res.get('artists', [])
                    channel = ", ".join([a.get('name', '') for a in artists_list]) if artists_list else ''

                    duration_str = res.get('duration', '0:00')
                    duration = 0
                    try:
                        parts = duration_str.split(':')
                        if len(parts) == 2:
                            duration = int(parts[0]) * 60 + int(parts[1])
                        elif len(parts) == 3:
                            duration = int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
                    except Exception:
                        duration = 0

                    thumbnails = res.get('thumbnails', [])
                    thumbnail = thumbnails[-1].get('url', '') if thumbnails else ''

                    score = self.calculate_match_score(
                        title,
                        channel,
                        track_name,
                        artist,
                        track_info=track_info,
                        rank=idx,
                        source='ytmusic',
                        yt_duration_seconds=duration,
                        yt_duration_str=duration_str,
                    )

                    candidates.append({
                        'video_id': video_id,
                        'title': title,
                        'channel': channel,
                        'duration': duration,
                        'thumbnail': thumbnail,
                        'score': round(score, 3),
                        'url': f"https://music.youtube.com/watch?v={video_id}",
                        'source': 'ytmusic'
                    })
            except Exception as e:
                print(f"YTMusic search failed: {e}")

        # Fallback to yt-dlp if no candidates found or YTMusic failed
        if not candidates:
            if track_info and track_info.get('album'):
                query = f"{artist} {track_name} {track_info.get('album')} official"
            else:
                query = f"{artist} {track_name} official audio"

            ydl_opts = {
                'quiet': True,
                'no_warnings': True,
                'extract_flat': True,
                'default_search': f'ytsearch{num_results}',
                'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            }
            ydl_opts = self._add_cookies_to_opts(ydl_opts)

            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    search_query = f"ytsearch{num_results}:{query}"
                    info = ydl.extract_info(search_query, download=False)

                    if 'entries' in info and info['entries']:
                        for idx, entry in enumerate(info['entries'], start=1):
                            if not entry:
                                continue

                            title = entry.get('title', '')
                            channel = entry.get('channel', entry.get('uploader', ''))
                            video_id = entry.get('id', '')
                            duration = entry.get('duration', 0)
                            thumbnail = entry.get('thumbnail', '')

                            score = self.calculate_match_score(
                                title,
                                channel,
                                track_name,
                                artist,
                                track_info=track_info,
                                rank=idx,
                                source='yt-dlp',
                                yt_duration_seconds=duration if isinstance(duration, int) else None,
                                yt_duration_str="",
                            )

                            candidates.append({
                                'video_id': video_id,
                                'title': title,
                                'channel': channel,
                                'duration': duration,
                                'thumbnail': thumbnail,
                                'score': round(score, 3),
                                'url': f"https://www.youtube.com/watch?v={video_id}",
                                'source': 'yt-dlp'
                            })
            except Exception as e:
                error_msg = str(e)
                # Log the error but don't fail completely - we might have YTMusic candidates
                if '403' in error_msg or 'Forbidden' in error_msg:
                    print(f"yt-dlp search blocked by YouTube (403). Using YTMusic results only: {e}")
                    yt_dlp_blocked = True
                else:
                    print(f"yt-dlp search failed: {e}")
                # Continue with whatever candidates we have (from YTMusic if available)

        # Sort by score descending
        candidates.sort(key=lambda x: x['score'], reverse=True)
        
        if not candidates:
            return {
                'success': False,
                'error': "No results found on YouTube or YouTube Music. YouTube may be blocking requests (403). Try using YouTube cookies (see documentation).",
                'candidates': [],
                'needs_confirmation': False
            }

        best_score = candidates[0]['score']
        # Only show confirmation if confidence is really low
        # Disable aggressive confirmation for now - let it work normally
        needs_confirmation = best_score < CONFIDENCE_THRESHOLD
        
        result = {
            'success': True,
            'candidates': candidates[:3],
            'best_score': best_score,
            'needs_confirmation': needs_confirmation,
            'threshold': CONFIDENCE_THRESHOLD
        }
        # Add warning if yt-dlp was blocked
        if yt_dlp_blocked:
            result['warning'] = 'YouTube blocked some requests (403). Consider configuring YouTube cookies for better reliability.'
        return result
    
    def download_by_url(self, url: str, output_path: str, output_format: str = None, audio_quality: str = None, source_lane: Optional[str] = None) -> Dict:
        """Download from any yt-dlp-supported URL — YouTube, SoundCloud, Bandcamp, etc.

        Generischer Download-Path. download_by_video_id() ist ein Wrapper der
        die YouTube-URL aus einer video_id baut und hier aufruft. Multi-
        Source-Resolver-Pfad kommt direkt mit URL.

        source_lane∈{a,b,None} → Source-IP-Bind (Dual-VPN).
        """
        output_format = (output_format or self.output_format or "mp3").strip().lower()
        audio_quality = audio_quality or self.audio_quality

        output_path = os.path.abspath(output_path)
        base_path = self._output_base_path(output_path, output_format)

        # If the user wants m4a and YouTube provides it as itag 140 (m4a/aac),
        # keep the original container by skipping FFmpegExtractAudio.
        wants_m4a_passthrough = (output_format or "").lower() == "m4a"

        ydl_opts = {
            # Avoid HLS (m3u8) formats that get blocked - prefer direct audio formats
            # Format priority: m4a direct > opus/webm direct > bestaudio (non-HLS) > fallback
            # Generischer Pattern — funktioniert auch für SoundCloud (mp3/m4a) und
            # Bandcamp (mp3-direct mit oft DRM-frei) ohne Anpassung.
            'format': 'bestaudio[ext=m4a][protocol!=m3u8]/bestaudio[ext=webm][protocol!=m3u8]/bestaudio[ext=opus][protocol!=m3u8]/bestaudio[protocol!=m3u8]/best[ext=m4a][protocol!=m3u8]/best[ext=webm][protocol!=m3u8]/best[height<=720][protocol!=m3u8]/best',
            'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            # YouTube-spezifische extractor-args; yt-dlp ignoriert die für andere
            # Sources, also schadet es nicht hier zu setzen.
            'extractor_args': {
                'youtube': {
                    'player_client': config.YOUTUBE_PLAYER_CLIENTS,
                }
            },
            'retries': 10,
            'fragment_retries': 10,
            'file_access_retries': 3,
            'outtmpl': self._yt_dlp_outtmpl(base_path),
            'fixup': 'never',
            'quiet': config.YT_DLP_QUIET,
            'no_warnings': config.YT_DLP_QUIET,
            'noplaylist': True,
        }

        if wants_m4a_passthrough:
            ydl_opts['postprocessors'] = [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'm4a',
                'nopostoverwrites': False,
            }]
            ydl_opts['postprocessor_args'] = {
                'ffmpeg': [
                    '-ac', '2',
                    '-c:a', 'copy',
                    '-q:a', '0',
                ]
            }
        else:
            pq = self._preferred_quality_for_extract(output_format, audio_quality)
            preferredcodec = self._ffmpeg_extract_preferredcodec(output_format)
            ydl_opts['postprocessors'] = [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': preferredcodec,
                'preferredquality': pq,
                'nopostoverwrites': False,
            }]
            if output_format not in ('flac', 'wav', 'alac'):
                ydl_opts['postprocessor_args'] = {
                    'ffmpeg': [
                        '-af', 'aresample=44100',
                        '-ac', '2',
                    ]
                }

        ydl_opts = self._add_cookies_to_opts(ydl_opts)
        ydl_opts = _apply_source_lane(ydl_opts, source_lane)
        ydl_opts = _apply_anti_detection_opts(ydl_opts)

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)

                actual_path = self._resolve_downloaded_audio(
                    base_path, output_format, wants_m4a_passthrough, info, ydl
                )
                if not actual_path:
                    exp = f"{base_path}.m4a" if wants_m4a_passthrough else f"{base_path}.{output_format}"
                    raise FileNotFoundError(f"Downloaded file not found. Expected: {exp}")

                return {
                    'success': True,
                    'file_path': actual_path,
                    'title': info.get('title', ''),
                    'duration': info.get('duration', 0),
                    'url': info.get('webpage_url', url),
                    'extractor': info.get('extractor', '') or info.get('extractor_key', ''),
                }

        except Exception as e:
            error_msg = str(e)
            if '403' in error_msg or 'Forbidden' in error_msg:
                error_msg = f"Source blocked the request (HTTP 403). Try again in a few minutes. URL: {url}"
            return {
                'success': False,
                'error': error_msg,
            }

    def download_by_video_id(self, video_id: str, output_path: str, output_format: str = None, audio_quality: str = None, source_lane: Optional[str] = None) -> Dict:
        """Backward-compat: download a specific YouTube video by ID.

        Wrapper für download_by_url. Bestehende Aufrufer (Direct-Video-ID-Endpoint,
        legacy CSV-Resume) funktionieren ohne Änderung.
        """
        return self.download_by_url(
            f"https://www.youtube.com/watch?v={video_id}",
            output_path,
            output_format,
            audio_quality,
            source_lane=source_lane,
        )

    def search_and_download(self, track_name: str, artist: str, output_path: str, track_info: Dict = None, video_id: str = None, output_format: str = None, audio_quality: str = None, source_lane: Optional[str] = None) -> Dict:
        """Search across all enabled sources and download the best match.

        Hard Cutover Phase 0.2.0: ignoriert die ehemalige Single-Source-Logik
        und nutzt den MultiSourceResolver für YouTube + SoundCloud + Bandcamp
        parallel. Resolver liefert ranked candidates; wir iterieren bis einer
        erfolgreich downloadet wird (z.B. Age-Gated YouTube → SoundCloud-Fallback).

        video_id-Param bleibt für Direct-URL/Track-Pin erhalten — wenn gesetzt,
        skippt der Resolver-Pfad komplett (User wollte explizit DIESES Video).
        """
        output_format = (output_format or self.output_format or "mp3").strip().lower()
        audio_quality = audio_quality or self.audio_quality

        # Direct-pin path: User hat explizit eine YouTube-Video-ID gegeben
        # (z.B. via Album-Pre-Resolve). Kein Resolver, kein Fallback —
        # einfach die ID downloaden.
        if video_id:
            return self.download_by_video_id(video_id, output_path, output_format, audio_quality, source_lane=source_lane)

        # Multi-Source-Resolver-Pfad: pre-search auf allen aktivierten Quellen
        # parallel, ranked candidates, iteriere bis success.
        # Lazy import um circular dependency mit multi_source.py zu vermeiden.
        from services.multi_source import MultiSourceResolver, normalize_corrupt_track_name

        # Normalisierung VOR allem track_name-Use — sonst läuft der Legacy-
        # ytsearch1-Fallback (unten) noch mit dem Original-"Unknown" und
        # findet 0 Items. Helper ist idempotent, der Resolver ruft denselben
        # Helper intern nochmal auf (no-op wenn schon normalisiert).
        track_name = normalize_corrupt_track_name(track_name, track_info)

        resolver = MultiSourceResolver(self)

        try:
            ranking = resolver.resolve(track_name, artist, track_info)
        except Exception as e:
            print(f"WARN: MultiSourceResolver raised {type(e).__name__}: {e} — falling back to legacy yt-dlp ytsearch1")
            ranking = []

        if ranking:
            print(f"Resolver ranking ({len(ranking)} candidates above min_score):")
            for c in ranking[:5]:
                print(f"  [{c['source']:>10}] score={c['score']:.3f} '{c['title'][:60]}' {c['url']}")

            last_err: Optional[str] = None
            for c in ranking:
                src = c["source"]
                url = c["url"]
                try:
                    if src == "youtube" and c.get("video_id"):
                        result = self.download_by_video_id(
                            c["video_id"], output_path, output_format, audio_quality, source_lane=source_lane
                        )
                    else:
                        result = self.download_by_url(
                            url, output_path, output_format, audio_quality, source_lane=source_lane
                        )
                except Exception as e:
                    print(f"WARN: download from {src} raised {type(e).__name__}: {e}")
                    last_err = f"{type(e).__name__}: {e}"
                    continue

                if result.get("success"):
                    # Source-Info anhängen damit Worker/UI weiß welche Quelle
                    # tatsächlich genutzt wurde (für Origin-Pills).
                    result["used_source"] = src
                    result["used_url"] = url
                    result["match_score"] = c.get("score", 0.0)
                    print(f"Download succeeded via {src} (score={c.get('score'):.3f})")
                    return result

                err = result.get("error", "Unknown error")
                last_err = f"[{src}] {err}"
                print(f"WARN: {src} failed: {err} — trying next candidate")

            # Alle ranked candidates haben failed
            return {
                "success": False,
                "error": f"All {len(ranking)} candidates across enabled sources failed. Last: {last_err}",
            }

        # Resolver hat KEINEN Treffer mit min_score gefunden — fallback auf
        # legacy yt-dlp ytsearch1 (alter Default-Pfad). Selten relevant weil
        # MIN_SCORE=0.65 in der Praxis fast immer von mindestens einem
        # Kandidaten erreicht wird.
        print(f"WARN: resolver found no candidate above min_score={config.MULTI_SOURCE_MIN_SCORE} — fallback to legacy ytsearch1")

        # Fallback to original yt-dlp search and download logic if no high-confidence candidate found
        # Create more specific search query to get better matches
        # Include album name if available for better matching
        if track_info and track_info.get('album'):
            query = f"{artist} {track_name} {track_info.get('album')} official"
        else:
            query = f"{artist} {track_name} official audio"
        
        # Convert to absolute path to avoid filesystem issues
        output_path = os.path.abspath(output_path)
        base_path = self._output_base_path(output_path, output_format)
        
        # If the user wants m4a and YouTube provides it as itag 140 (m4a/aac),
        # keep the original container by skipping FFmpegExtractAudio.
        wants_m4a_passthrough = (output_format or '').lower() == 'm4a'

        ydl_opts = {
            # Avoid HLS (m3u8) formats that get blocked - prefer direct audio formats
            'format': 'bestaudio[ext=m4a][protocol!=m3u8]/bestaudio[ext=webm][protocol!=m3u8]/bestaudio[ext=opus][protocol!=m3u8]/bestaudio[protocol!=m3u8]/best[ext=m4a][protocol!=m3u8]/best[ext=webm][protocol!=m3u8]/best[height<=720][protocol!=m3u8]/best',
            # Robust user agent to avoid 403 errors
            'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            # Try different YouTube clients as fallback (helps with 403 errors)
            'extractor_args': {
                'youtube': {
                    'player_client': ['tv_embedded'],  # Try multiple clients
                }
            },
            # Retry configuration for network issues and 403 errors
            'retries': 10,
            'fragment_retries': 10,
            'file_access_retries': 3,
            'outtmpl': self._yt_dlp_outtmpl(base_path),
            'fixup': 'never',  # Skip FixupM4a which causes filesystem errors
            'quiet': False,
            'no_warnings': False,
            'default_search': 'ytsearch1',  # Search and get first result
            'noplaylist': True,
            'extract_flat': False,
            'writesubtitles': False,
            'writeautomaticsub': False,
        }

        # Always add the FFmpegExtractAudio postprocessor when output is m4a
        # This ensures we get a proper .m4a file even if source was Opus/webm
        if wants_m4a_passthrough:
            ydl_opts['postprocessors'] = [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'm4a',
                # 'preferredquality': '256',
                'nopostoverwrites': False,
            }]
            ydl_opts['postprocessor_args'] = {
                'ffmpeg': [
                    # '-af', 'aresample=44100',
                    '-ac', '2',
                    '-c:a', 'copy',
                    '-q:a', '0',
                ]
            }
        else:
            pq = self._preferred_quality_for_extract(output_format, audio_quality)
            preferredcodec = self._ffmpeg_extract_preferredcodec(output_format)
            ydl_opts['postprocessors'] = [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': preferredcodec,
                'preferredquality': pq,
                'nopostoverwrites': False,
            }]
            if output_format not in ('flac', 'wav', 'alac'):
                ydl_opts['postprocessor_args'] = {
                    'ffmpeg': [
                        '-af', 'aresample=44100',
                        '-ac', '2',
                    ]
                }

        ydl_opts = self._add_cookies_to_opts(ydl_opts)
        ydl_opts = _apply_source_lane(ydl_opts, source_lane)
        ydl_opts = _apply_anti_detection_opts(ydl_opts)

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                # Search and download in one step (faster)
                search_query = f"ytsearch1:{query}"
                download_info = ydl.extract_info(search_query, download=True)

                meta = download_info
                # Extract actual video info from ytsearch result (it returns entries)
                if download_info.get('entries'):
                    video_entry = download_info['entries'][0]
                    if video_entry:
                        # Validate the match after download
                        youtube_title = (video_entry.get('title') or download_info.get('title') or '').lower()
                        youtube_uploader = (video_entry.get('uploader') or download_info.get('uploader') or '').lower()

                        track_name_lower = track_name.lower()
                        artist_parts = [a.strip().lower() for a in artist.lower().split(',')]
                        main_artist = artist_parts[0] if artist_parts else ''

                        # Check if title contains key words from track name
                        track_words = [w for w in track_name_lower.split() if len(w) > 2]
                        title_match = track_name_lower in youtube_title or any(word in youtube_title for word in track_words)
                        artist_match = main_artist in youtube_title or main_artist in youtube_uploader

                        # Log for debugging (non-blocking)
                        print(f"YouTube result: '{video_entry.get('title') or download_info.get('title')}' by '{video_entry.get('uploader') or download_info.get('uploader')}'")
                        print(f"Looking for: '{track_name}' by '{artist}' - Match: title={title_match}, artist={artist_match}")

                        if video_entry.get('title'):
                            meta = video_entry

                actual_path = self._resolve_downloaded_audio(
                    base_path, output_format, wants_m4a_passthrough, download_info, ydl
                )
                if not actual_path:
                    exp = f"{base_path}.m4a" if wants_m4a_passthrough else f"{base_path}.{output_format}"
                    raise FileNotFoundError(f"Downloaded file not found. Expected: {exp}")

                return {
                    'success': True,
                    'file_path': actual_path,
                    'title': meta.get('title', track_name),
                    'duration': meta.get('duration', 0),
                    'url': meta.get('webpage_url', ''),
                }
        
        except Exception as e:
            error_msg = str(e)
            
            # Provide helpful error messages for common issues
            if '403' in error_msg or 'Forbidden' in error_msg:
                error_msg = "YouTube blocked the request (HTTP 403). This can happen due to rate limiting, IP blocking, or YouTube's anti-bot measures. Try again in a few minutes, or ensure yt-dlp is up to date: pip install --upgrade yt-dlp"
            elif 'HTTP Error' in error_msg:
                error_msg = f"Network error: {error_msg}. Check your internet connection and try again."
            elif 'unable to download video data' in error_msg.lower():
                error_msg = f"YouTube download failed: {error_msg}. This may be due to the video being unavailable, region-locked, or YouTube blocking the request. Try a different track or wait a few minutes."
            
            print(f"YouTube download error: {e}")
            return {
                'success': False,
                'error': error_msg
            }
    
    def sanitize_filename(self, filename: str) -> str:
        """Remove invalid characters from filename"""
        # Remove invalid characters
        filename = re.sub(r'[<>:"/\\|?*]', '', filename)
        # Replace multiple spaces with single space
        filename = re.sub(r'\s+', ' ', filename)
        # Trim
        filename = filename.strip()
        return filename

    def extract_video_info(self, url_or_id: str) -> Dict:
        """Extract YouTube video metadata (no download).

        Accepts a YouTube/YouTube Music URL or a raw video id.
        """
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'noplaylist': True,
            'skip_download': True,
            'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'extractor_args': {
                'youtube': {
                    'player_client': config.YOUTUBE_PLAYER_CLIENTS,
                }
            },
        }
        ydl_opts = self._add_cookies_to_opts(ydl_opts)

        # Build a canonical URL if a bare ID was provided
        url = url_or_id
        if re.fullmatch(r"[A-Za-z0-9_-]{11}", (url_or_id or "")):
            url = f"https://www.youtube.com/watch?v={url_or_id}"

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)

            thumbnails = info.get('thumbnails') or []
            thumb_url = ''
            if isinstance(thumbnails, list) and thumbnails:
                # Pick the last (usually highest res)
                thumb_url = (thumbnails[-1] or {}).get('url') or ''
            if not thumb_url:
                thumb_url = info.get('thumbnail') or ''

            return {
                'success': True,
                'video_id': info.get('id') or '',
                'title': info.get('title') or '',
                'uploader': info.get('uploader') or info.get('channel') or '',
                'duration': info.get('duration') or 0,
                'webpage_url': info.get('webpage_url') or url,
                'thumbnail': thumb_url,
            }
        except Exception as e:
            return {
                'success': False,
                'error': str(e),
            }

    # ------------------------------------------------------------------
    # Generischer URL-Download (Phase 1) — funktioniert mit allem, was yt-dlp
    # versteht: YouTube, SoundCloud, Bandcamp, Vimeo, Mixcloud, Dailymotion …
    # Im Gegensatz zu download_by_video_id baut das hier KEINE YouTube-URL
    # zusammen, sondern reicht die User-URL direkt an yt-dlp weiter.
    # ------------------------------------------------------------------

    def download_by_url(self, url: str, output_path: str, output_format: str = None, audio_quality: str = None, source_lane: Optional[str] = None) -> Dict:
        """Download audio from any yt-dlp-supported URL (YouTube, SoundCloud, ...). source_lane∈{a,b,None} → Source-IP-Bind (Dual-VPN)."""
        output_format = (output_format or self.output_format or "mp3").strip().lower()
        audio_quality = audio_quality or self.audio_quality

        output_path = os.path.abspath(output_path)
        base_path = self._output_base_path(output_path, output_format)

        wants_m4a_passthrough = (output_format or "").lower() == "m4a"

        ydl_opts = {
            # Identische Format-Kaskade wie download_by_video_id — präzise ext-Fallbacks
            # vermeiden ffmpeg "Function not implemented", weil yt-dlp uns einen Container
            # liefert, in den der gewählte preferredcodec sauber gemuxt werden kann.
            'format': 'bestaudio[ext=m4a][protocol!=m3u8]/bestaudio[ext=webm][protocol!=m3u8]/bestaudio[ext=opus][protocol!=m3u8]/bestaudio[protocol!=m3u8]/best[ext=m4a][protocol!=m3u8]/best[ext=webm][protocol!=m3u8]/best[height<=720][protocol!=m3u8]/best',
            'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'extractor_args': {
                'youtube': {'player_client': ['tv_embedded']},
            },
            'retries': 10,
            'fragment_retries': 10,
            'file_access_retries': 3,
            'outtmpl': self._yt_dlp_outtmpl(base_path),
            'fixup': 'never',
            'quiet': config.YT_DLP_QUIET,
            'no_warnings': config.YT_DLP_QUIET,
            'noplaylist': True,
        }

        if wants_m4a_passthrough:
            # Wenn die yt-dlp-Source bereits .m4a ist (Format-Kaskade präferiert
            # bestaudio[ext=m4a]), würde FFmpegExtractAudio mit preferredcodec=m4a
            # einen zweiten Destination-Schreib triggern — das ist die Quelle
            # des doppelten "[download] Destination: ... .m4a"-Logs. nopostoverwrites=True
            # lässt yt-dlp den Postprocessor-Output skippen, wenn die Datei schon
            # vorhanden ist (= yt-dlp hat sie gerade selbst gespeichert).
            # Bei webm/opus-Fallback existiert die .m4a noch NICHT → Postprocessor
            # läuft regulär und konvertiert mit '-c:a copy' (oder Re-Encode falls
            # Codec inkompatibel).
            ydl_opts['postprocessors'] = [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'm4a',
                'nopostoverwrites': True,
            }]
            ydl_opts['postprocessor_args'] = {
                'ffmpeg': ['-ac', '2', '-c:a', 'copy', '-q:a', '0'],
            }
        else:
            pq = self._preferred_quality_for_extract(output_format, audio_quality)
            preferredcodec = self._ffmpeg_extract_preferredcodec(output_format)
            ydl_opts['postprocessors'] = [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': preferredcodec,
                'preferredquality': pq,
                'nopostoverwrites': False,
            }]
            if output_format not in ('flac', 'wav', 'alac'):
                ydl_opts['postprocessor_args'] = {
                    'ffmpeg': ['-af', 'aresample=44100', '-ac', '2'],
                }

        ydl_opts = self._add_cookies_to_opts(ydl_opts)
        ydl_opts = _apply_source_lane(ydl_opts, source_lane)
        ydl_opts = _apply_anti_detection_opts(ydl_opts)

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                actual_path = self._resolve_downloaded_audio(
                    base_path, output_format, wants_m4a_passthrough, info, ydl
                )
                if not actual_path:
                    exp = f"{base_path}.m4a" if wants_m4a_passthrough else f"{base_path}.{output_format}"
                    raise FileNotFoundError(f"Downloaded file not found. Expected: {exp}")

                return {
                    'success': True,
                    'file_path': actual_path,
                    'title': info.get('title', ''),
                    'duration': info.get('duration', 0),
                    'url': info.get('webpage_url', url),
                    'extractor': info.get('extractor_key') or info.get('extractor') or '',
                }
        except Exception as e:
            error_msg = str(e)
            if '403' in error_msg or 'Forbidden' in error_msg:
                error_msg = "Provider blocked the request (HTTP 403). Try again later or check yt-dlp/cookies."
            elif '429' in error_msg:
                error_msg = "Provider rate-limited the request (HTTP 429). Wait or configure cookies."
            return {'success': False, 'error': error_msg}

    # ------------------------------------------------------------------
    # Generische Suche (Phase 2)
    # source='youtube' → ytsearch{N}:query, source='soundcloud' → scsearch{N}:query
    # Liefert eine schmale Treffer-Liste fürs UI (kein Download).
    # ------------------------------------------------------------------

    def search_url(self, query: str, source: str = "youtube", limit: int = 10) -> Dict:
        """Suche per yt-dlp und liefere nur Metadaten zurück (keine Downloads).

        Source-Mapping:
            youtube    → ytsearch{N}: (flat extract, schnell, IDs zu URLs ergänzt)
            soundcloud → scsearch{N}: (full extract, sonst kaputte Resolver-URLs)
            bandcamp   → bcsearch{N}: (full extract, sonst Pseudo-URLs ohne webpage_url)
        """
        query = (query or "").strip()
        if not query:
            return {'success': True, 'results': []}
        limit = max(1, min(int(limit or 10), 25))

        prefix_map = {
            "youtube": "ytsearch",
            "soundcloud": "scsearch",
            "bandcamp": "bcsearch",
        }
        prefix = prefix_map.get(source, "ytsearch")
        search_url = f"{prefix}{limit}:{query}"

        # SoundCloud + Bandcamp brauchen full extract weil flat-extract bei
        # beiden Pseudo-Resolver-URLs liefert (sc: 'soundcloud:tracks:NNN',
        # bc: 'bandcamp:trackid:NNN'). Kostet 1 Extra-Roundtrip pro Treffer.
        needs_full_extract = source in ("soundcloud", "bandcamp")
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'skip_download': True,
            'extract_flat': False if needs_full_extract else True,
            'noplaylist': False,
            'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        }
        ydl_opts = self._add_cookies_to_opts(ydl_opts)

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(search_url, download=False)
            entries = (info or {}).get('entries') or []
            results: List[Dict] = []
            for e in entries:
                if not e:
                    continue
                # SC + BC liefern 'webpage_url' aus full extract. YT mit flat
                # extract liefert nur 'id' das wir zur Watch-URL ergänzen.
                if needs_full_extract:
                    webpage_url = (
                        e.get('webpage_url')
                        or e.get('permalink_url')
                        or e.get('url')
                        or ''
                    )
                    # API-URLs (z.B. api.soundcloud.com) skippen — die kann
                    # yt-dlp im Frontend nicht öffnen
                    if 'api.' in (webpage_url or ''):
                        continue
                else:
                    webpage_url = e.get('url') or e.get('webpage_url') or ''
                    if webpage_url and not webpage_url.startswith('http'):
                        webpage_url = f"https://www.youtube.com/watch?v={webpage_url}"

                thumbs = e.get('thumbnails') or []
                thumb = ''
                if isinstance(thumbs, list) and thumbs:
                    thumb = (thumbs[-1] or {}).get('url') or ''
                if not thumb:
                    thumb = e.get('thumbnail') or ''
                results.append({
                    'url': webpage_url,
                    'id': str(e.get('id') or ''),
                    'title': e.get('title') or '',
                    'uploader': e.get('uploader') or e.get('channel') or e.get('uploader_id') or '',
                    'duration': int(e.get('duration') or 0) if e.get('duration') is not None else 0,
                    'thumbnail': thumb,
                    'source': source,
                })
            return {'success': True, 'results': results}
        except Exception as e:
            return {'success': False, 'error': str(e), 'results': []}

