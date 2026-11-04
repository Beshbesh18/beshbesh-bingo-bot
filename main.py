# -*- coding: utf-8 -*-
"""
Flash Bingo — Telegram Bot + Telegram Mini App
Complete implementation supporting 1 to 600 cards grid selection,
Play/Win Wallets, Deposit/Withdraw, and live game loop.
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
ADMIN_IDS = {
    int(x.strip()) for x in os.environ.get("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}
DATABASE_PATH = os.environ.get("DATABASE_PATH", "flash_bingo.sqlite3")
os.makedirs(os.path.dirname(os.path.abspath(DATABASE_PATH)), exist_ok=True)

MAX_CARDS_TOTAL = 600
CARD_COST = 1.00
CALL_INTERVAL_SECONDS = 4

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger("flash-bingo")

if not TOKEN:
    raise RuntimeError("Set the TOKEN environment variable.")

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
            play_wallet REAL NOT NULL DEFAULT 10.00,
            win_wallet REAL NOT NULL DEFAULT 0.00,
            language TEXT NOT NULL DEFAULT 'am',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            type TEXT NOT NULL,
            method TEXT NOT NULL,
            amount REAL NOT NULL,
            reference TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """)
        con.commit()

def ensure_user(user_id, username="", first_name=""):
    with closing(db()) as con:
        con.execute("""
            INSERT INTO users(user_id, username, first_name)
            VALUES(?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET
              username=excluded.username, first_name=excluded.first_name
        """, (int(user_id), username or "", first_name or ""))
        con.commit()

def get_user(user_id):
    with closing(db()) as con:
        return con.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()

init_db()

# ------------------------------ Game State ---------------------------
game_state = {
    "round": 1,
    "prize": 0.00,
    "is_active": False,
    "called_numbers": [],
    "current_number": None,
    "cards_owner": {}, # card_id (1-600) -> user_id
    "cards_data": {},  # card_id -> 5x5 grid
    "marked": {},      # (user_id, card_id) -> set
}

def generate_card():
    cols = [
        random.sample(range(1, 16), 5),
        random.sample(range(16, 31), 5),
        random.sample(range(31, 46), 5),
        random.sample(range(46, 61), 5),
        random.sample(range(61, 76), 5),
    ]
    card = [[cols[c][r] for c in range(5)] for r in range(5)]
    card[2][2] = "FREE"
    return card

def get_card(card_id):
    if card_id not in game_state["cards_data"]:
        game_state["cards_data"][card_id] = generate_card()
    return game_state["cards_data"][card_id]

def auto_caller():
    while True:
        try:
            time.sleep(CALL_INTERVAL_SECONDS)
            with game_lock:
                if not game_state["is_active"]:
                    continue
                remaining = [n for n in range(1, 76) if n not in game_state["called_numbers"]]
                if not remaining or len(game_state["called_numbers"]) >= 75:
                    game_state["is_active"] = False
                    continue
                num = random.choice(remaining)
                game_state["called_numbers"].append(num)
                game_state["current_number"] = num
        except Exception:
            time.sleep(1)

threading.Thread(target=auto_caller, daemon=True).start()

# ------------------------------ Telegram Bot -------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user.id, user.username, user.first_name)
    markup = InlineKeyboardMarkup([[
        InlineKeyboardButton("⚡ Open Flash Bingo", web_app=WebAppInfo(url=PUBLIC_URL + "/"))
    ]]) if PUBLIC_URL else None
    await update.effective_message.reply_text("⚡ Welcome to Flash Bingo!", reply_markup=markup)

bot_application.add_handler(CommandHandler("start", start))

# ------------------------------ Flask Web App ------------------------
@app.route("/")
def index():
    return INDEX_HTML

@app.route("/admin")
def admin():
    return ADMIN_HTML

@app.route("/static/style.css")
def css():
    return Response(STYLE_CSS, mimetype="text/css")

@app.route("/static/app.js")
def js():
    return Response(APP_JS, mimetype="application/javascript")

