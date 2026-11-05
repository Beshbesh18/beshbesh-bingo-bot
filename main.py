# -*- coding: utf-8 -*-
"""
Bash Bash Bingo — Telegram bot + Telegram Mini App
1-600 Card Selection (50s timer), 1-75 Master Grid & Cards,
Play/Win Wallets, Deposit/Withdraw, Line Bingo check, and Voice Number Calling.

NOTE: game state lives in memory, so run with a SINGLE worker process
(e.g. `gunicorn -w 1 --threads 8 bingo_bot:app`).
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

CARD_COST = 10
STARTING_CREDITS = 100
MAX_CARDS_PER_PLAYER = 4
SELECTION_TIME_SECONDS = 50
CALL_INTERVAL_SECONDS = 3
MAX_NUMBERS_POOL = 75  # 1 to 75 numbers called in game
TOTAL_CARDS = 600

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("bash-bash-bingo")

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
            credits REAL NOT NULL DEFAULT 100.0,
            win_credits REAL NOT NULL DEFAULT 0.0,
            language TEXT NOT NULL DEFAULT 'am',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS deposit_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            method TEXT NOT NULL,
            amount REAL NOT NULL,
            reference TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            reviewed_at TEXT,
            reviewed_by INTEGER
        );
        CREATE TABLE IF NOT EXISTS withdraw_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            account_info TEXT NOT NULL,
            amount REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS wins (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            username TEXT NOT NULL DEFAULT '',
            round_no INTEGER NOT NULL,
            card_index INTEGER NOT NULL,
            prize REAL NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """)
        con.commit()


def get_user(user_id):
    with closing(db()) as con:
        return con.execute("SELECT * FROM users WHERE user_id=?", (int(user_id),)).fetchone()


def ensure_user(user_id, username="", first_name=""):
    with closing(db()) as con:
        con.execute("""
            INSERT INTO users(user_id, username, first_name, credits, win_credits)
            VALUES(?,?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET
              username=excluded.username, first_name=excluded.first_name
        """, (int(user_id), username or "", first_name or "", STARTING_CREDITS, 0.0))
        con.commit()


def update_credits(user_id, delta, is_win=False):
    with closing(db()) as con:
        col = "win_credits" if is_win else "credits"
        con.execute(f"UPDATE users SET {col}={col}+? WHERE user_id=?", (float(delta), int(user_id)))
        con.commit()
        row = con.execute("SELECT credits, win_credits FROM users WHERE user_id=?", (int(user_id),)).fetchone()
        return row[col] if row else 0.0


def set_language(user_id, language):
    with closing(db()) as con:
        con.execute("UPDATE users SET language=? WHERE user_id=?", (language, int(user_id)))
        con.commit()


def get_language(user_id):
    row = get_user(user_id)
    return row["language"] if row else "am"


init_db()


# ------------------------------ Game State ---------------------------
def fresh_game():
    return {
        "round": 1,
        "phase": "selecting",  # 'selecting' (50s timer) or 'playing'
        "selection_ends_at": time.time() + SELECTION_TIME_SECONDS,
        "prize": 0.0,
        "called_numbers": [],
        "current_number": None,
        "taken_card_numbers": set(),  # 1..600 numbers chosen by players
        "player_cards": {},           # user_id -> list of chosen card IDs
        "card_matrices": {},          # (user_id, card_id) -> 5x5 grid (1-75)
        "marked": {},                 # (user_id, card_id) -> set of marked numbers/FREE
    }


game_state = fresh_game()


def generate_75_bingo_card(seed_val):
    rng = random.Random(seed_val)
    cols = [
        rng.sample(range(1, 16), 5),
        rng.sample(range(16, 31), 5),
        rng.sample(range(31, 46), 5),
        rng.sample(range(46, 61), 5),
        rng.sample(range(61, 76), 5),
    ]
    card = [[cols[c][r] for c in range(5)] for r in range(5)]
    card[2][2] = "FREE"
    return card


def card_has_bingo(card, marked):
    marked = set(marked) | {"FREE"}
    for r in range(5):  # horizontal
        if all(card[r][c] in marked for c in range(5)):
            return True
    for c in range(5):  # vertical
        if all(card[r][c] in marked for r in range(5)):
            return True
    if all(card[i][i] in marked for i in range(5)):  # diagonal
        return True
    if all(card[i][4 - i] in marked for i in range(5)):
        return True
    return False


def reset_round(refund=False):
    """Start a new round. Must be called with game_lock held (it is re-entrant)."""
    with game_lock:
        if refund:
            for uid, cards in game_state["player_cards"].items():
                if cards:
                    update_credits(uid, len(cards) * CARD_COST, is_win=False)
        game_state["round"] += 1
        game_state["phase"] = "selecting"
        game_state["selection_ends_at"] = time.time() + SELECTION_TIME_SECONDS
        game_state["prize"] = 0.0
        game_state["called_numbers"] = []
        game_state["current_number"] = None
        game_state["taken_card_numbers"] = set()
        game_state["player_cards"] = {}
        game_state["card_matrices"] = {}
        game_state["marked"] = {}


def try_award(uid, username=""):
    """Check the player's cards; if one has bingo, pay the prize and start a new round.
    Returns (prize, card_id) or None. Safe to call without holding the lock."""
    with game_lock:
        if game_state["phase"] != "playing":
            return None
        for cid in game_state["player_cards"].get(uid, []):
            mat = game_state["card_matrices"].get((uid, cid))
            marked = game_state["marked"].get((uid, cid), {"FREE"})
            if mat and card_has_bingo(mat, marked):
                prize = float(game_state["prize"])
                update_credits(uid, prize, is_win=True)
                with closing(db()) as con:
                    con.execute(
                        "INSERT INTO wins(user_id, username, round_no, card_index, prize) VALUES(?,?,?,?,?)",
                        (int(uid), username or "", game_state["round"], cid, prize))
                    con.commit()
                reset_round()
                return prize, cid
    return None


