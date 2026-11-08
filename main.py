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
from urllib.parse import parse_qsl, quote
from contextlib import closing
from datetime import datetime, timedelta, timezone

from flask import Flask, request, Response, jsonify, send_from_directory
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
HOUSE_PERCENT = 15   # owner's share of every round's prize pool (winners share the other 85%)
MIN_WITHDRAW = 100   # smallest withdrawal a player may ask for (ETB)
MAX_PENDING_DEPOSITS = 5  # unreviewed deposit requests one player may have at the same time
MAX_CARDS_PER_PLAYER = 4
SELECTION_TIME_SECONDS = 50
CALL_INTERVAL_SECONDS = 3
MAX_NUMBERS_POOL = 75  # 1 to 75 numbers called in game
TOTAL_CARDS = 600
# ---- deposits (Telebirr only) ----
PAY_NUMBER = os.environ.get("PAY_NUMBER", "0928564065").strip()   # the owner's Telebirr number
PAY_NAME = os.environ.get("PAY_NAME", "Eineyew").strip()         # the name shown to players
PAY_TAIL = re.sub(r"\D", "", PAY_NUMBER)[-4:]
MIN_DEPOSIT = 20          # smallest deposit (ETB)
REFERRAL_REWARD = 10      # ETB the inviter gets when his invited NEW user makes a first approved deposit
# Secret for the phone's SMS-forwarding webhook (/api/sms-webhook). Without it the webhook is disabled.
SMS_SECRET = os.environ.get("SMS_SECRET", "").strip()
SMS_SENDER = os.environ.get("SMS_SENDER", "").strip()  # optional: only accept SMS from this sender name/number
RECEIPT_URL = "https://transactioninfo.ethiotelecom.et/receipt/"
FINISHED_SECONDS = 4  # how long the "BINGO!" result stays on screen
SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "beshbesh08").strip().lstrip("@")
VOICE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "voice")  # B1.mp3 ... O75.mp3

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
last_seen = {}  # user_id -> time of the last Mini App poll (for the 'online now' counter)


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
        con.executescript("""
        CREATE TABLE IF NOT EXISTS rounds (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            round_no INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'won',
            players INTEGER NOT NULL DEFAULT 0,
            cards INTEGER NOT NULL DEFAULT 0,
            pool REAL NOT NULL DEFAULT 0,
            house_cut REAL NOT NULL DEFAULT 0,
            payout REAL NOT NULL DEFAULT 0,
            winners TEXT NOT NULL DEFAULT '',
            called_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS round_players (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            round_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL DEFAULT '',
            cards INTEGER NOT NULL DEFAULT 0,
            stake REAL NOT NULL DEFAULT 0,
            prize REAL NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_round_players_round ON round_players(round_id);
        CREATE INDEX IF NOT EXISTS idx_deposit_reference ON deposit_requests(reference);
        """)
        ucols = {r["name"] for r in con.execute("PRAGMA table_info(users)").fetchall()}
        if "referred_by" not in ucols:
            con.execute("ALTER TABLE users ADD COLUMN referred_by INTEGER")
        if "referral_paid" not in ucols:
            con.execute("ALTER TABLE users ADD COLUMN referral_paid REAL NOT NULL DEFAULT 0")
        dcols = {r["name"] for r in con.execute("PRAGMA table_info(deposit_requests)").fetchall()}
        for col, ddl in (("bonus", "REAL NOT NULL DEFAULT 0"), ("credited", "REAL NOT NULL DEFAULT 0"),
                         ("approved_via", "TEXT NOT NULL DEFAULT ''")):
            if col not in dcols:
                con.execute("ALTER TABLE deposit_requests ADD COLUMN %s %s" % (col, ddl))
        fresh_rules = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='bonus_rules'").fetchone() is None
        con.executescript("""
        CREATE TABLE IF NOT EXISTS incoming_payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ref TEXT NOT NULL UNIQUE,
            amount REAL NOT NULL,
            sender TEXT NOT NULL DEFAULT '',
            paid_at TEXT NOT NULL,
            used_by_req INTEGER,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS bonus_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL DEFAULT '',
            days TEXT NOT NULL DEFAULT '',
            start_min INTEGER NOT NULL DEFAULT 0,
            end_min INTEGER NOT NULL DEFAULT 1440,
            percent REAL NOT NULL DEFAULT 0,
            cap REAL NOT NULL DEFAULT 0,
            active INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS promo_codes (
            code TEXT PRIMARY KEY,
            reward REAL NOT NULL DEFAULT 0,
            max_uses INTEGER NOT NULL DEFAULT 0,
            uses INTEGER NOT NULL DEFAULT 0,
            expires_at TEXT,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS promo_uses (
            code TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (code, user_id)
        );
        """)
        if fresh_rules:
            # Ethiopia time (UTC+3), standard clock. days: Monday=0 ... Sunday=6
            con.executemany(
                "INSERT INTO bonus_rules(name, days, start_min, end_min, percent, cap, active) VALUES(?,?,?,?,?,?,1)",
                [("\u12a5\u1201\u12f5 100%", "6", 900, 1290, 100, 200),
                 ("\u1301\u121b 50%", "4", 900, 1290, 50, 200),
                 ("\u1260\u1218\u12a8\u1275\u120d 10%", "", 0, 1440, 10, 0)])
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


def generate_75_bingo_card(card_num):
    """The grid depends ONLY on the card number (card #7 is the same for everybody, in every round).
    A hash-driven shuffle is used instead of random.sample so it stays identical on every Python version."""
    cols = []
    for c in range(5):
        pool = list(range(c * 15 + 1, c * 15 + 16))
        h = hashlib.sha256(("besh-besh-card:%d:%d" % (int(card_num), c)).encode()).digest()
        for i in range(14, 0, -1):
            j = int.from_bytes(h[(i - 1) * 2:(i - 1) * 2 + 2], "big") % (i + 1)
            pool[i], pool[j] = pool[j], pool[i]
        cols.append(pool[:5])
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
            record_round("refunded", 0.0, 0.0, [])
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


def record_round(status, house, payout, winners):
    """Save the finished round (money + who played + who won) to the history tables.
    Needs game_lock. Never raises: a history problem must not stop the game."""
    try:
        prize_by_uid = {}
        for w in winners:
            prize_by_uid[w["user_id"]] = prize_by_uid.get(w["user_id"], 0.0) + float(w["prize"])
        rows = []
        for uid, cards in game_state["player_cards"].items():
            if cards:
                rows.append((int(uid), display_name(uid), len(cards), len(cards) * CARD_COST,
                             prize_by_uid.get(uid, 0.0)))
        names = ", ".join("%s (#%s)" % (w["name"], w["card_id"]) for w in winners)
        with closing(db()) as con:
            cur = con.execute(
                "INSERT INTO rounds(round_no, status, players, cards, pool, house_cut, payout, winners, called_count) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (game_state["round"], status, len(rows), sum(r[2] for r in rows),
                 float(game_state["prize"]), float(house), float(payout), names,
                 len(game_state["called_numbers"])))
            rid = cur.lastrowid
            con.executemany(
                "INSERT INTO round_players(round_id, user_id, name, cards, stake, prize) VALUES(?,?,?,?,?,?)",
                [(rid,) + r for r in rows])
            con.commit()
    except Exception:
        logger.exception("could not save the round history")


def declare_winners(winners):
    """Pay the prize (split between winners) and stop the round. Needs game_lock."""
    # the owner keeps HOUSE_PERCENT of the pool; the rest is split between the winners
    # (integer cents, so no money is created or lost: left-over cents stay with the owner)
    pool_cents = int(round(float(game_state["prize"]) * 100))
    payout_cents = pool_cents * (100 - HOUSE_PERCENT) // 100
    share_cents = payout_cents // len(winners)
    share = share_cents / 100.0
    house = (pool_cents - share_cents * len(winners)) / 100.0
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
    record_round("won", house, share * len(winners), results)
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


TEXT["am"]["rules_msg"] = "\U0001f4dc \u1205\u130d\u1293 \u12f0\u1295\u1266\u127d\n\n1. \u1328\u12cb\u1273\u12cd \u1219\u1209 \u1260\u1219\u1209 \u12e8\u12d5\u12f5\u120d \u1290\u12cd\u1364 \u12a5\u12eb\u1295\u12f3\u1295\u12f1 \u12d9\u122d \u1208\u1265\u127b\u12cd \u1290\u12cd\u1362\n2. \u12e8\u12a0\u1295\u12f5 \u12ab\u122d\u12f5 \u12cb\u130b 10 \u1265\u122d \u1290\u12cd\u1362 \u12a81 \u12a5\u1235\u12a8 600 \u12ab\u122d\u12f5 \u1218\u121d\u1228\u1325 \u12ed\u127b\u120b\u120d (\u1262\u1260\u12db 4 \u12ab\u122d\u12f5)\u1364 \u12e8\u1218\u121d\u1228\u132b \u130a\u12dc 50 \u1230\u12a8\u1295\u12f5 \u1290\u12cd\u1362\n3. \u12a81 \u12a5\u1235\u12a8 75 \u12eb\u1209 \u1241\u1325\u122e\u127d \u12ed\u1320\u122b\u1209\u1362 \u12a0\u1295\u12f5 \u1228\u12f5\u134d\u1363 \u12a0\u1295\u12f5 \u12d3\u121d\u12f5\u1363 \u1230\u12eb\u134d \u12c8\u12ed\u121d \u12a0\u122b\u1271 \u1325\u130d \u1232\u121e\u120b \u1262\u1295\u130e \u1290\u12cd\u1362\n4. \u12eb\u1235\u1308\u1261\u1275 \u1265\u122d \u1270\u1218\u120b\u123d \u12a0\u12ed\u12f0\u1228\u130d\u121d\u1364 \u1208\u1328\u12cb\u1273 \u12ed\u12cd\u120b\u120d\u1362 \u12dd\u1245\u1270\u129b\u12cd \u1308\u1295\u12d8\u1265 \u121b\u1235\u1308\u1262\u12eb 20 \u1265\u122d \u1290\u12cd\u1362\n5. \u121b\u12cd\u1323\u1275 \u12e8\u121a\u127b\u1208\u12cd \u12a8\u12a0\u1238\u1293\u134a\u1290\u1275 (Win) \u1202\u1233\u1265 \u1265\u127b \u1290\u12cd\u1364 \u12dd\u1245\u1270\u129b\u12cd \u12e8\u121b\u12cd\u132b \u1218\u1320\u1295 100 \u1265\u122d \u1290\u12cd\u1362\n6. \u12a821 \u12d3\u1218\u1275 \u1260\u1273\u127d \u1208\u1206\u1291 \u12ad\u120d\u12ad\u120d \u1290\u12cd\u1362\n7. \u1328\u12cb\u1273\u12cd \u1208\u1218\u12dd\u1293\u129b \u1265\u127b \u1290\u12cd\u1364 \u12aa\u1235\u12ce\u1295 \u1260\u121b\u12ed\u130e\u12f3 \u1218\u1320\u1295 \u1265\u127b \u12ed\u132b\u12c8\u1271\u1362"
TEXT["en"]["rules_msg"] = "\U0001f4dc Rules\n\n1. The game is purely a game of chance; every round stands on its own.\n2. A card costs 10 ETB. Pick cards from 1 to 600 (up to 4 cards); you have 50 seconds to choose.\n3. Numbers from 1 to 75 are called. A full row, column, diagonal or the four corners wins BINGO.\n4. Money you deposit is not refundable; it is used for playing. The minimum deposit is 20 ETB.\n5. You can withdraw only from the Win wallet; the minimum withdrawal is 100 ETB.\n6. Not allowed for anyone under 21 years old.\n7. For entertainment only: play only with an amount that does not hurt your pocket."


TEXT["am"].update({"promo": "\U0001f381 \u1355\u122e\u121e \u12ae\u12f5", "invite": "\U0001f517 \u130b\u1265\u12dd \u12a5\u1293 \u12a0\u130d\u129d"})
TEXT["en"].update({"promo": "\U0001f381 Promo code", "invite": "\U0001f517 Invite & earn"})


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
        [KeyboardButton(t["promo"]), KeyboardButton(t["invite"])],
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
    existed = get_user(user.id) is not None
    ensure_user(user.id, user.username, user.first_name)
    args = getattr(context, "args", None) or []
    if not existed and args and str(args[0]).startswith("ref_"):
        set_referrer(user.id, str(args[0])[4:])   # only a brand-new account can be invited
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


MENU_KEYS = ("play", "balance", "rules", "wallet", "rank", "language", "support", "promo", "invite")
LEGACY_MENU = ("\U0001f3ae Flash Bingo \u1328\u12cb\u1273", "\U0001f3ae Play Flash Bingo", "\U0001f4b0 \u1265\u122d \u1202\u1233\u1265", "\U0001f4dc \u1205\u130d \u12a5\u1293 \u12f0\u1295\u1266\u127d",
               "\U0001f4b3 Wallet / \u12aa\u1235 \u1266\u122d\u1233", "\U0001f3c6 \u12f0\u1228\u1303", "\U0001f310 \u124b\u1295\u124b / Language", "\U0001f198 \u12f5\u130b\u134d")


def is_menu_text(msg):
    return msg in LEGACY_MENU or any(msg == lg.get(k) for lg in TEXT.values() for k in MENU_KEYS)


def parse_amount(msg):
    if not re.fullmatch(r"\s*[\d,]+(?:\.\d+)?\s*(?:\u1265\u122d|birr|etb)?\s*", msg or "", re.I):
        return None
    try:
        v = float(re.sub(r"[^\d.]", "", msg.replace(",", "")))
    except ValueError:
        return None
    return v if v > 0 else None


def cancel_markup(uid):
    return InlineKeyboardMarkup([[InlineKeyboardButton(L(uid, "\u274c \u1230\u122d\u12dd", "\u274c Cancel"), callback_data="w:cancel")]])


def deposit_instructions(uid, amt):
    bonus, pct = bonus_preview(amt)
    lines = [L(uid, "\U0001f4e5 \u1308\u1295\u12d8\u1265 \u121b\u1235\u1308\u1263\u1275 (Telebirr)", "\U0001f4e5 Deposit (Telebirr)"), "",
             "\U0001f4de " + L(uid, "\u1241\u1325\u122d", "Number") + ": " + PAY_NUMBER,
             "\U0001f464 " + L(uid, "\u1235\u121d", "Name") + ": " + PAY_NAME,
             "\U0001f4b0 " + L(uid, "\u1218\u1320\u1295", "Amount") + ": " + money(amt) + " ETB"]
    if bonus > 0:
        lines.append(L(uid, "\U0001f381 \u1266\u1290\u1235: +%s \u1265\u122d (%s%%) \u2014 \u12ad\u134d\u12eb\u12cd \u1260\u1270\u1228\u130b\u1308\u1320\u1260\u1275 \u1230\u12d3\u1275 \u12ed\u12c8\u1230\u1293\u120d" % (money(bonus), money(pct)),
                       "\U0001f381 Bonus: +%s ETB (%s%%) \u2014 decided by the payment time" % (money(bonus), money(pct))))
    lines += ["", L(uid,
                     "1\ufe0f\u20e3 \u12a8\u120b\u12ed \u12c8\u12f3\u1208\u12cd \u1241\u1325\u122d %s \u1265\u122d \u1260 Telebirr \u12ed\u120b\u12a9\u1362\n"
                     "2\ufe0f\u20e3 \u12a8\u12a8\u1348\u1209 \u1260\u128b\u120b \u12a8 Telebirr \u12e8\u12f0\u1228\u1230\u12ce\u1275\u1295 \u12e8 SMS \u133d\u1201\u134d \u1219\u1209 (\u12c8\u12ed\u121d \u12e8\u130d\u1265\u12ed\u1275 \u1241\u1325\u1229\u1295 - Transaction ID) \u12a5\u12da\u1205 \u12ed\u120b\u12a9\u1362\n\n"
                     "\u12ad\u134d\u12eb\u12cd \u1232\u1228\u130b\u1308\u1325 \u12c8\u12f2\u12eb\u12cd\u1291 \u12c8\u12f0 \u1202\u1233\u1265\u12ce \u12ed\u1328\u1218\u122b\u120d\u1362" % money(amt),
                     "1\ufe0f\u20e3 Send %s ETB to the number above with Telebirr.\n"
                     "2\ufe0f\u20e3 After paying, send the full Telebirr SMS text (or just the Transaction ID) here.\n\n"
                     "It is added to your balance as soon as the payment is confirmed." % money(amt))]
    return "\n".join(lines)


