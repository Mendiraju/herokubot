import asyncio
import logging
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from contextlib import asynccontextmanager
from typing import Optional
import time
from collections import deque, defaultdict

# ---------------------------------------------------------------------------
# OTP Bot - Sections
# - CONFIG & CONSTANTS
# - HELPERS & DB
# - DATABASE INITIALIZATION
# - USER & WALLET HELPERS
# - MENUS & KEYBOARDS
# - HANDLERS: START / MAIN MENU
# - HANDLERS: WALLET
# - HANDLERS: ADMIN
# - HANDLERS: SIM MANAGEMENT
# - HANDLERS: OTP FLOW
# - JOBS & RECOVERY
# - ADMIN DASHBOARD HELPERS
# - BOT SETUP & ENTRYPOINT
# ---------------------------------------------------------------------------

import aiosqlite
from dotenv import load_dotenv
from telegram import (
    BotCommand,
    BotCommandScopeChat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.helpers import escape_markdown

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID = int(os.getenv("OWNER_ID", "0") or 0)
SUPPORT_ADMINS = {int(x) for x in (os.getenv("SUPPORT_ADMINS", "") or "").split(',') if x.strip()}
DB_FILE = os.getenv("DB_FILE", "otp_bot.db")
UPI_ID = os.getenv("UPI_ID", "yourupi@upi")
SUPPORT_URL = os.getenv("SUPPORT_URL", "https://t.me/your_support_handle")

OTP_PRICE = int(os.getenv("OTP_PRICE", "30"))
OTP_PAYOUT = int(os.getenv("OTP_PAYOUT", "20"))
OTP_TIMEOUT_MINUTES = int(os.getenv("OTP_TIMEOUT_MINUTES", "10"))
MAX_ACTIVE_SIMS = int(os.getenv("MAX_ACTIVE_SIMS", "4"))
MAX_ACTIVE_REQUESTS = int(os.getenv("MAX_ACTIVE_REQUESTS", "2"))
SIM_USAGE_LIMIT = int(os.getenv("SIM_USAGE_LIMIT", "15"))
PAGE_SIZE = int(os.getenv("PAGE_SIZE", "5"))

logging.basicConfig(level=logging.ERROR, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger("otp_bot")
# Force ERROR-only logging to suppress INFO/DEBUG from libraries
try:
    logging.getLogger().setLevel(logging.ERROR)
except Exception:
    pass
# Raise common noisy library loggers to ERROR as well
for noisy in (
    "httpx",
    "urllib3",
    "aiohttp",
    "aiohttp.client",
    "telegram",
    "telegram.bot",
    "telegram.ext",
    "telegram.vendor",
    "asyncio",
    "urllib3.connectionpool",
):
    try:
        logging.getLogger(noisy).setLevel(logging.ERROR)
    except Exception:
        pass

# ---------------------------------------------------------------------------
# HELPERS: Rate limiter, send helper, misc
# ---------------------------------------------------------------------------
# Simple in-memory rate limiter: {key: deque[timestamps]}
_rate_buckets: dict[str, deque] = defaultdict(lambda: deque())


def check_rate_limit(key: str, limit: int, per_seconds: int) -> bool:
    """Return True if allowed, False if rate limit exceeded."""
    now = time.time()
    dq = _rate_buckets[key]
    # drop old
    while dq and dq[0] <= now - per_seconds:
        dq.popleft()
    if len(dq) >= limit:
        return False
    dq.append(now)
    return True


async def safe_send(bot, chat_id: int, text: str, max_retries: int = 3, delay: float = 1.0, **kwargs):
    """Send a message with simple retry/backoff. Returns Message or raises last exception."""
    last_exc = None
    for attempt in range(1, max_retries + 1):
        try:
            return await bot.send_message(chat_id=chat_id, text=text, **kwargs)
        except Exception as exc:
            last_exc = exc
            logger.warning("send_message failed (attempt %s) to %s: %s", attempt, chat_id, exc)
            await asyncio.sleep(delay * attempt)
    raise last_exc


def admin_targets() -> list[int]:
    return [admin_id for admin_id in {OWNER_ID, *SUPPORT_ADMINS} if admin_id]


async def broadcast_admins(context: ContextTypes.DEFAULT_TYPE, text: str, **kwargs) -> None:
    for admin_id in admin_targets():
        try:
            await safe_send(context.bot, admin_id, text, **kwargs)
        except Exception:
            logger.warning("Could not notify admin %s", admin_id)


async def safe_edit(query, *args, **kwargs):
    """Call `edit_message_text` on a CallbackQuery and ignore 'Message is not modified' errors."""
    try:
        return await query.edit_message_text(*args, **kwargs)
    except TelegramError as e:
        msg = str(e)
        if 'Message is not modified' in msg or 'message is not modified' in msg.lower():
            return None
        raise


async def notify_pending_recharge(context: ContextTypes.DEFAULT_TYPE, ref: str, amount: int, user_row) -> None:
    # user_row may contain some of: user_id, username, full_name, code — include whatever is available
    parts = ["📥 New recharge pending"]
    # Normalize row-like objects to a dict so `.get()` works whether we receive sqlite3.Row or dict
    info = dict(user_row) if user_row is not None else {}
    if info.get('full_name'):
        parts.append(f"Name: {md(info.get('full_name'))}")
    if info.get('username'):
        parts.append(f"User: @{md(info.get('username'))}")
    if info.get('user_id'):
        parts.append(f"User ID: {md(info.get('user_id'))}")
    if info.get('code'):
        parts.append(f"Code: {md(info.get('code'))}")
    parts.append(f"Amount: ₹{md(amount)}")
    parts.append(f"UTR/Ref: `{md(ref)}`")
    admin_text = "\n".join(parts)
    admin_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Approve", callback_data=f"admin:approve:{ref}"), InlineKeyboardButton("❌ Reject", callback_data=f"admin:reject:{ref}")],
        [InlineKeyboardButton("ℹ️ Details", callback_data=f"admin:details:{ref}:0")],
    ])
    await broadcast_admins(context, admin_text, reply_markup=admin_kb)


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def md(text: object) -> str:
    return escape_markdown(str(text), version=2)


def build_panel(title: str, lines: list[str], footer: str | None = None) -> str:
    parts = [f"━━ {title} ━━", ""]
    parts.extend(lines)
    if footer:
        parts.extend(["", footer])
    return "\n".join(parts)


async def get_setting(key: str) -> Optional[str]:
    async with db_connect() as db:
        row = await (await db.execute("SELECT value FROM settings WHERE key = ?", (key,))).fetchone()
        return row['value'] if row else None


async def set_setting(key: str, value: str) -> None:
    async with db_connect() as db:
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))
        await db.commit()


async def set_user_action(user_id: str, action: str, payload: str | None = None, ttl_minutes: int = 30) -> None:
    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)).isoformat()
    _awaiting_actions[user_id] = action if payload is None else f"{action}:{payload}"
    async with db_connect() as db:
        await db.execute(
            "INSERT OR REPLACE INTO user_actions (user_id, action, payload, expires_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (user_id, action, payload, expires_at, now_utc())
        )
        await db.commit()


async def get_user_action(user_id: str) -> Optional[str]:
    async with db_connect() as db:
        row = await (await db.execute("SELECT action, payload, expires_at FROM user_actions WHERE user_id = ?", (user_id,))).fetchone()
        if not row:
            return _awaiting_actions.get(user_id)
        try:
            expires_at = datetime.fromisoformat(row['expires_at'])
        except ValueError:
            expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        if expires_at <= datetime.now(timezone.utc):
            await db.execute("DELETE FROM user_actions WHERE user_id = ?", (user_id,))
            await db.commit()
            _awaiting_actions.pop(user_id, None)
            return None
    action = row['action'] if row['payload'] is None else f"{row['action']}:{row['payload']}"
    _awaiting_actions[user_id] = action
    return action


async def clear_user_action(user_id: str) -> None:
    _awaiting_actions.pop(user_id, None)
    async with db_connect() as db:
        await db.execute("DELETE FROM user_actions WHERE user_id = ?", (user_id,))
        await db.commit()


@asynccontextmanager
async def db_connect():
    async with aiosqlite.connect(DB_FILE) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys = ON")
        yield db


# in-memory short-lived awaiting actions per user: user_id -> action
_awaiting_actions: dict[str, str] = {}


def generate_ref() -> str:
    return secrets.token_hex(4).upper()


def normalize_payment_id(text: str) -> str:
    return re.sub(r"\s+", "", text.strip()).upper()


def is_valid_payment_id(payment_id: str) -> bool:
    return bool(re.fullmatch(r"[A-Z0-9._/-]{6,40}", payment_id))


def recharge_amount_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("₹50", callback_data="wallet:recharge:amt:50"),
            InlineKeyboardButton("₹100", callback_data="wallet:recharge:amt:100"),
            InlineKeyboardButton("₹200", callback_data="wallet:recharge:amt:200"),
        ],
        [
            InlineKeyboardButton("₹500", callback_data="wallet:recharge:amt:500"),
            InlineKeyboardButton("₹1000", callback_data="wallet:recharge:amt:1000"),
            InlineKeyboardButton("Custom", callback_data="wallet:recharge:custom"),
        ],
        [InlineKeyboardButton("🆘 Contact support", url=SUPPORT_URL)],
        [InlineKeyboardButton("◀️ Back", callback_data="menu:home")],
    ])


def payment_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ I have paid", callback_data="wallet:ihavepaid")],
        [InlineKeyboardButton("Change amount", callback_data="wallet:recharge")],
        [InlineKeyboardButton("🆘 Contact support", url=SUPPORT_URL)],
        [InlineKeyboardButton("◀️ Back", callback_data="wallet:cancel_recharge"), InlineKeyboardButton("🏠 Home", callback_data="menu:home")],
    ])


async def send_payment_instructions(message, amount: int) -> None:
    upi = await get_setting('upi_id') or UPI_ID
    qr = await get_setting('qr_file_id')
    lines = [
        f"Amount: ₹{amount}",
        f"UPI ID: {upi}",
        "",
        "Pay this exact amount in any UPI app.",
        "After payment, tap I have paid and send the UPI transaction ID / UTR from your receipt.",
    ]
    text = build_panel("Recharge Payment", lines, "Admins will approve it after checking the payment.")
    if qr:
        try:
            await message.reply_photo(qr, caption=text, reply_markup=payment_keyboard())
            return
        except Exception:
            logger.warning("Could not send payment QR; falling back to text instructions.")
    await message.reply_text(text, reply_markup=payment_keyboard())


async def edit_payment_instructions(query, amount: int) -> None:
    upi = await get_setting('upi_id') or UPI_ID
    qr = await get_setting('qr_file_id')
    lines = [
        f"Amount: ₹{amount}",
        f"UPI ID: {upi}",
        "",
        "Pay this exact amount in any UPI app.",
        "After payment, tap I have paid and send the UPI transaction ID / UTR from your receipt.",
    ]
    text = build_panel("Recharge Payment", lines, "Admins will approve it after checking the payment.")
    if qr:
        try:
            await safe_edit(query, "Payment details are below.")
            await query.message.reply_photo(qr, caption=text, reply_markup=payment_keyboard())
            return
        except Exception:
            logger.warning("Could not send payment QR from callback; falling back to text instructions.")
    await safe_edit(query, text, reply_markup=payment_keyboard())