def game_loop_worker():
    """Ends the selection phase when the timer runs out."""
    while True:
        time.sleep(1)
        try:
            with game_lock:
                if game_state["phase"] == "selecting" and time.time() >= game_state["selection_ends_at"]:
                    has_players = any(game_state["player_cards"].values())
                    if has_players:
                        game_state["phase"] = "playing"
                        game_state["called_numbers"] = []
                        game_state["current_number"] = None
                    else:
                        game_state["selection_ends_at"] = time.time() + SELECTION_TIME_SECONDS
        except Exception:
            logger.exception("game_loop_worker error")


def auto_caller_loop():
    """Calls a random number every few seconds while playing."""
    while True:
        time.sleep(CALL_INTERVAL_SECONDS)
        try:
            with game_lock:
                if game_state["phase"] != "playing":
                    continue
                remaining = [n for n in range(1, MAX_NUMBERS_POOL + 1)
                             if n not in game_state["called_numbers"]]
                if remaining:
                    num = random.choice(remaining)
                    game_state["called_numbers"].append(num)
                    game_state["current_number"] = num
                else:
                    reset_round(refund=True)
        except Exception:
            logger.exception("auto_caller_loop error")


# ------------------------------ Localization & Handlers -------------------------
TEXT = {
    "am": {
        "welcome": "🎰 እንኳን ደህና መጡ ወደ በሽ በሽ ቢንጎ!\n\nአማራጭ እና ፈጣን የቢንጎ ጨዋታ መድረክ",
        "play": "🎮 በሽ በሽ ቢንጎ ጨዋታ",
        "balance": "💰 ብር ሂሳብ",
        "rules": "📜 ሕግ እና ደንቦች",
        "wallet": "💳 Wallet / አካውንት ብር",
        "rank": "🏆 ደረጃ",
        "language": "🌐 ቋንቋ / Language",
        "support": "🆘 እርዳታ",
        "admin": "🛡 አድሚን",
        "choose": "ከታች ያሉትን አማራጮች ይምረጡ።",
        "balance_msg": "💰 የአካውንት ሂሳብዎ:\nPlay Wallet: {credits} ETB\nWin Wallet: {win_credits} ETB",
        "rules_msg": "📜 ሕጎች\n• ካርድ ውድድር: 10 ብር\n• ከ 1 እስከ 600 ካርዶች ውስጥ ይምረጡ\n• ቁጥሮች ከ 1 እስከ 75 ይጠራሉ",
        "language_set": "ቋንቋው ወደ አማርኛ ተስተካክሏል።",
    },
    "en": {
        "welcome": "🎰 Welcome to Bash Bash Bingo!\n\nFast and exciting bingo gaming platform.",
        "play": "🎮 Play Bash Bash Bingo",
        "balance": "💰 Balance",
        "rules": "📜 Rules",
        "wallet": "💳 Wallet",
        "rank": "🏆 Rank",
        "language": "🌐 Language",
        "support": "🆘 Help",
        "admin": "🛡 Admin",
        "choose": "Choose an option below.",
        "balance_msg": "💰 Your balance:\nPlay Wallet: {credits} ETB\nWin Wallet: {win_credits} ETB",
        "rules_msg": "📜 Rules\n• Card cost: 10 ETB\n• Pick cards from 1 to 600\n• Numbers called from 1 to 75",
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
            InlineKeyboardButton("⚡ በሽ በሽ ቢንጎ ክፍት", web_app=WebAppInfo(url=webapp_url()))
        ]])
    return None


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user.id, user.username, user.first_name)
    text = tr(user.id, "welcome") + "\n\n" + tr(user.id, "choose")
    markup = start_keyboard()
    if markup:
        await update.effective_message.reply_text(text, reply_markup=markup)
    else:
        await update.effective_message.reply_text(text)
    await update.effective_message.reply_text("በሽ በሽ ቢንጎ ⚡", reply_markup=main_keyboard(user.id))


async def language_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user.id, user.username, user.first_name)
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("አማርኛ", callback_data="lang:am"),
        InlineKeyboardButton("English", callback_data="lang:en"),
    ]])
    await update.effective_message.reply_text("ቋንቋ ይምረጡ / Choose language:", reply_markup=keyboard)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user.id, user.username, user.first_name)
    msg = (update.effective_message.text or "").strip()
    lang = get_language(user.id)
    t = TEXT.get(lang, TEXT["am"])

    if msg in (t["play"], "🎮 በሽ በሽ ቢንጎ ጨዋታ", "🎮 Play Bash Bash Bingo"):
        markup = start_keyboard()
        if markup:
            await update.effective_message.reply_text("በሽ በሽ ቢንጎ ⚡", reply_markup=markup)
        else:
            await update.effective_message.reply_text("Set RENDER_EXTERNAL_URL to enable the Mini App.")
    elif msg in (t["balance"], "💰 ብር ሂሳብ", "💰 Balance"):
        row = get_user(user.id)
        await update.effective_message.reply_text(
            tr(user.id, "balance_msg", credits=row["credits"], win_credits=row["win_credits"]))
    elif msg in (t["rules"], "📜 ሕግ እና ደንቦች", "📜 Rules"):
        await update.effective_message.reply_text(tr(user.id, "rules_msg"))
    elif msg in (t["wallet"], "💳 Wallet / አካውንት ብር", "💳 Wallet"):
        await update.effective_message.reply_text(
            "💳 Wallet\nTelebirr: 0918719332 · Enyew\nCBE Birr: 0918719332 · Enyew\n\n"
            "ክፍያ ከፈጸሙ በኋላ በ Mini App ውስጥ የክፍያ ማረጋገጫ ቁጥርዎን ያስገቡ።")
    elif msg in (t["rank"], "🏆 ደረጃ", "🏆 Rank"):
        with closing(db()) as con:
            rows = con.execute(
                "SELECT username, first_name, win_credits FROM users ORDER BY win_credits DESC LIMIT 10"
            ).fetchall()
        lines = ["🏆 Top Bash Players"]
        for i, r in enumerate(rows, 1):
            name = ("@" + r["username"]) if r["username"] else (r["first_name"] or "Player")
            lines.append(f"{i}. {name} — {r['win_credits']} ETB")
        await update.effective_message.reply_text("\n".join(lines))
    elif msg in (t["language"], "🌐 ቋንቋ / Language", "🌐 Language"):
        await language_command(update, context)
    elif msg in (t["support"], "🆘 እርዳታ", "🆘 Help"):
        await update.effective_message.reply_text("🆘 እርዳታ / Help\nContact the administrator for support.")
    else:
        await update.effective_message.reply_text(tr(user.id, "choose"), reply_markup=main_keyboard(user.id))


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        await update.effective_message.reply_text("⛔ Unauthorized.")
        return
    if PUBLIC_URL:
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("🛡 Open Bash Admin Panel", web_app=WebAppInfo(url=PUBLIC_URL + "/admin"))
        ]])
        await update.effective_message.reply_text("🛡 Admin panel", reply_markup=keyboard)
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


