import logging, os, uuid, shutil, requests, asyncio, subprocess, sys, threading
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from concurrent.futures import ThreadPoolExecutor
from flask import Flask

# --- CONFIGURATION ---
BOT_TOKEN = "8419784067:AAGMTG8M9QIzOBD56B_ROHe6a_VIHO6UpCM"  # <--- PASTE TOKEN HERE

# Render Free Tier has only 512MB RAM. 
# We MUST limit workers to 1 and file size to prevent crashing.
FFMPEG = "ffmpeg"
MAX_FILE_SIZE = 20 * 1024 * 1024  # Limit to 20MB input to be safe
MAX_WORKERS = 1                   # Only 1 video at a time
BASE_TEMP_DIR = "temp"

# Constants
MODE_SEARCH = "search"
MODE_MANUAL = "manual"
MODE_AUDIO_ONLY = "audio_only"
MODE_AUDIO_ONLY_DETAILS = "audio_only_details"

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

os.makedirs(BASE_TEMP_DIR, exist_ok=True)
executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)

# --- FAKE WEB SERVER (To keep Render alive) ---
app_flask = Flask(__name__)

@app_flask.route('/')
def health_check():
    return "Bot is Alive!", 200

def run_web_server():
    # Render provides the PORT env variable, defaults to 10000
    port = int(os.environ.get("PORT", 10000))
    app_flask.run(host="0.0.0.0", port=port)

# --- BOT LOGIC ---

async def run_async(func, *args, **kwargs):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(executor, lambda: func(*args, **kwargs))

def subprocess_run_with_timeout(cmd, timeout):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return type('Result', (), {'returncode': 1, 'stderr': 'Timeout'})()
    except Exception as e:
        return type('Result', (), {'returncode': 1, 'stderr': str(e)})()

async def cleanup_session(context: ContextTypes.DEFAULT_TYPE):
    temp_dir = context.user_data.get("temp_dir")
    if temp_dir and os.path.exists(temp_dir):
        try: shutil.rmtree(temp_dir, ignore_errors=True)
        except: pass
    context.user_data.clear()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_mode_selection(update, context, "🎬 **Render Bot Ready!**\n\nChoose a mode:")

async def modes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("processing"):
        await update.message.reply_text("⏳ Busy...")
        return
    await show_mode_selection(update, context, "🔄 Select mode:")

