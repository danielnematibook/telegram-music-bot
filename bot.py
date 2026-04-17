import os
import asyncio
import sqlite3
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from dotenv import load_dotenv
import subprocess

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

DB = "database.db"
DOWNLOAD_DIR = "downloads"

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# -------- DATABASE --------
def init_db():
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS files (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        file_path TEXT,
        expire_at TEXT
    )
    """)
    conn.commit()
    conn.close()

init_db()

# -------- LANGUAGE --------
user_lang = {}

def get_text(lang, key):
    texts = {
        "start": {
            "fa": "🎵 موزیک ارسال کنید تا فشرده شود",
            "en": "🎵 Send music to compress"
        },
        "done": {
            "fa": "✅ آماده شد",
            "en": "✅ Done"
        },
        "donate": {
            "fa": "💖 این بات رایگان است، برای حمایت دونیت کنید\nسازنده: Daniel Nemati",
            "en": "💖 This bot is free, support us\nCreator: Daniel Nemati"
        }
    }
    return texts[key][lang]

# -------- KEYBOARD --------
def main_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🇮🇷 فارسی", callback_data="fa"),
            InlineKeyboardButton(text="🇬🇧 English", callback_data="en")
        ],
        [
            InlineKeyboardButton(text="💖 Donate", callback_data="donate")
        ]
    ])

# -------- COMPRESS --------
def compress_audio(inp, out):
    subprocess.run(["ffmpeg", "-i", inp, "-b:a", "64k", out])

# -------- START --------
@dp.message(Command("start"))
async def start(msg: types.Message):
    user_lang[msg.from_user.id] = "en"
    await msg.answer("Select language", reply_markup=main_kb())

# -------- LANGUAGE --------
@dp.callback_query()
async def callbacks(call: types.CallbackQuery):
    if call.data in ["fa", "en"]:
        user_lang[call.from_user.id] = call.data
        await call.message.edit_text(
            get_text(call.data, "start"),
            reply_markup=main_kb()
        )
    elif call.data == "donate":
        lang = user_lang.get(call.from_user.id, "en")
        await call.answer(get_text(lang, "donate"), show_alert=True)

# -------- AUDIO --------
@dp.message()
async def handle_audio(msg: types.Message):
    if not (msg.audio or msg.document):
        return

    lang = user_lang.get(msg.from_user.id, "en")

    file = await bot.get_file(msg.audio.file_id if msg.audio else msg.document.file_id)

    input_file = f"{DOWNLOAD_DIR}/{msg.from_user.id}_in.mp3"
    output_file = f"{DOWNLOAD_DIR}/{msg.from_user.id}_out.mp3"

    await bot.download_file(file.file_path, input_file)

    compress_audio(input_file, output_file)

    expire = datetime.now() + timedelta(days=1)

    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO files (user_id, file_path, expire_at) VALUES (?, ?, ?)",
        (msg.from_user.id, output_file, expire.isoformat())
    )
    conn.commit()
    conn.close()

    await msg.answer(get_text(lang, "done"))
    await msg.answer_audio(types.FSInputFile(output_file))

    os.remove(input_file)

# -------- CLEANUP --------
async def cleanup():
    while True:
        conn = sqlite3.connect(DB)
        cur = conn.cursor()

        now = datetime.now().isoformat()
        cur.execute("SELECT id, file_path FROM files WHERE expire_at < ?", (now,))
        rows = cur.fetchall()

        for r in rows:
            if os.path.exists(r[1]):
                os.remove(r[1])
            cur.execute("DELETE FROM files WHERE id = ?", (r[0],))

        conn.commit()
        conn.close()

        await asyncio.sleep(3600)

# -------- MAIN --------
async def main():
    asyncio.create_task(cleanup())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