bot_application.add_handler(CommandHandler("start", start))
bot_application.add_handler(CommandHandler("language", language_command))
bot_application.add_handler(CommandHandler("admin", admin_command))
bot_application.add_handler(CallbackQueryHandler(callback_handler))
bot_application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))


# ---------------------------- Mini App auth ---------------------------
def validate_telegram_init_data(init_data):
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
        return user_data
    except Exception:
        logger.exception("Failed to validate initData")
        return None


def api_user():
    data = validate_telegram_init_data(request.headers.get("X-Telegram-Init-Data", ""))
    if data:
        ensure_user(data["id"], data.get("username", ""), data.get("first_name", ""))
        return data
    if os.environ.get("ALLOW_DEMO_MODE", "").lower() == "true":
        try:
            demo_id = int(request.headers.get("X-Demo-User", "999999"))
        except ValueError:
            demo_id = 999999
        ensure_user(demo_id, "demo", "Demo Player")
        return {"id": demo_id, "username": "demo", "first_name": "Demo Player"}
    return None


def api_admin():
    user = api_user()
    return user if user and int(user["id"]) in ADMIN_IDS else None


def json_error(message, status=400):
    return jsonify({"error": message}), status


def to_positive_float(value):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


# ------------------------------ HTML & JS Assets ---------------------------
INDEX_HTML = r"""<!doctype html>
<html lang="am"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no"><meta name="theme-color" content="#06180d"><title>በሽ በሽ ቢንጎ</title><script src="https://telegram.org/js/telegram-web-app.js"></script><link rel="stylesheet" href="/static/style.css"></head>
<body><main class="app-shell">
<header class="topbar">
  <div class="brand-info"><div class="eyebrow">⚡ በሽ በሽ ቢንጎ</div><h1 id="round-title">ዙር #1</h1></div>
  <div class="top-scores">
    <div class="score-box"><span>PLAY</span><strong id="play-bal">0.00</strong></div>
    <div class="score-box win"><span>WIN</span><strong id="win-bal">0.00</strong></div>
    <div class="current-call-badge" id="current">—</div>
  </div>
</header>

<div id="phase-banner" class="phase-banner">ሰዓቱ እየተጠናቀቀ ነው: <span id="timer">50</span> ሰከንድ (ካርድ ይምረጡ 1-600)</div>

<section class="game-panel" id="selection-panel">
  <div class="section-header">
    <h2>ካርድ ቁጥር ይምረጡ (ከ 1 እስከ 600)</h2>
    <span class="card-count-pill" id="my-chosen-count">የተመረጠ: 0/4</span>
  </div>
  <div class="master-grid" id="number-picker-grid"></div>
</section>

<section class="game-panel" id="live-game-panel" style="display:none;">
  <div class="section-header">
    <h2>የተጠሩ ቁጥሮች (1 - 75)</h2>
    <span class="card-count-pill" id="card-count">ካርዶች: 0</span>
  </div>
  <div class="master-grid-75" id="master-grid-75"></div>
  <div class="called-balls-row" id="called-balls-row"></div>
</section>

<section class="cards-section" id="cards-container"></section>

<div class="actions-row" id="game-actions" style="display:none;">
  <button id="claim" class="success-btn">🏆 BINGO! አገኙ</button>
</div>

<div class="wallet-section" id="wallet-section" style="display:none;">
  <h2>💳 ኪስ ቦርሳ እና ክፍያ</h2>
  <div class="balance-display-box">
    <div>PLAY WALLET: <strong id="w-play">0.00</strong> ETB</div>
    <div>WIN WALLET: <strong id="w-win">0.00</strong> ETB</div>
  </div>
  <div class="wallet-buttons-row">
    <button id="show-deposit" class="primary-btn">📥 DEPOSIT</button>
    <button id="show-withdraw" class="success-btn">📤 WITHDRAW</button>
  </div>
  <div id="deposit-form" class="sub-form">
    <h3>ገንዘብ ማስገባት</h3>
    <select id="deposit-method"><option>Telebirr</option><option>CBE Birr</option></select>
    <input id="deposit-amount" type="number" placeholder="የክፍያ መጠን (ብር)">
    <input id="deposit-reference" placeholder="Transaction ID">
    <button id="send-deposit" class="primary-btn">የክፍያ ጥያቄ ላክ</button>
  </div>
  <div id="withdraw-form" class="sub-form" style="display:none;">
    <h3>ገንዘብ ማውጣት</h3>
    <input id="withdraw-account" placeholder="አካውንት ቁጥር / ስልክ እና ስም">
    <input id="withdraw-amount" type="number" placeholder="መጠን (ብር)">
    <button id="send-withdraw" class="success-btn">ማውጣት ጥያቄ ላክ</button>
  </div>
</div>

<div class="rank-section" id="rank-section" style="display:none;">
  <h2>🏆 ከፍተኛ ተወዳዳሪዎች</h2>
  <div id="rank-list">እየጫነ ነው።...</div>
</div>

<nav class="bottom-nav">
  <button class="nav-btn active" data-tab="bingo" onclick="switchTab('bingo')">🎯 BINGO</button>
  <button class="nav-btn" data-tab="wallet" onclick="switchTab('wallet')">💳 WALLET</button>
  <button class="nav-btn" data-tab="rank" onclick="switchTab('rank')">🏆 RANK</button>
</nav>
</main><div id="toast"></div><script src="/static/app.js"></script></body></html>"""

