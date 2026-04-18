import os
import asyncio
import shutil
import logging
import subprocess
import time
import random
import string
import hashlib
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
MAX_FILE_SIZE = 70 * 1024 * 1024  # 70 مگابایت (برای فایل صوتی نهایی و ورودی صوتی)

# ------------------------- Rate Limiting -------------------------
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

# ------------------------- Database -------------------------
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
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS cache (
                file_hash TEXT PRIMARY KEY,
                output_path TEXT,
                created_at REAL,
                size INTEGER
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

async def get_cached_output(file_hash: str) -> str | None:
    async with aiosqlite.connect(DB) as conn:
        async with conn.execute("SELECT output_path FROM cache WHERE file_hash = ?", (file_hash,)) as cur:
            row = await cur.fetchone()
            if row and os.path.exists(row[0]):
                return row[0]
            elif row:
                await conn.execute("DELETE FROM cache WHERE file_hash = ?", (file_hash,))
                await conn.commit()
            return None

async def save_to_cache(file_hash: str, output_path: str, size: int):
    async with aiosqlite.connect(DB) as conn:
        await conn.execute(
            "INSERT OR REPLACE INTO cache (file_hash, output_path, created_at, size) VALUES (?, ?, ?, ?)",
            (file_hash, output_path, time.time(), size)
        )
        await conn.commit()

async def clean_old_cache():
    try:
        week_ago = time.time() - 7 * 86400
        async with aiosqlite.connect(DB) as conn:
            async with conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='cache'") as cur:
                if not await cur.fetchone():
                    return
            async with conn.execute("SELECT file_hash, output_path FROM cache WHERE created_at < ?", (week_ago,)) as cur:
                rows = await cur.fetchall()
                for file_hash, output_path in rows:
                    if os.path.exists(output_path):
                        try:
                            os.remove(output_path)
                        except:
                            pass
                await conn.execute("DELETE FROM cache WHERE created_at < ?", (week_ago,))
                await conn.commit()
                if rows:
                    logger.info(f"Cleaned {len(rows)} old cache entries")
    except Exception as e:
        logger.error(f"Error cleaning cache: {e}")

# ------------------------- Texts -------------------------
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
                  "• ارسال فایل ویدیویی برای استخراج صدا و فشرده‌سازی\n"
                  "• کش نتایج برای پردازش سریع‌تر فایل‌های تکراری\n\n"
                  "**دستورات:**\n"
                  "/quality - تنظیم کیفیت\n"
                  "/mono - تنظیم مونو/استریو\n"
                  "/about - درباره ربات\n"
                  "/help - راهنما",
            "en": "🎵 Welcome to Music Compressor Bot.\n\n"
                  "📀 **Features:**\n"
                  "• Send audio file for compression\n"
                  "• Send video file to extract audio and compress\n"
                  "• Result caching for faster repeated requests\n\n"
                  "**Commands:**\n"
                  "/quality - Set quality\n"
                  "/mono - Set mono/stereo\n"
                  "/about - About bot\n"
                  "/help - Help"
        },
        "queued": {"fa": "⏳ درخواست شما در صف قرار گرفت...", "en": "⏳ Your request has been queued..."},
        "processing": {"fa": "⏳ در حال پردازش...", "en": "⏳ Processing..."},
        "done": {"fa": "✅ فشرده‌سازی انجام شد\n📉 کاهش حجم: {percent:.1f}%", "en": "✅ Done\n📉 Reduction: {percent:.1f}%"},
        "error": {"fa": "❌ خطا در پردازش", "en": "❌ Error"},
        "quality_set": {"fa": "کیفیت به {name} تغییر کرد", "en": "Quality set to {name}"},
        "mono_set": {"fa": "حالت پخش به **{mode}** تغییر کرد.\n(حالت مونو حجم فایل را تا ۴۰٪ کاهش می‌دهد)", "en": "Audio mode changed to **{mode}**.\n(Mono mode reduces file size up to 40%)"},
        "mono_current": {"fa": "حالت فعلی: {mode}", "en": "Current mode: {mode}"},
        "about": {"fa": "🤖 ربات فشرده‌ساز موزیک\nنسخه 2.8", "en": "🤖 Music Compressor Bot\nVersion 2.8"},
        "help": {"fa": "📖 راهنما...", "en": "📖 Help..."},
        "group_not_admin": {"fa": "⚠️ ربات ادمین نیست", "en": "⚠️ Bot not admin"},
        "group_confirm": {"fa": "🎵 فایل دریافت شد. فشرده شود؟", "en": "🎵 Compress this file?"},
        "group_canceled": {"fa": "❌ لغو شد", "en": "❌ Canceled"},
        "group_expired": {"fa": "⏰ منقضی شد", "en": "⏰ Expired"},
        "group_result": {"fa": "👤 کاربر {name}:\n✅ کاهش {percent:.1f}%", "en": "👤 User {name}:\n✅ Reduced {percent:.1f}%"},
        "file_too_large": {"fa": "❌ حجم فایل صوتی نباید بیشتر از ۷۰ مگابایت باشد.\nحجم: {size:.1f} MB", "en": "❌ Audio size cannot exceed 70 MB.\nSize: {size:.1f} MB"},
        "welcome_group": {"fa": "🎉 ربات اضافه شد!", "en": "🎉 Bot added!"},
        "cache_hit": {"fa": "💾 ارسال از کش...", "en": "💾 Sending from cache..."}
    }
    txt = texts.get(key, {}).get(lang, texts.get(key, {}).get("en", "Processing error"))
    return txt.format(**kwargs) if kwargs else txt

