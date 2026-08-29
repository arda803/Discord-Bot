import time
import asyncio
import discord
from utils.helpers import format_duration, PlaybackContext, get_voice_client
from exceptions import VoiceError

class SeekModal(discord.ui.Modal, title="Şarkıda Sarma"):
    seconds = discord.ui.TextInput(label="Saniye", placeholder="Örn: 120", required=True)

    def __init__(self, player, ctx: PlaybackContext):
        super().__init__()
        self.player = player
        self.ctx = ctx

    async def on_submit(self, interaction: discord.Interaction):
        try:
            seconds = int(self.seconds.value)
            await self.player.perform_seek(self.ctx, seconds)
            await interaction.response.send_message(
                f"⏩ Şarkı {format_duration(seconds)} noktasına sarıldı.", ephemeral=True
            )
        except ValueError:
            await interaction.response.send_message("Lütfen geçerli bir saniye girin.", ephemeral=True)
        except (VoiceError,) as e:
            await interaction.response.send_message(f"Hata: {e}", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"Hata: {e}", ephemeral=True)

class MusicControlView(discord.ui.View):
    def __init__(self, player, ctx: PlaybackContext):
        super().__init__(timeout=None)
        self.player = player
        self.ctx = ctx

    @discord.ui.button(label="Duraklat", style=discord.ButtonStyle.primary, emoji="⏸️")
    async def pause_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = get_voice_client(self.ctx)
        if not vc or not vc.is_playing():
            await interaction.response.send_message("Şu anda çalan bir şarkı yok.", ephemeral=True)
            return
        state = self.player._get_state(self.ctx.guild.id)
        state.paused_at = time.monotonic()
        vc.pause()
        await interaction.response.send_message("⏸️ Duraklatıldı.", ephemeral=True)

    @discord.ui.button(label="Devam Et", style=discord.ButtonStyle.success, emoji="▶️")
    async def resume_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = get_voice_client(self.ctx)
        if not vc or not vc.is_paused():
            await interaction.response.send_message("Duraklatılmış bir şarkı yok.", ephemeral=True)
            return
        state = self.player._get_state(self.ctx.guild.id)
        if state.paused_at and state.started_at:
            state.playback_offset += state.paused_at - state.started_at
        state.started_at = time.monotonic()
        state.paused_at = 0.0
        vc.resume()
        await interaction.response.send_message("▶️ Devam ediyor.", ephemeral=True)

    @discord.ui.button(label="Geç", style=discord.ButtonStyle.secondary, emoji="⏭️")
    async def skip_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = get_voice_client(self.ctx)
        if not vc or not vc.is_playing():
            await interaction.response.send_message("Şu anda çalan bir şarkı yok.", ephemeral=True)
            return
        state = self.player._get_state(self.ctx.guild.id)
        if state.seek_target is not None:
            state.playback_generation += 1
            state.seek_target = None
            state.seek_retry_count = 0
        vc.stop()
        await interaction.response.send_message("⏭️ Şarkı atlandı.", ephemeral=True)

    @discord.ui.button(label="Durdur & Temizle", style=discord.ButtonStyle.danger, emoji="⏹️")
    async def stop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = get_voice_client(self.ctx)
        if not vc:
            await interaction.response.send_message("Bot zaten ses kanalında değil.", ephemeral=True)
            return
        state = self.player._get_state(self.ctx.guild.id)
        if self.player.karaoke_engine:
            await self.player.karaoke_engine.disable_karaoke(state)
        state.autoplay = False
        state.queue.clear()
        if vc.is_playing() or vc.is_paused():
            # Don't null current_song before stop() - after_callback treats a
            # None current_song as an intentional stop and skips stats/history
            # bookkeeping entirely. Let the normal callback path handle it.
            vc.stop()
            await asyncio.sleep(0.1)
        else:
            state.current_song = None
            state.active = False
        await vc.disconnect()
        await interaction.response.send_message("⏹️ Sıra temizlendi ve kanaldan ayrıldım.", ephemeral=True)

    @discord.ui.button(label="Sarma", style=discord.ButtonStyle.primary, emoji="⏩")
    async def seek_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = SeekModal(self.player, self.ctx)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Karaoke", style=discord.ButtonStyle.secondary, emoji="🎤")
    async def karaoke_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = get_voice_client(self.ctx)
        if not vc or not (vc.is_playing() or vc.is_paused()):
            await interaction.response.send_message("Şu anda çalan bir şarkı yok.", ephemeral=True)
            return
        state = self.player._get_state(self.ctx.guild.id)
        if state.karaoke_enabled:
            await self.player.karaoke_engine.disable_karaoke(state)
            await interaction.response.send_message("🎤 Karaoke modu kapatıldı.", ephemeral=True)
        else:
            await interaction.response.send_message("🎤 Karaoke modu açıldı.", ephemeral=True)
            await self.player.karaoke_engine.enable_karaoke(self.ctx, state, interaction.channel)

    @discord.ui.button(label="Favoriye Ekle", style=discord.ButtonStyle.success, emoji="❤️")
    async def favorite_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = self.player._get_state(self.ctx.guild.id)
        if not state.current_song:
            await interaction.response.send_message("Şu anda çalan şarkı yok.", ephemeral=True)
            return
        song = state.current_song
        db = self.player.db
        success = await db.add_favorite(interaction.user.id, song)
        if success:
            await interaction.response.send_message(f"❤️ `{song.title}` favorilerinize eklendi!", ephemeral=True)
        else:
            await interaction.response.send_message("Bu şarkı zaten favorilerinizde.", ephemeral=True)

    @discord.ui.button(label="Baştan", style=discord.ButtonStyle.secondary, emoji="⏮️")
    async def replay_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = self.player._get_state(self.ctx.guild.id)
        if not state.current_song:
            await interaction.response.send_message("Şu anda çalan şarkı yok.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            await self.player.replay(self.ctx)
            await interaction.followup.send("🔄 Şarkı baştan başlatıldı.", ephemeral=True)
        except VoiceError as e:
            await interaction.followup.send(f"Hata: {e}", ephemeral=True)

    @discord.ui.button(label="Önceki", style=discord.ButtonStyle.secondary, emoji="◀️")
    async def previous_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        try:
            if await self.player.previous(self.ctx):
                await interaction.followup.send("↩️ Önceki şarkı çalınıyor.", ephemeral=True)
            else:
                await interaction.followup.send("Geçmişte şarkı yok.", ephemeral=True)
        except VoiceError as e:
            await interaction.followup.send(f"Hata: {e}", ephemeral=True)