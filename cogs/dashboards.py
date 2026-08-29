import discord
from discord import app_commands
from discord.ext import commands
from database import DatabaseManager
from ui.dashboard_views import PlaylistDashboardView, FavoritesDashboardView
from utils.helpers import create_embed
from config import Colors
import logging

logger = logging.getLogger(__name__)

class Dashboards(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.db = DatabaseManager()

    async def cog_load(self) -> None:
        await self.db.init()
        logger.info("[DASHBOARDS] Dashboards cog ready")

    @app_commands.command(name="playlist_dashboard", description="Playlist yönetim panelini açar.")
    async def playlist_dashboard(self, interaction: discord.Interaction):
        playlists = await self.db.get_playlists(interaction.user.id)
        view = PlaylistDashboardView(self.bot, interaction, playlists)
        embed = discord.Embed(title="📀 Playlist Dashboard", description="Playlist'lerinizi yönetin.", color=Colors.PRIMARY)
        await interaction.response.send_message(embed=embed, view=view)

    @app_commands.command(name="favorites_dashboard", description="Favori şarkı yönetim panelini açar.")
    async def favorites_dashboard(self, interaction: discord.Interaction):
        favorites = await self.db.get_favorites(interaction.user.id)
        view = FavoritesDashboardView(self.bot, interaction, favorites)
        embed = discord.Embed(title="❤️ Favorites Dashboard", description="Favori şarkılarınızı yönetin.", color=Colors.PRIMARY)
        await interaction.response.send_message(embed=embed, view=view)