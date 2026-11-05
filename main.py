# -*- coding: utf-8 -*-
"""
Flash Bingo — Telegram bot + Telegram Mini App
Updated with 1-600 Card Selection (50s timer), 1-75 Card Number Pool (5x5 matrix),
Play/Win Wallets, Deposit/Withdraw, and Rank features.
"""
import os
import json
import time
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
)
from telegram.ext import (
    Application, CommandHandler, ContextTypes,
)

# --------------------------- Configuration ---------------------------
TOKEN = os.environ.get("TOKEN", "").strip()
PUBLIC_URL = os.environ.get("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
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

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("flash-bingo")

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
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS withdraw_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            account_info TEXT NOT NULL,
            amount REAL NOT NULL,
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
        """, (int(user_id), username or "", first_name or "", STARTING_CREDITS, 0.0))
        con.commit()

def update_credits(user_id, delta, is_win=False):
    with closing(db()) as con:
        col = "win_credits" if is_win else "credits"
        con.execute(f"UPDATE users SET {col}={col}+? WHERE user_id=?", (float(delta), int(user_id)))
        con.commit()
        row = con.execute(f"SELECT credits, win_credits FROM users WHERE user_id=?", (int(user_id),)).fetchone()
        return row["credits"] if row else 0.0

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
        "taken_card_numbers": set(),  # 1 to 600 numbers chosen by players
        "player_cards": {},           # user_id -> list of chosen card IDs (e.g. [54, 200])
        "card_matrices": {},          # (user_id, card_id) -> 5x5 grid (1-75)
        "marked": {},                 # (user_id, card_id) -> set of marked numbers/FREE
        "winner_ids": set(),
    }

game_state = fresh_game()

def generate_75_bingo_card(seed_val):
    # Standard Bingo 5x5 using numbers 1 to 75 distributed across columns
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

def game_loop_worker():
    global game_state
    while True:
        time.sleep(1)
        with game_lock:
            now = time.time()
            if game_state["phase"] == "selecting":
                if now >= game_state["selection_ends_at"]:
                    # Transition to playing phase
                    game_state["phase"] = "playing"
                    game_state["called_numbers"] = []
                    game_state["current_number"] = None
            elif game_state["phase"] == "playing":
                # Call numbers every interval if active players exist
                if game_state["player_cards"]:
                    # check call interval via time or separate thread loop
                    pass

def auto_caller_loop():
    while True:
        time.sleep(CALL_INTERVAL_SECONDS)
        with game_lock:
            if game_state["phase"] == "playing" and game_state["player_cards"]:
                remaining = [n for n in range(1, MAX_NUMBERS_POOL + 1) if n not in game_state["called_numbers"]]
                if remaining:
                    num = random.choice(remaining)
                    game_state["called_numbers"].append(num)
                    game_state["current_number"] = num
                    
                    # Check if any player won automatically or can claim
                    for uid, card_ids in list(game_state["player_cards"].items()):
                        if uid in game_state["winner_ids"]:
                            continue
                        for cid in card_ids:
                            mat = game_state["card_matrices"].get((uid, cid))
                            mset = game_state["marked"].get((uid, cid), {"FREE"})
                            if mat and card_has_bingo(mat, mset):
                                # Auto trigger win or mark winner
                                pass

def reset_round():
    global game_state
    with game_lock:
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
        game_state["winner_ids"] = set()

# ------------------------------ Telegram Handlers -------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user.id, user.username, user.first_name)
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("⚡ Flash Bingo ክፈት", web_app=WebAppInfo(url=PUBLIC_URL + "/"))
    ]]) if PUBLIC_URL else None
    await update.effective_message.reply_text("⚡ እንኳን ደህና መጡ ወደ Flash Bingo!", reply_markup=keyboard)

bot_application.add_handler(CommandHandler("start", start))

# ------------------------------ HTML / CSS / JS Assets ---------------------
INDEX_HTML = r"""<!doctype html>
<html lang="am"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no"><meta name="theme-color" content="#06180d"><title>Flash Bingo</title><script src="https://telegram.org/js/telegram-web-app.js"></script><link rel="stylesheet" href="/static/style.css"></head>
<body><main class="app-shell">
<header class="topbar">
  <div class="brand-info"><div class="eyebrow">⚡ FLASH BINGO</div><h1 id="round-title">ዙር #1</h1></div>
  <div class="top-scores">
    <div class="score-box"><span>PLAY</span><strong id="play-bal">0.00</strong></div>
    <div class="score-box win"><span>WIN</span><strong id="win-bal">0.00</strong></div>
    <div class="current-call-badge" id="current">—</div>
  </div>
</header>

<div id="phase-banner" class="phase-banner">ሰዓት እያለቀ ነው: <span id="timer">50</span> ሰከንድ (ካርድ ይምረጡ 1-600)</div>

<section class="game-panel" id="selection-panel">
  <div class="section-header">
    <h2>ካርድ ቁጥር ይምረጡ (ከ 1 እስከ 600)</h2>
    <span class="card-count-pill" id="my-chosen-count">የተመረጡ: 0/4</span>
  </div>
  <div class="master-grid" id="number-picker-grid"></div>
</section>

<section class="game-panel" id="live-game-panel" style="display:none;">
  <div class="section-header">
    <h2>የተጠሩ ቁጥሮች (1 - 75)</h2>
    <span class="card-count-pill" id="card-count">ካርዶች: 0</span>
  </div>
  <div class="master-grid-75" id="master-grid-75"></div>
</section>

<section class="cards-section" id="cards-container"></section>

<div class="actions-row" id="game-actions" style="display:none;">
  <button id="claim" class="success-btn">🏆 BINGO! አግኝ</button>
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
  <h2>🏆 ከፍተኛ ተጫዋቾች</h2>
  <div id="rank-list">ጭነት ላይ...</div>
</div>

<nav class="bottom-nav">
  <button class="nav-btn active" data-tab="bingo" onclick="switchTab('bingo')">🎯 BINGO</button>
  <button class="nav-btn" data-tab="wallet" onclick="switchTab('wallet')">💳 WALLET</button>
  <button class="nav-btn" data-tab="rank" onclick="switchTab('rank')">🏆 RANK</button>
</nav>
</main><div id="toast"></div><script src="/static/app.js"></script></body></html>"""

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
.card-count-pill{background:var(--panel2);padding:4px 8px;border-radius:6px;border:1px solid var(--line);font-size:11px;color:var(--teal)}
.master-grid{display:grid;grid-template-columns:repeat(15,1fr);gap:2px;max-height:220px;overflow-y:auto;background:#06180d;padding:6px;border-radius:10px;border:1px solid var(--line)}
.master-grid-75{display:grid;grid-template-columns:repeat(10,1fr);gap:3px;max-height:180px;overflow-y:auto;background:#06180d;padding:6px;border-radius:10px;border:1px solid var(--line)}
.picker-btn{aspect-ratio:1;background:var(--panel2);border:1px solid var(--line);border-radius:3px;font-size:8px;display:grid;place-items:center;font-weight:700;color:var(--text);cursor:pointer}
.picker-btn.taken{background:#331010;color:#777;border-color:#551111;cursor:not-allowed}
.picker-btn.mine{background:var(--teal);color:#041509;font-weight:900}
.m-cell{aspect-ratio:1;background:var(--panel2);border-radius:4px;font-size:9px;display:grid;place-items:center;font-weight:700;color:var(--muted)}
.m-cell.called{background:var(--teal);color:#041509;font-weight:900}
.cards-section{display:flex;flex-direction:column;gap:10px;margin-bottom:12px}
.bingo-card{background:var(--panel2);border:1px solid var(--line);border-radius:12px;padding:10px}
.card-title{font-size:12px;font-weight:bold;margin-bottom:6px;color:var(--gold)}
.card-grid{display:grid;grid-template-columns:repeat(5,1fr);gap:4px}
.c-cell{aspect-ratio:1;background:#06180d;border:1px solid var(--line);border-radius:6px;font-size:11px;font-weight:800;display:grid;place-items:center;cursor:pointer}
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
const tg=window.Telegram?.WebApp;if(tg){tg.ready();tg.expand()}
const initData=tg?.initData||'';let model=null;
function toast(s){const e=document.getElementById('toast');e.textContent=s;e.style.display='block';setTimeout(()=>e.style.display='none',2500)}

async function api(path,method='GET',body=null){const o={method,headers:{'Content-Type':'application/json','X-Telegram-Init-Data':initData}};if(body)o.body=JSON.stringify(body);const r=await fetch(path,o);const d=await r.json().catch(()=>({error:'Error'}));if(!r.ok)throw Error(d.error||'Failed');return d}
function render(){if(!model)return;
document.getElementById('round-title').textContent='ዙር #'+model.round;
document.getElementById('play-bal').textContent=model.credits.toFixed(2);
document.getElementById('win-bal').textContent=(model.win_credits||0).toFixed(2);
document.getElementById('w-play').textContent=model.credits.toFixed(2);
document.getElementById('w-win').textContent=(model.win_credits||0).toFixed(2);
document.getElementById('current').textContent=model.current_number??'—';

const isSelecting=(model.phase==='selecting');
document.getElementById('selection-panel').style.display=isSelecting?'block':'none';
document.getElementById('live-game-panel').style.display=isSelecting?'none':'block';
document.getElementById('game-actions').style.display=isSelecting?'none':'grid';

// Timer calculation
let rem=Math.max(0, Math.floor(model.selection_ends_at - Date.now()/1000));
document.getElementById('timer').textContent=rem;
document.getElementById('phase-banner.').style=isSelecting?'display:block':'display:none';

document.getElementById('my-chosen-count').textContent='የተመረጡ: '+model.my_cards.length+'/4';

// 1-600 picker grid
let picker=document.getElementById('number-picker-grid');
if(picker.children.length===0 && isSelecting){
  picker.innerHTML='';
  for(let i=1;i<=600;i++){
    let btn=document.createElement('button');btn.className='picker-btn';
    btn.textContent=i;
    btn.onclick=()=>action('/api/miniapp/select-card',{card_number:i});
    picker.appendChild(btn);
  }
}
// update classes for picker
if(isSelecting){
  let takenSet=new Set(model.taken_cards||[]);
  let mySet=new Set(model.my_cards||[]);
  Array.from(picker.children).forEach(btn=>{
    let n=Number(btn.textContent);
    btn.className='picker-btn';
    if(mySet.has(n))btn.classList.add('mine');
    else if(takenSet.has(n))btn.classList.add('taken');
  });
}

// 1-75 live master grid
let calledSet=new Set(model.called_numbers);
let mg75=document.getElementById('master-grid-75');
if(mg75.children.length===0 && !isSelecting){
  mg75.innerHTML='';
  for(let i=1;i<=75;i++){
    let d=document.createElement('div');d.className='m-cell';d.id='m75-'+i;d.textContent=i;
    mg75.appendChild(d);
  }
}
if(!isSelecting){
  for(let i=1;i<=75;i++){
    let el=document.getElementById('m75-'+i);
    if(el)el.className='m-cell'+(calledSet.has(i)?' called':'');
  }
}

// Render player cards (5x5, 1-75 pool)
let cc=document.getElementById('cards-container');cc.innerHTML='';
model.my_cards_data.forEach(item=>{
  let cid=item.card_id;
  let matrix=item.matrix;
  let marked=new Set(item.marked||['FREE']);
  let box=document.createElement('div');box.className='bingo-card';
  box.innerHTML=`<div class="card-title">ካርድ #${cid} (1-75)</div>`;
  let grid=document.createElement('div');grid.className='card-grid';
  matrix.forEach((row,r)=>row.forEach((n,c)=>{
    let btn=document.createElement('button');btn.className='c-cell';
    if(n==='FREE'){btn.classList.add('free');btn.textContent='★';}
    else{
      btn.textContent=n;
      if(calledSet.has(n))btn.classList.add('called');
      if(marked.has(n))btn.classList.add('marked');
      btn.onclick=()=>action('/api/miniapp/mark',{card_id:cid,number:n});
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
  document.getElementById('selection-panel').style.display=(tab==='bingo' && model?.phase==='selecting')?'block':'none';
  document.getElementById('live-game-panel').style.display=(tab==='bingo' && model?.phase==='playing')?'block':'none';
  document.getElementById('cards-container').style.display=tab==='bingo'?'flex':'none';
  document.getElementById('game-actions').style.display=(tab==='bingo' && model?.phase==='playing')?'grid':'none';
  document.getElementById('wallet-section').style.display=tab==='wallet'?'block':'none';
  document.getElementById('rank-section').style.display=tab==='rank'?'block':'none';
  if(tab==='rank')loadRank();
}
async function loadRank(){try{const d=await api('/api/miniapp/rank');document.getElementById('rank-list').innerHTML=d.rows.map((r,i)=>`<div style="padding:6px;border-bottom:1px solid #20552d">${i+1}. ${r.name} — <b>${r.credits} ETB</b></div>`).join('');}catch(e){toast(e.message)}}

document.getElementById('claim').onclick=()=>action('/api/miniapp/claim');
document.getElementById('show-deposit').onclick=()=>{document.getElementById('deposit-form').style.display='block';document.getElementById('withdraw-form').style.display='none';};
document.getElementById('show-withdraw').onclick=()=>{document.getElementById('withdraw-form').style.display='block';document.getElementById('deposit-form').style.display='none';};
document.getElementById('send-deposit').onclick=()=>{
  const amount=Number(document.getElementById('deposit-amount').value),method=document.getElementById('deposit-method').value,reference=document.getElementById('deposit-reference').value.trim();
  if(!amount||reference.length<3){toast('መረጃ ይሙሉ');return;}
  action('/api/miniapp/deposit-request',{amount,method,reference}).then(()=>{document.getElementById('deposit-reference').value='';})
};
document.getElementById('send-withdraw').onclick=()=>{
  const amount=Number(document.getElementById('withdraw-amount').value),account_info=document.getElementById('withdraw-account').value.trim();
  if(!amount||account_info.length<3){toast('መረጃ ይሙሉ');return;}
  action('/api/miniapp/withdraw-request',{amount,account_info}).then(()=>{document.getElementById('withdraw-amount').value='';document.getElementById('withdraw-account').value='';})
};

refresh();setInterval(refresh,1000);
})();"""

# ------------------------------ Flask Routes & API -------------------------
def api_user():
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    try:
        parsed = dict(parse_qsl(init_data))
        user_str = parsed.get("user")
        if user_str:
            u = json.loads(user_str)
            return {"id": u.get("id"), "username": u.get("username", ""), "first_name": u.get("first_name", "")}
    except Exception:
        pass
    return None

def json_error(msg, code=400):
    return jsonify({"error": msg}), code

@app.route("/static/style.css")
def serve_css(): return Response(STYLE_CSS, mimetype="text/css")

@app.route("/static/app.js")
def serve_app_js(): return Response(APP_JS, mimetype="application/javascript")

@app.route("/", methods=["GET"])
def index(): return Response(INDEX_HTML, mimetype="text/html")

@app.get("/api/miniapp/game")
def api_miniapp_game():
    user = api_user()
    if not user: return json_error("Unauthorized", 401)
    uid = user["id"]
    row = get_user(uid)
    with game_lock:
        now = time.time()
        # check timer phase switch
        if game_state["phase"] == "selecting" and now >= game_state["selection_ends_at"]:
            game_state["phase"] = "playing"
        
        my_cards = game_state["player_cards"].get(uid, [])
        my_cards_data = []
        for cid in my_cards:
            mat = game_state["card_matrices"].setdefault((uid, cid), generate_75_bingo_card(cid * 999 + uid))
            mset = list(game_state["marked"].setdefault((uid, cid), {"FREE"}))
            my_cards_data.append({"card_id": cid, "matrix": mat, "marked": mset})

        taken_list = list(game_state["taken_card_numbers"])

        return jsonify({
            "round": game_state["round"],
            "phase": game_state["phase"],
            "selection_ends_at": game_state["selection_ends_at"],
            "prize": game_state["prize"],
            "called_numbers": game_state["called_numbers"],
            "current_number": game_state["current_number"],
            "credits": row["credits"] if row else 100.0,
            "win_credits": row["win_credits"] if row else 0.0,
            "taken_cards": taken_list,
            "my_cards": my_cards,
            "my_cards_data": my_cards_data
        })

@app.post("/api/miniapp/select-card")
def api_miniapp_select_card():
    user = api_user()
    if not user: return json_error("Unauthorized", 401)
    uid = user["id"]
    body = request.get_json() or {}
    card_num = body.get("card_number")
    if card_num is None or card_num < 1 or card_num > 600:
        return json_error("ልክ ያልሆነ ካርድ ቁጥር")
    
    with game_lock:
        if game_state["phase"] != "selecting":
            return json_error("የካርድ ምርጫ ጊዜ አልቋል")
        row = get_user(uid)
        if (row["credits"] if row else 100.0) < CARD_COST:
            return json_error("በቂ ሂሳብ የለዎትም (10 ብር ያስፈልጋል)")
        
        my_cards = game_state["player_cards"].setdefault(uid, [])
        if card_num in my_cards:
            # Deselect / cancel
            my_cards.remove(card_num)
            game_state["taken_card_numbers"].discard(card_num)
            game_state["prize"] -= CARD_COST
            update_credits(uid, CARD_COST, is_win=False) # refund
            return jsonify({"message": "ካርዱ ተሰርዟል"})
        
        if len(my_cards) >= MAX_CARDS_PER_PLAYER:
            return json_error("ከ 4 በላይ ካርድ መያዝ አይቻልም")
        if card_num in game_state["taken_card_numbers"]:
            return json_error("ይህ ካርድ በሌላ ተጫዋች ተይዟል")
        
        game_state["taken_card_numbers"].add(card_num)
        my_cards.append(card_num)
        game_state["prize"] += CARD_COST
        update_credits(uid, -CARD_COST, is_win=False)
        
        # generate 5x5 matrix (1-75 pool)
        game_state["card_matrices"][(uid, card_num)] = generate_75_bingo_card(card_num * 999 + uid)
        game_state["marked"][(uid, card_num)] = {"FREE"}

    return jsonify({"message": f"ካርድ #{card_num} ተይዟል!"})

@app.post("/api/miniapp/mark")
def api_miniapp_mark():
    user = api_user()
    if not user: return json_error("Unauthorized", 401)
    uid = user["id"]
    body = request.get_json() or {}
    cid, num = body.get("card_id"), body.get("number")
    with game_lock:
        if game_state["phase"] != "playing": return json_error("ጨዋታው ገና አልጀመረም")
        if num != "FREE" and num not in game_state["called_numbers"]:
            return json_error("ቁጥሩ ገና አልተጠራም")
        key = (uid, cid)
        if key not in game_state["marked"]:
            game_state["marked"][key] = {"FREE"}
        game_state["marked"][key].add(num)
    return jsonify({"message": "ምልክት ተደርጓል"})

@app.post("/api/miniapp/claim")
def api_miniapp_claim():
    user = api_user()
    if not user: return json_error("Unauthorized", 401)
    uid = user["id"]
    with game_lock:
        my_cards = game_state["player_cards"].get(uid, [])
        won = any(card_has_bingo(game_state["card_matrices"].get((uid, cid), []), game_state["marked"].get((uid, cid), {"FREE"})) for cid in my_cards)
    if not won: return json_error("BINGO አልደረሰም!")
    if uid in game_state["winner_ids"]: return json_error("ቀደም ሲል ተጠይቋል")
    game_state["winner_ids"].add(uid)
    prize = game_state["prize"] if game_state["prize"] > 0 else 50.0
    update_credits(uid, prize, is_win=True)
    reset_round()
    return jsonify({"message": f"ብሩህ አሸናፊ! {prize} ETB አሸንፈዋል!"})

@app.post("/api/miniapp/deposit-request")
def api_miniapp_deposit():
    user = api_user()
    if not user: return json_error("Unauthorized", 401)
    body = request.get_json() or {}
    amt, method, ref = body.get("amount"), body.get("method"), body.get("reference")
    if not amt or not ref: return json_error("መረጃ ይሙሉ")
    with closing(db()) as con:
        con.execute("INSERT INTO deposit_requests(user_id, method, amount, reference) VALUES(?,?,?,?)", (user["id"], method, amt, ref))
        con.commit()
    return jsonify({"message": "የክፍያ ጥያቄ ተልኳል"})

@app.post("/api/miniapp/withdraw-request")
def api_miniapp_withdraw():
    user = api_user()
    if not user: return json_error("Unauthorized", 401)
    body = request.get_json() or {}
    amt, account_info = body.get("amount"), body.get("account_info")
    if not amt or not account_info: return json_error("መረጃ ይሙሉ")
    row = get_user(user["id"])
    if (row["win_credits"] if row else 0.0) < amt: return json_error("በቂ የድል ሂሳብ (Win Wallet) የለዎትም")
    with closing(db()) as con:
        con.execute("INSERT INTO withdraw_requests(user_id, account_info, amount) VALUES(?,?,?)", (user["id"], account_info, amt))
        con.commit()
    update_credits(user["id"], -amt, is_win=True)
    return jsonify({"message": "የማውጣት ጥያቄ ተልኳል"})

@app.get("/api/miniapp/rank")
def miniapp_rank():
    with closing(db()) as con:
        rows = con.execute("SELECT username, first_name, win_credits FROM users ORDER BY win_credits DESC LIMIT 10").fetchall()
        return jsonify({"rows": [{"name": ("@" + r["username"]) if r["username"] else (r["first_name"] or "Player"), "credits": r["win_credits"]} for r in rows]})

# ------------------------------ Initialization ---------------------------
def run_flask():
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

if __name__ == "__main__":
    threading.Thread(target=game_loop_worker, daemon=True).start()
    threading.Thread(target=auto_caller_loop, daemon=True).start()
    threading.Thread(target=run_flask, daemon=True).start()

    bot_loop.run_until_complete(bot_application.initialize())
    bot_loop.run_until_complete(bot_application.start())
    logger.info("Flash Bingo Bot started successfully.")
    
    try:
        bot_loop.run_forever()
    except (KeyboardInterrupt, SystemExit):
        bot_loop.run_until_complete(bot_application.stop())
