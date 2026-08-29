import discord
from discord import app_commands
from discord.ext import commands
from database import DatabaseManager
from utils.helpers import create_embed, format_duration
from config import Colors
import logging

logger = logging.getLogger(__name__)

class Favorites(commands.Cog):
    # Grup, sınıf seviyesinde bir öznitelik olarak tanımlanmalı ve alt komutlar
    # @favorites_group.command(...) ile eklenmeli. app_commands.command() ile
    # ayrı ayrı süslenip __init__ içinde add_command() ile gruba eklemek,
    # discord.py'nin Cog._inject mekanizmasıyla çakışıp
    # "CommandAlreadyRegistered" hatasına yol açar, çünkü her alt komut hâlâ
    # __cog_app_commands__ listesinde ayrı ayrı yer alır ve kök ebeveyne
    # (bu grup) tekrar tekrar eklenmeye çalışılır.
    favorites_group = app_commands.Group(name="favoriler", description="Favori şarkı yönetimi")

    def __init__(self, bot):
        self.bot = bot
        self.db = DatabaseManager()

    async def cog_load(self) -> None:
        await self.db.init()
        logger.info("[FAVORITES] Favorites group ready")

    @favorites_group.command(name="ekle", description="Mevcut şarkıyı favorilere ekle")
    async def favorites_add(self, interaction: discord.Interaction):
        music_cog = self.bot.get_cog("Music")
        if not music_cog:
            await interaction.response.send_message("Müzik sistemi yüklenmemiş.", ephemeral=True)
            return
        state = music_cog.player._get_state(interaction.guild.id)
        if not state.current_song:
            await interaction.response.send_message("Şu anda çalan şarkı yok.", ephemeral=True)
            return
        song = state.current_song
        success = await self.db.add_favorite(interaction.user.id, song)
        if success:
            embed = create_embed("Favori Eklendi", f"❤️ `{song.title}` favorilerinize eklendi.", Colors.SUCCESS)
        else:
            embed = create_embed("Favori", "Bu şarkı zaten favorilerinizde.", Colors.WARNING)
        await interaction.response.send_message(embed=embed)

    @favorites_group.command(name="liste", description="Favori şarkılarınızı listele")
    async def favorites_list(self, interaction: discord.Interaction):
        songs = await self.db.get_favorites(interaction.user.id)
        if not songs:
            embed = create_embed("Favoriler", "Henüz favori şarkınız yok.", Colors.INFO)
            await interaction.response.send_message(embed=embed)
            return
        lines = [f"`{i}.` [{s.title}]({s.url})" for i, s in enumerate(songs[:10], start=1)]
        embed = discord.Embed(title="❤️ Favori Şarkılarınız", description="\n".join(lines), color=Colors.PRIMARY)
        if len(songs) > 10:
            embed.set_footer(text=f"Toplam {len(songs)} şarkı, sadece ilk 10 gösteriliyor.")
        await interaction.response.send_message(embed=embed)

    @favorites_group.command(name="çal", description="Favori şarkılarınızı sıraya ekleyip çal")
    async def favorites_play(self, interaction: discord.Interaction):
        music_cog = self.bot.get_cog("Music")
        if not music_cog:
            await interaction.response.send_message("Müzik sistemi yüklenmemiş.", ephemeral=True)
            return
        songs = await self.db.get_favorites(interaction.user.id)
        if not songs:
            await interaction.response.send_message("Favori listeniz boş.", ephemeral=True)
            return
        if not await music_cog.player.ensure_voice(interaction):
            await interaction.response.send_message("Ses kanalına bağlanılamadı.", ephemeral=True)
            return
        state = music_cog.player._get_state(interaction.guild.id)
        for song in songs:
            song.requester = interaction.user
        await music_cog.player.enqueue_songs(interaction, songs)
        embed = create_embed("Favoriler", f"{len(songs)} şarkı sıraya eklendi.", Colors.SUCCESS)
        await interaction.response.send_message(embed=embed)
        await music_cog.player._maybe_start_playback(interaction)

    @favorites_group.command(name="sil", description="Favorilerden şarkı sil (sıra numarası ile)")
    async def favorites_remove(self, interaction: discord.Interaction, sıra: int):
        songs = await self.db.get_favorites(interaction.user.id)
        if sıra < 1 or sıra > len(songs):
            await interaction.response.send_message("Geçersiz sıra numarası.", ephemeral=True)
            return
        song = songs[sıra - 1]
        await self.db.remove_favorite(interaction.user.id, song.url)
        embed = create_embed("Favori Silindi", f"❤️❌ `{song.title}` favorilerden silindi.", Colors.SUCCESS)
        await interaction.response.send_message(embed=embed)


async def setup(bot):
    await bot.add_cog(Favorites(bot))
