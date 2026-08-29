from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional, Deque, List
from collections import deque
import asyncio
import discord

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
        idx = -1
        for i, line in enumerate(self.lines):
            if line.timestamp <= position:
                idx = i
            else:
                break
        return idx

class GuildMusicState:
    def __init__(self) -> None:
        self.queue: Deque[Song] = deque()
        self.lock = asyncio.Lock()
        self.loop_current: bool = False
        self.loop_queue: bool = False
        self.current_song: Optional[Song] = None
        self.volume: float = 0.5
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

        # Karaoke
        self.karaoke_enabled: bool = False
        self.lyrics_track: Optional[LyricsTrack] = None
        self.lyrics_song: Optional[Song] = None
        self.karaoke_active_index: int = -1
        self.karaoke_message: Optional[discord.Message] = None
        self.karaoke_task: Optional[asyncio.Task] = None
        self.karaoke_fetch_task: Optional[asyncio.Task] = None

        # New features
        self.history: Deque[Song] = deque(maxlen=20)
        self.autoplay: bool = False
        self.autoshuffle: bool = False
        self.crossfade_duration: float = 0.0          # seconds, 0 = off
        self.bass_boost: int = 0                      # 0–10
        self.eightd: bool = False
        self.speed: float = 1.0                       # 0.5–2.0
        # True only while a restart triggered by an effect change (bass
        # boost/8D/speed) is in flight, as opposed to a user-initiated /seek.
        # Lets _handle_after_playing tell the two apart so a broken filter
        # doesn't get treated - and retried - like a normal failed seek.
        self.is_effect_restart: bool = False