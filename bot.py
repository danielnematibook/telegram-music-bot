import os
import asyncio
import sqlite3
import shutil
import logging
import subprocess
import re
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile
from aiogram.filters import Command
from dotenv import load_dotenv
import yt_dlp

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
            "fa": "🎵 به ربات فشرده‌ساز موزیک خوش آمدید.\n\n"
                  "📀 **قابلیت‌ها:**\n"
                  "• ارسال فایل صوتی یا تصویری برای فشرده‌سازی\n"
                  "• ارسال لینک یوتیوب برای دانلود و فشرده‌سازی خودکار\n\n"
                  "**دستورات:**\n"
                  "/quality - تنظیم کیفیت\n"
                  "/about - درباره ربات\n"
                  "/help - راهنما",
            "en": "🎵 Welcome to Music Compressor Bot.\n\n"
                  "📀 **Features:**\n"
                  "• Send audio or video file for compression\n"
                  "• Send YouTube link for automatic download and compression\n\n"
                  "**Commands:**\n"
                  "/quality - Set quality\n"
                  "/about - About bot\n"
                  "/help - Help"
        },
        "processing": {"fa": "⏳ در حال پردازش... لطفاً صبر کنید", "en": "⏳ Processing... Please wait"},
        "done": {"fa": "✅ فشرده‌سازی انجام شد\n📉 کاهش حجم: {percent:.1f}%", "en": "✅ Done\n📉 Reduction: {percent:.1f}%"},
        "error": {"fa": "❌ خطا در پردازش", "en": "❌ Error"},
        "quality_set": {"fa": "کیفیت به {name} تغییر کرد", "en": "Quality set to {name}"},
        "youtube_start": {
            "fa": "🎬 در حال دریافت اطلاعات ویدیو از یوتیوب...",
            "en": "🎬 Fetching video info from YouTube..."
        },
        "youtube_download": {
            "fa": "📥 در حال دانلود ویدیو از یوتیوب... (این فرآیند ممکن است چند لحظه طول بکشد)",
            "en": "📥 Downloading video from YouTube... (this may take a few moments)"
        },
        "youtube_extract": {
            "fa": "🎵 در حال استخراج صدا از ویدیو...",
            "en": "🎵 Extracting audio from video..."
        },
        "youtube_title": {
            "fa": "🎬 **{title}**",
            "en": "🎬 **{title}**"
        },
        "about": {
            "fa": "🤖 ربات فشرده‌ساز موزیک\nنسخه 2.1\n\n"
                  "قابلیت‌ها:\n"
                  "• فشرده‌سازی فایل‌های صوتی\n"
                  "• استخراج صدا از ویدیو\n"
                  "• دانلود و فشرده‌سازی خودکار از یوتیوب\n\n"
                  "ساخته شده با aiogram 3 و FFmpeg",
            "en": "🤖 Music Compressor Bot\nVersion 2.1\n\n"
                  "Features:\n"
                  "• Compress audio files\n"
                  "• Extract audio from video\n"
                  "• Automatic YouTube download and compression\n\n"
                  "Built with aiogram 3 and FFmpeg"
        },
        "help": {
            "fa": "📖 **راهنما:**\n\n"
                  "1️⃣ **فایل صوتی/تصویری:**\n"
                  "   یک فایل صوتی یا تصویری ارسال کنید، ربات آن را فشرده می‌کند.\n\n"
                  "2️⃣ **لینک یوتیوب:**\n"
                  "   یک لینک یوتیوب ارسال کنید، ربات:\n"
                  "   • ویدیو را دانلود می‌کند\n"
                  "   • صدای آن را استخراج می‌کند\n"
                  "   • فشرده می‌کند و برای شما ارسال می‌کند\n\n"
                  "3️⃣ **تنظیم کیفیت:**\n"
                  "   از دستور /quality برای تنظیم کیفیت فشرده‌سازی استفاده کنید.\n\n"
                  "⏱️ فایل‌ها به مدت ۲۴ ساعت در سرور نگهداری می‌شوند.",
            "en": "📖 **Help:**\n\n"
                  "1️⃣ **Audio/Video File:**\n"
                  "   Send an audio or video file, the bot will compress it.\n\n"
                  "2️⃣ **YouTube Link:**\n"
                  "   Send a YouTube link, the bot will:\n"
                  "   • Download the video\n"
                  "   • Extract the audio\n"
                  "   • Compress and send it to you\n\n"
                  "3️⃣ **Quality Settings:**\n"
                  "   Use /quality command to set compression quality.\n\n"
                  "⏱️ Files are kept on server for 24 hours."
        }
    }
    txt = texts.get(key, {}).get(lang, texts.get(key, {}).get("en", "Processing error"))
    return txt.format(**kwargs) if kwargs else txt

