from __future__ import annotations

# bot.py - Geliştirilmiş, kararlı sürüm

"""
Discord Music Bot — Production-Ready with Slash Commands, UI, Spotify, and Playlists
====================================================================================
- Tamamen eğik çizgi (slash) komutları ile kontrol
- Etkileşimli butonlar (Pause, Resume, Skip, Stop & Clear)
- Gelişmiş seek arayüzü (Modal veya /seek komutu ile saniye girişi)
- Kullanıcı playlistleri (SQLite ile yerel veritabanı)
- YouTube playlistlerinden 500 parçaya kadar destek
- Spotify track/playlist desteği (Spotify Web API + YouTube arama, sayfalama destekli)
- Favori şarkılar sistemi
- Güçlü hata toleransı (erişilemeyen videolar otomatik atlanır)
- Geliştirilmiş döngü modları (tek parça / tüm sıra)
- Playlist'ten şarkı silme
- Bot presence (çalan şarkı gösterimi)
- Daha iyi ses kalitesi
"""

import asyncio
import logging
import random
import os
import re
import time
import sys
from pathlib import Path
from typing import Optional, Dict, Any, Deque, List
from dataclasses import dataclass, field
from collections import deque

import discord
from discord import app_commands
from discord.ext import commands, tasks

import yt_dlp as youtube_dl
import aiohttp
import aiosqlite
from dotenv import load_dotenv


# ─── Environment & Logging ───────────────────────────────────────────────────

env_path = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=env_path)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

youtube_dl.utils.bug_reports_message = lambda *args, **kwargs: ""


# ─── Constants ───────────────────────────────────────────────────────────────

class Colors:
    PRIMARY = discord.Color.from_rgb(86, 119, 252)
    SUCCESS = discord.Color.from_rgb(72, 187, 120)
    ERROR = discord.Color.from_rgb(235, 87, 87)
    WARNING = discord.Color.from_rgb(86, 165, 255)
    PLAYING = discord.Color.from_rgb(121, 96, 206)
    INFO = discord.Color.from_rgb(123, 142, 169)


YTDLP_OPTIONS: Dict[str, Any] = {
    "format": "bestaudio/best",
    "restrictfilenames": True,
    "noplaylist": True,
    "nocheckcertificate": True,
    "ignoreerrors": False,
    "logtostderr": False,
    "quiet": True,
    "no_warnings": True,
    "default_search": "auto",
    "source_address": "0.0.0.0",
}

YTDLP_PLAYLIST_OPTIONS: Dict[str, Any] = {
    "format": "bestaudio/best",
    "restrictfilenames": True,
    "noplaylist": False,
    "nocheckcertificate": True,
    "ignoreerrors": True,
    "logtostderr": False,
    "quiet": True,
    "no_warnings": True,
    "source_address": "0.0.0.0",
    "extract_flat": False,
    "playlist_items": "1:500",
}

SPOTIFY_PLAYLIST_TRACK_LIMIT = 200

FFMPEG_BEFORE_OPTIONS = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
FFMPEG_OPTIONS = "-vn"


# ─── Data Models ─────────────────────────────────────────────────────────────

@dataclass
class Song:
    source_url: str
    title: str
    url: str
    duration: Optional[int] = None
    thumbnail: Optional[str] = None
    uploader: Optional[str] = None
    requester: Optional[discord.abc.User] = None
    spotify_id: Optional[str] = None


@dataclass
class Playlist:
    id: int
    name: str
    owner_id: int
    songs: List[Song] = field(default_factory=list)


class GuildMusicState:
    def __init__(self) -> None:
        self.queue: Deque[Song] = deque()
        self.lock = asyncio.Lock()
        self.loop_current: bool = False      # Tek parça döngüsü
        self.loop_queue: bool = False        # Tüm sıra döngüsü
        self.current_song: Optional[Song] = None
        self.volume: float = 0.5
        self.is_seeking: bool = False
        self.seek_position: Optional[int] = None
        self.now_playing_message: Optional[discord.Message] = None
        self.active: bool = False
        self.played_any: bool = False
        self.failed_count: int = 0
        self.started_at: float = 0.0


# ─── Exceptions ──────────────────────────────────────────────────────────────

class VoiceError(commands.CommandError):
    pass


class YTDLError(commands.CommandError):
    pass


class SpotifyError(commands.CommandError):
    pass


class AudioSourceError(commands.CommandError):
    pass


# ─── Database Manager (SQLite) ──────────────────────────────────────────────

