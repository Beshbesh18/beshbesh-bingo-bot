# -*- coding: utf-8 -*-
import os
import json
import time
import random
import asyncio
import logging
import threading
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from flask import Flask, request, jsonify
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# Besh Besh Bingo â€” virtual-credit game.
# Set TOKEN in Render Environment Variables. Never hard-code your bot token.

TOKEN = os.environ.get("TOKEN", "").strip()
PUBLIC_URL = os.environ.get("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
WEBHOOK_SECRET_PATH = os.environ.get("WEBHOOK_SECRET_PATH", "").strip().strip("/")
LIVE_CHAT_ID = os.environ.get("LIVE_CHAT_ID", "").strip()
DATABASE_PATH = os.environ.get("DATABASE_PATH", "bingo.sqlite3")
PORT = int(os.environ.get("PORT", "10000"))
ADMIN_IDS = {
    int(value.strip())
    for value in os.environ.get("ADMIN_IDS", "").split(",")
    if value.strip().isdigit()
}

MAX_PLAYERS = 500
CARD_COST = 10
STARTING_CREDITS = 100
MAX_CARDS_PER_PLAYER = 4
CALL_INTERVAL_SECONDS = 5
MAX_NUMBER = 75
DEFAULT_WEBHOOK_PATH = "besh-besh-webhook"

os.makedirs(os.path.dirname(os.path.abspath(DATABASE_PATH)), exist_ok=True)

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("besh-besh-bingo")

app = Flask(__name__)
telegram_app = None
event_loop = None
bot_thread = None
startup_error = None
startup_ready = threading.Event()
state_lock = threading.RLock()
draw_task = None

game_state = {
    "round_id": 1,
    "active": False,
    "drawn": [],
    "live_message_id": None,
    "live_chat_id": LIVE_CHAT_ID,
    "round_started_at": None,
    "last_winner": None,
}


@contextmanager
def db_connection():
    conn = sqlite3.connect(DATABASE_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with db_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT NOT NULL DEFAULT '',
                first_name TEXT NOT NULL DEFAULT '',
                credits INTEGER NOT NULL DEFAULT 100,
                joined_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS cards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                round_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                card_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(user_id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS rounds (
                round_id INTEGER PRIMARY KEY,
                active INTEGER NOT NULL DEFAULT 0,
                drawn_json TEXT NOT NULL DEFAULT '[]',
                pot INTEGER NOT NULL DEFAULT 0,
                winner_id INTEGER,
                started_at TEXT,
                ended_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                amount INTEGER NOT NULL,
                reason TEXT NOT NULL,
                round_id INTEGER,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute(
            "INSERT OR IGNORE INTO rounds(round_id, active, drawn_json, pot) VALUES (1, 0, '[]', 0)"
        )
        row = conn.execute(
            "SELECT round_id, active, drawn_json, started_at FROM rounds ORDER BY round_id DESC LIMIT 1"
        ).fetchone()
        if row:
            game_state["round_id"] = row["round_id"]
            game_state["active"] = bool(row["active"])
            game_state["drawn"] = json.loads(row["drawn_json"] or "[]")
            game_state["round_started_at"] = row["started_at"]
        # A process restart should not silently resume a half-finished round.
        # Keep drawn numbers and cards, but pause the round until an admin resumes it.
        conn.execute(
            "UPDATE rounds SET active=0 WHERE round_id=?",
            (game_state["round_id"],),
        )
        game_state["active"] = False


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_user(user):
    if not user:
        return
    with db_connection() as conn:
        conn.execute(
            """INSERT INTO users(user_id, username, first_name, credits, joined_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                 username=excluded.username,
                 first_name=excluded.first_name""",
            (
                user.id,
                user.username or "",
                user.first_name or "",
                STARTING_CREDITS,
                now_iso(),
            ),
        )


def get_user(user_id):
    with db_connection() as conn:
        return conn.execute(
            "SELECT * FROM users WHERE user_id=?", (int(user_id),)
        ).fetchone()


def get_credits(user_id):
    row = get_user(user_id)
    return int(row["credits"]) if row else 0


def record_transaction(conn, user_id, amount, reason, round_id=None):
    conn.execute(
        """INSERT INTO transactions(user_id, amount, reason, round_id, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (int(user_id), int(amount), reason, round_id, now_iso()),
    )


def change_credits(user_id, amount, reason="adjustment"):
    with db_connection() as conn:
        row = conn.execute(
            "SELECT credits FROM users WHERE user_id=?", (int(user_id),)
        ).fetchone()
        if not row:
            return False
        new_balance = int(row["credits"]) + int(amount)
        if new_balance < 0:
            return False
        conn.execute(
            "UPDATE users SET credits=? WHERE user_id=?",
            (new_balance, int(user_id)),
        )
        record_transaction(
            conn, user_id, amount, reason, game_state["round_id"]
        )
    return True


def generate_bingo_card():
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


def card_is_winner(card, drawn):
    drawn_set = set(drawn)
    def marked(r, c):
        return card[r][c] == "FREE" or card[r][c] in drawn_set

    for r in range(5):
        if all(marked(r, c) for c in range(5)):
            return True
    for c in range(5):
        if all(marked(r, c) for r in range(5)):
            return True
    if all(marked(i, i) for i in range(5)):
        return True
    if all(marked(i, 4 - i) for i in range(5)):
        return True
    return False


def get_round_cards(round_id=None):
    rid = round_id if round_id is not None else game_state["round_id"]
    with db_connection() as conn:
        return conn.execute(
            "SELECT id, user_id, card_json FROM cards WHERE round_id=? ORDER BY id",
            (rid,),
        ).fetchall()


def card_text(card, drawn, title="ðŸŽŸï¸ BINGO CARD"):
    drawn_set = set(drawn)
    lines = [title, " B   I   N   G   O"]
    for r, row in enumerate(card):
        cells = []
        for value in row:
            if value == "FREE":
                cells.append("â˜…")
            elif value in drawn_set:
                cells.append(f"âœ“{value:02d}")
            else:
                cells.append(f" {value:02d}")
        lines.append(" ".join(cells))
    lines.append("âœ“ = called number   â˜… = FREE")
    return "\n".join(lines)


def board_text():
    with state_lock:
        drawn = list(game_state["drawn"])
        active = game_state["active"]
        round_id = game_state["round_id"]
    with db_connection() as conn:
        row = conn.execute("SELECT pot FROM rounds WHERE round_id=?", (round_id,)).fetchone()
        pot = int(row["pot"]) if row else 0
        players = conn.execute(
            "SELECT COUNT(DISTINCT user_id) AS n FROM cards WHERE round_id=?", (round_id,)
        ).fetchone()["n"]
    last = drawn[-1] if drawn else None
    status_am = "ðŸŸ¢ áŒ¨á‹‹á‰³á‹ áŠ¥á‹¨á‰°áŠ«áˆ„á‹° áŠá‹" if active else "â¸ï¸ áŒ¨á‹‹á‰³á‹ á‰†áˆŸáˆ"
    status_en = "ðŸŸ¢ Game in progress" if active else "â¸ï¸ Game paused"
    numbers_am = ", ".join(map(str, drawn[-15:])) if drawn else "áŒˆáŠ“ á‰áŒ¥áˆ­ áŠ áˆá‰°áŒ áˆ«áˆ"
    numbers_en = ", ".join(map(str, drawn[-15:])) if drawn else "No numbers called yet"
    return (
        "ðŸŽ± BESH BESH BINGO\nâ”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”\n"
        f"ðŸ”¢ á‹™áˆ­ / Round: #{round_id}\n"
        f"ðŸ“ áˆáŠ”á‰³ / Status: {status_am} / {status_en}\n"
        f"ðŸ‘¥ á‰°áŒ«á‹‹á‰¾á‰½ / Players: {players}/{MAX_PLAYERS}\n"
        f"ðŸ’° á‹¨á‰¨áˆ­á‰¹á‹‹áˆ áˆ½áˆáˆ›á‰µ / Virtual prize pool: {pot} credits\n"
        f"ðŸŽ¯ á‹¨á‰°áŒ áˆ© á‰áŒ¥áˆ®á‰½ / Numbers called: {len(drawn)}/{MAX_NUMBER}\n"
        f"ðŸ”¥ á‹¨áˆ˜áŒ¨áˆ¨áˆ»á‹ á‰áŒ¥áˆ­ / Last number: {last if last is not None else 'â€”'}\n"
        f"ðŸ§¾ á‹¨á‰…áˆ­á‰¥ áŒŠá‹œ á‰áŒ¥áˆ®á‰½ / Recent numbers: {numbers_am} / {numbers_en}\n"
        "â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”â”\n"
        "áŠ«áˆ­á‹µ áˆˆáˆ˜áŒá‹›á‰µ áŠ¨á‰³á‰½ á‹«áˆˆá‹áŠ• á‰áˆá á‹­áŒ«áŠ‘á¢\nTap a button below to buy a card."
    )

def main_keyboard():
    """Bilingual Amharic/English menu."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("ðŸŽŸï¸ áŠ«áˆ­á‹µ áŒá‹› / Buy card (10)", callback_data="buy_card"),
            InlineKeyboardButton("ðŸƒ áŠ«áˆ­á‹¶á‰¼ / My cards", callback_data="my_cards"),
        ],
        [
            InlineKeyboardButton("ðŸ’³ áˆ‚áˆ³á‰¤ / Balance", callback_data="balance"),
            InlineKeyboardButton("ðŸŽ± á‹¨áŒ¨á‹‹á‰³ áˆ°áˆŒá‹³ / Game board", callback_data="board"),
        ],
        [InlineKeyboardButton("ðŸ† BINGO! áŠ áˆ¸áŠ•áŒá‹«áˆˆáˆ / Claim BINGO", callback_data="claim")],
        [
            InlineKeyboardButton("ðŸ“œ á‹°áŠ•á‰¦á‰½ / Rules", callback_data="rules"),
            InlineKeyboardButton("â„¹ï¸ áŠ¥áŒˆá‹› / Help", callback_data="help"),
        ],
    ])

def is_admin(user_id):
    return int(user_id) in ADMIN_IDS


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user)
    credits = get_credits(user.id)
    message = (
        f"ðŸ‘‹ áŠ¥áŠ•áŠ³áŠ• á‹ˆá‹° Besh Besh Bingo á‰ á‹°áˆ…áŠ“ áˆ˜áŒ¡ / Welcome, {user.first_name}!\n\n"
        "ðŸŽ± á‹­áˆ… á‰ á‰¨áˆ­á‰¹á‹‹áˆ áŠ­áˆ¬á‹²á‰µ á‹¨áˆšáˆ°áˆ« á‹¨Bingo áŒ¨á‹‹á‰³ áŠá‹á‹\n"
        "This is a Bingo game using virtual credits.\n"
        f"ðŸ’³ á‹¨áŠ áˆáŠ‘ áˆ‚áˆ³á‰¥á‹Ž / Your balance: {credits} credits\n"
        f"ðŸŽŸï¸ áŠ áŠ•á‹µ áŠ«áˆ­á‹µ / One card: {CARD_COST} credits\n\n"
        "áŠ¨á‰³á‰½ á‹«áˆˆá‹áŠ• áˆœáŠ‘ á‹­áŒ á‰€áˆ™á¢ / Use the menu below."
    )
    await update.effective_message.reply_text(message, reply_markup=main_keyboard())

async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user)
    balance = get_credits(update.effective_user.id)
    await update.effective_message.reply_text(
        f"ðŸ’³ á‹¨áŠ¥áˆ­áˆµá‹Ž áˆ‚áˆ³á‰¥ / Your balance: {balance} credits"
    )

