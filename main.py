# -*- coding: utf-8 -*-
"""
Besh Besh Bingo — Telegram bot + Telegram Mini App
Updated with Classic 5x5 (1-75) Cards and 1-600 Master Called Numbers.

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

CARD_COST = 10
STARTING_CREDITS = 100
MAX_CARDS_PER_PLAYER = 4
MAX_MASTER_NUMBERS = 600  # Master called numbers pool range up to 600
CALL_INTERVAL_SECONDS = 3  # 3 seconds per number call
PREPARATION_SECONDS = 50   # 50 seconds preparation timer before round starts

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
            win_credits INTEGER NOT NULL DEFAULT 0,
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
            INSERT INTO users(user_id, username, first_name, credits, win_credits)
            VALUES(?,?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET
              username=excluded.username, first_name=excluded.first_name
        """, (int(user_id), username or "", first_name or "", STARTING_CREDITS, 0))
        con.commit()

def update_credits(user_id, delta, is_win=False):
    with closing(db()) as con:
        col = "win_credits" if is_win else "credits"
        con.execute(f"UPDATE users SET {col}={col}+? WHERE user_id=?", (int(delta), int(user_id)))
        con.commit()
        row = con.execute(f"SELECT credits, win_credits FROM users WHERE user_id=?", (int(user_id),)).fetchone()
        return row["credits"] if row else 0

init_db()

# ------------------------------ Game state ---------------------------
def fresh_game():
    return {
        "round": 1, "prize": 0, "is_active": False,
        "called_numbers": [], "current_number": None,
        "players": set(), "cards": {}, "marked": {}, "winner_ids": set(),
        "round_started_at": time.time(),
        "prep_ends_at": time.time() + PREPARATION_SECONDS,
    }

game_state = fresh_game()

def make_card():
    # Classic 5x5 Bingo Card with numbers 1 to 75 (B:1-15, I:16-30, N:31-45, G:46-60, O:61-75)
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
        remaining = [n for n in range(1, MAX_MASTER_NUMBERS + 1) if n not in game_state["called_numbers"]]
        if not remaining:
            return None
        number = random.choice(remaining)
        game_state["called_numbers"].append(number)
        game_state["current_number"] = number
        return number

def auto_caller_loop():
    while True:
        try:
            time.sleep(1)
            now = time.time()
            with game_lock:
                if not game_state["is_active"]:
                    # Check if preparation time (50 seconds) has passed to start calling numbers
                    if now >= game_state["prep_ends_at"] and len(game_state["players"]) > 0:
                        game_state["is_active"] = True
                else:
                    # Active game calling numbers every CALL_INTERVAL_SECONDS (3s)
                    if not hasattr(auto_caller_loop, "_last_call"):
                        auto_caller_loop._last_call = 0
                    if now - auto_caller_loop._last_call >= CALL_INTERVAL_SECONDS:
                        auto_caller_loop._last_call = now
                        number = call_next_number()
                        if number is None or len(game_state["called_numbers"]) >= MAX_MASTER_NUMBERS:
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
        game_state["round_started_at"] = time.time()
        game_state["prep_ends_at"] = time.time() + PREPARATION_SECONDS

def active_player_count():
    return len(game_state["players"])

# ------------------------------ Localization & Handlers -------------------------
TEXT = {
    "am": {
        "welcome": "🎰 እንኳን ደህና መጡ ወደ በሽ በሽ ቢንጎ!\n\nክላሲክ 5x5 ካርቴላዎች እና 1-600 ማስተር ቁጥሮች ያሉት አጓጊ ጨዋታ።",
        "play": "🎮 በሽ በሽ ቢንጎ ጨዋታ",
        "balance": "💰 ብር ሂሳብ",
        "rules": "📜 ሕግ እና ደንቦች",
        "wallet": "💳 Wallet / ኪስ ቦርሳ",
        "rank": "🏆 ደረጃ",
        "language": "🌐 ቋንቋ / Language",
        "support": "ជំនួយ",
        "admin": "🛡 አድሚን",
        "choose": "ከታች ያሉትን አማራጮች ይምረጡ።",
        "balance_msg": "💰 የኪስ ሂሳብዎ:\nPlay Wallet: {credits} ETB\nWin Wallet: {win_credits} ETB",
        "rules_msg": "📜 ሕጎች\n• ካርድ ዋጋ: 10 ብር\n• እያንዳንዱ ካርቴላ 5x5 (ከ1 እስከ 75) ቁጥሮች አሉት\n• ማስተር ቁጥሮች ከ1 እስከ 600 ይጠራሉ\n• የ50 ሰከንድ የዝግጅት ሰዓት አለው።",
    },
}

