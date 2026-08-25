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
