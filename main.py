# -*- coding: utf-8 -*-
"""
Besh Besh Bingo â€” Telegram Bot & Mini App
- Main pool numbers: 1 to 600
- Player cards: 5x5 matrix with numbers ranging from 1 to 75
- Card limit: Maximum 4 cards per player
"""
import os
import json
import time
import random
import sqlite3
from flask import Flask, request, jsonify
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import Application, CommandHandler, ContextTypes

# --------------------------- Configuration ---------------------------
TOKEN = os.environ.get("TOKEN", "").strip()
PUBLIC_URL = os.environ.get("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
ADMIN_IDS = {
    int(x.strip()) for x in os.environ.get("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}
DATABASE_PATH = os.environ.get("DATABASE_PATH", "bingo.sqlite3")
os.makedirs(os.path.dirname(os.path.abspath(DATABASE_PATH)), exist_ok=True)

STARTING_CREDITS = 100
MAX_CARDS_PER_PLAYER = 4
MAX_NUMBERS = 600

if not TOKEN:
    raise RuntimeError("Set the TOKEN environment variable to your Telegram bot token.")

app = Flask(__name__)
bot_application = Application.builder().token(TOKEN).build()

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
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """)
        con.commit()

def ensure_user(user_id, username="", first_name=""):
    with closing(db()) as con:
        con.execute("""
            INSERT INTO users(user_id, username, first_name, credits, win_credits)
            VALUES(?,?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET
              username=excluded.username, first_name=excluded.first_name
        """, (int(user_id), username or "", first_name or "", STARTING_CREDITS, 0))
        con.commit()

init_db()

# ------------------------------ Game Logic ---------------------------
game_state = {
    "round": 1,
    "prize": 2272,
    "is_active": True,
    "called_numbers": [39, 66, 8, 71, 41, 15, 63],
    "current_number": 66,
    "cards": {}, # user_id -> list of 5x5 cards
}

def make_card():
    """5x5 áŠ«áˆ­á‰´áˆ‹ áŠ¨ 1 áŠ¥áˆµáŠ¨ 75 á‰£áˆ‰á‰µ á‰áŒ¥áˆ®á‰½ á‹­áˆáŒ¥áˆ«áˆ"""
    selected_numbers = random.sample(range(1, 76), 25)
    card = [selected_numbers[i * 5:(i + 1) * 5] for i in range(5)]
    card[2][2] = "FREE"
    return card

# ------------------------------ Telegram Handlers -------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user.id, user.username, user.first_name)
    markup = InlineKeyboardMarkup([[
        InlineKeyboardButton("ðŸŽ° á‰ áˆ½ á‰ áˆ½ á‰¢áŠ•áŒŽ áŠ­áá‰µ", web_app=WebAppInfo(url=PUBLIC_URL + "/"))
    ]]) if PUBLIC_URL else None
    
    await update.effective_message.reply_text(
        "ðŸŽ° áŠ¥áŠ•áŠ³áŠ• á‹°áˆ…áŠ“ áˆ˜áŒ¡ á‹ˆá‹° á‰ áˆ½ á‰ áˆ½ á‰¢áŠ•áŒŽ!\n\náŒ¨á‹‹á‰³á‹áŠ• áˆˆáˆ˜áŒ€áˆ˜áˆ­ áŠ¨á‰³á‰½ á‹«áˆˆá‹áŠ• áˆŠáŠ•áŠ­ á‹­áŒ«áŠ‘á¢",
        reply_markup=markup
    )

bot_application.add_handler(CommandHandler("start", start))

# ------------------------------ Flask API Endpoints -----------------------
@app.route("/")
def index():
    return INDEX_HTML

@app.route("/api/state")
def api_state():
    return jsonify({
        "round": game_state["round"],
        "prize": game_state["prize"],
        "current_number": game_state["current_number"],
        "called_numbers": game_state["called_numbers"]
    })

@app.route("/api/buy_card", methods=["POST"])
def api_buy_card():
    data = request.json or {}
    user_id = int(data.get("user_id", 0))
    if not user_id:
        return jsonify({"error": "Unauthorized"}), 401
    
    user_cards = game_state["cards"].setdefault(user_id, [])
    if len(user_cards) >= MAX_CARDS_PER_PLAYER:
        return jsonify({"error": "á‹­á‰…áˆ­á‰³! áŠ¨áŠ áˆ«á‰µ (4) áŠ«áˆ­á‰´áˆ‹ á‰ áˆ‹á‹­ áˆ˜á‹«á‹ áŠ á‹­á‰½áˆ‰áˆá¢"}), 400
    
    new_card = make_card()
    user_cards.append(new_card)
    return jsonify({"success": True, "total_cards": len(user_cards), "card": new_card})

# ------------------------------ Frontend HTML/CSS/JS ----------------------
INDEX_HTML = r"""<!doctype html>
<html lang="am">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>á‰ áˆ½ á‰ áˆ½ á‰¢áŠ•áŒŽ</title>
<style>
  body { background: #06180d; color: #fff; font-family: sans-serif; margin: 0; padding: 10px; text-align: center; }
  .topbar { background: #0f2d1a; padding: 15px; border-radius: 12px; margin-bottom: 15px; border: 1px solid #20552d; }
  .primary-btn { background: #f3c614; color: #000; border: none; padding: 12px 24px; font-weight: bold; border-radius: 8px; cursor: pointer; width: 100%; margin-top: 10px; }
  .grid { display: grid; grid-template-columns: repeat(10, 1fr); gap: 4px; margin-top: 10px; }
  .cell { background: #133822; padding: 8px 2px; font-size: 12px; border-radius: 4px; border: 1px solid #1f4e33; }
  .cell.active { background: #f3c614; color: #000; font-weight: bold; }
</style>
</head>
<body>
  <div class="topbar">
    <h2>âš¡ á‰ áˆ½ á‰ áˆ½ á‰¢áŠ•áŒŽ âš¡</h2>
    <p>á‹™áˆ­ # <span id="round-val">1</span> | áˆ½áˆáˆ›á‰µ: <span id="prize-val">2272</span> á‰¥áˆ­</p>
    <div>á‹¨á‹ˆáŒ£ á‰áŒ¥áˆ­ (1-600): <h1 id="curr-num" style="color: #f3c614; margin: 5px;">--</h1></div>
  </div>
  
  <h3>á‹¨Ð³Ð»Ð°Ð²Ð½Ñ‹Ð¹ á‰áŒ¥áˆ®á‰½ áˆ°áˆŒá‹³ (1 - 600)</h3>
  <div class="grid" id="number-board"></div>

  <button class="primary-btn" onclick="buyCard()">ï¼‹ áŠ«áˆ­á‹µ áŒá‹¢ (áŠ¨áˆ›áŠ­áˆµ 4)</button>

  <script>
    const board = document.getElementById('number-board');
    for(let i=1; i<=100; i++) {
      let div = document.createElement('div');
      div.className = 'cell' + (i === 66 ? ' active' : '');
      div.innerText = i;
      board.appendChild(div);
    }
    function buyCard() {
      alert("áŠ«áˆ­á‹µá‹Ž á‰°áŒˆá‹á‰·áˆ! (5x5 áŠ«áˆ­á‰´áˆ‹ áŠ¨ 1-75)");
    }
  </script>
</body>
</html>
"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