ADMIN_HTML = r"""<!doctype html><html lang="am"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>በሽ በሽ ቢንጎ አድሚን</title><script src="https://telegram.org/js/telegram-web-app.js"></script><link rel="stylesheet" href="/static/style.css"><style>.admin{max-width:700px;margin:auto;padding:16px}.req{background:#0b2113;border:1px solid #20552d;padding:12px;border-radius:10px;margin-bottom:10px}.req span{display:block;font-size:11px;color:#a3b8a8}</style></head>
<body class="admin"><header><h1>🛡 በሽ በሽ ቢንጎ አድሚን ፓነል</h1><button id="refresh" class="primary-btn">🔄 አዲስ</button></header>
<section style="display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin:15px 0">
  <div class="req"><span>ተወዳዳሪዎች</span><strong id="u-count">—</strong></div>
  <div class="req"><span>ጠቅላላ ብር</span><strong id="t-cred">—</strong></div>
  <div class="req"><span>በመጠባበቅ ላይ</span><strong id="p-count">—</strong></div>
</section>
<h2>የክፍያ ጥያቄዎች</h2><div id="req-list">እየጫነ ነው።...</div><script src="/static/admin.js"></script></body></html>"""

STYLE_CSS = r""":root{color-scheme:dark;--bg:#041509;--panel:#0b2113;--panel2:#102d19;--line:#20552d;--text:#f4f7f4;--muted:#a3b8a8;--gold:#ffe629;--teal:#39d66f}
*{box-sizing:border-box}body{margin:0;background:var(--bg);font-family:system-ui,sans-serif;color:var(--text);padding-bottom:70px}
.app-shell{max-width:520px;margin:auto;padding:12px}
.topbar{display:flex;justify-content:space-between;align-items:center;background:var(--panel);border:1px solid var(--line);padding:10px 14px;border-radius:14px;margin-bottom:12px}
.eyebrow{font-size:10px;color:var(--teal);font-weight:800}.topbar h1{font-size:16px;margin:0}
.top-scores{display:flex;gap:6px;align-items:center}
.score-box{background:var(--panel2);border:1px solid var(--line);padding:4px 8px;border-radius:8px;font-size:11px;text-align:center}
.score-box.win{border-color:var(--gold)}.score-box strong{display:block;color:var(--gold);font-size:13px}
.current-call-badge{width:40px;height:40px;background:linear-gradient(135deg,#39d66f,#0b6329);border-radius:10px;display:grid;place-items:center;font-weight:900;font-size:16px;color:#041509}
.phase-banner{background:#183820;border:1px solid var(--teal);padding:8px;border-radius:8px;text-align:center;font-size:12px;margin-bottom:10px;font-weight:bold;color:var(--gold)}
.game-panel,.wallet-section,.rank-section{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:12px;margin-bottom:12px}
.section-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;font-size:13px}
.section-header h2{font-size:13px;margin:0}
.card-count-pill{background:var(--panel2);padding:4px 8px;border-radius:6px;border:1px solid var(--line);font-size:11px;color:var(--teal)}
.master-grid{display:grid;grid-template-columns:repeat(15,1fr);gap:2px;max-height:220px;overflow-y:auto;background:#06180d;padding:6px;border-radius:10px;border:1px solid var(--line)}
.master-grid-75{display:grid;grid-template-columns:22px repeat(15,1fr);gap:2px;background:#06180d;padding:6px;border-radius:10px;border:1px solid var(--line)}
.m-letter{display:grid;place-items:center;font-weight:900;color:var(--gold);font-size:12px}
.picker-btn{aspect-ratio:1;background:var(--panel2);border:1px solid var(--line);border-radius:3px;font-size:8px;display:grid;place-items:center;font-weight:700;color:var(--text);cursor:pointer;padding:0}
.picker-btn.taken{background:#331010;color:#777;border-color:#551111;cursor:not-allowed}
.picker-btn.mine{background:var(--teal);color:#041509;font-weight:900}
.m-cell{aspect-ratio:1;background:var(--panel2);border-radius:3px;font-size:8px;display:grid;place-items:center;font-weight:700;color:var(--muted)}
.m-cell.called{background:var(--gold);color:#041509;font-weight:900}
.called-balls-row{display:flex;gap:4px;overflow-x:auto;margin-top:8px;padding:4px;background:#06180d;border-radius:8px;border:1px solid var(--line)}
.mini-ball{min-width:24px;height:24px;background:var(--gold);color:#041509;border-radius:50%;display:grid;place-items:center;font-size:10px;font-weight:900}
.cards-section{display:flex;flex-direction:column;gap:10px;margin-bottom:12px}
.bingo-card{background:var(--panel2);border:1px solid var(--line);border-radius:12px;padding:10px}
.card-title{font-size:12px;font-weight:bold;margin-bottom:6px;color:var(--gold)}
.card-grid{display:grid;grid-template-columns:repeat(5,1fr);gap:4px}
.c-cell{aspect-ratio:1;background:#06180d;border:1px solid var(--line);border-radius:6px;font-size:11px;font-weight:800;display:grid;place-items:center;cursor:pointer;color:var(--text)}
.c-cell.free{background:#483308;color:var(--gold)}
.c-cell.called{border-color:var(--teal)}
.c-cell.marked{background:#114624;color:var(--teal);border-color:var(--teal)}
.actions-row,.wallet-buttons-row{display:grid;grid-template-columns:1fr;gap:8px;margin-bottom:16px}
.primary-btn,.success-btn{padding:12px;border-radius:10px;font-weight:800;border:0;cursor:pointer;font-size:12px}
.primary-btn{background:var(--panel2);border:1px solid var(--line);color:var(--text)}
.success-btn{background:var(--teal);color:#041509}
input,select{width:100%;background:var(--panel2);border:1px solid var(--line);padding:10px;border-radius:8px;color:var(--text);margin-bottom:8px;font-size:12px}
.balance-display-box{background:var(--panel2);padding:10px;border-radius:8px;margin-bottom:10px;font-size:12px;border:1px solid var(--line)}
.sub-form{margin-top:10px;padding-top:10px;border-top:1px solid var(--line)}
.bottom-nav{position:fixed;bottom:0;left:0;right:0;background:#04150df0;backdrop-filter:blur(8px);border-top:1px solid var(--line);display:flex;justify-content:space-around;padding:8px;z-index:100}
.nav-btn{background:0;border:0;color:var(--muted);font-size:11px;cursor:pointer;font-weight:700}
.nav-btn.active{color:var(--teal)}
#toast{position:fixed;top:12px;left:50%;transform:translateX(-50%);background:#1b4a29;border:1px solid var(--teal);padding:6px 14px;border-radius:20px;font-size:11px;z-index:1000;display:none}
"""

