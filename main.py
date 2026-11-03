# -*- coding: utf-8 -*-
"""
Besh Besh Bingo — Telegram bot + Telegram Mini App
Rebuilt as a complete main.py from the code shared in chat.

Environment variables:
  TOKEN                  Required Telegram bot token
  RENDER_EXTERNAL_URL    Render public URL, e.g. https://your-service.onrender.com
  WEBHOOK_SECRET_PATH    Optional random webhook path secret
  ADMIN_IDS              Comma-separated Telegram user IDs
  DATABASE_PATH          Optional SQLite path (default: bingo.sqlite3)
  LIVE_CHAT_ID           Optional Telegram chat ID for announcements
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
    return game_state["cards"].setdefault(int(user_id), [])

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
                active = game_state["is_active"]
            if not active:
                continue
            number = call_next_number()
            if number is None or len(game_state["called_numbers"]) >= 75:
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
        "welcome": "🎰 እንኳን ደህና መጡ ወደ Besh Besh Bingo!\n\nይህ በቨርቹዋል ካርድ የሚሠራ የቢንጎ ጨዋታ ነው።",
        "play": "🎮 ቢንጎ ጨዋታ",
        "balance": "💰 ብር ሂሳብ",
        "rules": "📜 ሕግ እና ደንቦች",
        "wallet": "💳 Wallet / ካርድ",
        "rank": "🏆 ደረጃ",
        "language": "🌐 ቋንቋ / Language",
        "support": "ជំនួយ",
        "admin": "🛡 አድሚን",
        "choose": "ከታች ያሉትን አማራጮች ይምረጡ።",
        "balance_msg": "💰 የካርድ ሂሳብዎ: {credits}",
        "rules_msg": "📜 ሕጎች\n• ካርድ ዋጋ: 10 ካርድ\n• በአንድ ተጫዋች እስከ 4 ካርዶች\n• በተጠሩ ቁጥሮች ላይ ብቻ ምልክት አድርግ\n• ካርዶች በቨርቹዋል ናቸውና የገንዘብ ዋጋ የላቸውም",
        "no_credit": "በቂ ካርድ የለዎትምም።",
        "bought": "✅ ካርድ ተገዝቷል።",
        "language_set": "ቋንቋው ወደ አማርኛ ተስተካክሏል።",
    },
    "en": {
        "welcome": "🎰 Welcome to Besh Besh Bingo!\n\nThis is a virtual-credit bingo game.",
        "play": "🎮 Play Bingo",
        "balance": "💰 Balance",
        "rules": "📜 Rules",
        "wallet": "💳 Wallet / Credits",
        "rank": "🏆 Rank",
        "language": "🌐 Language / ቋንቋ",
        "support": "Help",
        "admin": "🛡 Admin",
        "choose": "Choose an option below.",
        "balance_msg": "💰 Your balance: {credits} credits",
        "rules_msg": "📜 Rules\n• Card cost: 10 credits\n• Up to 4 cards per player\n• Only called numbers can be marked\n• Credits are virtual and have no cash value.",
        "no_credit": "You do not have enough credits.",
        "bought": "✅ Card purchased.",
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
            InlineKeyboardButton("🎮 Open Bingo / ቢንጎ ክፍት", web_app=WebAppInfo(url=webapp_url()))
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
    if msg in (t["play"], "🎮 ቢንጎ ጨዋታ", "🎮 Play Bingo"):
        markup = start_keyboard()
        if markup:
            await update.effective_message.reply_text("Besh Besh Bingo", reply_markup=markup)
        else:
            await update.effective_message.reply_text("Set RENDER_EXTERNAL_URL to enable the Mini App.")
    elif msg in (t["balance"], "💰 ብር ሂሳብ", "💰 Balance"):
        row = get_user(user.id)
        await update.effective_message.reply_text(tr(user.id, "balance_msg", credits=row["credits"]))
    elif msg in (t["rules"], "📜 ሕግ እና ደንቦች", "📜 Rules"):
        await update.effective_message.reply_text(tr(user.id, "rules_msg"))
    elif msg in (t["wallet"], "💳 Wallet / ካርድ"):
        await update.effective_message.reply_text(
            "💳 Wallet\nTelebirr: 0918719332 · Enyew\nCBE Birr: 0918719332 · Enyew\n\n"
            "ክፍያ ከፈጸሙ በኋላ Mini App ውስጥ የክፍያ ጥያቄ ይለሉ። አድሚን ሲያረጋግጥ ካርድ ይጨመራል።")
    elif msg in (t["rank"], "🏆 ደረጃ", "🏆 Rank"):
        with closing(db()) as con:
            rows = con.execute("SELECT username, first_name, credits FROM users ORDER BY credits DESC LIMIT 10").fetchall()
        lines = ["🏆 Top Players"]
        for i, r in enumerate(rows, 1):
            name = ("@" + r["username"]) if r["username"] else (r["first_name"] or "Player")
            lines.append(f"{i}. {name} — {r['credits']} pts")
        await update.effective_message.reply_text("\n".join(lines))
    elif msg in (t["language"], "🌐 ቋንቋ / Language"):
        await language_command(update, context)
    elif msg in (t["support"], "ជំនួយ", "Help"):
        await update.effective_message.reply_text("ជំនួយ / Help\nContact the bot administrator for support.")
    else:
        await update.effective_message.reply_text(tr(user.id, "choose"), reply_markup=main_keyboard(user.id))

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        await update.effective_message.reply_text("🚫 Unauthorized.")
        return
    if PUBLIC_URL:
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("🛡 Open Admin Panel", web_app=WebAppInfo(url=PUBLIC_URL + "/admin"))
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
<header class="topbar"><div class="brand-mark">🎰</div><div><div class="eyebrow">HABESHA GAME</div><h1>BESH BESH <span>BINGO</span></h1></div><div class="live-pill"><i></i> LIVE</div></header>
<section class="wallet-panel"><div><div class="muted">የካርድ ሂሳብ</div><div class="balance"><span id="credits">—</span> <small>PTS</small></div></div><div class="round-box"><span class="muted">ዙር</span><strong id="round">#1</strong></div></section>
<section class="stats-row"><div class="stat"><span>🏆 የዙሩ ሽልማት</span><strong id="prize">0 pts</strong></div><div class="stat"><span>👥 ተጫዋቾች</span><strong id="players">0</strong></div></section>
<section class="draw-panel"><div><span class="muted">የተጠራው ቁጥር</span><div class="last-number" id="current">—</div></div><div class="draw-meta"><span class="muted">የተጠሩ ቁጥሮች</span><strong id="called-count">0 / 75</strong><div class="number-strip" id="recent"></div></div></section>
<section class="game-panel"><div class="section-heading"><div><span class="eyebrow">YOUR GAME</span><h2>የቢንጎ ካርድ</h2></div><span class="card-count" id="card-count">0 / 4</span></div>
<div id="card-tabs" class="card-tabs"></div><div class="bingo-grid" id="bingo-grid"><div class="loading">ጨዋታው በመጀመር ላይ…</div></div>
<div class="legend"><span><i class="dot called"></i> የተጠራ</span><span><i class="dot marked"></i> የተመዘገበ</span></div>
<div class="actions"><button id="buy-card" class="secondary">＋ ካርድ ግዢ (10)</button><button id="claim" class="primary">🏆 BINGO! አግኝ…</button></div><p class="hint" id="hint">በተጠሩ ቁጥሮች ላይ ብቻ ምልክት አድርግ።</p></section>
<section class="called-panel"><h3>የተጠሩ ቁጥሮች</h3><div id="all-called" class="called-list">—</div></section>
<section class="game-panel deposit-panel"><div class="section-heading"><div><span class="eyebrow">WALLET</span><h2>💳 ካርድ መሙላት</h2></div></div>
<p class="hint">ክፍያዎን ከፈጸሙ በኋላ የክፍያ ማረጋገጫውን ማስገባት ይችላሉ። ካርድ የሚመነጨው በአድሚን ከተረጋገጠ በኋላ ብቻ ነው።</p>
<div class="payment-info"><strong>Telebirr</strong><span>0918719332 · Enyew</span></div><div class="payment-info"><strong>CBE Birr</strong><span>0918719332 · Enyew</span></div>
<label class="form-label" for="deposit-method">የክፍያ አይነት</label><select id="deposit-method"><option>Telebirr</option><option>CBE Birr</option></select>
<label class="form-label" for="deposit-amount">የክፍያ መጠን (ብር)</label><input id="deposit-amount" type="number" min="1" max="1000000" inputmode="numeric" placeholder="ለማሳያ 100">
<label class="form-label" for="deposit-reference">የክፍያ ማረጋገጫ ቁጥር</label><input id="deposit-reference" maxlength="100" placeholder="የክፍያ ማረጋገጫ ቁጥር">
<button id="send-deposit" class="primary full-width">የክፍያ ጥያቄ ላክ</button></section>
<nav class="bottom-nav"><button class="nav-active" onclick="window.scrollTo({top:0,behavior:'smooth'})">🎰<span>BINGO</span></button><button onclick="document.querySelector('.deposit-panel').scrollIntoView({behavior:'smooth'})">■<span>WALLET</span></button><button onclick="loadRank()">🏆<span>RANK</span></button></nav>
<footer>ይህ በቨርቹዋል ካርድ ጨዋታ ነው። ካርዶች የገንዘብ ዋጋ የላቸውም።</footer></main><div id="toast" role="status"></div><script src="/static/app.js"></script></body></html>"""