def main_kb(lang):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🇮🇷 فارسی", callback_data="fa"),
         InlineKeyboardButton(text="🇬🇧 English", callback_data="en")],
        [InlineKeyboardButton(text="⚙️ کیفیت" if lang == "fa" else "⚙️ Quality", callback_data="quality_menu")],
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

def is_youtube_url(url: str) -> bool:
    youtube_patterns = [
        r'(https?://)?(www\.)?(youtube\.com|youtu\.be)/',
        r'(https?://)?(www\.)?(m\.youtube\.com)/',
        r'(https?://)?(www\.)?(music\.youtube\.com)/'
    ]
    for pattern in youtube_patterns:
        if re.match(pattern, url):
            return True
    return False

async def download_youtube_audio(url: str, output_path: str, progress_msg: types.Message = None) -> tuple:
    """دانلود صدا از یوتیوب با استفاده از کوکی و تنظیمات ضد محدودیت"""
    # تنظیمات پیشرفته برای دور زدن محدودیت یوتیوب
    ydl_opts = {
        'format': 'bestaudio/best',
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }],
        'outtmpl': output_path.replace('.mp3', ''),
        'quiet': True,
        'no_warnings': True,
        'extract_flat': False,
        'extractor_args': {
            'youtube': {
                'player_client': ['android'],  # شبیه‌سازی کلاینت اندروید
                'skip': ['hls', 'dash']       # رد کردن فرمت‌های مشکل‌دار
            }
        },
        'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }
    
    # اگر فایل cookies.txt وجود داشت، از آن استفاده کن
    if os.path.exists('cookies.txt'):
        ydl_opts['cookiefile'] = 'cookies.txt'
        logger.info("Using cookies.txt for YouTube")
    else:
        logger.warning("cookies.txt not found, trying without cookies (may fail)")
    
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(url, download=False)
            title = info.get('title', 'Unknown Title')
            ydl.download([url])
            final_path = output_path.replace('.mp3', '.mp3')
            if not os.path.exists(final_path):
                for f in os.listdir(os.path.dirname(final_path)):
                    if f.endswith('.mp3') and str(info.get('id', '')) in f:
                        final_path = os.path.join(os.path.dirname(final_path), f)
                        break
            return final_path, title
        except Exception as e:
            logger.error(f"yt-dlp error: {e}")
            raise

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

