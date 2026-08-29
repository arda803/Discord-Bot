# 🎵 KEDU - Discord Music Bot

KEDU is a feature-rich Discord music bot designed to provide a smooth, simple, and enjoyable music experience.

Unlike many music bots that place features behind premium subscriptions, KEDU focuses on providing unlimited music playback without unnecessary restrictions.

---

## ✨ Features

### 🎵 Music Playback
- Play music directly from supported sources
- Search and play your favorite songs
- High-quality audio playback
- Music queue system
- Skip, pause, resume and stop playback

### ⏩ Playback Controls
- Seek to a specific position in a song
- Control the playback position easily
- Adjust the volume

### 📋 Queue System
- View the current music queue
- Add multiple songs to the queue
- Automatically play the next song

### ❤️ Favorites
- Save your favorite songs
- Quickly access and play saved music

### 🎛️ Easy Controls
- Slash commands
- Interactive music controls
- Simple and user-friendly interface

### 📊 Live Bot Status
KEDU automatically updates its Discord status with live information such as:

- Total number of servers
- Currently active servers
- Number of listeners
- Currently playing music
- Volume level
- Songs waiting in the queue

The bot status automatically rotates between server statistics and music information.
## 🤖 Bot Commands

All commands are used with `/`.

### 🎵 Playback Controls

- `/play <query>` — Play a song or Spotify link, or add it to the queue
- `/playlist <url>` — Add a YouTube playlist to the queue
- `/skip` — Skip the currently playing song
- `/pause` — Pause the current song
- `/resume` — Resume playback
- `/stop` — Clear the queue and leave the voice channel
- `/loop <mode>` — Loop mode: single, queue, or off
- `/shuffle` — Shuffle the songs in the queue
- `/remove <position>` — Remove a song from the queue
- `/seek <seconds>` — Seek forward or backward in the song
- `/karaoke` — Enable or disable synchronized karaoke lyrics

### 🔊 Voice & Queue Management

- `/join` — Join your current voice channel
- `/leave` — Leave the voice channel
- `/volume <0-200>` — Adjust the volume
- `/queue [page]` — Display the current music queue
- `/nowplaying` — Show the currently playing song

### ❤️ Favorites

- `/favori` — Add the currently playing song to your favorites
- `/favoriler` — Display your favorite songs
- `/favoriçal` — Add your favorite songs to the queue and start playing
- `/favorisil <position>` — Remove a song from your favorites

### 📀 User Playlists

- `/playlist_oluştur <name>` — Create a new playlist
- `/playlist_ekle <playlist_id>` — Add the currently playing song to a playlist
- `/playlist_queue_kaydet <playlist_id>` — Save the current queue to a playlist
- `/playlist_göster <playlist_id> [page]` — Display playlist contents
- `/playlist_shuffle <playlist_id>` — Load a playlist in shuffled order
- `/playlist_çal <playlist_id>` — Add a playlist to the queue and start playing
- `/playlist_sil <playlist_id>` — Delete a playlist
- `/playlist_remove_song <playlist_id> <position>` — Remove a song from a playlist

> **Note:** All commands start with `/`. You can also use the interactive buttons while music is playing.
---

## 🚀 Installation

### 1. Clone the repository

```bash
git clone https://github.com/arda803/Discord-Bot.git
cd Discord-Bot
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Install FFmpeg

KEDU requires FFmpeg for audio playback.

Make sure FFmpeg is installed and available in your system PATH.

Check your installation:

```bash
ffmpeg -version
```

### 4. Create your `.env` file

Create a file named `.env` in the project directory:

```env
DISCORD_TOKEN=YOUR_BOT_TOKEN

SPOTIFY_CLIENT_ID=YOUR_SPOTIFY_CLIENT_ID
SPOTIFY_CLIENT_SECRET=YOUR_SPOTIFY_CLIENT_SECRET
```

> ⚠️ Never upload your `.env` file or Discord token to GitHub.

---

## ▶️ Running the Bot

```bash
python iamgevoice.py
```

---

## 🛠️ Requirements

- Python 3.10+
- discord.py
- PyNaCl
- yt-dlp
- FFmpeg

---

## 🔒 Security

The following files are intentionally excluded from the repository:

- `.env`
- Downloaded music files
- Virtual environments
- Local databases
- Cache files

Never share your Discord bot token publicly.

---

## 🤝 Community

Join our Discord community to get support, share feedback, and follow the development of KEDU!

---

## 📌 Disclaimer

This project is intended for educational and personal use.

---

### 🎧 Enjoy unlimited music with KEDU!