ADMIN_HTML = r"""<!doctype html><html lang="am"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Besh Besh Bingo Admin</title><script src="https://telegram.org/js/telegram-web-app.js"></script><link rel="stylesheet" href="/static/style.css"><style>.admin-wrap{max-width:760px;margin:auto;padding:18px}.admin-title{display:flex;justify-content:space-between;align-items:center;gap:12px}.metrics{display:grid;grid-template-columns:repeat(3,1fr);gap:9px;margin:18px 0}.metric{background:#14291b;border:1px solid #285236;border-radius:14px;padding:13px}.metric span{display:block;color:#a6c5ad;font-size:12px}.metric strong{display:block;font-size:22px;margin-top:7px}.request{background:#102217;border:1px solid #285236;border-radius:14px;padding:14px;margin:12px 0;overflow-wrap:anywhere}.request-actions{display:flex;gap:8px;margin-top:12px}.request-actions button{flex:1;padding:11px;border:0;border-radius:10px;font-weight:700}.approve{background:#35c98b;color:#082719}.reject{background:#ff6575;color:#330a10}.warning{background:#392d1c;color:#ffdc9a;border-radius:12px;padding:12px;font-size:13px}.refresh{padding:10px;border-radius:9px}</style></head><body><main class="admin-wrap"><header class="admin-title"><div><div class="eyebrow">BESH BESH BINGO</div><h1>🛡 የአድሚን ፓነል</h1></div><button id="refresh" class="secondary">⟳ አዲስ</button></header><p class="warning">ክፍያውን በTelebirr/CBE Birr በትክክል ማረጋገጥዎን ይርሱ።</p><section class="metrics"><div class="metric"><span>ተጫዋቾች</span><strong id="users">—</strong></div><div class="metric"><span>ጠቅላላ ካርድ</span><strong id="credits">—</strong></div><div class="metric"><span>በመጠባበቅ ላይ</span><strong id="pending">—</strong></div></section><h2>💳 የክፍያ ጥያቄዎች</h2><div id="requests">በመጀመር ላይ…</div><div id="toast"></div></main><script src="/static/admin.js"></script></body></html>"""

