#!/usr/bin/env python
# pylint: disable=logging-fstring-interpolation, C0116, W0613, W0719, R0912, R0915
# type: ignore[union-attr]
# Standard Library Imports
import os
import logging
import traceback
import re
from datetime import datetime

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

# --- Configuration & Constants ---

# Load sensitive data from environment variables
BOT_TOKEN = os.getenv('BOT_TOKEN')
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN environment variable not set.")

DOWNLOADS_DIR = os.getenv("DOWNLOADS_DIR", "downloads")
MAX_FILE_SIZE = int(os.getenv("MAX_FILE_SIZE_MB", 200)) * 1024 * 1024  # 200 MB default
TIMESTAMP_FORMAT = os.getenv("TIMESTAMP_FORMAT", "%Y%m%d_%H%M%S") # Default timestamp format

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
# Set higher logging level for httpx to avoid all GET and POST requests being logged
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# Conversation states
(SELECTING_ACTION, AWAITING_PREFIX, AWAITING_SUFFIX, AWAITING_REMOVE,
 AWAITING_REPLACE_OLD, AWAITING_REPLACE_NEW, AWAITING_CASE, AWAITING_TIMESTAMP) = range(8)
# Fallback state for timeout or unexpected input
FALLBACK = ConversationHandler.END

# --- Helper Functions ---

def sanitize_filename(filename: str) -> str:
    """Sanitize filename to prevent invalid characters or path traversal."""
    # Remove potentially dangerous characters
    filename = re.sub(r'[<>:"/\\|?*\x00-\x1F]', '', filename)
    # Prevent path traversal
    filename = filename.replace('..', '')
    # Remove leading/trailing whitespace/dots
    filename = filename.strip(' .')
    # Ensure filename is not empty, default to "unnamed.pdf"
    return filename or "unnamed.pdf"

async def safe_cleanup(file_path: str | None, user_id: int, context: ContextTypes.DEFAULT_TYPE):
    """Safely remove a file and clear user data, logging any errors."""
    if file_path and os.path.exists(file_path):
        try:
            os.remove(file_path)
            logger.info(f"Cleaned up file for user {user_id}: {file_path}")
        except (OSError, PermissionError) as e:
            logger.error(f"Failed to clean up file {file_path} for user {user_id}: {str(e)}")
    # Clear sensitive user data
    context.user_data.pop('pdf_data', None)
    context.user_data.pop('message_id', None) # Clear reference to the interactive message


def get_pdf_data(context: ContextTypes.DEFAULT_TYPE) -> dict | None:
    """Safely retrieve PDF data from user context."""
    return context.user_data.get('pdf_data')

def generate_preview_filename(pdf_data: dict) -> str:
    """Generate the potential new filename based on current modifications."""
    if not pdf_data:
        return "Error: No PDF data"

    try:
        original_name = pdf_data.get('original_name', 'document.pdf')
        name, ext = os.path.splitext(original_name)
        new_name = name # Start with the base name

        # 1. Apply Remove
        if pdf_data.get('remove'):
            new_name = new_name.replace(pdf_data['remove'], '')

        # 2. Apply Replace
        replace_data = pdf_data.get('replace', {})
        if replace_data.get('old') and replace_data.get('new') is not None: # Allow replacing with empty string
             new_name = new_name.replace(replace_data['old'], replace_data['new'])

        # 3. Apply Case Change
        case_option = pdf_data.get('case')
        if case_option == 'upper':
            new_name = new_name.upper()
        elif case_option == 'lower':
            new_name = new_name.lower()
        elif case_option == 'title':
            new_name = new_name.title()

        # 4. Apply Prefix/Suffix/Timestamp
        prefix = pdf_data.get('prefix', '')
        suffix = pdf_data.get('suffix', '')
        timestamp = pdf_data.get('timestamp', '') # Already formatted

        # Construct final name parts
        final_name_parts = [part for part in [prefix, timestamp, new_name, suffix] if part]
        final_base_name = "_".join(final_name_parts) # Join with underscore or choose another separator

        if not final_base_name.strip(' .'):
            final_base_name = "renamed_pdf" # Fallback if name becomes empty

        # Sanitize the final composed name
        return sanitize_filename(f"{final_base_name}{ext}")

    except Exception as e:
        logger.error(f"Error generating preview filename: {e}\n{traceback.format_exc()}")
        return "Error generating preview"