# ------------------------- Keyboards -------------------------
def main_kb(lang):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🇮🇷 فارسی", callback_data="fa"),
         InlineKeyboardButton(text="🇬🇧 English", callback_data="en")],
        [InlineKeyboardButton(text="⚙️ کیفیت" if lang=="fa" else "⚙️ Quality", callback_data="quality_menu"),
         InlineKeyboardButton(text="🎚️ مونو/استریو" if lang=="fa" else "🎚️ Mono/Stereo", callback_data="mono_menu")],
        [InlineKeyboardButton(text="💖 Donate", callback_data="donate", style="primary")]
    ])

def quality_kb(lang, current):
    buttons = []
    for qid, q in QUALITIES.items():
        name = q["name_fa"] if lang=="fa" else q["name_en"]
        text = f"{'✅ ' if qid==current else ''}{name}"
        buttons.append([InlineKeyboardButton(text=text, callback_data=f"quality_{qid}")])
    buttons.append([InlineKeyboardButton(text="🔙 بازگشت" if lang=="fa" else "🔙 Back", callback_data="back")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def mono_kb(lang, current_mono):
    stereo_text = "✅ استریو (Stereo)" if current_mono==0 else "استریو (Stereo)"
    mono_text = "✅ مونو (Mono)" if current_mono==1 else "مونو (Mono)"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=stereo_text, callback_data="mono_0")],
        [InlineKeyboardButton(text=mono_text, callback_data="mono_1")],
        [InlineKeyboardButton(text="🔙 بازگشت" if lang=="fa" else "🔙 Back", callback_data="back")]
    ])

def group_confirm_kb(request_id: str, lang: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ فشرده‌سازی" if lang=="fa" else "✅ Compress", callback_data=f"group_compress_{request_id}"),
         InlineKeyboardButton(text="❌ انصراف" if lang=="fa" else "❌ Cancel", callback_data=f"group_cancel_{request_id}")]
    ])

# ------------------------- Group pending -------------------------
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
                        await bot.edit_message_text(chat_id=req["chat_id"], message_id=req["message_id"],
                                                    text=get_text(req["lang"], "group_expired"))
                    except:
                        pass
        except Exception as e:
            logger.error(f"Cleanup error: {e}")
        await asyncio.sleep(60)

# ------------------------- FFmpeg helpers -------------------------
async def run_ffmpeg(cmd, step_name=""):
    process = await asyncio.create_subprocess_exec(*cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
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

# ------------------------- Utility -------------------------
async def compute_md5(file_path: str) -> str:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _compute_md5_sync, file_path)