APP_JS = r"""(() => {
const tg=window.Telegram?.WebApp;if(tg){tg.ready();tg.expand();try{tg.setHeaderColor('#071a0d');tg.setBackgroundColor('#06180d')}catch(e){}}
const initData=tg?.initData||'';let model=null,activeCard=0,busy=false;const $=id=>document.getElementById(id);
function toast(s){const e=$('toast');e.textContent=s;e.classList.add('show');setTimeout(()=>e.classList.remove('show'),2600)}
async function api(path,method='GET',body=null){const o={method,headers:{'Content-Type':'application/json','X-Telegram-Init-Data':initData}};if(body)o.body=JSON.stringify(body);const r=await fetch(path,o);const d=await r.json().catch(()=>({error:'Server response error'}));if(!r.ok)throw Error(d.error||'Request failed');return d}
function label(n){if(n==='FREE')return 'FREE';return (n<=15?'B':n<=30?'I':n<=45?'N':n<=60?'G':'O')+n}
function esc(s){return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function render(){if(!model)return;$('credits').textContent=model.credits;$('round').textContent='#'+model.round;$('prize').textContent=model.prize+' pts';$('players').textContent=model.player_count;$('current').textContent=model.current_number??'—';$('called-count').textContent=model.called_numbers.length+' / 75';$('card-count').textContent=model.cards.length+' / 4';
$('recent').innerHTML=model.called_numbers.slice(-6).reverse().map(n=>`<span class="number-chip">${label(n)}</span>`).join('')||'<span class="muted">ሂሳብ የለም</span>';
$('all-called').innerHTML=model.called_numbers.length?model.called_numbers.map(n=>`<span>${label(n)}</span>`).join(''):'—';
$('card-tabs').innerHTML=model.cards.map((_,i)=>`<button class="card-tab ${i===activeCard?'active':''}" data-card="${i}">ካርድ ${i+1}</button>`).join('');
$('card-tabs').querySelectorAll('button').forEach(b=>b.onclick=()=>{activeCard=+b.dataset.card;render()});
const grid=$('bingo-grid');grid.innerHTML='';['B','I','N','G','O'].forEach(x=>{const e=document.createElement('div');e.className='cell head';e.textContent=x;grid.appendChild(e)});
const card=model.cards[activeCard],marked=new Set(model.marked[activeCard]||['FREE']),called=new Set(model.called_numbers);
if(!card){grid.innerHTML='<div class="loading">ካርድ ይክፈቱ እና ይምረጡ።</div>'}
else card.forEach((row,r)=>row.forEach((n,c)=>{const e=document.createElement('button');e.className='cell';if(n==='FREE'){e.classList.add('free');e.textContent='★'}else{e.textContent=n;if(called.has(n))e.classList.add('called');if(marked.has(n))e.classList.add('marked');e.disabled=!called.has(n)||marked.has(n)||busy;e.onclick=()=>action('/api/miniapp/mark',{card_index:activeCard,number:n})}grid.appendChild(e)}));
$('buy-card').disabled=busy||model.cards.length>=4||model.credits<10;$('claim').disabled=busy||!model.cards.length}
async function refresh(showError=false){try{model=await api('/api/miniapp/game');activeCard=Math.min(activeCard,Math.max(0,model.cards.length-1));render()}catch(e){if(showError)toast(e.message);$('bingo-grid').innerHTML='<div class="loading">'+esc(e.message)+'</div>'}}
async function action(path,body={}){if(busy)return;busy=true;render();try{const d=await api(path,'POST',body);toast(d.message||'ተሳካ ድርጊት');await refresh()}catch(e){toast(e.message)}finally{busy=false;render()}}
$('buy-card').onclick=()=>action('/api/miniapp/buy-card');$('claim').onclick=()=>action('/api/miniapp/claim');
$('send-deposit').onclick=()=>{const amount=Number($('deposit-amount').value),method=$('deposit-method').value,reference=$('deposit-reference').value.trim();if(!Number.isInteger(amount)||amount<1){toast('ትክክለኛ የክፍያ መጠን ያስገቡ');return}if(reference.length<3){toast('የክፍያ ማረጋገጫ ያስገቡ');return}action('/api/miniapp/deposit-request',{amount,method,reference}).then(()=>{$('deposit-reference').value=''})};
window.loadRank=async()=>{try{const d=await api('/api/miniapp/rank');toast(d.rows.map((r,i)=>`${i+1}. ${r.name}: ${r.credits}`).join(' | ')||'No rank data')}catch(e){toast(e.message)}};
refresh(true);setInterval(()=>refresh(false),4000);
})();"""

