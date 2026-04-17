import os
import asyncio
import sqlite3
import shutil
import logging
import subprocess
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
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

# ------------------------- Rate Limiting -------------------------
request_history = defaultdict(list)
RATE_LIMIT = 5      # حداکثر فایل در دقیقه
RATE_WINDOW = 60

def check_rate_limit(user_id: int) -> bool:
    now = time.time()
    request_history[user_id] = [t for t in request_history[user_id] if now - t < RATE_WINDOW]
    if len(request_history[user_id]) >= RATE_LIMIT:
        return False
    request_history[user_id].append(now)
    return True

def get_rate_limit_message(lang: str) -> str:
    if lang == "fa":
        return f"⏳ شما بیش از حد مجاز ({RATE_LIMIT} فایل در دقیقه) درخواست ارسال کردید. لطفاً کمی صبر کنید."
    else:
        return f"⏳ You have exceeded the rate limit ({RATE_LIMIT} files per minute). Please wait a moment."

# ------------------------- Database (only users) -------------------------
def init_db():
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        lang TEXT DEFAULT 'en',
        quality TEXT DEFAULT 'medium',
        mono_mode INTEGER DEFAULT 0
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
            "fa": "🎵 به ربات فشرده‌ساز موزیک خوش آمدید.\n\n"
                  "📀 **قابلیت‌ها:**\n"
                  "• ارسال فایل صوتی برای فشرده‌سازی\n"
                  "• ارسال فایل ویدیویی برای استخراج صدا و فشرده‌سازی\n\n"
                  "**دستورات:**\n"
                  "/quality - تنظیم کیفیت\n"
                  "/mono - تنظیم مونو/استریو\n"
                  "/about - درباره ربات\n"
                  "/help - راهنما",
            "en": "🎵 Welcome to Music Compressor Bot.\n\n"
                  "📀 **Features:**\n"
                  "• Send audio file for compression\n"
                  "• Send video file to extract audio and compress\n\n"
                  "**Commands:**\n"
                  "/quality - Set quality\n"
                  "/mono - Set mono/stereo\n"
                  "/about - About bot\n"
                  "/help - Help"
        },
        "queued": {
            "fa": "⏳ درخواست شما در صف قرار گرفت. لطفاً منتظر بمانید...",
            "en": "⏳ Your request has been queued. Please wait..."
        },
        "processing": {"fa": "⏳ در حال پردازش... لطفاً صبر کنید", "en": "⏳ Processing... Please wait"},
        "done": {"fa": "✅ فشرده‌سازی انجام شد\n📉 کاهش حجم: {percent:.1f}%", "en": "✅ Done\n📉 Reduction: {percent:.1f}%"},
        "error": {"fa": "❌ خطا در پردازش", "en": "❌ Error"},
        "quality_set": {"fa": "کیفیت به {name} تغییر کرد", "en": "Quality set to {name}"},
        "mono_set": {
            "fa": "حالت پخش به **{mode}** تغییر کرد.\n(حالت مونو حجم فایل را تا ۴۰٪ کاهش می‌دهد)",
            "en": "Audio mode changed to **{mode}**.\n(Mono mode reduces file size up to 40%)"
        },
        "mono_current": {
            "fa": "حالت فعلی: {mode}",
            "en": "Current mode: {mode}"
        },
        "about": {
            "fa": "🤖 ربات فشرده‌ساز موزیک\nنسخه 2.2\n\n"
                  "قابلیت‌ها:\n"
                  "• فشرده‌سازی فایل‌های صوتی\n"
                  "• استخراج صدا از ویدیو و فشرده‌سازی\n"
                  "• تبدیل استریو به مونو\n"
                  "• صف پردازش (مدیریت همزمان)\n\n"
                  "ساخته شده با aiogram 3 و FFmpeg",
            "en": "🤖 Music Compressor Bot\nVersion 2.2\n\n"
                  "Features:\n"
                  "• Compress audio files\n"
                  "• Extract audio from video and compress\n"
                  "• Stereo to mono conversion\n"
                  "• Processing queue\n\n"
                  "Built with aiogram 3 and FFmpeg"
        },
        "help": {
            "fa": "📖 **راهنما:**\n\n"
                  "1️⃣ **فایل صوتی:**\n"
                  "   یک فایل صوتی (mp3, m4a, ogg, wav) ارسال کنید، ربات آن را فشرده می‌کند.\n\n"
                  "2️⃣ **فایل ویدیویی:**\n"
                  "   یک فایل ویدیویی (mp4, mkv, avi, mov) ارسال کنید، ربات صدای آن را استخراج کرده، فشرده می‌کند و برای شما ارسال می‌کند.\n\n"
                  "3️⃣ **تنظیم کیفیت:**\n"
                  "   از دستور /quality برای تنظیم کیفیت فشرده‌سازی استفاده کنید.\n\n"
                  "4️⃣ **تبدیل به مونو:**\n"
                  "   از دستور /mono برای کاهش حجم بیشتر (مناسب پادکست و کتاب صوتی) استفاده کنید.\n\n"
                  f"⏳ محدودیت نرخ درخواست: {RATE_LIMIT} فایل در دقیقه\n"
                  "🔄 صف پردازش خودکار برای مدیریت همزمان درخواست‌ها",
            "en": "📖 **Help:**\n\n"
                  "1️⃣ **Audio File:**\n"
                  "   Send an audio file (mp3, m4a, ogg, wav), the bot will compress it.\n\n"
                  "2️⃣ **Video File:**\n"
                  "   Send a video file (mp4, mkv, avi, mov), the bot will extract its audio, compress it and send back.\n\n"
                  "3️⃣ **Quality Settings:**\n"
                  "   Use /quality command to set compression quality.\n\n"
                  "4️⃣ **Mono Mode:**\n"
                  "   Use /mono command to reduce file size further (ideal for podcasts and audiobooks).\n\n"
                  f"⏳ Rate limit: {RATE_LIMIT} files per minute\n"
                  "🔄 Auto queue for concurrent requests"
        }
    }
    txt = texts.get(key, {}).get(lang, texts.get(key, {}).get("en", "Processing error"))
    return txt.format(**kwargs) if kwargs else txt

