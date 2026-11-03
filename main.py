# -*- coding: utf-8 -*-
"""
Besh Besh Bingo — Full Telegram bot + Telegram Mini App
Updated with specific user-selected card IDs (e.g. #400, #200, #66) and 1-75 auto-calling.
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

TOKEN = os.environ.get("TOKEN", "").strip()
PUBLIC_URL = os.environ.get("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
WEBHOOK_SECRET_PATH = os.environ.get("WEBHOOK_SECRET_PATH", "").strip().strip("/")
ADMIN_IDS = {
    int(x.strip()) for x in os.environ.get("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}
DATABASE_PATH = os.environ.get("DATABASE_PATH", "bingo.sqlite3")
os.makedirs(os.path.dirname(os.path.abspath(DATABASE_PATH)), exist_ok=True)

CARD_COST = 10
STARTING_CREDITS = 100
MAX_CARDS_PER_PLAYER = 4
MAX_MASTER_NUMBERS = 75  # Master numbers range from 1 to 75 as per Flash Bingo rules
CALL_INTERVAL_SECONDS = 3
PREPARATION_SECONDS = 50

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger("besh-besh-bingo")

if not TOKEN:
    raise RuntimeError("Set TOKEN environment variable.")

app = Flask(__name__)
bot_application = Application.builder().token(TOKEN).build()
bot_loop = asyncio.new_event_loop()
bot_thread = None
game_lock = threading.RLock()

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

def fresh_game():
    return {
        "round": 1, "prize": 0, "is_active": False,
        "called_numbers": [], "current_number": None,
        "players": set(), "cards": {}, "marked": {}, "winner_ids": set(),
        "prep_ends_at": time.time() + PREPARATION_SECONDS,
    }

game_state = fresh_game()

def make_card_with_id(card_id):
    # Generates a unique 5x5 matrix based on the user-selected card ID (e.g. 400, 66, 8686, 200)
    rng = random.Random(card_id)
    columns = [
        rng.sample(range(1, 16), 5),
        rng.sample(range(16, 31), 5),
        rng.sample(range(31, 46), 5),
        rng.sample(range(46, 61), 5),
        rng.sample(range(61, 76), 5),
    ]
    card = [[columns[c][r] for c in range(5)] for r in range(5)]
    card[2][2] = "FREE"
    return {"id": card_id, "matrix": card}

def all_user_cards(user_id):
    if int(user_id) not in game_state["cards"]:
        game_state["cards"][int(user_id)] = []
    return game_state["cards"][int(user_id)]

def marked_set(user_id, card_index):
    key = (int(user_id), int(card_index))
    if key not in game_state["marked"]:
        game_state["marked"][key] = {"FREE"}
    return game_state["marked"][key]

def card_has_bingo(card_matrix, marked):
    marked = set(marked) | {"FREE"}
    for r in range(5):
        if all(card_matrix[r][c] in marked for c in range(5)): return True
    for c in range(5):
        if all(card_matrix[r][c] in marked for r in range(5)): return True
    if all(card_matrix[i][i] in marked for i in range(5)): return True
    if all(card_matrix[i][4-i] in marked for i in range(5)): return True
    return False

def call_next_number():
    with game_lock:
        remaining = [n for n in range(1, MAX_MASTER_NUMBERS + 1) if n not in game_state["called_numbers"]]
        if not remaining: return None
        number = random.choice(remaining)
        game_state["called_numbers"].append(number)
        game_state["current_number"] = number
        
        # Auto-mark numbers on players' cards as numbers are called (Flash Bingo style)
        for u_id, cards_list in game_state["cards"].items():
            for c_idx, card in enumerate(cards_list):
                m_set = marked_set(u_id, c_idx)
                matrix = card["matrix"]
                for r in range(5):
                    for col in range(5):
                        if matrix[r][col] == number:
                            m_set.add(number)
        return number

def auto_caller_loop():
    while True:
        try:
            time.sleep(1)
            now = time.time()
            with game_lock:
                if not game_state["is_active"]:
                    if now >= game_state["prep_ends_at"] and len(game_state["players"]) > 0:
                        game_state["is_active"] = True
                else:
                    if not hasattr(auto_caller_loop, "_last_call"): auto_caller_loop._last_call = 0
                    if now - auto_caller_loop._last_call >= CALL_INTERVAL_SECONDS:
                        auto_caller_loop._last_call = now
                        number = call_next_number()
                        if number is None or len(game_state["called_numbers"]) >= MAX_MASTER_NUMBERS:
                            game_state["is_active"] = False
        except Exception:
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
        game_state["prep_ends_at"] = time.time() + PREPARATION_SECONDS

def webapp_url():
    return (PUBLIC_URL + "/") if PUBLIC_URL else "https://t.me/"

def start_keyboard():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🎮 ቢንጎ ተጫወት", web_app=WebAppInfo(url=webapp_url()))
    ]])

def main_keyboard():
    return ReplyKeyboardMarkup([
        [KeyboardButton("🎮 ቢንጎ ተጫወት"), KeyboardButton("🎁 ፕሮሞ ኮድ")],
        [KeyboardButton("💰 ገንዘብ ለማድረግ"), KeyboardButton("💸 ወጪ ለማድረግ")],
        [KeyboardButton("👥 ጋብዝ & አግኝ"), KeyboardButton("👤 ፕሮፋይል & አካውንት")],
        [KeyboardButton("🆘 እርዳታ"), KeyboardButton("🌐 ቋንቋ / Language")],
        [KeyboardButton("📜 ህግ እና ደንቦች")],
    ], resize_keyboard=True)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user.id, user.username, user.first_name)
    await update.effective_message.reply_text(
        "እንኳን ደህና መጡ ወደ በሽ በሽ ቢንጎ 🎰\n\nከታች ያሉትን ቁልፎች በመጠቀም አገልግሎቶቻችንን ያግኙ 👇",
        reply_markup=start_keyboard()
    )
    await update.effective_message.reply_text("ምናሌ:", reply_markup=main_keyboard())

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user.id, user.username, user.first_name)
    msg = (update.effective_message.text or "").strip()
    if "ቢንጎ ተጫወት" in msg:
        await update.effective_message.reply_text("ጨዋታውን ለመጀመር ከታች ይጫኑ:", reply_markup=start_keyboard())
    elif "ፕሮሞ ኮድ" in msg:
        await update.effective_message.reply_text("🎁 ገና ምንም የለም።")
    elif "ገንዘብ ለማድረግ" in msg:
        await update.effective_message.reply_text("💰 የገንዘብ ማስገቢያ (Deposit)\nTelebirr / CBE Birr: 0918719332 · Enyew")
    elif "ወጪ ለማድረግ" in msg:
        await update.effective_message.reply_text("💸 ገንዘብ ማውጣት (Withdraw)\nለጊዜው በሂደት ላይ ነው።")
    elif "ጋብዝ & አግኝ" in msg:
        await update.effective_message.reply_text("👥 ጓደኛዎን በመጋበዝ ሽልማት ያግኙ!")
    elif "ፕሮፋይል & አካውንት" in msg:
        row = get_user(user.id)
        c = row["credits"] if row else 100
        w = row["win_credits"] if row else 0
        await update.effective_message.reply_text(f"👤 አካውንትዎ:\nPlay: {c} ETB\nWin: {w} ETB")
    elif "እርዳታ" in msg:
        await update.effective_message.reply_text("🆘 ድጋፍ ለማግኘት @admin ያግኙ።")
    elif "ቋንቋ" in msg:
        await update.effective_message.reply_text("🌐 አማርኛ ተመርጧል።")
    elif "ህግ" in msg:
        await update.effective_message.reply_text("📜 ሕጎች:\n• ካርድ ዋጋ: 10 ብር\n• 5x5 ካርቴላ (1-75)\n• ማስተር ቁጥሮች (1-75)")
    else:
        await update.effective_message.reply_text("እባክዎ ከታች ያሉትን ምናሌዎች ይጠቀሙ:", reply_markup=main_keyboard())

bot_application.add_handler(CommandHandler("start", start))
bot_application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

INDEX_HTML = r"""<!doctype html><html lang="am"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Besh Besh Bingo</title><link rel="stylesheet" href="/static/style.css"></head>
<body><main class="app-shell">
<header class="topbar"><div><h1>BESH BESH BINGO 🎰</h1></div><div id="current">—</div></header>
<div id="prep-timer" style="background:#142d19;padding:8px;border-radius:10px;text-align:center;margin-bottom:10px">ጨዋታው ሊጀመር: <b>50 ሰከንድ</b> ይቀራል</div>
<section class="game-panel"><h2>MASTER BOARD (1 - 75)</h2><div class="master-grid" id="master-grid"></div></section>
<section class="cards-section" id="cards-container"></section>
<div class="actions-row">
  <div style="grid-column: span 2; display:flex; gap:6px;">
    <input id="card-id-input" type="number" placeholder="የካርድ ቁጥር (ለምሳሌ 400, 66)" style="flex:1;padding:8px;border-radius:8px;border:1px solid var(--line);background:#06180d;color:white;">
    <button id="buy-specific-card" class="primary-btn">ካርድ ይምረጡ</button>
  </div>
  <button id="claim" class="success-btn" style="grid-column: span 2;">🏆 BINGO!</button>
