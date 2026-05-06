"""Multi-Source-Resolver — pre-searcht einen Track parallel auf allen aktivierten
Quellen (YouTube/SoundCloud/Bandcamp), scort die Kandidaten via existing match-
Scoring aus YouTubeService und liefert ein Ranking zurück.

Worker nutzt das Ranking als ordered Fallback-Chain: erstes Element wird
gedownloadet, bei Fehler das zweite, etc. Damit fallen Age-Gated- oder
Anti-Bot-Failures auf andere Quellen zurück, ohne dass der User Cookies
oder Account-Konfiguration setzen muss.

Architektur:
- ThreadPoolExecutor für parallele Suche (yt-dlp + ytmusicapi sind sync,
  asyncio würde die nicht beschleunigen — Threads OK weil I/O-bound)
- Per-Source-Timeout (config.MULTI_SOURCE_TIMEOUT_S) damit eine kaputte
  Quelle das Resolve nicht beliebig blockiert
- Source-Priority bei Score-Ties: Reihenfolge in ENABLED_SOURCES gewinnt
  (User kann via .env preference setzen, z.B. SoundCloud bei Auth-shy
  Setups vor YouTube ziehen)
- min_score-Threshold: schwache Treffer (z.B. nur Title-Match) werden
  verworfen statt ausgeliefert — sonst landen falsche Tracks in der Lib
"""
from __future__ import annotations

import sys
import os
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from typing import Dict, List, Optional, Any

import yt_dlp

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

from services.youtube import _apply_anti_detection_opts


