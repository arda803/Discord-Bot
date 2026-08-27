from __future__ import annotations

from discord import state

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
- 🎤 Senkronize Karaoke Modu (LRCLIB, mevcut oynatma zaman çizgisiyle senkron)
"""

import asyncio
import bisect
import difflib
import json
import logging
import random
import os
import re
import time
import sys
from pathlib import Path
from typing import Optional, Dict, Any, Deque, List, Tuple
from dataclasses import dataclass, field
from collections import deque
from datetime import datetime, timezone

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

# Karaoke negative cache TTL (seconds) – 7 days
NEGATIVE_CACHE_TTL = 7 * 24 * 3600


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
        # Robust seek state (replaces is_seeking/seek_position)
        self.seek_target: Optional[int] = None
        self.seek_retry_count: int = 0
        self.seek_confirm_task: Optional[asyncio.Task] = None
        self.playback_generation: int = 0
        self.now_playing_message: Optional[discord.Message] = None
        self.active: bool = False
        self.played_any: bool = False
        self.failed_count: int = 0
        self.started_at: float = 0.0
        self.playback_offset: float = 0.0
        self.paused_at: float = 0.0
        self.timer_task: Optional[asyncio.Task] = None
        # ── Karaoke state (per-guild, isolated) ──
        self.karaoke_enabled: bool = False
        self.lyrics_track: Optional["LyricsTrack"] = None
        self.lyrics_song: Optional[Song] = None       # which Song object lyrics_track belongs to
        self.karaoke_active_index: int = -1
        self.karaoke_message: Optional[discord.Message] = None
        self.karaoke_task: Optional[asyncio.Task] = None
        self.karaoke_fetch_task: Optional[asyncio.Task] = None


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
            await db.execute("""
                CREATE TABLE IF NOT EXISTS lyrics_cache (
                    cache_key TEXT PRIMARY KEY,
                    title TEXT,
                    artist TEXT,
                    duration INTEGER,
                    lines_json TEXT,
                    found INTEGER NOT NULL,
                    cached_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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

    # ── Lyrics cache (karaoke) ──────────────────────────────────────────

    async def get_cached_lyrics(self, cache_key: str, ttl_negative: int = NEGATIVE_CACHE_TTL) -> Optional[Any]:
        """Returns a LyricsTrack on a positive cache hit, the string 'NONE' on
        a cached negative result that is still fresh, or None if there is no
        cache entry or the negative entry has expired (TTL exceeded)."""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT title, artist, duration, lines_json, found, cached_at FROM lyrics_cache WHERE cache_key = ?",
                (cache_key,)
            ) as cursor:
                row = await cursor.fetchone()
        if not row:
            return None
        title, artist, duration, lines_json, found, cached_at = row
        if not found:
            # Negative cache: check TTL
            if ttl_negative > 0 and cached_at:
                try:
                    dt = datetime.strptime(cached_at, "%Y-%m-%d %H:%M:%S")
                    # SQLite stores CURRENT_TIMESTAMP in UTC (if default set)
                    dt = dt.replace(tzinfo=timezone.utc)
                    now = datetime.now(timezone.utc)
                    age = (now - dt).total_seconds()
                    if age > ttl_negative:
                        logger.info(f"[KARAOKE] Negative cache expired (age {age:.0f}s > {ttl_negative}s), treating as miss")
                        return None
                except Exception:
                    # If timestamp parsing fails, treat as expired to be safe
                    return None
            return "NONE"
        try:
            raw_lines = json.loads(lines_json) if lines_json else []
            lines = [LyricLine(timestamp=l["t"], text=l["x"], index=i) for i, l in enumerate(raw_lines)]
        except (json.JSONDecodeError, KeyError, TypeError):
            return None
        if not lines:
            return None
        return LyricsTrack(title=title or "", artist=artist or "", duration=duration, lines=lines)

    async def cache_lyrics(self, cache_key: str, track: "LyricsTrack") -> None:
        lines_json = json.dumps([{"t": l.timestamp, "x": l.text} for l in track.lines])
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT OR REPLACE INTO lyrics_cache
                   (cache_key, title, artist, duration, lines_json, found, cached_at)
                   VALUES (?, ?, ?, ?, ?, 1, CURRENT_TIMESTAMP)""",
                (cache_key, track.title, track.artist, track.duration, lines_json)
            )
            await db.commit()

    async def cache_lyrics_negative(self, cache_key: str) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT OR REPLACE INTO lyrics_cache
                   (cache_key, title, artist, duration, lines_json, found, cached_at)
                   VALUES (?, '', '', NULL, NULL, 0, CURRENT_TIMESTAMP)""",
                (cache_key,)
            )
            await db.commit()


# ─── Karaoke: Lyrics Data Model ──────────────────────────────────────────

@dataclass
class LyricLine:
    timestamp: float
    text: str
    index: int


@dataclass
class LyricsTrack:
    title: str
    artist: str
    duration: Optional[int]
    lines: List[LyricLine]

    def active_index(self, position: float) -> int:
        """Returns the index of the lyric line active at `position` seconds,
        or -1 if playback hasn't reached the first line yet."""
        if not self.lines:
            return -1
        timestamps = [l.timestamp for l in self.lines]
        return bisect.bisect_right(timestamps, position) - 1


# ─── Karaoke: Lyrics Provider (LRCLIB) ───────────────────────────────────