def _compute_md5_sync(file_path: str) -> str:
    hasher = hashlib.md5()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest()

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
    chat_id: int = None
    reply_to_message_id: int = None
    requester_name: str = None

processing_queue = asyncio.Queue(maxsize=100)
WORKERS_COUNT = 3

async def queue_worker():
    while True:
        item = await processing_queue.get()
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
    audio_for_compress = None

    try:
        file = await bot.get_file(file_id)
        input_path = os.path.join(DOWNLOAD_DIR, f"{user_id}_{int(datetime.now().timestamp())}_in{ext}")
        await bot.download_file(file.file_path, input_path)
        await status_msg.edit_text("⏳ [🟩⬜⬜⬜⬜] 20% - " + ("دانلود شد..." if lang=="fa" else "Downloaded..."))

        if is_video:
            temp_audio_path = os.path.join(DOWNLOAD_DIR, f"{user_id}_{int(datetime.now().timestamp())}_temp_audio.mp3")
            await status_msg.edit_text("⏳ [🟩🟩⬜⬜⬜] 40% - " + ("استخراج صدا..." if lang=="fa" else "Extracting audio..."))
            success = await extract_audio_from_video(input_path, temp_audio_path)
            if not success:
                raise Exception("Audio extraction failed")
            audio_for_compress = temp_audio_path
        else:
            audio_for_compress = input_path

        # بررسی حجم فایل صوتی (نهایی قبل از فشرده‌سازی)
        audio_size_mb = os.path.getsize(audio_for_compress) / (1024*1024)
        if audio_size_mb > 70:
            await status_msg.delete()
            await bot.send_message(chat_id, get_text(lang, "file_too_large", size=audio_size_mb))
            # پاکسازی فایل‌های موقت
            for f in [input_path, temp_audio_path]:
                if f and os.path.exists(f):
                    os.remove(f)
            return

        file_hash = await compute_md5(audio_for_compress)
        cached_output = await get_cached_output(file_hash)

        if cached_output:
            output_path = cached_output
            await status_msg.edit_text("⏳ [🟩🟩🟩🟩🟩] 100% - " + ("آماده (از کش)..." if lang=="fa" else "Ready (cached)..."))
        else:
            await status_msg.edit_text("⏳ [🟩🟩🟩⬜⬜] 60% - " + ("در حال فشرده‌سازی..." if lang=="fa" else "Compressing..."))
            output_path = os.path.join(DOWNLOAD_DIR, f"{user_id}_{int(datetime.now().timestamp())}_out.mp3")
            success = await compress_audio_async(audio_for_compress, output_path, bitrate, mono_mode)
            if not success:
                raise Exception("Compression failed")
            await save_to_cache(file_hash, output_path, os.path.getsize(output_path))
            await status_msg.edit_text("⏳ [🟩🟩🟩🟩🟩] 100% - " + ("آماده ارسال..." if lang=="fa" else "Ready..."))

        orig_size = os.path.getsize(audio_for_compress) / (1024*1024)
        new_size = os.path.getsize(output_path) / (1024*1024)
        percent = (1 - new_size/orig_size) * 100

        await status_msg.delete()
        if cached_output:
            await bot.send_message(chat_id, get_text(lang, "cache_hit"))
        if is_group and item.requester_name:
            result_text = get_text(lang, "group_result", name=item.requester_name, percent=percent)
        else:
            result_text = get_text(lang, "done", percent=percent)
        await bot.send_message(chat_id, result_text)
        await bot.send_audio(chat_id, FSInputFile(output_path))

        # فایل خروجی اگر از کش نیامده بود، برای کش نگه می‌داریم. فقط فایل‌های موقت پاک شوند.
    except Exception as e:
        logger.exception(f"Processing failed for user {user_id}")
        await status_msg.delete()
        await bot.send_message(chat_id, get_text(lang, "error"))
    finally:
        for f in [input_path, temp_audio_path]:
            if f and os.path.exists(f):
                try:
                    os.remove(f)
                except:
                    pass

# ------------------------- Helper -------------------------
async def is_bot_admin(chat_id: int) -> bool:
    try:
        bot_member = await bot.get_chat_member(chat_id, bot.id)
        return bot_member.status in ["administrator", "creator"]
    except:
        return False

