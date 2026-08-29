import re
import asyncio
import difflib
import discord
from discord.ext import commands
from typing import Optional, Dict, Any, List
import yt_dlp as youtube_dl
from config import YTDLP_OPTIONS, YTDLP_PLAYLIST_OPTIONS, Colors
from exceptions import YTDLError
from models import Song, LyricLine

def create_embed(title: str, description: str, color=Colors.PRIMARY, thumbnail: Optional[str] = None) -> discord.Embed:
    embed = discord.Embed(title=title, description=description, color=color, timestamp=discord.utils.utcnow())
    if thumbnail:
        embed.set_thumbnail(url=thumbnail)
    return embed

def format_duration(seconds: Optional[int]) -> str:
    if seconds is None or seconds < 0:
        return "Bilinmiyor"
    minutes, sec = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{sec:02d}"
    return f"{minutes}:{sec:02d}"

PlaybackContext = commands.Context | discord.Interaction

def get_voice_client(ctx: PlaybackContext) -> Optional[discord.VoiceClient]:
    return ctx.guild.voice_client if ctx.guild else None

async def extract_song_info(query: str, loop: asyncio.AbstractEventLoop) -> Dict[str, Any]:
    if not query.startswith(("http://", "https://", "www.", "youtu")):
        query = f"ytsearch1:{query}"
    ytdl = youtube_dl.YoutubeDL(YTDLP_OPTIONS)
    try:
        data = await loop.run_in_executor(None, lambda: ytdl.extract_info(query, download=False))
    except Exception as exc:
        raise YTDLError(f"Arama başarısız: {exc}")
    if data is None:
        raise YTDLError("İçerik bulunamadı.")
    if "entries" in data:
        entries = [e for e in data["entries"] if e] if data["entries"] else []
        if not entries:
            raise YTDLError("Arama sonucu bulunamadı.")
        data = entries[0]
    return data

async def extract_playlist_info(url: str, loop: asyncio.AbstractEventLoop) -> Dict[str, Any]:
    ytdl = youtube_dl.YoutubeDL(YTDLP_PLAYLIST_OPTIONS)
    try:
        data = await loop.run_in_executor(None, lambda: ytdl.extract_info(url, download=False))
    except Exception as exc:
        raise YTDLError(f"Playlist çekilemedi: {exc}")
    if data is None:
        raise YTDLError("Playlist bilgisi alınamadı.")
    return data

async def resolve_song_url(webpage_url: str, loop: asyncio.AbstractEventLoop) -> str:
    ytdl = youtube_dl.YoutubeDL(YTDLP_OPTIONS)
    try:
        data = await loop.run_in_executor(None, lambda: ytdl.extract_info(webpage_url, download=False))
    except Exception as exc:
        raise YTDLError(f"Stream URL çözümlenemedi: {exc}")
    if data is None:
        raise YTDLError("Stream verisi alınamadı.")
    stream_url = data.get("url")
    if not stream_url:
        raise YTDLError("Stream URL boş veya geçersiz (video erişilemez/özel olabilir).")
    return stream_url

def build_song(data: Dict[str, Any], requester: Optional[discord.abc.User]) -> Song:
    return Song(
        source_url=data.get("url", "") or "",
        title=data.get("title", "Bilinmeyen Başlık"),
        url=data.get("webpage_url", ""),
        duration=data.get("duration"),
        thumbnail=data.get("thumbnail"),
        uploader=data.get("uploader"),
        requester=requester,
    )

def build_song_from_flat(entry: Dict[str, Any], requester: Optional[discord.abc.User]) -> Song:
    webpage_url = entry.get("url") or entry.get("webpage_url", "")
    if webpage_url and not webpage_url.startswith("http"):
        webpage_url = f"https://www.youtube.com/watch?v={webpage_url}"
    return Song(
        source_url="",
        title=entry.get("title", "Bilinmeyen Başlık"),
        url=webpage_url,
        duration=entry.get("duration"),
        thumbnail=entry.get("thumbnail"),
        uploader=entry.get("uploader") or entry.get("channel"),
        requester=requester,
    )

def build_song_from_playlist_entry(entry: Dict[str, Any], requester: Optional[discord.abc.User]) -> Song:
    if entry.get("webpage_url"):
        return build_song(entry, requester)
    return build_song_from_flat(entry, requester)

def is_ffmpeg_missing_error(exc: Exception) -> bool:
    if isinstance(exc, FileNotFoundError):
        return True
    text = str(exc).lower()
    return "ffmpeg" in text and ("not found" in text or "no such file" in text or "bulunamadı" in text)

def _normalize_text(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"[\(\[].*?[\)\]]", " ", s)
    s = re.sub(r"[^a-z0-9ğüşıöçâîû ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()

def _normalize_turkish(s: str) -> str:
    if not s:
        return s
    replacements = {
        'ş': 's', 'Ş': 'S',
        'ğ': 'g', 'Ğ': 'G',
        'ı': 'i', 'I': 'I', 'İ': 'i',
        'ö': 'o', 'Ö': 'O',
        'ü': 'u', 'Ü': 'U',
        'ç': 'c', 'Ç': 'C'
    }
    return ''.join(replacements.get(c, c) for c in s)

def _similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, _normalize_text(a), _normalize_text(b)).ratio()

LRC_LINE_RE = re.compile(r"^\[(\d+):(\d+(?:\.\d+)?)\](.*)$")

def _parse_lrc(text: str) -> List[LyricLine]:
    lines: List[LyricLine] = []
    for raw in (text or "").splitlines():
        m = LRC_LINE_RE.match(raw.strip())
        if not m:
            continue
        minutes = int(m.group(1))
        seconds = float(m.group(2))
        content = m.group(3).strip()
        lines.append(LyricLine(timestamp=minutes * 60 + seconds, text=content, index=0))
    lines.sort(key=lambda l: l.timestamp)
    for i, l in enumerate(lines):
        l.index = i
    return lines