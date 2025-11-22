import logging, os, uuid, shutil, requests, asyncio, time, subprocess, json, sys, threading
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from concurrent.futures import ThreadPoolExecutor
from flask import Flask # Required for Render

# --- RENDER CONFIGURATION ---
# Uses the token from Render Environment, or falls back to your string if testing locally
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8419784067:AAGMTG8M9QIzOBD56B_ROHe6a_VIHO6UpCM")

# Performance settings - Restored to your original preferences
FFMPEG = "ffmpeg"
MAX_FILE_SIZE = 50 * 1024 * 1024  # Increased slightly for Cloud (50MB)
MAX_VIDEO_SIZE = 50 * 1024 * 1024
WRITE_TIMEOUT = 600
MAX_WORKERS = 4  # Cloud can handle more than Termux

# Mode constants
MODE_SEARCH = "search"
MODE_MANUAL = "manual"
MODE_AUDIO_ONLY = "audio_only"
MODE_AUDIO_ONLY_DETAILS = "audio_only_details"

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)

# Thread pool
executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)

# --- FAKE WEB SERVER (REQUIRED FOR RENDER) ---
app_flask = Flask(__name__)

@app_flask.route('/')
def health_check():
    return "Bot is Alive!", 200

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    app_flask.run(host="0.0.0.0", port=port)

# --- ORIGINAL BOT LOGIC RESTORED ---

async def run_async(func, *args, **kwargs):
    """Run blocking function in thread pool"""
    try:
        loop = asyncio.get_running_loop()
        return await asyncio.wait_for(
            loop.run_in_executor(executor, lambda: func(*args, **kwargs)),
            timeout=300
        )
    except asyncio.TimeoutError:
        logger.error(f"⏰ Operation timed out: {func.__name__}")
        raise Exception("Operation timed out.")
    except Exception as e:
        logger.error(f"⚡ Async error: {e}")
        raise

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_mode_selection(update, context, "🎬 **Bot Ready!**\nChoose a mode to start:")

async def modes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("processing"):
        await update.message.reply_text("⏳ Please wait for current operation...")
        return
    await cleanup_temp_dir(context, force=True)
    await show_mode_selection(update, context, "🔄 Select mode:")