</div>
</main><script src="/static/app.js"></script></body></html>"""

STYLE_CSS = r""":root{color-scheme:dark;--bg:#041509;--panel:#0b2113;--panel2:#102d19;--line:#20552d;--text:#f4f7f4;--gold:#ffe629;--teal:#39d66f}
body{margin:0;background:var(--bg);font-family:system-ui,sans-serif;color:var(--text);padding:12px}
.app-shell{max-width:520px;margin:auto}.topbar{display:flex;justify-content:space-between;background:var(--panel);padding:10px;border-radius:10px;margin-bottom:10px}
.master-grid{display:grid;grid-template-columns:repeat(15,1fr);gap:2px;max-height:140px;overflow-y:auto;background:#06180d;padding:4px}
.m-cell{aspect-ratio:1;background:var(--panel2);font-size:8px;display:grid;place-items:center;color:#a3b8a8}
.m-cell.called{background:var(--teal);color:#041509;font-weight:900}
.bingo-card{background:var(--panel2);border:1px solid var(--line);border-radius:10px;padding:8px;margin-bottom:8px}
.card-grid{display:grid;grid-template-columns:repeat(5,1fr);gap:3px}
.c-cell{aspect-ratio:1;background:#06180d;border:1px solid var(--line);font-size:11px;font-weight:bold;display:grid;place-items:center}
.c-cell.head{background:#173d22;color:var(--gold);font-size:9px}
.c-cell.free{background:#483308;color:var(--gold)}
.c-cell.called{border-color:var(--teal)}
.c-cell.marked{background:#114624;color:var(--teal)}
.actions-row{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.primary-btn,.success-btn{padding:10px;border-radius:8px;font-weight:bold;border:0;cursor:pointer}
.primary-btn{background:var(--panel2);color:var(--text)}
.success-btn{background:var(--teal);color:#041509}"""

APP_JS = r"""async function api(path,method='GET',body=null){const r=await fetch(path,{method,headers:{'Content-Type':'application/json'},body:body?JSON.stringify(body):null});return r.json()}
async function render(){
let model=await api('/api/miniapp/game');
document.getElementById('current').textContent=model.current_number??'—';
let mg=document.getElementById('master-grid');mg.innerHTML='';
let called=new Set(model.called_numbers);
for(let i=1;i<=75;i++){let d=document.createElement('div');d.className='m-cell'+(called.has(i)?' called':'');d.textContent=i;mg.appendChild(d);}
let cc=document.getElementById('cards-container');cc.innerHTML='';
model.cards.forEach((card,idx)=>{
  let marked=new Set(model.marked[idx]||['FREE']);
  let box=document.createElement('div');box.className='bingo-card';
  box.innerHTML=`<div style="font-size:12px;color:var(--gold);margin-bottom:4px">ካርድ #${card.id} (1-75)</div>`;
  let grid=document.createElement('div');grid.className='card-grid';
  ['B','I','N','G','O'].forEach(h=>{let d=document.createElement('div');d.className='c-cell head';d.textContent=h;grid.appendChild(d);});
  card.matrix.forEach(row=>row.forEach(n=>{
    let d=document.createElement('div');d.className='c-cell';
    if(n==='FREE'){d.classList.add('free');d.textContent='★';}
    else{
      d.textContent=n;
      if(called.has(n))d.classList.add('called');
      if(marked.has(n))d.classList.add('marked');
    }
    grid.appendChild(d);
  }));
  box.appendChild(grid);cc.appendChild(box);
});
}
document.getElementById('buy-specific-card').onclick=()=>{
  let val=Number(document.getElementById('card-id-input').value);
  if(!val){alert('እባክዎ የሚፈልጉትን የካርድ ቁጥር ያስገቡ (ለምሳሌ 400, 66)');return;}
  api('/api/miniapp/buy-card','POST',{card_id:val}).then(res=>{
    if(res.error) alert(res.error);
    else { document.getElementById('card-id-input').value=''; render(); }
  });
};
document.getElementById('claim').onclick=()=>api('/api/miniapp/claim','POST').then(d=>alert(d.message||d.error)).then(render);
setInterval(render,2000);render();
"""

@app.route("/", methods=["GET"])
def index(): return Response(INDEX_HTML, mimetype="text/html")

@app.route("/static/style.css")
def serve_css(): return Response(STYLE_CSS, mimetype="text/css")

@app.route("/static/app.js")
def serve_app_js(): return Response(APP_JS, mimetype="text/javascript")

@app.get("/api/miniapp/game")
def api_miniapp_game():
    user_id = 999999
    row = get_user(user_id)
    with game_lock:
        cards = all_user_cards(user_id)
        marked_dict = {str(idx): list(marked_set(user_id, idx)) for idx in range(len(cards))}
        time_left = max(0, int(game_state["prep_ends_at"] - time.time()))
        return jsonify({
            "round": game_state["round"], "prize": game_state["prize"],
            "is_active": game_state["is_active"], "time_left": time_left,
            "called_numbers": game_state["called_numbers"],
            "current_number": game_state["current_number"],
            "credits": row["credits"] if row else 100, "win_credits": row["win_credits"] if row else 0,
            "cards": cards, "marked": marked_dict
        })

@app.post("/api/miniapp/buy-card")
def api_miniapp_buy_card():
    body = request.get_json() or {}
    card_id = int(body.get("card_id", random.randint(1, 1000)))
    user_id = 999999
    with game_lock:
        cards = all_user_cards(user_id)
        if len(cards) >= MAX_CARDS_PER_PLAYER: return jsonify({"error": "እስከ 4 ካርዶች ብቻ መያዝ ይቻላል"}), 400
        # Check if card ID already chosen
        if any(c["id"] == card_id for c in cards): return jsonify({"error": f"ካርድ #{card_id} უკვე ተመርጧል!"}), 400
        new_card = make_card_with_id(card_id)
        cards.append(new_card)
        game_state["players"].add(user_id)
        game_state["prize"] += CARD_COST
    return jsonify({"message": f"ካርድ #{card_id} ተመርጧል!"})

@app.post("/api/miniapp/claim")
def api_miniapp_claim():
    user_id = 999999
    with game_lock:
        cards = all_user_cards(user_id)
        won = any(card_has_bingo(c["matrix"], marked_set(user_id, i)) for i, c in enumerate(cards))
        if not won: return jsonify({"error": "BINGO አልደረሰም!"}), 400
        prize = game_state["prize"] if game_state["prize"] > 0 else 50
    update_credits(user_id, prize, is_win=True)
    reset_round()
    return jsonify({"message": f"ብሩህ አሸናፊ! {prize} ETB አሸንፈዋል!"})

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
            await bot_application.bot.set_webhook(PUBLIC_URL + "/telegram-webhook")
    asyncio.run_coroutine_threadsafe(initialize(), bot_loop)

if __name__ == "__main__":
    start_bot()
    threading.Thread(target=auto_caller_loop, daemon=True).start()
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