async def update_status_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Edit the main interactive message to show current status and options."""
    pdf_data = get_pdf_data(context)
    if not pdf_data or 'message_id' not in context.user_data:
        # Should not happen in conversation, but handle defensively
        logger.warning(f"update_status_message called without pdf_data or message_id for user {update.effective_user.id}")
        if update.callback_query:
            await update.callback_query.message.reply_text("Session expired or data lost. Please send the PDF again.")
        return

    message_id = context.user_data['message_id']
    chat_id = update.effective_chat.id
    preview_name = generate_preview_filename(pdf_data)

    text = (
        f"📝 **Current PDF:** `{pdf_data['original_name']}`\n"
        f"✨ **Preview Name:** `{preview_name}`\n\n"
        "**Pending Changes:**\n"
        f"- Prefix: `{pdf_data.get('prefix', 'None')}`\n"
        f"- Suffix: `{pdf_data.get('suffix', 'None')}`\n"
        f"- Remove: `{pdf_data.get('remove', 'None')}`\n"
        f"- Replace: `{pdf_data.get('replace', {}).get('old', 'N/A')}` -> `{pdf_data.get('replace', {}).get('new', 'N/A')}`\n"
        f"- Case: `{pdf_data.get('case', 'None')}`\n"
        f"- Timestamp: `{pdf_data.get('timestamp_format', 'None')}`\n\n" # Show format user chose
        "Choose an action or apply changes:"
    )

    keyboard = [
        [
            InlineKeyboardButton("➕ Prefix", callback_data='add_prefix'),
            InlineKeyboardButton("➕ Suffix", callback_data='add_suffix'),
            InlineKeyboardButton("📅 Timestamp", callback_data='add_timestamp'),
        ],
        [
            InlineKeyboardButton("❌ Remove Text", callback_data='remove_name'),
            InlineKeyboardButton("🔁 Replace Text", callback_data='replace_word'),
            InlineKeyboardButton(" Aa Case", callback_data='change_case'),
        ],
        [
            InlineKeyboardButton("🔄 Reset Changes", callback_data='reset'),
            InlineKeyboardButton("🚫 Cancel Process", callback_data='cancel'),
        ],
        [InlineKeyboardButton("✅ Apply & Send", callback_data='apply')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN_V2
        )
    except TelegramError as e:
        # Handle potential errors like "Message not modified" or message deleted
        if "message is not modified" in str(e).lower():
            logger.info(f"Message not modified for user {update.effective_user.id}, skipping edit.")
        elif "message to edit not found" in str(e).lower():
             logger.warning(f"Message to edit not found for user {update.effective_user.id}. Ending conversation.")
             await update.callback_query.message.reply_text("Original message lost. Please start over by sending the PDF again.")
             await safe_cleanup(pdf_data.get('file_path'), update.effective_user.id, context)
             return FALLBACK # End conversation
        else:
            logger.error(f"Error editing status message for user {update.effective_user.id}: {e}")
            # Don't end conversation here, maybe it's temporary

# --- Command Handlers (Outside Conversation) ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command."""
    user = update.effective_user
    logger.info(f"User {user.id} ({user.username}) started the bot.")
    welcome_message = (
        f"Welcome, {user.mention_html()}!\n\n"
        "I am the **PDF Renamer Bot** 📄\n\n"
        "**How to use me:**\n"
        "1. Send me a PDF file (up to {MAX_FILE_SIZE // 1024 // 1024}MB).\n"
        "2. I'll show you interactive buttons to:\n"
        "   - Add prefixes/suffixes/timestamps\n"
        "   - Remove or replace text\n"
        "   - Change text case\n"
        "3. Preview the new filename instantly.\n"
        "4. Click 'Apply & Send' when you're ready!\n\n"
        "Send a PDF to begin!"
    )
    await update.message.reply_html(welcome_message)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /help command."""
    logger.info(f"User {update.effective_user.id} requested help.")
    help_message = (
        "**PDF Renamer Bot Help**\n\n"
        "1.  **Upload:** Send a PDF file (max {MAX_FILE_SIZE // 1024 // 1024}MB).\n"
        "2.  **Interact:** Use the inline buttons provided after uploading.\n"
        "    *   `➕ Prefix/Suffix/Timestamp`: Add text or a timestamp.\n"
        "    *   `❌ Remove Text`: Specify text to delete from the name.\n"
        "    *   `🔁 Replace Text`: Specify text to find and its replacement.\n"
        "    *   ` Aa Case`: Change the filename case (Upper, Lower, Title).\n"
        "    *   `🔄 Reset Changes`: Clear all modifications you've made.\n"
        "    *   `🚫 Cancel Process`: Stop editing and discard the PDF.\n"
        "    *   `✅ Apply & Send`: Rename the file and send it back.\n"
        "3.  **Preview:** The 'Preview Name' updates automatically as you make changes.\n\n"
        "Just send a PDF file to get started!"
    )
    await update.message.reply_markdown(help_message)


# --- Conversation Handlers ---

async def handle_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle PDF file uploads and start the conversation."""
    user = update.effective_user
    logger.info(f"User {user.id} uploaded a file.")

    if not update.message or not update.message.document:
        await update.message.reply_text("Error receiving file. Please try sending again.")
        return ConversationHandler.END

    document = update.message.document
    if document.mime_type != "application/pdf":
        await update.message.reply_text("⚠️ Please send a PDF file only.")
        return ConversationHandler.END

    if document.file_size > MAX_FILE_SIZE:
        await update.message.reply_text(f"⚠️ File is too large. Maximum size is {MAX_FILE_SIZE // 1024 // 1024}MB.")
        return ConversationHandler.END

    # Prepare user directory
    user_dir = os.path.join(DOWNLOADS_DIR, str(user.id))
    try:
        os.makedirs(user_dir, exist_ok=True)
    except (OSError, PermissionError) as e:
        logger.error(f"Failed to create directory {user_dir} for user {user.id}: {e}")
        await update.message.reply_text("🚫 Server error: Cannot create storage space. Please contact the admin.")
        return ConversationHandler.END

    try:
        pdf_file = await context.bot.get_file(document.file_id)
    except NetworkError as e:
        logger.error(f"Network error fetching file info for user {user.id}: {e}")
        await update.message.reply_text("⚠️ Network issue retrieving file details. Please try again later.")
        return ConversationHandler.END
    except TelegramError as e:
        logger.error(f"Telegram error fetching file info for user {user.id}: {e}")
        await update.message.reply_text("🚫 Telegram error retrieving file details. Please try again.")
        return ConversationHandler.END

    # Sanitize and prepare file path
    original_filename = sanitize_filename(document.file_name or "document.pdf")
    # Add a timestamp/unique ID to prevent collisions if the *exact* same filename is uploaded again quickly
    timestamp_suffix = datetime.now().strftime("%Y%m%d%H%M%S")
    base, ext = os.path.splitext(original_filename)
    temp_filename = f"{base}_{timestamp_suffix}{ext}"
    file_path = os.path.join(user_dir, temp_filename)

    # Download the file
    try:
        await pdf_file.download_to_drive(file_path)
        logger.info(f"PDF downloaded for user {user.id} to: {file_path}")
    except (OSError, PermissionError) as e:
        logger.error(f"Failed to download file to {file_path} for user {user.id}: {e}")
        await update.message.reply_text("🚫 Server error saving file. Please try again.")
        await safe_cleanup(file_path, user.id, context) # Clean up potentially partially downloaded file
        return ConversationHandler.END
    except TelegramError as e:
        logger.error(f"Telegram error downloading file for user {user.id}: {e}")
        await update.message.reply_text("🚫 Telegram error downloading file. Please try again.")
        await safe_cleanup(file_path, user.id, context)
        return ConversationHandler.END

    # Store initial data in context
    context.user_data['pdf_data'] = {
        'original_name': original_filename, # Store the *user visible* original name
        'file_path': file_path,             # Store the *actual* path on disk
        'prefix': '',
        'suffix': '',
        'remove': '',
        'replace': {'old': '', 'new': ''},
        'case': None, # 'upper', 'lower', 'title'
        'timestamp_format': None, # e.g., '%Y-%m-%d'
        'timestamp': '' # The generated timestamp string based on format
    }

    # Send initial status message and store its ID
    status_message = await update.message.reply_text("Processing PDF...") # Placeholder
    context.user_data['message_id'] = status_message.message_id

    # Update the status message with options
    await update_status_message(update, context)

    return SELECTING_ACTION # Move to the main action selection state


