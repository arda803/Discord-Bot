import os
import asyncio
import logging
from typing import Optional, Dict, Any, List
import aiohttp
import yt_dlp as youtube_dl
from exceptions import SpotifyError
from config import YTDLP_OPTIONS, SPOTIFY_PLAYLIST_TRACK_LIMIT

logger = logging.getLogger(__name__)

class SpotifyResolver:
    def __init__(self):
        self.client_id = os.getenv("SPOTIFY_CLIENT_ID")
        self.client_secret = os.getenv("SPOTIFY_CLIENT_SECRET")
        self.token: Optional[str] = None
        self.token_expires: float = 0

    async def _get_token(self) -> str:
        if not self.client_id or not self.client_secret:
            raise SpotifyError("Spotify Client ID/Secret ayarlanmamış.")
        loop = asyncio.get_running_loop()
        if self.token and loop.time() < self.token_expires:
            return self.token
        auth = aiohttp.BasicAuth(self.client_id, self.client_secret)
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://accounts.spotify.com/api/token",
                data={"grant_type": "client_credentials"},
                auth=auth
            ) as resp:
                if resp.status != 200:
                    raise SpotifyError("Spotify token alınamadı. Client ID/Secret kontrol edin.")
                data = await resp.json()
                self.token = data["access_token"]
                self.token_expires = loop.time() + data["expires_in"] - 60
                return self.token

    async def _api_request(self, endpoint: str) -> Dict[str, Any]:
        token = await self._get_token()
        headers = {"Authorization": f"Bearer {token}"}
        async with aiohttp.ClientSession() as session:
            async with session.get(f"https://api.spotify.com/v1/{endpoint}", headers=headers) as resp:
                if resp.status != 200:
                    raise SpotifyError(f"Spotify API hatası: {resp.status}")
                return await resp.json()

    async def get_track(self, track_id: str) -> Dict[str, Any]:
        return await self._api_request(f"tracks/{track_id}")

    async def get_playlist_tracks(self, playlist_id: str, limit: int = SPOTIFY_PLAYLIST_TRACK_LIMIT) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        token = await self._get_token()
        headers = {"Authorization": f"Bearer {token}"}
        url: Optional[str] = (
            f"https://api.spotify.com/v1/playlists/{playlist_id}/tracks"
            f"?limit=100&fields=items(track(name,artists(name))),next"
        )
        async with aiohttp.ClientSession() as session:
            while url and len(items) < limit:
                async with session.get(url, headers=headers) as resp:
                    if resp.status != 200:
                        raise SpotifyError(f"Spotify API hatası: {resp.status}")
                    data = await resp.json()
                items.extend(data.get("items", []))
                url = data.get("next")
        return items[:limit]

    async def search_youtube_for_track(self, track_name: str, artist: str) -> Optional[str]:
        query = f"{track_name} {artist} audio"
        ytdl = youtube_dl.YoutubeDL(YTDLP_OPTIONS)
        try:
            loop = asyncio.get_running_loop()
            data = await loop.run_in_executor(None, lambda: ytdl.extract_info(f"ytsearch1:{query}", download=False))
            if data and "entries" in data and data["entries"]:
                return data["entries"][0]["webpage_url"]
        except Exception as e:
            logger.warning(f"YouTube arama hatası: {e}")
        return None