ADMIN_JS = r"""(() => {
const tg=window.Telegram?.WebApp;if(tg){tg.ready();tg.expand()}const initData=tg?.initData||'';const $=id=>document.getElementById(id);
function toast(s){const e=$('toast');e.textContent=s;setTimeout(()=>e.textContent='',3000)}
async function api(path,method='GET',body=null){const o={method,headers:{'Content-Type':'application/json','X-Telegram-Init-Data':initData}};if(body)o.body=JSON.stringify(body);const r=await fetch(path,o),d=await r.json().catch(()=>({error:'Bad response'}));if(!r.ok)throw Error(d.error||'Request failed');return d}
function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function render(d){$('users').textContent=d.users_count;$('credits').textContent=d.total_credits;$('pending').textContent=d.pending_count;const root=$('requests');if(!d.requests.length){root.innerHTML='<p>ምንም የክፍያ ጥያቄ የለም።</p>';return}root.innerHTML=d.requests.map(r=>`<article class="request"><strong>#${r.id} · ${esc(r.method)} · ${r.amount} ብር</strong><p>User ID: <b>${r.user_id}</b></p><p>Reference: <b>${esc(r.reference)}</b></p><p>Status: ${esc(r.status)} · ${esc(r.created_at)}</p>${r.status==='pending'?`<div class="request-actions"><button class="approve" data-id="${r.id}" data-decision="approve">✓ አጽድቅና ካርድ ጨምር</button><button class="reject" data-id="${r.id}" data-decision="reject">✕ ስረዝ</button></div>`:''}</article>`).join('');root.querySelectorAll('button[data-id]').forEach(b=>b.onclick=()=>review(+b.dataset.id,b.dataset.decision,b))}
async function load(){try{render(await api('/api/admin/overview'))}catch(e){$('requests').textContent=e.message+' — ይህን ገጽ Telegram Mini App ወይም በተፈቀደ አድሚን መርሃል ይክፈቱ።'}}
async function review(id,decision,b){if(!confirm(decision==='approve'?'ክፍያውን በትክክል አረጋግጠዋል?':'ይህን ጥያቄ መሰረዝ ይፈልጋሉ?'))return;b.disabled=true;try{const d=await api(`/api/admin/deposit/${id}/review`,'POST',{decision});toast(d.message);load()}catch(e){toast(e.message);b.disabled=false}}
$('refresh').onclick=load;load();setInterval(load,15000);
})();"""

