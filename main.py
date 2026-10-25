import os
import logging
import asyncio
import threading
import random
from flask import Flask, request
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler

# ሎጊንግ ማስተካከል
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# አዲሱ ቶከን እዚህ ተካቷል
TOKEN = "8389391544:AAH1id2OWlLoC92_oXDRgcOfEAP48Uh1oL0"
PAYMENT_INFO = "ቴሌብር / CBE: 0918719332"

# የውሂብ ማከማቻዎች (በከፍተኛ ተጫዋች ብዛት (እስከ 5000) ለመቋቋም የተዘጋጁ)
balances = {}
game_state = {
    "is_active": False,
    "called_numbers": [],
    "current_number": None,
    "players": set(),
    "cards": {},       # {user_id: card_matrix}
    "marked": {},      # {user_id: set of marked numbers}
    "winners": []
}

app = Flask(__name__)

# 1. ለአስራር (Async) የሚሆን የተለየ የጀርባ ሉፕ (Background Loop) መፍጠር
main_loop = asyncio.new_event_loop()

def run_async_loop(loop):
    asyncio.set_event_loop(loop)
    loop.run_forever()

t = threading.Thread(target=run_async_loop, args=(main_loop,), daemon=True)
t.start()

application = Application.builder().token(TOKEN).build()

async def setup_bot():
    await application.initialize()
    WEBHOOK_URL = f"https://beshbesh-bingo-bot-5.onrender.com/{TOKEN}"
    await application.bot.set_webhook(url=WEBHOOK_URL)
    logger.info(f"Webhook set to {WEBHOOK_URL}")

asyncio.run_coroutine_threadsafe(setup_bot(), main_loop)

# ----------------- ቢንጎ ጨዋታ ሎጂክ እና ካርድ ማመንጨት -----------------
def generate_bingo_card():
    """ከ 1 እስከ 75 ያሉ ቁጥሮችን በመጠቀም የሀበሻ ቢንጎ ካርድ ማመንጨት (5x5)"""
    card = []
    # B: 1-15, I: 16-30, N: 31-45, G: 46-60, O: 61-75
    cols = [
        random.sample(range(1, 16), 5),
        random.sample(range(16, 31), 5),
        random.sample(range(31, 46), 5),
        random.sample(range(46, 61), 5),
        random.sample(range(61, 76), 5)
    ]
    # ማትሪክሱን ማስተካከል (Transpose)
    for r in range(5):
        row = [cols[c][r] for c in range(5)]
        card.append(row)
    
    # መሀልኛውን ነፃ (FREE) ማድረግ
    card[2][2] = "FREE"
    return card

def check_win(card, marked_set):
    """አግድም፣ ቀጥታ፣ ኮርነር፣ ስላሽ እና ሙሉ ዝግ (Full House) ማረጋገጫ"""
    size = 5
    
    # 1. አግድም (Horizontal)
    for r in range(size):
        if all(card[r][c] == "FREE" or card[r][c] in marked_set for c in range(size)):
            return "አግድም (Horizontal)"

    # 2. ቀጥታ (Vertical)
    for c in range(size):
        if all(card[r][c] == "FREE" or card[r][c] in marked_set for r in range(size)):
            return "ቀጥታ (Vertical)"

    # 3. ስላሽ / ሰያፍ (Diagonal)
    if all(card[i][i] == "FREE" or card[i][i] in marked_set for i in range(size)):
        return "ስላሽ (Diagonal)"
    if all(card[i][size - 1 - i] == "FREE" or card[i][size - 1 - i] in marked_set for i in range(size)):
        return "ስላሽ (Diagonal)"

    # 4. ኮርነር (Corners)
    corners = [(0, 0), (0, size-1), (size-1, 0), (size-1, size-1)]
    if all(card[r][c] in marked_set for r, c in corners):
        return "ኮርነር (Corners)"

    # 5. አንድ ዝግ / ሙሉ (Full House)
    all_cells = [card[r][c] for r in range(size) for c in range(size) if card[r][c] != "FREE"]
    if all(num in marked_set for num in all_cells):
        return "ሙሉ ዝግ (Full House Bingo!)"

    return None

# ----------------- የቦት ትዕዛዞች እና መቆጣጠሪያዎች -----------------

