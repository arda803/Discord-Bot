import asyncio
import logging
from typing import Optional, Dict, Any, List
import aiohttp
from models import LyricsTrack
from utils.helpers import _similarity, _parse_lrc

logger = logging.getLogger(__name__)

class LyricsProvider:
    async def fetch(self, title: str, artist: Optional[str], duration: Optional[int]) -> Optional[LyricsTrack]:
        raise NotImplementedError

class LRCLibProvider(LyricsProvider):
    BASE_URL = "https://lrclib.net/api"
    MIN_TITLE_CONFIDENCE = 0.45
    MAX_DURATION_DRIFT = 10

    def _validate(self, data: Dict[str, Any], title: str, artist: Optional[str], duration: Optional[int]) -> Optional[LyricsTrack]:
        synced = data.get("syncedLyrics")
        if not synced:
            return None
        result_duration = data.get("duration")
        if duration and result_duration and abs(duration - result_duration) > self.MAX_DURATION_DRIFT:
            return None
        title_score = _similarity(title, data.get("trackName", ""))
        if title_score < self.MIN_TITLE_CONFIDENCE:
            return None
        lines = _parse_lrc(synced)
        if not lines:
            return None
        return LyricsTrack(
            title=data.get("trackName") or title,
            artist=data.get("artistName") or (artist or ""),
            duration=result_duration,
            lines=lines,
        )

    def _pick_best(self, results: List[Dict[str, Any]], title: str, artist: Optional[str], duration: Optional[int]) -> Optional[Dict[str, Any]]:
        best, best_score = None, 0.0
        for r in results:
            if not r.get("syncedLyrics"):
                continue
            r_duration = r.get("duration")
            if duration and r_duration and abs(duration - r_duration) > self.MAX_DURATION_DRIFT:
                continue
            title_score = _similarity(title, r.get("trackName", ""))
            artist_score = 0.0
            if artist:
                artist_score = _similarity(artist, r.get("artistName", ""))
            if artist:
                combined = 0.6 * title_score + 0.4 * artist_score
                if title_score > 0.7 and artist_score > 0.7:
                    combined += 0.1
            else:
                combined = title_score
            if combined > best_score:
                best_score, best = combined, r
        if best_score < self.MIN_TITLE_CONFIDENCE:
            return None
        return best

    async def fetch(self, title: str, artist: Optional[str], duration: Optional[int]) -> Optional[LyricsTrack]:
        timeout = aiohttp.ClientTimeout(total=8)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                params: Dict[str, str] = {"track_name": title}
                if artist:
                    params["artist_name"] = artist
                if duration:
                    params["duration"] = str(int(duration))
                try:
                    async with session.get(f"{self.BASE_URL}/get", params=params) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            track = self._validate(data, title, artist, duration)
                            if track:
                                return track
                except (aiohttp.ClientError, asyncio.TimeoutError):
                    pass
                search_params = {"track_name": title, "artist_name": artist or ""}
                async with session.get(f"{self.BASE_URL}/search", params=search_params) as resp:
                    if resp.status != 200:
                        return None
                    results = await resp.json()
                    if not isinstance(results, list):
                        return None
                    best = self._pick_best(results, title, artist, duration)
                    if not best:
                        return None
                    return self._validate(best, title, artist, duration)
        except asyncio.TimeoutError:
            logger.warning("[KARAOKE] LRCLIB request timed out")
        except aiohttp.ClientError as e:
            logger.warning(f"[KARAOKE] LRCLIB request failed: {e}")
        except Exception as e:
            logger.warning(f"[KARAOKE] LRCLIB unexpected error: {e}")
        return None