def _normalize_text(s: str) -> str:
    """Lowercases and strips bracketed noise (e.g. '(Official Video)',
    '[Lyrics]') and punctuation, for fuzzy title/artist comparison."""
    s = (s or "").lower()
    s = re.sub(r"[\(\[].*?[\)\]]", " ", s)
    s = re.sub(r"[^a-z0-9ğüşıöçâîû ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, _normalize_text(a), _normalize_text(b)).ratio()


LRC_LINE_RE = re.compile(r"^\[(\d+):(\d+(?:\.\d+)?)\](.*)$")


def _parse_lrc(text: str) -> List[LyricLine]:
    lines: List[LyricLine] = []
    for raw in (text or "").splitlines():
        m = LRC_LINE_RE.match(raw.strip())
        if not m:
            continue
        minutes = int(m.group(1))
        seconds = float(m.group(2))
        content = m.group(3).strip()
        lines.append(LyricLine(timestamp=minutes * 60 + seconds, text=content, index=0))
    lines.sort(key=lambda l: l.timestamp)
    for i, l in enumerate(lines):
        l.index = i
    return lines


class LyricsProvider:
    """Abstraction over a synchronized-lyrics source. Karaoke code depends
    only on this interface (and the normalized LyricsTrack it returns), so
    the underlying provider can be swapped without touching the engine."""

    async def fetch(self, title: str, artist: Optional[str], duration: Optional[int]) -> Optional[LyricsTrack]:
        raise NotImplementedError


class LRCLibProvider(LyricsProvider):
    """Fetches synchronized lyrics from LRCLIB (https://lrclib.net)."""

    BASE_URL = "https://lrclib.net/api"
    MIN_TITLE_CONFIDENCE = 0.55
    MAX_DURATION_DRIFT = 5  # seconds

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
            score = _similarity(title, r.get("trackName", ""))
            if artist:
                score = (score + _similarity(artist, r.get("artistName", ""))) / 2
            if score > best_score:
                best_score, best = score, r
        if best_score < self.MIN_TITLE_CONFIDENCE:
            return None
        return best

    async def fetch(self, title: str, artist: Optional[str], duration: Optional[int]) -> Optional[LyricsTrack]:
        timeout = aiohttp.ClientTimeout(total=8)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                # Try the exact-match endpoint first (fast path when metadata lines up).
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
                    pass  # fall through to search

                # Fall back to search + fuzzy-matched candidate selection.
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


# ─── Karaoke: Lyrics Service (provider + cache) ──────────────────────────

class LyricsService:
    """Sits between the karaoke engine and the provider: checks the cache
    first, calls the provider on a miss, and caches both positive and
    negative results (negative with TTL)."""

    def __init__(self, db: DatabaseManager, provider: LyricsProvider):
        self.db = db
        self.provider = provider

    @staticmethod
    def cache_key(title: str, artist: Optional[str], duration: Optional[int]) -> str:
        norm_title = _normalize_text(title)
        norm_artist = _normalize_text(artist or "")
        # Bucket duration to the nearest 3s so trivial metadata jitter between
        # re-resolves of the same song still hits the same cache entry.
        bucket = int(duration // 3) if duration else 0
        return f"{norm_title}|{norm_artist}|{bucket}"

    def _clean_title_part(self, title_part: str) -> str:
        """Remove common video suffixes from the title part after split."""
        # Patterns that appear at the end, with optional dash/space
        suffixes = [
            r'\s*[-–—]\s*(Official\s*(Music\s*)?Video|Official\s*Audio|Lyrics\s*Video|HD|4K|Music\s*Video|Official|Video|Audio)',
            r'\s*\(Official\s*(Music\s*)?Video\)',
            r'\s*\[(Official\s*(Music\s*)?Video|HD|4K)\]',
        ]
        cleaned = title_part
        for pat in suffixes:
            cleaned = re.sub(pat, '', cleaned, flags=re.IGNORECASE)
        return cleaned.strip()

    def _parse_metadata(self, song: Song) -> Tuple[str, Optional[str]]:
        """Extract clean title and artist from the Song object.
        Priority:
          1. Parse from title using separators (e.g. "Artist - Title").
          2. If that fails, fall back to uploader as artist (if available and not generic).
        Returns (title, artist)."""
        raw_title = song.title or ""
        uploader = song.uploader

        # Step 1: Remove parenthetical/bracketed content and common suffixes.
        # We keep the cleaned version for splitting, but also need to preserve
        # the original title for fallback.
        cleaned = raw_title
        # Remove content in parentheses and brackets
        cleaned = re.sub(r'\([^)]*\)', '', cleaned)
        cleaned = re.sub(r'\[[^\]]*\]', '', cleaned)

        # Step 2: Try to split by common separators.
        separators = [" - ", " – ", " — ", " | "]
        artist = None
        title = None
        source = "unknown"

        for sep in separators:
            if sep in cleaned:
                parts = cleaned.split(sep, 1)
                potential_artist = parts[0].strip()
                potential_title = parts[1].strip()
                # Both parts should have some substance
                if potential_artist and len(potential_artist) > 1 and potential_title:
                    # Clean the title part from common suffixes
                    potential_title = self._clean_title_part(potential_title)
                    # Also clean the artist part (remove extra spaces, etc.)
                    potential_artist = potential_artist.strip()
                    if potential_artist and potential_title:
                        artist = potential_artist
                        title = potential_title
                        source = "title_parser"
                        break

        # If we got nothing, try to strip the uploader from the front if it appears as a prefix
        if not artist and uploader:
            for sep in separators:
                if raw_title.startswith(uploader + sep):
                    potential_title = raw_title[len(uploader) + len(sep):].strip()
                    # Clean the title
                    potential_title = self._clean_title_part(potential_title)
                    if potential_title:
                        artist = uploader
                        title = potential_title
                        source = "uploader_stripped"
                        break

        # If still no artist, fallback to uploader (if not generic) and use raw title (after cleaning)
        if not artist:
            if uploader and uploader.lower() not in ("unknown", "none", ""):
                artist = uploader
                source = "uploader_fallback"
            # Use the cleaned title (without brackets) as title, but we should preserve the original?
            # We'll use the cleaned title (which removed brackets, but not separators) and then clean suffixes
            title = self._clean_title_part(cleaned) if not title else title

        # Final fallback: if title is empty, use raw_title
        if not title:
            title = raw_title

        # Ensure we don't have an artist that is the same as title (e.g., if uploader is same as title)
        if artist and title and artist.lower() == title.lower():
            artist = None
            source = "same_as_title"

        logger.info(f"[KARAOKE] Metadata parsed: raw_title='{raw_title}', raw_uploader='{uploader}', "
                    f"parsed_artist='{artist}', parsed_title='{title}', source='{source}'")
        return title, artist

    async def get_lyrics(self, song: Song) -> Optional[LyricsTrack]:
        # Parse metadata
        title, artist = self._parse_metadata(song)
        duration = song.duration

        logger.info(f"[KARAOKE] Lyrics lookup started for: raw title='{song.title}', raw uploader='{song.uploader}'")
        logger.info(f"[KARAOKE] Parsed title='{title}', parsed artist='{artist}', duration={duration}s")

        key = self.cache_key(title, artist, duration)
        logger.info(f"[KARAOKE] Cache key: {key}")

        try:
            cached = await self.db.get_cached_lyrics(key)
        except Exception as e:
            logger.warning(f"[KARAOKE] Cache read failed: {e}")
            cached = None

        if cached is not None:
            if cached == "NONE":
                logger.info(f"[KARAOKE] Negative cache entry found (TTL is handled internally, treating as not found)")
                return None
            logger.info(f"[KARAOKE] Cache hit: synchronized lyrics found for '{title}'")
            return cached

        # Cache miss (or expired negative) – query provider
        logger.info(f"[KARAOKE] Requesting synchronized lyrics from provider for '{title}'")
        track: Optional[LyricsTrack] = None
        try:
            track = await self.provider.fetch(title, artist, duration)
        except Exception as e:
            logger.warning(f"[KARAOKE] Provider error: {e}")
            # Do not cache negative on errors; let it retry next time.
            return None

        try:
            if track:
                logger.info(f"[KARAOKE] Provider returned synchronized lyrics for '{title}' (artist: {track.artist})")
                await self.db.cache_lyrics(key, track)
                return track
            else:
                logger.info(f"[KARAOKE] No synchronized lyrics found after provider lookup for '{title}'")
                # Store negative with TTL (expiry handled by get_cached_lyrics)
                await self.db.cache_lyrics_negative(key)
                return None
        except Exception as e:
            logger.warning(f"[KARAOKE] Cache write failed: {e}")
            # If cache write fails, still return track if we have it.
            return track


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
        state = self.cog._get_state(self.ctx.guild.id)
        state.paused_at = time.monotonic()
        vc.pause()
        await interaction.response.send_message("⏸️ Duraklatıldı.", ephemeral=True)

    @discord.ui.button(label="Devam Et", style=discord.ButtonStyle.success, emoji="▶️")
    async def resume_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = get_voice_client(self.ctx)
        if not vc or not vc.is_paused():
            await interaction.response.send_message("Duraklatılmış bir şarkı yok.", ephemeral=True)
            return
        state = self.cog._get_state(self.ctx.guild.id)
        if state.paused_at and state.started_at:
            state.playback_offset += state.paused_at - state.started_at
        state.started_at = time.monotonic()
        state.paused_at = 0.0
        vc.resume()
        await interaction.response.send_message("▶️ Devam ediyor.", ephemeral=True)

    @discord.ui.button(label="Geç", style=discord.ButtonStyle.secondary, emoji="⏭️")
    async def skip_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = get_voice_client(self.ctx)
        if not vc or not vc.is_playing():
            await interaction.response.send_message("Şu anda çalan bir şarkı yok.", ephemeral=True)
            return
        state = self.cog._get_state(self.ctx.guild.id)
        # Invalidate any pending seek to prevent retry loops
        if state.seek_target is not None:
            logger.info(f"[SEEK] Skip button pressed during seek, clearing seek state (gen {state.playback_generation})")
            state.playback_generation += 1
            state.seek_target = None
            state.seek_retry_count = 0
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

    @discord.ui.button(label="Karaoke", style=discord.ButtonStyle.secondary, emoji="🎤")
    async def karaoke_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = get_voice_client(self.ctx)
        if not vc or not (vc.is_playing() or vc.is_paused()):
            await interaction.response.send_message("Şu anda çalan bir şarkı yok.", ephemeral=True)
            return
        state = self.cog._get_state(self.ctx.guild.id)
        if state.karaoke_enabled:
            await self.cog._disable_karaoke(state)
            await interaction.response.send_message("🎤 Karaoke modu kapatıldı.", ephemeral=True)
        else:
            await interaction.response.send_message("🎤 Karaoke modu açıldı.", ephemeral=True)
            await self.cog._enable_karaoke(self.ctx, state, interaction.channel)

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

        # Karaoke: lyrics provider + cache layer (swappable provider).
        self.lyrics_service = LyricsService(self.db, LRCLibProvider())

        # Fire-and-forget background tasks (e.g. auto-starting karaoke lookup
        # on a new song) MUST be kept referenced somewhere, or asyncio's
        # garbage collector can silently drop them before they ever run.
        self._background_tasks: "set[asyncio.Task]" = set()

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
                        current_position = self._get_current_position(state)
                        if song.duration:
                            text = (
                                f"🎧 {song.title} • "
                                f"⏱️ {format_duration(current_position)} / {format_duration(song.duration)}"
                            )
                        else:
                            text = f"🎧 {song.title} • ⏱️ {format_duration(current_position)}"
                    else:
                        text = "🎵 Müzik botu hazır"

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

    def _get_current_position(self, state: GuildMusicState) -> int:
        """Şarkının mevcut oynatma saniyesini hesaplar."""
        if not state.started_at:
            return int(state.playback_offset)
        # Pause durumunda süre ilerlemesin
        if state.paused_at:
            elapsed = state.paused_at - state.started_at
        else:
            elapsed = time.monotonic() - state.started_at
        return max(0, int(state.playback_offset + elapsed))

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

        self._fire_and_forget(_delete())

    def _clear_state(self, guild_id: int) -> None:
        state = self._states.get(guild_id)
        if state:
            self._cancel_timer_task(state)
            self._cancel_karaoke_task(state)
            self._cancel_karaoke_fetch(state)
            # Cancel any pending seek confirmation and invalidate callbacks
            if state.seek_confirm_task and not state.seek_confirm_task.done():
                state.seek_confirm_task.cancel()
                state.seek_confirm_task = None
            state.queue.clear()
            state.current_song = None
            state.playback_offset = 0.0
            state.paused_at = 0.0
            state.loop_current = False
            state.loop_queue = False
            state.seek_target = None
            state.seek_retry_count = 0
            state.playback_generation += 1
            state.active = False
            state.played_any = False
            state.failed_count = 0
            state.started_at = 0.0
            self._delete_message_later(state.now_playing_message)
            state.now_playing_message = None
            state.karaoke_enabled = False
            state.lyrics_track = None
            state.lyrics_song = None
            state.karaoke_active_index = -1
            self._delete_message_later(state.karaoke_message)
            state.karaoke_message = None

    def _fire_and_forget(self, coro) -> asyncio.Task:
        """Schedules a background coroutine while keeping a strong reference
        to the Task, so it can't be garbage-collected before it runs (a
        well-known asyncio pitfall with bare `asyncio.create_task(...)`)."""
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    def _cancel_timer_task(self, state: GuildMusicState) -> None:
        """Cancel the guild's Now Playing updater task, if any, so it never
        outlives the song it belongs to (avoids orphaned tasks / cross-guild
        leakage since each GuildMusicState holds its own task)."""
        task = state.timer_task
        state.timer_task = None
        if task and not task.done():
            task.cancel()

    def _build_now_playing_embed(self, state: GuildMusicState, song: Song) -> discord.Embed:
        """Builds the 'Now Playing' embed, including the live playback position.
        Kept as its own helper so both the initial send and the periodic
        updater produce an identical embed."""
        embed = discord.Embed(title="▶️ Şimdi Çalıyor", description=f"[{song.title}]({song.url})", color=Colors.PLAYING)
        if song.thumbnail:
            embed.set_image(url=song.thumbnail)
        position = self._get_current_position(state)
        if song.duration:
            embed.add_field(
                name="⏱️ Süre",
                value=f"{format_duration(position)} / {format_duration(song.duration)}",
                inline=True,
            )
        else:
            embed.add_field(name="⏱️ Süre", value=format_duration(position), inline=True)
        if song.uploader:
            embed.add_field(name="📺 Kanal", value=song.uploader, inline=True)
        embed.add_field(name="👤 İsteyen", value=song.requester.mention if song.requester else "Bilinmiyor", inline=True)
        if state.loop_current:
            embed.set_footer(text="🔂 Tek parça döngüsü aktif")
        elif state.loop_queue:
            embed.set_footer(text="🔁 Tüm sıra döngüsü aktif")
        return embed

    async def _now_playing_updater(self, ctx: PlaybackContext, state: GuildMusicState, song: Song) -> None:
        """Periodically edits the existing Now Playing message to refresh the
        live playback position. One of these runs per guild per song; it is
        cancelled as soon as the song changes, is skipped/stopped, or ends."""
        try:
            while True:
                await asyncio.sleep(1.5)

                # Stop as soon as this is no longer the current song/message,
                # e.g. skip, stop, seek-restart, or the next track starting.
                if state.current_song is not song or state.now_playing_message is None:
                    return

                vc = get_voice_client(ctx)
                if not vc or not (vc.is_playing() or vc.is_paused()):
                    return

                embed = self._build_now_playing_embed(state, song)
                try:
                    # No `view=` passed → the existing MusicControlView/buttons
                    # already attached to the message are left untouched.
                    await state.now_playing_message.edit(embed=embed)
                except discord.NotFound:
                    # Message was deleted (e.g. song change cleaned it up).
                    return
                except discord.HTTPException as e:
                    logger.warning(f"Now Playing mesajı güncellenemedi: {e}")
                    return
        except asyncio.CancelledError:
            raise

    # ── Karaoke engine ───────────────────────────────────────────────
    # The karaoke system is a pure CONSUMER of the existing playback
    # timeline (_get_current_position / state.started_at / playback_offset).
    # It never touches vc.play/vc.stop, never owns FFmpeg, and never creates
    # a second playback process — see NON-NEGOTIABLE COMPATIBILITY REQUIREMENTS.

    def _cancel_karaoke_task(self, state: GuildMusicState) -> None:
        task = state.karaoke_task
        state.karaoke_task = None
        if task and not task.done():
            task.cancel()

    def _cancel_karaoke_fetch(self, state: GuildMusicState) -> None:
        task = state.karaoke_fetch_task
        state.karaoke_fetch_task = None
        if task and not task.done():
            task.cancel()

    async def _disable_karaoke(self, state: GuildMusicState) -> None:
        state.karaoke_enabled = False
        self._cancel_karaoke_task(state)
        self._cancel_karaoke_fetch(state)
        if state.karaoke_message:
            self._delete_message_later(state.karaoke_message)
            state.karaoke_message = None
        logger.info("[KARAOKE] Karaoke disabled")

    async def _enable_karaoke(self, ctx: PlaybackContext, state: GuildMusicState, channel) -> None:
        state.karaoke_enabled = True
        logger.info("[KARAOKE] Karaoke enabled")
        try:
            msg = await channel.send(embed=create_embed("🎤 Karaoke Mode", "Sözler aranıyor...", Colors.PLAYING))
        except discord.HTTPException as e:
            logger.warning(f"[KARAOKE] Could not send karaoke message: {e}")
            return
        state.karaoke_message = msg
        if state.current_song:
            await self._start_karaoke_for_song(ctx, state, state.current_song, state.playback_generation)

    def _build_karaoke_embed(self, state: GuildMusicState, song: Song, position: int) -> discord.Embed:
        track = state.lyrics_track
        idx = state.karaoke_active_index
        rows: List[str] = []
        if track and track.lines:
            start = max(0, idx - 1)
            end = min(len(track.lines), idx + 3)
            for i in range(start, end):
                text = track.lines[i].text or "♪"
                if i == idx:
                    rows.append(f"🎤 **♪ {text} ♪**")
                else:
                    rows.append(text)
        body = "\n".join(rows) if rows else "…"
        artist_part = f" — {song.uploader}" if song.uploader else ""
        description = (
            f"🎵 **{song.title}**{artist_part}\n\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n{body}\n\n━━━━━━━━━━━━━━━━━━━━"
        )
        embed = discord.Embed(title="🎤 Karaoke Mode", description=description, color=Colors.PLAYING)
        dur_text = format_duration(position)
        if song.duration:
            dur_text += f" / {format_duration(song.duration)}"
        embed.add_field(name="⏱️", value=dur_text, inline=False)
        return embed

    async def _update_karaoke_message(self, state: GuildMusicState, song: Song, position: Optional[int] = None, no_lyrics: bool = False) -> None:
        if state.karaoke_message is None:
            return
        try:
            if no_lyrics or state.lyrics_track is None:
                embed = create_embed(
                    "🎤 Karaoke",
                    f"❌ **{song.title}** için senkronize karaoke sözleri bulunamadı.",
                    Colors.WARNING,
                )
            else:
                if position is None:
                    position = self._get_current_position(state)
                embed = self._build_karaoke_embed(state, song, position)
            await state.karaoke_message.edit(embed=embed)
        except discord.NotFound:
            state.karaoke_message = None
            self._cancel_karaoke_task(state)
        except discord.HTTPException as e:
            logger.warning(f"[KARAOKE] Mesaj güncellenemedi: {e}")

    async def _karaoke_updater(self, ctx: PlaybackContext, state: GuildMusicState, song: Song, generation: int) -> None:
        """One of these runs per guild per song while karaoke is active. Reuses
        the SAME authoritative playback position as the Now Playing timer —
        it never runs its own clock. Cancelled on song change, skip, stop,
        karaoke-disable, or a new seek restart (see _play_song)."""
        try:
            while True:
                if state.playback_generation != generation:
                    return
                if state.current_song is not song or not state.karaoke_enabled:
                    return
                if state.karaoke_message is None or state.lyrics_track is None:
                    return
                vc = get_voice_client(ctx)
                if not vc or not (vc.is_playing() or vc.is_paused()):
                    return

                position = self._get_current_position(state)
                new_index = state.lyrics_track.active_index(position)
                if new_index != state.karaoke_active_index:
                    state.karaoke_active_index = new_index
                    logger.info(f"[KARAOKE] Active lyric changed to index {new_index}")
                    await self._update_karaoke_message(state, song, position)

                await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            logger.info("[KARAOKE] Lyrics task cancelled")
            raise
        except Exception as e:
            logger.warning(f"[KARAOKE] Updater error: {e}")

    async def _fetch_and_start_karaoke(self, ctx: PlaybackContext, state: GuildMusicState, song: Song, generation: int) -> None:
        try:
            track = await self.lyrics_service.get_lyrics(song)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"[KARAOKE] API error: {e}")
            track = None

        # Stale guards: song may have changed/skipped/stopped while the API call was in flight.
        if state.playback_generation != generation or state.current_song is not song or not state.karaoke_enabled:
            return

        state.lyrics_song = song
        state.karaoke_active_index = -1

        if track is None:
            state.lyrics_track = None
            await self._update_karaoke_message(state, song, no_lyrics=True)
            return

        state.lyrics_track = track
        if state.karaoke_message is not None:
            self._cancel_karaoke_task(state)
            state.karaoke_task = asyncio.create_task(self._karaoke_updater(ctx, state, song, generation))

    async def _start_karaoke_for_song(self, ctx: PlaybackContext, state: GuildMusicState, song: Song, generation: int) -> None:
        """Ensures the current song has (or is fetching) synchronized lyrics
        and that the sync task is running. Safe to call on: karaoke enabled
        mid-song, a new song starting, or a seek restart of the same song."""
        if not state.karaoke_enabled:
            return

        if state.lyrics_song is song:
            if state.lyrics_track is not None:
                # Already resolved for this exact song — just (re)start the sync task,
                # e.g. after a /seek restart. Reset the index so the display refreshes
                # immediately even if the new position lands on the same line.
                state.karaoke_active_index = -1
                self._cancel_karaoke_task(state)
                state.karaoke_task = asyncio.create_task(self._karaoke_updater(ctx, state, song, generation))
                if state.karaoke_message:
                    await self._update_karaoke_message(state, song)
            else:
                # Already looked up and confirmed unavailable for this song.
                if state.karaoke_message:
                    await self._update_karaoke_message(state, song, no_lyrics=True)
            return

        self._cancel_karaoke_fetch(state)
        state.karaoke_fetch_task = asyncio.create_task(self._fetch_and_start_karaoke(ctx, state, song, generation))

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

    async def _handle_after_playing(self, ctx: PlaybackContext, error: Optional[Exception], was_seek_attempt: bool = False) -> None:
        state = self._get_state(ctx.guild.id)

        # Intentional stop: state was cleared by _clear_state or slash_stop
        if state.current_song is None:
            logger.info("[SEEK] After callback: intentional stop detected (current_song is None)")
            return

        if error and not was_seek_attempt:
            state.failed_count += 1
            logger.error(f"Playback error: {error}")
            embed = create_embed(
                "Oynatma Hatası",
                f"Bu şarkının ses akışı başlatılamadı, sıradakine geçiliyor...\ \n`{error}`",
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
            # ── Case A: Seek attempt ──
            if was_seek_attempt and state.seek_target is not None:
                logger.info(f"[SEEK] Handling seek aftermath (error={error is not None})")

                if error is None:
                    # Old playback stopped cleanly to make way for seek restart.
                    # (If seek had already succeeded, seek_target would be None.)
                    logger.info("[SEEK] Restarting playback at seek position")
                    song_to_play = state.current_song
                    seek_pos = state.seek_target
                else:
                    # Seek restart failed — retry once or give up.
                    state.seek_retry_count += 1
                    logger.error(f"[SEEK] Seek playback failed, retry {state.seek_retry_count}/1")

                    if state.seek_retry_count <= 1:
                        logger.info(f"[SEEK] Retry attempt: {state.seek_retry_count}/1")
                        # Increment generation so the failed attempt's callback is ignored
                        state.playback_generation += 1
                        song_to_play = state.current_song
                        seek_pos = state.seek_target
                    else:
                        logger.error("[SEEK] Seek failed after retry, giving up")
                        # Clean up — do NOT advance queue
                        state.seek_target = None
                        state.seek_retry_count = 0
                        state.playback_generation += 1
                        state.active = False
                        state.started_at = 0.0
                        state.playback_offset = 0.0
                        state.paused_at = 0.0
                        self._cancel_timer_task(state)
                        self._cancel_karaoke_task(state)
                        self._cancel_karaoke_fetch(state)
                        if state.seek_confirm_task and not state.seek_confirm_task.done():
                            state.seek_confirm_task.cancel()
                            state.seek_confirm_task = None
                        if state.now_playing_message:
                            self._delete_message_later(state.now_playing_message)
                            state.now_playing_message = None
                        try:
                            embed = create_embed(
                                "Sarma Başarısız",
                                f"⏩ Şarkıda sarma işlemi başarısız oldu. Şarkı durduruldu.\ \n`{error}`",
                                Colors.ERROR,
                            )
                            await ctx.channel.send(embed=embed)
                        except Exception:
                            pass
                        return  # Critical: do not fall through to queue logic

            # ── Case B: Normal playback end / loop / queue ──
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
                state.started_at = 0.0
                state.playback_offset = 0.0
                state.paused_at = 0.0
                self._cancel_timer_task(state)
                self._cancel_karaoke_task(state)
                self._cancel_karaoke_fetch(state)
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

        # Capture the generation at the moment we start this playback attempt.
        # Any operation that increments the generation after this point
        # (skip, stop, or a seek retry) will cause stale callbacks to be ignored.
        playback_generation = state.playback_generation

        # Önceki 'şimdi çalıyor' mesajını ve buna bağlı canlı zamanlayıcı görevini temizle
        if state.now_playing_message:
            self._delete_message_later(state.now_playing_message)
            state.now_playing_message = None
        self._cancel_timer_task(state)

        # Karaoke: always stop the sync task tied to the previous playback attempt.
        # Only forget the fetched lyrics if this is a genuinely NEW song — a seek
        # restart (seek is not None) plays the same song again, so its already-
        # resolved lyrics_track/lyrics_song stay valid and are reused.
        self._cancel_karaoke_task(state)
        if seek is None:
            self._cancel_karaoke_fetch(state)
            state.lyrics_track = None
            state.lyrics_song = None
            state.karaoke_active_index = -1

        try:
            # YouTube/Spotify stream URL'leri kısa süreli olabildiği için her şarkı
            # başlatılırken akışı yeniden çözüyoruz. Eski imzalı URL'nin hemen bitmesini önler.
            try:
                song.source_url = await resolve_song_url(song.url, asyncio.get_running_loop())
            except YTDLError as exc:
                logger.warning(f"URL resolve failed for '{song.title}': {exc}")
                embed = create_embed(
                    "Atlandı", f"**{song.title}** oynatılamadı, sıradakine geçiliyor.\ \n`{exc}`", Colors.WARNING
                )
                await ctx.channel.send(embed=embed)
                await self._handle_after_playing(ctx, None, was_seek_attempt=False)
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
                await self._handle_after_playing(ctx, None, was_seek_attempt=False)
                return

            state.current_song = song

            def after_callback(error: Optional[Exception]) -> None:
                # Stale callback guard: if generation changed, this callback is obsolete
                if state.playback_generation != playback_generation:
                    logger.info(
                        f"[SEEK] Stale after_callback ignored "
                        f"(gen {playback_generation} vs current {state.playback_generation})"
                    )
                    return

                # Intentional stop detection
                if state.current_song is None:
                    logger.info("[SEEK] After callback: intentional stop detected (current_song is None)")
                    return

                was_seek_attempt = state.seek_target is not None

                if error:
                    logger.error(f"[SEEK] After callback received with error: {error}")
                else:
                    elapsed = time.monotonic() - state.started_at if state.started_at else 0.0
                    # FFmpeg bazen bağlantı/stream hatasında exception döndürmeden hemen kapanır.
                    # Böyle bir durumda bunu normal şarkı sonu gibi saymıyoruz.
                    if elapsed < 2.0:
                        error = AudioSourceError(
                            f"Ses akışı çok erken kapandı ({elapsed:.1f} sn). FFmpeg/yt-dlp stream hatası olabilir."
                        )
                        logger.warning(f"[SEEK] Early termination detected after {elapsed:.1f}s")
                    else:
                        state.played_any = True
                        logger.info(f"[SEEK] Playback completed normally after {elapsed:.1f}s")

                coro = self._handle_after_playing(ctx, error, was_seek_attempt=was_seek_attempt)
                asyncio.run_coroutine_threadsafe(coro, self.bot.loop)

            if vc.is_playing() or vc.is_paused():
                vc.stop()
                await asyncio.sleep(0.2)

            # Guard against generation changes that happened during async setup above
            if state.playback_generation != playback_generation:
                logger.info(f"[SEEK] Generation changed during setup, aborting playback start (gen {playback_generation})")
                return

            try:
                if seek is not None and seek > 0:
                    state.playback_offset = float(seek)
                else:
                    state.playback_offset = 0.0
                state.paused_at = 0.0
                state.started_at = time.monotonic()
                vc.play(source, after=after_callback)
                logger.info(f"[SEEK] FFmpeg playback started for '{song.title}' (gen={playback_generation})")
            except Exception as e:
                logger.error(f"vc.play failed: {e}")
                embed = create_embed("Oynatma Hatası", f"Şarkı başlatılamadı: {e}", Colors.ERROR)
                await ctx.channel.send(embed=embed)
                await self._handle_after_playing(ctx, None, was_seek_attempt=(seek is not None))
                return

            # Şimdi çalıyor embed'ini gönder (canlı süre bilgisiyle birlikte)
            embed = self._build_now_playing_embed(state, song)

            view = MusicControlView(self, ctx)
            msg = await ctx.channel.send(embed=embed, view=view)
            state.now_playing_message = msg
            logger.info(f"Playing '{song.title}' in guild {ctx.guild.id}")

            # For seek attempts, delay the live timer until the health check confirms
            # FFmpeg is actually producing audio. For normal playback, start immediately.
            is_seek_attempt = seek is not None

            if not is_seek_attempt:
                # Normal playback: start timer immediately
                self._cancel_timer_task(state)
                state.timer_task = asyncio.create_task(self._now_playing_updater(ctx, state, song))
                if state.karaoke_enabled:
                    self._fire_and_forget(self._start_karaoke_for_song(ctx, state, song, playback_generation))
            else:
                # Seek attempt: start health check confirmation task
                logger.info(f"[SEEK] Starting health check for seek to {seek}s")
                state.seek_confirm_task = asyncio.create_task(
                    self._seek_confirm_task(ctx, state, song, playback_generation)
                )

        except Exception as exc:
            logger.error(f"Unexpected error in _play_song: {exc}")
            embed = create_embed("Oynatma Hatası", f"Beklenmeyen hata: {exc}", Colors.ERROR)
            try:
                await ctx.channel.send(embed=embed)
            except Exception:
                pass
            await self._handle_after_playing(ctx, None, was_seek_attempt=(seek is not None))

    async def _seek_confirm_task(self, ctx: PlaybackContext, state: GuildMusicState, song: Song, generation: int) -> None:
        """Wait briefly then confirm FFmpeg is actually producing audio.
        Only clears seek state on success; failures are handled by after_callback."""
        try:
            await asyncio.sleep(1.5)

            # Guard against stale checks
            if state.playback_generation != generation:
                logger.info(f"[SEEK] Health check stale (gen {generation} vs {state.playback_generation})")
                return

            if state.current_song is not song:
                logger.info("[SEEK] Health check: song changed")
                return

            vc = get_voice_client(ctx)
            if vc and (vc.is_playing() or vc.is_paused()):
                logger.info(f"[SEEK] Playback health check result: SUCCESS (gen={generation})")
                # Seek is confirmed successful — clear seek state so future callbacks
                # treat this as normal playback.
                state.seek_target = None
                state.seek_retry_count = 0

                # Start the live timer now that playback is confirmed real
                self._cancel_timer_task(state)
                state.timer_task = asyncio.create_task(self._now_playing_updater(ctx, state, song))
                if state.karaoke_enabled:
                    self._fire_and_forget(self._start_karaoke_for_song(ctx, state, song, generation))

                # Refresh the embed to confirm it's live
                try:
                    if state.now_playing_message:
                        embed = self._build_now_playing_embed(state, song)
                        await state.now_playing_message.edit(embed=embed)
                except Exception:
                    pass
            else:
                logger.warning(f"[SEEK] Playback health check result: FAILED (gen={generation})")
                # Do NOT handle failure here — let after_callback be the single source
                # of truth for failure handling to avoid double-processing.
        except asyncio.CancelledError:
            logger.info("[SEEK] Health check cancelled")
            raise
        except Exception as e:
            logger.error(f"[SEEK] Health check error: {e}")

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
            # Cancel any pending confirmation from a previous seek
            if state.seek_confirm_task and not state.seek_confirm_task.done():
                state.seek_confirm_task.cancel()
                state.seek_confirm_task = None

            # Note: we do NOT increment playback_generation here.
            # The stopping callback must be allowed to run so it can trigger the restart.
            state.seek_target = seconds
            state.seek_retry_count = 0
            logger.info(f"[SEEK] Seek requested: {seconds} seconds in guild {ctx.guild.id}")
            if state.karaoke_enabled:
                logger.info(f"[KARAOKE] Seek synchronization: {seconds}s")
            logger.info("[SEEK] Stopping current playback")
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
        state = self._get_state(interaction.guild.id)
        # Invalidate any pending seek to prevent retry loops
        if state.seek_target is not None:
            logger.info(f"[SEEK] Skip requested during seek, clearing seek state (gen {state.playback_generation})")
            state.playback_generation += 1
            state.seek_target = None
            state.seek_retry_count = 0
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
        state = self._get_state(interaction.guild.id)
        state.paused_at = time.monotonic()
        embed = create_embed("Duraklatıldı", "⏸️ Şarkı duraklatıldı.", Colors.WARNING)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="resume", description="Duraklatılmış şarkıyı devam ettir.")
    async def slash_resume(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if not vc or not vc.is_paused():
            embed = create_embed("Hata", "Duraklatılmış şarkı yok.", Colors.ERROR)
            await interaction.response.send_message(embed=embed)
            return
        state = self._get_state(interaction.guild.id)
        if state.paused_at and state.started_at:
            state.playback_offset += state.paused_at - state.started_at
        state.started_at = time.monotonic()
        state.paused_at = 0.0
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
        embed = discord.Embed(title="📋 Şarkı Sırası", description="\ \n".join(lines), color=Colors.INFO)
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

    @app_commands.command(name="karaoke", description="Karaoke modunu aç/kapat (senkronize şarkı sözleri).")
    async def slash_karaoke(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        state = self._get_state(interaction.guild.id)
        if not state.current_song or not vc or not (vc.is_playing() or vc.is_paused()):
            embed = create_embed("Hata", "Şu anda çalan bir şarkı yok.", Colors.ERROR)
            await interaction.response.send_message(embed=embed)
            return

        if state.karaoke_enabled:
            await self._disable_karaoke(state)
            embed = create_embed("Karaoke", "🎤 Karaoke modu kapatıldı.", Colors.WARNING)
            await interaction.response.send_message(embed=embed)
        else:
            embed = create_embed("Karaoke", "🎤 Karaoke modu açıldı.", Colors.SUCCESS)
            await interaction.response.send_message(embed=embed)
            await self._enable_karaoke(interaction, state, interaction.channel)

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
                "`/play <sorgu>` - Şarkı veya Spotify linki oynat / sıraya ekle\ \n"
                "`/playlist <url>` - YouTube playlist'ini sıraya ekle\ \n"
                "`/skip` - Şu an çalan şarkıyı atla\ \n"
                "`/pause` - Şarkıyı duraklat\ \n"
                "`/resume` - Duraklatılmış şarkıyı devam ettir\ \n"
                "`/stop` - Sırayı temizle ve kanaldan ayrıl\ \n"
                "`/loop <mod>` - Döngü modu: tek, sıra, kapat\ \n"
                "`/shuffle` - Kuyruktaki şarkıları karıştır\ \n"
                "`/remove <sıra>` - Kuyruktan şarkı kaldır\ \n"
                "`/seek <saniye>` - Şarkıda ileri/geri sar\ \n"
                "`/karaoke` - Senkronize karaoke sözlerini aç/kapat"
            ),
            inline=False
        )
        embed.add_field(
            name="🔊 Ses & Sıra Yönetimi",
            value=(
                "`/join` - Bulunduğunuz ses kanalına katıl\ \n"
                "`/leave` - Ses kanalından ayrıl\ \n"
                "`/volume <0-200>` - Ses seviyesini ayarla\ \n"
                "`/queue [sayfa]` - Şarkı sırasını göster\ \n"
                "`/nowplaying` - Şu an çalan şarkıyı göster"
            ),
            inline=False
        )
        embed.add_field(
            name="❤️ Favoriler",
            value=(
                "`/favori` - Şu an çalan şarkıyı favorilere ekle\ \n"
                "`/favoriler` - Favori şarkılarını listele\ \n"
                "`/favoriçal` - Favori şarkılarını sıraya ekleyip oynat\ \n"
                "`/favorisil <sıra>` - Favorilerden şarkı sil"
            ),
            inline=False
        )
        embed.add_field(
            name="📀 Kullanıcı Playlistleri",
            value=(
                "`/playlist_oluştur <isim>` - Yeni bir playlist oluştur\ \n"
                "`/playlist_ekle <playlist_id>` - Çalan şarkıyı playlist'e ekle\ \n"
                "`/playlist_queue_kaydet <playlist_id>` - Kuyruktaki şarkıları playlist'e kaydet\ \n"
                "`/playlist_göster <playlist_id> [sayfa]` - Playlist içeriğini göster\ \n"
                "`/playlist_shuffle <playlist_id>` - Playlist'i karıştırarak yükle\ \n"
                "`/playlist_çal <playlist_id>` - Playlist'i sıraya ekleyip oynat\ \n"
                "`/playlist_sil <playlist_id>` - Playlist'i sil\ \n"
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
