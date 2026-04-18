import os
import asyncio
import hashlib
import logging
import shutil
import subprocess
import time
import random
import string
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from collections import defaultdict
from typing import Optional, Dict, Tuple
from contextlib import asynccontextmanager

import aiosqlite
from aiogram import Bot, Dispatcher, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile
from aiogram.filters import Command
from dotenv import load_dotenv

# ----------------------------- logging -----------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN not found")

# ----------------------------- config -----------------------------
class Config:
    MAX_FILE_SIZE = 70 * 1024 * 1024  # 70 MB
    RATE_LIMIT = 5
    RATE_WINDOW = 60
    WORKERS_COUNT = 2  # با توجه به CPU
    FFMPEG_TIMEOUT = 120  # seconds
    QUEUE_MAX_SIZE = 100
    PENDING_EXPIRE_SECONDS = 300
    DOWNLOAD_DIR = "downloads"
    DB_PATH = "database.db"
    USE_REDIS = False  # اگر Redis در دسترس است True کنید

os.makedirs(Config.DOWNLOAD_DIR, exist_ok=True)

# ----------------------------- FFmpeg path -----------------------------
FFMPEG_PATH = shutil.which("ffmpeg")
if not FFMPEG_PATH:
    raise RuntimeError("FFmpeg not found")
logger.info(f"FFmpeg at {FFMPEG_PATH}")