async def board_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(board_text(), reply_markup=main_keyboard())


async def my_cards_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user)
    await send_user_cards(update.effective_user.id, update.effective_chat.id)


async def rules_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "ðŸ“œ BESH BESH BINGO â€” á‹°áŠ•á‰¦á‰½ / RULES\n\n"
        f"â€¢ áŠ«áˆ­á‹µ á‹‹áŒ‹ / Card cost: {CARD_COST} virtual credits\n"
        f"â€¢ áˆˆáŠ á‹²áˆµ á‰°áŒ«á‹‹á‰½ áˆ˜áŠáˆ» áˆ‚áˆ³á‰¥ / Starting balance: {STARTING_CREDITS} credits\n"
        f"â€¢ á‰ á‹™áˆ­ áŠ¥áˆµáŠ¨ {MAX_CARDS_PER_PLAYER} áŠ«áˆ­á‹¶á‰½ / Up to {MAX_CARDS_PER_PLAYER} cards per player per round\n"
        "â€¢ BINGO áˆˆáˆ›áˆ¸áŠá áˆ¨á‹µáá£ áŠ áˆá‹µ á‹ˆá‹­áˆ áˆ°á‹«á á‹­áˆ™áˆ‰á¢\n"
        "  Complete a row, column, or diagonal to win BINGO.\n"
        "â€¢ á‹¨áˆ˜áˆƒáˆ FREE áŠ­ááˆ á‰ áŠáƒ á‹­á‰†áŒ áˆ«áˆá¢ / The center FREE square counts automatically.\n"
        "â€¢ áŠ­áˆ¬á‹²á‰¶á‰½ á‰¨áˆ­á‰¹á‹‹áˆ áŠ“á‰¸á‹á¤ áŒˆáŠ•á‹˜á‰¥ áŠ á‹­á‹°áˆ‰áˆá¢ / Credits are virtual, not money.\n"
        "â€¢ á‹¨áˆ˜áŒ€áˆ˜áˆªá‹«á‹ á‰µáŠ­áŠ­áˆˆáŠ› BINGO á‹¨áˆšá‹«áˆ³á‹á‰… á‰°áŒ«á‹‹á‰½ á‹«áˆ¸áŠ•á‹áˆá¢\n"
        "  The first player to claim a valid BINGO wins."
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "â„¹ï¸ áŠ¥áŒˆá‹› / HELP\n\n"
        "/start â€” á‹‹áŠ“ áˆœáŠ‘ / Main menu\n"
        "/balance â€” áˆ‚áˆ³á‰¥ / Balance\n"
        "/cards â€” áŠ«áˆ­á‹¶á‰¼ / My cards\n"
        "/board â€” á‹¨áŒ¨á‹‹á‰³ áˆ°áˆŒá‹³ / Game board\n"
        "/rules â€” á‹°áŠ•á‰¦á‰½ / Rules\n"
        "/help â€” áŠ¥áŒˆá‹› / Help\n\n"
        "áŠ áˆµá‰°á‹³á‹³áˆª áŠ¨áˆ†áŠ‘ /admin á‹­áŒ á‰€áˆ™á¢ / Admins: use /admin."
    )

