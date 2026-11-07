# -*- coding: utf-8 -*-
"""
\u1260\u123d \u1260\u123d \u1262\u1295\u130e (Besh Besh Bingo) \u2014 Telegram bot + Telegram Mini App
1-600 Card Selection (50s timer), 1-75 Master Grid & Cards,
Play/Win Wallets, Deposit/Withdraw, Line Bingo check, and Voice Number Calling.

NOTE: game state lives in memory, so run with a SINGLE worker process
(e.g. `gunicorn -w 1 --threads 8 bingo_bot:app`).
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
import re
import sqlite3
from urllib.parse import parse_qsl
from contextlib import closing

from flask import Flask, request, Response, jsonify
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo,
    ReplyKeyboardMarkup, KeyboardButton, BotCommand, MenuButtonCommands,
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
# Free credits every account gets when it first opens the bot. Default 100 (unchanged);
# set the STARTING_CREDITS environment variable on Render (e.g. 0) to change it.
try:
    STARTING_CREDITS = float(os.environ.get("STARTING_CREDITS", "100"))
except ValueError:
    STARTING_CREDITS = 100.0
NEW_USER_BONUS = 10  # welcome bonus (ETB) for a NEW user who shares his phone contact
MAX_CARDS_PER_PLAYER = 4
SELECTION_TIME_SECONDS = 50
CALL_INTERVAL_SECONDS = 3
MAX_NUMBERS_POOL = 75  # 1 to 75 numbers called in game
TOTAL_CARDS = 600
FINISHED_SECONDS = 10  # how long the "BINGO!" result stays on screen

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
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            reviewed_at TEXT,
            reviewed_by INTEGER
        );
        CREATE TABLE IF NOT EXISTS withdraw_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            account_info TEXT NOT NULL,
            amount REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS wins (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            username TEXT NOT NULL DEFAULT '',
            round_no INTEGER NOT NULL,
            card_index INTEGER NOT NULL,
            prize REAL NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """)
        cols = {r["name"] for r in con.execute("PRAGMA table_info(users)").fetchall()}
        if "phone" not in cols:
            con.execute("ALTER TABLE users ADD COLUMN phone TEXT")
        if "bonus_given" not in cols:
            con.execute("ALTER TABLE users ADD COLUMN bonus_given INTEGER NOT NULL DEFAULT 0")
            # accounts that already exist are not "new": they get no welcome bonus
            con.execute("UPDATE users SET bonus_given=1")
        con.commit()


def get_user(user_id):
    with closing(db()) as con:
        return con.execute("SELECT * FROM users WHERE user_id=?", (int(user_id),)).fetchone()


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
        row = con.execute("SELECT credits, win_credits FROM users WHERE user_id=?", (int(user_id),)).fetchone()
        return row[col] if row else 0.0


def charge_card(user_id):
    """Take CARD_COST from the Play wallet; if that is too low take it from the Win wallet.
    Returns "credits", "win" (which wallet paid) or None (not enough money). Atomic."""
    with closing(db()) as con:
        cur = con.execute(
            "UPDATE users SET credits=credits-? WHERE user_id=? AND credits>=?",
            (CARD_COST, int(user_id), CARD_COST))
        if cur.rowcount:
            con.commit()
            return "credits"
        cur = con.execute(
            "UPDATE users SET win_credits=win_credits-? WHERE user_id=? AND win_credits>=?",
            (CARD_COST, int(user_id), CARD_COST))
        con.commit()
        return "win" if cur.rowcount else None


def refund_card(user_id, wallet):
    """Give the card price back to the wallet it was paid from."""
    update_credits(user_id, CARD_COST, is_win=(wallet == "win"))


def get_phone(user_id):
    row = get_user(user_id)
    return row["phone"] if row else None


def register_phone(user_id, phone):
    """Save the shared phone number. Returns:
    'bonus'   - new user, NEW_USER_BONUS was added to his Play wallet
    'saved'   - number saved, no bonus (old account)
    'dup'     - this number already belongs to another account, no bonus
    'already' - this account already shared a number"""
    uid = int(user_id)
    with closing(db()) as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT phone, bonus_given FROM users WHERE user_id=?", (uid,)).fetchone()
        if row is None or row["phone"]:
            con.rollback()
            return "already"
        used = con.execute(
            "SELECT 1 FROM users WHERE phone=? AND user_id!=?", (phone, uid)).fetchone()
        if used:
            con.execute("UPDATE users SET phone=?, bonus_given=1 WHERE user_id=?", (phone, uid))
            result = "dup"
        elif row["bonus_given"]:
            con.execute("UPDATE users SET phone=? WHERE user_id=?", (phone, uid))
            result = "saved"
        else:
            con.execute(
                "UPDATE users SET phone=?, bonus_given=1, credits=credits+? WHERE user_id=?",
                (phone, float(NEW_USER_BONUS), uid))
            result = "bonus"
        con.commit()
        return result


def set_language(user_id, language):
    with closing(db()) as con:
        con.execute("UPDATE users SET language=? WHERE user_id=?", (language, int(user_id)))
        con.commit()


def get_language(user_id):
    row = get_user(user_id)
    return row["language"] if row else "am"


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
        "taken_card_numbers": set(),  # 1..600 numbers chosen by players
        "player_cards": {},           # user_id -> list of chosen card IDs
        "card_matrices": {},          # (user_id, card_id) -> 5x5 grid (1-75)
        "marked": {},                 # (user_id, card_id) -> set of marked numbers/FREE
        "finished_ends_at": 0,        # when the 'finished' (winner shown) phase ends
        "last_winners": [],           # [{user_id, name, card_id, prize}]
        "card_paid_from": {},         # (user_id, card_id) -> "credits" or "win" wallet
    }


game_state = fresh_game()


def generate_75_bingo_card(seed_val):
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
    for r in range(5):  # horizontal
        if all(card[r][c] in marked for c in range(5)):
            return True
    for c in range(5):  # vertical
        if all(card[r][c] in marked for r in range(5)):
            return True
    if all(card[i][i] in marked for i in range(5)):  # diagonal
        return True
    if all(card[i][4 - i] in marked for i in range(5)):
        return True
    # four corners
    if all(card[r][c] in marked for r, c in ((0, 0), (0, 4), (4, 0), (4, 4))):
        return True
    return False


def reset_round(refund=False):
    """Start a new round. Must be called with game_lock held (it is re-entrant)."""
    with game_lock:
        if refund:
            for uid, cards in game_state["player_cards"].items():
                for cid in cards:
                    refund_card(uid, game_state.get("card_paid_from", {}).get((uid, cid), "credits"))
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
        game_state["last_winners"] = []
        game_state["card_paid_from"] = {}
        game_state["finished_ends_at"] = 0


def display_name(uid):
    row = get_user(uid)
    if not row:
        return "Player"
    return ("@" + row["username"]) if row["username"] else (row["first_name"] or "Player")


def find_winners():
    """Return [(uid, card_id)] for every player whose card currently has bingo. Needs game_lock."""
    winners = []
    for uid, cards in game_state["player_cards"].items():
        for cid in cards:
            mat = game_state["card_matrices"].get((uid, cid))
            marked = game_state["marked"].get((uid, cid), {"FREE"})
            if mat and card_has_bingo(mat, marked):
                winners.append((uid, cid))
                break
    return winners


def declare_winners(winners):
    """Pay the prize (split between winners) and stop the round. Needs game_lock."""
    share = round(float(game_state["prize"]) / len(winners), 2)
    results = []
    for uid, cid in winners:
        update_credits(uid, share, is_win=True)
        name = display_name(uid)
        with closing(db()) as con:
            con.execute(
                "INSERT INTO wins(user_id, username, round_no, card_index, prize) VALUES(?,?,?,?,?)",
                (int(uid), name, game_state["round"], cid, share))
            con.commit()
        results.append({"user_id": uid, "name": name, "card_id": cid, "prize": share})
    game_state["last_winners"] = results
    game_state["phase"] = "finished"
    game_state["finished_ends_at"] = time.time() + FINISHED_SECONDS


def try_award(uid, username=""):
    """Manual BINGO claim. Returns the winners list or None."""
    with game_lock:
        if game_state["phase"] != "playing":
            return None
        winners = find_winners()
        if not winners:
            return None
        declare_winners(winners)
        return game_state["last_winners"]


def game_loop_worker():
    """Ends the selection phase when the timer runs out."""
    while True:
        time.sleep(1)
        try:
            with game_lock:
                if game_state["phase"] == "finished" and time.time() >= game_state["finished_ends_at"]:
                    reset_round()
                elif game_state["phase"] == "selecting" and time.time() >= game_state["selection_ends_at"]:
                    has_players = any(game_state["player_cards"].values())
                    if has_players:
                        game_state["phase"] = "playing"
                        game_state["called_numbers"] = []
                        game_state["current_number"] = None
                    else:
                        # nobody picked a card: restart the timer instead of getting stuck
                        game_state["selection_ends_at"] = time.time() + SELECTION_TIME_SECONDS
        except Exception:
            logger.exception("game_loop_worker error")


def call_next_number():
    """Call one number, auto-mark every card and end the round as soon as someone has bingo.
    Needs game_lock."""
    if game_state["phase"] != "playing":
        return
    remaining = [n for n in range(1, MAX_NUMBERS_POOL + 1) if n not in game_state["called_numbers"]]
    if not remaining:
        # all 75 numbers called and no winner: refund and restart
        reset_round(refund=True)
        return
    num = random.choice(remaining)
    game_state["called_numbers"].append(num)
    game_state["current_number"] = num
    for key, mat in game_state["card_matrices"].items():
        if any(num in row for row in mat):
            game_state["marked"].setdefault(key, {"FREE"}).add(num)
    winners = find_winners()
    if winners:
        declare_winners(winners)


def auto_caller_loop():
    """Calls a random number every few seconds while playing."""
    while True:
        time.sleep(CALL_INTERVAL_SECONDS)
        try:
            with game_lock:
                call_next_number()
        except Exception:
            logger.exception("auto_caller_loop error")


