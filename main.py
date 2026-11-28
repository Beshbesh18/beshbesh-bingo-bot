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
    STARTING_CREDITS = float(os.environ.get("STARTING_CREDITS", "0"))   # v16: default 0 (was 100 free ETB per new account)
except ValueError:
    STARTING_CREDITS = 0.0
NEW_USER_BONUS = 10  # welcome bonus (ETB) for a NEW user who shares his phone contact
HOUSE_PERCENT = 20   # DEFAULT owner's share (commission) of every round's prize pool. The owner can change it any time with /commission or in the admin panel (winners share the rest)
MIN_WITHDRAW = 100   # smallest withdrawal a player may ask for (ETB)
MIN_WITHDRAW_DEPOSITS = 100   # v15: a player may withdraw only after ALL his approved deposits add up to this much (ETB)
MAX_PENDING_DEPOSITS = 5  # unreviewed deposit requests one player may have at the same time
MAX_CARDS_PER_PLAYER = 10   # most cards one player may hold in a round
RANK_SIZE = 10      # how many players the Rank screen shows
NEW_USER_DAYS = 7   # a claim bonus for 'new' users is open to accounts created in the last 7 days
SELECTION_TIME_SECONDS = 50
CALL_INTERVAL_SECONDS = 3
MAX_NUMBERS_POOL = 75  # 1 to 75 numbers called in game
TOTAL_CARDS = 600
# ---- deposits (Telebirr only) ----
PAY_NUMBER = os.environ.get("PAY_NUMBER", "0954302323").strip()   # the owner's Telebirr number
PAY_NAME = os.environ.get("PAY_NAME", "Enyew").strip()         # the name shown to players
PAY_TAIL = re.sub(r"\D", "", PAY_NUMBER)[-4:]

# ---- deposit methods: Telebirr and Ebirr (v18: Bank of Abyssinia removed) ----
# A method is shown to players ONLY when its number is filled in. Set the numbers in Render > Environment
# (PAY_NUMBER, EBIRR_NUMBER ...) or type them between the quotes below.
EBIRR_NUMBER = os.environ.get("EBIRR_NUMBER", "0954302323").strip()           # Ebirr (Kaafi) number
EBIRR_NAME = os.environ.get("EBIRR_NAME", "Enyew").strip()
DEPOSIT_METHODS = [
    {"key": "telebirr", "label": "Telebirr", "icon": "📱", "number": PAY_NUMBER, "name": PAY_NAME,
     "num_am": "ስልክ ቁጥር", "num_en": "Phone number"},
    {"key": "ebirr", "label": "Ebirr", "icon": "💵", "number": EBIRR_NUMBER, "name": EBIRR_NAME,
     "num_am": "ቁጥር", "num_en": "Number"},
]
WITHDRAW_METHOD = "Telebirr"   # withdrawals are paid with Telebirr only


def deposit_methods():
    return [m for m in DEPOSIT_METHODS if m["number"]]


def deposit_method(key):
    k = str(key or "").strip().lower()
    for m in DEPOSIT_METHODS:
        if k in (m["key"], m["label"].lower()):
            return m
    return None
MIN_DEPOSIT = 20          # smallest deposit (ETB)
REFERRAL_REWARD = 10      # ETB bonus (Play wallet) the inviter gets when an invited NEW person shares his own phone number (editable in the admin panel)
REFERRAL_MIN_DEPOSIT = 50  # smallest approved deposit of the invited person that pays the inviter (editable in the admin panel)
# Secret for the phone's SMS-forwarding webhook (/api/sms-webhook). Without it the webhook is disabled.
SMS_SECRET = os.environ.get("SMS_SECRET", "").strip()
SMS_SENDER = os.environ.get("SMS_SENDER", "").strip()  # optional: only accept SMS from this sender name/number
SMS_SENDERS = {x.strip().lower() for x in SMS_SENDER.split(",") if x.strip()}   # comma separated list is allowed
RECEIPT_URL = "https://transactioninfo.ethiotelecom.et/receipt/"
FINISHED_SECONDS = 4  # how long the "BINGO!" result stays on screen
BONUS_FINISHED_SECONDS = 12   # v19: a bonus round's winner screen stays longer (the bonus show)
BONUS_GRACE_MINUTES = 25      # v19: a bonus slot is started only within this many minutes after its time
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
        ucols2 = {r["name"] for r in con.execute("PRAGMA table_info(users)").fetchall()}
        if "last_seen_at" not in ucols2:
            con.execute("ALTER TABLE users ADD COLUMN last_seen_at TEXT")
        con.executescript("""
        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS claim_bonuses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL DEFAULT '',
            amount REAL NOT NULL DEFAULT 0,
            max_claims INTEGER NOT NULL DEFAULT 0,
            claimed INTEGER NOT NULL DEFAULT 0,
            audience TEXT NOT NULL DEFAULT 'all',
            expires_at TEXT,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS claim_bonus_uses (
            bonus_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (bonus_id, user_id)
        );
        """)
        icols = {r["name"] for r in con.execute("PRAGMA table_info(incoming_payments)").fetchall()}
        if "alt_ref" not in icols:
            con.execute("ALTER TABLE incoming_payments ADD COLUMN alt_ref TEXT NOT NULL DEFAULT ''")
        if "method" not in icols:
            con.execute("ALTER TABLE incoming_payments ADD COLUMN method TEXT NOT NULL DEFAULT 'Telebirr'")
        pcols = {r["name"] for r in con.execute("PRAGMA table_info(promo_codes)").fetchall()}
        if "min_deposit" not in pcols:
            con.execute("ALTER TABLE promo_codes ADD COLUMN min_deposit REAL NOT NULL DEFAULT 0")
        if fresh_rules:
            # Ethiopia time (UTC+3), standard clock. days: Monday=0 ... Sunday=6
            con.executemany(
                "INSERT INTO bonus_rules(name, days, start_min, end_min, percent, cap, active) VALUES(?,?,?,?,?,?,1)",
                [("\u12a5\u1201\u12f5 100%", "6", 900, 1290, 100, 200),
                 ("\u1301\u121b 50%", "4", 900, 1290, 50, 200),
                 ("\u1260\u1218\u12a8\u1275\u120d 10%", "", 0, 1440, 10, 0)])
        # ---- v19: round bonus slots (time is minutes after midnight, standard clock, Ethiopia time) ----
        fresh_slots = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='round_bonus_slots'").fetchone() is None
        con.execute("""CREATE TABLE IF NOT EXISTS round_bonus_slots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL DEFAULT '',
            minute INTEGER NOT NULL DEFAULT 0,
            amount REAL NOT NULL DEFAULT 0,
            active INTEGER NOT NULL DEFAULT 1,
            last_fired TEXT NOT NULL DEFAULT ''
        )""")
        rcols = {r["name"] for r in con.execute("PRAGMA table_info(rounds)").fetchall()}
        if "bonus" not in rcols:
            con.execute("ALTER TABLE rounds ADD COLUMN bonus REAL NOT NULL DEFAULT 0")
        if fresh_slots:
            con.executemany(
                "INSERT INTO round_bonus_slots(name, minute, amount, active) VALUES(?,?,?,1)",
                [("ቀን ቦነስ", 12 * 60 + 30, 300), ("ምሽት ቦነስ", 20 * 60, 500)])   # 6:30 ከቀኑ and 2 ሰዓት ምሽት (Ethiopian clock)
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
                (phone, setting_con(con, "new_user_bonus"), uid))
            result = "bonus"
        con.commit()
        return result


def pay_referral_on_phone(uid):
    """The invited person shared his own phone number: pay his inviter once (REFERRAL_REWARD, Play wallet).
    Returns the inviter id, or None when nothing was paid."""
    uid = int(uid)
    return None   # v16: the referral reward is paid only after the invited person's deposit (see approve_deposit)
    with closing(db()) as con:
        con.execute("BEGIN IMMEDIATE")
        u = con.execute("SELECT referred_by, referral_paid FROM users WHERE user_id=?", (uid,)).fetchone()
        if not u or not u["referred_by"] or u["referral_paid"]:
            con.rollback()
            return None
        inviter = int(u["referred_by"])
        reward = float(setting_con(con, "referral_reward"))
        if reward <= 0 or inviter == uid or con.execute("SELECT 1 FROM users WHERE user_id=?", (inviter,)).fetchone() is None:
            con.rollback()
            return None
        con.execute("UPDATE users SET credits=credits+? WHERE user_id=?", (reward, inviter))
        con.execute("UPDATE users SET referral_paid=? WHERE user_id=?", (reward, uid))
        con.commit()
    send_to_chat(inviter, L(inviter,
                            "🎉 የጋበዙት ሰው ተቀላቅሎ ስልክ ቁጥሩን አጋርቷል! %s ብር ቦነስ ወደ መጫወቻ ሂሳብዎ ተጨምሯል።" % money(reward),
                            "🎉 Someone you invited joined and shared their phone number! %s ETB bonus was added to your Play wallet." % money(reward)))
    return inviter


def set_language(user_id, language):
    with closing(db()) as con:
        con.execute("UPDATE users SET language=? WHERE user_id=?", (language, int(user_id)))
        con.commit()


def get_language(user_id):
    row = get_user(user_id)
    return row["language"] if row else "am"


# ---------------------- Editable settings (admin panel) ----------------------
SETTING_DEFAULTS = {
    "new_user_bonus": NEW_USER_BONUS,
    "referral_reward": REFERRAL_REWARD,
    "referral_min_deposit": REFERRAL_MIN_DEPOSIT,
    "house_percent": HOUSE_PERCENT,   # v17: the owner's commission in % (editable)
    "bonus_override_percent": -1,     # v18: -1 = use the time table; 0 or more = the owner's hand-set deposit bonus % for everyone
    "bonus_override_cap": 0,          # v18: most ETB of that hand-set bonus per deposit (0 = no limit)
    "bot_enabled": 1,                 # 1 = በርቷል, 0 = ጠፍቷል
    "bot_cards_per_round": 2,         # በአንድ ዙር የሚያዙ የቦት ካርዶች ብዛት (በእጅ የሚቀየር)
    "human_win_percent": 15,          # የሰው ማሸነፍ መቶኛ (0-100)
}


def setting_con(con, key):
    row = con.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
    try:
        return float(row["value"]) if row else float(SETTING_DEFAULTS[key])
    except (TypeError, ValueError):
        return float(SETTING_DEFAULTS[key])


def get_setting(key):
    with closing(db()) as con:
        return setting_con(con, key)


def set_setting(key, value):
    with closing(db()) as con:
        con.execute("INSERT INTO app_settings(key, value) VALUES(?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, repr(float(value))))
        con.commit()


def new_user_bonus_text():
    return ("%.2f" % get_setting("new_user_bonus")).rstrip("0").rstrip(".")


def house_percent():
    """v17: the owner's commission in percent. Default HOUSE_PERCENT (20); the owner changes it with the
    /commission command or in the admin panel (it is saved in the database, so a redeploy keeps it)."""
    try:
        v = get_setting("house_percent")
    except Exception:
        v = float(HOUSE_PERCENT)
    return min(90.0, max(0.0, v))


def round_house_percent():
    """The commission locked in for the round that is running now (a change applies from the NEXT round)."""
    p = game_state.get("house_percent")
    return float(p) if p is not None else house_percent()


def winners_cents(pool_cents, pct):
    """What the winners share (in cents) when the owner keeps pct percent of the pool."""
    return int(pool_cents * (100.0 - float(pct)) / 100.0 + 1e-9)


def card_ranking():
    """Players ranked by the number of cards they played (finished rounds; refunded rounds and the admins
    are left out). Returns [(user_id, cards)] best first; a tie is won by who started playing first."""
    with closing(db()) as con:
        rows = con.execute(
            "SELECT rp.user_id AS uid, SUM(rp.cards) AS total FROM round_players rp "
            "JOIN rounds r ON r.id=rp.round_id WHERE r.status!='refunded' "
            "GROUP BY rp.user_id HAVING total>0 ORDER BY total DESC, MIN(rp.id) ASC").fetchall()
    return [(int(r["uid"]), int(r["total"])) for r in rows if int(r["uid"]) not in ADMIN_IDS]


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
        "last_winners": [],           # [{user_id, name, card_id, prize, matrix, marked, cells}]
        "closing_number": None,       # the number that was called when the round closed
        "card_paid_from": {},         # (user_id, card_id) -> "credits" or "win" wallet
        "house_percent": house_percent(),   # v17: commission locked in when the round starts
        "bonus_round": False,         # v19: this round is a full-card (blackout) bonus round
        "bonus_amount": 0.0,          # v19: the owner's bonus (ETB) added to the winners' prize
        "bonus_name": "",
        "win_info": None,             # v19: {pool, bonus, total, n, share} of a finished bonus round
        "manual_bonus": None,         # v19: /roundbonus now <amount> -> the next round is a bonus round
    }


game_state = fresh_game()


def _restore_round_number():
    """After a restart / redeploy continue with the next round number instead of starting from 1 again."""
    try:
        with closing(db()) as con:
            last = con.execute("SELECT COALESCE(MAX(round_no), 0) FROM rounds").fetchone()[0]
        game_state["round"] = int(last) + 1
    except Exception:
        logger.exception("could not restore the round number")


_restore_round_number()


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


def card_has_blackout(card, marked):
    """v19: full-card bonus round - every number on the card (the middle FREE counts) is marked."""
    marked = set(marked) | {"FREE"}
    return all(card[r][c] in marked for r in range(5) for c in range(5))


def winning_cells(card, marked):
    """v15: the cells [row, col] of every completed pattern (row, column, diagonal, four corners) on a card."""
    marked = set(marked) | {"FREE"}
    patterns = [[(r, c) for c in range(5)] for r in range(5)]
    patterns += [[(r, c) for r in range(5)] for c in range(5)]
    patterns.append([(i, i) for i in range(5)])
    patterns.append([(i, 4 - i) for i in range(5)])
    patterns.append([(0, 0), (0, 4), (4, 0), (4, 4)])
    cells = set()
    for p in patterns:
        if all(card[r][c] in marked for r, c in p):
            cells.update(p)
    return [[r, c] for r, c in sorted(cells)]


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
        game_state["closing_number"] = None
        game_state["card_paid_from"] = {}
        game_state["house_percent"] = house_percent()   # v17: a changed commission applies from this new round
        game_state["finished_ends_at"] = 0
        game_state["bonus_round"] = False
        game_state["bonus_amount"] = 0.0
        game_state["bonus_name"] = ""
        game_state["win_info"] = None
        game_state.pop("round_favors_bot", None)
        game_state.pop("round_favors_human", None)
        game_state.pop("target_close_call", None)
        game_state.pop("bot_names", None)
        game_state.pop("designated_bot_winner", None)
        game_state.pop("bot_initial_burst_done", None)
        game_state.pop("bot_round_target", None)
        game_state.pop("bot_setting_ref", None)
        maybe_start_bonus_round()   # v19: the NEW round becomes a bonus round when a bonus time has arrived


BOT_NAMES = [
    "Alex", "shanbel alemu", "aweke kidu", "@kiduse25", "teddddo", "obsa",
    "King 👑", "@abduker_k", "ketema mulatu", "nebiy eshete",
    "Mohamed kedir", "@mikkireal", "@miki1876", "nati ♂️",
    "dingetu temogne", "firezer kebede", "@esmael345", "@zjhgtk12",
    "Dagne dish", "zumra Mohamed", "Endris", "ayu adama", "Nuru Nuru",
    "mustofa seyaa", "chala bekele", "@Eyoo1994",
    "@aputu12", "@brook_06", "@nati_12", "@ephrem_21", "@dawit_99",
    "@samuel_07", "@henok_19", "@abel_23", "@yared_04", "@solomon_88",
    "Brook", "Ephrem", "Dawit", "Samuel", "Nati", "Henok", "Abel",
    "Yohannes", "Yared", "Solomon", "Biniyam", "Bereket", "Nahom",
    "Elias", "Eyob", "Mikiyas", "ብሩክ12", "ብሩክ06", "ኤፍሬም", "መሀመድ አሊ",
    "ዳዊት", "ሳሙኤል", "ናቲ", "ሄኖክ", "አቤል", "ያሬድ", "ሰለሞን", "ተመስገን",
    "ታደሰ", "በረከት", "እዮብ", "ቢንያም", "Mekdes", "Tigist",
    "ህይወት ትቀጥላለች", "ዕድል ዛሬ ነው", "በጸሎት ብቻ", "ቀስ ብሎ ይሳካል",
    "ሁሉም በጊዜው", "ትዕግስት ፍሬ አለው",
]
BOT_UID_BASE = -900000


def is_bot_uid(uid):
    try:
        return int(uid) <= BOT_UID_BASE
    except (TypeError, ValueError):
        return False


def display_name(uid):
    if is_bot_uid(uid):
        return game_state.get("bot_names", {}).get(int(uid), "Player")
    row = get_user(uid)
    if not row:
        return "Player"
    return ("@" + row["username"]) if row["username"] else (row["first_name"] or "Player")


def find_winners():
    """Return [(uid, card_id)] for every player whose card currently has bingo. Needs game_lock."""
    winners = []
    check = card_has_blackout if game_state.get("bonus_round") else card_has_bingo   # v19: bonus round = full card
    for uid, cards in game_state["player_cards"].items():
        for cid in cards:
            mat = game_state["card_matrices"].get((uid, cid))
            marked = game_state["marked"].get((uid, cid), {"FREE"})
            if mat and check(mat, marked):
                winners.append((uid, cid))
                break
    return winners


def record_round(status, house, payout, winners, bonus=0.0):
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
                "INSERT INTO rounds(round_no, status, players, cards, pool, house_cut, payout, winners, called_count, bonus) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (game_state["round"], status, len(rows), sum(r[2] for r in rows),
                 float(game_state["prize"]), float(house), float(payout), names,
                 len(game_state["called_numbers"]), float(bonus)))
            rid = cur.lastrowid
            con.executemany(
                "INSERT INTO round_players(round_id, user_id, name, cards, stake, prize) VALUES(?,?,?,?,?,?)",
                [(rid,) + r for r in rows])
            con.commit()
    except Exception:
        logger.exception("could not save the round history")


def declare_winners(winners):
    """Pay the prize (split between winners) and stop the round. Needs game_lock.
    v19: in a bonus round the owner's bonus is added on top (no commission is taken from the bonus)."""
    # the owner keeps house% of the pool; the rest is split between the winners
    # (integer cents, so no money is created or lost: left-over cents stay with the owner)
    pool_cents = int(round(float(game_state["prize"]) * 100))
    payout_cents = winners_cents(pool_cents, round_house_percent())   # v17: commission is editable (default 20%)
    bonus_cents = int(round(float(game_state.get("bonus_amount") or 0) * 100)) if game_state.get("bonus_round") else 0
    total_cents = payout_cents + bonus_cents
    n = len(winners)
    share_cents = total_cents // n
    share = share_cents / 100.0
    paid_cents = share_cents * n
    house = (pool_cents - (paid_cents - bonus_cents)) / 100.0   # the part of the card money the owner keeps
    results = []
    for uid, cid in winners:
        if not is_bot_uid(uid):
            update_credits(uid, share, is_win=True)
        name = display_name(uid)
        with closing(db()) as con:
            con.execute(
                "INSERT INTO wins(user_id, username, round_no, card_index, prize) VALUES(?,?,?,?,?)",
                (int(uid), name, game_state["round"], cid, share))
            con.commit()
        mat = game_state["card_matrices"].get((uid, cid)) or generate_75_bingo_card(cid)
        mk = game_state["marked"].get((uid, cid), {"FREE"})
        results.append({"user_id": uid, "name": name, "card_id": cid, "prize": share,
                        "matrix": mat, "marked": [x for x in mk if x != "FREE"],
                        "cells": winning_cells(mat, mk)})
    game_state["closing_number"] = game_state["current_number"]   # v15: the call the round closed on
    game_state["last_winners"] = results
    game_state["win_info"] = ({"pool": payout_cents / 100.0, "bonus": bonus_cents / 100.0,
                               "total": total_cents / 100.0, "n": n, "share": share} if bonus_cents else None)
    record_round("won", house, paid_cents / 100.0, results, bonus_cents / 100.0)
    game_state["phase"] = "finished"
    game_state["finished_ends_at"] = time.time() + (BONUS_FINISHED_SECONDS if bonus_cents else FINISHED_SECONDS)
    if bonus_cents:
        try:
            notify_admins("🎁 ቦነስ ዙር #%d አልቋል\nደራሽ: %s ብር + ቦነስ: %s ብር = %s ብር\nአሸናፊ(ዎች): %s\nለእያንዳንዳቸው: %s ብር"
                          % (game_state["round"], money(payout_cents / 100.0), money(bonus_cents / 100.0),
                             money(total_cents / 100.0), ", ".join("%s (#%s)" % (w["name"], w["card_id"]) for w in results),
                             money(share)))
        except Exception:
            logger.exception("could not tell the admins about the bonus round")


# ---------------- v19: round bonus (a full-card "ቦነስ ዙር" at set times) ----------------
ROUND_PERIODS = {"ጠዋት": "morning", "ጥዋት": "morning", "morning": "morning",
                 "ቀን": "afternoon", "ከቀኑ": "afternoon", "day": "afternoon", "afternoon": "afternoon",
                 "ምሽት": "evening", "ማታ": "evening", "evening": "evening",
                 "ሌሊት": "night", "night": "night"}
ROUND_PERIOD_HOURS = {"morning": (12, 1, 2, 3, 4, 5), "afternoon": (6, 7, 8, 9, 10, 11),
                      "evening": (12, 1, 2, 3, 4, 5), "night": (6, 7, 8, 9, 10, 11)}


def eth_to_minutes(period, hour, minute):
    """Ethiopian clock (period + hour + minute) -> minutes after midnight on the standard clock."""
    base = {"morning": 6, "afternoon": 12, "evening": 18, "night": 0}[period]
    first = 0 if period in ("morning", "evening") else 6
    off = (((hour % 12) - first) % 12 + 12) % 12
    return ((base + off) % 24) * 60 + minute


def parse_eth_time(tokens):
    """['ቀን', '6:30'] -> 750 (12:30 standard). Returns None when it is not a valid Ethiopian time."""
    if len(tokens) < 2:
        return None
    p = ROUND_PERIODS.get(str(tokens[0]).lower())
    m = re.fullmatch(r"(\d{1,2})(?:[:.](\d{1,2}))?", str(tokens[1]))
    if not p or not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2) or 0)
    if h not in ROUND_PERIOD_HOURS[p] or not 0 <= mi < 60:
        return None
    return eth_to_minutes(p, h, mi)


def maybe_start_bonus_round():
    """Needs game_lock. Turns the round that has just started (selecting, nobody has a card yet) into a bonus round
    when a bonus time has arrived (or the owner used /roundbonus now). A game that is already running is never cut:
    the bonus simply waits for the next round."""
    if game_state["phase"] != "selecting" or game_state.get("bonus_round"):
        return
    if any(game_state["player_cards"].values()):
        return
    amount, name = None, ""
    manual = game_state.get("manual_bonus")
    if manual:
        amount, name = float(manual), "ቦነስ"
        game_state["manual_bonus"] = None
    else:
        local = utc_now() + timedelta(hours=3)
        today = local.strftime("%Y-%m-%d")
        minute = local.hour * 60 + local.minute
        with closing(db()) as con:
            for r in con.execute("SELECT * FROM round_bonus_slots WHERE active=1 ORDER BY minute").fetchall():
                if r["last_fired"] == today or minute < r["minute"]:
                    continue
                con.execute("UPDATE round_bonus_slots SET last_fired=? WHERE id=?", (today, r["id"]))
                if minute - r["minute"] <= BONUS_GRACE_MINUTES and amount is None:
                    amount, name = float(r["amount"]), r["name"]
                # a slot whose time passed long ago (e.g. the bot was off) is skipped for today
            con.commit()
    if amount and amount > 0:
        game_state["bonus_round"] = True
        game_state["bonus_amount"] = amount
        game_state["bonus_name"] = name
        game_state["selection_ends_at"] = time.time() + SELECTION_TIME_SECONDS   # nobody has picked yet: a full timer
        try:
            notify_admins("🎁 ቦነስ ዙር ተጀመረ (ዙር #%d)\nቦነስ: %s ብር (ከእርስዎ ኪስ) · ሙሉ ዝግ" % (game_state["round"], money(amount)))
        except Exception:
            logger.exception("could not tell the admins about the bonus round start")


_next_bonus_cache = {"t": 0.0, "v": None}