# ------------------------- Keyboards -------------------------
def main_kb(lang):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🇮🇷 فارسی", callback_data="fa"),
         InlineKeyboardButton(text="🇬🇧 English", callback_data="en")],
        [InlineKeyboardButton(text="⚙️ کیفیت" if lang == "fa" else "⚙️ Quality", callback_data="quality_menu"),
         InlineKeyboardButton(text="🎚️ مونو/استریو" if lang == "fa" else "🎚️ Mono/Stereo", callback_data="mono_menu")],
        [InlineKeyboardButton(text="💖 Donate", callback_data="donate", style="primary")]
    ])

def quality_kb(lang, current):
    buttons = []
    for qid, q in QUALITIES.items():
        name = q["name_fa"] if lang == "fa" else q["name_en"]
        text = f"{'✅ ' if qid == current else ''}{name}"
        buttons.append([InlineKeyboardButton(text=text, callback_data=f"quality_{qid}")])
    buttons.append([InlineKeyboardButton(text="🔙 بازگشت" if lang == "fa" else "🔙 Back", callback_data="back")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def mono_kb(lang, current_mono):
    stereo_text = "✅ استریو (Stereo)" if current_mono == 0 else "استریو (Stereo)"
    mono_text = "✅ مونو (Mono)" if current_mono == 1 else "مونو (Mono)"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=stereo_text, callback_data="mono_0")],
        [InlineKeyboardButton(text=mono_text, callback_data="mono_1")],
        [InlineKeyboardButton(text="🔙 بازگشت" if lang == "fa" else "🔙 Back", callback_data="back")]
    ])

# ------------------------- FFmpeg helpers -------------------------
async def run_ffmpeg(cmd, step_name=""):
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )
    _, stderr = await process.communicate()
    if process.returncode != 0:
        logger.error(f"FFmpeg error in {step_name}: {stderr.decode()}")
        return False
    return True

async def extract_audio_from_video(video_path, audio_path):
    cmd = [FFMPEG_PATH, "-i", video_path, "-q:a", "0", "-map", "a", audio_path, "-y"]
    return await run_ffmpeg(cmd, "extract_audio")

