# -*- coding: utf-8 -*-
"""
Besh Besh Bingo â€” Telegram bot + Telegram Mini App
Rebuilt as a complete main.py from the code shared in chat.

Environment variables:
  TOKEN                 Required Telegram bot token
  RENDER_EXTERNAL_URL   Render public URL, e.g. https://your-service.onrender.com
  WEBHOOK_SECRET_PATH   Optional random webhook path secret
  ADMIN_IDS             Comma-separated Telegram user IDs
  DATABASE_PATH         Optional SQLite path (default: bingo.sqlite3)
  LIVE_CHAT_ID          Optional Telegram chat ID for announcements
"""
import os
import json
import time
import hmac
import hashlib
import logging
import asyncio
import threading
import random
import sqlite3
from urllib.parse import parse_qsl
from contextlib import closing
from datetime import datetime, timezone

from flask import Flask, request, Response, jsonify
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo,
    ReplyKeyboardMarkup, KeyboardButton,
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    ContextTypes, filters,
)

# --------------------------- Configuration ---------------------------
TOKEN = os.environ.get("TOKEN", "").strip()
PUBLIC_URL = os.environ.get("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
WEBHOOK_SECRET_PATH = os.environ.get("WEBHOOK_SECRET_PATH", "").strip().strip("/")
LIVE_CHAT_ID = os.environ.get("LIVE_CHAT_ID", "").strip()
ADMIN_IDS = {
    int(x.strip()) for x in os.environ.get("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}
DATABASE_PATH = os.environ.get("DATABASE_PATH", "bingo.sqlite3")
os.makedirs(os.path.dirname(os.path.abspath(DATABASE_PATH)), exist_ok=True)

MAX_PLAYERS = 500
CARD_COST = 10
STARTING_CREDITS = 100
MAX_CARDS_PER_PLAYER = 4
CALL_INTERVAL_SECONDS = 5
ROUND_PAUSE_SECONDS = 8

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("besh-besh-bingo")

if not TOKEN:
    raise RuntimeError("Set the TOKEN environment variable to your Telegram bot token.")

app = Flask(__name__)
bot_application = Application.builder().token(TOKEN).build()
bot_loop = asyncio.new_event_loop()
bot_thread = None
game_lock = threading.RLock()

# ------------------------------ Database -----------------------------
def db():
    conn = sqlite3.connect(DATABASE_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with closing(db()) as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT NOT NULL DEFAULT '',
            first_name TEXT NOT NULL DEFAULT '',
            credits INTEGER NOT NULL DEFAULT 100,
            language TEXT NOT NULL DEFAULT 'am',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS deposit_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            method TEXT NOT NULL,
            amount INTEGER NOT NULL,
            reference TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            reviewed_at TEXT,
            reviewed_by INTEGER
        );
        CREATE TABLE IF NOT EXISTS wins (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            username TEXT NOT NULL DEFAULT '',
            round_no INTEGER NOT NULL,
            card_index INTEGER NOT NULL,
            prize INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS game_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """)
        con.commit()

def get_user(user_id):
    with closing(db()) as con:
        return con.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()

def ensure_user(user_id, username="", first_name=""):
    with closing(db()) as con:
        con.execute("""
            INSERT INTO users(user_id, username, first_name, credits)
            VALUES(?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET
              username=excluded.username, first_name=excluded.first_name
        """, (int(user_id), username or "", first_name or "", STARTING_CREDITS))
        con.commit()

def update_credits(user_id, delta):
    with closing(db()) as con:
        con.execute("UPDATE users SET credits=credits+? WHERE user_id=?", (int(delta), int(user_id)))
        con.commit()
        row = con.execute("SELECT credits FROM users WHERE user_id=?", (int(user_id),)).fetchone()
        return int(row["credits"]) if row else 0

def set_language(user_id, language):
    with closing(db()) as con:
        con.execute("UPDATE users SET language=? WHERE user_id=?", (language, int(user_id)))
        con.commit()

def get_language(user_id):
    row = get_user(user_id)
    return row["language"] if row else "am"

init_db()

# ------------------------------ Game state ---------------------------
def fresh_game():
    return {
        "round": 1, "prize": 0, "is_active": False,
        "called_numbers": [], "current_number": None,
        "players": set(), "cards": {}, "marked": {}, "winner_ids": set(),
        "started_at": None,
    }

game_state = fresh_game()

def make_card():
    # Standard 5x5 bingo card with FREE center.
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

def all_cards(user_id):
    return game_state["cards"].get(int(user_id), [])

def marked_set(user_id, card_index):
    key = (int(user_id), int(card_index))
    if key not in game_state["marked"]:
        game_state["marked"][key] = {"FREE"}
    return game_state["marked"][key]

def card_has_bingo(card, marked):
    marked = set(marked) | {"FREE"}
    # rows, columns, both diagonals
    for r in range(5):
        if all(card[r][c] in marked for c in range(5)):
            return True
    for c in range(5):
        if all(card[r][c] in marked for r in range(5)):
            return True
    if all(card[i][i] in marked for i in range(5)):
        return True
    if all(card[i][4-i] in marked for i in range(5)):
        return True
    return False

def call_next_number():
    with game_lock:
        remaining = [n for n in range(1, 76) if n not in game_state["called_numbers"]]
        if not remaining:
            return None
        number = random.choice(remaining)
        game_state["called_numbers"].append(number)
        game_state["current_number"] = number
        return number

def auto_caller_loop():
    """Call a new number periodically while a round is active."""
    while True:
        try:
            time.sleep(CALL_INTERVAL_SECONDS)
            with game_lock:
                active = game_state["is_active"]
                already_won = bool(game_state["winner_ids"])
            if not active:
                continue
            number = call_next_number()
            if number is None:
                with game_lock:
                    game_state["is_active"] = False
                continue
            # Stop after a full card pool of numbers; next purchase starts a new round.
            if len(game_state["called_numbers"]) >= 75:
                with game_lock:
                    game_state["is_active"] = False
        except Exception:
            logger.exception("Auto caller failed")
            time.sleep(1)

def reset_round():
    with game_lock:
        game_state["round"] += 1
        game_state["prize"] = 0
        game_state["is_active"] = False
        game_state["called_numbers"] = []
        game_state["current_number"] = None
        game_state["players"] = set()
        game_state["cards"] = {}
        game_state["marked"] = {}
        game_state["winner_ids"] = set()
        game_state["started_at"] = None

def active_player_count():
    return len(game_state["players"])

def number_label(n):
    if n == "FREE":
        return "FREE"
    if n <= 15:
        return f"B{n}"
    if n <= 30:
        return f"I{n}"
    if n <= 45:
        return f"N{n}"
    if n <= 60:
        return f"G{n}"
    return f"O{n}"

# ------------------------------ Localization -------------------------
TEXT = {
    "am": {
        "welcome": "ðŸŽ± áŠ¥áŠ•áŠ³áŠ• á‹ˆá‹° Besh Besh Bingo á‰ á‹°áˆ…áŠ“ áˆ˜áŒ¡!\n\ná‹­áˆ… á‰ á‰¨áˆ­á‰¹á‹‹áˆ áŠ­áˆ¬á‹²á‰µ á‹¨áˆšáˆ°áˆ« á‹¨á‰¢áŠ•áŒŽ áŒ¨á‹‹á‰³ áŠá‹á¢",
        "play": "ðŸŽ® á‰¢áŠ•áŒŽ á‰°áŒ«á‹ˆá‰µ",
        "balance": "ðŸ’° á‰€áˆª áˆ‚áˆ³á‰¥",
        "rules": "ðŸ“œ áˆ…áŒ áŠ¥áŠ“ á‹°áŠ•á‰¦á‰½",
        "wallet": "ðŸ’³ Wallet / áŠ­áˆ¬á‹²á‰µ",
        "rank": "ðŸ† á‹°áˆ¨áŒƒ",
        "language": "ðŸŒ á‰‹áŠ•á‰‹ / Language",
        "support": "ðŸ†˜ áŠ¥áŒˆá‹›",
        "admin": "ðŸ›¡ áŠ á‹µáˆšáŠ•",
        "choose": "áŠ¨á‰³á‰½ áŠ«áˆ‰á‰µ áŠ áˆ›áˆ«áŒ®á‰½ á‹­áˆáˆ¨áŒ¡á¢",
        "balance_msg": "ðŸ’° á‰€áˆª áŠ­áˆ¬á‹²á‰µá‹Ž: {credits}",
        "rules_msg": "ðŸ“œ áˆ…áŒŽá‰½\nâ€¢ áŠ«áˆ­á‹µ á‹‹áŒ‹: 10 áŠ­áˆ¬á‹²á‰µ\nâ€¢ á‰ áŠ áŠ•á‹µ á‰°áŒ«á‹‹á‰½ áŠ¥áˆµáŠ¨ 4 áŠ«áˆ­á‹¶á‰½\nâ€¢ á‰ á‰°áŒ áˆ© á‰áŒ¥áˆ®á‰½ áˆ‹á‹­ á‰¥á‰» áˆáˆáŠ­á‰µ á‹«á‹µáˆ­áŒ‰\nâ€¢ áŠ­áˆ¬á‹²á‰¶á‰½ á‰¨áˆ­á‰¹á‹‹áˆ áŠ“á‰¸á‹á¤ á‹¨áŒˆáŠ•á‹˜á‰¥ á‹‹áŒ‹ á‹¨áˆ‹á‰¸á‹áˆá¢",
        "no_credit": "á‰ á‰‚ áŠ­áˆ¬á‹²á‰µ á‹¨áˆˆá‹Žá‰µáˆá¢",
        "bought": "âœ… áŠ«áˆ­á‹µ á‰°áŒˆá‹á‰·áˆá¢",
        "language_set": "á‰‹áŠ•á‰‹á‹ á‹ˆá‹° áŠ áˆ›áˆ­áŠ› á‰°á‰€á‹­áˆ¯áˆá¢",
    },
    "en": {
        "welcome": "ðŸŽ± Welcome to Besh Besh Bingo!\n\nThis is a virtual-credit bingo game.",
        "play": "ðŸŽ® Play Bingo",
        "balance": "ðŸ’° Balance",
        "rules": "ðŸ“œ Rules",
        "wallet": "ðŸ’³ Wallet / Credits",
        "rank": "ðŸ† Rank",
        "language": "ðŸŒ Language / á‰‹áŠ•á‰‹",
        "support": "ðŸ†˜ Help",
        "admin": "ðŸ›¡ Admin",
        "choose": "Choose an option below.",
        "balance_msg": "ðŸ’° Your balance: {credits} credits",
        "rules_msg": "ðŸ“œ Rules\nâ€¢ Card cost: 10 credits\nâ€¢ Up to 4 cards per player\nâ€¢ Only called numbers can be marked\nâ€¢ Credits are virtual and have no cash value.",
        "no_credit": "You do not have enough credits.",
        "bought": "âœ… Card purchased.",
        "language_set": "Language changed to English.",
    },
}

def tr(user_id, key, **kwargs):
    lang = get_language(user_id)
    return TEXT.get(lang, TEXT["am"]).get(key, TEXT["am"].get(key, key)).format(**kwargs)

def main_keyboard(user_id):
    lang = get_language(user_id)
    t = TEXT.get(lang, TEXT["am"])
    return ReplyKeyboardMarkup([
        [KeyboardButton(t["play"]), KeyboardButton(t["balance"])],
        [KeyboardButton(t["wallet"]), KeyboardButton(t["rank"])],
        [KeyboardButton(t["rules"]), KeyboardButton(t["language"])],
        [KeyboardButton(t["support"])],
    ], resize_keyboard=True)

def webapp_url():
    return PUBLIC_URL + "/"

def start_keyboard():
    if PUBLIC_URL:
        return InlineKeyboardMarkup([[
            InlineKeyboardButton("ðŸŽ® Open Bingo / á‰¢áŠ•áŒŽ áŠ­áˆá‰µ", web_app=WebAppInfo(url=webapp_url()))
        ]])
    return None

# ---------------------------- Telegram handlers ----------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user.id, user.username, user.first_name)
    text = tr(user.id, "welcome") + "\n\n" + tr(user.id, "choose")
    markup = start_keyboard()
    if markup:
        await update.effective_message.reply_text(text, reply_markup=markup)
    await update.effective_message.reply_text("Besh Besh Bingo", reply_markup=main_keyboard(user.id))

async def language_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user.id, user.username, user.first_name)
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("áŠ áˆ›áˆ­áŠ›", callback_data="lang:am"),
        InlineKeyboardButton("English", callback_data="lang:en"),
    ]])
    await update.effective_message.reply_text("á‰‹áŠ•á‰‹ á‹­áˆáˆ¨áŒ¡ / Choose language:", reply_markup=keyboard)

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user.id, user.username, user.first_name)
    msg = (update.effective_message.text or "").strip()
    lang = get_language(user.id)
    t = TEXT.get(lang, TEXT["am"])
    if msg in (t["play"], "ðŸŽ® á‰¢áŠ•áŒŽ á‰°áŒ«á‹ˆá‰µ", "ðŸŽ® Play Bingo"):
        markup = start_keyboard()
        if markup:
            await update.effective_message.reply_text("Besh Besh Bingo", reply_markup=markup)
        else:
            await update.effective_message.reply_text("Set RENDER_EXTERNAL_URL to enable the Mini App.")
    elif msg in (t["balance"], "ðŸ’° á‰€áˆª áˆ‚áˆ³á‰¥", "ðŸ’° Balance"):
        row = get_user(user.id)
        await update.effective_message.reply_text(tr(user.id, "balance_msg", credits=row["credits"]))
    elif msg in (t["rules"], "ðŸ“œ áˆ…áŒ áŠ¥áŠ“ á‹°áŠ•á‰¦á‰½", "ðŸ“œ Rules"):
        await update.effective_message.reply_text(tr(user.id, "rules_msg"))
    elif msg in (t["wallet"], "ðŸ’³ Wallet / áŠ­áˆ¬á‹²á‰µ"):
        await update.effective_message.reply_text(
            "ðŸ’³ Wallet\nTelebirr: 0918719332 Â· Enyew\nCBE Birr: 0918719332 Â· Enyew\n\n"
            "áŠ­áá‹« áŠ¨áˆáŒ¸áˆ™ á‰ áŠ‹áˆ‹ Mini App á‹áˆµáŒ¥ á‹¨áŠ­áá‹« áŒ¥á‹«á‰„ á‹­áˆ‹áŠ©á¢ áŠ á‹µáˆšáŠ• áŠ¥áˆµáŠªá‹«áˆ¨áŒ‹áŒáŒ¥ áŠ­áˆ¬á‹²á‰µ áŠ á‹­áŒ¨áˆ˜áˆ­áˆá¢")
    elif msg in (t["rank"], "ðŸ† á‹°áˆ¨áŒƒ", "ðŸ† Rank"):
        with closing(db()) as con:
            rows = con.execute("SELECT username, first_name, credits FROM users ORDER BY credits DESC LIMIT 10").fetchall()
        lines = ["ðŸ† Top Players"]
        for i, r in enumerate(rows, 1):
            name = ("@" + r["username"]) if r["username"] else (r["first_name"] or "Player")
            lines.append(f"{i}. {name} â€” {r['credits']} pts")
        await update.effective_message.reply_text("\n".join(lines))
    elif msg in (t["language"], "ðŸŒ á‰‹áŠ•á‰‹ / Language"):
        await language_command(update, context)
    elif msg in (t["support"], "ðŸ†˜ áŠ¥áŒˆá‹›", "ðŸ†˜ Help"):
        await update.effective_message.reply_text("ðŸ†˜ áŠ¥áŒˆá‹› / Help\nContact the bot administrator for support.")
    else:
        await update.effective_message.reply_text(tr(user.id, "choose"), reply_markup=main_keyboard(user.id))

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        await update.effective_message.reply_text("â›” Unauthorized.")
        return
    if PUBLIC_URL:
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("ðŸ›¡ Open Admin Panel", web_app=WebAppInfo(url=PUBLIC_URL + "/admin"))
        ]])
        await update.effective_message.reply_text("ðŸ›¡ Admin panel", reply_markup=keyboard)
    else:
        await update.effective_message.reply_text("Admin panel unavailable until RENDER_EXTERNAL_URL is configured.")

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    ensure_user(user_id, query.from_user.username, query.from_user.first_name)
    if query.data.startswith("lang:"):
        lang = query.data.split(":", 1)[1]
        if lang in ("am", "en"):
            set_language(user_id, lang)
            await query.edit_message_text(TEXT[lang]["language_set"])
    elif query.data.startswith("buy:"):
        await query.edit_message_text("Open the Mini App to buy a card.")
    else:
        await query.edit_message_text("Unknown action.")

bot_application.add_handler(CommandHandler("start", start))
bot_application.add_handler(CommandHandler("language", language_command))
bot_application.add_handler(CommandHandler("admin", admin_command))
bot_application.add_handler(CallbackQueryHandler(callback_handler))
bot_application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

# ---------------------------- Mini App auth ---------------------------
def validate_telegram_init_data(init_data):
    """Validate Telegram WebApp initData. Return parsed user dict or None."""
    if not init_data:
        return None
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
        received_hash = pairs.pop("hash", None)
        if not received_hash:
            return None
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
        secret_key = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
        calculated = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calculated, received_hash):
            return None
        user_data = json.loads(pairs.get("user", "{}"))
        if not user_data.get("id"):
            return None
        auth_date = int(pairs.get("auth_date", "0"))
        if auth_date and time.time() - auth_date > 86400:
            return None
        return user_data
    except Exception:
        logger.exception("Failed to validate Telegram initData")
        return None

def api_user():
    data = validate_telegram_init_data(request.headers.get("X-Telegram-Init-Data", ""))
    if data:
        ensure_user(data["id"], data.get("username", ""), data.get("first_name", ""))
        return data
    # Local browser preview is allowed only when explicitly enabled.
    if os.environ.get("ALLOW_DEMO_MODE", "").lower() == "true":
        demo_id = int(request.headers.get("X-Demo-User", "999999"))
        ensure_user(demo_id, "demo", "Demo Player")
        return {"id": demo_id, "username": "demo", "first_name": "Demo Player"}
    return None

def api_admin():
    user = api_user()
    return user if user and int(user["id"]) in ADMIN_IDS else None

def json_error(message, status=400):
    return jsonify({"error": message}), status

# ------------------------------ HTML assets ---------------------------
INDEX_HTML = r"""<!doctype html>
<html lang="am"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no"><meta name="theme-color" content="#06180d"><title>Besh Besh Bingo</title><script src="https://telegram.org/js/telegram-web-app.js"></script><link rel="stylesheet" href="/static/style.css"></head>
<body><main class="app-shell">
<header class="topbar"><div class="brand-mark">ðŸŽ±</div><div><div class="eyebrow">HABESHA GAME</div><h1>BESH BESH <span>BINGO</span></h1></div><div class="live-pill"><i></i> LIVE</div></header>
<section class="wallet-panel"><div><div class="muted">á‹¨áŠ¥áˆ­áˆµá‹Ž á‰€áˆª áŠ­áˆ¬á‹²á‰µ</div><div class="balance"><span id="credits">â€”</span> <small>PTS</small></div></div><div class="round-box"><span class="muted">á‹™áˆ­</span><strong id="round">#1</strong></div></section>
<section class="stats-row"><div class="stat"><span>ðŸ† á‹¨á‹™áˆ© áˆ½áˆáˆ›á‰µ</span><strong id="prize">0 pts</strong></div><div class="stat"><span>ðŸ‘¥ á‰°áŒ«á‹‹á‰¾á‰½</span><strong id="players">0</strong></div></section>
<section class="draw-panel"><div><span class="muted">á‹¨á‰°áŒ áˆ«á‹ á‰áŒ¥áˆ­</span><div class="last-number" id="current">â€”</div></div><div class="draw-meta"><span class="muted">á‹¨á‰°áŒ áˆ© á‰áŒ¥áˆ®á‰½</span><strong id="called-count">0 / 75</strong><div class="number-strip" id="recent"></div></div></section>
<section class="game-panel"><div class="section-heading"><div><span class="eyebrow">YOUR GAME</span><h2>á‹¨á‰¢áŠ•áŒŽ áŠ«áˆ­á‹µ</h2></div><span class="card-count" id="card-count">0 / 4</span></div>
<div id="card-tabs" class="card-tabs"></div><div class="bingo-grid" id="bingo-grid"><div class="loading">áŒ¨á‹‹á‰³á‹ á‰ áˆ˜áŒ«áŠ• áˆ‹á‹­ áŠá‹â€¦</div></div>
<div class="legend"><span><i class="dot called"></i> á‹¨á‰°áŒ áˆ«</span><span><i class="dot marked"></i> á‹¨á‰°áˆ˜áˆ¨áŒ </span></div>
<div class="actions"><button id="buy-card" class="secondary">ï¼‹ áŠ«áˆ­á‹µ áŒá‹› (10)</button><button id="claim" class="primary">ðŸ† BINGO! áŒ á‹­á‰…</button></div><p class="hint" id="hint">á‰ á‰°áŒ áˆ© á‰áŒ¥áˆ®á‰½ áˆ‹á‹­ á‰¥á‰» áˆáˆáŠ­á‰µ á‹«á‹µáˆ­áŒ‰á¢</p></section>
<section class="called-panel"><h3>á‹¨á‰°áŒ áˆ© á‰áŒ¥áˆ®á‰½</h3><div id="all-called" class="called-list">â€”</div></section>
<section class="game-panel deposit-panel"><div class="section-heading"><div><span class="eyebrow">WALLET</span><h2>ðŸ’³ áŠ­áˆ¬á‹²á‰µ áŒ¨áˆáˆ­</h2></div></div>
<p class="hint">áŠ­áá‹«á‹ŽáŠ• áŠ¨áˆáŒ¸áˆ™ á‰ áŠ‹áˆ‹ á‹¨áŒá‰¥á‹­á‰µ áˆ›áˆ¨áŒ‹áŒˆáŒ«á‹áŠ• á‹­áˆ‹áŠ©á¢ áŠ­áˆ¬á‹²á‰µ á‹¨áˆšáŒ¨áˆ˜áˆ¨á‹ á‰ áŠ á‹µáˆšáŠ• áŠ¨á‰°áˆ¨áŒ‹áŒˆáŒ  á‰ áŠ‹áˆ‹ á‰¥á‰» áŠá‹á¢</p>
<div class="payment-info"><strong>Telebirr</strong><span>0918719332 Â· Enyew</span></div><div class="payment-info"><strong>CBE Birr</strong><span>0918719332 Â· Enyew</span></div>
<label class="form-label" for="deposit-method">á‹¨áŠ­áá‹« áŠ á‹­áŠá‰µ</label><select id="deposit-method"><option>Telebirr</option><option>CBE Birr</option></select>
<label class="form-label" for="deposit-amount">á‹¨áŠ­áá‹« áˆ˜áŒ áŠ• (á‰¥áˆ­)</label><input id="deposit-amount" type="number" min="1" max="1000000" inputmode="numeric" placeholder="áˆˆáˆáˆ³áˆŒ 100">
<label class="form-label" for="deposit-reference">á‹¨áŒá‰¥á‹­á‰µ áˆ›áˆ¨áŒ‹áŒˆáŒ« á‰áŒ¥áˆ­</label><input id="deposit-reference" maxlength="100" placeholder="á‹¨áŠ­áá‹« áˆ›áˆ¨áŒ‹áŒˆáŒ« á‰áŒ¥áˆ­">
<button id="send-deposit" class="primary full-width">á‹¨áŠ­áá‹« áŒ¥á‹«á‰„ áˆ‹áŠ­</button></section>
<nav class="bottom-nav"><button class="nav-active" onclick="window.scrollTo({top:0,behavior:'smooth'})">ðŸŽ±<span>BINGO</span></button><button onclick="document.querySelector('.deposit-panel').scrollIntoView({behavior:'smooth'})">â–£<span>WALLET</span></button><button onclick="loadRank()">ðŸ†<span>RANK</span></button></nav>
<footer>á‹­áˆ… á‹¨á‰¨áˆ­á‰¹á‹‹áˆ áŠ­áˆ¬á‹²á‰µ áŒ¨á‹‹á‰³ áŠá‹á¢ áŠ­áˆ¬á‹²á‰¶á‰½ á‹¨áŒˆáŠ•á‹˜á‰¥ á‹‹áŒ‹ á‹¨áˆ‹á‰¸á‹áˆá¢</footer></main><div id="toast" role="status"></div><script src="/static/app.js"></script></body></html>"""

ADMIN_HTML = r"""<!doctype html><html lang="am"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Besh Besh Bingo Admin</title><script src="https://telegram.org/js/telegram-web-app.js"></script><link rel="stylesheet" href="/static/style.css"><style>.admin-wrap{max-width:760px;margin:auto;padding:18px}.admin-title{display:flex;justify-content:space-between;align-items:center;gap:12px}.metrics{display:grid;grid-template-columns:repeat(3,1fr);gap:9px;margin:18px 0}.metric{background:#14291b;border:1px solid #285236;border-radius:14px;padding:13px}.metric span{display:block;color:#a6c5ad;font-size:12px}.metric strong{display:block;font-size:22px;margin-top:7px}.request{background:#102217;border:1px solid #285236;border-radius:14px;padding:14px;margin:12px 0;overflow-wrap:anywhere}.request-actions{display:flex;gap:8px;margin-top:12px}.request-actions button{flex:1;padding:11px;border:0;border-radius:10px;font-weight:700}.approve{background:#35c98b;color:#082719}.reject{background:#ff6575;color:#330a10}.warning{background:#392d1c;color:#ffdc9a;border-radius:12px;padding:12px;font-size:13px}.refresh{padding:10px;border-radius:9px}</style></head><body><main class="admin-wrap"><header class="admin-title"><div><div class="eyebrow">BESH BESH BINGO</div><h1>ðŸ›¡ á‹¨áŠ á‹µáˆšáŠ• á“áŠáˆ</h1></div><button id="refresh" class="secondary">âŸ³ áŠ á‹µáˆµ</button></header><p class="warning">áŠ­áá‹«á‹áŠ• á‰ Telebirr/CBE Birr á‰ á‰µáŠ­áŠ­áˆ áŠ«áˆ¨áŒ‹áŒˆáŒ¡ á‰¥á‰» á‹«áŒ½á‹µá‰á¢</p><section class="metrics"><div class="metric"><span>á‰°áŒ«á‹‹á‰¾á‰½</span><strong id="users">â€”</strong></div><div class="metric"><span>áŒ á‰…áˆ‹áˆ‹ áŠ­áˆ¬á‹²á‰µ</span><strong id="credits">â€”</strong></div><div class="metric"><span>á‰ áˆ˜áŒ á‰£á‰ á‰… áˆ‹á‹­</span><strong id="pending">â€”</strong></div></section><h2>ðŸ’³ á‹¨áŠ­áá‹« áŒ¥á‹«á‰„á‹Žá‰½</h2><div id="requests">á‰ áˆ˜áŒ«áŠ• áˆ‹á‹­â€¦</div><div id="toast"></div></main><script src="/static/admin.js"></script></body></html>"""

APP_JS = r"""(() => {
const tg=window.Telegram?.WebApp;if(tg){tg.ready();tg.expand();try{tg.setHeaderColor('#071a0d');tg.setBackgroundColor('#06180d')}catch(e){}}
const initData=tg?.initData||'';let model=null,activeCard=0,busy=false;const $=id=>document.getElementById(id);
function toast(s){const e=$('toast');e.textContent=s;e.classList.add('show');setTimeout(()=>e.classList.remove('show'),2600)}
async function api(path,method='GET',body=null){const o={method,headers:{'Content-Type':'application/json','X-Telegram-Init-Data':initData}};if(body)o.body=JSON.stringify(body);const r=await fetch(path,o);const d=await r.json().catch(()=>({error:'Server response error'}));if(!r.ok)throw Error(d.error||'Request failed');return d}
function label(n){if(n==='FREE')return 'FREE';return (n<=15?'B':n<=30?'I':n<=45?'N':n<=60?'G':'O')+n}
function esc(s){return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function render(){if(!model)return;$('credits').textContent=model.credits;$('round').textContent='#'+model.round;$('prize').textContent=model.prize+' pts';$('players').textContent=model.player_count;$('current').textContent=model.current_number??'â€”';$('called-count').textContent=model.called_numbers.length+' / 75';$('card-count').textContent=model.cards.length+' / 4';
$('recent').innerHTML=model.called_numbers.slice(-6).reverse().map(n=>`<span class="number-chip">${label(n)}</span>`).join('')||'<span class="muted">áŒˆáŠ“ á‹¨áˆˆáˆ</span>';
$('all-called').innerHTML=model.called_numbers.length?model.called_numbers.map(n=>`<span>${label(n)}</span>`).join(''):'â€”';
$('card-tabs').innerHTML=model.cards.map((_,i)=>`<button class="card-tab ${i===activeCard?'active':''}" data-card="${i}">áŠ«áˆ­á‹µ ${i+1}</button>`).join('');
$('card-tabs').querySelectorAll('button').forEach(b=>b.onclick=()=>{activeCard=+b.dataset.card;render()});
const grid=$('bingo-grid');grid.innerHTML='';['B','I','N','G','O'].forEach(x=>{const e=document.createElement('div');e.className='cell head';e.textContent=x;grid.appendChild(e)});
const card=model.cards[activeCard],marked=new Set(model.marked[activeCard]||['FREE']),called=new Set(model.called_numbers);
if(!card){grid.innerHTML='<div class="loading">áŠ«áˆ­á‹µ á‹­áŒá‹™ áŠ¥áŠ“ á‹­áŒ€áˆáˆ©á¢</div>'}
else card.forEach((row,r)=>row.forEach((n,c)=>{const e=document.createElement('button');e.className='cell';if(n==='FREE'){e.classList.add('free');e.textContent='â˜…'}else{e.textContent=n;if(called.has(n))e.classList.add('called');if(marked.has(n))e.classList.add('marked');e.disabled=!called.has(n)||marked.has(n)||busy;e.onclick=()=>action('/api/miniapp/mark',{card_index:activeCard,number:n})}grid.appendChild(e)}));
$('buy-card').disabled=busy||model.cards.length>=4||model.credits<10;$('claim').disabled=busy||!model.cards.length}
async function refresh(showError=false){try{model=await api('/api/miniapp/game');activeCard=Math.min(activeCard,Math.max(0,model.cards.length-1));render()}catch(e){if(showError)toast(e.message);$('bingo-grid').innerHTML='<div class="loading">'+esc(e.message)+'</div>'}}
async function action(path,body={}){if(busy)return;busy=true;render();try{const d=await api(path,'POST',body);toast(d.message||'á‹¨á‰°áˆ³áŠ« áˆ‚á‹°á‰µ');await refresh()}catch(e){toast(e.message)}finally{busy=false;render()}}
$('buy-card').onclick=()=>action('/api/miniapp/buy-card');$('claim').onclick=()=>action('/api/miniapp/claim');
$('send-deposit').onclick=()=>{const amount=Number($('deposit-amount').value),method=$('deposit-method').value,reference=$('deposit-reference').value.trim();if(!Number.isInteger(amount)||amount<1){toast('á‰µáŠ­áŠ­áˆˆáŠ› á‹¨áŠ­áá‹« áˆ˜áŒ áŠ• á‹«áˆµáŒˆá‰¡');return}if(reference.length<3){toast('á‹¨áŒá‰¥á‹­á‰µ áˆ›áˆ¨áŒ‹áŒˆáŒ« á‹«áˆµáŒˆá‰¡');return}action('/api/miniapp/deposit-request',{amount,method,reference}).then(()=>{$('deposit-reference').value=''})};
window.loadRank=async()=>{try{const d=await api('/api/miniapp/rank');toast(d.rows.map((r,i)=>`${i+1}. ${r.name}: ${r.credits}`).join(' | ')||'No rank data')}catch(e){toast(e.message)}};
refresh(true);setInterval(()=>refresh(false),4000);
})();"""

ADMIN_JS = r"""(() => {
const tg=window.Telegram?.WebApp;if(tg){tg.ready();tg.expand()}const initData=tg?.initData||'';const $=id=>document.getElementById(id);
function toast(s){const e=$('toast');e.textContent=s;setTimeout(()=>e.textContent='',3000)}
async function api(path,method='GET',body=null){const o={method,headers:{'Content-Type':'application/json','X-Telegram-Init-Data':initData}};if(body)o.body=JSON.stringify(body);const r=await fetch(path,o),d=await r.json().catch(()=>({error:'Bad response'}));if(!r.ok)throw Error(d.error||'Request failed');return d}
function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function render(d){$('users').textContent=d.users_count;$('credits').textContent=d.total_credits;$('pending').textContent=d.pending_count;const root=$('requests');if(!d.requests.length){root.innerHTML='<p>áŒˆáŠ“ á‹¨áŠ­áá‹« áŒ¥á‹«á‰„ á‹¨áˆˆáˆá¢</p>';return}root.innerHTML=d.requests.map(r=>`<article class="request"><strong>#${r.id} Â· ${esc(r.method)} Â· ${r.amount} á‰¥áˆ­</strong><p>User ID: <b>${r.user_id}</b></p><p>Reference: <b>${esc(r.reference)}</b></p><p>Status: ${esc(r.status)} Â· ${esc(r.created_at)}</p>${r.status==='pending'?`<div class="request-actions"><button class="approve" data-id="${r.id}" data-decision="approve">âœ“ áŠ áŒ½á‹µá‰… áŠ¥áŠ“ áŠ­áˆ¬á‹²á‰µ áŒ¨áˆáˆ­</button><button class="reject" data-id="${r.id}" data-decision="reject">âœ• áŠ¨áˆáŠ­áˆ</button></div>`:''}</article>`).join('');root.querySelectorAll('button[data-id]').forEach(b=>b.onclick=()=>review(+b.dataset.id,b.dataset.decision,b))}
async function load(){try{render(await api('/api/admin/overview'))}catch(e){$('requests').textContent=e.message+' â€” á‹­áˆ…áŠ• áŒˆáŒ½ á‰ Telegram Mini App á‹ˆá‹­áˆ á‰ á‰°áˆá‰€á‹° áŠ á‹µáˆšáŠ• áˆ˜áˆˆá‹« á‹­áŠ­áˆá‰±á¢'}}
async function review(id,decision,b){if(!confirm(decision==='approve'?'áŠ­áá‹«á‹áŠ• á‰ á‰µáŠ­áŠ­áˆ áŠ áˆ¨áŒ‹áŒáŒ á‹‹áˆ?':'á‹­áˆ…áŠ• áŒ¥á‹«á‰„ áˆ˜áŠ¨áˆáŠ¨áˆ á‹­áˆáˆáŒ‹áˆ‰?'))return;b.disabled=true;try{const d=await api(`/api/admin/deposit/${id}/review`,'POST',{decision});toast(d.message);load()}catch(e){toast(e.message);b.disabled=false}}
$('refresh').onclick=load;load();setInterval(load,15000);
})();"""

STYLE_CSS = r""":root{color-scheme:dark;--bg:#06180d;--panel:#0b2113;--panel2:#102d19;--line:#20552d;--text:#f4f7f4;--muted:#a3b8a8;--gold:#ffe629;--teal:#39d66f}*{box-sizing:border-box}body{margin:0;background:radial-gradient(ellipse at top,#12351d 0,#06180d 58%);font-family:system-ui,-apple-system,"Noto Sans Ethiopic",sans-serif;color:var(--text);min-height:100vh}.app-shell{max-width:520px;margin:auto;padding:16px 12px 30px}.topbar{display:flex;align-items:center;gap:11px;margin:4px 2px 17px}.brand-mark{display:grid;place-items:center;width:46px;height:46px;border-radius:15px;background:linear-gradient(135deg,#ffe629,#f59e0b);font-size:25px}.eyebrow{font-size:10px;letter-spacing:2px;color:#5be47b;font-weight:800}.topbar h1{font-size:17px;margin:2px 0 0}.topbar h1 span{color:var(--gold)}.live-pill{margin-left:auto;font-size:10px;color:#ff8585;background:#ff646414;border:1px solid #ff64644a;border-radius:99px;padding:6px 9px}.live-pill i{display:inline-block;width:6px;height:6px;border-radius:50%;background:#ff6c7c;margin-right:5px}.wallet-panel,.draw-panel,.game-panel,.called-panel{background:linear-gradient(145deg,#102b19,#091b10);border:1px solid var(--line);border-radius:17px;padding:15px;margin-bottom:11px;box-shadow:0 10px 26px #0003}.wallet-panel{display:flex;align-items:center;justify-content:space-between}.muted{font-size:12px;color:var(--muted)}.balance{font-size:28px;font-weight:850;margin-top:4px}.balance small{font-size:10px;color:var(--gold)}.round-box{border-left:1px solid var(--line);padding-left:20px;min-width:70px}.round-box strong{display:block;font-size:21px;color:var(--gold);margin-top:3px}.stats-row{display:grid;grid-template-columns:1fr 1fr;gap:9px;margin-bottom:11px}.stat{padding:12px;background:#0b2113;border:1px solid var(--line);border-radius:13px}.stat span{font-size:11px;color:var(--muted);display:block}.stat strong{display:block;font-size:18px;margin-top:5px}.draw-panel{display:flex;align-items:center;gap:16px}.last-number{display:grid;place-items:center;width:68px;height:68px;border-radius:17px;background:linear-gradient(145deg,#ffe629,#e6a900);color:#152317;font-size:29px;font-weight:900;margin-top:7px}.draw-meta{flex:1;min-width:0}.draw-meta strong{display:block;font-size:15px;margin:4px 0 8px}.number-strip{display:flex;gap:5px;flex-wrap:wrap}.number-chip{font-size:10px;padding:4px 6px;border-radius:7px;background:#173820;color:#e2ffe8}.section-heading{display:flex;align-items:center;justify-content:space-between;margin-bottom:13px}.section-heading h2{margin:4px 0 0;font-size:19px}.card-count{font-size:12px;color:var(--gold);background:#ffe62916;border:1px solid #ffe62966;border-radius:9px;padding:7px 9px}.card-tabs{display:flex;gap:7px;overflow-x:auto;margin-bottom:11px}.card-tab{border:1px solid var(--line);background:#081a0e;color:var(--muted);border-radius:9px;padding:8px 13px;font-size:12px;white-space:nowrap}.card-tab.active{background:#ffe6291c;border-color:var(--gold);color:var(--gold)}.bingo-grid{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:5px}.cell{aspect-ratio:1;display:grid;place-items:center;border:1px solid #20552d;border-radius:10px;background:#0c2916;color:#f0fff2;font-weight:750;font-size:clamp(13px,4vw,18px);padding:0;min-width:0}.cell.head{aspect-ratio:auto;height:32px;background:#14351e;color:var(--gold);font-size:13px;border:0}.cell.called{border-color:#39d66f;background:#164b27;color:#8affaa}.cell.marked{background:linear-gradient(135deg,#ffe629,#dca900);border-color:#fff27b;color:#102014;box-shadow:0 0 0 1px #ffe62944}.cell.free{font-size:11px;background:#45371e;color:var(--gold);border-color:#6c552a}.cell:disabled{opacity:1}.legend{display:flex;gap:15px;color:var(--muted);font-size:11px;margin:12px 2px 15px}.dot{display:inline-block;width:8px;height:8px;border-radius:3px;margin-right:5px;background:#164b27;border:1px solid #39d66f}.dot.marked{background:#ffe629}.actions{display:grid;grid-template-columns:1fr 1fr;gap:8px}button{font:inherit;cursor:pointer}.actions button,.primary,.secondary{border:0;border-radius:11px;padding:12px 9px;font-weight:800}.primary{background:linear-gradient(135deg,#ffe629,#ffbd22);color:#17200f}.secondary{background:#173b22;border:1px solid #2b6b3b!important;color:#f2fff3}.primary:disabled,.secondary:disabled{opacity:.45;cursor:not-allowed}.hint{font-size:12px;line-height:1.6;color:var(--muted)}.called-list{display:flex;flex-wrap:wrap;gap:6px}.called-list span{padding:6px 8px;border:1px solid var(--line);border-radius:7px;background:#0b2413;font-size:12px}.payment-info{display:flex;justify-content:space-between;gap:10px;padding:12px;background:#0a1b10;border:1px solid var(--line);border-radius:10px;margin:8px 0}.payment-info span{color:var(--muted)}.form-label{display:block;margin:13px 0 6px;font-size:12px;color:var(--muted)}input,select{width:100%;padding:12px;border-radius:10px;background:#07180c;border:1px solid #285536;color:white;font:inherit}.full-width{width:100%;margin-top:14px}.bottom-nav{position:sticky;bottom:0;display:flex;justify-content:space-around;margin:18px -12px -30px;padding:12px 5px;background:#06180df2;border-top:1px solid #1a4025}.bottom-nav button{display:flex;flex-direction:column;align-items:center;gap:4px;background:none;border:0;color:#809787;font-size:20px}.bottom-nav span{font-size:11px;font-weight:800}.bottom-nav .nav-active{color:var(--gold)}footer{text-align:center;color:#77917e;font-size:11px;padding:20px 8px}.loading{padding:20px;text-align:center;color:var(--muted)}#toast{position:fixed;bottom:22px;left:50%;transform:translateX(-50%);max-width:90%;background:#f5f5f5;color:#111;padding:11px 16px;border-radius:12px;box-shadow:0 8px 25px #0005;opacity:0;pointer-events:none;transition:opacity .2s;z-index:99}#toast.show{opacity:1}@media(max-width:360px){.actions{grid-template-columns:1fr}.stats-row{gap:6px}.stat{padding:10px}.payment-info{flex-direction:column}}"""

STATIC_ASSETS = {
    "index.html": INDEX_HTML,
    "admin.html": ADMIN_HTML,
    "app.js": APP_JS,
    "admin.js": ADMIN_JS,
    "style.css": STYLE_CSS,
}

# ------------------------------ Flask routes -------------------------
@app.get("/")
def home():
    return Response(STATIC_ASSETS["index.html"], mimetype="text/html; charset=utf-8")

@app.get("/admin")
def admin_page():
    return Response(STATIC_ASSETS["admin.html"], mimetype="text/html; charset=utf-8")

@app.get("/static/<path:asset>")
def static_asset(asset):
    if asset not in STATIC_ASSETS:
        return "Not found", 404
    mime = "text/css; charset=utf-8" if asset.endswith(".css") else (
        "application/javascript; charset=utf-8" if asset.endswith(".js") else "text/html; charset=utf-8"
    )
    return Response(STATIC_ASSETS[asset], mimetype=mime)

@app.get("/health")
def health():
    return jsonify({"ok": True, "service": "Besh Besh Bingo"})

@app.get("/api/miniapp/game")
def miniapp_game():
    user = api_user()
    if not user:
        return json_error("Open this game from the Telegram bot.", 401)
    uid = int(user["id"])
    row = get_user(uid)
    with game_lock:
        cards = all_cards(uid)
        marked = [sorted(marked_set(uid, i), key=str) for i in range(len(cards))]
        return jsonify({
            "credits": int(row["credits"]), "round": game_state["round"],
            "prize": game_state["prize"], "player_count": active_player_count(),
            "called_numbers": game_state["called_numbers"],
            "current_number": game_state["current_number"],
            "cards": cards, "marked": marked, "is_active": game_state["is_active"],
            "card_cost": CARD_COST,
        })

@app.post("/api/miniapp/buy-card")
def miniapp_buy_card():
    user = api_user()
    if not user:
        return json_error("Open this game from the Telegram bot.", 401)
    uid = int(user["id"])
    with game_lock:
        if len(all_cards(uid)) >= MAX_CARDS_PER_PLAYER:
            return json_error("You already have the maximum of 4 cards.")
        row = get_user(uid)
        if not row or int(row["credits"]) < CARD_COST:
            return json_error("Not enough credits.")
        update_credits(uid, -CARD_COST)
        game_state["cards"].setdefault(uid, []).append(make_card())
        idx = len(game_state["cards"][uid]) - 1
        game_state["marked"][(uid, idx)] = {"FREE"}
        game_state["players"].add(uid)
        game_state["prize"] += CARD_COST
        game_state["is_active"] = True
        game_state["started_at"] = game_state["started_at"] or time.time()
        return jsonify({"ok": True, "message": "áŠ«áˆ­á‹µ á‰°áŒˆá‹á‰·áˆ / Card purchased.", "card_index": idx})

@app.post("/api/miniapp/mark")
def miniapp_mark():
    user = api_user()
    if not user:
        return json_error("Open this game from the Telegram bot.", 401)
    uid = int(user["id"])
    body = request.get_json(silent=True) or {}
    try:
        card_index, number = int(body["card_index"]), int(body["number"])
    except (KeyError, ValueError, TypeError):
        return json_error("Invalid card or number.")
    with game_lock:
        cards = all_cards(uid)
        if card_index < 0 or card_index >= len(cards):
            return json_error("Card not found.")
        if number not in game_state["called_numbers"]:
            return json_error("This number has not been called yet.")
        if not any(number in row for row in cards[card_index]):
            return json_error("Number is not on this card.")
        marked_set(uid, card_index).add(number)
        return jsonify({"ok": True, "message": "á‰áŒ¥áˆ© á‰°áˆ˜áˆ­áŒ§áˆ / Number marked."})

@app.post("/api/miniapp/claim")
def miniapp_claim():
    user = api_user()
    if not user:
        return json_error("Open this game from the Telegram bot.", 401)
    uid = int(user["id"])
    username = user.get("username") or user.get("first_name") or str(uid)
    with game_lock:
        cards = all_cards(uid)
        if not cards:
            return json_error("Buy a card first.")
        if uid in game_state["winner_ids"]:
            return json_error("You have already won this round.")
        winning_index = None
        for i, card in enumerate(cards):
            if card_has_bingo(card, marked_set(uid, i)):
                winning_index = i
                break
        if winning_index is None:
            return json_error("No Bingo yet. Mark the completed row, column, or diagonal.")
        prize = int(game_state["prize"])
        update_credits(uid, prize)
        with closing(db()) as con:
            con.execute("INSERT INTO wins(user_id,username,round_no,card_index,prize) VALUES(?,?,?,?,?)",
                        (uid, username, game_state["round"], winning_index, prize))
            con.commit()
        game_state["winner_ids"].add(uid)
        result = {"ok": True, "message": f"ðŸ† BINGO! You won {prize} virtual credits.",
                  "winner": username, "card_index": winning_index, "prize": prize,
                  "round": game_state["round"]}
        # Start a fresh round after a short pause, retaining winner response.
        reset_round()
    return jsonify(result)

@app.post("/api/miniapp/deposit-request")
def miniapp_deposit_request():
    user = api_user()
    if not user:
        return json_error("Open this game from the Telegram bot.", 401)
    body = request.get_json(silent=True) or {}
    method = str(body.get("method", "")).strip()
    reference = str(body.get("reference", "")).strip()
    try:
        amount = int(body.get("amount", 0))
    except (ValueError, TypeError):
        amount = 0
    if method not in ("Telebirr", "CBE Birr"):
        return json_error("Invalid payment method.")
    if amount < 1 or amount > 1000000:
        return json_error("Invalid amount.")
    if len(reference) < 3 or len(reference) > 100:
        return json_error("Enter a valid transaction reference.")
    with closing(db()) as con:
        cur = con.execute("INSERT INTO deposit_requests(user_id,method,amount,reference) VALUES(?,?,?,?)",
                          (int(user["id"]), method, amount, reference))
        request_id = cur.lastrowid
        con.commit()
    return jsonify({"ok": True, "message": f"á‹¨áŠ­áá‹« áŒ¥á‹«á‰„ #{request_id} á‰°áˆáŠ³áˆá¢ áŠ á‹µáˆšáŠ• áŠ¥áˆµáŠªá‹«áˆ¨áŒ‹áŒáŒ¥ á‹­áŒ á‰¥á‰á¢"})

@app.get("/api/miniapp/rank")
def miniapp_rank():
    user = api_user()
    if not user:
        return json_error("Open this game from the Telegram bot.", 401)
    with closing(db()) as con:
        rows = con.execute("SELECT username,first_name,credits FROM users ORDER BY credits DESC LIMIT 10").fetchall()
    return jsonify({"rows": [{"name": ("@" + r["username"]) if r["username"] else (r["first_name"] or "Player"),
                              "credits": int(r["credits"])} for r in rows]})

@app.get("/api/admin/overview")
def admin_overview():
    user = api_admin()
    if not user:
        return json_error("Admin access denied. Open this page from the authorized Telegram admin account.", 403)
    with closing(db()) as con:
        users_count = con.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
        total_credits = con.execute("SELECT COALESCE(SUM(credits),0) AS n FROM users").fetchone()["n"]
        pending_count = con.execute("SELECT COUNT(*) AS n FROM deposit_requests WHERE status='pending'").fetchone()["n"]
        rows = con.execute("SELECT * FROM deposit_requests ORDER BY id DESC LIMIT 100").fetchall()
    return jsonify({
        "users_count": users_count, "total_credits": total_credits, "pending_count": pending_count,
        "requests": [{"id": r["id"], "user_id": r["user_id"], "method": r["method"],
                      "amount": r["amount"], "reference": r["reference"], "status": r["status"],
                      "created_at": r["created_at"]} for r in rows]
    })

@app.post("/api/admin/deposit/<int:request_id>/review")
def admin_review_deposit(request_id):
    user = api_admin()
    if not user:
        return json_error("Admin access denied.", 403)
    body = request.get_json(silent=True) or {}
    decision = body.get("decision")
    if decision not in ("approve", "reject"):
        return json_error("Invalid decision.")
    with closing(db()) as con:
        row = con.execute("SELECT * FROM deposit_requests WHERE id=?", (request_id,)).fetchone()
        if not row:
            return json_error("Request not found.", 404)
        if row["status"] != "pending":
            return json_error("This request has already been reviewed.")
        status = "approved" if decision == "approve" else "rejected"
        con.execute("UPDATE deposit_requests SET status=?,reviewed_at=CURRENT_TIMESTAMP,reviewed_by=? WHERE id=?",
                    (status, int(user["id"]), request_id))
        if decision == "approve":
            # This implementation treats one ETB requested as one virtual credit.
            con.execute("UPDATE users SET credits=credits+? WHERE user_id=?", (int(row["amount"]), int(row["user_id"])))
        con.commit()
    return jsonify({"ok": True, "message": "á‹¨áŠ­áá‹« áŒ¥á‹«á‰„á‹ á‰°á‹˜áˆáŠ—áˆá¢"})

# ------------------------------ Webhook/runtime ----------------------
@app.post("/telegram-webhook" if not WEBHOOK_SECRET_PATH else "/telegram-webhook/" + WEBHOOK_SECRET_PATH)
def telegram_webhook():
    try:
        payload = request.get_json(force=True)
        update = Update.de_json(payload, bot_application.bot)
        if bot_loop.is_running():
            asyncio.run_coroutine_threadsafe(bot_application.process_update(update), bot_loop)
        return "OK"
    except Exception:
        logger.exception("Webhook update failed")
        return "ERROR", 500

@app.route("/<path:path>", methods=["GET", "HEAD"])
def fallback(path):
    # Keep health checks and unknown URLs from crashing the service.
    return jsonify({"error": "Not found"}), 404

def run_bot_loop():
    asyncio.set_event_loop(bot_loop)
    bot_loop.run_forever()

def start_bot():
    global bot_thread
    if bot_thread and bot_thread.is_alive():
        return
    bot_thread = threading.Thread(target=run_bot_loop, daemon=True, name="telegram-bot-loop")
    bot_thread.start()
    async def initialize():
        await bot_application.initialize()
        await bot_application.start()
        if PUBLIC_URL:
            webhook_path = "/telegram-webhook" + (("/" + WEBHOOK_SECRET_PATH) if WEBHOOK_SECRET_PATH else "")
            await bot_application.bot.set_webhook(PUBLIC_URL + webhook_path)
            logger.info("Telegram webhook configured: %s", PUBLIC_URL + webhook_path)
        else:
            logger.warning("No RENDER_EXTERNAL_URL; webhook not configured.")
    future = asyncio.run_coroutine_threadsafe(initialize(), bot_loop)
    try:
        future.result(timeout=30)
    except Exception:
        logger.exception("Telegram bot initialization failed")

start_bot()
threading.Thread(target=auto_caller_loop, daemon=True, name="bingo-auto-caller").start()

if __name__ == "__main__":
    # Render provides PORT. Do not use Flask's reloader in production.
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