async def show_mode_selection(update: Update, context: ContextTypes.DEFAULT_TYPE, message_text: str):
    keyboard = [
        [InlineKeyboardButton("🎵 1. Song/Album Search", callback_data=MODE_SEARCH)],
        [InlineKeyboardButton("🖼️ 2. Send Image + Audio", callback_data=MODE_MANUAL)],
        [InlineKeyboardButton("🎧 3. Send Audio → Auto Cover", callback_data=MODE_AUDIO_ONLY)],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    if update.message:
        await update.message.reply_text(message_text, reply_markup=reply_markup)
    else:
        await update.callback_query.edit_message_text(message_text, reply_markup=reply_markup)

async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    mode = query.data
    context.user_data["mode"] = mode
    context.user_data["pending"] = {}
    context.user_data["processing"] = False
    
    # Render Compatible Temp Dir
    temp_dir = f"temp_{update.effective_user.id}_{uuid.uuid4().hex[:6]}"
    try:
        os.makedirs(temp_dir, exist_ok=True)
        context.user_data["temp_dir"] = temp_dir
    except Exception as e:
        await query.edit_message_text("❌ Failed to create storage.")
        return

    if mode == MODE_SEARCH:
        await query.edit_message_text("🎵 Mode: Song/Album Search\nSend album or song name.")
    elif mode == MODE_MANUAL:
        await query.edit_message_text("🖼️ Mode: Manual\nSend image + audio (any order).")
    elif mode == MODE_AUDIO_ONLY:
        await query.edit_message_text("🎧 Mode: Audio Only\nSend audio file first.")

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["processing"] = False
    await cleanup_temp_dir(context, force=True)
    await update.message.reply_text("❌ Operation cancelled.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("processing"):
        await update.message.reply_text("⏳ Please wait...")
        return

    mode = context.user_data.get("mode")
    if not mode:
        await update.message.reply_text("Please select a mode: /modes")
        return

    temp_dir = context.user_data.get("temp_dir")
    if not temp_dir or not os.path.exists(temp_dir):
        temp_dir = f"temp_{update.effective_user.id}_{uuid.uuid4().hex[:6]}"
        os.makedirs(temp_dir, exist_ok=True)
        context.user_data["temp_dir"] = temp_dir
        context.user_data["pending"] = {}

    pending = context.user_data.setdefault("pending", {})

    # Handle text input
    if update.message.text and not (update.message.photo or update.message.audio or update.message.document or update.message.voice):
        if mode == MODE_SEARCH:
            context.user_data["processing"] = True
            try:
                await do_search(update, context)
            finally:
                context.user_data["processing"] = False
            return
        elif mode == MODE_AUDIO_ONLY_DETAILS:
            pending["track_details"] = update.message.text.strip()
            context.user_data["processing"] = True
            try:
                await do_audio_only(update, context, pending)
            finally:
                context.user_data["processing"] = False
            return

    await process_files(update, context, pending, temp_dir)

async def process_files(update: Update, context: ContextTypes.DEFAULT_TYPE, pending: dict, temp_dir: str):
    # Process image
    if update.message.photo:
        context.user_data["processing"] = True
        try:
            file = await update.message.photo[-1].get_file()
            image_path = os.path.join(temp_dir, "image.jpg")
            await file.download_to_drive(image_path)
            pending["image"] = image_path
            await update.message.reply_text("✅ Image received!")
            
            if pending.get("audio") and context.user_data.get("mode") == MODE_MANUAL:
                 await merge_and_send(update, context, pending)
        finally:
            context.user_data["processing"] = False

    # Process audio
    audio_obj = update.message.audio or update.message.voice or (update.message.document if update.message.document and "audio" in update.message.document.mime_type else None)

    if audio_obj:
        context.user_data["processing"] = True
        try:
            file = await audio_obj.get_file()
            ext = ".m4a" if update.message.voice else os.path.splitext(audio_obj.file_name or "")[1] or ".m4a"
            audio_path = os.path.join(temp_dir, f"audio{ext}")
            await file.download_to_drive(audio_path)
            pending["audio"] = audio_path
            pending["title"] = getattr(audio_obj, 'title', 'Unknown Track')
            
            mode = context.user_data.get("mode")
            if mode == MODE_AUDIO_ONLY:
                context.user_data["mode"] = MODE_AUDIO_ONLY_DETAILS
                await update.message.reply_text(
                    f"🎧 Now tell me the exact track name and artist for:\n"
                    f"**{pending.get('title', 'this track')}**"
                )
                return
            elif mode == MODE_MANUAL and pending.get("image"):
                await merge_and_send(update, context, pending)
        finally:
            context.user_data["processing"] = False

# --- ORIGINAL SEARCH LOGIC ---
async def do_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.message.text.strip()
    temp_dir = context.user_data["temp_dir"]
    pending = context.user_data["pending"]
    
    try:
        status_msg = await update.message.reply_text("🔍 Searching Deezer...")
        
        # Original Search Logic
        search_url = f"https://api.deezer.com/search/album?q={requests.utils.quote(query)}&limit=1"
        response = await run_async(requests.get, search_url, timeout=15)
        response.raise_for_status()
        data = response.json()
        
        if not data.get("data"):
            raise Exception("No results found on Deezer.")
            
        album = data["data"][0]
        cover_url = album["cover_big"]
        artist = album["artist"]["name"]
        title = album["title"]
        
        await status_msg.edit_text(f"✅ Found: {artist} - {title}\n⬇️ Downloading cover...")
        
        cover_path = os.path.join(temp_dir, "cover.jpg")
        cover_data = await run_async(requests.get, cover_url, timeout=15)
        with open(cover_path, "wb") as f:
            f.write(cover_data.content)
        pending["image"] = cover_path
        
        await status_msg.edit_text(f"✅ Cover downloaded\n⬇️ Downloading audio...")
        
        audio_path = os.path.join(temp_dir, "audio.m4a")
        search_query = f"{artist} {title} audio"
        
        cmd = [
            'yt-dlp', '-x', '--audio-format', 'm4a',
            '-o', audio_path,
            f'ytsearch1:{search_query}',
            '--quiet', '--no-warnings',
            '--max-filesize', '50M',
            '--extract-audio'
        ]
        
        result = await run_async(subprocess_run_with_timeout, cmd, 120)
        if result.returncode != 0 or not os.path.exists(audio_path):
            raise Exception("Audio download failed")
            
        pending["audio"] = audio_path
        pending["caption"] = f"🎵 {artist} - {title}"
        
        await status_msg.edit_text("✅ Audio downloaded\n🎬 Creating video...")
        await merge_and_send(update, context, pending, status_msg)

    except Exception as e:
        logger.error(f"Search failed: {e}")
        await update.message.reply_text(f"❌ Search error: {str(e)}")
        await cleanup_temp_dir(context, force=True)

# --- ORIGINAL AUDIO ONLY LOGIC ---
async def do_audio_only(update: Update, context: ContextTypes.DEFAULT_TYPE, pending: dict):
    temp_dir = context.user_data["temp_dir"]
    track_details = pending.get("track_details", pending.get("title", "Unknown Track"))
    
    try:
        status_msg = await update.message.reply_text(f"🔍 Searching for: '{track_details}'...")
        
        clean_query = track_details.strip()
        # Original Logic: Limit 2 results, generic search
        search_url = f"https://api.deezer.com/search?q={requests.utils.quote(clean_query)}&limit=2"
        response = await run_async(requests.get, search_url, timeout=15)
        data = response.json()
        
        if not data.get("data"):
            raise Exception(f"No results for '{track_details}'")
        
        track = data["data"][0]
        cover_url = track["album"]["cover_big"]
        artist = track["artist"]["name"]
        song_title = track["title"]
        
        await status_msg.edit_text(f"✅ Found: {artist} - {song_title}\n⬇️ Downloading cover...")
        
        cover_path = os.path.join(temp_dir, "cover.jpg")
        cover_data = await run_async(requests.get, cover_url, timeout=15)
        with open(cover_path, "wb") as f:
            f.write(cover_data.content)
        pending["image"] = cover_path
        pending["caption"] = f"🎵 {artist} - {song_title}"
        
        await merge_and_send(update, context, pending, status_msg)

    except Exception as e:
        logger.error(f"Cover fetch failed: {e}")
        # Default Black Cover Fallback
        default_img_path = os.path.join(temp_dir, "default.jpg")
        cmd = [
            FFMPEG, '-f', 'lavfi', '-i', 'color=c=black:s=480x480',
            '-vf', "drawtext=text='No Cover':fontcolor=white:fontsize=24:x=(w-text_w)/2:y=(h-text_h)/2",
            '-vframes', '1', '-y', default_img_path
        ]
        await run_async(subprocess_run_with_timeout, cmd, 15)
        pending["image"] = default_img_path
        pending["caption"] = f"🎵 {pending.get('title', 'Unknown')}"
        await status_msg.edit_text("⚠️ Using default cover\n🎬 Creating video...")
        await merge_and_send(update, context, pending, status_msg)

# --- ORIGINAL MERGE & SEND (With minor AAC adjustment for compatibility) ---
async def merge_and_send(update: Update, context: ContextTypes.DEFAULT_TYPE, pending: dict, status_msg=None):
    temp_dir = context.user_data["temp_dir"]
    output_path = os.path.join(temp_dir, "video.mp4")
    caption = pending.get("caption", "🎵 Video ready!")
    
    try:
        if not status_msg:
            status_msg = await update.message.reply_text("🎬 Creating video...")
        
        # Original FFmpeg command structure
        cmd = [
            FFMPEG,
            '-loop', '1',
            '-i', pending["image"],
            '-i', pending["audio"],
            '-c:v', 'libx264',
            '-tune', 'stillimage',
            '-c:a', 'aac', '-b:a', '128k', # Keep AAC to prevent Telegram upload errors
            '-vf', 'scale=480:trunc(480*ih/iw),format=yuv420p',
            '-shortest',
            '-movflags', '+faststart',
            '-y',
            output_path,
            '-loglevel', 'error'
        ]
        
        await run_async(subprocess_run_with_timeout, cmd, 300)
        
        if not os.path.exists(output_path):
            raise Exception("FFmpeg failed to create video")
        
        await status_msg.edit_text("✅ Video ready\n📤 Sending...")
        
        # Single send attempt with fallback to document
        try:
            with open(output_path, 'rb') as video_file:
                await update.message.reply_video(
                    video_file,
                    caption=caption,
                    write_timeout=120
                )
            await status_msg.delete()
        except Exception as e:
            try:
                with open(output_path, 'rb') as video_file:
                    await update.message.reply_document(
                        video_file,
                        caption=caption,
                        write_timeout=120
                    )
                await status_msg.delete()
            except:
                await status_msg.edit_text("❌ Upload failed.")

        await cleanup_temp_dir(context, force=True)

    except Exception as e:
        logger.error(f"Processing failed: {e}")
        await update.message.reply_text(f"❌ Processing error: {str(e)}")
        await cleanup_temp_dir(context, force=True)

def subprocess_run_with_timeout(cmd, timeout):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except Exception:
        return type('Result', (), {'returncode': 1, 'stderr': 'Error'})()

async def cleanup_temp_dir(context: ContextTypes.DEFAULT_TYPE, force=False):
    temp_dir = context.user_data.get("temp_dir")
    if temp_dir and os.path.exists(temp_dir):
        try:
            shutil.rmtree(temp_dir, ignore_errors=True)
        except: pass
    context.user_data.clear()

def main():
    # Start Fake Web Server
    threading.Thread(target=run_web_server, daemon=True).start()
    
    # Start Bot
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("modes", modes))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CallbackQueryHandler(button))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))
    app.run_polling()

if __name__ == "__main__":
    main()