async def send_user_cards(user_id, chat_id):
    with state_lock:
        drawn = list(game_state["drawn"])
        round_id = game_state["round_id"]
    with db_connection() as conn:
        rows = conn.execute(
            "SELECT id, card_json FROM cards WHERE round_id=? AND user_id=? ORDER BY id",
            (round_id, int(user_id)),
        ).fetchall()
    if not rows:
        await telegram_app.bot.send_message(
            chat_id=chat_id,
            text="á‰ á‹šáˆ… á‹™áˆ­ áŠ«áˆ­á‹µ á‹¨áˆˆá‹Žá‰µáˆá¢ áŠ«áˆ­á‹µ áˆˆáˆ˜áŒá‹›á‰µ áŠ¨áˆœáŠ‘ á‹­áŒá‹™á¢",
            reply_markup=main_keyboard(),
        )
        return
    for index, row in enumerate(rows, 1):
        card = json.loads(row["card_json"])
        await telegram_app.bot.send_message(
            chat_id=chat_id,
            text=card_text(card, drawn, f"ðŸŽŸï¸ áŠ«áˆ­á‹µ {index} (ID {row['id']})"),
        )


async def buy_card(query, user):
    ensure_user(user)
    with state_lock:
        round_id = game_state["round_id"]
        active = game_state["active"]
    with db_connection() as conn:
        existing = conn.execute(
            "SELECT COUNT(*) AS n FROM cards WHERE round_id=? AND user_id=?",
            (round_id, user.id),
        ).fetchone()["n"]
        players = conn.execute(
            "SELECT COUNT(DISTINCT user_id) AS n FROM cards WHERE round_id=?",
            (round_id,),
        ).fetchone()["n"]
        account = conn.execute(
            "SELECT credits FROM users WHERE user_id=?", (user.id,)
        ).fetchone()
        if existing >= MAX_CARDS_PER_PLAYER:
            await query.edit_message_text(
                f"á‰ á‹šáˆ… á‹™áˆ­ áŠ¥áˆµáŠ¨ {MAX_CARDS_PER_PLAYER} áŠ«áˆ­á‹¶á‰½ á‰¥á‰» áˆ˜áŒá‹›á‰µ á‹­á‰½áˆ‹áˆ‰á¢",
                reply_markup=main_keyboard(),
            )
            return
        if players >= MAX_PLAYERS and existing == 0:
            await query.edit_message_text(
                "á‹­á‰…áˆ­á‰³á£ á‹¨á‹šáˆ… á‹™áˆ­ á‹¨á‰°áŒ«á‹‹á‰½ á‰¥á‹›á‰µ áˆžáˆá‰·áˆá¢",
                reply_markup=main_keyboard(),
            )
            return
        if not account or account["credits"] < CARD_COST:
            await query.edit_message_text(
                f"âŒ á‰ á‰‚ áŠ­áˆ¬á‹²á‰µ á‹¨áˆˆá‹Žá‰µáˆá¢ áŠ«áˆ­á‹µ {CARD_COST} credits á‹­áˆáˆáŒ‹áˆá¢",
                reply_markup=main_keyboard(),
            )
            return
        if active and len(game_state["drawn"]) > 0:
            # Late joining is allowed, but a new card can only win with future calls.
            pass
        card = generate_bingo_card()
        conn.execute(
            "UPDATE users SET credits=credits-? WHERE user_id=?",
            (CARD_COST, user.id),
        )
        conn.execute(
            "INSERT INTO cards(round_id, user_id, card_json, created_at) VALUES (?, ?, ?, ?)",
            (round_id, user.id, json.dumps(card), now_iso()),
        )
        conn.execute(
            "UPDATE rounds SET pot=pot+? WHERE round_id=?",
            (CARD_COST, round_id),
        )
        record_transaction(conn, user.id, -CARD_COST, "buy_card", round_id)
    with state_lock:
        if not game_state["active"]:
            game_state["active"] = True
            game_state["round_started_at"] = now_iso()
            with db_connection() as conn:
                conn.execute(
                    "UPDATE rounds SET active=1, started_at=? WHERE round_id=?",
                    (game_state["round_started_at"], round_id),
                )
            ensure_draw_task()
    await query.edit_message_text(
        f"âœ… áŠ«áˆ­á‹µá‹Ž á‰°áŒˆá‹á‰·áˆ!\nðŸ’³ á‰€áˆª áˆ‚áˆ³á‰¥: {get_credits(user.id)} credits\n"
        f"ðŸŽŸï¸ á‰ á‹šáˆ… á‹™áˆ­ á‹¨áŠ¥áˆ­áˆµá‹Ž áŠ«áˆ­á‹¶á‰½: {existing + 1}/{MAX_CARDS_PER_PLAYER}",
        reply_markup=main_keyboard(),
    )
    await telegram_app.bot.send_message(
        chat_id=user.id,
        text=card_text(card, list(game_state["drawn"]), f"ðŸŽŸï¸ áŠ á‹²áˆ± áŠ«áˆ­á‹µá‹Ž (á‹™áˆ­ #{round_id})"),
    )
    await publish_live_board()


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    ensure_user(user)
    await query.answer()
    data = query.data or ""

    if data == "buy_card":
        await buy_card(query, user)
    elif data == "my_cards":
        await query.edit_message_text("ðŸƒ áŠ«áˆ­á‹¶á‰½á‹ŽáŠ• á‰ áŒáˆ áˆ˜áˆá‹•áŠ­á‰µ áŠ¥á‹¨áˆ‹áŠ­áŠ© áŠá‹â€¦", reply_markup=main_keyboard())
        await send_user_cards(user.id, user.id)
    elif data == "balance":
        await query.edit_message_text(
            f"ðŸ’³ á‹¨áŠ¥áˆ­áˆµá‹Ž áˆ‚áˆ³á‰¥: {get_credits(user.id)} credits",
            reply_markup=main_keyboard(),
        )
    elif data == "board":
        await query.edit_message_text(board_text(), reply_markup=main_keyboard())
    elif data == "rules":
        await query.edit_message_text(
            f"ðŸ“œ áŠ«áˆ­á‹µ: {CARD_COST} credits | áŠ¨áá‰°áŠ› áŠ«áˆ­á‹¶á‰½: {MAX_CARDS_PER_PLAYER}\n"
            "BINGO áˆˆáˆ›áˆ¸áŠá áˆ¨á‹µáá£ áŠ áˆá‹µ á‹ˆá‹­áˆ áˆ°á‹«á á‹«áˆµáˆ™áˆ‰á¢\n"
            "áŠ­áˆ¬á‹²á‰¶á‰½ á‰¨áˆ­á‰¹á‹‹áˆ áŠ“á‰¸á‹á¢",
            reply_markup=main_keyboard(),
        )
    elif data == "help":
        await query.edit_message_text(
            "/start â€” áˆœáŠ‘\n/balance â€” áˆ‚áˆ³á‰¥\n/cards â€” áŠ«áˆ­á‹¶á‰¼\n/board â€” áˆ°áˆŒá‹³\n/rules â€” á‹°áŠ•á‰¦á‰½",
            reply_markup=main_keyboard(),
        )
    elif data == "claim":
        await claim_bingo(query, user)


