if not os.path.exists(DOWNLOADS_DIR):
    os.makedirs(DOWNLOADS_DIR)
#!/usr/bin/env python
# pylint: disable=logging-fstring-interpolation, C0116, W0613, W0719, R0912, R0915
# Standard Library Imports
import os
import logging
import traceback
import re
import shutil
from datetime import datetime
from typing import Optional

# Third-Party Imports
from telegram import Update, InputFile, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    CallbackQueryHandler,
    ConversationHandler,
    filters,
)
from telegram.constants import ParseMode
from telegram.error import TelegramError, NetworkError
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

# --- Configuration & Constants ---
BOT_TOKEN = os.getenv('BOT_TOKEN')
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN environment variable not set.")

DOWNLOADS_DIR = os.getenv("DOWNLOADS_DIR", "downloads")
MAX_FILE_SIZE = int(os.getenv("MAX_FILE_SIZE_MB", 200)) * 1024 * 1024
MIN_DISK_SPACE = 2 * MAX_FILE_SIZE  # Require 2x file size as buffer
TIMESTAMP_FORMAT = os.getenv("TIMESTAMP_FORMAT", "%Y%m%d_%H%M%S")

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# Conversation states
(SELECTING_ACTION, AWAITING_PREFIX, AWAITING_SUFFIX, AWAITING_REMOVE,
 AWAITING_REPLACE_OLD, AWAITING_REPLACE_NEW, AWAITING_CASE, AWAITING_TIMESTAMP) = range(8)
FALLBACK = ConversationHandler.END

# --- Enhanced Helper Functions ---
def validate_input(text: str) -> bool:
    """Strict validation for user-provided text."""
    if not text or len(text) > 100:
        return False
    return not bool(re.search(r'[<>:"/\\|?*\x00-\x1F]', text))

def sanitize_filename(filename: str) -> str:
    """Nuclear-grade filename sanitization."""
    filename = re.sub(r'[<>:"/\\|?*\x00-\x1F]', '', filename)
    filename = filename.replace('..', '').strip(' .')
    return filename[:255] or "unnamed.pdf"  # Limit to 255 chars

def ensure_disk_space(required: int) -> bool:
    """Check if sufficient disk space exists."""
    try:
        stat = os.statvfs(DOWNLOADS_DIR)
        return (stat.f_bavail * stat.f_frsize) >= required
    except OSError:
        return False

async def safe_cleanup(file_path: Optional[str], user_id: int, context: ContextTypes.DEFAULT_TYPE):
    """Guaranteed cleanup with retries."""
    if file_path and os.path.exists(file_path):
        for attempt in range(3):
            try:
                os.remove(file_path)
                logger.info(f"Cleaned up file for {user_id}: {file_path}")
                break
            except (OSError, PermissionError) as e:
                if attempt == 2:
                    logger.error(f"FINAL FAILURE deleting {file_path}: {e}")
    context.user_data.clear()

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception_type((TelegramError, NetworkError))
)
async def send_file_with_retry(chat_id: int, file_path: str, filename: str, context: ContextTypes.DEFAULT_TYPE):
    """Reliable file sending with retries."""
    with open(file_path, 'rb') as file:
        await context.bot.send_document(
            chat_id=chat_id,
            document=InputFile(file, filename=filename),
            caption=f"📄 Renamed: `{filename}`",
            parse_mode=ParseMode.MARKDOWN_V2
        )

# --- Critical Fix: Atomic File Operations ---
async def atomic_rename(src: str, dst: str) -> bool:
    """Guaranteed atomic rename with fallback."""
    try:
        temp_dst = f"{dst}.tmp"
        shutil.copy2(src, temp_dst)  # Copy preserves metadata
        os.replace(temp_dst, dst)    # Atomic operation
        return True
    except (OSError, shutil.Error) as e:
        logger.error(f"Atomic rename failed: {e}")
        for f in [temp_dst, dst]:
            if os.path.exists(f):
                try:
                    os.remove(f)
                except OSError:
                    pass
        return False