async def init_db() -> None:
    async with db_connect() as db:
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                role TEXT NOT NULL DEFAULT 'user',
                code TEXT UNIQUE,
                created_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS wallets (
                user_id TEXT PRIMARY KEY,
                balance INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            );

            CREATE TABLE IF NOT EXISTS wallet_ledger (
                ledger_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                amount INTEGER NOT NULL,
                kind TEXT NOT NULL,
                note TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS recharges (
                ref TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                amount INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                reviewed_by TEXT,
                reviewed_at TEXT
            );

            CREATE TABLE IF NOT EXISTS admins (
                user_id TEXT PRIMARY KEY,
                added_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sim_numbers (
                sim_id INTEGER PRIMARY KEY AUTOINCREMENT,
                number TEXT NOT NULL UNIQUE,
                owner_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                in_use INTEGER NOT NULL DEFAULT 0,
                otp_count INTEGER NOT NULL DEFAULT 0,
                delisted_at TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (owner_id) REFERENCES users(user_id)
            );

            CREATE TABLE IF NOT EXISTS otp_requests (
                request_id TEXT PRIMARY KEY,
                taker_id TEXT NOT NULL,
                giver_id TEXT NOT NULL,
                sim_id INTEGER NOT NULL,
                number TEXT NOT NULL,
                status TEXT NOT NULL,
                charged_amount INTEGER NOT NULL,
                payout_amount INTEGER NOT NULL,
                giver_message_id INTEGER,
                otp_text TEXT,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                completed_at TEXT
            );

            CREATE TABLE IF NOT EXISTS user_logs (
                log_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                action TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS admin_actions (
                action_id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_id TEXT NOT NULL,
                action_type TEXT NOT NULL,
                ref TEXT,
                user_id TEXT,
                amount INTEGER,
                note TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (admin_id) REFERENCES admins(user_id),
                FOREIGN KEY (ref) REFERENCES recharges(ref),
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            );

            CREATE TABLE IF NOT EXISTS user_actions (
                user_id TEXT PRIMARY KEY,
                action TEXT NOT NULL,
                payload TEXT,
                expires_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        if OWNER_ID:
            await db.execute("INSERT OR IGNORE INTO admins (user_id, added_at) VALUES (?, ?)", (str(OWNER_ID), now_utc()))
        for admin_id in SUPPORT_ADMINS:
            await db.execute("INSERT OR IGNORE INTO admins (user_id, added_at) VALUES (?, ?)", (str(admin_id), now_utc()))
        await db.commit()


async def ensure_user(user) -> None:
    uid = str(user.id)
    username = user.username or ""
    full_name = (user.full_name or username or "User")
    async with db_connect() as db:
        await db.execute(
            "INSERT OR IGNORE INTO users (user_id, username, full_name, code, created_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?)",
            (uid, username, full_name, secrets.token_hex(3), now_utc(), now_utc()),
        )
        await db.execute("UPDATE users SET username = ?, full_name = ?, last_seen_at = ? WHERE user_id = ?", (username, full_name, now_utc(), uid))
        await db.execute("INSERT OR IGNORE INTO wallets (user_id, balance) VALUES (?, 0)", (uid,))
        await db.commit()

    # ---------------------------------------------------------------------------
    # USER & WALLET HELPERS
    # - `ensure_user(user)`
    # - `add_wallet_entry(db, user_id, amount, kind, note)`
    # ---------------------------------------------------------------------------


async def add_wallet_entry(db: aiosqlite.Connection, user_id: str, amount: int, kind: str, note: str) -> None:
    await db.execute("INSERT OR IGNORE INTO wallets (user_id, balance) VALUES (?, 0)", (user_id,))
    if amount < 0:
        wallet = await (await db.execute("SELECT balance FROM wallets WHERE user_id = ?", (user_id,))).fetchone()
        if not wallet or wallet['balance'] + amount < 0:
            raise ValueError(f"Insufficient wallet balance for user {user_id}")
    await db.execute("UPDATE wallets SET balance = balance + ? WHERE user_id = ?", (amount, user_id))
    await db.execute("INSERT INTO wallet_ledger (user_id, amount, kind, note, created_at) VALUES (?, ?, ?, ?, ?)", (user_id, amount, kind, note, now_utc()))


def main_menu_keyboard(is_admin: bool = False, is_provider: bool = False) -> InlineKeyboardMarkup:
    """Return the main menu inline keyboard.

    This is synchronous and only builds the keyboard markup. Avoid side-effects
    like setting bot commands here.
    """
    rows = []
    if is_provider:
        rows.append([InlineKeyboardButton("🔐 Provide OTP", callback_data="menu:provider"), InlineKeyboardButton("💳 Wallet", callback_data="wallet:view")])
    else:
        rows.append([InlineKeyboardButton("🔐 Get OTP", callback_data="menu:receiver"), InlineKeyboardButton("💳 Recharge", callback_data="wallet:recharge")])

    # SIMs / recharges row
    if is_provider:
        rows.append([InlineKeyboardButton("📋 My SIMs", callback_data="sims:my"), InlineKeyboardButton("💸 Earn with SIM", callback_data="menu:provider")])
    else:
        rows.append([InlineKeyboardButton("📋 My SIMs", callback_data="sims:my"), InlineKeyboardButton("📄 My recharges", callback_data="wallet:recharges")])

    # Help / support
    rows.append([InlineKeyboardButton("❓ Help", callback_data="help:view"), InlineKeyboardButton("🆘 Contact support", url=SUPPORT_URL)])

    if is_admin:
        rows.append([InlineKeyboardButton("🛠️ Admin Console", callback_data="menu:admin")])

    return InlineKeyboardMarkup(rows)


async def send_home(update: Update, context: ContextTypes.DEFAULT_TYPE, edit: bool = False) -> None:
    user = update.effective_user
    await ensure_user(user)
    uid = str(user.id)
    async with db_connect() as db:
        row = await (await db.execute("SELECT u.code, u.role, w.balance FROM users u JOIN wallets w ON w.user_id = u.user_id WHERE u.user_id = ?", (uid,))).fetchone()
    role_label = "Provider" if row["role"] == "giver" else "Member"
    if user.id == OWNER_ID:
        role_label = "Owner"
    elif user.id in SUPPORT_ADMINS:
        role_label = "Admin"

    # Rich profile panel with quick-action buttons
    text_lines = [
        f"User ID: {uid}",
        f"Username: @{(user.username or 'n/a')}",
        f"Balance: 💰 ₹{row['balance']}",
        f"Role: {role_label}",
    ]
    text = build_panel(
        f"✨ Welcome {md(user.full_name or user.username or 'User')}!",
        text_lines,
        "Quick actions below — tap any button to proceed."
    )

    # Minimal sensible quick-actions for users
    kb_rows = [
        [InlineKeyboardButton("🔐 Get OTP", callback_data="menu:receiver"), InlineKeyboardButton("💳 Recharge", callback_data="wallet:recharge")],
        [InlineKeyboardButton("📋 My SIMs", callback_data="sims:my"), InlineKeyboardButton("📄 My recharges", callback_data="wallet:recharges")],
        [InlineKeyboardButton("❓ Help", callback_data="help:view"), InlineKeyboardButton("🆘 Contact support", url=SUPPORT_URL)],
    ]
    if user.id == OWNER_ID or user.id in SUPPORT_ADMINS:
        kb_rows.append([InlineKeyboardButton("🛠️ Admin Console", callback_data="menu:admin")])
    kb = InlineKeyboardMarkup(kb_rows)
    if edit and update.callback_query:
        msg = await safe_edit(update.callback_query, text, reply_markup=kb)
    else:
        msg = await update.effective_message.reply_text(text, reply_markup=kb)
    return msg

# ---------------------------------------------------------------------------
# HANDLERS: START / MAIN MENU
# - `start_cmd`
# - `menu_handler`
# ---------------------------------------------------------------------------


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = await send_home(update, context)
    # Try to pin a main-menu message for convenience (best-effort).
    try:
        if msg and hasattr(msg, 'chat'):
            await context.bot.pin_chat_message(chat_id=msg.chat.id, message_id=msg.message_id)
    except Exception:
        # ignore pin failures (private chats or insufficient rights)
        pass


async def profile_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_home(update, context)


async def getotp_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await ensure_user(update.effective_user)
    await update.message.reply_text(
        build_panel(
            "Receive OTP",
            ["Pick a number from the live queue and wait for the OTP to be delivered."],
            "Keep your wallet funded so the request can go through immediately."
        ),
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📞 Choose number", callback_data="otp:list")]])
    )


async def recharge_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await ensure_user(update.effective_user)
    await update.message.reply_text(
        build_panel("Recharge Wallet", ["Choose an amount to add to your wallet.", "Pay with UPI, then send the transaction ID / UTR from your payment receipt."], "No extra user code is needed."),
        reply_markup=recharge_amount_keyboard()
    )


async def menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await ensure_user(query.from_user)
    data = query.data
    if data == "menu:receiver":
        await safe_edit(query,
            build_panel(
                "Receive OTP",
                ["Pick a number from the live queue and wait for the OTP to be delivered."],
                "Keep your wallet funded so the request can go through immediately."
            ),
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("📞 Choose number", callback_data="otp:list")],
                    [InlineKeyboardButton("◀️ Back", callback_data="menu:home")],
                ]
            ),
        )
        return
    if data == "menu:provider":
        async with db_connect() as db:
            await db.execute("UPDATE users SET role = 'giver' WHERE user_id = ?", (str(query.from_user.id),))
            await db.commit()
        await safe_edit(query,
            build_panel(
                "Provider Mode Enabled",
                ["Your account is now set up for SIM sharing and OTP earning."],
                "Register a SIM, check your wallet, and manage active numbers from one place."
            ),
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("📱 My SIMs", callback_data="sims:my")],
                    [InlineKeyboardButton("➕ Add SIM", callback_data="sims:add")],
                    [InlineKeyboardButton("◀️ Back", callback_data="menu:home")],
                ]
            ),
        )
        return
    if data == "menu:profile":
        # Show the user's home/profile panel (edit mode when from callback)
        await send_home(update, context, edit=True)
        return
    if data == "menu:admin":
        await safe_edit(query,
            build_panel(
                "Admin Console",
                ["Review recharges, inspect users, and keep the queue moving."],
                "Use the shortcuts below to jump directly into the workflow."
            ),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📊 Dashboard", callback_data="admin:dashboard"), InlineKeyboardButton("📥 Pending queue", callback_data="admin:pending")],
                [InlineKeyboardButton("📈 Today", callback_data="admin:recharges:today"), InlineKeyboardButton("➕ Adjust wallet", callback_data="admin:addfunds")],
                [InlineKeyboardButton("Search", callback_data="admin:search"), InlineKeyboardButton("Health", callback_data="admin:health")],
                [InlineKeyboardButton("◀️ Home", callback_data="menu:home")],
            ])
        )
        return
    if data == "menu:home":
        await send_home(update, context, edit=True)
        return
    if data.startswith("help:"):
        await help_callback(update, context)
        return


async def wallet_view(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await ensure_user(update.effective_user)
    uid = str(update.effective_user.id)
    async with db_connect() as db:
        row = await (await db.execute("SELECT u.code, u.full_name, w.balance FROM users u JOIN wallets w ON u.user_id = w.user_id WHERE u.user_id = ?", (uid,))).fetchone()
        ledger = await (await db.execute("SELECT amount, kind, note, created_at FROM wallet_ledger WHERE user_id = ? ORDER BY ledger_id DESC LIMIT 5", (uid,))).fetchall()

    lines = [f"Code: {row['code']}", f"Balance: ₹{row['balance']}"]
    if ledger:
        lines.append("")
        lines.append("Recent activity:")
        for r in ledger:
            sign = "+" if r['amount'] > 0 else ""
            note = r['note'] or ""
            lines.append(f"{sign}{r['amount']} {r['kind']} · {note}")

    await update.effective_message.reply_text(
        build_panel("Wallet", lines, "Top up with UPI and track the last five ledger entries here."),
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("💳 Recharge", callback_data="wallet:recharge"), InlineKeyboardButton("📄 My recharges", callback_data="wallet:recharges")],
            [InlineKeyboardButton("📋 My SIMs", callback_data="sims:my")],
            [InlineKeyboardButton("◀️ Back", callback_data="menu:home")],
        ])
    )

# ---------------------------------------------------------------------------
# HANDLERS: WALLET
# - `wallet_view`
# - `wallet_callback`
# - inline `wallet:submit_ref` flow
# ---------------------------------------------------------------------------


async def wallet_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    uid = str(query.from_user.id)
    if query.data.startswith("wallet:recharges"):
        await my_recharges_callback(update, context)
        return
    if query.data == "wallet:recharge":
        text = build_panel(
            "Recharge Wallet",
            [
                "Choose an amount to add to your wallet.",
                "Pay with UPI, then send the transaction ID / UTR from your payment receipt.",
            ],
            "No extra user code is needed."
        )
        await safe_edit(query, text, reply_markup=recharge_amount_keyboard())
        return
    if query.data.startswith("wallet:recharge:amt:"):
        try:
            amount = int(query.data.rsplit(":", 1)[1])
        except ValueError:
            await safe_edit(query, "Invalid amount.", reply_markup=recharge_amount_keyboard())
            return
        if amount <= 0:
            await safe_edit(query, "Amount must be positive.", reply_markup=recharge_amount_keyboard())
            return
        await set_user_action(uid, "recharge_amount", str(amount), ttl_minutes=30)
        await edit_payment_instructions(query, amount)
        return
    if query.data == "wallet:recharge:custom":
        await set_user_action(uid, "enter_recharge_amount", ttl_minutes=30)
        await safe_edit(query, build_panel("Enter Recharge Amount", ["Type the amount you want to add to your wallet.", "Example: 500"], "You can cancel at any time."), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Back", callback_data="wallet:recharge")],[InlineKeyboardButton("🏠 Home", callback_data="menu:home")]]))
        return
    if query.data == "wallet:ihavepaid":
        current = await get_user_action(uid)
        if not current or not current.startswith("recharge_amount:"):
            await safe_edit(query, build_panel("Choose Amount First", ["Select a recharge amount before submitting payment details."], None), reply_markup=recharge_amount_keyboard())
            return
        amount = int(current.split(":", 1)[1])
        await set_user_action(uid, "submit_payment_id", str(amount), ttl_minutes=30)
        await safe_edit(query, build_panel("Send Transaction ID", [f"Amount: ₹{amount}", "Send only the UPI transaction ID / UTR from your payment receipt.", "Example: 425783920184"], "Do not send your user code or phone number."), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Change amount", callback_data="wallet:recharge")],[InlineKeyboardButton("🏠 Home", callback_data="menu:home")]]))
        return
    if query.data == "wallet:submit_ref":
        current = await get_user_action(uid)
        if current and current.startswith("recharge_amount:"):
            amount = int(current.split(":", 1)[1])
            await set_user_action(uid, "submit_payment_id", str(amount), ttl_minutes=30)
            await safe_edit(query,
                build_panel(
                    "Send Transaction ID",
                    [
                        f"Amount: ₹{amount}",
                        "Send only the UPI transaction ID / UTR from your payment receipt.",
                        "Example: 425783920184",
                    ],
                    "Do not send your user code or phone number."
                ),
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Change amount", callback_data="wallet:recharge")],
                    [InlineKeyboardButton("🏠 Home", callback_data="menu:home")],
                ])
            )
            return
        await safe_edit(query, build_panel("Choose Amount First", ["Select a recharge amount before submitting payment details."], None), reply_markup=recharge_amount_keyboard())
        return
    if query.data == "wallet:cancel_recharge":
        await clear_user_action(uid)
        await safe_edit(query, "Recharge cancelled.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Home", callback_data="menu:home")]]))
        return
    if query.data == "wallet:view":
        await wallet_view(update, context)
        return
    await safe_edit(query, build_panel("Wallet", ["That wallet action is no longer active."], "Use the buttons below."), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💳 Recharge", callback_data="wallet:recharge"), InlineKeyboardButton("📄 My recharges", callback_data="wallet:recharges")],[InlineKeyboardButton("🏠 Home", callback_data="menu:home")]]))
    return
    if query.data == "wallet:recharge":
        uid = str(query.from_user.id)
        # Show preset amounts and a custom entry option
        text = build_panel(
            "Recharge Wallet",
            ["Choose a preset amount or press Custom to enter any amount.", f"UPI: {UPI_ID}"],
            "After paying, submit your UTR / Reference for admin review."
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("₹20", callback_data="wallet:recharge:amt:20"), InlineKeyboardButton("₹30", callback_data="wallet:recharge:amt:30"), InlineKeyboardButton("₹50", callback_data="wallet:recharge:amt:50")],
            [InlineKeyboardButton("₹100", callback_data="wallet:recharge:amt:100"), InlineKeyboardButton("Custom amount", callback_data="wallet:recharge:custom")],
            [InlineKeyboardButton("🆘 Contact support", url=SUPPORT_URL)],
            [InlineKeyboardButton("◀️ Back", callback_data="menu:home"), InlineKeyboardButton("❓ Help", callback_data="help:view")]
        ])
        await safe_edit(query, text, reply_markup=kb)
        return
    if query.data == "wallet:submit_ref":
        uid = str(query.from_user.id)
        # If a draft exists for this user, show a one-tap confirmation using the stored ref.
        async with db_connect() as db:
            draft = await (await db.execute("SELECT ref, amount FROM recharges WHERE user_id = ? AND status = 'draft' ORDER BY created_at DESC LIMIT 1", (uid,))).fetchone()

        if draft:
            ref = draft['ref']
            amount = draft['amount']
            text = build_panel(
                "Submit Payment Reference",
                [f"Reference: {ref}", f"Amount: ₹{amount}", "", "If you paid with this reference, confirm below."],
                "You can also choose to submit a different reference manually."
            )
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Confirm submit", callback_data=f"wallet:confirm_ref:{ref}" )],
                [InlineKeyboardButton("✏️ I used a different reference", callback_data="wallet:manual_ref")],
                [InlineKeyboardButton("◀️ Back", callback_data="menu:home")],
            ])
            await safe_edit(query, text, reply_markup=kb)
            return

        # no draft available — check if user previously selected an amount
        current = _awaiting_actions.get(uid)
        if current and current.startswith('submit_ref_amount:'):
            # already have an amount selected — ask user to send only the reference
            amount = int(current.split(':', 1)[1])
            prompt = build_panel(
                "Submit Payment Reference",
                [f"Selected amount: ₹{amount}", "Send only the UPI reference you used for payment.", "Example: REF1234"],
                "If you did not select an amount, go back and choose one."
            )
            await safe_edit(query, prompt, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Back", callback_data="menu:home")]]))
            return

        # completely manual flow: request ref and amount together
        _awaiting_actions[uid] = 'submit_ref'
        prompt = build_panel(
            "Submit Payment Reference",
            ["Send the payment reference and amount in one message.", "Example: REF1234 100"],
            "Use Back to cancel."
        )
        await safe_edit(query, prompt, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Back", callback_data="menu:home")]]))
        return

    # handle amount selection
    if query.data.startswith("wallet:recharge:amt:"):
        parts = query.data.split(":")
        amount = int(parts[-1])
        uid = str(query.from_user.id)

        # rate-limit recharge requests: max 3 per hour per user
        if not check_rate_limit(f"recharge:{uid}", limit=3, per_seconds=3600):
            await safe_edit(query, "You're creating recharges too quickly. Try later.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Back", callback_data="menu:home")]]))
            return

        # Do NOT generate an internal reference. Users will pay using their own UPI reference.
        # Remember the desired amount and prompt user to submit their UPI reference after payment.
        _awaiting_actions[uid] = f"submit_ref_amount:{amount}"

        text = build_panel(
            "Payment Instructions",
            [f"Amount: ₹{amount}", f"UPI ID: {UPI_ID}", "", "After paying, press Submit payment reference and send only the reference."],
            "You will be asked to provide the exact UPI reference you used."
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✉️ Submit payment reference", callback_data="wallet:submit_ref")],
            [InlineKeyboardButton("🆘 Contact support", url=SUPPORT_URL)],
            [InlineKeyboardButton("◀️ Back", callback_data="menu:home")]
        ])
        await safe_edit(query, text, reply_markup=kb)
        return
    if query.data == "wallet:recharge:custom":
        uid = str(query.from_user.id)
        _awaiting_actions[uid] = 'enter_recharge_amount'
        prompt = build_panel(
            "Enter Recharge Amount",
            ["Please type the amount you want to add to your wallet.", "Example: 500"],
            "You can cancel at any time."
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("◀️ Back", callback_data="menu:home")],
            [InlineKeyboardButton("❓ Help", callback_data="help:view")]
        ])
        await safe_edit(query, prompt, reply_markup=kb)
        return

    if query.data == "wallet:ihavepaid":
        # user indicates they've paid -> ask for UTR
        uid = str(query.from_user.id)
        current = _awaiting_actions.get(uid)
        if current and current.startswith('submit_utr:'):
            # already expecting UTR for an amount
            await safe_edit(query, "Please send your UTR / Reference Number.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Back", callback_data="menu:home")],[InlineKeyboardButton("🏠 Home", callback_data="menu:home")]]))
            return
        # fallback - ask user to enter the UTR and cancel
        _awaiting_actions[uid] = 'submit_utr:0'
        await safe_edit(query, "Please send your UTR / Reference Number.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Back", callback_data="menu:home")],[InlineKeyboardButton("🏠 Home", callback_data="menu:home")]]))
        return

    if query.data == "wallet:cancel_recharge":
        uid = str(query.from_user.id)
        _awaiting_actions.pop(uid, None)
        await safe_edit(query, "Recharge cancelled.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Home", callback_data="menu:home")]]))
        return


