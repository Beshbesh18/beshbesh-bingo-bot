# -*- coding: utf-8 -*-
"""
Besh Besh Bingo — Telegram bot + Telegram Mini App
Updated to support up to 600 cards layout matching the video design.
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

MAX_CARDS_PER_ROUND = 600
CARD_COST = 10
STARTING_CREDITS = 100
MAX_CARDS_PER_PLAYER = 4
CALL_INTERVAL_SECONDS = 3

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
game_lock = threading.RLock()

# ------------------------------ Database -----------------------------
def db():
    conn = sqlite3.connect(DATABASE_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
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
    while True:
        try:
            time.sleep(CALL_INTERVAL_SECONDS)
            with game_lock:
                if not game_state["is_active"]:
                    continue
                number = call_next_number()
                if number is None or len(game_state["called_numbers"]) >= 75:
                    game_state["is_active"] = False
        except Exception:
            logger.exception("Auto caller failed")
            time.sleep(1)

def active_player_count():
    total_cards = sum(len(c_list) for c_list in game_state["cards"].values())
    return total_cards

# ------------------------------ Localization -------------------------
TEXT = {
    "am": {
        "welcome": "🎰 እንኳን ደህና መጡ ወደ Flash Bingo!\n\nይህ እስከ 600 ካርዶች ማስተናገድ የሚችል የቢንጎ ጨዋታ ነው።",
        "play": "🎮 ቢንጎ ጨዋታ",
        "balance": "💰 ሂሳብ",
        "rules": "📜 ሕጎች",
        "wallet": "💳 ዋሌት / ክሬዲት",
        "rank": "🏆 ደረጃ",
        "language": "🌐 ቋንቋ",
        "support": "🆘 እርዳታ",
        "choose": "ከታች ያሉትን አማራጮች ይምረጡ።",
    },
    "en": {
        "welcome": "🎰 Welcome to Flash Bingo!\n\nThis is a high-capacity bingo game supporting up to 600 cards.",
        "play": "🎮 Play Bingo",
        "balance": "💰 Balance",
        "rules": "📜 Rules",
        "wallet": "💳 Wallet / Credits",
        "rank": "🏆 Rank",
        "language": "🌐 Language",
        "support": "🆘 Help",
        "choose": "Choose an option below.",
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
            InlineKeyboardButton("⚡ Open Flash Bingo", web_app=WebAppInfo(url=webapp_url()))
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
    await update.effective_message.reply_text("Flash Bingo", reply_markup=main_keyboard(user.id))

bot_application.add_handler(CommandHandler("start", start))

# ------------------------------ HTML / Frontend Assets ---------------
INDEX_HTML = r"""<!doctype html>
<html lang="am"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no"><meta name="theme-color" content="#06180d"><title>Flash Bingo</title><script src="https://telegram.org/js/telegram-web-app.js"></script><link rel="stylesheet" href="/static/style.css"></head>
<body><main class="app-shell">
<header class="topbar">
  <div class="brand-title">⚡ <b>Flash Bingo</b></div>
  <div class="live-pill"><i></i> LIVE</div>
</header>
<section class="top-matrix" id="top-matrix">
  <!-- 1 to 600 / 75 numbers dashboard matching video -->
</section>
<section class="wallet-panel">
  <div><div class="muted">ክሬዲት</div><div class="balance"><span id="credits">—</span></div></div>
  <div class="round-box"><span class="muted">ተጫዋች ካርዶች</span><strong id="players-cards">0 / 600</strong></div>
</section>
<section class="stats-row">
  <div class="stat"><span>🏆 ሽልማት</span><strong id="prize">0 pts</strong></div>
  <div class="stat"><span>👥 ጠቅላላ ካርዶች</span><strong id="total-cards-count">0</strong></div>
</section>
<section class="draw-panel">
  <div class="current-ball" id="current">—</div>
  <div class="draw-meta">
    <span class="muted">የተጠሩ ቁጥሮች</span>
    <div class="number-strip" id="recent"></div>
  </div>
</section>
<section class="game-panel">
  <div class="section-heading"><h2>የእርስዎ ካርዶች (እስከ 4)</h2><span id="card-count">0 / 4</span></div>
  <div id="cards-container" class="cards-container"></div>
  <div class="actions">
    <button id="buy-card" class="secondary">＋ ካርድ ግዢ (10)</button>
    <button id="claim" class="primary">🏆 BINGO! አሸነፍኩ</button>
  </div>
</section>
<section class="game-panel deposit-panel">
  <h2>💳 ክሬዲት መሙላት (Telebirr / CBE)</h2>
  <div class="payment-info"><strong>Telebirr / CBE:</strong> 0918719332 · Enyew</div>
  <label class="form-label">መጠን (ብር)</label><input id="deposit-amount" type="number" placeholder="100">
  <label class="form-label">የማረጋገጫ ቁጥር (Reference)</label><input id="deposit-reference" placeholder="Ref No">
  <button id="send-deposit" class="primary full-width">ማመልከቻ ላክ</button>
</section>
<nav class="bottom-nav">
  <button class="nav-active" onclick="window.scrollTo({top:0,behavior:'smooth'})">🎰<span>BINGO</span></button>
  <button onclick="document.querySelector('.deposit-panel').scrollIntoView({behavior:'smooth'})">💳<span>WALLET</span></button>
</nav></main><div id="toast"></div><script src="/static/app.js"></script></body></html>"""

STYLE_CSS = r""":root{color-scheme:dark;--bg:#06180d;--panel:#0b2113;--panel2:#102d19;--line:#20552d;--text:#f4f7f4;--muted:#a3b8a8;--gold:#ffe629;--teal:#39d66f}
*{box-sizing:border-box}body{margin:0;background:var(--bg);font-family:sans-serif;color:var(--text);padding-bottom:50px}
.app-shell{max-width:600px;margin:auto;padding:12px}.topbar{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px}
.top-matrix{display:grid;grid-template-columns:repeat(15,1fr);gap:2px;background:var(--panel);padding:6px;border-radius:10px;margin-bottom:10px;max-height:120px;overflow-y:auto}
.m-cell{font-size:9px;background:var(--panel2);text-align:center;padding:3px 0;border-radius:3px;color:var(--muted)}
.m-cell.called{background:var(--gold);color:#000;font-weight:bold}
.wallet-panel,.draw-panel,.game-panel{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:12px;margin-bottom:10px;display:flex;justify-content:space-between;align-items:center}
.balance{font-size:22px;font-weight:bold;color:var(--gold)}
.current-ball{width:54px;height:54px;background:var(--gold);color:#000;border-radius:50%;display:grid;place-items:center;font-size:20px;font-weight:bold}
.cards-container{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:10px}
.mini-card{background:var(--panel2);border:1px solid
