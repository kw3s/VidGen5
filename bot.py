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
BASE_TEMP_DIR = "temp"

# Constants
MODE_SEARCH = "search"
MODE_MANUAL = "manual"
MODE_AUDIO_ONLY = "audio_only"
MODE_AUDIO_ONLY_DETAILS = "audio_only_details"
MODE_LINKS = "links"  # <--- NEW MODE

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

os.makedirs(BASE_TEMP_DIR, exist_ok=True)
executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)

# --- FAKE WEB SERVER ---
app_flask = Flask(__name__)
@app_flask.route('/')
def health_check(): return "Bot is Alive!", 200
def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    app_flask.run(host="0.0.0.0", port=port)

# --- HELPERS ---
async def run_async(func, *args, **kwargs):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(executor, lambda: func(*args, **kwargs))

def subprocess_run_with_timeout(cmd, timeout):
    try: return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except Exception as e: return type('Result', (), {'returncode': 1, 'stderr': str(e)})()

async def cleanup_session(context: ContextTypes.DEFAULT_TYPE):
    temp_dir = context.user_data.get("temp_dir")
    if temp_dir and os.path.exists(temp_dir):
        try: shutil.rmtree(temp_dir, ignore_errors=True)
        except: pass
    context.user_data.clear()

# --- LINK PARSER (THE MAGIC PART) ---
def resolve_metadata_from_link(url):
    """Scrapes the URL to find Artist - Song Name"""
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
    try:
        response = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(response.content, 'html.parser')
        title = soup.title.string if soup.title else ""
        
        # Clean up the title based on platform
        clean_title = title
        
        if "spotify" in url:
            # Format: "Song - song by Artist | Spotify"
            clean_title = title.split("|")[0].replace("song and lyrics by", "").replace("song by", "")
        elif "deezer" in url:
            # Format: "Song - Artist - Listen on Deezer"
            clean_title = title.split("- Listen")[0]
        elif "apple" in url:
            # Format: "Song by Artist on Apple Music"
            clean_title = title.split(" on Apple")[0].replace(" by ", " ")
        elif "tidal" in url:
            # Format: "Song by Artist | TIDAL"
            clean_title = title.split("|")[0].replace(" by ", " ")
        elif "youtube" in url or "youtu.be" in url:
            # YouTube usually has the clean name in title
            clean_title = title.replace("- YouTube", "")
            
        return clean_title.strip()
    except Exception as e:
        logger.error(f"Link parsing failed: {e}")
        return None

# --- HANDLERS ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_mode_selection(update, context, "🎬 **Bot Ready!**\nChoose a mode:")

async def modes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("processing"):
        await update.message.reply_text("⏳ Please wait...")
        return
    await show_mode_selection(update, context, "🔄 Select mode:")

