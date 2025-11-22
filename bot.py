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
MAX_WORKERS = 2
BASE_TEMP_DIR = "/data/temp" if os.path.exists("/data") else "temp"  # Fly.io persistent storage

MODE_SEARCH = "search"
MODE_MANUAL = "manual"
MODE_AUDIO_ONLY = "audio_only"
MODE_AUDIO_ONLY_DETAILS = "audio_only_details"
MODE_LINKS = "links"

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

os.makedirs(BASE_TEMP_DIR, exist_ok=True)
executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)

# --- FAKE WEB SERVER FOR HOSTING PLATFORMS ---
app_flask = Flask(__name__)
@app_flask.route('/')
def health_check(): return "Bot Alive", 200

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    app_flask.run(host="0.0.0.0", port=port)

# --- HELPERS ---
async def run_async(func, *args, **kwargs):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(executor, lambda: func(*args, **kwargs))

def subprocess_run_with_timeout(cmd, timeout):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except Exception as e:
        return type('Result', (), {'returncode': 1, 'stderr': str(e)})()

async def cleanup_session(context: ContextTypes.DEFAULT_TYPE):
    temp_dir = context.user_data.get("temp_dir")
    if temp_dir and os.path.exists(temp_dir):
        try: shutil.rmtree(temp_dir, ignore_errors=True)
        except: pass
    context.user_data.clear()

# --- LINK METADATA PARSER ---
def resolve_metadata_from_link(url):
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
    try:
        r = requests.get(url, headers=headers, timeout=12)
        soup = BeautifulSoup(r.text, 'html.parser')
        title = soup.title.string if soup.title else ""
        clean = re.sub(r"( \| | - | on )(Spotify|TIDAL|Deezer|Apple Music|SoundCloud).*", "", title, flags=re.I)
        clean = re.sub(r"( - | )(song|Single|Album|EP).*", " ", clean, flags=re.I)
        clean = re.sub(r"(Listen to |Play |Stream )", "", clean, flags=re.I)
        return clean.strip() or "Unknown Track"
    except: return "Unknown Track"

# --- DEEZER + YTMDL SEARCH (REPLACED YT-DLP) ---
async def execute_deezer_search(query, temp_dir):
    try:
        search_url = f"https://api.deezer.com/search?q={requests.utils.quote(query)}&limit=1"
        resp = await run_async(requests.get, search_url, timeout=12)
        data = resp.json()

        if not data.get("data"):
            return None

        track = data["data"][0]
        artist = track["artist"]["name"]
        title = track["title"]
        cover_big = track["album"]["cover_big"]

        # Download cover (ytmdl might embed, but ensure we have it)
        cover_path = os.path.join(temp_dir, "cover.jpg")
        cover_data = await run_async(requests.get, cover_big, timeout=12)
        with open(cover_path, "wb") as f:
            f.write(cover_data.content)

        # ytmdl: music-focused replacement for better matches
        audio_path = os.path.join(temp_dir, "audio.m4a")
        search_q = f"{artist} {title}"
        cmd = [
            'ytmdl', '--quiet', '--format', 'm4a', '--no-meta',
            '--song', search_q, '-o', audio_path
        ]
        res = await run_async(subprocess_run_with_timeout, cmd, 180)

        if res.returncode != 0 or not os.path.exists(audio_path):
            logger.warning(f"ytmdl failed: {res.stderr[-200:]}")
            return None

        return {
            "audio": audio_path,
            "image": cover_path,
            "caption": f"🎵 {artist} - {title}"
        }
    except Exception as e:
        logger.error(f"Search failed: {e}")
        return None

# --- BOT HANDLERS ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_mode_selection(update, context, "🎬 **VidGen Pro Ready!**\nChoose mode:")

async def modes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("processing"):
        await update.message.reply_text("⏳ Please wait...")
        return
    await show_mode_selection(update, context, "🔄 Select mode:")