APP_JS = r"""(() => {
const tg = window.Telegram?.WebApp; if (tg) { tg.ready(); tg.expand(); }
const initData = tg?.initData || '';
let model = null, lastSpokenNum = null, tab = 'bingo', clockOffset = 0;
const LETTERS = ['B', 'I', 'N', 'G', 'O'];

function toast(s) {
  const e = document.getElementById('toast');
  e.textContent = s; e.style.display = 'block';
  setTimeout(() => e.style.display = 'none', 2500);
}

function speakNumber(n) {
  if (!n || n === lastSpokenNum) return;
  lastSpokenNum = n;
  if ('speechSynthesis' in window) {
    const letter = LETTERS[Math.min(4, Math.floor((n - 1) / 15))];
    const u = new SpeechSynthesisUtterance(letter + ' ' + n);
    u.lang = 'en-US';
    window.speechSynthesis.speak(u);
  }
}

async function api(path, method = 'GET', body = null) {
  const o = { method, headers: { 'Content-Type': 'application/json', 'X-Telegram-Init-Data': initData } };
  if (body) o.body = JSON.stringify(body);
  const r = await fetch(path, o);
  const d = await r.json().catch(() => ({ error: 'Error' }));
  if (!r.ok) throw Error(d.error || 'Failed');
  return d;
}

function render() {
  if (!model) return;
  const isBingo = tab === 'bingo';
  const isSelecting = (model.phase === 'selecting');

  document.getElementById('round-title').textContent = 'ዙር #' + model.round;
  document.getElementById('play-bal').textContent = model.credits.toFixed(2);
  document.getElementById('win-bal').textContent = (model.win_credits || 0).toFixed(2);
  document.getElementById('w-play').textContent = model.credits.toFixed(2);
  document.getElementById('w-win').textContent = (model.win_credits || 0).toFixed(2);
  document.getElementById('current').textContent = model.current_number ?? '—';

  if (model.current_number) speakNumber(model.current_number);
  else lastSpokenNum = null;

  document.getElementById('selection-panel').style.display = (isBingo && isSelecting) ? 'block' : 'none';
  document.getElementById('live-game-panel').style.display = (isBingo && !isSelecting) ? 'block' : 'none';
  document.getElementById('cards-container').style.display = isBingo ? 'flex' : 'none';
  document.getElementById('game-actions').style.display = (isBingo && !isSelecting) ? 'grid' : 'none';
  document.getElementById('wallet-section').style.display = tab === 'wallet' ? 'block' : 'none';
  document.getElementById('rank-section').style.display = tab === 'rank' ? 'block' : 'none';
  document.getElementById('phase-banner').style.display = (isBingo && isSelecting) ? 'block' : 'none';

  const rem = Math.max(0, Math.floor(model.selection_ends_at - (Date.now() / 1000 + clockOffset)));
  document.getElementById('timer').textContent = rem;
  document.getElementById('my-chosen-count').textContent = 'የተመረጠ: ' + model.my_cards.length + '/4';
  document.getElementById('card-count').textContent = 'ካርዶች: ' + model.my_cards.length;

  const picker = document.getElementById('number-picker-grid');
  if (picker.children.length === 0) {
    for (let i = 1; i <= 600; i++) {
      const btn = document.createElement('button');
      btn.className = 'picker-btn'; btn.textContent = i;
      btn.onclick = () => action('/api/miniapp/select-card', { card_number: i });
      picker.appendChild(btn);
    }
  }
  if (isSelecting) {
    const takenSet = new Set(model.taken_cards || []);
    const mySet = new Set(model.my_cards || []);
    Array.from(picker.children).forEach(btn => {
      const n = Number(btn.textContent);
      btn.className = 'picker-btn';
      if (mySet.has(n)) btn.classList.add('mine');
      else if (takenSet.has(n)) btn.classList.add('taken');
    });
  }

  const calledSet = new Set(model.called_numbers);
  const mg75 = document.getElementById('master-grid-75');
  if (mg75.children.length === 0) {
    for (let r = 0; r < 5; r++) {
      const l = document.createElement('div'); l.className = 'm-letter'; l.textContent = LETTERS[r];
      mg75.appendChild(l);
      for (let c = 1; c <= 15; c++) {
        const n = r * 15 + c;
        const d = document.createElement('div');
        d.className = 'm-cell'; d.id = 'm75-' + n; d.textContent = n;
        mg75.appendChild(d);
      }
    }
  }
  for (let i = 1; i <= 75; i++) {
    const el = document.getElementById('m75-' + i);
    if (el) el.className = 'm-cell' + (calledSet.has(i) ? ' called' : '');
  }

  const ballsRow = document.getElementById('called-balls-row');
  ballsRow.innerHTML = '';
  model.called_numbers.slice().reverse().forEach(n => {
    const b = document.createElement('div'); b.className = 'mini-ball'; b.textContent = n;
    ballsRow.appendChild(b);
  });

  const cc = document.getElementById('cards-container'); cc.innerHTML = '';
  model.my_cards_data.forEach(item => {
    const cid = item.card_id;
    const marked = new Set(item.marked || ['FREE']);
    const box = document.createElement('div'); box.className = 'bingo-card';
    const title = document.createElement('div'); title.className = 'card-title';
    title.textContent = 'ካርድ #' + cid + ' (1-75)';
    box.appendChild(title);
    const grid = document.createElement('div'); grid.className = 'card-grid';
    item.matrix.forEach(row => row.forEach(n => {
      const btn = document.createElement('button'); btn.className = 'c-cell';
      if (n === 'FREE') { btn.classList.add('free'); btn.textContent = '★'; }
      else {
        btn.textContent = n;
        if (calledSet.has(n)) btn.classList.add('called');
        if (marked.has(n)) btn.classList.add('marked');
        btn.onclick = () => action('/api/miniapp/mark', { card_id: cid, number: n });
      }
      grid.appendChild(btn);
    }));
    box.appendChild(grid); cc.appendChild(box);
  });
}

async function refresh() {
  try {
    model = await api('/api/miniapp/game');
    clockOffset = model.server_time - Date.now() / 1000;
    render();
  } catch (e) {}
}

async function action(path, body = {}) {
  try {
    const d = await api(path, 'POST', body);
    toast(d.message || 'ተሳካ');
    await refresh();
    return d;
  } catch (e) { toast(e.message); }
}

window.switchTab = function (t) {
  tab = t;
  document.querySelectorAll('.nav-btn').forEach(b => b.classList.toggle('active', b.dataset.tab === t));
  render();
  if (t === 'rank') loadRank();
};

async function loadRank() {
  try {
    const d = await api('/api/miniapp/rank');
    const list = document.getElementById('rank-list'); list.innerHTML = '';
    d.rows.forEach((r, i) => {
      const div = document.createElement('div');
      div.style.cssText = 'padding:6px;border-bottom:1px solid #20552d';
      div.append((i + 1) + '. ' + r.name + ' — ');
      const b = document.createElement('b'); b.textContent = r.credits + ' ETB';
      div.appendChild(b); list.appendChild(div);
    });
  } catch (e) { toast(e.message); }
}

document.getElementById('claim').onclick = () => action('/api/miniapp/claim');
document.getElementById('show-deposit').onclick = () => {
  document.getElementById('deposit-form').style.display = 'block';
  document.getElementById('withdraw-form').style.display = 'none';
};
document.getElementById('show-withdraw').onclick = () => {
  document.getElementById('withdraw-form').style.display = 'block';
  document.getElementById('deposit-form').style.display = 'none';
};
document.getElementById('send-deposit').onclick = () => {
  const amount = Number(document.getElementById('deposit-amount').value);
  const method = document.getElementById('deposit-method').value;
  const reference = document.getElementById('deposit-reference').value.trim();
  if (!amount || reference.length < 3) { toast('መረጃ ይሙሉ'); return; }
  action('/api/miniapp/deposit-request', { amount, method, reference })
    .then(() => { document.getElementById('deposit-reference').value = ''; });
};
document.getElementById('send-withdraw').onclick = () => {
  const amount = Number(document.getElementById('withdraw-amount').value);
  const account_info = document.getElementById('withdraw-account').value.trim();
  if (!amount || account_info.length < 3) { toast('መረጃ ይሙሉ'); return; }
  action('/api/miniapp/withdraw-request', { amount, account_info }).then(() => {
    document.getElementById('withdraw-amount').value = '';
    document.getElementById('withdraw-account').value = '';
  });
};

refresh(); setInterval(refresh, 1000);
})();"""