# ------------------------- Auto welcome -------------------------
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
        await bot.send_message(chat.id, get_text("en", "welcome_group"), parse_mode="Markdown")
        logger.info(f"Sent welcome message to group {chat.id}")

# ------------------------- Handlers -------------------------
@dp.message(Command("start"))
async def start(msg: types.Message):
    user = await get_user(msg.from_user.id)
    await msg.answer(get_text(user["lang"], "start"), reply_markup=main_kb(user["lang"]), parse_mode="Markdown")

@dp.message(Command("quality"))
async def quality_cmd(msg: types.Message):
    user = await get_user(msg.from_user.id)
    await msg.answer("Select quality:" if user["lang"]=="en" else "کیفیت:", reply_markup=quality_kb(user["lang"], user["quality"]))

@dp.message(Command("mono"))
async def mono_cmd(msg: types.Message):
    user = await get_user(msg.from_user.id)
    mode_name = "مونو (Mono)" if user["mono_mode"]==1 else "استریو (Stereo)"
    await msg.answer(get_text(user["lang"], "mono_current", mode=mode_name), reply_markup=mono_kb(user["lang"], user["mono_mode"]))

@dp.message(Command("about"))
async def about_cmd(msg: types.Message):
    user = await get_user(msg.from_user.id)
    await msg.answer(get_text(user["lang"], "about"), reply_markup=main_kb(user["lang"]), parse_mode="Markdown")

@dp.message(Command("help"))
async def help_cmd(msg: types.Message):
    user = await get_user(msg.from_user.id)
    await msg.answer(get_text(user["lang"], "help"), reply_markup=main_kb(user["lang"]), parse_mode="Markdown")

@dp.callback_query()
async def callback(call: types.CallbackQuery):
    user_id = call.from_user.id
    data = call.data
    user = await get_user(user_id)
    lang = user["lang"]

    if data.startswith("group_compress_"):
        req_id = data.split("_")[2]
        async with group_pending_lock:
            req = group_pending.get(req_id)
            if not req or req["user_id"] != user_id:
                await call.answer(get_text(lang, "group_expired") if not req else "Not yours", show_alert=True)
                await call.message.delete()
                return
            group_pending.pop(req_id)
        await call.message.delete()
        user = await get_user(user_id)
        item = QueueItem(
            user_id=user_id, lang=req["lang"], file_id=req["file_id"],
            quality=user["quality"], mono_mode=user["mono_mode"],
            is_video=req["is_video"], ext=req["ext"], chat_id=req["chat_id"],
            reply_to_message_id=req["message_id"], requester_name=call.from_user.full_name
        )
        try:
            await processing_queue.put(item)
            await call.answer("Added to queue")
            await bot.send_message(req["chat_id"], get_text(req["lang"], "queued"), reply_to_message_id=req["message_id"])
        except asyncio.QueueFull:
            await call.answer("Queue full, try later", show_alert=True)
        return

    if data.startswith("group_cancel_"):
        req_id = data.split("_")[2]
        async with group_pending_lock:
            req = group_pending.get(req_id)
            if req and req["user_id"] == user_id:
                group_pending.pop(req_id)
                await call.message.edit_text(get_text(req["lang"], "group_canceled"))
        await call.answer()
        return

    # normal buttons
    if data in ["fa", "en"]:
        await update_user_lang(user_id, data)
        await call.message.edit_text(get_text(data, "start"), reply_markup=main_kb(data), parse_mode="Markdown")
    elif data == "quality_menu":
        await call.message.edit_text("Select quality:", reply_markup=quality_kb(lang, user["quality"]))
    elif data.startswith("quality_"):
        qid = data.split("_")[1]
        if qid in QUALITIES:
            await update_user_quality(user_id, qid)
            name = QUALITIES[qid]["name_fa"] if lang=="fa" else QUALITIES[qid]["name_en"]
            await call.message.edit_text(get_text(lang, "quality_set", name=name), reply_markup=main_kb(lang))
    elif data == "mono_menu":
        await call.message.edit_text("Select mode:", reply_markup=mono_kb(lang, user["mono_mode"]))
    elif data.startswith("mono_"):
        new_mode = int(data.split("_")[1])
        await update_user_mono(user_id, new_mode)
        mode_name = "Mono" if new_mode==1 else "Stereo"
        await call.message.edit_text(get_text(lang, "mono_set", mode=mode_name), reply_markup=main_kb(lang))
    elif data == "back":
        await call.message.edit_text(get_text(lang, "start"), reply_markup=main_kb(lang), parse_mode="Markdown")
    elif data == "donate":
        donate_text = "💖 Ton: `UQCv_m88yafoOWMD9h9MMglPP3DSiBL5xbLiU7akxWs5Q0pk`\nUSDT TRC20: `THXUWRaBgEyC27e8xC9JWG7unvygkFGNov`"
        await call.message.answer(donate_text, parse_mode="MarkdownV2")
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
        await msg.reply(get_text(user["lang"], "help")[:200])

