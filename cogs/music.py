import asyncio
import os
import random
import time
import logging
import discord
import re
from pathlib import Path
from discord import app_commands
from discord.ext import commands
from audio.filters import AudioFilterBuilder
from discord import app_commands
from discord.ext import commands

from config import Colors
from database import DatabaseManager
from karaoke.service import LyricsService
from karaoke.provider import LRCLibProvider
from music.player import MusicPlayer
from music.karaoke_engine import KaraokeEngine
from spotify import SpotifyResolver
from models import Song  # <-- EKLENDI
from utils.helpers import (
    create_embed, format_duration, extract_song_info, extract_playlist_info,
    build_song, build_song_from_playlist_entry, PlaybackContext, get_voice_client
)
from exceptions import VoiceError, YTDLError, SpotifyError
from ui.controls import MusicControlView

logger = logging.getLogger(__name__)

class Music(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.db = DatabaseManager()
        has_spotify_creds = bool(os.getenv("SPOTIFY_CLIENT_ID")) and bool(os.getenv("SPOTIFY_CLIENT_SECRET"))
        self.spotify = SpotifyResolver() if has_spotify_creds else None
        if not has_spotify_creds:
            logger.info("Spotify kimlik bilgileri bulunamadı, Spotify desteği devre dışı.")
        self.ffmpeg_executable = os.getenv("FFMPEG_EXECUTABLE") or "ffmpeg"

        self.lyrics_service = LyricsService(self.db, LRCLibProvider())
        self.player = MusicPlayer(bot, self.db, self.ffmpeg_executable)
        self.karaoke_engine = KaraokeEngine(self.player, self.db, self.lyrics_service)
        self.player.karaoke_engine = self.karaoke_engine

        self._presence_task = None
        self._presence_index = 0

    async def cog_load(self) -> None:
        await self.db.init()
        self._presence_task = asyncio.create_task(self._presence_loop())

    def cog_unload(self) -> None:
        if self._presence_task:
            self._presence_task.cancel()

    async def _presence_loop(self) -> None:
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            try:
                stats = self._get_presence_stats()
                if self._presence_index % 2 == 0:
                    text = f"📊 {stats['total_guilds']} sunucu • 🟢 {stats['active_guilds']} aktif • 👥 {stats['listeners']} dinleyici"
                else:
                    song = stats["song"]
                    state = stats["state"]
                    if song and state:
                        current_position = self.player._get_current_position(state)
                        if song.duration:
                            text = f"🎧 {song.title} • ⏱️ {format_duration(current_position)} / {format_duration(song.duration)}"
                        else:
                            text = f"🎧 {song.title} • ⏱️ {format_duration(current_position)}"
                    else:
                        text = "🎵 Müzik botu hazır"
                text = text[:120]
                await self.bot.change_presence(
                    activity=discord.Activity(type=discord.ActivityType.listening, name=text)
                )
                self._presence_index += 1
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"Presence güncelleme hatası: {e}")
            await asyncio.sleep(12)

    def _get_presence_stats(self):
        total_guilds = len(self.bot.guilds)
        active_guilds = 0
        listeners = 0
        active_song = None
        active_state = None
        for guild in self.bot.guilds:
            state = self.player._get_state(guild.id)
            vc = guild.voice_client
            if vc and vc.channel and state and state.current_song and (vc.is_playing() or vc.is_paused()):
                active_guilds += 1
                if active_song is None:
                    active_song = state.current_song
                    active_state = state
                listeners += sum(1 for m in vc.channel.members if not m.bot)
        return {
            "total_guilds": total_guilds,
            "active_guilds": active_guilds,
            "listeners": listeners,
            "song": active_song,
            "state": active_state,
        }

    # --- Spotify helper ---
    async def _resolve_spotify(self, url: str):
        import re
        if not self.spotify:
            raise SpotifyError("Spotify entegrasyonu için Client ID/Secret ayarlanmamış.")
        match_track = re.search(r"track/([a-zA-Z0-9]+)", url)
        match_playlist = re.search(r"playlist/([a-zA-Z0-9]+)", url)
        songs = []
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

    # --- Slash commands ---

    @app_commands.command(name="join", description="Ses kanalına katıl.")
    async def slash_join(self, interaction: discord.Interaction):
        if await self.player.ensure_voice(interaction):
            embed = create_embed("Katıldı", "🔊 Ses kanalına katıldım.", Colors.SUCCESS)
            await interaction.response.send_message(embed=embed)
        else:
            embed = create_embed("Hata", "Önce bir ses kanalına katılmalısınız.", Colors.ERROR)
            await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="leave", description="Ses kanalından ayrıl ve sırayı temizle.")
    async def slash_leave(self, interaction: discord.Interaction):
        vc = get_voice_client(interaction)
        if not vc:
            await interaction.response.send_message("Zaten bir ses kanalında değilim.", ephemeral=True)
            return
        state = self.player._get_state(interaction.guild.id)
        if self.karaoke_engine:
            await self.karaoke_engine.disable_karaoke(state)
        state.autoplay = False
        async with state.lock:
            state.queue.clear()
        if vc.is_playing() or vc.is_paused():
            # Let the normal after_callback -> _handle_after_playing path record
            # stats/history for the song that's ending, instead of nulling
            # current_song here first (which would make after_callback's own
            # "intentional stop" guard skip that bookkeeping entirely).
            vc.stop()
            await asyncio.sleep(0.1)
        else:
            self.player._cancel_timer_task(state)
            state.current_song = None
            state.active = False
        await vc.disconnect(force=True)
        embed = create_embed("Ayrıldı", "👋 Ses kanalından ayrıldım ve sıra temizlendi.", Colors.SUCCESS)
        await interaction.response.send_message(embed=embed)

    @discord.app_commands.command(name="play", description="Şarkı çal veya sıraya ekle (arama, YouTube veya Spotify linki).")
    @discord.app_commands.describe(sorgu="Şarkı adı, YouTube linki veya Spotify linki")
    async def slash_play(self, interaction: discord.Interaction, sorgu: str):
        await interaction.response.defer()

        if not await self.player.ensure_voice(interaction):
            embed = create_embed("Hata", "Önce bir ses kanalına katılmalısınız.", Colors.ERROR)
            await interaction.followup.send(embed=embed)
            return

        state = self.player._get_state(interaction.guild.id)

        try:
            if "open.spotify.com" in sorgu:
                songs = await self._resolve_spotify(sorgu)
                if not songs:
                    raise SpotifyError("Spotify şarkı(ları) bulunamadı.")
                for s in songs:
                    s.requester = interaction.user
                await self.player.enqueue_songs(interaction, songs)
                if len(songs) == 1:
                    embed = create_embed(
                        "Sıraya Eklendi",
                        f"🎵 [{songs[0].title}]({songs[0].url})",
                        Colors.SUCCESS,
                        thumbnail=songs[0].thumbnail
                    )
                else:
                    embed = create_embed(
                        "Sıraya Eklendi",
                        f"🎵 Spotify'dan {len(songs)} şarkı sıraya eklendi.",
                        Colors.SUCCESS
                    )
                await interaction.followup.send(embed=embed)
            else:
                data = await extract_song_info(sorgu, asyncio.get_running_loop())
                song = build_song(data, interaction.user)
                await self.player.enqueue_songs(interaction, [song])
                embed = create_embed(
                    "Sıraya Eklendi",
                    f"🎵 [{song.title}]({song.url})",
                    Colors.SUCCESS,
                    thumbnail=song.thumbnail
                )
                await interaction.followup.send(embed=embed)
        except (YTDLError, SpotifyError) as e:
            embed = create_embed("Hata", str(e), Colors.ERROR)
            await interaction.followup.send(embed=embed)
            return
        except Exception as e:
            logger.error(f"/play error: {e}")
            embed = create_embed("Hata", f"Beklenmeyen bir hata oluştu: {e}", Colors.ERROR)
            await interaction.followup.send(embed=embed)
            return

        await self.player._maybe_start_playback(interaction)

    @app_commands.command(name="pause", description="Çalan şarkıyı duraklat.")
    async def slash_pause(self, interaction: discord.Interaction):
        vc = get_voice_client(interaction)
        if not vc or not vc.is_playing():
            await interaction.response.send_message("Şu anda çalan bir şarkı yok.", ephemeral=True)
            return
        state = self.player._get_state(interaction.guild.id)
        state.paused_at = time.monotonic()
        vc.pause()
        embed = create_embed("Duraklatıldı", "⏸️ Şarkı duraklatıldı.", Colors.SUCCESS)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="resume", description="Duraklatılan şarkıya devam et.")
    async def slash_resume(self, interaction: discord.Interaction):
        vc = get_voice_client(interaction)
        if not vc or not vc.is_paused():
            await interaction.response.send_message("Duraklatılmış bir şarkı yok.", ephemeral=True)
            return
        state = self.player._get_state(interaction.guild.id)
        if state.paused_at and state.started_at:
            state.playback_offset += state.paused_at - state.started_at
        state.started_at = time.monotonic()
        state.paused_at = 0.0
        vc.resume()
        embed = create_embed("Devam Ediyor", "▶️ Şarkı devam ediyor.", Colors.SUCCESS)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="skip", description="Şu anki şarkıyı atla.")
    async def slash_skip(self, interaction: discord.Interaction):
        vc = get_voice_client(interaction)
        if not vc or not (vc.is_playing() or vc.is_paused()):
            await interaction.response.send_message("Şu anda çalan bir şarkı yok.", ephemeral=True)
            return
        state = self.player._get_state(interaction.guild.id)
        if state.seek_target is not None:
            state.playback_generation += 1
            state.seek_target = None
            state.seek_retry_count = 0
        title = state.current_song.title if state.current_song else "Şarkı"
        vc.stop()
        embed = create_embed("Atlandı", f"⏭️ `{title}` atlandı.", Colors.SUCCESS)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="stop", description="Oynatmayı durdur, sırayı temizle ve kanaldan ayrıl.")
    async def slash_stop(self, interaction: discord.Interaction):
        vc = get_voice_client(interaction)
        if not vc:
            await interaction.response.send_message("Bot zaten bir ses kanalında değil.", ephemeral=True)
            return
        state = self.player._get_state(interaction.guild.id)
        if self.karaoke_engine:
            await self.karaoke_engine.disable_karaoke(state)
        state.autoplay = False
        async with state.lock:
            state.queue.clear()
        if vc.is_playing() or vc.is_paused():
            vc.stop()
            await asyncio.sleep(0.1)
        else:
            self.player._cancel_timer_task(state)
            state.current_song = None
            state.active = False
        await vc.disconnect(force=True)
        embed = create_embed("Durduruldu", "⏹️ Sıra temizlendi ve kanaldan ayrıldım.", Colors.SUCCESS)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="volume", description="Ses seviyesini ayarla (0-200).")
    @app_commands.describe(seviye="Ses seviyesi yüzdesi (0-200)")
    async def slash_volume(self, interaction: discord.Interaction, seviye: app_commands.Range[int, 0, 200]):
        state = self.player._get_state(interaction.guild.id)
        state.volume = seviye / 100
        vc = get_voice_client(interaction)
        if vc and vc.source and isinstance(vc.source, discord.PCMVolumeTransformer):
            vc.source.volume = state.volume
        embed = create_embed("Ses Seviyesi", f"🔊 Ses seviyesi %{seviye} olarak ayarlandı.", Colors.SUCCESS)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="queue", description="Sıradaki şarkıları göster.")
    @app_commands.describe(sayfa="Sayfa numarası")
    async def slash_queue(self, interaction: discord.Interaction, sayfa: int = 1):
        state = self.player._get_state(interaction.guild.id)
        async with state.lock:
            songs = list(state.queue)
        if not songs and not state.current_song:
            embed = create_embed("Sıra", "Sıra boş.", Colors.INFO)
            await interaction.response.send_message(embed=embed)
            return
        per_page = 10
        total_pages = max(1, (len(songs) + per_page - 1) // per_page)
        sayfa = max(1, min(sayfa, total_pages))
        start = (sayfa - 1) * per_page
        items = songs[start:start + per_page]
        lines = [
            f"`{start + i + 1}.` [{s.title}]({s.url}) • {format_duration(s.duration)}"
            for i, s in enumerate(items)
        ]
        description = ""
        if state.current_song:
            description += f"**▶️ Şimdi Çalıyor:** [{state.current_song.title}]({state.current_song.url})\n\n"
        description += "\n".join(lines) if lines else "Sırada başka şarkı yok."
        embed = discord.Embed(title="📜 Sıra", description=description, color=Colors.PRIMARY)
        embed.set_footer(text=f"Sayfa {sayfa}/{total_pages} • Toplam {len(songs)} şarkı")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="nowplaying", description="Şu anda çalan şarkıyı göster.")
    async def slash_nowplaying(self, interaction: discord.Interaction):
        state = self.player._get_state(interaction.guild.id)
        if not state.current_song:
            embed = create_embed("Şimdi Çalıyor", "Şu anda çalan bir şarkı yok.", Colors.INFO)
            await interaction.response.send_message(embed=embed)
            return
        embed = self.player._build_now_playing_embed(state, state.current_song)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="shuffle", description="Sıradaki şarkıları karıştır.")
    async def slash_shuffle(self, interaction: discord.Interaction):
        state = self.player._get_state(interaction.guild.id)
        async with state.lock:
            if len(state.queue) < 2:
                await interaction.response.send_message("Karıştırmak için sırada yeterli şarkı yok.", ephemeral=True)
                return
            items = list(state.queue)
            random.shuffle(items)
            state.queue.clear()
            state.queue.extend(items)
        embed = create_embed("Karıştırıldı", "🔀 Sıra karıştırıldı.", Colors.SUCCESS)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="remove", description="Sıradan belirli bir şarkıyı çıkar.")
    @app_commands.describe(sıra="Sıra numarası (queue listesindeki numara)")
    async def slash_remove(self, interaction: discord.Interaction, sıra: int):
        state = self.player._get_state(interaction.guild.id)
        async with state.lock:
            if sıra < 1 or sıra > len(state.queue):
                await interaction.response.send_message("Geçersiz sıra numarası.", ephemeral=True)
                return
            items = list(state.queue)
            removed = items.pop(sıra - 1)
            state.queue.clear()
            state.queue.extend(items)
        embed = create_embed("Sıradan Çıkarıldı", f"🗑️ `{removed.title}` sıradan çıkarıldı.", Colors.SUCCESS)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="loop", description="Döngü modunu ayarla.")
    @app_commands.choices(mod=[
        app_commands.Choice(name="Kapalı", value="off"),
        app_commands.Choice(name="Tek Şarkı", value="song"),
        app_commands.Choice(name="Tüm Sıra", value="queue"),
    ])
    async def slash_loop(self, interaction: discord.Interaction, mod: app_commands.Choice[str]):
        state = self.player._get_state(interaction.guild.id)
        if mod.value == "off":
            state.loop_current = False
            state.loop_queue = False
            text = "🔁 Döngü kapatıldı."
        elif mod.value == "song":
            state.loop_current = True
            state.loop_queue = False
            text = "🔂 Tek şarkı döngüsü açıldı."
        else:
            state.loop_current = False
            state.loop_queue = True
            text = "🔁 Tüm sıra döngüsü açıldı."
        embed = create_embed("Döngü", text, Colors.SUCCESS)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="seek", description="Şarkıda belirli bir saniyeye sar.")
    @app_commands.describe(saniye="Sarılacak saniye")
    async def slash_seek(self, interaction: discord.Interaction, saniye: int):
        await interaction.response.defer(ephemeral=True)
        try:
            await self.player.perform_seek(interaction, saniye)
            embed = create_embed("Sarıldı", f"⏩ Şarkı {format_duration(saniye)} noktasına sarıldı.", Colors.SUCCESS)
            await interaction.followup.send(embed=embed, ephemeral=True)
        except VoiceError as e:
            await interaction.followup.send(f"Hata: {e}", ephemeral=True)
        except Exception as e:
            logger.error(f"/seek error: {e}")
            await interaction.followup.send(f"Hata: {e}", ephemeral=True)

    @app_commands.command(name="karaoke", description="Karaoke modunu aç/kapat.")
    async def slash_karaoke(self, interaction: discord.Interaction):
        state = self.player._get_state(interaction.guild.id)
        if state.karaoke_enabled:
            await self.karaoke_engine.disable_karaoke(state)
            embed = create_embed("Karaoke", "🎤 Karaoke modu kapatıldı.", Colors.SUCCESS)
            await interaction.response.send_message(embed=embed)
        else:
            if not state.current_song:
                embed = create_embed("Karaoke", "Karaoke için önce bir şarkı çalıyor olmalı.", Colors.WARNING)
                await interaction.response.send_message(embed=embed, ephemeral=True)
                return
            embed = create_embed("Karaoke", "🎤 Karaoke modu açıldı.", Colors.SUCCESS)
            await interaction.response.send_message(embed=embed)
            await self.karaoke_engine.enable_karaoke(interaction, state, interaction.channel)
    # New commands:
    @app_commands.command(name="previous", description="Önceki şarkıyı tekrar çal.")
    async def slash_previous(self, interaction: discord.Interaction):
        if await self.player.previous(interaction):
            embed = create_embed("Önceki Şarkı", "↩️ Önceki şarkı çalınıyor.", Colors.SUCCESS)
        else:
            embed = create_embed("Hata", "Geçmişte şarkı yok.", Colors.ERROR)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="replay", description="Şu anki şarkıyı baştan başlat.")
    async def slash_replay(self, interaction: discord.Interaction):
        if await self.player.replay(interaction):
            embed = create_embed("Tekrar", "🔄 Şarkı baştan başlatıldı.", Colors.SUCCESS)
        else:
            embed = create_embed("Hata", "Çalan şarkı yok.", Colors.ERROR)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="similar", description="Mevcut şarkıya benzer şarkıları sıraya ekle.")
    @app_commands.describe(adet="Eklenecek benzer şarkı sayısı (1-10)")
    async def slash_similar(self, interaction: discord.Interaction, adet: app_commands.Range[int, 1, 10] = 5):
        state = self.player._get_state(interaction.guild.id)
        if not state.current_song:
            await interaction.response.send_message("Şu anda çalan şarkı yok.", ephemeral=True)
            return
        await interaction.response.defer()
        songs = await self.player._fetch_similar_songs(interaction, state.current_song, limit=adet)
        if not songs:
            await interaction.followup.send("Benzer şarkı bulunamadı.")
            return
        await self.player.enqueue_songs(interaction, songs)
        embed = create_embed("Benzer Şarkılar", f"{len(songs)} benzer şarkı sıraya eklendi.", Colors.SUCCESS)
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="autoplay", description="Kuyruk boşaldığında otomatik olarak benzer şarkıları çal.")
    async def slash_autoplay(self, interaction: discord.Interaction):
        state = self.player._get_state(interaction.guild.id)
        state.autoplay = not state.autoplay
        status = "açıldı" if state.autoplay else "kapatıldı"
        embed = create_embed("Otomatik Oynatma", f"🎶 Otomatik oynatma {status}.", Colors.SUCCESS)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="autoshuffle", description="Sıraya eklenen şarkıları otomatik karıştır.")
    async def slash_autoshuffle(self, interaction: discord.Interaction):
        state = self.player._get_state(interaction.guild.id)
        state.autoshuffle = not state.autoshuffle
        status = "açıldı" if state.autoshuffle else "kapatıldı"
        embed = create_embed("Otomatik Karıştırma", f"🔀 Otomatik karıştırma {status}.", Colors.SUCCESS)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="bassboost", description="Bas güçlendirme seviyesini ayarla (0-10).")
    @app_commands.describe(seviye="Bas seviyesi (0-10)")
    async def slash_bassboost(self, interaction: discord.Interaction, seviye: app_commands.Range[int, 0, 10]):
        state = self.player._get_state(interaction.guild.id)
        previous = state.bass_boost
        state.bass_boost = seviye
        await interaction.response.defer()
        vc = get_voice_client(interaction)
        if state.current_song and vc and (vc.is_playing() or vc.is_paused()):
            restarted = await self.player.apply_audio_settings(interaction)
            if not restarted:
                state.bass_boost = previous
                embed = create_embed("Hata", "Bas güçlendirme uygulanamadı, önceki ayara geri dönüldü.", Colors.ERROR)
                await interaction.followup.send(embed=embed)
                return
        embed = create_embed("Bas Güçlendirme", f"🔊 Bas seviyesi {seviye} olarak ayarlandı.", Colors.SUCCESS)
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="speed", description="Oynatma hızını ayarla (0.5 - 2.0).")
    @app_commands.describe(hız="Hız oranı (0.5 - 2.0)")
    async def slash_speed(self, interaction: discord.Interaction, hız: app_commands.Range[float, 0.5, 2.0]):
        state = self.player._get_state(interaction.guild.id)
        previous = state.speed
        state.speed = hız
        await interaction.response.defer()
        vc = get_voice_client(interaction)
        if state.current_song and vc and (vc.is_playing() or vc.is_paused()):
            restarted = await self.player.apply_audio_settings(interaction)
            if not restarted:
                state.speed = previous
                embed = create_embed("Hata", "Hız ayarı uygulanamadı, önceki ayara geri dönüldü.", Colors.ERROR)
                await interaction.followup.send(embed=embed)
                return
        embed = create_embed("Oynatma Hızı", f"⚡ Hız {hız:.1f}x olarak ayarlandı.", Colors.SUCCESS)
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="eightd", description="8D ses efektini aç/kapat.")
    async def slash_eightd(self, interaction: discord.Interaction):
        state = self.player._get_state(interaction.guild.id)
        previous = state.eightd
        state.eightd = not state.eightd
        await interaction.response.defer()
        vc = get_voice_client(interaction)
        if state.current_song and vc and (vc.is_playing() or vc.is_paused()):
            restarted = await self.player.apply_audio_settings(interaction)
            if not restarted:
                state.eightd = previous
                embed = create_embed("Hata", "8D efekti uygulanamadı, önceki duruma geri dönüldü.", Colors.ERROR)
                await interaction.followup.send(embed=embed)
                return
        status = "açıldı" if state.eightd else "kapatıldı"
        embed = create_embed("8D Ses", f"🎧 8D efekti {status}.", Colors.SUCCESS)
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="crossfade", description="Şarkılar arası geçiş süresini ayarla (saniye).")
    @app_commands.describe(süre="Geçiş süresi (0 = kapat)")
    async def slash_crossfade(self, interaction: discord.Interaction, süre: app_commands.Range[float, 0, 10]):
        state = self.player._get_state(interaction.guild.id)
        state.crossfade_duration = süre
        embed = create_embed("Crossfade", f"⏳ Geçiş süresi {süre} saniye olarak ayarlandı.", Colors.SUCCESS)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="removedupes", description="Kuyruktaki kopya şarkıları temizle.")
    async def slash_removedupes(self, interaction: discord.Interaction):
        removed = await self.player.remove_duplicates(interaction)
        embed = create_embed("Kopya Temizleme", f"🧹 {removed} kopya şarkı kaldırıldı.", Colors.SUCCESS)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="playfile", description="Yerel bir ses dosyasını çal.")
    @app_commands.describe(dosya="Dosya adı (allowed_media klasörü içinde)")
    async def slash_playfile(self, interaction: discord.Interaction, dosya: str):
        allowed_dir = Path(os.getenv("ALLOWED_MEDIA_DIR", "./media")).resolve()
        target = (allowed_dir / dosya).resolve()
        if not target.is_file() or allowed_dir not in target.parents:
            await interaction.response.send_message("Geçersiz dosya veya izin verilen dizinin dışında.", ephemeral=True)
            return
        if not target.suffix.lower() in [".mp3", ".wav", ".flac", ".m4a", ".ogg"]:
            await interaction.response.send_message("Desteklenmeyen dosya formatı.", ephemeral=True)
            return
        # Play the file
        if not await self.player.ensure_voice(interaction):
            await interaction.response.send_message("Ses kanalına bağlanılamadı.", ephemeral=True)
            return
        # Create a Song object with local file URL (file://)
        song = Song(
            source_url=target.as_uri(),
            title=target.stem,
            url=target.as_uri(),
            duration=None,
            uploader="Local",
            requester=interaction.user
        )
        state = self.player._get_state(interaction.guild.id)
        await self.player.enqueue_songs(interaction, [song])
        await interaction.response.send_message(f"📁 `{dosya}` sıraya eklendi.")
        await self.player._maybe_start_playback(interaction)

    @app_commands.command(name="playfolder", description="Yerel bir klasördeki tüm ses dosyalarını sıraya ekle.")
    @app_commands.describe(klasör="Klasör adı (allowed_media_dir içinde)")
    async def slash_playfolder(self, interaction: discord.Interaction, klasör: str):
        allowed_dir = Path(os.getenv("ALLOWED_MEDIA_DIR", "./media")).resolve()
        target_dir = (allowed_dir / klasör).resolve()
        if not target_dir.is_dir() or allowed_dir not in target_dir.parents:
            await interaction.response.send_message("Geçersiz klasör.", ephemeral=True)
            return
        files = []
        for ext in [".mp3", ".wav", ".flac", ".m4a", ".ogg"]:
            files.extend(target_dir.glob(f"*{ext}"))
        if not files:
            await interaction.response.send_message("Klasörde desteklenen ses dosyası yok.", ephemeral=True)
            return
        if not await self.player.ensure_voice(interaction):
            await interaction.response.send_message("Ses kanalına bağlanılamadı.", ephemeral=True)
            return
        state = self.player._get_state(interaction.guild.id)
        new_songs = []
        for f in files:
            song = Song(
                source_url=f.as_uri(),
                title=f.stem,
                url=f.as_uri(),
                duration=None,
                uploader="Local",
                requester=interaction.user
            )
            new_songs.append(song)
        await self.player.enqueue_songs(interaction, new_songs)
        await interaction.response.send_message(f"📁 {len(files)} dosya sıraya eklendi.")
        await self.player._maybe_start_playback(interaction)

    @app_commands.command(name="cleanup", description="Bot'un kanaldaki mesajlarını temizle.")
    @app_commands.describe(adet="Silinecek mesaj sayısı (varsayılan 50)")
    async def slash_cleanup(self, interaction: discord.Interaction, adet: int = 50):
        # Önce defer yaparak etkileşimin zaman aşımına uğramasını engelle
        await interaction.response.defer(ephemeral=True)

        def is_bot(msg):
            return msg.author == interaction.guild.me

        try:
            deleted = await interaction.channel.purge(limit=adet, check=is_bot)
            await interaction.followup.send(f"🧹 {len(deleted)} bot mesajı silindi.", ephemeral=True)
        except discord.Forbidden:
            await interaction.followup.send("❌ Mesajları silmek için yeterli iznim yok.", ephemeral=True)
        except discord.HTTPException as e:
            await interaction.followup.send(f"❌ Temizleme sırasında bir hata oluştu: {e}", ephemeral=True)

    @app_commands.command(name="ping", description="Bot gecikmesini göster.")
    async def slash_ping(self, interaction: discord.Interaction):
        latency = round(self.bot.latency * 1000)
        embed = create_embed("Ping", f"🏓 Gecikme: {latency} ms", Colors.INFO)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="stats", description="Kişisel dinleme istatistiklerinizi göster.")
    async def slash_stats(self, interaction: discord.Interaction):
        stats = await self.player.stats.get_user_stats(interaction.user.id)
        mins = stats["total_seconds"] // 60
        embed = create_embed(
            "📊 Dinleme İstatistikleriniz",
            f"🎵 Toplam dinleme süresi: {mins} dakika\n"
            f"📀 Çalınan şarkı sayısı: {stats['songs_played']}",
            Colors.INFO
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="serverstats", description="Sunucunun toplam dinleme süresini göster.")
    async def slash_serverstats(self, interaction: discord.Interaction):
        secs = await self.player.stats.get_server_stats(interaction.guild.id)
        mins = secs // 60
        embed = create_embed(
            "📊 Sunucu Dinleme Süresi",
            f"🕒 Toplam dinleme: {mins} dakika",
            Colors.INFO
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="globalstats", description="Bot'un global dinleme süresini göster.")
    async def slash_globalstats(self, interaction: discord.Interaction):
        secs = await self.player.stats.get_global_stats()
        mins = secs // 60
        embed = create_embed(
            "🌍 Global Dinleme Süresi",
            f"🕒 Tüm sunucularda toplam: {mins} dakika",
            Colors.INFO
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="servertop", description="Bu sunucuda en çok dinlenen şarkılar.")
    async def slash_servertop(self, interaction: discord.Interaction):
        top = await self.player.stats.get_top_songs_guild(interaction.guild.id, 10)
        if not top:
            await interaction.response.send_message("Bu sunucuda henüz istatistik yok.")
            return
        lines = [f"{i+1}. **{t['title']}** - {t.get('artist','Bilinmiyor')} ({t['play_count']} kez)" for i, t in enumerate(top)]
        embed = discord.Embed(title=f"🏆 En Çok Dinlenen Şarkılar ({interaction.guild.name})", description="\n".join(lines), color=Colors.PRIMARY)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="globaltop", description="Global en çok dinlenen şarkılar.")
    async def slash_globaltop(self, interaction: discord.Interaction):
        top = await self.player.stats.get_top_songs_global(10)
        if not top:
            await interaction.response.send_message("Henüz istatistik yok.")
            return
        lines = [f"{i+1}. **{t['title']}** - {t.get('artist','Bilinmiyor')} ({t['play_count']} kez)" for i, t in enumerate(top)]
        embed = discord.Embed(title="🏆 En Çok Dinlenen Şarkılar (Global)", description="\n".join(lines), color=Colors.PRIMARY)
        await interaction.response.send_message(embed=embed)

async def setup(bot):
    await bot.add_cog(Music(bot))