async def my_recharges_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await ensure_user(query.from_user)
    uid = str(query.from_user.id)
    parts = query.data.split(":")
    page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
    async with db_connect() as db:
        total = (await (await db.execute("SELECT COUNT(*) AS c FROM recharges WHERE user_id = ?", (uid,))).fetchone())['c']
        rows = await (await db.execute(
            "SELECT ref, amount, status, created_at, reviewed_at FROM recharges WHERE user_id = ? ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (uid, PAGE_SIZE, page * PAGE_SIZE)
        )).fetchall()

    lines = [f"Total recharges: {total}"]
    if not rows:
        lines.append("No recharge requests yet.")
    else:
        lines.append("")
        for row in rows:
            reviewed = f" reviewed {row['reviewed_at']}" if row['reviewed_at'] else ""
            lines.append(f"{row['ref']} · ₹{row['amount']} · {row['status']}{reviewed}")

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"wallet:recharges:{page-1}"))
    if (page + 1) * PAGE_SIZE < total:
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"wallet:recharges:{page+1}"))
    kb_rows = [nav] if nav else []
    kb_rows.append([InlineKeyboardButton("💳 Recharge", callback_data="wallet:recharge")])
    kb_rows.append([InlineKeyboardButton("🏠 Home", callback_data="menu:home")])
    await safe_edit(query, build_panel("My Recharges", lines, "Pending requests are reviewed by admins."), reply_markup=InlineKeyboardMarkup(kb_rows))


async def submit_ref(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await ensure_user(update.effective_user)
    await update.message.reply_text(
        build_panel(
            "Recharge Updated",
            ["The old /ref command is retired.", "Use Recharge, pay with UPI, then send only the transaction ID / UTR when the bot asks."],
            None
        ),
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💳 Recharge", callback_data="wallet:recharge")], [InlineKeyboardButton("📄 My recharges", callback_data="wallet:recharges")]])
    )
    return
    if len(context.args) != 2:
        await update.message.reply_text("Usage: /ref <reference> <amount>")
        return
    ref = context.args[0].strip().upper()
    if not re.fullmatch(r"[A-Z0-9._-]{4,40}", ref):
        await update.message.reply_text("Invalid reference format.")
        return
    try:
        amount = int(context.args[1])
    except ValueError:
        await update.message.reply_text("Amount must be a number.")
        return
    if amount <= 0:
        await update.message.reply_text("Amount must be positive.")
        return

    uid = str(update.effective_user.id)
    # rate-limit refs per user: max 5 per hour
    if not check_rate_limit(f"ref:{update.effective_user.id}", limit=5, per_seconds=3600):
        await update.message.reply_text("You're submitting references too quickly. Try later.")
        return

    async with db_connect() as db:
        existing = await (await db.execute("SELECT ref, status FROM recharges WHERE ref = ?", (ref,))).fetchone()
        if existing:
            if existing['status'] == 'draft':
                await db.execute("UPDATE recharges SET status='pending', reviewed_by=NULL, reviewed_at=NULL WHERE ref = ?", (ref,))
                row = await (await db.execute("SELECT user_id, username, code, full_name FROM users WHERE user_id = ?", (uid,))).fetchone()
                await db.commit()
                await update.message.reply_text(f"Reference {ref} submitted for review.")
                await notify_pending_recharge(context, ref, amount, row)
                return
            if existing['status'] == 'approved':
                await update.message.reply_text(f"This reference `{md(ref)}` was already approved. You cannot resubmit it.")
                return
            if existing['status'] == 'pending':
                await update.message.reply_text(f"This reference `{md(ref)}` is already pending review.")
                return
            await update.message.reply_text("This reference is already submitted.")
            return
        
        await db.execute("INSERT INTO recharges (ref, user_id, amount, status, created_at) VALUES (?, ?, ?, 'pending', ?)", (ref, uid, amount, now_utc()))
        row = await (await db.execute("SELECT user_id, username, code, full_name FROM users WHERE user_id = ?", (uid,))).fetchone()
        await db.commit()

    await notify_pending_recharge(context, ref, amount, row)

    await update.message.reply_text(f"Reference {ref} submitted for review.")


async def wallet_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    parts = query.data.split(":")
    if len(parts) < 3:
        await safe_edit(query, "Invalid confirmation data.")
        return
    ref = parts[2].strip().upper()
    uid = str(query.from_user.id)
    async with db_connect() as db:
        row = await (await db.execute("SELECT ref, user_id, amount, status FROM recharges WHERE ref = ? AND user_id = ?", (ref, uid))).fetchone()
        if not row:
            await safe_edit(query, "No matching draft found. You can submit the reference manually.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✏️ Submit manually", callback_data="wallet:manual_ref")],[InlineKeyboardButton("◀️ Back", callback_data="menu:home")]]))
            return
        if row['status'] != 'draft':
            await safe_edit(query, "This reference is not a draft or has already been submitted.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Back", callback_data="menu:home")]]))
            return
        await db.execute("UPDATE recharges SET status='pending' WHERE ref = ?", (ref,))
        user_row = await (await db.execute("SELECT user_id, username, code, full_name FROM users WHERE user_id = ?", (uid,))).fetchone()
        await db.commit()

    # notify admins
    await notify_pending_recharge(context, ref, row['amount'], user_row)
    await safe_edit(query, f"Reference {ref} submitted for review. Thank you.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Back", callback_data="menu:home")]]))


async def wallet_manual_ref_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    uid = str(query.from_user.id)
    current = _awaiting_actions.get(uid)
    if current and current.startswith("recharge_amount:"):
        amount = int(current.split(":", 1)[1])
        _awaiting_actions[uid] = f"submit_payment_id:{amount}"
        await safe_edit(query, build_panel("Send Transaction ID", [f"Amount: ₹{amount}", "Send only the UPI transaction ID / UTR from your receipt.", "Example: 425783920184"], "Do not send your user code or phone number."), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Change amount", callback_data="wallet:recharge")],[InlineKeyboardButton("🏠 Home", callback_data="menu:home")]]))
        return
    await safe_edit(query, build_panel("Choose Amount First", ["Select a recharge amount before submitting payment details."], None), reply_markup=recharge_amount_keyboard())
    return
    _awaiting_actions[uid] = 'submit_ref'
    prompt = build_panel(
        "Submit Payment Reference",
        ["Send the payment reference and amount in one message.", "Example: REF1234 100"],
        "Use Back to cancel."
    )
    await safe_edit(query, prompt, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Back", callback_data="menu:home")]]))


def is_admin(user_id: int) -> bool:
    return user_id == OWNER_ID or user_id in SUPPORT_ADMINS


async def approve_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return
    if len(context.args) != 1:
        await update.message.reply_text("Usage: /approve <ref>")
        return
    ref = context.args[0].strip().upper()
    admin_id = str(update.effective_user.id)
    async with db_connect() as db:
        # fetch with immediate locking
        await db.execute("BEGIN IMMEDIATE")
        row = await (await db.execute("SELECT user_id, amount, status FROM recharges WHERE ref = ?", (ref,))).fetchone()
        if not row:
            await db.rollback()
            await update.message.reply_text("Reference not found.")
            return
        if row['status'] != 'pending':
            await db.rollback()
            await update.message.reply_text(f"Reference already {row['status']}. Cannot approve twice.")
            return
        # idempotent approve: only credit once
        await db.execute("UPDATE recharges SET status='approved', reviewed_by=?, reviewed_at=? WHERE ref=?", (admin_id, now_utc(), ref))
        await add_wallet_entry(db, row['user_id'], row['amount'], 'recharge', f'Approved {ref}')
        # log admin action
        await db.execute(
            "INSERT INTO admin_actions (admin_id, action_type, ref, user_id, amount, note, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (admin_id, 'approve', ref, row['user_id'], row['amount'], 'Approved via /approve command', now_utc())
        )
        await db.commit()
    await update.message.reply_text(f"✅ Approved {ref} and credited ₹{row['amount']}")
    try:
        await safe_send(context.bot, int(row['user_id']), f"✅ Your recharge {ref} was approved and ₹{row['amount']} credited")
    except Exception:
        pass

# ---------------------------------------------------------------------------
# HANDLERS: ADMIN (command fallbacks)
# - `approve_cmd`, `reject_cmd` (kept as fallbacks)
# - inline admin handlers implemented separately
# ---------------------------------------------------------------------------


async def reject_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return
    if len(context.args) != 1:
        await update.message.reply_text("Usage: /reject <ref>")
        return
    ref = context.args[0].strip().upper()
    admin_id = str(update.effective_user.id)
    async with db_connect() as db:
        # fetch with immediate locking
        await db.execute("BEGIN IMMEDIATE")
        row = await (await db.execute("SELECT user_id, status FROM recharges WHERE ref = ?", (ref,))).fetchone()
        if not row:
            await db.rollback()
            await update.message.reply_text("Reference not found.")
            return
        if row['status'] != 'pending':
            await db.rollback()
            await update.message.reply_text(f"Reference already {row['status']}. Cannot reject twice.")
            return
        await db.execute("UPDATE recharges SET status='rejected', reviewed_by=?, reviewed_at=? WHERE ref=?", (admin_id, now_utc(), ref))
        # log admin action
        await db.execute(
            "INSERT INTO admin_actions (admin_id, action_type, ref, user_id, note, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (admin_id, 'reject', ref, row['user_id'], 'Rejected via /reject command', now_utc())
        )
        await db.commit()
    await update.message.reply_text(f"❌ Rejected {ref}")
    try:
        await safe_send(context.bot, int(row['user_id']), f"❌ Your recharge {ref} was rejected. Contact support for details.")
    except TelegramError:
        pass


async def addsim_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await ensure_user(update.effective_user)
    if len(context.args) == 0:
        await update.message.reply_text(
            "📱 *Register a SIM*\n\n"
            "Send a 10-digit SIM number to register it to your account.\n"
            "You can also use /addsim <10-digit-number> directly.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ Add SIM now", callback_data="sims:add")],
                [InlineKeyboardButton("📋 My SIMs", callback_data="sims:my")],
                [InlineKeyboardButton("🏠 Home", callback_data="menu:home")],
            ])
        )
        return
    if len(context.args) != 1 or not re.fullmatch(r"\d{10}", context.args[0]):
        await update.message.reply_text(
            "❌ Send a valid 10-digit SIM number.\n\n"
            "Example: /addsim 9876543210"
        )
        return
    number = context.args[0]
    user_id = str(update.effective_user.id)
    async with db_connect() as db:
        count = await (await db.execute("SELECT COUNT(*) AS c FROM sim_numbers WHERE owner_id = ? AND status = 'active'", (user_id,))).fetchone()
        existing = await (await db.execute("SELECT sim_id FROM sim_numbers WHERE number = ?", (number,))).fetchone()
        if count['c'] >= MAX_ACTIVE_SIMS:
            await update.message.reply_text(f"You can have up to {MAX_ACTIVE_SIMS} active SIMs.")
            return
        if existing:
            await update.message.reply_text("This number is already registered.")
            return
        await db.execute("INSERT INTO sim_numbers (number, owner_id, created_at) VALUES (?, ?, ?)", (number, user_id, now_utc()))
        await db.commit()
    await update.message.reply_text(
        f"✅ SIM registered successfully. You can now earn ₹{OTP_PAYOUT} per completed OTP.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📋 My SIMs", callback_data="sims:my")],
            [InlineKeyboardButton("🏠 Home", callback_data="menu:home")],
        ])
    )

# ---------------------------------------------------------------------------
# HANDLERS: SIM MANAGEMENT
# - `addsim_cmd`, `mysims_cmd`, `sims_callback`
# ---------------------------------------------------------------------------