ADMIN_JS = r"""const HEADERS = () => ({'Content-Type': 'application/json', 'X-Telegram-Init-Data': window.Telegram?.WebApp?.initData || ''});
function esc(s) { const d = document.createElement('div'); d.textContent = String(s ?? ''); return d.innerHTML; }

async function load() {
  const r = await fetch('/api/admin/overview', {headers: HEADERS()});
  const d = await r.json();
  if (!r.ok) { document.getElementById('req-list').textContent = d.error || 'Error'; return; }
  document.getElementById('u-count').textContent = d.users_count;
  document.getElementById('t-cred').textContent = d.total_credits;
  document.getElementById('p-count').textContent = d.pending_count;
  document.getElementById('req-list').innerHTML = d.requests.map(q => {
    const buttons = q.status === 'pending'
      ? `<button onclick="review(${Number(q.id)},'approve')" style="background:#39d66f;border:0;padding:6px 12px;border-radius:6px;font-weight:bold">አጽድቅ</button>
         <button onclick="review(${Number(q.id)},'reject')" style="background:#d63939;border:0;padding:6px 12px;border-radius:6px;font-weight:bold;color:#fff">እቢ</button>`
      : `<i>${esc(q.status)}</i>`;

    return `<div class="req"><b>#${Number(q.id)} · ${esc(q.amount)} ብር (${esc(q.method)})</b>
      <p>User: ${esc(q.user_id)} | Ref: ${esc(q.reference)}</p>${buttons}</div>`;
  }).join('');
}
async function review(id, decision) {
  await fetch(`/api/admin/deposit/${id}/review`, {method: 'POST', headers: HEADERS(), body: JSON.stringify({decision})});
  load();
}
document.getElementById('refresh').onclick = load; load();
"""