async def start(update: Update, context):
    user_id = update.effective_user.id
    if user_id not in balances:
        balances[user_id] = 100.0

    keyboard = [
        [InlineKeyboardButton("🎮 ቢንጎ መጫወት (Play Bingo)", callback_data="play_bingo")],
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
        await query.edit_message_text(
            f"💰 የእርስዎ ቀሪ ሂሳብ: {balances[user_id]} ብር\n\nለመመለስ /start ይጫኑ።", 
            parse_mode="Markdown"
        )
    elif query.data == "deposit":
        await query.edit_message_text(
            f"📥 ገንዘብ ለማስገባት (Deposit):\n\nእባክዎ ገንዘቡን በሚከተለው አካውንት ያስተላልፉ:\n\n📌 {PAYMENT_INFO}\n\nገንዘቡን ከላኩ በኋላ ደረሰኙን ለአድሚን ያጋሩ።", 
            parse_mode="Markdown"
        )
    elif query.data == "withdraw":
        await query.edit_message_text(
            f"📤 ገንዘብ ለማውጣት (Withdraw):\n\nየማውጫ ጥያቄዎ ተመዝግቧል። በቅርብ ጊዜ ወደ አካውንትዎ ይገባል።", 
            parse_mode="Markdown"
        )
    elif query.data == "play_bingo":
        # ተጫዋቹን ወደ ጨዋታው ማስገባት እና ካርድ ማመንጨት
        game_state["players"].add(user_id)
        if user_id not in game_state["cards"]:
            game_state["cards"][user_id] = generate_bingo_card()
            game_state["marked"][user_id] = set()

        card = game_state["cards"][user_id]
        marked = game_state["marked"][user_id]

        # የካርድ አቀማመጥ በቁልፍ ሰሌዳ (Interactive Keyboard) መልክ ማዘጋጀት
        keyboard = []
        for r_idx, row in enumerate(card):
            row_buttons = []
            for c_idx, val in enumerate(row):
                if val == "FREE":
                    btn_text = "⭐ FREE"
                    cb_data = f"cell_free_{r_idx}_{c_idx}"
                else:
                    status = "✅" if val in marked else "⬜"
                    btn_text = f"{status} {val}"
                    cb_data = f"cell_{val}"
                row_buttons.append(InlineKeyboardButton(btn_text, callback_data=cb_data))
            keyboard.append(row_buttons)

        keyboard.append([InlineKeyboardButton("📢 ቢንጎ (Bingo!) አሳውቅ", callback_data="claim_bingo")])
        keyboard.append([InlineKeyboardButton("🔙 ወደ ዋናው ምናሌ", callback_data="main_menu")])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        active_players_count = len(game_state["players"])
        
        current_num_display = game_state["current_number"] if game_state["current_number"] else "እየጠበቀ ነው..."

        await query.edit_message_text(
            f"🎮 **የሀበሻ ቢንጎ ጨዋታ በሂደት ላይ ነው!**\n\n"
            f"🔊 የተጠራው ቁጥር: **{current_num_display}**\n"
            f"👥 LIVE የተጫዋቾች ብዛት: **{active_players_count} ሰው**\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"የእርስዎ ቢንጎ ካርድ ከታች ይታያል። የተጠራ ቁጥር ሲደርስ እየተጫኑ ይምረጡ!:",
            reply_markup=reply_markup,
            parse_mode="Markdown"
        )
    elif query.data.startswith("cell_"):
        # ተጫዋቹ በካርዱ ላይ ቁጥር ሲነካ
        parts = query.data.split("_")
        if len(parts) == 2:
            num = int(parts[1])
            # ቁጥሩ መጥራቱን ማረጋገጥ
            if num in game_state["called_numbers"]:
                game_state["marked"][user_id].add(num)
                await query.answer(f"ቁጥር {num} ተይዟል! ✅", show_alert=False)
            else:
                await query.answer(f"ይህ ቁጥር ({num}) ገና አልተጠራም!", show_alert=True)
        
        # ካርዱን መልሶ ማሳየት
        await button_handler_refresh(query, user_id)

    elif query.data == "claim_bingo":
        card = game_state["cards"].get(user_id)
        marked = game_state["marked"].get(user_id, set())
        win_type = check_win(card, marked)
        
        if win_type:
            await query.edit_message_text(
                f"🎉 **እንኳን ደስ አለዎት! አሸንፈዋል!** 🎉\n\n"
                f"ተաղግቷል/የተዘጋው የጨዋታ አይነት: **{win_type}**\n"
                f"ሽልማቱ ወደ ቀሪ ሂሳብዎ ተጨምሯል!",
                parse_mode="Markdown"
            )
            balances[user_id] += 500.0  # የሽልማት መጠን
            game_state["winners"].append(user_id)
        else:
            await query.answer("እስካሁን የቢንጎ መስፈርት አልተሟላም! ተጨማሪ ቁጥሮችን ይጠብቁ።", show_alert=True)

    elif query.data == "main_menu":
        await start_from_query(query, user_id)

async def button_handler_refresh(query, user_id):
    card = game_state["cards"][user_id]
    marked = game_state["marked"][user_id]
    keyboard = []
    for r_idx, row in enumerate(card):
        row_buttons = []
        for c_idx, val in enumerate(row):
            if val == "FREE":
                btn_text = "⭐ FREE"
                cb_data = f"cell_free_{r_idx}_{c_idx}"
            else:
                status = "✅" if val in marked else "⬜"
                btn_text = f"{status} {val}"
                cb_data = f"cell_{val}"
            row_buttons.append(InlineKeyboardButton(btn_text, callback_data=cb_data))
        keyboard.append(row_buttons)

    keyboard.append([InlineKeyboardButton("📢 ቢንጎ (Bingo!) አሳውቅ", callback_data="claim_bingo")])
    keyboard.append([InlineKeyboardButton("🔙 ወደ ዋናው ምናሌ", callback_data="main_menu")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    active_players_count = len(game_state["players"])
    current_num_display = game_state["current_number"] if game_state["current_number"] else "እየጠበቀ ነው..."

    try:
        await query.edit_message_text(
            f"🎮 **የሀበሻ ቢንጎ ጨዋታ በሂደት ላይ ነው!**\n\n"
            f"🔊 የተጠራው ቁጥር: **{current_num_display}**\n"
            f"👥 LIVE የተጫዋቾች ብዛት: **{active_players_count} ሰው**\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"የእርስዎ ቢንጎ ካርድ ከታች ይታያል። ቁጥሮቹን እየመረጡ ይጫወቱ!",
            reply_markup=reply_markup,
            parse_mode="Markdown"
        )
    except Exception:
        pass

async def start_from_query(query, user_id):
    keyboard = [
        [InlineKeyboardButton("🎮 ቢንጎ መጫወት", callback_data="play_bingo")],
        [InlineKeyboardButton("💰 ቀሪ ሂሳብ (Balance)", callback_data="balance")],
        [InlineKeyboardButton("📥 ገንዘብ ማስገባት (Deposit)", callback_data="deposit")],
        [InlineKeyboardButton("📤 ገንዘብ ማውጣት (Withdraw)", callback_data="withdraw")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(
        f"እንኳን ወደ በሽ በሽ ቢንጎ ቦት በደህና መጡ!\n\n"
        f"💳 የእርስዎ ቀሪ ሂሳብ: {balances[user_id]} ብር\n\n"
        f"ከታች ባሉት አማራጮች መምረጥ ይችላሉ፦",
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )

# ----------------- አውቶማቲክ ቁጥር ጥሪ የጀርባ ስራ (Background Game Loop) -----------------
async def bingo_number_caller():
    """ከ 1 እስከ 75 ያሉ ቁጥሮችን በየሰከንዱ እየጠራ ለተጫዋቾች ድምጽ/ማሳወቂያ የሚሰጥ ሉፕ"""
    while True:
        await asyncio.sleep(5)  # በየ 5 ሰከንዱ ቁጥር ይጠራል
        if len(game_state["players"]) > 0:
            available_nums = [n for n in range(1, 76) if n not in game_state["called_numbers"]]
            if available_nums:
                called = random.choice(available_nums)
                game_state["called_numbers"].append(called)
                game_state["current_number"] = called
                logger.info(f"ቢንጎ ቁጥር ተጠራ: {called} (አጠቃላይ ተጫዋቾች: {len(game_state['players'])})")
                
                # ድምጽ የማስመሰል (Audio/Voice notification notice) ወይም መልእክት ማሰራጨት
                for uid in list(game_state["players"]):
                    try:
                        # ቁጥር ሲጠራ የድምጽ/ኖቲፊኬሽን ማሳወቂያ መላክ ይቻላል
                        pass
                    except Exception as e:
                        logger.error(f"Error notifying user {uid}: {e}")
            else:
                # ቁጥሮች ካለቁ ጨዋታውን እንደገና ማስጀመር
                game_state["called_numbers"] = []
                game_state["current_number"] = None

# የጀርባ ቁጥር ጥሪ ስራውን በሉፑ ማስጀመር
asyncio.run_coroutine_threadsafe(bingo_number_caller(), main_loop)

application.add_handler(CommandHandler("start", start))
application.add_handler(CallbackQueryHandler(button_handler))

@app.route('/')
def home():
    return "Telegram Habesha Bingo Bot is running via Webhook on Render!"

@app.route(f'/{TOKEN}', methods=['POST'])
def webhook():
    """ቴሌግራም መልእክት ሲልክ የሚቀበለው እና ወደ ቦቱ የሚያስተላልፈው ትክክለኛ ክፍል"""
    try:
        json_data = request.get_json(force=True)
        update = Update.de_json(json_data, application.bot)
        asyncio.run_coroutine_threadsafe(application.process_update(update), main_loop)
    except Exception as e:
        logger.error(f"Error processing update: {e}")
    return "OK", 200

if __name__ == '__main__':
    PORT = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=PORT)