def next_bonus_info():
    """The next bonus the Mini App announces (cached for a few seconds), or None."""
    if game_state.get("manual_bonus"):
        return {"name": "ቦነስ", "amount": float(game_state["manual_bonus"]), "time_text": "", "mins_left": 0, "due": True}
    if time.time() - _next_bonus_cache["t"] < 5:
        return _next_bonus_cache["v"]
    local = utc_now() + timedelta(hours=3)
    today = local.strftime("%Y-%m-%d")
    minute = local.hour * 60 + local.minute
    best = None
    with closing(db()) as con:
        for r in con.execute("SELECT * FROM round_bonus_slots WHERE active=1").fetchall():
            if r["last_fired"] != today and minute >= r["minute"] and minute - r["minute"] <= BONUS_GRACE_MINUTES:
                left = 0   # its time has come: it starts with the next round
            elif r["minute"] > minute and r["last_fired"] != today:
                left = r["minute"] - minute
            else:
                left = r["minute"] + 1440 - minute
            if best is None or left < best[0]:
                best = (left, r)
    val = None
    if best:
        left, r = best
        val = {"name": r["name"], "amount": float(r["amount"]), "time_text": eth_clock(r["minute"]),
               "mins_left": int(left), "due": left == 0}
    _next_bonus_cache["t"], _next_bonus_cache["v"] = time.time(), val
    return val


def bonus_cache_reset():
    _next_bonus_cache["t"] = 0.0


def try_award(uid, username=""):
    """Manual BINGO claim. Returns the winners list or None."""
    with game_lock:
        if game_state["phase"] != "playing":
            return None
        try:
            human_pct = max(0.0, min(100.0, float(get_setting("human_win_percent"))))
        except Exception:
            human_pct = 15.0
        winners = find_winners()
        if human_pct <= 0.0 and not game_state.get("bonus_round"):
            winners = [w for w in winners if is_bot_uid(w[0])]
        if not winners:
            return None
        declare_winners(winners)
        return game_state["last_winners"]


def cells_needed_for_bingo(card, marked):
    marked = set(marked) | {"FREE"}
    patterns = [[(r, c) for c in range(5)] for r in range(5)]
    patterns += [[(r, c) for r in range(5)] for c in range(5)]
    patterns.append([(i, i) for i in range(5)])
    patterns.append([(i, 4 - i) for i in range(5)])
    patterns.append([(0, 0), (0, 4), (4, 0), (4, 4)])
    best = None
    for p in patterns:
        missing = [card[r][c] for r, c in p if card[r][c] not in marked]
        if best is None or len(missing) < len(best):
            best = missing
    return best or []


def _add_single_bot_card(current_bot_cards):
    available = [n for n in range(1, TOTAL_CARDS + 1) if n not in game_state["taken_card_numbers"]]
    if not available:
        return False
    card_num = random.choice(available)
    bot_uid = BOT_UID_BASE - (current_bot_cards + 1)
    bot_names_map = game_state.setdefault("bot_names", {})
    if bot_uid not in bot_names_map:
        used = set(bot_names_map.values())
        choices = [n for n in BOT_NAMES if n not in used] or BOT_NAMES
        bot_names_map[bot_uid] = random.choice(choices)

    game_state["taken_card_numbers"].add(card_num)
    game_state["player_cards"].setdefault(bot_uid, []).append(card_num)
    game_state["prize"] += CARD_COST
    game_state["card_paid_from"][(bot_uid, card_num)] = "bot"
    game_state["card_matrices"][(bot_uid, card_num)] = generate_75_bingo_card(card_num)
    game_state["marked"][(bot_uid, card_num)] = {"FREE"}
    return True


def spawn_bot_cards_tick():
    if game_state["phase"] != "selecting":
        return
    try:
        if int(get_setting("bot_enabled")) != 1:
            return
        target = max(0, min(200, int(get_setting("bot_cards_per_round"))))
    except Exception:
        return
    if target <= 0:
        return

    current_bot_cards = sum(
        len(c) for uid, c in game_state["player_cards"].items() if is_bot_uid(uid)
    )
    if current_bot_cards >= target:
        return

    time_left = max(0.0, game_state["selection_ends_at"] - time.time())
    elapsed = SELECTION_TIME_SECONDS - time_left
    if elapsed < 2.0:
        return

    # Leave a natural slight variance (90% to 100% of target, e.g., 27-30 when target is 30)
    # while ensuring fast initial burst + steady pace so high targets (30, 50, 100) are reached reliably.
    if "bot_round_target" not in game_state or game_state.get("bot_setting_ref") != target:
        if target >= 10:
            low_bound = max(1, int(round(target * 0.90)))
            game_state["bot_round_target"] = random.randint(low_bound, target)
        else:
            game_state["bot_round_target"] = target
        game_state["bot_setting_ref"] = target

    effective_target = game_state["bot_round_target"]
    if current_bot_cards >= effective_target:
        return

    needed = effective_target - current_bot_cards
    if not game_state.get("bot_initial_burst_done"):
        game_state["bot_initial_burst_done"] = True
        burst = min(needed, max(1, int(round(effective_target * 0.28))))
        for _ in range(burst):
            if _add_single_bot_card(current_bot_cards):
                current_bot_cards += 1
        return

    # Pace the remaining cards smoothly across the remaining seconds (finishing ~2s before timer ends)
    effective_time_left = max(1.0, time_left - 2.0)
    rate_per_sec = needed / effective_time_left

    if rate_per_sec >= 1.0:
        base_add = int(rate_per_sec)
        extra = 1 if random.random() < (rate_per_sec - base_add) else 0
        to_add = min(needed, base_add + extra)
    else:
        # Slightly boost probability so it finishes comfortably before selection ends
        prob = min(0.95, rate_per_sec * 1.25)
        to_add = 1 if random.random() < prob else 0

    for _ in range(to_add):
        if _add_single_bot_card(current_bot_cards):
            current_bot_cards += 1


def game_loop_worker():
    """Ends the selection phase when the timer runs out."""
    while True:
        time.sleep(1)
        try:
            with game_lock:
                if game_state["phase"] == "selecting":
                    maybe_start_bonus_round()   # v19: a bonus time may arrive while the room is empty
                    spawn_bot_cards_tick()
                if game_state["phase"] == "finished" and time.time() >= game_state["finished_ends_at"]:
                    reset_round()
                elif game_state["phase"] == "selecting" and time.time() >= game_state["selection_ends_at"]:
                    has_players = any(game_state["player_cards"].values())
                    if has_players:
                        game_state["phase"] = "playing"
                        game_state["called_numbers"] = []
                        game_state["current_number"] = None
                    else:
                        game_state["selection_ends_at"] = time.time() + SELECTION_TIME_SECONDS
        except Exception:
            logger.exception("game_loop_worker error")


def _ensure_bot_cards_for_zero_human():
    """If human_win_percent is 0 and humans joined without bots, spawn at least 1 bot card so a bot can win cleanly."""
    has_bot = any(is_bot_uid(u) and c for u, c in game_state["player_cards"].items())
    if not has_bot:
        _add_single_bot_card(0)


def call_next_number():
    """Call one number, auto-mark every card and end the round strictly following human_win_percent."""
    if game_state["phase"] != "playing":
        return
    remaining = [n for n in range(1, MAX_NUMBERS_POOL + 1) if n not in game_state["called_numbers"]]
    if not remaining:
        reset_round(refund=True)
        return

    try:
        human_pct = max(0.0, min(100.0, float(get_setting("human_win_percent"))))
    except Exception:
        human_pct = 15.0

    if human_pct <= 0.0 and not game_state.get("bonus_round"):
        _ensure_bot_cards_for_zero_human()

    if "round_favors_human" not in game_state:
        has_human = any(not is_bot_uid(u) and c for u, c in game_state["player_cards"].items())
        has_bot = any(is_bot_uid(u) and c for u, c in game_state["player_cards"].items())
        if game_state.get("bonus_round"):
            game_state["round_favors_human"] = True
        elif human_pct <= 0.0:
            game_state["round_favors_human"] = False
        elif human_pct >= 100.0:
            game_state["round_favors_human"] = True
        elif has_human and has_bot:
            game_state["round_favors_human"] = (random.random() * 100.0) < human_pct
        else:
            game_state["round_favors_human"] = has_human and not has_bot

    favors_human = bool(game_state.get("round_favors_human"))
    strict_bot_only = (human_pct <= 0.0 and not game_state.get("bonus_round"))
    call_index = len(game_state["called_numbers"]) + 1
    if "target_close_call" not in game_state:
        r = random.random()
        if r < 0.75:
            game_state["target_close_call"] = random.randint(19, 24)
        elif r < 0.93:
            game_state["target_close_call"] = random.randint(25, 30)
        else:
            game_state["target_close_call"] = random.randint(31, 36)

    target_close = max(19, game_state.get("target_close_call", 22))

    # Pick a specific designated bot card to steer to victory when bots should win
    bot_card_keys = [
        (uid, cid)
        for uid, cards in game_state["player_cards"].items()
        if is_bot_uid(uid)
        for cid in cards
    ]
    if not favors_human and bot_card_keys:
        dw = game_state.get("designated_bot_winner")
        if dw not in bot_card_keys:
            game_state["designated_bot_winner"] = random.choice(bot_card_keys)
    else:
        game_state.pop("designated_bot_winner", None)

    human_closing_nums = set()
    human_near_nums = set()
    human_advance_nums = []
    bot_closing_nums = set()
    bot_advance_nums = []
    designated_missing = []

    dw = game_state.get("designated_bot_winner")

    for uid, cards in game_state["player_cards"].items():
        is_b = is_bot_uid(uid)
        for cid in cards:
            mat = game_state["card_matrices"].get((uid, cid))
            mk = game_state["marked"].get((uid, cid), {"FREE"})
            if not mat:
                continue
            miss = [m for m in cells_needed_for_bingo(mat, mk) if m in remaining]
            if not is_b:
                if len(miss) == 1:
                    human_closing_nums.add(miss[0])
                elif len(miss) == 2:
                    human_near_nums.update(miss)
                elif len(miss) > 2:
                    human_advance_nums.extend(miss)
            else:
                if (uid, cid) == dw:
                    designated_missing = miss
                if len(miss) == 1:
                    bot_closing_nums.add(miss[0])
                elif len(miss) > 1:
                    bot_advance_nums.extend(miss)

    num = None
    if favors_human:
        safe_for_human = [n for n in remaining if n not in bot_closing_nums] or remaining
        h_close_safe = [n for n in human_closing_nums if n in safe_for_human]
        h_adv_safe = [n for n in human_advance_nums if n in safe_for_human]

        if call_index >= target_close and h_close_safe:
            num = random.choice(h_close_safe)
        elif call_index < max(5, target_close - 2):
            no_close = [n for n in safe_for_human if n not in human_closing_nums]
            if no_close and random.random() < 0.85:
                num = random.choice(no_close)
            elif h_adv_safe:
                num = random.choice(h_adv_safe)
        else:
            if h_adv_safe and random.random() < 0.65:
                num = random.choice(h_adv_safe)
            else:
                num = random.choice(safe_for_human)
    else:
        # Strictly forbid any number that would give a human BINGO
        non_human_closing = [n for n in remaining if n not in human_closing_nums]
        safe_for_bot = non_human_closing if non_human_closing else remaining
        # Also avoid advancing a human from 2-away to 1-away whenever possible
        extra_safe = [n for n in safe_for_bot if n not in human_near_nums] or safe_for_bot

        b_close_safe = [n for n in bot_closing_nums if n in safe_for_bot]
        dw_safe = [n for n in designated_missing if n in extra_safe] or [n for n in designated_missing if n in safe_for_bot]
        b_adv_safe = [n for n in bot_advance_nums if n in extra_safe] or [n for n in bot_advance_nums if n in safe_for_bot]

        if call_index >= target_close and b_close_safe:
            num = random.choice(b_close_safe)
        elif call_index < max(5, target_close - 2):
            # Advance the designated bot card steadily toward 1-away without closing before target_close
            dw_no_close = [n for n in dw_safe if n not in bot_closing_nums]
            no_close = [n for n in extra_safe if n not in bot_closing_nums]
            if dw_no_close and random.random() < 0.55:
                num = random.choice(dw_no_close)
            elif no_close and random.random() < 0.85:
                num = random.choice(no_close)
            elif b_adv_safe:
                num = random.choice(b_adv_safe)
        else:
            if dw_safe and random.random() < 0.80:
                num = random.choice(dw_safe)
            elif b_adv_safe and random.random() < 0.70:
                num = random.choice(b_adv_safe)
            else:
                num = random.choice(extra_safe)

    if num is None:
        if strict_bot_only and non_human_closing:
            num = random.choice(non_human_closing)
        else:
            num = random.choice(remaining)

    # Final hard safety guard when human_win_percent == 0: never call a number that completes any human card
    if strict_bot_only and num in human_closing_nums and non_human_closing:
        b_close_safe = [n for n in bot_closing_nums if n in non_human_closing]
        if b_close_safe:
            num = random.choice(b_close_safe)
        else:
            dw_fallback = [n for n in designated_missing if n in non_human_closing]
            num = random.choice(dw_fallback or non_human_closing)

    game_state["called_numbers"].append(num)
    game_state["current_number"] = num
    for key, mat in game_state["card_matrices"].items():
        if any(num in row for row in mat):
            game_state["marked"].setdefault(key, {"FREE"}).add(num)

    if call_index >= 19:
        winners = find_winners()
        if winners:
            if strict_bot_only:
                winners = [w for w in winners if is_bot_uid(w[0])]
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
        "rules_msg": "\U0001f4dc \u12f0\u1295\u1266\u127d\n\u2022 \u12e8\u12a0\u1295\u12f5 \u12ab\u122d\u12f5 \u12cb\u130b 10 \u1265\u122d \u1290\u12cd\n\u2022 \u12a81 \u12a5\u1235\u12a8 600 \u12ab\u122d\u12f6\u127d \u1218\u121d\u1228\u1325 \u12ed\u127b\u120b\u120d (\u1262\u1260\u12db 10 \u12ab\u122d\u12f5)\n\u2022 \u12a81 \u12a5\u1235\u12a8 75 \u12eb\u1209 \u1241\u1325\u122e\u127d \u12ed\u1320\u122b\u1209\n\u2022 \u12a0\u1295\u12f5 \u1228\u12f5\u134d\u1363 \u12a0\u121d\u12f5\u1363 \u1230\u12eb\u134d \u12c8\u12ed\u121d \u12a0\u122b\u1271 \u121b\u12d5\u12d8\u1296\u127d \u1232\u121e\u1209 \u1262\u1295\u130e \u1290\u12cd",
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


TEXT["am"]["rules_msg"] = "\U0001f4dc \u1205\u130d\u1293 \u12f0\u1295\u1266\u127d\n\n1. \u1328\u12cb\u1273\u12cd \u1219\u1209 \u1260\u1219\u1209 \u12e8\u12d5\u12f5\u120d \u1290\u12cd\u1364 \u12a5\u12eb\u1295\u12f3\u1295\u12f1 \u12d9\u122d \u1208\u1265\u127b\u12cd \u1290\u12cd\u1362\n2. \u12e8\u12a0\u1295\u12f5 \u12ab\u122d\u12f5 \u12cb\u130b 10 \u1265\u122d \u1290\u12cd\u1362 \u12a81 \u12a5\u1235\u12a8 600 \u12ab\u122d\u12f5 \u1218\u121d\u1228\u1325 \u12ed\u127b\u120b\u120d (\u1262\u1260\u12db 10 \u12ab\u122d\u12f5)\u1364 \u12e8\u1218\u121d\u1228\u132b \u130a\u12dc 50 \u1230\u12a8\u1295\u12f5 \u1290\u12cd\u1362\n3. \u12a81 \u12a5\u1235\u12a8 75 \u12eb\u1209 \u1241\u1325\u122e\u127d \u12ed\u1320\u122b\u1209\u1362 \u12a0\u1295\u12f5 \u1228\u12f5\u134d\u1363 \u12a0\u1295\u12f5 \u12d3\u121d\u12f5\u1363 \u1230\u12eb\u134d \u12c8\u12ed\u121d \u12a0\u122b\u1271 \u1325\u130d \u1232\u121e\u120b \u1262\u1295\u130e \u1290\u12cd\u1362\n4. \u12eb\u1235\u1308\u1261\u1275 \u1265\u122d \u1270\u1218\u120b\u123d \u12a0\u12ed\u12f0\u1228\u130d\u121d\u1364 \u1208\u1328\u12cb\u1273 \u12ed\u12cd\u120b\u120d\u1362 \u12dd\u1245\u1270\u129b\u12cd \u1308\u1295\u12d8\u1265 \u121b\u1235\u1308\u1262\u12eb 20 \u1265\u122d \u1290\u12cd\u1362\n5. \u121b\u12cd\u1323\u1275 \u12e8\u121a\u127b\u1208\u12cd \u12a8\u12a0\u1238\u1293\u134a\u1290\u1275 (Win) \u1202\u1233\u1265 \u1265\u127b \u1290\u12cd\u1364 \u12dd\u1245\u1270\u129b\u12cd \u12e8\u121b\u12cd\u132b \u1218\u1320\u1295 100 \u1265\u122d \u1290\u12cd\u1362\n6. \u12a821 \u12d3\u1218\u1275 \u1260\u1273\u127d \u1208\u1206\u1291 \u12ad\u120d\u12ad\u120d \u1290\u12cd\u1362\n7. \u1328\u12cb\u1273\u12cd \u1208\u1218\u12dd\u1293\u129b \u1265\u127b \u1290\u12cd\u1364 \u12aa\u1235\u12ce\u1295 \u1260\u121b\u12ed\u130e\u12f3 \u1218\u1320\u1295 \u1265\u127b \u12ed\u132b\u12c8\u1271\u1362"
TEXT["en"]["rules_msg"] = "\U0001f4dc Rules\n\n1. The game is purely a game of chance; every round stands on its own.\n2. A card costs 10 ETB. Pick cards from 1 to 600 (up to 10 cards); you have 50 seconds to choose.\n3. Numbers from 1 to 75 are called. A full row, column, diagonal or the four corners wins BINGO.\n4. Money you deposit is not refundable; it is used for playing. The minimum deposit is 20 ETB.\n5. You can withdraw only from the Win wallet; the minimum withdrawal is 100 ETB.\n6. Not allowed for anyone under 21 years old.\n7. For entertainment only: play only with an amount that does not hurt your pocket."


TEXT["am"].update({"promo": "\U0001f381 \u1355\u122e\u121e \u12ae\u12f5", "invite": "\U0001f517 \u130b\u1265\u12dd \u12a5\u1293 \u12a0\u130d\u129d"})
TEXT["en"].update({"promo": "\U0001f381 Promo code", "invite": "\U0001f517 Invite & earn"})


TEXT["am"].update({"deposit": "📥 ገቢ ለማድረግ", "withdraw": "📤 ወጪ ለማድረግ"})
TEXT["en"].update({"deposit": "📥 Deposit", "withdraw": "📤 Withdraw"})


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
        [KeyboardButton(t["deposit"]), KeyboardButton(t["rank"])],
        [KeyboardButton(t["promo"]), KeyboardButton(t["invite"])],
        [KeyboardButton(t["language"]), KeyboardButton(t["support"])],
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
            tr(user.id, "contact_ask", amount=new_user_bonus_text()), reply_markup=main_keyboard(user.id))


async def language_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user.id, user.username, user.first_name)
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("\u12a0\u121b\u122d\u129b", callback_data="lang:am"),
        InlineKeyboardButton("English", callback_data="lang:en"),
    ]])
    await update.effective_message.reply_text("\u124b\u1295\u124b \u12ed\u121d\u1228\u1321 / Choose language:", reply_markup=keyboard)


MENU_KEYS = ("play", "balance", "rules", "wallet", "rank", "language", "support", "promo", "invite", "deposit", "withdraw")
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


def deposit_instructions(uid, amt, method_key="telebirr"):
    m = deposit_method(method_key) or DEPOSIT_METHODS[0]
    bonus, pct = bonus_preview(amt)
    lines = [L(uid, "📥 ገንዘብ ማስገቢያ (%s)" % m["label"], "📥 Deposit (%s)" % m["label"]), "",
             m["icon"] + " " + L(uid, m["num_am"], m["num_en"]) + ": " + m["number"],
             "👤 " + L(uid, "ስም", "Name") + ": " + m["name"],
             "💰 " + L(uid, "መጠን", "Amount") + ": " + money(amt) + " ETB"]
    if bonus > 0:
        lines.append(L(uid, "🎁 ቦነስ: +%s ብር (%s%%) — ክፍያው በተረጋገጠበት ሰዓት ይወሰናል" % (money(bonus), money(pct)),
                       "🎁 Bonus: +%s ETB (%s%%) — decided by the payment time" % (money(bonus), money(pct))))
    hint_am = {"abyssinia": "\n(የ Bank of Abyssinia SMS ውስጥ ያለውን Reference ቁጥር ወይም የደረሰኝ ሊንክ መላክ ይችላሉ።)",
               "ebirr": "\n(የ Ebirr SMS ውስጥ ያለውን Ref ቁጥር ወይም የደረሰኝ ሊንክ መላክ ይችላሉ።)"}.get(m["key"], "")
    hint_en = {"abyssinia": "\n(You can send the Reference number from the Bank of Abyssinia SMS or the receipt link.)",
               "ebirr": "\n(You can send the Ref number from the Ebirr SMS or the receipt link.)"}.get(m["key"], "")
    lines += ["", L(uid,
                     "1️⃣ ከላይ ወዳለው ቁጥር %s ብር በ %s ይላኩ።\n"
                     "2️⃣ ከከፈሉ በኋላ ከ %s የደረሰዎትን የ SMS ጽሑፍ ሙሉ (ወይም የግብይት ቁጥሩን - Transaction ID) እዚህ ይላኩ።%s\n\n"
                     "ክፍያው ሲረጋገጥ ወዲያውኑ ወደ ሂሳብዎ ይጨመራል።" % (money(amt), m["label"], m["label"], hint_am),
                     "1️⃣ Send %s ETB to the number above with %s.\n"
                     "2️⃣ After paying, send the full SMS text from %s (or just the Transaction ID) here.%s\n\n"
                     "It is added to your balance as soon as the payment is confirmed." % (money(amt), m["label"], m["label"], hint_en))]
    return "\n".join(lines)


def method_keyboard(uid):
    rows, row = [], []
    for m in deposit_methods():
        row.append(InlineKeyboardButton(m["label"] + " " + m["icon"], callback_data="dm:" + m["key"]))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(L(uid, "❌ ሰርዝ", "❌ Cancel"), callback_data="w:cancel")])
    return InlineKeyboardMarkup(rows)


async def show_deposit_methods(message, uid):
    pct, cap = current_bonus()
    text = L(uid, "📥 ገንዘብ ማስገቢያ\n\nየክፍያ አማራጭ ይምረጡ:-", "📥 Deposit\n\nChoose a payment method:-")
    if pct > 0:
        text += L(uid, "\n\n🎁 አሁን የገንዘብ ማስገቢያ ቦነስ: %s%%" % money(pct) + (" (እስከ %s ብር)" % money(cap) if cap else ""),
                  "\n\n🎁 Deposit bonus right now: %s%%" % money(pct) + (" (up to %s ETB)" % money(cap) if cap else ""))
    await message.reply_text(text, reply_markup=method_keyboard(uid))


def total_deposited(uid):
    """Sum of all APPROVED deposits of this player (the money he really paid in; bonuses are not counted)."""
    with closing(db()) as con:
        return float(con.execute(
            "SELECT COALESCE(SUM(amount),0) FROM deposit_requests WHERE user_id=? AND status='approved'",
            (int(uid),)).fetchone()[0])


def withdraw_deposit_problem(uid):
    """v15: only players whose deposits add up to MIN_WITHDRAW_DEPOSITS or more may withdraw.
    Returns the message to show, or None when the player may withdraw."""
    if total_deposited(uid) + 1e-9 >= MIN_WITHDRAW_DEPOSITS:
        return None
    return L(uid, "⚠️ ለማውጣት ቢያንስ %d ብር ዲፖዚት ማድረግ አለብዎት" % MIN_WITHDRAW_DEPOSITS,
             "⚠️ To withdraw you must have deposited at least %d ETB." % MIN_WITHDRAW_DEPOSITS)