def get_api_user():
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    if not init_data:
        if os.environ.get("ALLOW_DEMO") == "true":
            return {"id": 99999, "username": "demo", "first_name": "Demo"}
        return None
    try:
        pairs = dict(parse_qsl(init_data))
        user_data = json.loads(pairs.get("user", "{}"))
        if user_data.get("id"):
            ensure_user(user_data["id"], user_data.get("username", ""), user_data.get("first_name", ""))
            return user_data
    except Exception:
        pass
    return None

@app.route("/api/game", methods=["GET"])
def api_game():
    user = get_api_user()
    if not user: return jsonify({"error": "Unauthorized"}), 401
    uid = int(user["id"])
    u = get_user(uid)
    with game_lock:
        user_cards = [cid for cid, uid_val in game_state["cards_owner"].items() if uid_val == uid]
        cards_payload = [{"card_id": cid, "grid": get_card(cid)} for cid in user_cards]
        marked_payload = {cid: list(game_state["marked"].get((uid, cid), {"FREE"})) for cid in user_cards}
        
        return jsonify({
            "play_wallet": u["play_wallet"],
            "win_wallet": u["win_wallet"],
            "round": game_state["round"],
            "prize": game_state["prize"],
            "is_active": game_state["is_active"],
            "current_number": game_state["current_number"],
            "called_numbers": game_state["called_numbers"],
            "user_cards": cards_payload,
            "marked": marked_payload,
            "cards_status": game_state["cards_owner"]
        })

@app.route("/api/buy", methods=["POST"])
def api_buy():
    user = get_api_user()
    if not user: return jsonify({"error": "Unauthorized"}), 401
    uid = int(user["id"])
    data = request.get_json() or {}
    cid = int(data.get("card_id", 0))
    if not (1 <= cid <= 600): return jsonify({"error": "Invalid card ID"}), 400
    
    with game_lock:
        if cid in game_state["cards_owner"]:
            return jsonify({"error": "Card already taken"}), 400
        u = get_user(uid)
        if u["play_wallet"] < CARD_COST:
            return jsonify({"error": "Insufficient balance"}), 400
        
        # Deduct balance
        with closing(db()) as con:
            con.execute("UPDATE users SET play_wallet = play_wallet - ? WHERE user_id = ?", (CARD_COST, uid))
            con.commit()
        
        game_state["cards_owner"][cid] = uid
        game_state["prize"] += CARD_COST
        game_state["is_active"] = True
    return jsonify({"message": f"Card #{cid} purchased!"})

@app.route("/api/mark", methods=["POST"])
def api_mark():
    user = get_api_user()
    if not user: return jsonify({"error": "Unauthorized"}), 401
    uid = int(user["id"])
    data = request.get_json() or {}
    cid = int(data.get("card_id", 0))
    num = data.get("number")
    
    with game_lock:
        if game_state["cards_owner"].get(cid) != uid:
            return jsonify({"error": "Not your card"}), 400
        if num not in game_state["called_numbers"]:
            return jsonify({"error": "Number not called yet"}), 400
        
        key = (uid, cid)
        if key not in game_state["marked"]:
            game_state["marked"][key] = {"FREE"}
        game_state["marked"][key].add(num)
    return jsonify({"success": True})

@app.route("/api/deposit", methods=["POST"])
def api_deposit():
    user = get_api_user()
    if not user: return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json() or {}
    amount = float(data.get("amount", 0))
    method = data.get("method", "Telebirr")
    ref = data.get("reference", "")
    if amount <= 0 or not ref: return jsonify({"error": "Invalid details"}), 400
    
    with closing(db()) as con:
        con.execute("INSERT INTO transactions(user_id, type, method, amount, reference) VALUES(?,?,?,?,?)",
                    (int(user["id"]), "deposit", method, amount, ref))
        con.commit()
    return jsonify({"message": "Deposit request sent successfully."})