async def compress_audio_async(input_path, output_path, bitrate):
    cmd = [FFMPEG_PATH, "-i", input_path, "-b:a", bitrate, "-y", output_path]
    return await run_ffmpeg(cmd, "compress_audio")

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
    text = msg.text or msg.caption or ""
    
    if text and is_youtube_url(text):
        await handle_youtube(msg, text, lang)
        return
    
    is_audio = msg.audio is not None
    is_video = msg.video is not None
    is_document_audio = (msg.document and msg.document.mime_type and msg.document.mime_type.startswith('audio/'))
    is_document_video = (msg.document and msg.document.mime_type and msg.document.mime_type.startswith('video/'))

    if not (is_audio or is_video or is_document_audio or is_document_video):
        return

    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute("SELECT quality FROM users WHERE user_id = ?", (user_id,))
    quality = cur.fetchone()[0]
    conn.close()
    bitrate = QUALITIES[quality]["bitrate"]

    progress_msg = await msg.answer(get_text(lang, "processing"))

    input_path = None
    output_path = None
    temp_audio_path = None

    try:
        if is_audio:
            file_id = msg.audio.file_id
            ext = ".mp3"
        elif is_document_audio:
            file_id = msg.document.file_id
            ext = os.path.splitext(msg.document.file_name)[1] or ".mp3"
        elif is_video:
            file_id = msg.video.file_id
            ext = ".mp4"
        else:
            file_id = msg.document.file_id
            ext = os.path.splitext(msg.document.file_name)[1] or ".mp4"

        file = await bot.get_file(file_id)
        input_path = os.path.join(DOWNLOAD_DIR, f"{user_id}_{int(datetime.now().timestamp())}_in{ext}")
        await bot.download_file(file.file_path, input_path)

        await progress_msg.edit_text("⏳ [🟩⬜⬜⬜⬜] 20% - " + ("دانلود شد، در حال آماده‌سازی..." if lang == "fa" else "Downloaded, preparing..."))

        if is_video or is_document_video:
            temp_audio_path = os.path.join(DOWNLOAD_DIR, f"{user_id}_{int(datetime.now().timestamp())}_temp_audio.mp3")
            await progress_msg.edit_text("⏳ [🟩🟩⬜⬜⬜] 40% - " + ("در حال استخراج صدا از ویدیو..." if lang == "fa" else "Extracting audio from video..."))
            success = await extract_audio_from_video(input_path, temp_audio_path)
            if not success:
                raise Exception("Audio extraction failed")
            audio_for_compress = temp_audio_path
            await progress_msg.edit_text("⏳ [🟩🟩🟩⬜⬜] 60% - " + ("صدا استخراج شد، در حال فشرده‌سازی..." if lang == "fa" else "Audio extracted, compressing..."))
        else:
            audio_for_compress = input_path
            await progress_msg.edit_text("⏳ [🟩🟩🟩⬜⬜] 60% - " + ("در حال فشرده‌سازی..." if lang == "fa" else "Compressing..."))

        output_path = os.path.join(DOWNLOAD_DIR, f"{user_id}_{int(datetime.now().timestamp())}_out.mp3")
        success = await compress_audio_async(audio_for_compress, output_path, bitrate)
        if not success:
            raise Exception("Compression failed")

        await progress_msg.edit_text("⏳ [🟩🟩🟩🟩🟩] 100% - " + ("آماده ارسال..." if lang == "fa" else "Ready to send..."))

        orig_size = os.path.getsize(audio_for_compress) / (1024*1024)
        new_size = os.path.getsize(output_path) / (1024*1024)
        percent = (1 - new_size/orig_size) * 100

        expire = datetime.now() + timedelta(days=1)
        conn = sqlite3.connect(DB)
        cur = conn.cursor()
        cur.execute("INSERT INTO files (user_id, file_path, expire_at) VALUES (?, ?, ?)", (user_id, output_path, expire.isoformat()))
        conn.commit()
        conn.close()

        await progress_msg.delete()
        await msg.answer(get_text(lang, "done", percent=percent))
        await msg.answer_audio(FSInputFile(output_path))

    except Exception as e:
        logger.exception("Error in handle_media")
        await progress_msg.delete()
        await msg.answer(get_text(lang, "error"))
    finally:
        for f in [input_path, temp_audio_path, output_path]:
            if f and os.path.exists(f):
                try:
                    os.remove(f)
                except:
                    pass

async def handle_youtube(msg: types.Message, url: str, lang: str):
    user_id = msg.from_user.id
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute("SELECT quality FROM users WHERE user_id = ?", (user_id,))
    quality = cur.fetchone()[0]
    conn.close()
    bitrate = QUALITIES[quality]["bitrate"]
    
    progress_msg = await msg.answer(get_text(lang, "youtube_start"))
    
    output_file = None
    downloaded_audio = None
    
    try:
        await progress_msg.edit_text(get_text(lang, "youtube_download"))
        temp_audio_path = os.path.join(DOWNLOAD_DIR, f"{user_id}_{int(datetime.now().timestamp())}_youtube")
        downloaded_audio, title = await download_youtube_audio(url, temp_audio_path, progress_msg)
        
        await msg.answer(get_text(lang, "youtube_title", title=title), parse_mode="Markdown")
        
        await progress_msg.edit_text(get_text(lang, "youtube_extract"))
        output_file = os.path.join(DOWNLOAD_DIR, f"{user_id}_{int(datetime.now().timestamp())}_compressed.mp3")
        
        success = await compress_audio_async(downloaded_audio, output_file, bitrate)
        if not success:
            raise Exception("Compression failed")
        
        orig_size = os.path.getsize(downloaded_audio) / (1024*1024)
        new_size = os.path.getsize(output_file) / (1024*1024)
        percent = (1 - new_size/orig_size) * 100
        
        expire = datetime.now() + timedelta(days=1)
        conn = sqlite3.connect(DB)
        cur = conn.cursor()
        cur.execute("INSERT INTO files (user_id, file_path, expire_at) VALUES (?, ?, ?)", (user_id, output_file, expire.isoformat()))
        conn.commit()
        conn.close()
        
        await progress_msg.delete()
        await msg.answer(get_text(lang, "done", percent=percent))
        await msg.answer_audio(FSInputFile(output_file))
        
    except Exception as e:
        logger.exception("Error in handle_youtube")
        await progress_msg.delete()
        await msg.answer(get_text(lang, "error"))
    finally:
        for f in [downloaded_audio, output_file]:
            if f and os.path.exists(f):
                try:
                    os.remove(f)
                except:
                    pass

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