async def show_wallet(update, uid):
    row = get_user(uid)
    pct, cap = current_bonus()
    text = L(uid, "\U0001f4b3 Wallet\n\nPlay: %s \u1265\u122d\nWin: %s \u1265\u122d" % (money(row["credits"]), money(row["win_credits"])),
             "\U0001f4b3 Wallet\n\nPlay: %s ETB\nWin: %s ETB" % (money(row["credits"]), money(row["win_credits"])))
    if pct > 0:
        text += L(uid, "\n\n\U0001f381 \u12a0\u1201\u1295 \u12e8\u1308\u1295\u12d8\u1265 \u121b\u1235\u1308\u1262\u12eb \u1266\u1290\u1235: %s%%" % money(pct) + (" (\u12a5\u1235\u12a8 %s \u1265\u122d)" % money(cap) if cap else ""),
                  "\n\n\U0001f381 Deposit bonus right now: %s%%" % money(pct) + (" (up to %s ETB)" % money(cap) if cap else ""))
    text += L(uid, "\n\n\u1308\u1295\u12d8\u1265 \u1208\u121b\u1235\u1308\u1263\u1275 \u12a8\u1273\u127d \u12eb\u1208\u12cd\u1295 \u1241\u120d\u134d \u12ed\u132b\u1291\u1362 \u1208\u121b\u12cd\u1323\u1275 \u1260 Mini App \u12cd\u1235\u1325 Wallet \u2192 WITHDRAW \u12ed\u1320\u1240\u1219\u1362",
              "\n\nTap the button below to deposit. To withdraw use Wallet \u2192 WITHDRAW inside the Mini App.")
    rows = [[InlineKeyboardButton(L(uid, "\U0001f4e5 \u1308\u1295\u12d8\u1265 \u121b\u1235\u1308\u1263\u1275", "\U0001f4e5 Deposit"), callback_data="w:dep")]]
    if PUBLIC_URL:
        rows.append([InlineKeyboardButton(L(uid, "\U0001f4e4 \u1308\u1295\u12d8\u1265 \u121b\u12cd\u1323\u1275 (Mini App)", "\U0001f4e4 Withdraw (Mini App)"),
                                          web_app=WebAppInfo(url=webapp_url()))])
    await update.effective_message.reply_text(text, reply_markup=InlineKeyboardMarkup(rows))


async def send_invite(update, context, uid):
    try:
        username = (await context.bot.get_me()).username
    except Exception:
        username = ""
    if not username:
        await update.effective_message.reply_text("Please try again in a moment.")
        return
    link = "https://t.me/%s?start=ref_%d" % (username, uid)
    n, paid, earned = referral_stats(uid)
    text = L(uid,
             "\U0001f517 \u130b\u1265\u12dd \u12a5\u1293 \u12a0\u130d\u129d\n\n\u12e8\u12a5\u122d\u1235\u12ce \u1218\u130b\u1260\u12e3 \u120a\u1295\u12ad:\n%s\n\n\u1313\u12f0\u129b\u12ce \u1260\u12da\u1205 \u120a\u1295\u12ad \u1308\u1265\u1276 \u12e8\u1218\u1300\u1218\u122a\u12eb \u1308\u1295\u12d8\u1261\u1295 \u1232\u12eb\u1235\u1308\u1263 \u12a5\u1293 \u1232\u1338\u12f5\u1245 "
             "%d \u1265\u122d \u12c8\u12f0 \u1202\u1233\u1265\u12ce \u12ed\u1328\u1218\u122b\u120d\u1362\n\n\U0001f465 \u12e8\u130b\u1260\u12df\u1278\u12cd: %d\n\u2705 \u1308\u1295\u12d8\u1265 \u12eb\u1235\u1308\u1261: %d\n\U0001f4b0 \u12eb\u1308\u1299\u1275: %s \u1265\u122d" % (link, REFERRAL_REWARD, n, paid, money(earned)),
             "\U0001f517 Invite & earn\n\nYour invite link:\n%s\n\nWhen a friend joins with this link and his first deposit is approved, "
             "%d ETB is added to your balance.\n\n\U0001f465 Invited: %d\n\u2705 Deposited: %d\n\U0001f4b0 Earned: %s ETB" % (link, REFERRAL_REWARD, n, paid, money(earned)))
    share = "https://t.me/share/url?url=%s&text=%s" % (quote(link), quote(L(uid, "\u1260\u123d \u1260\u123d \u1262\u1295\u130e - \u1270\u132b\u12cd\u1270\u12cd \u12eb\u1238\u1295\u1349! \U0001f3b0", "Besh Besh Bingo - play and win! \U0001f3b0")))
    await update.effective_message.reply_text(
        text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(L(uid, "\U0001f4e4 \u1208\u1313\u12f0\u129b \u12a0\u130b\u122b", "\U0001f4e4 Share"), url=share)]]))


async def handle_state_text(update, ud, user, msg):
    uid, state = user.id, ud.get("state")
    reply = update.effective_message.reply_text
    loop = asyncio.get_running_loop()
    if state == "dep_amount":
        amt = parse_amount(msg)
        if amt is None or amt < MIN_DEPOSIT:
            await reply(L(uid, "\u26a0\ufe0f \u12a5\u1263\u12ad\u12ce \u1275\u12ad\u12ad\u1208\u129b \u1218\u1320\u1295 (\u1241\u1325\u122d \u1265\u127b) \u12ed\u120b\u12a9\u1362 \u12dd\u1245\u1270\u129b\u12cd %d \u1265\u122d \u1290\u12cd\u1362" % MIN_DEPOSIT,
                          "\u26a0\ufe0f Please send a valid amount (numbers only). The minimum is %d ETB." % MIN_DEPOSIT),
                        reply_markup=cancel_markup(uid))
            return
        ud["dep_amount"], ud["state"] = amt, "dep_ref"
        await reply(deposit_instructions(uid, amt), reply_markup=cancel_markup(uid))
    elif state == "dep_ref":
        ref, err = extract_reference(msg)
        if err:
            await reply(err, reply_markup=cancel_markup(uid))
            return
        status, message = await loop.run_in_executor(None, submit_deposit, uid, ud.get("dep_amount") or MIN_DEPOSIT, ref)
        if status in ("approved", "pending"):
            ud.pop("state", None)
            ud.pop("dep_amount", None)
        if status != "approved":   # an approved deposit already sends its own confirmation
            await reply(message, reply_markup=(cancel_markup(uid) if status == "error" else None))
    elif state == "promo":
        ud.pop("state", None)
        result, reward = await loop.run_in_executor(None, redeem_promo, uid, msg)
        await reply(promo_message(uid, result, reward))


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user.id, user.username, user.first_name)
    msg = (update.effective_message.text or "").strip()
    lang = get_language(user.id)
    t = TEXT.get(lang, TEXT["am"])
    ud = getattr(context, "user_data", None)
    if ud is None:
        ud = {}
    menu = is_menu_text(msg)
    if menu:   # pressing any menu button cancels a half-finished deposit / promo entry
        ud.pop("state", None)
        ud.pop("dep_amount", None)
    elif ud.get("state"):
        await handle_state_text(update, ud, user, msg)
        return

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
    elif msg in (t["rules"], "\U0001f4dc \u1205\u130d \u12a5\u1293 \u12f0\u1295\u1266\u127d", "\U0001f4dc Rules"):
        await update.effective_message.reply_text(tr(user.id, "rules_msg"))
    elif msg in (t["wallet"], "\U0001f4b3 Wallet / \u12aa\u1235 \u1266\u122d\u1233", "\U0001f4b3 Wallet"):
        await show_wallet(update, user.id)
    elif msg == t["promo"]:
        ud["state"] = "promo"
        await update.effective_message.reply_text(
            L(user.id, "\U0001f381 \u12e8\u1355\u122e\u121e \u12ae\u12f1\u1295 \u12ed\u120b\u12a9\u1362", "\U0001f381 Send your promo code."), reply_markup=cancel_markup(user.id))
    elif msg == t["invite"]:
        await send_invite(update, context, user.id)
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
        await update.effective_message.reply_text(
            "\U0001f198 \u12f5\u130b\u134d / Help\n\n\u12a0\u12f5\u121a\u1291\u1295 \u1208\u121b\u130d\u1298\u1275 / \u12f5\u130b\u134d \u1208\u121b\u130d\u1298\u1275\u1366 @%s\nContact the admin / get support: @%s"
            % (SUPPORT_USERNAME, SUPPORT_USERNAME))
    else:
        await update.effective_message.reply_text(tr(user.id, "choose"), reply_markup=main_keyboard(user.id))


# ---------------- /announce (admin only, preview + confirm) ----------------
pending_announce = {}   # admin_id -> {"text", "audience", "ids"}
_bg_tasks = set()


async def announce_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        await update.effective_message.reply_text("\U0001f6ab Unauthorized.")
        return
    raw = update.effective_message.text or ""
    cmd = raw.split(None, 1)[0].split("@")[0].lower() if raw.strip() else "/announce"
    parts = raw.split(None, 1)
    body = parts[1].strip() if len(parts) > 1 else ""
    audience = "players" if cmd.startswith("/announce_players") else "all"
    if not body:
        await update.effective_message.reply_text(
            "\U0001f4e3 \u12a0\u1320\u1243\u1240\u121d:\n/announce \u1218\u120d\u12d5\u12ad\u1275 \u2014 \u1208\u1201\u1209\u121d \u1270\u1320\u1243\u121a\u12ce\u127d\n/announce_players \u1218\u120d\u12d5\u12ad\u1275 \u2014 \u12a8\u12da\u1205 \u1260\u134a\u1275 \u1208\u1270\u132b\u12c8\u1271\u1275 \u1265\u127b")
        return
    ids = announce_recipients(audience)
    pending_announce[user.id] = {"text": body, "audience": audience, "ids": ids}
    await update.effective_message.reply_text(
        "\U0001f4e3 \u1245\u12f5\u1218 \u12a5\u12ed\u1273 (%s \u1230\u12ce\u127d \u12ed\u12f0\u122d\u1233\u1278\u12cb\u120d):\n\n%s" % (len(ids), body),
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("\u2705 \u120b\u12ad", callback_data="ann:send"),
            InlineKeyboardButton("\u274c \u1230\u122d\u12dd", callback_data="ann:cancel")]]))


async def run_announce(bot, admin_id, text, ids):
    sent = failed = 0
    for uid in ids:
        for attempt in (1, 2):
            try:
                await bot.send_message(chat_id=uid, text=text)
                sent += 1
                break
            except Exception as e:
                wait = getattr(e, "retry_after", None)
                if wait and attempt == 1:
                    await asyncio.sleep(float(wait) + 1)
                    continue
                failed += 1
                break
        await asyncio.sleep(0.05)   # stay below Telegram's ~30 messages per second
    try:
        await bot.send_message(chat_id=admin_id, text="\U0001f4e3 \u121b\u1235\u1273\u12c8\u1242\u12eb \u1270\u1320\u1293\u1240\u1240\u1362\n\u2705 \u12f0\u122d\u1237\u120d: %d\n\u26a0\ufe0f \u12a0\u120d\u12f0\u1228\u1230\u121d: %d" % (sent, failed))
    except Exception:
        logger.exception("could not send the announcement report")


async def handle_announce_button(query, context, data, admin_id):
    job = pending_announce.pop(admin_id, None)
    if data == "ann:cancel" or job is None:
        await query.answer("\u1270\u1230\u122d\u12df\u120d" if job else "\u121d\u1295\u121d \u12e8\u121a\u1320\u1265\u1245 \u121b\u1235\u1273\u12c8\u1242\u12eb \u12e8\u1208\u121d", show_alert=job is None)
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        return
    await query.answer("\u12a5\u12e8\u1270\u120b\u12a8 \u1290\u12cd...")
    try:
        await query.edit_message_text((query.message.text or "") + "\n\n\u23f3 \u12a5\u12e8\u1270\u120b\u12a8 \u1290\u12cd...")
    except Exception:
        pass
    task = asyncio.create_task(run_announce(context.bot, admin_id, job["text"], job["ids"]))
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


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
    if data.startswith("ann:"):
        if user_id not in ADMIN_IDS:
            await query.answer("\U0001f6ab Unauthorized", show_alert=True)
            return
        await handle_announce_button(query, context, data, user_id)
        return
    if data.startswith(("dep:", "wd:")):
        # buttons also work inside a group, so only real admins may press them
        if user_id not in ADMIN_IDS:
            await query.answer("\U0001f6ab Unauthorized", show_alert=True)
            return
        await handle_review_button(query, data, user_id)
        return
    await query.answer()
    ensure_user(user_id, query.from_user.username, query.from_user.first_name)
    if data.startswith("w:"):
        ud = context.user_data
        if data == "w:dep":
            ud["state"] = "dep_amount"
            ud.pop("dep_amount", None)
            await query.message.reply_text(
                L(user_id, "\U0001f4b0 \u1235\u1295\u1275 \u1265\u122d \u121b\u1235\u1308\u1263\u1275 \u12ed\u1348\u120d\u130b\u1209?\n\n\u12dd\u1245\u1270\u129b\u12cd %d \u1265\u122d \u1290\u12cd\u1362 \u1241\u1325\u122d \u1265\u127b \u12ed\u120b\u12a9 (\u1208\u121d\u1233\u120c 100)\u1362" % MIN_DEPOSIT,
                  "\U0001f4b0 How much do you want to deposit?\n\nThe minimum is %d ETB. Send the number only (e.g. 100)." % MIN_DEPOSIT),
                reply_markup=cancel_markup(user_id))
        elif data == "w:cancel":
            ud.pop("state", None)
            ud.pop("dep_amount", None)
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass
            await query.message.reply_text(L(user_id, "\u1270\u1230\u122d\u12df\u120d\u1362", "Cancelled."))
        return
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
bot_application.add_handler(CommandHandler("announce", announce_command))
bot_application.add_handler(CommandHandler("announce_players", announce_command))
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


def L(uid, am, en):
    return en if get_language(uid) == "en" else am