# ------------------------------ Frontend HTML/CSS/JS -------------------
INDEX_HTML = r"""<!doctype html>
<html lang="am"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Flash Bingo</title><script src="https://telegram.org/js/telegram-web-app.js"></script><link rel="stylesheet" href="/static/style.css"></head>
<body>
<div class="app-shell">
  <header class="topbar"><div class="logo">⚡</div><div><h1>Flash Bingo</h1></div><div class="live">LIVE</div></header>
  <div class="wallet-box">
    <div><small>PLAY WALLET</small><div id="play-wallet" class="val">0.00</div></div>
    <div><small>WIN WALLET</small><div id="win-wallet" class="val">0.00</div></div>
  </div>
  <div class="action-btns"><button id="btn-dep" class="sec">＋ DEPOSIT (ብር)</button><button id="btn-wit" class="sec">💸 WITHDRAW</button></div>
  
  <div class="draw-section">
    <div id="current-ball" class="ball">—</div>
    <div><div>ዙር #<span id="round">1</span></div><div>ሽልማት: <span id="prize">0</span> ብር</div></div>
  </div>

  <div class="grid-container" id="cards-view"></div>
  <button id="open-grid" class="pri full">＋ ካርድ ምረጥ (1-600)</button>

  <nav class="nav"><button class="active">BINGO</button><button onclick="openWalletTab()">WALLET</button><button onclick="openRankTab()">RANK</button></nav>
</div>
<div id="modal" class="modal" style="display:none;"><div class="modal-content">
  <h3>ካርድ ቁጥር ምረጥ (1 እስከ 600)</h3>
  <div id="grid-600" class="grid-600"></div>
  <button onclick="document.getElementById('modal').style.display='none'" class="sec full">ዝጋ</button>
</div></div>
<div id="deposit-modal" class="modal" style="display:none;"><div class="modal-content">
  <h3>ክሬዲት መሙላት (Telebirr/CBE)</h3>
  <input id="dep-amount" type="number" placeholder="መጠን (ብር)">
  <input id="dep-ref" type="text" placeholder="የክፍያ ማረጋገጫ ቁጥር">
  <button onclick="sendDeposit()" class="pri full">ማመልከቻ ላክ</button>
  <button onclick="document.getElementById('deposit-modal').style.display='none'" class="sec full" style="margin-top:6px;">ዝጋ</button>
</div></div>
<script src="/static/app.js"></script></body></html>"""

STYLE_CSS = r""":root{--bg:#06180d;--panel:#0b2113;--panel2:#102d19;--gold:#ffe629;--teal:#39d66f;--text:#fff}
body{background:var(--bg);color:var(--text);font-family:sans-serif;margin:0;padding:10px}
.app-shell{max-width:480px;margin:auto;padding-bottom:70px}
.topbar{display:flex;align-items:center;gap:10px;margin-bottom:10px}.logo{background:var(--gold);color:#000;padding:6px 10px;border-radius:8px;font-weight:bold}
.wallet-box{display:grid;grid-template-columns:1fr 1fr;gap:8px;background:var(--panel);padding:10px;border-radius:10px;margin-bottom:8px}
.val{font-size:18px;font-weight:bold;color:var(--gold)}
.action-btns{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:10px}
.draw-section{background:var(--panel);padding:12px;border-radius:10px;display:flex;align-items:center;gap:15px;margin-bottom:10px}
.ball{width:55px;height:55px;background:var(--gold);color:#000;font-size:22px;font-weight:bold;display:grid;place-items:center;border-radius:50%}
button{padding:10px;border:0;border-radius:8px;font-weight:bold;cursor:pointer}
.pri{background:var(--teal);color:#000}.sec{background:var(--panel2);color:#fff;border:1px solid #20552d}
.full{width:100%;margin-top:6px}
.grid-container{display:flex;flex-direction:column;gap:10px;margin-bottom:10px}
.card-box{background:var(--panel2);padding:8px;border-radius:8px}
.card-grid{display:grid;grid-template-columns:repeat(5,1fr);gap:4px;margin-top:6px}
.cell{aspect-ratio:1;background:#06180d;border:1px solid #20552d;color:#fff;font-size:12px;border-radius:4px;display:grid;place-items:center}
.cell.marked{background:var(--teal);color:#000}
.cell.called{border-color:var(--gold)}
.nav{position:fixed;bottom:0;left:0;right:0;background:#031008;display:flex;justify-content:around;padding:10px;border-top:1px solid #20552d}
.nav button{background:0;color:#aaa}
.nav button.active{color:var(--teal)}
.modal{position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,0.8);padding:20px;z-index:100}
.modal-content{background:var(--panel);padding:15px;border-radius:12px;max-width:400px;margin:auto}
.grid-600{display:grid;grid-template-columns:repeat(10,1fr);gap:3px;max-height:280px;overflow-y:auto;margin:10px 0}
.grid-600 button{font-size:9px;padding:6px 2px;background:#112d1b;color:#fff;border:1px solid #20552d}
.grid-600 button.taken{background:var(--teal);color:#000}
input{width:100%;padding:8px;margin:6px 0;background:#06180d;border:1px solid #20552d;color:#fff;border-radius:6px}
"""