async def start_withdraw(message, uid, ud):
    row = get_user(uid)
    win = float(row["win_credits"]) if row else 0.0
    problem = await asyncio.get_running_loop().run_in_executor(None, withdraw_deposit_problem, uid)
    if problem:
        await message.reply_text(problem)
        return
    if win < MIN_WITHDRAW:
        await message.reply_text(L(uid,
            "📤 ገንዘብ ማውጫ (%s ብቻ)\n\nማውጣት የሚችሉት ከአሸናፊነት (Win) ሂሳብዎ ብቻ ነው።\nየ Win ሂሳብዎ: %s ብር\nዝቅተኛው የማውጫ መጠን: %d ብር" % (WITHDRAW_METHOD, money(win), MIN_WITHDRAW),
            "📤 Withdraw (%s only)\n\nYou can withdraw only from your Win wallet.\nYour Win wallet: %s ETB\nMinimum withdrawal: %d ETB" % (WITHDRAW_METHOD, money(win), MIN_WITHDRAW)))
        return
    ud["state"] = "wd_amount"
    ud.pop("wd_amount", None)
    await message.reply_text(L(uid,
        "📤 ገንዘብ ማውጫ (%s ብቻ)\n\nየ Win ሂሳብዎ: %s ብር\nስንት ብር ማውጣት ይፈልጋሉ? (ዝቅተኛ %d ብር)\nቁጥር ብቻ ይላኩ።" % (WITHDRAW_METHOD, money(win), MIN_WITHDRAW),
        "📤 Withdraw (%s only)\n\nYour Win wallet: %s ETB\nHow much do you want to withdraw? (minimum %d ETB)\nSend the number only." % (WITHDRAW_METHOD, money(win), MIN_WITHDRAW)),
        reply_markup=cancel_markup(uid))


def telebirr_phone(text):
    """The Ethiopian mobile number inside what the player typed (09xxxxxxxx), or None."""
    t = re.sub(r"[\s\-]", "", str(text or ""))
    m = re.search(r"(?<!\d)(?:\+?251|0)?([79]\d{8})(?!\d)", t)
    return ("0" + m.group(1)) if m else None


def phone_shared_problem(uid):
    """v16: True when an EARLIER account already registered the same phone number (one phone = one account)."""
    with closing(db()) as con:
        me = con.execute("SELECT phone, created_at FROM users WHERE user_id=?", (int(uid),)).fetchone()
        if not me or not me["phone"]:
            return False
        return con.execute(
            "SELECT 1 FROM users WHERE phone=? AND user_id!=? AND (created_at<? OR (created_at=? AND user_id<?))",
            (me["phone"], int(uid), me["created_at"], me["created_at"], int(uid))).fetchone() is not None


def create_withdraw(uid, amt, account_info):
    """One withdrawal request from the bot chat or the Mini App (Telebirr only). Returns (ok, message)."""
    account_info = str(account_info or "").strip()[:200]
    if not amt:
        return False, L(uid, "እባክዎ መረጃውን ይሙሉ", "Please fill in all the details.")
    problem = withdraw_deposit_problem(uid)   # v15: total deposits must reach MIN_WITHDRAW_DEPOSITS
    if problem:
        return False, problem
    if amt < MIN_WITHDRAW:
        return False, L(uid, "ዝቅተኛው የማውጫ መጠን %d ብር ነው።" % MIN_WITHDRAW, "The minimum withdrawal is %d ETB." % MIN_WITHDRAW)
    # v17: the money may go to ANY Telebirr phone number the player types (many real players could not receive
    # on the number they registered with, e.g. a Safaricom SIM). If he types no number, his registered one is used.
    reg = telebirr_phone(account_info)
    if not reg:
        if re.search(r"\d{7,}", re.sub(r"[\s\-]", "", account_info)):
            return False, L(uid, "የስልክ ቁጥሩ ትክክል አይመስልም። እባክዎ የ Telebirr ስልክ ቁጥር ያስገቡ (ለምሳሌ 0912345678)።",
                            "The phone number does not look right. Please enter a Telebirr phone number (e.g. 0912345678).")
        reg = telebirr_phone(get_phone(uid))
    if not reg:
        return False, L(uid, "እባክዎ ገንዘቡ የሚላክበትን የ Telebirr ስልክ ቁጥር ያስገቡ (ለምሳሌ 0912345678)።",
                        "Please enter the Telebirr phone number the money should be sent to (e.g. 0912345678).")
    name = re.sub(r"[+\d][\d\-\s]{6,}", " ", account_info).strip()[:60]
    stored = ("%s: %s %s" % (WITHDRAW_METHOD, reg, name)).strip()
    with game_lock:  # serialize balance check + deduction
        row = get_user(uid)
        if (row["win_credits"] if row else 0.0) < amt:
            return False, L(uid, "በአሸናፊነት ሂሳብዎ (Win Wallet) ውስጥ በቂ ገንዘብ የለም።", "Not enough money in your Win wallet.")
        update_credits(uid, -amt, is_win=True)
        with closing(db()) as con:
            cur = con.execute("INSERT INTO withdraw_requests(user_id, account_info, amount) VALUES(?,?,?)",
                              (uid, stored, amt))
            req_id = cur.lastrowid
            con.commit()
    notify_withdraw(req_id, uid, stored, amt)
    return True, L(uid, "✅ የገንዘብ ማውጫ ጥያቄዎ ተልኳል። ክፍያው ሲፈጸም መልዕክት ይደርስዎታል።",
                   "✅ Your withdrawal request was sent. You will get a message when it is paid.")


async def show_wallet(update, uid):
    row = get_user(uid)
    pct, cap = current_bonus()
    text = L(uid, "💳 Wallet\n\nPlay: %s ብር\nWin: %s ብር" % (money(row["credits"]), money(row["win_credits"])),
             "💳 Wallet\n\nPlay: %s ETB\nWin: %s ETB" % (money(row["credits"]), money(row["win_credits"])))
    if pct > 0:
        text += L(uid, "\n\n🎁 አሁን የገንዘብ ማስገቢያ ቦነስ: %s%%" % money(pct) + (" (እስከ %s ብር)" % money(cap) if cap else ""),
                  "\n\n🎁 Deposit bonus right now: %s%%" % money(pct) + (" (up to %s ETB)" % money(cap) if cap else ""))
    text += L(uid, "\n\nገንዘብ ለማስገባት ወይም ለማውጣት ከታች ያሉትን ቁልፎች ይጫኑ።",
              "\n\nTap the buttons below to deposit or withdraw.")
    rows = [[InlineKeyboardButton(L(uid, "📥 ገንዘብ ማስገባት", "📥 Deposit"), callback_data="w:dep"),
             InlineKeyboardButton(L(uid, "📤 ገንዘብ ማውጣት", "📤 Withdraw"), callback_data="w:wd")]]
    if PUBLIC_URL:
        rows.append([InlineKeyboardButton("📱 Mini App", web_app=WebAppInfo(url=webapp_url()))])
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
    reward = money(get_setting("referral_reward"))
    text = L(uid,
             "🔗 ጋብዝ እና አግኝ\n\nየእርስዎ መጋበዣ ሊንክ:\n%s\n\nጓደኛዎ በዚህ ሊንክ ገብቶ ቢያንስ %s ብር ዲፖዚት ሲያደርግ %s ብር ቦነስ ወደ መጫወቻ ሂሳብዎ ይጨመራል።\n\n"
             "👥 የጋበዟቸው: %d\n✅ ቦነስ ያገኙበት: %d\n💰 ያገኙት: %s ብር" % (link, money(get_setting("referral_min_deposit")), reward, n, paid, money(earned)),
             "🔗 Invite & earn\n\nYour invite link:\n%s\n\nWhen a friend joins with this link and deposits at least %s ETB, "
             "a %s ETB bonus is added to your Play wallet.\n\n👥 Invited: %d\n✅ Bonus earned from: %d\n💰 Earned: %s ETB" % (link, money(get_setting("referral_min_deposit")), reward, n, paid, money(earned)))
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
            await reply(L(uid, "⚠️ እባክዎ ትክክለኛ መጠን (ቁጥር ብቻ) ይላኩ። ዝቅተኛው %d ብር ነው።" % MIN_DEPOSIT,
                          "⚠️ Please send a valid amount (numbers only). The minimum is %d ETB." % MIN_DEPOSIT),
                        reply_markup=cancel_markup(uid))
            return
        ud["dep_amount"], ud["state"] = amt, "dep_ref"
        await reply(deposit_instructions(uid, amt, ud.get("dep_method")), reply_markup=cancel_markup(uid))
    elif state == "dep_ref":
        mkey = ud.get("dep_method") or "telebirr"
        ref, err = extract_reference(msg, mkey)
        if err:
            await reply(err, reply_markup=cancel_markup(uid))
            return
        status, message = await loop.run_in_executor(None, submit_deposit, uid, ud.get("dep_amount") or MIN_DEPOSIT, ref, mkey)
        if status in ("approved", "pending"):
            for k in ("state", "dep_amount", "dep_method"):
                ud.pop(k, None)
        if status != "approved":   # an approved deposit already sends its own confirmation
            await reply(message, reply_markup=(cancel_markup(uid) if status == "error" else None))
    elif state == "promo":
        ud.pop("state", None)
        result, reward, extra = await loop.run_in_executor(None, redeem_promo, uid, msg)
        await reply(promo_message(uid, result, reward, extra))
    elif state == "wd_amount":
        amt = parse_amount(msg)
        row = get_user(uid)
        win = float(row["win_credits"]) if row else 0.0
        if amt is None or amt < MIN_WITHDRAW:
            await reply(L(uid, "⚠️ ዝቅተኛው የማውጫ መጠን %d ብር ነው። ቁጥር ብቻ ይላኩ።" % MIN_WITHDRAW,
                          "⚠️ The minimum withdrawal is %d ETB. Send the number only." % MIN_WITHDRAW),
                        reply_markup=cancel_markup(uid))
            return
        if amt > win + 1e-9:
            await reply(L(uid, "⚠️ በአሸናፊነት (Win) ሂሳብዎ ውስጥ በቂ ገንዘብ የለም። የ Win ሂሳብዎ: %s ብር።" % money(win),
                          "⚠️ Not enough money in your Win wallet. Your Win wallet: %s ETB." % money(win)),
                        reply_markup=cancel_markup(uid))
            return
        ud["wd_amount"], ud["state"] = amt, "wd_account"
        await reply(L(uid, "📱 ገንዘቡ እንዲላክበት የሚፈልጉትን የ %s ስልክ ቁጥር እና ስም ይላኩ።\n\nለምሳሌ: 0912345678 አበበ ከበደ" % WITHDRAW_METHOD,
                      "📱 Send the %s phone number the money should be sent to, and the name.\n\nExample: 0912345678 Abebe Kebede" % WITHDRAW_METHOD),
                    reply_markup=cancel_markup(uid))
    elif state == "wd_account":
        ok, message = await loop.run_in_executor(None, create_withdraw, uid, ud.get("wd_amount") or 0, msg)
        if ok:
            for k in ("state", "wd_amount"):
                ud.pop(k, None)
        await reply(message, reply_markup=(None if ok else cancel_markup(uid)))


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
        for _k in ("dep_amount", "dep_method", "wd_amount"):
            ud.pop(_k, None)
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
    elif msg == t["deposit"]:
        await show_deposit_methods(update.effective_message, user.id)
    elif msg == t["withdraw"]:
        await start_withdraw(update.effective_message, user.id, ud)
    elif msg == t["promo"]:
        ud["state"] = "promo"
        await update.effective_message.reply_text(
            L(user.id, "\U0001f381 \u12e8\u1355\u122e\u121e \u12ae\u12f1\u1295 \u12ed\u120b\u12a9\u1362", "\U0001f381 Send your promo code."), reply_markup=cancel_markup(user.id))
    elif msg == t["invite"]:
        await send_invite(update, context, user.id)
    elif msg in (t["rank"], "\U0001f3c6 \u12f0\u1228\u1303", "\U0001f3c6 Rank"):
        ranking = await asyncio.get_running_loop().run_in_executor(None, card_ranking)
        lines = [L(user.id, "\U0001f3c6 \u12a8\u134d\u1270\u129b 10 \u1270\u132b\u12cb\u127e\u127d (\u1260\u12ab\u122d\u12f5 \u1265\u12db\u1275)", "\U0001f3c6 Top 10 players (by number of cards)")]
        for i, (ruid, cards) in enumerate(ranking[:RANK_SIZE], 1):
            lines.append(L(user.id, "%d. %s \u2014 %d \u12ab\u122d\u12f6\u127d", "%d. %s \u2014 %d cards") % (i, display_name(ruid), cards))
        if len(lines) == 1:
            lines.append(L(user.id, "\u1308\u1293 \u121b\u1295\u121d \u12a0\u120d\u1270\u132b\u12c8\u1270\u121d\u1362", "Nobody has played yet."))
        mine = next(((i, c) for i, (ruid, c) in enumerate(ranking, 1) if ruid == user.id), None)
        if mine:
            lines.append("")
            lines.append(L(user.id, "\u12e8\u12a5\u122d\u1235\u12ce \u12f0\u1228\u1303: %d (%d \u12ab\u122d\u12f6\u127d)", "Your rank: %d (%d cards)") % mine)
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
        await loop.run_in_executor(None, pay_referral_on_phone, user.id)   # new person + his own, unused number
        key = "contact_bonus"
    elif result == "dup":
        key = "contact_dup"
    else:
        key = "contact_saved"
    await update.effective_message.reply_text(
        tr(user.id, key, amount=new_user_bonus_text()), reply_markup=main_keyboard(user.id))


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
    elif kind == "wd" and decision in ("paid", "reject", "delete"):
        row = await loop.run_in_executor(None, review_withdraw, rid, decision)
        label = ("\u2705 \u1270\u12a8\u134d\u120f\u120d" if decision == "paid" else
                 ("🗑 ተሰርዟል (ያለ መልስ)" if decision == "delete" else
                  "\u274c \u12cd\u12f5\u1245 \u1270\u12f0\u122d\u1313\u120d (\u1265\u1229 \u1270\u1218\u120d\u1237\u120d)"))
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
    if data.startswith(("dep:", "wd:")):   # includes wd:delete:<id> (v16)
        # buttons also work inside a group, so only real admins may press them
        if user_id not in ADMIN_IDS:
            await query.answer("\U0001f6ab Unauthorized", show_alert=True)
            return
        await handle_review_button(query, data, user_id)
        return
    if data.startswith("cb:"):
        ensure_user(user_id, query.from_user.username, query.from_user.first_name)
        result, amount = await asyncio.get_running_loop().run_in_executor(None, claim_bonus, user_id, data[3:])
        await query.answer(claim_message(user_id, result, amount), show_alert=True)
        return
    await query.answer()
    ensure_user(user_id, query.from_user.username, query.from_user.first_name)
    if data.startswith(("w:", "dm:")):
        ud = context.user_data
        if data == "w:dep":
            ud.pop("state", None)
            await show_deposit_methods(query.message, user_id)
        elif data.startswith("dm:"):
            m = deposit_method(data[3:])
            if m is None or not m["number"]:
                await query.message.reply_text(L(user_id, "ይህ የክፍያ አማራጭ ለጊዜው አይሰራም።", "This payment method is not available right now."))
                return
            ud["state"] = "dep_amount"
            ud["dep_method"] = m["key"]
            ud.pop("dep_amount", None)
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass
            await query.message.reply_text(
                L(user_id, "💰 ስንት ብር ማስገባት ይፈልጋሉ? (%s)\n\nዝቅተኛው %d ብር ነው። ቁጥር ብቻ ይላኩ (ለምሳሌ 100)።" % (m["label"], MIN_DEPOSIT),
                  "💰 How much do you want to deposit? (%s)\n\nThe minimum is %d ETB. Send the number only (e.g. 100)." % (m["label"], MIN_DEPOSIT)),
                reply_markup=cancel_markup(user_id))
        elif data == "w:wd":
            await start_withdraw(query.message, user_id, ud)
        elif data == "w:cancel":
            for k in ("state", "dep_amount", "dep_method", "wd_amount"):
                ud.pop(k, None)
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass
            await query.message.reply_text(L(user_id, "ተሰርዟል።", "Cancelled."))
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
    ov = setting_con(con, "bonus_override_percent")
    if ov >= 0:   # v18: the owner set the bonus by hand (/bonus); it beats the time table until /bonus auto
        cap = setting_con(con, "bonus_override_cap")
        b = float(amount) * ov / 100.0
        if cap > 0:
            b = min(b, cap)
        return round(b + 1e-9, 2), ov
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
        ov = setting_con(con, "bonus_override_percent")
        if ov >= 0:   # v18: hand-set bonus (see /bonus)
            return (ov, setting_con(con, "bonus_override_cap"))
        best = (0.0, 0.0)
        for r in matching_rules(con, utc_now()):
            if r["percent"] > best[0]:
                best = (r["percent"], r["cap"])
        return best


def bonus_info():
    pct, cap = current_bonus()
    return {"bonus_percent": pct, "bonus_cap": cap, "pay_number": PAY_NUMBER, "pay_name": PAY_NAME,
            "min_deposit": MIN_DEPOSIT, "max_cards": MAX_CARDS_PER_PLAYER,
            "deposit_methods": [{"key": m["key"], "label": m["label"], "number": m["number"], "name": m["name"],
                                 "number_label": m["num_am"]} for m in deposit_methods()]}


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


def parse_ebirr_sms(text):
    """Incoming-money SMS of Ebirr, e.g. '[-EBIRR-KAAFI-] Ref:802787751210 Confirmed. ETB30 Received from NAME (918719332),
    Date: 15/11/26 04:00:25, ... https://receipt.ebirr.com/kaafimf/<token>'. Returns a dict or None."""
    t = text or ""
    if not re.search(r"\breceived\b", t, re.I):
        return None
    m = re.search(r"\bRef\s*[:#]?\s*(\d{6,16})", t, re.I)
    if not m:
        return None
    a = re.search(r"Confirmed\.?\s*ETB\s*([\d,]+(?:\.\d+)?)", t, re.I) or re.search(r"ETB\s*([\d,]+(?:\.\d+)?)", t, re.I)
    if not a:
        return None
    try:
        amount = float(a.group(1).replace(",", ""))
    except ValueError:
        return None
    if amount <= 0:
        return None
    paid_at = utc_now()
    d = re.search(r"Date:?\s*(\d{1,2})/(\d{1,2})/(\d{2,4})\s+(\d{1,2}):(\d{2})(?::(\d{2}))?", t)
    if d:
        try:
            year = int(d.group(3))
            if year < 100:
                year += 2000
            local = datetime(year, int(d.group(2)), int(d.group(1)), int(d.group(4)), int(d.group(5)), int(d.group(6) or 0))
            utc = local - timedelta(hours=3)
            if abs((utc - paid_at).total_seconds()) < 2 * 86400:   # sanity: ignore absurd dates
                paid_at = utc
        except ValueError:
            pass
    sender_tail = ""
    s = re.search(r"\(([0-9*+]{6,16})\)", t)
    if s:
        sender_tail = re.sub(r"\D", "", s.group(1))[-4:]
    k = re.search(r"receipt\.ebirr\.com/[^/\s]+/([A-Za-z0-9_\-]{6,40})", t)
    return {"ref": m.group(1).upper(), "alt_ref": k.group(1).upper() if k else "", "amount": amount,
            "paid_at": paid_at, "sender_tail": sender_tail, "method": "Ebirr"}


def parse_abyssinia_sms(text):
    """Bank of Abyssinia credit SMS: 'your account 8*****98 was credited with ETB 25.00 from Telebirr by Reference XXXX ...
    Receipt: https://cs.bankofabyssinia.com/slip/?trx=FT...'. Returns a dict or None."""
    t = text or ""
    a = re.search(r"credited\s+with\s+ETB\s*([\d,]+(?:\.\d+)?)", t, re.I)
    if not a:
        return None
    try:
        amount = float(a.group(1).replace(",", ""))
    except ValueError:
        return None
    if amount <= 0:
        return None
    r = re.search(r"Reference\s*[:#]?\s*([A-Za-z0-9]{6,20})", t, re.I)
    slip = re.search(r"slip/\?trx=([A-Za-z0-9]{8,30})", t)
    main = r.group(1) if r else (slip.group(1) if slip else None)
    if not main:
        return None
    alt = slip.group(1).upper() if (slip and r) else ""
    return {"ref": main.upper(), "alt_ref": alt, "amount": amount, "paid_at": utc_now(),
            "sender_tail": "", "method": "Abyssinia Bank"}


def parse_incoming_sms(text):
    """Telebirr, Ebirr or Bank of Abyssinia 'money received' SMS -> dict, otherwise None."""
    t = text or ""
    low = t.lower()
    if "abyssinia" in low:
        return parse_abyssinia_sms(t)
    if re.search(r"(?<![a-z])ebirr", low):   # not 'Telebirr' (it contains 'ebirr' too)
        return parse_ebirr_sms(t)
    if "credited with" in low:
        return parse_abyssinia_sms(t)
    p = parse_telebirr_sms(t)
    if p:
        p.setdefault("alt_ref", "")
        p.setdefault("method", "Telebirr")
    return p


def receipt_link(method, ref):
    ref = ref or ""
    if method == "Telebirr" and re.fullmatch(r"[A-Z0-9]{8,14}", ref):
        return RECEIPT_URL + ref
    if method == "Abyssinia Bank" and re.fullmatch(r"FT[A-Z0-9]{8,28}", ref):
        return "https://cs.bankofabyssinia.com/slip/?trx=" + ref
    return None


def extract_reference(text, method="telebirr"):
    """Transaction ID from what the player typed or pasted (the whole SMS, a receipt link or the bare ID).
    Understands Telebirr, Ebirr and Bank of Abyssinia messages. Returns (ref, error_message_or_None)."""
    t = str(text or "").strip()
    if not t:
        return "", "እባክዎ የግብይት ቁጥሩን ወይም የ SMS ጽሑፉን ይላኩ።"
    ref = None
    m = re.search(r"ቁጥርዎ\s*([A-Za-z0-9]{8,14})", t)
    if m:
        ref = m.group(1).upper()
        if "ልከዋል" in t:
            # a SENT-money Telebirr SMS: the masked number in brackets is the receiver - it must be the owner's number
            r = re.search(r"\(([0-9*+]{6,16})\)", t)
            if r and re.sub(r"\D", "", r.group(1))[-4:] != PAY_TAIL:
                return "", "ይህ ክፍያ ወደ %s የተላከ አይመስልም። እባክዎ ወደ ትክክለኛው ቁጥር ይክፈሉ።" % PAY_NUMBER
    if ref is None:
        m = re.search(r"/receipt/([A-Za-z0-9]{8,14})", t)
        if m:
            ref = m.group(1).upper()
    if ref is None:
        m = re.search(r"\bRef\s*[:#]?\s*(\d{6,16})", t, re.I)          # Ebirr
        if m:
            ref = m.group(1).upper()
    if ref is None:
        m = re.search(r"Reference\s*[:#]?\s*([A-Za-z0-9]{6,20})", t, re.I)   # Bank of Abyssinia
        if m:
            ref = m.group(1).upper()
    if ref is None:
        m = re.search(r"slip/\?trx=([A-Za-z0-9]{8,30})", t)             # Bank of Abyssinia receipt link
        if m:
            ref = m.group(1).upper()
    if ref is None:
        m = re.search(r"receipt\.ebirr\.com/[^/\s]+/([A-Za-z0-9_\-]{6,40})", t)   # Ebirr receipt link
        if m:
            ref = m.group(1).upper()
    if ref is None:
        ref = clean_reference(t)
    problem = reference_problem(ref)
    if problem:
        return "", problem
    return ref, None


