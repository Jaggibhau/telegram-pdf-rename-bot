from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
import os

API_ID = 24500851  # Replace with your API_ID
API_HASH = "4e1329c4610258e6fb2c271a337f8b3c"
BOT_TOKEN = "7173618731:AAG44jG60Tpgytah9TKuXiL8J9e_dvVZXsY"

app = Client("rename_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

user_data = {}

@app.on_message(filters.document | filters.video | filters.audio)
async def handle_file(client, message: Message):
    file = message.document or message.video or message.audio
    user_data[message.chat.id] = {
        "file_id": file.file_id,
        "original_name": file.file_name
    }

    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("✏️ Rename by replacing a word", callback_data="replace_word")]]
    )
    
    await message.reply(
        f"Original filename: `{file.file_name}`",
        reply_markup=keyboard
    )

@app.on_callback_query(filters.regex("replace_word"))
async def ask_word_to_replace(client, query: CallbackQuery):
    await query.message.reply("Send me the word you want to replace in the filename.")
    user_data[query.message.chat.id]["step"] = "awaiting_replace_word"
    await query.answer()

@app.on_message(filters.text)
async def handle_text_input(client, message: Message):
    chat_id = message.chat.id
    if chat_id not in user_data or "step" not in user_data[chat_id]:
        return

    step = user_data[chat_id]["step"]
    text = message.text.strip()

    if step == "awaiting_replace_word":
        user_data[chat_id]["replace_this"] = text
        user_data[chat_id]["step"] = "awaiting_new_word"
        await message.reply(f"Now send the new word to replace `{text}`.")
    
    elif step == "awaiting_new_word":
        context = user_data[chat_id]
        old_name = context["original_name"]
        new_name = old_name.replace(context["replace_this"], text)

        temp_path = await client.download_media(context["file_id"])
        new_path = f"downloads/{new_name}"

        os.rename(temp_path, new_path)
        await message.reply_document(document=new_path, caption=f"Renamed file: `{new_name}`")
        os.remove(new_path)

        user_data.pop(chat_id)

app.run()
