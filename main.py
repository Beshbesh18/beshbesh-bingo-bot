import os
import logging
from flask import Flask, request
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler

# Logging ማስተካከል
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = "8891809240:AAGTl1vmyt-Gd1DIFJSAkm-BjpeLDS-Wm3M"
PAYMENT_INFO = "ቴሌብር / CBE: 0918719332"
balances = {}

app = Flask(__name__)

# ቴሌግራም አፕሊኬሽኑን ማስጀመር (առանց run_polling)
application = Application.builder().token(TOKEN).build()

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
    """ቴሌግራም የሚልከውን መረጃ የሚቀበለው ክፍል"""
    update = Update.de_json(request.get_json(force=True), application.bot)
    application.update_queue.put(update)
    return "OK", 200

if __name__ == '__main__':
    # ቦቱ ሲነሳ ዌብሁክን ከ Render ዩአርኤል ጋር ማያያዝ
    PORT = int(os.environ.get("PORT", 10000))
    WEBHOOK_URL = f"https://beshbesh-bingo-bot-5.onrender.com/{TOKEN}"
    
    # Bot webhook ማስተካከል
    import asyncio
    async def setup_webhook():
        await application.initialize()
        await application.bot.set_webhook(url=WEBHOOK_URL)
        logger.info(f"Webhook set to {WEBHOOK_URL}")

    asyncio.run(setup_webhook())
    
    app.run(host='0.0.0.0', port=PORT)
    
