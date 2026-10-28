import os
import logging
import asyncio
import threading
import random
import sqlite3
from contextlib import closing
from flask import Flask, request
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# Besh Besh Bingo — virtual-credit bingo bot.
# Never put your Telegram bot token directly in source code.
TOKEN = os.environ.get("TOKEN", "").strip()
PUBLIC_URL = os.environ.get("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
WEBHOOK_SECRET_PATH = os.environ.get("WEBHOOK_SECRET_PATH", "").strip().strip("/")
LIVE_CHAT_ID = os.environ.get("LIVE_CHAT_ID", "").strip()
DATABASE_PATH = os.environ.get("DATABASE_PATH", "bingo.sqlite3")
# Ensure the database folder exists (important when using a mounted disk such as /var/data).
os.makedirs(os.path.dirname(os.path.abspath(DATABASE_PATH)), exist_ok=True)

MAX_PLAYERS = 500
CARD_COST = 10
STARTING_CREDITS = 100
MAX_CARDS_PER_PLAYER = 4
CALL_INTERVAL_SECONDS = 5  # live number is published every 5 seconds

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("besh-besh-bingo")

if not TOKEN:
    raise RuntimeError("Set the TOKEN environment variable to your new Telegram bot token.")
if not PUBLIC_URL:
    logger.warning("RENDER_EXTERNAL_URL is not set; webhook setup will be skipped.")

app = Flask(__name__)
loop = asyncio.new_event_loop()
application = Application.builder().token(TOKEN).build()

# In-memory round state. User credits and cards are stored in SQLite.
state_lock = threading.RLock()
game_state = {
    "round": 1,
    "prize": 0,
    "is_active": False,
    "called_numbers": [],
    "current_number": None,
    "players": set(),
    "cards": {},       # user_id -> list of 5x5 card matrices
    "marked": {},      # user_id -> list of sets
    "winners": set(),  # one claim per user per round
}


def db_connection():
    conn = sqlite3.connect(DATABASE_PATH, timeout=20)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            credits INTEGER NOT NULL DEFAULT 100
        )"""
    )
    return conn


def ensure_user(user_id):
    with closing(db_connection()) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO users (user_id, credits) VALUES (?, ?)",
            (user_id, STARTING_CREDITS),
        )
        conn.commit()


def get_credits(user_id):
    ensure_user(user_id)
    with closing(db_connection()) as conn:
        row = conn.execute(
            "SELECT credits FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        return int(row[0]) if row else 0


def change_credits(user_id, delta):
    ensure_user(user_id)
    with closing(db_connection()) as conn:
        conn.execute(
            "UPDATE users SET credits = credits + ? WHERE user_id = ?",
            (int(delta), user_id),
        )
        conn.commit()
        row = conn.execute(
            "SELECT credits FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        return int(row[0]) if row else 0


def spend_credits(user_id, amount):
    ensure_user(user_id)
    with closing(db_connection()) as conn:
        cur = conn.execute(
            "UPDATE users SET credits = credits - ? "
            "WHERE user_id = ? AND credits >= ?",
            (amount, user_id, amount),
        )
        conn.commit()
        return cur.rowcount == 1


def generate_bingo_card():
    columns = [
        random.sample(range(1, 16), 5),
        random.sample(range(16, 31), 5),
        random.sample(range(31, 46), 5),
        random.sample(range(46, 61), 5),
        random.sample(range(61, 76), 5),
    ]
    card = [[columns[c][r] for c in range(5)] for r in range(5)]
    card[2][2] = "FREE"
    return card


def check_win(card, marked):
    size = 5
    for r in range(size):
        if all(card[r][c] == "FREE" or card[r][c] in marked for c in range(size)):
            return "ረድፍ"
    for c in range(size):
        if all(card[r][c] == "FREE" or card[r][c] in marked for r in range(size)):
            return "ዓምድ"
    if all(card[i][i] == "FREE" or card[i][i] in marked for i in range(size)):
        return "ሰያፍ"
    if all(card[i][size - 1 - i] == "FREE" or card[i][size - 1 - i] in marked for i in range(size)):
        return "ሰያፍ"
    corners = [(0, 0), (0, 4), (4, 0), (4, 4)]
    if all(card[r][c] == "FREE" or card[r][c] in marked for r, c in corners):
        return "ክሮነር"
    numbers = [card[r][c] for r in range(5) for c in range(5) if card[r][c] != "FREE"]
    if all(number in marked for number in numbers):
        return "ሙሉ ካርድ"
    return None


def main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎫 ቢንጎ ተጫወት", callback_data="play")],
        [InlineKeyboardButton("💳 የሂሳብ አከፋፈል", callback_data="balance")],
        [InlineKeyboardButton("ℹ️ የቢንጎ መመሪያ", callback_data="rules")],
    ])


def main_text(user_id):
    return (
        "🎯 *BESH BESH BINGO*\n\n"
        f"💳 የሂሳብ መጠኖች: *{get_credits(user_id)}*\n"
        f"🔄 ዙር: *#{game_state['round']}*\n"
        f"🏆 ድምአልሽ ሽልማት: *{game_state['prize']} ነጥብ*\n\n"
        "ከታች ያሉትን አማራጮች ይምረጡ።\n"
        "_ይህ ሰርቪስ የሂሳብ ነጥብ ብቻ ይጫወታል።_"
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ensure_user(user_id)
    await update.effective_message.reply_text(
        main_text(user_id), reply_markup=main_keyboard(), parse_mode="Markdown"
    )


async def show_menu(query, user_id):
    await query.edit_message_text(
        main_text(user_id), reply_markup=main_keyboard(), parse_mode="Markdown"
    )


def build_board_payload(user_id):
    """Build the current personal card view for button replies and live updates."""
    with state_lock:
        cards = game_state["cards"].get(user_id, [])
        marks = game_state["marked"].get(user_id, [])
        if not cards:
            return (
                "ካርድ አልዎትም፤ እባክዎ ከታች ጀምሮ ቢንጎውን ይምረጡ።",
                main_keyboard(),
                False,
            )

        idx = min(max(0, contextless_active_card(user_id)), len(cards) - 1)
        card = cards[idx]
        marked = marks[idx]
        called_count = len(game_state["called_numbers"])
        current = game_state["current_number"]
        player_count = len(game_state["players"])
        round_num = game_state["round"]
        prize = game_state["prize"]
        called = list(game_state["called_numbers"])

    keyboard = []
    keyboard.append([
        InlineKeyboardButton(
            ("🟢 " if i == idx else "⚪ ") + f"ካርድ {i + 1}",
            callback_data=f"switch:{i}",
        )
        for i in range(len(cards))
    ])
    keyboard.append([
        InlineKeyboardButton(letter, callback_data="noop")
        for letter in ["B", "I", "N", "G", "O"]
    ])
    for r, row in enumerate(card):
        row_buttons = []
        for c, value in enumerate(row):
            if value == "FREE":
                label = "⭐ FREE"
            else:
                label = ("✅" if value in marked else "⬜") + str(value)
            row_buttons.append(
                InlineKeyboardButton(label, callback_data=f"cell:{idx}:{value}")
            )
        keyboard.append(row_buttons)

    extras = []
    if len(cards) < MAX_CARDS_PER_PLAYER:
        extras.append(InlineKeyboardButton(f"➕ ካርድ (+{CARD_COST} ነጥብ)", callback_data="add"))
    extras.append(InlineKeyboardButton("🔄 አድስ", callback_data="refresh"))
    keyboard.append(extras)
    keyboard.append([InlineKeyboardButton("📢 BINGO! አሁት", callback_data="claim")])
    keyboard.append([InlineKeyboardButton("🏠 ኋላ መሺ", callback_data="menu")])

    text = (
        "🎯 *BESH BESH BINGO*\n\n"
        f"🔄 ዙር: *#{round_num}* | 🎫 ካርድ: *{idx + 1}/{len(cards)}*\n"
        f"🏆 ድምአልሽ ሽልማት: *{prize} ነጥብ*\n"
        f"🔊 የመጠሪያ ቁጥር: *{current if current is not None else 'ጊዜ አልተደረቀም'}*\n"
        f"⚪ የተጠሩ ሳዎች: *{called_count}/75*\n"
        f"👥 ተጫዋቾች: *{player_count}/{MAX_PLAYERS}*\n\n"
        "ቁጥር ሲጠራ በአልባ ላይ ያለውን ቁጥር ይጫኑ።\n"
        f"📋 የተጠሩ ቁጥሮች: {', '.join(map(str, called[-15:])) or '—'}"
    )
    return text, InlineKeyboardMarkup(keyboard), True


async def render_board(query, user_id):
    text, markup, has_card = build_board_payload(user_id)
    await query.edit_message_text(text, reply_markup=markup, parse_mode="Markdown")
    if has_card and query.message:
        private_board_messages[user_id] = (query.message.chat_id, query.message.message_id)


active_cards = {}
live_message_id = None
private_board_messages = {}
private_update_lock = asyncio.Lock()


def contextless_active_card(user_id):
    return active_cards.get(user_id, 0)


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    ensure_user(user_id)
    data = query.data or ""

    if data == "noop":
        await query.answer()
        return
    if data in ("menu", "balance", "rules"):
        await query.answer()
        if data == "balance":
            await query.edit_message_text(
                f"💳 *የሂሳብ መጠኖችዎ: {get_credits(user_id)}*\n\n"
                "ነጥብ ለዚህ ጨዋታ ብቻ የሚውል ነው።",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("🔙 ኋላ መሺ", callback_data="menu")]]
                ),
                parse_mode="Markdown",
            )
        elif data == "rules":
            await query.edit_message_text(
                "📖 *የቢንጎ መመሪያ*\n\n"
                f"• ካርድ ዋጋ: {CARD_COST} ድምአልሽ ነጥብ\n"
                "• ቁጥሮች በሰልፍ-ሰልፍ ይለዋሉ።\n"
                "• የተጠራ ቁጥር በካርዶችዎ ላይ ይለብቃል።\n"
                "• አዕምድ፣ ሰያፍ ወይም ክሮነር ሲሞላ BINGO ይለብቃል።\n"
                "• እስከ 4 ካርዶች መጫወት ይችላሉ።\n"
                "• እውነተኛ ገንዘብ ወይም ሽልማት አያከፍቱም።",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("🔙 ኋላ መሺ", callback_data="menu")]]
                ),
                parse_mode="Markdown",
            )
        else:
            await show_menu(query, user_id)
        return

    if data == "play":
        with state_lock:
            if user_id not in game_state["players"] and len(game_state["players"]) >= MAX_PLAYERS:
                await query.answer("የቢንጎው ቦታ ሞልቷል።", show_alert=True)
                return
            has_card = bool(game_state["cards"].get(user_id))
            if not has_card:
                if not spend_credits(user_id, CARD_COST):
                    await query.answer("በቂ ነጥብ የለዎትም።", show_alert=True)
                    return
                game_state["cards"][user_id] = [generate_bingo_card()]
                game_state["marked"][user_id] = [set()]
                game_state["players"].add(user_id)
                active_cards[user_id] = 0
                game_state["prize"] += CARD_COST
                game_state["is_active"] = True
        await query.answer()
        await publish_live_board()
        await render_board(query, user_id)
        return

    if data == "add":
        with state_lock:
            cards = game_state["cards"].get(user_id, [])
            if len(cards) >= MAX_CARDS_PER_PLAYER:
                await query.answer("እስከ 4 ካርዶች ብቻ ይጫወቱ።", show_alert=True)
                return
            if not spend_credits(user_id, CARD_COST):
                await query.answer("በቂ ነጥብ የለዎትም።", show_alert=True)
                return
            cards.append(generate_bingo_card())
            game_state["marked"][user_id].append(set())
            active_cards[user_id] = len(cards) - 1
            game_state["prize"] += CARD_COST
        await query.answer("ካርድ ተጨምሯል።")
        await publish_live_board()
        await render_board(query, user_id)
        return

    if data.startswith("switch:"):
        try:
            idx = int(data.split(":", 1)[1])
        except ValueError:
            return
        if 0 <= idx < len(game_state["cards"].get(user_id, [])):
            active_cards[user_id] = idx
        await query.answer()
        await render_board(query, user_id)
        return

    if data in ("refresh",):
        await query.answer()
        await render_board(query, user_id)
        return

    if data.startswith("cell:"):
        parts = data.split(":")
        if len(parts) != 3:
            return
        try:
            idx = int(parts[1])
        except ValueError:
            return
        if idx >= len(game_state["cards"].get(user_id, [])) or idx < 0:
            await query.answer("ይህ ካርድ አይገኝም።", show_alert=True)
            return
        value = parts[2]
        if value == "FREE":
            await query.answer("FREE ሐል በራስ ሰር ይፈቀዳል።")
            return
        try:
            number = int(value)
        except ValueError:
            return
        with state_lock:
            if number not in game_state["called_numbers"]:
                await query.answer("ይህ ቁጥር ጊዜ አልተደረቀም።", show_alert=True)
                return
            game_state["marked"][user_id][idx].add(number)
            active_cards[user_id] = idx
        await query.answer(f"ቁጥር {number} ተመርጧል።")
        await render_board(query, user_id)
        return

    if data == "claim":
        with state_lock:
            cards = game_state["cards"].get(user_id, [])
            marks = game_state["marked"].get(user_id, [])
            idx = active_cards.get(user_id, 0)
            if not cards or idx >= len(cards) or idx >= len(marks):
                await query.answer("ማስተማሪያ ካርድ ይጫወቱ።", show_alert=True)
                return
            if user_id in game_state["winners"]:
                await query.answer("በዚህ ዙር አስቀድመው አሸንፈዋል።", show_alert=True)
                return
            win_type = check_win(cards[idx], marks[idx])
            if not win_type:
                await query.answer("የቢንጎ መስርተ ጊዜ አልተሞላም።", show_alert=True)
                return
            reward = max(0, game_state["prize"])
            change_credits(user_id, reward)
            game_state["round"] += 1
            game_state["prize"] = 0
            game_state["is_active"] = False
            game_state["called_numbers"] = []
            game_state["current_number"] = None
            game_state["players"] = set()
            game_state["cards"] = {}
            game_state["marked"] = {}
            game_state["winners"] = set()
            active_cards.clear()
            private_board_messages.pop(user_id, None)
            await publish_live_board()
            await query.edit_message_text(
                "🎉 *BINGO! አሸንፈዋል!*\n\n"
                f"🏆 የአሸናፊነት ዓይነት: *{win_type}*\n"
                f"🎁 የተጨመረው ድምአልሽ ሽልማት: *{reward} ነጥብ*\n"
                f"💳 አዲስ ቀሪ ነጥብ: *{get_credits(user_id)}*",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("🏠 ኋላ መሺ", callback_data="menu")]]
                ),
                parse_mode="Markdown",
            )
            await publish_private_boards()
        return


async def publish_live_board():
    global live_message_id
    if not LIVE_CHAT_ID:
        return
    with state_lock:
        round_num = game_state["round"]
        current = game_state["current_number"]
        called = list(game_state["called_numbers"])
        players = len(game_state["players"])
        prize = game_state["prize"]
        active = game_state["is_active"]
    text = (
        "🎯 *BESH BESH BINGO — LIVE*\n\n"
        f"🔄 ዙር: *#{round_num}*\n"
        f"🔊 አሁን የተደረቀው ቁጥር: *{current if current is not None else 'ጊዜ አልተደረቀም'}*\n"
        f"📋 የተጠሩ ቁጥሮች ({len(called)}/75): {', '.join(map(str, called)) or '—'}\n"
        f"👥 ተጫዋቾች: *{players}/{MAX_PLAYERS}*\n"
        f"🏆 ድምአልሽ ሽልማት: *{prize} ነጥብ*\n"
        f"📡 ኹኔታ: *{'LIVE' if active else 'ተቋንቋ'}*\n\n"
        "በግል ፔት ውስጥ ካርዶችዎን ይከታተሉ።"
    )
    try:
        chat_id = int(LIVE_CHAT_ID)
        if live_message_id is None:
            message = await application.bot.send_message(
                chat_id=chat_id, text=text, parse_mode="Markdown"
            )
            live_message_id = message.message_id
        else:
            try:
                await application.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=live_message_id,
                    text=text,
                    parse_mode="Markdown",
                )
            except Exception as exc:
                logger.warning("Could not edit live board; creating a new one: %s", exc)
                message = await application.bot.send_message(
                    chat_id=chat_id, text=text, parse_mode="Markdown"
                )
                live_message_id = message.message_id
    except Exception:
        logger.exception("Failed to publish live board. Check LIVE_CHAT_ID and bot permissions.")


async def publish_private_boards():
    async with private_update_lock:
        with state_lock:
            recipients = list(private_board_messages.items())
        for user_id, (chat_id, message_id) in recipients:
            text, markup, has_card = build_board_payload(user_id)
            if not has_card:
                try:
                    await application.bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=message_id,
                        text="🎯 *BESH BESH BINGO*\n\nዙር ተጠናቋል፤ እንደገና ለማለማት ኋላ መሺ ይበሉ።",
                        reply_markup=main_keyboard(),
                        parse_mode="Markdown",
                    )
                except Exception as exc:
                    logger.debug("Could not clear ended private board for user %s: %s", user_id, exc)
                private_board_messages.pop(user_id, None)
                await asyncio.sleep(0.05)
                continue
            try:
                await application.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=text,
                    reply_markup=markup,
                    parse_mode="Markdown",
                )
            except Exception as exc:
                message = str(exc).lower()
                if "message is not modified" in message:
                    pass
                elif "message to edit not found" in message or "chat not found" in message:
                    private_board_messages.pop(user_id, None)
                    logger.info("Removed unavailable private board for user %s", user_id)
                else:
                    logger.warning("Could not refresh private board for user %s: %s", user_id, exc)
            await asyncio.sleep(0.05)


async def number_caller():
    while True:
        await asyncio.sleep(CALL_INTERVAL_SECONDS)
        with state_lock:
            if not game_state["players"] or not game_state["is_active"]:
                continue
            available = [
                n for n in range(1, 76)
                if n not in game_state["called_numbers"]
            ]
            if available:
                number = random.choice(available)
                game_state["called_numbers"].append(number)
                game_state["current_number"] = number
                logger.info("Round %s called number %s", game_state["round"], number)
            else:
                game_state["round"] += 1
                game_state["called_numbers"] = []
                game_state["current_number"] = None
                game_state["winners"] = set()
                game_state["cards"] = {}
                game_state["marked"] = {}
                game_state["players"] = set()
                active_cards.clear()
                game_state["prize"] = 0
                game_state["is_active"] = False
        await publish_live_board()
        await publish_private_boards()


async def initialize_bot():
    await application.initialize()
    await application.start()
    applica