async def claim_bingo(query, user):
    with state_lock:
        active = game_state["active"]
        drawn = list(game_state["drawn"])
        round_id = game_state["round_id"]
    if not active:
        await query.edit_message_text("â¸ï¸ áŠ áˆáŠ• áŠ•á‰ áŒ¨á‹‹á‰³ á‹¨áˆˆáˆá¢", reply_markup=main_keyboard())
        return
    with db_connection() as conn:
        rows = conn.execute(
            "SELECT id, card_json FROM cards WHERE round_id=? AND user_id=?",
            (round_id, user.id),
        ).fetchall()
        round_row = conn.execute(
            "SELECT pot, winner_id FROM rounds WHERE round_id=?", (round_id,)
        ).fetchone()
    if round_row and round_row["winner_id"]:
        await query.edit_message_text("ðŸ á‹­áˆ… á‹™áˆ­ áŠ áˆµá‰€á‹µáˆž áŠ áˆ¸áŠ“áŠ áŠ áŒáŠá‰·áˆá¢", reply_markup=main_keyboard())
        return
    winning_card = None
    for row in rows:
        card = json.loads(row["card_json"])
        if card_is_winner(card, drawn):
            winning_card = row["id"]
            break
    if winning_card is None:
        await query.edit_message_text(
            "âŒ áŒˆáŠ“ BINGO áŠ áˆáˆžáˆ‰áˆá¢ á‹¨á‰°áŒ áˆ©á‰µáŠ• á‰áŒ¥áˆ®á‰½ á‹«áˆ¨áŒ‹áŒáŒ¡á¢",
            reply_markup=main_keyboard(),
        )
        return
    prize = int(round_row["pot"]) if round_row else 0
    # Claim is committed atomically so two users cannot both win the same round.
    with db_connection() as conn:
        row = conn.execute(
            "SELECT winner_id FROM rounds WHERE round_id=?", (round_id,)
        ).fetchone()
        if not row or row["winner_id"] is not None:
            await query.edit_message_text("ðŸ á‹­áˆ… á‹™áˆ­ áŠ áˆµá‰€á‹µáˆž á‰°á‹ˆáˆµáŠ—áˆá¢", reply_markup=main_keyboard())
            return
        conn.execute(
            "UPDATE rounds SET active=0, winner_id=?, ended_at=? WHERE round_id=? AND winner_id IS NULL",
            (user.id, now_iso(), round_id),
        )
        conn.execute(
            "UPDATE users SET credits=credits+? WHERE user_id=?", (prize, user.id)
        )
        record_transaction(conn, user.id, prize, "bingo_prize", round_id)
    with state_lock:
        game_state["active"] = False
        game_state["last_winner"] = user.id
    cancel_draw_task()
    await query.edit_message_text(
        f"ðŸŽ‰ BINGO! áŠ áˆ¸áŠ•áˆá‹‹áˆ!\nðŸ† áˆ½áˆáˆ›á‰µ: {prize} virtual credits\n"
        f"ðŸ’³ áŠ á‹²áˆµ áˆ‚áˆ³á‰¥: {get_credits(user.id)} credits",
        reply_markup=main_keyboard(),
    )
    await publish_live_board(
        extra=f"\n\nðŸ† áŠ áˆ¸áŠ“áŠ: {user.full_name}\nðŸ’° áˆ½áˆáˆ›á‰µ: {prize} credits"
    )