STYLE_CSS = r""":root{color-scheme:dark;--bg:#06180d;--panel:#0b2113;--panel2:#102d19;--line:#20552d;--text:#f4f7f4;--muted:#a3b8a8;--gold:#ffe629;--teal:#39d66f}*{box-sizing:border-box}body{margin:0;background:radial-gradient(ellipse at top,#12351d 0,#06180d 58%);font-family:system-ui,-apple-system,"Noto Sans Ethiopic",sans-serif;color:var(--text);min-height:100vh}.app-shell{max-width:520px;margin:auto;padding:16px 12px 30px}.topbar{display:flex;align-items:center;gap:11px;margin:4px 2px 17px}.brand-mark{display:grid;place-items:center;width:46px;height:46px;border-radius:15px;background:linear-gradient(135deg,#ffe629,#f59e0b);font-size:25px}.eyebrow{font-size:10px;letter-spacing:2px;color:#5be47b;font-weight:800}.topbar h1{font-size:17px;margin:2px 0 0}.topbar h1 span{color:var(--gold)}.live-pill{margin-left:auto;font-size:10px;color:#ff8585;background:#ff646414;border:1px solid #ff64644a;border-radius:99px;padding:6px 9px}.live-pill i{display:inline-block;width:6px;height:6px;border-radius:50%;background:#ff6c7c;margin-right:5px}.wallet-panel,.draw-panel,.game-panel,.called-panel{background:var(--panel);border:1px solid var(--line);border-radius:18px;padding:15px;margin-bottom:14px}.wallet-panel{display:flex;justify-content:space-between;align-items:center}.muted{color:var(--muted);font-size:12px}.balance{font-size:24px;font-weight:800;color:var(--gold)}.round-box{text-align:right}.stats-row{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:14px}.stat{background:var(--panel2);border:1px solid var(--line);border-radius:14px;padding:12px}.stat span{display:block;font-size:12px;color:var(--muted)}.stat strong{display:block;font-size:18px;margin-top:4px;color:var(--teal)}.draw-panel{display:grid;grid-template-columns:auto 1fr;gap:15px;align-items:center}.last-number{width:62px;height:62px;border-radius:16px;background:linear-gradient(135deg,#39d66f,#0b6329);display:grid;place-items:center;font-size:24px;font-weight:900;color:#041509;margin-top:4px}.draw-meta{overflow:hidden}.number-strip{display:flex;gap:6px;margin-top:6px;overflow-x:auto}.number-chip{background:var(--panel2);border:1px solid var(--line);border-radius:8px;padding:4px 8px;font-size:12px;font-weight:700}.section-heading{display:flex;justify-content:space-between;align-items:center;margin-bottom:12px}.section-heading h2{font-size:15px;margin:2px 0 0}.card-count{font-size:12px;background:var(--panel2);padding:4px 8px;border-radius:8px;border:1px solid var(--line)}.card-tabs{display:flex;gap:6px;margin-bottom:12px;overflow-x:auto}.card-tab{background:var(--panel2);border:1px solid var(--line);color:var(--text);padding:6px 12px;border-radius:8px;font-size:12px;cursor:pointer}.card-tab.active{background:var(--teal);color:#06180d;font-weight:800}.bingo-grid{display:grid;grid-template-columns:repeat(5,1fr);gap:6px;margin-bottom:12px}.cell{aspect-ratio:1;background:var(--panel2);border:1px solid var(--line);border-radius:10px;font-size:14px;font-weight:800;color:var(--text);display:grid;place-items:center;cursor:pointer}.cell.head{background:#173d22;color:var(--gold);cursor:default}.cell.free{background:#483308;color:var(--gold)}.cell.called{border-color:#39d66f}.cell.marked{background:#114624;color:#39d66f;border-color:#39d66f}.loading{grid-column:span 5;text-align:center;padding:24px;color:var(--muted)}.legend{display:flex;gap:16px;font-size:12px;color:var(--muted);margin-bottom:12px}.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:4px}.dot.called{background:#39d66f}.dot.marked{background:#ff6e6e}.actions{display:grid;grid-template-columns:1fr 1fr;gap:8px}.primary,.secondary{padding:12px;border-radius:12px;font-weight:800;border:0;cursor:pointer}.primary{background:var(--teal);color:#06180d}.secondary{background:var(--panel2);border:1px solid var(--line);color:var(--text)}.full-width{width:100%;margin-top:10px}.hint{font-size:11px;color:var(--muted);margin-top:8px;line-height:1.4}.called-panel{margin-bottom:14px}.called-list{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px;max-height:100px;overflow-y:auto}.called-list span{background:var(--panel2);border:1px solid var(--line);padding:4px 8px;border-radius:6px;font-size:11px}.deposit-panel{margin-bottom:70px}.form-label{display:block;font-size:12px;color:var(--muted);margin:10px 0 4px}input,select{width:100%;background:var(--panel2);border:1px solid var(--line);padding:10px;border-radius:10px;color:var(--text)}.bottom-nav{position:fixed;bottom:0;left:0;right:0;background:#06180df0;backdrop-filter:blur(10px);border-top:1px solid var(--line);display:flex;justify-content:space-around;padding:8px;z-index:100}.bottom-nav button{background:0;border:0;color:var(--muted);font-size:10px;display:flex;flex-direction:column;align-items:center;gap:2px;cursor:pointer}.bottom-nav button.nav-active{color:var(--teal)}footer{text-align:center;font-size:11px;color:var(--muted);margin-top:20px}#toast{position:fixed;top:16px;left:50%;transform:translateX(-50%);background:#1b4a29;border:1px solid var(--teal);padding:8px 16px;border-radius:20px;font-size:12px;z-index:1000;opacity:0;transition:opacity 0.3s;pointer-events:none}#toast.show{opacity:1}"""

