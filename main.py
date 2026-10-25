import os
import logging
import asyncio
import threading
from flask import Flask, request
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler

# ሎጊንግ ማስተካከል
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# አዲሱ ቶከን እዚህ ተካቷል
TOKEN = "8389391544:AAH1id2OWlLoC92_oXDRgcOfEAP48Uh1oL0"
PAYMENT_INFO = "ቴሌብር / CBE: 0918719332"
balances = {}

app = Flask(__name__)

# 1. ለአስራር (Async) የሚሆን የተለየ የጀርባ ሉፕ (Background Loop) መፍጠር
main_loop = asyncio.new_event_loop()

def run_async_loop(loop):
    asyncio.set_event_loop(loop)
    loop.run_forever()

# ሉፑን በጀርባ ትሬድ (Thread) ማስጀመር
t = threading.Thread(target=run_async_loop, args=(main_loop,), daemon=True)
t.start()

# ቴሌግራም አፕሊኬሽኑን ማስጀመር
application = Application.builder().token(TOKEN).build()

async def setup_bot():
    await application.initialize()
    WEBHOOK_URL = f"https://beshbesh-bingo-bot-5.onrender.com/{TOKEN}"
    await application.bot.set_webhook(url=WEBHOOK_URL)
    logger.info(f"Webhook set to {WEBHOOK_URL}")

# ቦቱ ሲነሳ ዌብሁኩን በጀርባ ሉፕ ማዋቀር
asyncio.run_coroutine_threadsafe(setup_bot(), main_loop)

async def start(update: Update, context):
    user_id = update.effective_user.id
    if user_id not in balances:
        balances[user_id] = 100.0

    keyboard = [
        [InlineKeyboardButton("🎮 ቢንጎ መጫወት", callback_data="play_bingo")],
        [InlineKeyboardButton("💰 ቀሪ ሂሳብ (Balance)", callback_data="balance")],
        [InlineKeyboardButton("📥 ገንዘብ ማስገባት (Deposit)", callback_data="deposit")],
        [InlineKeyboardButton("📤 ገንዘብ ማውጣት (Withdraw)", callback_data="withdraw")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        f"እንኳን ወደ በሽ በሽ ቢንጎ ቦት በደህና መጡ!\n\n"
        f"💳 የእርስዎ ቀሪ ሂሳብ: {balances[user_id]} ብር\n\n"
        f"ከታች ባሉት አማራጮች መምረጥ ይችላሉ፦",
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )

async def button_handler(update: Update, context):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if user_id not in balances:
        balances[user_id] = 100.0

    if query.data == "balance":
        await query.edit_message_text(f"💰 የእርስዎ ቀሪ ሂሳብ: {balances[user_id]} ብር", parse_mode="Markdown")
    elif query.data == "deposit":
        await query.edit_message_text(f"📥 ገንዘብ ለማስገባት (Deposit):\n\nእባክዎ ገንዘቡን በሚከተለው አካውንት ያስተላልፉ:\n\n📌 {PAYMENT_INFO}", parse_mode="Markdown")
    elif query.data == "withdraw":
        await query.edit_message_text(f"📤 ገንዘብ ለማውጣት (Withdraw):\n\nየማውጫ ጥያቄዎ ተመዝግቧል።", parse_mode="Markdown")
    elif query.data == "play_bingo":
        await query.edit_message_text(f"🎮 ቢንጎ ጨዋታ ተጀምሯል!\n\nቁጥሮች እየተጠሩ ነው...", parse_mode="Markdown")

application.add_handler(CommandHandler("start", start))
application.add_handler(CallbackQueryHandler(button_handler))

@app.route('/')
def home():
    return "Telegram Bingo Bot is running via Webhook on Render!"

@app.route(f'/{TOKEN}', methods=['POST'])
def webhook():
    """ቴሌግራም መልእክት ሲልክ የሚቀበለው እና ወደ ቦቱ የሚያስተላልፈው ትክክለኛ ክፍል"""
    try:
        json_data = request.get_json(force=True)
        update = Update.de_json(json_data, application.bot)
        # መልእክቱን በጀርባው ባለው ሉፕ ማስተካከል
        asyncio.run_coroutine_threadsafe(application.process_update(update), main_loop)
    except Exception as e:
        logger.error(f"Error processing update: {e}")
    return "OK", 200

if __name__ == '__main__':
    PORT = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=PORT)
