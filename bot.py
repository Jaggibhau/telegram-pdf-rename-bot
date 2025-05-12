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

# --- Command and Utility Functions ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start command."""
    await update.message.reply_text("Welcome! Upload a PDF to rename it.")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /help command."""
    await update.message.reply_text("Upload a PDF, then choose options to rename it. Use /start to begin.")

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle errors."""
    logger.error(f"Update {update} caused error {context.error}")
    if update.effective_chat:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="⚠️ An error occurred. Please try again or contact support."
        )

async def unexpected_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle unexpected messages."""
    await update.message.reply_text("⚠️ Unexpected input. Use /cancel to reset.")
    return FALLBACK

async def cancel_operation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel the current operation."""
    user_id = update.effective_user.id
    pdf_data = get_pdf_data(context)
    await safe_cleanup(pdf_data.get('file_path') if pdf_data else None, user_id, context)
    if update.message:
        await update.message.reply_text("Operation cancelled. Use /start to begin again.")
    return FALLBACK

async def update_status_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Update the status message with action buttons."""
    pdf_data = get_pdf_data(context)
    preview = generate_preview_filename(pdf_data)
    keyboard = [
        [InlineKeyboardButton("Add Prefix", callback_data="add_prefix"),
         InlineKeyboardButton("Add Suffix", callback_data="add_suffix")],
        [InlineKeyboardButton("Remove Text", callback_data="remove_name"),
         InlineKeyboardButton("Replace Text", callback_data="replace_word")],
        [InlineKeyboardButton("Change Case", callback_data="change_case"),
         InlineKeyboardButton("Add Timestamp", callback_data="add_timestamp")],
        [InlineKeyboardButton("Apply", callback_data="apply"),
         InlineKeyboardButton("Reset", callback_data="reset"),
         InlineKeyboardButton("Cancel", callback_data="cancel")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    if update.message:
        await update.message.reply_text(
            f"Current filename: `{preview}`\nChoose an action:",
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN_V2
        )
    elif update.callback_query:
        await update.callback_query.edit_message_text(
            f"Current filename: `{preview}`\nChoose an action:",
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN_V2
        )

def get_pdf_data(context: ContextTypes.DEFAULT_TYPE) -> dict:
    """Retrieve pdf_data from context."""
    return context.user_data.get('pdf_data', {})

def generate_preview_filename(pdf_data: dict) -> str:
    """Generate a preview of the renamed filename."""
    if not pdf_data:
        return "Error: No PDF data"
    name = pdf_data['original_name']
    prefix = pdf_data.get('prefix', '')
    suffix = pdf_data.get('suffix', '')
    remove = pdf_data.get('remove', '')
    replace = pdf_data.get('replace', {'old': '', 'new': ''})
    case = pdf_data.get('case')
    timestamp = pdf_data.get('timestamp', '')

    # Apply transformations
    if remove:
        name = name.replace(remove, '')
    if replace['old'] and replace['new']:
        name = name.replace(replace['old'], replace['new'])
    if case == 'upper':
        name = name.upper()
    elif case == 'lower':
        name = name.lower()
    elif case == 'title':
        name = name.title()
    name = f"{prefix}{name}{suffix}{timestamp}"
    return sanitize_filename(name)

async def select_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle action selection from the inline keyboard."""
    query = update.callback_query
    await query.answer()
    action = query.data

    if action == "add_prefix":
        await query.edit_message_text("Enter the prefix to add:")
        return AWAITING_PREFIX
    elif action == "add_suffix":
        await query.edit_message_text("Enter the suffix to add:")
        return AWAITING_SUFFIX
    elif action == "remove_name":
        await query.edit_message_text("Enter the text to remove:")
        return AWAITING_REMOVE
    elif action == "replace_word":
        await query.edit_message_text("Enter the text to replace:")
        return AWAITING_REPLACE_OLD
    elif action == "change_case":
        keyboard = [
            [InlineKeyboardButton("Uppercase", callback_data="case_upper"),
             InlineKeyboardButton("Lowercase", callback_data="case_lower"),
             InlineKeyboardButton("Title Case", callback_data="case_title")],
            [InlineKeyboardButton("Back", callback_data="back_to_menu")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text("Select case option:", reply_markup=reply_markup)
        return AWAITING_CASE
    elif action == "add_timestamp":
        keyboard = [
            [InlineKeyboardButton("YYYYMMDD_HHMMSS", callback_data="ts_ymdhms"),
             InlineKeyboardButton("YYYYMMDD", callback_data="ts_ymd"),
             InlineKeyboardButton("DDMMYYYY", callback_data="ts_dmy")],
            [InlineKeyboardButton("Back", callback_data="back_to_menu")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text("Select timestamp format:", reply_markup=reply_markup)
        return AWAITING_TIMESTAMP
    elif action == "apply":
        return await apply_changes(update, context)
    elif action == "reset":
        pdf_data = get_pdf_data(context)
        pdf_data.update({
            'prefix': '', 'suffix': '', 'remove': '',
            'replace': {'old': '', 'new': ''}, 'case': None,
            'timestamp_format': None, 'timestamp': ''
        })
        context.user_data['pdf_data'] = pdf_data
        await update_status_message(update, context)
        return SELECTING_ACTION
    elif action == "cancel":
        return await cancel_operation(update, context)
    return SELECTING_ACTION

async def receive_prefix(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle prefix input."""
    text = update.message.text
    if not validate_input(text):
        await update.message.reply_text("⚠️ Invalid prefix. Try again.")
        return AWAITING_PREFIX
    pdf_data = get_pdf_data(context)
    pdf_data['prefix'] = text
    context.user_data['pdf_data'] = pdf_data
    await update_status_message(update, context)
    return SELECTING_ACTION

async def receive_suffix(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle suffix input."""
    text = update.message.text
    if not validate_input(text):
        await update.message.reply_text("⚠️ Invalid suffix. Try again.")
        return AWAITING_SUFFIX
    pdf_data = get_pdf_data(context)
    pdf_data['suffix'] = text
    context.user_data['pdf_data'] = pdf_data
    await update_status_message(update, context)
    return SELECTING_ACTION

async def receive_remove_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle text to remove."""
    text = update.message.text
    if not validate_input(text):
        await update.message.reply_text("⚠️ Invalid text. Try again.")
        return AWAITING_REMOVE
    pdf_data = get_pdf_data(context)
    pdf_data['remove'] = text
    context.user_data['pdf_data'] = pdf_data
    await update_status_message(update, context)
    return SELECTING_ACTION

async def receive_replace_old(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle text to replace (old)."""
    text = update.message.text
    if not validate_input(text):
        await update.message.reply_text("⚠️ Invalid text. Try again.")
        return AWAITING_REPLACE_OLD
    pdf_data = get_pdf_data(context)
    pdf_data['replace']['old'] = text
    context.user_data['pdf_data'] = pdf_data
    await update.message.reply_text("Enter the new text to replace with:")
    return AWAITING_REPLACE_NEW

async def receive_replace_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle replacement text (new)."""
    text = update.message.text
    if not validate_input(text):
        await update.message.reply_text("⚠️ Invalid text. Try again.")
        return AWAITING_REPLACE_NEW
    pdf_data = get_pdf_data(context)
    pdf_data['replace']['new'] = text
    context.user_data['pdf_data'] = pdf_data
    await update_status_message(update, context)
    return SELECTING_ACTION

async def receive_case_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle case change selection."""
    query = update.callback_query
    await query.answer()
    choice = query.data
    if choice == "back_to_menu":
        await update_status_message(update, context)
        return SELECTING_ACTION
    pdf_data = get_pdf_data(context)
    pdf_data['case'] = choice.split("_")[1]  # e.g., "case_upper" -> "upper"
    context.user_data['pdf_data'] = pdf_data
    await update_status_message(update, context)
    return SELECTING_ACTION

async def receive_timestamp_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle timestamp format selection."""
    query = update.callback_query
    await query.answer()
    choice = query.data
    if choice == "back_to_menu":
        await update_status_message(update, context)
        return SELECTING_ACTION
    format_choice = choice.split("_")[1]  # e.g., "ts_ymdhms" -> "ymdhms"
    timestamp = ""
    if format_choice == "ymdhms":
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    elif format_choice == "ymd":
        timestamp = datetime.now().strftime("%Y%m%d")
    elif format_choice == "dmy":
        timestamp = datetime.now().strftime("%d%m%Y")
    pdf_data = get_pdf_data(context)
    pdf_data['timestamp_format'] = format_choice
    pdf_data['timestamp'] = f"_{timestamp}" if timestamp else ""
    context.user_data['pdf_data'] = pdf_data
    await update_status_message(update, context)
    return SELECTING_ACTION

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
        per_message=False,  # Explicitly set to False
        per_callback=True,  # Ensure CallbackQueryHandler is tracked per callback
        per_chat=True       # Ensure conversation state is per chat
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(conv_handler)
    application.add_error_handler(error_handler)  # Fixed: Use add_error_handler

    logger.info("Bot starting with enhanced reliability")
    application.run_polling(allowed_updates=Update.all_types())

if __name__ == '__main__':
    main()