class MultiSourceResolver:
    """Resolve a track to a ranked list of (source, url, score, meta) candidates.

    Reusable across the codebase — both `search_and_download()` (auto-pull) und
    der API-Endpoint `/api/search/top` können denselben Resolver aufrufen.
    """

    def __init__(self, youtube_service: Any) -> None:
        self.yt = youtube_service  # YouTubeService instance for YT/YTMusic search
        self.enabled_sources = config.ENABLED_SOURCES
        self.timeout_s = config.MULTI_SOURCE_TIMEOUT_S
        self.min_score = config.MULTI_SOURCE_MIN_SCORE
        self.candidates_per_source = config.MULTI_SOURCE_CANDIDATES_PER_SOURCE
        # Source-Priority-Map für Tie-Breaking — niedriger Index = höher Priorität
        self._source_priority = {s: i for i, s in enumerate(self.enabled_sources)}

    def resolve(
        self,
        track_name: str,
        artist: str,
        track_info: Optional[Dict] = None,
    ) -> List[Dict]:
        """Returns ranked candidates [{source, url, video_id, title, score, meta}, ...].

        Empty list wenn keine Quelle einen Treffer >= min_score liefert. Caller
        muss diesen Fall (alle Sources fail) als Error behandeln.
        """
        if not self.enabled_sources:
            return []

        all_candidates: List[Dict] = []

        with ThreadPoolExecutor(max_workers=max(1, len(self.enabled_sources))) as pool:
            futures = {
                src: pool.submit(self._search_source, src, track_name, artist, track_info)
                for src in self.enabled_sources
            }
            for src, fut in futures.items():
                try:
                    candidates = fut.result(timeout=self.timeout_s)
                    all_candidates.extend(candidates)
                except FuturesTimeoutError:
                    print(f"WARN: source '{src}' resolve timed out after {self.timeout_s}s")
                except Exception as e:
                    print(f"WARN: source '{src}' resolve failed: {type(e).__name__}: {e}")

        # min_score-Filter, dann sort by score DESC, tiebreak by source priority ASC
        filtered = [c for c in all_candidates if c.get("score", 0.0) >= self.min_score]
        filtered.sort(
            key=lambda c: (
                -float(c.get("score", 0.0)),
                self._source_priority.get(c.get("source", ""), 99),
            )
        )
        return filtered

    # ──────────────────────────────────────────────────────────────────
    # Per-Source Search-Helpers
    # ──────────────────────────────────────────────────────────────────

    def _search_source(
        self,
        src: str,
        track_name: str,
        artist: str,
        track_info: Optional[Dict],
    ) -> List[Dict]:
        if src == "youtube":
            return self._search_youtube(track_name, artist, track_info)
        if src == "soundcloud":
            return self._search_yt_dlp_extractor("scsearch", "soundcloud", track_name, artist, track_info)
        if src == "bandcamp":
            return self._search_yt_dlp_extractor("bcsearch", "bandcamp", track_name, artist, track_info)
        print(f"WARN: unknown source in ENABLED_SOURCES: '{src}'")
        return []

    def _search_youtube(
        self,
        track_name: str,
        artist: str,
        track_info: Optional[Dict],
    ) -> List[Dict]:
        """Reuse den existing YouTubeService-Helper der YTMusic + yt-dlp-Fallback
        kombiniert und schon scoring macht."""
        try:
            res = self.yt.search_candidates(track_name, artist, track_info, num_results=self.candidates_per_source)
        except Exception as e:
            print(f"WARN: youtube search_candidates raised {type(e).__name__}: {e}")
            return []
        if not res.get("success"):
            return []
        out: List[Dict] = []
        for c in res.get("candidates", []) or []:
            out.append(
                {
                    "source": "youtube",
                    "url": c.get("url") or f"https://www.youtube.com/watch?v={c.get('video_id', '')}",
                    "video_id": c.get("video_id", ""),
                    "title": c.get("title", ""),
                    "score": float(c.get("score", 0.0)),
                    "meta": c,
                }
            )
        return out

    def _search_yt_dlp_extractor(
        self,
        prefix: str,
        source_label: str,
        track_name: str,
        artist: str,
        track_info: Optional[Dict],
    ) -> List[Dict]:
        """Generischer yt-dlp-Search für scsearch (SoundCloud) und bcsearch (Bandcamp).

        Beide nutzen yt-dlp's flat-extract Pfad. SoundCloud braucht full extract
        weil flat-extract kaputte Resolver-URLs liefert; Bandcamp ist ähnlich —
        flat-extract liefert dort `bandcamp:trackid:NNN`-Pseudo-URLs, also
        full extract.

        Scoring nutzt YouTubeService.calculate_match_score.
        """
        n = max(1, int(self.candidates_per_source))
        query = f"{artist} {track_name}".strip()
        if track_info and track_info.get("album"):
            query = f"{query} {track_info['album']}"

        # Beide Provider profitieren von full-extract (statt flat) für saubere
        # webpage_urls. Kostet 1 Extra-Roundtrip pro Treffer — bei 3 candidates
        # also 3 Calls. Akzeptabel innerhalb des per-source-Timeouts.
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "extract_flat": False,
            "noplaylist": True,
            "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        }
        # Anti-detection-helper aus youtube.py wiederverwendet — gleiche
        # ratelimit/sleep/impersonate-Werte für alle Sources.
        ydl_opts = _apply_anti_detection_opts(ydl_opts)

        search_url = f"{prefix}{n}:{query}"
        out: List[Dict] = []
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(search_url, download=False)
        except Exception as e:
            print(f"WARN: {source_label} search failed: {type(e).__name__}: {e}")
            return []

        entries = (info or {}).get("entries") or []
        for idx, e in enumerate(entries, start=1):
            if not e:
                continue
            webpage_url = (
                e.get("webpage_url")
                or e.get("permalink_url")
                or e.get("url")
                or ""
            )
            # Skip wenn webpage_url noch eine API-URL ist (kann bei SC vorkommen)
            if not webpage_url or "api." in webpage_url:
                continue

            title = e.get("title", "") or ""
            uploader = e.get("uploader") or e.get("channel") or e.get("uploader_id") or ""
            duration = int(e.get("duration") or 0) if e.get("duration") is not None else 0

            # Score via existing helper aus YouTubeService — gleiche Algorithmik
            # für alle Sources damit Ranking konsistent ist.
            try:
                score = self.yt.calculate_match_score(
                    title,
                    uploader,
                    track_name,
                    artist,
                    track_info=track_info,
                    rank=idx,
                    source=source_label,
                    yt_duration_seconds=duration,
                    yt_duration_str="",
                )
            except Exception:
                score = 0.0

            out.append(
                {
                    "source": source_label,
                    "url": webpage_url,
                    "video_id": str(e.get("id") or ""),
                    "title": title,
                    "score": round(float(score), 3),
                    "meta": {
                        "uploader": uploader,
                        "duration": duration,
                        "thumbnail": e.get("thumbnail") or "",
                    },
                }
            )
        return out
