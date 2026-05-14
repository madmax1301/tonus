import ipaddress
import os
from urllib.parse import urlparse

import requests
from mutagen.id3 import ID3, TIT2, TPE1, TPE2, TALB, APIC, TDRC, TRCK, TCON
from mutagen.mp3 import MP3
from mutagen.flac import FLAC, Picture
from mutagen.mp4 import MP4, MP4Cover
from typing import Dict, List
from pathlib import Path

import config


def _is_allowed_album_art_url(url: str) -> bool:
    """SSRF-Schutz für album_art-Downloads (Audit C-1, 2026-05-12).

    Validiert in dieser Reihenfolge:
      1. URL parsebar, scheme ∈ {http, https}
      2. Host vorhanden und KEIN bare IP-Literal — verhindert direct-IP
         Bypass auf Cloud-Metadata (169.254.169.254) und Docker-bridge-
         Hosts. Wer einen legitimen IP-only-CDN nutzt, kann ihn via DNS-
         Eintrag in der Allowlist whitelisten.
      3. Host matched eines Suffixes aus config.ALBUM_ART_ALLOWED_HOSTS
         (Subdomain-Match aktiv: `sndcdn.com` → auch `i1.sndcdn.com`).
    """
    if not url or not isinstance(url, str):
        return False
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").lower().strip()
    if not host:
        return False
    try:
        ipaddress.ip_address(host)
        return False
    except ValueError:
        pass
    for allowed in config.ALBUM_ART_ALLOWED_HOSTS:
        if host == allowed or host.endswith("." + allowed):
            return True
    return False