async def mysims_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await ensure_user(update.effective_user)
    uid = str(update.effective_user.id)
    async with db_connect() as db:
        rows = await (await db.execute("SELECT sim_id, number, status, in_use, otp_count FROM sim_numbers WHERE owner_id = ? ORDER BY sim_id DESC", (uid,))).fetchall()
    if not rows:
        await update.message.reply_text("You have no SIMs registered.")
        return
    lines = ["*My SIMs*"]
    kb = []
    for r in rows:
        busy = "busy" if r['in_use'] else "available"
        lines.append(f"`{md(r['number'])}` - {md(busy)} - OTPs: {r['otp_count']}")
        if r['status'] == 'active':
            kb.append([InlineKeyboardButton(f"🗑️ Delist {r['number'][-4:]}", callback_data=f"sims:delist:{r['sim_id']}")])
        else:
            kb.append([InlineKeyboardButton(f"♻️ Relist {r['number'][-4:]}", callback_data=f"sims:relist:{r['sim_id']}")])
    kb.append([InlineKeyboardButton("Back", callback_data="menu:home")])
    await update.message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(kb))


async def sims_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    parts = query.data.split(":")
    # handle add sim via inline: prompt user to send number
    if parts[1] == 'add':
        uid = str(query.from_user.id)
        await set_user_action(uid, 'addsim', ttl_minutes=30)
        await safe_edit(query, "📱 Send the 10-digit SIM number as a plain message.\n\nExample: 9876543210", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Back", callback_data="menu:home")]]))
        return
    if parts[1] == 'my':
        # call mysims_cmd to show the user's sims
        fake_update = type("U", (), {})()
        fake_update.effective_user = query.from_user
        fake_update.message = query.message
        fake_update.effective_message = query.message
        await mysims_cmd(fake_update, context)
        return
    if parts[1] == 'delist':
        sim_id = int(parts[2])
        uid = str(query.from_user.id)
        async with db_connect() as db:
            row = await (await db.execute("SELECT number FROM sim_numbers WHERE sim_id = ? AND owner_id = ?", (sim_id, uid))).fetchone()
            if not row:
                await safe_edit(query, "SIM not found.")
                return
            await db.execute("UPDATE sim_numbers SET status='delisted', in_use=0, delisted_at=? WHERE sim_id = ?", (now_utc(), sim_id))
            await db.commit()
        await safe_edit(query, f"SIM {row['number']} delisted.")
        return
    if parts[1] == 'relist':
        sim_id = int(parts[2])
        uid = str(query.from_user.id)
        async with db_connect() as db:
            await db.execute("UPDATE sim_numbers SET status='active', delisted_at=NULL WHERE sim_id = ? AND owner_id = ?", (sim_id, uid))
            await db.commit()
        await safe_edit(query, "SIM relisted.")


async def list_otp_sims(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await ensure_user(query.from_user)
    uid = str(query.from_user.id)
    async with db_connect() as db:
        wallet = await (await db.execute("SELECT balance FROM wallets WHERE user_id = ?", (uid,))).fetchone()
        active_requests = await (await db.execute("SELECT COUNT(*) AS c FROM otp_requests WHERE taker_id = ? AND status = 'waiting'", (uid,))).fetchone()
        rows = await (await db.execute("SELECT sim_id, number FROM sim_numbers WHERE status = 'active' AND in_use = 0 AND owner_id != ? ORDER BY otp_count ASC LIMIT 20", (uid,))).fetchall()
    if wallet['balance'] < OTP_PRICE:
        await safe_edit(query, build_panel("Insufficient Balance", [f"OTP cost: ₹{OTP_PRICE}", f"Your balance: ₹{wallet['balance']}"], "Recharge first, then choose a number."), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💳 Recharge", callback_data="wallet:recharge")],[InlineKeyboardButton("🏠 Home", callback_data="menu:home")]]))
        return
    if active_requests['c'] >= MAX_ACTIVE_REQUESTS:
        await safe_edit(query, build_panel("Active Request Limit", [f"You already have {MAX_ACTIVE_REQUESTS} active OTP requests."], "Wait for one to finish or expire."), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Home", callback_data="menu:home")]]))
        return
    if not rows:
        await safe_edit(query, build_panel("No Numbers Available", ["No active provider numbers are free right now."], "Try again shortly."), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Refresh", callback_data="otp:list")],[InlineKeyboardButton("🏠 Home", callback_data="menu:home")]]))
        return
    kb = [[InlineKeyboardButton(f"📞 Number ending {r['number'][-4:]}", callback_data=f"otp:choose:{r['sim_id']}")] for r in rows]
    kb.append([InlineKeyboardButton("◀️ Back", callback_data="menu:home")])
    await safe_edit(query, f"📞 Choose a number. Cost: ₹{OTP_PRICE}", reply_markup=InlineKeyboardMarkup(kb))

# ---------------------------------------------------------------------------
# HANDLERS: OTP REQUEST FLOW
# - `list_otp_sims`, `choose_otp_sim`, `otp_reply`, `otp_feedback`
# ---------------------------------------------------------------------------


async def choose_otp_sim(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await ensure_user(query.from_user)
    taker_id = str(query.from_user.id)
    sim_id = int(query.data.split(":")[2])
    request_id = secrets.token_hex(8)
    # rate-limit OTP requests per user
    if not check_rate_limit(f"otp:{taker_id}", limit=MAX_ACTIVE_REQUESTS * 3, per_seconds=3600):
        await safe_edit(query, "You're requesting OTPs too frequently. Try later.")
        return

    charged = False
    try:
        async with db_connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            wallet = await (await db.execute("SELECT balance FROM wallets WHERE user_id = ?", (taker_id,))).fetchone()
        if not wallet:
            await db.rollback()
            await safe_edit(query, "Wallet not found. Please press Home and try again.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Home", callback_data="menu:home")]]))
            return
            sim = await (await db.execute("SELECT sim_id, number, owner_id, in_use, status FROM sim_numbers WHERE sim_id = ?", (sim_id,))).fetchone()
            if not sim or sim['status'] != 'active' or sim['in_use']:
                await db.rollback()
                await safe_edit(query, "Number not available anymore.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Choose another", callback_data="otp:list")],[InlineKeyboardButton("🏠 Home", callback_data="menu:home")]]))
                return
            if sim['owner_id'] == taker_id:
                await db.rollback()
                await safe_edit(query, "You cannot request OTP from your own SIM.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Choose another", callback_data="otp:list")],[InlineKeyboardButton("🏠 Home", callback_data="menu:home")]]))
                return
            if wallet['balance'] < OTP_PRICE:
                await db.rollback()
                await safe_edit(query, "Insufficient balance.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💳 Recharge", callback_data="wallet:recharge")],[InlineKeyboardButton("🏠 Home", callback_data="menu:home")]]))
                return
            await db.execute("UPDATE sim_numbers SET in_use = 1 WHERE sim_id = ?", (sim_id,))
            await add_wallet_entry(db, taker_id, -OTP_PRICE, 'otp_charge', f'Request {request_id}')
            await db.execute("INSERT INTO otp_requests (request_id, taker_id, giver_id, sim_id, number, status, charged_amount, payout_amount, created_at, expires_at) VALUES (?, ?, ?, ?, ?, 'waiting', ?, ?, ?, ?)", (request_id, taker_id, sim['owner_id'], sim_id, sim['number'], OTP_PRICE, OTP_PAYOUT, now_utc(), (datetime.now(timezone.utc) + timedelta(minutes=OTP_TIMEOUT_MINUTES)).isoformat()))
            await db.commit()
            charged = True
    except Exception as exc:
        logger.exception("Error while creating OTP request: %s", exc)
        try:
            async with db_connect() as db:
                await db.execute("UPDATE sim_numbers SET in_use = 0 WHERE sim_id = ?", (sim_id,))
                await db.execute("UPDATE otp_requests SET status='failed', completed_at=? WHERE request_id = ?", (now_utc(), request_id))
                if charged:
                    await add_wallet_entry(db, taker_id, OTP_PRICE, 'otp_refund', f'Error {request_id}')
                await db.commit()
        except Exception:
            logger.exception("Failed to rollback after error")
        await safe_edit(query, "Could not create the request. You were refunded if charged.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Try again", callback_data="otp:list")],[InlineKeyboardButton("🏠 Home", callback_data="menu:home")]]))
        return

    try:
        giver_msg = await safe_send(context.bot, int(sim['owner_id']), f"OTP request for {sim['number']}\nReply to this message with the OTP. Request: {request_id}")
    except Exception:
        # notify taker and rollback if we failed to notify giver
        async with db_connect() as db:
            await db.execute("UPDATE sim_numbers SET in_use = 0 WHERE sim_id = ?", (sim_id,))
            await db.execute("UPDATE otp_requests SET status='failed', completed_at=? WHERE request_id = ?", (now_utc(), request_id))
            await add_wallet_entry(db, taker_id, OTP_PRICE, 'otp_refund', f'Failed notify {request_id}')
            await db.commit()
        await safe_edit(query, "Could not notify the SIM owner. Your amount was refunded.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Choose another", callback_data="otp:list")],[InlineKeyboardButton("🏠 Home", callback_data="menu:home")]]))
        return
    async with db_connect() as db:
        await db.execute("UPDATE otp_requests SET giver_message_id = ? WHERE request_id = ?", (giver_msg.message_id, request_id))
        await db.commit()

    await safe_edit(query, build_panel("OTP Request Started", [f"Number: {sim['number']}", f"Cost: ₹{OTP_PRICE}", f"Request ID: {request_id}"], "Waiting for the SIM owner to reply with the OTP."), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Home", callback_data="menu:home")]]))
    await schedule_request_expiry(context.application, request_id, OTP_TIMEOUT_MINUTES * 60)


async def otp_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if not message or not message.reply_to_message:
        return
    giver_id = str(message.from_user.id)
    replied_id = message.reply_to_message.message_id
    text = (message.text or "").strip()
    if not re.fullmatch(r"\d{4,10}", text):
        await message.reply_text("OTP should be digits (4-10).")
        return
    async with db_connect() as db:
        row = await (await db.execute("SELECT request_id, taker_id, sim_id FROM otp_requests WHERE giver_id = ? AND giver_message_id = ? AND status = 'waiting'", (giver_id, replied_id))).fetchone()
        if not row:
            return
        await db.execute("UPDATE otp_requests SET status='delivered', otp_text=? WHERE request_id = ?", (text, row['request_id']))
        await db.commit()

    await context.bot.send_message(int(row['taker_id']), f"OTP received: {text}\nDid it work?", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Worked", callback_data=f"otp:accept:{row['request_id']}")],[InlineKeyboardButton("❌ Problem", callback_data=f"otp:problem:{row['request_id']}")]]))
    await message.reply_text("OTP forwarded to requester.")


async def otp_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    parts = query.data.split(":")
    action = parts[1]
    request_id = parts[2]
    user_id = str(query.from_user.id)
    async with db_connect() as db:
        row = await (await db.execute("SELECT * FROM otp_requests WHERE request_id = ? AND taker_id = ?", (request_id, user_id))).fetchone()
        if not row:
            await safe_edit(query, "Request not found.")
            return
        if row['status'] not in {'delivered', 'waiting'}:
            await safe_edit(query, f"Request already {row['status']}")
            return
        if action == 'accept':
            await db.execute("UPDATE otp_requests SET status='accepted', completed_at=? WHERE request_id = ?", (now_utc(), request_id))
            await db.execute("UPDATE sim_numbers SET in_use = 0, otp_count = otp_count + 1 WHERE sim_id = ?", (row['sim_id'],))
            await add_wallet_entry(db, row['giver_id'], row['payout_amount'], 'otp_payout', f'Request {request_id}')
            simu = await (await db.execute("SELECT otp_count FROM sim_numbers WHERE sim_id = ?", (row['sim_id'],))).fetchone()
            if simu['otp_count'] >= SIM_USAGE_LIMIT:
                await db.execute("UPDATE sim_numbers SET status='delisted', in_use=0, delisted_at=? WHERE sim_id = ?", (now_utc(), row['sim_id']))
            await db.commit()
            await safe_edit(query, "Marked worked. Payout credited to giver.")
            try:
                await context.bot.send_message(int(row['giver_id']), f"OTP accepted. {row['payout_amount']} credited.")
            except TelegramError:
                pass
            return
        # problem
        await db.execute("UPDATE otp_requests SET status='disputed', completed_at=? WHERE request_id = ?", (now_utc(), request_id))
        await db.execute("UPDATE sim_numbers SET in_use = 0 WHERE sim_id = ?", (row['sim_id'],))
        await db.commit()
    await safe_edit(query, "Marked as problem. Support will review.")
    for admin_id in {OWNER_ID, *SUPPORT_ADMINS}:
        if admin_id:
            try:
                await context.bot.send_message(admin_id, f"OTP dispute: {request_id}")
            except TelegramError:
                pass


async def expire_request_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    request_id = context.job.data
    await expire_request_by_id(context.application, request_id)


async def expire_request_by_id(app: Application, request_id: str) -> None:
    async with db_connect() as db:
        row = await (await db.execute("SELECT * FROM otp_requests WHERE request_id = ?", (request_id,))).fetchone()
        if not row or row['status'] != 'waiting':
            return
        await db.execute("UPDATE otp_requests SET status='expired', completed_at=? WHERE request_id = ?", (now_utc(), request_id))
        await db.execute("UPDATE sim_numbers SET in_use = 0 WHERE sim_id = ?", (row['sim_id'],))
        await add_wallet_entry(db, row['taker_id'], row['charged_amount'], 'otp_refund', f'Timeout {request_id}')
        await db.commit()
    try:
        await app.bot.send_message(int(row['taker_id']), f"OTP request timed out. {row['charged_amount']} refunded.")
    except TelegramError:
        pass
    try:
        await app.bot.send_message(int(row['giver_id']), "OTP request expired. The SIM has been released.")
    except TelegramError:
        pass


async def schedule_request_expiry(app: Application, request_id: str, delay: int) -> None:
    if app.job_queue:
        app.job_queue.run_once(expire_request_job, delay, data=request_id, name=f"expire:{request_id}")
        return

    async def _sleep_then_expire() -> None:
        await asyncio.sleep(delay)
        await expire_request_by_id(app, request_id)

    asyncio.create_task(_sleep_then_expire())

# ---------------------------------------------------------------------------
# JOBS & BACKGROUND TASKS
# - `expire_request_job`
# - `recover_waiting_requests`
# ---------------------------------------------------------------------------


async def recover_waiting_requests(app: Application) -> None:
    async with db_connect() as db:
        rows = await (await db.execute("SELECT request_id, expires_at FROM otp_requests WHERE status = 'waiting'",)).fetchall()
    for r in rows:
        expire_time = datetime.fromisoformat(r['expires_at'])
        delay = max(1, int((expire_time - datetime.now(timezone.utc)).total_seconds()))
        await schedule_request_expiry(app, r['request_id'], delay)


async def admin_dashboard_text() -> str:
    async with db_connect() as db:
        stats = {}
        queries = {
            'users': "SELECT COUNT(*) AS v FROM users",
            'active_sims': "SELECT COUNT(*) AS v FROM sim_numbers WHERE status = 'active'",
            'pending_recharges': "SELECT COUNT(*) AS v FROM recharges WHERE status = 'pending'",
            'draft_recharges': "SELECT COUNT(*) AS v FROM recharges WHERE status = 'draft'",
            'waiting_otps': "SELECT COUNT(*) AS v FROM otp_requests WHERE status = 'waiting'",
            'disputes': "SELECT COUNT(*) AS v FROM otp_requests WHERE status = 'disputed'",
            'wallet_total': "SELECT COALESCE(SUM(balance),0) AS v FROM wallets",
            'today_recharge_count': "SELECT COUNT(*) AS v FROM recharges WHERE created_at >= datetime('now','start of day')",
            'today_recharge_total': "SELECT COALESCE(SUM(amount),0) AS v FROM recharges WHERE created_at >= datetime('now','start of day') AND status IN ('pending','approved','rejected','draft')",
            'active_providers': "SELECT COUNT(*) AS v FROM users WHERE role = 'giver'",
        }
        for k, q in queries.items():
            stats[k] = (await (await db.execute(q)).fetchone())['v']
    return build_panel(
        "Admin Dashboard",
        [
            f"Users: {stats['users']}",
            f"Active SIMs: {stats['active_sims']}",
            f"Providers: {stats['active_providers']}",
            f"Wallet total: ₹{stats['wallet_total']}",
            f"Pending recharges: {stats['pending_recharges']}",
            f"Draft recharges: {stats['draft_recharges']}",
            f"Waiting OTPs: {stats['waiting_otps']}",
            f"Disputes: {stats['disputes']}",
            f"Today's recharges: {stats['today_recharge_count']} / ₹{stats['today_recharge_total']}",
        ],
        "Use the buttons below to move quickly through the queue."
    )

# ---------------------------------------------------------------------------
# ADMIN HELPERS
# - `admin_dashboard_text`, admin callbacks
# ---------------------------------------------------------------------------


async def admin_dashboard_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📥 Pending queue", callback_data="admin:pending")],
        [InlineKeyboardButton("📈 Today", callback_data="admin:recharges:today"), InlineKeyboardButton("➕ Adjust wallet", callback_data="admin:addfunds")],
        [InlineKeyboardButton("Search", callback_data="admin:search"), InlineKeyboardButton("Health", callback_data="admin:health")],
        [InlineKeyboardButton("🔄 Refresh", callback_data="admin:dashboard"), InlineKeyboardButton("◀️ Home", callback_data="menu:home")],
    ])
    await safe_edit(query, await admin_dashboard_text(), reply_markup=kb)