# ------------------------------ Localization & Handlers -------------------------
TEXT = {
    "am": {
        "welcome": "\U0001f3b0 \u12a5\u1295\u12b3\u1295 \u12c8\u12f0 \u1260\u123d \u1260\u123d \u1262\u1295\u130e \u1260\u12f0\u1205\u1293 \u1218\u1321!\n\n\u12a0\u1313\u130a \u12a5\u1293 \u1348\u1323\u1295 \u12e8\u1262\u1295\u130e \u1328\u12cb\u1273\u1362",
        "play": "\U0001f3ae \u1260\u123d \u1260\u123d \u1262\u1295\u130e \u1328\u12cb\u1273",
        "balance": "\U0001f4b0 \u1240\u122a \u1202\u1233\u1265",
        "rules": "\U0001f4dc \u1215\u130d \u12a5\u1293 \u12f0\u1295\u1266\u127d",
        "wallet": "\U0001f4b3 Wallet / \u12aa\u1235 \u1266\u122d\u1233",
        "rank": "\U0001f3c6 \u12f0\u1228\u1303",
        "language": "\U0001f310 \u124b\u1295\u124b / Language",
        "support": "\U0001f198 \u12f5\u130b\u134d",
        "admin": "\U0001f6e1 \u12a0\u12f5\u121a\u1295",
        "choose": "\u12a8\u1273\u127d \u12ab\u1209\u1275 \u12a0\u121b\u122b\u132e\u127d \u12ed\u121d\u1228\u1321\u1362",
        "balance_msg": "\U0001f4b0 \u1240\u122a \u1202\u1233\u1265\u12ce\u1366\n\u12e8\u1218\u132b\u12c8\u127b \u1202\u1233\u1265 (Play): {credits} \u1265\u122d\n\u12e8\u12a0\u1238\u1293\u134a\u1290\u1275 \u1202\u1233\u1265 (Win): {win_credits} \u1265\u122d",
        "rules_msg": "\U0001f4dc \u12f0\u1295\u1266\u127d\n\u2022 \u12e8\u12a0\u1295\u12f5 \u12ab\u122d\u12f5 \u12cb\u130b 10 \u1265\u122d \u1290\u12cd\n\u2022 \u12a81 \u12a5\u1235\u12a8 600 \u12ab\u122d\u12f6\u127d \u1218\u121d\u1228\u1325 \u12ed\u127b\u120b\u120d (\u1262\u1260\u12db 4 \u12ab\u122d\u12f5)\n\u2022 \u12a81 \u12a5\u1235\u12a8 75 \u12eb\u1209 \u1241\u1325\u122e\u127d \u12ed\u1320\u122b\u1209\n\u2022 \u12a0\u1295\u12f5 \u1228\u12f5\u134d\u1363 \u12a0\u121d\u12f5\u1363 \u1230\u12eb\u134d \u12c8\u12ed\u121d \u12a0\u122b\u1271 \u121b\u12d5\u12d8\u1296\u127d \u1232\u121e\u1209 \u1262\u1295\u130e \u1290\u12cd",
        "language_set": "\u124b\u1295\u124b\u12cd \u12c8\u12f0 \u12a0\u121b\u122d\u129b \u1270\u1240\u12ed\u122f\u120d\u1362",
        "dep_approved": "\u2705 {amount} \u1265\u122d \u12c8\u12f0 \u1202\u1233\u1265\u12ce \u1270\u1328\u121d\u122f\u120d\u1362 \u1218\u120d\u12ab\u121d \u1328\u12cb\u1273!",
        "dep_rejected": "\u274c \u12e8{amount} \u1265\u122d \u12e8\u1308\u1295\u12d8\u1265 \u121b\u1235\u1308\u1262\u12eb \u1325\u12eb\u1244\u12ce \u12cd\u12f5\u1245 \u1270\u12f0\u122d\u1313\u120d\u1362 \u12a5\u1263\u12ad\u12ce \u12a0\u1235\u1270\u12f3\u12f3\u122a\u12cd\u1295 \u12eb\u1290\u130b\u130d\u1229\u1362",
        "wd_paid": "\u2705 \u12e8{amount} \u1265\u122d \u12e8\u121b\u12cd\u132b \u1325\u12eb\u1244\u12ce \u1270\u12a8\u134d\u120f\u120d\u1362",
        "wd_rejected": "\u274c \u12e8{amount} \u1265\u122d \u12e8\u121b\u12cd\u132b \u1325\u12eb\u1244\u12ce \u12cd\u12f5\u1245 \u1270\u12f0\u122d\u1313\u120d\u1364 \u1265\u1229 \u12c8\u12f0 \u12a0\u1238\u1293\u134a\u1290\u1275 \u1202\u1233\u1265\u12ce \u1270\u1218\u120d\u1237\u120d\u1362",
    },
    "en": {
        "welcome": "\U0001f3b0 Welcome to Besh Besh Bingo!\n\nFast and exciting bingo gaming platform.",
        "play": "\U0001f3ae Play Besh Besh Bingo",
        "balance": "\U0001f4b0 Balance",
        "rules": "\U0001f4dc Rules",
        "wallet": "\U0001f4b3 Wallet",
        "rank": "\U0001f3c6 Rank",
        "language": "\U0001f310 Language",
        "support": "\U0001f198 Help",
        "admin": "\U0001f6e1 Admin",
        "choose": "Choose an option below.",
        "balance_msg": "\U0001f4b0 Your balance:\nPlay Wallet: {credits} ETB\nWin Wallet: {win_credits} ETB",
        "rules_msg": "\U0001f4dc Rules\n\u2022 Card cost: 10 ETB\n\u2022 Pick cards from 1 to 600\n\u2022 Numbers called from 1 to 75\n\u2022 A full row, column, diagonal or the four corners wins BINGO",
        "language_set": "Language changed to English.",
        "dep_approved": "\u2705 {amount} ETB was added to your balance. Good luck!",
        "dep_rejected": "\u274c Your {amount} ETB deposit request was rejected. Please contact the administrator.",
        "wd_paid": "\u2705 Your {amount} ETB withdrawal has been paid.",
        "wd_rejected": "\u274c Your {amount} ETB withdrawal was rejected; the amount is back in your Win wallet.",
    },
}


TEXT["am"].update({
    "share_phone": "\U0001f4f1 \u1235\u120d\u12ad \u1241\u1325\u122c\u1295 \u12a0\u130b\u122b",
    "contact_ask": "\U0001f44b \u12a5\u1295\u12b3\u1295 \u12f0\u1205\u1293 \u1218\u1321!\n\n\U0001f4f1 \u1218\u1208\u12eb\u12ce\u1295 \u1208\u121b\u1228\u130b\u1308\u1325 \u12a5\u1293 \u12e8\U0001f381 {amount} \u1265\u122d \u12e8\u12a5\u1295\u12b3\u1295 \u12f0\u1205\u1293 \u1218\u1321 \u1235\u1326\u1273 \u1208\u121b\u130d\u1298\u1275 \u12a8\u1273\u127d \u12eb\u1208\u12cd\u1295 \u00ab\u1235\u120d\u12ad \u1241\u1325\u122c\u1295 \u12a0\u130b\u122b\u00bb \u1241\u120d\u134d \u12ed\u132b\u1291\u1362",
    "contact_bonus": "\u2705 \u1235\u120d\u12ad \u1241\u1325\u122d\u12ce \u1270\u1218\u12dd\u130d\u1267\u120d!\n\U0001f381 \u12e8{amount} \u1265\u122d \u12e8\u12a5\u1295\u12b3\u1295 \u12f0\u1205\u1293 \u1218\u1321 \u1235\u1326\u1273 \u12c8\u12f0 \u1218\u132b\u12c8\u127b \u1202\u1233\u1265\u12ce \u1270\u1328\u121d\u122f\u120d\u1362\n\n\u1218\u120d\u12ab\u121d \u12d5\u12f5\u120d!",
    "contact_saved": "\u2705 \u1235\u120d\u12ad \u1241\u1325\u122d\u12ce \u1270\u1218\u12dd\u130d\u1267\u120d\u1362",
    "contact_dup": "\u2705 \u1235\u120d\u12ad \u1241\u1325\u122d\u12ce \u1270\u1218\u12dd\u130d\u1267\u120d\u1362 \u12ed\u1205 \u1241\u1325\u122d \u1240\u12f0\u121d \u1232\u120d \u1235\u1326\u1273 \u1235\u1208\u1270\u1240\u1260\u1208 \u12f3\u130d\u121d \u12a0\u120d\u1270\u1230\u1320\u121d\u1362",
    "contact_wrong": "\u26a0\ufe0f \u12a5\u1263\u12ad\u12ce \u12e8\u122b\u1235\u12ce\u1295 \u1235\u120d\u12ad \u1241\u1325\u122d \u1208\u121b\u130b\u122b\u1275 \u12a8\u1273\u127d \u12eb\u1208\u12cd\u1295 \u1241\u120d\u134d \u12ed\u1320\u1240\u1219\u1362",
})
TEXT["en"].update({
    "share_phone": "\U0001f4f1 Share my phone number",
    "contact_ask": "\U0001f44b Welcome!\n\n\U0001f4f1 Tap the \"Share my phone number\" button below to verify your account and get a \U0001f381 {amount} ETB welcome bonus.",
    "contact_bonus": "\u2705 Your phone number is registered!\n\U0001f381 A {amount} ETB welcome bonus was added to your Play wallet.\n\nGood luck!",
    "contact_saved": "\u2705 Your phone number is registered.",
    "contact_dup": "\u2705 Your phone number is registered. This number already received a bonus, so it was not given again.",
    "contact_wrong": "\u26a0\ufe0f Please use the button below to share your own phone number.",
})


def tr(user_id, key, **kwargs):
    lang = get_language(user_id)
    return TEXT.get(lang, TEXT["am"]).get(key, TEXT["am"].get(key, key)).format(**kwargs)


