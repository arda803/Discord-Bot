import os
import asyncio
import logging
import aiohttp
import discord
from discord.ext import commands
from dotenv import load_dotenv

from cogs.search import Search
from cogs.dashboards import Dashboards   # <-- added import
from config import Colors
from cogs.music import Music
from cogs.favorites import Favorites
from cogs.playlists import Playlists
from utils.helpers import create_embed

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

class MusicBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.voice_states = True
        intents.guilds = True
        super().__init__(command_prefix="!", intents=intents, help_command=None)

    async def setup_hook(self):
        # Music cog is the bot's core purpose - if it fails to load, the bot
        # would otherwise still come online and look healthy while having no
        # actual music functionality at all, which is misleading. Let this
        # exception propagate instead of swallowing it: discord.py surfaces
        # setup_hook exceptions out of bot.start(), so main() below will log
        # it clearly and exit rather than the bot silently running broken.
        try:
            await self.add_cog(Music(self))
            logger.info("[COGS] Music cog loaded successfully")
        except Exception as e:
            logger.critical(f"[COGS] CRITICAL: Failed to load Music cog - the bot cannot run without it: {e}")
            import traceback
            traceback.print_exc()
            raise

        # Favorites cog'u yükle
        try:
            await self.add_cog(Favorites(self))
            logger.info("[COGS] Favorites cog loaded successfully")
        except Exception as e:
            logger.error(f"[COGS] Failed to load Favorites cog: {e}")
            import traceback
            traceback.print_exc()

        # Playlists cog'u yükle
        try:
            await self.add_cog(Playlists(self))
            logger.info("[COGS] Playlists cog loaded successfully")
        except Exception as e:
            logger.error(f"[COGS] Failed to load Playlists cog: {e}")
            import traceback
            traceback.print_exc()

        # Search cog
        try:
            await self.add_cog(Search(self))
            logger.info("[COGS] Search cog loaded successfully")
        except Exception as e:
            logger.error(f"[COGS] Failed to load Search cog: {e}")
            import traceback
            traceback.print_exc()

        # Dashboards cog (separate try/except)
        try:
            await self.add_cog(Dashboards(self))
            logger.info("[COGS] Dashboards cog loaded successfully")
        except Exception as e:
            logger.error(f"[COGS] Failed to load Dashboards cog: {e}")
            import traceback
            traceback.print_exc()

        # Slash komutlarını senkronize et
        try:
            synced = await self.tree.sync()
            logger.info(f"{len(synced)} slash komutu senkronize edildi.")
            for cmd in synced:
                logger.info(f"  - /{cmd.name}")
        except Exception as e:
            logger.error(f"Slash komutları senkronize edilemedi: {e}")
            import traceback
            traceback.print_exc()

    async def on_ready(self):
        logger.info(f"Giriş yapıldı: {self.user} (ID: {self.user.id})")

async def main():
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        logger.error("DISCORD_BOT_TOKEN .env dosyasında bulunamadı.")
        return

    bot = MusicBot()

    @bot.tree.error
    async def on_app_command_error(interaction: discord.Interaction, error: discord.app_commands.AppCommandError):
        logger.error(f"App command hatası ({interaction.command.name if interaction.command else '?'}): {error}")
        embed = create_embed("Beklenmeyen Hata", "Komut işlenirken bir hata oluştu. Lütfen tekrar deneyin.", Colors.ERROR)
        try:
            if interaction.response.is_done():
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                await interaction.response.send_message(embed=embed, ephemeral=True)
        except Exception:
            pass

    try:
        async with bot:
            await bot.start(token)
    except discord.LoginFailure:
        logger.error("Geçersiz DISCORD_BOT_TOKEN.")
    except aiohttp.ClientConnectorDNSError as e:
        logger.error(f"DNS hatası – Discord sunucusuna bağlanılamadı: {e}")
    except Exception as e:
        logger.error(f"Bot başlatılamadı: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(main())