def notify_deposit(req_id, uid, method, amount, ref, note=None):
    phone = get_phone(uid)
    when = time.strftime("%Y-%m-%d %H:%M", time.gmtime(time.time() + 3 * 3600))  # Ethiopia time
    lines = [
        "💰 አዲስ የገንዘብ ማስገቢያ ጥያቄ #%d" % req_id,
        "",
        "ተጫዋች: %s (ID %s)" % (display_name(uid), uid),
    ]
    if phone:
        lines.append("ስልክ: +%s" % phone)
    lines += [
        "ባንክ: %s" % method,
        "መጠን: %s ብር" % money(amount),
        "Transaction ID: %s" % ref,
        "ሰዓት: %s" % when,
        "",
        note or "⚠️ ከ SMS ጋር አልተጣመረም (ስልክህ ላይ ገና አልደረሰ ይሆናል)። ደረሰኙን አረጋግጥ።",
        "",
        "✅ ከማጽደቅህ በፊት አረጋግጥ:",
        "1. ቁጥሩ በራስህ የ%s ታሪክ/SMS ውስጥ አለ" % method,
        "2. መጠኑ በትክክል ይመሳሰላል (የደረሰው ብር ነው የሚጨመረው)",
        "3. ሁኔታው «ተሳክቷል» ነው",
        "4. ቁጥሩ ከዚህ በፊት በሌላ ዲፖዚት ላይ አልዋለም",
        "",
        "⚠️ ስክሪንሾት ብቻ ማስረጃ አይደለም። አጠራጣሪ ከሆነ ምንም አትጫን።",
    ]
    rows = []
    link = receipt_link(method, ref)
    if link:
        rows.append([InlineKeyboardButton("🔗 ደረሰኝ ክፈት", url=link)])
    rows.append([
        InlineKeyboardButton("✅ አጽድቅ", callback_data="dep:approve:%d" % req_id),
        InlineKeyboardButton("❌ ውድቅ አድርግ", callback_data="dep:reject:%d" % req_id),
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
        inc = find_incoming(con, ref)
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
            con.execute("UPDATE incoming_payments SET used_by_req=? WHERE id=?", (req_id, inc["id"]))
        u = con.execute("SELECT referred_by, referral_paid FROM users WHERE user_id=?", (uid,)).fetchone()
        if u and u["referred_by"] and not u["referral_paid"]:   # v16: paid only when the invited person deposits (>= referral_min_deposit)
            exists = con.execute("SELECT 1 FROM users WHERE user_id=?", (u["referred_by"],)).fetchone()
            reward_now = setting_con(con, "referral_reward")
            if exists and reward_now > 0 and amt >= setting_con(con, "referral_min_deposit"):
                inviter, ref_reward = int(u["referred_by"]), float(reward_now)
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


def find_incoming(con, ref):
    """An incoming payment whose main OR second reference number equals ref (a player may type either one)."""
    ref = str(ref or "").replace(" ", "").upper()
    if not ref:
        return None
    return con.execute("SELECT * FROM incoming_payments WHERE ref=? OR alt_ref=? ORDER BY id LIMIT 1", (ref, ref)).fetchone()


def handle_incoming_sms(p):
    """A 'money received' SMS (Telebirr / Ebirr / Bank of Abyssinia) came from the owner's phone.
    Remember it and match it to a pending request."""
    ref = p["ref"]
    alt = (p.get("alt_ref") or "").upper()
    ids = [ref] + ([alt] if alt else [])
    with closing(db()) as con:
        con.execute("BEGIN IMMEDIATE")
        for i in ids:
            if find_incoming(con, i):
                con.rollback()
                return "duplicate"
        con.execute("INSERT INTO incoming_payments(ref, alt_ref, method, amount, sender, paid_at) VALUES(?,?,?,?,?,?)",
                    (ref, alt, p.get("method") or "Telebirr", p["amount"], p.get("sender_tail") or "", fmt_db(p["paid_at"])))
        marks = ",".join("?" for _ in ids)
        req = con.execute(
            "SELECT * FROM deposit_requests WHERE UPPER(REPLACE(reference,' ','')) IN (%s) AND status='pending' "
            "ORDER BY id LIMIT 1" % marks, tuple(ids)).fetchone()
        con.commit()
    if req is None:
        return "stored"   # the player has not sent his request yet; it is approved the moment he does
    return auto_approve(req, p["amount"], p["paid_at"], p.get("sender_tail"))


def submit_deposit(uid, amt, ref, method_key="telebirr"):
    """One deposit request from the bot chat or the Mini App. Returns (status, message):
    status 'approved' | 'pending' | 'error'."""
    m = deposit_method(method_key) or DEPOSIT_METHODS[0]
    label = m["label"]
    ref = str(ref or "").replace(" ", "").upper()
    if amt < MIN_DEPOSIT:
        return "error", L(uid, "⚠️ ዝቅተኛው የገንዘብ ማስገቢያ %d ብር ነው።" % MIN_DEPOSIT,
                          "⚠️ The minimum deposit is %d ETB." % MIN_DEPOSIT)
    inc, req_id = None, None
    with closing(db()) as con:
        con.execute("BEGIN IMMEDIATE")  # check + insert as one step, so the same ID cannot slip in twice
        pending = con.execute("SELECT COUNT(*) FROM deposit_requests WHERE user_id=? AND status='pending'",
                              (uid,)).fetchone()[0]
        if pending >= MAX_PENDING_DEPOSITS:
            con.rollback()
            return "error", L(uid, "ብዙ ያልተረጋገጡ ጥያቄዎች አሉዎት። አስተዳዳሪው እስኪያረጋግጥ ድረስ ይጠብቁ።",
                              "You have too many unreviewed requests. Please wait for the administrator.")
        found = find_incoming(con, ref)
        ids = {ref}
        if found is not None:
            ids.add(found["ref"])
            if found["alt_ref"]:
                ids.add(found["alt_ref"])
        marks = ",".join("?" for _ in ids)
        used = con.execute("SELECT 1 FROM deposit_requests WHERE UPPER(REPLACE(reference,' ','')) IN (%s) "
                           "AND status IN ('pending','approved')" % marks, tuple(ids)).fetchone()
        if used:
            con.rollback()
            return "error", L(uid, "ይህ የግብይት ቁጥር ቀደም ሲል ተመዝግቧል። እባክዎ ትክክለኛውን ቁጥር ያስገቡ።",
                              "This transaction ID was already used. Please send the correct one.")
        cur = con.execute("INSERT INTO deposit_requests(user_id, method, amount, reference) VALUES(?,?,?,?)",
                          (uid, label, amt, ref))
        req_id = cur.lastrowid
        if found is not None and not found["used_by_req"]:
            inc = found
        con.commit()
    if inc is not None:
        # the owner's phone already reported this payment: verify and approve at once
        req = {"id": req_id, "user_id": uid, "method": label, "amount": amt, "reference": ref}
        res = auto_approve(req, inc["amount"], parse_db_time(inc["paid_at"]), inc["sender"])
        if res == "approved":
            return "approved", L(uid, "✅ ክፍያዎ ተረጋግጧል!", "✅ Your payment is confirmed!")
    else:
        notify_deposit(req_id, uid, label, amt, ref)
    return "pending", L(uid, "⏳ ጥያቄዎ ደርሶናል። ክፍያው ሲረጋገጥ ወዲያውኑ ወደ ሂሳብዎ ይጨመራል።",
                        "⏳ We received your request. It is added to your balance as soon as the payment is confirmed.")


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
def eth_day_start_utc():
    """v18: the start of the current Ethiopian day = 12:00 ጥዋት on the Ethiopian clock = 06:00 standard (Ethiopia
    time, UTC+3) = 03:00 UTC. Returns it as a database time string (UTC)."""
    now = utc_now()
    start = now.replace(hour=3, minute=0, second=0, microsecond=0)
    if now < start:
        start -= timedelta(days=1)
    return fmt_db(start)


def deposits_today(con, uid):
    """v18: sum of the player's APPROVED deposits made today, counted from 12 ሰዓት ጥዋት (Ethiopian clock)."""
    return float(con.execute(
        "SELECT COALESCE(SUM(amount),0) FROM deposit_requests "
        "WHERE user_id=? AND status='approved' AND COALESCE(reviewed_at, created_at) >= ?",
        (uid, eth_day_start_utc())).fetchone()[0])


def redeem_promo(uid, code):
    """Returns (result, reward, extra): ok | invalid | expired | used_up | already | need_deposit."""
    code = re.sub(r"\s+", "", str(code or "")).upper()[:40]
    if not code:
        return "invalid", 0, None
    with closing(db()) as con:
        con.execute("BEGIN IMMEDIATE")
        p = con.execute("SELECT * FROM promo_codes WHERE code=?", (code,)).fetchone()
        if p is None or not p["active"]:
            con.rollback()
            return "invalid", 0, None
        if p["expires_at"] and p["expires_at"] < fmt_db(utc_now()):
            con.rollback()
            return "expired", 0, None
        if p["max_uses"] and p["uses"] >= p["max_uses"]:
            con.rollback()
            return "used_up", 0, None
        if con.execute("SELECT 1 FROM promo_uses WHERE code=? AND user_id=?", (code, uid)).fetchone():
            con.rollback()
            return "already", 0, None
        need = float(p["min_deposit"] or 0)
        if need > 0:
            have = deposits_today(con, uid)   # v18: only deposits made today count (not yesterday's)
            if have + 1e-9 < need:
                con.rollback()
                return "need_deposit", 0, {"min": need, "have": have}
        con.execute("INSERT INTO promo_uses(code, user_id) VALUES(?,?)", (code, uid))
        con.execute("UPDATE promo_codes SET uses=uses+1 WHERE code=?", (code,))
        con.execute("UPDATE users SET credits=credits+? WHERE user_id=?", (p["reward"], uid))
        con.commit()
        return "ok", float(p["reward"]), None


def promo_message(uid, result, reward, extra=None):
    if result == "ok":
        return L(uid, "🎉 ተሳክቷል! %s ብር ወደ መጫወቻ ሂሳብዎ ተጨምሯል።" % money(reward),
                 "🎉 Done! %s ETB was added to your Play wallet." % money(reward))
    if result == "need_deposit":
        extra = extra or {}
        return L(uid, "🔒 ይህ ኮድ ዛሬ ባጠቃላይ %s ብር እና ከዚያ በላይ ገንዘብ ላስገቡ ተጫዋቾች ብቻ ነው።\nዛሬ ያስገቡት: %s ብር።" % (money(extra.get("min", 0)), money(extra.get("have", 0))),
                 "🔒 This code is only for players who deposited %s ETB or more TODAY.\nYou have deposited today: %s ETB." % (money(extra.get("min", 0)), money(extra.get("have", 0))))
    return {
        "expired": L(uid, "⌛ ይህ ኮድ ጊዜው አልፏል።", "⌛ This code has expired."),
        "used_up": L(uid, "😔 ይህ ኮድ ሙሉ በሙሉ ጥቅም ላይ ውሏል።", "😔 This code has been fully used."),
        "already": L(uid, "ℹ️ ይህን ኮድ ቀደም ብለው ተጠቅመዋል።", "ℹ️ You already used this code."),
    }.get(result, L(uid, "❌ ኮዱ ትክክል አይደለም። እባክዎ እንደገና ይሞክሩ።", "❌ Invalid code. Please try again."))


def promo_announcement(p):
    """Ready-to-send announcement text for a promo code (the owner sends it himself)."""
    lines = ["🎁 የፕሮሞ ኮድ!", "", "🔑 ኮድ: %s" % p["code"], "💰 ሽልማት: %s ብር" % money(p["reward"])]
    need = float(p.get("min_deposit") or 0)
    if need > 0:
        lines.append("✅ ለማን: ዛሬ ባጠቃላይ %s ብር እና ከዚያ በላይ ገንዘብ ላስገቡ ተጫዋቾች ብቻ" % money(need))
    else:
        lines.append("✅ ለማን: ለሁሉም ተጫዋች")
    if p.get("max_uses"):
        lines.append("👥 ለመጀመሪያዎቹ %d ሰዎች ብቻ" % int(p["max_uses"]))
    if p.get("expires_at"):
        lines.append("⏰ እስከ: %s" % (parse_db_time(p["expires_at"]) + timedelta(hours=3)).strftime("%Y-%m-%d"))
    lines += ["", "📲 በቦቱ ውስጥ «🎁 ፕሮሞ ኮድ» ን ተጭነው ኮዱን ይላኩ።"]
    return "\n".join(lines)


# ---------------- claim bonuses (created by the admin, "first N people") ----------------
def claim_bonus(uid, bid):
    """A player claims a bonus. Returns (result, amount): ok | invalid | closed | expired | full | already |
    need_phone | new_only. The count and the payment happen in one locked transaction."""
    uid = int(uid)
    try:
        bid = int(bid)
    except (TypeError, ValueError):
        return "invalid", 0
    with closing(db()) as con:
        con.execute("BEGIN IMMEDIATE")
        b = con.execute("SELECT * FROM claim_bonuses WHERE id=?", (bid,)).fetchone()
        u = con.execute("SELECT phone, created_at FROM users WHERE user_id=?", (uid,)).fetchone()
        if b is None or u is None:
            con.rollback()
            return "invalid", 0
        problem = None
        if not b["active"]:
            problem = "closed"
        elif b["expires_at"] and b["expires_at"] < fmt_db(utc_now()):
            problem = "expired"
        elif b["max_claims"] and b["claimed"] >= b["max_claims"]:
            problem = "full"
        elif con.execute("SELECT 1 FROM claim_bonus_uses WHERE bonus_id=? AND user_id=?", (bid, uid)).fetchone():
            problem = "already"
        elif not u["phone"]:
            problem = "need_phone"
        elif b["audience"] == "new" and utc_now() - parse_db_time(u["created_at"]) > timedelta(days=NEW_USER_DAYS):
            problem = "new_only"
        if problem:
            con.rollback()
            return problem, 0
        con.execute("INSERT INTO claim_bonus_uses(bonus_id, user_id) VALUES(?,?)", (bid, uid))
        con.execute("UPDATE claim_bonuses SET claimed=claimed+1 WHERE id=?", (bid,))
        con.execute("UPDATE users SET credits=credits+? WHERE user_id=?", (float(b["amount"]), uid))
        con.commit()
        return "ok", float(b["amount"])


def claim_message(uid, result, amount):
    if result == "ok":
        return L(uid, "\U0001f389 \u1270\u1233\u12ad\u1277\u120d! %s \u1265\u122d \u1266\u1290\u1235 \u12c8\u12f0 \u1218\u132b\u12c8\u127b \u1202\u1233\u1265\u12ce \u1270\u1328\u121d\u122f\u120d\u1362" % money(amount),
                 "\U0001f389 Done! A %s ETB bonus was added to your Play wallet." % money(amount))
    return {
        "full": L(uid, "\U0001f614 \u12ed\u1245\u122d\u1273\u1363 \u12ed\u1205 \u1266\u1290\u1235 \u1219\u1209 \u1260\u1219\u1209 \u1270\u12c8\u1235\u12f7\u120d\u1362", "\U0001f614 Sorry, this bonus has been fully claimed."),
        "already": L(uid, "\u2139\ufe0f \u12ed\u1205\u1295 \u1266\u1290\u1235 \u1240\u12f5\u1218\u12cd \u12c8\u1235\u12f0\u12cb\u120d\u1362", "\u2139\ufe0f You already claimed this bonus."),
        "expired": L(uid, "\u231b \u12ed\u1205 \u1266\u1290\u1235 \u12a0\u1265\u1245\u1277\u120d\u1362", "\u231b This bonus has ended."),
        "closed": L(uid, "\u231b \u12ed\u1205 \u1266\u1290\u1235 \u12a0\u1265\u1245\u1277\u120d\u1362", "\u231b This bonus has ended."),
        "need_phone": L(uid, "\U0001f4f1 \u1266\u1290\u1231\u1295 \u1208\u1218\u12cd\u1230\u12f5 \u1218\u1300\u1218\u122a\u12eb \u1260\u1266\u1271 \u12cd\u1235\u1325 \u1235\u120d\u12ad \u1241\u1325\u122d\u12ce\u1295 \u12eb\u130b\u1229\u1362",
                        "\U0001f4f1 Share your phone number in the bot first to claim this bonus."),
        "new_only": L(uid, "\u2139\ufe0f \u12ed\u1205 \u1266\u1290\u1235 \u1208\u12a0\u12f2\u1235 \u1270\u1320\u1243\u121a\u12ce\u127d \u1265\u127b \u1290\u12cd\u1362", "\u2139\ufe0f This bonus is for new players only."),
    }.get(result, L(uid, "\u274c \u1266\u1290\u1231 \u12a0\u120d\u1270\u1308\u1298\u121d\u1362", "\u274c Bonus not found."))


def claimable_bonuses(uid):
    """Claim bonuses the Mini App shows to this player."""
    uid = int(uid)
    now = fmt_db(utc_now())
    out = []
    with closing(db()) as con:
        u = con.execute("SELECT created_at FROM users WHERE user_id=?", (uid,)).fetchone()
        mine = {r[0] for r in con.execute("SELECT bonus_id FROM claim_bonus_uses WHERE user_id=?", (uid,)).fetchall()}
        for b in con.execute("SELECT * FROM claim_bonuses WHERE active=1 ORDER BY id DESC LIMIT 20").fetchall():
            if b["expires_at"] and b["expires_at"] < now:
                continue
            claimed = b["id"] in mine
            if b["max_claims"] and b["claimed"] >= b["max_claims"] and not claimed:
                continue
            eligible = True
            if b["audience"] == "new":
                eligible = bool(u) and utc_now() - parse_db_time(u["created_at"]) <= timedelta(days=NEW_USER_DAYS)
            out.append({"id": b["id"], "name": b["name"], "amount": b["amount"], "claimed": claimed,
                        "left": (max(0, b["max_claims"] - b["claimed"]) if b["max_claims"] else None),
                        "audience": b["audience"], "eligible": eligible})
    return out


def broadcast_claim_bonus(bid):
    """Send the 'claim' message with its button to every player (runs in its own thread)."""
    try:
        with closing(db()) as con:
            b = con.execute("SELECT * FROM claim_bonuses WHERE id=?", (bid,)).fetchone()
        if b is None:
            return
        ids = announce_recipients("all")
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("\U0001f381 \u1266\u1290\u1231\u1295 \u12cd\u1230\u12f5 / Claim", callback_data="cb:%d" % bid)]])
        limit_am = (" \u1208\u1218\u1300\u1218\u122a\u12eb\u12ce\u1279 %d \u1230\u12ce\u127d" % b["max_claims"]) if b["max_claims"] else ""
        limit_en = (" for the first %d people" % b["max_claims"]) if b["max_claims"] else ""
        for uid in ids:
            text = L(uid,
                     "\U0001f381 %s\n\n%s \u1265\u122d \u1266\u1290\u1235%s!\n\u12a8\u1273\u127d \u12eb\u1208\u12cd\u1295 \u1241\u120d\u134d \u12ed\u132b\u1291\u1362 \u1240\u12f5\u1218\u12cd \u1208\u121a\u12c8\u1235\u12f1 \u1265\u127b \u1290\u12cd\u1362" % (b["name"], money(b["amount"]), limit_am),
                     "\U0001f381 %s\n\nA %s ETB bonus%s!\nTap the button below. First come, first served." % (b["name"], money(b["amount"]), limit_en))
            send_to_chat(uid, text, markup)
            time.sleep(0.06)   # stay below Telegram's ~30 messages per second
        notify_admins("\U0001f4e3 \u12e8\u12ad\u120a\u121d \u1266\u1290\u1235 \"%s\" \u1208%d \u1230\u12ce\u127d \u1270\u120d\u12b3\u120d\u1362" % (b["name"], len(ids)))
    except Exception:
        logger.exception("could not broadcast the claim bonus")


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
    # v16: "delete" = remove the request and the money WITHOUT paying and WITHOUT giving it back (fake bonus money)
    status = "paid" if decision == "paid" else ("deleted" if decision == "delete" else "rejected")
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
    if status != "deleted":   # a deleted request sends no message to the player
        send_to_chat(row["user_id"], tr(row["user_id"], "wd_" + status, amount=money(row["amount"])))
    return row


# ---------------- v16: audit + removing fake bonus money (admin only) ----------------
def player_audit(con, uid):
    """Everything the owner needs to judge one player: real deposits, bonuses he received, what he took out."""
    uid = int(uid)
    dep = float(con.execute("SELECT COALESCE(SUM(amount),0) FROM deposit_requests WHERE user_id=? AND status='approved'", (uid,)).fetchone()[0])
    dep_bonus = float(con.execute("SELECT COALESCE(SUM(bonus),0) FROM deposit_requests WHERE user_id=? AND status='approved'", (uid,)).fetchone()[0])
    ref_earned = float(con.execute("SELECT COALESCE(SUM(referral_paid),0) FROM users WHERE referred_by=?", (uid,)).fetchone()[0])
    invited = int(con.execute("SELECT COUNT(*) FROM users WHERE referred_by=?", (uid,)).fetchone()[0])
    claims = float(con.execute("SELECT COALESCE(SUM(b.amount),0) FROM claim_bonus_uses u JOIN claim_bonuses b ON b.id=u.bonus_id WHERE u.user_id=?", (uid,)).fetchone()[0])
    promos = float(con.execute("SELECT COALESCE(SUM(p.reward),0) FROM promo_uses u JOIN promo_codes p ON p.code=u.code WHERE u.user_id=?", (uid,)).fetchone()[0])
    paid = float(con.execute("SELECT COALESCE(SUM(amount),0) FROM withdraw_requests WHERE user_id=? AND status='paid'", (uid,)).fetchone()[0])
    pend = float(con.execute("SELECT COALESCE(SUM(amount),0) FROM withdraw_requests WHERE user_id=? AND status='pending'", (uid,)).fetchone()[0])
    u = con.execute("SELECT credits, win_credits, phone FROM users WHERE user_id=?", (uid,)).fetchone()
    bonus_all = dep_bonus + ref_earned + claims + promos
    if dep + 1e-9 < MIN_WITHDRAW_DEPOSITS:
        flag = "\U0001f534"                      # deposited less than 100: the money came from bonus
    elif ref_earned > dep or (invited >= 5 and ref_earned >= dep * 0.5):
        flag = "\U0001f7e1"                      # real deposit, but most of the money looks like invite bonus
    else:
        flag = "\U0001f7e2"
    return {"uid": uid, "deposit": dep, "dep_bonus": dep_bonus, "ref_earned": ref_earned, "invited": invited,
            "claims": claims, "promos": promos, "bonus_all": bonus_all, "paid": paid, "pending": pend,
            "play": float(u["credits"]) if u else 0.0, "win": float(u["win_credits"]) if u else 0.0,
            "phone": (u["phone"] if u else "") or "", "flag": flag}


def audit_pending_withdrawals():
    """Pending withdrawal requests, each with its audit line (worst first)."""
    out = []
    with closing(db()) as con:
        reqs = con.execute("SELECT * FROM withdraw_requests WHERE status='pending' ORDER BY id").fetchall()
        for r in reqs:
            a = player_audit(con, r["user_id"])
            a["req_id"], a["amount"], a["account_info"] = r["id"], float(r["amount"]), r["account_info"]
            out.append(a)
    order = {"\U0001f534": 0, "\U0001f7e1": 1, "\U0001f7e2": 2}
    out.sort(key=lambda a: (order[a["flag"]], -a["amount"]))
    return out


def audit_text(a):
    return ("%s #%d  %s (ID %s)\n"
            "💸 የጠየቀው: %s ብር\n"
            "📥 ዲፖዚት: %s ብር · 🎁 ቦነስ ከዲፖዚት: %s ብር\n"
            "🔗 ጋብዞ ያገኘው: %s ብር (%d ሰው) · ክሊም/ፕሮሞ: %s ብር\n"
            "✅ እስካሁን የተከፈለው: %s ብር · Play: %s · Win: %s\n"
            "📱 ስልክ: %s\n🏦 %s"
            % (a["flag"], a["req_id"], display_name(a["uid"]), a["uid"], money(a["amount"]),
               money(a["deposit"]), money(a["dep_bonus"]), money(a["ref_earned"]), a["invited"], money(a["claims"] + a["promos"]),
               money(a["paid"]), money(a["play"]), money(a["win"]), a["phone"] or "-", a["account_info"]))


def wd_audit_markup(req_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ ተከፍሏል", callback_data="wd:paid:%d" % req_id),
         InlineKeyboardButton("❌ ውድቅ (ብሩን መልስ)", callback_data="wd:reject:%d" % req_id)],
        [InlineKeyboardButton("🗑 ሰርዝ (ያለ መልስ - ገንዘቡ ይጥፋ)", callback_data="wd:delete:%d" % req_id)]])


