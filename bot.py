import os
import asyncio
import sqlite3
import shutil
import logging
import subprocess
from datetime import datetime, timedelta
from typing import Dict, Optional
from dataclasses import dataclass
from contextlib import asynccontextmanager

from aiogram import Bot, Dispatcher, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from dotenv import load_dotenv

# ----------------------------- logging ----------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# ----------------------------- config -----------------------------------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN not found in environment")

# کیفیت‌های فشرده‌سازی (bitrate)
QUALITIES = {
    "low": {"bitrate": "32k", "name_fa": "خیلی کم حجم (۳۲ کیلوبیت)", "name_en": "Very low (32kbps)"},
    "medium": {"bitrate": "64k", "name_fa": "کم حجم معمولی (۶۴ کیلوبیت)", "name_en": "Medium (64kbps)"},
    "high": {"bitrate": "128k", "name_fa": "کیفیت خوب (۱۲۸ کیلوبیت)", "name_en": "Good quality (128kbps)"}
}
DEFAULT_QUALITY = "medium"

DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)
DB_PATH = "database.db"

# ----------------------------- database ---------------------------------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            lang TEXT DEFAULT 'en',
            quality TEXT DEFAULT 'medium',
            created_at TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            original_file_id TEXT,
            compressed_path TEXT,
            original_size INTEGER,
            compressed_size INTEGER,
            expire_at TEXT,
            created_at TEXT
        )
    """)
    conn.commit()
    conn.close()

init_db()

# ----------------------------- states (FSM) -----------------------------
class CompressState(StatesGroup):
    waiting_for_quality = State()

# ----------------------------- helpers ----------------------------------
def get_ffmpeg_path() -> str:
    path = shutil.which("ffmpeg")
    if not path:
        raise RuntimeError("FFmpeg not found. Please install ffmpeg.")
    return path

FFMPEG = get_ffmpeg_path()

def get_text(lang: str, key: str, **kwargs) -> str:
    texts = {
        "start": {
            "fa": "🎵 به ربات فشرده‌ساز موزیک خوش آمدید.\n\n"
                  "📀 یک فایل صوتی ارسال کنید تا با کیفیت دلخواه فشرده شود.\n"
                  "از دکمه‌های زیر می‌توانید زبان و کیفیت پیش‌فرض را تغییر دهید.\n\n"
                  "⚡️ دستورات:\n"
                  "/start - منو اصلی\n"
                  "/quality - تنظیم کیفیت فشرده‌سازی\n"
                  "/about - درباره ربات\n"
                  "/help - راهنما",
            "en": "🎵 Welcome to Music Compressor Bot.\n\n"
                  "📀 Send an audio file to compress it with your preferred quality.\n"
                  "Use buttons below to change language or default quality.\n\n"
                  "⚡️ Commands:\n"
                  "/start - Main menu\n"
                  "/quality - Set compression quality\n"
                  "/about - About bot\n"
                  "/help - Help"
        },
        "processing": {
            "fa": "⏳ در حال پردازش فایل... لطفاً صبر کنید.",
            "en": "⏳ Processing file... Please wait."
        },
        "done": {
            "fa": "✅ فشرده‌سازی با موفقیت انجام شد.\n"
                  "📉 حجم اصلی: {orig_size:.2f} MB\n"
                  "📈 حجم جدید: {new_size:.2f} MB\n"
                  "🗜️ کاهش: {percent:.1f}%",
            "en": "✅ Compression completed.\n"
                  "📉 Original size: {orig_size:.2f} MB\n"
                  "📈 New size: {new_size:.f} MB\n"
                  "🗜️ Reduction: {percent:.1f}%"
        },
        "error": {
            "fa": "❌ خطا در پردازش فایل. ممکن است فایل خراب یا فرمت آن پشتیبانی نشود.\n"
                  "لطفاً دوباره تلاش کنید.",
            "en": "❌ Error processing file. The file may be corrupted or format unsupported.\n"
                  "Please try again."
        },
        "quality_set": {
            "fa": "⚙️ کیفیت فشرده‌سازی به **{name}** تغییر کرد.",
            "en": "⚙️ Compression quality set to **{name}**."
        },
        "current_quality": {
            "fa": "کیفیت فعلی شما: {name}",
            "en": "Your current quality: {name}"
        },
        "quality_prompt": {
            "fa": "لطفاً کیفیت فشرده‌سازی مورد نظر خود را انتخاب کنید:",
            "en": "Please select your desired compression quality:"
        },
        "about": {
            "fa": "🤖 ربات فشرده‌ساز موزیک\n"
                  "نسخه 2.0\n\n"
                  "ساخته شده با aiogram 3 و FFmpeg\n"
                  "💡 منبع باز - برای حمایت دونیت کنید",
            "en": "🤖 Music Compressor Bot\n"
                  "Version 2.0\n\n"
                  "Built with aiogram 3 and FFmpeg\n"
                  "💡 Open source - Donate to support"
        },
        "help": {
            "fa": "📖 راهنما:\n"
                  "1. یک فایل صوتی (mp3, m4a, ogg, wav, flac) ارسال کنید.\n"
                  "2. ربات به طور خودکار با کیفیت ذخیره‌شده برای شما فشرده می‌کند.\n"
                  "3. می‌توانید از دکمه‌ها برای تغییر زبان و کیفیت استفاده کنید.\n"
                  "4. فایل فشرده‌شده به مدت ۲۴ ساعت نگهداری می‌شود.",
            "en": "📖 Help:\n"
                  "1. Send an audio file (mp3, m4a, ogg, wav, flac).\n"
                  "2. The bot will compress it using your saved quality.\n"
                  "3. Use buttons to change language or quality.\n"
                  "4. Compressed file will be kept for 24 hours."
        }
    }
    txt = texts.get(key, {}).get(lang, texts[key]["en"])
    if kwargs:
        txt = txt.format(**kwargs)
    return txt

async def get_user_data(user_id: int) -> dict:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT lang, quality FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    if row:
        return {"lang": row[0], "quality": row[1]}
    else:
        # ثبت کاربر جدید
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO users (user_id, lang, quality, created_at) VALUES (?, ?, ?, ?)",
            (user_id, "en", DEFAULT_QUALITY, datetime.now().isoformat())
        )
        conn.commit()
        conn.close()
        return {"lang": "en", "quality": DEFAULT_QUALITY}

async def update_user_lang(user_id: int, lang: str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("UPDATE users SET lang = ? WHERE user_id = ?", (lang, user_id))
    conn.commit()
    conn.close()

async def update_user_quality(user_id: int, quality: str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("UPDATE users SET quality = ? WHERE user_id = ?", (quality, user_id))
    conn.commit()
    conn.close()

async def compress_audio(input_path: str, output_path: str, bitrate: str) -> bool:
    """اجرای FFmpeg در ترد جداگانه (non-blocking)"""
    cmd = [FFMPEG, "-i", input_path, "-b:a", bitrate, "-y", output_path]
    try:
        # اجرای همزمان ولی در thread pool (برای آزاد کردن event loop)
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            error_msg = stderr.decode().strip()
            logger.error(f"FFmpeg error: {error_msg}")
            return False
        return True
    except Exception as e:
        logger.exception("compress_audio failed")
        return False

def get_file_size_mb(path: str) -> float:
    return os.path.getsize(path) / (1024 * 1024)

# ----------------------------- keyboards ---------------------------------
def main_menu_kb(lang: str):
    quality_text = QUALITIES[DEFAULT_QUALITY]["name_fa"] if lang == "fa" else QUALITIES[DEFAULT_QUALITY]["name_en"]
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🇮🇷 فارسی", callback_data="lang_fa"),
            InlineKeyboardButton(text="🇬🇧 English", callback_data="lang_en")
        ],
        [
            InlineKeyboardButton(text="⚙️ کیفیت" if lang == "fa" else "⚙️ Quality", callback_data="quality_menu"),
            InlineKeyboardButton(text="💖 Donate" if lang == "en" else "💖 حمایت", callback_data="donate")
        ],
        [
            InlineKeyboardButton(text="❓ Help" if lang == "en" else "❓ راهنما", callback_data="help")
        ]
    ])

def quality_kb(lang: str, current_quality: str):
    buttons = []
    for qid, qdata in QUALITIES.items():
        name = qdata["name_fa"] if lang == "fa" else qdata["name_en"]
        text = f"{'✅ ' if qid == current_quality else ''}{name}"
        buttons.append([InlineKeyboardButton(text=text, callback_data=f"quality_{qid}")])
    buttons.append([InlineKeyboardButton(text="🔙 بازگشت" if lang == "fa" else "🔙 Back", callback_data="back_main")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def back_kb(lang: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 بازگشت" if lang == "fa" else "🔙 Back", callback_data="back_main")]
    ])

# ----------------------------- bot handlers ------------------------------
@dp.message(Command("start"))
async def cmd_start(msg: types.Message):
    user_data = await get_user_data(msg.from_user.id)
    lang = user_data["lang"]
    await msg.answer(get_text(lang, "start"), reply_markup=main_menu_kb(lang))

@dp.message(Command("quality"))
async def cmd_quality(msg: types.Message):
    user_data = await get_user_data(msg.from_user.id)
    lang = user_data["lang"]
    current = user_data["quality"]
    await msg.answer(get_text(lang, "quality_prompt"), reply_markup=quality_kb(lang, current))

@dp.message(Command("about"))
async def cmd_about(msg: types.Message):
    user_data = await get_user_data(msg.from_user.id)
    lang = user_data["lang"]
    await msg.answer(get_text(lang, "about"), reply_markup=back_kb(lang))

@dp.message(Command("help"))
async def cmd_help(msg: types.Message):
    user_data = await get_user_data(msg.from_user.id)
    lang = user_data["lang"]
    await msg.answer(get_text(lang, "help"), reply_markup=back_kb(lang))

@dp.callback_query()
async def handle_callback(call: types.CallbackQuery):
    user_id = call.from_user.id
    user_data = await get_user_data(user_id)
    lang = user_data["lang"]
    data = call.data

    if data.startswith("lang_"):
        new_lang = data.split("_")[1]
        await update_user_lang(user_id, new_lang)
        user_data["lang"] = new_lang
        lang = new_lang
        await call.message.edit_text(get_text(lang, "start"), reply_markup=main_menu_kb(lang))
        await call.answer(get_text(lang, "done"))

    elif data == "quality_menu":
        current = user_data["quality"]
        await call.message.edit_text(get_text(lang, "quality_prompt"), reply_markup=quality_kb(lang, current))

    elif data.startswith("quality_"):
        qid = data.split("_")[1]
        if qid in QUALITIES:
            await update_user_quality(user_id, qid)
            user_data["quality"] = qid
            qname = QUALITIES[qid]["name_fa"] if lang == "fa" else QUALITIES[qid]["name_en"]
            await call.message.edit_text(
                get_text(lang, "quality_set", name=qname),
                reply_markup=back_kb(lang)
            )
            await call.answer()

    elif data == "back_main":
        await call.message.edit_text(get_text(lang, "start"), reply_markup=main_menu_kb(lang))

    elif data == "donate":
        await call.answer("BTC: 1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa\nETH: 0x...", show_alert=True)

    elif data == "help":
        await call.message.edit_text(get_text(lang, "help"), reply_markup=back_kb(lang))

    await call.answer()

@dp.message(lambda msg: msg.audio or (msg.document and msg.document.mime_type and msg.document.mime_type.startswith("audio/")))
async def handle_audio(msg: types.Message):
    user_id = msg.from_user.id
    user_data = await get_user_data(user_id)
    lang = user_data["lang"]
    quality = user_data["quality"]
    bitrate = QUALITIES[quality]["bitrate"]

    # ارسال پیام "در حال پردازش"
    status_msg = await msg.answer(get_text(lang, "processing"))

    input_path = None
    output_path = None
    try:
        # دریافت فایل
        file_id = msg.audio.file_id if msg.audio else msg.document.file_id
        file = await bot.get_file(file_id)

        # نام فایل‌های موقت
        timestamp = int(datetime.now().timestamp())
        input_path = os.path.join(DOWNLOAD_DIR, f"{user_id}_{timestamp}_in")
        # پسوند را حفظ می‌کنیم اما خروجی mp3 است
        if msg.audio:
            ext = ".mp3"  # معمولا تلگرام audio را mp3 می‌دهد
        else:
            ext = os.path.splitext(msg.document.file_name)[1] or ".mp3"
        input_path += ext
        output_path = os.path.join(DOWNLOAD_DIR, f"{user_id}_{timestamp}_out.mp3")

        # دانلود
        await bot.download_file(file.file_path, input_path)
        orig_size = get_file_size_mb(input_path)

        # فشرده‌سازی
        success = await compress_audio(input_path, output_path, bitrate)
        if not success or not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            raise Exception("Compression failed")

        new_size = get_file_size_mb(output_path)
        percent = (1 - new_size / orig_size) * 100

        # ذخیره در دیتابیس (برای پاکسازی بعدی)
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO files (user_id, original_file_id, compressed_path, original_size, compressed_size, expire_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user_id, file_id, output_path, orig_size, new_size, (datetime.now() + timedelta(days=1)).isoformat(), datetime.now().isoformat())
        )
        conn.commit()
        conn.close()

        # حذف پیام پردازش
        await status_msg.delete()
        # ارسال نتیجه
        result_text = get_text(lang, "done", orig_size=orig_size, new_size=new_size, percent=percent)
        await msg.answer(result_text)
        # ارسال فایل فشرده
        await msg.answer_audio(FSInputFile(output_path))

        # پاکسازی فایل ورودی (خروجی بعداً توسط تسک پاکسازی حذف می‌شود)
        if input_path and os.path.exists(input_path):
            os.remove(input_path)

    except Exception as e:
        logger.exception("handle_audio error")
        await status_msg.delete()
        await msg.answer(get_text(lang, "error"))
        # پاکسازی فایل‌های موقت
        for p in [input_path, output_path]:
            if p and os.path.exists(p):
                try:
                    os.remove(p)
                except:
                    pass

# ----------------------------- auto cleanup task -------------------------
async def cleanup_expired_files():
    while True:
        try:
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()
            now = datetime.now().isoformat()
            cur.execute("SELECT id, compressed_path FROM files WHERE expire_at < ?", (now,))
            rows = cur.fetchall()
            for fid, path in rows:
                if path and os.path.exists(path):
                    os.remove(path)
                cur.execute("DELETE FROM files WHERE id = ?", (fid,))
            conn.commit()
            conn.close()
            logger.info(f"Cleaned up {len(rows)} expired files")
        except Exception as e:
            logger.error(f"Cleanup error: {e}")
        await asyncio.sleep(3600)  # هر ساعت

# ----------------------------- main --------------------------------------
async def main():
    storage = MemoryStorage()  # می‌توان از Redis هم استفاده کرد
    dp.storage = storage
    asyncio.create_task(cleanup_expired_files())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
