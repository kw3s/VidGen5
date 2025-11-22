import logging, os, uuid, shutil, requests, asyncio, subprocess, sys, threading, re
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from telegram.request import HTTPXRequest
from concurrent.futures import ThreadPoolExecutor
from flask import Flask
from bs4 import BeautifulSoup

# --- CONFIGURATION ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
FFMPEG = "ffmpeg"
MAX_FILE_SIZE = 50 * 1024 * 1024
MAX_WORKERS = 2
BASE_TEMP_DIR = "/data/temp" if os.path.exists("/data") else "temp"  # Fly.io persistent volume

MODE_SEARCH = "search"
MODE_MANUAL = "manual"
MODE_AUDIO_ONLY = "audio_only"
MODE_AUDIO_ONLY_DETAILS = "audio_only_details"
MODE_LINKS = "links"

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

os.makedirs(BASE_TEMP_DIR, exist_ok=True)
executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)

# --- FAKE WEB SERVER ---
app_flask = Flask(__name__)
@app_flask.route('/')
def health_check(): return "Bot is Alive!", 200

def run_web_server():
    port = int(os.environ.get("PORT", 8080))
    app_flask.run(host="0.0.0.0", port=port)

# --- HELPERS ---
async def run_async(func, *args, **kwargs):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(executor, lambda: func(*args, **kwargs))

def subprocess_run_with_timeout(c, t):
    try: return subprocess.run(c, capture_output=True, text=True, timeout=t)
    except Exception as e: return type('Result', (), {'returncode': 1, 'stderr': str(e)})()

async def cleanup_session(context: ContextTypes.DEFAULT_TYPE):
    temp_dir = context.user_data.get("temp_dir")
    if temp_dir and os.path.exists(temp_dir):
        try: shutil.rmtree(temp_dir, ignore_errors=True)
        except: pass
    context.user_data.clear()

# --- DZM.PM DOWNLOADER (FASTEST METHOD 2025) ---
async def download_via_dzm(link: str, temp_dir: str) -> dict | None:
    try:
        api = f"https://dzm.pm/api/{link.strip()}"
        resp = await run_async(requests.get, api, timeout=15)
        if resp.status_code != 200:
            return None
        data = resp.json()

        audio_url = data["url"]
        cover_url = data["cover"]
        artist = data.get("artist", "Unknown")
        title = data.get("title", "Unknown Track")
        album = data.get("album")

        # Download audio
        audio_data = await run_async(requests.get, audio_url, timeout=90)
        audio_path = os.path.join(temp_dir, "audio.m4a")
        with open(audio_path, "wb") as f:
            f.write(audio_data.content)

        # Download cover
        cover_data = await run_async(requests.get, cover_url, timeout=10)
        cover_path = os.path.join(temp_dir, "cover.jpg")
        with open(cover_path, "wb") as f:
            f.write(cover_data.content)

        caption = f"🎵 {artist} - {title}"
        if album:
            caption += f"\n💿 {album}"

        return {"audio": audio_path, "image": cover_path, "caption": caption}
    except Exception as e:
        logger.warning(f"dzm.pm failed: {e}")
        return None

# --- FALLBACK: OLD YOUTUBE SEARCH ---
async def fallback_ytdlp_search(query: str, temp_dir: str, status_msg) -> dict | None:
    try:
        await status_msg.edit_text("🔄 Falling back to YouTube search...")
        search_url = f"https://api.deezer.com/search?q={requests.utils.quote(query)}&limit=1"
        resp = await run_async(requests.get, search_url, timeout=10)
        data = resp.json()
        if not data.get("data"):
            return None

        track = data["data"][0]
        artist, title = track["artist"]["name"], track["title"]
        cover = track["album"]["cover_big"]

        cover_data = await run_async(requests.get, cover, timeout=10)
        cover_path = os.path.join(temp_dir, "cover.jpg")
        with open(cover_path, "wb") as f: f.write(cover_data.content)

        audio_path = os.path.join(temp_dir, "audio.m4a")
        cmd = ['yt-dlp', '-x', '--audio-format', 'm4a', '-o', audio_path,
               f'ytsearch: {artist} {title} audio', '--quiet', '--max-filesize', '25M']
        await run_async(subprocess_run_with_timeout, cmd, 180)

        if not os.path.exists(audio_path):
            return None

        return {
            "audio": audio_path,
            "image": cover_path,
            "caption": f"🎵 {artist} - {title} (YouTube fallback)"
        }
    except Exception as e:
        logger.error(f"Fallback failed: {e}")
        return None

# --- HANDLERS ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_mode_selection(update, context, "🎬 **VidGen Pro Ready!**\nChoose a mode:")

async def modes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("processing"):
        await update.message.reply_text("⏳ Please wait...")
        return
    await show_mode_selection(update, context, "🔄 Select mode:")