async def compress_audio_async(input_path, output_path, bitrate, mono_mode):
    cmd = [FFMPEG_PATH, "-i", input_path, "-b:a", bitrate, "-y"]
    if mono_mode == 1:
        cmd.extend(["-ac", "1"])
    cmd.append(output_path)
    return await run_ffmpeg(cmd, "compress_audio")

# ------------------------- Processing Queue -------------------------
@dataclass
class QueueItem:
    user_id: int
    lang: str
    file_id: str
    quality: str
    mono_mode: int
    is_video: bool
    ext: str
    reply_to_message_id: int = None   # برای پاسخ به پیام اصلی

processing_queue = asyncio.Queue()
WORKERS_COUNT = 3   # تعداد کارگرهای همزمان

async def queue_worker():
    """کارگری که از صف می‌خواند و فایل را پردازش می‌کند"""
    while True:
        item: QueueItem = await processing_queue.get()
        try:
            await process_file(item)
        except Exception as e:
            logger.exception(f"Worker error for user {item.user_id}")
        finally:
            processing_queue.task_done()

async def process_file(item: QueueItem):
    """پردازش واقعی فایل (دانلود، استخراج صدا، فشرده‌سازی، ارسال، پاکسازی)"""
    user_id = item.user_id
    lang = item.lang
    file_id = item.file_id
    quality = item.quality
    mono_mode = item.mono_mode
    is_video = item.is_video
    ext = item.ext

    bitrate = QUALITIES[quality]["bitrate"]

    # پیام وضعیت اولیه (به کاربر می‌گوییم پردازش شروع شد)
    status_msg = await bot.send_message(user_id, get_text(lang, "processing"))

    input_path = None
    temp_audio_path = None
    output_path = None

    try:
        # دانلود فایل
        file = await bot.get_file(file_id)
        input_path = os.path.join(DOWNLOAD_DIR, f"{user_id}_{int(datetime.now().timestamp())}_in{ext}")
        await bot.download_file(file.file_path, input_path)
        await status_msg.edit_text("⏳ [🟩⬜⬜⬜⬜] 20% - " + ("دانلود شد، در حال آماده‌سازی..." if lang == "fa" else "Downloaded, preparing..."))

        if is_video:
            temp_audio_path = os.path.join(DOWNLOAD_DIR, f"{user_id}_{int(datetime.now().timestamp())}_temp_audio.mp3")
            await status_msg.edit_text("⏳ [🟩🟩⬜⬜⬜] 40% - " + ("در حال استخراج صدا از ویدیو..." if lang == "fa" else "Extracting audio from video..."))
            success = await extract_audio_from_video(input_path, temp_audio_path)
            if not success:
                raise Exception("Audio extraction failed")
            audio_for_compress = temp_audio_path
            await status_msg.edit_text("⏳ [🟩🟩🟩⬜⬜] 60% - " + ("صدا استخراج شد، در حال فشرده‌سازی..." if lang == "fa" else "Audio extracted, compressing..."))
        else:
            audio_for_compress = input_path
            await status_msg.edit_text("⏳ [🟩🟩🟩⬜⬜] 60% - " + ("در حال فشرده‌سازی..." if lang == "fa" else "Compressing..."))

        output_path = os.path.join(DOWNLOAD_DIR, f"{user_id}_{int(datetime.now().timestamp())}_out.mp3")
        success = await compress_audio_async(audio_for_compress, output_path, bitrate, mono_mode)
        if not success:
            raise Exception("Compression failed")

        await status_msg.edit_text("⏳ [🟩🟩🟩🟩🟩] 100% - " + ("آماده ارسال..." if lang == "fa" else "Ready to send..."))

        orig_size = os.path.getsize(audio_for_compress) / (1024*1024)
        new_size = os.path.getsize(output_path) / (1024*1024)
        percent = (1 - new_size/orig_size) * 100

        # ارسال نتیجه
        await status_msg.delete()
        await bot.send_message(user_id, get_text(lang, "done", percent=percent))
        await bot.send_audio(user_id, FSInputFile(output_path))

        # حذف فایل خروجی از دیسک (بلافاصله بعد از ارسال)
        if output_path and os.path.exists(output_path):
            os.remove(output_path)
            logger.info(f"Deleted output file {output_path}")

    except Exception as e:
        logger.exception(f"Processing failed for user {user_id}")
        await status_msg.delete()
        await bot.send_message(user_id, get_text(lang, "error"))
    finally:
        # پاکسازی فایل‌های موقت (ورودی و temp)
        for f in [input_path, temp_audio_path]:
            if f and os.path.exists(f):
                try:
                    os.remove(f)
                except:
                    pass