# ------------------------------ Flask Routes -------------------------
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
    user_id = user["id"]
    row = get_user(user_id)
    credits = row["credits"] if row else STARTING_CREDITS

    with game_lock:
        cards = all_cards(user_id)
        marked_dict = {str(idx): list(marked_set(user_id, idx)) for idx in range(len(cards))}
        data = {
            "round": game_state["round"],
            "prize": game_state["prize"],
            "is_active": game_state["is_active"],
            "called_numbers": game_state["called_numbers"],
            "current_number": game_state["current_number"],
            "player_count": active_player_count(),
            "credits": credits,
            "cards": cards,
            "marked": marked_dict,
        }
    return jsonify(data)

@app.post("/api/miniapp/buy-card")
def api_miniapp_buy_card():
    user = api_user()
    if not user:
        return json_error("Unauthorized", 401)
    user_id = user["id"]
    row = get_user(user_id)
    credits = row["credits"] if row else STARTING_CREDITS

    if credits < CARD_COST:
        return json_error("Not enough credits")

    with game_lock:
        cards = all_cards(user_id)
        if len(cards) >= MAX_CARDS_PER_PLAYER:
            return json_error("Maximum 4 cards per player allowed")
        
        new_card = make_card()
        cards.append(new_card)
        game_state["players"].add(user_id)
        game_state["prize"] += CARD_COST
        if not game_state["is_active"] and len(game_state["players"]) >= 1:
            game_state["is_active"] = True

    update_credits(user_id, -CARD_COST)
    return jsonify({"message": "Card purchased successfully"})