def main_keyboard(user_id):
    lang = get_language(user_id)
    t = TEXT.get(lang, TEXT["am"])
    rows = []
    if not get_phone(user_id):
        # shown on top of the menu until the user has shared his phone contact
        rows.append([KeyboardButton(t["share_phone"], request_contact=True)])
    rows += [
        [KeyboardButton(t["play"]), KeyboardButton(t["balance"])],
        [KeyboardButton(t["wallet"]), KeyboardButton(t["rank"])],
        [KeyboardButton(t["rules"]), KeyboardButton(t["language"])],
        [KeyboardButton(t["support"])],
    ]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def webapp_url():
    return PUBLIC_URL + "/"


def start_keyboard():
    if PUBLIC_URL:
        return InlineKeyboardMarkup([[
            InlineKeyboardButton("\u26a1 \u1260\u123d \u1260\u123d \u1262\u1295\u130e \u12ad\u1348\u1275", web_app=WebAppInfo(url=webapp_url()))
        ]])
    return None


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user.id, user.username, user.first_name)
    text = tr(user.id, "welcome") + "\n\n" + tr(user.id, "choose")
    markup = start_keyboard()
    if markup:
        await update.effective_message.reply_text(text, reply_markup=markup)
    else:
        await update.effective_message.reply_text(text)
    if get_phone(user.id):
        await update.effective_message.reply_text("\u1260\u123d \u1260\u123d \u1262\u1295\u130e \u26a1", reply_markup=main_keyboard(user.id))
    else:
        await update.effective_message.reply_text(
            tr(user.id, "contact_ask", amount=NEW_USER_BONUS), reply_markup=main_keyboard(user.id))


async def language_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user.id, user.username, user.first_name)
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("\u12a0\u121b\u122d\u129b", callback_data="lang:am"),
        InlineKeyboardButton("English", callback_data="lang:en"),
    ]])
    await update.effective_message.reply_text("\u124b\u1295\u124b \u12ed\u121d\u1228\u1321 / Choose language:", reply_markup=keyboard)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user.id, user.username, user.first_name)
    msg = (update.effective_message.text or "").strip()
    lang = get_language(user.id)
    t = TEXT.get(lang, TEXT["am"])

    if msg in (t["play"], "\U0001f3ae \u1260\u123d \u1260\u123d \u1262\u1295\u130e \u1328\u12cb\u1273", "\U0001f3ae Play Besh Besh Bingo",
               "\U0001f3ae Flash Bingo \u1328\u12cb\u1273", "\U0001f3ae Play Flash Bingo"):
        markup = start_keyboard()
        if markup:
            await update.effective_message.reply_text("\u1260\u123d \u1260\u123d \u1262\u1295\u130e \u26a1", reply_markup=markup)
        else:
            await update.effective_message.reply_text("Set RENDER_EXTERNAL_URL to enable the Mini App.")
    elif msg in (t["balance"], "\U0001f4b0 \u1265\u122d \u1202\u1233\u1265", "\U0001f4b0 \u1240\u122a \u1202\u1233\u1265", "\U0001f4b0 Balance"):
        row = get_user(user.id)
        await update.effective_message.reply_text(
            tr(user.id, "balance_msg", credits=row["credits"], win_credits=row["win_credits"]))
    elif msg in (t["rules"], "\U0001f4dc \u1215\u130d \u12a5\u1293 \u12f0\u1295\u1266\u127d", "\U0001f4dc Rules"):
        await update.effective_message.reply_text(tr(user.id, "rules_msg"))
    elif msg in (t["wallet"], "\U0001f4b3 Wallet / \u12aa\u1235 \u1266\u122d\u1233", "\U0001f4b3 Wallet"):
        await update.effective_message.reply_text(
            "\U0001f4b3 Wallet\nTelebirr: 0918719332 \u00b7 Enyew\nCBE Birr: 0918719332 \u00b7 Enyew\n\n"
            "\u12ad\u134d\u12eb\u12cd\u1295 \u12a8\u1348\u1338\u1219 \u1260\u128b\u120b \u1260 Mini App \u12cd\u1235\u1325 \u12e8\u130d\u1265\u12ed\u1275 \u1241\u1325\u1229\u1295 (Transaction ID) \u12ed\u120b\u12a9\u1362")
    elif msg in (t["rank"], "\U0001f3c6 \u12f0\u1228\u1303", "\U0001f3c6 Rank"):
        with closing(db()) as con:
            rows = con.execute(
                "SELECT username, first_name, win_credits FROM users ORDER BY win_credits DESC LIMIT 10"
            ).fetchall()
        lines = ["\U0001f3c6 \u12a8\u134d\u1270\u129b \u1270\u132b\u12cb\u127e\u127d (Top Players)"]
        for i, r in enumerate(rows, 1):
            name = ("@" + r["username"]) if r["username"] else (r["first_name"] or "Player")
            lines.append(f"{i}. {name} \u2014 {r['win_credits']} ETB")
        await update.effective_message.reply_text("\n".join(lines))
    elif msg in (t["language"], "\U0001f310 \u124b\u1295\u124b / Language", "\U0001f310 Language"):
        await language_command(update, context)
    elif msg in (t["support"], "\U0001f198 \u12f5\u130b\u134d", "\U0001f198 Help"):
        await update.effective_message.reply_text("\U0001f198 \u12f5\u130b\u134d / Help\n\u1208\u12a5\u122d\u12f3\u1273 \u12a0\u1235\u1270\u12f3\u12f3\u122a\u12cd\u1295 \u12eb\u1290\u130b\u130d\u1229\u1362\nContact the administrator for support.")
    else:
        await update.effective_message.reply_text(tr(user.id, "choose"), reply_markup=main_keyboard(user.id))


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        await update.effective_message.reply_text("\U0001f6ab Unauthorized.")
        return
    if PUBLIC_URL:
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("\U0001f6e1 Open Admin Panel", web_app=WebAppInfo(url=PUBLIC_URL + "/admin"))
        ]])
        await update.effective_message.reply_text("\U0001f6e1 Admin panel", reply_markup=keyboard)
    else:
        await update.effective_message.reply_text("Admin panel unavailable until RENDER_EXTERNAL_URL is configured.")