async def publish_live_board(extra=""):
    chat_id = game_state.get("live_chat_id")
    if not chat_id or not telegram_app:
        return
    text = board_text() + extra
    try:
        message_id = game_state.get("live_message_id")
        if message_id:
            await telegram_app.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                reply_markup=main_keyboard(),
            )
        else:
            message = await telegram_app.bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=main_keyboard(),
            )
            game_state["live_message_id"] = message.message_id
    except Exception as exc:
        # Telegram may reject an identical edit; keep the service alive.
        if "Message is not modified" not in str(exc):
            log.warning("Live board update failed: %s", exc)


async def number_caller():
    global draw_task
    try:
        while True:
            await asyncio.sleep(CALL_INTERVAL_SECONDS)
            with state_lock:
                if not game_state["active"]:
                    return
                drawn = list(game_state["drawn"])
                round_id = game_state["round_id"]
                available = [n for n in range(1, MAX_NUMBER + 1) if n not in drawn]
                if not available:
                    game_state["active"] = False
                    with db_connection() as conn:
                        conn.execute("UPDATE rounds SET active=0, ended_at=? WHERE round_id=?", (now_iso(), round_id))
                    if game_state["live_chat_id"]:
                        await telegram_app.bot.send_message(
                            chat_id=game_state["live_chat_id"],
                            text="â¹ï¸ 75 á‰áŒ¥áˆ®á‰½ á‰°áŒ áˆ­á‰°á‹‹áˆá¤ áˆ›áŠ•áˆ BINGO áŠ«áˆ‹áˆ¸áŠáˆ á‹™áˆ© á‰°áŒ áŠ“á‰‹áˆá¢",
                        )
                    return
                number = random.choice(available)
                game_state["drawn"].append(number)
                drawn = list(game_state["drawn"])
                with db_connection() as conn:
                    conn.execute(
                        "UPDATE rounds SET drawn_json=? WHERE round_id=?",
                        (json.dumps(drawn), round_id),
                    )
            await publish_live_board(extra=f"\n\nðŸ“£ áŠ á‹²áˆµ á‰áŒ¥áˆ­: {number}")
            # Send the latest card state privately only to players, not every user in the bot.
            with db_connection() as conn:
                player_ids = [
                    row["user_id"] for row in conn.execute(
                        "SELECT DISTINCT user_id FROM cards WHERE round_id=?", (round_id,)
                    ).fetchall()
                ]
            for user_id in player_ids:
                try:
                    await telegram_app.bot.send_message(
                        chat_id=user_id,
                        text=f"ðŸ“£ á‹¨á‰°áŒ áˆ«á‹ á‰áŒ¥áˆ­: {number}\nðŸŽ¯ á‰áŒ¥áˆ®á‰½: {', '.join(map(str, drawn[-12:]))}",
                    )
                except Exception as exc:
                    log.info("Could not DM player %s: %s", user_id, exc)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("Number caller crashed")
    finally:
        draw_task = None


