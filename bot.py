from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)
import os

BOT_TOKEN = "7790737352:AAFf8UBYIJDb4_VlM21Xsb5nyGJxxgvjX1I"
DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Start command
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Send me a PDF to begin.")

# When user sends PDF
async def handle_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if doc.mime_type != "application/pdf":
        await update.message.reply_text("Please send a valid PDF.")
        return

    file_path = os.path.join(DOWNLOAD_DIR, doc.file_name)
    await context.bot.get_file(doc.file_id).download_to_drive(file_path)

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
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        f"PDF received: {doc.file_name}\nChoose an option:",
        reply_markup=reply_markup
    )

# Handle button clicks
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data
    context.user_data["action"] = data

    prompts = {
        "add_prefix": "Send the prefix you want to add.",
        "add_suffix": "Send the suffix you want to add.",
        "remove_part": "Send the part of the name you want to remove.",
    }

    if data == "rename_now":
        await perform_rename(update, context)
    else:
        await query.message.reply_text(prompts[data])

# When user replies with a prefix/suffix/word to remove
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
        await update.message.reply_text(f"Part '{text}' will be removed from filename.")

    del context.user_data["action"]

# Final rename
async def perform_rename(update_or_query, context: ContextTypes.DEFAULT_TYPE):
    if "file_path" not in context.user_data:
        return await update_or_query.message.reply_text("Send a PDF first.")

    old_name = context.user_data["file_name"]
    name_only = old_name.rsplit(".pdf", 1)[0]
    name_only = name_only.replace(context.user_data["remove"], "")
    new_name = f"{context.user_data['prefix']}{name_only}{context.user_data['suffix']}.pdf"

    old_path = context.user_data["file_path"]
    new_path = os.path.join(DOWNLOAD_DIR, new_name)
    os.rename(old_path, new_path)

    await update_or_query.message.reply_document(document=InputFile(new_path), filename=new_name)
    os.remove(new_path)
    context.user_data.clear()

    await update_or_query.message.reply_text("Here is your renamed PDF.")

# Main app
if __name__ == "__main__":
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Document.PDF, handle_pdf))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), text_handler))

    print("Bot running...")
    app.run_polling()