async def id_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows the Telegram ID of the user and of the chat (use it to fill ADMIN_IDS / LIVE_CHAT_ID)."""
    await update.effective_message.reply_text(
        "\U0001f464 Your Telegram ID: %s\n\U0001f4ac This chat ID: %s" % (update.effective_user.id, update.effective_chat.id))


async def handle_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """The user pressed the "share my phone number" button."""
    user = update.effective_user
    ensure_user(user.id, user.username, user.first_name)
    contact = update.effective_message.contact
    # the contact must be his OWN number (not a forwarded / picked contact of someone else)
    if not contact or contact.user_id != user.id:
        await update.effective_message.reply_text(
            tr(user.id, "contact_wrong"), reply_markup=main_keyboard(user.id))
        return
    phone = re.sub(r"\D", "", contact.phone_number or "")
    if not phone:
        await update.effective_message.reply_text(
            tr(user.id, "contact_wrong"), reply_markup=main_keyboard(user.id))
        return
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, register_phone, user.id, phone)
    if result == "bonus":
        key = "contact_bonus"
    elif result == "dup":
        key = "contact_dup"
    else:
        key = "contact_saved"
    await update.effective_message.reply_text(
        tr(user.id, key, amount=NEW_USER_BONUS), reply_markup=main_keyboard(user.id))


async def handle_review_button(query, data, admin_id):
    try:
        kind, decision, rid = data.split(":")
        rid = int(rid)
    except ValueError:
        await query.answer("Invalid request", show_alert=True)
        return
    loop = asyncio.get_running_loop()
    if kind == "dep" and decision in ("approve", "reject"):
        row = await loop.run_in_executor(None, review_deposit, rid, decision, admin_id)
        label = "\u2705 \u1338\u12f5\u124b\u120d" if decision == "approve" else "\u274c \u12cd\u12f5\u1245 \u1270\u12f0\u122d\u1313\u120d"
    elif kind == "wd" and decision in ("paid", "reject"):
        row = await loop.run_in_executor(None, review_withdraw, rid, decision)
        label = "\u2705 \u1270\u12a8\u134d\u120f\u120d" if decision == "paid" else "\u274c \u12cd\u12f5\u1245 \u1270\u12f0\u122d\u1313\u120d (\u1265\u1229 \u1270\u1218\u120d\u1237\u120d)"
    else:
        await query.answer("Invalid request", show_alert=True)
        return
    if row is None:
        await query.answer("\u12ed\u1205 \u1325\u12eb\u1244 \u1240\u12f0\u121d \u1265\u120e \u1270\u1235\u1270\u1293\u130d\u12f7\u120d", show_alert=True)
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        return
    await query.answer(label)
    try:
        await query.edit_message_text((query.message.text or "") + "\n\n" + label)
    except Exception:
        logger.exception("could not update the admin message")


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data or ""
    user_id = query.from_user.id
    if data.startswith(("dep:", "wd:")):
        # buttons also work inside a group, so only real admins may press them
        if user_id not in ADMIN_IDS:
            await query.answer("\U0001f6ab Unauthorized", show_alert=True)
            return
        await handle_review_button(query, data, user_id)
        return
    await query.answer()
    ensure_user(user_id, query.from_user.username, query.from_user.first_name)
    if data.startswith("lang:"):
        lang = data.split(":", 1)[1]
        if lang in ("am", "en"):
            set_language(user_id, lang)
            await query.edit_message_text(TEXT[lang]["language_set"])


bot_application.add_handler(CommandHandler("start", start))
bot_application.add_handler(CommandHandler("restart", start))
bot_application.add_handler(MessageHandler(filters.CONTACT, handle_contact))
bot_application.add_handler(CommandHandler("language", language_command))
bot_application.add_handler(CommandHandler("id", id_command))
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
        logger.exception("Failed to validate initData")
        return None


def api_user():
    data = validate_telegram_init_data(request.headers.get("X-Telegram-Init-Data", ""))
    if data:
        ensure_user(data["id"], data.get("username", ""), data.get("first_name", ""))
        return data
    if os.environ.get("ALLOW_DEMO_MODE", "").lower() == "true":
        try:
            demo_id = int(request.headers.get("X-Demo-User", "999999"))
        except ValueError:
            demo_id = 999999
        ensure_user(demo_id, "demo", "Demo Player")
        return {"id": demo_id, "username": "demo", "first_name": "Demo Player"}
    return None


def api_admin():
    user = api_user()
    return user if user and int(user["id"]) in ADMIN_IDS else None


def json_error(message, status=400):
    return jsonify({"error": message}), status


def to_positive_float(value):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


# ------------------------- Admin notifications & reviews -------------------------
def admin_chat_ids():
    ids = set(ADMIN_IDS)
    if LIVE_CHAT_ID:
        try:
            ids.add(int(LIVE_CHAT_ID))
        except ValueError:
            logger.warning("LIVE_CHAT_ID must be a number such as -1001234567890")
    return ids


def send_to_chat(chat_id, text, markup=None):
    """Send a Telegram message from any thread. Never raises."""
    async def _send():
        try:
            await bot_application.bot.send_message(chat_id=chat_id, text=text, reply_markup=markup)
        except Exception:
            logger.exception("could not send a message to chat %s", chat_id)
    try:
        if bot_loop.is_running():
            asyncio.run_coroutine_threadsafe(_send(), bot_loop)
    except Exception:
        logger.exception("send_to_chat failed")


def notify_admins(text, markup=None):
    chats = admin_chat_ids()
    if not chats:
        logger.warning("ADMIN_IDS and LIVE_CHAT_ID are empty: nobody can be notified.")
    for cid in chats:
        send_to_chat(cid, text, markup)


def money(x):
    return ("%.2f" % float(x)).rstrip("0").rstrip(".")


def notify_deposit(req_id, uid, method, amount, ref):
    text = ("\U0001f4b0 \u12a0\u12f2\u1235 \u12e8\u1308\u1295\u12d8\u1265 \u121b\u1235\u1308\u1262\u12eb \u1325\u12eb\u1244 #%d\n\n"
            "\u1270\u132b\u12cb\u127d: %s (ID %s)\n\u1263\u1295\u12ad: %s\n\u1218\u1320\u1295: %s \u1265\u122d\nTransaction ID: %s\n\n"
            "\u12ad\u134d\u12eb\u12cd \u12c8\u12f0 \u12a0\u12ab\u12cd\u1295\u1275\u1205 \u1218\u130d\u1263\u1271\u1295 \u1260\u1263\u1295\u12ad/Telebirr \u1218\u120d\u12a5\u12ad\u1275\u1205 \u12a0\u1228\u130b\u130d\u1320\u1205 \u121d\u1228\u1325\u1362"
            % (req_id, display_name(uid), uid, method, money(amount), ref))
    markup = InlineKeyboardMarkup([[
        InlineKeyboardButton("\u2705 \u12a0\u133d\u12f5\u1245", callback_data="dep:approve:%d" % req_id),
        InlineKeyboardButton("\u274c \u12cd\u12f5\u1245 \u12a0\u12f5\u122d\u130d", callback_data="dep:reject:%d" % req_id),
    ]])
    notify_admins(text, markup)


def notify_withdraw(req_id, uid, account_info, amount):
    text = ("\U0001f4e4 \u12a0\u12f2\u1235 \u12e8\u121b\u12cd\u1323\u1275 \u1325\u12eb\u1244 #%d\n\n"
            "\u1270\u132b\u12cb\u127d: %s (ID %s)\n\u1218\u1320\u1295: %s \u1265\u122d\n\u12e8\u12a0\u12ab\u12cd\u1295\u1275 \u1218\u1228\u1303: %s\n\n"
            "\u1265\u1229\u1295 \u12a8\u120b\u12ad\u1205 \u1260\u128b\u120b \u00ab\u1270\u12a8\u134d\u120f\u120d\u00bb \u1295 \u1270\u132b\u1295\u1362 \u12cd\u12f5\u1245 \u12ab\u12f0\u1228\u130d\u1205 \u1265\u1229 \u12c8\u12f0 \u1270\u132b\u12cb\u1279 \u12ed\u1218\u1208\u1233\u120d\u1362"
            % (req_id, display_name(uid), uid, money(amount), account_info))
    markup = InlineKeyboardMarkup([[
        InlineKeyboardButton("\u2705 \u1270\u12a8\u134d\u120f\u120d", callback_data="wd:paid:%d" % req_id),
        InlineKeyboardButton("\u274c \u12cd\u12f5\u1245 (\u1265\u1229\u1295 \u1218\u120d\u1235)", callback_data="wd:reject:%d" % req_id),
    ]])
    notify_admins(text, markup)


def review_deposit(req_id, decision, admin_id):
    """Approve/reject a pending deposit exactly once. Returns the request row, or None if not pending."""
    status = "approved" if decision == "approve" else "rejected"
    with closing(db()) as con:
        cur = con.execute(
            "UPDATE deposit_requests SET status=?, reviewed_at=CURRENT_TIMESTAMP, reviewed_by=? "
            "WHERE id=? AND status='pending'", (status, int(admin_id), req_id))
        if cur.rowcount != 1:
            con.commit()
            return None
        row = con.execute("SELECT * FROM deposit_requests WHERE id=?", (req_id,)).fetchone()
        if decision == "approve":
            con.execute("UPDATE users SET credits=credits+? WHERE user_id=?", (row["amount"], row["user_id"]))
        con.commit()
    send_to_chat(row["user_id"], tr(row["user_id"], "dep_" + status, amount=money(row["amount"])))
    return row


def review_withdraw(req_id, decision):
    """Mark a pending withdrawal as paid, or reject it and refund the Win wallet. Returns the row or None."""
    status = "paid" if decision == "paid" else "rejected"
    with closing(db()) as con:
        cur = con.execute("UPDATE withdraw_requests SET status=? WHERE id=? AND status='pending'",
                          (status, req_id))
        if cur.rowcount != 1:
            con.commit()
            return None
        row = con.execute("SELECT * FROM withdraw_requests WHERE id=?", (req_id,)).fetchone()
        if status == "rejected":
            con.execute("UPDATE users SET win_credits=win_credits+? WHERE user_id=?",
                        (row["amount"], row["user_id"]))
        con.commit()
    send_to_chat(row["user_id"], tr(row["user_id"], "wd_" + status, amount=money(row["amount"])))
    return row


# ------------------------------ HTML & JS Assets ---------------------------
INDEX_HTML = r"""<!doctype html>
<html lang="am"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no"><meta name="theme-color" content="#06180d"><title>&#x1260;&#x123D; &#x1260;&#x123D; &#x1262;&#x1295;&#x130E;</title><script src="https://telegram.org/js/telegram-web-app.js"></script><link rel="stylesheet" href="/static/style.css"></head>
<body><main class="app-shell">
<header class="topbar">
  <div class="brand-info"><div class="eyebrow">&#x26A1; &#x1260;&#x123D; &#x1260;&#x123D; &#x1262;&#x1295;&#x130E; &#xB7; v8</div><h1 id="round-title">&#x12D9;&#x122D; #1</h1></div>
  <div class="top-scores">
    <div class="score-box"><span>PLAY</span><strong id="play-bal">0.00</strong></div>
    <div class="score-box win"><span>WIN</span><strong id="win-bal">0.00</strong></div>
    <button id="sound-btn" class="sound-btn" title="Sound">&#x1F50A;</button>
    <div class="current-call-badge" id="current">&#x2014;</div>
  </div>
</header>

<div id="phase-banner" class="phase-banner"></div>

<section class="game-panel" id="selection-panel">
  <div class="section-header">
    <h2>&#x12E8;&#x12AB;&#x122D;&#x12F5; &#x1241;&#x1325;&#x122D; &#x12ED;&#x121D;&#x1228;&#x1321; (&#x12A8;1 &#x12A5;&#x1235;&#x12A8; 600)</h2>
    <span class="card-count-pill" id="my-chosen-count">&#x12E8;&#x1270;&#x1218;&#x1228;&#x1321; &#x12AB;&#x122D;&#x12F6;&#x127D;: 0/4</span>
  </div>
  <div class="master-grid" id="number-picker-grid"></div>
</section>

<section class="game-panel" id="live-game-panel" style="display:none;">
  <div class="section-header">
    <h2>&#x12E8;&#x1270;&#x1320;&#x1229; &#x1241;&#x1325;&#x122E;&#x127D; (1 - 75)</h2>
    <span class="card-count-pill" id="card-count">&#x12AB;&#x122D;&#x12F6;&#x127D;: 0</span>
  </div>
  <div class="master-grid-75" id="master-grid-75"></div>
  <div class="called-balls-row" id="called-balls-row"></div>
</section>

<section class="cards-section" id="cards-container"></section>

<div class="actions-row" id="game-actions" style="display:none;">
  <button id="claim" class="success-btn">&#x1F3C6; BINGO &#x1260;&#x120D;!</button>
</div>

