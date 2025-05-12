import os
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)
from dotenv import load_dotenv
from flask import Flask, request

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
APP_URL = os.getenv("RENDER_EXTERNAL_HOSTNAME", os.getenv("RENDER_EXTERNAL_URL"))
if APP_URL and not APP_URL.startswith("https://"):
    APP_URL = f"https://{APP_URL}"
DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

flask_app = Flask(__name__)
flask_app.telegram_app = None  # Initialize to None to avoid AttributeError

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Send me a PDF to begin.")

async def handle_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if doc.mime_type != "application/pdf":
        await update.message.reply_text("Please send a valid PDF.")
        return

    file_path = os.path.join(DOWNLOAD_DIR, doc.file_name)
    try:
        file = await context.bot.get_file(doc.file_id)
        await file.download_to_drive(file_path)
    except Exception as e:
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
            print("DEBUG: Sending renamed PDF...")
            await query_or_update.message.reply_document(document=f, filename=new_name)
        await query_or_update.message.reply_text("Here is your renamed PDF.")
        os.remove(new_path)
    except Exception as e:
        print(f"ERROR processing PDF: {e}")
        await query_or_update.message.reply_text(f"Error processing PDF: {e}")
    finally:
        context.user_data.clear()

async def set_webhook(app):
    print(f"DEBUG: BOT_TOKEN is: {'set' if BOT_TOKEN else 'not set'}")
    print(f"DEBUG: APP_URL is: {APP_URL}")
    if not BOT_TOKEN or not APP_URL:
        print("ERROR: BOT_TOKEN or APP_URL is missing")
        return
    try:
        await app.bot.set_webhook(url=f"{APP_URL}/webhook")
        print(f"Webhook set to {APP_URL}/webhook")
    except Exception as e:
        print(f"ERROR setting webhook: {e}")

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

async def main():
    print(f"DEBUG: Starting main function")
    if not BOT_TOKEN:
        print("ERROR: BOT_TOKEN is not set")
        return None
    try:
        application = ApplicationBuilder().token(BOT_TOKEN).build()
        flask_app.telegram_app = application  # Use telegram_app instead of application
        print("DEBUG: Telegram application initialized")
    except Exception as e:
        print(f"ERROR initializing application: {e}")
        return None

    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.Document.PDF, handle_pdf))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    await set_webhook(application)
    return application

if __name__ == "__main__":
    import asyncio
    loop = asyncio.get_event_loop()
    application = loop.run_until_complete(main())
    if application:
        print("DEBUG: Starting Flask app")
        flask_app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