async def announcement_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != OWNER_ID:
        return
    if not context.args:
        await update.message.reply_text("Usage: /announcement <message>")
        return
    text = " ".join(context.args)
    async with db_connect() as db:
        users = await (await db.execute("SELECT user_id FROM users")).fetchall()
    sent = 0
    failed = 0
    for u in users:
        try:
            await context.bot.send_message(int(u['user_id']), f"Announcement:\n\n{text}")
            sent += 1
        except TelegramError:
            failed += 1
    await update.message.reply_text(f"Announcement sent to {sent}. Failed: {failed}.")


async def addfunds_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Fallback command for admins: /addfunds <user_id|code> <amount> [note]
    if not is_admin(update.effective_user.id):
        return
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /addfunds <user_id|code> <amount> [note]")
        return
    target = context.args[0]
    try:
        amount = int(context.args[1])
    except ValueError:
        await update.message.reply_text("Amount must be an integer.")
        return
    note = " ".join(context.args[2:])[:200]
    async with db_connect() as db:
        user_row = None
        if re.fullmatch(r"\d+", target):
            user_row = await (await db.execute("SELECT user_id, full_name FROM users WHERE user_id = ?", (target,))).fetchone()
        if not user_row:
            user_row = await (await db.execute("SELECT user_id, full_name FROM users WHERE code = ?", (target,))).fetchone()
        if not user_row:
            await update.message.reply_text("Target user not found by id or code.")
            return
        await add_wallet_entry(db, user_row['user_id'], amount, 'admin_adjust', f"{note} (by admin {update.effective_user.id})")
        await db.commit()
    await update.message.reply_text(f"Adjusted {user_row['full_name']}'s balance by {amount}.")
    try:
        await safe_send(context.bot, int(user_row['user_id']), f"Your wallet was adjusted by admin: {amount}. Note: {note}")
    except Exception:
        pass


async def setupi_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Unauthorized.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /setupi payments@upi")
        return
    upi = context.args[0].strip()
    await set_setting('upi_id', upi)
    await update.message.reply_text("✅ UPI ID Updated")


async def setqr_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Unauthorized.")
        return
    uid = str(update.effective_user.id)
    await set_user_action(uid, 'awaiting_setqr', ttl_minutes=10)
    await update.message.reply_text("Please send the QR code image now. Send cancel to abort.")


async def paymentinfo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    upi = await get_setting('upi_id') or UPI_ID
    qr = await get_setting('qr_file_id')
    lines = [f"Current UPI ID:", f"{upi}", "", f"QR Code: { 'Configured' if qr else 'Not configured' }"]
    await update.message.reply_text("\n".join(lines))


async def pending_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Unauthorized.")
        return
    async with db_connect() as db:
        r = await (await db.execute("SELECT ref, user_id, amount, created_at FROM recharges WHERE status = 'pending' ORDER BY created_at DESC LIMIT 1",)).fetchone()
        if not r:
            await update.message.reply_text("No pending recharges.")
            return
        user = await (await db.execute("SELECT full_name, username FROM users WHERE user_id = ?", (r['user_id'],))).fetchone()
    text = build_panel("Pending Recharge", [f"User: @{user['username']}", f"User ID: {r['user_id']}", f"Amount: ₹{r['amount']}", f"UTR: {r['ref']}"])
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Approve", callback_data=f"admin:approve:{r['ref']}"), InlineKeyboardButton("❌ Reject", callback_data=f"admin:reject:{r['ref']}")]])
    await update.message.reply_text(text, reply_markup=kb)


async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Handle admin QR upload when awaiting
    uid = str(update.effective_user.id)
    action = await get_user_action(uid)
    await clear_user_action(uid)
    if action == 'awaiting_setqr':
        photos = update.message.photo
        if not photos:
            await update.message.reply_text("No image found. Please send a photo.")
            return
        file_id = photos[-1].file_id
        await set_setting('qr_file_id', file_id)
        await update.message.reply_text("✅ QR Code Updated")
        return


async def admin_search_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    uid = str(query.from_user.id)
    await set_user_action(uid, 'admin_search', ttl_minutes=15)
    await safe_edit(query, build_panel("Admin Search", ["Send a user ID, user code, username, or transaction ID / UTR."], "Example: 425783920184"), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Back", callback_data="admin:dashboard")]]))


async def admin_health_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    upi = await get_setting('upi_id') or UPI_ID
    qr = await get_setting('qr_file_id')
    async with db_connect() as db:
        stats = {}
        for key, sql in {
            'pending_recharges': "SELECT COUNT(*) AS c FROM recharges WHERE status = 'pending'",
            'waiting_otps': "SELECT COUNT(*) AS c FROM otp_requests WHERE status = 'waiting'",
            'stuck_sims': "SELECT COUNT(*) AS c FROM sim_numbers WHERE in_use = 1",
            'actions': "SELECT COUNT(*) AS c FROM user_actions",
        }.items():
            stats[key] = (await (await db.execute(sql)).fetchone())['c']
    lines = [
        f"Bot token: {'set' if BOT_TOKEN else 'missing'}",
        f"Owner ID: {OWNER_ID or 'missing'}",
        f"UPI ID: {upi}",
        f"QR: {'configured' if qr else 'not configured'}",
        f"Database: {DB_FILE}",
        f"Pending recharges: {stats['pending_recharges']}",
        f"Waiting OTPs: {stats['waiting_otps']}",
        f"SIMs marked busy: {stats['stuck_sims']}",
        f"Saved user actions: {stats['actions']}",
    ]
    await safe_edit(query, build_panel("Bot Health", lines, "Use Refresh after changing payment settings."), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Refresh", callback_data="admin:health")],[InlineKeyboardButton("◀️ Back", callback_data="admin:dashboard")]]))


async def admin_pending_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    # parse page if provided: admin:pending[:page]
    parts = query.data.split(":")
    page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
    async with db_connect() as db:
        total = (await (await db.execute("SELECT COUNT(*) AS c FROM recharges WHERE status = 'pending'",)).fetchone())['c']
        rows = await (await db.execute("SELECT ref, user_id, amount, created_at FROM recharges WHERE status = 'pending' ORDER BY created_at DESC LIMIT ? OFFSET ?", (PAGE_SIZE, page * PAGE_SIZE))).fetchall()
    if not rows:
        await safe_edit(query, build_panel("Pending Recharges", ["No pending recharges right now."], "Check again later or refresh the dashboard."), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Back", callback_data="menu:admin")]]))
        return
    lines = [f"Pending recharges: {total}", "", "Review the queue below:"]
    kb = []
    async with db_connect() as db:
        for r in rows:
            user = await (await db.execute("SELECT full_name, code FROM users WHERE user_id = ?", (r['user_id'],))).fetchone()
            lines.append(f"{r['ref']} · {user['full_name']} ({user['code']}) · ₹{r['amount']}")
            kb.append([
                InlineKeyboardButton(f"ℹ️ Details", callback_data=f"admin:details:{r['ref']}:{page}"),
                InlineKeyboardButton(f"✅ Approve", callback_data=f"admin:approve:{r['ref']}") ,
                InlineKeyboardButton(f"❌ Reject", callback_data=f"admin:reject:{r['ref']}")
            ])
    # navigation buttons
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"admin:pending:{page-1}"))
    if (page + 1) * PAGE_SIZE < total:
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"admin:pending:{page+1}"))
    if nav:
        kb.append(nav)
    kb.append([InlineKeyboardButton("◀️ Back", callback_data="menu:admin")])
    await safe_edit(query, build_panel("Pending Recharges", lines, None), reply_markup=InlineKeyboardMarkup(kb))


async def admin_addfunds_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    uid = str(query.from_user.id)
    await set_user_action(uid, 'admin_addfund', ttl_minutes=30)
    await safe_edit(query, build_panel("Adjust Wallet", ["Send: user_id/code amount optional-note", "Example: 123456789 100 Bonus payout"], "Send cancel to abort or go back to the admin menu."), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Back", callback_data="menu:admin")]]))
    return


async def admin_approve_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    parts = query.data.split(":")
    if len(parts) < 3:
        return
    ref = parts[2].strip().upper()
    page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
    
    admin_id = str(query.from_user.id)
    async with db_connect() as db:
        # fetch recharge with row locking to prevent race conditions
        await db.execute("BEGIN IMMEDIATE")
        row = await (await db.execute("SELECT user_id, amount, status FROM recharges WHERE ref = ?", (ref,))).fetchone()
        if not row:
            await db.rollback()
            await safe_edit(query, "❌ Reference not found.")
            return
        if row['status'] != 'pending':
            await db.rollback()
            await safe_edit(query, f"⚠️ Reference already {row['status']}. Cannot approve twice.")
            return
        
        # update status and credit wallet
        await db.execute("UPDATE recharges SET status='approved', reviewed_by=?, reviewed_at=? WHERE ref=?", (admin_id, now_utc(), ref))
        await add_wallet_entry(db, row['user_id'], row['amount'], 'recharge', f'Approved {ref}')
        # log admin action with default note
        await db.execute(
            "INSERT INTO admin_actions (admin_id, action_type, ref, user_id, amount, note, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (admin_id, 'approve', ref, row['user_id'], row['amount'], 'Approved via inline button', now_utc())
        )
        await db.commit()
    
    await safe_edit(query, f"✅ Approved {ref} and credited ₹{row['amount']} to user")
    try:
        await safe_send(context.bot, int(row['user_id']), f"✅ Your recharge {ref} was approved and ₹{row['amount']} credited to your wallet")
    except Exception:
        logger.warning("Failed to notify user %s", row['user_id'])
    await broadcast_admins(
        context,
        f"✅ Recharge approved\nRef: `{md(ref)}`\nAmount: ₹{md(row['amount'])}\nUser ID: {md(row['user_id'])}\nApproved by: {md(admin_id)}"
    )


async def admin_reject_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    parts = query.data.split(":")
    if len(parts) < 3:
        return
    ref = parts[2].strip().upper()
    page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
    # Prompt admin to provide a rejection reason; store awaiting action
    admin_id = str(query.from_user.id)
    await set_user_action(admin_id, 'admin_reject_reason', ref, ttl_minutes=30)
    await safe_edit(query, build_panel("Reject Recharge", ["Enter rejection reason:", "Example: UTR not found"], "Send cancel to abort."), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Back", callback_data=f"admin:details:{ref}:{page}")]]))


async def admin_details_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    parts = query.data.split(":")
    if len(parts) < 3:
        return
    ref = parts[2].strip().upper()
    page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
    async with db_connect() as db:
        r = await (await db.execute("SELECT ref, user_id, amount, status, created_at, reviewed_by, reviewed_at FROM recharges WHERE ref = ?", (ref,))).fetchone()
        if not r:
            await safe_edit(query, "Reference not found.")
            return
        user = await (await db.execute("SELECT user_id, full_name, username, code FROM users WHERE user_id = ?", (r['user_id'],))).fetchone()
        bal = await (await db.execute("SELECT balance FROM wallets WHERE user_id = ?", (r['user_id'],))).fetchone()
        sims = await (await db.execute("SELECT number, status, in_use, otp_count FROM sim_numbers WHERE owner_id = ?", (r['user_id'],))).fetchall()

    lines = [
        f"Ref: {r['ref']}",
        f"Amount: ₹{r['amount']}",
        f"Status: {r['status']}",
        f"Created: {r['created_at']}",
    ]
    if r['reviewed_at']:
        lines.append(f"Reviewed at: {r['reviewed_at']} by {r['reviewed_by'] or ''}")
    lines.append("")
    lines.append("User info:")
    lines.append(f"Name: {user['full_name'] or ''} (@{user['username'] or ''})")
    lines.append(f"Code: {user['code']}")
    lines.append(f"Balance: ₹{bal['balance'] if bal else 0}")
    lines.append("")
    lines.append("SIM numbers:")
    if sims:
        for s in sims:
            lines.append(f"{s['number']} · {s['status']} · {'busy' if s['in_use'] else 'available'} · OTPs: {s['otp_count']}")
    else:
        lines.append("No SIM numbers registered.")

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Approve", callback_data=f"admin:approve:{r['ref']}"), InlineKeyboardButton("❌ Reject", callback_data=f"admin:reject:{r['ref']}")],
        [InlineKeyboardButton("💳 Wallet history", callback_data=f"admin:wallet_hist:{r['user_id']}:{r['ref']}:{page}"), InlineKeyboardButton("🔁 User recharges", callback_data=f"admin:user_recharges:{r['user_id']}:{r['ref']}:{page}")],
        [InlineKeyboardButton("◀️ Back", callback_data=f"admin:pending:{page}")],
    ])
    await safe_edit(query, build_panel("Recharge Details", lines, "Use the buttons below to review history or approve/reject."), reply_markup=kb)