@dp.message()
async def handle_media(msg: types.Message, is_command: bool = False):
    user_id = msg.from_user.id
    chat_id = msg.chat.id
    is_group = chat_id < 0
    user = await get_user(user_id)
    lang = user["lang"]

    is_audio = msg.audio is not None
    is_video = msg.video is not None
    is_document_audio = (msg.document and msg.document.mime_type and msg.document.mime_type.startswith('audio/'))
    is_document_video = (msg.document and msg.document.mime_type and msg.document.mime_type.startswith('video/'))

    if not (is_audio or is_video or is_document_audio or is_document_video):
        return

    # محدودیت حجم فقط برای فایل‌های صوتی (ورودی)
    if is_audio or is_document_audio:
        file_size = msg.audio.file_size if is_audio else msg.document.file_size
        if file_size and file_size > MAX_FILE_SIZE:
            size_mb = file_size / (1024*1024)
            await msg.reply(get_text(lang, "file_too_large", size=size_mb))
            return
    # برای ویدیوها محدودیتی در ورودی نداریم

    if not check_rate_limit(user_id):
        await msg.reply(get_rate_limit_message(lang))
        return

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
                "user_id": user_id, "chat_id": chat_id, "file_id": file_id,
                "is_video": is_video_flag, "ext": ext, "message_id": msg.message_id,
                "timestamp": time.time(), "lang": lang
            }
        await msg.reply(get_text(lang, "group_confirm"), reply_markup=group_confirm_kb(request_id, lang))
        return

    # حالت خصوصی یا دستور compress
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
        user_id=user_id, lang=lang, file_id=file_id,
        quality=user["quality"], mono_mode=user["mono_mode"],
        is_video=is_video_flag, ext=ext, chat_id=chat_id,
        reply_to_message_id=msg.message_id, requester_name=msg.from_user.full_name if is_group else None
    )
    try:
        await processing_queue.put(item)
        await msg.reply(get_text(lang, "queued"))
    except asyncio.QueueFull:
        await msg.reply("❌ سرور شلوغ است، لحظاتی دیگر تلاش کنید.")

# ------------------------- Cleanup -------------------------
def clean_orphaned_files():
    now = time.time()
    deleted = 0
    for f in os.listdir(DOWNLOAD_DIR):
        path = os.path.join(DOWNLOAD_DIR, f)
        if os.path.isfile(path) and now - os.path.getmtime(path) > 86400:
            try:
                os.remove(path)
                deleted += 1
            except:
                pass
    if deleted:
        logger.info(f"Cleaned up {deleted} orphaned files")

# ------------------------- Main -------------------------
async def main():
    clean_orphaned_files()
    await init_db()
    await clean_old_cache()
    asyncio.create_task(cleanup_pending_requests())
    async def periodic_clean():
        while True:
            await asyncio.sleep(86400)
            await clean_old_cache()
    asyncio.create_task(periodic_clean())
    workers = [asyncio.create_task(queue_worker()) for _ in range(WORKERS_COUNT)]
    await dp.start_polling(bot)
    for w in workers:
        w.cancel()

if __name__ == "__main__":
    asyncio.run(main())