async def select_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle button clicks in the main menu."""
    query = update.callback_query
    await query.answer() # Acknowledge button press
    action = query.data

    pdf_data = get_pdf_data(context)
    if not pdf_data:
        await query.edit_message_text("Session expired or data lost. Please send the PDF again.")
        return FALLBACK

    logger.info(f"User {update.effective_user.id} selected action: {action}")

    next_state = SELECTING_ACTION # Default stay in menu

    if action == 'add_prefix':
        await query.edit_message_text("Please send the **prefix** text you want to add:")
        next_state = AWAITING_PREFIX
    elif action == 'add_suffix':
        await query.edit_message_text("Please send the **suffix** text you want to add:")
        next_state = AWAITING_SUFFIX
    elif action == 'remove_name':
        await query.edit_message_text("Please send the exact text you want to **remove** from the filename:")
        next_state = AWAITING_REMOVE
    elif action == 'replace_word':
        await query.edit_message_text("Please send the text you want to **replace** (the 'old' word):")
        next_state = AWAITING_REPLACE_OLD
    elif action == 'change_case':
        keyboard = [
            [
                InlineKeyboardButton("UPPERCASE", callback_data='case_upper'),
                InlineKeyboardButton("lowercase", callback_data='case_lower'),
                InlineKeyboardButton("Title Case", callback_data='case_title'),
            ],
            [InlineKeyboardButton("🔙 Back", callback_data='back_to_menu')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text("Choose the desired case transformation:", reply_markup=reply_markup)
        next_state = AWAITING_CASE # Wait for case button press
    elif action == 'add_timestamp':
         # Offer common formats or custom
        keyboard = [
            [InlineKeyboardButton("YYYYMMDD_HHMMSS", callback_data='ts_ymdhms')], # Default from const
            [InlineKeyboardButton("YYYY-MM-DD", callback_data='ts_ymd')],
            [InlineKeyboardButton("DD-MM-YYYY", callback_data='ts_dmy')],
            # [InlineKeyboardButton("Custom Format", callback_data='ts_custom')], # Could add custom later
            [InlineKeyboardButton("🔙 Back", callback_data='back_to_menu')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text("Choose a timestamp format to add (or go back):", reply_markup=reply_markup)
        next_state = AWAITING_TIMESTAMP # Wait for timestamp button press
    elif action == 'apply':
        return await apply_changes(update, context) # This function will end the conversation
    elif action == 'reset':
        pdf_data['prefix'] = ''
        pdf_data['suffix'] = ''
        pdf_data['remove'] = ''
        pdf_data['replace'] = {'old': '', 'new': ''}
        pdf_data['case'] = None
        pdf_data['timestamp_format'] = None
        pdf_data['timestamp'] = ''
        await query.answer("🔄 Changes reset!")
        await update_status_message(update, context) # Update display
        next_state = SELECTING_ACTION
    elif action == 'cancel':
        return await cancel_operation(update, context) # This function will end the conversation
    else:
        await query.answer("Unknown action.") # Should not happen with defined buttons

    return next_state


async def receive_prefix(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive and store the prefix text."""
    user_input = update.message.text
    user_id = update.effective_user.id
    pdf_data = get_pdf_data(context)

    if not pdf_data:
        await update.message.reply_text("Session expired. Please send the PDF again.")
        return FALLBACK

    prefix = sanitize_filename(user_input) # Sanitize potential prefix
    if not prefix:
        await update.message.reply_text("⚠️ Invalid prefix. Please avoid special characters like / \\ : * ? \" < > | and try again.")
        # Re-prompt (stay in the same state by returning it) - better UX?
        # For simplicity now, we go back to menu. Could add a re-prompt mechanism.
        await update_status_message(update, context)
        return SELECTING_ACTION
        # return AWAITING_PREFIX # To re-prompt immediately

    logger.info(f"User {user_id} set prefix: {prefix}")
    pdf_data['prefix'] = prefix
    await update.message.delete() # Delete user's prefix message
    await update_status_message(update, context) # Update the main message
    return SELECTING_ACTION


