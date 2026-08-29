import discord
from discord import app_commands
from discord.ext import commands
import asyncio
import yt_dlp
from utils.helpers import create_embed, build_song, extract_song_info
from config import Colors, YTDLP_OPTIONS
import logging

logger = logging.getLogger(__name__)

class Search(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def _search(self, query: str, limit: int = 10) -> list:
        ytdl = yt_dlp.YoutubeDL(YTDLP_OPTIONS)
        loop = asyncio.get_running_loop()
        try:
            data = await loop.run_in_executor(
                None,
                lambda: ytdl.extract_info(f"ytsearch{limit}:{query}", download=False)
            )
            entries = data.get("entries", [])
            return entries
        except Exception as e:
            logger.error(f"Search error: {e}")
            return []

    search_group = app_commands.Group(name="search", description="Müzik arama")

    @search_group.command(name="song", description="Şarkı ara")
    @app_commands.describe(query="Aranacak şarkı adı")
    async def search_song(self, interaction: discord.Interaction, query: str):
        await interaction.response.defer()
        results = await self._search(query, limit=5)
        if not results:
            await interaction.followup.send("Sonuç bulunamadı.")
            return
        embed = self._build_results_embed("🎵 Şarkı Sonuçları", results)
        view = SearchResultView(results, self.bot, interaction)
        await interaction.followup.send(embed=embed, view=view)

    @search_group.command(name="artist", description="Sanatçı ara (şarkıları listeler)")
    @app_commands.describe(query="Sanatçı adı")
    async def search_artist(self, interaction: discord.Interaction, query: str):
        await interaction.response.defer()
        results = await self._search(f"{query} songs", limit=5)
        if not results:
            await interaction.followup.send("Sonuç bulunamadı.")
            return
        embed = self._build_results_embed(f"🎤 Sanatçı: {query}", results)
        view = SearchResultView(results, self.bot, interaction)
        await interaction.followup.send(embed=embed, view=view)

    @search_group.command(name="album", description="Albüm ara (şarkıları listeler)")
    @app_commands.describe(query="Albüm adı")
    async def search_album(self, interaction: discord.Interaction, query: str):
        await interaction.response.defer()
        results = await self._search(f"{query} album", limit=5)
        if not results:
            await interaction.followup.send("Sonuç bulunamadı.")
            return
        embed = self._build_results_embed(f"💿 Albüm: {query}", results)
        view = SearchResultView(results, self.bot, interaction)
        await interaction.followup.send(embed=embed, view=view)

    @search_group.command(name="playlist", description="Playlist ara (YouTube playlists)")
    @app_commands.describe(query="Playlist adı")
    async def search_playlist(self, interaction: discord.Interaction, query: str):
        await interaction.response.defer()
        results = await self._search(f"{query} playlist", limit=5)
        if not results:
            await interaction.followup.send("Sonuç bulunamadı.")
            return
        embed = self._build_results_embed(f"📋 Playlist: {query}", results)
        view = SearchResultView(results, self.bot, interaction)
        await interaction.followup.send(embed=embed, view=view)

    def _build_results_embed(self, title: str, results: list) -> discord.Embed:
        embed = discord.Embed(title=title, color=Colors.PRIMARY)
        for i, entry in enumerate(results, start=1):
            name = entry.get("title", "Bilinmiyor")
            url = entry.get("webpage_url", "")
            embed.add_field(
                name=f"{i}. {name[:50]}",
                value=f"[Link]({url})" if url else "",
                inline=False
            )
        return embed

class SearchResultView(discord.ui.View):
    def __init__(self, results, bot, interaction):
        super().__init__(timeout=60)
        self.results = results
        self.bot = bot
        self.original_interaction = interaction

    @discord.ui.button(label="1", style=discord.ButtonStyle.primary)
    async def select_1(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._add_song(interaction, 0)

    @discord.ui.button(label="2", style=discord.ButtonStyle.primary)
    async def select_2(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._add_song(interaction, 1)

    @discord.ui.button(label="3", style=discord.ButtonStyle.primary)
    async def select_3(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._add_song(interaction, 2)

    @discord.ui.button(label="4", style=discord.ButtonStyle.primary)
    async def select_4(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._add_song(interaction, 3)

    @discord.ui.button(label="5", style=discord.ButtonStyle.primary)
    async def select_5(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._add_song(interaction, 4)

    async def _add_song(self, interaction: discord.Interaction, index: int):
        if index >= len(self.results):
            await interaction.response.send_message("Geçersiz seçim.", ephemeral=True)
            return

        # Zaman aşımını önlemek için hemen defer
        await interaction.response.defer(ephemeral=True)

        entry = self.results[index]
        song = build_song(entry, interaction.user)
        music_cog = self.bot.get_cog("Music")
        if not music_cog:
            await interaction.followup.send("Müzik sistemi yüklenmemiş.", ephemeral=True)
            return

        if not await music_cog.player.ensure_voice(interaction):
            await interaction.followup.send("Ses kanalına bağlanılamadı.", ephemeral=True)
            return

        state = music_cog.player._get_state(interaction.guild.id)
        await music_cog.player.enqueue_songs(interaction, [song])

        await interaction.followup.send(f"🎵 `{song.title}` sıraya eklendi.", ephemeral=True)
        await music_cog.player._maybe_start_playback(interaction)