async def show_mode_selection(update: Update, context: ContextTypes.DEFAULT_TYPE, message_text: str):
    keyboard = [
        [InlineKeyboardButton("🔗 1. Link Mode (Spotify/Tidal/etc)", callback_data=MODE_LINKS)],
        [InlineKeyboardButton("🎵 2. Search by Name", callback_data=MODE_SEARCH)],
        [InlineKeyboardButton("🖼️ 3. Manual (Image+Audio)", callback_data=MODE_MANUAL)],
        [InlineKeyboardButton("🎧 4. Audio Only", callback_data=MODE_AUDIO_ONLY)],
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

    msgs = {
        MODE_LINKS: "🔗 **Link Mode**\nSend a link from Spotify, Deezer, Tidal, or Apple Music.",
        MODE_SEARCH: "🎵 **Search Mode**\nSend the song name (e.g. 'Thriller MJ').",
        MODE_MANUAL: "🖼️ **Manual Mode**\nSend Image + Audio.",
        MODE_AUDIO_ONLY: "🎧 **Audio Mode**\nSend audio file first."
    }
    await query.edit_message_text(msgs.get(query.data, "Ready"), parse_mode='Markdown')

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

    # Session check
    if not context.user_data.get("temp_dir") or not os.path.exists(context.user_data.get("temp_dir")):
        session_dir = os.path.join(BASE_TEMP_DIR, f"{update.effective_user.id}_{uuid.uuid4().hex[:6]}")
        os.makedirs(session_dir, exist_ok=True)
        context.user_data["temp_dir"] = session_dir
        context.user_data["pending"] = {}

    mode = context.user_data["mode"]
    pending = context.user_data.setdefault("pending", {})

    # Text Handler
    if update.message.text and not (update.message.photo or update.message.audio or update.message.voice):
        text = update.message.text.strip()
        
        # --- NEW LINK MODE LOGIC ---
        if mode == MODE_LINKS:
            if "http" not in text:
                await update.message.reply_text("❌ Please send a valid URL.")
                return
            
            context.user_data["processing"] = True
            status_msg = await update.message.reply_text("🔗 Analyzing link...")
            
            # 1. Scrape metadata from link
            meta_query = await run_async(resolve_metadata_from_link, text)
            
            if not meta_query:
                await status_msg.edit_text("❌ Could not identify song from link. Try Search Mode.")
                context.user_data["processing"] = False
                return
                
            await status_msg.edit_text(f"✅ Identified: **{meta_query}**\n🔍 Searching database...", parse_mode='Markdown')
            
            # 2. Pass metadata to the existing Search Logic
            await execute_deezer_search(update, context, meta_query, status_msg)
            return

        elif mode == MODE_SEARCH:
            context.user_data["processing"] = True
            msg = await update.message.reply_text("🔍 Searching...")
            await execute_deezer_search(update, context, text, msg)
            return
            
        elif mode == MODE_AUDIO_ONLY_DETAILS:
            pending["track_details"] = text
            context.user_data["processing"] = True
            await do_audio_only(update, context, pending)
            return

    await process_files(update, context, pending, context.user_data["temp_dir"])

# --- SHARED SEARCH LOGIC (Used by Search Mode AND Link Mode) ---
async def execute_deezer_search(update, context, query, status_msg):
    temp_dir = context.user_data["temp_dir"]
    pending = context.user_data["pending"]
    
    try:
        # Deezer Search
        search_url = f"https://api.deezer.com/search/album?q={requests.utils.quote(query)}&limit=1"
        response = await run_async(requests.get, search_url, timeout=10)
        data = response.json()
        
        if not data.get("data"):
            await status_msg.edit_text(f"❌ No results found for: {query}")
            context.user_data["processing"] = False
            return

        album = data["data"][0]
        cover_data = await run_async(requests.get, album["cover_big"], timeout=10)
        with open(os.path.join(temp_dir, "cover.jpg"), "wb") as f: f.write(cover_data.content)
        pending["image"] = os.path.join(temp_dir, "cover.jpg")
        
        await status_msg.edit_text(f"⬇️ Downloading audio for: {album['artist']['name']} - {album['title']}")
        
        # Download Audio
        audio_path = os.path.join(temp_dir, "audio.m4a")
        search_q = f"{album['artist']['name']} {album['title']} audio"
        cmd = ['yt-dlp', '-x', '--audio-format', 'm4a', '-o', audio_path, f'ytsearch1:{search_q}', '--quiet', '--max-filesize', '20M']
        
        res = await run_async(subprocess_run_with_timeout, cmd, 180)
        
        if not os.path.exists(audio_path):
            await status_msg.edit_text("❌ Audio download failed.")
            context.user_data["processing"] = False
            return

        pending["audio"] = audio_path
        pending["caption"] = f"🎵 {album['artist']['name']} - {album['title']}"
        
        await merge_and_send(update, context, pending, status_msg)

    except Exception as e:
        await status_msg.edit_text(f"❌ Error: {str(e)}")
        context.user_data["processing"] = False

# --- OTHER MODES ---

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
            else: context.user_data["processing"] = False
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
            pending["title"] = getattr(audio_obj, 'title', 'Unknown Track')
            
            mode = context.user_data.get("mode")
            if mode == MODE_AUDIO_ONLY:
                context.user_data["mode"] = MODE_AUDIO_ONLY_DETAILS
                context.user_data["processing"] = False
                await update.message.reply_text(f"🎧 Got audio! Send **Artist - Song Name**.")
            elif mode == MODE_MANUAL and pending.get("image"):
                await merge_and_send(update, context, pending)
            else:
                 await update.message.reply_text("✅ Audio set.")
                 context.user_data["processing"] = False
        except: context.user_data["processing"] = False

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
    output_path = os.path.join(temp_dir, "video.mp4")
    if not status_msg: status_msg = await update.message.reply_text("🎬 Rendering...")
    else: await status_msg.edit_text("🎬 Rendering...")

    cmd = [
        FFMPEG, '-threads', '1', '-loop', '1', '-i', pending["image"], '-i', pending["audio"],
        '-c:v', 'libx264', '-tune', 'stillimage', '-preset', 'ultrafast', 
        '-c:a', 'aac', '-b:a', '128k', 
        '-shortest', '-movflags', '+faststart', '-vf', 'scale=480:trunc(480*ih/iw),format=yuv420p', '-y', output_path
    ]
    
    res = await run_async(subprocess_run_with_timeout, cmd, 300)
    
    if not os.path.exists(output_path):
        log = res.stderr[-100:] if res.stderr else "Unknown"
        await status_msg.edit_text(f"❌ FFmpeg Error: {log}")
        context.user_data["processing"] = False
        return

    await status_msg.edit_text("📤 Uploading...")
    try:
        with open(output_path, 'rb') as f:
            await update.message.reply_video(
                video=f, caption=pending.get("caption"), 
                write_timeout=300, connect_timeout=60, read_timeout=300
            )
        await status_msg.delete()
    except Exception as e:
        try:
            await status_msg.edit_text("⚠️ Sending as file...")
            with open(output_path, 'rb') as f:
                await update.message.reply_document(document=f, caption=pending.get("caption"), write_timeout=300)
            await status_msg.delete()
        except:
            await status_msg.edit_text(f"❌ Upload Failed.")

    await cleanup_session(context)
    context.user_data["processing"] = False

def main():
    if not BOT_TOKEN: sys.exit(1)
    threading.Thread(target=run_web_server, daemon=True).start()
    print("🚀 Bot Started")
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
