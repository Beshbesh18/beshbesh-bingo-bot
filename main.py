# -*- coding: utf-8 -*-
"""
Besh Besh Bingo — Full Telegram bot + Telegram Mini App
Complete with 1-600 master numbers, 5x5 (1-75) cards with unique IDs, wallet, and admin panel.
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

def set_language(user_id, language):
    with closing(db()) as con:
        con.execute("UPDATE users SET language=? WHERE user_id=?", (language, int(user_id)))
        con.commit()

def get_language(user_id):
    row = get_user(user_id)
    return row["language"] if row else "am"

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

TEXT = {
    "am": {
        "welcome": "🎰 እንኳን ደህና መጡ ወደ በሽ በሽ ቢንጎ!",
        "play": "🎮 በሽ በሽ ቢንጎ ጨዋታ",
        "balance": "💰 ብር ሂሳብ",
        "rules": "📜 ሕግ እና ደንቦች",
        "wallet": "💳 Wallet / ኪስ ቦርሳ",
        "rank": "🏆 ደረጃ",
        "language": "🌐 ቋንቋ / Language",
        "support": "ជំនួយ",
        "choose": "ከታች ያሉትን አማራጮች ይምረጡ።",
        "balance_msg": "💰 የኪስ ሂሳብዎ:\nPlay Wallet: {credits} ETB\nWin Wallet: {win_credits} ETB",
        "rules_msg": "📜 ሕጎች\n• ካርድ ዋጋ: 10 ብር\n• ከ 1 እስከ 75 ቁጥሮች ያሉት 5x5 ካርቴላዎች\n• ማስተር ቁጥሮች ከ 1 እስከ 600",
    }
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

def start_keyboard():
    if PUBLIC_URL:
        return InlineKeyboardMarkup([[InlineKeyboardButton("🎰 በሽ በሽ ቢንጎ ክፍት", web_app=WebAppInfo(url=PUBLIC_URL + "/"))]])
    return None

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user.id, user.username, user.first_name)
    markup = start_keyboard()
    if markup:
        await update.effective_message.reply_text(tr(user.id, "welcome"), reply_markup=markup)
    await update.effective_message.reply_text("Besh Besh Bingo 🎰", reply_markup=main_keyboard(user.id))

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user.id, user.username, user.first_name)
    msg = (update.effective_message.text or "").strip()
    if msg in ("🎮 በሽ በሽ ቢንጎ ጨዋታ",):
        markup = start_keyboard()
        if markup: await update.effective_message.reply_text("Besh Besh Bingo 🎰", reply_markup=markup)
    elif msg in ("💰 ብር ሂሳብ",):
        row = get_user(user.id)
        await update.effective_message.reply_text(tr(user.id, "balance_msg", credits=row["credits"], win_credits=row["win_credits"]))
    elif msg in ("📜 ሕግ እና ደንቦች",):
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
        await update.effective_message.reply_text(tr(user.id, "choose"), reply_markup=main_keyboard(user.id))

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        await update.effective_message.reply_text("🚫 Unauthorized.")
        return
    if PUBLIC_URL:
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🛡 Open Admin Panel", web_app=WebAppInfo(url=PUBLIC_URL + "/admin"))]])
        await update.effective_message.reply_text("🛡 Admin panel", reply_markup=keyboard)

bot_application.add_handler(CommandHandler("start", start))
bot_application.add_handler(CommandHandler("admin", admin_command))
bot_application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

INDEX_HTML = r"""<!doctype html><html lang="am"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Besh Besh Bingo</title><link rel="stylesheet" href="/static/style.css"></head>
<body><main class="app-shell">
<header class="topbar"><div><h1>BESH BESH BINGO 🎰</h1></div><div id="current">—</div></header>
<div id="prep-timer" style="background:#142d19;padding:8px;border-radius:10px;text-align:center;margin-bottom:10px">ጨዋታው ሊጀመር: <b>50 ሰከንድ</b> ይቀራል</div>
<section class="game-panel"><h2>MASTER BOARD (1 - 600)</h2><div class="master-grid" id="master-grid"></div></section>
<section class="cards-section" id="cards-container"></section>
<div class="actions-row"><button id="buy-card" class="primary-btn">＋ ካርድ ግዢ (10 ብር)</button><button id="claim" class="success-btn">🏆 BINGO!</button></div>
<div class="wallet-section" id="wallet-section" style="display:none"><h2>💳 ኪስ ቦርሳ</h2><select id="deposit-method"><option>Telebirr</option><option>CBE Birr</option></select><input id="deposit-amount" type="number" placeholder="መጠን"><input id="deposit-reference" placeholder="Ref ID"><button id="send-deposit" class="primary-btn">ጥያቄ ላክ</button></div>
<div class="rank-section" id="rank-section" style="display:none"><h2>🏆 ደረጃ</h2><div id="rank-list">ጭነት ላይ...</div></div>
<nav class="bottom-nav"><button onclick="switchTab('bingo')">🎯 BINGO</button><button onclick="switchTab('wallet')">💳 WALLET</button><button onclick="switchTab('rank')">🏆 RANK</button></nav>
</main><script src="/static/app.js"></script></body></html>"""

ADMIN_HTML = r"""<!doctype html><html lang="am"><head><meta charset="utf-8"><title>Admin</title><link rel="stylesheet" href="/static/style.css"></head>
<body style="padding:16px"><h1>🛡 አድሚን ፓነል</h1><div id="req-list">ጭነት ላይ...</div><script src="/static/admin.js"></script></body></html>"""

STYLE_CSS = r""":root{color-scheme:dark;--bg:#041509;--panel:#0b2113;--panel2:#102d19;--line:#20552d;--text:#f4f7f4;--gold:#ffe629;--teal:#39d66f}
body{margin:0;background:var(--bg);font-family:system-ui,sans-serif;color:var(--text);padding:12px;padding-bottom:70px}
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
.success-btn{background:var(--teal);color:#041509}
.bottom-nav{position:fixed;bottom:0;left:0;right:0;background:#04150df0;display:flex;justify-content:around;padding:8px;border-top:1px solid var(--line)}
.bottom-nav button{background:0;border:0;color:var(--text);font-weight:bold;cursor:pointer}"""

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
window.switchTab=function(tab){
  document.querySelector('.game-panel').style.display=tab==='bingo'?'block':'none';
  document.getElementById('cards-container').style.display=tab==='bingo'?'flex':'none';
  document.querySelector('.actions-row').style.display=tab==='bingo'?'grid':'none';
  document.getElementById('wallet-section').style.display=tab==='wallet'?'block':'none';
  document.getElementById('rank-section').style.display=tab==='rank'?'block':'none';
  if(tab==='rank') loadRank();
}
async function loadRank(){
let d=await api('/api/miniapp/rank');
document.getElementById('rank-list').innerHTML=d.rows.map((r,i)=>`<div>${i+1}. ${r.name} — ${r.credits} ETB</div>`).join('');
}
document.getElementById('buy-card').onclick=()=>api('/api/miniapp/buy-card','POST').then(render);
document.getElementById('claim').onclick=()=>api('/api/miniapp/claim','POST').then(d=>alert(d.message||d.error)).then(render);
document.getElementById('send-deposit').onclick=()=>{
  let amount=Number(document.getElementById('deposit-amount').value), method=document.getElementById('deposit-method').value, reference=document.getElementById('deposit-reference').value;
  api('/api/miniapp/deposit-request','POST',{amount,method,reference}).then(d=>alert(d.message));
};
setInterval(render,2000);render();
"""

ADMIN_JS = r"""async function load(){
const r=await fetch('/api/admin/overview');
const d=await r.json();
document.getElementById('req-list').innerHTML=d.requests.map(r=>`<div style="background:#0b2113;padding:10px;border-radius:8px;margin-bottom:8px"><b>#${r.id} · ${r.amount} ብር (${r.method})</b><button onclick="review(${r.id},'approve')" style="background:#39d66f;border:0;padding:6px 12px;border-radius:6px;font-weight:bold">አጽድቅ</button></div>`).join('');
}
async function review(id,decision){await fetch(`/api/admin/deposit/${id}/review`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({decision})});load();}
load();"""

@app.route("/", methods=["GET"])
def index(): return Response(INDEX_HTML, mimetype="text/html")

@app.route("/admin", methods=["GET"])
def admin_panel(): return Response(ADMIN_HTML, mimetype="text/html")

@app.route("/static/style.css")
def serve_css(): return Response(STYLE_CSS, mimetype="text/css")

@app.route("/static/app.js")
def serve_app_js(): return Response(APP_JS, mimetype="text/javascript")

@app.route("/static/admin.js")
def serve_admin_js(): return Response(ADMIN_JS, mimetype="text/javascript")

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
    user_id = 999999
    with game_lock:
        cards = all_user_cards(user_id)
        if len(cards) >= MAX_CARDS_PER_PLAYER: return jsonify({"error": "እስከ 4 ካርዶች ብቻ መያዝ ይቻላል"}), 400
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

@app.post("/api/miniapp/deposit-request")
def api_miniapp_deposit():
    body = request.get_json() or {}
    amt, method, ref = body.get("amount"), body.get("method"), body.get("reference")
    with closing(db()) as con:
        con.execute("INSERT INTO deposit_requests(user_id, method, amount, reference) VALUES(?,?,?,?)", (999999, method, amt, ref))
        con.commit()
    return jsonify({"message": "የክፍያ ጥያቄ ተልኳል"})

@app.get("/api/miniapp/rank")
def miniapp_rank():
    with closing(db()) as con:
        rows = con.execute("SELECT username, first_name, win_credits FROM users ORDER BY win_credits DESC LIMIT 10").fetchall()
        return jsonify({"rows": [{"name": ("@" + r["username"]) if r["username"] else (r["first_name"] or "Player"), "credits": r["win_credits"]} for r in rows]})

@app.get("/api/admin/overview")
def admin_overview():
    with closing(db()) as con:
        reqs = con.execute("SELECT * FROM deposit_requests ORDER BY id DESC LIMIT 50").fetchall()
        return jsonify({"requests": [dict(r) for r in reqs]})

@app.post("/api/admin/deposit/<int:req_id>/review")
def admin_review(req_id):
    body = request.get_json(silent=True) or {}
    decision = body.get("decision")
    with closing(db()) as con:
        row = con.execute("SELECT * FROM deposit_requests WHERE id=?", (req_id,)).fetchone()
        if not row or row["status"] != "pending": return jsonify({"error": "Invalid"}), 400
        status = "approved" if decision == "approve" else "rejected"
        con.execute("UPDATE deposit_requests SET status=?, reviewed_at=CURRENT_TIMESTAMP WHERE id=?", (status, req_id))
        if decision == "approve":
            con.execute("UPDATE users SET credits=credits+? WHERE user_id=?", (row["amount"], row["user_id"]))
        con.commit()
    return jsonify({"ok": True, "message": "ተረጋግጧል"})

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
