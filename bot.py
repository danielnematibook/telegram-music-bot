import os
import asyncio
import shutil
import logging
import subprocess
import time
import random
import string
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

import aiosqlite
from aiogram import Bot, Dispatcher, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile
from aiogram.filters import Command
from dotenv import load_dotenv

# ----------------------------- logging -----------------------------
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

# ------------------------- محدودیت حجم فایل -------------------------
MAX_FILE_SIZE = 70 * 1024 * 1024  # 70 مگابایت

# ------------------------- Rate Limiting (in-memory با پاکسازی خودکار) -------------------------
request_history = defaultdict(list)
RATE_LIMIT = 5
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

# ------------------------- Database (aiosqlite) -------------------------
async def init_db():
    async with aiosqlite.connect(DB) as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                lang TEXT DEFAULT 'en',
                quality TEXT DEFAULT 'medium',
                mono_mode INTEGER DEFAULT 0
            )
        """)
        await conn.commit()

async def get_user(user_id: int):
    async with aiosqlite.connect(DB) as conn:
        async with conn.execute(
            "SELECT lang, quality, mono_mode FROM users WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
            if row:
                return {"lang": row[0], "quality": row[1], "mono_mode": row[2]}
            else:
                await conn.execute(
                    "INSERT INTO users (user_id, lang, quality, mono_mode) VALUES (?, ?, ?, ?)",
                    (user_id, "en", "medium", 0)
                )
                await conn.commit()
                return {"lang": "en", "quality": "medium", "mono_mode": 0}

async def update_user_lang(user_id: int, lang: str):
    async with aiosqlite.connect(DB) as conn:
        await conn.execute("UPDATE users SET lang = ? WHERE user_id = ?", (lang, user_id))
        await conn.commit()

async def update_user_quality(user_id: int, quality: str):
    async with aiosqlite.connect(DB) as conn:
        await conn.execute("UPDATE users SET quality = ? WHERE user_id = ?", (quality, user_id))
        await conn.commit()

async def update_user_mono(user_id: int, mono_mode: int):
    async with aiosqlite.connect(DB) as conn:
        await conn.execute("UPDATE users SET mono_mode = ? WHERE user_id = ?", (mono_mode, user_id))
        await conn.commit()

# ------------------------- متن‌ها و کیبوردها -------------------------
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
            "fa": "🤖 ربات فشرده‌ساز موزیک\nنسخه 2.6\n\n"
                  "قابلیت‌ها:\n"
                  "• فشرده‌سازی فایل‌های صوتی\n"
                  "• استخراج صدا از ویدیو و فشرده‌سازی\n"
                  "• تبدیل استریو به مونو\n"
                  "• صف پردازش (مدیریت همزمان)\n"
                  "• پشتیبانی از گروه‌ها (با ادمین)\n"
                  "• محدودیت حجم فایل: ۷۰ مگابایت\n\n"
                  "ساخته شده با aiogram 3 و FFmpeg",
            "en": "🤖 Music Compressor Bot\nVersion 2.6\n\n"
                  "Features:\n"
                  "• Compress audio files\n"
                  "• Extract audio from video and compress\n"
                  "• Stereo to mono conversion\n"
                  "• Processing queue\n"
                  "• Group support (requires admin)\n"
                  "• File size limit: 70 MB\n\n"
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
                  "5️⃣ **استفاده در گروه:**\n"
                  "   ربات را به گروه اضافه کنید و به او نقش ادمین بدهید. سپس کاربران می‌توانند فایل ارسال کنند و با کلیک روی دکمه «فشرده‌سازی» فایل فشرده را دریافت کنند.\n\n"
                  f"📦 محدودیت حجم فایل: ۷۰ مگابایت\n"
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
                  "5️⃣ **Group Usage:**\n"
                  "   Add bot to group and give it admin rights. Then users can send files and click 'Compress' button to get compressed file.\n\n"
                  f"📦 File size limit: 70 MB\n"
                  f"⏳ Rate limit: {RATE_LIMIT} files per minute\n"
                  "🔄 Auto queue for concurrent requests"
        },
        "group_not_admin": {
            "fa": "⚠️ ربات در این گروه ادمین نیست. لطفاً ابتدا ربات را ادمین کنید تا بتواند فایل‌ها را پردازش کند.",
            "en": "⚠️ Bot is not admin in this group. Please make bot admin first to process files."
        },
        "group_confirm": {
            "fa": "🎵 فایل صوتی دریافت شد. آیا می‌خواهید آن را فشرده کنید؟",
            "en": "🎵 Audio file received. Do you want to compress it?"
        },
        "group_canceled": {
            "fa": "❌ عملیات فشرده‌سازی لغو شد.",
            "en": "❌ Compression canceled."
        },
        "group_expired": {
            "fa": "⏰ زمان درخواست شما منقضی شده است. لطفاً دوباره فایل را ارسال کنید.",
            "en": "⏰ Your request has expired. Please send the file again."
        },
        "group_result": {
            "fa": "👤 کاربر {name}:\n✅ فشرده‌سازی انجام شد\n📉 کاهش حجم: {percent:.1f}%",
            "en": "👤 User {name}:\n✅ Compression done\n📉 Reduction: {percent:.1f}%"
        },
        "file_too_large": {
            "fa": "❌ حجم فایل ارسالی نباید بیشتر از ۷۰ مگابایت باشد.\nحجم فایل شما: {size:.1f} مگابایت",
            "en": "❌ File size cannot exceed 70 MB.\nYour file size: {size:.1f} MB"
        },
        "welcome_group": {
            "fa": "🎉 به گروه خوش آمدید!\n\nربات فشرده‌ساز موزیک با موفقیت به این گروه اضافه شد.\n\n📖 **نحوه استفاده:**\n"
                  "• یک فایل صوتی یا ویدیویی ارسال کنید.\n"
                  "• ربات یک دکمه «فشرده‌سازی» نشان می‌دهد.\n"
                  "• روی آن کلیک کنید تا فایل فشرده شود.\n\n"
                  "• همچنین می‌توانید روی فایل ریپلی کنید و دستور /compress را بفرستید.\n\n"
                  "برای اطلاعات بیشتر از دستور /help استفاده کنید.\n\n"
                  "موفق باشید! 🚀",
            "en": "🎉 Welcome to the group!\n\nMusic Compressor Bot has been successfully added to this group.\n\n📖 **How to use:**\n"
                  "• Send an audio or video file.\n"
                  "• The bot will show a 'Compress' button.\n"
                  "• Click it to get the compressed file.\n\n"
                  "• Alternatively, reply to the file with /compress.\n\n"
                  "Use /help for more information.\n\n"
                  "Enjoy! 🚀"
        }
    }
    txt = texts.get(key, {}).get(lang, texts.get(key, {}).get("en", "Processing error"))
    return txt.format(**kwargs) if kwargs else txt

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

def group_confirm_kb(request_id: str, lang: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ فشرده‌سازی" if lang == "fa" else "✅ Compress", callback_data=f"group_compress_{request_id}"),
            InlineKeyboardButton(text="❌ انصراف" if lang == "fa" else "❌ Cancel", callback_data=f"group_cancel_{request_id}")
        ]
    ])

# ------------------------- Group pending requests (با Lock) -------------------------
group_pending = {}
group_pending_lock = asyncio.Lock()
PENDING_EXPIRE_SECONDS = 300

async def cleanup_pending_requests():
    while True:
        try:
            now = time.time()
            async with group_pending_lock:
                expired = [rid for rid, data in group_pending.items() if now - data["timestamp"] > PENDING_EXPIRE_SECONDS]
                for rid in expired:
                    req = group_pending.pop(rid)
                    try:
                        await bot.edit_message_text(
                            chat_id=req["chat_id"],
                            message_id=req["message_id"],
                            text=get_text(req["lang"], "group_expired")
                        )
                    except Exception:
                        pass
        except Exception as e:
            logger.error(f"Cleanup error: {e}")
        await asyncio.sleep(60)

# ------------------------- FFmpeg helpers با timeout -------------------------
async def run_ffmpeg(cmd, step_name=""):
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )
    try:
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=120)
        if process.returncode != 0:
            logger.error(f"FFmpeg error in {step_name}: {stderr.decode()}")
            return False
        return True
    except asyncio.TimeoutError:
        logger.error(f"FFmpeg timeout in {step_name}")
        process.kill()
        await process.wait()
        return False
    except Exception as e:
        logger.exception(f"FFmpeg exception in {step_name}")
        return False

async def extract_audio_from_video(video_path, audio_path):
    cmd = [FFMPEG_PATH, "-i", video_path, "-q:a", "0", "-map", "a", audio_path, "-y"]
    return await run_ffmpeg(cmd, "extract_audio")

async def compress_audio_async(input_path, output_path, bitrate, mono_mode):
    cmd = [FFMPEG_PATH, "-i", input_path, "-b:a", bitrate, "-y"]
    if mono_mode == 1:
        cmd.extend(["-ac", "1"])
    cmd.append(output_path)
    return await run_ffmpeg(cmd, "compress_audio")

# ------------------------- Processing Queue (با maxsize) -------------------------
@dataclass
class QueueItem:
    user_id: int
    lang: str
    file_id: str
    quality: str
    mono_mode: int
    is_video: bool
    ext: str
    chat_id: int = None
    reply_to_message_id: int = None
    requester_name: str = None

processing_queue = asyncio.Queue(maxsize=100)
WORKERS_COUNT = 3

async def queue_worker():
    while True:
        item: QueueItem = await processing_queue.get()
        try:
            await process_file(item)
        except Exception as e:
            logger.exception(f"Worker error for user {item.user_id}")
        finally:
            processing_queue.task_done()

async def process_file(item: QueueItem):
    user_id = item.user_id
    lang = item.lang
    file_id = item.file_id
    quality = item.quality
    mono_mode = item.mono_mode
    is_video = item.is_video
    ext = item.ext
    chat_id = item.chat_id or user_id
    is_group = (chat_id != user_id)

    bitrate = QUALITIES[quality]["bitrate"]

    status_msg = await bot.send_message(chat_id, get_text(lang, "processing"), reply_to_message_id=item.reply_to_message_id)

    input_path = None
    temp_audio_path = None
    output_path = None

    try:
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

        await status_msg.delete()

        if is_group and item.requester_name:
            result_text = get_text(lang, "group_result", name=item.requester_name, percent=percent)
        else:
            result_text = get_text(lang, "done", percent=percent)

        await bot.send_message(chat_id, result_text)
        await bot.send_audio(chat_id, FSInputFile(output_path))

        # حذف فایل خروجی بعد از ارسال
        if output_path and os.path.exists(output_path):
            os.remove(output_path)
            logger.info(f"Deleted output file {output_path}")

    except Exception as e:
        logger.exception(f"Processing failed for user {user_id}")
        await status_msg.delete()
        await bot.send_message(chat_id, get_text(lang, "error"))
    finally:
        for f in [input_path, temp_audio_path]:
            if f and os.path.exists(f):
                try:
                    os.remove(f)
                except Exception:
                    pass

# ------------------------- Helper: check bot admin in group -------------------------
async def is_bot_admin(chat_id: int) -> bool:
    try:
        bot_member = await bot.get_chat_member(chat_id, bot.id)
        return bot_member.status in ["administrator", "creator"]
    except Exception:
        return False

# ------------------------- Auto welcome when bot added to group -------------------------
welcomed_groups = set()

@dp.my_chat_member()
async def on_bot_chat_member_update(update: types.ChatMemberUpdated):
    chat = update.chat
    if chat.type not in ["group", "supergroup"]:
        return
    new_status = update.new_chat_member.status
    old_status = update.old_chat_member.status
    if (old_status in ["left", "kicked"] and new_status in ["member", "administrator", "creator"]) or \
       (old_status == "member" and new_status == "administrator"):
        if chat.id in welcomed_groups:
            return
        welcomed_groups.add(chat.id)
        welcome_text = get_text("en", "welcome_group")
        await bot.send_message(chat.id, welcome_text, parse_mode="Markdown")
        logger.info(f"Sent welcome message to group {chat.id}")

# ------------------------- پاکسازی فایل‌های اورفان (در استارت) -------------------------
def clean_orphaned_files():
    now = time.time()
    deleted = 0
    for filename in os.listdir(DOWNLOAD_DIR):
        filepath = os.path.join(DOWNLOAD_DIR, filename)
        if os.path.isfile(filepath):
            if now - os.path.getmtime(filepath) > 86400:  # 24 ساعت
                try:
                    os.remove(filepath)
                    deleted += 1
                except Exception:
                    pass
    if deleted:
        logger.info(f"Cleaned up {deleted} orphaned files")

# ------------------------- Bot Handlers -------------------------
@dp.message(Command("start"))
async def start(msg: types.Message):
    user_id = msg.from_user.id
    user = await get_user(user_id)
    lang = user["lang"]
    await msg.answer(get_text(lang, "start"), reply_markup=main_kb(lang), parse_mode="Markdown")

@dp.message(Command("quality"))
async def quality_cmd(msg: types.Message):
    user_id = msg.from_user.id
    user = await get_user(user_id)
    lang = user["lang"]
    current = user["quality"]
    await msg.answer("Select quality:" if lang == "en" else "کیفیت مورد نظر را انتخاب کنید:", 
                     reply_markup=quality_kb(lang, current))

@dp.message(Command("mono"))
async def mono_cmd(msg: types.Message):
    user_id = msg.from_user.id
    user = await get_user(user_id)
    lang = user["lang"]
    current = user["mono_mode"]
    mode_name = "مونو (Mono)" if current == 1 else "استریو (Stereo)"
    await msg.answer(get_text(lang, "mono_current", mode=mode_name), reply_markup=mono_kb(lang, current))

@dp.message(Command("about"))
async def about_cmd(msg: types.Message):
    user_id = msg.from_user.id
    user = await get_user(user_id)
    lang = user["lang"]
    await msg.answer(get_text(lang, "about"), reply_markup=main_kb(lang), parse_mode="Markdown")

@dp.message(Command("help"))
async def help_cmd(msg: types.Message):
    user_id = msg.from_user.id
    user = await get_user(user_id)
    lang = user["lang"]
    await msg.answer(get_text(lang, "help"), reply_markup=main_kb(lang), parse_mode="Markdown")

@dp.callback_query()
async def callback(call: types.CallbackQuery):
    user_id = call.from_user.id
    data = call.data
    user = await get_user(user_id)
    lang = user["lang"]
    quality = user["quality"]
    mono_mode = user["mono_mode"]

    # پردازش دکمه‌های گروه
    if data.startswith("group_compress_"):
        request_id = data.split("_")[2]
        async with group_pending_lock:
            req = group_pending.get(request_id)
            if not req:
                await call.answer(get_text(lang, "group_expired"), show_alert=True)
                await call.message.delete()
                return
            if req["user_id"] != user_id:
                await call.answer("این دکمه متعلق به شما نیست.", show_alert=True)
                return
            group_pending.pop(request_id)
        await call.message.delete()
        user = await get_user(user_id)  # دریافت مجدد برای اطمینان
        item = QueueItem(
            user_id=user_id,
            lang=req["lang"],
            file_id=req["file_id"],
            quality=user["quality"],
            mono_mode=user["mono_mode"],
            is_video=req["is_video"],
            ext=req["ext"],
            chat_id=req["chat_id"],
            reply_to_message_id=req["message_id"],
            requester_name=call.from_user.full_name
        )
        try:
            await processing_queue.put(item)
        except asyncio.QueueFull:
            await call.answer("صف پردازش پر است، لحظاتی دیگر تلاش کنید.", show_alert=True)
            return
        await call.answer("درخواست شما به صف اضافه شد.", show_alert=False)
        await bot.send_message(req["chat_id"], get_text(req["lang"], "queued"), reply_to_message_id=req["message_id"])
        return

    elif data.startswith("group_cancel_"):
        request_id = data.split("_")[2]
        async with group_pending_lock:
            req = group_pending.get(request_id)
            if req and req["user_id"] == user_id:
                group_pending.pop(request_id)
                await call.message.edit_text(get_text(req["lang"], "group_canceled"))
        await call.answer()
        return

    # دکمه‌های عادی
    if data in ["fa", "en"]:
        await update_user_lang(user_id, data)
        await call.message.edit_text(get_text(data, "start"), reply_markup=main_kb(data), parse_mode="Markdown")
    elif data == "quality_menu":
        await call.message.edit_text("Select quality:" if lang == "en" else "کیفیت مورد نظر را انتخاب کنید:", 
                                     reply_markup=quality_kb(lang, quality))
    elif data.startswith("quality_"):
        qid = data.split("_")[1]
        if qid in QUALITIES:
            await update_user_quality(user_id, qid)
            name = QUALITIES[qid]["name_fa"] if lang == "fa" else QUALITIES[qid]["name_en"]
            await call.message.edit_text(get_text(lang, "quality_set", name=name), reply_markup=main_kb(lang))
    elif data == "mono_menu":
        await call.message.edit_text("Select audio mode:" if lang == "en" else "حالت صدا را انتخاب کنید:",
                                     reply_markup=mono_kb(lang, mono_mode))
    elif data.startswith("mono_"):
        new_mode = int(data.split("_")[1])
        await update_user_mono(user_id, new_mode)
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

@dp.message(Command("compress"))
async def compress_command(msg: types.Message):
    if msg.reply_to_message and (msg.reply_to_message.audio or msg.reply_to_message.video or 
                                 (msg.reply_to_message.document and msg.reply_to_message.document.mime_type and 
                                  (msg.reply_to_message.document.mime_type.startswith('audio/') or 
                                   msg.reply_to_message.document.mime_type.startswith('video/')))):
        await handle_media(msg.reply_to_message, is_command=True)
    else:
        user = await get_user(msg.from_user.id)
        lang = user["lang"]
        await msg.reply(get_text(lang, "help")[:200])

@dp.message()
async def handle_media(msg: types.Message, is_command: bool = False):
    user_id = msg.from_user.id
    chat_id = msg.chat.id
    is_group = chat_id < 0

    user = await get_user(user_id)
    lang = user["lang"]

    # تشخیص نوع رسانه
    is_audio = msg.audio is not None
    is_video = msg.video is not None
    is_document_audio = (msg.document and msg.document.mime_type and msg.document.mime_type.startswith('audio/'))
    is_document_video = (msg.document and msg.document.mime_type and msg.document.mime_type.startswith('video/'))

    if not (is_audio or is_video or is_document_audio or is_document_video):
        return

    # بررسی محدودیت حجم فایل
    if msg.audio:
        file_size = msg.audio.file_size
    elif msg.video:
        file_size = msg.video.file_size
    elif msg.document:
        file_size = msg.document.file_size
    else:
        file_size = 0

    if file_size and file_size > MAX_FILE_SIZE:
        size_mb = file_size / (1024*1024)
        await msg.reply(get_text(lang, "file_too_large", size=size_mb))
        return

    # بررسی محدودیت نرخ درخواست
    if not check_rate_limit(user_id):
        await msg.reply(get_rate_limit_message(lang))
        return

    # حالت گروه (بدون دستور مستقیم)
    if is_group and not is_command:
        if not await is_bot_admin(chat_id):
            await msg.reply(get_text(lang, "group_not_admin"))
            return

        request_id = ''.join(random.choices(string.ascii_lowercase + string.digits, k=12))
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
        else:
            file_id = msg.document.file_id
            is_video_flag = True
            ext = os.path.splitext(msg.document.file_name)[1] or ".mp4"

        async with group_pending_lock:
            group_pending[request_id] = {
                "user_id": user_id,
                "chat_id": chat_id,
                "file_id": file_id,
                "is_video": is_video_flag,
                "ext": ext,
                "message_id": msg.message_id,
                "timestamp": time.time(),
                "lang": lang
            }
        await msg.reply(
            get_text(lang, "group_confirm"),
            reply_markup=group_confirm_kb(request_id, lang)
        )
        return

    # حالت خصوصی یا دستور /compress در گروه
    quality = user["quality"]
    mono_mode = user["mono_mode"]

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
    else:
        file_id = msg.document.file_id
        is_video_flag = True
        ext = os.path.splitext(msg.document.file_name)[1] or ".mp4"

    item = QueueItem(
        user_id=user_id,
        lang=lang,
        file_id=file_id,
        quality=quality,
        mono_mode=mono_mode,
        is_video=is_video_flag,
        ext=ext,
        chat_id=chat_id,
        reply_to_message_id=msg.message_id,
        requester_name=msg.from_user.full_name if is_group else None
    )
    try:
        await processing_queue.put(item)
        await msg.reply(get_text(lang, "queued"))
    except asyncio.QueueFull:
        await msg.reply("❌ سرور شلوغ است، لحظاتی دیگر تلاش کنید.")

# ------------------------- Main -------------------------
async def main():
    # پاکسازی فایل‌های اورفان در استارت
    clean_orphaned_files()
    # راه‌اندازی دیتابیس
    await init_db()
    # تسک پاکسازی درخواست‌های گروه
    asyncio.create_task(cleanup_pending_requests())
    # کارگرهای صف
    workers = [asyncio.create_task(queue_worker()) for _ in range(WORKERS_COUNT)]
    # استارت ربات
    await dp.start_polling(bot)
    # در صورت خروج (معمولاً هرگز)
    for w in workers:
        w.cancel()

if __name__ == "__main__":
    asyncio.run(main())