def utc_now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def fmt_db(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def parse_db_time(s):
    try:
        return datetime.strptime(str(s)[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return utc_now()


def eth_clock(minute):
    """Ethiopian clock text for minutes-after-midnight on the standard clock (15:00 -> \u12a8\u1240\u1291 9 \u1230\u12d3\u1275)."""
    h, m = (int(minute) // 60) % 24, int(minute) % 60
    eh = (h - 6) % 12
    if eh == 0:
        eh = 12
    period = "\u1320\u12cb\u1275" if 6 <= h < 12 else ("\u12a8\u1240\u1291" if 12 <= h < 18 else ("\u121d\u123d\u1275" if h >= 18 else "\u120c\u120a\u1275"))
    return "%s %d%s \u1230\u12d3\u1275" % (period, eh, (":%02d" % m) if m else "")


# ---------------- deposit bonus (rules are editable in the admin panel) ----------------
def matching_rules(con, when_utc):
    """Active bonus rules that apply at this moment (Ethiopia time = UTC+3)."""
    local = when_utc + timedelta(hours=3)
    minute, weekday = local.hour * 60 + local.minute, local.weekday()
    out = []
    for r in con.execute("SELECT * FROM bonus_rules WHERE active=1").fetchall():
        days = [int(x) for x in (r["days"] or "").split(",") if x.strip().isdigit()]
        if days and weekday not in days:
            continue
        if r["start_min"] <= minute < r["end_min"]:
            out.append(r)
    return out


def bonus_for(con, amount, when_utc):
    """(bonus, percent): only the HIGHEST applicable percentage is used; the cap of that rule limits the bonus."""
    best_b, best_p = 0.0, 0.0
    for r in matching_rules(con, when_utc):
        b = float(amount) * r["percent"] / 100.0
        if r["cap"] and r["cap"] > 0:
            b = min(b, r["cap"])
        b = round(b + 1e-9, 2)
        if r["percent"] > best_p or (r["percent"] == best_p and b > best_b):
            best_b, best_p = b, r["percent"]
    return best_b, best_p


def current_bonus():
    """(percent, cap) of the best rule right now (for banners)."""
    with closing(db()) as con:
        best = (0.0, 0.0)
        for r in matching_rules(con, utc_now()):
            if r["percent"] > best[0]:
                best = (r["percent"], r["cap"])
        return best


def bonus_info():
    pct, cap = current_bonus()
    return {"bonus_percent": pct, "bonus_cap": cap, "pay_number": PAY_NUMBER, "pay_name": PAY_NAME,
            "min_deposit": MIN_DEPOSIT}


def bonus_preview(amount):
    with closing(db()) as con:
        return bonus_for(con, amount, utc_now())


# ---------------- reading a Telebirr SMS ----------------
def parse_telebirr_sms(text):
    """Returns a dict for an INCOMING-money SMS (\u1270\u1240\u1265\u1208\u12cb\u120d / received), otherwise None."""
    t = text or ""
    if not ("\u1270\u1240\u1265\u1208\u12cb\u120d" in t or re.search(r"\breceived\b", t, re.I)):
        return None
    m = re.search(r"\u1241\u1325\u122d\u12ce\s*([A-Za-z0-9]{8,14})", t) or re.search(r"/receipt/([A-Za-z0-9]{8,14})", t)
    if not m:
        return None
    a = re.search(r"([\d,]+(?:\.\d+)?)\s*(?:\u1265\u122d|ETB|Birr)", t, re.I)
    if not a:
        return None
    try:
        amount = float(a.group(1).replace(",", ""))
    except ValueError:
        return None
    if amount <= 0:
        return None
    paid_at = utc_now()
    d = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})\s+(\d{1,2}):(\d{2})(?::(\d{2}))?", t)
    if d:
        try:
            local = datetime(int(d.group(3)), int(d.group(2)), int(d.group(1)),
                             int(d.group(4)), int(d.group(5)), int(d.group(6) or 0))
            utc = local - timedelta(hours=3)
            if abs((utc - paid_at).total_seconds()) < 2 * 86400:   # sanity: ignore absurd dates
                paid_at = utc
        except ValueError:
            pass
    sender_tail = ""
    s = re.search(r"\(([0-9*+]{6,16})\)", t)
    if s:
        sender_tail = re.sub(r"\D", "", s.group(1))[-4:]
    return {"ref": m.group(1).upper(), "amount": amount, "paid_at": paid_at, "sender_tail": sender_tail}


def extract_reference(text):
    """Transaction ID from what the player typed or pasted (whole SMS, receipt link or the bare ID).
    Returns (ref, error_message_or_None)."""
    t = str(text or "").strip()
    if not t:
        return "", "\u12a5\u1263\u12ad\u12ce \u12e8\u130d\u1265\u12ed\u1275 \u1241\u1325\u1229\u1295 \u12c8\u12ed\u121d \u12e8 SMS \u133d\u1201\u1349\u1295 \u12ed\u120b\u12a9\u1362"
    m = re.search(r"\u1241\u1325\u122d\u12ce\s*([A-Za-z0-9]{8,14})", t)
    if m and "\u120d\u12a8\u12cb\u120d" in t:
        # a SENT-money SMS: the masked number in brackets is the receiver - it must be the owner's number
        r = re.search(r"\(([0-9*+]{6,16})\)", t)
        if r and re.sub(r"\D", "", r.group(1))[-4:] != PAY_TAIL:
            return "", "\u12ed\u1205 \u12ad\u134d\u12eb \u12c8\u12f0 %s \u12e8\u1270\u120b\u12a8 \u12a0\u12ed\u1218\u1235\u120d\u121d\u1362 \u12a5\u1263\u12ad\u12ce \u12c8\u12f0 \u1275\u12ad\u12ad\u1208\u129b\u12cd \u1241\u1325\u122d \u12ed\u12ad\u1348\u1209\u1362" % PAY_NUMBER
    if not m:
        m = re.search(r"/receipt/([A-Za-z0-9]{8,14})", t)
    ref = m.group(1).upper() if m else clean_reference(t)
    problem = reference_problem(ref)
    if problem:
        return "", problem
    return ref, None


def notify_deposit(req_id, uid, method, amount, ref, note=None):
    phone = get_phone(uid)
    when = time.strftime("%Y-%m-%d %H:%M", time.gmtime(time.time() + 3 * 3600))  # Ethiopia time
    lines = [
        "\U0001f4b0 \u12a0\u12f2\u1235 \u12e8\u1308\u1295\u12d8\u1265 \u121b\u1235\u1308\u1262\u12eb \u1325\u12eb\u1244 #%d" % req_id,
        "",
        "\u1270\u132b\u12cb\u127d: %s (ID %s)" % (display_name(uid), uid),
    ]
    if phone:
        lines.append("\u1235\u120d\u12ad: +%s" % phone)
    lines += [
        "\u1263\u1295\u12ad: %s" % method,
        "\u1218\u1320\u1295: %s \u1265\u122d" % money(amount),
        "Transaction ID: %s" % ref,
        "\u1230\u12d3\u1275: %s" % when,
        "",
        note or "\u26a0\ufe0f \u12a8 SMS \u130b\u122d \u12a0\u120d\u1270\u1323\u1218\u1228\u121d (\u1235\u120d\u12ad\u1205 \u120b\u12ed \u1308\u1293 \u12a0\u120d\u12f0\u1228\u1230 \u12ed\u1206\u1293\u120d)\u1362 \u12f0\u1228\u1230\u1299\u1295 \u12a0\u1228\u130b\u130d\u1325\u1362",
        "",
        "\u2705 \u12a8\u121b\u133d\u12f0\u1245\u1205 \u1260\u134a\u1275 \u12a0\u1228\u130b\u130d\u1325:",
        "1. \u1241\u1325\u1229 \u1260\u122b\u1235\u1205 \u12e8\u1274\u120c\u1265\u122d \u1273\u122a\u12ad/SMS \u12cd\u1235\u1325 \u12a0\u1208",
        "2. \u1218\u1320\u1291 \u1260\u1275\u12ad\u12ad\u120d \u12ed\u1218\u1233\u1230\u120b\u120d (\u12e8\u12f0\u1228\u1230\u12cd \u1265\u122d \u1290\u12cd \u12e8\u121a\u1328\u1218\u1228\u12cd)",
        "3. \u1201\u1294\u1273\u12cd \u00ab\u1270\u1233\u12ad\u1277\u120d\u00bb \u1290\u12cd",
        "4. \u1241\u1325\u1229 \u12a8\u12da\u1205 \u1260\u134a\u1275 \u1260\u120c\u120b \u12f2\u1356\u12da\u1275 \u120b\u12ed \u12a0\u120d\u12cb\u1208\u121d",
        "",
        "\u26a0\ufe0f \u1235\u12ad\u122a\u1295\u123e\u1275 \u1265\u127b \u121b\u1235\u1228\u1303 \u12a0\u12ed\u12f0\u1208\u121d\u1362 \u12a0\u1320\u122b\u1323\u122a \u12a8\u1206\u1290 \u121d\u1295\u121d \u12a0\u1275\u132b\u1295\u1362",
    ]
    rows = []
    if re.fullmatch(r"[A-Z0-9]{8,14}", ref or ""):
        rows.append([InlineKeyboardButton("\U0001f517 \u12f0\u1228\u1230\u129d \u12ad\u1348\u1275", url=RECEIPT_URL + ref)])
    rows.append([
        InlineKeyboardButton("\u2705 \u12a0\u133d\u12f5\u1245", callback_data="dep:approve:%d" % req_id),
        InlineKeyboardButton("\u274c \u12cd\u12f5\u1245 \u12a0\u12f5\u122d\u130d", callback_data="dep:reject:%d" % req_id),
    ])
    notify_admins("\n".join(lines), InlineKeyboardMarkup(rows))


def approve_deposit(req_id, reviewer_id, via="admin", amount=None, paid_at=None):
    """Approve a pending deposit exactly once and credit it: the amount that REALLY arrived (when known from
    the SMS) + the bonus for the payment time (both to the Play wallet) + the one-time referral reward.
    Returns a dict, or None when the request was not pending."""
    inviter, ref_reward = None, 0.0
    with closing(db()) as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT * FROM deposit_requests WHERE id=? AND status='pending'", (req_id,)).fetchone()
        if row is None:
            con.rollback()
            return None
        uid = row["user_id"]
        ref = (row["reference"] or "").replace(" ", "").upper()
        inc = con.execute("SELECT * FROM incoming_payments WHERE ref=?", (ref,)).fetchone()
        inc_free = inc is not None and not inc["used_by_req"]
        if amount is None and inc_free:
            amount, paid_at = inc["amount"], parse_db_time(inc["paid_at"])
        amt = float(amount) if amount else float(row["amount"])
        when = paid_at or utc_now()
        bonus, pct = bonus_for(con, amt, when)
        con.execute(
            "UPDATE deposit_requests SET status='approved', reviewed_at=CURRENT_TIMESTAMP, reviewed_by=?, "
            "amount=?, bonus=?, credited=?, approved_via=? WHERE id=?",
            (int(reviewer_id), amt, bonus, amt + bonus, via, req_id))
        con.execute("UPDATE users SET credits=credits+? WHERE user_id=?", (amt + bonus, uid))
        if inc_free:
            con.execute("UPDATE incoming_payments SET used_by_req=? WHERE ref=?", (req_id, ref))
        u = con.execute("SELECT referred_by, referral_paid FROM users WHERE user_id=?", (uid,)).fetchone()
        if u and u["referred_by"] and not u["referral_paid"]:
            earlier = con.execute(
                "SELECT COUNT(*) FROM deposit_requests WHERE user_id=? AND status='approved' AND id!=?",
                (uid, req_id)).fetchone()[0]
            exists = con.execute("SELECT 1 FROM users WHERE user_id=?", (u["referred_by"],)).fetchone()
            if earlier == 0 and exists:
                inviter, ref_reward = int(u["referred_by"]), float(REFERRAL_REWARD)
                con.execute("UPDATE users SET credits=credits+? WHERE user_id=?", (ref_reward, inviter))
                con.execute("UPDATE users SET referral_paid=? WHERE user_id=?", (ref_reward, uid))
        con.commit()
    typed = float(row["amount"])
    msg = L(uid, "\u2705 %s \u1265\u122d \u12c8\u12f0 \u1202\u1233\u1265\u12ce \u1270\u1328\u121d\u122f\u120d\u1362 \u1218\u120d\u12ab\u121d \u1328\u12cb\u1273!" % money(amt),
            "\u2705 %s ETB was added to your balance. Good luck!" % money(amt))
    if bonus > 0:
        msg += L(uid, "\n\U0001f381 \u1266\u1290\u1235: +%s \u1265\u122d (%s%%) \u12c8\u12f0 \u1218\u132b\u12c8\u127b \u1202\u1233\u1265\u12ce \u1270\u1328\u121d\u122f\u120d\u1362" % (money(bonus), money(pct)),
                 "\n\U0001f381 Bonus: +%s ETB (%s%%) added to your Play wallet." % (money(bonus), money(pct)))
    send_to_chat(uid, msg)
    if inviter:
        send_to_chat(inviter, L(inviter,
                                "\U0001f389 \u12eb\u1235\u130b\u1260\u12d9\u1275 \u1230\u12cd \u12e8\u1218\u1300\u1218\u122a\u12eb \u1308\u1295\u12d8\u1265 \u12a0\u1235\u1308\u1265\u1277\u120d! %s \u1265\u122d \u123d\u120d\u121b\u1275 \u12c8\u12f0 \u1202\u1233\u1265\u12ce \u1270\u1328\u121d\u122f\u120d\u1362" % money(ref_reward),
                                "\U0001f389 Someone you invited made a first deposit! %s ETB reward was added to your balance." % money(ref_reward)))
    if via == "sms":
        text = ("\u2705 \u122b\u1235-\u1230\u122d \u1270\u1228\u130b\u130d\u1327\u120d #%d\n\u1270\u132b\u12cb\u127d: %s (ID %s)\n\u1218\u1320\u1295: %s \u1265\u122d" % (req_id, display_name(uid), uid, money(amt)))
        if bonus > 0:
            text += " (+%s \u1266\u1290\u1235, %s%%)" % (money(bonus), money(pct))
        text += "\nTransaction ID: %s" % ref
        if abs(typed - amt) > 0.004:
            text += "\n\u26a0\ufe0f \u1270\u132b\u12cb\u1279 \u12e8\u133b\u1348\u12cd %s \u1265\u122d \u1290\u1260\u122d\u1364 \u1260\u12a5\u12cd\u1290\u1275 \u12e8\u12f0\u1228\u1230\u12cd %s \u1265\u122d \u1270\u1328\u121d\u122f\u120d\u1362" % (money(typed), money(amt))
        notify_admins(text)
    return {"id": req_id, "user_id": uid, "typed": typed, "amount": amt, "bonus": bonus, "percent": pct,
            "via": via, "inviter": inviter, "referral_reward": ref_reward}


def auto_approve(req, amount, paid_at, sender_tail):
    """The SMS for this pending request has arrived. Approve automatically unless something looks off,
    in which case the admin gets the normal approve/reject message with the reason."""
    uid = req["user_id"]
    phone = get_phone(uid)
    note = None
    if amount < MIN_DEPOSIT:
        note = "\u26a0\ufe0f \u12ad\u134d\u12eb\u12cd \u1260 SMS \u12f0\u122d\u1237\u120d (%s \u1265\u122d) \u130d\u1295 \u12a8\u12dd\u1245\u1270\u129b\u12cd (%d \u1265\u122d) \u12eb\u1295\u1233\u120d\u1362 \u1260\u12a5\u1305 \u12c8\u1235\u1295\u1362" % (money(amount), MIN_DEPOSIT)
    elif sender_tail and phone and phone[-4:] != sender_tail:
        note = ("\u26a0\ufe0f \u12ad\u134d\u12eb\u12cd \u1260 SMS \u12f0\u122d\u1237\u120d (%s \u1265\u122d) \u130d\u1295 \u120b\u12aa\u12cd (\u2026%s) \u12a8\u1270\u132b\u12cb\u1279 \u12a8\u1270\u1218\u12d8\u1308\u1260\u12cd \u1235\u120d\u12ad (\u2026%s) \u130b\u122d \u12a0\u12ed\u1218\u1233\u1230\u120d\u121d\u1362 "
                "\u1260\u12a5\u1305 \u12a0\u1228\u130b\u130d\u1325\u1362" % (money(amount), sender_tail, phone[-4:]))
    if note:
        notify_deposit(req["id"], uid, req["method"], req["amount"], req["reference"], note)
        return "manual"
    return "approved" if approve_deposit(req["id"], 0, "sms", amount, paid_at) else "gone"


def handle_incoming_sms(p):
    """A Telebirr 'received' SMS came from the owner's phone. Remember it and match it to a pending request."""
    ref = p["ref"]
    with closing(db()) as con:
        con.execute("BEGIN IMMEDIATE")
        if con.execute("SELECT 1 FROM incoming_payments WHERE ref=?", (ref,)).fetchone():
            con.rollback()
            return "duplicate"
        con.execute("INSERT INTO incoming_payments(ref, amount, sender, paid_at) VALUES(?,?,?,?)",
                    (ref, p["amount"], p.get("sender_tail") or "", fmt_db(p["paid_at"])))
        req = con.execute(
            "SELECT * FROM deposit_requests WHERE UPPER(REPLACE(reference,' ',''))=? AND status='pending' "
            "ORDER BY id LIMIT 1", (ref,)).fetchone()
        con.commit()
    if req is None:
        return "stored"   # the player has not sent his request yet; it is approved the moment he does
    return auto_approve(req, p["amount"], p["paid_at"], p.get("sender_tail"))


def submit_deposit(uid, amt, ref):
    """One deposit request from the bot chat or the Mini App. Returns (status, message):
    status 'approved' | 'pending' | 'error'."""
    ref = str(ref or "").replace(" ", "").upper()
    if amt < MIN_DEPOSIT:
        return "error", L(uid, "\u26a0\ufe0f \u12dd\u1245\u1270\u129b\u12cd \u1308\u1295\u12d8\u1265 \u121b\u1235\u1308\u1262\u12eb %d \u1265\u122d \u1290\u12cd\u1362" % MIN_DEPOSIT,
                          "\u26a0\ufe0f The minimum deposit is %d ETB." % MIN_DEPOSIT)
    inc, req_id = None, None
    with closing(db()) as con:
        con.execute("BEGIN IMMEDIATE")  # check + insert as one step, so the same ID cannot slip in twice
        pending = con.execute("SELECT COUNT(*) FROM deposit_requests WHERE user_id=? AND status='pending'",
                              (uid,)).fetchone()[0]
        if pending >= MAX_PENDING_DEPOSITS:
            con.rollback()
            return "error", L(uid, "\u1265\u12d9 \u12eb\u120d\u1270\u1228\u130b\u1308\u1321 \u1325\u12eb\u1244\u12ce\u127d \u12a0\u1209\u12ce\u1275\u1362 \u12a0\u1235\u1270\u12f3\u12f3\u122a\u12cd \u12a5\u1235\u12aa\u12eb\u1228\u130b\u130d\u1325 \u12f5\u1228\u1235 \u12ed\u1320\u1265\u1241\u1362",
                              "You have too many unreviewed requests. Please wait for the administrator.")
        used = con.execute("SELECT 1 FROM deposit_requests WHERE UPPER(REPLACE(reference,' ',''))=? "
                           "AND status IN ('pending','approved')", (ref,)).fetchone()
        if used:
            con.rollback()
            return "error", L(uid, "\u12ed\u1205 \u12e8\u130d\u1265\u12ed\u1275 \u1241\u1325\u122d \u1240\u12f0\u121d \u1232\u120d \u1270\u1218\u12dd\u130d\u1267\u120d\u1362 \u12a5\u1263\u12ad\u12ce \u1275\u12ad\u12ad\u1208\u129b\u12cd\u1295 \u1241\u1325\u122d \u12eb\u1235\u1308\u1261\u1362",
                              "This transaction ID was already used. Please send the correct one.")
        cur = con.execute("INSERT INTO deposit_requests(user_id, method, amount, reference) VALUES(?,?,?,?)",
                          (uid, "Telebirr", amt, ref))
        req_id = cur.lastrowid
        inc = con.execute("SELECT * FROM incoming_payments WHERE ref=? AND used_by_req IS NULL", (ref,)).fetchone()
        con.commit()
    if inc is not None:
        # the owner's phone already reported this payment: verify and approve at once
        req = {"id": req_id, "user_id": uid, "method": "Telebirr", "amount": amt, "reference": ref}
        res = auto_approve(req, inc["amount"], parse_db_time(inc["paid_at"]), inc["sender"])
        if res == "approved":
            return "approved", L(uid, "\u2705 \u12ad\u134d\u12eb\u12ce \u1270\u1228\u130b\u130d\u1327\u120d!", "\u2705 Your payment is confirmed!")
    else:
        notify_deposit(req_id, uid, "Telebirr", amt, ref)
    return "pending", L(uid, "\u23f3 \u1325\u12eb\u1244\u12ce \u12f0\u122d\u1236\u1293\u120d\u1362 \u12ad\u134d\u12eb\u12cd \u1232\u1228\u130b\u1308\u1325 \u12c8\u12f2\u12eb\u12cd\u1291 \u12c8\u12f0 \u1202\u1233\u1265\u12ce \u12ed\u1328\u1218\u122b\u120d\u1362",
                        "\u23f3 We received your request. It is added to your balance as soon as the payment is confirmed.")


def review_deposit(req_id, decision, admin_id):
    """Approve/reject a pending deposit exactly once. Returns something truthy, or None if not pending."""
    if decision == "approve":
        return approve_deposit(req_id, admin_id, "admin")
    with closing(db()) as con:
        cur = con.execute(
            "UPDATE deposit_requests SET status='rejected', reviewed_at=CURRENT_TIMESTAMP, reviewed_by=? "
            "WHERE id=? AND status='pending'", (int(admin_id), req_id))
        if cur.rowcount != 1:
            con.commit()
            return None
        row = con.execute("SELECT * FROM deposit_requests WHERE id=?", (req_id,)).fetchone()
        con.commit()
    send_to_chat(row["user_id"], tr(row["user_id"], "dep_rejected", amount=money(row["amount"])))
    return dict(row)


# ---------------- promo codes ----------------
def redeem_promo(uid, code):
    """Returns (result, reward): ok | invalid | expired | used_up | already."""
    code = re.sub(r"\s+", "", str(code or "")).upper()[:40]
    if not code:
        return "invalid", 0
    with closing(db()) as con:
        con.execute("BEGIN IMMEDIATE")
        p = con.execute("SELECT * FROM promo_codes WHERE code=?", (code,)).fetchone()
        if p is None or not p["active"]:
            con.rollback()
            return "invalid", 0
        if p["expires_at"] and p["expires_at"] < fmt_db(utc_now()):
            con.rollback()
            return "expired", 0
        if p["max_uses"] and p["uses"] >= p["max_uses"]:
            con.rollback()
            return "used_up", 0
        if con.execute("SELECT 1 FROM promo_uses WHERE code=? AND user_id=?", (code, uid)).fetchone():
            con.rollback()
            return "already", 0
        con.execute("INSERT INTO promo_uses(code, user_id) VALUES(?,?)", (code, uid))
        con.execute("UPDATE promo_codes SET uses=uses+1 WHERE code=?", (code,))
        con.execute("UPDATE users SET credits=credits+? WHERE user_id=?", (p["reward"], uid))
        con.commit()
        return "ok", float(p["reward"])


def promo_message(uid, result, reward):
    if result == "ok":
        return L(uid, "\U0001f389 \u1270\u1233\u12ad\u1277\u120d! %s \u1265\u122d \u12c8\u12f0 \u1218\u132b\u12c8\u127b \u1202\u1233\u1265\u12ce \u1270\u1328\u121d\u122f\u120d\u1362" % money(reward),
                 "\U0001f389 Done! %s ETB was added to your Play wallet." % money(reward))
    return {
        "expired": L(uid, "\u231b \u12ed\u1205 \u12ae\u12f5 \u130a\u12dc\u12cd \u12a0\u120d\u134e\u1260\u1273\u120d\u1362", "\u231b This code has expired."),
        "used_up": L(uid, "\U0001f614 \u12ed\u1205 \u12ae\u12f5 \u1219\u1209 \u1260\u1219\u1209 \u1325\u1245\u121d \u120b\u12ed \u12cd\u120f\u120d\u1362", "\U0001f614 This code has been fully used."),
        "already": L(uid, "\u2139\ufe0f \u12ed\u1205\u1295 \u12ae\u12f5 \u1240\u12f0\u121d \u1265\u1208\u12cd \u1270\u1320\u1245\u1218\u12cb\u120d\u1362", "\u2139\ufe0f You already used this code."),
    }.get(result, L(uid, "\u274c \u12ae\u12f1 \u1275\u12ad\u12ad\u120d \u12a0\u12ed\u12f0\u1208\u121d\u1362 \u12a5\u1263\u12ad\u12ce \u12a5\u1295\u12f0\u1308\u1293 \u12ed\u121e\u12ad\u1229\u1362", "\u274c Invalid code. Please try again."))


# ---------------- referral ----------------
def set_referrer(uid, inviter_raw):
    try:
        inv = int(inviter_raw)
    except (TypeError, ValueError):
        return
    if inv == int(uid):
        return
    with closing(db()) as con:
        if con.execute("SELECT 1 FROM users WHERE user_id=?", (inv,)).fetchone() is None:
            return
        con.execute("UPDATE users SET referred_by=? WHERE user_id=? AND referred_by IS NULL", (inv, int(uid)))
        con.commit()


def referral_stats(uid):
    with closing(db()) as con:
        r = con.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(CASE WHEN referral_paid>0 THEN 1 ELSE 0 END),0) AS paid, "
            "COALESCE(SUM(referral_paid),0) AS earned FROM users WHERE referred_by=?", (int(uid),)).fetchone()
        return r["n"], r["paid"], r["earned"]