class DatabaseManager:
    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or str(Path(__file__).with_name("music_bot.db"))

    async def init(self) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA foreign_keys = ON")
            await db.execute("""
                CREATE TABLE IF NOT EXISTS user_playlists (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    owner_id INTEGER NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS playlist_songs (
                    playlist_id INTEGER NOT NULL,
                    song_title TEXT NOT NULL,
                    song_url TEXT NOT NULL,
                    song_duration INTEGER,
                    song_thumbnail TEXT,
                    song_uploader TEXT,
                    position INTEGER NOT NULL,
                    FOREIGN KEY(playlist_id) REFERENCES user_playlists(id) ON DELETE CASCADE
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS favorites (
                    user_id INTEGER NOT NULL,
                    song_title TEXT NOT NULL,
                    song_url TEXT NOT NULL,
                    song_duration INTEGER,
                    song_thumbnail TEXT,
                    song_uploader TEXT,
                    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (user_id, song_url)
                )
            """)
            await db.commit()

    async def create_playlist(self, name: str, owner_id: int) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "INSERT INTO user_playlists (name, owner_id) VALUES (?, ?)",
                (name, owner_id)
            )
            await db.commit()
            return cursor.lastrowid

    async def get_playlists(self, owner_id: int) -> List[Dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT id, name FROM user_playlists WHERE owner_id = ? ORDER BY created_at DESC",
                (owner_id,)
            ) as cursor:
                rows = await cursor.fetchall()
                return [{"id": row[0], "name": row[1]} for row in rows]

    async def add_song_to_playlist(self, playlist_id: int, song: Song, position: int) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT INTO playlist_songs
                   (playlist_id, song_title, song_url, song_duration, song_thumbnail, song_uploader, position)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (playlist_id, song.title, song.url, song.duration, song.thumbnail, song.uploader, position)
            )
            await db.commit()

    async def get_playlist_songs(self, playlist_id: int) -> List[Song]:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT song_title, song_url, song_duration, song_thumbnail, song_uploader "
                "FROM playlist_songs WHERE playlist_id = ? ORDER BY position",
                (playlist_id,)
            ) as cursor:
                rows = await cursor.fetchall()
                songs = []
                for row in rows:
                    songs.append(Song(
                        source_url="",
                        title=row[0],
                        url=row[1],
                        duration=row[2],
                        thumbnail=row[3],
                        uploader=row[4],
                        requester=None
                    ))
                return songs

    async def delete_playlist(self, playlist_id: int, owner_id: int) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT id FROM user_playlists WHERE id = ? AND owner_id = ?",
                (playlist_id, owner_id)
            ) as cursor:
                if not await cursor.fetchone():
                    return False
            await db.execute("DELETE FROM playlist_songs WHERE playlist_id = ?", (playlist_id,))
            await db.execute("DELETE FROM user_playlists WHERE id = ?", (playlist_id,))
            await db.commit()
            return True

    async def remove_song_from_playlist(self, playlist_id: int, position: int) -> bool:
        """Playlist'ten belirli bir pozisyondaki şarkıyı siler ve sonraki pozisyonları günceller."""
        async with aiosqlite.connect(self.db_path) as db:
            # Önce varlığını kontrol et
            cursor = await db.execute(
                "SELECT 1 FROM playlist_songs WHERE playlist_id = ? AND position = ?",
                (playlist_id, position)
            )
            if not await cursor.fetchone():
                return False
            await db.execute(
                "DELETE FROM playlist_songs WHERE playlist_id = ? AND position = ?",
                (playlist_id, position)
            )
            await db.execute(
                "UPDATE playlist_songs SET position = position - 1 WHERE playlist_id = ? AND position > ?",
                (playlist_id, position)
            )
            await db.commit()
            return True

    async def add_favorite(self, user_id: int, song: Song) -> bool:
        try:
            async with aiosqlite.connect(self.db_path) as db:
                cursor = await db.execute(
                    """INSERT OR IGNORE INTO favorites
                       (user_id, song_title, song_url, song_duration, song_thumbnail, song_uploader)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (user_id, song.title, song.url, song.duration, song.thumbnail, song.uploader)
                )
                await db.commit()
                return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"Favorite ekleme hatası: {e}")
            return False

    async def remove_favorite(self, user_id: int, song_url: str) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "DELETE FROM favorites WHERE user_id = ? AND song_url = ?",
                (user_id, song_url)
            )
            await db.commit()
            return cursor.rowcount > 0

    async def get_favorites(self, user_id: int) -> List[Song]:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT song_title, song_url, song_duration, song_thumbnail, song_uploader "
                "FROM favorites WHERE user_id = ? ORDER BY added_at DESC",
                (user_id,)
            ) as cursor:
                rows = await cursor.fetchall()
                songs = []
                for row in rows:
                    songs.append(Song(
                        source_url="",
                        title=row[0],
                        url=row[1],
                        duration=row[2],
                        thumbnail=row[3],
                        uploader=row[4],
                        requester=None
                    ))
                return songs


# ─── Spotify Helper ─────────────────────────────────────────────────────

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


# ─── Helpers ─────────────────────────────────────────────────────────────────

def create_embed(title: str, description: str, color: discord.Color = Colors.PRIMARY) -> discord.Embed:
    return discord.Embed(title=title, description=description, color=color, timestamp=discord.utils.utcnow())

PlaybackContext = commands.Context | discord.Interaction

def get_voice_client(ctx: PlaybackContext) -> Optional[discord.VoiceClient]:
    return ctx.guild.voice_client if ctx.guild else None


def format_duration(seconds: Optional[int]) -> str:
    if seconds is None or seconds < 0:
        return "Bilinmiyor"
    minutes, sec = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{sec:02d}"
    return f"{minutes}:{sec:02d}"


async def extract_song_info(query: str, loop: asyncio.AbstractEventLoop) -> Dict[str, Any]:
    if not query.startswith(("http://", "https://", "www.", "youtu")):
        query = f"ytsearch1:{query}"
    ytdl = youtube_dl.YoutubeDL(YTDLP_OPTIONS)
    try:
        data = await loop.run_in_executor(None, lambda: ytdl.extract_info(query, download=False))
    except Exception as exc:
        raise YTDLError(f"Arama başarısız: {exc}")
    if data is None:
        raise YTDLError("İçerik bulunamadı.")
    if "entries" in data:
        entries = [e for e in data["entries"] if e] if data["entries"] else []
        if not entries:
            raise YTDLError("Arama sonucu bulunamadı.")
        data = entries[0]
    return data


async def extract_playlist_info(url: str, loop: asyncio.AbstractEventLoop) -> Dict[str, Any]:
    ytdl = youtube_dl.YoutubeDL(YTDLP_PLAYLIST_OPTIONS)
    try:
        data = await loop.run_in_executor(None, lambda: ytdl.extract_info(url, download=False))
    except Exception as exc:
        raise YTDLError(f"Playlist çekilemedi: {exc}")
    if data is None:
        raise YTDLError("Playlist bilgisi alınamadı.")
    return data


async def resolve_song_url(webpage_url: str, loop: asyncio.AbstractEventLoop) -> str:
    ytdl = youtube_dl.YoutubeDL(YTDLP_OPTIONS)
    try:
        data = await loop.run_in_executor(None, lambda: ytdl.extract_info(webpage_url, download=False))
    except Exception as exc:
        raise YTDLError(f"Stream URL çözümlenemedi: {exc}")
    if data is None:
        raise YTDLError("Stream verisi alınamadı.")
    stream_url = data.get("url")
    if not stream_url:
        raise YTDLError("Stream URL boş veya geçersiz (video erişilemez/özel olabilir).")
    return stream_url


def build_song(data: Dict[str, Any], requester: Optional[discord.abc.User]) -> Song:
    return Song(
        source_url=data.get("url", "") or "",
        title=data.get("title", "Bilinmeyen Başlık"),
        url=data.get("webpage_url", ""),
        duration=data.get("duration"),
        thumbnail=data.get("thumbnail"),
        uploader=data.get("uploader"),
        requester=requester,
    )


def build_song_from_flat(entry: Dict[str, Any], requester: Optional[discord.abc.User]) -> Song:
    webpage_url = entry.get("url") or entry.get("webpage_url", "")
    if webpage_url and not webpage_url.startswith("http"):
        webpage_url = f"https://www.youtube.com/watch?v={webpage_url}"
    return Song(
        source_url="",
        title=entry.get("title", "Bilinmeyen Başlık"),
        url=webpage_url,
        duration=entry.get("duration"),
        thumbnail=entry.get("thumbnail"),
        uploader=entry.get("uploader") or entry.get("channel"),
        requester=requester,
    )


def build_song_from_playlist_entry(entry: Dict[str, Any], requester: Optional[discord.abc.User]) -> Song:
    if entry.get("webpage_url"):
        return build_song(entry, requester)
    return build_song_from_flat(entry, requester)


def is_ffmpeg_missing_error(exc: Exception) -> bool:
    if isinstance(exc, FileNotFoundError):
        return True
    text = str(exc).lower()
    return "ffmpeg" in text and ("not found" in text or "no such file" in text or "bulunamadı" in text)


# ─── UI Views ────────────────────────────────────────────────────────────

class SeekModal(discord.ui.Modal, title="Şarkıda Sarma"):
    seconds = discord.ui.TextInput(label="Saniye", placeholder="Örn: 120", required=True)

    def __init__(self, cog: "Music", ctx: PlaybackContext):
        super().__init__()
        self.cog = cog
        self.ctx = ctx

    async def on_submit(self, interaction: discord.Interaction):
        try:
            seconds = int(self.seconds.value)
            await self.cog.perform_seek(self.ctx, seconds)
            await interaction.response.send_message(
                f"⏩ Şarkı {format_duration(seconds)} noktasına sarıldı.", ephemeral=True
            )
        except ValueError:
            await interaction.response.send_message("Lütfen geçerli bir saniye girin.", ephemeral=True)
        except (VoiceError,) as e:
            await interaction.response.send_message(f"Hata: {e}", ephemeral=True)
        except Exception as e:
            logger.error(f"Seek modal hatası: {e}")
            await interaction.response.send_message(f"Hata: {e}", ephemeral=True)


class MusicControlView(discord.ui.View):
    def __init__(self, cog: "Music", ctx: PlaybackContext):
        super().__init__(timeout=None)
        self.cog = cog
        self.ctx = ctx

    @discord.ui.button(label="Duraklat", style=discord.ButtonStyle.primary, emoji="⏸️")
    async def pause_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = get_voice_client(self.ctx)
        if not vc or not vc.is_playing():
            await interaction.response.send_message("Şu anda çalan bir şarkı yok.", ephemeral=True)
            return
        vc.pause()
        await interaction.response.send_message("⏸️ Duraklatıldı.", ephemeral=True)

    @discord.ui.button(label="Devam Et", style=discord.ButtonStyle.success, emoji="▶️")
    async def resume_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = get_voice_client(self.ctx)
        if not vc or not vc.is_paused():
            await interaction.response.send_message("Duraklatılmış bir şarkı yok.", ephemeral=True)
            return
        vc.resume()
        await interaction.response.send_message("▶️ Devam ediyor.", ephemeral=True)

    @discord.ui.button(label="Geç", style=discord.ButtonStyle.secondary, emoji="⏭️")
    async def skip_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = get_voice_client(self.ctx)
        if not vc or not vc.is_playing():
            await interaction.response.send_message("Şu anda çalan bir şarkı yok.", ephemeral=True)
            return
        vc.stop()
        await interaction.response.send_message("⏭️ Şarkı atlandı.", ephemeral=True)

    @discord.ui.button(label="Durdur & Temizle", style=discord.ButtonStyle.danger, emoji="⏹️")
    async def stop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = get_voice_client(self.ctx)
        if not vc:
            await interaction.response.send_message("Bot zaten ses kanalında değil.", ephemeral=True)
            return
        self.cog._clear_state(self.ctx.guild.id)
        vc.stop()
        await vc.disconnect()
        await interaction.response.send_message("⏹️ Sıra temizlendi ve kanaldan ayrıldım.", ephemeral=True)

    @discord.ui.button(label="Sarma", style=discord.ButtonStyle.primary, emoji="⏩")
    async def seek_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = SeekModal(self.cog, self.ctx)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Favoriye Ekle", style=discord.ButtonStyle.success, emoji="❤️")
    async def favorite_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = self.cog._get_state(self.ctx.guild.id)
        if not state.current_song:
            await interaction.response.send_message("Şu anda çalan şarkı yok.", ephemeral=True)
            return
        song = state.current_song
        db = self.cog.db
        success = await db.add_favorite(interaction.user.id, song)
        if success:
            await interaction.response.send_message(f"❤️ `{song.title}` favorilerinize eklendi!", ephemeral=True)
        else:
            await interaction.response.send_message("Bu şarkı zaten favorilerinizde.", ephemeral=True)


# ─── Music Cog with Slash Commands ───────────────────────────────────────

class Music(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._states: Dict[int, GuildMusicState] = {}
        self.db = DatabaseManager()
        has_spotify_creds = bool(os.getenv("SPOTIFY_CLIENT_ID")) and bool(os.getenv("SPOTIFY_CLIENT_SECRET"))
        self.spotify = SpotifyResolver() if has_spotify_creds else None
        if not has_spotify_creds:
            logger.info("Spotify kimlik bilgileri bulunamadı, Spotify desteği devre dışı.")
        self.ffmpeg_executable = os.getenv("FFMPEG_EXECUTABLE") or "ffmpeg"

        # Discord profilindeki durum kartları arasında dönen sayaç.
        self._presence_index: int = 0
        self._presence_task: Optional[asyncio.Task] = None

    async def cog_load(self) -> None:
        await self.db.init()
        self._presence_task = asyncio.create_task(self._presence_loop())

    def cog_unload(self) -> None:
        if self._presence_task:
            self._presence_task.cancel()

    def _get_presence_stats(self) -> Dict[str, Any]:
        total_guilds = len(self.bot.guilds)
        active_guilds = 0
        listeners = 0
        active_song: Optional[Song] = None
        active_state: Optional[GuildMusicState] = None

        for guild in self.bot.guilds:
            state = self._states.get(guild.id)
            vc = guild.voice_client

            is_active = bool(
                vc
                and vc.channel
                and state
                and state.current_song
                and (vc.is_playing() or vc.is_paused())
            )

            if is_active:
                active_guilds += 1

                # Profilde göstermek için aktif şarkılardan birini seç.
                if active_song is None and state:
                    active_song = state.current_song
                    active_state = state

                # Botların kendisini saymadan kanaldaki gerçek dinleyicileri say.
                listeners += sum(
                    1 for member in vc.channel.members
                    if not member.bot
                )

        return {
            "total_guilds": total_guilds,
            "active_guilds": active_guilds,
            "listeners": listeners,
            "song": active_song,
            "state": active_state,
        }

    async def _presence_loop(self) -> None:
        await self.bot.wait_until_ready()

        while not self.bot.is_closed():
            try:
                stats = self._get_presence_stats()

                # Kart 1: Sunucu/aktiflik bilgileri
                # Kart 2: Çalan şarkı + ses + sıra bilgileri
                if self._presence_index % 2 == 0:
                    text = (
                        f"📊 {stats['total_guilds']} sunucu • "
                        f"🟢 {stats['active_guilds']} aktif • "
                        f"👥 {stats['listeners']} dinleyici"
                    )
                else:
                    song = stats["song"]
                    state = stats["state"]

                    if song and state:
                        volume = round(state.volume * 100)
                        queue_count = len(state.queue)
                        text = (
                            f"🎧 {song.title} • "
                            f"🔊 %{volume} • "
                            f"📋 {queue_count} sırada"
                        )
                    else:
                        text = "🎵 Müzik bekliyorum • /play ile başlat"

                # Discord activity name alanı sınırlı olduğu için uzun başlıkları kırp.
                text = text[:120]

                await self.bot.change_presence(
                    activity=discord.Activity(
                        type=discord.ActivityType.listening,
                        name=text,
                    )
                )

                self._presence_index += 1

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"Presence güncelleme hatası: {e}")

            # Kart değişim süresi.
            await asyncio.sleep(12)

    def _get_state(self, guild_id: int) -> GuildMusicState:
        if guild_id not in self._states:
            self._states[guild_id] = GuildMusicState()
        return self._states[guild_id]

    def _delete_message_later(self, message: Optional[discord.Message]) -> None:
        if message is None:
            return

        async def _delete() -> None:
            try:
                await message.delete()
            except (discord.HTTPException, discord.NotFound, discord.Forbidden):
                pass

        asyncio.create_task(_delete())

    def _clear_state(self, guild_id: int) -> None:
        state = self._states.get(guild_id)
        if state:
            state.queue.clear()
            state.current_song = None
            state.loop_current = False
            state.loop_queue = False
            state.is_seeking = False
            state.seek_position = None
            state.active = False
            state.played_any = False
            state.failed_count = 0
            state.started_at = 0.0
            self._delete_message_later(state.now_playing_message)
            state.now_playing_message = None

    async def _maybe_start_playback(self, ctx: PlaybackContext) -> None:
        vc = get_voice_client(ctx)
        if not vc:
            return
        state = self._get_state(ctx.guild.id)
        async with state.lock:
            if state.active or vc.is_playing() or vc.is_paused():
                return
            if not state.queue:
                return
            next_song = state.queue.popleft()
            # Yeni bir oynatma turu başlıyor.
            state.active = True
            state.played_any = False
            state.failed_count = 0
            state.started_at = 0.0
        await self._play_song(ctx, next_song)

    async def _handle_after_playing(self, ctx: PlaybackContext, error: Optional[Exception]) -> None:
        state = self._get_state(ctx.guild.id)
        if error:
            state.failed_count += 1
            logger.error(f"Playback error: {error}")
            embed = create_embed(
                "Oynatma Hatası",
                f"Bu şarkının ses akışı başlatılamadı, sıradakine geçiliyor...\n`{error}`",
                Colors.ERROR,
            )
            try:
                await ctx.channel.send(embed=embed)
            except Exception as e:
                logger.error(f"Failed to send error embed: {e}")

        song_to_play: Optional[Song] = None
        seek_pos: Optional[int] = None
        queue_finished = False

        async with state.lock:
            if state.is_seeking:
                state.is_seeking = False
                seek_pos = state.seek_position
                state.seek_position = None
                song_to_play = state.current_song
            elif state.loop_current and state.current_song:
                song_to_play = state.current_song
            elif state.loop_queue and state.current_song:
                # Tüm sıra döngüsü: mevcut şarkıyı kuyruğun sonuna ekle, sıradakini al
                if state.queue:
                    state.queue.append(state.current_song)
                    song_to_play = state.queue.popleft()
                else:
                    # Kuyruk boşsa, aynı şarkıyı tekrar çal (loop_current gibi)
                    song_to_play = state.current_song
            elif state.queue:
                song_to_play = state.queue.popleft()
            else:
                state.current_song = None
                state.active = False
                queue_finished = True

        if song_to_play is not None:
            await self._play_song(ctx, song_to_play, seek=seek_pos)
        elif queue_finished:
            if state.played_any:
                title = "Sıra Bitti"
                description = "Tüm şarkılar çalındı. Yeni şarkılar ekleyebilirsiniz."
                color = Colors.INFO
            else:
                title = "Oynatma Başarısız"
                description = "Şarkılar ses akışı başlatılamadan bitti. FFmpeg ve yt-dlp çıktısını kontrol edin."
                color = Colors.ERROR
            embed = create_embed(title, description, color)
            try:
                await ctx.channel.send(embed=embed)
            except Exception as e:
                logger.error(f"Failed to send queue status embed: {e}")

    async def _play_song(self, ctx: PlaybackContext, song: Song, seek: Optional[int] = None) -> None:
        vc = get_voice_client(ctx)
        if not vc:
            state = self._get_state(ctx.guild.id)
            state.active = False
            raise VoiceError("Bot ses kanalında değil.")

        state = self._get_state(ctx.guild.id)

        # Önceki 'şimdi çalıyor' mesajını temizle
        if state.now_playing_message:
            self._delete_message_later(state.now_playing_message)
            state.now_playing_message = None

        try:
            # YouTube/Spotify stream URL'leri kısa süreli olabildiği için her şarkı
            # başlatılırken akışı yeniden çözüyoruz. Eski imzalı URL'nin hemen bitmesini önler.
            try:
                song.source_url = await resolve_song_url(song.url, asyncio.get_running_loop())
            except YTDLError as exc:
                    logger.warning(f"URL resolve failed for '{song.title}': {exc}")
                    embed = create_embed(
                        "Atlandı", f"**{song.title}** oynatılamadı, sıradakine geçiliyor.\n`{exc}`", Colors.WARNING
                    )
                    await ctx.channel.send(embed=embed)
                    await self._handle_after_playing(ctx, None)
                    return

            # FFmpeg ses kaynağını oluştur
            before_options = FFMPEG_BEFORE_OPTIONS
            if seek is not None and seek > 0:
                # -ss ile önceki seçenek arasında boşluk zorunlu.
                before_options += f" -ss {int(seek)}"
            try:
                source = discord.FFmpegPCMAudio(
                    song.source_url,
                    executable=self.ffmpeg_executable,
                    before_options=before_options,
                    options=FFMPEG_OPTIONS,
                    stderr=sys.stderr,  # FFmpeg hataları konsolda görünür
                )
                source = discord.PCMVolumeTransformer(source, volume=state.volume)
            except Exception as e:
                logger.error(f"FFmpeg source creation failed: {e}")
                if is_ffmpeg_missing_error(e):
                    msg = (
                        "FFmpeg bulunamadı. FFmpeg'in kurulu olduğundan ve PATH'e ekli olduğundan, "
                        "veya .env dosyasındaki FFMPEG_EXECUTABLE değişkeninin doğru olduğundan emin olun."
                    )
                else:
                    msg = f"Ses kaynağı oluşturulamadı: {e}"
                embed = create_embed("Oynatma Hatası", msg, Colors.ERROR)
                await ctx.channel.send(embed=embed)
                await self._handle_after_playing(ctx, None)
                return

            state.current_song = song

            def after_callback(error: Optional[Exception]) -> None:
                elapsed = time.monotonic() - state.started_at if state.started_at else 0.0
                # FFmpeg bazen bağlantı/stream hatasında exception döndürmeden hemen kapanır.
                # Böyle bir durumda bunu normal şarkı sonu gibi saymıyoruz.
                if error is None and elapsed < 2.0:
                    error = AudioSourceError(
                        f"Ses akışı çok erken kapandı ({elapsed:.1f} sn). FFmpeg/yt-dlp stream hatası olabilir."
                    )
                if error:
                    logger.error(f"Playback error in after_callback: {error}")
                else:
                    state.played_any = True
                    logger.info(f"Playback completed normally after {elapsed:.1f} seconds.")
                coro = self._handle_after_playing(ctx, error)
                asyncio.run_coroutine_threadsafe(coro, self.bot.loop)

            if vc.is_playing() or vc.is_paused():
                vc.stop()
                await asyncio.sleep(0.2)

            try:
                state.started_at = time.monotonic()
                vc.play(source, after=after_callback)
            except Exception as e:
                logger.error(f"vc.play failed: {e}")
                embed = create_embed("Oynatma Hatası", f"Şarkı başlatılamadı: {e}", Colors.ERROR)
                await ctx.channel.send(embed=embed)
                await self._handle_after_playing(ctx, None)
                return

            # Şimdi çalıyor embed'ini gönder
            embed = discord.Embed(title="▶️ Şimdi Çalıyor", description=f"[{song.title}]({song.url})", color=Colors.PLAYING)
            if song.thumbnail:
                embed.set_image(url=song.thumbnail)
            if song.duration:
                embed.add_field(name="⏱️ Süre", value=format_duration(song.duration), inline=True)
            if song.uploader:
                embed.add_field(name="📺 Kanal", value=song.uploader, inline=True)
            embed.add_field(name="👤 İsteyen", value=song.requester.mention if song.requester else "Bilinmiyor", inline=True)
            if state.loop_current:
                embed.set_footer(text="🔂 Tek parça döngüsü aktif")
            elif state.loop_queue:
                embed.set_footer(text="🔁 Tüm sıra döngüsü aktif")

            view = MusicControlView(self, ctx)
            msg = await ctx.channel.send(embed=embed, view=view)
            state.now_playing_message = msg
            logger.info(f"Playing '{song.title}' in guild {ctx.guild.id}")

        except Exception as exc:
            logger.error(f"Unexpected error in _play_song: {exc}")
            embed = create_embed("Oynatma Hatası", f"Beklenmeyen hata: {exc}", Colors.ERROR)
            try:
                await ctx.channel.send(embed=embed)
            except Exception:
                pass
            await self._handle_after_playing(ctx, None)

    async def perform_seek(self, ctx: PlaybackContext, seconds: int) -> None:
        vc = get_voice_client(ctx)
        if not vc or not (vc.is_playing() or vc.is_paused()):
            raise VoiceError("Şu anda çalan bir şarkı yok.")
        state = self._get_state(ctx.guild.id)
        if not state.current_song:
            raise VoiceError("Mevcut şarkı bilgisi yok.")
        if seconds < 0:
            raise ValueError("Saniye negatif olamaz.")
        if state.current_song.duration and seconds > state.current_song.duration:
            raise ValueError(f"Süre aşıldı (maks: {format_duration(state.current_song.duration)})")
        async with state.lock:
            state.seek_position = seconds
            state.is_seeking = True
            vc.stop()

    async def _resolve_spotify(self, url: str) -> List[Song]:
        if not self.spotify:
            raise SpotifyError("Spotify entegrasyonu için Client ID/Secret ayarlanmamış.")
        match_track = re.search(r"track/([a-zA-Z0-9]+)", url)
        match_playlist = re.search(r"playlist/([a-zA-Z0-9]+)", url)
        songs: List[Song] = []
        if match_track:
            track_id = match_track.group(1)
            data = await self.spotify.get_track(track_id)
            track_name = data["name"]
            artist = data["artists"][0]["name"] if data.get("artists") else "Bilinmiyor"
            youtube_url = await self.spotify.search_youtube_for_track(track_name, artist)
            if youtube_url:
                song_info = await extract_song_info(youtube_url, asyncio.get_running_loop())
                song = build_song(song_info, None)
                songs.append(song)
            else:
                raise SpotifyError(f"YouTube'da bulunamadı: {track_name} - {artist}")
        elif match_playlist:
            playlist_id = match_playlist.group(1)
            items = await self.spotify.get_playlist_tracks(playlist_id)
            for item in items:
                track = item.get("track")
                if not track or not track.get("name"):
                    continue
                track_name = track["name"]
                artists = track.get("artists") or []
                artist = artists[0]["name"] if artists else "Bilinmiyor"
                youtube_url = await self.spotify.search_youtube_for_track(track_name, artist)
                if youtube_url:
                    try:
                        song_info = await extract_song_info(youtube_url, asyncio.get_running_loop())
                        song = build_song(song_info, None)
                        songs.append(song)
                    except Exception as e:
                        logger.warning(f"Spotify playlist track atlandı: {e}")
                await asyncio.sleep(0.05)
        else:
            raise SpotifyError("Geçersiz Spotify URL'si.")
        return songs

    async def _ensure_voice(self, interaction: discord.Interaction) -> bool:
        if interaction.guild.voice_client:
            return True
        if not interaction.user.voice or not interaction.user.voice.channel:
            return False
        channel = interaction.user.voice.channel
        perms = channel.permissions_for(interaction.guild.me or interaction.guild.get_member(self.bot.user.id))
        if not perms.connect or not perms.speak:
            return False
        try:
            await channel.connect()
        except Exception as e:
            logger.error(f"Ses kanalına bağlanılamadı: {e}")
            return False
        return True

    async def _play_or_queue(self, interaction: discord.Interaction, song: Song) -> None:
        if not await self._ensure_voice(interaction):
            raise VoiceError("Ses kanalına bağlanılamadı. Lütfen bir ses kanalında olduğunuzdan emin olun.")
        state = self._get_state(interaction.guild.id)
        async with state.lock:
            state.queue.append(song)
        await self._maybe_start_playback(interaction)

    # ─── Slash Commands ──────────────────────────────────────────────────

    @app_commands.command(name="join", description="Ses kanalına katıl.")
    async def slash_join(self, interaction: discord.Interaction):
        if not interaction.user.voice or not interaction.user.voice.channel:
            embed = create_embed("Hata", "Bir ses kanalında değilsin.", Colors.ERROR)
            await interaction.response.send_message(embed=embed)
            return
        channel = interaction.user.voice.channel
        perms = channel.permissions_for(interaction.guild.me or interaction.guild.get_member(self.bot.user.id))
        if not perms.connect or not perms.speak:
            embed = create_embed("Hata", "Bu kanala bağlanma/konuşma iznim yok.", Colors.ERROR)
            await interaction.response.send_message(embed=embed)
            return
        try:
            if interaction.guild.voice_client:
                await interaction.guild.voice_client.move_to(channel)
            else:
                await channel.connect()
        except Exception as e:
            embed = create_embed("Hata", f"Kanala bağlanılamadı: {e}", Colors.ERROR)
            await interaction.response.send_message(embed=embed)
            return
        embed = create_embed("Katılındı", f"{channel.mention} kanalına katıldım!", Colors.SUCCESS)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="leave", description="Ses kanalından ayrıl ve sırayı temizle.")
    async def slash_leave(self, interaction: discord.Interaction):
        if not interaction.guild.voice_client:
            embed = create_embed("Hata", "Bot herhangi bir ses kanalında değil.", Colors.ERROR)
            await interaction.response.send_message(embed=embed)
            return
        self._clear_state(interaction.guild.id)
        await interaction.guild.voice_client.disconnect()
        embed = create_embed("Ayrılındı", "Ses kanalından ayrıldım.", Colors.WARNING)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="play", description="Bir şarkıyı sıraya ekle (YouTube veya Spotify).")
    async def slash_play(self, interaction: discord.Interaction, query: str):
        await interaction.response.defer()
        try:
            if "spotify.com" in query:
                if not self.spotify:
                    raise SpotifyError("Spotify desteği aktif değil. Client ID/Secret ayarlayın.")
                songs = await self._resolve_spotify(query)
                if not songs:
                    raise SpotifyError("Spotify'dan hiç şarkı bulunamadı.")
                for song in songs:
                    song.requester = interaction.user
                    await self._play_or_queue(interaction, song)
                embed = create_embed("Spotify Eklendi", f"{len(songs)} şarkı sıraya eklendi.", Colors.SUCCESS)
                await interaction.followup.send(embed=embed)
            else:
                data = await extract_song_info(query, asyncio.get_running_loop())
                song = build_song(data, interaction.user)
                await self._play_or_queue(interaction, song)
                embed = discord.Embed(title="📥 Sıraya Eklendi", description=f"[{song.title}]({song.url})", color=Colors.SUCCESS)
                if song.thumbnail:
                    embed.set_thumbnail(url=song.thumbnail)
                if song.duration:
                    embed.add_field(name="⏱️ Süre", value=format_duration(song.duration), inline=True)
                embed.add_field(name="👤 İsteyen", value=interaction.user.mention, inline=True)
                await interaction.followup.send(embed=embed)
        except (YTDLError, SpotifyError, VoiceError) as e:
            embed = create_embed("Hata", str(e), Colors.ERROR)
            await interaction.followup.send(embed=embed)
        except Exception as e:
            logger.error(f"Play slash error: {e}")
            embed = create_embed("Hata", f"Bir sorun oluştu: {e}", Colors.ERROR)
            await interaction.followup.send(embed=embed)

    @app_commands.command(name="playlist", description="Bir YouTube playlist'ini sıraya ekle.")
    async def slash_playlist(self, interaction: discord.Interaction, url: str):
        await interaction.response.defer()
        if not await self._ensure_voice(interaction):
            embed = create_embed("Hata", "Ses kanalına bağlanılamadı. Lütfen bir ses kanalında olduğunuzdan emin olun.", Colors.ERROR)
            await interaction.followup.send(embed=embed)
            return
        state = self._get_state(interaction.guild.id)
        try:
            data = await extract_playlist_info(url, asyncio.get_running_loop())
            entries = [e for e in data.get("entries", []) if e]
            if not entries:
                raise YTDLError("Playlist boş veya geçersiz.")
            added = 0
            async with state.lock:
                for entry in entries:
                    try:
                        song = build_song_from_playlist_entry(entry, interaction.user)
                        if song.url:
                            state.queue.append(song)
                            added += 1
                    except Exception:
                        continue
            embed = discord.Embed(title="📋 Playlist Sıraya Eklendi", description=data.get("title", "Playlist"), color=Colors.SUCCESS)
            embed.add_field(name="🎵 Eklenen Şarkı", value=str(added), inline=True)
            embed.add_field(name="📌 Sıradaki Toplam", value=str(len(state.queue)), inline=True)
            await interaction.followup.send(embed=embed)

            await self._maybe_start_playback(interaction)
        except YTDLError as e:
            embed = create_embed("Playlist Hatası", str(e), Colors.ERROR)
            await interaction.followup.send(embed=embed)

    @app_commands.command(name="skip", description="Şu an çalan şarkıyı atla.")
    async def slash_skip(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if not vc or not vc.is_playing():
            embed = create_embed("Hata", "Çalan bir şarkı yok.", Colors.ERROR)
            await interaction.response.send_message(embed=embed)
            return
        vc.stop()
        embed = create_embed("Atlandı", "⏭️ Şarkı atlandı!", Colors.WARNING)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="pause", description="Şarkıyı duraklat.")
    async def slash_pause(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if not vc or not vc.is_playing():
            embed = create_embed("Hata", "Çalan bir şarkı yok.", Colors.ERROR)
            await interaction.response.send_message(embed=embed)
            return
        vc.pause()
        embed = create_embed("Duraklatıldı", "⏸️ Şarkı duraklatıldı.", Colors.WARNING)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="resume", description="Duraklatılmış şarkıyı devam ettir.")
    async def slash_resume(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if not vc or not vc.is_paused():
            embed = create_embed("Hata", "Duraklatılmış şarkı yok.", Colors.ERROR)
            await interaction.response.send_message(embed=embed)
            return
        vc.resume()
        embed = create_embed("Devam Ediyor", "▶️ Şarkı devam ediyor.", Colors.SUCCESS)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="stop", description="Durdur, sırayı temizle ve kanaldan ayrıl.")
    async def slash_stop(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if not vc:
            embed = create_embed("Hata", "Bot ses kanalında değil.", Colors.ERROR)
            await interaction.response.send_message(embed=embed)
            return
        self._clear_state(interaction.guild.id)
        vc.stop()
        await vc.disconnect()
        embed = create_embed("Durduruldu", "⏹️ Sıra temizlendi, durdurdum ve kanaldan ayrıldım.", Colors.ERROR)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="seek", description="Şarkıda belirtilen saniyeye sar.")
    async def slash_seek(self, interaction: discord.Interaction, seconds: int):
        try:
            await self.perform_seek(interaction, seconds)
            embed = create_embed("Sarıldı", f"⏩ Şarkı {format_duration(seconds)} noktasına sarıldı.", Colors.SUCCESS)
            await interaction.response.send_message(embed=embed)
        except (VoiceError, ValueError) as e:
            embed = create_embed("Hata", str(e), Colors.ERROR)
            await interaction.response.send_message(embed=embed)

    @app_commands.command(name="queue", description="Şarkı sırasını göster.")
    async def slash_queue(self, interaction: discord.Interaction, page: int = 1):
        state = self._get_state(interaction.guild.id)
        if not state.queue:
            embed = create_embed("Şarkı Sırası", "Sıra boş.", Colors.INFO)
            await interaction.response.send_message(embed=embed)
            return
        per_page = 10
        queue_snapshot = list(state.queue)
        total_pages = max(1, (len(queue_snapshot) + per_page - 1) // per_page)
        page = max(1, min(page, total_pages))
        start = (page - 1) * per_page
        items = queue_snapshot[start:start + per_page]
        lines = []
        for idx, song in enumerate(items, start=start + 1):
            req = song.requester.mention if song.requester else "Bilinmiyor"
            lines.append(f"`{idx}.` [{song.title}]({song.url}) — {req}")
        embed = discord.Embed(title="📋 Şarkı Sırası", description="\n".join(lines), color=Colors.INFO)
        embed.set_footer(text=f"Sayfa {page}/{total_pages} • Toplam {len(queue_snapshot)} şarkı")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="loop", description="Döngü modunu ayarla: tek, sıra, kapat.")
    @app_commands.describe(mode="'tek' (tek parça), 'sıra' (tüm sıra), 'kapat'")
    async def slash_loop(self, interaction: discord.Interaction, mode: str):
        state = self._get_state(interaction.guild.id)
        mode_lower = mode.lower()
        if mode_lower == "tek":
            state.loop_current = True
            state.loop_queue = False
            status = "tek parça döngüsü aktif"
        elif mode_lower == "sıra":
            state.loop_current = False
            state.loop_queue = True
            status = "tüm sıra döngüsü aktif"
        elif mode_lower == "kapat":
            state.loop_current = False
            state.loop_queue = False
            status = "döngü devre dışı"
        else:
            embed = create_embed("Hata", "Geçersiz mod. Kullanım: `tek`, `sıra`, `kapat`", Colors.ERROR)
            await interaction.response.send_message(embed=embed)
            return
        embed = create_embed("Döngü Modu", f"🔁 {status}.", Colors.SUCCESS if mode_lower != "kapat" else Colors.WARNING)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="volume", description="Ses seviyesini ayarla (0-200).")
    async def slash_volume(self, interaction: discord.Interaction, volume: int):
        vc = interaction.guild.voice_client
        if not vc or not vc.source:
            embed = create_embed("Hata", "Çalan bir şey yok.", Colors.ERROR)
            await interaction.response.send_message(embed=embed)
            return
        if not 0 <= volume <= 200:
            embed = create_embed("Hata", "Ses 0-200 arasında olmalı.", Colors.ERROR)
            await interaction.response.send_message(embed=embed)
            return
        state = self._get_state(interaction.guild.id)
        state.volume = volume / 100
        vc.source.volume = state.volume
        embed = create_embed("Ses Seviyesi", f"🔊 Ses **%{volume}** olarak ayarlandı.", Colors.INFO)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="nowplaying", description="Şu an çalan şarkıyı göster.")
    async def slash_np(self, interaction: discord.Interaction):
        state = self._get_state(interaction.guild.id)
        if not state.current_song:
            embed = create_embed("Bilgi", "Şu anda çalan şarkı yok.", Colors.INFO)
            await interaction.response.send_message(embed=embed)
            return
        song = state.current_song
        embed = discord.Embed(title="🎵 Şimdi Çalıyor", description=f"[{song.title}]({song.url})", color=Colors.PLAYING)
        if song.thumbnail:
            embed.set_thumbnail(url=song.thumbnail)
        if song.duration:
            embed.add_field(name="⏱️ Süre", value=format_duration(song.duration), inline=True)
        if song.uploader:
            embed.add_field(name="📺 Kanal", value=song.uploader, inline=True)
        embed.add_field(name="👤 İsteyen", value=song.requester.mention if song.requester else "Bilinmiyor", inline=True)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="help", description="Botun tüm komutlarını ve kullanımlarını gösterir.")
    async def slash_help(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="🎵 Müzik Botu Yardım Menüsü",
            description="Aşağıda botun tüm slash komutlarını ve açıklamalarını bulabilirsiniz.",
            color=Colors.PRIMARY
        )
        embed.add_field(
            name="▶️ Oynatma Kontrolleri",
            value=(
                "`/play <sorgu>` - Şarkı veya Spotify linki oynat / sıraya ekle\n"
                "`/playlist <url>` - YouTube playlist'ini sıraya ekle\n"
                "`/skip` - Şu an çalan şarkıyı atla\n"
                "`/pause` - Şarkıyı duraklat\n"
                "`/resume` - Duraklatılmış şarkıyı devam ettir\n"
                "`/stop` - Sırayı temizle ve kanaldan ayrıl\n"
                "`/loop <mod>` - Döngü modu: tek, sıra, kapat\n"
                "`/shuffle` - Kuyruktaki şarkıları karıştır\n"
                "`/remove <sıra>` - Kuyruktan şarkı kaldır\n"
                "`/seek <saniye>` - Şarkıda ileri/geri sar"
            ),
            inline=False
        )
        embed.add_field(
            name="🔊 Ses & Sıra Yönetimi",
            value=(
                "`/join` - Bulunduğunuz ses kanalına katıl\n"
                "`/leave` - Ses kanalından ayrıl\n"
                "`/volume <0-200>` - Ses seviyesini ayarla\n"
                "`/queue [sayfa]` - Şarkı sırasını göster\n"
                "`/nowplaying` - Şu an çalan şarkıyı göster"
            ),
            inline=False
        )
        embed.add_field(
            name="❤️ Favoriler",
            value=(
                "`/favori` - Şu an çalan şarkıyı favorilere ekle\n"
                "`/favoriler` - Favori şarkılarını listele\n"
                "`/favoriçal` - Favori şarkılarını sıraya ekleyip oynat\n"
                "`/favorisil <sıra>` - Favorilerden şarkı sil"
            ),
            inline=False
        )
        embed.add_field(
            name="📀 Kullanıcı Playlistleri",
            value=(
                "`/playlist_oluştur <isim>` - Yeni bir playlist oluştur\n"
                "`/playlist_ekle <playlist_id>` - Çalan şarkıyı playlist'e ekle\n"
                "`/playlist_queue_kaydet <playlist_id>` - Kuyruktaki şarkıları playlist'e kaydet\n"
                "`/playlist_göster <playlist_id> [sayfa]` - Playlist içeriğini göster\n"
                "`/playlist_shuffle <playlist_id>` - Playlist'i karıştırarak yükle\n"
                "`/playlist_çal <playlist_id>` - Playlist'i sıraya ekleyip oynat\n"
                "`/playlist_sil <playlist_id>` - Playlist'i sil\n"
                "`/playlist_remove_song <playlist_id> <sıra>` - Playlist'ten şarkı sil"
            ),
            inline=False
        )
        embed.set_footer(text="Not: Tüm komutlar / ile başlar. Şarkı sırasında butonları da kullanabilirsiniz.")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="favori", description="Şu an çalan şarkıyı favorilere ekle.")
    async def slash_favorite(self, interaction: discord.Interaction):
        await interaction.response.defer()
        state = self._get_state(interaction.guild.id)
        if not state.current_song:
            embed = create_embed("Hata", "Şu anda çalan şarkı yok.", Colors.ERROR)
            await interaction.followup.send(embed=embed)
            return
        success = await self.db.add_favorite(interaction.user.id, state.current_song)
        if success:
            embed = create_embed("Favori", f"❤️ `{state.current_song.title}` favorilerinize eklendi.", Colors.SUCCESS)
        else:
            embed = create_embed("Favori", "Bu şarkı zaten favorilerinizde.", Colors.WARNING)
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="favoriler", description="Favori şarkılarınızı listele.")
    async def slash_favorites(self, interaction: discord.Interaction):
        await interaction.response.defer()
        songs = await self.db.get_favorites(interaction.user.id)
        if not songs:
            embed = create_embed("Favoriler", "Henüz favori şarkınız yok.", Colors.INFO)
            await interaction.followup.send(embed=embed)
            return
        lines = [f"`{i}.` [{s.title}]({s.url})" for i, s in enumerate(songs[:10], start=1)]
        embed = discord.Embed(title="❤️ Favori Şarkılarınız", description="\n".join(lines), color=Colors.PRIMARY)
        if len(songs) > 10:
            embed.set_footer(text=f"Toplam {len(songs)} şarkı, sadece ilk 10 gösteriliyor.")
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="favoriçal", description="Favori şarkılarınızı sıraya ekleyip çal.")
    async def slash_playfavorites(self, interaction: discord.Interaction):
        await interaction.response.defer()
        songs = await self.db.get_favorites(interaction.user.id)
        if not songs:
            embed = create_embed("Hata", "Favori listeniz boş.", Colors.ERROR)
            await interaction.followup.send(embed=embed)
            return
        if not await self._ensure_voice(interaction):
            embed = create_embed("Hata", "Ses kanalına bağlanılamadı. Lütfen bir ses kanalında olduğunuzdan emin olun.", Colors.ERROR)
            await interaction.followup.send(embed=embed)
            return
        state = self._get_state(interaction.guild.id)
        async with state.lock:
            for song in songs:
                song.requester = interaction.user
                state.queue.append(song)
        embed = create_embed("Favoriler", f"{len(songs)} şarkı sıraya eklendi.", Colors.SUCCESS)
        await interaction.followup.send(embed=embed)

        await self._maybe_start_playback(interaction)

    @app_commands.command(name="playlist_oluştur", description="Yeni bir playlist oluştur.")
    async def slash_create_playlist(self, interaction: discord.Interaction, name: str):
        await interaction.response.defer()
        pl_id = await self.db.create_playlist(name, interaction.user.id)
        embed = create_embed("Playlist Oluşturuldu", f"`{name}` adlı playlist oluşturuldu (ID: {pl_id}).", Colors.SUCCESS)
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="playlist_ekle", description="Mevcut şarkıyı bir playlist'e ekle.")
    async def slash_add_to_playlist(self, interaction: discord.Interaction, playlist_id: int):
        await interaction.response.defer()
        state = self._get_state(interaction.guild.id)
        if not state.current_song:
            embed = create_embed("Hata", "Şu anda çalan şarkı yok.", Colors.ERROR)
            await interaction.followup.send(embed=embed)
            return
        playlists = await self.db.get_playlists(interaction.user.id)
        if not any(p["id"] == playlist_id for p in playlists):
            embed = create_embed("Hata", "Bu playlist size ait değil veya bulunamadı.", Colors.ERROR)
            await interaction.followup.send(embed=embed)
            return
        songs = await self.db.get_playlist_songs(playlist_id)
        position = len(songs)
        await self.db.add_song_to_playlist(playlist_id, state.current_song, position)
        embed = create_embed("Playlist'e Eklendi", f"`{state.current_song.title}` playlist'e eklendi.", Colors.SUCCESS)
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="playlist_göster", description="Playlist'teki şarkıları listele.")
    async def slash_show_playlist(self, interaction: discord.Interaction, playlist_id: int, page: int = 1):
        await interaction.response.defer()
        playlists = await self.db.get_playlists(interaction.user.id)
        if not any(p["id"] == playlist_id for p in playlists):
            embed = create_embed("Hata", "Bu playlist size ait değil veya bulunamadı.", Colors.ERROR)
            await interaction.followup.send(embed=embed)
            return

        songs = await self.db.get_playlist_songs(playlist_id)
        if not songs:
            embed = create_embed("Playlist", "Playlist boş.", Colors.INFO)
            await interaction.followup.send(embed=embed)
            return

        per_page = 10
        total_pages = max(1, (len(songs) + per_page - 1) // per_page)
        page = max(1, min(page, total_pages))
        start = (page - 1) * per_page
        items = songs[start:start + per_page]
        lines = [f"{start + i + 1}. [{s.title}]({s.url})" for i, s in enumerate(items)]
        embed = discord.Embed(title=f"📀 Playlist ID: {playlist_id}", description="\n".join(lines), color=Colors.PRIMARY)
        embed.set_footer(text=f"Sayfa {page}/{total_pages} • Toplam {len(songs)} şarkı")
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="playlist_çal", description="Bir playlist'i sıraya ekleyip çal.")
    async def slash_play_playlist(self, interaction: discord.Interaction, playlist_id: int):
        await interaction.response.defer()
        playlists = await self.db.get_playlists(interaction.user.id)
        if not any(p["id"] == playlist_id for p in playlists):
            embed = create_embed("Hata", "Bu playlist size ait değil veya bulunamadı.", Colors.ERROR)
            await interaction.followup.send(embed=embed)
            return

        songs = await self.db.get_playlist_songs(playlist_id)
        if not songs:
            embed = create_embed("Hata", "Playlist boş veya bulunamadı.", Colors.ERROR)
            await interaction.followup.send(embed=embed)
            return

        if not await self._ensure_voice(interaction):
            embed = create_embed("Hata", "Ses kanalına bağlanılamadı. Lütfen bir ses kanalında olduğunuzdan emin olun.", Colors.ERROR)
            await interaction.followup.send(embed=embed)
            return

        state = self._get_state(interaction.guild.id)
        async with state.lock:
            for song in songs:
                song.requester = interaction.user
                state.queue.append(song)
        embed = create_embed("Playlist", f"{len(songs)} şarkı sıraya eklendi.", Colors.SUCCESS)
        await interaction.followup.send(embed=embed)

        await self._maybe_start_playback(interaction)

    @app_commands.command(name="shuffle", description="Kuyruktaki şarkıları karıştır.")
    async def slash_shuffle(self, interaction: discord.Interaction):
        state = self._get_state(interaction.guild.id)

        async with state.lock:
            if len(state.queue) < 2:
                embed = create_embed("Hata", "Karıştırmak için en az 2 şarkı gerekli.", Colors.ERROR)
                await interaction.response.send_message(embed=embed)
                return

            songs = list(state.queue)
            random.shuffle(songs)
            state.queue.clear()
            state.queue.extend(songs)
            total = len(state.queue)

        embed = create_embed("Karıştırıldı", f"🔀 {total} şarkılık sıra karıştırıldı.", Colors.SUCCESS)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="remove", description="Belirtilen sıradaki şarkıyı kaldır.")
    async def slash_remove(self, interaction: discord.Interaction, position: int):
        state = self._get_state(interaction.guild.id)

        async with state.lock:
            queue_list = list(state.queue)

            if position < 1 or position > len(queue_list):
                embed = create_embed("Hata", "Geçersiz sıra numarası.", Colors.ERROR)
                await interaction.response.send_message(embed=embed)
                return

            removed_song = queue_list.pop(position - 1)
            state.queue.clear()
            state.queue.extend(queue_list)

        embed = create_embed("Şarkı Kaldırıldı", f"🗑️ **{removed_song.title}** kuyruktan kaldırıldı.", Colors.SUCCESS)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="playlist_shuffle", description="Playlisti karıştırarak yükle.")
    async def slash_playlist_shuffle(self, interaction: discord.Interaction, playlist_id: int):
        await interaction.response.defer()
        playlists = await self.db.get_playlists(interaction.user.id)
        if not any(p["id"] == playlist_id for p in playlists):
            embed = create_embed("Hata", "Bu playlist size ait değil veya bulunamadı.", Colors.ERROR)
            await interaction.followup.send(embed=embed)
            return

        songs = await self.db.get_playlist_songs(playlist_id)
        if not songs:
            embed = create_embed("Hata", "Playlist boş veya bulunamadı.", Colors.ERROR)
            await interaction.followup.send(embed=embed)
            return

        if not await self._ensure_voice(interaction):
            embed = create_embed("Hata", "Ses kanalına bağlanılamadı. Lütfen bir ses kanalında olduğunuzdan emin olun.", Colors.ERROR)
            await interaction.followup.send(embed=embed)
            return

        random.shuffle(songs)
        state = self._get_state(interaction.guild.id)
        async with state.lock:
            for song in songs:
                song.requester = interaction.user
                state.queue.append(song)

        embed = create_embed("Playlist", f"{len(songs)} şarkı karıştırılarak sıraya eklendi.", Colors.SUCCESS)
        await interaction.followup.send(embed=embed)

        await self._maybe_start_playback(interaction)

    @app_commands.command(name="favorisil", description="Favorilerden şarkı sil.")
    async def slash_remove_favorite(self, interaction: discord.Interaction, index: int):
        await interaction.response.defer()
        songs = await self.db.get_favorites(interaction.user.id)

        if index < 1 or index > len(songs):
            embed = create_embed("Hata", "Geçersiz sıra numarası.", Colors.ERROR)
            await interaction.followup.send(embed=embed)
            return

        song = songs[index - 1]
        await self.db.remove_favorite(interaction.user.id, song.url)
        embed = create_embed("Favori Silindi", f"❤️❌ `{song.title}` favorilerden silindi.", Colors.SUCCESS)
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="playlist_queue_kaydet", description="Kuyruktaki tüm şarkıları bir playlist'e kaydet.")
    async def slash_save_queue_to_playlist(self, interaction: discord.Interaction, playlist_id: int):
        await interaction.response.defer()
        playlists = await self.db.get_playlists(interaction.user.id)
        if not any(p["id"] == playlist_id for p in playlists):
            embed = create_embed("Hata", "Bu playlist size ait değil veya bulunamadı.", Colors.ERROR)
            await interaction.followup.send(embed=embed)
            return

        state = self._get_state(interaction.guild.id)
        async with state.lock:
            songs = list(state.queue)

        if not songs:
            embed = create_embed("Hata", "Kuyruk boş.", Colors.ERROR)
            await interaction.followup.send(embed=embed)
            return

        existing = await self.db.get_playlist_songs(playlist_id)
        position = len(existing)
        for idx, song in enumerate(songs, start=position):
            await self.db.add_song_to_playlist(playlist_id, song, idx)

        embed = create_embed("Playlist Kaydedildi", f"{len(songs)} şarkı playlist'e kaydedildi.", Colors.SUCCESS)
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="playlist_sil", description="Bir playlist'i sil.")
    async def slash_delete_playlist(self, interaction: discord.Interaction, playlist_id: int):
        await interaction.response.defer()
        deleted = await self.db.delete_playlist(playlist_id, interaction.user.id)
        if deleted:
            embed = create_embed("Silindi", f"Playlist ID {playlist_id} silindi.", Colors.SUCCESS)
        else:
            embed = create_embed("Hata", "Playlist bulunamadı veya size ait değil.", Colors.ERROR)
        await interaction.followup.send(embed=embed)

    # Yeni komut: Playlist'ten şarkı sil
    @app_commands.command(name="playlist_remove_song", description="Playlist'ten belirli bir sıradaki şarkıyı sil.")
    async def slash_playlist_remove_song(self, interaction: discord.Interaction, playlist_id: int, position: int):
        await interaction.response.defer()
        playlists = await self.db.get_playlists(interaction.user.id)
        if not any(p["id"] == playlist_id for p in playlists):
            embed = create_embed("Hata", "Bu playlist size ait değil veya bulunamadı.", Colors.ERROR)
            await interaction.followup.send(embed=embed)
            return

        songs = await self.db.get_playlist_songs(playlist_id)
        if position < 1 or position > len(songs):
            embed = create_embed("Hata", "Geçersiz sıra numarası.", Colors.ERROR)
            await interaction.followup.send(embed=embed)
            return

        removed_song = songs[position - 1]
        success = await self.db.remove_song_from_playlist(playlist_id, position)
        if success:
            embed = create_embed("Playlist Şarkısı Silindi", f"`{removed_song.title}` playlistten silindi.", Colors.SUCCESS)
        else:
            embed = create_embed("Hata", "Şarkı silinemedi.", Colors.ERROR)
        await interaction.followup.send(embed=embed)


# ─── Bot Setup ───────────────────────────────────────────────────────────────

class MusicBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.voice_states = True
        super().__init__(command_prefix="!", description="Discord Music Bot", intents=intents, help_command=None)

    async def setup_hook(self) -> None:
        await self.add_cog(Music(self))
        try:
            synced = await self.tree.sync()
            logger.info(f"{len(synced)} slash komutu senkronize edildi.")
        except Exception as e:
            logger.error(f"Slash komutları senkronize edilemedi: {e}")

    async def on_ready(self):
        logger.info(f"Giriş yapıldı: {self.user} (ID: {self.user.id})")
        # Presence, Music cog içindeki döngü tarafından otomatik güncellenir.


async def main() -> None:
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        logger.error("DISCORD_BOT_TOKEN .env dosyasında bulunamadı. Lütfen .env dosyasını oluşturun.")
        return

    bot = MusicBot()

    @bot.tree.error
    async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        logger.error(f"App command hatası ({interaction.command.name if interaction.command else '?'}): {error}")
        embed = create_embed("Beklenmeyen Hata", "Komut işlenirken bir hata oluştu. Lütfen tekrar deneyin.", Colors.ERROR)
        try:
            if interaction.response.is_done():
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                await interaction.response.send_message(embed=embed, ephemeral=True)
        except Exception:
            pass

    try:
        async with bot:
            await bot.start(token)
    except discord.LoginFailure:
        logger.error("Geçersiz DISCORD_BOT_TOKEN. Lütfen .env dosyasını kontrol edin.")
    except Exception as e:
        logger.error(f"Bot başlatılamadı: {e}")


if __name__ == "__main__":
    asyncio.run(main())