<div class="wallet-section" id="wallet-section" style="display:none;">
  <h2>&#x1F4B3; &#x12AA;&#x1235; &#x1266;&#x122D;&#x1233; &#x12A5;&#x1293; &#x12AD;&#x134D;&#x12EB;</h2>
  <div class="balance-display-box">
    <div>PLAY WALLET: <strong id="w-play">0.00</strong> ETB</div>
    <div>WIN WALLET: <strong id="w-win">0.00</strong> ETB</div>
  </div>
  <div class="wallet-buttons-row">
    <button id="show-deposit" class="primary-btn">&#x1F4E5; DEPOSIT</button>
    <button id="show-withdraw" class="success-btn">&#x1F4E4; WITHDRAW</button>
  </div>
  <div id="deposit-form" class="sub-form">
    <h3>&#x1308;&#x1295;&#x12D8;&#x1265; &#x121B;&#x1235;&#x1308;&#x1263;&#x1275;</h3>
    <select id="deposit-method"><option>Telebirr</option><option>CBE Birr</option></select>
    <input id="deposit-amount" type="number" placeholder="&#x12E8;&#x1308;&#x1295;&#x12D8;&#x1265; &#x1218;&#x1320;&#x1295; (&#x1265;&#x122D;)">
    <input id="deposit-reference" placeholder="Transaction ID">
    <button id="send-deposit" class="primary-btn">&#x1325;&#x12EB;&#x1244; &#x120B;&#x12AD;</button>
  </div>
  <div id="withdraw-form" class="sub-form" style="display:none;">
    <h3>&#x1308;&#x1295;&#x12D8;&#x1265; &#x121B;&#x12CD;&#x1323;&#x1275;</h3>
    <input id="withdraw-account" placeholder="&#x12E8;&#x12A0;&#x12AB;&#x12CD;&#x1295;&#x1275; &#x1241;&#x1325;&#x122D; &#x12C8;&#x12ED;&#x121D; &#x1235;&#x120D;&#x12AD; &#x12A5;&#x1293; &#x1235;&#x121D;">
    <input id="withdraw-amount" type="number" placeholder="&#x1218;&#x1320;&#x1295; (&#x1265;&#x122D;)">
    <button id="send-withdraw" class="success-btn">&#x12E8;&#x121B;&#x12CD;&#x1323;&#x1275; &#x1325;&#x12EB;&#x1244; &#x120B;&#x12AD;</button>
  </div>
</div>

<div class="rank-section" id="rank-section" style="display:none;">
  <h2>&#x1F3C6; &#x12A8;&#x134D;&#x1270;&#x129B; &#x1270;&#x132B;&#x12CB;&#x127E;&#x127D;</h2>
  <div id="rank-list">&#x1260;&#x1218;&#x132B;&#x1295; &#x120B;&#x12ED;...</div>
</div>

<nav class="bottom-nav">
  <button class="nav-btn active" data-tab="bingo" onclick="switchTab('bingo')">&#x1F3AF; BINGO</button>
  <button class="nav-btn" data-tab="wallet" onclick="switchTab('wallet')">&#x1F4B3; WALLET</button>
  <button class="nav-btn" data-tab="rank" onclick="switchTab('rank')">&#x1F3C6; RANK</button>
</nav>
</main>
<div id="win-overlay" class="win-overlay" style="display:none"><div class="win-box"><div class="win-title">&#x1F389; BINGO! &#x1F389;</div><div id="win-names" class="win-line"></div><div id="win-prize" class="win-line"></div><div id="win-next" class="win-sub"></div></div></div>
<div id="toast"></div><script src="/static/app.js"></script></body></html>"""

ADMIN_HTML = r"""<!doctype html><html lang="am"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>&#x1260;&#x123D; &#x1260;&#x123D; &#x1262;&#x1295;&#x130E; Admin</title><script src="https://telegram.org/js/telegram-web-app.js"></script><link rel="stylesheet" href="/static/style.css"><style>.admin{max-width:700px;margin:auto;padding:16px}.req{background:#0b2113;border:1px solid #20552d;padding:12px;border-radius:10px;margin-bottom:10px}.req span{display:block;font-size:11px;color:#a3b8a8}</style></head>
<body class="admin"><header><h1>&#x1F6E1; &#x1260;&#x123D; &#x1260;&#x123D; &#x1262;&#x1295;&#x130E; &#x12E8;&#x12A0;&#x1235;&#x1270;&#x12F3;&#x12F3;&#x122A; &#x1353;&#x1290;&#x120D;</h1><button id="refresh" class="primary-btn">&#x27F3; &#x12A0;&#x12F5;&#x1235;</button></header>
<section style="display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin:15px 0">
  <div class="req"><span>&#x1270;&#x132B;&#x12CB;&#x127E;&#x127D;</span><strong id="u-count">&#x2014;</strong></div>
  <div class="req"><span>&#x1320;&#x1245;&#x120B;&#x120B; &#x1265;&#x122D;</span><strong id="t-cred">&#x2014;</strong></div>
  <div class="req"><span>&#x1260;&#x1218;&#x1320;&#x1263;&#x1260;&#x1245; &#x120B;&#x12ED;</span><strong id="p-count">&#x2014;</strong></div>
</section>
<h2>&#x12E8;&#x1308;&#x1295;&#x12D8;&#x1265; &#x121B;&#x1235;&#x1308;&#x1262;&#x12EB; &#x1325;&#x12EB;&#x1244;&#x12CE;&#x127D;</h2><div id="req-list">&#x1260;&#x1218;&#x132B;&#x1295; &#x120B;&#x12ED;...</div><h2>&#x12E8;&#x121B;&#x12CD;&#x1323;&#x1275; &#x1325;&#x12EB;&#x1244;&#x12CE;&#x127D;</h2><div id="wd-list">&#x1260;&#x1218;&#x132B;&#x1295; &#x120B;&#x12ED;...</div><script src="/static/admin.js"></script></body></html>"""