def announce_recipients(audience):
    with closing(db()) as con:
        if audience == "players":
            rows = con.execute("SELECT DISTINCT user_id FROM round_players").fetchall()
        else:
            rows = con.execute("SELECT user_id FROM users").fetchall()
    return [int(r[0]) for r in rows]


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
  <div class="brand-info"><div class="eyebrow">&#x26A1; &#x1260;&#x123D; &#x1260;&#x123D; &#x1262;&#x1295;&#x130E; &#xB7; v11</div><h1 id="round-title">&#x12D9;&#x122D; #1</h1></div>
  <div class="top-scores">
    <div class="score-box"><span>PLAY</span><strong id="play-bal">0.00</strong></div>
    <div class="score-box win"><span>WIN</span><strong id="win-bal">0.00</strong></div>
    <button id="sound-btn" class="sound-btn" title="Sound">&#x1F50A;</button>
    <div class="current-call-badge" id="current">&#x2014;</div><div class="call-count" id="call-count"></div>
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
    <button id="show-deposit" class="primary-btn btn-deposit active">&#x1F4E5; DEPOSIT</button>
    <button id="show-withdraw" class="primary-btn btn-withdraw">&#x1F4E4; WITHDRAW</button>
  </div>
  <div id="deposit-form" class="sub-form">
    <h3>&#x1308;&#x1295;&#x12D8;&#x1265; &#x121B;&#x1235;&#x1308;&#x1263;&#x1275;</h3>
    <div class="pay-box">
      <div>&#x1F4DE; <b id="pay-number">0928564065</b> <button id="copy-pay" class="copy-btn" type="button">&#x1245;&#x12F3;</button></div>
      <div>&#x1F464; &#x1235;&#x121D;: <b id="pay-name">Eineyew</b> &#xB7; Telebirr</div>
    </div>
    <div id="bonus-banner" class="bonus-banner" style="display:none"></div>
    <input id="deposit-amount" type="number" placeholder="&#x1235;&#x1295;&#x1275; &#x1265;&#x122D;? (&#x1262;&#x12EB;&#x1295;&#x1235; 20)">
    <input id="deposit-reference" placeholder="Transaction ID &#x12C8;&#x12ED;&#x121D; &#x12E8; SMS &#x133D;&#x1201;&#x134D;">
    <div class="hint">1. &#x12A8;&#x120B;&#x12ED; &#x12C8;&#x12F3;&#x1208;&#x12CD; &#x1241;&#x1325;&#x122D; &#x1260; Telebirr &#x12ED;&#x12AD;&#x1348;&#x1209;&#x1362; 2. &#x12A8; Telebirr &#x12E8;&#x12F0;&#x1228;&#x1230;&#x12CE;&#x1275;&#x1295; &#x12E8;&#x130D;&#x1265;&#x12ED;&#x1275; &#x1241;&#x1325;&#x122D; (Transaction ID) &#x12C8;&#x12ED;&#x121D; &#x12E8; SMS &#x133D;&#x1201;&#x1349;&#x1295; &#x1219;&#x1209; &#x12EB;&#x1235;&#x1308;&#x1261;&#x1362; &#x12AD;&#x134D;&#x12EB;&#x12CD; &#x1232;&#x1228;&#x130B;&#x1308;&#x1325; &#x12C8;&#x12F2;&#x12EB;&#x12CD;&#x1291; &#x12C8;&#x12F0; &#x1202;&#x1233;&#x1265;&#x12CE; &#x12ED;&#x1328;&#x1218;&#x122B;&#x120D;&#x1362; &#x12DD;&#x1245;&#x1270;&#x129B;&#x12CD; 20 &#x1265;&#x122D; &#x1290;&#x12CD;&#x1364; &#x12EB;&#x1235;&#x1308;&#x1261;&#x1275; &#x1265;&#x122D; &#x1270;&#x1218;&#x120B;&#x123D; &#x12A0;&#x12ED;&#x12F0;&#x1228;&#x130D;&#x121D;&#x1362;</div>
    <button id="send-deposit" class="primary-btn btn-deposit">&#x1325;&#x12EB;&#x1244; &#x120B;&#x12AD;</button>
  </div>
  <div id="withdraw-form" class="sub-form" style="display:none;">
    <h3>&#x1308;&#x1295;&#x12D8;&#x1265; &#x121B;&#x12CD;&#x1323;&#x1275;</h3>
    <input id="withdraw-account" placeholder="&#x12E8;&#x12A0;&#x12AB;&#x12CD;&#x1295;&#x1275; &#x1241;&#x1325;&#x122D; &#x12C8;&#x12ED;&#x121D; &#x1235;&#x120D;&#x12AD; &#x12A5;&#x1293; &#x1235;&#x121D;">
    <input id="withdraw-amount" type="number" placeholder="&#x1218;&#x1320;&#x1295; (&#x1265;&#x122D;)">
    <div class="hint">&#x12DD;&#x1245;&#x1270;&#x129B;&#x12CD; &#x12E8;&#x121B;&#x12CD;&#x132B; &#x1218;&#x1320;&#x1295; 100 &#x1265;&#x122D; &#x1290;&#x12CD;&#x1362; &#x12E8;&#x121A;&#x12C8;&#x1323;&#x12CD; &#x12A8;&#x12A0;&#x1238;&#x1293;&#x134A;&#x1290;&#x1275; (Win) &#x1202;&#x1233;&#x1265; &#x1265;&#x127B; &#x1290;&#x12CD;&#x1362;</div>
    <button id="send-withdraw" class="primary-btn btn-withdraw">&#x12E8;&#x121B;&#x12CD;&#x1323;&#x1275; &#x1325;&#x12EB;&#x1244; &#x120B;&#x12AD;</button>
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
  <button class="nav-btn" id="nav-rules" onclick="openRules()">&#x1F4DC; &#x1205;&#x130D;</button>
