from telegram import Update, InputFile, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
    filters
)
from telegram.error import TelegramError, NetworkError, BadRequest
import os
import logging
import traceback
import re
import shutil
from PyPDF2 import PdfReader, PdfWriter  # Added for PDF compression

# Configure logging with more detailed format
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Constants
DOWNLOADS_DIR = "downloads"
MAX_FILE_SIZE = 200 * 1024 * 1024  # 200 MB limit
COMPRESSED_DIR = os.path.join(DOWNLOADS_DIR, "compressed")

def sanitize_filename(filename: str) -> str:
    """Sanitize filename to prevent invalid characters or path traversal."""
    filename = re.sub(r'[<>:"/\\|?*\x00-\x1F]', '', filename)
    filename = filename.replace('..', '').strip()
    return filename or "unnamed.pdf"

async def safe_cleanup(file_path: str, user_id: int):
    """Safely remove a file and log any errors."""
    try:
        if os.path.exists(file_path):
            os.remove(file_path)
            logger.info(f"Cleaned up file for user {user_id}: {file_path}")
    except (OSError, PermissionError) as e:
        logger.error(f"Failed to clean up file {file_path} for user {user_id}: {str(e)}")

def compress_pdf(input_path: str, output_path: str, user_id: int) -> bool:
    """Compress PDF file using PyPDF2."""
    try:
        reader = PdfReader(input_path)
        writer = PdfWriter()

        for page in reader.pages:
            writer.add_page(page)

        # Enable compression
        writer.compress = True

        with open(output_path, 'wb') as output_file:
            writer.write(output_file)

        logger.info(f"Compressed PDF for user {user_id}: {output_path}")
        return True
    except Exception as e:
        logger.error(f"Failed to compress PDF for user {user_id}: {str(e)}")
        return False

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command with welcome message and instructions."""
    try:
        user_id = update.effective_user.id
        logger.info(f"User {user_id} used /start")
        welcome_message = (
            "Welcome to the PDF Renamer Bot! 📄\n\n"
            "Here's how to use me:\n"
            "1. Send a PDF file (max 200MB)\n"
            "2. Use commands to modify the filename:\n"
            "   - /add_prefix YourText - Add text at the start\n"
            "   - /add_suffix YourText - Add text at the end\n"
            "   - /remove_name Word - Remove specific word\n"
            "   - /replace_word OldWord NewWord - Replace word in filename\n"
            "   - /to - Apply changes and get renamed PDF\n"
            "3. Or use inline buttons after uploading a PDF\n\n"
            "Try sending a PDF to start!"
        )
        await update.message.reply_text(welcome_message)
    except TelegramError as e:
        logger.error(f"Telegram error in start for user {user_id}: {str(e)}")
        await update.message.reply_text("Failed to send welcome message. Please try again later.")

async def handle_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle PDF file uploads."""
    try:
        user_id = update.effective_user.id
        logger.info(f"User {user_id} uploaded a file")

        if not update.message.document:
            await update.message.reply_text("No file received. Please send a PDF file.")
            return

        if update.message.document.mime_type != "application/pdf":
            await update.message.reply_text("Please send a PDF file only.")
            return

        if update.message.document.file_size > MAX_FILE_SIZE:
            await update.message.reply_text("File is too large. Maximum size is 200MB.")
            return

        user_dir = os.path.join(DOWNLOADS_DIR, str(user_id))
        try:
            os.makedirs(user_dir, exist_ok=True)
            os.makedirs(COMPRESSED_DIR, exist_ok=True)
        except (OSError, PermissionError) as e:
            logger.error(f"Failed to create directory {user_dir} for user {user_id}: {str(e)}")
            await update.message.reply_text("Server error creating storage directory. Please try again.")
            return

        try:
            file = await context.bot.get_file(update.message.document.file_id)
        except NetworkError as e:
            logger.error(f"Network error fetching file for user {user_id}: {str(e)}")
            await update.message.reply_text("Network issue downloading file. Please try again later.")
            return

        file_name = sanitize_filename(update.message.document.file_name or "document.pdf")
        file_path = os.path.join(user_dir, file_name)

        if os.path.exists(file_path):
            base, ext = os.path.splitext(file_name)
            file_name = f"{base}_{user_id}{ext}"
            file_path = os.path.join(user_dir, file_name)

        try:
            await file.download_to_drive(file_path)
        except (OSError, PermissionError) as e:
            logger.error(f"Failed to download file to {file_path} for user {user_id}: {str(e)}")
            await update.message.reply_text("Server error saving file. Please try again.")
            return

        try:
            context.user_data['pdf'] = {
                'original_name': file_name,
                'current_name': file_name,
                'file_path': file_path,
                'prefix': '',
                'suffix': '',
                'remove': '',
                'replace': {'old': '', 'new': ''},
                'compress': True  # Enable compression by default
            }
        except Exception as e:
            logger.error(f"Error storing metadata for user {user_id}: {str(e)}")
            await safe_cleanup(file_path, user_id)
            await update.message.reply_text("Error storing file metadata. Please try again.")
            return

        logger.info(f"PDF saved for user {user_id}: {file_name}")

        try:
            keyboard = [
                [
                    InlineKeyboardButton("Add Prefix", callback_data='add_prefix'),
                    InlineKeyboardButton("Add Suffix", callback_data='add_suffix')
                ],
                [
                    InlineKeyboardButton("Remove Name Part", callback_data='remove_name'),
                    InlineKeyboardButton("Replace Word", callback_data='replace_word')
                ],
                [
                    InlineKeyboardButton("Apply Changes", callback_data='apply')
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text(
                f"PDF received: {file_name}\nChoose an action:",
                reply_markup=reply_markup
            )
        except TelegramError as e:
            logger.error(f"Error sending reply for user {user_id}: {str(e)}")
            await safe_cleanup(file_path, user_id)
            await update.message.reply_text("Error sending response. Please try again.")

    except TelegramError as e:
        logger.error(f"Telegram error handling PDF for user {user_id}: {str(e)}")
        await update.message.reply_text("Telegram API error. Please try again later.")
    except Exception as e:
        logger.error(f"Unexpected error handling PDF for user {user_id}: {str(e)}\n{traceback.format_exc()}")
        await update.message.reply_text("An unexpected error occurred. Please try again.")

async def add_prefix(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /add_prefix command."""
    try:
        user_id = update.effective_user.id
        logger.info(f"User {user_id} used /add_prefix")

        if 'pdf' not in context.user_data or not context.user_data.get('pdf'):
            await update.message.reply_text("Please upload a PDF first.")
            return

        if not context.args:
            await update.message.reply_text("Please provide a prefix. Example: /add_prefix MyPrefix")
            return

        prefix = sanitize_filename(' '.join(context.args))
        if not prefix:
            await update.message.reply_text("Invalid prefix. Please use valid characters.")
            return

        try:
            context.user_data['pdf']['prefix'] = prefix
        except KeyError as e:
            logger.error(f"KeyError in add_prefix for user {user_id}: {str(e)}")
            await update.message.reply_text("Error updating prefix. Please try again.")
            return

        await update.message.reply_text(f"Prefix '{prefix}' added. Use /to to apply changes.")

    except TelegramError as e:
        logger.error(f"Telegram error in add_prefix for user {user_id}: {str(e)}")
        await update.message.reply_text("Telegram API error. Please try again later.")
    except Exception as e:
        logger.error(f"Unexpected error in add_prefix for user {user_id}: {str(e)}\n{traceback.format_exc()}")
        await update.message.reply_text("An unexpected error occurred. Please try again.")

async def add_suffix(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /add_suffix command."""
    try:
        user_id = update.effective_user.id
        logger.info(f"User {user_id} used /add_suffix")

        if 'pdf' not in context.user_data or not context.user_data.get('pdf'):
            await update.message.reply_text("Please upload a PDF first.")
            return

        if not context.args:
            await update.message.reply_text("Please provide a suffix. Example: /add_suffix MySuffix")
            return

        suffix = sanitize_filename(' '.join(context.args))
        if not suffix:
            await update.message.reply_text("Invalid suffix. Please use valid characters.")
            return

        try:
            context.user_data['pdf']['suffix'] = suffix
        except KeyError as e:
            logger.error(f"KeyError in add_suffix for user {user_id}: {str(e)}")
            await update.message.reply_text("Error updating suffix. Please try again.")
            return

        await update.message.reply_text(f"Suffix '{suffix}' added. Use /to to apply changes.")

    except TelegramError as e:
        logger.error(f"Telegram error in add_suffix for user {user_id}: {str(e)}")
        await update.message.reply_text("Telegram API error. Please try again later.")
    except Exception as e:
        logger.error(f"Unexpected error in add_suffix for user {user_id}: {str(e)}\n{traceback.format_exc()}")
        await update.message.reply_text("An unexpected error occurred. Please try again.")

async def remove_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /remove_name command."""
    try:
        user_id = update.effective_user.id
        logger.info(f"User {user_id} used /remove_name")

        if 'pdf' not in context.user_data or not context.user_data.get('pdf'):
            await update.message.reply_text("Please upload a PDF first.")
            return

        if not context.args:
            await update.message.reply_text("Please provide text to remove. Example: /remove_name Word")
            return

        remove_text = ' '.join(context.args)
        if not remove_text.strip():
            await update.message.reply_text("Invalid text to remove. Please provide valid text.")
            return

        try:
            context.user_data['pdf']['remove'] = remove_text
        except KeyError as e:
            logger.error(f"KeyError in remove_name for user {user_id}: {str(e)}")
            await update.message.reply_text("Error updating remove text. Please try again.")
            return

        await update.message.reply_text(f"Text '{remove_text}' will be removed. Use /to to apply changes.")

    except TelegramError as e:
        logger.error(f"Telegram error in remove_name for user {user_id}: {str(e)}")
        await update.message.reply_text("Telegram API error. Please try again later.")
    except Exception as e:
        logger.error(f"Unexpected error in remove_name for user {user_id}: {str(e)}\n{traceback.format_exc()}")
        await update.message.reply_text("An unexpected error occurred. Please try again.")

async def replace_word(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /replace_word command."""
    try:
        user_id = update.effective_user.id
        logger.info(f"User {user_id} used /replace_word")

        if 'pdf' not in context.user_data or not context.user_data.get('pdf'):
            await update.message.reply_text("Please upload a PDF first.")
            return

        if len(context.args) < 2:
            await update.message.reply_text("Please provide old and new words. Example: /replace_word OldWord NewWord")
            return

        old_word = ' '.join(context.args[:-1])
        new_word = context.args[-1]
        
        if not old_word.strip() or not new_word.strip():
            await update.message.reply_text("Invalid words. Please provide valid text.")
            return

        try:
            context.user_data['pdf']['replace'] = {'old': old_word, 'new': new_word}
        except KeyError as e:
            logger.error(f"KeyError in replace_word for user {user_id}: {str(e)}")
            await update.message.reply_text("Error updating replace words. Please try again.")
            return

        await update.message.reply_text(f"Will replace '{old_word}' with '{new_word}'. Use /to to apply changes.")

    except TelegramError as e:
        logger.error(f"Telegram error in replace_word for user {user_id}: {str(e)}")
        await update.message.reply_text("Telegram API error. Please try again later.")
    except Exception as e:
        logger.error(f"Unexpected error in replace_word for user {user_id}: {str(e)}\n{traceback.format_exc()}")
        await update.message.reply_text("An unexpected error occurred. Please try again.")

async def apply_changes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /to command to apply renaming, compression, and send file."""
    try:
        user_id = update.effective_user.id
        logger.info(f"User {user_id} used /to")

        if 'pdf' not in context.user_data or not context.user_data.get('pdf'):
            await update.message.reply_text("Please upload a PDF first.")
            return

        pdf_data = context.user_data['pdf']
        try:
            original_name = pdf_data['original_name']
            file_path = pdf_data['file_path']
            prefix = pdf_data['prefix']
            suffix = pdf_data['suffix']
            remove_text = pdf_data['remove']
            replace_data = pdf_data['replace']
            compress = pdf_data['compress']
        except KeyError as e:
            logger.error(f"Missing metadata key for user {user_id}: {str(e)}")
            await update.message.reply_text("Error accessing file metadata. Please upload the PDF again.")
            return

        if not os.path.exists(file_path):
            logger.error(f"File not found for user {user_id}: {file_path}")
            context.user_data.clear()
            await update.message.reply_text("Original file not found. Please upload the PDF again.")
            return

        try:
            name, ext = os.path.splitext(original_name)
            new_name = name

            # Apply remove
            if remove_text:
                new_name = new_name.replace(remove_text, '')

            # Apply replace
            if replace_data['old'] and replace_data['new']:
                new_name = new_name.replace(replace_data['old'], replace_data['new'])

            # Apply prefix and suffix
            new_name = f"{prefix}{new_name}{suffix}"
            if not new_name.strip():
                await update.message.reply_text("Resulting filename is empty. Please modify the changes.")
                return

            new_filename = sanitize_filename(f"{new_name}{ext}")
            new_filepath = os.path.join(os.path.dirname(file_path), new_filename)

            if os.path.exists(new_filepath):
                base, ext = os.path.splitext(new_filename)
                new_filename = f"{base}_{user_id}{ext}"
                new_filepath = os.path.join(os.path.dirname(file_path), new_filename)
        except Exception as e:
            logger.error(f"Error creating new filename for user {user_id}: {str(e)}")
            await update.message.reply_text("Error generating new filename. Please try again.")
            return

        # Handle compression
        final_filepath = new_filepath
        if compress:
            compressed_filename = f"compressed_{new_filename}"
            compressed_filepath = os.path.join(COMPRESSED_DIR, compressed_filename)
            
            if compress_pdf(file_path, compressed_filepath, user_id):
                final_filepath = compressed_filepath
                new_filename = compressed_filename
            else:
                logger.warning(f"Compression failed for user {user_id}, using original file")
                # Continue with original file if compression fails

        # Rename file if not compressed
        if final_filepath == new_filepath:
            try:
                os.rename(file_path, new_filepath)
                logger.info(f"File renamed for user {user_id}: {original_name} -> {new_filename}")
            except (OSError, PermissionError) as e:
                logger.error(f"Failed to rename file for user {user_id}: {str(e)}")
                await update.message.reply_text("Server error renaming file. Please try again.")
                return

        # Send file
        try:
            with open(final_filepath, 'rb') as file:
                await update.message.reply_document(
                    document=InputFile(file, filename=new_filename),
                    caption=f"Renamed {'and compressed ' if compress else ''}PDF: {new_filename}"
                )
        except (FileNotFoundError, PermissionError) as e:
            logger.error(f"Failed to read file for user {user_id}: {str(e)}")
            await safe_cleanup(final_filepath, user_id)
            await update.message.reply_text("Error reading renamed file. Please try again.")
            return
        except TelegramError as e:
            logger.error(f"Telegram error sending file for user {user_id}: {str(e)}")
            await safe_cleanup(final_filepath, user_id)
            await update.message.reply_text("Error sending renamed file. Please try again.")
            return

        # Cleanup
        try:
            await safe_cleanup(file_path, user_id)
            await safe_cleanup(final_filepath, user_id)
            context.user_data.clear()
            logger.info(f"Cleanup completed for user {user_id}")
        except Exception as e:
            logger.error(f"Error during cleanup for user {user_id}: {str(e)}")
            await update.message.reply_text("File sent, but cleanup failed. Please upload a new PDF.")

    except TelegramError as e:
        logger.error(f"Telegram error in apply_changes for user {user_id}: {str(e)}")
        await update.message.reply_text("Telegram API error. Please try again later.")
    except Exception as e:
        logger.error(f"Unexpected error inBLACK apply_changes for user {user_id}: {str(e)}\n{traceback.format_exc()}")
        await update.message.reply_text("An unexpected error occurred. Please try again.")

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline button callbacks."""
    query = update.callback_query
    try:
        await query.answer()
    except TelegramError as e:
        logger.error(f"Error answering callback query for user {update.effective_user.id}: {str(e)}")
        return

    user_id = update.effective_user.id
    logger.info(f"User {user_id} clicked button: {query.data}")

    try:
        if query.data == 'add_prefix':
            await query.message.reply_text("Please enter prefix using /add_prefix YourText")
        elif query.data == 'add_suffix':
            await query.message.reply_text("Please enter suffix using /add_suffix YourText")
        elif query.data == 'remove_name':
            await query.message.reply_text("Please enter text to remove using /remove_name Word")
        elif query.data == 'replace_word':
            await query.message.reply_text("Please enter words to replace using /replace_word OldWord NewWord")
        elif query.data == 'apply':
            await apply_changes(update, context)
        else:
            await query.message.reply_text("Unknown action. Please use the provided buttons.")
    except TelegramError as e:
        logger.error(f"Telegram error in button_callback for user {user_id}: {str(e)}")
        await query.message.reply_text("Telegram API error. Please try again later.")
    except Exception as e:
        logger.error(f"Unexpected error in button_callback for user {user_id}: {str(e)}\n{traceback.format_exc()}")
        await query.message.reply_text("An unexpected error occurred. Please try again.")

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle errors globally."""
    try:
        logger.error(f"Update {update} caused error {context.error}\n{traceback.format_exc()}")
        if update and update.effective_message:
            await update.effective_message.reply_text("An unexpected error occurred. Please try again later.")
    except Exception as e:
        logger.error(f"Error in error_handler: {str(e)}\n{traceback.format_exc()}")

def main():
    """Start the bot."""
    try:
        bot_token = os.getenv('BOT_TOKEN')
        if not bot_token:
            logger.error("BOT_TOKEN environment variable not set")
            raise ValueError("BOT_TOKEN environment variable not set")

        try:
            os.makedirs(DOWNLOADS_DIR, exist_ok=True)
            os.makedirs(COMPRESSED_DIR, exist_ok=True)
        except (OSError, PermissionError) as e:
            logger.error(f"Failed to create downloads directory: {str(e)}")
            raise RuntimeError("Cannot create downloads directory")

        app = ApplicationBuilder().token(bot_token).build()

        # Register handlers
        app.add_handler(CommandHandler("start", start))
        app.add_handler(CommandHandler("add_prefix", add_prefix))
        app.add_handler(CommandHandler("add_suffix", add_suffix))
        app.add_handler(CommandHandler("remove_name", remove_name))
        app.add_handler(CommandHandler("replace_word", replace_word))
        app.add_handler(CommandHandler("to", apply_changes))
        app.add_handler(MessageHandler(filters.Document.PDF, handle_pdf))
        app.add_handler(CallbackQueryHandler(button_callback))
        app.add_error_handler(error_handler)

        logger.info("Bot started")
        app.run_polling()
    except TelegramError as e:
        logger.error(f"Telegram error starting bot: {str(e)}\n{traceback.format_exc()}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error starting bot: {str(e)}\n{traceback.format_exc()}")
        raise

if __name__ == '__main__':
    main()