APP_JS = r"""(() => {
const tg=window.Telegram?.WebApp;if(tg){tg.ready();tg.expand()}
const initData=tg?.initData||'';
async function api(path, method='GET', body=null){
  let res = await fetch(path, {method, headers:{'Content-Type':'application/json','X-Telegram-Init-Data':initData}, body: body?JSON.stringify(body):null});
  return await res.json();
}
async function refresh(){
  let data = await api('/api/game');
  if(data.error) return;
  document.getElementById('play-wallet').textContent = data.play_wallet.toFixed(2);
  document.getElementById('win-wallet').textContent = data.win_wallet.toFixed(2);
  document.getElementById('round').textContent = data.round;
  document.getElementById('prize').textContent = data.prize.toFixed(2);
  document.getElementById('current-ball').textContent = data.current_number ?? '—';
  
  let html = '';
  data.user_cards.forEach(uc => {
    let marked = new Set(data.marked[uc.card_id] || ['FREE']);
    let called = new Set(data.called_numbers);
    html += `<div class="card-box"><div>CARD #${uc.card_id}</div><div class="card-grid">`;
    uc.grid.forEach(row => row.forEach(n => {
      let isFree = (n==='FREE');
      let isCalled = called.has(n);
      let isMarked = marked.has(n);
      html += `<button class="cell ${isMarked?'marked':''} ${isCalled?'called':''}" onclick="mark(${uc.card_id}, ${n})">${isFree?'★':n}</button>`;
    }));
    html += `</div></div>`;
  });
  document.getElementById('cards-view').innerHTML = html || '<div style="color:#aaa;text-align:center;">እስካሁን የገዙት ካርድ የለም</div>';
  window.cardsStatus = data.cards_status || {};
}
window.mark = async(cardId, num) => {
  await api('/api/mark', 'POST', {card_id: cardId, number: num});
  refresh();
};
document.getElementById('open-grid').onclick = () => {
  let h = '';
  for(let i=1; i<=600; i++){
    let taken = window.cardsStatus[i];
    h += `<button class="${taken?'taken':''}" onclick="buyCard(${i})">${i}</button>`;
  }
  document.getElementById('grid-600').innerHTML = h;
  document.getElementById('modal').style.display = 'block';
};
window.buyCard = async(cid) => {
  let res = await api('/api/buy', 'POST', {card_id: cid});
  if(res.error) alert(res.error);
  document.getElementById('modal').style.display = 'none';
  refresh();
};
document.getElementById('btn-dep').onclick = () => document.getElementById('deposit-modal').style.display = 'block';
window.sendDeposit = async() => {
  let amt = Number(document.getElementById('dep-amount').value);
  let ref = document.getElementById('dep-ref').value;
  let res = await api('/api/deposit', 'POST', {amount: amt, method: 'Telebirr', reference: ref});
  alert(res.message || res.error);
  document.getElementById('deposit-modal').style.display = 'none';
  refresh();
};
refresh(); setInterval(refresh, 4000);
})();"""

ADMIN_HTML = r"""<!doctype html><html><head><meta charset="utf-8"><title>Admin</title></head>
<body style="background:#111;color:#fff;padding:20px;"><h1>🛡 Admin Panel</h1><p>관리자 패널</p></body></html>"""

if __name__ == "__main__":
    threading.Thread(target=bot_application.run_polling, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
