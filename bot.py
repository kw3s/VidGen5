import logging, os, uuid, shutil, requests, asyncio, subprocess, sys, threading, time
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from telegram.request import HTTPXRequest
from concurrent.futures import ThreadPoolExecutor
from flask import Flask

# --- CONFIGURATION ---

# Get Token from Render Environment
BOT_TOKEN = os.environ.get("BOT_TOKEN")

# Render Settings
FFMPEG = "ffmpeg"
MAX_FILE_SIZE = 50 * 1024 * 1024
MAX_WORKERS = 2
BASE_TEMP_DIR = "temp"

# Mode Constants (From your original code)
MODE_SEARCH = "search"
MODE_MANUAL = "manual"
MODE_AUDIO_ONLY = "audio_only"
MODE_AUDIO_ONLY_DETAILS = "audio_only_details"

# Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)

# Ensure temp dir exists
os.makedirs(BASE_TEMP_DIR, exist_ok=True)
executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)

# --- FAKE WEB SERVER (REQUIRED FOR RENDER) ---
app_flask = Flask(__name__)

@app_flask.route('/')
def health_check():
    return "Bot is Alive!", 200

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    app_flask.run(host="0.0.0.0", port=port)

# --- ASYNC HELPERS ---

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

# --- BOT LOGIC (RESTORED FROM ORIGINAL) ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_mode_selection(update, context, "🎬 **Bot Ready!**\nChoose a mode:")

async def modes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("processing"):
        await update.message.reply_text("⏳ Please wait...")
        return
    await show_mode_selection(update, context, "🔄 Select mode:")

