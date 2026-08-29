import discord
from database import DatabaseManager
from utils.helpers import create_embed, format_duration
from config import Colors
import logging

logger = logging.getLogger(__name__)

class PlaylistDashboardView(discord.ui.View):
    def __init__(self, bot, interaction, playlists):
        super().__init__(timeout=180)
        self.bot = bot
        self.interaction = interaction
        self.playlists = playlists
        self.db = DatabaseManager()
        self.current_playlist_id = None
        self.page = 0
        self.per_page = 5

        if playlists:
            options = []
            for pl in playlists[:25]:
                options.append(discord.SelectOption(label=pl['name'][:50], value=str(pl['id'])))
            if options:
                self.add_item(PlaylistSelect(options, self))

    async def update_embed(self, interaction: discord.Interaction):
        if self.current_playlist_id is None:
            embed = discord.Embed(title="📀 Playlist Dashboard", description="Yukarıdan bir playlist seçin.", color=Colors.PRIMARY)
            await interaction.response.edit_message(embed=embed, view=self)
            return

        songs = await self.db.get_playlist_songs(self.current_playlist_id)
        start = self.page * self.per_page
        end = start + self.per_page
        page_songs = songs[start:end]
        lines = [f"{i+1}. {s.title}" for i, s in enumerate(page_songs, start=start)]
        embed = discord.Embed(
            title=f"📀 Playlist ID: {self.current_playlist_id}",
            description="\n".join(lines) if lines else "Playlist boş.",
            color=Colors.PRIMARY
        )
        embed.set_footer(text=f"Sayfa {self.page+1} / {max(1, (len(songs)+self.per_page-1)//self.per_page)} • Toplam {len(songs)} şarkı")
        
        # View'i yeniden oluştur
        self.clear_items()
        if self.playlists:
            options = []
            for pl in self.playlists[:25]:
                options.append(discord.SelectOption(label=pl['name'][:50], value=str(pl['id'])))
            if options:
                self.add_item(PlaylistSelect(options, self))
        
        if self.page > 0:
            self.add_item(PrevPageButton(self))
        if (self.page + 1) * self.per_page < len(songs):
            self.add_item(NextPageButton(self))
        
        await interaction.response.edit_message(embed=embed, view=self)

class PlaylistSelect(discord.ui.Select):
    def __init__(self, options, dashboard):
        super().__init__(placeholder="Playlist seç...", options=options)
        self.dashboard = dashboard

    async def callback(self, interaction: discord.Interaction):
        self.dashboard.current_playlist_id = int(self.values[0])
        self.dashboard.page = 0
        await self.dashboard.update_embed(interaction)

class PrevPageButton(discord.ui.Button):
    def __init__(self, dashboard):
        super().__init__(label="◀", style=discord.ButtonStyle.secondary)
        self.dashboard = dashboard

    async def callback(self, interaction: discord.Interaction):
        self.dashboard.page -= 1
        await self.dashboard.update_embed(interaction)

class NextPageButton(discord.ui.Button):
    def __init__(self, dashboard):
        super().__init__(label="▶", style=discord.ButtonStyle.secondary)
        self.dashboard = dashboard

    async def callback(self, interaction: discord.Interaction):
        self.dashboard.page += 1
        await self.dashboard.update_embed(interaction)

# Favorites Dashboard
class FavoritesDashboardView(discord.ui.View):
    def __init__(self, bot, interaction, favorites):
        super().__init__(timeout=180)
        self.bot = bot
        self.interaction = interaction
        self.favorites = favorites
        self.db = DatabaseManager()
        self.page = 0
        self.per_page = 10

        self.add_item(PlayAllFavoritesButton(self))
        self.add_item(RemoveFavoriteButton(self))

    async def update_embed(self, interaction: discord.Interaction):
        start = self.page * self.per_page
        end = start + self.per_page
        page_favs = self.favorites[start:end]
        lines = [f"{i+1}. {s.title}" for i, s in enumerate(page_favs, start=start)]
        embed = discord.Embed(
            title="❤️ Favorites Dashboard",
            description="\n".join(lines) if lines else "Favori yok.",
            color=Colors.PRIMARY
        )
        embed.set_footer(text=f"Sayfa {self.page+1} / {max(1, (len(self.favorites)+self.per_page-1)//self.per_page)} • Toplam {len(self.favorites)} şarkı")
        
        self.clear_items()
        self.add_item(PlayAllFavoritesButton(self))
        self.add_item(RemoveFavoriteButton(self))
        
        if self.page > 0:
            self.add_item(PrevPageFavButton(self))
        if (self.page + 1) * self.per_page < len(self.favorites):
            self.add_item(NextPageFavButton(self))
        
        await interaction.response.edit_message(embed=embed, view=self)