def tr(user_id, key, **kwargs):
    return TEXT["am"].get(key, key).format(**kwargs)

def main_keyboard(user_id):
    return ReplyKeyboardMarkup([
        [KeyboardButton("🎮 በሽ በሽ ቢንጎ ጨዋታ"), KeyboardButton("💰 ብር ሂሳብ")],
        [KeyboardButton("💳 Wallet / ኪስ ቦርሳ"), KeyboardButton("🏆 ደረጃ")],
        [KeyboardButton("📜 ሕግ እና ደንቦች"), KeyboardButton("🌐 ቋንቋ / Language")],
        [KeyboardButton("ជំនួយ")],
    ], resize_keyboard=True)

def webapp_url():
    return PUBLIC_URL + "/"

def start_keyboard():
    if PUBLIC_URL:
        return InlineKeyboardMarkup([[
            InlineKeyboardButton("🎰 በሽ በሽ ቢንጎ ክፍት", web_app=WebAppInfo(url=webapp_url()))
        ]])
    return None

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user.id, user.username, user.first_name)
    text = tr(user.id, "welcome") + "\n\n" + tr(user.id, "choose")
    markup = start_keyboard()
    if markup:
        await update.effective_message.reply_text(text, reply_markup=markup)
    await update.effective_message.reply_text("Besh Besh Bingo 🎰", reply_markup=main_keyboard(user.id))

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user.id, user.username, user.first_name)
    msg = (update.effective_message.text or "").strip()
    if msg in ("🎮 በሽ በሽ ቢንጎ ጨዋታ", "Play Besh Besh Bingo"):
        markup = start_keyboard()
        if markup:
            await update.effective_message.reply_text("Besh Besh Bingo 🎰", reply_markup=markup)
        else:
            await update.effective_message.reply_text("Set RENDER_EXTERNAL_URL.")
    elif msg in ("💰 ብር ሂሳብ", "Balance"):
        row = get_user(user.id)
        await update.effective_message.reply_text(tr(user.id, "balance_msg", credits=row["credits"], win_credits=row["win_credits"]))
    elif msg in ("📜 ሕግ እና ደንቦች", "Rules"):
        await update.effective_message.reply_text(tr(user.id, "rules_msg"))
    elif msg in ("💳 Wallet / ኪስ ቦርሳ",):
        await update.effective_message.reply_text("💳 Wallet\nTelebirr: 0918719332 · Enyew\nCBE Birr: 0918719332 · Enyew")
    elif msg in ("🏆 ደረጃ",):
        with closing(db()) as con:
            rows = con.execute("SELECT username, first_name, win_credits FROM users ORDER BY win_credits DESC LIMIT 10").fetchall()
        lines = ["🏆 Top Players"]
        for i, r in enumerate(rows, 1):
            name = ("@" + r["username"]) if r["username"] else (r["first_name"] or "Player")
            lines.append(f"{i}. {name} — {r['win_credits']} ETB")
        await update.effective_message.reply_text("\n".join(lines))
    else:
        await update.effective_message.reply_text("አማራጭ ይምረጡ:", reply_markup=main_keyboard(user.id))

bot_application.add_handler(CommandHandler("start", start))
bot_application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

# ------------------------------ Mini App auth & API ---------------------------
def validate_telegram_init_data(init_data):
    if not init_data: return None
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
        received_hash = pairs.pop("hash", None)
        if not received_hash: return None
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
        secret_key = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
        calculated = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calculated, received_hash): return None
        user_data = json.loads(pairs.get("user", "{}"))
        return user_data if user_data.get("id") else None
    except Exception:
        return None