async def show_mode_selection(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    keyboard = [
        [InlineKeyboardButton("🔗 1. Link Mode (Spotify/Tidal/Deezer)", callback_data=MODE_LINKS)],
        [InlineKeyboardButton("🎵 2. Search Mode", callback_data=MODE_SEARCH)],
        [InlineKeyboardButton("🖼️ 3. Manual (Image + Audio)", callback_data=MODE_MANUAL)],
        [InlineKeyboardButton("🎧 4. Audio → Auto Cover", callback_data=MODE_AUDIO_ONLY)],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    if update.message:
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")
    else:
        await update.callback_query.edit_message_text(text, reply_markup=reply_markup, parse_mode="Markdown")

async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["mode"] = query.data
    context.user_data["pending"] = {}

    session_dir = os.path.join(BASE_TEMP_DIR, f"{update.effective_user.id}_{uuid.uuid4().hex[:8]}")
    os.makedirs(session_dir, exist_ok=True)
    context.user_data["temp_dir"] = session_dir

    msgs = {
        MODE_LINKS: "🔗 **Link Mode**\nSend Spotify, Deezer, Tidal, or YouTube link!",
        MODE_SEARCH: "🎵 Send song name or artist",
        MODE_MANUAL: "🖼️ Send image first, then audio",
        MODE_AUDIO_ONLY: "🎧 Send audio → I’ll add cover"
    }
    await query.edit_message_text(msgs[query.data], parse_mode="Markdown")

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cleanup_session(context)
    await update.message.reply_text("❌ Cancelled")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("processing"):
        await update.message.reply_text("⏳ Working...")
        return
    if not context.user_data.get("mode"):
        await update.message.reply_text("Use /modes first")
        return

    mode = context.user_data["mode"]
    temp_dir = context.user_data["temp_dir"] = context.user_data.get("temp_dir") or \
        os.path.join(BASE_TEMP_DIR, f"{update.effective_user.id}_{uuid.uuid4().hex[:8]}")
    os.makedirs(temp_dir, exist_ok=True)
    pending = context.user_data.setdefault("pending", {})

    # Link & Search Mode
    if update.message.text and "http" in update.message.text and mode == MODE_LINKS or mode == MODE_SEARCH:
        query = update.message.text.strip() if mode == MODE_SEARCH else await run_async(resolve_metadata_from_link, update.message.text.strip())
        context.user_data["processing"] = True
        status = await update.message.reply_text("🔍 Searching...")
        result = await execute_deezer_search(query, temp_dir)
        if not result:
            await status.edit_text("❌ No audio found. Try a different link/search.")
            context.user_data["processing"] = False
            return
        pending.update(result)
        await merge_and_send(update, context, pending, status)
        return

    await process_files(update, context, pending, temp_dir)

async def process_files(update: Update, context: ContextTypes.DEFAULT_TYPE, pending: dict, temp_dir: str):
    context.user_data["processing"] = True

    if update.message.photo:
        file = await update.message.photo[-1].get_file()
        path = os.path.join(temp_dir, "image.jpg")
        await file.download_to_drive(path)
        pending["image"] = path
        await update.message.reply_text("✅ Image received")
        if pending.get("audio") and context.user_data["mode"] == MODE_MANUAL:
            await merge_and_send(update, context, pending)
        context.user_data["processing"] = False
        return

    audio_obj = update.message.audio or update.message.voice or \
               (update.message.document if update.message.document and "audio" in update.message.document.mime_type else None)
    if audio_obj:
        file = await audio_obj.get_file()
        ext = ".m4a" if update.message.voice else (os.path.splitext(audio_obj.file_name or "")[1] or ".mp3")
        path = os.path.join(temp_dir, f"audio{ext}")
        await file.download_to_drive(path)
        pending["audio"] = path
        pending["title"] = getattr(audio_obj, "title", "Unknown Track")

        if context.user_data["mode"] == MODE_AUDIO_ONLY:
            context.user_data["mode"] = MODE_AUDIO_ONLY_DETAILS
            await update.message.reply_text("🎧 Audio received!\nNow send **Artist - Title** for cover")
        elif context.user_data["mode"] == MODE_MANUAL and pending.get("image"):
            await merge_and_send(update, context, pending)
        else:
            await update.message.reply_text("✅ Audio received")
        context.user_data["processing"] = False
        return

    if context.user_data["mode"] == MODE_AUDIO_ONLY_DETAILS and update.message.text:
        pending["track_details"] = update.message.text.strip()
        await do_audio_only(update, context, pending)
        return

    context.user_data["processing"] = False

async def do_audio_only(update: Update, context: ContextTypes.DEFAULT_TYPE, pending: dict):
    temp_dir = context.user_data["temp_dir"]
    status = await update.message.reply_text("🔍 Finding cover...")
    result = await execute_deezer_search(pending["track_details"], temp_dir)
    if result:
        pending.update(result)
    else:
        cover_path = os.path.join(temp_dir, "cover.jpg")
        cmd = [FFMPEG, '-f', 'lavfi', '-i', 'color=c=black:s=720x720:d=1', '-frames:v', '1', '-y', cover_path]
        res = await run_async(subprocess_run_with_timeout, cmd, 10)
        if not os.path.exists(cover_path):
            logger.error(f"Black cover failed: {res.stderr}")
            await status.edit_text("❌ Cover creation failed")
            context.user_data["processing"] = False
            return
        pending["image"] = cover_path
        pending["caption"] = f"🎵 {pending.get('title', 'Unknown Track')}"
    await merge_and_send(update, context, pending, status)

# --- FINAL MERGE: ULTRA-FAST & ALWAYS PLAYABLE ---
async def merge_and_send(update: Update, context: ContextTypes.DEFAULT_TYPE, pending: dict, status_msg=None):
    output = os.path.join(context.user_data["temp_dir"], "result.mp4")
    if not status_msg:
        status_msg = await update.message.reply_text("🎬 Rendering...")

    await status_msg.edit_text("🎬 Rendering video...")

    cmd = [
        FFMPEG, '-threads', '1', '-framerate', '1', '-loop', '1', '-i', pending["image"],
        '-i', pending["audio"], '-c:v', 'libx264', '-tune', 'stillimage', '-preset', 'ultrafast',
        '-crf', '28', '-c:a', 'copy', '-shortest', '-movflags', '+faststart',
        '-vf', 'scale=480:-2,format=yuv420p,pad=ceil(iw/2)*2:ceil(ih/2)*2', '-y', output
    ]

    res = await run_async(subprocess_run_with_timeout, cmd, 300)
    if not os.path.exists(output):
        await status_msg.edit_text("❌ Rendering failed")
        context.user_data["processing"] = False
        return

    await status_msg.edit_text("📤 Uploading...")
    try:
        with open(output, "rb") as f:
            await update.message.reply_video(video=f, caption=pending.get("caption"), write_timeout=600)
        await status_msg.delete()
    except:
        with open(output, "rb") as f:
            await update.message.reply_document(document=f, caption=pending.get("caption"))
        await status_msg.delete()

    await cleanup_session(context)
    context.user_data["processing"] = False

# --- START BOT ---
def main():
    if not BOT_TOKEN:
        print("BOT_TOKEN missing!")
        sys.exit(1)
    threading.Thread(target=run_web_server, daemon=True).start()
    print("🚀 VidGen Pro starting...")

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
