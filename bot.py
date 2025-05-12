import os
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)
from dotenv import load_dotenv
from flask import Flask, request

# Load environment variables
load_dotenv()

# Get BOT_TOKEN
BOT_TOKEN = os.getenv("BOT_TOKEN")

# Hardcode your Render service webhook URL
WEBHOOK_URL = "https://telegram-pdf-rename-bot.onrender.com/webhook"

# Create downloads directory
DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Initialize Flask app
flask_app = Flask(__name__)
flask_app.telegram_app = None

# Log initial configuration
print(f"DEBUG: BOT_TOKEN: {'set' if BOT_TOKEN else 'not set'}")
print(f"DEBUG: BOT_TOKEN value: {BOT_TOKEN if BOT_TOKEN else 'None'}")
print(f"DEBUG: WEBHOOK_URL: {WEBHOOK_URL}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print("DEBUG: Handling /start command")
    await update.message.reply_text("Send me a PDF to begin.")

async def handle_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print("DEBUG: Handling PDF")
    doc = update.message.document
    if doc.mime_type != "application/pdf":
        await update.message.reply_text("Please send a valid PDF.")
        return

    file_path = os.path.join(DOWNLOAD_DIR, doc.file_name)
    try:
        file = await context.bot.get_file(doc.file_id)
        await file.download_to_drive(file_path)
    except Exception as e:
        print(f"ERROR downloading PDF: {e}")
        await update.message.reply_text(f"Error downloading PDF: {e}")
        return

    context.user_data["file_path"] = file_path
    context.user_data["file_name"] = doc.file_name
    context.user_data["prefix"] = ""
    context.user_data["suffix"] = ""
    context.user_data["remove"] = ""

    keyboard = [
        [InlineKeyboardButton("➕ Add Prefix", callback_data="add_prefix"),
         InlineKeyboardButton("➕ Add Suffix", callback_data="add_suffix")],
        [InlineKeyboardButton("❌ Remove Part", callback_data="remove_part"),
         InlineKeyboardButton("✅ Finish Rename", callback_data="rename_now")]
    ]
    await update.message.reply_text(
        f"PDF received: {doc.file_name}\nChoose an option:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print("DEBUG: Handling button callback")
    query = update.callback_query
    await query.answer()
    action = query.data
    context.user_data["action"] = action

    if action == "rename_now":
        await perform_rename(query, context)
        return

    prompts = {
        "add_prefix": "Send the prefix you want to add.",
        "add_suffix": "Send the suffix you want to add.",
        "remove_part": "Send the part of the name you want to remove.",
    }
    await query.message.reply_text(prompts[action])

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print("DEBUG: Handling text input")
    if "file_path" not in context.user_data or "action" not in context.user_data:
        return

    action = context.user_data["action"]
    text = update.message.text.strip()

    if action == "add_prefix":
        context.user_data["prefix"] = text
        await update.message.reply_text(f"Prefix '{text}' added.")
    elif action == "add_suffix":
        context.user_data["suffix"] = text
        await update.message.reply_text(f"Suffix '{text}' added.")
    elif action == "remove_part":
        context.user_data["remove"] = text
        await update.message.reply_text(f"Part '{text}' will be removed.")

    context.user_data.pop("action", None)

async def perform_rename(query_or_update, context: ContextTypes.DEFAULT_TYPE):
    print("DEBUG: Performing rename")
    if "file_path" not in context.user_data:
        await query_or_update.message.reply_text("Send a PDF first.")
        return

    old_name = context.user_data["file_name"]
    base_name = old_name.rsplit(".pdf", 1)[0]
    base_name = base_name.replace(context.user_data.get("remove", ""), "")

    new_name = f"{context.user_data['prefix']}{base_name}{context.user_data['suffix']}.pdf"
    old_path = context.user_data["file_path"]
    new_path = os.path.join(DOWNLOAD_DIR, new_name)

    try:
        os.rename(old_path, new_path)
        with open(new_path, "rb") as f:
            print("DEBUG: Sending renamed PDF")
            await query_or_update.message.reply_document(document=f, filename=new_name)
        await query_or_update.message.reply_text("Here is your renamed PDF.")
        os.remove(new_path)
    except Exception as e:
        print(f"ERROR processing PDF: {e}")
        await query_or_update.message.reply_text(f"Error processing PDF: {e}")
    finally:
        context.user_data.clear()

async def set_webhook(app):
    print(f"DEBUG: Setting webhook with BOT_TOKEN: {'set' if BOT_TOKEN else 'not set'}, WEBHOOK_URL: {WEBHOOK_URL}")
    if not BOT_TOKEN or not WEBHOOK_URL:
        print("ERROR: BOT_TOKEN or WEBHOOK_URL is missing")
        return False
    try:
        await app.bot.set_webhook(url=WEBHOOK_URL)
        print(f"Webhook set to {WEBHOOK_URL}")
        return True
    except Exception as e:
        print(f"ERROR setting webhook: {e}")
        return False

@flask_app.route("/webhook", methods=["POST"])
def webhook():
    print("DEBUG: Received webhook request")
    try:
        if not hasattr(flask_app, 'telegram_app') or flask_app.telegram_app is None:
            print("ERROR: Telegram application not initialized")
            return "error", 500
        data = request.get_json(force=True)
        print(f"DEBUG: Webhook data: {data}")
        update = Update.de_json(data, flask_app.telegram_app.bot)
        if update:
            flask_app.telegram_app.create_task(flask_app.telegram_app.process_update(update))
            return "ok"
        else:
            print("ERROR: Failed to parse update")
            return "error", 500
    except Exception as e:
        print(f"ERROR in webhook: {e}")
        return "error", 500

def init_application():
    print("DEBUG: Starting initialization")
    if not BOT_TOKEN:
        print("ERROR: BOT_TOKEN is not set")
        return None
    if not WEBHOOK_URL:
        print("ERROR: WEBHOOK_URL is not set")
        return None
    
    print("DEBUG: Building Telegram application")
    try:
        application = ApplicationBuilder().token(BOT_TOKEN).build()
        print("DEBUG: Telegram application built successfully")
    except Exception as e:
        print(f"ERROR building application: {e}")
        return None

    flask_app.telegram_app = application
    print("DEBUG: Telegram application assigned to flask_app")

    # Add handlers
    print("DEBUG: Adding handlers")
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.Document.PDF, handle_pdf))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    # Set webhook
    print("DEBUG: Setting webhook")
    import asyncio
    success = asyncio.run(set_webhook(application))
    if not success:
        print("WARNING: Webhook setup failed, please set manually using: "
              f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook?url={WEBHOOK_URL}")
    else:
        print("DEBUG: Webhook setup completed")

    return application

if __name__ == "__main__":
    print("DEBUG: Starting Flask app")
    application = init_application()
    if application:
        print("DEBUG: Flask app starting with initialized application")
        flask_app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
    else:
        print("FATAL: Application initialization failed, Flask not started")