def api_user():
    data = validate_telegram_init_data(request.headers.get("X-Telegram-Init-Data", ""))
    if data:
        ensure_user(data["id"], data.get("username", ""), data.get("first_name", ""))
        return data
    if os.environ.get("ALLOW_DEMO_MODE", "").lower() == "true":
        ensure_user(999999, "demo", "Demo Player")
        return {"id": 999999, "username": "demo", "first_name": "Demo Player"}
    return None

def api_admin():
    user = api_user()
    return user if user and int(user["id"]) in ADMIN_IDS else None

def json_error(message, status=400):
    return jsonify({"error": message}), status

# ------------------------------ HTML & JS Assets ---------------------------
INDEX_HTML = r"""<!doctype html>
<html lang="am"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Besh Besh Bingo</title><script src="https://telegram.org/js/telegram-web-app.js"></script><link rel="stylesheet" href="/static/style.css"></head>
<body><main class="app-shell">
<header class="topbar">
  <div><div class="eyebrow">BESH BESH BINGO 🎰</div><h1 id="round-title">ዙር #1</h1></div>
  <div class="top-scores">
    <div class="score-box"><span>PLAY</span><strong id="play-bal">0.00</strong></div>
    <div class="score-box win"><span>WIN</span><strong id="win-bal">0.00</strong></div>
    <div class="current-call-badge" id="current">—</div>
  </div>
</header>
<div id="prep-timer" style="background:#142d19;border:1px solid #39d66f;padding:8px;border-radius:10px;text-align:center;font-size:12px;margin-bottom:10px">ጨዋታው ሊጀመር: <b>50 ሰከንድ</b> ይቀራል</div>

<section class="game-panel">
  <div class="section-header">
    <h2>MASTER BOARD (1 - 600)</h2>
    <span class="card-count-pill" id="card-count">የተያዙ ካርዶች: 0</span>
  </div>
  <div class="master-grid" id="master-grid"></div>
</section>

<section class="cards-section" id="cards-container"></section>

<div class="actions-row">
  <button id="buy-card" class="primary-btn">＋ ካርድ ግዢ (10 ብር)</button>
  <button id="claim" class="success-btn">🏆 BINGO! አግኝ</button>
</div>

<div class="wallet-section" id="wallet-section" style="display:none;">
  <h2>💳 ኪስ ቦርሳ እና ክፍያ</h2>
  <p class="hint">Telebirr / CBE Birr: 0918719332 · Enyew</p>
  <select id="deposit-method"><option>Telebirr</option><option>CBE Birr</option></select>
  <input id="deposit-amount" type="number" placeholder="የክፍያ መጠን (ብር)">
  <input id="deposit-reference" placeholder="የክፍያ ማረጋገጫ ቁጥር">
  <button id="send-deposit" class="primary-btn">የክፍያ ጥያቄ ላክ</button>
</div>

<div class="rank-section" id="rank-section" style="display:none;">
  <h2>🏆 ከፍተኛ ተጫዋቾች</h2>
  <div id="rank-list">ጭነት ላይ...</div>
</div>

<nav class="bottom-nav">
  <button class="nav-btn active" data-tab="bingo" onclick="switchTab('bingo')">🎯 BINGO</button>
  <button class="nav-btn" data-tab="wallet" onclick="switchTab('wallet')">💳 WALLET</button>
  <button class="nav-btn" data-tab="rank" onclick="switchTab('rank')">🏆 RANK</button>
</nav>
</main><div id="toast"></div><script src="/static/app.js"></script></body></html>"""

ADMIN_HTML = r"""<!doctype html><html lang="am"><head><meta charset="utf-8"><title>Admin</title><link rel="stylesheet" href="/static/style.css"></head>
<body style="padding:16px"><h1>🛡 አድሚን ፓነል</h1><div id="req-list">ጭነት ላይ...</div><script src="/static/admin.js"></script></body></html>"""