</nav>
</main>
<div id="win-overlay" class="win-overlay" style="display:none"><div class="win-box"><div class="win-title">&#x1F389; BINGO! &#x1F389;</div><div id="win-names" class="win-line"></div><div id="win-prize" class="win-line"></div><div id="win-call" class="win-line"></div><div id="win-next" class="win-sub"></div></div></div>
<div id="rules-overlay" class="rules-overlay" style="display:none">
  <div class="rules-sheet">
    <div class="rules-head"><button id="rules-close" class="rules-close">&#x12DD;&#x130B;</button><div class="rules-title">&#x12E8;&#x1328;&#x12CB;&#x1273;&#x12CD; &#x1218;&#x1218;&#x122A;&#x12EB; &#x12A5;&#x1293; &#x12F0;&#x1295;&#x1265;</div></div>
    <div class="rules-body">
      <div class="rules-card">
        <h3>&#x1260;&#x123D; &#x1260;&#x123D; &#x1262;&#x1295;&#x130E; &#x2014; &#x12E8;&#x1270;&#x1320;&#x1243;&#x121A; &#x1218;&#x1218;&#x122A;&#x12EB; &#x12A5;&#x1293; &#x12E8;&#x1235;&#x121D;&#x121D;&#x1290;&#x1275; &#x12CD;&#x120D;</h3>
        <p>&#x12A5;&#x1295;&#x12B3;&#x1295; &#x12C8;&#x12F0; &#x1260;&#x123D; &#x1260;&#x123D; &#x1262;&#x1295;&#x130E; &#x1260;&#x12F0;&#x1205;&#x1293; &#x1218;&#x1321;! &#x1328;&#x12CB;&#x1273;&#x12CD;&#x1295; &#x12A8;&#x1218;&#x1300;&#x1218;&#x122D;&#x12CE; &#x1260;&#x134A;&#x1275; &#x12A5;&#x1290;&#x12DA;&#x1205;&#x1295; &#x12F0;&#x1295;&#x1266;&#x127D; &#x1260;&#x1325;&#x1295;&#x1243;&#x1244; &#x12EB;&#x1295;&#x1265;&#x1261;&#x1362; &#x1218;&#x132B;&#x12C8;&#x1275;&#x12CE;&#x1295; &#x1260;&#x1218;&#x1240;&#x1320;&#x120D;&#x12CE; &#x1260;&#x1201;&#x1209;&#x121D; &#x12F0;&#x1295;&#x1266;&#x127D; &#x1270;&#x1235;&#x121B;&#x121D;&#x1270;&#x12CB;&#x120D; &#x121B;&#x1208;&#x1275; &#x1290;&#x12CD;&#x1362;</p>
        <h4>1. &#x12E8;&#x1328;&#x12CB;&#x1273;&#x12CD; &#x1263;&#x1205;&#x122A; &#x12A5;&#x1293; &#x12A0;&#x1220;&#x122B;&#x122D;</h4>
        <ul>
          <li><b>&#x1219;&#x1209; &#x1260;&#x1219;&#x1209; &#x12E8;&#x12D5;&#x12F5;&#x120D; &#x1328;&#x12CB;&#x1273;&#x1366;</b> &#x1241;&#x1325;&#x122E;&#x1279; &#x1260;&#x12D8;&#x1348;&#x1240;&#x12F0; (&#x1260;&#x12D5;&#x1323;) &#x12ED;&#x1320;&#x122B;&#x1209;&#x1362; &#x1235;&#x1208;&#x12DA;&#x1205; &#x12E8;&#x1275;&#x129B;&#x12CD;&#x1295;&#x121D; &#x12D9;&#x122D; &#x12A5;&#x1295;&#x12F0;&#x121A;&#x12EB;&#x1238;&#x1295;&#x1349; &#x121D;&#x1295;&#x121D; &#x12CB;&#x1235;&#x1275;&#x1293; &#x12E8;&#x1208;&#x121D;&#x1362;</li>
          <li><b>&#x12A5;&#x12EB;&#x1295;&#x12F3;&#x1295;&#x12F1; &#x12D9;&#x122D; &#x1208;&#x1265;&#x127B;&#x12CD; &#x1290;&#x12CD;&#x1366;</b> &#x12A0;&#x1295;&#x12F5; &#x12D9;&#x122D; &#x12A8;&#x1240;&#x12F0;&#x1218;&#x12CD; &#x12D9;&#x122D; &#x130B;&#x122D; &#x130D;&#x1295;&#x1299;&#x1290;&#x1275; &#x12E8;&#x1208;&#x12CD;&#x121D;&#x1362; &#x1270;&#x12A8;&#x1273;&#x1273;&#x12ED; &#x123D;&#x1295;&#x1348;&#x1275; &#x1240;&#x1323;&#x12E9;&#x1295; &#x12D9;&#x122D; &#x121B;&#x1238;&#x1290;&#x134D; &#x12EB;&#x1235;&#x1308;&#x129B;&#x120D; &#x1265;&#x120E; &#x121B;&#x1230;&#x1265; &#x1235;&#x1205;&#x1270;&#x1275; &#x1290;&#x12CD;&#x1362;</li>
          <li><b>&#x12AB;&#x122D;&#x12F5; &#x1218;&#x121D;&#x1228;&#x1325;&#x1366;</b> &#x12A8;1 &#x12A5;&#x1235;&#x12A8; 600 &#x12AB;&#x122D;&#x12F5; &#x1218;&#x121D;&#x1228;&#x1325; &#x12ED;&#x127B;&#x120B;&#x120D;&#x1364; &#x1262;&#x1260;&#x12DB; 4 &#x12AB;&#x122D;&#x12F5; &#x12ED;&#x1348;&#x1240;&#x12F3;&#x120D;&#x1362; &#x12E8;&#x12A0;&#x1295;&#x12F5; &#x12AB;&#x122D;&#x12F5; &#x12CB;&#x130B; 10 &#x1265;&#x122D; &#x1290;&#x12CD;&#x1362; &#x12E8;&#x1218;&#x121D;&#x1228;&#x132B;&#x12CD; &#x130A;&#x12DC; 50 &#x1230;&#x12A8;&#x1295;&#x12F5; &#x1232;&#x1206;&#x1295; &#x12A8;&#x12DA;&#x12EB; &#x1260;&#x128B;&#x120B; &#x12D9;&#x1229; &#x12ED;&#x1300;&#x121D;&#x122B;&#x120D;&#x1362;</li>
          <li><b>&#x12E8;&#x1202;&#x1233;&#x1265; &#x12A0;&#x12A8;&#x134B;&#x1348;&#x120D;&#x1366;</b> &#x1208;&#x12AB;&#x122D;&#x12F5; &#x12AD;&#x134D;&#x12EB; &#x1218;&#x1300;&#x1218;&#x122A;&#x12EB; &#x12E8;&#x1218;&#x132B;&#x12C8;&#x127B; (Play) &#x1202;&#x1233;&#x1265;&#x12CE; &#x12ED;&#x1240;&#x1290;&#x1233;&#x120D;&#x1364; &#x12A5;&#x1231; &#x1232;&#x12EB;&#x120D;&#x1245; &#x12A8;&#x12A0;&#x1238;&#x1293;&#x134A;&#x1290;&#x1275; (Win) &#x1202;&#x1233;&#x1265;&#x12CE; &#x12ED;&#x1240;&#x1290;&#x1233;&#x120D;&#x1362;</li>
          <li><b>&#x12A0;&#x1238;&#x1293;&#x134A; &#x1218;&#x1208;&#x12E8;&#x1275;&#x1366;</b> &#x121B;&#x1238;&#x1290;&#x134A;&#x12EB;&#x12CD;&#x1295; &#x1328;&#x12CB;&#x1273;&#x12CD; &#x1260;&#x122B;&#x1231; &#x12EB;&#x12CD;&#x1243;&#x120D;&#x1362; &#x12E8;&#x12A0;&#x1238;&#x1293;&#x134A;&#x12CD; &#x1235;&#x121D;&#x1363; &#x12E8;&#x12AB;&#x122D;&#x12F5; &#x1241;&#x1325;&#x122D; &#x12A5;&#x1293; &#x123D;&#x120D;&#x121B;&#x1275; &#x12C8;&#x12F2;&#x12EB;&#x12CD;&#x1291; &#x1208;&#x1201;&#x1209;&#x121D; &#x12ED;&#x1273;&#x12EB;&#x120D;&#x1362; &#x1265;&#x12D9; &#x1270;&#x132B;&#x12CB;&#x127E;&#x127D; &#x1260;&#x12A0;&#x1295;&#x12F5; &#x130A;&#x12DC; &#x12AB;&#x1238;&#x1290;&#x1349; &#x123D;&#x120D;&#x121B;&#x1271; &#x1260;&#x12A5;&#x12A9;&#x120D; &#x12ED;&#x12AB;&#x1348;&#x120B;&#x120D;&#x1362;</li>
        </ul>
        <h4>2. &#x12E8;&#x12A0;&#x12AB;&#x12CD;&#x1295;&#x1275; &#x12A5;&#x1293; &#x12E8;&#x12AD;&#x134D;&#x12EB; &#x12A0;&#x1235;&#x1270;&#x12F3;&#x12F0;&#x122D;</h4>
        <ul>
          <li><b>&#x1270;&#x1218;&#x120B;&#x123D; &#x12E8;&#x121B;&#x12ED;&#x12F0;&#x1228;&#x130D; &#x1308;&#x1262;&#x1366;</b> &#x12C8;&#x12F0; &#x1260;&#x123D; &#x1260;&#x123D; &#x1262;&#x1295;&#x130E; &#x1202;&#x1233;&#x1265;&#x12CE; &#x12E8;&#x1308;&#x1263; &#x121B;&#x1295;&#x129B;&#x12CD;&#x121D; &#x1308;&#x1295;&#x12D8;&#x1265; &#x1208;&#x1328;&#x12CB;&#x1273; &#x12E8;&#x121A;&#x12CD;&#x120D; &#x1290;&#x12CD;&#x1362; &#x12A0;&#x1295;&#x12F4; &#x12A8;&#x1270;&#x121E;&#x120B; &#x1260;&#x128B;&#x120B; &#x1308;&#x1262;&#x12CD; &#x1270;&#x1218;&#x120B;&#x123D; &#x12A0;&#x12ED;&#x12F0;&#x1228;&#x130D;&#x121D;&#x1362;</li>
          <li><b>&#x1308;&#x1295;&#x12D8;&#x1265; &#x121B;&#x12CD;&#x1323;&#x1275;&#x1366;</b> &#x121B;&#x12CD;&#x1323;&#x1275; &#x12E8;&#x121A;&#x127B;&#x1208;&#x12CD; &#x12A8;&#x12A0;&#x1238;&#x1293;&#x134A;&#x1290;&#x1275; (Win) &#x1202;&#x1233;&#x1265; &#x1265;&#x127B; &#x1290;&#x12CD;&#x1362; &#x12DD;&#x1245;&#x1270;&#x129B;&#x12CD; &#x12E8;&#x121B;&#x12CD;&#x132B; &#x1218;&#x1320;&#x1295; 100 &#x1265;&#x122D; &#x1290;&#x12CD;&#x1362; &#x1325;&#x12EB;&#x1244;&#x12CE; &#x1260;&#x12A0;&#x1235;&#x1270;&#x12F3;&#x12F3;&#x122A;&#x12CD; &#x12A8;&#x1270;&#x1228;&#x130B;&#x1308;&#x1320; &#x1260;&#x128B;&#x120B; &#x12ED;&#x12A8;&#x1348;&#x120B;&#x120D;&#x1362;</li>
          <li><b>&#x1275;&#x12AD;&#x12AD;&#x1208;&#x129B; &#x12E8;&#x130D;&#x1265;&#x12ED;&#x1275; &#x1241;&#x1325;&#x122D;&#x1366;</b> &#x1308;&#x1295;&#x12D8;&#x1265; &#x1232;&#x12EB;&#x1235;&#x1308;&#x1261; &#x12A8;&#x12F0;&#x1228;&#x1230;&#x1299; &#x120B;&#x12ED; &#x1275;&#x12AD;&#x12AD;&#x1208;&#x129B;&#x12CD;&#x1295; &#x12E8;&#x130D;&#x1265;&#x12ED;&#x1275; &#x1241;&#x1325;&#x122D; (Transaction ID) &#x12EB;&#x1235;&#x1308;&#x1261;&#x1362; &#x1210;&#x1230;&#x1270;&#x129B; &#x12C8;&#x12ED;&#x121D; &#x1240;&#x12F0;&#x121D; &#x1232;&#x120D; &#x1325;&#x1245;&#x121D; &#x120B;&#x12ED; &#x12E8;&#x12CB;&#x1208; &#x1241;&#x1325;&#x122D; &#x12CD;&#x12F5;&#x1245; &#x12ED;&#x12F0;&#x1228;&#x130B;&#x120D;&#x1362;</li>
        </ul>
        <h4>3. &#x12E8;&#x12D5;&#x12F5;&#x121C; &#x1308;&#x12F0;&#x1265;</h4>
        <ul>
          <li><b>&#x12A8;21 &#x12D3;&#x1218;&#x1275; &#x1260;&#x1273;&#x127D; &#x12AD;&#x120D;&#x12AD;&#x120D; &#x1290;&#x12CD;&#x1366;</b> &#x12ED;&#x1205; &#x1328;&#x12CB;&#x1273; &#x12D5;&#x12F5;&#x121C;&#x12EB;&#x1278;&#x12CD; &#x12A8;21 &#x12D3;&#x1218;&#x1275; &#x1260;&#x1273;&#x127D; &#x1208;&#x1206;&#x1291; &#x1230;&#x12CE;&#x127D; &#x12A0;&#x12ED;&#x1348;&#x1240;&#x12F5;&#x121D;&#x1362; &#x1218;&#x132B;&#x12C8;&#x1275;&#x12CE;&#x1295; &#x1260;&#x1218;&#x1240;&#x1320;&#x120D;&#x12CE; &#x12D5;&#x12F5;&#x121C;&#x12CE; 21 &#x12D3;&#x1218;&#x1275; &#x12C8;&#x12ED;&#x121D; &#x12A8;&#x12DA;&#x12EB; &#x1260;&#x120B;&#x12ED; &#x1218;&#x1206;&#x1291;&#x1295; &#x12EB;&#x1228;&#x130B;&#x130D;&#x1323;&#x1209;&#x1362;</li>
        </ul>
        <h4>4. &#x1283;&#x120B;&#x134A;&#x1290;&#x1275; &#x12EB;&#x1208;&#x1260;&#x1275; &#x1328;&#x12CB;&#x1273;</h4>
        <ul>
          <li><b>&#x1208;&#x1218;&#x12DD;&#x1293;&#x129B; &#x1265;&#x127B;&#x1366;</b> &#x12ED;&#x1205; &#x1328;&#x12CB;&#x1273; &#x1208;&#x1218;&#x12DD;&#x1293;&#x1293;&#x1275; &#x1265;&#x127B; &#x12E8;&#x1270;&#x12D8;&#x130B;&#x1300; &#x1290;&#x12CD;&#x1362; &#x12AA;&#x1235;&#x12CE;&#x1295; &#x1260;&#x121B;&#x12ED;&#x130E;&#x12F3; &#x1218;&#x1320;&#x1295; &#x1265;&#x127B; &#x12ED;&#x132B;&#x12C8;&#x1271;&#x1362; &#x1208;&#x12D5;&#x1208;&#x1275; &#x1270;&#x12D5;&#x1208;&#x1275; &#x1291;&#x122E;&#x12CE; &#x12E8;&#x121A;&#x12EB;&#x1235;&#x1348;&#x120D;&#x130D;&#x12CE;&#x1275;&#x1295; &#x12C8;&#x12ED;&#x121D; &#x12E8;&#x1270;&#x1260;&#x12F0;&#x1229;&#x1275;&#x1295; &#x1308;&#x1295;&#x12D8;&#x1265; &#x1260;&#x134D;&#x1339;&#x121D; &#x12A0;&#x12ED;&#x132B;&#x12C8;&#x1271;&#x1362; &#x1328;&#x12CB;&#x1273;&#x12CD; &#x1260;&#x1308;&#x1295;&#x12D8;&#x1265;&#x12CE; &#x12C8;&#x12ED;&#x121D; &#x1260;&#x1205;&#x12ED;&#x12C8;&#x1275;&#x12CE; &#x120B;&#x12ED; &#x1309;&#x12F3;&#x1275; &#x121B;&#x121D;&#x1323;&#x1275; &#x12A8;&#x1300;&#x1218;&#x1228; &#x12EB;&#x1241;&#x1219;&#x1362;</li>
        </ul>
      </div>
      <div class="rules-card">
        <h3 class="gold">&#x1F3C6; &#x121B;&#x1238;&#x1290;&#x134A;&#x12EB; &#x1218;&#x1295;&#x1308;&#x12F6;&#x127D;</h3>
        <p class="sub">&#x12A8;&#x1273;&#x127D; &#x12AB;&#x1209;&#x1275; &#x1245;&#x122D;&#x133E;&#x127D; &#x12CD;&#x1235;&#x1325; &#x12A0;&#x1295;&#x12F1;&#x1295; &#x1232;&#x12EB;&#x121F;&#x1209; &#x12EB;&#x1238;&#x1295;&#x134B;&#x1209;&#x1362; &#x1328;&#x12CB;&#x1273;&#x12CD; &#x1262;&#x1295;&#x130E;&#x12CD;&#x1295; &#x1260;&#x122B;&#x1231; &#x12EB;&#x12CD;&#x1243;&#x120D;&#x1362;</p>
        <div class="pat-wrap" id="pat-wrap"></div>
      </div>
    </div>
    <button id="rules-ok" class="rules-ok">&#x1270;&#x1228;&#x12F3;&#x1201;</button>
  </div>
</div>
<div id="toast"></div><script src="/static/app.js"></script></body></html>"""

ADMIN_HTML = r"""<!doctype html><html lang="am"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>&#x1260;&#x123D; &#x1260;&#x123D; &#x1262;&#x1295;&#x130E; Admin</title><script src="https://telegram.org/js/telegram-web-app.js"></script><link rel="stylesheet" href="/static/style.css"><style>.admin{max-width:700px;margin:auto;padding:16px}.req{background:#0b2113;border:1px solid #20552d;padding:12px;border-radius:10px;margin-bottom:10px}.req span{display:block;font-size:11px;color:#a3b8a8}.req p{margin:4px 0;font-size:13px;line-height:1.5}.stat-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}.req details{margin-top:6px}.req .pl{font-size:12px;padding:3px 0;border-bottom:1px solid #20552d}</style></head>
<body class="admin"><header><h1>&#x1F6E1; &#x1260;&#x123D; &#x1260;&#x123D; &#x1262;&#x1295;&#x130E; &#x12E8;&#x12A0;&#x1235;&#x1270;&#x12F3;&#x12F3;&#x122A; &#x1353;&#x1290;&#x120D;</h1><button id="refresh" class="primary-btn">&#x27F3; &#x12A0;&#x12F5;&#x1235;</button></header>
<section style="display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin:15px 0">
  <div class="req"><span>&#x1270;&#x132B;&#x12CB;&#x127E;&#x127D;</span><strong id="u-count">&#x2014;</strong></div>
  <div class="req"><span>&#x1320;&#x1245;&#x120B;&#x120B; &#x1265;&#x122D;</span><strong id="t-cred">&#x2014;</strong></div>
  <div class="req"><span>&#x1260;&#x1218;&#x1320;&#x1263;&#x1260;&#x1245; &#x120B;&#x12ED;</span><strong id="p-count">&#x2014;</strong></div>
