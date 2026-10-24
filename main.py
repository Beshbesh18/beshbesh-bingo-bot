
import os
import threading
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, CallbackQueryHandler

# 1. 24/7 Uptime ለማቆየት የሚረዳ የ Flask ሰርቨር
app = Flask('')

@app.route('/')
def home():
    return "Telegram Bingo Bot is running 24/7 on Render!"

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

# 2. የቦት ቶከን
TOKEN = "8891809240:AAG0dKX9863zZSD5b6CrKN514EjeaOKNMm0"
PAYMENT_INFO = "ቴሌብር / CBE: 0918719332"

balances = {}

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if user_id not in balances:
        balances[user_id] = 100.0

    if query.data == "balance":
        await query.edit_message_text(
            f"💰 የእርስዎ ቀሪ ሂሳብ: {balances[user_id]} ብር",
            parse_mode="Markdown"
        )
    elif query.data == "deposit":
        await query.edit_message_text(
            f"📥 ገንዘብ ለማስገባት (Deposit):\n\n"
            f"እባክዎ ገንዘቡን በሚከተለው አካውንት ያስተላልፉ:\n\n"
            f"📌 {PAYMENT_INFO}\n\n"
            f"ငንዘቡን ካስተላለፉ በኋላ የትራንዛክሽን ማረጋገጫውን ለአስተዳዳሪው ይላኩ።",
            parse_mode="Markdown"
        )
    elif query.data == "withdraw":
        await query.edit_message_text(
            f"📤 ገንዘብ ለማውጣት (Withdraw):\n\n"
            f"የማውጫ ጥያቄዎ ተመዝግቧል። አስተዳዳሪው ሲያረጋግጥ ወደ አካውንትዎ ይላካል።",
            parse_mode="Markdown"
        )
    elif query.data == "play_bingo":
        await query.edit_message_text(
            f"🎮 ቢንጎ ጨዋታ ተጀምሯል!\n\n"
            f"ቁጥሮች እየተጠሩ ነው... መልካም እድል!\n"
            f"(የዴሞ ጨዋታ በሂደት ላይ ነው)",
            parse_mode="Markdown"
        )

def main():
    t = threading.Thread(target=run_flask)
    t.start()

    application = ApplicationBuilder().token(TOKEN).build()
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(button_handler))

    print("በሽ በሽ ቢንጎ ቦት በሥራ ላይ ነው...")
    application.run_polling()

if __name__ == '__main__':
    main()