STYLE_CSS = r""":root{color-scheme:dark;--bg:#041509;--panel:#0b2113;--panel2:#102d19;--line:#20552d;--text:#f4f7f4;--muted:#a3b8a8;--gold:#ffe629;--teal:#39d66f}
*{box-sizing:border-box}body{margin:0;background:var(--bg);font-family:system-ui,sans-serif;color:var(--text);padding-bottom:70px}
.app-shell{max-width:520px;margin:auto;padding:12px}
.topbar{display:flex;justify-content:space-between;align-items:center;background:var(--panel);border:1px solid var(--line);padding:10px 14px;border-radius:14px;margin-bottom:12px}
.eyebrow{font-size:10px;color:var(--teal);font-weight:800}.topbar h1{font-size:16px;margin:0}
.top-scores{display:flex;gap:6px;align-items:center}
.score-box{background:var(--panel2);border:1px solid var(--line);padding:4px 8px;border-radius:8px;font-size:11px;text-align:center}
.score-box.win{border-color:var(--gold)}.score-box strong{display:block;color:var(--gold);font-size:13px}
.current-call-badge{width:40px;height:40px;background:linear-gradient(135deg,#39d66f,#0b6329);border-radius:10px;display:grid;place-items:center;font-weight:900;font-size:16px;color:#041509}
.game-panel,.wallet-section,.rank-section{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:12px;margin-bottom:12px}
.section-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;font-size:13px}
.card-count-pill{background:var(--panel2);padding:4px 8px;border-radius:6px;border:1px solid var(--line);font-size:11px;color:var(--teal)}
.master-grid{display:grid;grid-template-columns:repeat(10,1fr);gap:3px;max-height:160px;overflow-y:auto;background:#06180d;padding:6px;border-radius:10px;border:1px solid var(--line)}
.m-cell{aspect-ratio:1;background:var(--panel2);border-radius:3px;font-size:8px;display:grid;place-items:center;font-weight:700;color:var(--muted)}
.m-cell.called{background:var(--teal);color:#041509;font-weight:900}
.cards-section{display:flex;flex-direction:column;gap:10px;margin-bottom:12px}
.bingo-card{background:var(--panel2);border:1px solid var(--line);border-radius:12px;padding:10px}
.card-title{font-size:12px;font-weight:bold;margin-bottom:6px;color:var(--gold)}
.card-grid{display:grid;grid-template-columns:repeat(5,1fr);gap:4px}
.c-cell{aspect-ratio:1;background:#06180d;border:1px solid var(--line);border-radius:6px;font-size:12px;font-weight:800;display:grid;place-items:center;cursor:pointer}
.c-cell.head{background:#173d22;color:var(--gold);font-size:10px;cursor:default}
.c-cell.free{background:#483308;color:var(--gold);font-size:10px}
.c-cell.called{border-color:var(--teal)}
.c-cell.marked{background:#114624;color:var(--teal);border-color:var(--teal)}
.actions-row{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:16px}
.primary-btn,.success-btn{padding:12px;border-radius:10px;font-weight:800;border:0;cursor:pointer;font-size:12px}
.primary-btn{background:var(--panel2);border:1px solid var(--line);color:var(--text)}
.success-btn{background:var(--teal);color:#041509}
input,select{width:100%;background:var(--panel2);border:1px solid var(--line);padding:10px;border-radius:8px;color:var(--text);margin-bottom:8px;font-size:12px}
.bottom-nav{position:fixed;bottom:0;left:0;right:0;background:#04150df0;backdrop-filter:blur(8px);border-top:1px solid var(--line);display:flex;justify-content:around;padding:8px;z-index:100}
.nav-btn{background:0;border:0;color:var(--muted);font-size:11px;cursor:pointer;font-weight:700}
.nav-btn.active{color:var(--teal)}
#toast{position:fixed;top:12px;left:50%;transform:translateX(-50%);background:#1b4a29;border:1px solid var(--teal);padding:6px 14px;border-radius:20px;font-size:11px;z-index:1000;display:none}
"""