</section>
<h2>&#x1F534; &#x12A0;&#x1201;&#x1295; &#x1260;&#x1328;&#x12CB;&#x1273; &#x120B;&#x12ED;</h2><div class="req" id="live-box">&#x1260;&#x1218;&#x132B;&#x1295; &#x120B;&#x12ED;...</div>
<h2>&#x1F4CA; &#x1235;&#x1273;&#x1272;&#x1235;&#x1272;&#x12AD;&#x1235;</h2><div class="stat-grid" id="stats-box"></div>
<h2>&#x1F558; &#x12E8;&#x130C;&#x121D; &#x1202;&#x1235;&#x1275;&#x122A;</h2><div id="hist-list">&#x1260;&#x1218;&#x132B;&#x1295; &#x120B;&#x12ED;...</div>
<h2>&#x12E8;&#x1308;&#x1295;&#x12D8;&#x1265; &#x121B;&#x1235;&#x1308;&#x1262;&#x12EB; &#x1325;&#x12EB;&#x1244;&#x12CE;&#x127D;</h2><div id="req-list">&#x1260;&#x1218;&#x132B;&#x1295; &#x120B;&#x12ED;...</div><h2>&#x12E8;&#x121B;&#x12CD;&#x1323;&#x1275; &#x1325;&#x12EB;&#x1244;&#x12CE;&#x127D;</h2><div id="wd-list">&#x1260;&#x1218;&#x132B;&#x1295; &#x120B;&#x12ED;...</div><h2>&#x1F4F2; SMS &#x122B;&#x1235;-&#x121B;&#x133D;&#x12F0;&#x1245;</h2><div class="req" id="sms-box">...</div>
<h2>&#x1F381; &#x12E8;&#x1266;&#x1290;&#x1235; &#x1205;&#x130E;&#x127D; (&#x12E8;&#x12A2;&#x1275;&#x12EE;&#x1335;&#x12EB; &#x1230;&#x12D3;&#x1275;)</h2><div id="rules-box"></div><button id="add-rule" class="primary-btn">&#x2795; &#x12A0;&#x12F2;&#x1235; &#x1205;&#x130D;</button>
<h2>&#x1F3F7; &#x1355;&#x122E;&#x121E; &#x12AE;&#x12F6;&#x127D;</h2>
<div class="rule-card"><div class="grid2"><input type="text" id="pc-code" placeholder="&#x12AE;&#x12F5;"><input type="number" id="pc-reward" placeholder="&#x123D;&#x120D;&#x121B;&#x1275; (&#x1265;&#x122D;)"></div>
<div class="grid2"><input type="number" id="pc-max" placeholder="&#x12A8;&#x134D;&#x1270;&#x129B; &#x1270;&#x1320;&#x1243;&#x121A; &#x1265;&#x12DB;&#x1275; (0 = &#x12EB;&#x120D;&#x1270;&#x12C8;&#x1230;&#x1290;)"><input type="date" id="pc-exp" title="&#x12E8;&#x121A;&#x12EB;&#x1260;&#x1243;&#x1260;&#x1275; &#x1240;&#x1295;"></div>
<button id="pc-save" class="primary-btn">&#x1F4BE; &#x12AE;&#x12F1;&#x1295; &#x12A0;&#x1235;&#x1240;&#x121D;&#x1325;</button></div><div id="promo-list"></div>
<h2>&#x1F517; &#x122A;&#x1348;&#x122B;&#x120D;</h2><div id="ref-box"></div>
<script src="/static/admin.js"></script></body></html>"""

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
.btn-deposit{background:linear-gradient(135deg,#39d66f,#1fa44f);color:#041509;border:1px solid #39d66f}
.btn-withdraw{background:linear-gradient(135deg,#ffe629,#e6b800);color:#241a00;border:1px solid #ffe629}
.wallet-buttons-row{grid-template-columns:1fr 1fr}
.wallet-buttons-row button{opacity:.6;transition:opacity .15s,box-shadow .15s,transform .1s}
.wallet-buttons-row button.active{opacity:1;transform:scale(1.03)}
.wallet-buttons-row .btn-deposit.active{box-shadow:0 0 0 2px #39d66f99,0 0 16px #39d66f66}
.wallet-buttons-row .btn-withdraw.active{box-shadow:0 0 0 2px #ffe62999,0 0 16px #ffe62966}
.hint{font-size:11px;color:var(--muted);margin:-2px 0 8px;line-height:1.5}
.rules-overlay{position:fixed;inset:0;background:rgba(0,0,0,.72);z-index:3000;display:flex;align-items:flex-end;justify-content:center}
.rules-sheet{background:#06140b;width:100%;max-width:520px;height:90vh;border-radius:18px 18px 0 0;border:1px solid var(--line);display:flex;flex-direction:column;overflow:hidden}
.rules-head{display:flex;align-items:center;gap:10px;padding:14px;border-bottom:1px solid var(--line)}
.rules-close{background:0;border:0;color:var(--muted);font-size:14px;font-weight:700;cursor:pointer;padding:4px 6px}
.rules-title{flex:1;text-align:center;font-weight:800;font-size:15px;margin-right:36px}
.rules-body{flex:1;overflow-y:auto;-webkit-overflow-scrolling:touch;padding:12px}
.rules-card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:14px;margin-bottom:12px;font-size:13px;line-height:1.7}
.rules-card h3{margin:0 0 8px;font-size:15px;line-height:1.5}
.rules-card h3.gold{color:var(--gold)}
.rules-card h4{margin:14px 0 6px;color:var(--teal);font-size:14px}
.rules-card p{margin:0 0 6px}.rules-card p.sub{color:var(--muted);font-size:12px}
.rules-card ul{margin:0;padding-left:18px}.rules-card li{margin-bottom:8px}
.pat-wrap{display:grid;grid-template-columns:1fr 1fr;gap:14px;justify-items:center;margin-top:10px}
.pat-label{text-align:center;font-weight:800;font-size:12px;margin-bottom:6px}
.pat-grid{display:grid;grid-template-columns:repeat(5,1fr);gap:3px;width:96px}
.pat-cell{aspect-ratio:1;background:#12301c;border-radius:3px}
.pat-cell.on{background:var(--teal);box-shadow:0 0 6px #39d66f99}
.rules-ok{margin:10px 12px 14px;padding:14px;border:0;border-radius:12px;background:linear-gradient(135deg,#39d66f,#1fa44f);color:#041509;font-weight:900;font-size:15px;cursor:pointer}
.pay-box{background:#06180d;border:1px solid var(--line);border-radius:10px;padding:10px 12px;margin:8px 0;line-height:1.7;font-size:14px}
.pay-box b{color:var(--gold);letter-spacing:.5px}
.copy-btn{margin-left:8px;border:0;border-radius:6px;padding:3px 10px;background:var(--teal);color:#041509;font-weight:800;cursor:pointer}
.bonus-banner{margin:8px 0;padding:9px 12px;border-radius:10px;background:linear-gradient(135deg,#ffe629,#f5a300);color:#2a1d00;font-weight:900;font-size:14px;text-align:center}
.rule-card{border:1px solid var(--line);border-radius:10px;padding:10px;margin:8px 0;background:var(--panel)}
.rule-card input[type=text],.rule-card input[type=number],.rule-card input[type=time],.rule-card input[type=date]{width:100%;margin:3px 0}
.rule-card .grid2{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.rule-card label.d{display:inline-block;margin:2px 6px 2px 0;font-size:13px}
.eth{color:var(--gold);font-size:12px}
.call-count{text-align:center;font-size:12px;font-weight:800;color:var(--gold);margin-top:4px}
.rule-card input[type=checkbox]{width:auto;min-width:0;height:auto;margin:0 4px;padding:0;vertical-align:middle}
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

// The owner's own recorded voice (static/voice/B1.mp3 ... O75.mp3); falls back to the synthetic voice if a file is missing.
const voiceCache = {};
function voiceClip(key) {
  let a = voiceCache[key];
  if (!a) { a = new Audio('/static/voice/' + key + '.mp3'); a.preload = 'auto'; voiceCache[key] = a; }
  return a;
}
function preloadVoices() {
  for (let n = 1; n <= 75; n++) {
    const key = LETTERS[Math.min(4, Math.floor((n - 1) / 15))] + n;
    setTimeout(() => { try { voiceClip(key); } catch (e) {} }, n * 80);
  }
}
setTimeout(preloadVoices, 2500);
function speakNumber(n) {
  if (!n || n === lastSpokenNum) return;
  lastSpokenNum = n;
  if (!soundOn) return;
  const letter = LETTERS[Math.min(4, Math.floor((n - 1) / 15))];
  const fallback = () => { beep(880, 0.12, 0.2); speak(letter + ', ' + n); };
  try {
    const a = voiceClip(letter + n);
    a.currentTime = 0;
    const p = a.play();
    if (p && p.catch) p.catch(fallback);
  } catch (e) { fallback(); }
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
  if (model.pay_number) {
    document.getElementById('pay-number').textContent = model.pay_number;
    document.getElementById('pay-name').textContent = model.pay_name;
  }
  const bb = document.getElementById('bonus-banner');
  if (bb) {
    if (model.bonus_percent > 0) {
      bb.style.display = 'block';
      bb.textContent = '\uD83C\uDF81 \u12A0\u1201\u1295 \u1266\u1290\u1235: ' + model.bonus_percent + '%' + (model.bonus_cap > 0 ? ' (\u12A5\u1235\u12A8 ' + model.bonus_cap + ' \u1265\u122D)' : '');
    } else { bb.style.display = 'none'; }
  }
  document.getElementById('current').textContent = model.current_number ?? '\u2014';
  const calls = (model.called_numbers || []).length;
  document.getElementById('call-count').textContent = (model.phase === 'playing' || model.phase === 'finished') && calls ? ('\u1325\u122A ' + calls + '/75') : '';

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
    document.getElementById('win-call').textContent = '\u1260 ' + (model.called_numbers || []).length + '\u129B \u1325\u122A \u1270\u12D8\u130B';
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
function walletView(which) {
  const dep = which === 'deposit';
  document.getElementById('deposit-form').style.display = dep ? 'block' : 'none';
  document.getElementById('withdraw-form').style.display = dep ? 'none' : 'block';
  document.getElementById('show-deposit').classList.toggle('active', dep);
  document.getElementById('show-withdraw').classList.toggle('active', !dep);
}
document.getElementById('show-deposit').onclick = () => walletView('deposit');
document.getElementById('show-withdraw').onclick = () => walletView('withdraw');
document.getElementById('copy-pay').onclick = () => {
  const n = document.getElementById('pay-number').textContent;
  try { navigator.clipboard.writeText(n).then(() => toast('\u1270\u1240\u12F5\u1277\u120D'), () => toast(n)); } catch (e) { toast(n); }
};
document.getElementById('send-deposit').onclick = () => {
  const amount = Number(document.getElementById('deposit-amount').value);
  const reference = document.getElementById('deposit-reference').value.trim();
  if (!amount || reference.length < 3) { toast('\u12A5\u1263\u12AD\u12CE \u1218\u1228\u1303\u12CD\u1295 \u12ED\u1219\u1209'); return; }
  if (amount < 20) { toast('\u12DD\u1245\u1270\u129B\u12CD \u1308\u1295\u12D8\u1265 \u121B\u1235\u1308\u1262\u12EB 20 \u1265\u122D \u1290\u12CD'); return; }
  action('/api/miniapp/deposit-request', { amount, method: 'Telebirr', reference })
    .then(() => { document.getElementById('deposit-reference').value = ''; });
};
document.getElementById('send-withdraw').onclick = () => {
  const amount = Number(document.getElementById('withdraw-amount').value);
  const account_info = document.getElementById('withdraw-account').value.trim();
  if (amount && amount < 100) { toast('\u12DD\u1245\u1270\u129B\u12CD \u12E8\u121B\u12CD\u132B \u1218\u1320\u1295 100 \u1265\u122D \u1290\u12CD'); return; }
  if (!amount || account_info.length < 3) { toast('\u12A5\u1263\u12AD\u12CE \u1218\u1228\u1303\u12CD\u1295 \u12ED\u1219\u1209'); return; }
  action('/api/miniapp/withdraw-request', { amount, account_info }).then(() => {
    document.getElementById('withdraw-amount').value = '';
    document.getElementById('withdraw-account').value = '';
  });
};


function patGrid(label, on) {
  const w = document.createElement('div'); w.className = 'pat-item';
  const t = document.createElement('div'); t.className = 'pat-label'; t.textContent = label; w.appendChild(t);
  const g = document.createElement('div'); g.className = 'pat-grid';
  for (let r = 0; r < 5; r++) for (let c = 0; c < 5; c++) {
    const d = document.createElement('div'); d.className = 'pat-cell' + (on(r, c) ? ' on' : ''); g.appendChild(d);
  }
  w.appendChild(g); return w;
}
function buildPatterns() {
  const box = document.getElementById('pat-wrap'); if (!box || box.children.length) return;
  box.appendChild(patGrid('1. \u1260\u12A0\u130D\u12F5\u121D', (r, c) => r === 2));
  box.appendChild(patGrid('2. \u12C8\u12F0 \u1273\u127D', (r, c) => c === 2));
  box.appendChild(patGrid('3. \u1260\u1230\u12EB\u134D', (r, c) => r === c));
  box.appendChild(patGrid('4. \u12A0\u122B\u1271 \u1325\u130D', (r, c) => (r === 0 || r === 4) && (c === 0 || c === 4)));
}
window.openRules = function () {
  buildPatterns();
  document.getElementById('rules-overlay').style.display = 'flex';
};
function closeRules(markSeen) {
  document.getElementById('rules-overlay').style.display = 'none';
  if (markSeen) { try { localStorage.setItem('bb_rules_v1', '1'); } catch (e) {} }
}
document.getElementById('rules-close').onclick = () => closeRules(false);
document.getElementById('rules-ok').onclick = () => closeRules(true);
// show the rules once on the first visit (only when the browser can remember that it was shown)
try { if (!localStorage.getItem('bb_rules_v1')) openRules(); } catch (e) {}

refresh(); setInterval(refresh, 1000);
})();"""