# ------------------------- Bot Handlers -------------------------
@dp.message(Command("start"))
async def start(msg: types.Message):
    user_id = msg.from_user.id
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO users (user_id, lang, quality, mono_mode) VALUES (?, ?, ?, ?)",
                (user_id, "en", "medium", 0))
    conn.commit()
    cur.execute("SELECT lang FROM users WHERE user_id = ?", (user_id,))
    lang = cur.fetchone()[0]
    conn.close()
    user_lang[user_id] = lang
    await msg.answer(get_text(lang, "start"), reply_markup=main_kb(lang), parse_mode="Markdown")

@dp.message(Command("quality"))
async def quality_cmd(msg: types.Message):
    user_id = msg.from_user.id
    lang = user_lang.get(user_id, "en")
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute("SELECT quality FROM users WHERE user_id = ?", (user_id,))
    current = cur.fetchone()[0]
    conn.close()
    await msg.answer("Select quality:" if lang == "en" else "کیفیت مورد نظر را انتخاب کنید:", 
                     reply_markup=quality_kb(lang, current))

@dp.message(Command("mono"))
async def mono_cmd(msg: types.Message):
    user_id = msg.from_user.id
    lang = user_lang.get(user_id, "en")
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute("SELECT mono_mode FROM users WHERE user_id = ?", (user_id,))
    current = cur.fetchone()[0]
    conn.close()
    mode_name = "مونو (Mono)" if current == 1 else "استریو (Stereo)"
    await msg.answer(get_text(lang, "mono_current", mode=mode_name), reply_markup=mono_kb(lang, current))

@dp.message(Command("about"))
async def about_cmd(msg: types.Message):
    user_id = msg.from_user.id
    lang = user_lang.get(user_id, "en")
    await msg.answer(get_text(lang, "about"), reply_markup=main_kb(lang), parse_mode="Markdown")

@dp.message(Command("help"))
async def help_cmd(msg: types.Message):
    user_id = msg.from_user.id
    lang = user_lang.get(user_id, "en")
    await msg.answer(get_text(lang, "help"), reply_markup=main_kb(lang), parse_mode="Markdown")

