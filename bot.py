import os
import asyncio
import sqlite3
import shutil
import logging
import subprocess
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile
from aiogram.filters import Command
from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN not found")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

DB = "database.db"
DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

FFMPEG_PATH = shutil.which("ffmpeg")
if FFMPEG_PATH is None:
    logger.error("FFmpeg not found")
    raise RuntimeError("FFmpeg missing")
logger.info(f"FFmpeg at {FFMPEG_PATH}")

def init_db():
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        lang TEXT DEFAULT 'en',
        quality TEXT DEFAULT 'medium'
    )
    """)
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

user_lang = {}

QUALITIES = {
    "low": {"bitrate": "32k", "name_fa": "خیلی کم حجم", "name_en": "Very low"},
    "medium": {"bitrate": "64k", "name_fa": "معمولی", "name_en": "Medium"},
    "high": {"bitrate": "128k", "name_fa": "کیفیت بالا", "name_en": "High"}
}

def get_text(lang, key, **kwargs):
    texts = {
        "start": {
            "fa": "🎵 به ربات فشرده‌ساز موزیک خوش آمدید.\nیک فایل صوتی ارسال کنید تا فشرده شود.\n\nدستورات:\n/quality - تنظیم کیفیت",
            "en": "🎵 Welcome to Music Compressor.\nSend an audio file to compress.\n\nCommands:\n/quality - Set quality"
        },
        "processing": {"fa": "⏳ در حال پردازش... لطفاً صبر کنید", "en": "⏳ Processing... Please wait"},
        "done": {"fa": "✅ فشرده‌سازی انجام شد\n📉 کاهش حجم: {percent:.1f}%", "en": "✅ Done\n📉 Reduction: {percent:.1f}%"},
        "error": {"fa": "❌ خطا در پردازش", "en": "❌ Error"},
        "quality_set": {"fa": "کیفیت به {name} تغییر کرد", "en": "Quality set to {name}"}
    }
    txt = texts[key][lang]
    return txt.format(**kwargs) if kwargs else txt

def main_kb(lang):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🇮🇷 فارسی", callback_data="fa"),
         InlineKeyboardButton(text="🇬🇧 English", callback_data="en")],
        [InlineKeyboardButton(text="⚙️ کیفیت", callback_data="quality_menu")],
        # دکمه دونیت با رنگ آبی (primary)
        [InlineKeyboardButton(text="💖 Donate", callback_data="donate", style="primary")]
    ])

def quality_kb(lang, current):
    buttons = []
    for qid, q in QUALITIES.items():
        name = q["name_fa"] if lang == "fa" else q["name_en"]
        text = f"{'✅ ' if qid == current else ''}{name}"
        buttons.append([InlineKeyboardButton(text=text, callback_data=f"quality_{qid}")])
    buttons.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="back")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

async def compress_audio_async(input_path, output_path, bitrate):
    cmd = [FFMPEG_PATH, "-i", input_path, "-b:a", bitrate, "-y", output_path]
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )
    _, stderr = await process.communicate()
    if process.returncode != 0:
        logger.error(f"FFmpeg error: {stderr.decode()}")
        return False
    return True

@dp.message(Command("start"))
async def start(msg: types.Message):
    user_id = msg.from_user.id
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO users (user_id, lang, quality) VALUES (?, ?, ?)", (user_id, "en", "medium"))
    conn.commit()
    cur.execute("SELECT lang FROM users WHERE user_id = ?", (user_id,))
    lang = cur.fetchone()[0]
    conn.close()
    user_lang[user_id] = lang
    await msg.answer(get_text(lang, "start"), reply_markup=main_kb(lang))

@dp.message(Command("quality"))
async def quality_cmd(msg: types.Message):
    user_id = msg.from_user.id
    lang = user_lang.get(user_id, "en")
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute("SELECT quality FROM users WHERE user_id = ?", (user_id,))
    current = cur.fetchone()[0]
    conn.close()
    await msg.answer("Select quality:", reply_markup=quality_kb(lang, current))

@dp.callback_query()
async def callback(call: types.CallbackQuery):
    user_id = call.from_user.id
    data = call.data
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute("SELECT lang, quality FROM users WHERE user_id = ?", (user_id,))
    lang, quality = cur.fetchone()
    conn.close()
    user_lang[user_id] = lang

    if data in ["fa", "en"]:
        conn = sqlite3.connect(DB)
        cur = conn.cursor()
        cur.execute("UPDATE users SET lang = ? WHERE user_id = ?", (data, user_id))
        conn.commit()
        conn.close()
        user_lang[user_id] = data
        await call.message.edit_text(get_text(data, "start"), reply_markup=main_kb(data))

    elif data == "quality_menu":
        await call.message.edit_text("Select quality:", reply_markup=quality_kb(lang, quality))

    elif data.startswith("quality_"):
        qid = data.split("_")[1]
        if qid in QUALITIES:
            conn = sqlite3.connect(DB)
            cur = conn.cursor()
            cur.execute("UPDATE users SET quality = ? WHERE user_id = ?", (qid, user_id))
            conn.commit()
            conn.close()
            name = QUALITIES[qid]["name_fa"] if lang == "fa" else QUALITIES[qid]["name_en"]
            await call.message.edit_text(get_text(lang, "quality_set", name=name), reply_markup=main_kb(lang))

    elif data == "back":
        await call.message.edit_text(get_text(lang, "start"), reply_markup=main_kb(lang))

    elif data == "donate":
        # ارسال پیام جدید با اطلاعات حمایت مالی
        donate_text = (
            "💖 این ربات به رایگان در اختیار شما قرار گرفته است اما برای بقای این پروژه می‌توانید از ما حمایت مالی کنید:\n\n"
            "**Ton network:**\n"
            "`UQCv_m88yafoOWMD9h9MMglPP3DSiBL5xbLiU7akxWs5Q0pk`\n\n"
            "**USDT TRC20**\n"
            "`THXUWRaBgEyC27e8xC9JWG7unvygkFGNov`\n\n"
            "سازنده: Daniel Nemati"
        )
        await call.message.answer(donate_text, parse_mode="MarkdownV2")
        await call.answer()

    await call.answer()

@dp.message()
async def handle_audio(msg: types.Message):
    if not (msg.audio or (msg.document and msg.document.mime_type and msg.document.mime_type.startswith('audio/'))):
        return
    user_id = msg.from_user.id
    lang = user_lang.get(user_id, "en")
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute("SELECT quality FROM users WHERE user_id = ?", (user_id,))
    quality = cur.fetchone()[0]
    conn.close()
    bitrate = QUALITIES[quality]["bitrate"]

    status_msg = await msg.answer(get_text(lang, "processing"))

    input_file = f"{DOWNLOAD_DIR}/{user_id}_in.mp3"
    output_file = f"{DOWNLOAD_DIR}/{user_id}_out.mp3"
    try:
        file_id = msg.audio.file_id if msg.audio else msg.document.file_id
        file = await bot.get_file(file_id)
        await bot.download_file(file.file_path, input_file)
        orig_size = os.path.getsize(input_file) / (1024*1024)
        success = await compress_audio_async(input_file, output_file, bitrate)
        if not success:
            raise Exception("FFmpeg failed")
        new_size = os.path.getsize(output_file) / (1024*1024)
        percent = (1 - new_size/orig_size) * 100

        expire = datetime.now() + timedelta(days=1)
        conn = sqlite3.connect(DB)
        cur = conn.cursor()
        cur.execute("INSERT INTO files (user_id, file_path, expire_at) VALUES (?, ?, ?)", (user_id, output_file, expire.isoformat()))
        conn.commit()
        conn.close()

        await status_msg.delete()
        await msg.answer(get_text(lang, "done", percent=percent))
        await msg.answer_audio(FSInputFile(output_file))
        os.remove(input_file)
    except Exception as e:
        logger.exception("Error")
        await status_msg.delete()
        await msg.answer(get_text(lang, "error"))
        for f in [input_file, output_file]:
            if os.path.exists(f):
                os.remove(f)

async def cleanup():
    while True:
        conn = sqlite3.connect(DB)
        cur = conn.cursor()
        now = datetime.now().isoformat()
        cur.execute("SELECT id, file_path FROM files WHERE expire_at < ?", (now,))
        for rid, path in cur.fetchall():
            if os.path.exists(path):
                os.remove(path)
            cur.execute("DELETE FROM files WHERE id = ?", (rid,))
        conn.commit()
        conn.close()
        await asyncio.sleep(3600)

async def main():
    asyncio.create_task(cleanup())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