# ----------------------------- Database (aiosqlite) -----------------------------
class Database:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._pool = None

    async def init(self):
        self._pool = await aiosqlite.connect(self.db_path)
        await self._pool.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                lang TEXT DEFAULT 'en',
                quality TEXT DEFAULT 'medium',
                mono_mode INTEGER DEFAULT 0
            )
        """)
        await self._pool.execute("""
            CREATE TABLE IF NOT EXISTS file_cache (
                file_hash TEXT PRIMARY KEY,
                output_path TEXT,
                created_at REAL,
                size INTEGER
            )
        """)
        await self._pool.commit()

    async def close(self):
        if self._pool:
            await self._pool.close()

    @asynccontextmanager
    async def connect(self):
        async with aiosqlite.connect(self.db_path) as conn:
            yield conn

    async def get_user(self, user_id: int) -> dict:
        async with self.connect() as conn:
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

    async def update_user_lang(self, user_id: int, lang: str):
        async with self.connect() as conn:
            await conn.execute("UPDATE users SET lang = ? WHERE user_id = ?", (lang, user_id))
            await conn.commit()

    async def update_user_quality(self, user_id: int, quality: str):
        async with self.connect() as conn:
            await conn.execute("UPDATE users SET quality = ? WHERE user_id = ?", (quality, user_id))
            await conn.commit()

    async def update_user_mono(self, user_id: int, mono_mode: int):
        async with self.connect() as conn:
            await conn.execute("UPDATE users SET mono_mode = ? WHERE user_id = ?", (mono_mode, user_id))
            await conn.commit()

    async def get_cached_output(self, file_hash: str) -> Optional[str]:
        async with self.connect() as conn:
            async with conn.execute(
                "SELECT output_path FROM file_cache WHERE file_hash = ? AND created_at > ?",
                (file_hash, time.time() - 86400)  # 24h
            ) as cur:
                row = await cur.fetchone()
                return row[0] if row else None

    async def cache_file(self, file_hash: str, output_path: str, size: int):
        async with self.connect() as conn:
            await conn.execute(
                "INSERT OR REPLACE INTO file_cache (file_hash, output_path, created_at, size) VALUES (?, ?, ?, ?)",
                (file_hash, output_path, time.time(), size)
            )
            await conn.commit()

db = Database(Config.DB_PATH)

# ----------------------------- Rate Limiter (Redis-ready) -----------------------------
class RateLimiter:
    def __init__(self):
        self._store = defaultdict(list)  # fallback in-memory

    async def check(self, user_id: int) -> bool:
        now = time.time()
        if Config.USE_REDIS:
            # placeholder for Redis implementation
            pass
        else:
            history = self._store[user_id]
            history[:] = [t for t in history if now - t < Config.RATE_WINDOW]
            if len(history) >= Config.RATE_LIMIT:
                return False
            history.append(now)
            return True

rate_limiter = RateLimiter()

# ----------------------------- FFmpeg Service -----------------------------
class FFmpegService:
    def __init__(self, max_concurrent: int = 2):
        self._semaphore = asyncio.Semaphore(max_concurrent)

    async def run(self, cmd: list, timeout: int = Config.FFMPEG_TIMEOUT) -> Tuple[bool, str]:
        async with self._semaphore:
            try:
                process = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
                if process.returncode != 0:
                    error_msg = stderr.decode().strip()
                    logger.error(f"FFmpeg error: {error_msg}")
                    return False, error_msg
                return True, ""
            except asyncio.TimeoutError:
                logger.error("FFmpeg timeout")
                return False, "timeout"
            except Exception as e:
                logger.exception("FFmpeg exception")
                return False, str(e)

    async def extract_audio(self, video_path: str, audio_path: str) -> bool:
        cmd = [FFMPEG_PATH, "-i", video_path, "-q:a", "0", "-map", "a", audio_path, "-y"]
        success, _ = await self.run(cmd)
        return success

    async def compress(self, input_path: str, output_path: str, bitrate: str, mono_mode: int) -> bool:
        cmd = [FFMPEG_PATH, "-i", input_path, "-b:a", bitrate, "-y"]
        if mono_mode == 1:
            cmd.extend(["-ac", "1"])
        cmd.append(output_path)
        success, _ = await self.run(cmd)
        return success

ffmpeg_service = FFmpegService(max_concurrent=Config.WORKERS_COUNT)

# ----------------------------- File Utils -----------------------------
def sanitize_filename(filename: str) -> str:
    """حذف کاراکترهای خطرناک از نام فایل"""
    return "".join(c for c in filename if c.isalnum() or c in "._- ")

def get_file_hash(file_path: str) -> str:
    """محاسبه MD5 هش فایل برای کش"""
    hasher = hashlib.md5()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest()

# ----------------------------- Processing Queue -----------------------------
@dataclass
class QueueItem:
    user_id: int
    lang: str
    file_id: str
    quality: str
    mono_mode: int
    is_video: bool
    ext: str
    chat_id: int
    reply_to_message_id: Optional[int] = None
    requester_name: Optional[str] = None
    original_size: int = 0

class ProcessingQueue:
    def __init__(self, maxsize: int = Config.QUEUE_MAX_SIZE):
        self._queue = asyncio.Queue(maxsize=maxsize)
        self._workers = []

    async def start(self):
        for _ in range(Config.WORKERS_COUNT):
            self._workers.append(asyncio.create_task(self._worker()))

    async def stop(self):
        for w in self._workers:
            w.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)

    async def put(self, item: QueueItem):
        try:
            await self._queue.put(item)
        except asyncio.QueueFull:
            logger.warning(f"Queue full for user {item.user_id}")
            raise

    async def _worker(self):
        while True:
            item = await self._queue.get()
            try:
                await self._process(item)
            except Exception as e:
                logger.exception(f"Worker error for user {item.user_id}")
            finally:
                self._queue.task_done()

    async def _process(self, item: QueueItem):
        # پیام وضعیت
        status_msg = await bot.send_message(
            item.chat_id,
            get_text(item.lang, "processing"),
            reply_to_message_id=item.reply_to_message_id
        )
        input_path = None
        temp_audio = None
        output_path = None
        try:
            # دانلود فایل
            file = await bot.get_file(item.file_id)
            input_path = os.path.join(Config.DOWNLOAD_DIR, f"{item.user_id}_{int(time.time())}_in{item.ext}")
            await bot.download_file(file.file_path, input_path)

            await status_msg.edit_text("⏳ [🟩⬜⬜⬜⬜] 20% - " + ("دانلود شد..." if item.lang == "fa" else "Downloaded..."))

            if item.is_video:
                temp_audio = os.path.join(Config.DOWNLOAD_DIR, f"{item.user_id}_{int(time.time())}_temp.mp3")
                await status_msg.edit_text("⏳ [🟩🟩⬜⬜⬜] 40% - " + ("استخراج صدا..." if item.lang == "fa" else "Extracting audio..."))
                if not await ffmpeg_service.extract_audio(input_path, temp_audio):
                    raise Exception("Audio extraction failed")
                audio_for_compress = temp_audio
            else:
                audio_for_compress = input_path

            await status_msg.edit_text("⏳ [🟩🟩🟩⬜⬜] 60% - " + ("فشرده‌سازی..." if item.lang == "fa" else "Compressing..."))

            # کش: بررسی هش فایل
            file_hash = get_file_hash(audio_for_compress)
            cached = await db.get_cached_output(file_hash)
            if cached and os.path.exists(cached):
                output_path = cached
                logger.info(f"Cache hit for {file_hash}")
            else:
                output_path = os.path.join(Config.DOWNLOAD_DIR, f"{item.user_id}_{int(time.time())}_out.mp3")
                bitrate = QUALITIES[item.quality]["bitrate"]
                if not await ffmpeg_service.compress(audio_for_compress, output_path, bitrate, item.mono_mode):
                    raise Exception("Compression failed")
                await db.cache_file(file_hash, output_path, os.path.getsize(output_path))

            await status_msg.edit_text("⏳ [🟩🟩🟩🟩🟩] 100% - " + ("آماده ارسال..." if item.lang == "fa" else "Ready..."))

            orig_size = os.path.getsize(audio_for_compress) / (1024*1024)
            new_size = os.path.getsize(output_path) / (1024*1024)
            percent = (1 - new_size/orig_size) * 100

            await status_msg.delete()

            if item.chat_id != item.user_id and item.requester_name:
                result_text = get_text(item.lang, "group_result", name=item.requester_name, percent=percent)
            else:
                result_text = get_text(item.lang, "done", percent=percent)

            await bot.send_message(item.chat_id, result_text)
            await bot.send_audio(item.chat_id, FSInputFile(output_path))

        except Exception as e:
            logger.exception("Process failed")
            await status_msg.delete()
            await bot.send_message(item.chat_id, get_text(item.lang, "error"))
        finally:
            # پاکسازی فایل‌های موقت (فایل کش شده را پاک نکن)
            for f in [input_path, temp_audio]:
                if f and os.path.exists(f):
                    try:
                        os.remove(f)
                    except:
                        pass

queue = ProcessingQueue()

# ----------------------------- Text & Keyboards -----------------------------
QUALITIES = {
    "low": {"bitrate": "32k", "name_fa": "خیلی کم حجم", "name_en": "Very low"},
    "medium": {"bitrate": "64k", "name_fa": "معمولی", "name_en": "Medium"},
    "high": {"bitrate": "128k", "name_fa": "کیفیت بالا", "name_en": "High"}
}

def get_text(lang: str, key: str, **kwargs) -> str:
    texts = {
        "start": {
            "fa": "🎵 به ربات فشرده‌ساز موزیک خوش آمدید.\n\nارسال فایل صوتی یا تصویری",
            "en": "🎵 Welcome to Music Compressor Bot.\n\nSend audio or video file"
        },
        "processing": {"fa": "⏳ در حال پردازش...", "en": "⏳ Processing..."},
        "done": {"fa": "✅ فشرده‌سازی انجام شد\n📉 کاهش حجم: {percent:.1f}%", "en": "✅ Done\n📉 Reduction: {percent:.1f}%"},
        "error": {"fa": "❌ خطا در پردازش", "en": "❌ Error"},
        "queued": {"fa": "⏳ درخواست در صف قرار گرفت", "en": "⏳ Queued"},
        "file_too_large": {"fa": "❌ حجم فایل نباید بیشتر از ۷۰ مگابایت باشد.\nحجم: {size:.1f} MB", "en": "❌ File size > 70 MB.\nSize: {size:.1f} MB"},
        "rate_limit": {"fa": f"⏳ بیش از {Config.RATE_LIMIT} فایل در دقیقه", "en": f"⏳ Rate limit: {Config.RATE_LIMIT} files/min"},
        "group_confirm": {"fa": "🎵 فایل دریافت شد. فشرده شود؟", "en": "🎵 Compress this file?"},
        "group_canceled": {"fa": "❌ لغو شد", "en": "❌ Canceled"},
        "group_result": {"fa": "👤 {name}\n✅ کاهش {percent:.1f}%", "en": "👤 {name}\n✅ Reduced {percent:.1f}%"},
        "group_not_admin": {"fa": "⚠️ ربات ادمین نیست", "en": "⚠️ Bot not admin"},
        "group_expired": {"fa": "⏰ منقضی شد", "en": "⏰ Expired"},
        "welcome_group": {
            "fa": "🎉 ربات اضافه شد!\nفایل صوتی/تصویری بفرستید و روی دکمه فشرده‌سازی کلیک کنید.",
            "en": "🎉 Bot added!\nSend audio/video and click Compress."
        }
    }
    txt = texts.get(key, {}).get(lang, texts.get(key, {}).get("en", ""))
    return txt.format(**kwargs) if kwargs else txt

def main_kb(lang: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🇮🇷 فارسی", callback_data="fa"),
         InlineKeyboardButton(text="🇬🇧 English", callback_data="en")],
        [InlineKeyboardButton(text="⚙️ کیفیت" if lang=="fa" else "⚙️ Quality", callback_data="quality_menu"),
         InlineKeyboardButton(text="🎚️ مونو" if lang=="fa" else "🎚️ Mono", callback_data="mono_menu")],
        [InlineKeyboardButton(text="💖 Donate", callback_data="donate", style="primary")]
    ])

def quality_kb(lang: str, current: str):
    buttons = []
    for qid, q in QUALITIES.items():
        name = q["name_fa"] if lang=="fa" else q["name_en"]
        text = f"{'✅ ' if qid==current else ''}{name}"
        buttons.append([InlineKeyboardButton(text=text, callback_data=f"quality_{qid}")])
    buttons.append([InlineKeyboardButton(text="🔙 بازگشت" if lang=="fa" else "🔙 Back", callback_data="back")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def mono_kb(lang: str, current: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ استریو" if current==0 else "استریو", callback_data="mono_0")],
        [InlineKeyboardButton(text="✅ مونو" if current==1 else "مونو", callback_data="mono_1")],
        [InlineKeyboardButton(text="🔙 بازگشت" if lang=="fa" else "🔙 Back", callback_data="back")]
    ])

def group_confirm_kb(req_id: str, lang: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ فشرده‌سازی" if lang=="fa" else "✅ Compress", callback_data=f"group_compress_{req_id}"),
         InlineKeyboardButton(text="❌ انصراف" if lang=="fa" else "❌ Cancel", callback_data=f"group_cancel_{req_id}")]
    ])

# ----------------------------- Group pending (با Lock) -----------------------------
group_pending: Dict[str, dict] = {}
group_pending_lock = asyncio.Lock()

async def cleanup_pending():
    while True:
        try:
            now = time.time()
            async with group_pending_lock:
                expired = [rid for rid, data in group_pending.items() if now - data["timestamp"] > Config.PENDING_EXPIRE_SECONDS]
                for rid in expired:
                    req = group_pending.pop(rid)
                    try:
                        await bot.edit_message_text(
                            chat_id=req["chat_id"],
                            message_id=req["message_id"],
                            text=get_text(req["lang"], "group_expired")
                        )
                    except:
                        pass
        except Exception as e:
            logger.error(f"Cleanup error: {e}")
        await asyncio.sleep(60)

# ----------------------------- Handlers -----------------------------
async def is_bot_admin(chat_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id, bot.id)
        return member.status in ["administrator", "creator"]
    except:
        return False

@dp.message(Command("start"))
async def cmd_start(msg: types.Message):
    user = await db.get_user(msg.from_user.id)
    lang = user["lang"]
    await msg.answer(get_text(lang, "start"), reply_markup=main_kb(lang), parse_mode="Markdown")

@dp.message(Command("quality"))
async def cmd_quality(msg: types.Message):
    user = await db.get_user(msg.from_user.id)
    lang = user["lang"]
    current = user["quality"]
    await msg.answer("Select quality:" if lang=="en" else "کیفیت:", reply_markup=quality_kb(lang, current))

@dp.message(Command("mono"))
async def cmd_mono(msg: types.Message):
    user = await db.get_user(msg.from_user.id)
    lang = user["lang"]
    current = user["mono_mode"]
    await msg.answer("Select mode:" if lang=="en" else "حالت:", reply_markup=mono_kb(lang, current))

@dp.message(Command("help"))
async def cmd_help(msg: types.Message):
    user = await db.get_user(msg.from_user.id)
    lang = user["lang"]
    help_text = (
        "📖 /quality - change quality\n/mono - stereo/mono mode"
        if lang=="en" else
        "📖 /quality - تغییر کیفیت\n/mono - تبدیل به مونو"
    )
    await msg.answer(help_text, reply_markup=main_kb(lang))

@dp.callback_query()
async def handle_callback(call: types.CallbackQuery):
    data = call.data
    user_id = call.from_user.id
    user = await db.get_user(user_id)
    lang = user["lang"]

    # group callbacks
    if data.startswith("group_compress_"):
        req_id = data.split("_")[2]
        async with group_pending_lock:
            req = group_pending.get(req_id)
            if not req:
                await call.answer(get_text(lang, "group_expired"), show_alert=True)
                await call.message.delete()
                return
            if req["user_id"] != user_id:
                await call.answer("Not yours", show_alert=True)
                return
            group_pending.pop(req_id)
        await call.message.delete()
        # fetch latest user settings
        user = await db.get_user(user_id)
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
        await queue.put(item)
        await call.answer("Added to queue")
        await bot.send_message(req["chat_id"], get_text(req["lang"], "queued"), reply_to_message_id=req["message_id"])
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

    # normal callbacks
    if data in ["fa", "en"]:
        await db.update_user_lang(user_id, data)
        await call.message.edit_text(get_text(data, "start"), reply_markup=main_kb(data), parse_mode="Markdown")
    elif data == "quality_menu":
        await call.message.edit_text("Select quality:", reply_markup=quality_kb(lang, user["quality"]))
    elif data.startswith("quality_"):
        qid = data.split("_")[1]
        if qid in QUALITIES:
            await db.update_user_quality(user_id, qid)
            name = QUALITIES[qid]["name_fa"] if lang=="fa" else QUALITIES[qid]["name_en"]
            await call.message.edit_text(get_text(lang, "quality_set", name=name), reply_markup=main_kb(lang))
    elif data == "mono_menu":
        await call.message.edit_text("Select mode:", reply_markup=mono_kb(lang, user["mono_mode"]))
    elif data.startswith("mono_"):
        mode = int(data.split("_")[1])
        await db.update_user_mono(user_id, mode)
        mode_name = "Mono" if mode==1 else "Stereo"
        await call.message.edit_text(get_text(lang, "mono_set", mode=mode_name), reply_markup=main_kb(lang))
    elif data == "back":
        await call.message.edit_text(get_text(lang, "start"), reply_markup=main_kb(lang), parse_mode="Markdown")
    elif data == "donate":
        donate_text = (
            "💖 Ton: `UQCv_m88yafoOWMD9h9MMglPP3DSiBL5xbLiU7akxWs5Q0pk`\nUSDT TRC20: `THXUWRaBgEyC27e8xC9JWG7unvygkFGNov`"
        )
        await call.message.answer(donate_text, parse_mode="MarkdownV2")
    await call.answer()

@dp.message()
async def handle_media(msg: types.Message):
    user_id = msg.from_user.id
    chat_id = msg.chat.id
    is_group = chat_id < 0
    user = await db.get_user(user_id)
    lang = user["lang"]

    # detect media
    if msg.audio:
        file_id = msg.audio.file_id
        file_size = msg.audio.file_size
        is_video = False
        ext = ".mp3"
    elif msg.video:
        file_id = msg.video.file_id
        file_size = msg.video.file_size
        is_video = True
        ext = ".mp4"
    elif msg.document and msg.document.mime_type and msg.document.mime_type.startswith("audio/"):
        file_id = msg.document.file_id
        file_size = msg.document.file_size
        is_video = False
        ext = os.path.splitext(msg.document.file_name)[1] or ".mp3"
    elif msg.document and msg.document.mime_type and msg.document.mime_type.startswith("video/"):
        file_id = msg.document.file_id
        file_size = msg.document.file_size
        is_video = True
        ext = os.path.splitext(msg.document.file_name)[1] or ".mp4"
    else:
        return

    # size limit
    if file_size > Config.MAX_FILE_SIZE:
        size_mb = file_size / (1024*1024)
        await msg.reply(get_text(lang, "file_too_large", size=size_mb))
        return

    # rate limit
    if not await rate_limiter.check(user_id):
        await msg.reply(get_text(lang, "rate_limit"))
        return

    # group mode without command
    if is_group:
        if not await is_bot_admin(chat_id):
            await msg.reply(get_text(lang, "group_not_admin"))
            return
        req_id = ''.join(random.choices(string.ascii_lowercase + string.digits, k=12))
        async with group_pending_lock:
            group_pending[req_id] = {
                "user_id": user_id,
                "chat_id": chat_id,
                "file_id": file_id,
                "is_video": is_video,
                "ext": ext,
                "message_id": msg.message_id,
                "timestamp": time.time(),
                "lang": lang
            }
        await msg.reply(get_text(lang, "group_confirm"), reply_markup=group_confirm_kb(req_id, lang))
        return

    # private or /compress command
    item = QueueItem(
        user_id=user_id,
        lang=lang,
        file_id=file_id,
        quality=user["quality"],
        mono_mode=user["mono_mode"],
        is_video=is_video,
        ext=ext,
        chat_id=chat_id,
        reply_to_message_id=msg.message_id
    )
    try:
        await queue.put(item)
        await msg.reply(get_text(lang, "queued"))
    except asyncio.QueueFull:
        await msg.reply("❌ سرور شلوغ است، لحظاتی دیگر تلاش کنید.")

# ----------------------------- Auto welcome on group add -----------------------------
welcomed_groups = set()
@dp.my_chat_member()
async def on_bot_added(update: types.ChatMemberUpdated):
    chat = update.chat
    if chat.type not in ["group", "supergroup"]:
        return
    new = update.new_chat_member.status
    old = update.old_chat_member.status
    if (old in ["left", "kicked"] and new in ["member", "administrator"]) or (old == "member" and new == "administrator"):
        if chat.id not in welcomed_groups:
            welcomed_groups.add(chat.id)
            await bot.send_message(chat.id, get_text("en", "welcome_group"))

# ----------------------------- Main -----------------------------
async def main():
    await db.init()
    await queue.start()
    asyncio.create_task(cleanup_pending())
    logger.info("Bot started")
    await dp.start_polling(bot)
    await queue.stop()
    await db.close()

if __name__ == "__main__":
    asyncio.run(main())