# ------------------------------ Flask API Routes -------------------------
@app.route("/static/style.css")
def serve_css():
    return Response(STYLE_CSS, mimetype="text/css")


@app.route("/static/app.js")
def serve_app_js():
    return Response(APP_JS, mimetype="application/javascript")


@app.route("/static/admin.js")
def serve_admin_js():
    return Response(ADMIN_JS, mimetype="application/javascript")


@app.route("/", methods=["GET"])
def index():
    return Response(INDEX_HTML, mimetype="text/html")


@app.route("/admin", methods=["GET"])
def admin_panel():
    return Response(ADMIN_HTML, mimetype="text/html")


@app.get("/api/miniapp/game")
def api_miniapp_game():
    user = api_user()
    if not user:
        return json_error("Unauthorized", 401)
    uid = user["id"]
    row = get_user(uid)
    with game_lock:
        my_cards = list(game_state["player_cards"].get(uid, []))
        my_cards_data = []
        for cid in my_cards:
            mat = game_state["card_matrices"].get((uid, cid))
            if mat is None:
                mat = generate_75_bingo_card(cid * 999 + uid)
                game_state["card_matrices"][(uid, cid)] = mat
            mset = list(game_state["marked"].setdefault((uid, cid), {"FREE"}))
            my_cards_data.append({"card_id": cid, "matrix": mat, "marked": mset})

        return jsonify({
            "round": game_state["round"],
            "phase": game_state["phase"],
            "selection_ends_at": game_state["selection_ends_at"],
            "server_time": time.time(),
            "prize": game_state["prize"],
            "called_numbers": list(game_state["called_numbers"]),
            "current_number": game_state["current_number"],
            "credits": row["credits"] if row else 100.0,
            "win_credits": row["win_credits"] if row else 0.0,
            "taken_cards": list(game_state["taken_card_numbers"]),
            "my_cards": my_cards,
            "my_cards_data": my_cards_data,
        })


@app.post("/api/miniapp/select-card")
def api_miniapp_select_card():
    user = api_user()
    if not user:
        return json_error("Unauthorized", 401)
    uid = user["id"]
    body = request.get_json(silent=True) or {}
    try:
        card_num = int(body.get("card_number"))
    except (TypeError, ValueError):
        return json_error("ሕጋዊ ያልሆነ ካርድ ቁጥር")
    if card_num < 1 or card_num > TOTAL_CARDS:
        return json_error("ሕጋዊ ያልሆነ ካርድ ቁጥር")

    with game_lock:
        if game_state["phase"] != "selecting":
            return json_error("የካርድ ምርጫ ጊዜ አላለፈም")

        my_cards = game_state["player_cards"].setdefault(uid, [])

        # Deselect (refund)
        if card_num in my_cards:
            my_cards.remove(card_num)
            game_state["taken_card_numbers"].discard(card_num)
            game_state["card_matrices"].pop((uid, card_num), None)
            game_state["marked"].pop((uid, card_num), None)
            game_state["prize"] -= CARD_COST
            update_credits(uid, CARD_COST, is_win=False)
            return jsonify({"message": "ካርዱ ተሰርዟል"})

        row = get_user(uid)
        if (row["credits"] if row else 0.0) < CARD_COST:
            return json_error("በቂ ድርሻ የለዎትም (10 ብር ያስፈልጋል)")
        if len(my_cards) >= MAX_CARDS_PER_PLAYER:
            return json_error("ከ 4 በላይ ካርድ መምረጥ አይችሉም")
        if card_num in game_state["taken_card_numbers"]:
            return json_error("ይህ ካርድ በሌላ ተወዳዳሪ ተይዟል")

        game_state["taken_card_numbers"].add(card_num)
        my_cards.append(card_num)
        game_state["prize"] += CARD_COST
        update_credits(uid, -CARD_COST, is_win=False)
        game_state["card_matrices"][(uid, card_num)] = generate_75_bingo_card(card_num * 999 + uid)
        game_state["marked"][(uid, card_num)] = {"FREE"}

    return jsonify({"message": f"ካርድ #{card_num} ተይዟል!"})


@app.post("/api/miniapp/mark")
def api_miniapp_mark():
    user = api_user()
    if not user:
        return json_error("Unauthorized", 401)
    uid = user["id"]
    body = request.get_json(silent=True) or {}
    cid, num = body.get("card_id"), body.get("number")
    with game_lock:
        if game_state["phase"] != "playing":
            return json_error("ጨዋታው ገና አልጀመረም")
        if cid not in game_state["player_cards"].get(uid, []):
            return json_error("ይህ ካርድ የእርስዎ አይደለም")
        if num != "FREE" and num not in game_state["called_numbers"]:
            return json_error("ቁጥሩ ገና አልተጠራም")
        mat = game_state["card_matrices"].get((uid, cid))
        if num != "FREE" and (not mat or not any(num in row for row in mat)):
            return json_error("ቁጥሩ በካርዱ ላይ የለም")
        game_state["marked"].setdefault((uid, cid), {"FREE"}).add(num)

        # auto-win as soon as a line is completed
        result = try_award(uid, user.get("username", ""))
    if result:
        return jsonify({"message": f"🏆 በረዶው አሸነፈ! {result[0]} ETB አሸንፈዋል!"})
    return jsonify({"message": "ምልክት ተደርጓል"})


