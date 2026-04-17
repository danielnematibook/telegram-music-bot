# 🎵 Music Compressor Bot

A high‑performance Telegram bot that compresses audio files and extracts & compresses audio from videos. Built with **aiogram 3** and **FFmpeg**, featuring a processing queue, rate limiting, stereo‑to‑mono conversion, and full group support.

[![Deploy on Railway](https://railway.app/button.svg)](https://railway.app/template/your-template-link)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/)
[![Telegram Bot](https://img.shields.io/badge/Telegram-Bot-blue.svg)](https://t.me/your_bot_username)

---

## ✨ Features

- 🎵 **Compress audio files** (MP3, M4A, OGG, WAV, FLAC)
- 🎬 **Extract & compress audio from videos** (MP4, MKV, AVI, MOV)
- 🎚️ **Stereo → Mono conversion** – reduces file size up to 40% (perfect for podcasts/audiobooks)
- ⚙️ **Three quality presets** – Low (32kbps), Medium (64kbps), High (128kbps)
- 🌍 **Bilingual** – Persian & English interface
- 👥 **Full group support** – works when bot is admin
- ⏳ **Processing queue** – 3 concurrent workers, no more overload
- 🛡️ **Rate limiting** – 5 files per minute per user
- 📦 **File size limit** – 70 MB max
- 🧹 **Auto cleanup** – temporary files are deleted immediately after sending
- 💖 **Colored donate button** (primary blue style)

---

## 🚀 Quick Deployment on Railway

[![Deploy on Railway](https://railway.app/button.svg)](https://railway.app/template/your-template-link)

1. Fork / clone this repository to GitHub.
2. Log in to [Railway](https://railway.app) → **New Project** → **Deploy from GitHub repo**.
3. Select your repo.
4. In **Variables**, add `BOT_TOKEN` with your Telegram bot token (get it from [@BotFather](https://t.me/BotFather)).
5. Railway automatically detects the Dockerfile and installs FFmpeg.
6. Click **Deploy** – done!

---

## 📦 Local Installation

### Prerequisites
- Python 3.11+
- FFmpeg ([download](https://ffmpeg.org/download.html)) – make sure `ffmpeg` is in your PATH.

### Steps
```bash
# Clone the repository
git clone https://github.com/yourusername/music-compressor-bot.git
cd music-compressor-bot

# Install dependencies
pip install -r requirements.txt

# Create .env file with your bot token
echo "BOT_TOKEN=your_telegram_bot_token" > .env

# Run the bot
python bot.py
🤖 Bot Commands
Command	Description
/start	Welcome message and main menu
/quality	Set compression quality (low/medium/high)
/mono	Enable/disable mono mode (stereo → mono)
/about	Show bot information and version
/help	Full usage guide
/compress	(In groups) Compress the replied audio/video file
👥 Group Usage
Add the bot to your group and make it an administrator (needed to delete confirmation messages and send files).

Any member sends an audio or video file.

The bot replies with an inline keyboard:
✅ Compress | ❌ Cancel

The user clicks Compress.

The request enters the processing queue.
When finished, the compressed audio is sent back to the group with a message showing the user’s name and the reduction percentage.

Alternatively, a user can reply to the file with /compress to skip the confirmation step.

⚠️ The bot will warn you if it is not an admin.

💖 Donate
This bot is completely free, but if you would like to support its development and server costs, you can send a donation to the following addresses:

Ton Network
UQCv_m88yafoOWMD9h9MMglPP3DSiBL5xbLiU7akxWs5Q0pk

USDT (TRC20)
THXUWRaBgEyC27e8xC9JWG7unvygkFGNov

Thank you for your support! 🙏

🛠️ Project Structure
music-compressor-bot/
├── bot.py                # Main bot code
├── requirements.txt      # Python dependencies
├── Dockerfile            # Docker configuration for Railway
├── database.db           # SQLite database (user settings)
├── downloads/            # Temporary folder (auto‑created)
└── README.md             # This file


📄 License
This project is licensed under the MIT License. See the LICENSE file for details.

👨‍💻 Author
Daniel Nemati


