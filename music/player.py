import asyncio
import time
import sys
import logging
import random
import re
import difflib
from pathlib import Path
from urllib.parse import urlparse, unquote
from collections import deque
from typing import Optional, Dict, Set, List
import discord
import yt_dlp as youtube_dl

from models import GuildMusicState, Song
from database import DatabaseManager
from audio.filters import AudioFilterBuilder
from stats.manager import StatsManager
from utils.helpers import (
    get_voice_client, create_embed, format_duration,
    resolve_song_url, is_ffmpeg_missing_error, PlaybackContext,
    extract_song_info, build_song
)
from config import FFMPEG_BEFORE_OPTIONS_REMOTE, FFMPEG_BEFORE_OPTIONS_LOCAL, FFMPEG_OPTIONS, YTDLP_OPTIONS, Colors
from exceptions import VoiceError, AudioSourceError, YTDLError

logger = logging.getLogger(__name__)

class MusicPlayer:
    def __init__(self, bot, db: DatabaseManager, ffmpeg_executable: str, karaoke_engine=None):
        self.bot = bot
        self.db = db
        self.stats = StatsManager(db)
        self.ffmpeg_executable = ffmpeg_executable
        self.karaoke_engine = karaoke_engine
        self._states: Dict[int, GuildMusicState] = {}
        self._background_tasks: Set[asyncio.Task] = set()

    def _get_state(self, guild_id: int) -> GuildMusicState:
        if guild_id not in self._states:
            self._states[guild_id] = GuildMusicState()
        return self._states[guild_id]

    def _delete_message_later(self, message: Optional[discord.Message]) -> None:
        if message is None:
            return
        async def _delete():
            try:
                await message.delete()
            except (discord.HTTPException, discord.NotFound, discord.Forbidden):
                pass
        self._fire_and_forget(_delete())

    def _fire_and_forget(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    def _cancel_timer_task(self, state: GuildMusicState) -> None:
        task = state.timer_task
        state.timer_task = None
        if task and not task.done():
            task.cancel()

    def _get_current_position(self, state: GuildMusicState) -> int:
        if not state.started_at:
            return int(state.playback_offset)
        if state.paused_at:
            elapsed = state.paused_at - state.started_at
        else:
            elapsed = time.monotonic() - state.started_at
        return max(0, int(state.playback_offset + elapsed))

    def _build_now_playing_embed(self, state: GuildMusicState, song: Song) -> discord.Embed:
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
        try:
            while True:
                await asyncio.sleep(1.5)
                if state.current_song is not song or state.now_playing_message is None:
                    return
                vc = get_voice_client(ctx)
                if not vc or not (vc.is_playing() or vc.is_paused()):
                    return
                embed = self._build_now_playing_embed(state, song)
                try:
                    await state.now_playing_message.edit(embed=embed)
                except discord.NotFound:
                    return
                except discord.HTTPException as e:
                    logger.warning(f"Now Playing mesajı güncellenemedi: {e}")
                    return
        except asyncio.CancelledError:
            raise

    async def _play_song(self, ctx: PlaybackContext, song: Song, seek: Optional[int] = None) -> None:
        vc = get_voice_client(ctx)
        if not vc:
            state = self._get_state(ctx.guild.id)
            state.active = False
            raise VoiceError("Bot ses kanalında değil.")

        state = self._get_state(ctx.guild.id)
        playback_generation = state.playback_generation

        # Clean up old now-playing message and timer
        if state.now_playing_message:
            self._delete_message_later(state.now_playing_message)
            state.now_playing_message = None
        self._cancel_timer_task(state)

        # Cancel any stale seek confirm task
        if state.seek_confirm_task and not state.seek_confirm_task.done():
            state.seek_confirm_task.cancel()
            state.seek_confirm_task = None

        # Karaoke cleanup
        if self.karaoke_engine:
            self.karaoke_engine._cancel_karaoke_task(state)
            if seek is None:
                self.karaoke_engine._cancel_karaoke_fetch(state)
                state.lyrics_track = None
                state.lyrics_song = None
                state.karaoke_active_index = -1
                await self.karaoke_engine.delete_karaoke_message(state)
            else:
                await self.karaoke_engine.delete_karaoke_message(state)

        try:
            # ---- RESOLVE STREAM URL ----
            # Local files never go through yt-dlp. Remote songs are ALWAYS
            # re-resolved right before actually playing - whether this is a
            # fresh play, a queue advance, or a seek - instead of only when
            # source_url happens to be empty. A Song's source_url can be
            # minutes or hours old by the time it's actually dequeued in a
            # long queue, and YouTube's signed CDN URLs expire, so reusing a
            # stale one silently fails the song. This also makes normal
            # playback and seek behave the same way instead of diverging.
            is_local = song.url.startswith("file://") or (song.source_url and song.source_url.startswith("file://"))
            if not is_local:
                try:
                    song.source_url = await resolve_song_url(song.url, asyncio.get_running_loop())
                except YTDLError as exc:
                    logger.warning(f"URL resolve failed for '{song.title}': {exc}")
                    embed = create_embed(
                        "Atlandı", f"**{song.title}** oynatılamadı, sıradakine geçiliyor.\\n`{exc}`", Colors.WARNING
                    )
                    await ctx.channel.send(embed=embed)
                    await self._handle_after_playing(ctx, None, was_seek_attempt=False)
                    return

            # ---- NORMALIZE LOCAL INPUT / BUILD FFMPEG OPTIONS ----
            # Keep local files out of yt-dlp and pass FFmpeg a real filesystem path.
            source_input = song.source_url or song.url
            if is_local and source_input.startswith("file://"):
                parsed = urlparse(source_input)
                source_input = unquote(parsed.path)
                # On Windows file:///C:/... is parsed as /C:/...
                if len(source_input) >= 3 and source_input[0] == "/" and source_input[2] == ":":
                    source_input = source_input[1:]
                source_input = str(Path(source_input))

            start_time = seek if seek is not None else 0
            filter_str = AudioFilterBuilder.build(
                speed=state.speed,
                bass_boost=state.bass_boost,
                eightd=state.eightd,
                crossfade_duration=state.crossfade_duration,
                song_duration=song.duration,
                start_time=start_time
            )

            # For remote streams, input seeking is more reliable when -ss is placed
            # before -i (FFmpegPCMAudio's before_options).  Re-resolving above gives
            # each seek a fresh signed CDN URL instead of reusing a stale one.
            before_options = FFMPEG_BEFORE_OPTIONS_LOCAL if is_local else FFMPEG_BEFORE_OPTIONS_REMOTE
            options = FFMPEG_OPTIONS
            if seek is not None and seek > 0:
                before_options = f"{before_options} -ss {float(seek):.3f}".strip()
            if filter_str:
                options += f" -af {filter_str}"

            # ---- CREATE FFMPEG SOURCE ----
            try:
                source = discord.FFmpegPCMAudio(
                    source_input,
                    executable=self.ffmpeg_executable,
                    before_options=before_options,
                    options=options,
                    stderr=sys.stderr,
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
            state.played_any = False  # Reset per playback attempt - this flag must reflect
            # whether *this* attempt reached the "genuinely playing" threshold, not
            # whatever the previous song happened to achieve. Without this reset it
            # stays True forever after the first successful song in a guild, which lets
            # a song that fails immediately after this point (e.g. vc.play() raising)
            # be falsely recorded as "played" in _handle_after_playing's stats block.

            # ---- AFTER CALLBACK ----
            def after_callback(error: Optional[Exception]) -> None:
                if state.playback_generation != playback_generation:
                    logger.info(f"[SEEK] Stale after_callback ignored (gen {playback_generation} vs current {state.playback_generation})")
                    return
                if state.current_song is None:
                    logger.info("[SEEK] After callback: intentional stop detected (current_song is None)")
                    return
                was_seek_attempt = state.seek_target is not None
                if error:
                    logger.error(f"[SEEK] After callback error: {error}")
                else:
                    elapsed = time.monotonic() - state.started_at if state.started_at else 0.0
                    if elapsed < 2.0:
                        error = AudioSourceError(
                            f"Ses akışı çok erken kapandı ({elapsed:.1f} sn)."
                        )
                        logger.warning(f"[SEEK] Early termination after {elapsed:.1f}s")
                    else:
                        state.played_any = True
                        logger.info(f"[SEEK] Playback completed normally after {elapsed:.1f}s")
                coro = self._handle_after_playing(ctx, error, was_seek_attempt=was_seek_attempt)
                asyncio.run_coroutine_threadsafe(coro, self.bot.loop)

            # ---- STOP PREVIOUS PLAYBACK ----
            if vc.is_playing() or vc.is_paused():
                vc.stop()
                # Wait for the old process to shut down
                await asyncio.sleep(0.5)

            if state.playback_generation != playback_generation:
                logger.info(f"[SEEK] Generation changed during setup, aborting start (gen {playback_generation})")
                return

            # ---- START NEW PLAYBACK ----
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

            # ---- SEND NOW PLAYING MESSAGE ----
            from ui.controls import MusicControlView
            embed = self._build_now_playing_embed(state, song)
            view = MusicControlView(self, ctx)
            msg = await ctx.channel.send(embed=embed, view=view)
            state.now_playing_message = msg
            logger.info(f"Playing '{song.title}' in guild {ctx.guild.id}")

            # ---- HEALTH CHECK (only for seek) ----
            is_seek_attempt = seek is not None
            if not is_seek_attempt:
                self._cancel_timer_task(state)
                state.timer_task = asyncio.create_task(self._now_playing_updater(ctx, state, song))
                if state.karaoke_enabled and self.karaoke_engine:
                    self._fire_and_forget(self.karaoke_engine.start_karaoke_for_song(ctx, state, song, playback_generation))
            else:
                logger.info(f"[SEEK] Starting health check for seek to {seek}s")
                if state.seek_confirm_task and not state.seek_confirm_task.done():
                    state.seek_confirm_task.cancel()
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
        try:
            await asyncio.sleep(2.0)  # Daha uzun bekle
            if state.playback_generation != generation:
                logger.info(f"[SEEK] Health check stale (gen {generation} vs current {state.playback_generation})")
                return
            if state.current_song is not song:
                logger.info("[SEEK] Health check: song changed")
                return
            vc = get_voice_client(ctx)
            if vc and (vc.is_playing() or vc.is_paused()):
                logger.info(f"[SEEK] Playback health check result: SUCCESS (gen={generation})")
                state.seek_target = None
                state.seek_retry_count = 0
                state.is_effect_restart = False
                self._cancel_timer_task(state)
                state.timer_task = asyncio.create_task(self._now_playing_updater(ctx, state, song))
                if state.karaoke_enabled and self.karaoke_engine:
                    self._fire_and_forget(self.karaoke_engine.start_karaoke_for_song(ctx, state, song, generation))
                try:
                    if state.now_playing_message:
                        embed = self._build_now_playing_embed(state, song)
                        await state.now_playing_message.edit(embed=embed)
                except Exception:
                    pass
            else:
                logger.warning(f"[SEEK] Playback health check result: FAILED (gen={generation})")
        except asyncio.CancelledError:
            logger.info("[SEEK] Health check cancelled")
            raise
        except Exception as e:
            logger.error(f"[SEEK] Health check error: {e}")

    async def _handle_after_playing(
        self, ctx: PlaybackContext, error: Optional[Exception], was_seek_attempt: bool
    ) -> None:
        state = self._get_state(ctx.guild.id)
        self._cancel_timer_task(state)
        if self.karaoke_engine:
            self.karaoke_engine._cancel_karaoke_task(state)

        # Record stats if playback finished without error and we played a significant portion
        if not error and state.current_song and state.played_any:
            song = state.current_song
            duration_played = self._get_current_position(state)
            if song.duration and duration_played > song.duration:
                duration_played = song.duration
            if duration_played > 0 and song.requester:
                await self.stats.record_play(
                    song.requester.id,
                    ctx.guild.id,
                    song,
                    duration_played
                )
            state.history.append(song)
            logger.info(f"Stats recorded for {song.title} by user {song.requester}")

        if error:
            logger.error(f"Playback error in guild {ctx.guild.id}: {error}")

            if was_seek_attempt and state.is_effect_restart and state.seek_retry_count < 1 and state.current_song is not None:
                # This restart was triggered by an effect change (bass boost /
                # 8D / speed), not a user /seek. Retrying with the exact same
                # filter chain would just fail again and, if left alone, would
                # keep failing on every subsequent song too (since the broken
                # effect stays enabled) - eventually tripping the failed_count
                # guard and killing the whole queue. Instead, revert the
                # guild's effects to safe defaults and retry once with a clean
                # filter chain.
                logger.warning(f"[EFFECT] Restart failed after an effect change in guild {ctx.guild.id}; reverting effects to defaults.")
                state.bass_boost = 0
                state.eightd = False
                state.speed = 1.0
                state.is_effect_restart = False
                state.seek_retry_count += 1
                seek_target = state.seek_target
                song = state.current_song
                state.playback_generation += 1
                try:
                    embed = create_embed(
                        "Efekt Hatası",
                        "Uygulanan ses efekti oynatmayı bozdu; efektler sıfırlandı ve normal oynatmaya devam ediliyor.",
                        Colors.WARNING,
                    )
                    await ctx.channel.send(embed=embed)
                except Exception:
                    pass
                await self._play_song(ctx, song, seek=seek_target)
                return

            # Retry seek if it was a genuine user seek attempt and we have retries left
            if was_seek_attempt and not state.is_effect_restart and state.seek_retry_count < 2 and state.current_song is not None:
                state.seek_retry_count += 1
                seek_target = state.seek_target
                song = state.current_song
                if seek_target is None:
                    logger.warning("[SEEK] Retry cancelled because the target is no longer valid")
                else:
                    if state.seek_confirm_task and not state.seek_confirm_task.done():
                        state.seek_confirm_task.cancel()
                        state.seek_confirm_task = None
                    # Give the retry its own generation so callbacks/tasks from the failed
                    # FFmpeg instance can never advance or stop the replacement playback.
                    state.playback_generation += 1
                    logger.info(f"[SEEK] Retrying seek to {seek_target}s (attempt {state.seek_retry_count})")
                    await self._play_song(ctx, song, seek=seek_target)
                    return

            # If not seek or retries exhausted, show error and continue
            state.failed_count += 1
            try:
                if was_seek_attempt:
                    embed = create_embed(
                        "Sarma Hatası",
                        "Şarkıda sarma işlemi başarısız oldu, normal oynatmaya devam ediliyor.",
                        Colors.ERROR,
                    )
                else:
                    title = state.current_song.title if state.current_song else "Şarkı"
                    embed = create_embed(
                        "Oynatma Hatası",
                        f"**{title}** çalınırken bir sorun oluştu, sıradakine geçiliyor.",
                        Colors.ERROR,
                    )
                await ctx.channel.send(embed=embed)
            except Exception:
                pass
        else:
            state.failed_count = 0

        state.seek_target = None
        state.seek_retry_count = 0
        state.is_effect_restart = False
        # Cancel any stale health check
        if state.seek_confirm_task and not state.seek_confirm_task.done():
            state.seek_confirm_task.cancel()
            state.seek_confirm_task = None

        if state.failed_count >= 5:
            logger.error(f"[GUARD] Too many consecutive failures in guild {ctx.guild.id}, stopping playback.")
            state.failed_count = 0
            state.current_song = None
            state.active = False
            try:
                embed = create_embed(
                    "Durduruldu",
                    "Ardarda çok fazla oynatma hatası oluştu, oynatma durduruldu.",
                    Colors.ERROR,
                )
                await ctx.channel.send(embed=embed)
            except Exception:
                pass
            return

        prev_song = state.current_song
        state.current_song = None

        next_song: Optional[Song] = None
        if state.loop_current and prev_song and not error:
            next_song = prev_song
        else:
            if state.loop_queue and prev_song:
                async with state.lock:
                    state.queue.append(prev_song)
            async with state.lock:
                if state.queue:
                    next_song = state.queue.popleft()

        # Autoplay if queue empty and enabled
        if next_song is None and state.autoplay and prev_song:
            similar = await self._fetch_similar_songs(ctx, prev_song, limit=1)
            if similar:
                async with state.lock:
                    state.queue.extend(similar)
                next_song = similar[0]
                logger.info(f"Autoplay: queued {next_song.title}")

        if next_song is None:
            state.active = False
            logger.info(f"Queue finished in guild {ctx.guild.id}")
            return

        state.active = True
        state.playback_generation += 1
        await self._play_song(ctx, next_song)

    async def _fetch_similar_songs(self, ctx: PlaybackContext, base_song: Song, limit: int = 5) -> List[Song]:
        music_cog = ctx.bot.get_cog("Music") if hasattr(ctx, 'bot') else None
        if music_cog and music_cog.spotify and base_song.spotify_id:
            try:
                recs = await music_cog.spotify.get_recommendations(seed_tracks=[base_song.spotify_id], limit=limit)
                songs = []
                for track in recs:
                    youtube_url = await music_cog.spotify.search_youtube_for_track(track["name"], track["artists"][0]["name"])
                    if youtube_url:
                        info = await extract_song_info(youtube_url, asyncio.get_running_loop())
                        song = build_song(info, None)
                        songs.append(song)
                return songs[:limit]
            except Exception as e:
                logger.warning(f"Spotify recommendations failed: {e}")

        # YouTube fallback
        def clean_title(title: str) -> str:
            title = re.sub(r'\([^)]*\)', '', title)
            title = re.sub(r'\[[^\]]*\]', '', title)
            title = re.sub(r'\b(official|music|video|lyrics|audio|hd|4k|remix|cover|live|version|concert|interview|full)\b', '', title, flags=re.IGNORECASE)
            title = re.sub(r'\s+', ' ', title).strip()
            return title

        def is_live_or_long(title: str, duration: int) -> bool:
            if duration and duration > 600:
                return True
            title_low = title.lower()
            if any(word in title_low for word in ['live', 'concert', 'interview', 'full album', 'documentary', 'set']):
                return True
            return False

        clean_base_title = clean_title(base_song.title)
        artist = base_song.uploader or ""
        query = f"{clean_base_title} {artist} song"
        logger.info(f"[SIMILAR] Searching with query: {query}")

        try:
            ytdl = youtube_dl.YoutubeDL(YTDLP_OPTIONS)
            loop = asyncio.get_running_loop()
            data = await loop.run_in_executor(
                None,
                lambda: ytdl.extract_info(f"ytsearch{limit * 3}:{query}", download=False)
            )
            entries = data.get("entries", [])
            if not entries:
                return []

            seen = set()
            result_songs = []
            base_title_norm = clean_title(base_song.title).lower()

            for entry in entries:
                entry_title = entry.get("title", "")
                entry_uploader = entry.get("uploader", entry.get("channel", ""))
                duration = entry.get("duration")
                if not entry_title:
                    continue

                if is_live_or_long(entry_title, duration):
                    continue

                clean_entry_title = clean_title(entry_title).lower()
                similarity = difflib.SequenceMatcher(None, clean_entry_title, base_title_norm).ratio()
                if similarity > 0.8:
                    continue

                key = (clean_entry_title, entry_uploader.lower())
                if key in seen:
                    continue
                seen.add(key)

                song = build_song(entry, None)
                if song.url:
                    result_songs.append(song)

                if len(result_songs) >= limit:
                    break

            return result_songs

        except Exception as e:
            logger.warning(f"Similar songs fetch failed: {e}")
            return []

    async def previous(self, ctx: PlaybackContext) -> bool:
        state = self._get_state(ctx.guild.id)
        if not state.history:
            return False
        prev_song = state.history.pop()
        state.queue.appendleft(prev_song)
        if state.active:
            vc = get_voice_client(ctx)
            if vc:
                vc.stop()
        else:
            await self._maybe_start_playback(ctx)
        return True

    async def replay(self, ctx: PlaybackContext) -> bool:
        state = self._get_state(ctx.guild.id)
        if not state.current_song:
            return False
        song = state.current_song
        state.playback_generation += 1
        await self._play_song(ctx, song, seek=0)
        return True

    async def shuffle_queue(self, ctx: PlaybackContext) -> bool:
        state = self._get_state(ctx.guild.id)
        async with state.lock:
            if len(state.queue) < 2:
                return False
            items = list(state.queue)
            random.shuffle(items)
            state.queue.clear()
            state.queue.extend(items)
        return True

    async def remove_duplicates(self, ctx: PlaybackContext) -> int:
        state = self._get_state(ctx.guild.id)
        async with state.lock:
            seen = set()
            new_queue = deque()
            removed = 0
            for song in state.queue:
                key = song.url or song.title
                if key in seen:
                    removed += 1
                else:
                    seen.add(key)
                    new_queue.append(song)
            state.queue = new_queue
        return removed

    async def get_suggestions(self, user_id: int, limit: int = 5) -> List[Song]:
        rows = await self.db.get_user_top_songs(user_id, limit)
        songs = []
        for item in rows:
            song = Song(
                source_url="",
                title=item.get("title", "Unknown"),
                url=item.get("url", ""),
                duration=None,
                uploader=item.get("artist", ""),
            )
            songs.append(song)
        return songs

    async def _maybe_start_playback(self, ctx: PlaybackContext) -> None:
        state = self._get_state(ctx.guild.id)
        vc = get_voice_client(ctx)
        if not vc:
            return
        if state.active or vc.is_playing() or vc.is_paused():
            return
        async with state.lock:
            if not state.queue:
                return
            song = state.queue.popleft()
        state.active = True
        state.playback_generation += 1
        await self._play_song(ctx, song)

    async def ensure_voice(self, ctx: PlaybackContext) -> bool:
        guild = ctx.guild
        if guild is None:
            return False
        user = ctx.user if isinstance(ctx, discord.Interaction) else ctx.author
        voice_state = getattr(user, "voice", None)
        if voice_state is None or voice_state.channel is None:
            return False
        target_channel = voice_state.channel
        vc = guild.voice_client
        try:
            if vc is None:
                await target_channel.connect(self_deaf=True)
            elif vc.channel and vc.channel.id != target_channel.id:
                await vc.move_to(target_channel)
        except discord.ClientException as e:
            logger.warning(f"ensure_voice: {e}")
            return False
        except Exception as e:
            logger.error(f"ensure_voice failed: {e}")
            return False
        return True

    async def perform_seek(self, ctx: PlaybackContext, seconds: int, is_effect_restart: bool = False) -> None:
        state = self._get_state(ctx.guild.id)
        if not state.current_song:
            raise VoiceError("Şu anda çalan bir şarkı yok.")
        if seconds < 0:
            raise VoiceError("Saniye negatif olamaz.")
        if state.current_song.duration and seconds > state.current_song.duration:
            raise VoiceError("Belirtilen süre şarkının toplam süresini aşıyor.")
        vc = get_voice_client(ctx)
        if not vc or not (vc.is_playing() or vc.is_paused()):
            raise VoiceError("Şu anda oynatma yapılmıyor.")

        song = state.current_song
        state.seek_target = seconds
        state.seek_retry_count = 0
        state.is_effect_restart = is_effect_restart
        state.playback_generation += 1
        logger.info(f"[SEEK] Seek requested to {seconds}s in guild {ctx.guild.id} (effect_restart={is_effect_restart})")
        await self._play_song(ctx, song, seek=seconds)

    async def apply_audio_settings(self, ctx: PlaybackContext) -> bool:
        """Restarts the current song's FFmpeg source (via a same-position
        seek) so a changed effect setting (bass boost / 8D / speed /
        crossfade) takes effect immediately. Returns True if a restart was
        actually attempted (i.e. something is currently playing) and it
        appears to have started without immediately erroring out; False if
        nothing is playing (the new settings still apply automatically to
        the next song, since _play_song always reads state fresh) or the
        restart could not even be started. This lets command handlers report
        the real outcome instead of a hard-coded success message before the
        restart has actually happened."""
        state = self._get_state(ctx.guild.id)
        if not state.current_song:
            return False
        vc = get_voice_client(ctx)
        if not vc or not (vc.is_playing() or vc.is_paused()):
            return False
        position = self._get_current_position(state)
        try:
            await self.perform_seek(ctx, position, is_effect_restart=True)
        except VoiceError as e:
            logger.warning(f"[EFFECT] apply_audio_settings restart failed: {e}")
            return False
        return True

    async def enqueue_songs(self, ctx: PlaybackContext, songs: List[Song]) -> None:
        """Appends songs to the guild's queue, honoring autoshuffle if it's
        enabled for that guild. This is the one place queue insertion should
        happen so autoshuffle behavior is consistent everywhere songs get
        added, instead of being (or not being) re-implemented per command."""
        if not songs:
            return
        state = self._get_state(ctx.guild.id)
        async with state.lock:
            state.queue.extend(songs)
            if state.autoshuffle and len(state.queue) > 1:
                items = list(state.queue)
                random.shuffle(items)
                state.queue.clear()
                state.queue.extend(items)