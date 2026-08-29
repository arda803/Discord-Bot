from database import DatabaseManager
from models import Song
from typing import Dict, List, Any

class StatsManager:
    def __init__(self, db: DatabaseManager):
        self.db = db

    async def record_play(self, user_id: int, guild_id: int, song: Song, duration_played: int) -> None:
        await self.db.update_stats(user_id, guild_id, song, duration_played)

    async def get_user_stats(self, user_id: int) -> Dict[str, int]:
        return await self.db.get_user_stats(user_id)

    async def get_server_stats(self, guild_id: int) -> int:
        return await self.db.get_server_stats(guild_id)

    async def get_global_stats(self) -> int:
        return await self.db.get_global_stats()

    async def get_top_songs_global(self, limit: int = 10) -> List[Dict[str, Any]]:
        return await self.db.get_top_songs_global(limit)

    async def get_top_songs_guild(self, guild_id: int, limit: int = 10) -> List[Dict[str, Any]]:
        return await self.db.get_top_songs_guild(guild_id, limit)