async def admin_wallet_history_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    parts = query.data.split(":")
    # expected: admin:wallet_hist:<user_id>:<ref>:<page>
    if len(parts) < 4:
        return
    user_id = parts[2]
    ref = parts[3]
    page = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 0
    async with db_connect() as db:
        total = (await (await db.execute("SELECT COUNT(*) AS c FROM wallet_ledger WHERE user_id = ?", (user_id,))).fetchone())['c']
        rows = await (await db.execute("SELECT amount, kind, note, created_at FROM wallet_ledger WHERE user_id = ? ORDER BY ledger_id DESC LIMIT ? OFFSET ?", (user_id, PAGE_SIZE, page * PAGE_SIZE))).fetchall()
        bal = await (await db.execute("SELECT balance FROM wallets WHERE user_id = ?", (user_id,))).fetchone()
        user = await (await db.execute("SELECT full_name, username, code FROM users WHERE user_id = ?", (user_id,))).fetchone()
    lines = [f"User: {user['full_name'] or ''} ({user['code']})", f"Current balance: ₹{bal['balance'] if bal else 0}", ""]
    if not rows:
        lines.append("No ledger entries.")
    else:
        for r in rows:
            sign = "+" if r['amount'] > 0 else ""
            lines.append(f"{sign}{r['amount']} {r['kind']} · {r['note'] or ''} · {r['created_at']}")
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"admin:wallet_hist:{user_id}:{ref}:{page-1}"))
    if (page + 1) * PAGE_SIZE < total:
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"admin:wallet_hist:{user_id}:{ref}:{page+1}"))
    kb_rows = [nav] if nav else []
    kb_rows.append([InlineKeyboardButton("◀️ Back", callback_data=f"admin:details:{ref}:{page}")])
    kb = InlineKeyboardMarkup(kb_rows)
    await safe_edit(query, build_panel("Wallet History", lines, None), reply_markup=kb)


async def admin_user_recharges_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    parts = query.data.split(":")
    # expected: admin:user_recharges:<user_id>:<ref>:<page>
    if len(parts) < 4:
        return
    user_id = parts[2]
    ref = parts[3]
    page = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 0
    async with db_connect() as db:
        total = (await (await db.execute("SELECT COUNT(*) AS c FROM recharges WHERE user_id = ?", (user_id,))).fetchone())['c']
        rows = await (await db.execute("SELECT ref, amount, status, created_at, reviewed_at FROM recharges WHERE user_id = ? ORDER BY created_at DESC LIMIT ? OFFSET ?", (user_id, PAGE_SIZE, page * PAGE_SIZE))).fetchall()
        bal = await (await db.execute("SELECT balance FROM wallets WHERE user_id = ?", (user_id,))).fetchone()
        user = await (await db.execute("SELECT full_name, username, code FROM users WHERE user_id = ?", (user_id,))).fetchone()
    lines = [f"User: {user['full_name'] or ''} ({user['code']})", f"Balance: ₹{bal['balance'] if bal else 0}", ""]
    if not rows:
        lines.append("No recharges found.")
    else:
        for r in rows:
            extra = f" · reviewed at {r['reviewed_at']}" if r['reviewed_at'] else ""
            lines.append(f"{r['ref']} · ₹{r['amount']} · {r['status']} · {r['created_at']}{extra}")
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"admin:user_recharges:{user_id}:{ref}:{page-1}"))
    if (page + 1) * PAGE_SIZE < total:
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"admin:user_recharges:{user_id}:{ref}:{page+1}"))
    kb_rows = [nav] if nav else []
    kb_rows.append([InlineKeyboardButton("◀️ Back", callback_data=f"admin:details:{ref}:{page}")])
    kb = InlineKeyboardMarkup(kb_rows)
    await safe_edit(query, build_panel("User Recharges", lines, None), reply_markup=kb)


async def admin_recharges_today_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    parts = query.data.split(":")
    page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
    start_today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    async with db_connect() as db:
        row = await (await db.execute("SELECT COUNT(*) AS c, COALESCE(SUM(amount),0) AS s FROM recharges WHERE created_at >= ?", (start_today,))).fetchone()
        recent = await (await db.execute("SELECT r.ref, r.user_id, r.amount, r.status, r.created_at, u.code FROM recharges r LEFT JOIN users u ON r.user_id = u.user_id WHERE r.created_at >= ? ORDER BY r.created_at DESC LIMIT ? OFFSET ?", (start_today, PAGE_SIZE, page * PAGE_SIZE))).fetchall()
    lines = [f"Count: {row['c']}", f"Total amount: ₹{row['s']}", "", "Recent:"]
    if not recent:
        lines.append("No recharges today.")
    else:
        for r in recent:
            lines.append(f"{r['ref']} · ₹{r['amount']} · {r['status']} · user {r['user_id']} ({r['code'] or ''}) · {r['created_at']}")
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"admin:recharges:today:{page-1}"))
    if (page + 1) * PAGE_SIZE < row['c']:
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"admin:recharges:today:{page+1}"))
    kb_rows = [nav] if nav else []
    kb_rows.append([InlineKeyboardButton("◀️ Back", callback_data="admin:dashboard")])
    kb = InlineKeyboardMarkup(kb_rows)
    await safe_edit(query, build_panel("Today's Recharges", lines, None), reply_markup=kb)


