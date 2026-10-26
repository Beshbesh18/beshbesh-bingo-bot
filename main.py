import os
import logging
import asyncio
import threading
import random
from flask import Flask, request
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters

# ሎጊንግ ማስተካከል
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# የቦት ቶከን
TOKEN = os.environ.get("TOKEN", "8389391544:AAH1id2OWlLoC92_oXDRgcOfEAP48Uh1oL0")

# የውሂብ ማከማቻዎች
balances = {}          # {user_id: balance}
user_phones = {}       # {user_id: phone_number}
user_active_card = {}  # {user_id: currently viewed card index}

MAX_PLAYERS = 500      # ከፍተኛ የተጫዋቾች ብዛት
GAME_COST = 10.0       # የጨዋታ/ካርርድ ዋጋ (10 ብር)

game_state = {
    "is_active": False,
    "called_numbers": [],
    "current_number": None,
    "players": set(),
    "cards": {},       # {user_id: [card_matrix_1, card_matrix_2, ...]}
    "marked": {},      # {user_id: [marked_set_1, marked_set_2, ...]}
    "winners": []
}

app = Flask(__name__)

# የጀርባ ሉፕ (Background Loop)
main_loop = asyncio.new_event_loop()

def run_async_loop(loop):
    asyncio.set_event_loop(loop)
    loop.run_forever()

t = threading.Thread(target=run_async_loop, args=(main_loop,), daemon=True)
t.start()

application = Application.builder().token(TOKEN).build()

async def setup_bot():
    await application.initialize()
    await application.start()
    
    RENDER_APP_NAME = os.environ.get("RENDER_EXTERNAL_URL", "https://beshbesh-bingo-bot-5.onrender.com")
    WEBHOOK_URL = f"{RENDER_APP_NAME}/{TOKEN}"
    
    await application.bot.set_webhook(url=WEBHOOK_URL)
    logger.info(f"Webhook set to {WEBHOOK_URL}")

asyncio.run_coroutine_threadsafe(setup_bot(), main_loop)

# ----------------- ቢንጎ ካርድ (5x5) እና ሎጂክ -----------------
def generate_bingo_card():
    cols = [
        random.sample(range(1, 16), 5),   # B
        random.sample(range(16, 31), 5),  # I
        random.sample(range(31, 46), 5),  # N
        random.sample(range(46, 61), 5),  # G
        random.sample(range(61, 76), 5)   # O
    ]
    card = []
    for r in range(5):
        row = [cols[c][r] for c in range(5)]
        card.append(row)
    
    card[2][2] = "FREE"
    return card

def check_win(card, marked_set):
    size = 5
    for r in range(size):
        if all(card[r][c] == "FREE" or card[r][c] in marked_set for c in range(size)):
            return "አግድም (Horizontal)"

    for c in range(size):
        if all(card[r][c] == "FREE" or card[r][c] in marked_set for r in range(size)):
            return "ቀጥታ (Vertical)"

    if all(card[i][i] == "FREE" or card[i][i] in marked_set for i in range(size)):
        return "ሰያፍ (Diagonal)"
    if all(card[i][size - 1 - i] == "FREE" or card[i][size - 1 - i] in marked_set for i in range(size)):
        return "ሰያፍ (Diagonal)"

    corners = [(0, 0), (0, size-1), (size-1, 0), (size-1, size-1)]
    if all(card[r][c] in marked_set for r, c in corners):
        return "ኮርነር (Corners)"

    all_cells = [card[r][c] for r in range(size) for c in range(size) if card[r][c] != "FREE"]
    if all(num in marked_set for num in all_cells):
        return "ሙሉ ዝግ (Full House Bingo!)"

    return None

# ----------------- የቦት ትዕዛዞች እና መቆጣጠሪያዎች -----------------

