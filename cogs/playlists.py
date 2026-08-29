import discord
from discord import app_commands
from discord.ext import commands
from database import DatabaseManager
from utils.helpers import create_embed, format_duration, extract_playlist_info, build_song_from_playlist_entry
from config import Colors
import logging
import asyncio
import random

logger = logging.getLogger(__name__)

class Playlists(commands.Cog):
    # Grup, sınıf seviyesinde bir öznitelik olarak tanımlanmalı ve alt komutlar
    # @playlist_group.command(...) ile eklenmeli (bkz. Favorites cogundaki not).
    playlist_group = app_commands.Group(name="playlist", description="Kullanıcı playlist yönetimi")

    def __init__(self, bot):
        self.bot = bot
        self.db = DatabaseManager()

    async def cog_load(self) -> None:
        await self.db.init()
        logger.info("[PLAYLISTS] Playlist group ready")

    @playlist_group.command(name="youtube", description="Bir YouTube playlist'ini sıraya ekle")
    async def playlist_youtube(self, interaction: discord.Interaction, url: str):
        await interaction.response.defer()
        music_cog = self.bot.get_cog("Music")
        if not music_cog:
            await interaction.followup.send("Müzik sistemi yüklenmemiş.", ephemeral=True)
            return
        if not await music_cog.player.ensure_voice(interaction):
            await interaction.followup.send("Ses kanalına bağlanılamadı.", ephemeral=True)
            return
        state = music_cog.player._get_state(interaction.guild.id)
        try:
            data = await extract_playlist_info(url, asyncio.get_running_loop())
            entries = [e for e in data.get("entries", []) if e]
            if not entries:
                raise Exception("Playlist boş veya geçersiz.")
            new_songs = []
            for entry in entries:
                try:
                    song = build_song_from_playlist_entry(entry, interaction.user)
                    if song.url:
                        new_songs.append(song)
                except Exception:
                    continue
            await music_cog.player.enqueue_songs(interaction, new_songs)
            added = len(new_songs)
            embed = discord.Embed(title="📋 Playlist Sıraya Eklendi", description=data.get("title", "Playlist"), color=Colors.SUCCESS)
            embed.add_field(name="🎵 Eklenen Şarkı", value=str(added), inline=True)
            embed.add_field(name="📌 Sıradaki Toplam", value=str(len(state.queue)), inline=True)
            await interaction.followup.send(embed=embed)
            await music_cog.player._maybe_start_playback(interaction)
        except Exception as e:
            embed = create_embed("Playlist Hatası", str(e), Colors.ERROR)
            await interaction.followup.send(embed=embed)

    @playlist_group.command(name="oluştur", description="Yeni bir playlist oluştur")
    async def playlist_create(self, interaction: discord.Interaction, isim: str):
        await interaction.response.defer()
        pl_id = await self.db.create_playlist(isim, interaction.user.id)
        embed = create_embed("Playlist Oluşturuldu", f"`{isim}` adlı playlist oluşturuldu (ID: {pl_id}).", Colors.SUCCESS)
        await interaction.followup.send(embed=embed)

    @playlist_group.command(name="ekle", description="Mevcut şarkıyı bir playlist'e ekle")
    async def playlist_add(self, interaction: discord.Interaction, playlist_id: int):
        await interaction.response.defer()
        music_cog = self.bot.get_cog("Music")
        if not music_cog:
            await interaction.followup.send("Müzik sistemi yüklenmemiş.", ephemeral=True)
            return
        state = music_cog.player._get_state(interaction.guild.id)
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

    @playlist_group.command(name="kuyruk_kaydet", description="Kuyruktaki şarkıları playlist'e kaydet")
    async def playlist_savequeue(self, interaction: discord.Interaction, playlist_id: int):
        await interaction.response.defer()
        music_cog = self.bot.get_cog("Music")
        if not music_cog:
            await interaction.followup.send("Müzik sistemi yüklenmemiş.", ephemeral=True)
            return
        playlists = await self.db.get_playlists(interaction.user.id)
        if not any(p["id"] == playlist_id for p in playlists):
            embed = create_embed("Hata", "Bu playlist size ait değil veya bulunamadı.", Colors.ERROR)
            await interaction.followup.send(embed=embed)
            return
        state = music_cog.player._get_state(interaction.guild.id)
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

    @playlist_group.command(name="göster", description="Playlist içeriğini göster")
    async def playlist_show(self, interaction: discord.Interaction, playlist_id: int, sayfa: int = 1):
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
        sayfa = max(1, min(sayfa, total_pages))
        start = (sayfa - 1) * per_page
        items = songs[start:start + per_page]
        lines = [f"{start + i + 1}. [{s.title}]({s.url})" for i, s in enumerate(items)]
        embed = discord.Embed(title=f"📀 Playlist ID: {playlist_id}", description="\n".join(lines), color=Colors.PRIMARY)
        embed.set_footer(text=f"Sayfa {sayfa}/{total_pages} • Toplam {len(songs)} şarkı")
        await interaction.followup.send(embed=embed)

    @playlist_group.command(name="karıştır", description="Playlist'i karıştırarak yükle")
    async def playlist_shuffle(self, interaction: discord.Interaction, playlist_id: int):
        await interaction.response.defer()
        music_cog = self.bot.get_cog("Music")
        if not music_cog:
            await interaction.followup.send("Müzik sistemi yüklenmemiş.", ephemeral=True)
            return
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
        if not await music_cog.player.ensure_voice(interaction):
            embed = create_embed("Hata", "Ses kanalına bağlanılamadı.", Colors.ERROR)
            await interaction.followup.send(embed=embed)
            return
        random.shuffle(songs)
        state = music_cog.player._get_state(interaction.guild.id)
        for song in songs:
            song.requester = interaction.user
        await music_cog.player.enqueue_songs(interaction, songs)
        embed = create_embed("Playlist", f"{len(songs)} şarkı karıştırılarak sıraya eklendi.", Colors.SUCCESS)
        await interaction.followup.send(embed=embed)
        await music_cog.player._maybe_start_playback(interaction)

    @playlist_group.command(name="çal", description="Playlist'i sıraya ekleyip çal")
    async def playlist_play(self, interaction: discord.Interaction, playlist_id: int):
        await interaction.response.defer()
        music_cog = self.bot.get_cog("Music")
        if not music_cog:
            await interaction.followup.send("Müzik sistemi yüklenmemiş.", ephemeral=True)
            return
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
        if not await music_cog.player.ensure_voice(interaction):
            embed = create_embed("Hata", "Ses kanalına bağlanılamadı.", Colors.ERROR)
            await interaction.followup.send(embed=embed)
            return
        state = music_cog.player._get_state(interaction.guild.id)
        for song in songs:
            song.requester = interaction.user
        await music_cog.player.enqueue_songs(interaction, songs)
        embed = create_embed("Playlist", f"{len(songs)} şarkı sıraya eklendi.", Colors.SUCCESS)
        await interaction.followup.send(embed=embed)
        await music_cog.player._maybe_start_playback(interaction)

    @playlist_group.command(name="sil", description="Playlist'i sil")
    async def playlist_delete(self, interaction: discord.Interaction, playlist_id: int):
        await interaction.response.defer()
        deleted = await self.db.delete_playlist(playlist_id, interaction.user.id)
        if deleted:
            embed = create_embed("Silindi", f"Playlist ID {playlist_id} silindi.", Colors.SUCCESS)
        else:
            embed = create_embed("Hata", "Playlist bulunamadı veya size ait değil.", Colors.ERROR)
        await interaction.followup.send(embed=embed)

    @playlist_group.command(name="şarkı_sil", description="Playlist'ten belirli bir sıradaki şarkıyı sil")
    async def playlist_remove_song(self, interaction: discord.Interaction, playlist_id: int, sıra: int):
        await interaction.response.defer()
        playlists = await self.db.get_playlists(interaction.user.id)
        if not any(p["id"] == playlist_id for p in playlists):
            embed = create_embed("Hata", "Bu playlist size ait değil veya bulunamadı.", Colors.ERROR)
            await interaction.followup.send(embed=embed)
            return
        songs = await self.db.get_playlist_songs(playlist_id)
        if sıra < 1 or sıra > len(songs):
            embed = create_embed("Hata", "Geçersiz sıra numarası.", Colors.ERROR)
            await interaction.followup.send(embed=embed)
            return
        removed_song = songs[sıra - 1]
        success = await self.db.remove_song_from_playlist(playlist_id, sıra)
        if success:
            embed = create_embed("Playlist Şarkısı Silindi", f"`{removed_song.title}` playlistten silindi.", Colors.SUCCESS)
        else:
            embed = create_embed("Hata", "Şarkı silinemedi.", Colors.ERROR)
        await interaction.followup.send(embed=embed)

async def setup(bot):
    await bot.add_cog(Playlists(bot))