class PlayAllFavoritesButton(discord.ui.Button):
    def __init__(self, dashboard):
        super().__init__(label="▶ Tümünü Çal", style=discord.ButtonStyle.success)
        self.dashboard = dashboard

    async def callback(self, interaction: discord.Interaction):
        music_cog = self.dashboard.bot.get_cog("Music")
        if not music_cog:
            await interaction.response.send_message("Müzik sistemi yüklenmemiş.", ephemeral=True)
            return
        if not self.dashboard.favorites:
            await interaction.response.send_message("Favori yok.", ephemeral=True)
            return
        if not await music_cog.player.ensure_voice(interaction):
            await interaction.response.send_message("Ses kanalına bağlanılamadı.", ephemeral=True)
            return
        state = music_cog.player._get_state(interaction.guild.id)
        for song in self.dashboard.favorites:
            song.requester = interaction.user
        await music_cog.player.enqueue_songs(interaction, list(self.dashboard.favorites))
        await interaction.response.send_message(f"{len(self.dashboard.favorites)} şarkı sıraya eklendi.", ephemeral=True)
        await music_cog.player._maybe_start_playback(interaction)

class RemoveFavoriteButton(discord.ui.Button):
    def __init__(self, dashboard):
        super().__init__(label="🗑 Seçili Favoriyi Sil", style=discord.ButtonStyle.danger)
        self.dashboard = dashboard

    async def callback(self, interaction: discord.Interaction):
        modal = RemoveFavoriteModal(self.dashboard)
        await interaction.response.send_modal(modal)

class RemoveFavoriteModal(discord.ui.Modal, title="Favori Sil"):
    index = discord.ui.TextInput(label="Sıra Numarası", placeholder="Örn: 1", required=True)

    def __init__(self, dashboard):
        super().__init__()
        self.dashboard = dashboard

    async def on_submit(self, interaction: discord.Interaction):
        try:
            idx = int(self.index.value) - 1
            if idx < 0 or idx >= len(self.dashboard.favorites):
                await interaction.response.send_message("Geçersiz numara.", ephemeral=True)
                return
            song = self.dashboard.favorites[idx]
            db = DatabaseManager()
            await db.remove_favorite(interaction.user.id, song.url)
            self.dashboard.favorites = await db.get_favorites(interaction.user.id)
            # update_embed() itself calls interaction.response.edit_message() -
            # that's the ONE allowed response for this interaction. Calling
            # response.send_message() first (as before) and then
            # update_embed()'s response.edit_message() second raised
            # discord.InteractionResponded, since a single interaction can
            # only be acknowledged once via `.response`. Do the dashboard
            # update first (that's the actual required response), then send
            # the confirmation as a followup, which is always valid after
            # the initial response regardless of which method produced it.
            await self.dashboard.update_embed(interaction)
            await interaction.followup.send(f"`{song.title}` favorilerden silindi.", ephemeral=True)
        except ValueError:
            await interaction.response.send_message("Lütfen sayı girin.", ephemeral=True)

class PrevPageFavButton(discord.ui.Button):
    def __init__(self, dashboard):
        super().__init__(label="◀", style=discord.ButtonStyle.secondary)
        self.dashboard = dashboard

    async def callback(self, interaction: discord.Interaction):
        self.dashboard.page -= 1
        await self.dashboard.update_embed(interaction)

class NextPageFavButton(discord.ui.Button):
    def __init__(self, dashboard):
        super().__init__(label="▶", style=discord.ButtonStyle.secondary)
        self.dashboard = dashboard

    async def callback(self, interaction: discord.Interaction):
        self.dashboard.page += 1
        await self.dashboard.update_embed(interaction)