async def help_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Help main menu and topic pages."""
    query = update.callback_query
    data = (query.data or "help:view")
    parts = data.split(":")
    topic = parts[1] if len(parts) > 1 else 'view'

    if topic == 'view':
        text = build_panel(
            "Help Topics",
            [
                "Wallet & Recharge - how to top up and submit UTRs.",
                "Submitting UTR - format, validation, and what admins check.",
                "OTP & SIM - how to provide/receive OTPs and register SIMs.",
                "Navigation & Recovery - how to get unstuck and use Home/Back/Help.",
            ],
            "Choose a topic for step-by-step instructions."
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("💳 Wallet & Recharge", callback_data="help:wallet")],
            [InlineKeyboardButton("✉️ Submitting UTR", callback_data="help:utr")],
            [InlineKeyboardButton("🔐 OTP & SIM", callback_data="help:otp")],
            [InlineKeyboardButton("🧭 Navigation & Recovery", callback_data="help:navigation")],
            [InlineKeyboardButton("🆘 Contact support", url=SUPPORT_URL)],
            [InlineKeyboardButton("◀️ Back", callback_data="menu:home")],
        ])
        await safe_edit(query, text, reply_markup=kb)
        return

    if topic == 'wallet':
        text = build_panel(
            "Wallet & Recharge",
            [
                "1) Choose Recharge, then pick a preset amount or press Custom.",
                "2) Pay the exact amount to the UPI ID shown in the bot.",
                "3) Tap I Have Paid and send the UPI transaction ID / UTR from your receipt.",
                "4) Your request stays Pending until an admin approves it.",
            ],
            "You do not need to send your user code."
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("How to pay (UPI apps)", callback_data="help:payment")],
            [InlineKeyboardButton("◀️ Back", callback_data="help:view"), InlineKeyboardButton("🏠 Home", callback_data="menu:home")],
        ])
        await safe_edit(query, text, reply_markup=kb)
        return

    if topic == 'wallet_old':
        text = build_panel(
            "Wallet & Recharge",
            [
                "1) Choose Recharge → pick a preset amount or press Custom to enter any amount.",
                "2) Pay using your UPI app to the UPI ID shown. You may scan the QR code if provided.",
                "3) After payment, press 'I Have Paid' and send your UTR/Reference (10-25 digits).",
                "4) The request becomes 'Pending' until an admin reviews and approves it.",
            ],
            "Buttons: I Have Paid → Send UTR | Back | Home"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("How to pay (UPI apps)", callback_data="help:payment")],
            [InlineKeyboardButton("◀️ Back", callback_data="help:view"), InlineKeyboardButton("🏠 Home", callback_data="menu:home")],
        ])
        await safe_edit(query, text, reply_markup=kb)
        return

    if topic == 'payment':
        text = build_panel(
            "Paying via UPI",
            [
                "Open your UPI app (GPay, PhonePe, Paytm, BHIM etc.) and choose 'Send' or 'UPI' option.",
                "Enter the UPI ID shown in the bot (or scan the QR). Complete the payment.",
                "After successful payment, copy the UTR / Reference number from your payment receipt and return to the bot.",
            ],
            None
        )
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Back", callback_data="help:wallet"), InlineKeyboardButton("🏠 Home", callback_data="menu:home")]])
        await safe_edit(query, text, reply_markup=kb)
        return

    if topic == 'utr':
        text = build_panel(
            "Submitting Transaction ID / UTR",
            [
                "A UTR is the transaction ID from your UPI payment receipt.",
                "After paying, tap I Have Paid and send only that ID.",
                "Example: 425783920184",
                "If it was already submitted, the bot will show its current status.",
            ],
            None
        )
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Back", callback_data="help:wallet"), InlineKeyboardButton("🏠 Home", callback_data="menu:home")]])
        await safe_edit(query, text, reply_markup=kb)
        return

    if topic == 'utr_old':
        text = build_panel(
            "Submitting UTR / Reference",
            [
                "A UTR is the transaction reference from your UPI app. It should be 10-25 digits.",
                "After paying: press 'I Have Paid' then send only the UTR (e.g. 425783920184).",
                "Do not add any extra text — send the exact UTR so admins can verify the payment quickly.",
                "If your UTR was already submitted, the bot will tell you its status (Pending / Approved).",
            ],
            None
        )
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Back", callback_data="help:wallet"), InlineKeyboardButton("🏠 Home", callback_data="menu:home")]])
        await safe_edit(query, text, reply_markup=kb)
        return

    if topic == 'otp':
        text = build_panel(
            "OTP & SIM Guide",
            [
                "Requesting OTP: choose a number and pay the OTP price. Wait for the provider to reply with the code.",
                "Providing SIM: register your number via Add SIM. When you reply to a request with the OTP, the taker receives it.",
                "Payouts: approved OTPs are paid into your wallet; monitor your balance in Wallet → Wallet History.",
            ],
            None
        )
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Back", callback_data="help:view"), InlineKeyboardButton("🏠 Home", callback_data="menu:home")]])
        await safe_edit(query, text, reply_markup=kb)
        return

    if topic == 'navigation':
        text = build_panel(
            "Navigation & Recovery",
            [
                "Use '🏠 Home' to return to the main menu from anywhere.",
                "Use '◀️ Back' to step back one screen when available.",
                "If you see 'You're in the middle of an action' use Continue, Back, or Help to recover.",
                "Never type /start to recover — use the provided buttons for a consistent state.",
            ],
            None
        )
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Back", callback_data="help:view"), InlineKeyboardButton("🏠 Home", callback_data="menu:home")]])
        await safe_edit(query, text, reply_markup=kb)
        return

    # fallback
    await safe_edit(query, build_panel("Help", ["Select a topic from the Help menu."], None), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Back", callback_data="help:view"), InlineKeyboardButton("🏠 Home", callback_data="menu:home")]]))


async def unknown_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle unexpected text gracefully."""
    user = update.effective_user
    await ensure_user(user)
    uid = str(user.id)
    action = await get_user_action(uid)
    if action:
        # Give contextual recovery options
        title = "You're in the middle of an action"
        lines = [f"Current task: {action}", "Choose how you'd like to continue."]
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("Continue", callback_data="menu:home")],
            [InlineKeyboardButton("◀️ Back", callback_data="menu:home"), InlineKeyboardButton("🏠 Home", callback_data="menu:home")],
            [InlineKeyboardButton("❓ Help", callback_data="help:view")]
        ])
        await update.message.reply_text(build_panel(title, lines, None), reply_markup=kb)
        return

    text = build_panel(
        "I didn’t understand that",
        ["Use the buttons in the menu to navigate."],
        "Type /start any time to return to the main menu."
    )
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("🏠 Main Menu", callback_data="menu:home")],
        [InlineKeyboardButton("❓ Help", callback_data="help:view")]
    ]))


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle errors gracefully."""
    logger.exception("Unhandled error: %s", context.error)
    if update and hasattr(update, 'effective_chat'):
        try:
            await context.bot.send_message(
                update.effective_chat.id,
                build_panel("Something went wrong", ["Please try again.", "If the problem persists, contact support."], None),
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Home", callback_data="menu:home")]])
            )
        except Exception as e:
            logger.warning("Could not notify user about error: %s", e)


async def general_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    uid = str(user.id)
    text = (update.message.text or "").strip()
    action = await get_user_action(uid)
    legacy_actions = {'submit_ref', 'submit_utr:0'}
    if action in legacy_actions or (action and (action.startswith('submit_ref_amount:') or action.startswith('submit_utr:'))):
        await clear_user_action(uid)
        await update.message.reply_text(
            build_panel(
                "Recharge Flow Updated",
                ["That old payment step has been retired.", "Start a fresh recharge and send only the UPI transaction ID / UTR after payment."],
                None
            ),
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💳 Recharge", callback_data="wallet:recharge")], [InlineKeyboardButton("🏠 Home", callback_data="menu:home")]])
        )
        return
    if action == 'admin_search':
        if not is_admin(user.id):
            await clear_user_action(uid)
            await update.message.reply_text("Unauthorized.")
            return
        term = text.strip().lstrip("@")
        like = f"%{term}%"
        async with db_connect() as db:
            recharge = await (await db.execute(
                "SELECT ref, user_id, amount, status, created_at FROM recharges WHERE ref = ? COLLATE NOCASE LIMIT 1",
                (term,)
            )).fetchone()
            users = await (await db.execute(
                "SELECT u.user_id, u.username, u.full_name, u.code, COALESCE(w.balance,0) AS balance FROM users u LEFT JOIN wallets w ON w.user_id = u.user_id WHERE u.user_id = ? OR u.code = ? OR u.username LIKE ? COLLATE NOCASE OR u.full_name LIKE ? COLLATE NOCASE LIMIT 5",
                (term, term, like, like)
            )).fetchall()
        lines = [f"Query: {term}"]
        if recharge:
            lines.extend(["", "Recharge:", f"{recharge['ref']} · ₹{recharge['amount']} · {recharge['status']} · user {recharge['user_id']}"])
        if users:
            lines.extend(["", "Users:"])
            for row in users:
                username = f"@{row['username']}" if row['username'] else "no username"
                lines.append(f"{row['full_name']} · {username} · {row['code']} · {row['user_id']} · ₹{row['balance']}")
        if len(lines) == 1:
            lines.append("No matches found.")
        await clear_user_action(uid)
        kb_rows = []
        if recharge:
            kb_rows.append([InlineKeyboardButton("Recharge details", callback_data=f"admin:details:{recharge['ref']}:0")])
        kb_rows.append([InlineKeyboardButton("Search again", callback_data="admin:search"), InlineKeyboardButton("Dashboard", callback_data="admin:dashboard")])
        await update.message.reply_text(build_panel("Search Results", lines, None), reply_markup=InlineKeyboardMarkup(kb_rows))
        return

    if action == 'enter_recharge_amount':
        try:
            amount = int(text)
        except ValueError:
            await set_user_action(uid, 'enter_recharge_amount', ttl_minutes=30)
            await update.message.reply_text(
                "Invalid amount. Send a number like 500.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Back", callback_data="wallet:recharge")], [InlineKeyboardButton("🏠 Home", callback_data="menu:home")]])
            )
            return
        if amount <= 0:
            await set_user_action(uid, 'enter_recharge_amount', ttl_minutes=30)
            await update.message.reply_text(
                "Amount must be positive.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Back", callback_data="wallet:recharge")], [InlineKeyboardButton("🏠 Home", callback_data="menu:home")]])
            )
            return
        await set_user_action(uid, "recharge_amount", str(amount), ttl_minutes=30)
        await send_payment_instructions(update.message, amount)
        return

    if action and action.startswith('submit_payment_id:'):
        try:
            amount = int(action.split(':', 1)[1])
        except ValueError:
            await clear_user_action(uid)
            await update.message.reply_text("Recharge session expired. Please start again.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Recharge", callback_data="wallet:recharge")]]))
            return

        payment_id = normalize_payment_id(text)
        if not is_valid_payment_id(payment_id):
            await set_user_action(uid, 'submit_payment_id', str(amount), ttl_minutes=30)
            await update.message.reply_text(
                build_panel(
                    "Invalid Transaction ID",
                    [
                        "Send only the UPI transaction ID / UTR from your payment receipt.",
                        "It should be 6-40 letters or digits. Spaces are okay; I will remove them.",
                        "Example: 425783920184",
                    ],
                    None
                ),
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Try Again", callback_data="wallet:ihavepaid")], [InlineKeyboardButton("🏠 Home", callback_data="menu:home")]])
            )
            return

        if not check_rate_limit(f"recharge:{uid}", limit=3, per_seconds=3600):
            await update.message.reply_text("You are creating recharges too quickly. Try again later.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Home", callback_data="menu:home")]]))
            return

        async with db_connect() as db:
            existing = await (await db.execute("SELECT user_id, amount, status FROM recharges WHERE ref = ?", (payment_id,))).fetchone()
            if existing:
                await clear_user_action(uid)
                if existing['user_id'] == uid:
                    await update.message.reply_text(
                        build_panel(
                            "Already Submitted",
                            [f"Transaction ID: {payment_id}", f"Amount: ₹{existing['amount']}", f"Status: {existing['status'].title()}"],
                            "Admins will review it if it is still pending."
                        ),
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Home", callback_data="menu:home")], [InlineKeyboardButton("🆘 Support", url=SUPPORT_URL)]])
                    )
                    return
                await update.message.reply_text(
                    "This transaction ID was already submitted. Contact support if you think this is a mistake.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🆘 Support", url=SUPPORT_URL)], [InlineKeyboardButton("🏠 Home", callback_data="menu:home")]])
                )
                return

            await db.execute("INSERT INTO recharges (ref, user_id, amount, status, created_at) VALUES (?, ?, ?, 'pending', ?)", (payment_id, uid, amount, now_utc()))
            user_row = await (await db.execute("SELECT user_id, username, code, full_name FROM users WHERE user_id = ?", (uid,))).fetchone()
            await db.commit()

        await clear_user_action(uid)
        await notify_pending_recharge(context, payment_id, amount, user_row)
        await update.message.reply_text(
            build_panel(
                "Recharge Submitted",
                [f"Transaction ID: {payment_id}", f"Amount: ₹{amount}", "Status: Pending admin review"],
                "Your wallet will be credited after approval."
            ),
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Main Menu", callback_data="menu:home")], [InlineKeyboardButton("Submit Another", callback_data="wallet:recharge")]])
        )
        return

    # --- Handle enter recharge amount flow ---
    if action == 'enter_recharge_amount':
        # parse amount
        try:
            amount = int(text)
        except Exception:
            _awaiting_actions[uid] = 'enter_recharge_amount'
            await update.message.reply_text("❌ Invalid amount. Please send a number like: 500", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Back", callback_data="menu:home")],[InlineKeyboardButton("🏠 Home", callback_data="menu:home")]]))
            return
        if amount <= 0:
            _awaiting_actions[uid] = 'enter_recharge_amount'
            await update.message.reply_text("❌ Amount must be positive.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Back", callback_data="menu:home")],[InlineKeyboardButton("🏠 Home", callback_data="menu:home")]]))
            return

        # show payment details
        upi = await get_setting('upi_id') or UPI_ID
        qr = await get_setting('qr_file_id')
        _awaiting_actions[uid] = f'submit_utr:{amount}'
        lines = [f"Recharge Amount: ₹{amount}", "", "Pay using:", f"UPI ID:\n{upi}"]
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("I Have Paid", callback_data="wallet:ihavepaid")],
            [InlineKeyboardButton("◀️ Back", callback_data="wallet:cancel_recharge"), InlineKeyboardButton("🏠 Home", callback_data="menu:home")],
        ])
        if qr:
            # send qr image then message
            try:
                await update.message.reply_photo(qr, caption=build_panel("Payment Instructions", lines, None), reply_markup=kb)
            except Exception:
                await update.message.reply_text(build_panel("Payment Instructions", lines, None), reply_markup=kb)
        else:
            await update.message.reply_text(build_panel("Payment Instructions", lines, None), reply_markup=kb)
        return

    if action and action.startswith('submit_utr:'):
        # expecting only UTR digits
        try:
            amount = int(action.split(':',1)[1])
        except Exception:
            amount = 0
        utr = re.sub(r"\s+","", text).strip()
        if not re.fullmatch(r"\d{10,25}", utr):
            _awaiting_actions[uid] = action
            await update.message.reply_text("❌ Invalid UTR. A UTR should be 10-25 digits. Please resend.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Try Again", callback_data="wallet:ihavepaid")],[InlineKeyboardButton("◀️ Back", callback_data="menu:home")]]))
            return

        # prevent duplicate UTRs
        async with db_connect() as db:
            existing = await (await db.execute("SELECT ref, status FROM recharges WHERE ref = ?", (utr,))).fetchone()
            if existing:
                await update.message.reply_text("This UTR was already submitted. If you believe this is an error contact support.")
                return
            # cooldown per-user
            if not check_rate_limit(f"recharge:{uid}", limit=3, per_seconds=3600):
                await update.message.reply_text("You're creating recharges too quickly. Try later.")
                return
            # insert pending recharge (store utr in ref column)
            await db.execute("INSERT INTO recharges (ref, user_id, amount, status, created_at) VALUES (?, ?, ?, 'pending', ?)", (utr, uid, amount, now_utc()))
            user_row = await (await db.execute("SELECT code, full_name, username FROM users WHERE user_id = ?", (uid,))).fetchone()
            await db.commit()
        # clear awaiting action now that request recorded
        _awaiting_actions.pop(uid, None)
        await notify_pending_recharge(context, utr, amount, user_row)
        await update.message.reply_text(build_panel("✅ Request Submitted", [f"Request ID: {utr}", f"Amount: ₹{amount}", "Status: Pending Review"], "What would you like to do next?"), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Main Menu", callback_data="menu:home")],[InlineKeyboardButton("View Status", callback_data=f"admin:details:{utr}")],[InlineKeyboardButton("Submit Another", callback_data="wallet:recharge")]]))
        return

    # admin rejecting with reason flow handled via awaiting action
    if action and action.startswith('admin_reject_reason:'):
        parts = action.split(':',1)
        if len(parts) < 2:
            await update.message.reply_text("Invalid request.")
            return
        ref = parts[1]
        reason = text.strip()[:400]
        if reason.lower() == 'cancel':
            await clear_user_action(uid)
            await update.message.reply_text("Rejection cancelled.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Home", callback_data="menu:home")]]))
            return
        if not reason:
            await set_user_action(uid, 'admin_reject_reason', ref, ttl_minutes=30)
            await update.message.reply_text("Please enter a rejection reason.")
            return
        admin_id = str(user.id)
        async with db_connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await (await db.execute("SELECT user_id, status FROM recharges WHERE ref = ?", (ref,))).fetchone()
            if not row:
                await db.rollback()
                await update.message.reply_text("Reference not found.")
                return
            if row['status'] != 'pending':
                await db.rollback()
                await update.message.reply_text(f"Reference already {row['status']}. Cannot reject.")
                return
            await db.execute("UPDATE recharges SET status='rejected', reviewed_by=?, reviewed_at=? WHERE ref=?", (admin_id, now_utc(), ref))
            await db.execute("INSERT INTO admin_actions (admin_id, action_type, ref, user_id, note, created_at) VALUES (?, ?, ?, ?, ?, ?)", (admin_id, 'reject', ref, row['user_id'], reason, now_utc()))
            await db.commit()
        # notify user with reason
        try:
            await safe_send(context.bot, int(row['user_id']), f"❌ Recharge Rejected\n\nReason:\n{reason}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🆘 Support", url=SUPPORT_URL)]]))
        except Exception:
            logger.warning("Failed to notify user %s", row['user_id'])
        await broadcast_admins(context, f"❌ Recharge rejected\nRef: `{md(ref)}`\nUser ID: {md(row['user_id'])}\nRejected by: {md(admin_id)}\nReason: {md(reason)}")
        await clear_user_action(uid)
        await update.message.reply_text("Rejection recorded and user notified.")
        return
    if action and action.startswith('submit_ref_amount:'):
        # user previously selected amount via buttons; expect only the reference now
        amount = int(action.split(':', 1)[1])
        parts = text.split()
        if len(parts) != 1:
            _awaiting_actions[uid] = action
            await update.message.reply_text(build_panel("How to send reference", ["Please send only the UTR / UPI reference you used for payment.", "Example: 425783920184"], None), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Try Again", callback_data="wallet:submit_ref")],[InlineKeyboardButton("◀️ Back", callback_data="menu:home"), InlineKeyboardButton("🏠 Home", callback_data="menu:home")]]))
            return
        ref = parts[0].strip().upper()
        if not re.fullmatch(r"[A-Z0-9._-]{4,40}", ref):
            _awaiting_actions[uid] = action
            await update.message.reply_text(build_panel("❌ Invalid reference", ["Reference format invalid. Use 4-40 chars (A-Z,0-9,._-)."], None), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Try Again", callback_data="wallet:submit_ref")],[InlineKeyboardButton("◀️ Back", callback_data="menu:home")]]))
            return

        # rate-limit refs per user: max 5 per hour
        if not check_rate_limit(f"ref:{update.effective_user.id}", limit=5, per_seconds=3600):
            _awaiting_actions[uid] = action
            await update.message.reply_text(build_panel("⚠️ Too many submissions", ["You're submitting references too quickly. Try again later."], None), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Home", callback_data="menu:home")]]))
            return

        uid = str(update.effective_user.id)
        async with db_connect() as db:
            existing = await (await db.execute("SELECT ref, user_id, status, amount FROM recharges WHERE ref = ?", (ref,))).fetchone()
            if existing:
                if existing['status'] == 'draft' and existing['user_id'] == uid:
                    # promote user's draft to pending
                    await db.execute("UPDATE recharges SET status='pending' WHERE ref = ?", (ref,))
                    row = await (await db.execute("SELECT user_id, username, code, full_name FROM users WHERE user_id = ?", (uid,))).fetchone()
                    await db.commit()
                    await notify_pending_recharge(context, ref, amount, row)
                    await update.message.reply_text(f"Reference {ref} submitted for review.")
                    return
                if existing['status'] == 'approved':
                    await update.message.reply_text(f"This reference `{md(ref)}` was already approved. You cannot resubmit it.")
                    return
                if existing['status'] == 'pending':
                    await update.message.reply_text(f"This reference `{md(ref)}` is already pending review.")
                    return
                await update.message.reply_text("This reference is already submitted.")
                return

            # insert new pending recharge with user's provided ref
            await db.execute("INSERT INTO recharges (ref, user_id, amount, status, created_at) VALUES (?, ?, ?, 'pending', ?)", (ref, uid, amount, now_utc()))
            row = await (await db.execute("SELECT user_id, username, code, full_name FROM users WHERE user_id = ?", (uid,))).fetchone()
            await db.commit()

        await notify_pending_recharge(context, ref, amount, row)
        await update.message.reply_text(f"Reference {ref} submitted for review.")
        return

    if action == 'submit_ref':
        draft = None
        async with db_connect() as db:
            draft = await (await db.execute("SELECT ref, amount FROM recharges WHERE user_id = ? AND status = 'draft' ORDER BY created_at DESC LIMIT 1", (uid,))).fetchone()

        parts = text.split()
        if draft:
            if len(parts) == 1:
                ref = parts[0].strip().upper()
                amount = int(draft['amount'])
            elif len(parts) == 2:
                ref = parts[0].strip().upper()
                try:
                    amount = int(parts[1])
                except ValueError:
                    _awaiting_actions[uid] = 'submit_ref'
                    await update.message.reply_text(build_panel("❌ Invalid amount", ["Amount must be a number.", "Please resend the reference and amount like: REF1234 100"], None), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Try Again", callback_data="wallet:submit_ref")],[InlineKeyboardButton("◀️ Back", callback_data="menu:home"), InlineKeyboardButton("🏠 Home", callback_data="menu:home")]]))
                    return
                if amount != int(draft['amount']):
                    await update.message.reply_text(f"That reference was created for ₹{draft['amount']}. Please resend it with the same amount.")
                    return
                else:
                    _awaiting_actions[uid] = 'submit_ref'
                    await update.message.reply_text(build_panel("How to send reference", ["Send the reference in the format: REF1234", "If you created a draft earlier send only the reference."], None), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Try Again", callback_data="wallet:submit_ref")],[InlineKeyboardButton("◀️ Back", callback_data="menu:home")]]))
                    return
            if ref != str(draft['ref']).upper():
                _awaiting_actions[uid] = 'submit_ref'
                await update.message.reply_text(build_panel("❌ Reference mismatch", ["Please send the exact UTR / reference shown in the payment instructions.", "Example: 65406540651"], None), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Try Again", callback_data="wallet:submit_ref")],[InlineKeyboardButton("◀️ Back", callback_data="menu:home"), InlineKeyboardButton("🏠 Home", callback_data="menu:home")]]))
                return
        else:
            if len(parts) != 2:
                _awaiting_actions[uid] = 'submit_ref'
                await update.message.reply_text(build_panel("❌ Invalid format", ["Please send reference and amount like: REF1234 100"], None), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Try Again", callback_data="wallet:submit_ref")],[InlineKeyboardButton("◀️ Back", callback_data="menu:home")]]))
                return
            ref = parts[0].strip().upper()
            try:
                amount = int(parts[1])
            except ValueError:
                _awaiting_actions[uid] = 'submit_ref'
                await update.message.reply_text(build_panel("❌ Invalid amount", ["Amount must be a number."], None), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Try Again", callback_data="wallet:submit_ref")],[InlineKeyboardButton("◀️ Back", callback_data="menu:home")]]))
                return

        if not re.fullmatch(r"[A-Z0-9._-]{4,40}", ref):
            _awaiting_actions[uid] = 'submit_ref'
            await update.message.reply_text(build_panel("❌ Invalid reference", ["Reference should be 4-40 characters using A-Z, 0-9, .,_-"], None), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Try Again", callback_data="wallet:submit_ref")],[InlineKeyboardButton("◀️ Back", callback_data="menu:home")]]))
            return
        if amount <= 0:
            _awaiting_actions[uid] = 'submit_ref'
            await update.message.reply_text(build_panel("❌ Invalid amount", ["Amount must be positive."], None), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Try Again", callback_data="wallet:submit_ref")],[InlineKeyboardButton("◀️ Back", callback_data="menu:home")]]))
            return
        # rate-limit refs per user: max 5 per hour
        if not check_rate_limit(f"ref:{update.effective_user.id}", limit=5, per_seconds=3600):
            await update.message.reply_text("You're submitting references too quickly. Try later.")
            return
        async with db_connect() as db:
            existing = await (await db.execute("SELECT ref, status FROM recharges WHERE ref = ?", (ref,))).fetchone()
            if existing:
                if existing['status'] == 'draft':
                    if amount != int(existing['amount']):
                        _awaiting_actions[uid] = 'submit_ref'
                        await update.message.reply_text(build_panel("❌ Amount mismatch", [f"That reference was created for ₹{existing['amount']}. Please resend it with the same amount."], None), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Try Again", callback_data="wallet:submit_ref")],[InlineKeyboardButton("◀️ Back", callback_data="menu:home")]]))
                        return
                    await db.execute("UPDATE recharges SET status='pending', reviewed_by=NULL, reviewed_at=NULL WHERE ref = ?", (ref,))
                    row = await (await db.execute("SELECT user_id, username, code, full_name FROM users WHERE user_id = ?", (uid,))).fetchone()
                    await db.commit()
                    await update.message.reply_text(build_panel("✅ Submitted", [f"Reference {ref} submitted for review."], None), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Main Menu", callback_data="menu:home")],[InlineKeyboardButton("View Status", callback_data=f"admin:details:{ref}")]]))
                    await notify_pending_recharge(context, ref, amount, row)
                    return
                if existing['status'] == 'approved':
                    await update.message.reply_text(build_panel("⚠️ Already approved", [f"This reference {ref} was already approved. You cannot resubmit it."], None), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Main Menu", callback_data="menu:home")]]))
                    return
                if existing['status'] == 'pending':
                    await update.message.reply_text(build_panel("⚠️ Already pending", [f"This reference {ref} is already pending review."], None), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Main Menu", callback_data="menu:home")]]))
                    return
                await update.message.reply_text(build_panel("⚠️ Duplicate", ["This reference is already submitted."], None), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Main Menu", callback_data="menu:home")]]))
                return
            uid = str(update.effective_user.id)
            await db.execute("INSERT INTO recharges (ref, user_id, amount, status, created_at) VALUES (?, ?, ?, 'pending', ?)", (ref, uid, amount, now_utc()))
            row = await (await db.execute("SELECT user_id, username, code, full_name FROM users WHERE user_id = ?", (uid,))).fetchone()
            await db.commit()

        await notify_pending_recharge(context, ref, amount, row)

        await update.message.reply_text(f"Reference {ref} submitted for review.")
        return
    if update.message.reply_to_message:
        return
    if action == 'admin_addfund':
        # only admins can perform this action
        if not is_admin(update.effective_user.id):
            await update.message.reply_text("❌ Unauthorized.")
            return
        if text.lower().strip() == 'cancel':
            await update.message.reply_text("✖️ Cancelled.")
            return
        parts = text.split()
        if len(parts) < 2:
            await update.message.reply_text("❌ Please provide target and amount, e.g. 123456789 100 Optional note")
            _awaiting_actions[uid] = 'admin_addfund'
            return
        target = parts[0].strip()
        try:
            amount = int(parts[1])
        except ValueError:
            await update.message.reply_text("❌ Amount must be an integer.")
            _awaiting_actions[uid] = 'admin_addfund'
            return
        if amount == 0:
            await update.message.reply_text("❌ Amount cannot be zero.")
            _awaiting_actions[uid] = 'admin_addfund'
            return
        note = " ".join(parts[2:])[:200]
        # resolve user by id or code
        async with db_connect() as db:
            user_row = None
            if re.fullmatch(r"\d+", target):
                user_row = await (await db.execute("SELECT user_id, full_name FROM users WHERE user_id = ?", (target,))).fetchone()
            if not user_row:
                user_row = await (await db.execute("SELECT user_id, full_name FROM users WHERE code = ?", (target,))).fetchone()
            if not user_row:
                await update.message.reply_text("❌ Target user not found by id or code.")
                _awaiting_actions[uid] = 'admin_addfund'
                return
            # perform credit/debit
            await add_wallet_entry(db, user_row['user_id'], amount, 'admin_adjust', f"{note} (by admin {uid})")
            # log admin action
            await db.execute(
                "INSERT INTO admin_actions (admin_id, action_type, user_id, amount, note, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (uid, 'adjust', user_row['user_id'], amount, note, now_utc())
            )
            await db.commit()
        await update.message.reply_text(f"✅ Adjusted {user_row['full_name']}'s balance by ₹{amount}.")
        try:
            await safe_send(context.bot, int(user_row['user_id']), f"💰 Your wallet was adjusted by admin: ₹{amount}.\nNote: {note}")
        except Exception:
            logger.warning("Could not notify user about balance adjustment")
        return
    if action == 'addsim':
        # validate 10-digit
        if not re.fullmatch(r"\d{10}", text):
            await update.message.reply_text("❌ Invalid SIM number. Must be exactly 10 digits.")
            _awaiting_actions[uid] = 'addsim'
            return
        number = text
        async with db_connect() as db:
            existing = await (await db.execute("SELECT sim_id FROM sim_numbers WHERE number = ?", (number,))).fetchone()
            if existing:
                await update.message.reply_text("❌ This number is already registered.")
                return
            count = await (await db.execute("SELECT COUNT(*) AS c FROM sim_numbers WHERE owner_id = ? AND status = 'active'", (uid,))).fetchone()
            if count['c'] >= MAX_ACTIVE_SIMS:
                await update.message.reply_text(f"❌ You can have up to {MAX_ACTIVE_SIMS} active SIMs. Please delist one first.")
                return
            await db.execute("INSERT INTO sim_numbers (number, owner_id, created_at) VALUES (?, ?, ?)", (number, uid, now_utc()))
            await db.commit()
        await update.message.reply_text(
            f"✅ SIM registered successfully! You can now earn ₹{OTP_PAYOUT} per completed OTP.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📋 My SIMs", callback_data="sims:my")],
                [InlineKeyboardButton("🏠 Home", callback_data="menu:home")],
            ])
        )
        return

    # default fallback
    await unknown_text(update, context)


async def search_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return
    uid = str(update.effective_user.id)
    if context.args:
        term = " ".join(context.args).strip().lstrip("@")
        like = f"%{term}%"
        async with db_connect() as db:
            recharge = await (await db.execute("SELECT ref, user_id, amount, status, created_at FROM recharges WHERE ref = ? COLLATE NOCASE LIMIT 1", (term,))).fetchone()
            users = await (await db.execute(
                "SELECT u.user_id, u.username, u.full_name, u.code, COALESCE(w.balance,0) AS balance FROM users u LEFT JOIN wallets w ON w.user_id = u.user_id WHERE u.user_id = ? OR u.code = ? OR u.username LIKE ? COLLATE NOCASE OR u.full_name LIKE ? COLLATE NOCASE LIMIT 5",
                (term, term, like, like)
            )).fetchall()
        lines = [f"Query: {term}"]
        if recharge:
            lines.extend(["", "Recharge:", f"{recharge['ref']} · ₹{recharge['amount']} · {recharge['status']} · user {recharge['user_id']}"])
        if users:
            lines.extend(["", "Users:"])
            for row in users:
                username = f"@{row['username']}" if row['username'] else "no username"
                lines.append(f"{row['full_name']} · {username} · {row['code']} · {row['user_id']} · ₹{row['balance']}")
        if len(lines) == 1:
            lines.append("No matches found.")
        await update.message.reply_text(build_panel("Search Results", lines, None))
        return
    await set_user_action(uid, 'admin_search', ttl_minutes=15)
    await update.message.reply_text("Send a user ID, user code, username, or transaction ID / UTR.")


async def health_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return
    upi = await get_setting('upi_id') or UPI_ID
    qr = await get_setting('qr_file_id')
    async with db_connect() as db:
        pending = (await (await db.execute("SELECT COUNT(*) AS c FROM recharges WHERE status = 'pending'")).fetchone())['c']
        waiting = (await (await db.execute("SELECT COUNT(*) AS c FROM otp_requests WHERE status = 'waiting'")).fetchone())['c']
        actions = (await (await db.execute("SELECT COUNT(*) AS c FROM user_actions")).fetchone())['c']
    await update.message.reply_text(build_panel("Bot Health", [
        f"Bot token: {'set' if BOT_TOKEN else 'missing'}",
        f"Owner ID: {OWNER_ID or 'missing'}",
        f"UPI ID: {upi}",
        f"QR: {'configured' if qr else 'not configured'}",
        f"Database: {DB_FILE}",
        f"Pending recharges: {pending}",
        f"Waiting OTPs: {waiting}",
        f"Saved user actions: {actions}",
    ], None))


async def configure_commands(app: Application) -> None:
    # Keep global command list minimal so clients show inline buttons
    # instead of a long slash-command bar. Admin/owner commands are set
    # only in the owner's private chat scope.
    cmds = [
        BotCommand("start", "🏠 Open main menu"),
        BotCommand("profile", "👤 View profile"),
        BotCommand("getotp", "🔐 Get OTP"),
        BotCommand("recharge", "💳 Recharge wallet"),
        BotCommand("addsim", "➕ Add SIM"),
        BotCommand("mysims", "📋 My SIMs"),
        BotCommand("help", "❓ How to use"),
    ]
    await app.bot.set_my_commands(cmds)
    if OWNER_ID:
        admin_cmds = [
            BotCommand("start", "🏠 Open main menu"),
            BotCommand("wallet", "💰 View wallet"),
            BotCommand("approve", "✅ Approve recharge"),
            BotCommand("reject", "❌ Reject recharge"),
            BotCommand("addfunds", "➕ Add funds"),
            BotCommand("search", "Search users or recharges"),
            BotCommand("health", "Bot health"),
            BotCommand("announcement", "📢 Announcement"),
        ]
        await app.bot.set_my_commands(admin_cmds, scope=BotCommandScopeChat(chat_id=OWNER_ID))


async def post_init(app: Application) -> None:
    await init_db()
    await configure_commands(app)
    await recover_waiting_requests(app)


# ---------------------------------------------------------------------------
# BOT SETUP & ENTRYPOINT
# - `configure_commands`, `post_init`, `build_app`, `main`
# ---------------------------------------------------------------------------


def build_app() -> Application:
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN not set. Check .env file.")
    
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_error_handler(error_handler)

    # Command handlers
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("wallet", wallet_view))
    app.add_handler(CommandHandler("ref", submit_ref))
    app.add_handler(CommandHandler("addsim", addsim_cmd))
    app.add_handler(CommandHandler("mysims", mysims_cmd))
    app.add_handler(CommandHandler("profile", profile_cmd))
    app.add_handler(CommandHandler("getotp", getotp_cmd))
    app.add_handler(CommandHandler("recharge", recharge_cmd))
    app.add_handler(CommandHandler("approve", approve_cmd))
    app.add_handler(CommandHandler("reject", reject_cmd))
    app.add_handler(CommandHandler("addfunds", addfunds_cmd))
    app.add_handler(CommandHandler("search", search_cmd))
    app.add_handler(CommandHandler("health", health_cmd))
    app.add_handler(CommandHandler("announcement", announcement_cmd))
    app.add_handler(CommandHandler("setupi", setupi_cmd))
    app.add_handler(CommandHandler("setqr", setqr_cmd))
    app.add_handler(CommandHandler("paymentinfo", paymentinfo_cmd))
    app.add_handler(CommandHandler("pending", pending_cmd))

    # Callback handlers (menu & navigation)
    app.add_handler(CallbackQueryHandler(menu_handler, pattern=r"^menu:|^help:"))
    app.add_handler(CallbackQueryHandler(wallet_callback, pattern=r"^wallet:"))
    app.add_handler(CallbackQueryHandler(sims_callback, pattern=r"^sims:"))
    
    # Admin dashboard & pagination handlers
    app.add_handler(CallbackQueryHandler(admin_dashboard_callback, pattern=r"^admin:dashboard$"))
    app.add_handler(CallbackQueryHandler(admin_search_callback, pattern=r"^admin:search$"))
    app.add_handler(CallbackQueryHandler(admin_health_callback, pattern=r"^admin:health$"))
    app.add_handler(CallbackQueryHandler(admin_pending_callback, pattern=r"^admin:pending"))
    app.add_handler(CallbackQueryHandler(admin_addfunds_callback, pattern=r"^admin:addfunds$"))
    app.add_handler(CallbackQueryHandler(admin_approve_callback, pattern=r"^admin:approve:"))
    app.add_handler(CallbackQueryHandler(admin_reject_callback, pattern=r"^admin:reject:"))
    app.add_handler(CallbackQueryHandler(admin_details_callback, pattern=r"^admin:details:"))
    app.add_handler(CallbackQueryHandler(admin_wallet_history_callback, pattern=r"^admin:wallet_hist:"))
    app.add_handler(CallbackQueryHandler(admin_user_recharges_callback, pattern=r"^admin:user_recharges:"))
    app.add_handler(CallbackQueryHandler(admin_recharges_today_callback, pattern=r"^admin:recharges:"))
    
    # OTP flow handlers
    app.add_handler(CallbackQueryHandler(list_otp_sims, pattern=r"^otp:list$"))
    app.add_handler(CallbackQueryHandler(choose_otp_sim, pattern=r"^otp:choose:\d+$"))
    app.add_handler(CallbackQueryHandler(otp_feedback, pattern=r"^otp:(accept|problem):"))
    
    # OTP reply handler (for giver responding with OTP)
    app.add_handler(MessageHandler(filters.REPLY, otp_reply))

    # Message handlers (for awaiting text input)
    app.add_handler(MessageHandler(filters.PHOTO & ~filters.COMMAND, photo_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & ~filters.REPLY, general_text_handler))
    
    return app


def main() -> None:
    app = build_app()
    # Stylish and clear RAJU banner
    raju_banner = r'''
╔══════════════════════════════════════════════════════════════╗
║                                                              ║
║                      ██████╗  █████╗ ██╗   ██╗               ║
║                      ██╔══██╗██╔══██╗██║   ██║               ║
║                      ██████╔╝███████║██║   ██║               ║
║                      ██╔══██╗██╔══██║██║   ██║               ║
║                      ██║  ██║██║  ██║╚██████╔╝               ║
║                      ╚═╝  ╚═╝╚═╝  ╚═╝ ╚═════╝                ║
║                                                              ║
║                    🚀 OTP BOT STARTING RAJU YOU DID IT 🚀                   ║
║                                                              ║
╚══════════════════════════════════════════════════════════════╝
'''
    logger.error("✨ RAJU OTP Bot startinS...")
    print(raju_banner)
    logger.error("✅ OTP Bot is running")
    app.run_polling()


if __name__ == "__main__":
    main()