async def show_mode_selection(update: Update, context: ContextTypes.DEFAULT_TYPE, message_text: str):
    keyboard = [
        [InlineKeyboardButton("🎵 1. Song Search", callback_data=MODE_SEARCH)],
        [InlineKeyboardButton("🖼️ 2. Image + Audio", callback_data=MODE_MANUAL)],
        [InlineKeyboardButton("🎧 3. Audio → Auto Cover", callback_data=MODE_AUDIO_ONLY)],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    if update.message: await update.message.reply_text(message_text, reply_markup=reply_markup)
    else: await update.callback_query.edit_message_text(message_text, reply_markup=reply_markup)

async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["mode"] = query.data
    context.user_data["pending"] = {}
    session_dir = os.path.join(BASE_TEMP_DIR, f"{update.effective_user.id}_{uuid.uuid4().hex[:6]}")
    os.makedirs(session_dir, exist_ok=True)
    context.user_data["temp_dir"] = session_dir
    
    prompts = {
        MODE_SEARCH: "🎵 **Search Mode**\nSend song name.",
        MODE_MANUAL: "🖼️ **Manual Mode**\nSend Image + Audio.",
        MODE_AUDIO_ONLY: "🎧 **Audio Mode**\nSend audio file."
    }
    await query.edit_message_text(prompts.get(query.data, "Ready"))

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cleanup_session(context)
    await update.message.reply_text("❌ Cancelled.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("processing"):
        await update.message.reply_text("⏳ Working...")
        return
    if not context.user_data.get("mode"):
        await update.message.reply_text("Select mode: /modes")
        return

    temp_dir = context.user_data.get("temp_dir")
    if not temp_dir or not os.path.exists(temp_dir):
        session_dir = os.path.join(BASE_TEMP_DIR, f"{update.effective_user.id}_{uuid.uuid4().hex[:6]}")
        os.makedirs(session_dir, exist_ok=True)
        context.user_data["temp_dir"] = session_dir
        context.user_data["pending"] = {}

    pending = context.user_data.setdefault("pending", {})
    mode = context.user_data["mode"]

    if update.message.text and not (update.message.photo or update.message.audio or update.message.voice):
        if mode == MODE_SEARCH:
            context.user_data["processing"] = True
            await do_search(update, context)
            return
        elif mode == MODE_AUDIO_ONLY_DETAILS:
            pending["track_details"] = update.message.text.strip()
            context.user_data["processing"] = True
            await do_audio_only(update, context, pending)
            return

    await process_files(update, context, pending, temp_dir)

async def process_files(update: Update, context: ContextTypes.DEFAULT_TYPE, pending: dict, temp_dir: str):
    if update.message.photo:
        context.user_data["processing"] = True
        try:
            file = await update.message.photo[-1].get_file()
            path = os.path.join(temp_dir, "image.jpg")
            await file.download_to_drive(path)
            pending["image"] = path
            await update.message.reply_text("✅ Image set.")
            if pending.get("audio") and context.user_data.get("mode") == MODE_MANUAL:
                 await merge_and_send(update, context, pending)
            else:
                context.user_data["processing"] = False
        except: context.user_data["processing"] = False

    audio_obj = update.message.audio or update.message.voice or (update.message.document if update.message.document and "audio" in update.message.document.mime_type else None)
    if audio_obj:
        context.user_data["processing"] = True
        try:
            file = await audio_obj.get_file()
            ext = ".m4a" if update.message.voice else os.path.splitext(audio_obj.file_name or "")[1] or ".mp3"
            path = os.path.join(temp_dir, f"audio{ext}")
            await file.download_to_drive(path)
            pending["audio"] = path
            
            mode = context.user_data.get("mode")
            if mode == MODE_AUDIO_ONLY:
                context.user_data["mode"] = MODE_AUDIO_ONLY_DETAILS
                context.user_data["processing"] = False
                await update.message.reply_text(f"🎧 Got audio! Reply with **Artist - Song Name**.")
            elif mode == MODE_MANUAL and pending.get("image"):
                await merge_and_send(update, context, pending)
            else:
                 await update.message.reply_text("✅ Audio set.")
                 context.user_data["processing"] = False
        except: context.user_data["processing"] = False

async def do_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.message.text.strip()
    temp_dir = context.user_data["temp_dir"]
    pending = context.user_data["pending"]
    try:
        status = await update.message.reply_text("🔍 Searching...")
        response = await run_async(requests.get, f"https://api.deezer.com/search/album?q={requests.utils.quote(query)}&limit=1", timeout=10)
        data = response.json()
        if not data.get("data"):
            await status.edit_text("❌ No results.")
            context.user_data["processing"] = False
            return

        album = data["data"][0]
        cover_data = await run_async(requests.get, album["cover_big"], timeout=10)
        with open(os.path.join(temp_dir, "cover.jpg"), "wb") as f: f.write(cover_data.content)
        pending["image"] = os.path.join(temp_dir, "cover.jpg")
        
        audio_path = os.path.join(temp_dir, "audio.m4a")
        cmd = ['yt-dlp', '-x', '--audio-format', 'm4a', '-o', audio_path, f"ytsearch1:{album['artist']['name']} {album['title']} audio", '--quiet']
        await run_async(subprocess_run_with_timeout, cmd, 120)
        
        pending["audio"] = audio_path
        pending["caption"] = f"🎵 {album['artist']['name']} - {album['title']}"
        await merge_and_send(update, context, pending, status)
    except:
        await status.edit_text("❌ Error.")
        context.user_data["processing"] = False

async def do_audio_only(update: Update, context: ContextTypes.DEFAULT_TYPE, pending: dict):
    temp_dir = context.user_data["temp_dir"]
    try:
        status = await update.message.reply_text("🔍 Fetching cover...")
        response = await run_async(requests.get, f"https://api.deezer.com/search?q={requests.utils.quote(pending['track_details'])}&limit=1", timeout=10)
        data = response.json()
        cover_path = os.path.join(temp_dir, "cover.jpg")
        if data.get("data"):
            track = data["data"][0]
            cover_data = await run_async(requests.get, track["album"]["cover_big"], timeout=10)
            with open(cover_path, "wb") as f: f.write(cover_data.content)
            pending["caption"] = f"🎵 {track['artist']['name']} - {track['title']}"
        else:
            cmd = [FFMPEG, '-f', 'lavfi', '-i', 'color=c=black:s=500x500', '-frames:v', '1', '-y', cover_path]
            await run_async(subprocess_run_with_timeout, cmd, 10)
            pending["caption"] = "🎵 Audio Only"
        pending["image"] = cover_path
        await merge_and_send(update, context, pending, status)
    except: context.user_data["processing"] = False

async def merge_and_send(update: Update, context: ContextTypes.DEFAULT_TYPE, pending: dict, status_msg=None):
    temp_dir = context.user_data["temp_dir"]
    output = os.path.join(temp_dir, "video.mp4")
    if not status_msg: status_msg = await update.message.reply_text("🎬 Rendering...")
    else: await status_msg.edit_text("🎬 Rendering...")

    # Low memory mode for Render
    cmd = [
        FFMPEG, '-threads', '1', '-loop', '1', '-i', pending["image"], '-i', pending["audio"],
        '-c:v', 'libx264', '-tune', 'stillimage', '-preset', 'ultrafast', '-c:a', 'copy',
        '-shortest', '-movflags', '+faststart', '-vf', 'scale=480:trunc(480*ih/iw),format=yuv420p', '-y', output
    ]
    res = await run_async(subprocess_run_with_timeout, cmd, 300)
    if res.returncode != 0:
        cmd[15] = 'aac'
        await run_async(subprocess_run_with_timeout, cmd, 300)

    await status_msg.edit_text("📤 Uploading...")
    try:
        with open(output, 'rb') as f:
            await update.message.reply_video(video=f, caption=pending.get("caption"), write_timeout=60)
        await status_msg.delete()
    except: await status_msg.edit_text("❌ Upload failed.")
    await cleanup_session(context)
    context.user_data["processing"] = False

def main():
    if "PASTE" in BOT_TOKEN:
        print("❌ ERROR: Token not set")
        sys.exit(1)

    # Start Fake Web Server in a separate thread
    threading.Thread(target=run_web_server, daemon=True).start()
    print("🌍 Fake Web Server Started")

    # Start Bot
    print("🚀 Bot Started")
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("modes", modes))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CallbackQueryHandler(button))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))
    app.run_polling()

if __name__ == "__main__":
    main()
