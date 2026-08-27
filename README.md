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
### Bot Commands
🎵 Müzik Botu Yardım Menüsü
Aşağıda botun tüm slash komutlarını ve açıklamalarını bulabilirsiniz.
▶️ Oynatma Kontrolleri
/play <sorgu> - Şarkı veya Spotify linki oynat / sıraya ekle\
/playlist <url> - YouTube playlist'ini sıraya ekle\
/skip - Şu an çalan şarkıyı atla\
/pause - Şarkıyı duraklat\
/resume - Duraklatılmış şarkıyı devam ettir\
/stop - Sırayı temizle ve kanaldan ayrıl\
/loop <mod> - Döngü modu: tek, sıra, kapat\
/shuffle - Kuyruktaki şarkıları karıştır\
/remove <sıra> - Kuyruktan şarkı kaldır\
/seek <saniye> - Şarkıda ileri/geri sar\
/karaoke - Senkronize karaoke sözlerini aç/kapat
🔊 Ses & Sıra Yönetimi
/join - Bulunduğunuz ses kanalına katıl\n/leave - Ses kanalından ayrıl\n/volume <0-200> - Ses seviyesini ayarla\n/queue [sayfa] - Şarkı sırasını göster\n/nowplaying - Şu an çalan şarkıyı göster
❤️ Favoriler
/favori - Şu an çalan şarkıyı favorilere ekle\n/favoriler - Favori şarkılarını listele\n/favoriçal - Favori şarkılarını sıraya ekleyip oynat\n/favorisil <sıra> - Favorilerden şarkı sil
📀 Kullanıcı Playlistleri
/playlist_oluştur <isim> - Yeni bir playlist oluştur\n/playlist_ekle <playlist_id> - Çalan şarkıyı playlist'e ekle\n/playlist_queue_kaydet <playlist_id> - Kuyruktaki şarkıları playlist'e kaydet\n/playlist_göster <playlist_id> [sayfa] - Playlist içeriğini göster\n/playlist_shuffle <playlist_id> - Playlist'i karıştırarak yükle\n/playlist_çal <playlist_id> - Playlist'i sıraya ekleyip oynat\n/playlist_sil <playlist_id> - Playlist'i sil\n/playlist_remove_song <playlist_id> <sıra> - Playlist'ten şarkı sil
Not: Tüm komutlar / ile başlar. Şarkı sırasında butonları da kullanabilirsiniz.
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