ADMIN_JS = r"""const HEADERS = () => ({'Content-Type': 'application/json', 'X-Telegram-Init-Data': window.Telegram?.WebApp?.initData || ''});
function esc(s) { const d = document.createElement('div'); d.textContent = String(s ?? ''); return d.innerHTML; }
const BTN = 'border:0;padding:6px 12px;border-radius:6px;font-weight:bold;margin-right:6px;cursor:pointer;';
function fmtTime(s) { try { const d = new Date(String(s).replace(' ', 'T') + 'Z'); return isNaN(d) ? String(s) : d.toLocaleString(); } catch (e) { return String(s); } }
function birr(x) { return String(Math.round(Number(x || 0) * 100) / 100); }

function renderLive(l) {
  const names = {selecting: '\u12AB\u122D\u12F5 \u1260\u1218\u1218\u1228\u1325 \u120B\u12ED', playing: '\u1328\u12CB\u1273 \u1260\u1218\u12AB\u1204\u12F5 \u120B\u12ED', finished: '\u12A0\u1238\u1293\u134A \u1273\u12CD\u124B\u120D'};
  document.getElementById('live-box').innerHTML =
    `<b>\u12D9\u122D #${Number(l.round)} \u00B7 ${esc(names[l.phase] || l.phase)}</b>` +
    `<p>\uD83D\uDFE2 \u12A0\u1201\u1295 \u12A6\u1295\u120B\u12ED\u1295: <b>${Number(l.online)}</b> \u1230\u12CD</p>` +
    `<p>\uD83C\uDFAE \u1260\u12DA\u1205 \u12D9\u122D \u12E8\u121A\u132B\u12C8\u1271: <b>${Number(l.players)}</b> \u1230\u12CD \u00B7 \uD83C\uDCCF \u12AB\u122D\u12F6\u127D: <b>${Number(l.cards)}</b></p>` +
    `<p>\uD83D\uDCB0 \u12E8\u12D9\u1229 \u1320\u1245\u120B\u120B \u1308\u1295\u12D8\u1265: <b>${birr(l.pool)}</b> \u1265\u122D` + (l.phase === 'selecting' ? ` \u00B7 \u23F1 ${Number(l.selection_left)} \u1230\u12A8\u1295\u12F5` : '') + `</p>`;
}
async function loadLive() {
  try { const r = await fetch('/api/admin/live', {headers: HEADERS()}); if (r.ok) renderLive(await r.json()); } catch (e) {}
}
function statCard(title, o) {
  return `<div class="req"><b>${title}</b>` +
    `<p>\u12D9\u122E\u127D: ${Number(o.rounds)}` + (o.players !== undefined ? ` \u00B7 \u12E8\u1270\u132B\u12C8\u1271 \u1230\u12CE\u127D: ${Number(o.players)}` : '') + `</p>` +
    `<p>\u12E8\u12AB\u122D\u12F5 \u123D\u12EB\u132D: ${birr(o.sales)} \u1265\u122D</p>` +
    `<p>\u12F5\u122D\u123B\u12EC (15%): <b>${birr(o.house)} \u1265\u122D</b></p>` +
    `<p>\u1208\u12A0\u1238\u1293\u134A\u12CE\u127D \u12E8\u1270\u12A8\u1348\u1208: ${birr(o.payout)} \u1265\u122D</p>` +
    `<p>\u12E8\u1308\u1263 \u12F2\u1356\u12DA\u1275 (\u12E8\u1338\u12F0\u1240): ${birr(o.deposits)} \u1265\u122D</p>` +
    (o.withdrawals !== undefined ? `<p>\u12E8\u12C8\u1323 (\u12E8\u1270\u12A8\u1348\u1208): ${birr(o.withdrawals)} \u1265\u122D</p>` : '') + `</div>`;
}
function renderHistory(list) {
  document.getElementById('hist-list').innerHTML = (list || []).map(h => {
    const status = h.status === 'won' ? '\uD83C\uDFC6 \u12A0\u1238\u1293\u134A \u1270\u1308\u129D\u1277\u120D' : '\u21A9\uFE0F \u12A0\u1238\u1293\u134A \u12A0\u120D\u1270\u1308\u1298\u121D (\u1265\u122D \u1270\u1218\u120D\u1237\u120D)';
    return `<div class="req"><b>\u12D9\u122D #${Number(h.round_no)} \u00B7 ${status}</b>` +
      `<p>\uD83D\uDD52 ${esc(fmtTime(h.created_at))}</p>` +
      `<p>\uD83C\uDFAE \u1270\u132B\u12CB\u127E\u127D: ${Number(h.players)} \u00B7 \uD83C\uDCCF \u12AB\u122D\u12F6\u127D: ${Number(h.cards)} \u00B7 \u12E8\u1270\u1320\u1229 \u1241\u1325\u122E\u127D: ${Number(h.called_count)}</p>` +
      `<p>\uD83D\uDCB0 \u1320\u1245\u120B\u120B: ${birr(h.pool)} \u1265\u122D \u00B7 \u12F5\u122D\u123B\u12EC: <b>${birr(h.house_cut)}</b> \u00B7 \u1208\u12A0\u1238\u1293\u134A: ${birr(h.payout)}</p>` +
      (h.winners ? `<p>\uD83C\uDFC6 \u12A0\u1238\u1293\u134A: ${esc(h.winners)}</p>` : '') +
      `<button onclick="showRound(${Number(h.id)})" style="${BTN}background:#102d19;color:#f4f7f4;border:1px solid #20552d">\uD83D\uDC65 \u1270\u132B\u12CB\u127E\u127D\u1295 \u12A0\u1233\u12ED</button>` +
      `<div id="rp-${Number(h.id)}"></div></div>`;
  }).join('') || '\u2014';
}
async function showRound(id) {
  const box = document.getElementById('rp-' + id);
  if (box.innerHTML) { box.innerHTML = ''; return; }
  box.textContent = '...';
  const r = await fetch('/api/admin/round/' + id, {headers: HEADERS()});
  const d = await r.json();
  if (!r.ok) { box.textContent = d.error || 'Error'; return; }
  box.innerHTML = (d.players || []).map(p =>
    `<div class="pl">${esc(p.name)} (ID ${esc(p.user_id)}) \u2014 ${Number(p.cards)} \u12AB\u122D\u12F5 \u00B7 \u12A8\u134D\u120F\u120D ${birr(p.stake)} \u00B7 ` +
    (Number(p.prize) > 0 ? `<b style="color:#ffe629">\uD83C\uDFC6 \u12A0\u1238\u1290\u1348 ${birr(p.prize)} \u1265\u122D</b>` : '\u12A0\u120B\u1238\u1290\u1348\u121D') + `</div>`).join('') || '\u2014';
}

async function load() {
  const r = await fetch('/api/admin/overview', {headers: HEADERS()});
  const d = await r.json();
  if (!r.ok) { document.getElementById('req-list').textContent = d.error || 'Error'; return; }
  document.getElementById('u-count').textContent = d.users_count;
  document.getElementById('t-cred').textContent = d.total_credits;
  document.getElementById('p-count').textContent = d.pending_count;
  renderLive(d.live);
  document.getElementById('stats-box').innerHTML = statCard('\uD83D\uDCC5 \u12DB\u122C', d.stats.today) + statCard('\uD83D\uDCC8 \u1320\u1245\u120B\u120B', d.stats.all);
  renderHistory(d.history);
  document.getElementById('req-list').innerHTML = d.requests.map(q => {
    const buttons = q.status === 'pending'
      ? `<button onclick="review(${Number(q.id)},'approve')" style="${BTN}background:#39d66f">\u12A0\u133D\u12F5\u1245</button>` +
        `<button onclick="review(${Number(q.id)},'reject')" style="${BTN}background:#d63939;color:#fff">\u12CD\u12F5\u1245</button>`
      : `<i>${esc(q.status)}</i>`;
    return `<div class="req"><b>#${Number(q.id)} \u00B7 ${esc(q.amount)} \u1265\u122D (${esc(q.method)})</b>
      <p>User: ${esc(q.user_id)} | Ref: ${esc(q.reference)}</p>${Number(q.bonus) > 0 ? `<p>\uD83C\uDF81 \u1266\u1290\u1235 +${birr(q.bonus)} \u1265\u122D</p>` : ''}${q.approved_via === 'sms' ? '<p>\uD83D\uDCF2 \u122B\u1235-\u1230\u122D (SMS)</p>' : ''}<p>\uD83D\uDD52 ${esc(fmtTime(q.created_at))}</p>${buttons}</div>`;
  }).join('') || '\u2014';
  document.getElementById('wd-list').innerHTML = (d.withdrawals || []).map(q => {
    const buttons = q.status === 'pending'
      ? `<button onclick="reviewWd(${Number(q.id)},'paid')" style="${BTN}background:#39d66f">\u1270\u12A8\u134D\u120F\u120D</button>` +
        `<button onclick="reviewWd(${Number(q.id)},'reject')" style="${BTN}background:#d63939;color:#fff">\u12CD\u12F5\u1245 (\u1265\u1229\u1295 \u1218\u120D\u1235)</button>`
      : `<i>${esc(q.status)}</i>`;
    return `<div class="req"><b>#${Number(q.id)} \u00B7 ${esc(q.amount)} \u1265\u122D</b>
      <p>User: ${esc(q.user_id)} | ${esc(q.account_info)}</p><p>\uD83D\uDD52 ${esc(fmtTime(q.created_at))}</p>${buttons}</div>`;
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
const DAYN = ['\u1230\u129E', '\u121B\u12AD\u1230\u129E', '\u1228\u1261\u12D5', '\u1210\u1219\u1235', '\u12D3\u122D\u1265', '\u1245\u12F3\u121C', '\u12A5\u1201\u12F5'];
function ethTime(min) {
  const h = Math.floor(min / 60) % 24, m = min % 60;
  let eh = ((h - 6) % 12 + 12) % 12; if (eh === 0) eh = 12;
  const p = (h >= 6 && h < 12) ? '\u1320\u12CB\u1275' : (h >= 12 && h < 18) ? '\u12A8\u1240\u1291' : (h >= 18 ? '\u121D\u123D\u1275' : '\u120C\u120A\u1275');
  return p + ' ' + eh + (m ? ':' + String(m).padStart(2, '0') : '') + ' \u1230\u12D3\u1275';
}
function hhmm(min) { return String(Math.floor(min / 60) % 24).padStart(2, '0') + ':' + String(min % 60).padStart(2, '0'); }
function toMin(v, isEnd) { const p = String(v || '0:0').split(':'); const m = (+p[0]) * 60 + (+p[1] || 0); return (isEnd && m === 0) ? 1440 : m; }
async function postJson(path, body) {
  const r = await fetch(path, {method: 'POST', headers: HEADERS(), body: JSON.stringify(body)});
  const d = await r.json().catch(() => ({}));
  if (!r.ok) { alert(d.error || 'Error'); return false; }
  return true;
}
function ethUpd(i) {
  const s = toMin(document.getElementById('r' + i + '-s').value, false), e = toMin(document.getElementById('r' + i + '-e').value, true);
  document.getElementById('r' + i + '-eth').textContent = '\uD83D\uDD52 ' + ethTime(s) + ' \u2192 ' + ethTime(e % 1440);
}
function ruleCard(r, i) {
  const days = String(r.days || '').split(',').filter(x => x !== '').map(Number);
  const dayBoxes = DAYN.map((n, d) => `<label class="d"><input type="checkbox" class="r${i}-d" value="${d}" ${days.includes(d) ? 'checked' : ''}> ${n}</label>`).join('');
  return `<div class="rule-card"><input type="text" id="r${i}-n" placeholder="\u1235\u121D" value="${esc(r.name)}">` +
    `<div>${dayBoxes}<div class="eth">\u121D\u1295\u121D \u1240\u1295 \u12AB\u120D\u1270\u1218\u1228\u1320 \u1260\u1201\u1209\u121D \u1240\u1293\u1275 \u12ED\u1220\u122B\u120D</div></div>` +
    `<div class="grid2"><div>\u12A8 (\u1218\u12F0\u1260\u129B \u1230\u12D3\u1275)<input type="time" id="r${i}-s" value="${hhmm(r.start_min)}" oninput="ethUpd(${i})"></div>` +
    `<div>\u12A5\u1235\u12A8<input type="time" id="r${i}-e" value="${hhmm(r.end_min % 1440)}" oninput="ethUpd(${i})"></div></div><div class="eth" id="r${i}-eth"></div>` +
    `<div class="grid2"><div>\u1266\u1290\u1235 %<input type="number" id="r${i}-p" value="${Number(r.percent)}"></div>` +
    `<div>\u12A8\u134D\u1270\u129B (\u1265\u122D, 0 = \u1308\u12F0\u1265 \u12E8\u1208\u121D)<input type="number" id="r${i}-c" value="${Number(r.cap)}"></div></div>` +
    `<label><input type="checkbox" id="r${i}-a" ${r.active ? 'checked' : ''}> \u1295\u1241</label> ` +
    `<button onclick="saveRule(${i})" style="${BTN}background:#39d66f">\uD83D\uDCBE \u12A0\u1235\u1240\u121D\u1325</button>` +
    (r.id ? `<button onclick="delRule(${i})" style="${BTN}background:#d63939;color:#fff">\uD83D\uDDD1 \u1230\u122D\u12DD</button>` : '') + `</div>`;
}
let RULES = [];
function renderRules(list) {
  RULES = list;
  document.getElementById('rules-box').innerHTML = list.map(ruleCard).join('') || '\u2014';
  list.forEach((r, i) => ethUpd(i));
}
async function saveRule(i) {
  const g = id => document.getElementById('r' + i + '-' + id);
  const days = Array.from(document.querySelectorAll('.r' + i + '-d')).filter(x => x.checked).map(x => Number(x.value));
  const ok = await postJson('/api/admin/bonus-rule', {id: RULES[i].id || null, name: g('n').value, days, start: toMin(g('s').value, false),
    end: toMin(g('e').value, true), percent: Number(g('p').value), cap: Number(g('c').value || 0), active: g('a').checked});
  if (ok) loadSettings();
}
async function delRule(i) { if (confirm('\u12ED\u1205 \u1205\u130D \u12ED\u1230\u1228\u12DD?') && await postJson('/api/admin/bonus-rule', {id: RULES[i].id, delete: true})) loadSettings(); }
document.getElementById('add-rule').onclick = () => {
  RULES.push({id: null, name: '', days: '', start_min: 0, end_min: 1440, percent: 10, cap: 0, active: 1});
  renderRules(RULES);
};
document.getElementById('pc-save').onclick = async () => {
  const ok = await postJson('/api/admin/promo', {code: document.getElementById('pc-code').value, reward: Number(document.getElementById('pc-reward').value),
    max_uses: Number(document.getElementById('pc-max').value || 0), expires: document.getElementById('pc-exp').value});
  if (ok) { ['pc-code', 'pc-reward', 'pc-max', 'pc-exp'].forEach(x => document.getElementById(x).value = ''); loadSettings(); }
};
async function promoAct(code, what) {
  if (what === 'delete' && !confirm('\u12AE\u12F1 \u12ED\u1230\u1228\u12DD?')) return;
  if (await postJson('/api/admin/promo', {code, [what]: true})) loadSettings();
}
function renderPromos(list) {
  document.getElementById('promo-list').innerHTML = list.map(p =>
    `<div class="req"><b>${esc(p.code)}</b> \u00B7 ${birr(p.reward)} \u1265\u122D ${p.active ? '\u2705' : '\u23F8'}` +
    `<p>\u1325\u1245\u121D \u120B\u12ED \u12E8\u12CB\u1208: ${Number(p.uses)}${p.max_uses ? ' / ' + Number(p.max_uses) : ' (\u12EB\u120D\u1270\u12C8\u1230\u1290)'}` +
    (p.expires_at ? ` \u00B7 \u12A5\u1235\u12A8: ${esc(fmtTime(p.expires_at))}` : '') + `</p>` +
    `<button onclick="promoAct('${esc(p.code)}','toggle')" style="${BTN}background:#ffe629">${p.active ? '\u12A0\u1241\u121D' : '\u12A0\u1235\u1300\u121D\u122D'}</button>` +
    `<button onclick="promoAct('${esc(p.code)}','delete')" style="${BTN}background:#d63939;color:#fff">\uD83D\uDDD1</button></div>`).join('') || '\u2014';
}
function renderSms(s, incoming) {
  document.getElementById('sms-box').innerHTML =
    `<p>\uD83D\uDCDE ${esc(s.number)} \u00B7 ${esc(s.name)}</p>` +
    (s.configured ? `<p>\u2705 SMS_SECRET \u1270\u12D8\u130B\u1305\u1277\u120D\u1362 \u1235\u120D\u12AD\u1205 \u120B\u12ED \u12E8 SMS forwarder \u1218\u1270\u130D\u1260\u122A\u12EB \u12CD\u1235\u1325 \u12ED\u1205\u1295 \u12A0\u12F5\u122B\u123B (POST) \u12A0\u1235\u1308\u1263:</p>` +
      `<input type="text" readonly style="width:100%" value="${esc(s.url || 'RENDER_EXTERNAL_URL \u12A0\u120D\u1270\u12D8\u130B\u1300\u121D')}" onclick="this.select()">`
      : `<p>\u26A0\uFE0F SMS_SECRET \u1260 Render Environment \u12CD\u1235\u1325 \u12A0\u120D\u1270\u12D8\u130B\u1300\u121D\u1364 \u122B\u1235-\u121B\u133D\u12F0\u1245 \u1320\u134D\u1277\u120D (\u1260\u12A5\u1305 \u121B\u133D\u12F0\u1245 \u12ED\u1220\u122B\u120D)\u1362</p>`) +
    `<p><b>\u12E8\u1245\u122D\u1265 \u130A\u12DC \u12E8\u12F0\u1228\u1231 \u12AD\u134D\u12EB\u12CE\u127D:</b></p>` +
    ((incoming || []).map(x => `<div class="pl">${esc(x.ref)} \u00B7 ${birr(x.amount)} \u1265\u122D \u00B7 \u2026${esc(x.sender)} \u00B7 ${x.used_by_req ? '\u2705 #' + Number(x.used_by_req) : '\u23F3 \u12EB\u120D\u1270\u1323\u1218\u1228'} \u00B7 ${esc(fmtTime(x.paid_at))}</div>`).join('') || '\u2014');
}
function renderRefs(r) {
  document.getElementById('ref-box').innerHTML =
    `<div class="req"><p>\u123D\u120D\u121B\u1275: ${birr(r.reward)} \u1265\u122D (\u1208\u121A\u130B\u1265\u12D8\u12CD\u1363 \u12E8\u1270\u130B\u1263\u12E1 \u12E8\u1218\u1300\u1218\u122A\u12EB \u1308\u1295\u12D8\u1265 \u1232\u1338\u12F5\u1245)</p><b>\u12A8\u134D\u1270\u129B \u130B\u1263\u12E6\u127D</b>` +
    (r.top.map(t => `<div class="pl">${esc(t.name)} (ID ${esc(t.inviter)}) \u2014 ${Number(t.invited)} \u130B\u1265\u12DF\u120D \u00B7 ${Number(t.paid)} \u12EB\u1235\u1308\u1261 \u00B7 ${birr(t.earned)} \u1265\u122D</div>`).join('') || '<p>\u2014</p>') +
    `<b>\u12E8\u1245\u122D\u1265 \u130A\u12DC \u130D\u1265\u12E3\u12CE\u127D</b>` +
    (r.recent.map(t => `<div class="pl">${esc(t.name)} \u2190 ${esc(t.inviter_name)} ${Number(t.referral_paid) > 0 ? '\u2705' : '\u23F3'}</div>`).join('') || '<p>\u2014</p>') + `</div>`;
}
async function loadSettings() {
  try {
    const r = await fetch('/api/admin/settings', {headers: HEADERS()});
    if (!r.ok) return;
    const d = await r.json();
    renderRules(d.rules); renderPromos(d.promos); renderSms(d.sms, d.incoming); renderRefs(d.referrals);
  } catch (e) {}
}
document.getElementById('refresh').onclick = () => { load(); loadSettings(); }; load(); loadSettings();
setInterval(loadLive, 5000);
"""