def ensure_draw_task():
    global draw_task
    if event_loop and event_loop.is_running():
        def start_task():
            global draw_task
            if draw_task is None or draw_task.done():
                draw_task = asyncio.create_task(number_caller())
        event_loop.call_soon_threadsafe(start_task)


def cancel_draw_task():
    if event_loop and event_loop.is_running():
        def cancel():
            if draw_task and not draw_task.done():
                draw_task.cancel()
        event_loop.call_soon_threadsafe(cancel)


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user)
    if not is_admin(user.id):
        await update.effective_message.reply_text("â›” á‹­áˆ… á‰µáŠ¥á‹›á‹ áˆˆáŠ áˆµá‰°á‹³á‹³áˆª á‰¥á‰» áŠá‹á¢")
        return
    await update.effective_message.reply_text(
        "ðŸ› ï¸ á‹¨áŠ áˆµá‰°á‹³á‹³áˆª á‰µáŠ¥á‹›á‹žá‰½\n\n"
        "/stats â€” á‹¨áŒ¨á‹‹á‰³ áˆµá‰³á‰²áˆµá‰²áŠ­áˆµ\n"
        "/startgame â€” á‹¨á‰°á‰†áˆ˜á‹áŠ• á‹™áˆ­ áˆ›áˆµá‰€áŒ áˆ\n"
        "/stopgame â€” áŒ¨á‹‹á‰³ áˆ›á‰†áˆ\n"
        "/newround â€” áŠ á‹²áˆµ á‹™áˆ­ áˆ˜áŠ­áˆá‰µ\n"
        "/addcredits USER_ID AMOUNT â€” áŠ­áˆ¬á‹²á‰µ áˆ˜áŒ¨áˆ˜áˆ­\n"
        "/setlive CHAT_ID â€” á‹¨á‰€áŒ¥á‰³ áˆ°áˆŒá‹³ á‰»á‰µ áˆ˜á‹ˆáˆ°áŠ•"
    )


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.effective_message.reply_text("â›” áŠ áˆµá‰°á‹³á‹³áˆª á‰¥á‰»á¢")
        return
    with db_connection() as conn:
        users = conn.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
        cards = conn.execute(
            "SELECT COUNT(*) n FROM cards WHERE round_id=?", (game_state["round_id"],)
        ).fetchone()["n"]
        credits = conn.execute("SELECT COALESCE(SUM(credits),0) n FROM users").fetchone()["n"]
        round_row = conn.execute(
            "SELECT pot, active, winner_id FROM rounds WHERE round_id=?", (game_state["round_id"],)
        ).fetchone()
    await update.effective_message.reply_text(
        "ðŸ“Š á‹¨Besh Besh Bingo áˆµá‰³á‰²áˆµá‰²áŠ­áˆµ\n\n"
        f"ðŸ‘¥ á‰°áŒ á‰ƒáˆšá‹Žá‰½: {users}\n"
        f"ðŸŽŸï¸ á‹¨á‹šáˆ… á‹™áˆ­ áŠ«áˆ­á‹¶á‰½: {cards}\n"
        f"ðŸ’³ áŒ á‰…áˆ‹áˆ‹ áŠ­áˆ¬á‹²á‰µ: {credits}\n"
        f"ðŸ’° á‹¨á‹šáˆ… á‹™áˆ­ áˆ½áˆáˆ›á‰µ: {round_row['pot'] if round_row else 0}\n"
        f"ðŸŽ± á‹™áˆ­: #{game_state['round_id']}\n"
        f"ðŸ“ áˆáŠ”á‰³: {'áŠ•á‰' if game_state['active'] else 'á‰†áˆŸáˆ'}"
    )


