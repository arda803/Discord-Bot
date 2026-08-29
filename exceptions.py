from discord.ext import commands

class VoiceError(commands.CommandError):
    pass

class YTDLError(commands.CommandError):
    pass

class SpotifyError(commands.CommandError):
    pass

class AudioSourceError(commands.CommandError):
    pass