APP_JS = r"""(() => {
const tg=window.Telegram?.WebApp;if(tg){tg.ready();tg.expand()}
const initData=tg?.initData||'';let model=null;
function toast(s){const e=document.getElementById('toast');e.textContent=s;e.style.display='block';setTimeout(()=>e.style.display='none',2500)}
async function api(path,method='GET',body=null){const o={method,headers:{'Content-Type':'application/json','X-Telegram-Init-Data':initData}};if(body)o.body=JSON.stringify(body);const r=await fetch(path,o);const d=await r.json().catch(()=>({error:'Error'}));if(!r.ok)throw Error(d.error||'Failed');return d}
function render(){if(!model)return;
document.getElementById('round-title').textContent='ዙር #'+model.round;
document.getElementById('play-bal').textContent=model.credits.toFixed(2);
document.getElementById('win-bal').textContent=(model.win_credits||0).toFixed(2);
document.getElementById('current').textContent=model.current_number??'—';
document.getElementById('card-count').textContent='የተያዙ ካርዶች: '+model.cards.length;

// Prep timer display
const timerBox=document.getElementById('prep-timer');
if(!model.is_active && model.time_left>0){
  timerBox.style.display='block';
  timerBox.innerHTML=`ጨዋታው ሊጀመር: <b>${model.time_left} ሰከንድ</b> ይቀራል`;
}else{
  timerBox.style.display='none';
}

// Master Grid 1 to 600
const calledSet=new Set(model.called_numbers);
let mg=document.getElementById('master-grid');mg.innerHTML='';
for(let i=1;i<=600;i++){const d=document.createElement('div');d.className='m-cell'+(calledSet.has(i)?' called':'');d.textContent=i;mg.appendChild(d);}

// Cards view (5x5 classic with B-I-N-G-O header)
let cc=document.getElementById('cards-container');cc.innerHTML='';
model.cards.forEach((card,idx)=>{
  let marked=new Set(model.marked[idx]||['FREE']);
  let box=document.createElement('div');box.className='bingo-card';
  box.innerHTML=`<div class="card-title">ካርድ #${idx+1} (1-75)</div>`;
  let grid=document.createElement('div');grid.className='card-grid';
  ['B','I','N','G','O'].forEach(h=>{let d=document.createElement('div');d.className='c-cell head';d.textContent=h;grid.appendChild(d);});
  
  card.forEach((row,r)=>row.forEach((n,c)=>{
    let btn=document.createElement('button');btn.className='c-cell';
    if(n==='FREE'){btn.classList.add('free');btn.textContent='★';}
    else{
      btn.textContent=n;
      if(calledSet.has(n))btn.classList.add('called');
      if(marked.has(n))btn.classList.add('marked');
      btn.onclick=()=>action('/api/miniapp/mark',{card_index:idx,number:n});
    }
    grid.appendChild(btn);
  }));
  box.appendChild(grid);cc.appendChild(box);
});
}
async function refresh(){try{model=await api('/api/miniapp/game');render();}catch(e){}}
async function action(path,body={}){try{const d=await api(path,'POST',body);toast(d.message||'ተሳካ');await refresh()}catch(e){toast(e.message)}}

window.switchTab=function(tab){
  ['bingo','wallet','rank'].forEach(t=>{
    document.querySelector(`.nav-btn[data-tab="${t}"]`).classList.toggle('active',t===tab);
  });
  document.querySelector('.game-panel').style.display=tab==='bingo'?'block':'none';
  document.getElementById('prep-timer').style.display=tab==='bingo'?'block':'none';
  document.getElementById('cards-container').style.display=tab==='bingo'?'flex':'none';
  document.querySelector('.actions-row').style.display=tab==='bingo'?'grid':'none';
  document.getElementById('wallet-section').style.display=tab==='wallet'?'block':'none';
  document.getElementById('rank-section').style.display=tab==='rank'?'block':'none';
  if(tab==='rank')loadRank();
}
async function loadRank(){try{const d=await api('/api/miniapp/rank');document.getElementById('rank-list').innerHTML=d.rows.map((r,i)=>`<div style="padding:6px;border-bottom:1px solid #20552d">${i+1}. ${r.name} — <b>${r.credits} ETB</b></div>`).join('');}catch(e){}}

document.getElementById('buy-card').onclick=()=>action('/api/miniapp/buy-card');
document.getElementById('claim').onclick=()=>action('/api/miniapp/claim');
document.getElementById('send-deposit').onclick=()=>{
  const amount=Number(document.getElementById('deposit-amount').value),method=document.getElementById('deposit-method').value,reference=document.getElementById('deposit-reference').value.trim();
  if(!amount||reference.length<3){toast('ትክክለኛ መረጃ ያስገቡ');return;}
  action('/api/miniapp/deposit-request',{amount,method,reference}).then(()=>{document.getElementById('deposit-reference').value='';})
};
refresh();setInterval(refresh,1500);
})();"""