@app.post("/api/miniapp/mark")
def api_miniapp_mark():
    user = api_user()
    if not user:
        return json_error("Unauthorized", 401)
    
    body = request.get_json() or {}
    card_index = body.get("card_index")
    number = body.get("number")
    if card_index is None or number is None:
        return json_error("Invalid payload")

    user_id = user["id"]
    with game_lock:
        cards = all_cards(user_id)
        if card_index < 0 or card_index >= len(cards):
            return json_error("Invalid card index")
        
        if number != "FREE" and number not in game_state["called_numbers"]:
            return json_error("Number has not been called yet")
        
        m_set = marked_set(user_id, card_index)
        m_set.add(number)

    return jsonify({"message": "Marked successfully"})

@app.post("/api/miniapp/claim")
def api_miniapp_claim():
    user = api_user()
    if not user:
        return json_error("Unauthorized", 401)
    
    user_id = user["id"]
    with game_lock:
        cards = all_cards(user_id)
        if not cards:
            return json_error("You don't have any cards")
        
        won = False
        winning_card_index = -1
        for idx, card in enumerate(cards):
            m_set = marked_set(user_id, idx)
            if card_has_bingo(card, m_set):
                won = True
                winning_card_index = idx
                break
        
        if not won:
            return json_error("No BINGO found on your cards yet!")
        
        if user_id in game_state["winner_ids"]:
            return json_error("You already claimed for this round")

        game_state["winner_ids"].add(user_id)
        prize = game_state["prize"] if game_state["prize"] > 0 else 50

    update_credits(user_id, prize)
    with closing(db()) as con:
        con.execute(
            "INSERT INTO wins(user_id, username, round_no, card_index, prize) VALUES(?,?,?,?,?)",
            (user_id, user.get("username", ""), game_state["round"], winning_card_index, prize)
        )
        con.commit()

    reset_round()
    return jsonify({"message": f"BINGO! You won {prize} credits!"})

@app.post("/api/miniapp/deposit-request")
def api_miniapp_deposit_request():
    user = api_user()
    if not user:
        return json_error("Unauthorized", 401)
    
    body = request.get_json() or {}
    amount = body.get("amount")
    method = body.get("method")
    reference = body.get("reference")

    if not amount or not method or not reference:
        return json_error("Missing fields")

    try:
        amount = int(amount)
        if amount <= 0:
            raise ValueError()
    except ValueError:
        return json_error("Invalid amount")

    with closing(db()) as con:
        con.execute(
            "INSERT INTO deposit_requests(user_id, method, amount, reference) VALUES(?,?,?,?)",
            (user["id"], method, amount, reference)
        )
        con.commit()

    return jsonify({"message": "Deposit request submitted successfully"})

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
            con.execute("UPDATE users SET credits=credits+? WHERE user_id=?", (int(row["amount"]), int(row["user_id"])))
        con.commit()
    return jsonify({"ok": True, "message": "የክፍያ ጥያቄው ተረጋግጧል።"})

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
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
