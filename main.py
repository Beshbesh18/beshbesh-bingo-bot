# -*- coding: utf-8 -*-
"""
Besh Besh Bingo — Telegram bot + Telegram Mini App
Updated with specific Card IDs (e.g. Card #500, #560) and 5x5 (1-75) layout.
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
MAX_MASTER_NUMBERS = 600
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

def fresh_game():
    return {
        "round": 1, "prize": 0, "is_active": False,
        "called_numbers": [], "current_number": None,
        "players": set(), "cards": {}, "marked": {}, "winner_ids": set(),
        "prep_ends_at": time.time() + PREPARATION_SECONDS,
    }

game_state = fresh_game()

def make_card_with_id(card_id):
    # Seed random with card_id so each specific card number (e.g. #500, #560) always generates its unique 5x5 numbers
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

@app.route("/", methods=["GET"])
def index():
    return Response(INDEX_HTML, mimetype="text/html")

@app.route("/static/style.css")
def serve_css(): return Response(STYLE_CSS, mimetype="text/css")

@app.route("/static/app.js")
def serve_app_js(): return Response(APP_JS, mimetype="text/javascript")

@app.get("/api/miniapp/game")
def api_miniapp_game():
    data = request.headers.get("X-Telegram-Init-Data", "")
    # Simplified mock user for local test or Telegram WebApp
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
    user_id = 999999
    with game_lock:
        cards = all_user_cards(user_id)
        if len(cards) >= MAX_CARDS_PER_PLAYER: return jsonify({"error": "እስከ 4 ካርዶች ብቻ መያዝ ይቻላል"}), 400
        # Assign a specific card number/ID like 500, 560, etc.
        card_id = random.randint(100, 600)
        new_card = make_card_with_id(card_id)
        cards.append(new_card)
        game_state["players"].add(user_id)
        game_state["prize"] += CARD_COST
    return jsonify({"message": f"ካርድ #{card_id} ተገዝቷል!"})

@app.post("/api/miniapp/mark")
def api_miniapp_mark():
    body = request.get_json() or {}
    idx, num = body.get("card_index"), body.get("number")
    user_id = 999999
    with game_lock:
        cards = all_user_cards(user_id)
        if idx is None or idx < 0 or idx >= len(cards): return jsonify({"error": "Invalid card"}), 400
        if num != "FREE" and num not in game_state["called_numbers"]: return jsonify({"error": "ቁጥሩ ገና አልተጠራም"}), 400
        marked_set(user_id, idx).add(num)
    return jsonify({"message": "ምልክት ተደርጓል"})

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

INDEX_HTML = r"""<!doctype html><html lang="am"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Besh Besh Bingo</title><link rel="stylesheet" href="/static/style.css"></head>
<body><main class="app-shell">
<header class="topbar"><div><h1>BESH BESH BINGO 🎰</h1></div><div id="current">—</div></header>
<div id="prep-timer" style="background:#142d19;padding:8px;border-radius:10px;text-align:center;margin-bottom:10px">ጨዋታው ሊጀመር: <b>50 ሰከንድ</b> ይቀራል</div>
<section class="game-panel"><h2>MASTER BOARD (1 - 600)</h2><div class="master-grid" id="master-grid"></div></section>
<section class="cards-section" id="cards-container"></section>
<div class="actions-row"><button id="buy-card" class="primary-btn">＋ ካርድ ግዢ (10 ብር)</button><button id="claim" class="success-btn">🏆 BINGO!</button></div>
</main><script src="/static/app.js"></script></body></html>"""

STYLE_CSS = r""":root{color-scheme:dark;--bg:#041509;--panel:#0b2113;--panel2:#102d19;--line:#20552d;--text:#f4f7f4;--gold:#ffe629;--teal:#39d66f}
body{margin:0;background:var(--bg);font-family:system-ui,sans-serif;color:var(--text);padding:12px}
.app-shell{max-width:520px;margin:auto}.topbar{display:flex;justify-content:space-between;background:var(--panel);padding:10px;border-radius:10px;margin-bottom:10px}
.master-grid{display:grid;grid-template-columns:repeat(10,1fr);gap:2px;max-height:140px;overflow-y:auto;background:#06180d;padding:4px}
.m-cell{aspect-ratio:1;background:var(--panel2);font-size:8px;display:grid;place-items:center;color:#a3b8a8}
.m-cell.called{background:var(--teal);color:#041509;font-weight:900}
.bingo-card{background:var(--panel2);border:1px solid var(--line);border-radius:10px;padding:8px;margin-bottom:8px}
.card-grid{display:grid;grid-template-columns:repeat(5,1fr);gap:3px}
.c-cell{aspect-ratio:1;background:#06180d;border:1px solid var(--line);font-size:11px;font-weight:bold;display:grid;place-items:center;cursor:pointer}
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
for(let i=1;i<=600;i++){let d=document.createElement('div');d.className='m-cell'+(called.has(i)?' called':'');d.textContent=i;mg.appendChild(d);}
let cc=document.getElementById('cards-container');cc.innerHTML='';
model.cards.forEach((card,idx)=>{
  let marked=new Set(model.marked[idx]||['FREE']);
  let box=document.createElement('div');box.className='bingo-card';
  box.innerHTML=`<div style="font-size:12px;color:var(--gold);margin-bottom:4px">ካርድ #${card.id} (1-75)</div>`;
  let grid=document.createElement('div');grid.className='card-grid';
  ['B','I','N','G','O'].forEach(h=>{let d=document.createElement('div');d.className='c-cell head';d.textContent=h;grid.appendChild(d);});
  card.matrix.forEach(row=>row.forEach(n=>{
    let btn=document.createElement('button');btn.className='c-cell';
    if(n==='FREE'){btn.classList.add('free');btn.textContent='★';}
    else{
      btn.textContent=n;
      if(called.has(n))btn.classList.add('called');
      if(marked.has(n))btn.classList.add('marked');
      btn.onclick=()=>api('/api/miniapp/mark','POST',{card_index:idx,number:n}).then(render);
    }
    grid.appendChild(btn);
  }));
  box.appendChild(grid);cc.appendChild(box);
});
}
document.getElementById('buy-card').onclick=()=>api('/api/miniapp/buy-card','POST').then(render);
document.getElementById('claim').onclick=()=>api('/api/miniapp/claim','POST').then(d=>alert(d.message||d.error)).then(render);
setInterval(render,2000);render();
"""

if __name__ == "__main__":
    start_bot()
    threading.Thread(target=auto_caller_loop, daemon=True).start()
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