STYLE_CSS = r""":root{color-scheme:dark;--bg:#041509;--panel:#0b2113;--panel2:#102d19;--line:#20552d;--text:#f4f7f4;--muted:#a3b8a8;--gold:#ffe629;--teal:#39d66f}
*{box-sizing:border-box}body{margin:0;background:var(--bg);font-family:system-ui,"Noto Sans Ethiopic","Nyala","Abyssinica SIL",sans-serif;color:var(--text);padding-bottom:70px}
.app-shell{max-width:520px;margin:auto;padding:12px}
.topbar{display:flex;justify-content:space-between;align-items:center;background:var(--panel);border:1px solid var(--line);padding:10px 14px;border-radius:14px;margin-bottom:12px}
.eyebrow{font-size:10px;color:var(--teal);font-weight:800}.topbar h1{font-size:16px;margin:0}
.top-scores{display:flex;gap:6px;align-items:center}
.score-box{background:var(--panel2);border:1px solid var(--line);padding:4px 8px;border-radius:8px;font-size:11px;text-align:center}
.score-box.win{border-color:var(--gold)}.score-box strong{display:block;color:var(--gold);font-size:13px}
.current-call-badge{width:40px;height:40px;background:linear-gradient(135deg,#39d66f,#0b6329);border-radius:10px;display:grid;place-items:center;font-weight:900;font-size:16px;color:#041509}
.phase-banner{background:#183820;border:1px solid var(--teal);padding:8px;border-radius:8px;text-align:center;font-size:12px;margin-bottom:10px;font-weight:bold;color:var(--gold)}
.win-overlay{position:fixed;inset:0;background:rgba(0,0,0,.82);display:flex;align-items:center;justify-content:center;z-index:2000;padding:20px}
.win-box{background:linear-gradient(160deg,#0b6329,#041509);border:3px solid var(--gold);border-radius:20px;padding:26px 20px;text-align:center;max-width:380px;width:100%;box-shadow:0 0 40px rgba(255,230,41,.45);animation:winpulse 1s ease-in-out infinite alternate}
.win-title{font-size:34px;font-weight:900;color:var(--gold);margin-bottom:14px}
.win-line{font-size:18px;font-weight:800;margin:6px 0;word-break:break-word;white-space:pre-line}
.win-sub{font-size:12px;color:var(--muted);margin-top:12px}
@keyframes winpulse{from{transform:scale(1)}to{transform:scale(1.04)}}
.phase-banner.win{background:#483308;border-color:var(--gold);font-size:13px}
.sound-btn{background:var(--panel2);border:1px solid var(--line);border-radius:8px;width:34px;height:34px;font-size:16px;cursor:pointer;color:var(--text)}
.game-panel,.wallet-section,.rank-section{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:12px;margin-bottom:12px}
.section-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;font-size:13px}
.section-header h2{font-size:13px;margin:0}
.card-count-pill{background:var(--panel2);padding:4px 8px;border-radius:6px;border:1px solid var(--line);font-size:11px;color:var(--teal)}
.master-grid{display:grid;grid-template-columns:repeat(8,1fr);gap:6px;max-height:48vh;overflow-y:auto;-webkit-overflow-scrolling:touch;background:#06180d;padding:8px;border-radius:10px;border:1px solid var(--line)}
.master-grid-75{display:grid;grid-template-columns:22px repeat(15,1fr);gap:2px;background:#06180d;padding:6px;border-radius:10px;border:1px solid var(--line)}
.m-letter{display:grid;place-items:center;font-weight:900;color:var(--gold);font-size:12px}
.picker-btn{aspect-ratio:1;min-height:36px;background:var(--panel2);border:1px solid var(--line);border-radius:8px;font-size:14px;display:grid;place-items:center;font-weight:800;color:var(--text);cursor:pointer;padding:0;touch-action:manipulation;-webkit-tap-highlight-color:transparent}
.picker-btn.taken{background:#331010;color:#777;border-color:#551111;cursor:not-allowed}
.picker-btn.mine{background:var(--teal);color:#041509;font-weight:900}
.m-cell{aspect-ratio:1;background:var(--panel2);border-radius:3px;font-size:8px;display:grid;place-items:center;font-weight:700;color:var(--muted)}
.m-cell.called{background:var(--gold);color:#041509;font-weight:900}
.called-balls-row{display:flex;gap:4px;overflow-x:auto;margin-top:8px;padding:4px;background:#06180d;border-radius:8px;border:1px solid var(--line)}
.mini-ball{min-width:24px;height:24px;background:var(--gold);color:#041509;border-radius:50%;display:grid;place-items:center;font-size:10px;font-weight:900}
.cards-section{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:6px;margin-bottom:12px}
.bingo-card{background:var(--panel2);border:1px solid var(--line);border-radius:10px;padding:6px;min-width:0}
.card-title{font-size:10px;font-weight:bold;margin-bottom:4px;color:var(--gold);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.card-grid{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:2px}
.c-cell{aspect-ratio:1;min-width:0;background:#06180d;border:1px solid var(--line);border-radius:4px;font-size:clamp(9px,3.2vw,13px);font-weight:800;display:grid;place-items:center;cursor:pointer;color:var(--text);padding:0}
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
const tg = window.Telegram?.WebApp; if (tg) { tg.ready(); tg.expand(); }
const initData = tg?.initData || '';
let model = null, lastSpokenNum = null, tab = 'bingo', clockOffset = 0, announcedRound = null, soundOn = true;
const LETTERS = ['B', 'I', 'N', 'G', 'O'];

function toast(s) {
  const e = document.getElementById('toast');
  e.textContent = s; e.style.display = 'block';
  setTimeout(() => e.style.display = 'none', 2500);
}

let actx = null;
function getCtx() {
  try { actx = actx || new (window.AudioContext || window.webkitAudioContext)(); return actx; }
  catch (e) { return null; }
}
function beep(freq = 880, dur = 0.15, vol = 0.2) {
  if (!soundOn) return;
  const c = getCtx(); if (!c) return;
  try {
    if (c.state === 'suspended') c.resume();
    const o = c.createOscillator(), g = c.createGain();
    o.type = 'sine'; o.frequency.value = freq;
    o.connect(g); g.connect(c.destination);
    const t = c.currentTime;
    g.gain.setValueAtTime(vol, t);
    g.gain.exponentialRampToValueAtTime(0.001, t + dur);
    o.start(t); o.stop(t + dur);
  } catch (e) {}
}
function speak(text) {
  if (!soundOn) return;
  try {
    if ('speechSynthesis' in window) {
      const u = new SpeechSynthesisUtterance(text);
      u.lang = 'en-US'; u.rate = 0.9; u.volume = 1;
      const v = window.speechSynthesis.getVoices().find(x => x.lang && x.lang.toLowerCase().startsWith('en'));
      if (v) u.voice = v;
      window.speechSynthesis.speak(u);
    }
  } catch (e) {}
}
// Browsers / Telegram only allow sound after the first touch: unlock audio then.
function unlockAudio() {
  const c = getCtx(); if (c && c.state === 'suspended') c.resume();
  try {
    if ('speechSynthesis' in window) {
      const u = new SpeechSynthesisUtterance(' '); u.volume = 0;
      window.speechSynthesis.speak(u);
    }
  } catch (e) {}
}
['pointerdown', 'touchstart', 'click'].forEach(ev =>
  document.addEventListener(ev, unlockAudio, { once: true, passive: true }));

function speakNumber(n) {
  if (!n || n === lastSpokenNum) return;
  lastSpokenNum = n;
  const letter = LETTERS[Math.min(4, Math.floor((n - 1) / 15))];
  beep(880, 0.12, 0.2);                 // short "ding" so there is always a sound
  speak(letter + ', ' + n);
}
function announceBingo() {
  [523, 659, 784, 1046].forEach((f, i) => setTimeout(() => beep(f, 0.18, 0.25), i * 150));
  setTimeout(() => speak('Bingo! Bingo!'), 700);
}

async function api(path, method = 'GET', body = null) {
  const o = { method, headers: { 'Content-Type': 'application/json', 'X-Telegram-Init-Data': initData } };
  if (body) o.body = JSON.stringify(body);
  const r = await fetch(path, o);
  const d = await r.json().catch(() => ({ error: 'Error' }));
  if (!r.ok) throw Error(d.error || 'Failed');
  return d;
}

function render() {
  if (!model) return;
  const isBingo = tab === 'bingo';
  const isSelecting = (model.phase === 'selecting');

  document.getElementById('round-title').textContent = '\u12D9\u122D #' + model.round;
  document.getElementById('play-bal').textContent = model.credits.toFixed(2);
  document.getElementById('win-bal').textContent = (model.win_credits || 0).toFixed(2);
  document.getElementById('w-play').textContent = model.credits.toFixed(2);
  document.getElementById('w-win').textContent = (model.win_credits || 0).toFixed(2);
  document.getElementById('current').textContent = model.current_number ?? '\u2014';

  if (model.current_number) speakNumber(model.current_number);
  else lastSpokenNum = null;

  document.getElementById('selection-panel').style.display = (isBingo && isSelecting) ? 'block' : 'none';
  document.getElementById('live-game-panel').style.display = (isBingo && !isSelecting) ? 'block' : 'none';
  document.getElementById('cards-container').style.display = isBingo ? 'grid' : 'none';
  document.getElementById('game-actions').style.display = (isBingo && model.phase === 'playing') ? 'grid' : 'none';
  document.getElementById('wallet-section').style.display = tab === 'wallet' ? 'block' : 'none';
  document.getElementById('rank-section').style.display = tab === 'rank' ? 'block' : 'none';
  const nowS = Date.now() / 1000 + clockOffset;
  const banner = document.getElementById('phase-banner');
  document.getElementById('win-overlay').style.display = (model.phase === 'finished') ? 'flex' : 'none';
  if (model.phase === 'selecting') {
    const rem = Math.max(0, Math.floor(model.selection_ends_at - nowS));
    banner.className = 'phase-banner';
    banner.textContent = '\u23F1 \u12AB\u122D\u12F5 \u12ED\u121D\u1228\u1321 (1-600) \u2014 ' + rem + ' \u1230\u12A8\u1295\u12F5 \u1240\u122D\u1277\u120D';
    banner.style.display = isBingo ? 'block' : 'none';
  } else if (model.phase === 'finished') {
    const rem = Math.max(0, Math.floor(model.finished_ends_at - nowS));
    const w = model.winners || [];
    banner.className = 'phase-banner win';
    banner.textContent = '\uD83C\uDFC6 BINGO! ' + w.map(x => x.name + ' (\u12AB\u122D\u12F5 #' + x.card_id + ')').join(', ') +
      ' \u2014 ' + (w[0] ? w[0].prize : 0) + ' ETB \u00B7 \u12A0\u12F2\u1235 \u12D9\u122D \u1260 ' + rem + ' \u1230';
    banner.style.display = isBingo ? 'block' : 'none';
    if (announcedRound !== model.round) { announcedRound = model.round; announceBingo(); }
    document.getElementById('win-names').textContent = w.map(x => x.name + ' \u2014 \u12AB\u122D\u12F5 #' + x.card_id).join('\n');
    document.getElementById('win-prize').textContent = '\u123D\u120D\u121B\u1275: ' + (w[0] ? w[0].prize : 0) + ' \u1265\u122D' + (w.length > 1 ? ' (\u1208\u12A5\u12EB\u1295\u12F3\u1295\u12F3\u1278\u12CD)' : '');
    document.getElementById('win-next').textContent = '\u12A0\u12F2\u1235 \u12D9\u122D \u1260 ' + rem + ' \u1230\u12A8\u1295\u12F5 \u12CD\u1235\u1325 \u12ED\u1300\u121D\u122B\u120D';
  } else {
    banner.style.display = 'none';
  }
  document.getElementById('my-chosen-count').textContent = '\u12E8\u1270\u1218\u1228\u1321 \u12AB\u122D\u12F6\u127D: ' + model.my_cards.length + '/4';
  document.getElementById('card-count').textContent = '\u12AB\u122D\u12F6\u127D: ' + model.my_cards.length;

  // 1..600 card picker
  const picker = document.getElementById('number-picker-grid');
  if (picker.children.length === 0) {
    for (let i = 1; i <= 600; i++) {
      const btn = document.createElement('button');
      btn.className = 'picker-btn'; btn.textContent = i;
      btn.onclick = () => action('/api/miniapp/select-card', { card_number: i });
      picker.appendChild(btn);
    }
  }
  if (isSelecting) {
    const takenSet = new Set(model.taken_cards || []);
    const mySet = new Set(model.my_cards || []);
    Array.from(picker.children).forEach(btn => {
      const n = Number(btn.textContent);
      btn.className = 'picker-btn';
      if (mySet.has(n)) btn.classList.add('mine');
      else if (takenSet.has(n)) btn.classList.add('taken');
    });
  }

  // 1..75 master grid (rows B-I-N-G-O, 15 numbers each)
  const calledSet = new Set(model.called_numbers);
  const mg75 = document.getElementById('master-grid-75');
  if (mg75.children.length === 0) {
    for (let r = 0; r < 5; r++) {
      const l = document.createElement('div'); l.className = 'm-letter'; l.textContent = LETTERS[r];
      mg75.appendChild(l);
      for (let c = 1; c <= 15; c++) {
        const n = r * 15 + c;
        const d = document.createElement('div');
        d.className = 'm-cell'; d.id = 'm75-' + n; d.textContent = n;
        mg75.appendChild(d);
      }
    }
  }
  for (let i = 1; i <= 75; i++) {
    const el = document.getElementById('m75-' + i);
    if (el) el.className = 'm-cell' + (calledSet.has(i) ? ' called' : '');
  }

  const ballsRow = document.getElementById('called-balls-row');
  ballsRow.innerHTML = '';
  model.called_numbers.slice().reverse().forEach(n => {
    const b = document.createElement('div'); b.className = 'mini-ball'; b.textContent = n;
    ballsRow.appendChild(b);
  });

  // my cards
  const cc = document.getElementById('cards-container'); cc.innerHTML = '';
  model.my_cards_data.forEach(item => {
    const cid = item.card_id;
    const marked = new Set(item.marked || ['FREE']);
    const box = document.createElement('div'); box.className = 'bingo-card';
    const title = document.createElement('div'); title.className = 'card-title';
    title.textContent = '\u12AB\u122D\u12F5 #' + cid + ' (1-75)';
    box.appendChild(title);
    const grid = document.createElement('div'); grid.className = 'card-grid';
    item.matrix.forEach(row => row.forEach(n => {
      const btn = document.createElement('button'); btn.className = 'c-cell';
      if (n === 'FREE') { btn.classList.add('free'); btn.textContent = '\u2605'; }
      else {
        btn.textContent = n;
        if (calledSet.has(n)) btn.classList.add('called');
        if (marked.has(n)) btn.classList.add('marked');
        btn.onclick = () => action('/api/miniapp/mark', { card_id: cid, number: n });
      }
      grid.appendChild(btn);
    }));
    box.appendChild(grid); cc.appendChild(box);
  });
}

async function refresh() {
  try {
    model = await api('/api/miniapp/game');
    clockOffset = model.server_time - Date.now() / 1000;
    render();
  } catch (e) {}
}

async function action(path, body = {}) {
  try {
    const d = await api(path, 'POST', body);
    toast(d.message || '\u1270\u1233\u12AB');
    await refresh();
    return d;
  } catch (e) { toast(e.message); }
}

window.switchTab = function (t) {
  tab = t;
  document.querySelectorAll('.nav-btn').forEach(b => b.classList.toggle('active', b.dataset.tab === t));
  render();
  if (t === 'rank') loadRank();
};

async function loadRank() {
  try {
    const d = await api('/api/miniapp/rank');
    const list = document.getElementById('rank-list'); list.innerHTML = '';
    d.rows.forEach((r, i) => {
      const div = document.createElement('div');
      div.style.cssText = 'padding:6px;border-bottom:1px solid #20552d';
      div.append((i + 1) + '. ' + r.name + ' \u2014 ');
      const b = document.createElement('b'); b.textContent = r.credits + ' ETB';
      div.appendChild(b); list.appendChild(div);
    });
  } catch (e) { toast(e.message); }
}

document.getElementById('claim').onclick = () => action('/api/miniapp/claim');
document.getElementById('sound-btn').onclick = () => {
  soundOn = !soundOn;
  document.getElementById('sound-btn').textContent = soundOn ? '\uD83D\uDD0A' : '\uD83D\uDD07';
  if (!soundOn) { try { window.speechSynthesis.cancel(); } catch (e) {} } else { unlockAudio(); beep(660, 0.15); }
};
document.getElementById('show-deposit').onclick = () => {
  document.getElementById('deposit-form').style.display = 'block';
  document.getElementById('withdraw-form').style.display = 'none';
};
document.getElementById('show-withdraw').onclick = () => {
  document.getElementById('withdraw-form').style.display = 'block';
  document.getElementById('deposit-form').style.display = 'none';
};
document.getElementById('send-deposit').onclick = () => {
  const amount = Number(document.getElementById('deposit-amount').value);
  const method = document.getElementById('deposit-method').value;
  const reference = document.getElementById('deposit-reference').value.trim();
  if (!amount || reference.length < 3) { toast('\u12A5\u1263\u12AD\u12CE \u1218\u1228\u1303\u12CD\u1295 \u12ED\u1219\u1209'); return; }
  action('/api/miniapp/deposit-request', { amount, method, reference })
    .then(() => { document.getElementById('deposit-reference').value = ''; });
};
document.getElementById('send-withdraw').onclick = () => {
  const amount = Number(document.getElementById('withdraw-amount').value);
  const account_info = document.getElementById('withdraw-account').value.trim();
  if (!amount || account_info.length < 3) { toast('\u12A5\u1263\u12AD\u12CE \u1218\u1228\u1303\u12CD\u1295 \u12ED\u1219\u1209'); return; }
  action('/api/miniapp/withdraw-request', { amount, account_info }).then(() => {
    document.getElementById('withdraw-amount').value = '';
    document.getElementById('withdraw-account').value = '';
  });
};

refresh(); setInterval(refresh, 1000);
})();"""