async def show_mode_selection(update: Update, context: ContextTypes.DEFAULT_TYPE, message_text: str):
    keyboard = [
        [InlineKeyboardButton("🎵 1. Song/Album Search", callback_data=MODE_SEARCH)],
        [InlineKeyboardButton("🖼️ 2. Image + Audio", callback_data=MODE_MANUAL)],
        [InlineKeyboardButton("🎧 3. Audio → Auto Cover", callback_data=MODE_AUDIO_ONLY)],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    if update.message:
        await update.message.reply_text(message_text, reply_markup=reply_markup)
    else:
        await update.callback_query.edit_message_text(message_text, reply_markup=reply_markup)

async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    context.user_data["mode"] = query.data
    context.user_data["pending"] = {}
    
    # Create unique session folder
    session_dir = os.path.join(BASE_TEMP_DIR, f"{update.effective_user.id}_{uuid.uuid4().hex[:6]}")
    os.makedirs(session_dir, exist_ok=True)
    context.user_data["temp_dir"] = session_dir

    if query.data == MODE_SEARCH:
        msg = "🎵 **Mode: Search**\nSend song name (e.g. 'Thriller MJ')."
    elif query.data == MODE_MANUAL:
        msg = "🖼️ **Mode: Manual**\nSend Image + Audio."
    elif query.data == MODE_AUDIO_ONLY:
        msg = "🎧 **Mode: Audio Only**\nSend audio file first."
    
    await query.edit_message_text(msg, parse_mode='Markdown')

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cleanup_session(context)
    await update.message.reply_text("❌ Cancelled.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("processing"):
        await update.message.reply_text("⏳ Working...")
        return
    
    if not context.user_data.get("mode"):
        await update.message.reply_text("Please select a mode: /modes")
        return

    # Restore session if lost
    if not context.user_data.get("temp_dir") or not os.path.exists(context.user_data.get("temp_dir")):
        session_dir = os.path.join(BASE_TEMP_DIR, f"{update.effective_user.id}_{uuid.uuid4().hex[:6]}")
        os.makedirs(session_dir, exist_ok=True)
        context.user_data["temp_dir"] = session_dir
        context.user_data["pending"] = {}

    mode = context.user_data["mode"]
    pending = context.user_data.setdefault("pending", {})

    # Text Handling (Search & Audio Details)
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

    await process_files(update, context, pending, context.user_data["temp_dir"])

async def process_files(update: Update, context: ContextTypes.DEFAULT_TYPE, pending: dict, temp_dir: str):
    # Image
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

    # Audio
    audio_obj = update.message.audio or update.message.voice or (update.message.document if update.message.document and "audio" in update.message.document.mime_type else None)
    
    if audio_obj:
        context.user_data["processing"] = True
        try:
            file = await audio_obj.get_file()
            ext = ".m4a" if update.message.voice else os.path.splitext(audio_obj.file_name or "")[1] or ".mp3"
            path = os.path.join(temp_dir, f"audio{ext}")
            await file.download_to_drive(path)
            pending["audio"] = path
            pending["title"] = getattr(audio_obj, 'title', 'Unknown Track')
            
            mode = context.user_data.get("mode")
            if mode == MODE_AUDIO_ONLY:
                context.user_data["mode"] = MODE_AUDIO_ONLY_DETAILS
                context.user_data["processing"] = False
                await update.message.reply_text(f"🎧 Got audio! Now send the **Artist - Song Name** to find the cover.")
            elif mode == MODE_MANUAL and pending.get("image"):
                await merge_and_send(update, context, pending)
            else:
                 await update.message.reply_text("✅ Audio set.")
                 context.user_data["processing"] = False
        except: context.user_data["processing"] = False

# --- DEEZER LOGIC (RESTORED) ---

async def do_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.message.text.strip()
    temp_dir = context.user_data["temp_dir"]
    pending = context.user_data["pending"]
    
    try:
        status = await update.message.reply_text("🔍 Searching Deezer...")
        
        # Use Deezer API exactly as in original
        search_url = f"https://api.deezer.com/search/album?q={requests.utils.quote(query)}&limit=1"
        response = await run_async(requests.get, search_url, timeout=15)
        data = response.json()
        
        if not data.get("data"):
            await status.edit_text("❌ No results found.")
            context.user_data["processing"] = False
            return

        album = data["data"][0]
        cover_url = album["cover_big"]
        artist = album["artist"]["name"]
        title = album["title"]

        # Download Cover
        cover_path = os.path.join(temp_dir, "cover.jpg")
        cover_data = await run_async(requests.get, cover_url, timeout=15)
        with open(cover_path, "wb") as f: f.write(cover_data.content)
        pending["image"] = cover_path
        
        await status.edit_text("⬇️ Downloading audio...")
        
        # Download Audio via yt-dlp
        audio_path = os.path.join(temp_dir, "audio.m4a")
        search_q = f"{artist} {title} audio"
        cmd = ['yt-dlp', '-x', '--audio-format', 'm4a', '-o', audio_path, f'ytsearch1:{search_q}', '--quiet']
        await run_async(subprocess_run_with_timeout, cmd, 120)
        
        pending["audio"] = audio_path
        pending["caption"] = f"🎵 {artist} - {title}"
        
        await merge_and_send(update, context, pending, status)

    except Exception as e:
        logger.error(f"Search Error: {e}")
        await update.message.reply_text("❌ Error occurred.")
        context.user_data["processing"] = False

async def do_audio_only(update: Update, context: ContextTypes.DEFAULT_TYPE, pending: dict):
    temp_dir = context.user_data["temp_dir"]
    query = pending["track_details"]
    
    try:
        status = await update.message.reply_text("🔍 Fetching cover...")
        
        # Deezer API search for track
        search_url = f"https://api.deezer.com/search?q={requests.utils.quote(query)}&limit=1"
        response = await run_async(requests.get, search_url, timeout=15)
        data = response.json()
        cover_path = os.path.join(temp_dir, "cover.jpg")
        
        if data.get("data"):
            track = data["data"][0]
            cover_data = await run_async(requests.get, track["album"]["cover_big"], timeout=10)
            with open(cover_path, "wb") as f: f.write(cover_data.content)
            pending["caption"] = f"🎵 {track['artist']['name']} - {track['title']}"
        else:
            # Black background fallback
            cmd = [FFMPEG, '-f', 'lavfi', '-i', 'color=c=black:s=500x500', '-frames:v', '1', '-y', cover_path]
            await run_async(subprocess_run_with_timeout, cmd, 10)
            pending["caption"] = f"🎵 {pending.get('title', 'Unknown')}"

        pending["image"] = cover_path
        await merge_and_send(update, context, pending, status)
    except Exception as e:
        logger.error(e)
        context.user_data["processing"] = False

# --- UPLOAD & RENDER (FIXED FOR TIMEOUTS) ---

async def merge_and_send(update: Update, context: ContextTypes.DEFAULT_TYPE, pending: dict, status_msg=None):
    temp_dir = context.user_data["temp_dir"]
    output_path = os.path.join(temp_dir, "video.mp4")
    
    if not status_msg: status_msg = await update.message.reply_text("🎬 Rendering...")
    else: await status_msg.edit_text("🎬 Rendering...")

    # 1. FFmpeg (AAC for compatibility)
    cmd = [
        FFMPEG, '-threads', '1', '-loop', '1', '-i', pending["image"], '-i', pending["audio"],
        '-c:v', 'libx264', '-tune', 'stillimage', '-preset', 'ultrafast', 
        '-c:a', 'aac', '-b:a', '128k', 
        '-shortest', '-movflags', '+faststart', '-vf', 'scale=480:trunc(480*ih/iw),format=yuv420p', '-y', output_path
    ]
    
    await run_async(subprocess_run_with_timeout, cmd, 300)
    
    if not os.path.exists(output_path):
        await status_msg.edit_text("❌ FFmpeg Failed.")
        context.user_data["processing"] = False
        return

    await status_msg.edit_text("📤 Uploading (this may take a moment)...")

    # 2. Upload with HIGH TIMEOUTS (Fixes "Upload Failed")
    try:
        with open(output_path, 'rb') as f:
            await update.message.reply_video(
                video=f, 
                caption=pending.get("caption"), 
                write_timeout=300,   # 5 minutes
                connect_timeout=60,
                read_timeout=300
            )
        await status_msg.delete()
    except Exception as e:
        logger.error(f"Video Upload Error: {e}")
        # Fallback to Document
        try:
            await status_msg.edit_text("⚠️ Video upload timed out, sending as file...")
            with open(output_path, 'rb') as f:
                await update.message.reply_document(
                    document=f, 
                    caption=pending.get("caption"),
                    write_timeout=300
                )
            await status_msg.delete()
        except Exception as e2:
             await status_msg.edit_text(f"❌ Upload failed completely.")

    await cleanup_session(context)
    context.user_data["processing"] = False

def main():
    if not BOT_TOKEN:
        print("❌ ERROR: Token not set in Render Environment Variables")
        sys.exit(1)

    # Start Fake Server
    threading.Thread(target=run_web_server, daemon=True).start()
    print("🚀 Bot Started")
    
    # Configure Bot with Extended Timeouts
    # This is the specific fix for "No Response" / "Upload Failed"
    request = HTTPXRequest(connect_timeout=60, read_timeout=300, write_timeout=300)
    
    app = Application.builder().token(BOT_TOKEN).request(request).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("modes", modes))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CallbackQueryHandler(button))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))
    app.run_polling()

if __name__ == "__main__":
    main()
