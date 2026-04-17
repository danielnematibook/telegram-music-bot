import os
import asyncio
import sqlite3
import shutil
import logging
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from dotenv import load_dotenv

# فعال کردن لاگینگ برای خطایابی
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN not found in .env")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

DB = "database.db"
DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# ------------------ بررسی وجود FFmpeg ------------------
FFMPEG_PATH = shutil.which("ffmpeg")
if FFMPEG_PATH is None:
    logger.error("❌ FFmpeg not found! Install it via Railway.json or Dockerfile")
    raise RuntimeError("FFmpeg missing")

logger.info(f"✅ FFmpeg found at {FFMPEG_PATH}")

# ------------------ دیتابیس ------------------
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

# ------------------ زبان ------------------
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
        },
        "error": {
            "fa": "❌ خطا در پردازش فایل. دوباره تلاش کنید.",
            "en": "❌ Error processing file. Please try again."
        }
    }
    return texts[key][lang]

# ------------------ کیبورد ------------------
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

# ------------------ فشرده‌سازی ناهمگام (Async) ------------------
async def compress_audio_async(input_path, output_path):
    """اجرای FFmpeg به صورت غیرهمگام"""
    cmd = [FFMPEG_PATH, "-i", input_path, "-b:a", "64k", output_path]
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await process.communicate()
    if process.returncode != 0:
        logger.error(f"FFmpeg error: {stderr.decode()}")
        raise RuntimeError(f"FFmpeg failed with code {process.returncode}")
    return True

# ------------------ هندلر استارت ------------------
@dp.message(Command("start"))
async def start(msg: types.Message):
    user_lang[msg.from_user.id] = "en"
    await msg.answer("Select language", reply_markup=main_kb())

# ------------------ کالبک ------------------
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

# ------------------ هندلر اصلی دریافت موزیک ------------------
@dp.message()
async def handle_audio(msg: types.Message):
    if not (msg.audio or msg.document):
        return

    lang = user_lang.get(msg.from_user.id, "en")
    user_id = msg.from_user.id

    try:
        # دریافت فایل
        if msg.audio:
            file_id = msg.audio.file_id
        else:
            file_id = msg.document.file_id

        file = await bot.get_file(file_id)

        input_file = f"{DOWNLOAD_DIR}/{user_id}_in.mp3"
        output_file = f"{DOWNLOAD_DIR}/{user_id}_out.mp3"

        # دانلود فایل
        await bot.download_file(file.file_path, input_file)
        logger.info(f"Downloaded {input_file}")

        # فشرده‌سازی
        await compress_audio_async(input_file, output_file)
        logger.info(f"Compressed to {output_file}")

        # ذخیره در دیتابیس
        expire = datetime.now() + timedelta(days=1)
        conn = sqlite3.connect(DB)
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO files (user_id, file_path, expire_at) VALUES (?, ?, ?)",
            (user_id, output_file, expire.isoformat())
        )
        conn.commit()
        conn.close()

        # ارسال فایل فشرده شده به کاربر
        await msg.answer(get_text(lang, "done"))
        await msg.answer_audio(types.FSInputFile(output_file))

        # پاکسازی فایل ورودی (خروجی بعداً توسط تسک پاکسازی حذف می‌شود)
        if os.path.exists(input_file):
            os.remove(input_file)

    except Exception as e:
        logger.exception("Error in handle_audio")
        await msg.answer(get_text(lang, "error"))
        # پاکسازی فایل‌های ناقص
        for f in [input_file, output_file]:
            if os.path.exists(f):
                os.remove(f)

# ------------------ تسک پاکسازی فایل‌های منقضی ------------------
async def cleanup():
    while True:
        try:
            conn = sqlite3.connect(DB)
            cur = conn.cursor()
            now = datetime.now().isoformat()
            cur.execute("SELECT id, file_path FROM files WHERE expire_at < ?", (now,))
            rows = cur.fetchall()
            for row_id, path in rows:
                if os.path.exists(path):
                    os.remove(path)
                    logger.info(f"Cleaned up {path}")
                cur.execute("DELETE FROM files WHERE id = ?", (row_id,))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Cleanup error: {e}")
        await asyncio.sleep(3600)  # هر ساعت یکبار

# ------------------ هندلر خطای سراسری (اختیاری) ------------------
@dp.errors()
async def global_error_handler(update, exception):
    logger.exception(f"Unhandled error: {exception}")
    # می‌توانید به کاربر پیام بدهید
    return True  # ادامه بده

# ------------------ اجرای اصلی ------------------
async def main():
    asyncio.create_task(cleanup())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