async def start(update: Update, context):
    user_id = update.effective_user.id
    if user_id not in balances:
        balances[user_id] = 10.0

    # ስልክ ቁጥር ካልተመዘገበ እንዲያጋራ መጠየቅ
    if user_id not in user_phones:
        contact_keyboard = [[KeyboardButton("📱 ስልክ ቁጥር አጋራ (Share Phone)", request_contact=True)]]
        reply_markup = ReplyKeyboardMarkup(contact_keyboard, resize_keyboard=True, one_time_keyboard=True)
        await update.message.reply_text(
            "እንኳን ወደ በሽ በሽ ቢንጎ ቦት በደህና መጡ!\n\n"
            "ቦቱን ለመጠቀም እባክዎ ከታች ያለውን ቁልፍ በመጫን ስልክ ቁጥርዎን ያጋሩ።",
            reply_markup=reply_markup
        )
        return

    await show_main_menu_message(update, user_id, is_new_message=True)

async def contact_handler(update: Update, context):
    user_id = update.effective_user.id
    if update.message.contact:
        phone_number = update.message.contact.phone_number
        user_phones[user_id] = phone_number

    if user_id not in balances:
        balances[user_id] = 10.0

    await show_main_menu_message(update, user_id, is_new_message=True)

async def show_main_menu_message(update_or_query, user_id, is_new_message=False):
    keyboard = [
        [InlineKeyboardButton("🎮 ቢንጎ መጫወት (Play Bingo)", callback_data="play_bingo")],
        [InlineKeyboardButton("💰 ቀሪ ሂሳብ (Balance)", callback_data="balance")],
        [InlineKeyboardButton("📥 ገንዘብ ማስገባት (Deposit)", callback_data="deposit")],
        [InlineKeyboardButton("📤 ገንዘብ ማውጣት (Withdraw)", callback_data="withdraw")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    text = (
        f"እንኳን ወደ በሽ በሽ ቢንጎ ቦት በደህና መጡ!\n\n"
        f"💳 የእርስዎ ቀሪ ሂሳብ: **{balances[user_id]} ብር**\n\n"
        f"ከታች ባሉት አማራጮች መምረጥ ይችላሉ፦"
    )
    
    if is_new_message:
        await update_or_query.message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")
    else:
        await update_or_query.edit_message_text(text, reply_markup=reply_markup, parse_mode="Markdown")

async def button_handler(update: Update, context):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if user_id not in balances:
        balances[user_id] = 10.0

    if query.data == "balance":
        keyboard = [[InlineKeyboardButton("🔙 ተመለስ", callback_data="main_menu")]]
        await query.edit_message_text(
            f"💰 የእርስዎ ቀሪ ሂሳብ: **{balances[user_id]} ብር**", 
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
    elif query.data == "deposit":
        keyboard = [
            [InlineKeyboardButton("📱 ቴሌብር (Telebirr)", callback_data="pay_telebirr")],
            [InlineKeyboardButton("🏦 ሲቢኢ ብር (CBE Birr)", callback_data="pay_cbe")],
            [InlineKeyboardButton("🔙 ተመለስ", callback_data="main_menu")]
        ]
        await query.edit_message_text(
            "📥 **ገንዘብ ማስገባት (Deposit)**\n\nእባክዎ ገንዘብ ሊያስገቡበት የሚፈልጉትን የክፍያ አማራጭ ይምረጡ (ቁጥር: `0918719332`):",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
    elif query.data in ["pay_telebirr", "pay_cbe"]:
        method_name = "ቴሌብር (Telebirr)" if query.data == "pay_telebirr" else "ሲቢኢ ብር (CBE Birr)"
        await query.edit_message_text(
            f"📱 **{method_name} አካውንት**:\n\n"
            f"• ስም: በሽ በሽ ቢንጎ\n"
            f"• ቁጥር: `0918719332`\n\n"
            f"ገንዘቡን ካስተላለፉ በኋላ የግብይቱን ማረጋገጫ (Screenshot) ለአድሚን ይላኩ።",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 ተመለስ", callback_data="deposit")]]),
            parse_mode="Markdown"
        )
    elif query.data == "withdraw":
        keyboard = [[InlineKeyboardButton("🔙 ተመለስ", callback_data="main_menu")]]
        await query.edit_message_text(
            f"📤 ገንዘብ ለማውጣት (Withdraw):\n\nየማውጫ ጥያቄዎ ተመዝግቧል። በቅርብ ጊዜ ወደ አካውንትዎ ይገባል።", 
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
    elif query.data == "play_bingo":
        # ተጫዋቾች ብዛት ከ 500 መብለጡን ማረጋገጥ
        if user_id not in game_state["players"] and len(game_state["players"]) >= MAX_PLAYERS:
            await query.answer("⚠️ የጨዋታው አዳራሽ ሞልቷል (ከማክስ 500 ተጫዋቾች አልፈዋል)። እባክዎ የሚቀጥለውን ዙር ይጠብቁ።", show_alert=True)
            return

        # የመጀመሪያ ካርድ ከሌለ 10 ብር መቀነስ
        if user_id not in game_state["cards"] or len(game_state["cards"][user_id]) == 0:
            if balances[user_id] < GAME_COST:
                await query.answer("⚠️ ቀሪ ሂሳብዎ በቂ አይደለም! ለመጫወት 10 ብር ያስፈልጋል። እባክዎ ገንዘብ ያስገቡ።", show_alert=True)
                return
            balances[user_id] -= GAME_COST
            game_state["players"].add(user_id)
            game_state["cards"][user_id] = [generate_bingo_card()]
            game_state["marked"][user_id] = [set()]
            user_active_card[user_id] = 0

        await render_bingo_board(query, user_id)

    elif query.data == "add_card":
        user_cards = game_state["cards"].get(user_id, [])
        if len(user_cards) >= 4:
            await query.answer("⚠️ እስከ 4 ካርቴላ ብቻ መያዝ ይችላሉ!", show_alert=True)
        elif balances[user_id] < GAME_COST:
            await query.answer("⚠️ አዲስ ካርቴላ ለመጨመር ቀሪ ሂሳብዎ በቂ አይደለም (10 ብር ያስፈልጋል)።", show_alert=True)
        else:
            balances[user_id] -= GAME_COST
            user_cards.append(generate_bingo_card())
            game_state["marked"][user_id].append(set())
            user_active_card[user_id] = len(user_cards) - 1
            await query.answer("✅ አዲስ ካርቴላ ተጨምሯል!", show_alert=False)
        
        await render_bingo_board(query, user_id)

    elif query.data.startswith("switch_card_"):
        card_idx = int(query.data.split("_")[2])
        user_active_card[user_id] = card_idx
        await render_bingo_board(query, user_id)

    elif query.data.startswith("cell_"):
        parts = query.data.split("_")
        # format: cell_{card_idx}_{num}
        if len(parts) == 3 and parts[2].isdigit():
            card_idx = int(parts[1])
            num = int(parts[2])
            if num in game_state["called_numbers"]:
                game_state["marked"][user_id][card_idx].add(num)
                await query.answer(f"ቁጥር {num} ተይዟል! ✅", show_alert=False)
            else:
                await query.answer(f"ይህ ቁጥር ({num}) ገና አልተጠራም!", show_alert=True)
        
        await render_bingo_board(query, user_id)

    elif query.data == "claim_bingo":
        active_idx = user_active_card.get(user_id, 0)
        card = game_state["cards"].get(user_id, [[]])[active_idx]
        marked = game_state["marked"].get(user_id, [set()])[active_idx]
        win_type = check_win(card, marked)
        
        if win_type:
            reward = 500.0
            balances[user_id] += reward
            game_state["winners"].append(user_id)
            keyboard = [[InlineKeyboardButton("🔙 ወደ ዋናው ምናሌ", callback_data="main_menu")]]
            await query.edit_message_text(
                f"🎉 **እንኳን ደስ አለዎት! አሸንፈዋል!** 🎉\n\n"
                f"የተረጋገጠው የጨዋታ አይነት: **{win_type}**\n"
                f"🎁 ሽልማቱ **{reward} ብር** ወደ ቀሪ ሂሳብዎ ተጨምሯል!",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )
        else:
            await query.answer("እስካሁን የቢንጎ መስፈርት አልተሟላም! ተጨማሪ ቁጥሮችን ይጠብቁ።", show_alert=True)

    elif query.data == "main_menu":
        await show_main_menu_message(query, user_id, is_new_message=False)

async def render_bingo_board(query, user_id):
    cards = game_state["cards"].get(user_id, [])
    marked_list = game_state["marked"].get(user_id, [])
    active_idx = user_active_card.get(user_id, 0)
    
    if active_idx >= len(cards):
        active_idx = 0
        user_active_card[user_id] = 0

    card = cards[active_idx]
    marked = marked_list[active_idx]

    keyboard = []
    
    # ካርቴላዎች መቀያየሪያ ቁልፎች (እስከ 4 ካርዶች)
    card_switch_row = []
    for idx in range(len(cards)):
        btn_text = f"{'🟢' if idx == active_idx else '⚪'} ካርድ {idx + 1}"
        card_switch_row.append(InlineKeyboardButton(btn_text, callback_data=f"switch_card_{idx}"))
    keyboard.append(card_switch_row)

    # የ B-I-N-G-O ራስጌ
    header_row = [
        InlineKeyboardButton("B", callback_data="noop"),
        InlineKeyboardButton("I", callback_data="noop"),
        InlineKeyboardButton("N", callback_data="noop"),
        InlineKeyboardButton("G", callback_data="noop"),
        InlineKeyboardButton("O", callback_data="noop")
    ]
    keyboard.append(header_row)

    for r_idx, row in enumerate(card):
        row_buttons = []
        for c_idx, val in enumerate(row):
            if val == "FREE":
                btn_text = "⭐ FREE"
                cb_data = f"cell_{active_idx}_free_{r_idx}_{c_idx}"
            else:
                status = "✅" if val in marked else "⬜"
                btn_text = f"{status}{val}"
                cb_data = f"cell_{active_idx}_{val}"
            row_buttons.append(InlineKeyboardButton(btn_text, callback_data=cb_data))
        keyboard.append(row_buttons)

    # ተጨማሪ ካርድ መግዣ አዝማም (ከ 4 በታች ከሆነ)
    if len(cards) < 4:
        keyboard.append([InlineKeyboardButton("➕ ተጨማሪ ካርድ ጨምር (10 ብር)", callback_data="add_card")])

    keyboard.append([InlineKeyboardButton("📢 ቢንጎ (Bingo!) አሳውቅ", callback_data="claim_bingo")])
    keyboard.append([InlineKeyboardButton("🔙 ወደ ዋናው ምናሌ", callback_data="main_menu")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    active_players_count = len(game_state["players"])
    current_num_display = game_state["current_number"] if game_state["current_number"] else "እየጠበቀ ነው..."

    try:
        await query.edit_message_text(
            f"🎮 **የሀበሻ ቢንጎ ጨዋታ በሂደት ላይ ነው!**\n\n"
            f"🔊 የተጠራው ቁጥር: **{current_num_display}**\n"
            f"👥 LIVE የተጫዋቾች ብዛት: **{active_players_count}/{MAX_PLAYERS}**\n"
            f"💳 የእርስዎ ቀሪ ሂሳብ: **{balances[user_id]} ብር** | የያዙት ካርድ: **{active_idx + 1} / {len(cards)}**\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"ቁጥሮቹን እየመረጡ ይጫወቱ!",
            reply_markup=reply_markup,
            parse_mode="Markdown"
        )
    except Exception:
        pass

# ----------------- አውቶማቲክ ቁጥር ጥሪ የጀርባ ስራ -----------------
async def bingo_number_caller():
    while True:
        await asyncio.sleep(5)
        if len(game_state["players"]) > 0:
            available_nums = [n for n in range(1, 76) if n not in game_state["called_numbers"]]
            if available_nums:
                called = random.choice(available_nums)
                game_state["called_numbers"].append(called)
                game_state["current_number"] = called
                logger.info(f"ቢንጎ ቁጥር ተጠራ: {called} (አጠቃላይ ተጫዋቾች: {len(game_state['players'])})")
            else:
                game_state["called_numbers"] = []
                game_state["current_number"] = None

asyncio.run_coroutine_threadsafe(bingo_number_caller(), main_loop)

application.add_handler(CommandHandler("start", start))
application.add_handler(MessageHandler(filters.CONTACT, contact_handler))
application.add_handler(CallbackQueryHandler(button_handler))

@app.route('/')
def home():
    return "Telegram Habesha Bingo Bot is running via Webhook on Render!"

@app.route(f'/{TOKEN}', methods=['POST'])
def webhook():
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
