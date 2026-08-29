import os
import discord

class Colors:
    PRIMARY = discord.Color.from_rgb(86, 119, 252)
    SUCCESS = discord.Color.from_rgb(72, 187, 120)
    ERROR = discord.Color.from_rgb(235, 87, 87)
    WARNING = discord.Color.from_rgb(86, 165, 255)
    PLAYING = discord.Color.from_rgb(121, 96, 206)
    INFO = discord.Color.from_rgb(123, 142, 169)

# FFmpeg user-agent (bazı CDN'lerin engellemesini aşmak için)
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

# Optional cookies.txt (Netscape format) for yt-dlp. Entirely optional: if the
# file isn't present, yt-dlp options simply omit `cookiefile` and everything
# (search, normal resolution, playlists) continues to work unauthenticated -
# this must never be a hard requirement for the bot to function.
YTDLP_COOKIES_FILE = os.getenv("YTDLP_COOKIES_FILE", "cookies.txt")


def _base_ytdlp_options(playlist: bool) -> dict:
    opts = {
        "format": "bestaudio/best",
        "restrictfilenames": True,
        "noplaylist": not playlist,
        "nocheckcertificate": True,
        "ignoreerrors": playlist,
        "logtostderr": False,
        "quiet": True,
        "no_warnings": True,
        "source_address": "0.0.0.0",
    }
    if playlist:
        opts["extract_flat"] = False
        opts["playlist_items"] = "1:500"
    else:
        opts["default_search"] = "auto"
    if YTDLP_COOKIES_FILE and os.path.isfile(YTDLP_COOKIES_FILE):
        opts["cookiefile"] = YTDLP_COOKIES_FILE
    return opts


YTDLP_OPTIONS = _base_ytdlp_options(playlist=False)
YTDLP_PLAYLIST_OPTIONS = _base_ytdlp_options(playlist=True)

SPOTIFY_PLAYLIST_TRACK_LIMIT = 200

# Remote (HTTP/YouTube/CDN) streams benefit from reconnect handling since the
# connection can drop mid-stream; local files are opened straight off disk and
# have no such concept - handing FFmpeg network-reconnect flags for a local
# file input does nothing useful and, on some FFmpeg builds/inputs, can cause
# the input to fail to open at all. These are kept as two separate constants
# so player.py can pick the right one per source instead of always using the
# network-oriented options.
FFMPEG_BEFORE_OPTIONS_REMOTE = f"-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -user_agent \"{USER_AGENT}\""
FFMPEG_BEFORE_OPTIONS_LOCAL = ""
# Backward-compatible alias (remote behavior) for any code that hasn't been
# updated to pick a source-aware variant yet.
FFMPEG_BEFORE_OPTIONS = FFMPEG_BEFORE_OPTIONS_REMOTE

FFMPEG_OPTIONS = "-vn"
NEGATIVE_CACHE_TTL = 3600
