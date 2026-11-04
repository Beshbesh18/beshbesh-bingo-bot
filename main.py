# -*- coding: utf-8 -*-
"""
Besh Besh Bingo — Telegram bot + Telegram Mini App
Updated to support 600 cards grid view matching the user reference video.
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

MAX_CARDS_TOTAL = 600
CARD_COST = 10
STARTING_CREDITS = 100
CALL_INTERVAL_SECONDS = 5

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
        "cards_owner": {}, # card_id (1-600) -> user_id
        "cards_data": {},  # card_id -> 5x5 grid
        "marked": {},      # (user_id, card_id) -> set of numbers
        "winner_ids": set(),
        "started_at": None,
    }

game_state = fresh_game()

def generate_card_grid():
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

def get_card_data(card_id):
    if card_id not in game_state["cards_data"]:
        game_state["cards_data"][card_id] = generate_card_grid()
    return game_state["cards_data"][card_id]

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
    return len(set(game_state["cards_owner"].values()))

# ------------------------------ Localization -------------------------
TEXT = {
    "am": {
        "welcome": "🎰 እንኳን ደህና መጡ ወደ Flash Bingo!\n\nእስከ 600 ካርዶች የሚስተናገዱበት የቢንጎ ጨዋታ።",
        "play": "🎮 ቢንጎ ጨዋታ",
        "balance": "💰 ብር እና ሂሳብ",
        "rules": "📜 ሕግ እና ድንጋጌዎች",
        "wallet": "💳 Wallet / ክሬዲት",
        "rank": "🏆 ደረጃ",
        "language": "🌐 ቋንቋ / Language",
        "support": "🆘 እርዳታ",
        "choose": "ከታች ያሉትን አማራጮች ይምረጡ።",
        "balance_msg": "💰 የእርስዎ ክሬዲት መጠን: {credits}",
        "rules_msg": "📜 ሕጎች\n• የካርድ ዋጋ: 10 ክሬዲት\n• ከ 1 እስከ 600 ካርዶች ይገኛሉ\n• በተጠሩ ቁጥሮች ላይ ምልክት ያድርጉ",
        "language_set": "ቋንቋው ወደ አማርኛ ተቀይሯል።",
    },
    "en": {
        "welcome": "🎰 Welcome to Flash Bingo!\n\n600-card multiplayer bingo game.",
        "play": "🎮 Play Bingo",
        "balance": "💰 Balance",
        "rules": "📜 Rules",
        "wallet": "💳 Wallet / Credits",
        "rank": "🏆 Rank",
        "language": "🌐 Language",
        "support": "🆘 Help",
        "choose": "Choose an option below.",
        "balance_msg": "💰 Your balance: {credits} credits",
        "rules_msg": "📜 Rules\n• Card cost: 10 credits\n• Choose from cards 1 to 600",
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
            InlineKeyboardButton("🎮 Open Flash Bingo / ዎችን ክፍት", web_app=WebAppInfo(url=webapp_url()))
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
            await update.effective_message.reply_text("Flash Bingo", reply_markup=markup)
        else:
            await update.effective_message.reply_text("Set RENDER_EXTERNAL_URL to enable the Mini App.")
    elif msg in (t["balance"], "💰 ብር እና ሂሳብ", "💰 Balance"):
        row = get_user(user.id)
        await update.effective_message.reply_text(tr(user.id, "balance_msg", credits=row["credits"]))
    elif msg in (t["rules"], "📜 ሕግ እና ድንጋጌዎች", "📜 Rules"):
        await update.effective_message.reply_text(tr(user.id, "rules_msg"))
    elif msg in (t["wallet"], "💳 Wallet / ክሬዲት"):
        await update.effective_message.reply_text(
            "💳 Wallet\nTelebirr: 0918719332 · Enyew\nCBE Birr: 0918719332 · Enyew\n\n"
            "ክፍያ ከፈጸሙ በኋላ በMini App በኩል የክፍያ ማረጋገጫ ይላኩ።")
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
    else:
        await update.effective_message.reply_text(tr(user.id, "choose"), reply_markup=main_keyboard(user.id))

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        await update.effective_message.reply_text("⛔ Unauthorized.")
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

# ------------------------------ Flask Routes -------------------------
@app.route("/")
def index_route():
    return INDEX_HTML

@app.route("/admin")
def admin_route():
    return ADMIN_HTML

@app.route("/static/style.css")
def css_route():
    return Response(STYLE_CSS, mimetype="text/css")

@app.route("/static/app.js")
def app_js_route():
    return Response(APP_JS, mimetype="application/javascript")

@app.route("/static/admin.js")
def admin_js_route():
    return Response(ADMIN_JS, mimetype="application/javascript")

@app.route("/api/miniapp/game", methods=["GET"])
def api_miniapp_game():
    user = api_user()
    if not user:
        return json_error("Unauthorized", 401)
    user_id = int(user["id"])
    u = get_user(user_id)
    with game_lock:
        user_cards = [cid for cid, uid in game_state["cards_owner"].items() if uid == user_id]
        cards_payload = []
        marked_payload = {}
        for cid in user_cards:
            cards_payload.append({"card_id": cid, "grid": get_card_data(cid)})
            m_key = (user_id, cid)
            marked_payload[cid] = list(game_state["marked"].get(m_key, {"FREE"}))
        
        data = {
            "credits": u["credits"] if u else 0,
            "round": game_state["round"],
            "prize": game_state["prize"],
            "is_active": game_state["is_active"],
            "player_count": active_player_count(),
            "current_number": game_state["current_number"],
            "called_numbers": game_state["called_numbers"],
            "user_cards": cards_payload,
            "marked": marked_payload,
            "all_cards_status": {cid: game_state["cards_owner"].get(cid) for cid in range(1, MAX_CARDS_TOTAL + 1)}
        }
    return jsonify(data)

@app.route("/api/miniapp/buy-card", methods=["POST"])
def api_miniapp_buy_card():
    user = api_user()
    if not user:
        return json_error("Unauthorized", 401)
    user_id = int(user["id"])
    body = request.get_json(silent=True) or {}
    card_id = int(body.get("card_id", 0))
    if card_id < 1 or card_id > MAX_CARDS_TOTAL:
        return json_error("Invalid card ID (1-600).")
    with game_lock:
        if card_id in game_state["cards_owner"]:
            return json_error("Card already taken.")
        u = get_user(user_id)
        if not u or u["credits"] < CARD_COST:
            return json_error("Not enough credits.")
        update_credits(user_id, -CARD_COST)
        game_state["cards_owner"][card_id] = user_id
        game_state["prize"] += CARD_COST
        if not game_state["is_active"]:
            game_state["is_active"] = True
    return jsonify({"message": f"Card #{card_id} purchased successfully."})

@app.route("/api/miniapp/mark", methods=["POST"])
def api_miniapp_mark():
    user = api_user()
    if not user:
        return json_error("Unauthorized", 401)
    user_id = int(user["id"])
    body = request.get_json(silent=True) or {}
    card_id = int(body.get("card_id", 0))
    number = body.get("number")
    with game_lock:
        if game_state["cards_owner"].get(card_id) != user_id:
            return json_error("You do not own this card.")
        if number not in game_state["called_numbers"]:
            return json_error("Number not called yet.")
        card = get_card_data(card_id)
        if not any(number in row for row in card):
            return json_error("Number not on card.")
        m_key = (user_id, card_id)
        if m_key not in game_state["marked"]:
            game_state["marked"][m_key] = {"FREE"}
        game_state["marked"][m_key].add(number)
    return jsonify({"message": "Marked."})

@app.route("/api/miniapp/claim", methods=["POST"])
def api_miniapp_claim():
    user = api_user()
    if not user:
        return json_error("Unauthorized", 401)
    user_id = int(user["id"])
    with game_lock:
        user_cards = [cid for cid, uid in game_state["cards_owner"].items() if uid == user_id]
        won_card = None
        for cid in user_cards:
            card = get_card_data(cid)
            m = game_state["marked"].get((user_id, cid), {"FREE"})
            if card_has_bingo(card, m):
                won_card = cid
                break
        if won_card is None:
            return json_error("No valid bingo found.")
        prize = game_state["prize"] or 100
        game_state["is_active"] = False
        update_credits(user_id, prize)
        with closing(db()) as con:
            con.execute(
                "INSERT INTO wins(user_id, username, round_no, card_index, prize) VALUES(?,?,?,?,?)",
                (user_id, user.get("username", ""), game_state["round"], won_card, prize)
            )
            con.commit()
    return jsonify({"message": f"BINGO! You won {prize} credits!"})

@app.route("/api/miniapp/deposit-request", methods=["POST"])
def api_miniapp_deposit():
    user = api_user()
    if not user:
        return json_error("Unauthorized", 401)
    user_id = int(user["id"])
    body = request.get_json(silent=True) or {}
    amount = int(body.get("amount", 0))
    method = str(body.get("method", "Telebirr")).strip()
    reference = str(body.get("reference", "")).strip()
    if amount <= 0 or not reference:
        return json_error("Invalid amount or reference.")
    with closing(db()) as con:
        con.execute(
            "INSERT INTO deposit_requests(user_id, method, amount, reference) VALUES(?,?,?,?)",
            (user_id, method, amount, reference)
        )
        con.commit()
    return jsonify({"message": "Deposit request submitted."})

@app.route("/api/miniapp/rank", methods=["GET"])
def api_miniapp_rank():
    with closing(db()) as con:
        rows = con.execute("SELECT username, first_name, credits FROM users ORDER BY credits DESC LIMIT 10").fetchall()
    data = [{"name": (("@" + r["username"]) if r["username"] else (r["first_name"] or "Player")), "credits": r["credits"]} for r in rows]
    return jsonify({"rows": data})

@app.route("/api/admin/overview", methods=["GET"])
def api_admin_overview():
    admin = api_admin()
    if not admin:
        return json_error("Unauthorized", 401)
    with closing(db()) as con:
        users_count = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        total_credits = con.execute("SELECT SUM(credits) FROM users").fetchone()[0] or 0
        pending_count = con.execute("SELECT COUNT(*) FROM deposit_requests WHERE status='pending'").fetchone()[0]
        reqs = con.execute("SELECT * FROM deposit_requests ORDER BY id DESC LIMIT 50").fetchall()
    req_list = [{
        "id": r["id"], "user_id": r["user_id"], "method": r["method"],
        "amount": r["amount"], "reference": r["reference"], "status": r["status"], "created_at": r["created_at"]
    } for r in reqs]
    return jsonify({"users_count": users_count, "total_credits": total_credits, "pending_count": pending_count, "requests": req_list})

@app.route("/api/admin/deposit/<int:req_id>/review", methods=["POST"])
def api_admin_review_deposit(req_id):
    admin = api_admin()
    if not admin:
        return json_error("Unauthorized", 401)
    body = request.get_json(silent=True) or {}
    decision = body.get("decision")
    with closing(db()) as con:
        row = con.execute("SELECT * FROM deposit_requests WHERE id=?", (req_id,)).fetchone()
        if not row or row["status"] != "pending":
            return json_error("Invalid request.")
        new_status = "approved" if decision == "approve" else "rejected"
        con.execute("UPDATE deposit_requests SET status=?, reviewed_at=CURRENT_TIMESTAMP, reviewed_by=? WHERE id=?", (new_status, int(admin["id"]), req_id))
        con.commit()
        if new_status == "approved":
            update_credits(row["user_id"], row["amount"])
    return jsonify({"message": f"Deposit {new_status}."})

# ------------------------------ HTML & Frontend UI -------------------
INDEX_HTML = r"""<!doctype html>
<html lang="am"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no"><meta name="theme-color" content="#06180d"><title>Flash Bingo</title><script src="https://telegram.org/js/telegram-web-app.js"></script><link rel="stylesheet" href="/static/style.css"></head>
<body><main class="app-shell">
<header class="topbar"><div class="brand-mark">⚡</div><div><div class="eyebrow">FLASH BINGO</div><h1>FLASH <span>BINGO</span></h1></div><div class="live-pill"><i></i> LIVE</div></header>
<section class="board-matrix" id="board-matrix"></section>
<section class="wallet-panel"><div><div class="muted">የእርስዎ ክሬዲት</div><div class="balance"><span id="credits">—</span> <small>PTS</small></div></div><div class="round-box"><span class="muted">ዙር</span><strong id="round">#1</strong></div></section>
<section class="stats-row"><div class="stat"><span>🏆 ሽልማት</span><strong id="prize">0 pts</strong></div><div class="stat"><span>👥 ተጫዋቾች</span><strong id="players">0</strong></div></section>
<section class="draw-panel">
<div class="current-ball-box"><div class="muted">የተጠራ</div><div class="last-number" id="current">—</div></div>
<div class="draw-meta"><span class="muted">የተጠሩ ቁጥሮች (12/75)</span><div class="number-strip" id="recent"></div></div>
</section>
<section class="game-panel">
<div class="section-heading"><h2>የእርስዎ ካርዶች (1-600)</h2><button id="open-selector" class="secondary">+ ካርድ ምረጥ</button></div>
<div id="my-cards-container"></div>
<button id="claim" class="primary full-width" style="margin-top:10px;">🏆 BINGO! አሸንፍ</button>
</section>
<section class="game-panel deposit-panel">
<h2>💳 ክሬዲት መሙላት</h2>
<div class="payment-info"><strong>Telebirr / CBE</strong><span>0918719332 · Enyew</span></div>
<select id="deposit-method"><option>Telebirr</option><option>CBE Birr</option></select>
<input id="deposit-amount" type="number" placeholder="የብር መጠን (ብር)">
<input id="deposit-reference" placeholder="የክፍያ ማረጋገጫ ቁጥር">
<button id="send-deposit" class="primary full-width">ማመልከቻ ላክ</button>
</section>
<nav class="bottom-nav"><button class="nav-active">🎰<span>BINGO</span></button><button onclick="document.querySelector('.deposit-panel').scrollIntoView({behavior:'smooth'})">▪<span>WALLET</span></button><button onclick="loadRank()">🏆<span>RANK</span></button></nav>
</main>
<div id="card-modal" class="modal" style="display:none;"><div class="modal-content"><h3>ካርድ ቁጥር ምረጥ (1 እስከ 600)</h3><div id="cards-grid-600" class="grid-600"></div><button id="close-modal" class="secondary full-width">ዝጋ</button></div></div>
<div id="toast"></div><script src="/static/app.js"></script></body></html>"""

ADMIN_HTML = r"""<!doctype html><html lang="am"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Admin Panel</title><link rel="stylesheet" href="/static/style.css"></head>
<body style="padding:20px;"><main><h1>🛡 አድሚን ፓነል</h1><div id="requests">መረጃ በመጫን ላይ...</div></main><script src="/static/admin.js"></script></body></html>"""

STYLE_CSS = r""":root{color-scheme:dark;--bg:#06180d;--panel:#0b2113;--panel2:#102d19;--line:#20552d;--text:#f4f7f4;--muted:#a3b8a8;--gold:#ffe629;--teal:#39d66f}
body{margin:0;background:var(--bg);font-family:system-ui,sans-serif;color:var(--text)}
.app-shell{max-width:500px;margin:auto;padding:12px;padding-bottom:60px}
.topbar{display:flex;align-items:center;gap:10px;margin-bottom:10px}.brand-mark{font-size:24px;background:var(--gold);padding:8px;border-radius:10px;color:#000}
.board-matrix{display:grid;grid-template-columns:repeat(15,1fr);gap:2px;background:#031008;padding:6px;border-radius:10px;margin-bottom:10px;max-height:120px;overflow-y:auto}
.matrix-cell{font-size:9px;text-align:center;padding:3px 0;background:#0d2616;border-radius:3px;color:var(--muted)}
.matrix-cell.called{background:var(--gold);color:#000;font-weight:bold}
.wallet-panel,.draw-panel,.game-panel{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:12px;margin-bottom:10px;display:flex;justify-content:space-between;align-items:center}
.balance{font-size:22px;font-weight:bold;color:var(--gold)}
.draw-panel{display:flex;gap:10px}.last-number{width:50px;height:50px;background:var(--gold);color:#000;font-size:20px;font-weight:bold;display:grid;place-items:center;border-radius:12px}
.number-strip{display:flex;gap:4px;flex-wrap:wrap;margin-top:4px}.number-chip{background:var(--gold);color:#000;padding:2px 6px;border-radius:4px;font-size:10px;font-weight:bold}
.bingo-card-ui{background:var(--panel2);border:1px solid var(--line);border-radius:10px;padding:8px;margin-bottom:8px}
.card-header{font-size:12px;color:var(--gold);font-weight:bold;margin-bottom:6px}
.mini-grid{display:grid;grid-template-columns:repeat(5,1fr);gap:4px}
.cell{aspect-ratio:1;display:grid;place-items:center;background:#06180d;border:1px solid var(--line);font-size:11px;font-weight:bold;border-radius:6px;cursor:pointer}
.cell.free{background:#39d66f33;color:var(--teal)}
.cell.called{border-color:var(--gold)}
.cell.marked{background:var(--teal);color:#000}
button.primary,button.secondary{border:0;border-radius:10px;padding:10px;font-weight:bold;cursor:pointer}
button.primary{background:var(--teal);color:#000}button.secondary{background:var(--panel2);border:1px solid var(--line);color:var(--text)}
.full-width{width:100%}
.modal{position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,0.8);z-index:100;padding:20px;overflow-y:auto}
.modal-content{background:var(--panel);padding:15px;border-radius:14px;max-width:450px;margin:auto}
.grid-600{display:grid;grid-template-columns:repeat(10,1fr);gap:4px;max-height:300px;overflow-y:auto;margin:10px 0}
.grid-600 button{font-size:10px;padding:6px 2px;background:#112d1b;border:1px solid var(--line);color:#fff;border-radius:4px}
.grid-600 button.owned{background:var(--teal);color:#000}
.bottom-nav{position:fixed;bottom:0;left:0;right:0;background:#04120a;display:flex;justify-content:space-around;padding:8px;border-top:1px solid var(--line)}
.bottom-nav button{background:0;border:0;color:var(--muted);font-size:10px;display:flex;flex-direction:column;align-items:center}
input,select{width:100%;background:var(--panel2);border:1px solid var(--line);padding:8px;margin:4px 0;border-radius:8px;color:#fff}
#toast{position:fixed;bottom:60px;left:50%;transform:translateX(-50%);background:#222;color:#fff;padding:8px 14px;border-radius:10px;font-size:11px;display:none}
"""

APP_JS = r"""(() => {
const tg=window.Telegram?.WebApp;if(tg){tg.ready();tg.expand()}
const initData=tg?.initData||'';let model=null;const $=id=>document.getElementById(id);
function toast(s){const e=$('toast');e.textContent=s;e.style.display='block';setTimeout(()=>e.style.display='none',2500)}
async function api(path,method='GET',body=null){const o={method,headers:{'Content-Type':'application/json','X-Telegram-Init-Data':initData}};if(body)o.body=JSON.stringify(body);const r=await fetch(path,o);const d=await r.json().catch(()=>({}));if(!r.ok)throw Error(d.error||'Failed');return d}
function render(){if(!model)return;
$('credits').textContent=model.credits;$('round').textContent='#'+model.round;$('prize').textContent=model.prize+' pts';$('players').textContent=model.player_count;$('current').textContent=model.current_number??'—';
let matrixHTML='';for(let i=1;i<=75;i++){let called=model.called_numbers.includes(i);matrixHTML+=`<div class="matrix-cell ${called?'called':''}">${i}</div>`}
$('board-matrix').innerHTML=matrixHTML;
$('recent').innerHTML=model.called_numbers.slice(-5).reverse().map(n=>`<span class="number-chip">${n}</span>`).join('');
let cardsHTML='';
model.user_cards.forEach(uc=>{
  let marked=new Set(model.marked[uc.card_id]||['FREE']);
  let called=new Set(model.called_numbers);
  cardsHTML+=`<div class="bingo-card-ui"><div class="card-header">CARD #${uc.card_id}</div><div class="mini-grid">`;
  uc.grid.forEach(row=>row.forEach(n=>{
    let isFree=(n==='FREE');let isCalled=called.has(n);let isMarked=marked.has(n);
    cardsHTML+=`<button class="cell ${isFree?'free':''} ${isCalled?'called':''} ${isMarked?'marked':''}" ${!isFree&&!isCalled?'disabled':''} onclick="markCard(${uc.card_id}, ${n})">${isFree?'★':n}</button>`;
  }));
  cardsHTML+=`</div></div>`;
});
$('my-cards-container').innerHTML=cardsHTML||'<div class="muted">እስካሁን የገዙት ካርድ የለም። ከ 1 እስከ 600 ካርድ ይምረጡ።</div>';
}
window.markCard=async(cardId,number)=>{try{await api('/api/miniapp/mark','POST',{card_id:cardId,number});refresh()}catch(e){toast(e.message)}};
async function refresh(){try{model=await api('/api/miniapp/game');render()}catch(e){}}
$('open-selector').onclick=()=>{
  let html='';
  for(let i=1;i<=600;i++){
    let owner=model.all_cards_status[i];
    html+=`<button class="${owner?'owned':''}" onclick="buyCard(${i})">${i}</button>`;
  }
  $('cards-grid-600').innerHTML=html;
  $('card-modal').style.display='block';
};
$('close-modal').onclick=()=>$('card-modal').style.display='none';
window.buyCard=async(cardId)=>{
  try{let d=await api('/api/miniapp/buy-card','POST',{card_id:cardId});toast(d.message);$('card-modal').style.display='none';refresh()}catch(e){toast(e.message)}
};
$('claim').onclick=async()=>{try{let d=await api('/api/miniapp/claim','POST');toast(d.message);refresh()}catch(e){toast(e.message)}};
$('send-deposit').onclick=async()=>{
  let amount=Number($('deposit-amount').value),method=$('deposit-method').value,reference=$('deposit-reference').value.trim();
  if(!amount||!reference){toast('መጠን እና ማረጋገጫ ያስገቡ');return}
  try{let d=await api('/api/miniapp/deposit-request','POST',{amount,method,reference});toast(d.message);$('deposit-amount').value='';$('deposit-reference').value=''}catch(e){toast(e.message)}
};
refresh();setInterval(refresh,4000);
})();"""

ADMIN_JS = r"""(() => {
async function load(){
  let r=await fetch('/api/admin/overview',{headers:{'X-Telegram-Init-Data':window.Telegram?.WebApp?.initData||''}});
  let d=await r.json();
  document.getElementById('requests').innerHTML=d.requests.map(req=>`<p>ID: ${req.id} | Amount: ${req.amount} | Ref: ${req.reference} <button onclick="review(${req.id})">አጽድቅ</button></p>`).join('');
}
window.review=async(id)=>{await fetch(`/api/admin/deposit/${id}/review`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({decision:'approve'})});load()};
load();
})();"""

# ------------------------------ Main Execution -----------------------
def run_bot():
    bot_loop.run_until_complete(bot_application.initialize())
    bot_loop.run_until_complete(bot_application.start())
    if PUBLIC_URL:
        webhook_url = f"{PUBLIC_URL}/telegram/webhook"
        if WEBHOOK_SECRET_PATH:
            webhook_url += f"/{WEBHOOK_SECRET_PATH}"
        bot_loop.run_until_complete(bot_application.bot.set_webhook(webhook_url))
    else:
        bot_loop.run_until_complete(bot_application.updater.start_polling())
    bot_loop.run_forever()

@app.route("/telegram/webhook", methods=["POST"])
@app.route(f"/telegram/webhook/{WEBHOOK_SECRET_PATH}", methods=["POST"])
def telegram_webhook():
    if request.headers.get("content-type") == "application/json":
        json_data = request.get_json(force=True)
        update = Update.de_json(json_data, bot_application.bot)
        bot_loop.call_soon_threadsafe(
            lambda: asyncio.create_task(bot_application.process_update(update))
        )
        return "OK"
    return "Invalid Request", 400

if __name__ == "__main__":
    threading.Thread(target=auto_caller_loop, daemon=True).start()
    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