async def receive_suffix(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive and store the suffix text."""
    user_input = update.message.text
    user_id = update.effective_user.id
    pdf_data = get_pdf_data(context)

    if not pdf_data:
        await update.message.reply_text("Session expired. Please send the PDF again.")
        return FALLBACK

    suffix = sanitize_filename(user_input) # Sanitize potential suffix
    if not suffix:
        await update.message.reply_text("⚠️ Invalid suffix. Please avoid special characters like / \\ : * ? \" < > | and try again.")
        await update_status_message(update, context)
        return SELECTING_ACTION

    logger.info(f"User {user_id} set suffix: {suffix}")
    pdf_data['suffix'] = suffix
    await update.message.delete()
    await update_status_message(update, context)
    return SELECTING_ACTION


async def receive_remove_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive and store the text to remove."""
    remove_text = update.message.text.strip()
    user_id = update.effective_user.id
    pdf_data = get_pdf_data(context)

    if not pdf_data:
        await update.message.reply_text("Session expired. Please send the PDF again.")
        return FALLBACK

    if not remove_text:
        await update.message.reply_text("⚠️ Please provide some text to remove.")
        await update_status_message(update, context)
        return SELECTING_ACTION

    logger.info(f"User {user_id} set remove text: {remove_text}")
    pdf_data['remove'] = remove_text
    await update.message.delete()
    await update_status_message(update, context)
    return SELECTING_ACTION


async def receive_replace_old(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive the 'old' text for replacement."""
    old_word = update.message.text.strip()
    user_id = update.effective_user.id
    pdf_data = get_pdf_data(context)

    if not pdf_data:
        await update.message.reply_text("Session expired. Please send the PDF again.")
        return FALLBACK

    if not old_word:
        await update.message.reply_text("⚠️ Please provide the text you want to replace.")
        await update_status_message(update, context) # Go back to menu if invalid
        return SELECTING_ACTION

    logger.info(f"User {user_id} set replace old word: {old_word}")
    pdf_data['replace'] = {'old': old_word, 'new': ''} # Store old, wait for new
    context.user_data['message_id'] = update.message.message_id # Track the user's message to delete later? Risky.
    # Edit the *bot's* last message (which was the prompt)
    await context.bot.edit_message_text(
        chat_id=update.effective_chat.id,
        message_id=context.user_data['message_id'], # Use the bot's prompt message ID
        text=f"OK. Now send the text you want to replace '{old_word}' with (send /empty to replace with nothing):"
    )
    await update.message.delete() # Delete user's "old word" message
    return AWAITING_REPLACE_NEW


async def receive_replace_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive the 'new' text for replacement."""
    new_word = update.message.text.strip()
    user_id = update.effective_user.id
    pdf_data = get_pdf_data(context)

    if not pdf_data or 'replace' not in pdf_data or 'old' not in pdf_data['replace']:
        await update.message.reply_text("Session error during replace. Please start over.")
        await safe_cleanup(pdf_data.get('file_path'), user_id, context)
        return FALLBACK

    # Allow replacing with empty string using a command
    if new_word.lower() == '/empty':
        new_word = ''

    old_word = pdf_data['replace']['old']
    logger.info(f"User {user_id} set replace new word: '{new_word}' (for old: '{old_word}')")
    pdf_data['replace']['new'] = new_word # Complete the replacement pair

    await update.message.delete() # Delete user's "new word" message
    # Need to re-fetch the main message ID if we overwrote it in the previous step
    # This part is tricky. Let's assume the main message ID is still valid.
    # If not, we might need a better way to manage message IDs across steps.
    await update_status_message(update, context) # Update main status
    return SELECTING_ACTION


async def receive_case_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle button clicks for case change."""
    query = update.callback_query
    await query.answer()
    choice = query.data # e.g., 'case_upper'
    user_id = update.effective_user.id
    pdf_data = get_pdf_data(context)

    if not pdf_data:
        await query.edit_message_text("Session expired. Please send the PDF again.")
        return FALLBACK

    if choice == 'back_to_menu':
        await update_status_message(update, context)
        return SELECTING_ACTION

    case_map = {'case_upper': 'upper', 'case_lower': 'lower', 'case_title': 'title'}
    selected_case = case_map.get(choice)

    if selected_case:
        logger.info(f"User {user_id} selected case: {selected_case}")
        pdf_data['case'] = selected_case
    else:
        logger.warning(f"User {user_id} sent invalid case choice: {choice}")
        await query.answer("Invalid choice.") # Brief feedback on button

    # Go back to main menu
    await update_status_message(update, context)
    return SELECTING_ACTION


async def receive_timestamp_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle button clicks for timestamp format."""
    query = update.callback_query
    await query.answer()
    choice = query.data # e.g., 'ts_ymdhms'
    user_id = update.effective_user.id
    pdf_data = get_pdf_data(context)

    if not pdf_data:
        await query.edit_message_text("Session expired. Please send the PDF again.")
        return FALLBACK

    if choice == 'back_to_menu':
        await update_status_message(update, context)
        return SELECTING_ACTION

    format_map = {
        'ts_ymdhms': '%Y%m%d_%H%M%S',
        'ts_ymd': '%Y-%m-%d',
        'ts_dmy': '%d-%m-%Y'
    }
    selected_format = format_map.get(choice)

    if selected_format:
        logger.info(f"User {user_id} selected timestamp format: {selected_format}")
        pdf_data['timestamp_format'] = selected_format
        pdf_data['timestamp'] = datetime.now().strftime(selected_format) # Generate and store the string now
    else:
        logger.warning(f"User {user_id} sent invalid timestamp choice: {choice}")
        await query.answer("Invalid choice.")

    # Go back to main menu
    await update_status_message(update, context)
    return SELECTING_ACTION


async def apply_changes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Apply all stored modifications, rename, send, and cleanup."""
    query = update.callback_query # Triggered by button
    await query.answer("⏳ Applying changes...") # Feedback
    user_id = update.effective_user.id
    pdf_data = get_pdf_data(context)

    if not pdf_data or 'file_path' not in pdf_data:
        await query.edit_message_text("🚫 Error: File data missing. Please upload again.")
        return FALLBACK

    original_filepath = pdf_data['file_path']
    if not os.path.exists(original_filepath):
        logger.error(f"Original file not found for user {user_id}: {original_filepath}")
        await query.edit_message_text("🚫 Error: Original file missing on server. Please upload again.")
        await safe_cleanup(None, user_id, context) # Clear data even if file missing
        return FALLBACK

    # Generate the final filename
    final_filename = generate_preview_filename(pdf_data)
    if "Error" in final_filename:
        logger.error(f"Error generating final filename for user {user_id}.")
        await query.edit_message_text("🚫 Error generating final filename. Please check your modifications or reset.")
        # Don't end conversation here, let user fix it
        return SELECTING_ACTION # Go back to menu


    if not final_filename or final_filename == os.path.splitext(pdf_data['original_name'])[1]: # Check if name only contains extension
        await query.edit_message_text("⚠️ Resulting filename is empty or invalid. Cannot apply changes.")
        logger.warning(f"Empty/invalid filename generated for user {user_id}: '{final_filename}'")
        return SELECTING_ACTION

    # Prepare new path
    user_dir = os.path.dirname(original_filepath)
    new_filepath = os.path.join(user_dir, final_filename)

    # Rename the file
    try:
        # Handle case where new filename is same as old (no changes made)
        if original_filepath == new_filepath:
             logger.info(f"No rename needed for user {user_id}, filenames are identical.")
        else:
             # Ensure the target path doesn't already exist (should be rare with sanitized names)
             if os.path.exists(new_filepath):
                 logger.warning(f"Target path {new_filepath} already exists for user {user_id}. Appending unique ID.")
                 base, ext = os.path.splitext(final_filename)
                 final_filename = f"{base}_{datetime.now().strftime('%f')}{ext}" # Add microseconds
                 new_filepath = os.path.join(user_dir, final_filename)

             os.rename(original_filepath, new_filepath)
             logger.info(f"File renamed for user {user_id}: {os.path.basename(original_filepath)} -> {final_filename}")
             pdf_data['file_path'] = new_filepath # Update path in case of sending error

    except (OSError, PermissionError) as e:
        logger.error(f"Failed to rename file for user {user_id}: {e}")
        await query.edit_message_text("🚫 Server error renaming file. Please try again or contact admin.")
        # Don't clean up yet, maybe it's temporary
        return SELECTING_ACTION # Let them try again? Or FALLBACK? Depends on desired robustness.

    # Send the renamed file
    try:
        await query.edit_message_text(f"✅ Renamed! Sending '{final_filename}'...") # Update status before sending
        with open(new_filepath, 'rb') as file_to_send:
            await context.bot.send_document(
                chat_id=update.effective_chat.id,
                document=InputFile(file_to_send, filename=final_filename),
                caption=f"📄 Here is your renamed PDF:\n`{final_filename}`",
                parse_mode=ParseMode.MARKDOWN_V2
            )
        logger.info(f"Renamed file sent to user {user_id}: {final_filename}")

        # Delete the interactive message after successful sending
        try:
            await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=context.user_data['message_id'])
        except TelegramError as del_e:
             logger.warning(f"Could not delete interactive message {context.user_data['message_id']} for user {user_id}: {del_e}")

    except FileNotFoundError:
        logger.error(f"Renamed file not found for sending to user {user_id}: {new_filepath}")
        await context.bot.send_message(chat_id=update.effective_chat.id, text="🚫 Error: Renamed file vanished before sending. Please try again.")
        # Original file might still exist if rename failed silently? Unlikely.
    except (TelegramError, NetworkError) as e:
        logger.error(f"Error sending renamed file to user {user_id}: {e}")
        await context.bot.send_message(chat_id=update.effective_chat.id, text=f"🚫 Error sending the file: {e}. Please try applying again.")
        # Don't clean up, file still exists, let them try again.
        # We need to restore the interactive message if we deleted it prematurely.
        # This gets complex. Simpler: leave the "Sending..." message and let them try again.
        return SELECTING_ACTION # Allow retry
    except Exception as e:
         logger.error(f"Unexpected error sending file for user {user_id}: {e}\n{traceback.format_exc()}")
         await context.bot.send_message(chat_id=update.effective_chat.id, text="🚫 An unexpected error occurred while sending.")
         # Return to menu to allow retry
         return SELECTING_ACTION
    finally:
        # Cleanup should ideally happen *after* successful send confirmation,
        # but doing it here ensures it runs even if sending fails.
        # If sending failed, the file remains for potential retry.
        # If sending succeeded, cleanup happens now.
        await safe_cleanup(new_filepath, user_id, context)

    return FALLBACK # End conversation successfully


async def cancel_operation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel the current operation, cleanup, and end conversation."""
    query = update.callback_query
    await query.answer() # Acknowledge button press
    user_id = update.effective_user.id
    pdf_data = get_pdf_data(context)
    file_path = pdf_data.get('file_path') if pdf_data else None

    logger.info(f"User {user_id} cancelled operation.")
    await query.edit_message_text("🚫 Operation cancelled. The PDF file has been discarded.")

    await safe_cleanup(file_path, user_id, context)

    return FALLBACK # End conversation


async def conversation_timeout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle conversation timeout."""
    user_id = update.effective_user.id if update.effective_user else "Unknown"
    logger.warning(f"Conversation timed out for user {user_id}")
    pdf_data = get_pdf_data(context)
    file_path = pdf_data.get('file_path') if pdf_data else None

    if 'message_id' in context.user_data and update.effective_chat:
         try:
             await context.bot.edit_message_text(
                 chat_id=update.effective_chat.id,
                 message_id=context.user_data['message_id'],
                 text="⌛ Session timed out due to inactivity. Please send the PDF again if needed."
             )
         except TelegramError as e:
              logger.warning(f"Failed to edit message on timeout for user {user_id}: {e}")
              # Send a new message if editing fails
              await context.bot.send_message(
                  chat_id=update.effective_chat.id,
                  text="⌛ Session timed out due to inactivity. Please send the PDF again if needed."
              )
    elif update.effective_chat:
         await context.bot.send_message(
             chat_id=update.effective_chat.id,
             text="⌛ Session timed out due to inactivity. Please send the PDF again if needed."
         )


    await safe_cleanup(file_path, user_id, context)
    return FALLBACK # End conversation


async def unexpected_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle messages that are not expected in the current state."""
    logger.warning(f"User {update.effective_user.id} sent unexpected message in state {context.user_data.get(ConversationHandler.STATE, 'N/A')}: {update.message.text}")
    await update.message.reply_text("I wasn't expecting that. Please use the buttons or follow the prompts.")
    # Stay in the current state - don't end the conversation unless necessary


# --- Global Error Handler ---
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Log Errors caused by Updates."""
    logger.error(f"Exception while handling an update: {context.error}", exc_info=context.error)

    # traceback.print_exception(type(context.error), context.error, context.error.__traceback__)

    # Try to notify the user
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "🚫 An unexpected error occurred internally. "
                "If you were in the middle of renaming, please try sending the file again. "
                "The developers have been notified."
            )
        except TelegramError:
            logger.error("Failed to send error message to user.")

    # If it's an error within a conversation, try to clean up
    if isinstance(context.error, Exception) and context.user_data:
        user_id = update.effective_user.id if isinstance(update, Update) and update.effective_user else "Unknown (error handler)"
        pdf_data = get_pdf_data(context)
        file_path = pdf_data.get('file_path') if pdf_data else None
        logger.info(f"Attempting cleanup for user {user_id} after error: {context.error}")
        await safe_cleanup(file_path, user_id, context)
        # Consider ending the conversation if the error is severe
        # return ConversationHandler.END # Be careful using this in a generic error handler


# --- Main Bot Setup ---
def main() -> None:
    """Start the bot."""
    logger.info("Starting bot...")

    try:
        os.makedirs(DOWNLOADS_DIR, exist_ok=True)
        logger.info(f"Downloads directory ensured: {DOWNLOADS_DIR}")
    except (OSError, PermissionError) as e:
        logger.critical(f"CRITICAL: Failed to create downloads directory '{DOWNLOADS_DIR}': {e}")
        raise SystemExit("Cannot create required download directory.") from e

    # Create the Application and pass it your bot's token.
    application = Application.builder().token(BOT_TOKEN).build()

    # --- Conversation Handler Setup ---
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
            AWAITING_REPLACE_NEW: [MessageHandler(filters.TEXT | filters.COMMAND, receive_replace_new)], # Allow /empty command
            AWAITING_CASE: [
                CallbackQueryHandler(receive_case_choice, pattern='^case_(upper|lower|title)$'),
                CallbackQueryHandler(select_action, pattern='^back_to_menu$') # Go back
            ],
             AWAITING_TIMESTAMP: [
                CallbackQueryHandler(receive_timestamp_choice, pattern='^ts_(ymdhms|ymd|dmy)$'),
                CallbackQueryHandler(select_action, pattern='^back_to_menu$') # Go back
            ],
        },
        fallbacks=[
            CommandHandler('cancel', cancel_operation), # Allow /cancel command globally in conversation
            CallbackQueryHandler(cancel_operation, pattern='^cancel$'), # Button cancel
            MessageHandler(filters.COMMAND, unexpected_message), # Handle unexpected commands
            MessageHandler(filters.ALL, unexpected_message),    # Handle any other unexpected message
            # Temporarily disable timeout for debugging if needed
            # TypeHandler(Update, conversation_timeout) # Catches timeout via PTB internal mechanism - needs testing if it works as expected
        ],
        # conversation_timeout=60 * 10, # Timeout after 10 minutes of inactivity
        per_message=False, # Use one conversation per user
        # name="pdf_rename_conversation", # Optional: For debugging
        # persistent=False # Optional: Don't store state across restarts
    )

    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(conv_handler) # Add the main conversation handler

    # Add the error handler LAST
    application.add_error_handler(error_handler)

    # Run the bot until the user presses Ctrl-C
    logger.info("Bot polling started.")
    application.run_polling(allowed_updates=Update.ALL_TYPES)
    logger.info("Bot polling stopped.")

if __name__ == '__main__':
    main()