async def startgame_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.effective_message.reply_text("â›” áŠ áˆµá‰°á‹³á‹³áˆª á‰¥á‰»á¢")
        return
    with state_lock:
        game_state["active"] = True
        if not game_state["round_started_at"]:
            game_state["round_started_at"] = now_iso()
        with db_connection() as conn:
            conn.execute(
                "UPDATE rounds SET active=1, started_at=COALESCE(started_at, ?) WHERE round_id=?",
                (game_state["round_started_at"], game_state["round_id"]),
            )
    ensure_draw_task()
    await update.effective_message.reply_text("â–¶ï¸ á‹™áˆ© á‰°áŒ€áˆáˆ¯áˆ/á‰€áŒ¥áˆáˆá¢")


async def stopgame_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.effective_message.reply_text("â›” áŠ áˆµá‰°á‹³á‹³áˆª á‰¥á‰»á¢")
        return
    with state_lock:
        game_state["active"] = False
        with db_connection() as conn:
            conn.execute(
                "UPDATE rounds SET active=0 WHERE round_id=?", (game_state["round_id"],)
            )
    cancel_draw_task()
    await update.effective_message.reply_text("â¸ï¸ áŒ¨á‹‹á‰³á‹ á‰†áˆŸáˆá¢ áŠ¥áŠ•á‹°áŒˆáŠ“ áˆˆáˆ›áˆµá‰€áŒ áˆ /startgame á‹­áŒ á‰€áˆ™á¢")
    await publish_live_board()


async def newround_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.effective_message.reply_text("â›” áŠ áˆµá‰°á‹³á‹³áˆª á‰¥á‰»á¢")
        return
    with state_lock:
        if game_state["active"]:
            await update.effective_message.reply_text("âš ï¸ áŠ áˆáŠ• áŒ¨á‹‹á‰³á‹ áŠ•á‰ áŠá‹á¢ áˆ˜áŒ€áˆ˜áˆªá‹« /stopgame á‹­áŒ á‰€áˆ™á¢")
            return
        game_state["round_id"] += 1
        game_state["drawn"] = []
        game_state["active"] = False
        game_state["round_started_at"] = None
        game_state["last_winner"] = None
        with db_connection() as conn:
            conn.execute(
                "INSERT INTO rounds(round_id, active, drawn_json, pot) VALUES (?, 0, '[]', 0)",
                (game_state["round_id"],),
            )
    await update.effective_message.reply_text(f"ðŸ†• áŠ á‹²áˆµ á‹™áˆ­ #{game_state['round_id']} á‰°áŠ¨áá‰·áˆá¢ á‰°áŒ«á‹‹á‰¾á‰½ áŠ«áˆ­á‹µ áˆ˜áŒá‹›á‰µ á‹­á‰½áˆ‹áˆ‰á¢")
    await publish_live_board()


async def addcredits_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.effective_message.reply_text("â›” áŠ áˆµá‰°á‹³á‹³áˆª á‰¥á‰»á¢")
        return
    if len(context.args) != 2:
        await update.effective_message.reply_text("áŠ áŒ á‰ƒá‰€áˆ: /addcredits USER_ID AMOUNT")
        return
    try:
        user_id, amount = int(context.args[0]), int(context.args[1])
    except ValueError:
        await update.effective_message.reply_text("USER_ID áŠ¥áŠ“ AMOUNT á‰áŒ¥áˆ®á‰½ áˆ˜áˆ†áŠ• áŠ áˆˆá‰£á‰¸á‹á¢")
        return
    if amount <= 0 or amount > 1000000:
        await update.effective_message.reply_text("AMOUNT áŠ¨1 áŠ¥áˆµáŠ¨ 1,000,000 áˆ˜áˆ†áŠ• áŠ áˆˆá‰ á‰µá¢")
        return
    if not change_credits(user_id, amount, "admin_credit"):
        await update.effective_message.reply_text("á‰°áŒ á‰ƒáˆšá‹ áŠ áˆá‰°áŒˆáŠ˜áˆá¢ áˆ˜áŒ€áˆ˜áˆªá‹« /start áŠ¥áŠ•á‹²áŒ«áŠ• á‹­áŒ á‹­á‰á¢")
        return
    await update.effective_message.reply_text(f"âœ… {amount} credits á‹ˆá‹° {user_id} á‰°áŒ¨áˆáˆ¯áˆá¢")