ADMIN_JS = r"""const HEADERS = () => ({'Content-Type': 'application/json', 'X-Telegram-Init-Data': window.Telegram?.WebApp?.initData || ''});
function esc(s) { const d = document.createElement('div'); d.textContent = String(s ?? ''); return d.innerHTML; }
const BTN = 'border:0;padding:6px 12px;border-radius:6px;font-weight:bold;margin-right:6px;cursor:pointer;';

async function load() {
  const r = await fetch('/api/admin/overview', {headers: HEADERS()});
  const d = await r.json();
  if (!r.ok) { document.getElementById('req-list').textContent = d.error || 'Error'; return; }
  document.getElementById('u-count').textContent = d.users_count;
  document.getElementById('t-cred').textContent = d.total_credits;
  document.getElementById('p-count').textContent = d.pending_count;
  document.getElementById('req-list').innerHTML = d.requests.map(q => {
    const buttons = q.status === 'pending'
      ? `<button onclick="review(${Number(q.id)},'approve')" style="${BTN}background:#39d66f">\u12A0\u133D\u12F5\u1245</button>` +
        `<button onclick="review(${Number(q.id)},'reject')" style="${BTN}background:#d63939;color:#fff">\u12CD\u12F5\u1245</button>`
      : `<i>${esc(q.status)}</i>`;
    return `<div class="req"><b>#${Number(q.id)} \u00B7 ${esc(q.amount)} \u1265\u122D (${esc(q.method)})</b>
      <p>User: ${esc(q.user_id)} | Ref: ${esc(q.reference)}</p>${buttons}</div>`;
  }).join('') || '\u2014';
  document.getElementById('wd-list').innerHTML = (d.withdrawals || []).map(q => {
    const buttons = q.status === 'pending'
      ? `<button onclick="reviewWd(${Number(q.id)},'paid')" style="${BTN}background:#39d66f">\u1270\u12A8\u134D\u120F\u120D</button>` +
        `<button onclick="reviewWd(${Number(q.id)},'reject')" style="${BTN}background:#d63939;color:#fff">\u12CD\u12F5\u1245 (\u1265\u1229\u1295 \u1218\u120D\u1235)</button>`
      : `<i>${esc(q.status)}</i>`;
    return `<div class="req"><b>#${Number(q.id)} \u00B7 ${esc(q.amount)} \u1265\u122D</b>
      <p>User: ${esc(q.user_id)} | ${esc(q.account_info)}</p>${buttons}</div>`;
  }).join('') || '\u2014';
}
async function review(id, decision) {
  await fetch(`/api/admin/deposit/${id}/review`, {method: 'POST', headers: HEADERS(), body: JSON.stringify({decision})});
  load();
}
async function reviewWd(id, decision) {
  await fetch(`/api/admin/withdraw/${id}/review`, {method: 'POST', headers: HEADERS(), body: JSON.stringify({decision})});
  load();
}
document.getElementById('refresh').onclick = load; load();
"""


# ------------------------------ Flask API Routes -------------------------
@app.after_request
def no_cache(resp):
    # Telegram's WebView caches aggressively: always fetch the newest page/JS/CSS.
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    return resp


@app.route("/static/style.css")
def serve_css():
    return Response(STYLE_CSS, content_type="text/css; charset=utf-8")


@app.route("/static/app.js")
def serve_app_js():
    return Response(APP_JS, content_type="application/javascript; charset=utf-8")


@app.route("/static/admin.js")
def serve_admin_js():
    return Response(ADMIN_JS, content_type="application/javascript; charset=utf-8")


@app.route("/", methods=["GET"])
def index():
    return Response(INDEX_HTML, content_type="text/html; charset=utf-8")


@app.route("/admin", methods=["GET"])
def admin_panel():
    return Response(ADMIN_HTML, content_type="text/html; charset=utf-8")


@app.get("/api/miniapp/game")
def api_miniapp_game():
    user = api_user()
    if not user:
        return json_error("Unauthorized", 401)
    uid = user["id"]
    row = get_user(uid)
    with game_lock:
        my_cards = list(game_state["player_cards"].get(uid, []))
        my_cards_data = []
        for cid in my_cards:
            mat = game_state["card_matrices"].get((uid, cid))
            if mat is None:
                mat = generate_75_bingo_card(cid * 999 + uid)
                game_state["card_matrices"][(uid, cid)] = mat
            mset = list(game_state["marked"].setdefault((uid, cid), {"FREE"}))
            my_cards_data.append({"card_id": cid, "matrix": mat, "marked": mset})

        return jsonify({
            "round": game_state["round"],
            "phase": game_state["phase"],
            "selection_ends_at": game_state["selection_ends_at"],
            "server_time": time.time(),
            "prize": game_state["prize"],
            "called_numbers": list(game_state["called_numbers"]),
            "current_number": game_state["current_number"],
            "credits": row["credits"] if row else 100.0,
            "win_credits": row["win_credits"] if row else 0.0,
            "taken_cards": list(game_state["taken_card_numbers"]),
            "my_cards": my_cards,
            "my_cards_data": my_cards_data,
            "finished_ends_at": game_state["finished_ends_at"],
            "winners": [{"name": w["name"], "card_id": w["card_id"], "prize": w["prize"]}
                        for w in game_state["last_winners"]],
        })


@app.post("/api/miniapp/select-card")
def api_miniapp_select_card():
    user = api_user()
    if not user:
        return json_error("Unauthorized", 401)
    uid = user["id"]
    body = request.get_json(silent=True) or {}
    try:
        card_num = int(body.get("card_number"))
    except (TypeError, ValueError):
        return json_error("\u120d\u12ad \u12eb\u120d\u1206\u1290 \u12e8\u12ab\u122d\u12f5 \u1241\u1325\u122d")
    if card_num < 1 or card_num > TOTAL_CARDS:
        return json_error("\u120d\u12ad \u12eb\u120d\u1206\u1290 \u12e8\u12ab\u122d\u12f5 \u1241\u1325\u122d")

    with game_lock:
        if game_state["phase"] != "selecting":
            return json_error("\u12e8\u12ab\u122d\u12f5 \u1218\u121d\u1228\u132b \u130a\u12dc \u12a0\u1265\u1245\u1277\u120d")

        my_cards = game_state["player_cards"].setdefault(uid, [])

        # Deselect (refund)
        if card_num in my_cards:
            my_cards.remove(card_num)
            game_state["taken_card_numbers"].discard(card_num)
            game_state["card_matrices"].pop((uid, card_num), None)
            game_state["marked"].pop((uid, card_num), None)
            game_state["prize"] -= CARD_COST
            refund_card(uid, game_state["card_paid_from"].pop((uid, card_num), "credits"))
            return jsonify({"message": "\u12ab\u122d\u12f1 \u1270\u1230\u122d\u12df\u120d"})

        if len(my_cards) >= MAX_CARDS_PER_PLAYER:
            return json_error("\u12a84 \u12ab\u122d\u12f5 \u1260\u120b\u12ed \u1218\u12eb\u12dd \u12a0\u12ed\u127d\u120d\u121d")
        if card_num in game_state["taken_card_numbers"]:
            return json_error("\u12ed\u1205 \u12ab\u122d\u12f5 \u1260\u120c\u120b \u1270\u132b\u12cb\u127d \u1270\u12ed\u12df\u120d")

        # Play wallet first; when it is empty the Win wallet pays for the card
        wallet = charge_card(uid)
        if wallet is None:
            return json_error("\u1260\u1242 \u1202\u1233\u1265 \u12e8\u1208\u12ce\u1275\u121d (10 \u1265\u122d \u12eb\u1235\u1348\u120d\u130b\u120d)")

        game_state["taken_card_numbers"].add(card_num)
        my_cards.append(card_num)
        game_state["prize"] += CARD_COST
        game_state["card_paid_from"][(uid, card_num)] = wallet
        game_state["card_matrices"][(uid, card_num)] = generate_75_bingo_card(card_num * 999 + uid)
        game_state["marked"][(uid, card_num)] = {"FREE"}

    return jsonify({"message": f"\u12ab\u122d\u12f5 #{card_num} \u1270\u12ed\u12df\u120d!"})