class MetadataService:
    def __init__(self):
        pass

    @staticmethod
    def _get_genre_string(track_info: Dict) -> str:
        """Reduziert eine genres-Liste auf einen Tag-String.

        Akzeptiert verschiedene Eingabeformen, die in der Codebasis vorkommen:
        - track_info['genres'] = ['Hard Rock', 'Metal']  (list[str])
        - track_info['genres'] = [{'name': 'Rock', 'id': 152}, ...]  (Deezer Album-Genre-Objekte)
        - track_info['genres'] = 'Rock; Metal'  (string mit Semikolon)

        Joint mit '; ', das ist der Standard für ID3 V2.4 Multi-Genres und wird auch
        von FLAC/Vorbis-Comments und Navidrome korrekt geparst.
        """
        raw = track_info.get('genres')
        if not raw:
            return ''
        if isinstance(raw, str):
            return raw.strip()
        if isinstance(raw, (list, tuple)):
            names: List[str] = []
            seen = set()
            for item in raw:
                if isinstance(item, str):
                    n = item.strip()
                elif isinstance(item, dict):
                    n = (item.get('name') or '').strip()
                else:
                    n = str(item).strip()
                if n and n not in seen:
                    seen.add(n)
                    names.append(n)
            return '; '.join(names)
        return str(raw).strip()

    def apply_metadata(self, file_path: str, track_info: Dict) -> bool:
        """Apply metadata and album art to audio file"""
        try:
            file_ext = Path(file_path).suffix.lower()

            if file_ext == '.mp3':
                return self._apply_mp3_metadata(file_path, track_info)
            elif file_ext == '.flac':
                return self._apply_flac_metadata(file_path, track_info)
            elif file_ext == '.m4a':
                return self._apply_m4a_metadata(file_path, track_info)
            elif file_ext == '.opus':
                return self._apply_opus_metadata(file_path, track_info)
            else:
                print(f"Metadata tagging not supported for {file_ext}")
                return False

        except Exception as e:
            print(f"Error applying metadata: {e}")
            return False

    def _download_album_art(self, url: str) -> bytes | None:
        if not url:
            return None
        if not _is_allowed_album_art_url(url):
            print(f"Album art URL rejected by allowlist (Audit C-1): {url[:80]}")
            return None
        try:
            response = requests.get(url, timeout=10, headers={'User-Agent': 'Mozilla/5.0'}, allow_redirects=False)
            if response.status_code == 200:
                return response.content
            if response.status_code in (301, 302, 303, 307, 308):
                # Folge nur Redirects deren Ziel wieder in der Allowlist liegt.
                # Sonst könnte ein erlaubter Host auf einen internen Host
                # weiterleiten und die Allowlist umgehen.
                redirect_target = response.headers.get('Location', '')
                if redirect_target and _is_allowed_album_art_url(redirect_target):
                    response = requests.get(redirect_target, timeout=10, headers={'User-Agent': 'Mozilla/5.0'}, allow_redirects=False)
                    if response.status_code == 200:
                        return response.content
        except Exception as e:
            print(f"Error downloading album art: {e}")
        return None

    def _apply_mp3_metadata(self, file_path: str, track_info: Dict) -> bool:
        """Apply metadata to MP3 file"""
        try:
            # Ensure artist names are joined with semicolons
            if 'artist' in track_info and isinstance(track_info.get('artist'), str):
                track_info['artist'] = track_info['artist'].replace(',', ';')

            artist_name = ''
            if 'album_artist' in track_info and isinstance(track_info.get('album_artist'), str):
                track_info['album_artist'] = track_info['album_artist'].replace(',', ';')
                # Extract only the first artist for album artist
                artist_name = track_info['album_artist'].split(';')[0].strip()

            audio = MP3(file_path, ID3=ID3)

            # Add ID3 tag if it doesn't exist
            try:
                audio.add_tags()
            except Exception:
                pass

            # Set basic metadata
            audio['TIT2'] = TIT2(encoding=3, text=track_info.get('name', ''))
            audio['TPE1'] = TPE1(encoding=3, text=track_info.get('artist', ''))
            audio['TPE2'] = TPE2(encoding=3, text=artist_name)
            audio['TALB'] = TALB(encoding=3, text=track_info.get('album', ''))
            audio['TRCK'] = TRCK(encoding=3, text=str(track_info.get('track_number', 1)))

            genre_str = self._get_genre_string(track_info)
            if genre_str:
                audio['TCON'] = TCON(encoding=3, text=genre_str)

            if track_info.get('release_date'):
                audio['TDRC'] = TDRC(encoding=3, text=str(track_info['release_date'])[:4])

            # Add album art
            if track_info.get('album_art'):
                art_bytes = self._download_album_art(track_info.get('album_art'))
                if art_bytes:
                    try:
                        audio.tags.add(APIC(
                            encoding=3,
                            mime='image/jpeg',
                            type=3,
                            desc='Cover',
                            data=art_bytes
                        ))
                    except Exception as e:
                        print(f"Error adding album art: {e}")

            audio.save()
            return True

        except Exception as e:
            print(f"Error applying MP3 metadata: {e}")
            return False

    def _apply_flac_metadata(self, file_path: str, track_info: Dict) -> bool:
        """Apply metadata to FLAC file"""
        try:
            audio = FLAC(file_path)

            audio['TITLE'] = track_info.get('name', '')
            audio['ARTIST'] = track_info.get('artist', '')
            audio['ALBUM'] = track_info.get('album', '')
            audio['TRACKNUMBER'] = str(track_info.get('track_number', 1))

            genre_str = self._get_genre_string(track_info)
            if genre_str:
                audio['GENRE'] = genre_str

            if track_info.get('release_date'):
                audio['DATE'] = str(track_info['release_date'])[:4]

            if track_info.get('album_art'):
                art_bytes = self._download_album_art(track_info.get('album_art'))
                if art_bytes:
                    try:
                        picture = Picture()
                        picture.type = 3  # Cover (front)
                        picture.mime = 'image/jpeg'
                        picture.data = art_bytes
                        audio.add_picture(picture)
                    except Exception as e:
                        print(f"Error adding album art: {e}")

            audio.save()
            return True

        except Exception as e:
            print(f"Error applying FLAC metadata: {e}")
            return False

    def _apply_m4a_metadata(self, file_path: str, track_info: Dict) -> bool:
        """Apply metadata to M4A (MP4) file."""
        try:
            audio = MP4(file_path)

            title = track_info.get('name', '')
            artist = track_info.get('artist', '')
            album = track_info.get('album', '')
            track_number = track_info.get('track_number', 1)
            album_artist = track_info.get('album_artist')

            if isinstance(artist, str):
                # MP4 typically expects a list for artists
                artists = [a.strip() for a in artist.replace(';', ',').split(',') if a.strip()]
            else:
                artists = []

            audio['\xa9nam'] = [title] if title else []
            audio['\xa9ART'] = artists
            audio['\xa9alb'] = [album] if album else []

            if album_artist and isinstance(album_artist, str):
                aa = album_artist.split(',')[0].split(';')[0].strip()
                audio['aART'] = [aa] if aa else []

            # track number is tuple: (track, total)
            try:
                audio['trkn'] = [(int(track_number), 0)]
            except Exception:
                pass

            genre_str = self._get_genre_string(track_info)
            if genre_str:
                audio['\xa9gen'] = [genre_str]

            if track_info.get('release_date'):
                year = str(track_info['release_date'])[:4]
                audio['\xa9day'] = [year]

            # Album art
            art_url = track_info.get('album_art')
            if art_url:
                art_bytes = self._download_album_art(art_url)
                if art_bytes:
                    # Mutagen uses MP4Cover with imageformat
                    cover = MP4Cover(art_bytes, imageformat=MP4Cover.FORMAT_JPEG)
                    audio['covr'] = [cover]

            audio.save()
            return True

        except Exception as e:
            print(f"Error applying M4A metadata: {e}")
            return False

    def _apply_opus_metadata(self, file_path: str, track_info: Dict) -> bool:
        """Apply metadata to Opus/Ogg file (Vorbis Comments)"""
        try:
            from mutagen.oggopus import OggOpus
            from mutagen.flac import Picture
            import base64

            audio = OggOpus(file_path)

            audio['TITLE'] = track_info.get('name', '')
            audio['ARTIST'] = track_info.get('artist', '')
            audio['ALBUM'] = track_info.get('album', '')

            # Album artist aus track_info holen
            if track_info.get('album_artist'):
                aa = track_info['album_artist'].split(',')[0].split(';')[0].strip()
                if aa:
                    audio['ALBUMARTIST'] = aa

            audio['TRACKNUMBER'] = str(track_info.get('track_number', 1))

            genre_str = self._get_genre_string(track_info)
            if genre_str:
                audio['GENRE'] = genre_str

            if track_info.get('release_date'):
                audio['DATE'] = str(track_info['release_date'])[:4]

            # Cover Art einbetten (wie FLAC: Picture-Blob)
            if track_info.get('album_art'):
                art_bytes = self._download_album_art(track_info.get('album_art'))
                if art_bytes:
                    try:
                        pic = Picture()
                        pic.type = 3
                        pic.mime = 'image/jpeg'
                        pic.data = art_bytes
                        audio['metadata_block_picture'] = [base64.b64encode(pic.write()).decode()]
                    except Exception as e:
                        print(f"Error adding Opus album art: {e}")

            audio.save()
            return True

        except Exception as e:
            print(f"Error applying Opus metadata: {e}")
            return False