async def setlive_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.effective_message.reply_text("â›” áŠ áˆµá‰°á‹³á‹³áˆª á‰¥á‰»á¢")
        return
    if not context.args:
        await update.effective_message.reply_text("áŠ áŒ á‰ƒá‰€áˆ: /setlive CHAT_ID")
        return
    game_state["live_chat_id"] = context.args[0]
    with state_lock:
        game_state["live_message_id"] = None
    await update.effective_message.reply_text("âœ… á‹¨á‰€áŒ¥á‰³ áˆ°áˆŒá‹³ á‰»á‰µ á‰°á‰€á‹­áˆ¯áˆá¢")
    await publish_live_board()


def run_bot_loop():
    global event_loop, telegram_app, startup_error
    event_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(event_loop)

    async def boot():
        global telegram_app
        telegram_app = Application.builder().token(TOKEN).build()
        telegram_app.add_handler(CommandHandler("start", start))
        telegram_app.add_handler(CommandHandler("balance", balance_command))
        telegram_app.add_handler(CommandHandler("cards", my_cards_command))
        telegram_app.add_handler(CommandHandler("board", board_command))
        telegram_app.add_handler(CommandHandler("rules", rules_command))
        telegram_app.add_handler(CommandHandler("help", help_command))
        telegram_app.add_handler(CommandHandler("admin", admin_command))
        telegram_app.add_handler(CommandHandler("stats", stats_command))
        telegram_app.add_handler(CommandHandler("startgame", startgame_command))
        telegram_app.add_handler(CommandHandler("stopgame", stopgame_command))
        telegram_app.add_handler(CommandHandler("newround", newround_command))
        telegram_app.add_handler(CommandHandler("addcredits", addcredits_command))
        telegram_app.add_handler(CommandHandler("setlive", setlive_command))
        telegram_app.add_handler(CallbackQueryHandler(callback_handler))
        await telegram_app.initialize()
        await telegram_app.start()
        if PUBLIC_URL:
            path = WEBHOOK_SECRET_PATH or DEFAULT_WEBHOOK_PATH
            await telegram_app.bot.set_webhook(
                url=f"{PUBLIC_URL}/{path}",
                allowed_updates=Update.ALL_TYPES,
                drop_pending_updates=False,
            )
            log.info("Telegram webhook configured")
        startup_ready.set()
        await asyncio.Event().wait()

    try:
        event_loop.run_until_complete(boot())
    except Exception as exc:
        startup_error = exc
        startup_ready.set()
        log.exception("Bot startup failed")
    finally:
        if telegram_app:
            try:
                event_loop.run_until_complete(telegram_app.stop())
                event_loop.run_until_complete(telegram_app.shutdown())
            except Exception:
                log.exception("Bot shutdown error")
        event_loop.close()


@app.get("/")
def health():
    return jsonify({
        "service": "Besh Besh Bingo",
        "status": "ok" if startup_ready.is_set() and startup_error is None else "starting",
        "round": game_state["round_id"],
        "active": game_state["active"],
    }), 200


@app.post("/" + (WEBHOOK_SECRET_PATH or DEFAULT_WEBHOOK_PATH))
def telegram_webhook():
    if not telegram_app or not event_loop or not event_loop.is_running():
        return jsonify({"error": "bot is starting"}), 503
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "invalid update"}), 400
    try:
        update = Update.de_json(payload, telegram_app.bot)
        future = asyncio.run_coroutine_threadsafe(
            telegram_app.process_update(update), event_loop
        )
        # Return promptly; Telegram does not need to wait for all bot handlers.
        future.add_done_callback(
            lambda fut: log.error("Update processing failed: %s", fut.exception())
            if fut.exception() else None
        )
        return jsonify({"ok": True}), 200
    except Exception:
        log.exception("Webhook processing failed")
        return jsonify({"error": "processing failed"}), 500


def main():
    global bot_thread
    if not TOKEN:
        raise RuntimeError("TOKEN environment variable is missing.")
    init_db()
    bot_thread = threading.Thread(target=run_bot_loop, daemon=True)
    bot_thread.start()
    if not startup_ready.wait(timeout=25):
        log.warning("Telegram bot startup is taking longer than expected")
    if startup_error:
        raise RuntimeError(f"Telegram bot startup failed: {startup_error}")
    app.run(host="0.0.0.0", port=PORT, threaded=True)


if __name__ == "__main__":
    main()
