import logging
import re
from typing import Optional, Tuple
from models import Song, LyricsTrack
from database import DatabaseManager
from karaoke.provider import LyricsProvider
from utils.helpers import _normalize_text, _normalize_turkish, _similarity

logger = logging.getLogger(__name__)

class LyricsService:
    def __init__(self, db: DatabaseManager, provider: LyricsProvider):
        self.db = db
        self.provider = provider

    @staticmethod
    def cache_key(title: str, artist: Optional[str], duration: Optional[int]) -> str:
        norm_title = _normalize_text(title)
        norm_artist = _normalize_text(artist or "")
        bucket = int(duration // 3) if duration else 0
        return f"{norm_title}|{norm_artist}|{bucket}"

    def _clean_title_part(self, title_part: str) -> str:
        if not title_part:
            return title_part
        cleaned = re.sub(r'\([^)]*\)', '', title_part)
        cleaned = re.sub(r'\[[^\]]*\]', '', cleaned)
        keywords = r'(?:Official\s*(?:Music\s*)?Video|Music\s*Video|Official\s*Audio|Lyrics?\s*Video|HD|4K|Official|Video|Audio)'
        pattern = r'\s*[|/–—\-]\s*' + keywords + r'$'
        cleaned = re.sub(pattern, '', cleaned, flags=re.IGNORECASE)
        return cleaned.strip()

    def _parse_metadata(self, song: Song) -> Tuple[str, Optional[str]]:
        raw_title = song.title or ""
        uploader = song.uploader
        cleaned = raw_title
        cleaned = re.sub(r'\([^)]*\)', '', cleaned)
        cleaned = re.sub(r'\[[^\]]*\]', '', cleaned)
        separators = [" - ", " – ", " — ", " | "]
        artist = None
        title = None
        source = "unknown"
        removed_suffix = ""

        for sep in separators:
            if sep in cleaned:
                parts = cleaned.split(sep, 1)
                potential_artist = parts[0].strip()
                potential_title = parts[1].strip()
                if potential_artist and len(potential_artist) > 1 and potential_title:
                    cleaned_title = self._clean_title_part(potential_title)
                    if cleaned_title != potential_title:
                        removed_suffix = potential_title.replace(cleaned_title, "").strip()
                    potential_artist = potential_artist.strip()
                    if potential_artist and cleaned_title:
                        artist = potential_artist
                        title = cleaned_title
                        source = "title_parser"
                        break

        if not artist and uploader:
            for sep in separators:
                if raw_title.startswith(uploader + sep):
                    potential_title = raw_title[len(uploader) + len(sep):].strip()
                    cleaned_title = self._clean_title_part(potential_title)
                    if cleaned_title:
                        artist = uploader
                        title = cleaned_title
                        source = "uploader_stripped"
                        break

        if not artist:
            if uploader and uploader.lower() not in ("unknown", "none", ""):
                artist = uploader
                source = "uploader_fallback"
            title = self._clean_title_part(cleaned) if not title else title

        if not title:
            title = raw_title

        if artist and title and artist.lower() == title.lower():
            artist = None
            source = "same_as_title"

        logger.info(f"[KARAOKE] Metadata parsed: raw_title='{raw_title}', raw_uploader='{uploader}', "
                    f"parsed_artist='{artist}', parsed_title='{title}', source='{source}', removed_suffix='{removed_suffix}'")
        return title, artist

    async def _try_provider(self, title: str, artist: Optional[str], duration: Optional[int], variation_desc: str) -> Optional[LyricsTrack]:
        logger.info(f"[KARAOKE] Trying {variation_desc}: title='{title}', artist='{artist}'")
        try:
            track = await self.provider.fetch(title, artist, duration)
            if track:
                logger.info(f"[KARAOKE] {variation_desc} succeeded!")
                return track
        except Exception as e:
            logger.warning(f"[KARAOKE] {variation_desc} failed with error: {e}")
        return None

    async def get_lyrics(self, song: Song) -> Optional[LyricsTrack]:
        title, artist = self._parse_metadata(song)
        duration = song.duration
        logger.info(f"[KARAOKE] Lyrics lookup started for: raw title='{song.title}', raw uploader='{song.uploader}'")
        logger.info(f"[KARAOKE] Parsed title='{title}', parsed artist='{artist}', raw duration={duration}s")

        key = self.cache_key(title, artist, duration)
        bucket = int(duration // 3) if duration else 0
        logger.info(f"[KARAOKE] Cache key: {key} (duration bucket={bucket})")

        try:
            cached = await self.db.get_cached_lyrics(key)
        except Exception as e:
            logger.warning(f"[KARAOKE] Cache read failed: {e}")
            cached = None

        if cached is not None:
            if cached == "NONE":
                logger.info(f"[KARAOKE] Negative cache entry found (TTL handled internally, treating as not found)")
                return None
            logger.info(f"[KARAOKE] Cache hit: synchronized lyrics found for '{title}'")
            return cached

        variations = []
        variations.append((title, artist, "original"))
        if artist:
            norm_artist = _normalize_turkish(artist)
            norm_title = _normalize_turkish(title)
            if (norm_artist != artist) or (norm_title != title):
                variations.append((norm_title, norm_artist, "turkish_normalized"))
        if artist:
            variations.append((title, None, "title_only"))
        for feat_pattern in [r'\s*[\(\[]feat\.?\s+[^\)\]]+[\)\]]', r'\s*ft\.?\s+[^\s]+']:
            cleaned_title = re.sub(feat_pattern, '', title, flags=re.IGNORECASE).strip()
            if cleaned_title and cleaned_title != title:
                variations.append((cleaned_title, artist, "no_feat"))
        if not artist:
            for sep in [" - ", " – ", " — ", " | "]:
                if sep in title:
                    parts = title.split(sep, 1)
                    pot_artist = parts[0].strip()
                    pot_title = parts[1].strip()
                    if pot_artist and pot_title and len(pot_artist) > 1:
                        variations.append((pot_title, pot_artist, "split_from_title"))
                        break

        seen = set()
        unique_variations = []
        for t, a, desc in variations:
            key_v = (t.lower(), (a.lower() if a else None))
            if key_v not in seen:
                seen.add(key_v)
                unique_variations.append((t, a, desc))

        for t, a, desc in unique_variations:
            track = await self._try_provider(t, a, duration, desc)
            if track:
                await self.db.cache_lyrics(key, track)
                return track

        logger.info(f"[KARAOKE] All lookup strategies failed for '{title}'")
        await self.db.cache_lyrics_negative(key)
        return None