import json
from pathlib import Path
from typing import Optional, Dict, Any, List, Union
import aiosqlite
import logging
from datetime import datetime, timezone

from models import Song, LyricsTrack, LyricLine
from config import NEGATIVE_CACHE_TTL

logger = logging.getLogger(__name__)


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
            await db.execute("""
                CREATE TABLE IF NOT EXISTS user_stats (
                    user_id INTEGER PRIMARY KEY,
                    total_seconds INTEGER DEFAULT 0,
                    songs_played INTEGER DEFAULT 0
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS server_stats (
                    guild_id INTEGER PRIMARY KEY,
                    total_seconds INTEGER DEFAULT 0
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS global_stats (
                    id INTEGER PRIMARY KEY CHECK (id=1),
                    total_seconds INTEGER DEFAULT 0
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS song_stats (
                    url TEXT PRIMARY KEY,
                    title TEXT,
                    artist TEXT,
                    play_count INTEGER DEFAULT 0,
                    total_seconds INTEGER DEFAULT 0
                )
            """)
            # Per-guild song stats - song_stats above is global-only (no
            # guild_id), which meant /servertop had no real per-server data
            # to query and was silently showing global numbers instead. This
            # table is what actual guild-scoped "most played" comes from.
            await db.execute("""
                CREATE TABLE IF NOT EXISTS guild_song_stats (
                    guild_id INTEGER NOT NULL,
                    url TEXT NOT NULL,
                    title TEXT,
                    artist TEXT,
                    play_count INTEGER DEFAULT 0,
                    total_seconds INTEGER DEFAULT 0,
                    PRIMARY KEY (guild_id, url)
                )
            """)
            # Per-user song stats - used for real personalized suggestions.
            # Previously get_user_top_songs() had nowhere to read real
            # per-user data from and fell back to returning the global top
            # songs, which is not a personalized result at all.
            await db.execute("""
                CREATE TABLE IF NOT EXISTS user_song_stats (
                    user_id INTEGER NOT NULL,
                    url TEXT NOT NULL,
                    title TEXT,
                    artist TEXT,
                    play_count INTEGER DEFAULT 0,
                    total_seconds INTEGER DEFAULT 0,
                    PRIMARY KEY (user_id, url)
                )
            """)
            # Global stats başlangıç satırı
            await db.execute(
                "INSERT OR IGNORE INTO global_stats (id, total_seconds) VALUES (1, 0)"
            )
            await db.commit()

    @staticmethod
    def _row_to_song(row: aiosqlite.Row) -> Song:
        return Song(
            source_url="",
            title=row["song_title"],
            url=row["song_url"],
            duration=row["song_duration"],
            thumbnail=row["song_thumbnail"],
            uploader=row["song_uploader"],
        )

    # ------------------------------------------------------------------
    # Favorites
    # ------------------------------------------------------------------
    async def add_favorite(self, user_id: int, song: Song) -> bool:
        """Add a song to a user's favorites. Returns False if it's already there."""
        async with aiosqlite.connect(self.db_path) as db:
            try:
                await db.execute(
                    """INSERT INTO favorites
                       (user_id, song_title, song_url, song_duration, song_thumbnail, song_uploader)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (user_id, song.title, song.url, song.duration, song.thumbnail, song.uploader),
                )
                await db.commit()
                return True
            except aiosqlite.IntegrityError:
                return False

    async def get_favorites(self, user_id: int) -> List[Song]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT song_title, song_url, song_duration, song_thumbnail, song_uploader
                   FROM favorites WHERE user_id = ? ORDER BY added_at DESC""",
                (user_id,),
            ) as cursor:
                rows = await cursor.fetchall()
        return [self._row_to_song(row) for row in rows]

    async def remove_favorite(self, user_id: int, url: str) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "DELETE FROM favorites WHERE user_id = ? AND song_url = ?", (user_id, url)
            )
            await db.commit()
            return cursor.rowcount > 0

    # ------------------------------------------------------------------
    # Playlists
    # ------------------------------------------------------------------
    async def create_playlist(self, name: str, owner_id: int) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "INSERT INTO user_playlists (name, owner_id) VALUES (?, ?)", (name, owner_id)
            )
            await db.commit()
            return cursor.lastrowid

    async def get_playlists(self, owner_id: int) -> List[Dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT id, name, owner_id, created_at FROM user_playlists WHERE owner_id = ? ORDER BY created_at DESC",
                (owner_id,),
            ) as cursor:
                rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_playlist_songs(self, playlist_id: int) -> List[Song]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT song_title, song_url, song_duration, song_thumbnail, song_uploader
                   FROM playlist_songs WHERE playlist_id = ? ORDER BY position ASC""",
                (playlist_id,),
            ) as cursor:
                rows = await cursor.fetchall()
        return [self._row_to_song(row) for row in rows]

    async def add_song_to_playlist(self, playlist_id: int, song: Song, position: int) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT INTO playlist_songs
                   (playlist_id, song_title, song_url, song_duration, song_thumbnail, song_uploader, position)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (playlist_id, song.title, song.url, song.duration, song.thumbnail, song.uploader, position),
            )
            await db.commit()

    async def delete_playlist(self, playlist_id: int, owner_id: int) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "DELETE FROM user_playlists WHERE id = ? AND owner_id = ?", (playlist_id, owner_id)
            )
            await db.commit()
            return cursor.rowcount > 0

    async def remove_song_from_playlist(self, playlist_id: int, position_1_indexed: int) -> bool:
        """Remove the song at the given 1-indexed display position (matching the
        order returned by get_playlist_songs) and keep remaining positions contiguous."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT rowid, position FROM playlist_songs WHERE playlist_id = ? ORDER BY position ASC",
                (playlist_id,),
            ) as cursor:
                rows = await cursor.fetchall()

            if position_1_indexed < 1 or position_1_indexed > len(rows):
                return False

            target = rows[position_1_indexed - 1]
            await db.execute("DELETE FROM playlist_songs WHERE rowid = ?", (target["rowid"],))

            remaining = [r for r in rows if r["rowid"] != target["rowid"]]
            for idx, r in enumerate(remaining):
                if r["position"] != idx:
                    await db.execute(
                        "UPDATE playlist_songs SET position = ? WHERE rowid = ?", (idx, r["rowid"])
                    )
            await db.commit()
            return True

    # ------------------------------------------------------------------
    # Lyrics cache
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_ts(value: Optional[str]) -> Optional[datetime]:
        if not value:
            return None
        try:
            return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except ValueError:
            return None

    async def get_cached_lyrics(self, cache_key: str) -> Optional[Union[LyricsTrack, str]]:
        """Returns a LyricsTrack on a positive cache hit, the string "NONE" on a
        still-valid negative cache hit, or None if there's no usable cache entry."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM lyrics_cache WHERE cache_key = ?", (cache_key,)
            ) as cursor:
                row = await cursor.fetchone()

        if row is None:
            return None

        if not row["found"]:
            cached_at = self._parse_ts(row["cached_at"])
            if cached_at is not None:
                age = (datetime.now(timezone.utc) - cached_at).total_seconds()
                if age > NEGATIVE_CACHE_TTL:
                    return None  # Expired negative cache entry - allow a retry.
            return "NONE"

        try:
            lines_data = json.loads(row["lines_json"] or "[]")
            lines = [
                LyricLine(timestamp=item["timestamp"], text=item["text"], index=item["index"])
                for item in lines_data
            ]
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.warning(f"Lyrics cache entry could not be parsed, ignoring: {e}")
            return None

        return LyricsTrack(
            title=row["title"] or "",
            artist=row["artist"] or "",
            duration=row["duration"],
            lines=lines,
        )

    async def cache_lyrics(self, cache_key: str, track: LyricsTrack) -> None:
        lines_json = json.dumps(
            [{"timestamp": l.timestamp, "text": l.text, "index": l.index} for l in track.lines]
        )
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT INTO lyrics_cache (cache_key, title, artist, duration, lines_json, found, cached_at)
                   VALUES (?, ?, ?, ?, ?, 1, CURRENT_TIMESTAMP)
                   ON CONFLICT(cache_key) DO UPDATE SET
                       title = excluded.title,
                       artist = excluded.artist,
                       duration = excluded.duration,
                       lines_json = excluded.lines_json,
                       found = 1,
                       cached_at = CURRENT_TIMESTAMP""",
                (cache_key, track.title, track.artist, track.duration, lines_json),
            )
            await db.commit()

    async def cache_lyrics_negative(self, cache_key: str) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT INTO lyrics_cache (cache_key, title, artist, duration, lines_json, found, cached_at)
                   VALUES (?, NULL, NULL, NULL, NULL, 0, CURRENT_TIMESTAMP)
                   ON CONFLICT(cache_key) DO UPDATE SET
                       found = 0,
                       cached_at = CURRENT_TIMESTAMP""",
                (cache_key,),
            )
            await db.commit()

    # ------------------------------------------------------------------
    # Statistics
    # ------------------------------------------------------------------
    async def update_stats(
        self,
        user_id: int,
        guild_id: int,
        song: Song,
        duration_played: int
    ) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            # User
            await db.execute(
                """INSERT INTO user_stats (user_id, total_seconds, songs_played)
                   VALUES (?, ?, 1)
                   ON CONFLICT(user_id) DO UPDATE SET
                       total_seconds = total_seconds + excluded.total_seconds,
                       songs_played = songs_played + 1""",
                (user_id, duration_played)
            )
            # Server
            await db.execute(
                """INSERT INTO server_stats (guild_id, total_seconds)
                   VALUES (?, ?)
                   ON CONFLICT(guild_id) DO UPDATE SET
                       total_seconds = total_seconds + excluded.total_seconds""",
                (guild_id, duration_played)
            )
            # Global
            await db.execute(
                """UPDATE global_stats SET total_seconds = total_seconds + ? WHERE id = 1""",
                (duration_played,)
            )
            # Song (global)
            await db.execute(
                """INSERT INTO song_stats (url, title, artist, play_count, total_seconds)
                   VALUES (?, ?, ?, 1, ?)
                   ON CONFLICT(url) DO UPDATE SET
                       play_count = play_count + 1,
                       total_seconds = total_seconds + excluded.total_seconds""",
                (song.url, song.title, song.uploader, duration_played)
            )
            # Song (this guild only) - what /servertop actually reads from.
            await db.execute(
                """INSERT INTO guild_song_stats (guild_id, url, title, artist, play_count, total_seconds)
                   VALUES (?, ?, ?, ?, 1, ?)
                   ON CONFLICT(guild_id, url) DO UPDATE SET
                       play_count = play_count + 1,
                       total_seconds = total_seconds + excluded.total_seconds""",
                (guild_id, song.url, song.title, song.uploader, duration_played)
            )
            # Song (this user only) - real data for personalized suggestions.
            await db.execute(
                """INSERT INTO user_song_stats (user_id, url, title, artist, play_count, total_seconds)
                   VALUES (?, ?, ?, ?, 1, ?)
                   ON CONFLICT(user_id, url) DO UPDATE SET
                       play_count = play_count + 1,
                       total_seconds = total_seconds + excluded.total_seconds""",
                (user_id, song.url, song.title, song.uploader, duration_played)
            )
            await db.commit()

    async def get_user_stats(self, user_id: int) -> Dict[str, int]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT total_seconds, songs_played FROM user_stats WHERE user_id = ?",
                (user_id,)
            ) as cursor:
                row = await cursor.fetchone()
        if row:
            return {"total_seconds": row["total_seconds"], "songs_played": row["songs_played"]}
        return {"total_seconds": 0, "songs_played": 0}

    async def get_server_stats(self, guild_id: int) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT total_seconds FROM server_stats WHERE guild_id = ?",
                (guild_id,)
            ) as cursor:
                row = await cursor.fetchone()
        return row["total_seconds"] if row else 0

    async def get_global_stats(self) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT total_seconds FROM global_stats WHERE id = 1"
            ) as cursor:
                row = await cursor.fetchone()
        return row["total_seconds"] if row else 0

    async def get_top_songs_global(self, limit: int = 10) -> List[Dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT title, artist, play_count, total_seconds
                   FROM song_stats
                   ORDER BY total_seconds DESC
                   LIMIT ?""",
                (limit,)
            ) as cursor:
                rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_top_songs_guild(self, guild_id: int, limit: int = 10) -> List[Dict[str, Any]]:
        """Real per-guild "most played" data - independent of global stats,
        so one server's numbers never bleed into another's."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT title, artist, play_count, total_seconds
                   FROM guild_song_stats
                   WHERE guild_id = ?
                   ORDER BY total_seconds DESC
                   LIMIT ?""",
                (guild_id, limit)
            ) as cursor:
                rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_user_top_songs(self, user_id: int, limit: int = 10) -> List[Dict[str, Any]]:
        """Real per-user listening data, used as the seed for personalized
        suggestions. This used to just return the global top songs (with a
        comment admitting there was no real per-user table), which isn't a
        personalized result at all - it's the same list every user would see."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT title, artist, url, play_count, total_seconds
                   FROM user_song_stats
                   WHERE user_id = ?
                   ORDER BY play_count DESC, total_seconds DESC
                   LIMIT ?""",
                (user_id, limit)
            ) as cursor:
                rows = await cursor.fetchall()
        return [dict(row) for row in rows]