async def audit_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/audit - admin only: every pending withdrawal with the player's deposits and bonuses, with buttons."""
    if update.effective_user.id not in ADMIN_IDS:
        await update.effective_message.reply_text("\U0001f6ab Unauthorized.")
        return
    loop = asyncio.get_running_loop()
    rows = await loop.run_in_executor(None, audit_pending_withdrawals)
    if not rows:
        await update.effective_message.reply_text("ምንም ያልተፈጸመ የማውጫ ጥያቄ የለም።")
        return
    tot = sum(a["amount"] for a in rows)
    red = sum(a["amount"] for a in rows if a["flag"] == "🔴")
    await update.effective_message.reply_text(
        "📋 ያልተፈጸሙ የማውጫ ጥያቄዎች: %d · ጠቅላላ: %s ብር\n"
        "🔴 ዲፖዚት ከ100 ብር በታች (ገንዘቡ ከቦነስ የመጣ): %s ብር\n"
        "🟡 ከጋበዣ ቦነስ የተገኘ ይመስላል · 🟢 ትክክለኛ ይመስላል\n\n"
        "ከ🔴 ጀምሮ ይታያሉ። ለእያንዳንዱ ይወስኑ:\n"
        "✅ ተከፍሏል = ከፍለዋል | ❌ ውድቅ = ብሩ ወደ ተጫዋቹ ይመለሳል | 🗑 ሰርዝ = ብሩ ሙሉ በሙሉ ይጠፋል" % (len(rows), money(tot), money(red)))
    for a in rows[:40]:
        await update.effective_message.reply_text(audit_text(a), reply_markup=wd_audit_markup(a["req_id"]))
        await asyncio.sleep(0.05)
    if len(rows) > 40:
        await update.effective_message.reply_text("… ተጨማሪ %d ጥያቄዎች አሉ። እነዚህን ከጨረሱ በኋላ እንደገና /audit ይጠቀሙ።" % (len(rows) - 40))


async def wipe_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/wipe <user_id> - admin only: set both wallets of that player to 0 and delete his pending withdrawals
    (no refund, no message to him). Use it for accounts whose money came from fake bonuses."""
    if update.effective_user.id not in ADMIN_IDS:
        await update.effective_message.reply_text("\U0001f6ab Unauthorized.")
        return
    args = getattr(context, "args", None) or []
    if not args or not str(args[0]).isdigit():
        await update.effective_message.reply_text("አጠቃቀም: /wipe <user_id>")
        return
    uid = int(args[0])
    with closing(db()) as con:
        con.execute("BEGIN IMMEDIATE")
        u = con.execute("SELECT credits, win_credits FROM users WHERE user_id=?", (uid,)).fetchone()
        if not u:
            con.rollback()
            await update.effective_message.reply_text("ተጫዋች አልተገኘም።")
            return
        n = con.execute("UPDATE withdraw_requests SET status='deleted' WHERE user_id=? AND status='pending'", (uid,)).rowcount
        con.execute("UPDATE users SET credits=0, win_credits=0 WHERE user_id=?", (uid,))
        con.commit()
    await update.effective_message.reply_text(
        "🗑 %s (ID %d) ተሰርዟል: Play %s + Win %s ብር ጠፍቷል። የተሰረዙ የማውጫ ጥያቄዎች: %d"
        % (display_name(uid), uid, money(u["credits"]), money(u["win_credits"]), n))


async def bal_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/bal <user_id>               - show the Play and Win balance of a player
    /bal <user_id> play +50      - add 50 to his Play wallet
    /bal <user_id> win -20       - subtract 20 from his Win wallet   (a wallet never goes below 0)"""
    if update.effective_user.id not in ADMIN_IDS:
        await update.effective_message.reply_text("\U0001f6ab Unauthorized.")
        return
    args = getattr(context, "args", None) or []
    help_text = ("አጠቃቀም:\n/bal <user_id>  — ሂሳቡን ለማየት\n/bal <user_id> play +50  — ወደ Play መጨመር\n"
                 "/bal <user_id> win -20  — ከ Win መቀነስ")
    if not args or not str(args[0]).isdigit():
        await update.effective_message.reply_text(help_text)
        return
    uid = int(args[0])
    with closing(db()) as con:
        con.execute("BEGIN IMMEDIATE")
        u = con.execute("SELECT credits, win_credits FROM users WHERE user_id=?", (uid,)).fetchone()
        if not u:
            con.rollback()
            await update.effective_message.reply_text("ተጫዋች አልተገኘም።")
            return
        if len(args) == 1:
            con.rollback()
            await update.effective_message.reply_text("👤 %s (ID %d)\n🎮 Play: %s ብር\n🏆 Win: %s ብር" % (display_name(uid), uid, money(u["credits"]), money(u["win_credits"])))
            return
        col = {"play": "credits", "win": "win_credits"}.get(str(args[1]).lower())
        try:
            delta = float(str(args[2]).replace("+", "")) if len(args) > 2 else None
        except ValueError:
            delta = None
        if not col or delta is None:
            con.rollback()
            await update.effective_message.reply_text(help_text)
            return
        new = max(0.0, float(u[col]) + delta)
        con.execute("UPDATE users SET %s=? WHERE user_id=?" % col, (new, uid))
        con.commit()
    await update.effective_message.reply_text(
        "✅ %s (ID %d) %s: %s → %s ብር" % (display_name(uid), uid, "Play" if col == "credits" else "Win", money(u[col]), money(new)))


async def commission_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/commission        - show the owner's commission
    /commission 20     - set it to 20 % (the winners share the rest). Applies from the NEXT round."""
    if update.effective_user.id not in ADMIN_IDS:
        await update.effective_message.reply_text("\U0001f6ab Unauthorized.")
        return
    args = getattr(context, "args", None) or []
    loop = asyncio.get_running_loop()
    cur = await loop.run_in_executor(None, house_percent)
    running = round_house_percent()
    help_text = ("💼 የአሁኑ ተቆራጭ: %s%% (አሸናፊው %s%% ያገኛል)\n"
                 "▶️ አሁን እየሄደ ባለው ዙር: %s%%\n\n"
                 "ለመቀየር: /commission 20\n(ከ0 እስከ 90 በመቶ፤ ለውጡ ከሚቀጥለው ዙር ጀምሮ ይሠራል)"
                 % (money(cur), money(100 - cur), money(running)))
    if not args:
        await update.effective_message.reply_text(help_text)
        return
    try:
        v = float(str(args[0]).replace("%", "").replace(",", "."))
    except ValueError:
        await update.effective_message.reply_text(help_text)
        return
    if not (0 <= v <= 90):
        await update.effective_message.reply_text("⚠️ ተቆራጩ ከ0 እስከ 90 በመቶ መሆን አለበት።\n\n" + help_text)
        return
    await loop.run_in_executor(None, set_setting, "house_percent", v)
    await update.effective_message.reply_text(
        "✅ ተቆራጭ ወደ %s%% ተቀይሯል።\nአሸናፊው %s%% ያገኛል።\nከሚቀጥለው ዙር ጀምሮ ይሠራል (አሁን እየሄደ ያለው ዙር በ%s%% ይጨርሳል)።"
        % (money(v), money(100 - v), money(running)))


DAY_NAMES = ["ሰኞ", "ማክሰኞ", "ረቡዕ", "ሐሙስ", "ዓርብ", "ቅዳሜ", "እሑድ"]


def bonus_status_text():
    """v18: the deposit bonus as it is right now (hand-set value or the time table) + the table of rules."""
    with closing(db()) as con:
        ov = setting_con(con, "bonus_override_percent")
        ov_cap = setting_con(con, "bonus_override_cap")
        rules = con.execute("SELECT * FROM bonus_rules ORDER BY id").fetchall()
    lines = ["🎁 የዲፖዚት ቦነስ", ""]
    if ov >= 0:
        lines.append("✋ በእጅ የተቀመጠ (ከሰዓት ሠንጠረዥ ይበልጣል): %s%%%s" % (
            money(ov), (" (እስከ %s ብር)" % money(ov_cap)) if ov_cap > 0 else ""))
    else:
        pct, cap = current_bonus()
        lines.append("🕒 በሰዓት ሠንጠረዥ እየሠራ ነው። አሁን: %s%%%s" % (
            money(pct), (" (እስከ %s ብር)" % money(cap)) if cap else ""))
    lines += ["", "📋 የሰዓት ሠንጠረዥ:"]
    for r in rules:
        days = [int(x) for x in (r["days"] or "").split(",") if x.strip().isdigit()]
        dtxt = ", ".join(DAY_NAMES[d] for d in days if 0 <= d <= 6) if days else "ሁሉም ቀን"
        lines.append("%s #%d %s · %s · %s → %s · %s%%%s" % (
            "✅" if r["active"] else "⏸", r["id"], r["name"] or "-", dtxt,
            eth_clock(r["start_min"]), eth_clock(int(r["end_min"]) % 1440),
            money(r["percent"]), (" (እስከ %s ብር)" % money(r["cap"])) if r["cap"] else ""))
    lines += ["",
              "ለመቀየር:",
              "/bonus 50 — አሁኑኑ ለሁሉም ዲፖዚት 50% (በእጅ)",
              "/bonus 50 200 — 50%፣ ከፍተኛው 200 ብር",
              "/bonus off — ቦነስ የለም (0%)",
              "/bonus auto — ወደ ሰዓት ሠንጠረዥ መመለስ",
              "/bonus rule 1 100 200 — የሠንጠረዥ #1 ወደ 100% (ከፍተኛ 200 ብር)",
              "/bonus rule 1 off — የሠንጠረዥ #1 አጥፋ (on = አስጀምር)",
              "",
              "ለውጡ ከሚቀጥለው ዲፖዚት ማጽደቅ ጀምሮ ይሠራል። ቦነሱ ወደ Play ሂሳብ ብቻ ይገባል።"]
    return "\n".join(lines)


async def bonus_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/bonus - admin only: see and change the deposit bonus by hand (see bonus_status_text for the commands)."""
    if update.effective_user.id not in ADMIN_IDS:
        await update.effective_message.reply_text("\U0001f6ab Unauthorized.")
        return
    args = [str(a).strip() for a in (getattr(context, "args", None) or [])]
    loop = asyncio.get_running_loop()
    reply = update.effective_message.reply_text

    def num(x):
        try:
            return float(str(x).replace("%", "").replace(",", "."))
        except ValueError:
            return None

    if not args:
        await reply(await loop.run_in_executor(None, bonus_status_text))
        return
    a0 = args[0].lower()
    done = None
    if a0 == "auto":
        await loop.run_in_executor(None, set_setting, "bonus_override_percent", -1)
        await loop.run_in_executor(None, set_setting, "bonus_override_cap", 0)
        done = "✅ ወደ ሰዓት ሠንጠረዥ ተመልሷል።"
    elif a0 == "off":
        await loop.run_in_executor(None, set_setting, "bonus_override_percent", 0)
        await loop.run_in_executor(None, set_setting, "bonus_override_cap", 0)
        done = "✅ ቦነስ ተዘግቷል (0%)። ለመመለስ: /bonus auto"
    elif a0 == "rule":
        rid = int(args[1]) if len(args) > 1 and args[1].isdigit() else None
        a2 = args[2].lower() if len(args) > 2 else ""
        pct = num(a2) if a2 not in ("on", "off") else None
        if rid is None or not a2 or (a2 not in ("on", "off") and (pct is None or not 0 <= pct <= 1000)):
            await reply("⚠️ አጠቃቀም: /bonus rule <ቁጥር> <በመቶ> [ከፍተኛ ብር]  ወይም  /bonus rule <ቁጥር> on|off\n\n"
                        + await loop.run_in_executor(None, bonus_status_text))
            return
        with closing(db()) as con:
            r = con.execute("SELECT * FROM bonus_rules WHERE id=?", (rid,)).fetchone()
            if r is None:
                await reply("⚠️ ሠንጠረዥ #%d አልተገኘም።" % rid)
                return
            if a2 in ("on", "off"):
                con.execute("UPDATE bonus_rules SET active=? WHERE id=?", (1 if a2 == "on" else 0, rid))
                done = "✅ ሠንጠረዥ #%d %s።" % (rid, "ተጀምሯል" if a2 == "on" else "ቆሟል")
            else:
                cap = num(args[3]) if len(args) > 3 else float(r["cap"])
                if cap is None or cap < 0:
                    await reply("⚠️ ከፍተኛው ብር ትክክል አይደለም።")
                    return
                con.execute("UPDATE bonus_rules SET percent=?, cap=? WHERE id=?", (pct, cap, rid))
                done = "✅ ሠንጠረዥ #%d: %s%%%s" % (rid, money(pct), (" (እስከ %s ብር)" % money(cap)) if cap else "")
            con.commit()
    else:
        pct = num(a0)
        cap = num(args[1]) if len(args) > 1 else 0.0
        if pct is None or not 0 <= pct <= 1000 or cap is None or cap < 0:
            await reply("⚠️ ቁጥሩ ትክክል አይደለም።\n\n" + await loop.run_in_executor(None, bonus_status_text))
            return
        await loop.run_in_executor(None, set_setting, "bonus_override_percent", pct)
        await loop.run_in_executor(None, set_setting, "bonus_override_cap", cap)
        done = "✅ የዲፖዚት ቦነስ በእጅ ወደ %s%%%s ተቀምጧል።" % (money(pct), (" (እስከ %s ብር)" % money(cap)) if cap else "")
    await reply(done + "\n\n" + await loop.run_in_executor(None, bonus_status_text))