@app.post("/api/miniapp/mark")
def api_miniapp_mark():
    user = api_user()
    if not user:
        return json_error("Unauthorized", 401)
    uid = user["id"]
    body = request.get_json(silent=True) or {}
    cid, num = body.get("card_id"), body.get("number")
    with game_lock:
        if game_state["phase"] != "playing":
            return json_error("\u1328\u12cb\u1273\u12cd \u1308\u1293 \u12a0\u120d\u1300\u1218\u1228\u121d")
        if cid not in game_state["player_cards"].get(uid, []):
            return json_error("\u12ed\u1205 \u12ab\u122d\u12f5 \u12e8\u12a5\u122d\u1235\u12ce \u12a0\u12ed\u12f0\u1208\u121d")
        if num != "FREE" and num not in game_state["called_numbers"]:
            return json_error("\u1241\u1325\u1229 \u1308\u1293 \u12a0\u120d\u1270\u1320\u122b\u121d")
        mat = game_state["card_matrices"].get((uid, cid))
        if num != "FREE" and (not mat or not any(num in row for row in mat)):
            return json_error("\u1241\u1325\u1229 \u1260\u12ab\u122d\u12f1 \u120b\u12ed \u12e8\u1208\u121d")
        game_state["marked"].setdefault((uid, cid), {"FREE"}).add(num)
    return jsonify({"message": "\u121d\u120d\u12ad\u1275 \u1270\u12f0\u122d\u1313\u120d"})


@app.post("/api/miniapp/claim")
def api_miniapp_claim():
    user = api_user()
    if not user:
        return json_error("Unauthorized", 401)
    result = try_award(user["id"], user.get("username", ""))
    if not result:
        return json_error("\u1308\u1293 \u1262\u1295\u130e \u12a0\u120d\u121e\u120b\u121d!")
    return jsonify({"message": "\U0001f3c6 BINGO!"})


@app.post("/api/miniapp/deposit-request")
def api_miniapp_deposit():
    user = api_user()
    if not user:
        return json_error("Unauthorized", 401)
    body = request.get_json(silent=True) or {}
    amt = to_positive_float(body.get("amount"))
    method = str(body.get("method") or "")[:30]
    ref = str(body.get("reference") or "").strip()[:100]
    if not amt or not ref or method not in ("Telebirr", "CBE Birr"):
        return json_error("\u12a5\u1263\u12ad\u12ce \u1218\u1228\u1303\u12cd\u1295 \u12ed\u1219\u1209")
    with closing(db()) as con:
        cur = con.execute("INSERT INTO deposit_requests(user_id, method, amount, reference) VALUES(?,?,?,?)",
                          (user["id"], method, amt, ref))
        req_id = cur.lastrowid
        con.commit()
    notify_deposit(req_id, user["id"], method, amt, ref)
    return jsonify({"message": "\u12e8\u1308\u1295\u12d8\u1265 \u121b\u1235\u1308\u1262\u12eb \u1325\u12eb\u1244\u12ce \u1270\u120d\u12b3\u120d"})


@app.post("/api/miniapp/withdraw-request")
def api_miniapp_withdraw():
    user = api_user()
    if not user:
        return json_error("Unauthorized", 401)
    body = request.get_json(silent=True) or {}
    amt = to_positive_float(body.get("amount"))
    account_info = str(body.get("account_info") or "").strip()[:200]
    if not amt or not account_info:
        return json_error("\u12a5\u1263\u12ad\u12ce \u1218\u1228\u1303\u12cd\u1295 \u12ed\u1219\u1209")
    with game_lock:  # serialize balance check + deduction
        row = get_user(user["id"])
        if (row["win_credits"] if row else 0.0) < amt:
            return json_error("\u1260\u12a0\u1238\u1293\u134a\u1290\u1275 \u1202\u1233\u1265\u12ce (Win Wallet) \u12cd\u1235\u1325 \u1260\u1242 \u1308\u1295\u12d8\u1265 \u12e8\u1208\u121d")
        update_credits(user["id"], -amt, is_win=True)
        with closing(db()) as con:
            cur = con.execute("INSERT INTO withdraw_requests(user_id, account_info, amount) VALUES(?,?,?)",
                              (user["id"], account_info, amt))
            req_id = cur.lastrowid
            con.commit()
    notify_withdraw(req_id, user["id"], account_info, amt)
    return jsonify({"message": "\u12e8\u1308\u1295\u12d8\u1265 \u121b\u12cd\u132b \u1325\u12eb\u1244\u12ce \u1270\u120d\u12b3\u120d"})


@app.get("/api/miniapp/rank")
def miniapp_rank():
    with closing(db()) as con:
        rows = con.execute(
            "SELECT username, first_name, win_credits FROM users ORDER BY win_credits DESC LIMIT 10").fetchall()
        return jsonify({"rows": [
            {"name": ("@" + r["username"]) if r["username"] else (r["first_name"] or "Player"),
             "credits": r["win_credits"]} for r in rows]})


@app.get("/api/admin/overview")
def admin_overview():
    user = api_admin()
    if not user:
        return json_error("Admin access denied", 403)
    with closing(db()) as con:
        uc = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        tc = con.execute("SELECT COALESCE(SUM(credits+win_credits),0) FROM users").fetchone()[0]
        pc = con.execute("SELECT COUNT(*) FROM deposit_requests WHERE status='pending'").fetchone()[0]
        pw = con.execute("SELECT COUNT(*) FROM withdraw_requests WHERE status='pending'").fetchone()[0]
        reqs = con.execute("SELECT * FROM deposit_requests ORDER BY id DESC LIMIT 50").fetchall()
        wds = con.execute("SELECT * FROM withdraw_requests ORDER BY id DESC LIMIT 50").fetchall()
        return jsonify({"users_count": uc, "total_credits": tc, "pending_count": pc + pw,
                        "requests": [dict(r) for r in reqs], "withdrawals": [dict(r) for r in wds]})


@app.post("/api/admin/deposit/<int:req_id>/review")
def admin_review(req_id):
    user = api_admin()
    if not user:
        return json_error("Admin access denied", 403)
    body = request.get_json(silent=True) or {}
    decision = body.get("decision")
    if decision not in ("approve", "reject"):
        return json_error("Invalid decision")
    if review_deposit(req_id, decision, user["id"]) is None:
        return json_error("Invalid request")
    return jsonify({"ok": True, "message": "\u1270\u1228\u130b\u130d\u1327\u120d"})


@app.post("/api/admin/withdraw/<int:req_id>/review")
def admin_review_withdraw(req_id):
    user = api_admin()
    if not user:
        return json_error("Admin access denied", 403)
    body = request.get_json(silent=True) or {}
    decision = body.get("decision")
    if decision not in ("paid", "reject"):
        return json_error("Invalid decision")
    if review_withdraw(req_id, decision) is None:
        return json_error("Invalid request")
    return jsonify({"ok": True, "message": "\u1270\u1228\u130b\u130d\u1327\u120d"})


@app.post("/telegram-webhook" if not WEBHOOK_SECRET_PATH else "/telegram-webhook/" + WEBHOOK_SECRET_PATH)
def telegram_webhook():
    try:
        payload = request.get_json(force=True)
        update = Update.de_json(payload, bot_application.bot)
        if bot_loop.is_running():
            asyncio.run_coroutine_threadsafe(bot_application.process_update(update), bot_loop)
        return "OK"
    except Exception:
        logger.exception("webhook error")
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
    bot_thread = threading.Thread(target=run_bot_loop, daemon=True, name="bot-loop")
    bot_thread.start()

    async def initialize():
        await bot_application.initialize()
        await bot_application.start()
        try:
            commands = [
                BotCommand("start", "\u1266\u1271\u1295 \u1300\u121d\u122d / Start"),
                BotCommand("restart", "\u12a5\u1295\u12f0\u1308\u1293 \u1300\u121d\u122d (\u121c\u1291 \u12a0\u121d\u1323) / Restart"),
                BotCommand("language", "\u124b\u1295\u124b / Language"),
            ]
            await bot_application.bot.set_my_commands(commands)
            await bot_application.bot.set_chat_menu_button(menu_button=MenuButtonCommands())
        except Exception:
            logger.exception("could not set the bot commands / menu button")
        if PUBLIC_URL:
            wh = "/telegram-webhook" + (("/" + WEBHOOK_SECRET_PATH) if WEBHOOK_SECRET_PATH else "")
            await bot_application.bot.set_webhook(PUBLIC_URL + wh)

    asyncio.run_coroutine_threadsafe(initialize(), bot_loop)


start_bot()
threading.Thread(target=game_loop_worker, daemon=True, name="game-loop").start()
threading.Thread(target=auto_caller_loop, daemon=True, name="auto-caller").start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
