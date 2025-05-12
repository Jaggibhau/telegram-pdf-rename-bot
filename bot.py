from telegram import Update, InputFile, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
    filters
)
import os
import logging

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Constants
DOWNLOADS_DIR = "downloads"

# Ensure downloads directory exists
os.makedirs(DOWNLOADS_DIR, exist_ok=True)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command with welcome message and instructions."""
    logger.info(f"User {update.effective_user.id} used /start")
    welcome_message = (
        "Welcome to the PDF Renamer Bot! 📄\n\n"
        "Here's how to use me:\n"
        "1. Send a PDF file\n"
        "2. Use commands to modify the filename:\n"
        "   - /add_prefix YourText - Add text at the start\n"
        "   - /add_suffix YourText - Add text at the end\n"
        "   - /remove_name Word - Remove specific word\n"
        "   - /to - Apply changes and get renamed PDF\n"
        "3. Or use inline buttons after uploading a PDF\n\n"
        "Try sending a PDF to start!"
    )
    await update.message.reply_text(welcome_message)

async def handle_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle PDF file uploads."""
    try:
        user_id = update.effective_user.id
        logger.info(f"User {user_id} uploaded a PDF")

        # Check if file is PDF
        if not update.message.document.mime_type == "application/pdf":
            await update.message.reply_text("Please send a PDF file only.")
            return

        # Create user-specific directory
        user_dir = os.path.join(DOWNLOADS_DIR, str(user_id))
        os.makedirs(user_dir, exist_ok=True)

        # Get file
        file = await context.bot.get_file(update.message.document.file_id)
        file_name = update.message.document.file_name
        file_path = os.path.join(user_dir, file_name)

        # Download file
        await file.download_to_drive(file_path)

        # Store metadata
        context.user_data['pdf'] = {
            'original_name': file_name,
            'current_name': file_name,
            'file_path': file_path,
            'prefix': '',
            'suffix': '',
            'remove': ''
        }

        logger.info(f"PDF saved for user {user_id}: {file_name}")

        # Create inline keyboard
        keyboard = [
            [
                InlineKeyboardButton("Add Prefix", callback_data='add_prefix'),
                InlineKeyboardButton("Add Suffix", callback_data='add_suffix')
            ],
            [
                InlineKeyboardButton("Remove Name Part", callback_data='remove_name'),
                InlineKeyboardButton("Apply Changes", callback_data='apply')
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(
            f"PDF received: {file_name}\nChoose an action:",
            reply_markup=reply_markup
        )

    except Exception as e:
        logger.error(f"Error handling PDF for user {user_id}: {str(e)}")
        await update.message.reply_text("Sorry, an error occurred. Please try again.")

async def add_prefix(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /add_prefix command."""
    try:
        user_id = update.effective_user.id
        logger.info(f"User {user_id} used /add_prefix")

        if not context.user_data.get('pdf'):
            await update.message.reply_text("Please upload a PDF first.")
            return

        if not context.args:
            await update.message.reply_text("Please provide a prefix. Example: /add_prefix MyPrefix")
            return

        prefix = ' '.join(context.args)
        context.user_data['pdf']['prefix'] = prefix
        await update.message.reply_text(f"Prefix '{prefix}' added. Use /to to apply changes.")

    except Exception as e:
        logger.error(f"Error in add_prefix for user {user_id}: {str(e)}")
        await update.message.reply_text("Sorry, an error occurred. Please try again.")

async def add_suffix(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /add_suffix command."""
    try:
        user_id = update.effective_user.id
        logger.info(f"User {user_id} used /add_suffix")

        if not context.user_data.get('pdf'):
            await update.message.reply_text("Please upload a PDF first.")
            return

        if not context.args:
            await update.message.reply_text("Please provide a suffix. Example: /add_suffix MySuffix")
            return

        suffix = ' '.join(context.args)
        context.user_data['pdf']['suffix'] = suffix
        await update.message.reply_text(f"Suffix '{suffix}' added. Use /to to apply changes.")

    except Exception as e:
        logger.error(f"Error in add_suffix for user {user_id}: {str(e)}")
        await update.message.reply_text("Sorry, an error occurred. Please try again.")

async def remove_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /remove_name command."""
    try:
        user_id = update.effective_user.id
        logger.info(f"User {user_id} used /remove_name")

        if not context.user_data.get('pdf'):
            await update.message.reply_text("Please upload a PDF first.")
            return

        if not context.args:
            await update.message.reply_text("Please provide text to remove. Example: /remove_name Word")
            return

        remove_text = ' '.join(context.args)
        context.user_data['pdf']['remove'] = remove_text
        await update.message.reply_text(f"Text '{remove_text}' will be removed. Use /to to apply changes.")

    except Exception as e:
        logger.error(f"Error in remove_name for user {user_id}: {str(e)}")
        await update.message.reply_text("Sorry, an error occurred. Please try again.")

async def apply_changes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /to command to apply renaming and send file."""
    try:
        user_id = update.effective_user.id
        logger.info(f"User {user_id} used /to")

        if not context.user_data.get('pdf'):
            await update.message.reply_text("Please upload a PDF first.")
            return

        pdf_data = context.user_data['pdf']
        original_name = pdf_data['original_name']
        file_path = pdf_data['file_path']
        prefix = pdf_data['prefix']
        suffix = pdf_data['suffix']
        remove_text = pdf_data['remove']

        # Create new filename
        name, ext = os.path.splitext(original_name)
        new_name = name

        # Apply remove
        if remove_text:
            new_name = new_name.replace(remove_text, '')
        
        # Apply prefix and suffix
        new_name = f"{prefix}{new_name}{suffix}"
        new_filename = f"{new_name}{ext}"
        new_filepath = os.path.join(os.path.dirname(file_path), new_filename)

        # Rename file
        os.rename(file_path, new_filepath)
        logger.info(f"File renamed for user {user_id}: {original_name} -> {new_filename}")

        # Send renamed file
        with open(new_filepath, 'rb') as file:
            await update.message.reply_document(
                document=InputFile(file, filename=new_filename),
                caption=f"Renamed PDF: {new_filename}"
            )

        # Cleanup
        os.remove(new_filepath)
        context.user_data.clear()
        logger.info(f"Cleanup completed for user {user_id}")

    except Exception as e:
        logger.error(f"Error in apply_changes for user {user_id}: {str(e)}")
        await update.message.reply_text("Sorry, an error occurred while processing the file.")

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline button callbacks."""
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id
    logger.info(f"User {user_id} clicked button: {query.data}")

    if query.data == 'add_prefix':
        await query.message.reply_text("Please enter prefix using /add_prefix YourText")
    elif query.data == 'add_suffix':
        await query.message.reply_text("Please enter suffix using /add_suffix YourText")
    elif query.data == 'remove_name':
        await query.message.reply_text("Please enter text to remove using /remove_name Word")
    elif query.data == 'apply':
        await apply_changes(update, context)

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle errors globally."""
    logger.error(f"Update {update} caused error {context.error}")
    if update and update.effective_message:
        await update.effective_message.reply_text("An unexpected error occurred. Please try again.")

def main():
    """Start the bot."""
    # Replace 'YOUR_BOT_TOKEN' with your actual bot token
    app = ApplicationBuilder().token('YOUR_BOT_TOKEN').build()

    # Register handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("add_prefix", add_prefix))
    app.add_handler(CommandHandler("add_suffix", add_suffix))
    app.add_handler(CommandHandler("remove_name", remove_name))
    app.add_handler(CommandHandler("to", apply_changes))
    app.add_handler(MessageHandler(filters.Document.PDF, handle_pdf))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_error_handler(error_handler)

    # Start the bot
    logger.info("Bot started")
    app.run_polling()

if __name__ == '__main__':
    main()