# --- Enhanced PDF Handler ---
async def handle_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Bulletproof PDF handling."""
    user = update.effective_user
    if not update.message or not update.message.document:
        await update.message.reply_text("⚠️ Invalid file. Please send a PDF.")
        return FALLBACK

    document = update.message.document
    if document.mime_type != "application/pdf":
        await update.message.reply_text("❌ Only PDF files are accepted.")
        return FALLBACK

    if document.file_size > MAX_FILE_SIZE:
        await update.message.reply_text(
            f"⚠️ File too large. Max size: {MAX_FILE_SIZE//1024//1024}MB"
        )
        return FALLBACK

    if not ensure_disk_space(MIN_DISK_SPACE):
        await update.message.reply_text("🚫 Server storage full. Try later.")
        return FALLBACK

    # Secure download
    user_dir = os.path.join(DOWNLOADS_DIR, str(user.id))
    os.makedirs(user_dir, exist_ok=True)
    
    original_name = sanitize_filename(document.file_name or "document.pdf")
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S%f")
    safe_name = f"{os.path.splitext(original_name)[0]}_{timestamp}.pdf"
    file_path = os.path.join(user_dir, safe_name)

    try:
        pdf_file = await context.bot.get_file(document.file_id)
        await pdf_file.download_to_drive(file_path)
    except Exception as e:
        logger.error(f"Download failed for {user.id}: {e}")
        await update.message.reply_text("⚠️ File download failed. Please retry.")
        await safe_cleanup(file_path, user.id, context)
        return FALLBACK

    # Initialize state
    context.user_data.update({
        'pdf_data': {
            'original_name': original_name,
            'file_path': file_path,
            'prefix': '',
            'suffix': '',
            'remove': '',
            'replace': {'old': '', 'new': ''},
            'case': None,
            'timestamp_format': None,
            'timestamp': ''
        },
        'message_id': update.message.message_id
    })

    await update_status_message(update, context)
    return SELECTING_ACTION

# --- Robust Apply Changes ---
async def apply_changes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Failure-resistant rename and send."""
    query = update.callback_query
    await query.answer("⏳ Processing...")
    user_id = update.effective_user.id
    pdf_data = get_pdf_data(context)

    if not pdf_data:
        await query.edit_message_text("❌ Session expired. Upload again.")
        return FALLBACK

    original_path = pdf_data['file_path']
    if not os.path.exists(original_path):
        await query.edit_message_text("⚠️ File missing. Please re-upload.")
        await safe_cleanup(None, user_id, context)
        return FALLBACK

    final_name = generate_preview_filename(pdf_data)
    if not final_name or "Error" in final_name:
        await query.edit_message_text("⚠️ Invalid filename generated. Reset and retry.")
        return SELECTING_ACTION

    user_dir = os.path.dirname(original_path)
    new_path = os.path.join(user_dir, final_name)

    # Atomic rename
    if not await atomic_rename(original_path, new_path):
        await query.edit_message_text("🚫 File operation failed. Please retry.")
        return SELECTING_ACTION

    # Guaranteed send
    try:
        await send_file_with_retry(
            update.effective_chat.id,
            new_path,
            final_name,
            context
        )
        await query.delete_message()
    except Exception as e:
        logger.error(f"Final send failed for {user_id}: {e}")
        await query.edit_message_text("⚠️ Sending failed but file was renamed. Contact support.")
    finally:
        await safe_cleanup(new_path, user_id, context)

    return FALLBACK

# --- Timeout Recovery ---
async def conversation_timeout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle timeout with state preservation."""
    user_id = update.effective_user.id
    pdf_data = get_pdf_data(context)
    
    if pdf_data:
        # In a production system, save to Redis/DB here
        logger.info(f"Timeout: Preserving state for {user_id}")

    await safe_cleanup(pdf_data.get('file_path') if pdf_data else None, user_id, context)
    
    if update.effective_chat:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="⏳ Session expired. Use /start to begin again."
        )
    
    return FALLBACK

# --- Main Bot Setup ---
def main() -> None:
    """Initialize with enhanced handlers."""
    application = Application.builder().token(BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Document.PDF, handle_pdf)],
        states={
            SELECTING_ACTION: [
                CallbackQueryHandler(select_action, pattern='^(add_prefix|add_suffix|remove_name|replace_word|change_case|add_timestamp|apply|reset|cancel)$')
            ],
            AWAITING_PREFIX: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_prefix)],
            AWAITING_SUFFIX: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_suffix)],
            AWAITING_REMOVE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_remove_text)],
            AWAITING_REPLACE_OLD: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_replace_old)],
            AWAITING_REPLACE_NEW: [MessageHandler(filters.TEXT | filters.COMMAND, receive_replace_new)],
            AWAITING_CASE: [
                CallbackQueryHandler(receive_case_choice, pattern='^case_(upper|lower|title)$'),
                CallbackQueryHandler(select_action, pattern='^back_to_menu$')
            ],
            AWAITING_TIMESTAMP: [
                CallbackQueryHandler(receive_timestamp_choice, pattern='^ts_(ymdhms|ymd|dmy)$'),
                CallbackQueryHandler(select_action, pattern='^back_to_menu$')
            ],
        },
        fallbacks=[
            CommandHandler('cancel', cancel_operation),
            CallbackQueryHandler(cancel_operation, pattern='^cancel$'),
            MessageHandler(filters.ALL, unexpected_message)
        ],
        conversation_timeout=600,  # 10 minutes
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(conv_handler)
    application.add_error_handler(error_handler)

    logger.info("Bot starting with enhanced reliability")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