ADMIN_JS = r"""async function load(){
const r=await fetch('/api/admin/overview',{headers:{'X-Telegram-Init-Data':window.Telegram?.WebApp?.initData||''}});
const d=await r.json();
document.getElementById('req-list').innerHTML=d.requests.map(r=>`<div style="background:#0b2113;padding:10px;border-radius:8px;margin-bottom:8px"><b>#${r.id} · ${r.amount} ብር (${r.method})</b><button onclick="review(${r.id},'approve')" style="background:#39d66f;border:0;padding:6px 12px;border-radius:6px;font-weight:bold">አጽድቅ</button></div>`).join('');
}
async function review(id,decision){await fetch(`/api/admin/deposit/${id}/review`,{method:'POST',headers:{'Content-Type':'application/json','X-Telegram-Init-Data':window.Telegram?.WebApp?.initData||''},body:JSON.stringify({decision})});load();}
load();"""

# ------------------------------ Flask API Routes -------------------------
@app.route("/static/style.css")
def serve_css(): return Response(STYLE_CSS, mimetype="text/css")

@app.route("/static/app.js")
def serve_app_js(): return Response(APP_JS, mimetype="application/javascript")

@app.route("/static/admin.js")
def serve_admin_js(): return Response(ADMIN_JS, mimetype="application/javascript")

@app.route("/", methods=["GET"])
def index(): return Response(INDEX_HTML, mimetype="text/html")

@app.route("/admin", methods=["GET"])
def admin_panel(): return Response(ADMIN_HTML, mimetype="text/html")

@app.get("/api/miniapp/game")
def api_miniapp_game():
    user = api_user()
    if not user: return json_error("Unauthorized", 401)
    row = get_user(user["id"])
    with game_lock:
        cards = all_cards(user["id"])
        marked_dict = {str(idx): list(marked_set(user["id"], idx)) for idx in range(len(cards))}
        time_left = max(0, int(game_state["prep_ends_at"] - time.time()))
        return jsonify({
            "round": game_state["round"], "prize": game_state["prize"],
            "is_active": game_state["is_active"], "time_left": time_left,
            "called_numbers": game_state["called_numbers"],
            "current_number": game_state["current_number"], "player_count": active_player_count(),
            "credits": row["credits"] if row else 100, "win_credits": row["win_credits"] if row else 0,
            "cards": cards, "marked": marked_dict
        })

@app.post("/api/miniapp/buy-card")
def api_miniapp_buy_card():
    user = api_user()
    if not user: return json_error("Unauthorized", 401)
    row = get_user(user["id"])
    if (row["credits"] if row else 100) < CARD_COST: return json_error("በቂ ሂሳብ የለዎትም")
    with game_lock:
        cards = all_cards(user["id"])
        if len(cards) >= MAX_CARDS_PER_PLAYER: return json_error("እስከ 4 ካርዶች ብቻ መያዝ ይቻላል")
        cards.append(make_card())
        game_state["players"].add(user["id"])
        game_state["prize"] += CARD_COST
    update_credits(user["id"], -CARD_COST)
    return jsonify({"message": "ካርድ ተገዝቷል!"})

@app.post("/api/miniapp/mark")
def api_miniapp_mark():
    user = api_user()
    if not user: return json_error("Unauthorized", 401)
    body = request.get_json() or {}
    idx, num = body.get("card_index"), body.get("number")
    with game_lock:
        cards = all_cards(user["id"])
        if idx is None or idx < 0 or idx >= len(cards): return json_error("Invalid card")
        if num != "FREE" and num not in game_state["called_numbers"]: return json_error("ቁጥሩ ገና አልተጠራም")
        marked_set(user["id"], idx).add(num)
    return jsonify({"message": "ምልክት ተደርጓል"})