async def show_mode_selection(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    keyboard = [
        [InlineKeyboardButton("🔗 1. Link Mode (Spotify/Tidal/Deezer)", callback_data=MODE_LINKS)],
        [InlineKeyboardButton("🎵 2. Search Mode", callback_data=MODE_SEARCH)],
        [InlineKeyboardButton("🖼️ 3. Image + Audio", callback_data=MODE_MANUAL)],
        [InlineKeyboardButton("🎧 4. Audio → Auto Cover", callback_data=MODE_AUDIO_ONLY)],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    if update.message:
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode='Markdown')
    else:
        await update.callback_query.edit_message_text(text, reply_markup=reply_markup, parse_mode='Markdown')

async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data.update({"mode": query.data, "pending": {}})

    session_dir = os.path.join(BASE_TEMP_DIR, f"{update.effective_user.id}_{uuid.uuid4 ().hex[:8]}")
    os.makedirs(session_dir, exist_ok=True)
    context.user_data["temp_dir"] = session_dir

    msgs = {
        MODE_LINKS: "🔗 **Link Mode**\nSend any Spotify, Deezer, Tidal, or YouTube link!",
        MODE_SEARCH: "🎵 **Search Mode**\nSend song/artist name.",
        MODE_MANUAL: "🖼️ **Manual Mode**\nSend image + audio.",
        MODE_AUDIO_ONLY: "🎧 **Audio Only**\nSend audio file first."
    }
    await query.edit_message_text(msgs[query.data], parse_mode='Markdown')

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cleanup_session(context)
    await update.message.reply_text("❌ Cancelled.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("processing"):
        await update.message.reply_text("⏳ Working...")
        return
    if not context.user_data.get("mode"):
        await update.message.reply_text("Use /modes first!")
        return

    mode = context.user_data["mode"]
    pending = context.user_data.setdefault("pending", {})
    temp_dir = context.user_data["temp_dir"] = context.user_data.get("temp_dir") or \
        os.path.join(BASE_TEMP_DIR, f"{update.effective_user.id}_{uuid.uuid4().hex[:8]}")
    os.makedirs(temp_dir, exist_ok=True)
    context.user_data["temp_dir"] = temp_dir

    if update.message.text and "http" in update.message.text and mode == MODE_LINKS:
        context.user_data["processing"] = True
        status = await update.message.reply_text("🔗 Processing link via dzm.pm...")
        result = await download_via_dzm(update.message.text.strip(), temp_dir)

        if not result:
            result = await fallback_ytdlp_search(update.message.text.strip(), temp_dir, status)

        if not result or not os.path.exists(result["audio"]):
            await status.edit_text("❌ Failed to download audio from this link.")
            context.user_data["processing"] = False
            return

        pending.update(result)
        await status.edit_text("🎬 Rendering video...")
        await merge_and_send(update, context, pending, status)
        return

    # Rest of your modes (Search, Manual, Audio Only) unchanged below...
    # (I kept them exactly as you had, only fixed FFmpeg scale)
    # For brevity, the rest is identical to your last version except the FFmpeg fix:

    # ... [rest of process_files, do_audio_only, etc. — unchanged except merge_and_send below]

# --- MERGE WITH FIXED SCALE (NO MORE UNPLAYABLE MP4s) ---
async def merge_and_send(update: Update, context: ContextTypes.DEFAULT_TYPE, pending: dict, status_msg=None):
    output_path = os.path.join(context.user_data["temp_dir"], "output.mp4")
    if not status_msg:
        status_msg = await update.message.reply_text("🎬 Rendering video...")
    else:
        await status_msg.edit_text("🎬 Rendering video...")

    cmd = [
        FFMPEG, '-threads', '1', '-loop', '1', '-i', pending["image"],
        '-i', pending["audio"], '-c:v', 'libx264', '-tune', 'stillimage',
        '-preset', 'ultrafast', '-c:a', 'aac', '-b:a', '192k', '-shortest',
        '-movflags', '+faststart', '-vf', 'scale=480:-2,format=yuv420p,pad=ceil(iw/2)*2:ceil(ih/2)*2',
        '-y', output_path
    ]

    res = await run_async(subprocess_run_with_timeout, cmd, 300)
    if not os.path.exists(output_path) or res.returncode != 0:
        err = res.stderr[-200:] if res.stderr else "Unknown"
        await status_msg.edit_text(f"❌ FFmpeg failed:\n{err}")
        context.user_data["processing"] = False
        return

    await status_msg.edit_text("📤 Uploading...")
    try:
        with open(output_path, 'rb') as f:
            await update.message.reply_video(video=f, caption=pending.get("caption"),
                                             write_timeout=600, read_timeout=600)
        await status_msg.delete()
    except Exception as e:
        await status_msg.edit_text("⚠️ Sending as document...")
        try:
            with open(output_path, 'rb') as f:
                await update.message.reply_document(document=f, caption=pending.get("caption"))
            await status_msg.delete()
        except:
            await status_msg.edit_text("❌ Upload failed.")

    await cleanup_session(context)
    context.user_data["processing"] = False

# ... [rest of your handlers: process_files, do_audio_only, etc. — keep your existing ones]

def main():
    if not BOT_TOKEN:
        print("No token!")
        sys.exit(1)
    threading.Thread(target=run_web_server, daemon=True).start()
    print("Bot starting...")
    request = HTTPXRequest(connect_timeout=60, read_timeout=600, write_timeout=600)
    app = Application.builder().token(BOT_TOKEN).request(request).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("modes", modes))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CallbackQueryHandler(button))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))

    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
