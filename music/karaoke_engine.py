import asyncio
import logging
from typing import Optional
import discord
from models import GuildMusicState, Song, LyricsTrack
from database import DatabaseManager
from karaoke.service import LyricsService
from utils.helpers import create_embed, format_duration, PlaybackContext, get_voice_client
from config import Colors

logger = logging.getLogger(__name__)

class KaraokeEngine:
    def __init__(self, player, db: DatabaseManager, lyrics_service: LyricsService):
        self.player = player
        self.db = db
        self.lyrics_service = lyrics_service

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

    async def delete_karaoke_message(self, state: GuildMusicState) -> None:
        if state.karaoke_message is None:
            return
        try:
            await state.karaoke_message.delete()
            logger.info("[KARAOKE] Previous karaoke message deleted successfully")
        except discord.NotFound:
            logger.info("[KARAOKE] Previous karaoke message already deleted")
        except discord.Forbidden:
            logger.warning("[KARAOKE] No permission to delete karaoke message")
        except discord.HTTPException as e:
            logger.warning(f"[KARAOKE] Failed to delete karaoke message: {e}")
        finally:
            state.karaoke_message = None

    async def disable_karaoke(self, state: GuildMusicState) -> None:
        state.karaoke_enabled = False
        self._cancel_karaoke_task(state)
        self._cancel_karaoke_fetch(state)
        await self.delete_karaoke_message(state)
        logger.info("[KARAOKE] Karaoke disabled")

    async def enable_karaoke(self, ctx: PlaybackContext, state: GuildMusicState, channel) -> None:
        state.karaoke_enabled = True
        logger.info("[KARAOKE] Karaoke enabled")
        await self.delete_karaoke_message(state)
        try:
            msg = await channel.send(embed=create_embed("🎤 Karaoke Mode", "Sözler aranıyor...", Colors.PLAYING))
        except discord.HTTPException as e:
            logger.warning(f"[KARAOKE] Could not send karaoke message: {e}")
            return
        state.karaoke_message = msg
        if state.current_song:
            await self.start_karaoke_for_song(ctx, state, state.current_song, state.playback_generation)

    def _build_karaoke_embed(self, state: GuildMusicState, song: Song, position: int) -> discord.Embed:
        track = state.lyrics_track
        idx = state.karaoke_active_index
        rows = []
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

    async def _ensure_karaoke_message(self, ctx: PlaybackContext, state: GuildMusicState) -> discord.Message:
        if state.karaoke_message is not None:
            return state.karaoke_message
        logger.info("[KARAOKE] Karaoke message missing, sending a new one.")
        embed = create_embed("🎤 Karaoke Mode", "Sözler aranıyor...", Colors.PLAYING)
        try:
            msg = await ctx.channel.send(embed=embed)
        except discord.HTTPException as e:
            logger.warning(f"[KARAOKE] Failed to send new karaoke message: {e}")
            raise
        state.karaoke_message = msg
        return msg

    async def _update_karaoke_message(self, ctx: PlaybackContext, state: GuildMusicState, song: Song, position: Optional[int] = None, no_lyrics: bool = False) -> None:
        try:
            await self._ensure_karaoke_message(ctx, state)
        except Exception:
            logger.warning("[KARAOKE] Cannot ensure karaoke message, aborting update.")
            return
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
                    position = self.player._get_current_position(state)
                embed = self._build_karaoke_embed(state, song, position)
            await state.karaoke_message.edit(embed=embed)
        except discord.NotFound:
            logger.info("[KARAOKE] Karaoke message was deleted, resetting reference.")
            state.karaoke_message = None
            self._cancel_karaoke_task(state)
        except discord.HTTPException as e:
            logger.warning(f"[KARAOKE] Mesaj güncellenemedi: {e}")

    async def _karaoke_updater(self, ctx: PlaybackContext, state: GuildMusicState, song: Song, generation: int) -> None:
        logger.info(f"[KARAOKE] Karaoke updater started for guild {ctx.guild.id}, generation {generation}, song '{song.title}'")
        try:
            while True:
                if state.playback_generation != generation:
                    logger.info(f"[KARAOKE] Stale karaoke updater ignored (task_gen={generation}, current_gen={state.playback_generation})")
                    return
                if state.current_song is not song or not state.karaoke_enabled:
                    logger.info("[KARAOKE] Karaoke updater stopping: song changed or karaoke disabled")
                    return
                if state.karaoke_message is None:
                    logger.info("[KARAOKE] Karaoke message lost, attempting to recreate")
                    try:
                        await self._ensure_karaoke_message(ctx, state)
                    except Exception:
                        logger.warning("[KARAOKE] Could not recreate karaoke message, aborting updater")
                        return
                    if state.karaoke_message is None:
                        return
                vc = get_voice_client(ctx)
                if not vc or not (vc.is_playing() or vc.is_paused()):
                    return
                position = self.player._get_current_position(state)
                new_index = state.lyrics_track.active_index(position) if state.lyrics_track else -1
                if new_index != state.karaoke_active_index:
                    state.karaoke_active_index = new_index
                    logger.info(f"[KARAOKE] Active lyric changed to index {new_index}")
                    await self._update_karaoke_message(ctx, state, song, position)
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

        if state.playback_generation != generation or state.current_song is not song or not state.karaoke_enabled:
            logger.info("[KARAOKE] Stale fetch result ignored (generation or song mismatch)")
            return

        state.lyrics_song = song
        state.karaoke_active_index = -1

        try:
            await self._ensure_karaoke_message(ctx, state)
        except Exception as e:
            logger.warning(f"[KARAOKE] Could not ensure message: {e}")
            return

        if track is None:
            state.lyrics_track = None
            await self._update_karaoke_message(ctx, state, song, no_lyrics=True)
            return

        state.lyrics_track = track
        if state.karaoke_message is not None:
            self._cancel_karaoke_task(state)
            state.karaoke_task = asyncio.create_task(self._karaoke_updater(ctx, state, song, generation))
            logger.info(f"[KARAOKE] New updater task started for song '{song.title}', generation {generation}")

    async def start_karaoke_for_song(self, ctx: PlaybackContext, state: GuildMusicState, song: Song, generation: int) -> None:
        if not state.karaoke_enabled:
            logger.info("[KARAOKE] start_karaoke_for_song called but karaoke disabled, ignoring")
            return

        logger.info(f"[KARAOKE] Starting karaoke for song '{song.title}', generation {generation}")

        if state.lyrics_song is song:
            if state.lyrics_track is not None:
                state.karaoke_active_index = -1
                self._cancel_karaoke_task(state)
                state.karaoke_task = asyncio.create_task(self._karaoke_updater(ctx, state, song, generation))
                if state.karaoke_message:
                    await self._update_karaoke_message(ctx, state, song)
                return
            else:
                if state.karaoke_message:
                    await self._update_karaoke_message(ctx, state, song, no_lyrics=True)
                return

        self._cancel_karaoke_fetch(state)
        state.karaoke_fetch_task = asyncio.create_task(self._fetch_and_start_karaoke(ctx, state, song, generation))
        logger.info(f"[KARAOKE] Fetch task started for song '{song.title}', generation {generation}")