@app.post("/api/miniapp/claim")
def api_miniapp_claim():
    user = api_user()
    if not user: return json_error("Unauthorized", 401)
    with game_lock:
        cards = all_cards(user["id"])
        won = any(card_has_bingo(c, marked_set(user["id"], i)) for i, c in enumerate(cards))
        if not won: return json_error("BINGO አልደረሰም!")
        if user["id"] in game_state["winner_ids"]: return json_error("ቀድመው ጠይቀዋል")
        game_state["winner_ids"].add(user["id"])
        prize = game_state["prize"] if game_state["prize"] > 0 else 50
    update_credits(user["id"], prize, is_win=True)
    reset_round()
    return jsonify({"message": f"ብሩህ አሸናፊ! {prize} ETB አሸንፈዋል!"})

@app.post("/api/miniapp/deposit-request")
def api_miniapp_deposit():
    user = api_user()
    if not user: return json_error("Unauthorized", 401)
    body = request.get_json() or {}
    amt, method, ref = body.get("amount"), body.get("method"), body.get("reference")
    with closing(db()) as con:
        con.execute("INSERT INTO deposit_requests(user_id, method, amount, reference) VALUES(?,?,?,?)", (user["id"], method, amt, ref))
        con.commit()
    return jsonify({"message": "የክፍያ ጥያቄ ተልኳል"})

@app.get("/api/miniapp/rank")
def miniapp_rank():
    with closing(db()) as con:
        rows = con.execute("SELECT username, first_name, win_credits FROM users ORDER BY win_credits DESC LIMIT 10").fetchall()
        return jsonify({"rows": [{"name": ("@" + r["username"]) if r["username"] else (r["first_name"] or "Player"), "credits": r["win_credits"]} for r in rows]})

@app.get("/api/admin/overview")
def admin_overview():
    user = api_admin()
    if not user: return json_error("Admin access denied", 403)
    with closing(db()) as con:
        reqs = con.execute("SELECT * FROM deposit_requests ORDER BY id DESC LIMIT 50").fetchall()
        return jsonify({"requests": [dict(r) for r in reqs]})

@app.post("/api/admin/deposit/<int:req_id>/review")
def admin_review(req_id):
    user = api_admin()
    if not user: return json_error("Admin access denied", 403)
    body = request.get_json(silent=True) or {}
    decision = body.get("decision")
    with closing(db()) as con:
        row = con.execute("SELECT * FROM deposit_requests WHERE id=?", (req_id,)).fetchone()
        if not row or row["status"] != "pending": return json_error("Invalid request")
        status = "approved" if decision == "approve" else "rejected"
        con.execute("UPDATE deposit_requests SET status=?, reviewed_at=CURRENT_TIMESTAMP WHERE id=?", (status, req_id))
        if decision == "approve":
            con.execute("UPDATE users SET credits=credits+? WHERE user_id=?", (row["amount"], row["user_id"]))
        con.commit()
    return jsonify({"ok": True, "message": "ተረጋግጧል"})

@app.route("/telegram-webhook" if not WEBHOOK_SECRET_PATH else "/telegram-webhook/" + WEBHOOK_SECRET_PATH, methods=["POST"])
def telegram_webhook():
    try:
        payload = request.get_json(force=True)
        update = Update.de_json(payload, bot_application.bot)
        if bot_loop.is_running():
            asyncio.run_coroutine_threadsafe(bot_application.process_update(update), bot_loop)
        return "OK"
    except Exception:
        return "ERROR", 500

@app.route("/<path:path>", methods=["GET", "HEAD"])
def fallback(path): return jsonify({"error": "Not found"}), 404

def run_bot_loop():
    asyncio.set_event_loop(bot_loop)
    bot_loop.run_forever()

def start_bot():
    global bot_thread
    if bot_thread and bot_thread.is_alive(): return
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
threading.Thread(target=auto_caller_loop, daemon=True, name="auto-caller").start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