@app.post("/api/miniapp/claim")
def api_miniapp_claim():
    user = api_user()
    if not user:
        return json_error("Unauthorized", 401)
    result = try_award(user["id"], user.get("username", ""))
    if not result:
        return json_error("BINGO አላደረጉም!")
    return jsonify({"message": f"🏆 በረዶው አሸነፈ! {result[0]} ETB አሸንፈዋል!"})


@app.post("/api/miniapp/deposit-request")
def api_miniapp_deposit():
    user = api_user()
    if not user:
        return json_error("Unauthorized", 401)
    body = request.get_json(silent=True) or {}
    amt = to_positive_float(body.get("amount"))
    method = str(body.get("method") or "")[:30]
    ref = str(body.get("reference") or "").strip()[:100]
    if not amt or not ref or method not in ("Telebirr", "CBE Birr"):
        return json_error("መረጃ ይሙሉ")
    with closing(db()) as con:
        con.execute("INSERT INTO deposit_requests(user_id, method, amount, reference) VALUES(?,?,?,?)",
                    (user["id"], method, amt, ref))
        con.commit()
    return jsonify({"message": "የክፍያ ጥያቄ ተልዟል"})


@app.post("/api/miniapp/withdraw-request")
def api_miniapp_withdraw():
    user = api_user()
    if not user:
        return json_error("Unauthorized", 401)
    body = request.get_json(silent=True) or {}
    amt = to_positive_float(body.get("amount"))
    account_info = str(body.get("account_info") or "").strip()[:200]
    if not amt or not account_info:
        return json_error("መረጃ ይሙሉ")
    with game_lock:  # serialize balance check + deduction
        row = get_user(user["id"])
        if (row["win_credits"] if row else 0.0) < amt:
            return json_error("በቂ የሽልማት ድርሻ (Win Wallet) የለዎትም")
        update_credits(user["id"], -amt, is_win=True)
        with closing(db()) as con:
            con.execute("INSERT INTO withdraw_requests(user_id, account_info, amount) VALUES(?,?,?)",
                        (user["id"], account_info, amt))
            con.commit()
    return jsonify({"message": "የማውጣት ጥያቄ ተልዟል"})


@app.get("/api/miniapp/rank")
def miniapp_rank():
    with closing(db()) as con:
        rows = con.execute(
            "SELECT username, first_name, win_credits FROM users ORDER BY win_credits DESC LIMIT 10").fetchall()
        return jsonify({"rows": [
            {"name": ("@" + r["username"]) if r["username"] else (r["first_name"] or "Player"),
             "credits": r["win_credits"]} for r in rows]})


@app.get("/api/admin/overview")
def admin_overview():
    user = api_admin()
    if not user:
        return json_error("Admin access denied", 403)
    with closing(db()) as con:
        uc = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        tc = con.execute("SELECT COALESCE(SUM(credits+win_credits),0) FROM users").fetchone()[0]
        pc = con.execute("SELECT COUNT(*) FROM deposit_requests WHERE status='pending'").fetchone()[0]
        reqs = con.execute("SELECT * FROM deposit_requests ORDER BY id DESC LIMIT 50").fetchall()
        return jsonify({"users_count": uc, "total_credits": tc, "pending_count": pc,
                        "requests": [dict(r) for r in reqs]})


@app.post("/api/admin/deposit/<int:req_id>/review")
def admin_review(req_id):
    user = api_admin()
    if not user:
        return json_error("Admin access denied", 403)
    body = request.get_json(silent=True) or {}
    decision = body.get("decision")
    if decision not in ("approve", "reject"):
        return json_error("Invalid decision")
    with closing(db()) as con:
        row = con.execute("SELECT * FROM deposit_requests WHERE id=?", (req_id,)).fetchone()
        if not row or row["status"] != "pending":
            return json_error("Invalid request")
        status = "approved" if decision == "approve" else "rejected"
        con.execute(
            "UPDATE deposit_requests SET status=?, reviewed_at=CURRENT_TIMESTAMP, reviewed_by=? WHERE id=?",
            (status, int(user["id"]), req_id))
        if decision == "approve":
            con.execute("UPDATE users SET credits=credits+? WHERE user_id=?", (row["amount"], row["user_id"]))
        con.commit()
    return jsonify({"ok": True, "message": "ተረጋግጧል"})


@app.post("/telegram-webhook" if not WEBHOOK_SECRET_PATH else "/telegram-webhook/" + WEBHOOK_SECRET_PATH)
def telegram_webhook():
    try:
        payload = request.get_json(force=True)
        update = Update.de_json(payload, bot_application.bot)
        if bot_loop.is_running():
            asyncio.run_coroutine_threadsafe(bot_application.process_update(update), bot_loop)
        return "OK"
    except Exception:
        logger.exception("webhook error")
        return "ERROR", 500


@app.route("/<path:path>", methods=["GET", "HEAD"])
def fallback(path):
    return jsonify({"error": "Not found"}), 404


def run_bot_loop():
    asyncio.set_event_loop(bot_loop)
    bot_loop.run_forever()


def start_bot():
    global bot_thread
    if bot_thread and bot_thread.is_alive():
        return
    bot_thread = threading.Thread(target=run_bot_loop, daemon=True, name="bot-loop")
    bot_thread.start()

    async def initialize():
        await bot_application.initialize()
        await bot_application.start()
        if PUBLIC_URL:
            wh = "/telegram-webhook" + (("/" + WEBHOOK_SECRET_PATH) if WEBHOOK_SECRET_PATH else "")
            await bot_application.bot.set_webhook(PUBLIC_URL + wh)

    asyncio.run_coroutine_threadsafe(initialize(), bot_loop)


start_bot()
threading.Thread(target=game_loop_worker, daemon=True, name="game-loop").start()
threading.Thread(target=auto_caller_loop, daemon=True, name="auto-caller").start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