def roundbonus_status_text():
    with closing(db()) as con:
        slots = con.execute("SELECT * FROM round_bonus_slots ORDER BY minute").fetchall()
    lines = ["🎁 ቦነስ ዙር (ሙሉ ዝግ)", ""]
    if game_state.get("bonus_round"):
        lines.append("▶️ አሁን ያለው ዙር ቦነስ ዙር ነው: +%s ብር" % money(game_state.get("bonus_amount") or 0))
    if game_state.get("manual_bonus"):
        lines.append("⏭ ቀጣዩ ዙር ቦነስ ይሆናል: +%s ብር" % money(game_state["manual_bonus"]))
    lines.append("📋 የቦነስ ሰዓቶች (የኢትዮጵያ ሰዓት):")
    for r in slots:
        lines.append("%s #%d %s · %s (መደበኛ %02d:%02d) · +%s ብር" % (
            "✅" if r["active"] else "⏸", r["id"], r["name"] or "-", eth_clock(r["minute"]),
            r["minute"] // 60, r["minute"] % 60, money(r["amount"])))
    if not slots:
        lines.append("— ምንም የለም —")
    lines += ["",
              "ለመቀየር:",
              "/roundbonus 1 400 — የ#1 ቦነስ 400 ብር",
              "/roundbonus 1 time ቀን 6:30 — ሰዓት መቀየር (ጠዋት / ቀን / ምሽት / ሌሊት)",
              "/roundbonus 1 std 20:00 — በመደበኛ ሰዓት መቀየር",
              "/roundbonus 1 on  ወይም  /roundbonus 1 off — ማብራት / ማጥፋት",
              "/roundbonus add 300 ምሽት 2 — አዲስ ቦነስ መጨመር",
              "/roundbonus del 1 — መሰረዝ",
              "/roundbonus now 400 — ቀጣዩ ዙር ወዲያውኑ ቦነስ ዙር (400 ብር)",
              "/roundbonus cancel — «now»ን መሰረዝ",
              "",
              "ሰዓቱ ሲደርስ እየተጫወተ ያለው ጨዋታ እስኪያልቅ ይጠብቃል፤ ከዚያ ቀጣዩ ዙር ቦነስ ዙር ይሆናል።"]
    return "\n".join(lines)


async def roundbonus_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/roundbonus - admin only: the full-card bonus rounds (times and amounts)."""
    if update.effective_user.id not in ADMIN_IDS:
        await update.effective_message.reply_text("\U0001f6ab Unauthorized.")
        return
    args = [str(a).strip() for a in (getattr(context, "args", None) or [])]
    loop = asyncio.get_running_loop()
    reply = update.effective_message.reply_text

    def num(x):
        try:
            return float(str(x).replace(",", "."))
        except ValueError:
            return None

    async def status():
        return await loop.run_in_executor(None, roundbonus_status_text)

    if not args:
        await reply(await status())
        return
    a0 = args[0].lower()
    done = None
    if a0 == "now":
        amount = num(args[1]) if len(args) > 1 else None
        if not amount or amount <= 0 or amount > 100000:
            await reply("⚠️ አጠቃቀም: /roundbonus now 400")
            return
        with game_lock:
            game_state["manual_bonus"] = amount
            maybe_start_bonus_round()   # starts at once when the room is empty, otherwise with the next round
            started = bool(game_state.get("bonus_round"))
        bonus_cache_reset()
        done = ("✅ ቦነስ ዙር +%s ብር ተጀምሯል።" if started else "✅ ቀጣዩ ዙር ቦነስ ዙር (+%s ብር) ይሆናል።") % money(amount)
    elif a0 == "cancel":
        with game_lock:
            game_state["manual_bonus"] = None
        bonus_cache_reset()
        done = "✅ ተሰርዟል።"
    elif a0 == "add":
        amount = num(args[1]) if len(args) > 1 else None
        minute = parse_eth_time(args[2:4])
        if not amount or amount <= 0 or minute is None:
            await reply("⚠️ አጠቃቀም: /roundbonus add 300 ምሽት 2\n(ጠዋት/ቀን/ምሽት/ሌሊት + ሰዓት፣ ለምሳሌ ቀን 6:30)")
            return
        with closing(db()) as con:
            con.execute("INSERT INTO round_bonus_slots(name, minute, amount, active) VALUES(?,?,?,1)", ("ቦነስ", minute, amount))
            con.commit()
        bonus_cache_reset()
        done = "✅ አዲስ ቦነስ ተጨምሯል: %s · +%s ብር" % (eth_clock(minute), money(amount))
    elif a0 == "del" and len(args) > 1 and args[1].isdigit():
        with closing(db()) as con:
            n = con.execute("DELETE FROM round_bonus_slots WHERE id=?", (int(args[1]),)).rowcount
            con.commit()
        bonus_cache_reset()
        done = "✅ #%s ተሰርዟል።" % args[1] if n else "⚠️ #%s አልተገኘም።" % args[1]
    elif a0.isdigit() and len(args) > 1:
        rid, sub = int(a0), args[1].lower()
        with closing(db()) as con:
            r = con.execute("SELECT * FROM round_bonus_slots WHERE id=?", (rid,)).fetchone()
            if r is None:
                await reply("⚠️ #%d አልተገኘም።" % rid)
                return
            if sub in ("on", "off"):
                con.execute("UPDATE round_bonus_slots SET active=? WHERE id=?", (1 if sub == "on" else 0, rid))
                done = "✅ #%d %s።" % (rid, "ተጀምሯል" if sub == "on" else "ቆሟል")
            elif sub in ("time", "std"):
                if sub == "time":
                    minute = parse_eth_time(args[2:4])
                else:
                    m = re.fullmatch(r"(\d{1,2})[:.](\d{2})", args[2]) if len(args) > 2 else None
                    minute = (int(m.group(1)) * 60 + int(m.group(2))) if m and int(m.group(1)) < 24 and int(m.group(2)) < 60 else None
                if minute is None:
                    await reply("⚠️ ሰዓቱ ትክክል አይደለም።\n\n" + await status())
                    return
                con.execute("UPDATE round_bonus_slots SET minute=?, last_fired='' WHERE id=?", (minute, rid))
                done = "✅ #%d ሰዓት: %s" % (rid, eth_clock(minute))
            else:
                amount = num(sub)
                if not amount or amount <= 0 or amount > 100000:
                    await reply("⚠️ መጠኑ ትክክል አይደለም።\n\n" + await status())
                    return
                con.execute("UPDATE round_bonus_slots SET amount=? WHERE id=?", (amount, rid))
                done = "✅ #%d ቦነስ: +%s ብር" % (rid, money(amount))
            con.commit()
        bonus_cache_reset()
    else:
        await reply("⚠️ ትዕዛዙ አልገባኝም።\n\n" + await status())
        return
    await reply(done + "\n\n" + await status())


# these commands are defined after the main handler block above, so they are registered here
bot_application.add_handler(CommandHandler("bal", bal_command))
bot_application.add_handler(CommandHandler("audit", audit_command))
bot_application.add_handler(CommandHandler("wipe", wipe_command))
bot_application.add_handler(CommandHandler("commission", commission_command))
async def botcards_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.effective_message.reply_text("\U0001f6ab Unauthorized.")
        return
    args = [str(a).strip().lower() for a in (getattr(context, "args", None) or [])]
    if not args:
        en = int(get_setting("bot_enabled"))
        cnt = int(get_setting("bot_cards_per_round"))
        hp = int(get_setting("human_win_percent"))
        await update.effective_message.reply_text(
            f"🤖 ሁኔታ: {'በርቷል ✅' if en else 'ጠፍቷል ⏸'}\n"
            f"🎴 የቦት ካርድ ብዛት: {cnt}\n"
            f"👤 የሰው ማሸነፍ እድል: {hp}%\n\n"
            f"/botcards 2 — ካርድ ብዛት (0-200)\n"
            f"/botcards on / off — ማብራት/ማጥፋት\n"
            f"/humanwin 15 — የሰው ማሸነፍ መቶኛ"
        )
        return
    if args[0] in ("on", "off"):
        set_setting("bot_enabled", 1 if args[0] == "on" else 0)
        await update.effective_message.reply_text(f"✅ ቦት {'በርቷል' if args[0] == 'on' else 'ጠፍቷል'}።")
    elif args[0].isdigit() and 0 <= int(args[0]) <= 200:
        set_setting("bot_cards_per_round", int(args[0]))
        await update.effective_message.reply_text(f"✅ የቦት ካርድ ብዛት ወደ {args[0]} ተቀይሯል።")
    else:
        await update.effective_message.reply_text("⚠️ አጠቃቀም: /botcards 2  ወይም  /botcards on / off")


async def humanwin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.effective_message.reply_text("\U0001f6ab Unauthorized.")
        return
    args = [str(a).strip() for a in (getattr(context, "args", None) or [])]
    if args and args[0].isdigit() and 0 <= int(args[0]) <= 100:
        set_setting("human_win_percent", int(args[0]))
        await update.effective_message.reply_text(f"✅ የሰው ማሸነፍ መቶኛ ወደ {args[0]}% ተቀይሯል።")
    else:
        await update.effective_message.reply_text("⚠️ አጠቃቀም: /humanwin 15 (ከ0 እስከ 100)")


bot_application.add_handler(CommandHandler("bonus", bonus_command))
bot_application.add_handler(CommandHandler("roundbonus", roundbonus_command))
bot_application.add_handler(CommandHandler("botcards", botcards_command))
bot_application.add_handler(CommandHandler("humanwin", humanwin_command))


# ------------------------------ HTML & JS Assets ---------------------------
INDEX_HTML = r"""<!doctype html>
<html lang="am"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no"><meta name="theme-color" content="#06180d"><title>&#x1260;&#x123D; &#x1260;&#x123D; &#x1262;&#x1295;&#x130E;</title><script src="https://telegram.org/js/telegram-web-app.js"></script><link rel="stylesheet" href="/static/style.css"></head>
<body><main class="app-shell">
<header class="topbar">
  <div class="brand-info"><div class="eyebrow">&#x26A1; &#x1260;&#x123D; &#x1260;&#x123D; &#x1262;&#x1295;&#x130E; &#xB7; v19</div><h1 id="round-title">&#x12D9;&#x122D; #1</h1></div>
  <div class="top-scores">
    <div class="score-box"><span>PLAY</span><strong id="play-bal">0.00</strong></div>
    <div class="score-box win"><span>WIN</span><strong id="win-bal">0.00</strong></div>
    <button id="sound-btn" class="sound-btn" title="Sound">&#x1F50A;</button>
    <div class="current-call-badge" id="current">&#x2014;</div><div class="call-count" id="call-count"></div>
  </div>
</header>

<div id="prize-bar" class="prize-bar">&#x1F3C6; &#x12F0;&#x1228;&#x123D;: <b id="prize-amt">0.00</b> &#x1265;&#x122D; <span id="prize-cards"></span> <span id="prize-bonus" class="prize-bonus" style="display:none"></span></div>
<div id="bonus-live" class="bonus-live" style="display:none"></div>
<div id="bonus-next" class="bonus-next" style="display:none"></div>
<div id="phase-banner" class="phase-banner"></div>

<section class="game-panel" id="selection-panel">
  <div class="section-header">
    <h2>&#x12E8;&#x12AB;&#x122D;&#x12F5; &#x1241;&#x1325;&#x122D; &#x12ED;&#x121D;&#x1228;&#x1321; (&#x12A8;1 &#x12A5;&#x1235;&#x12A8; 600)</h2>
    <span class="card-count-pill" id="my-chosen-count">&#x12E8;&#x1270;&#x1218;&#x1228;&#x1321; &#x12AB;&#x122D;&#x12F6;&#x127D;: 0/10</span>
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
  <div id="claim-box"></div>
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
    <select id="deposit-method"></select>
    <div class="pay-box">
      <div>📞 <span id="pay-number-label">ስልክ ቁጥር</span>: <b id="pay-number">0954302323</b> <button id="copy-pay" class="copy-btn" type="button">ቅዳ</button></div>
      <div>👤 ስም: <b id="pay-name">Enyew</b> · <span id="pay-method-label">Telebirr</span></div>
    </div>
    <div id="bonus-banner" class="bonus-banner" style="display:none"></div>
    <input id="deposit-amount" type="number" placeholder="&#x1235;&#x1295;&#x1275; &#x1265;&#x122D;? (&#x1262;&#x12EB;&#x1295;&#x1235; 20)">
    <input id="deposit-reference" placeholder="Transaction ID / Ref ወይም የ SMS ጽሑፍ">
    <div class="hint">1. ከላይ ወዳለው ቁጥር በመረጡት መንገድ (Telebirr / Ebirr) ይክፈሉ። 2. ከከፈሉ በኋላ የደረሰዎትን የግብይት ቁጥር (Transaction ID / Ref) ወይም የ SMS ጽሑፉን ሙሉ ያስገቡ። ክፍያው ሲረጋገጥ ወዲያውኑ ወደ ሂሳብዎ ይጨመራል። ዝቅተኛው 20 ብር ነው፤ ያስገቡት ብር ተመላሽ አይደረግም።</div>
    <button id="send-deposit" class="primary-btn btn-deposit">&#x1325;&#x12EB;&#x1244; &#x120B;&#x12AD;</button>
  </div>
  <div id="withdraw-form" class="sub-form" style="display:none;">
    <h3>&#x1308;&#x1295;&#x12D8;&#x1265; &#x121B;&#x12CD;&#x1323;&#x1275;</h3>
    <input id="withdraw-account" placeholder="የ Telebirr ስልክ ቁጥር እና ስም (ለምሳሌ 0912345678 አበበ ከበደ)">
    <input id="withdraw-amount" type="number" placeholder="&#x1218;&#x1320;&#x1295; (&#x1265;&#x122D;)">
    <div class="hint">ገንዘብ ማውጣት በ Telebirr ብቻ ነው፤ ገንዘቡ የሚላከው እርስዎ በሚያስገቡት ስልክ ቁጥር ነው — ስልክ ቁጥሩን በትክክል ያስገቡ። ዝቅተኛው የማውጫ መጠን 100 ብር ነው። የሚወጣው ከአሸናፊነት (Win) ሂሳብ ብቻ ነው። ለማውጣት ቢያንስ 100 ብር ዲፖዚት ማድረግ አለብዎት።</div>
    <button id="send-withdraw" class="primary-btn btn-withdraw">&#x12E8;&#x121B;&#x12CD;&#x1323;&#x1275; &#x1325;&#x12EB;&#x1244; &#x120B;&#x12AD;</button>
  </div>
</div>

<div class="rank-section" id="rank-section" style="display:none;">
  <h2>&#x1F3C6; &#x12A8;&#x134D;&#x1270;&#x129B; 10 &#x1270;&#x132B;&#x12CB;&#x127E;&#x127D;</h2>
  <div class="rank-sub">&#x1260;&#x1270;&#x132B;&#x12C8;&#x1271;&#x1275; &#x12E8;&#x12AB;&#x122D;&#x12F5; &#x1265;&#x12DB;&#x1275;</div>
  <div id="rank-list">&#x1260;&#x1218;&#x132B;&#x1295; &#x120B;&#x12ED;...</div>
  <div class="rank-me" id="rank-me"></div>
</div>

<nav class="bottom-nav">
  <button class="nav-btn active" data-tab="bingo" onclick="switchTab('bingo')">&#x1F3AF; BINGO</button>
  <button class="nav-btn" data-tab="wallet" onclick="switchTab('wallet')">&#x1F4B3; WALLET</button>
  <button class="nav-btn" data-tab="rank" onclick="switchTab('rank')">&#x1F3C6; RANK</button>
  <button class="nav-btn" id="nav-rules" onclick="openRules()">&#x1F4DC; &#x1205;&#x130D;</button>
</nav>
</main>
<div id="win-overlay" class="win-overlay" style="display:none"><canvas id="confetti" class="confetti"></canvas><div class="win-box"><div class="win-title">&#x1F389; BINGO! &#x1F389;</div><div id="win-names" class="win-line"></div><div id="win-prize" class="win-line"></div><div id="win-cards" class="win-cards"></div><div id="win-call" class="win-call"></div><div id="win-bonus" class="win-bonus" style="display:none"></div><div id="win-next" class="win-sub"></div></div></div>
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
          <li><b>&#x12AB;&#x122D;&#x12F5; &#x1218;&#x121D;&#x1228;&#x1325;&#x1366;</b> &#x12A8;1 &#x12A5;&#x1235;&#x12A8; 600 &#x12AB;&#x122D;&#x12F5; &#x1218;&#x121D;&#x1228;&#x1325; &#x12ED;&#x127B;&#x120B;&#x120D;&#x1364; &#x1262;&#x1260;&#x12DB; 10 &#x12AB;&#x122D;&#x12F5; &#x12ED;&#x1348;&#x1240;&#x12F3;&#x120D;&#x1362; &#x12E8;&#x12A0;&#x1295;&#x12F5; &#x12AB;&#x122D;&#x12F5; &#x12CB;&#x130B; 10 &#x1265;&#x122D; &#x1290;&#x12CD;&#x1362; &#x12E8;&#x1218;&#x121D;&#x1228;&#x132B;&#x12CD; &#x130A;&#x12DC; 50 &#x1230;&#x12A8;&#x1295;&#x12F5; &#x1232;&#x1206;&#x1295; &#x12A8;&#x12DA;&#x12EB; &#x1260;&#x128B;&#x120B; &#x12D9;&#x1229; &#x12ED;&#x1300;&#x121D;&#x122B;&#x120D;&#x1362;</li>
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
<h2>&#x1F465; &#x1270;&#x132B;&#x12CB;&#x127E;&#x127D;</h2>
<div class="req"><div id="pl-sum">...</div><div class="grid2" style="margin-top:8px"><input type="text" id="pl-q" placeholder="&#x1235;&#x121D;&#x1363; ID &#x12C8;&#x12ED;&#x121D; &#x1235;&#x120D;&#x12AD; &#x1348;&#x120D;&#x130D;"><select id="pl-f"><option value="all">&#x1201;&#x1209;&#x121D;</option><option value="playing">&#x12A0;&#x1201;&#x1295; &#x1260;&#x1328;&#x12CB;&#x1273; &#x120B;&#x12ED;</option><option value="online">&#x12A6;&#x1295;&#x120B;&#x12ED;&#x1295;</option><option value="active">&#x1263;&#x1208;&#x1349;&#x1275; 24 &#x1230;&#x12D3;&#x1275; &#x12A0;&#x12AD;&#x1272;&#x126D;</option><option value="balance">&#x1265;&#x122D; &#x12EB;&#x120B;&#x1278;&#x12CD;</option></select></div></div>
<div id="pl-list">&#x1260;&#x1218;&#x132B;&#x1295; &#x120B;&#x12ED;...</div>
<h2>&#x1F4CA; &#x1235;&#x1273;&#x1272;&#x1235;&#x1272;&#x12AD;&#x1235;</h2><div class="stat-grid" id="stats-box"></div>
<h2>&#x1F558; &#x12E8;&#x130C;&#x121D; &#x1202;&#x1235;&#x1275;&#x122A;</h2><div id="hist-list">&#x1260;&#x1218;&#x132B;&#x1295; &#x120B;&#x12ED;...</div>
<h2>&#x12E8;&#x1308;&#x1295;&#x12D8;&#x1265; &#x121B;&#x1235;&#x1308;&#x1262;&#x12EB; &#x1325;&#x12EB;&#x1244;&#x12CE;&#x127D;</h2><div id="req-list">&#x1260;&#x1218;&#x132B;&#x1295; &#x120B;&#x12ED;...</div><h2>&#x12E8;&#x121B;&#x12CD;&#x1323;&#x1275; &#x1325;&#x12EB;&#x1244;&#x12CE;&#x127D;</h2><div id="wd-list">&#x1260;&#x1218;&#x132B;&#x1295; &#x120B;&#x12ED;...</div><h2>&#x1F4F2; SMS &#x122B;&#x1235;-&#x121B;&#x133D;&#x12F0;&#x1245;</h2><div class="req" id="sms-box">...</div>
<h2>&#x1F381; &#x12E8;&#x1266;&#x1290;&#x1235; &#x1205;&#x130E;&#x127D; (&#x12E8;&#x12A2;&#x1275;&#x12EE;&#x1335;&#x12EB; &#x1230;&#x12D3;&#x1275;)</h2><div id="rules-box"></div><button id="add-rule" class="primary-btn">&#x2795; &#x12A0;&#x12F2;&#x1235; &#x1205;&#x130D;</button>
<h2>&#x2699;&#xFE0F; &#x12E8;&#x1266;&#x1290;&#x1235; &#x1245;&#x1295;&#x1265;&#x122D;</h2>
<div class="rule-card"><div class="grid2"><div>&#x12A0;&#x12F2;&#x1235; &#x1270;&#x1320;&#x1243;&#x121A; &#x1266;&#x1290;&#x1235; (&#x1265;&#x122D;)<input type="number" id="st-nub"></div><div>&#x1218;&#x130B;&#x1260;&#x12E3; &#x1266;&#x1290;&#x1235; (&#x1265;&#x122D;)<input type="number" id="st-rr"></div></div>
<div>&#x1218;&#x130B;&#x1260;&#x12E3; &#x1266;&#x1290;&#x1235; &#x12E8;&#x121A;&#x12A8;&#x1348;&#x1208;&#x12CD;&#x1363; &#x12E8;&#x1270;&#x130B;&#x1260;&#x12D8;&#x12CD; &#x1230;&#x12CD; &#x1262;&#x12EB;&#x1295;&#x1235; &#x12E8;&#x121A;&#x12EB;&#x1235;&#x1308;&#x1263;&#x12CD; &#x1265;&#x122D;<input type="number" id="st-rmd"></div>
<div>ተቆራጭ — የባለቤት ድርሻ በመቶ (%) ከእያንዳንዱ ዙር ገንዘብ (ለውጡ ከሚቀጥለው ዙር ጀምሮ ይሠራል)<input type="number" id="st-hp" step="0.5" min="0" max="90"></div>
<div>ራስ-ሰር ተጫዋች (1 = በርቷል፣ 0 = ጠፍቷል)<input type="number" id="st-be" step="1" min="0" max="1"></div>
<div>በአንድ ዙር የሚያዙ ካርዶች ብዛት (0 - 200)<input type="number" id="st-bc" step="1" min="0" max="200"></div>
<div>የሰው ማሸነፍ መቶኛ (%) (0 - 100)<input type="number" id="st-hw" step="1" min="0" max="100"></div>
<button id="st-save" class="primary-btn">&#x1F4BE; &#x12A0;&#x1235;&#x1240;&#x121D;&#x1325;</button></div>
<h2>&#x1F381; &#x12E8;&#x12AD;&#x120A;&#x121D; &#x1266;&#x1290;&#x1236;&#x127D; (&#x1208;&#x1218;&#x1300;&#x1218;&#x122A;&#x12EB;&#x12CE;&#x1279; N &#x1230;&#x12CE;&#x127D;)</h2>
<div class="rule-card"><div class="grid2"><input type="text" id="cb-name" placeholder="&#x1235;&#x121D; (&#x1208;&#x121D;&#x1233;&#x120C; &#x12E8;&#x12DB;&#x122C; &#x1266;&#x1290;&#x1235;)"><input type="number" id="cb-amount" placeholder="&#x1266;&#x1290;&#x1235; (&#x1265;&#x122D;)"></div>
<div class="grid2"><input type="number" id="cb-max" placeholder="&#x1208;&#x1235;&#x1295;&#x1275; &#x1230;&#x12CD; (0 = &#x12EB;&#x120D;&#x1270;&#x12C8;&#x1230;&#x1290;)"><select id="cb-aud"><option value="all">&#x1208;&#x1201;&#x1209;&#x121D; &#x1270;&#x132B;&#x12CB;&#x127D;</option><option value="new">&#x1208;&#x12A0;&#x12F2;&#x1235; &#x1270;&#x1320;&#x1243;&#x121A; &#x1265;&#x127B; (7 &#x1240;&#x1295;)</option></select></div>
<div><input type="date" id="cb-exp" title="&#x12E8;&#x121A;&#x12EB;&#x1260;&#x1243;&#x1260;&#x1275; &#x1240;&#x1295;"></div>
<label><input type="checkbox" id="cb-ann" checked> &#x1208;&#x1201;&#x1209;&#x121D; &#x1270;&#x132B;&#x12CB;&#x127D; &#x1260;&#x1266;&#x1271; &#x1218;&#x120D;&#x12A5;&#x12AD;&#x1275; &#x120B;&#x12AD; (&#x1266;&#x1290;&#x1235; &#x12CD;&#x1230;&#x12F5; &#x1241;&#x120D;&#x134D; &#x12EB;&#x1208;&#x12CD;)</label>
<button id="cb-save" class="primary-btn">&#x1F381; &#x1266;&#x1290;&#x1231;&#x1295; &#x134D;&#x1320;&#x122D;</button></div><div id="claim-list"></div>
<h2>&#x1F3F7; &#x1355;&#x122E;&#x121E; &#x12AE;&#x12F6;&#x127D;</h2>
<div class="rule-card"><div class="grid2"><input type="text" id="pc-code" placeholder="&#x12AE;&#x12F5;"><input type="number" id="pc-reward" placeholder="&#x123D;&#x120D;&#x121B;&#x1275; (&#x1265;&#x122D;)"></div>
<div class="grid2"><input type="number" id="pc-max" placeholder="&#x12A8;&#x134D;&#x1270;&#x129B; &#x1270;&#x1320;&#x1243;&#x121A; &#x1265;&#x12DB;&#x1275; (0 = &#x12EB;&#x120D;&#x1270;&#x12C8;&#x1230;&#x1290;)"><input type="date" id="pc-exp" title="&#x12E8;&#x121A;&#x12EB;&#x1260;&#x1243;&#x1260;&#x1275; &#x1240;&#x1295;"></div>
<div><input type="number" id="pc-min" placeholder="ዝቅተኛ ጠቅላላ ተቀማጭ (ብር) — ለምሳሌ 100፣ 150፣ 200 (0 = ለሁሉም)"></div>
<button id="pc-save" class="primary-btn">&#x1F4BE; &#x12AE;&#x12F1;&#x1295; &#x12A0;&#x1235;&#x1240;&#x121D;&#x1325;</button></div><div id="promo-list"></div>
<h2>🎁 ቦነስ ዙር (ሙሉ ዝግ)</h2><div id="rb-box"></div><button id="rb-add" class="primary-btn">➕ አዲስ ቦነስ</button><div class="eth">ሰዓቱ ሲደርስ የሚጫወተው ጨዋታ ሲያልቅ ቀጣዩ ዙር ቦነስ ዙር ይሆናል። ደራሹ + ቦነሱ ለአሸናፊው (ወይም ለአሸናፊዎች እኩል) ይሰጣል። በቦቱ ውስጥ በ /roundbonus ም መቀየር ይችላሉ።</div>
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
.prize-bar{background:linear-gradient(90deg,#3a2f05,#6b5508,#3a2f05);border:1px solid var(--gold);padding:8px;border-radius:10px;text-align:center;font-size:15px;margin-bottom:8px;font-weight:bold;color:#fff}
.prize-bar b{color:var(--gold);font-size:22px;margin:0 2px}
.prize-bar span{font-size:12px;opacity:.85}
.phase-banner{background:#183820;border:1px solid var(--teal);padding:8px;border-radius:8px;text-align:center;font-size:12px;margin-bottom:10px;font-weight:bold;color:var(--gold)}
.win-overlay{position:fixed;inset:0;background:rgba(0,0,0,.82);display:flex;align-items:center;justify-content:center;z-index:2000;padding:20px}
.win-box{background:linear-gradient(160deg,#0b6329,#041509);border:3px solid var(--gold);border-radius:20px;padding:26px 20px;text-align:center;max-width:380px;width:100%;box-shadow:0 0 40px rgba(255,230,41,.45);animation:winpulse 1s ease-in-out infinite alternate}
.win-title{font-size:34px;font-weight:900;color:var(--gold);margin-bottom:14px}
.win-line{font-size:18px;font-weight:800;margin:6px 0;word-break:break-word;white-space:pre-line}
.win-sub{font-size:12px;color:var(--muted);margin-top:12px}
@keyframes winpulse{from{transform:scale(1)}to{transform:scale(1.04)}}
.win-cards{display:flex;flex-wrap:wrap;gap:10px;justify-content:center;margin:12px 0 4px}
.win-card{width:100%;max-width:250px}
.win-cards.multi .win-card{width:calc(50% - 5px);max-width:none}
.win-card-title{font-size:11px;font-weight:800;margin-bottom:3px;color:var(--gold);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.win-card-head{display:grid;grid-template-columns:repeat(5,1fr);gap:3px;margin-bottom:3px}
.win-card-head div{font-weight:900;font-size:13px;color:var(--gold);text-align:center}
.win-card-grid{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:3px}
.wc-cell{aspect-ratio:1;display:grid;place-items:center;border-radius:6px;background:#06180d;border:1px solid var(--line);font-weight:800;font-size:clamp(11px,3.6vw,15px);color:var(--text)}
.wc-cell.wc-mark{background:#f59e0b;color:#2a1600;border-color:#f59e0b}
.wc-cell.wc-win{background:#22c55e;color:#04210d;border-color:#86efac;box-shadow:0 0 8px #22c55e99}
.win-call{font-size:44px;font-weight:900;color:var(--gold);line-height:1.1;margin-top:8px}
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
.rank-sub{color:var(--muted);font-size:12px;margin:-4px 0 10px}
.podium{display:grid;grid-template-columns:1fr 1.15fr 1fr;gap:8px;align-items:end;margin:26px 0 14px}
.pod{background:var(--panel2);border:1px solid var(--line);border-radius:12px;padding:12px 6px 10px;text-align:center}
.pod.p1{border-color:var(--gold);box-shadow:0 0 14px rgba(255,230,41,.25);padding-top:18px;padding-bottom:16px}
.pod .medal{width:26px;height:26px;border-radius:50%;background:#7e8a82;color:#041509;font-weight:900;font-size:13px;display:flex;align-items:center;justify-content:center;margin:-25px auto 6px}
.pod.p1 .medal{background:var(--gold)}
.pod.p3 .medal{background:#d98e4a}
.pod .nm{font-size:12px;font-weight:800;word-break:break-word}
.pod .ct{font-size:11px;color:var(--gold);margin-top:3px;font-weight:700}
.rank-row{display:flex;align-items:center;gap:10px;background:var(--panel2);border:1px solid var(--line);border-left:3px solid var(--teal);border-radius:10px;padding:10px;margin-bottom:8px;font-size:13px}
.rank-row .no{width:24px;height:24px;border-radius:50%;border:1px solid var(--teal);display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:800;flex:none}
.rank-row .nm{flex:1;font-weight:700;word-break:break-word}
.rank-row .ct{color:var(--teal);font-weight:800;font-size:12px;white-space:nowrap}
.rank-me{display:flex;justify-content:space-between;align-items:center;border:1px solid var(--gold);border-radius:12px;padding:12px;margin-top:10px;font-weight:800;font-size:13px;color:var(--gold)}
.claim-card{display:flex;align-items:center;justify-content:space-between;gap:10px;background:var(--panel2);border:1px solid var(--gold);border-radius:12px;padding:10px 12px;margin-bottom:10px}
.claim-card b{display:block;font-size:14px}.claim-card span{font-size:12px;color:var(--muted)}
.claim-card button{border:0;border-radius:10px;padding:10px 14px;font-weight:900;background:var(--gold);color:#041509;cursor:pointer;white-space:nowrap}
.claim-card button:disabled{background:#3a4a40;color:#a3b8a8;cursor:default}
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
.bonus-next{background:linear-gradient(90deg,#3b0764,#7e22ce,#3b0764);border:1px solid #f0abfc;color:#fff;padding:9px 8px;border-radius:10px;text-align:center;font-size:13px;font-weight:800;margin-bottom:8px;animation:bnpulse 2s ease-in-out infinite}
.bonus-next b{color:var(--gold)}
@keyframes bnpulse{0%,100%{box-shadow:0 0 0 rgba(240,171,252,0)}50%{box-shadow:0 0 18px rgba(240,171,252,.75)}}
.bonus-live{background:linear-gradient(135deg,#ffe629,#f59e0b);color:#2a1d00;padding:9px 8px;border-radius:10px;text-align:center;font-weight:900;font-size:13px;margin-bottom:8px;animation:popb 1s ease-in-out infinite alternate}
.prize-bar.bonus{background:linear-gradient(90deg,#4a1d96,#b45309,#4a1d96);background-size:200% 100%;animation:shimmer 3s linear infinite;border-color:#fff0a0;box-shadow:0 0 22px rgba(255,230,41,.6)}
.prize-bonus{display:inline-block;margin-left:6px;padding:2px 12px;border-radius:20px;background:#ffe629;color:#2a1d00;font-size:16px;font-weight:900;animation:popb 1.2s ease-in-out infinite alternate}
@keyframes shimmer{to{background-position:200% 0}}
@keyframes popb{from{transform:scale(1)}to{transform:scale(1.08)}}
.win-box{position:relative;z-index:3}
.win-bonus{margin-top:12px;padding:12px;border-radius:16px;background:linear-gradient(135deg,#ffe629,#f59e0b);color:#2a1d00;font-weight:900;box-shadow:0 0 30px rgba(255,230,41,.8);animation:popb .7s ease-in-out infinite alternate}
.win-bonus .eq{font-size:15px}.win-bonus .tot{font-size:38px;line-height:1.15}
.confetti{position:absolute;inset:0;width:100%;height:100%;pointer-events:none;z-index:4}
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

let depKey = null;
function renderPayBox() {
  const ms = model.deposit_methods || [];
  const sel = document.getElementById('deposit-method');
  if (!sel || !ms.length) return;
  if (sel.options.length !== ms.length) {
    sel.innerHTML = '';
    ms.forEach(m => { const o = document.createElement('option'); o.value = m.key; o.textContent = m.label; sel.appendChild(o); });
    if (depKey) sel.value = depKey;
  }
  depKey = sel.value || ms[0].key;
  const m = ms.find(x => x.key === depKey) || ms[0];
  document.getElementById('pay-number').textContent = m.number;
  document.getElementById('pay-name').textContent = m.name;
  document.getElementById('pay-number-label').textContent = m.number_label || '';
  document.getElementById('pay-method-label').textContent = m.label;
}
// v15: the winner's card - winning pattern cells green, the other called (marked) numbers orange
// ---------- v19: bonus round (look + sound) ----------
let bonusStartRound = null, bonusShownRound = null, confettiRaf = null;
function fmtMoney(v) { return Number(v).toFixed(2).replace(/\.00$/, '').replace(/(\.\d)0$/, '$1'); }
function fmtMins(m) {
  const h = Math.floor(m / 60), mm = m % 60;
  return (h ? h + ' ሰዓት ' : '') + (mm || !h ? mm + ' ደቂቃ' : '');
}
function bell(f, at, dur, vol) {
  const c = getCtx(); if (!c) return;
  try {
    [1, 2.01].forEach((m, k) => {
      const o = c.createOscillator(), g = c.createGain();
      o.type = 'sine'; o.frequency.value = f * m;
      o.connect(g); g.connect(c.destination);
      g.gain.setValueAtTime(0.0001, at);
      g.gain.exponentialRampToValueAtTime(k ? vol * 0.4 : vol, at + 0.012);
      g.gain.exponentialRampToValueAtTime(0.0001, at + dur);
      o.start(at); o.stop(at + dur + 0.05);
    });
  } catch (e) {}
}
function chime() {   // the bonus round starts
  if (!soundOn) return;
  const c = getCtx(); if (!c) return;
  if (c.state === 'suspended') c.resume();
  const t = c.currentTime;
  [784, 988, 1319, 1568].forEach((f, i) => bell(f, t + i * 0.14, 0.45, 0.2));
}
function bonusFanfare() {   // the bonus is revealed
  if (!soundOn) return;
  const c = getCtx(); if (!c) return;
  if (c.state === 'suspended') c.resume();
  const t = c.currentTime;
  [523.25, 659.25, 783.99, 1046.5, 783.99, 1046.5, 1318.5, 1568].forEach((f, i) => bell(f, t + i * 0.13, 0.55, 0.22));
  for (let i = 0; i < 16; i++) bell(2000 + Math.random() * 2200, t + 1.0 + i * 0.11, 0.2, 0.07);
  [523.25, 659.25, 783.99, 1046.5].forEach(f => bell(f, t + 1.3, 1.5, 0.17));
}
function stopConfetti() {
  if (confettiRaf) { cancelAnimationFrame(confettiRaf); confettiRaf = null; }
  const cv = document.getElementById('confetti');
  if (cv) { const x = cv.getContext('2d'); x.clearRect(0, 0, cv.width, cv.height); }
}
function startConfetti() {
  const cv = document.getElementById('confetti'); if (!cv) return;
  cv.width = cv.clientWidth || window.innerWidth; cv.height = cv.clientHeight || window.innerHeight;
  const ctx = cv.getContext('2d');
  const cols = ['#ffe629', '#39d66f', '#ff4d6d', '#4dabf7', '#ffffff', '#f59e0b', '#c084fc'];
  const ps = [];
  for (let i = 0; i < 170; i++) ps.push({ x: Math.random() * cv.width, y: -20 - Math.random() * cv.height * 0.7,
    r: 4 + Math.random() * 6, c: cols[i % cols.length], vx: (Math.random() - 0.5) * 3, vy: 2 + Math.random() * 4,
    rot: Math.random() * 6, vr: (Math.random() - 0.5) * 0.3 });
  const end = performance.now() + 9500;
  function draw(now) {
    ctx.clearRect(0, 0, cv.width, cv.height);
    ps.forEach(p => {
      p.x += p.vx; p.y += p.vy; p.rot += p.vr;
      if (p.y > cv.height + 20 && now < end - 1800) { p.y = -20; p.x = Math.random() * cv.width; }
      ctx.save(); ctx.translate(p.x, p.y); ctx.rotate(p.rot); ctx.fillStyle = p.c;
      ctx.fillRect(-p.r, -p.r / 2, p.r * 2, p.r); ctx.restore();
    });
    if (now < end) confettiRaf = requestAnimationFrame(draw); else { ctx.clearRect(0, 0, cv.width, cv.height); confettiRaf = null; }
  }
  if (confettiRaf) cancelAnimationFrame(confettiRaf);
  confettiRaf = requestAnimationFrame(draw);
}
function renderBonusUI() {
  const brd = !!model.bonus_round;
  const pb = document.getElementById('prize-bar'); if (pb) pb.classList.toggle('bonus', brd);
  const chip = document.getElementById('prize-bonus');
  if (chip) { chip.style.display = brd ? 'inline-block' : 'none'; chip.textContent = brd ? ('🎁 +' + fmtMoney(model.bonus_amount) + ' ቦነስ') : ''; }
  const live = document.getElementById('bonus-live');
  if (live) {
    const show = brd && model.phase !== 'finished';
    live.style.display = show ? 'block' : 'none';
    if (show) live.textContent = '🎁 ቦነስ ዙር · ሙሉ ዝግ — ሁሉንም ቁጥር ዘግቶ የሚያጠናቅቅ ደራሹንና +' + fmtMoney(model.bonus_amount) + ' ብር ቦነሱን ይወስዳል!';
  }
  const nxt = document.getElementById('bonus-next'), nb = model.next_bonus;
  if (nxt) {
    if (!brd && nb) {
      let when = nb.due ? 'ከሚቀጥለው ዙር ይጀምራል!' : (nb.mins_left <= 180 ? ('በ ' + fmtMins(nb.mins_left) + ' ውስጥ') : nb.time_text);
      nxt.style.display = 'block';
      nxt.innerHTML = '🎁 ቀጣይ ቦነስ ዙር · ' + (nb.time_text && !nb.due && nb.mins_left <= 180 ? nb.time_text + ' · ' : '') +
        '<b>+' + fmtMoney(nb.amount) + ' ብር</b> · ሙሉ ዝግ<br>⏳ ' + when;
    } else nxt.style.display = 'none';
  }
  if (brd && bonusStartRound !== model.round && model.phase !== 'finished') { bonusStartRound = model.round; chime(); }
  if (model.phase !== 'finished') stopConfetti();
}
function renderWinBonus(w) {
  const box = document.getElementById('win-bonus'); if (!box) return;
  const wi = model.win_info;
  if (!wi || !(wi.bonus > 0)) { box.style.display = 'none'; return; }
  if (bonusShownRound === model.round) return;
  bonusShownRound = model.round;
  box.innerHTML = '<div class="eq">ደራሽ ' + fmtMoney(wi.pool) + ' + 🎁 ቦነስ ' + fmtMoney(wi.bonus) + '</div>' +
    '<div class="tot" id="win-tot">' + fmtMoney(wi.pool) + ' ብር</div>' +
    (w.length > 1 ? '<div class="eq">ለእያንዳንዳቸው ' + fmtMoney(wi.share) + ' ብር (' + w.length + ' አሸናፊዎች)</div>' : '');
  box.style.display = 'block';
  const t0 = performance.now() + 1200, dur = 2200;
  function step(now) {
    const el = document.getElementById('win-tot'); if (!el || bonusShownRound !== model.round) return;
    const p = Math.min(1, Math.max(0, (now - t0) / dur));
    el.textContent = fmtMoney(wi.pool + (wi.total - wi.pool) * p) + ' ብር';
    if (p < 1) requestAnimationFrame(step);
  }
  requestAnimationFrame(step);
  for (let i = 0; i < 12; i++) setTimeout(() => beep(500 + i * 70, 0.06, 0.1), 1200 + i * 180);
  setTimeout(startConfetti, 1100);
}
let winCardsKey = null;
function renderWinCards(winners, round) {
  const box = document.getElementById('win-cards'); if (!box) return;
  const key = round + ':' + winners.map(x => x.card_id).join(',');
  if (key === winCardsKey) return;
  winCardsKey = key;
  box.innerHTML = '';
  box.className = 'win-cards' + (winners.length > 1 ? ' multi' : '');
  winners.slice(0, 4).forEach(x => {
    if (!x.matrix) return;
    const cells = new Set((x.cells || []).map(c => c[0] + ',' + c[1]));
    const marked = new Set(x.marked || []);
    const wrap = document.createElement('div'); wrap.className = 'win-card';
    if (winners.length > 1) {
      const t = document.createElement('div'); t.className = 'win-card-title';
      t.textContent = x.name + ' #' + x.card_id; wrap.appendChild(t);
    }
    const hd = document.createElement('div'); hd.className = 'win-card-head';
    ['B', 'I', 'N', 'G', 'O'].forEach(l => { const h = document.createElement('div'); h.textContent = l; hd.appendChild(h); });
    wrap.appendChild(hd);
    const g = document.createElement('div'); g.className = 'win-card-grid';
    x.matrix.forEach((row, r) => row.forEach((n, c) => {
      const d = document.createElement('div'); d.className = 'wc-cell';
      if (n === 'FREE') { d.textContent = '★'; d.classList.add('wc-free'); }
      else d.textContent = n;
      if (cells.has(r + ',' + c)) d.classList.add('wc-win');
      else if (n !== 'FREE' && marked.has(n)) d.classList.add('wc-mark');
      g.appendChild(d);
    }));
    wrap.appendChild(g); box.appendChild(wrap);
  });
}
function render() {
  if (!model) return;
  const isBingo = tab === 'bingo';
  const isSelecting = (model.phase === 'selecting');

  document.getElementById('round-title').textContent = '\u12D9\u122D #' + model.round;
  document.getElementById('prize-amt').textContent = Number(model.prize_payout || 0).toFixed(2);
  document.getElementById('prize-cards').textContent = (model.taken_cards || []).length ? '\u00B7 ' + (model.taken_cards || []).length + ' \u12AB\u122D\u12F5' : '';
  renderBonusUI();
  document.getElementById('play-bal').textContent = model.credits.toFixed(2);
  document.getElementById('win-bal').textContent = (model.win_credits || 0).toFixed(2);
  document.getElementById('w-play').textContent = model.credits.toFixed(2);
  document.getElementById('w-win').textContent = (model.win_credits || 0).toFixed(2);
  renderPayBox();
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
    if (announcedRound !== model.round) { announcedRound = model.round; announceBingo(); if (model.win_info && model.win_info.bonus > 0) setTimeout(bonusFanfare, 1100); }
    document.getElementById('win-names').textContent = w.map(x => x.name + ' \u2014 \u12AB\u122D\u12F5 #' + x.card_id).join('\n');
    document.getElementById('win-prize').textContent = '\u123D\u120D\u121B\u1275: ' + (w[0] ? w[0].prize : 0) + ' \u1265\u122D' + (w.length > 1 ? ' (\u1208\u12A5\u12EB\u1295\u12F3\u1295\u12F3\u1278\u12CD)' : '');
    renderWinCards(w, model.round);
    renderWinBonus(w);
    document.getElementById('win-call').textContent = (model.closing_number !== null && model.closing_number !== undefined) ? String(model.closing_number) : '';
    document.getElementById('win-next').textContent = '\u12A0\u12F2\u1235 \u12D9\u122D \u1260 ' + rem + ' \u1230\u12A8\u1295\u12F5 \u12CD\u1235\u1325 \u12ED\u1300\u121D\u122B\u120D';
  } else {
    banner.style.display = 'none';
  }
  document.getElementById('my-chosen-count').textContent = '\u12E8\u1270\u1218\u1228\u1321 \u12AB\u122D\u12F6\u127D: ' + model.my_cards.length + '/' + (model.max_cards || 10);
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
  if (t === 'wallet') loadBonuses();
};

async function loadRank() {
  try {
    const d = await api('/api/miniapp/rank');
    const list = document.getElementById('rank-list'); list.innerHTML = '';
    const rows = d.rows || [];
    if (!rows.length) {
      list.textContent = '\u1308\u1293 \u121B\u1295\u121D \u12A0\u120D\u1270\u132B\u12C8\u1270\u121D';
    } else {
      const pod = document.createElement('div'); pod.className = 'podium';
      [1, 0, 2].forEach(i => {
        const r = rows[i];
        if (!r) { pod.appendChild(document.createElement('div')); return; }
        const p = document.createElement('div'); p.className = 'pod p' + (i + 1);
        const m = document.createElement('div'); m.className = 'medal'; m.textContent = String(i + 1);
        const n = document.createElement('div'); n.className = 'nm'; n.textContent = r.name;
        const c = document.createElement('div'); c.className = 'ct'; c.textContent = r.cards + ' \u12AB\u122D\u12F6\u127D';
        p.append(m, n, c); pod.appendChild(p);
      });
      list.appendChild(pod);
      rows.slice(3).forEach((r, k) => {
        const row = document.createElement('div'); row.className = 'rank-row';
        const no = document.createElement('div'); no.className = 'no'; no.textContent = String(k + 4);
        const n = document.createElement('div'); n.className = 'nm'; n.textContent = r.name;
        const c = document.createElement('div'); c.className = 'ct'; c.textContent = r.cards + ' \u12AB\u122D\u12F6\u127D';
        row.append(no, n, c); list.appendChild(row);
      });
    }
    const me = document.getElementById('rank-me'); me.innerHTML = '';
    const a = document.createElement('span'); a.textContent = '\u12E8\u12A5\u122D\u1235\u12CE \u12F0\u1228\u1303: ' + (d.me ? ('#' + d.me.rank) : '\u2014');
    const b = document.createElement('span'); b.textContent = (d.me ? d.me.cards : 0) + ' \u12AB\u122D\u12F6\u127D';
    me.append(a, b);
  } catch (e) { toast(e.message); }
}

async function loadBonuses() {
  const box = document.getElementById('claim-box'); if (!box) return;
  try {
    const d = await api('/api/miniapp/bonuses');
    box.innerHTML = '';
    (d.bonuses || []).forEach(b => {
      const card = document.createElement('div'); card.className = 'claim-card';
      const info = document.createElement('div');
      const t = document.createElement('b'); t.textContent = '\uD83C\uDF81 ' + b.name + ' \u2014 ' + b.amount + ' \u1265\u122D';
      const s = document.createElement('span'); s.textContent = (b.left !== null && b.left !== undefined) ? ('\u12E8\u1240\u1229: ' + b.left) : '\u1208\u1201\u1209\u121D';
      info.append(t, s);
      const btn = document.createElement('button');
      btn.textContent = b.claimed ? '\u2705 \u12C8\u1235\u12F0\u12CB\u120D' : (b.eligible ? '\u12CD\u1230\u12F5' : '\u1208\u12A0\u12F2\u1235 \u1265\u127B');
      btn.disabled = !!b.claimed || !b.eligible;
      btn.onclick = async () => { await action('/api/miniapp/claim-bonus', { id: b.id }); loadBonuses(); };
      card.append(info, btn); box.appendChild(card);
    });
  } catch (e) {}
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
document.getElementById('deposit-method').onchange = () => { depKey = document.getElementById('deposit-method').value; renderPayBox(); };
document.getElementById('copy-pay').onclick = () => {
  const n = document.getElementById('pay-number').textContent;
  try { navigator.clipboard.writeText(n).then(() => toast('\u1270\u1240\u12F5\u1277\u120D'), () => toast(n)); } catch (e) { toast(n); }
};
document.getElementById('send-deposit').onclick = () => {
  const amount = Number(document.getElementById('deposit-amount').value);
  const reference = document.getElementById('deposit-reference').value.trim();
  if (!amount || reference.length < 3) { toast('\u12A5\u1263\u12AD\u12CE \u1218\u1228\u1303\u12CD\u1295 \u12ED\u1219\u1209'); return; }
  if (amount < 20) { toast('\u12DD\u1245\u1270\u129B\u12CD \u1308\u1295\u12D8\u1265 \u121B\u1235\u1308\u1262\u12EB 20 \u1265\u122D \u1290\u12CD'); return; }
  action('/api/miniapp/deposit-request', { amount, method: (document.getElementById('deposit-method') || {}).value || 'telebirr', reference })
    .then(() => { document.getElementById('deposit-reference').value = ''; });
};
document.getElementById('send-withdraw').onclick = () => {
  const amount = Number(document.getElementById('withdraw-amount').value);
  const account_info = document.getElementById('withdraw-account').value.trim();
  if (amount && amount < 100) { toast('\u12DD\u1245\u1270\u129B\u12CD \u12E8\u121B\u12CD\u132B \u1218\u1320\u1295 100 \u1265\u122D \u1290\u12CD'); return; }
  if (!amount) { toast('\u12A5\u1263\u12AD\u12CE \u1218\u1228\u1303\u12CD\u1295 \u12ED\u1219\u1209'); return; }
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
// the rules open only when the player taps the rules button (no automatic popup)

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
    `<p>\u12F5\u122D\u123B\u12EC: <b>${birr(o.house)} \u1265\u122D</b></p>` +
    `<p>\u1208\u12A0\u1238\u1293\u134A\u12CE\u127D \u12E8\u1270\u12A8\u1348\u1208: ${birr(o.payout)} \u1265\u122D</p>` +
    `<p>\u12E8\u1308\u1263 \u12F2\u1356\u12DA\u1275 (\u12E8\u1338\u12F0\u1240): ${birr(o.deposits)} \u1265\u122D</p>` +
    (Number(o.bonus) > 0 ? `<p>🎁 ቦነስ የተከፈለ: ${birr(o.bonus)} ብር · ትርፍ ከቦነስ በኋላ: <b>${birr(o.house - o.bonus)} ብር</b></p>` : '') +
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
      (Number(h.bonus) > 0 ? `<p>🎁 ቦነስ ዙር: <b>+${birr(h.bonus)} ብር</b> (ከባለቤቱ)</p>` : '') +
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
const ETH_PERIODS = [['morning', 'ጠዋት'], ['afternoon', 'ከቀኑ'], ['evening', 'ምሽት'], ['night', 'ሌሊት']];
const ETH_HOURS = {morning: [12, 1, 2, 3, 4, 5], afternoon: [6, 7, 8, 9, 10, 11], evening: [12, 1, 2, 3, 4, 5], night: [6, 7, 8, 9, 10, 11]};
function minToEth(min) {
  const h = Math.floor(min / 60) % 24, m = min % 60;
  let eh = ((h - 6) % 12 + 12) % 12; if (eh === 0) eh = 12;
  const p = (h >= 6 && h < 12) ? 'morning' : (h >= 12 && h < 18) ? 'afternoon' : (h >= 18 ? 'evening' : 'night');
  return {p, eh, m};
}
function ethToMin(p, eh, m) {
  const base = {morning: 6, afternoon: 12, evening: 18, night: 0}[p];
  const first = (p === 'morning' || p === 'evening') ? 0 : 6;
  const off = (((eh % 12) - first) % 12 + 12) % 12;
  return (base + off) * 60 + m;
}
function ethMinOpts(cur) {
  const list = []; for (let k = 0; k < 60; k += 5) list.push(k);
  if (!list.includes(cur)) list.push(cur);
  list.sort((a, b) => a - b);
  return list.map(k => `<option value="${k}" ${k === cur ? 'selected' : ''}>${String(k).padStart(2, '0')}</option>`).join('');
}
function ethSel(i, w, min) {
  const e = minToEth(min);
  const per = ETH_PERIODS.map(x => `<option value="${x[0]}" ${x[0] === e.p ? 'selected' : ''}>${x[1]}</option>`).join('');
  const hrs = ETH_HOURS[e.p].map(h => `<option value="${h}" ${h === e.eh ? 'selected' : ''}>${h}</option>`).join('');
  return `<div style="display:flex;gap:4px"><select id="r${i}-${w}-p" onchange="ethPeriod(${i},'${w}')">${per}</select>` +
    `<select id="r${i}-${w}-h" onchange="ethSync(${i},'${w}')">${hrs}</select>` +
    `<select id="r${i}-${w}-m" onchange="ethSync(${i},'${w}')">${ethMinOpts(e.m)}</select></div>`;
}
function ethPeriod(i, w) {
  const p = document.getElementById('r' + i + '-' + w + '-p').value;
  document.getElementById('r' + i + '-' + w + '-h').innerHTML = ETH_HOURS[p].map(h => `<option value="${h}">${h}</option>`).join('');
  ethSync(i, w);
}
function ethSync(i, w) {
  const g = id => document.getElementById('r' + i + '-' + w + '-' + id);
  const min = ethToMin(g('p').value, Number(g('h').value), Number(g('m').value));
  document.getElementById('r' + i + '-' + w).value = hhmm(min);
  ethUpd(i);
}
function stdSync(i, w) {
  const v = document.getElementById('r' + i + '-' + w).value;
  const e = minToEth(toMin(v, w === 'e'));
  document.getElementById('r' + i + '-' + w + '-p').value = e.p;
  document.getElementById('r' + i + '-' + w + '-h').innerHTML = ETH_HOURS[e.p].map(h => `<option value="${h}" ${h === e.eh ? 'selected' : ''}>${h}</option>`).join('');
  document.getElementById('r' + i + '-' + w + '-m').innerHTML = ethMinOpts(e.m);
  ethUpd(i);
}
function ruleCard(r, i) {
  const days = String(r.days || '').split(',').filter(x => x !== '').map(Number);
  const dayBoxes = DAYN.map((n, d) => `<label class="d"><input type="checkbox" class="r${i}-d" value="${d}" ${days.includes(d) ? 'checked' : ''}> ${n}</label>`).join('');
  return `<div class="rule-card"><input type="text" id="r${i}-n" placeholder="ስም" value="${esc(r.name)}">` +
    `<div>${dayBoxes}<div class="eth">ምንም ቀን ካልተመረጠ በሁሉም ቀናት ይሰራል</div></div>` +
    `<div class="grid2"><div>ከ (የኢትዮጵያ ሰዓት)${ethSel(i, 's', r.start_min)}</div><div>እስከ (የኢትዮጵያ ሰዓት)${ethSel(i, 'e', r.end_min)}</div></div>` +
    `<div class="grid2"><div>ከ (መደበኛ ሰዓት)<input type="time" id="r${i}-s" value="${hhmm(r.start_min)}" oninput="stdSync(${i},'s')"></div>` +
    `<div>እስከ (መደበኛ ሰዓት)<input type="time" id="r${i}-e" value="${hhmm(r.end_min % 1440)}" oninput="stdSync(${i},'e')"></div></div><div class="eth" id="r${i}-eth"></div>` +
    `<div class="grid2"><div>ቦነስ %<input type="number" id="r${i}-p" value="${Number(r.percent)}"></div>` +
    `<div>ከፍተኛ (ብር, 0 = ገደብ የለም)<input type="number" id="r${i}-c" value="${Number(r.cap)}"></div></div>` +
    `<label><input type="checkbox" id="r${i}-a" ${r.active ? 'checked' : ''}> ንቁ</label> ` +
    `<button onclick="saveRule(${i})" style="${BTN}background:#39d66f">💾 አስቀምጥ</button>` +
    (r.id ? `<button onclick="delRule(${i})" style="${BTN}background:#d63939;color:#fff">🗑 ሰርዝ</button>` : '') + `</div>`;
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
    max_uses: Number(document.getElementById('pc-max').value || 0), min_deposit: Number(document.getElementById('pc-min').value || 0),
    expires: document.getElementById('pc-exp').value});
  if (ok) { ['pc-code', 'pc-reward', 'pc-max', 'pc-min', 'pc-exp'].forEach(x => document.getElementById(x).value = ''); loadSettings(); }
};
let PROMOS = [];
async function promoAct(i, what) {
  const code = PROMOS[i].code;
  if (what === 'delete' && !confirm('ኮዱ ይሰረዝ?')) return;
  if (await postJson('/api/admin/promo', {code, [what]: true})) loadSettings();
}
function showPromoAnn(i) {
  const box = document.getElementById('pu-' + i);
  if (box.dataset.k === 'ann') { box.innerHTML = ''; box.dataset.k = ''; return; }
  box.dataset.k = 'ann';
  box.innerHTML = `<textarea readonly rows="9" style="width:100%;margin-top:6px" id="pa-${i}">${esc(PROMOS[i].announcement)}</textarea>` +
    `<button onclick="copyAnn(${i})" style="${BTN}background:#39d66f">📋 ቅዳ</button>`;
}
function copyAnn(i) {
  const ta = document.getElementById('pa-' + i); ta.select();
  try { navigator.clipboard.writeText(ta.value); } catch (e) { try { document.execCommand('copy'); } catch (e2) {} }
}
async function showPromoUsers(i) {
  const box = document.getElementById('pu-' + i);
  if (box.dataset.k === 'users') { box.innerHTML = ''; box.dataset.k = ''; return; }
  box.dataset.k = 'users'; box.textContent = '...';
  const r = await fetch('/api/admin/promo-uses?code=' + encodeURIComponent(PROMOS[i].code), {headers: HEADERS()});
  const d = await r.json();
  if (!r.ok) { box.textContent = d.error || 'Error'; return; }
  box.innerHTML = `<p><b>ወሰዱት: ${Number(d.count)} ሰዎች</b></p>` + (d.users || []).map(u =>
    `<div class="pl">${esc(u.name)} (ID ${esc(u.user_id)}) · ${esc(fmtTime(u.at))}</div>`).join('');
}
function renderPromos(list) {
  PROMOS = list;
  document.getElementById('promo-list').innerHTML = list.map((p, i) =>
    `<div class="req"><b>${esc(p.code)}</b> · ${birr(p.reward)} ብር ${p.active ? '✅' : '⏸'}` +
    `<p>የተጠቀሙ: ${Number(p.uses)}${p.max_uses ? ' / ' + Number(p.max_uses) : ' (ያልተወሰነ)'}` +
    (Number(p.min_deposit) > 0 ? ` · 🔒 ዛሬ ባጠቃላይ ${birr(p.min_deposit)} ብር+ ላስገቡ ብቻ` : ' · ለሁሉም') +
    (p.expires_at ? ` · እስከ: ${esc(fmtTime(p.expires_at))}` : '') + `</p>` +
    `<button onclick="promoAct(${i},'toggle')" style="${BTN}background:#ffe629">${p.active ? 'አቁም' : 'አስጀምር'}</button>` +
    `<button onclick="showPromoUsers(${i})" style="${BTN}background:#102d19;color:#f4f7f4;border:1px solid #20552d">👥 ማን ወሰደ</button>` +
    `<button onclick="showPromoAnn(${i})" style="${BTN}background:#102d19;color:#f4f7f4;border:1px solid #20552d">📣 ማስታወቂያ ጽሑፍ</button>` +
    `<button onclick="promoAct(${i},'delete')" style="${BTN}background:#d63939;color:#fff">🗑</button>` +
    `<div id="pu-${i}"></div></div>`).join('') || '—';
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
    `<div class="req"><p>\u123D\u120D\u121B\u1275: ${birr(r.reward)} \u1265\u122D \u1266\u1290\u1235 (\u1208\u121A\u130B\u1265\u12D8\u12CD\u1363 \u12E8\u1270\u130B\u1260\u12D8\u12CD \u1230\u12CD \u1262\u12EB\u1295\u1235 ${birr(r.min_deposit)} \u1265\u122D \u1232\u12EB\u1235\u1308\u1263)</p><b>\u12A8\u134D\u1270\u129B \u130B\u1263\u12E6\u127D</b>` +
    (r.top.map(t => `<div class="pl">${esc(t.name)} (ID ${esc(t.inviter)}) \u2014 ${Number(t.invited)} \u130B\u1265\u12DF\u120D \u00B7 ${Number(t.paid)} \u12EB\u1235\u1308\u1261 \u00B7 ${birr(t.earned)} \u1265\u122D</div>`).join('') || '<p>\u2014</p>') +
    `<b>\u12E8\u1245\u122D\u1265 \u130A\u12DC \u130D\u1265\u12E3\u12CE\u127D</b>` +
    (r.recent.map(t => `<div class="pl">${esc(t.name)} \u2190 ${esc(t.inviter_name)} ${Number(t.referral_paid) > 0 ? '\u2705' : '\u23F3'}</div>`).join('') || '<p>\u2014</p>') + `</div>`;
}
async function loadSettings() {
  try {
    const r = await fetch('/api/admin/settings', {headers: HEADERS()});
    if (!r.ok) return;
    const d = await r.json();
    renderRules(d.rules); renderPromos(d.promos); renderSms(d.sms, d.incoming); renderRefs(d.referrals); renderSettings(d.settings); renderClaims(d.claims);
  } catch (e) {}
}

function fmtAgo(s) {
  if (!s) return '\u2014';
  const d = new Date(String(s).replace(' ', 'T') + 'Z');
  const m = Math.round((Date.now() - d.getTime()) / 60000);
  if (isNaN(m)) return '\u2014';
  if (m < 1) return '\u12A0\u1201\u1295';
  if (m < 60) return m + ' \u12F0\u1242\u1243 \u1260\u134A\u1275';
  if (m < 1440) return Math.floor(m / 60) + ' \u1230\u12D3\u1275 \u1260\u134A\u1275';
  return Math.floor(m / 1440) + ' \u1240\u1295 \u1260\u134A\u1275';
}
let PL_TIMER = null;
async function loadPlayers() {
  try {
    const q = encodeURIComponent(document.getElementById('pl-q').value || '');
    const f = document.getElementById('pl-f').value;
    const r = await fetch('/api/admin/players?q=' + q + '&filter=' + f, {headers: HEADERS()});
    if (!r.ok) return;
    const d = await r.json(), s = d.summary;
    document.getElementById('pl-sum').innerHTML =
      `\u1320\u1245\u120B\u120B: <b>${Number(s.total)}</b> \u00B7 \uD83C\uDFAE \u12A0\u1201\u1295 \u1260\u1328\u12CB\u1273 \u120B\u12ED: <b>${Number(s.playing)}</b> \u00B7 \uD83D\uDFE2 \u12A6\u1295\u120B\u12ED\u1295: <b>${Number(s.online)}</b> \u00B7 \u23F1 \u1263\u1208\u1349\u1275 24 \u1230\u12D3\u1275: <b>${Number(s.active24)}</b>` +
      `<p>\uD83D\uDCB0 \u12E8\u1201\u1209\u121D \u1270\u132B\u12CB\u127E\u127D \u1320\u1245\u120B\u120B \u1202\u1233\u1265: <b>${birr(s.money)}</b> \u1265\u122D \u00B7 \u12E8\u1270\u1308\u1299: ${Number(d.matched)}</p>`;
    document.getElementById('pl-list').innerHTML = (d.players || []).map(p =>
      `<div class="req"><b>${p.playing ? '\uD83C\uDFAE ' : (p.online ? '\uD83D\uDFE2 ' : '')}${esc(p.name)}</b>` +
      `<span>ID ${esc(p.user_id)}${p.phone ? ' \u00B7 ' + esc(p.phone) : ''}</span>` +
      `<p>Play: <b>${birr(p.credits)}</b> \u00B7 Win: <b>${birr(p.win_credits)}</b> \u00B7 \uD83C\uDF9F ${Number(p.cards)} \u12AB\u122D\u12F6\u127D</p>` +
      `<p>\u1208\u1218\u1328\u1228\u123B \u130A\u12DC \u12E8\u1273\u12E8\u12CD: ${esc(fmtAgo(p.last_seen_at))}</p></div>`).join('') || '\u2014';
  } catch (e) {}
}
document.getElementById('pl-q').oninput = () => { clearTimeout(PL_TIMER); PL_TIMER = setTimeout(loadPlayers, 300); };
document.getElementById('pl-f').onchange = loadPlayers;

function renderSettings(s) {
  document.getElementById('st-nub').value = s.new_user_bonus;
  document.getElementById('st-rr').value = s.referral_reward;
  document.getElementById('st-rmd').value = s.referral_min_deposit;
  document.getElementById('st-hp').value = s.house_percent;
  document.getElementById('st-be').value = s.bot_enabled;
  document.getElementById('st-bc').value = s.bot_cards_per_round;
  document.getElementById('st-hw').value = s.human_win_percent;
}
document.getElementById('st-save').onclick = async () => {
  const body = {new_user_bonus: Number(document.getElementById('st-nub').value || 0),
    referral_reward: Number(document.getElementById('st-rr').value || 0),
    referral_min_deposit: Number(document.getElementById('st-rmd').value || 0),
    bot_enabled: Number(document.getElementById('st-be').value || 0),
    bot_cards_per_round: Number(document.getElementById('st-bc').value || 0),
    human_win_percent: Number(document.getElementById('st-hw').value || 0)};
  const hp = document.getElementById('st-hp').value;
  if (hp !== '') body.house_percent = Number(hp);
  const ok = await postJson('/api/admin/setting', body);
  if (ok) { alert('\u1270\u1240\u121D\u1327\u120D \u2705'); loadSettings(); }
};

function renderClaims(list) {
  document.getElementById('claim-list').innerHTML = (list || []).map(c =>
    `<div class="req"><b>${esc(c.name)}</b> \u00B7 ${birr(c.amount)} \u1265\u122D ${c.active ? '\u2705' : '\u23F8'}` +
    `<p>\u12E8\u12C8\u1230\u12F1: ${Number(c.claimed)}${c.max_claims ? ' / ' + Number(c.max_claims) : ' (\u12EB\u120D\u1270\u12C8\u1230\u1290)'} \u00B7 ${c.audience === 'new' ? '\u1208\u12A0\u12F2\u1235 \u1270\u1320\u1243\u121A' : '\u1208\u1201\u1209\u121D'}` +
    (c.expires_at ? ` \u00B7 \u12A5\u1235\u12A8: ${esc(fmtTime(c.expires_at))}` : '') + `</p>` +
    `<button onclick="claimAct(${Number(c.id)},'toggle')" style="${BTN}background:#ffe629">${c.active ? '\u12A0\u1241\u121D' : '\u12A0\u1235\u1300\u121D\u122D'}</button>` +
    `<button onclick="claimAct(${Number(c.id)},'delete')" style="${BTN}background:#d63939;color:#fff">\uD83D\uDDD1</button></div>`).join('') || '\u2014';
}
async function claimAct(id, what) {
  if (what === 'delete' && !confirm('\u1266\u1290\u1231 \u12ED\u1230\u1228\u12DD?')) return;
  if (await postJson('/api/admin/claim-bonus', {id, [what]: true})) loadSettings();
}
document.getElementById('cb-save').onclick = async () => {
  const g = id => document.getElementById(id);
  const announce = g('cb-ann').checked;
  const ok = await postJson('/api/admin/claim-bonus', {name: g('cb-name').value, amount: Number(g('cb-amount').value),
    max_claims: Number(g('cb-max').value || 0), audience: g('cb-aud').value, expires: g('cb-exp').value, announce});
  if (ok) {
    ['cb-name', 'cb-amount', 'cb-max', 'cb-exp'].forEach(x => g(x).value = '');
    alert(announce ? '\u1270\u1348\u1325\u122F\u120D \u2705 \u1218\u120D\u12A5\u12AD\u1271 \u1208\u1201\u1209\u121D \u1270\u132B\u12CB\u127D \u12A5\u12E8\u1270\u120B\u12A8 \u1290\u12CD' : '\u1270\u1348\u1325\u122F\u120D \u2705');
    loadSettings();
  }
};
let RB = [];
function rbCard(r, i) {
  return `<div class="rule-card"><input type="text" id="rb${i}-n" placeholder="ስም" value="${esc(r.name)}">` +
    `<div class="grid2"><div>ሰዓት (መደበኛ)<input type="time" id="rb${i}-t" value="${hhmm(r.minute)}" oninput="rbEth(${i})"></div>` +
    `<div>ቦነስ (ብር)<input type="number" id="rb${i}-a" value="${Number(r.amount)}"></div></div><div class="eth" id="rb${i}-eth"></div>` +
    `<label><input type="checkbox" id="rb${i}-on" ${r.active ? 'checked' : ''}> ንቁ</label> ` +
    `<button onclick="rbSave(${i})" style="${BTN}background:#39d66f">💾 አስቀምጥ</button>` +
    (r.id ? `<button onclick="rbDel(${i})" style="${BTN}background:#d63939;color:#fff">🗑 ሰርዝ</button>` : '') + `</div>`;
}
function rbEth(i) {
  const m = toMin(document.getElementById('rb' + i + '-t').value, false);
  document.getElementById('rb' + i + '-eth').textContent = '🕒 የኢትዮጵያ ሰዓት: ' + ethTime(m);
}
function renderRB(list) {
  RB = list;
  document.getElementById('rb-box').innerHTML = list.map(rbCard).join('') || '—';
  list.forEach((r, i) => rbEth(i));
}
async function loadRB() {
  try { const r = await fetch('/api/admin/round-bonus', {headers: HEADERS()}); if (r.ok) renderRB((await r.json()).slots); } catch (e) {}
}
async function rbSave(i) {
  const g = id => document.getElementById('rb' + i + '-' + id);
  const ok = await postJson('/api/admin/round-bonus', {id: RB[i].id || null, name: g('n').value, minute: toMin(g('t').value, false),
    amount: Number(g('a').value), active: g('on').checked});
  if (ok) { alert('ተቀምጧል ✅'); loadRB(); }
}
async function rbDel(i) { if (confirm('ይሰረዝ?') && await postJson('/api/admin/round-bonus', {id: RB[i].id, delete: true})) loadRB(); }
document.getElementById('rb-add').onclick = () => { RB.push({id: null, name: 'ቦነስ', minute: 1200, amount: 300, active: 1}); renderRB(RB); };
document.getElementById('refresh').onclick = () => { load(); loadSettings(); loadPlayers(); loadRB(); }; load(); loadSettings(); loadPlayers(); loadRB();
setInterval(loadLive, 5000);
setInterval(() => { if (!document.hidden) loadPlayers(); }, 10000);
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
    now_seen = time.time()
    first_in_a_while = now_seen - last_seen.get(uid, 0) > 60
    last_seen[uid] = now_seen
    if first_in_a_while:   # remember when the player was last active (written at most once a minute)
        try:
            with closing(db()) as con:
                con.execute("UPDATE users SET last_seen_at=CURRENT_TIMESTAMP WHERE user_id=?", (int(uid),))
                con.commit()
        except Exception:
            logger.exception("could not save last_seen_at")
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
            "prize_payout": winners_cents(int(round(float(game_state["prize"]) * 100)), round_house_percent()) / 100.0,   # what the winner gets
            "called_numbers": list(game_state["called_numbers"]),
            "current_number": game_state["current_number"],
            "credits": row["credits"] if row else 100.0,
            "win_credits": row["win_credits"] if row else 0.0,
            "taken_cards": list(game_state["taken_card_numbers"]),
            "my_cards": my_cards,
            "my_cards_data": my_cards_data,
            "finished_ends_at": game_state["finished_ends_at"],
            "closing_number": game_state.get("closing_number"),
            "bonus_round": bool(game_state.get("bonus_round")),
            "bonus_amount": float(game_state.get("bonus_amount") or 0),
            "bonus_name": game_state.get("bonus_name") or "",
            "win_info": game_state.get("win_info"),
            "next_bonus": next_bonus_info(),
            "winners": [{"name": w["name"], "card_id": w["card_id"], "prize": w["prize"],
                         "matrix": w.get("matrix"), "marked": w.get("marked", []), "cells": w.get("cells", [])}
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
            return json_error("ከ%d ካርድ በላይ መያዝ አይችሉም" % MAX_CARDS_PER_PLAYER)
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
        return json_error("እባክዎ መረጃውን ይሙሉ")
    m = deposit_method(body.get("method") or "telebirr")
    if m is None or not m["number"]:
        m = DEPOSIT_METHODS[0]
    ref, err = extract_reference(str(body.get("reference") or "")[:800], m["key"])
    if err:
        return json_error(err)
    status, message = submit_deposit(user["id"], amt, ref, m["key"])
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
    ok, message = create_withdraw(user["id"], amt, account_info)
    if not ok:
        return json_error(message)
    return jsonify({"message": message})


@app.get("/api/miniapp/rank")
def miniapp_rank():
    user = api_user()
    ranking = card_ranking()
    out = {"rows": [{"name": display_name(uid), "cards": cards} for uid, cards in ranking[:RANK_SIZE]], "me": None}
    if user:
        for i, (uid, cards) in enumerate(ranking, 1):
            if uid == int(user["id"]):
                out["me"] = {"rank": i, "cards": cards}
                break
    return jsonify(out)


@app.get("/api/miniapp/bonuses")
def miniapp_bonuses():
    user = api_user()
    if not user:
        return json_error("Unauthorized", 401)
    return jsonify({"bonuses": claimable_bonuses(user["id"])})


@app.post("/api/miniapp/claim-bonus")
def miniapp_claim_bonus():
    user = api_user()
    if not user:
        return json_error("Unauthorized", 401)
    body = request.get_json(silent=True) or {}
    result, amount = claim_bonus(user["id"], body.get("id"))
    message = claim_message(user["id"], result, amount)
    if result != "ok":
        return json_error(message)
    return jsonify({"ok": True, "message": message})


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
        "COALESCE(SUM(house_cut),0) AS house, COALESCE(SUM(payout),0) AS payout, COALESCE(SUM(bonus),0) AS bonus "
        "FROM rounds WHERE " + r_cond).fetchone()
    d_cond = ("status='approved' AND " + (day % "reviewed_at")) if today_only else "status='approved'"
    deposits = con.execute("SELECT COALESCE(SUM(amount),0) FROM deposit_requests WHERE " + d_cond).fetchone()[0]
    out = {"rounds": row["n"], "sales": row["sales"], "house": row["house"],
           "payout": row["payout"], "bonus": row["bonus"], "deposits": deposits}
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
    if decision not in ("paid", "reject", "delete"):
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

    if SMS_SENDERS and sender and sender.strip().lower() not in SMS_SENDERS:
        return done({"ok": True, "ignored": "sender"})
    parsed = parse_incoming_sms(text)
    if not parsed:
        return done({"ok": True, "ignored": "not an incoming payment SMS"})
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
    for _p in promos:
        _p["announcement"] = promo_announcement(_p)
    top = [dict(r, name=display_name(r["inviter"])) for r in top]
    recent = [dict(r, name=display_name(r["user_id"]), inviter_name=display_name(r["referred_by"])) for r in recent]
    url = (PUBLIC_URL + "/api/sms-webhook?key=" + SMS_SECRET) if (SMS_SECRET and PUBLIC_URL) else ""
    with closing(db()) as con:
        claims = [dict(r) for r in con.execute("SELECT * FROM claim_bonuses ORDER BY id DESC LIMIT 30").fetchall()]
        settings = {k: setting_con(con, k) for k in SETTING_DEFAULTS}
    return jsonify({"rules": rules, "promos": promos, "incoming": incoming, "claims": claims, "settings": settings,
                    "referrals": {"top": top, "recent": recent, "reward": settings["referral_reward"],
                                  "min_deposit": settings["referral_min_deposit"]},
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
            min_deposit = max(0.0, float(b.get("min_deposit") or 0))
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
            "INSERT INTO promo_codes(code, reward, max_uses, expires_at, min_deposit, active) VALUES(?,?,?,?,?,1) "
            "ON CONFLICT(code) DO UPDATE SET reward=excluded.reward, max_uses=excluded.max_uses, "
            "expires_at=excluded.expires_at, min_deposit=excluded.min_deposit, active=1",
            (code, reward, max_uses, expires, min_deposit))
        con.commit()
    return jsonify({"ok": True})


@app.get("/api/admin/promo-uses")
def admin_promo_uses():
    """Admin only: who claimed a promo code."""
    user = api_admin()
    if not user:
        return json_error("Admin access denied", 403)
    code = re.sub(r"\s+", "", str(request.args.get("code") or "")).upper()[:40]
    with closing(db()) as con:
        rows = con.execute("SELECT user_id, created_at FROM promo_uses WHERE code=? ORDER BY created_at DESC LIMIT 500",
                           (code,)).fetchall()
    return jsonify({"count": len(rows),
                    "users": [{"user_id": int(r["user_id"]), "name": display_name(r["user_id"]), "at": r["created_at"]} for r in rows]})


@app.get("/api/admin/players")
def admin_players():
    """Admin only: who plays, who is online / active, how much money everybody has."""
    user = api_admin()
    if not user:
        return json_error("Admin access denied", 403)
    q = (request.args.get("q") or "").strip().lower()[:40]
    flt = request.args.get("filter") or "all"
    now = time.time()
    with game_lock:
        playing = {int(u) for u, c in game_state["player_cards"].items() if c}
    online = {int(u) for u, t in list(last_seen.items()) if now - t < 20}
    day_ago = fmt_db(utc_now() - timedelta(hours=24))
    with closing(db()) as con:
        rows = con.execute(
            "SELECT user_id, username, first_name, phone, credits, win_credits, last_seen_at FROM users").fetchall()
        cards = {int(r[0]): int(r[1] or 0) for r in con.execute(
            "SELECT rp.user_id, SUM(rp.cards) FROM round_players rp JOIN rounds r ON r.id=rp.round_id "
            "WHERE r.status!='refunded' GROUP BY rp.user_id").fetchall()}
    summary = {"total": len(rows), "playing": 0, "online": 0, "active24": 0, "money": 0.0}
    players = []
    for r in rows:
        uid = int(r["user_id"])
        is_playing, is_online = uid in playing, uid in online
        active24 = is_online or bool(r["last_seen_at"] and r["last_seen_at"] >= day_ago)
        summary["playing"] += 1 if is_playing else 0
        summary["online"] += 1 if is_online else 0
        summary["active24"] += 1 if active24 else 0
        summary["money"] += float(r["credits"]) + float(r["win_credits"])
        name = ("@" + r["username"]) if r["username"] else (r["first_name"] or "Player")
        if q and q not in (name.lower() + " " + str(uid) + " " + (r["phone"] or "")):
            continue
        if ((flt == "playing" and not is_playing) or (flt == "online" and not is_online)
                or (flt == "active" and not active24)
                or (flt == "balance" and float(r["credits"]) + float(r["win_credits"]) <= 0)):
            continue
        players.append({"user_id": uid, "name": name, "phone": r["phone"] or "", "credits": r["credits"],
                        "win_credits": r["win_credits"], "cards": cards.get(uid, 0), "playing": is_playing,
                        "online": is_online, "last_seen_at": r["last_seen_at"]})
    players.sort(key=lambda p: p["last_seen_at"] or "", reverse=True)
    players.sort(key=lambda p: (not p["playing"], not p["online"]))
    return jsonify({"summary": summary, "matched": len(players), "players": players[:200]})


@app.post("/api/admin/setting")
def admin_setting():
    user = api_admin()
    if not user:
        return json_error("Admin access denied", 403)
    b = request.get_json(silent=True) or {}
    clean = {}
    for key in SETTING_DEFAULTS:
        if key in b:
            try:
                v = float(b[key])
            except (TypeError, ValueError):
                return json_error("\u1275\u12ad\u12ad\u120d \u12eb\u120d\u1206\u1290 \u1218\u1228\u1303")
            if key == "house_percent" and not (0 <= v <= 90):
                return json_error("ተቆራጩ ከ0 እስከ 90 በመቶ መሆን አለበት")
            if not (0 <= v <= 100000):
                return json_error("\u1218\u1320\u1291 \u12a80 \u12a5\u1235\u12a8 100000 \u1218\u1206\u1295 \u12a0\u1208\u1260\u1275")
            clean[key] = v
    for key, v in clean.items():
        set_setting(key, v)
    return jsonify({"ok": True})


@app.post("/api/admin/claim-bonus")
def admin_claim_bonus():
    user = api_admin()
    if not user:
        return json_error("Admin access denied", 403)
    b = request.get_json(silent=True) or {}
    with closing(db()) as con:
        if b.get("delete"):
            con.execute("DELETE FROM claim_bonuses WHERE id=?", (int(b.get("id") or 0),))
            con.execute("DELETE FROM claim_bonus_uses WHERE bonus_id=?", (int(b.get("id") or 0),))
            con.commit()
            return jsonify({"ok": True})
        if b.get("toggle"):
            con.execute("UPDATE claim_bonuses SET active=1-active WHERE id=?", (int(b.get("id") or 0),))
            con.commit()
            return jsonify({"ok": True})
        try:
            amount = float(b.get("amount"))
            max_claims = int(b.get("max_claims") or 0)
        except (TypeError, ValueError):
            return json_error("\u1275\u12ad\u12ad\u120d \u12eb\u120d\u1206\u1290 \u1218\u1228\u1303")
        if amount <= 0 or max_claims < 0:
            return json_error("\u1266\u1290\u1231 \u12a80 \u1260\u120b\u12ed \u1218\u1206\u1295 \u12a0\u1208\u1260\u1275")
        audience = "new" if b.get("audience") == "new" else "all"
        name = str(b.get("name") or "").strip()[:40] or "\u1266\u1290\u1235"
        expires = None
        if b.get("expires"):
            try:   # a date in Ethiopia time: valid until the end of that day
                d = datetime.strptime(str(b["expires"])[:10], "%Y-%m-%d")
                expires = fmt_db(d + timedelta(hours=24) - timedelta(hours=3) - timedelta(seconds=1))
            except ValueError:
                return json_error("\u1240\u1291 \u1275\u12ad\u12ad\u120d \u12a0\u12ed\u12f0\u1208\u121d")
        cur = con.execute(
            "INSERT INTO claim_bonuses(name, amount, max_claims, audience, expires_at, active) VALUES(?,?,?,?,?,1)",
            (name, amount, max_claims, audience, expires))
        bid = cur.lastrowid
        con.commit()
    if b.get("announce"):
        threading.Thread(target=broadcast_claim_bonus, args=(bid,), daemon=True, name="claim-broadcast").start()
    return jsonify({"ok": True, "id": bid})


@app.get("/api/admin/round-bonus")
def admin_round_bonus_get():
    user = api_admin()
    if not user:
        return json_error("Admin access denied", 403)
    with closing(db()) as con:
        rows = [dict(r) for r in con.execute("SELECT * FROM round_bonus_slots ORDER BY minute").fetchall()]
    return jsonify({"slots": rows})


@app.post("/api/admin/round-bonus")
def admin_round_bonus_post():
    user = api_admin()
    if not user:
        return json_error("Admin access denied", 403)
    b = request.get_json(silent=True) or {}
    sid = b.get("id")
    with closing(db()) as con:
        if b.get("delete"):
            con.execute("DELETE FROM round_bonus_slots WHERE id=?", (int(sid or 0),))
            con.commit()
            bonus_cache_reset()
            return jsonify({"ok": True})
        try:
            minute, amount = int(b.get("minute")), float(b.get("amount"))
        except (TypeError, ValueError):
            return json_error("ትክክል ያልሆነ መረጃ")
        if not (0 <= minute < 1440) or not (0 < amount <= 100000):
            return json_error("ሰዓቱን ወይም መጠኑን ያረጋግጡ")
        name = str(b.get("name") or "ቦነስ")[:40]
        active = 1 if b.get("active") else 0
        if sid:
            con.execute("UPDATE round_bonus_slots SET name=?, minute=?, amount=?, active=?, last_fired='' WHERE id=?",
                        (name, minute, amount, active, int(sid)))
        else:
            con.execute("INSERT INTO round_bonus_slots(name, minute, amount, active) VALUES(?,?,?,?)",
                        (name, minute, amount, active))
        con.commit()
    bonus_cache_reset()
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