# ------------------------------ Flask API Routes -------------------------
@app.after_request
def no_cache(resp):
    # Telegram's WebView caches aggressively: always fetch the newest page/JS/CSS.
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    return resp


@app.route("/static/voice/<name>")
def serve_voice(name):
    """The owner's recorded number calls: B1.mp3 ... O75.mp3 in the static/voice folder next to this file."""
    if not re.fullmatch(r"[BINGO]\d{1,2}\.mp3", name):
        return Response("not found", status=404)
    resp = send_from_directory(VOICE_DIR, name, mimetype="audio/mpeg", max_age=86400)
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
    last_seen[uid] = time.time()
    if len(last_seen) > 5000:
        for k in [k for k, t in list(last_seen.items()) if time.time() - t > 60]:
            last_seen.pop(k, None)
    row = get_user(uid)
    with game_lock:
        my_cards = list(game_state["player_cards"].get(uid, []))
        my_cards_data = []
        for cid in my_cards:
            mat = game_state["card_matrices"].get((uid, cid))
            if mat is None:
                mat = generate_75_bingo_card(cid)
                game_state["card_matrices"][(uid, cid)] = mat
            mset = list(game_state["marked"].setdefault((uid, cid), {"FREE"}))
            my_cards_data.append({"card_id": cid, "matrix": mat, "marked": mset})

        return jsonify({
            **bonus_info(),
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
        game_state["card_matrices"][(uid, card_num)] = generate_75_bingo_card(card_num)
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


def clean_reference(value):
    """Transaction ID as typed -> upper case without spaces (so 'ab 12' and 'AB12' are the same ID)."""
    return re.sub(r"\s+", "", str(value or "")).upper()[:60]


def reference_problem(ref):
    """Returns an error message when the text obviously is not a transaction ID, otherwise None.
    Deliberately lenient: the owner still checks every ID in his own Telebirr/CBE history."""
    if re.fullmatch(r"(?:\+?251|0)?[79]\d{8}", ref.replace("-", "")):
        return "\u12eb\u1235\u1308\u1261\u1275 \u12e8\u1235\u120d\u12ad \u1241\u1325\u122d \u12ed\u1218\u1235\u120b\u120d\u1362 \u12a5\u1263\u12ad\u12ce \u12a8\u12f0\u1228\u1230\u1299 \u120b\u12ed \u12e8\u130d\u1265\u12ed\u1275 \u1241\u1325\u122d (Transaction ID) \u12eb\u1235\u1308\u1261\u1362"
    if len(ref) < 6 or len(ref) > 40 or not re.fullmatch(r"[A-Z0-9_./\-]+", ref) or len(set(ref)) <= 2:
        return "\u12e8\u130d\u1265\u12ed\u1275 \u1241\u1325\u1229 \u1275\u12ad\u12ad\u120d \u12a0\u12ed\u1218\u1235\u120d\u121d\u1362 \u12a8\u12f0\u1228\u1230\u1299 \u120b\u12ed \u1275\u12ad\u12ad\u1208\u129b\u12cd\u1295 \u12e8\u130d\u1265\u12ed\u1275 \u1241\u1325\u122d (Transaction ID) \u12eb\u1235\u1308\u1261\u1362"
    return None


@app.post("/api/miniapp/deposit-request")
def api_miniapp_deposit():
    user = api_user()
    if not user:
        return json_error("Unauthorized", 401)
    body = request.get_json(silent=True) or {}
    amt = to_positive_float(body.get("amount"))
    if not amt:
        return json_error("\u12a5\u1263\u12ad\u12ce \u1218\u1228\u1303\u12cd\u1295 \u12ed\u1219\u1209")
    ref, err = extract_reference(str(body.get("reference") or "")[:800])
    if err:
        return json_error(err)
    status, message = submit_deposit(user["id"], amt, ref)
    if status == "error":
        return json_error(message)
    return jsonify({"message": message})


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
    if amt < MIN_WITHDRAW:
        return json_error("\u12dd\u1245\u1270\u129b\u12cd \u12e8\u121b\u12cd\u132b \u1218\u1320\u1295 %d \u1265\u122d \u1290\u12cd\u1362" % MIN_WITHDRAW)
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


def live_snapshot():
    now = time.time()
    with game_lock:
        players = sum(1 for c in game_state["player_cards"].values() if c)
        cards = sum(len(c) for c in game_state["player_cards"].values())
        snap = {
            "round": game_state["round"], "phase": game_state["phase"],
            "players": players, "cards": cards, "pool": game_state["prize"],
            "called": len(game_state["called_numbers"]),
            "selection_left": max(0, int(game_state["selection_ends_at"] - now))
            if game_state["phase"] == "selecting" else 0,
        }
    snap["online"] = sum(1 for t in list(last_seen.values()) if now - t < 20)
    return snap


def money_stats(con, today_only):
    """Rounds / sales / owner's share / payouts / deposits. 'Today' = the Ethiopian calendar day."""
    day = "date(%s,'+3 hours')=date('now','+3 hours')"
    r_cond = day % "created_at" if today_only else "1=1"
    row = con.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(CASE WHEN status='won' THEN pool END),0) AS sales, "
        "COALESCE(SUM(house_cut),0) AS house, COALESCE(SUM(payout),0) AS payout "
        "FROM rounds WHERE " + r_cond).fetchone()
    d_cond = ("status='approved' AND " + (day % "reviewed_at")) if today_only else "status='approved'"
    deposits = con.execute("SELECT COALESCE(SUM(amount),0) FROM deposit_requests WHERE " + d_cond).fetchone()[0]
    out = {"rounds": row["n"], "sales": row["sales"], "house": row["house"],
           "payout": row["payout"], "deposits": deposits}
    if today_only:
        out["players"] = con.execute(
            "SELECT COUNT(DISTINCT rp.user_id) FROM round_players rp JOIN rounds r ON r.id=rp.round_id WHERE "
            + (day % "r.created_at")).fetchone()[0]
    else:
        out["withdrawals"] = con.execute(
            "SELECT COALESCE(SUM(amount),0) FROM withdraw_requests WHERE status='paid'").fetchone()[0]
    return out


@app.get("/api/admin/live")
def admin_live():
    user = api_admin()
    if not user:
        return json_error("Admin access denied", 403)
    return jsonify(live_snapshot())


@app.get("/api/admin/round/<int:round_id>")
def admin_round(round_id):
    user = api_admin()
    if not user:
        return json_error("Admin access denied", 403)
    with closing(db()) as con:
        rows = con.execute(
            "SELECT user_id, name, cards, stake, prize FROM round_players WHERE round_id=? "
            "ORDER BY prize DESC, stake DESC", (round_id,)).fetchall()
        return jsonify({"players": [dict(r) for r in rows]})


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
        hist = con.execute("SELECT * FROM rounds ORDER BY id DESC LIMIT 50").fetchall()
        return jsonify({"users_count": uc, "total_credits": tc, "pending_count": pc + pw,
                        "requests": [dict(r) for r in reqs], "withdrawals": [dict(r) for r in wds],
                        "live": live_snapshot(),
                        "stats": {"today": money_stats(con, True), "all": money_stats(con, False)},
                        "history": [dict(r) for r in hist]})


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


@app.route("/api/sms-webhook", methods=["POST", "GET"])
def sms_webhook():
    """The owner's phone forwards every incoming Telebirr SMS here (see the admin panel for the address)."""
    if not SMS_SECRET:
        return json_error("SMS_SECRET is not set", 503)
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        data = {}

    def pick(*names):
        for n in names:
            v = data.get(n)
            if v is None:
                v = request.values.get(n)
            if v not in (None, ""):
                return str(v)
        return ""

    key = request.args.get("key") or request.headers.get("X-Secret") or pick("key", "secret")
    if not hmac.compare_digest(str(key).encode(), SMS_SECRET.encode()):
        return json_error("forbidden", 403)
    text = pick("message", "text", "body", "content", "sms", "msg")
    if not text and not data and not request.form:
        text = request.get_data(as_text=True) or ""
    sender = pick("from", "sender", "address", "number")
    as_json = request.args.get("json") == "1"   # some forwarder apps insist on the plain text reply "OK"

    def done(payload):
        return jsonify(payload) if as_json else Response("OK", content_type="text/plain; charset=utf-8")

    if SMS_SENDER and sender and sender.strip().lower() != SMS_SENDER.lower():
        return done({"ok": True, "ignored": "sender"})
    parsed = parse_telebirr_sms(text)
    if not parsed:
        return done({"ok": True, "ignored": "not an incoming Telebirr payment"})
    try:
        result = handle_incoming_sms(parsed)
    except Exception:
        logger.exception("sms webhook failed")
        return json_error("server error", 500)
    logger.info("incoming SMS payment %s -> %s", parsed["ref"], result)
    return done({"ok": True, "result": result})


@app.get("/api/admin/settings")
def admin_settings():
    user = api_admin()
    if not user:
        return json_error("Admin access denied", 403)
    with closing(db()) as con:
        rules = [dict(r) for r in con.execute("SELECT * FROM bonus_rules ORDER BY id").fetchall()]
        promos = [dict(r) for r in con.execute("SELECT * FROM promo_codes ORDER BY created_at DESC").fetchall()]
        incoming = [dict(r) for r in con.execute("SELECT * FROM incoming_payments ORDER BY id DESC LIMIT 15").fetchall()]
        top = con.execute(
            "SELECT referred_by AS inviter, COUNT(*) AS invited, "
            "SUM(CASE WHEN referral_paid>0 THEN 1 ELSE 0 END) AS paid, COALESCE(SUM(referral_paid),0) AS earned "
            "FROM users WHERE referred_by IS NOT NULL GROUP BY referred_by ORDER BY invited DESC LIMIT 20").fetchall()
        recent = con.execute(
            "SELECT user_id, referred_by, referral_paid, created_at FROM users WHERE referred_by IS NOT NULL "
            "ORDER BY created_at DESC LIMIT 20").fetchall()
    top = [dict(r, name=display_name(r["inviter"])) for r in top]
    recent = [dict(r, name=display_name(r["user_id"]), inviter_name=display_name(r["referred_by"])) for r in recent]
    url = (PUBLIC_URL + "/api/sms-webhook?key=" + SMS_SECRET) if (SMS_SECRET and PUBLIC_URL) else ""
    return jsonify({"rules": rules, "promos": promos, "incoming": incoming,
                    "referrals": {"top": top, "recent": recent, "reward": REFERRAL_REWARD},
                    "sms": {"configured": bool(SMS_SECRET), "url": url, "number": PAY_NUMBER, "name": PAY_NAME}})


@app.post("/api/admin/bonus-rule")
def admin_bonus_rule():
    user = api_admin()
    if not user:
        return json_error("Admin access denied", 403)
    b = request.get_json(silent=True) or {}
    rid = b.get("id")
    with closing(db()) as con:
        if b.get("delete"):
            con.execute("DELETE FROM bonus_rules WHERE id=?", (int(rid or 0),))
            con.commit()
            return jsonify({"ok": True})
        try:
            days = sorted({int(x) for x in (b.get("days") or []) if 0 <= int(x) <= 6})
            start, end = int(b.get("start")), int(b.get("end"))
            percent, cap = float(b.get("percent")), float(b.get("cap") or 0)
        except (TypeError, ValueError):
            return json_error("\u1275\u12ad\u12ad\u120d \u12eb\u120d\u1206\u1290 \u1218\u1228\u1303")
        if not (0 <= start < end <= 1440) or not (0 <= percent <= 1000) or cap < 0:
            return json_error("\u1230\u12d3\u1271\u1295 \u12c8\u12ed\u121d \u1218\u1276\u129b\u12cd\u1295 \u12eb\u1228\u130b\u130d\u1321 (\u1218\u1300\u1218\u122a\u12eb \u12a8\u1218\u1328\u1228\u123b \u12eb\u1295\u1233\u120d)")
        name = str(b.get("name") or "")[:40]
        active = 1 if b.get("active") else 0
        dstr = ",".join(str(d) for d in days)
        if rid:
            con.execute("UPDATE bonus_rules SET name=?, days=?, start_min=?, end_min=?, percent=?, cap=?, active=? WHERE id=?",
                        (name, dstr, start, end, percent, cap, active, int(rid)))
        else:
            con.execute("INSERT INTO bonus_rules(name, days, start_min, end_min, percent, cap, active) VALUES(?,?,?,?,?,?,?)",
                        (name, dstr, start, end, percent, cap, active))
        con.commit()
    return jsonify({"ok": True})


@app.post("/api/admin/promo")
def admin_promo():
    user = api_admin()
    if not user:
        return json_error("Admin access denied", 403)
    b = request.get_json(silent=True) or {}
    code = re.sub(r"\s+", "", str(b.get("code") or "")).upper()[:40]
    if not code:
        return json_error("\u12ae\u12f1\u1295 \u12eb\u1235\u1308\u1261")
    with closing(db()) as con:
        if b.get("delete"):
            con.execute("DELETE FROM promo_codes WHERE code=?", (code,))
            con.commit()
            return jsonify({"ok": True})
        if b.get("toggle"):
            con.execute("UPDATE promo_codes SET active=1-active WHERE code=?", (code,))
            con.commit()
            return jsonify({"ok": True})
        try:
            reward = float(b.get("reward"))
            max_uses = int(b.get("max_uses") or 0)
        except (TypeError, ValueError):
            return json_error("\u1275\u12ad\u12ad\u120d \u12eb\u120d\u1206\u1290 \u1218\u1228\u1303")
        if reward <= 0 or max_uses < 0:
            return json_error("\u123d\u120d\u121b\u1271 \u12a80 \u1260\u120b\u12ed \u1218\u1206\u1295 \u12a0\u1208\u1260\u1275")
        expires = None
        if b.get("expires"):
            try:   # the admin picks a date in Ethiopia time: valid until the end of that day
                d = datetime.strptime(str(b["expires"])[:10], "%Y-%m-%d")
                expires = fmt_db(d + timedelta(hours=24) - timedelta(hours=3) - timedelta(seconds=1))
            except ValueError:
                return json_error("\u1240\u1291 \u1275\u12ad\u12ad\u120d \u12a0\u12ed\u12f0\u1208\u121d")
        con.execute(
            "INSERT INTO promo_codes(code, reward, max_uses, expires_at, active) VALUES(?,?,?,?,1) "
            "ON CONFLICT(code) DO UPDATE SET reward=excluded.reward, max_uses=excluded.max_uses, "
            "expires_at=excluded.expires_at, active=1", (code, reward, max_uses, expires))
        con.commit()
    return jsonify({"ok": True})


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