@dp.callback_query()
async def callback(call: types.CallbackQuery):
    user_id = call.from_user.id
    data = call.data
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute("SELECT lang, quality, mono_mode FROM users WHERE user_id = ?", (user_id,))
    lang, quality, mono_mode = cur.fetchone()
    conn.close()
    user_lang[user_id] = lang

    if data in ["fa", "en"]:
        conn = sqlite3.connect(DB)
        cur = conn.cursor()
        cur.execute("UPDATE users SET lang = ? WHERE user_id = ?", (data, user_id))
        conn.commit()
        conn.close()
        user_lang[user_id] = data
        await call.message.edit_text(get_text(data, "start"), reply_markup=main_kb(data), parse_mode="Markdown")

    elif data == "quality_menu":
        await call.message.edit_text("Select quality:" if lang == "en" else "کیفیت مورد نظر را انتخاب کنید:", 
                                     reply_markup=quality_kb(lang, quality))

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

    elif data == "mono_menu":
        await call.message.edit_text("Select audio mode:" if lang == "en" else "حالت صدا را انتخاب کنید:",
                                     reply_markup=mono_kb(lang, mono_mode))

    elif data.startswith("mono_"):
        new_mode = int(data.split("_")[1])
        conn = sqlite3.connect(DB)
        cur = conn.cursor()
        cur.execute("UPDATE users SET mono_mode = ? WHERE user_id = ?", (new_mode, user_id))
        conn.commit()
        conn.close()
        mode_name = "مونو (Mono)" if new_mode == 1 else "استریو (Stereo)"
        await call.message.edit_text(get_text(lang, "mono_set", mode=mode_name), reply_markup=main_kb(lang))

    elif data == "back":
        await call.message.edit_text(get_text(lang, "start"), reply_markup=main_kb(lang), parse_mode="Markdown")

    elif data == "donate":
        if lang == "fa":
            donate_text = (
                "💖 این ربات به رایگان در اختیار شما قرار گرفته است اما برای بقای این پروژه می‌توانید از ما حمایت مالی کنید:\n\n"
                "**شبکه Ton:**\n"
                "`UQCv_m88yafoOWMD9h9MMglPP3DSiBL5xbLiU7akxWs5Q0pk`\n\n"
                "**شبکه TRC20 (USDT)**\n"
                "`THXUWRaBgEyC27e8xC9JWG7unvygkFGNov`\n\n"
                "سازنده: Daniel Nemati"
            )
        else:
            donate_text = (
                "💖 This bot is provided to you for free, but to support the continuation of this project, you can donate:\n\n"
                "**Ton Network:**\n"
                "`UQCv_m88yafoOWMD9h9MMglPP3DSiBL5xbLiU7akxWs5Q0pk`\n\n"
                "**USDT TRC20**\n"
                "`THXUWRaBgEyC27e8xC9JWG7unvygkFGNov`\n\n"
                "Creator: Daniel Nemati"
            )
        await call.message.answer(donate_text, parse_mode="MarkdownV2")
        await call.answer()

    await call.answer()

@dp.message()
async def handle_media(msg: types.Message):
    user_id = msg.from_user.id
    lang = user_lang.get(user_id, "en")

    # بررسی محدودیت نرخ درخواست
    if not check_rate_limit(user_id):
        await msg.answer(get_rate_limit_message(lang))
        return

    # تشخیص نوع رسانه
    is_audio = msg.audio is not None
    is_video = msg.video is not None
    is_document_audio = (msg.document and msg.document.mime_type and msg.document.mime_type.startswith('audio/'))
    is_document_video = (msg.document and msg.document.mime_type and msg.document.mime_type.startswith('video/'))

    if not (is_audio or is_video or is_document_audio or is_document_video):
        return

    # دریافت تنظیمات کاربر از دیتابیس
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute("SELECT quality, mono_mode FROM users WHERE user_id = ?", (user_id,))
    quality, mono_mode = cur.fetchone()
    conn.close()

    # تعیین file_id و نوع
    if is_audio:
        file_id = msg.audio.file_id
        is_video_flag = False
        ext = ".mp3"
    elif is_document_audio:
        file_id = msg.document.file_id
        is_video_flag = False
        ext = os.path.splitext(msg.document.file_name)[1] or ".mp3"
    elif is_video:
        file_id = msg.video.file_id
        is_video_flag = True
        ext = ".mp4"
    else:  # is_document_video
        file_id = msg.document.file_id
        is_video_flag = True
        ext = os.path.splitext(msg.document.file_name)[1] or ".mp4"

    # ساخت آیتم صف
    item = QueueItem(
        user_id=user_id,
        lang=lang,
        file_id=file_id,
        quality=quality,
        mono_mode=mono_mode,
        is_video=is_video_flag,
        ext=ext,
        reply_to_message_id=msg.message_id
    )

    # اضافه کردن به صف و اعلام به کاربر
    await processing_queue.put(item)
    await msg.answer(get_text(lang, "queued"))

# ------------------------- Main -------------------------
async def main():
    # راه‌اندازی کارگرهای صف
    workers = [asyncio.create_task(queue_worker()) for _ in range(WORKERS_COUNT)]
    # شروع polling ربات
    await dp.start_polling(bot)
    # در حالت عادی هیچ‌گاه به اینجا نمی‌رسد، اما برای خوش‌دستی:
    for w in workers:
        w.cancel()

if __name__ == "__main__":
    asyncio.run(main())
