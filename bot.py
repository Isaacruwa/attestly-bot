"""
Attestly Telegram Bot
---------------------
Commands:
  /start      - welcome + overview
  /riskcheck  - free EU AI Act risk classifier (lead magnet)
  /status     - shows saved risk result + free doc-generations remaining
  /generate   - upload a trace JSON file, get a drafted Annex IV paragraph as .docx
  /upgrade    - link to attestly.online/pricing once free limit is hit
  /help       - list commands

Storage: local SQLite (attestly_bot.db) - one row per Telegram user.
"""

import os
import io
import re
import time
import json
import hashlib
import logging
import sqlite3
from collections import defaultdict, deque
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse

import requests
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ChatPermissions,
    LabeledPrice,
    BotCommand,
    BotCommandScopeDefault,
    BotCommandScopeAllChatAdministrators,
    BotCommandScopeChat,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ChatMemberHandler,
    ContextTypes,
    ConversationHandler,
    PreCheckoutQueryHandler,
    filters,
)

from docx import Document
from faq_data import TOPICS

try:
    from anthropic import Anthropic
except ImportError:
    Anthropic = None

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")  # only needed for /generate
DB_PATH = os.environ.get("ATTESTLY_BOT_DB", "attestly_bot.db")
PRICING_URL = "https://www.attestly.online/pricing"
LOGIN_URL = "https://www.attestly.online/login"
FREE_GENERATIONS = 3  # free docx drafts per user before we point them to pricing

# --- bot owner config (for /adminpanel and /grantpro, not tied to any one group) ---
# Defaults to the three Telegram accounts confirmed as the channel's admins/creator.
BOT_OWNER_IDS = set(
    int(x) for x in os.environ.get(
        "BOT_OWNER_IDS", "8440306556,6444902164,1804638516"
    ).split(",") if x.strip()
)

# --- generation-pack payment config ---
# Telegram Stars (XTR) need no provider setup and work everywhere instantly.
GENERATION_PACK_SIZE = int(os.environ.get("GENERATION_PACK_SIZE", "10"))
GENERATION_PACK_STARS = int(os.environ.get("GENERATION_PACK_STARS", "150"))  # ~$2-3 equivalent
# Ammer Pay (fiat, USD): set AMMER_PAY_TOKEN in Railway env vars, never commit it to the repo.
AMMER_PAY_TOKEN = os.environ.get("AMMER_PAY_TOKEN")
AMMER_PAY_CURRENCY = "USD"
GENERATION_PACK_USD_CENTS = int(os.environ.get("GENERATION_PACK_USD_CENTS", "299"))  # $2.99

# --- moderation config ---
ALLOWED_LINK_DOMAINS = ("attestly.online",)  # links to these domains are never removed
MAX_LINKS_PER_MESSAGE = 2       # more than this from a non-admin is treated as spam
DUPLICATE_WINDOW_SECONDS = 30   # same user repeating identical text within this window = spam
FAQ_COOLDOWN_SECONDS = 20       # per-chat cooldown between two keyword-triggered FAQ replies
WARNING_AUTODELETE_SECONDS = 12 # how long moderation warning messages stay visible

# --- channel announcement config ---
# The channel the bot posts updates to. The bot must be an admin of this channel
# with "Post Messages" permission for either feature below to work.
ANNOUNCE_CHANNEL_ID = os.environ.get("ANNOUNCE_CHANNEL_ID", "@AI_Act_Compliance")
# Comma-separated URLs to watch for content changes (e.g. a future attestly.online/blog
# or /changelog page). Empty by default: attestly.online has no blog/changelog yet, so
# there's nothing meaningful to watch until one exists. Set via env var once it does.
WATCH_URLS = [u.strip() for u in os.environ.get("WATCH_URLS", "").split(",") if u.strip()]
WATCH_INTERVAL_HOURS = float(os.environ.get("WATCH_INTERVAL_HOURS", "6"))

# --- chat cleanliness config ---
WELCOME_AUTODELETE_SECONDS = 180  # 3 minutes
COMMAND_AUTODELETE_SECONDS = 10   # how long admin command messages + confirmations stay visible
PURGE_MAX_MESSAGES = 200          # safety cap per /purge run

# in-memory moderation state (per-process; resets on restart, which is fine for this scale)
_last_message_by_user = {}      # (chat_id, user_id) -> (text, timestamp)
_last_faq_reply_at = defaultdict(float)  # chat_id -> timestamp

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
log = logging.getLogger("attestly-bot")

# Conversation states for /riskcheck
Q_PROHIBITED, Q_HIGH_RISK, Q_GPAI, Q_TRANSPARENCY = range(4)

# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY,
            username TEXT,
            risk_result TEXT,
            risk_checked_at TEXT,
            generations_used INTEGER DEFAULT 0,
            purchased_generations INTEGER DEFAULT 0,
            subscription_until TEXT,
            created_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS site_watch (
            url TEXT PRIMARY KEY,
            content_hash TEXT,
            last_checked TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS chat_settings (
            chat_id INTEGER PRIMARY KEY,
            clean_service INTEGER DEFAULT 1
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER,
            username TEXT,
            kind TEXT,
            currency TEXT,
            amount INTEGER,
            created_at TEXT
        )
        """
    )
    return conn


def get_clean_service(chat_id: int) -> bool:
    conn = db()
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT clean_service FROM chat_settings WHERE chat_id=?", (chat_id,)
    ).fetchone()
    conn.close()
    return bool(row["clean_service"]) if row else True  # on by default


def set_clean_service(chat_id: int, enabled: bool):
    conn = db()
    conn.execute(
        """
        INSERT INTO chat_settings (chat_id, clean_service) VALUES (?, ?)
        ON CONFLICT(chat_id) DO UPDATE SET clean_service=excluded.clean_service
        """,
        (chat_id, int(enabled)),
    )
    conn.commit()
    conn.close()


def upsert_user(telegram_id: int, username: str | None):
    conn = db()
    conn.execute(
        """
        INSERT INTO users (telegram_id, username, generations_used, created_at)
        VALUES (?, ?, 0, ?)
        ON CONFLICT(telegram_id) DO UPDATE SET username=excluded.username
        """,
        (telegram_id, username, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()


def save_risk_result(telegram_id: int, result: str):
    conn = db()
    conn.execute(
        "UPDATE users SET risk_result=?, risk_checked_at=? WHERE telegram_id=?",
        (result, datetime.now(timezone.utc).isoformat(), telegram_id),
    )
    conn.commit()
    conn.close()


def get_user(telegram_id: int) -> sqlite3.Row | None:
    conn = db()
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM users WHERE telegram_id=?", (telegram_id,)
    ).fetchone()
    conn.close()
    return row


def increment_generations(telegram_id: int) -> int:
    conn = db()
    conn.execute(
        "UPDATE users SET generations_used = generations_used + 1 WHERE telegram_id=?",
        (telegram_id,),
    )
    conn.commit()
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT generations_used FROM users WHERE telegram_id=?", (telegram_id,)
    ).fetchone()
    conn.close()
    return row["generations_used"]


def add_purchased_generations(telegram_id: int, n: int):
    conn = db()
    conn.execute(
        "UPDATE users SET purchased_generations = purchased_generations + ? WHERE telegram_id=?",
        (n, telegram_id),
    )
    conn.commit()
    conn.close()


def get_generation_limit(telegram_id: int) -> int:
    row = get_user(telegram_id)
    purchased = row["purchased_generations"] if row and row["purchased_generations"] else 0
    return FREE_GENERATIONS + purchased


def is_subscribed(telegram_id: int) -> bool:
    row = get_user(telegram_id)
    if not row or not row["subscription_until"]:
        return False
    try:
        return datetime.fromisoformat(row["subscription_until"]) > datetime.now(timezone.utc)
    except Exception:
        return False


def extend_subscription(telegram_id: int, days: int = 30):
    conn = db()
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT subscription_until FROM users WHERE telegram_id=?", (telegram_id,)
    ).fetchone()
    now = datetime.now(timezone.utc)
    current = None
    if row and row["subscription_until"]:
        try:
            current = datetime.fromisoformat(row["subscription_until"])
        except Exception:
            current = None
    base = current if current and current > now else now
    new_until = (base + timedelta(days=days)).isoformat()
    conn.execute(
        "UPDATE users SET subscription_until=? WHERE telegram_id=?", (new_until, telegram_id)
    )
    conn.commit()
    conn.close()
    return new_until


def log_payment(telegram_id: int, username: str | None, kind: str, currency: str, amount: int):
    conn = db()
    conn.execute(
        "INSERT INTO payments (telegram_id, username, kind, currency, amount, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (telegram_id, username, kind, currency, amount, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()


def get_admin_stats():
    conn = db()
    conn.row_factory = sqlite3.Row
    total_users = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
    now_iso = datetime.now(timezone.utc).isoformat()
    pro_count = conn.execute(
        "SELECT COUNT(*) c FROM users WHERE subscription_until IS NOT NULL AND subscription_until > ?",
        (now_iso,),
    ).fetchone()["c"]
    earnings = conn.execute(
        "SELECT currency, SUM(amount) total, COUNT(*) n FROM payments GROUP BY currency"
    ).fetchall()
    users = conn.execute(
        "SELECT telegram_id, username, generations_used, purchased_generations, "
        "subscription_until FROM users ORDER BY created_at DESC LIMIT 50"
    ).fetchall()
    conn.close()
    return {
        "total_users": total_users,
        "pro_count": pro_count,
        "earnings": earnings,
        "users": users,
    }


def find_user_by_username(username: str):
    username = username.lstrip("@").lower()
    conn = db()
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT telegram_id FROM users WHERE LOWER(username)=?", (username,)
    ).fetchone()
    conn.close()
    return row["telegram_id"] if row else None


# ---------------------------------------------------------------------------
# /start, /help
# ---------------------------------------------------------------------------

WELCOME = (
    "\U0001f916 *Attestly* \\- EU AI Act compliance, generated from what your "
    "AI agents already do\\.\n\n"
    "Free risk checks, drafted Annex IV documentation, and answers on the EU "
    "AI Act \\- right here in Telegram\\.\n\n"
    "Pick an option below, or just ask a question\\."
)

ADMIN_INFO_TEXT = (
    "\U0001f6e1 Group admin toolkit\n\n"
    "In groups, I automatically answer questions about Attestly and the EU AI Act, "
    "filter links that aren't to attestly.online, and (for group admins) support:\n\n"
    "/ban, /unban, /kick, /mute, /unmute, /promote, /demote, /pin, /unpin \u2014 "
    "all by replying to a user's message\n"
    "/purge \u2014 reply to bulk-delete from there to now, or /purge <N>\n"
    "/cleanservice on|off \u2014 toggle auto-cleanup of join notices and command clutter\n"
    "/announce <text> \u2014 (channel admins) post to the Attestly channel"
)


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("\U0001f50d Risk Check", callback_data="menu_riskcheck"),
         InlineKeyboardButton("\U0001f4c4 Generate", callback_data="menu_generate")],
        [InlineKeyboardButton("\U0001f4ca Status", callback_data="menu_status"),
         InlineKeyboardButton("\u2b50 Upgrade to Pro", callback_data="menu_upgrade")],
        [InlineKeyboardButton("\U0001f6e1 Admin Commands", callback_data="menu_admin")],
        [InlineKeyboardButton("\U0001f310 attestly.online", url="https://www.attestly.online")],
    ])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    upsert_user(user.id, user.username)
    await update.message.reply_markdown_v2(WELCOME, reply_markup=main_menu_keyboard())


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_markdown_v2(WELCOME, reply_markup=main_menu_keyboard())


async def menu_admin_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await context.bot.send_message(query.message.chat_id, ADMIN_INFO_TEXT)


# ---------------------------------------------------------------------------
# /riskcheck - conversational EU AI Act risk classifier
# ---------------------------------------------------------------------------


def yn_keyboard():
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Yes", callback_data="yes"),
          InlineKeyboardButton("No", callback_data="no")]]
    )


RISKCHECK_Q1 = (
    "\U0001f50d EU AI Act Risk Check \u2014 4 quick questions.\n\n"
    "1) Does your system do any of the following: subliminal manipulation, "
    "social scoring, real-time remote biometric identification by law "
    "enforcement in public spaces, or emotion recognition in workplaces "
    "or schools?"
)


async def riskcheck_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["risk_answers"] = {}
    await update.message.reply_text(RISKCHECK_Q1, reply_markup=yn_keyboard())
    return Q_PROHIBITED


async def riskcheck_start_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["risk_answers"] = {}
    await context.bot.send_message(query.message.chat_id, RISKCHECK_Q1, reply_markup=yn_keyboard())
    return Q_PROHIBITED


async def q_prohibited(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["risk_answers"]["prohibited"] = query.data
    if query.data == "yes":
        context.user_data["risk_verdict"] = "Prohibited practice"
        await query.edit_message_text(
            "Result: this touches a *prohibited practice* under Article 5. "
            "That means it likely can't be deployed in the EU as described, "
            "regardless of documentation. Get a proper legal read before proceeding.",
            parse_mode="Markdown",
        )
        return await finish_riskcheck(update, context)

    await query.edit_message_text(
        "2) Is it used in one of these areas: biometrics, critical infrastructure, "
        "education/training, employment or worker management, access to essential "
        "services, law enforcement, migration/border control, or administration of "
        "justice/democratic processes?"
    )
    await context.bot.send_message(
        chat_id=query.message.chat_id, text="Pick one:", reply_markup=yn_keyboard()
    )
    return Q_HIGH_RISK


async def q_high_risk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["risk_answers"]["high_risk_area"] = query.data
    if query.data == "yes":
        context.user_data["risk_verdict"] = "Likely high-risk (Annex III)"
        await query.edit_message_text(
            "3) Is it a general-purpose AI model (e.g. a foundation/LLM model "
            "you or someone else trained) rather than a narrow single-purpose system?",
        )
        await context.bot.send_message(
            chat_id=query.message.chat_id, text="Pick one:", reply_markup=yn_keyboard()
        )
        return Q_GPAI

    await query.edit_message_text(
        "3) Is it a general-purpose AI model (e.g. a foundation/LLM model "
        "you or someone else trained) rather than a narrow single-purpose system?"
    )
    await context.bot.send_message(
        chat_id=query.message.chat_id, text="Pick one:", reply_markup=yn_keyboard()
    )
    return Q_GPAI


async def q_gpai(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["risk_answers"]["gpai"] = query.data
    if query.data == "yes" and "risk_verdict" not in context.user_data:
        context.user_data["risk_verdict"] = "GPAI obligations may apply"

    await query.edit_message_text(
        "4) Does it interact directly with people, or generate/manipulate "
        "images, audio, or video content (e.g. a chatbot or deepfake-style tool)?"
    )
    await context.bot.send_message(
        chat_id=query.message.chat_id, text="Pick one:", reply_markup=yn_keyboard()
    )
    return Q_TRANSPARENCY


async def q_transparency(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["risk_answers"]["transparency"] = query.data

    if "risk_verdict" not in context.user_data:
        if query.data == "yes":
            context.user_data["risk_verdict"] = "Limited risk (transparency obligations)"
        else:
            context.user_data["risk_verdict"] = "Minimal risk"

    verdict = context.user_data["risk_verdict"]
    save_risk_result(update.effective_user.id, verdict)

    await query.edit_message_text(
        f"*Indicative result: {verdict}*\n\n"
        "This is a directional read, not legal advice. For an audit-ready "
        "documentation trail mapped to Annex IV, run /generate on your trace "
        f"data, or see the full picture at {LOGIN_URL}",
        parse_mode="Markdown",
    )
    return ConversationHandler.END


async def riskcheck_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Risk check cancelled.")
    return ConversationHandler.END


async def finish_riskcheck(update: Update, context: ContextTypes.DEFAULT_TYPE):
    save_risk_result(update.effective_user.id, context.user_data.get("risk_verdict", "Unknown"))
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# /status
# ---------------------------------------------------------------------------


async def send_status(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user):
    upsert_user(user.id, user.username)
    row = get_user(user.id)
    risk = row["risk_result"] if row and row["risk_result"] else "not checked yet \u2014 run /riskcheck"
    used = row["generations_used"] if row else 0

    if is_subscribed(user.id):
        until = datetime.fromisoformat(row["subscription_until"]).strftime("%b %d, %Y")
        plan_line = f"\u2b50 Pro \u2014 unlimited generations (renews/expires {until})"
    else:
        limit = get_generation_limit(user.id)
        remaining = max(limit - used, 0)
        plan_line = f"\U0001f193 Free \u2014 {remaining}/{limit} generations remaining"

    await context.bot.send_message(
        chat_id,
        f"\U0001f4ca *Your Attestly status*\n\n"
        f"Plan: {plan_line}\n"
        f"Risk classification: {risk}\n\n"
        f"Full account + history: {LOGIN_URL}",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("\u2b50 Upgrade to Pro", callback_data="menu_upgrade"),
             InlineKeyboardButton("\U0001f4b0 Buy generations", callback_data="menu_buy")]
        ]),
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_status(context, update.effective_chat.id, update.effective_user)


async def status_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await send_status(context, query.message.chat_id, query.from_user)


# ---------------------------------------------------------------------------
# /generate - upload trace JSON -> drafted Annex IV paragraph -> docx
# ---------------------------------------------------------------------------

GENERATE_PROMPT_TEXT = (
    "Send me a trace file as a JSON document (OpenTelemetry, LangSmith, "
    "AgentOps export, or plain normalized JSON with tool_call / "
    "human_intervention / error / deployment_change events)."
)


async def send_generate_prompt(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user):
    upsert_user(user.id, user.username)
    row = get_user(user.id)
    used = row["generations_used"] if row else 0

    if not is_subscribed(user.id) and used >= get_generation_limit(user.id):
        await context.bot.send_message(
            chat_id,
            "You've used all your doc-generations.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("\u2b50 Upgrade to Pro", callback_data="menu_upgrade"),
                 InlineKeyboardButton("\U0001f4b0 Buy generations", callback_data="menu_buy")]
            ]),
        )
        return

    if not ANTHROPIC_API_KEY or Anthropic is None:
        await context.bot.send_message(
            chat_id,
            "Doc generation isn't configured on this bot yet (missing API key). "
            f"In the meantime, use the full tool at {LOGIN_URL}",
        )
        return

    await context.bot.send_message(chat_id, GENERATE_PROMPT_TEXT)


async def generate_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_generate_prompt(context, update.effective_chat.id, update.effective_user)


async def generate_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await send_generate_prompt(context, query.message.chat_id, query.from_user)


async def generate_receive_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    row = get_user(user.id)
    used = row["generations_used"] if row else 0
    limit = get_generation_limit(user.id)
    subscribed = is_subscribed(user.id)
    if not subscribed and used >= limit:
        await update.message.reply_text(
            "You've used all your doc-generations. Try /buy or /upgrade.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("\u2b50 Upgrade to Pro", callback_data="menu_upgrade"),
                 InlineKeyboardButton("\U0001f4b0 Buy generations", callback_data="menu_buy")]
            ]),
        )
        return

    doc = update.message.document
    if not doc or not (doc.file_name or "").lower().endswith(".json"):
        await update.message.reply_text("Please send a .json trace file.")
        return

    if not ANTHROPIC_API_KEY or Anthropic is None:
        await update.message.reply_text(
            "Doc generation isn't configured on this bot yet (missing API key). "
            f"In the meantime, use the full tool at {LOGIN_URL}"
        )
        return

    tg_file = await doc.get_file()
    raw = await tg_file.download_as_bytearray()
    try:
        trace_events = json.loads(raw.decode("utf-8"))
    except Exception:
        await update.message.reply_text("That file isn't valid JSON \u2014 please check and resend.")
        return

    await update.message.reply_text("\u2699\ufe0f Drafting your Annex IV section\u2026 one moment.")

    try:
        drafted = draft_annex_iv_section(trace_events)
    except Exception as e:
        log.exception("draft failed")
        await update.message.reply_text(f"Couldn't draft that: {e}")
        return

    docx_path = build_docx(drafted, trace_events)
    used_after = increment_generations(user.id)
    caption = "\u2705 Drafted section attached."
    if subscribed:
        caption += " (Pro plan \u2014 unlimited)"
    else:
        remaining_after = max(limit - used_after, 0)
        caption += f" Generations left: {remaining_after}/{limit}."
    caption += f" Full account + more sections: {LOGIN_URL}"

    with open(docx_path, "rb") as f:
        await update.message.reply_document(
            document=f,
            filename="annex_iv_draft.docx",
            caption=caption,
        )
    os.remove(docx_path)


def draft_annex_iv_section(trace_events) -> str:
    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    prompt = (
        "You are drafting a short EU AI Act Annex IV 'monitoring measures' "
        "paragraph for a compliance document. Base every sentence ONLY on the "
        "trace events given below \u2014 do not invent anything not evidenced in "
        "them. If evidence is thin, say so plainly rather than padding. "
        "Keep it to 3-5 sentences.\n\n"
        f"Trace events (JSON):\n{json.dumps(trace_events)[:8000]}"
    )
    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(block.text for block in resp.content if block.type == "text")


def build_docx(drafted_text: str, trace_events) -> str:
    document = Document()
    document.add_heading("Annex IV \u2014 Monitoring Measures (Draft)", level=1)
    document.add_paragraph(drafted_text)
    document.add_heading("Source trace events", level=2)
    for ev in (trace_events if isinstance(trace_events, list) else [trace_events]):
        document.add_paragraph(json.dumps(ev), style="List Bullet")
    document.add_paragraph()
    document.add_paragraph(
        "This is an AI-generated draft and requires human review before it "
        "counts as final. Attestly does not provide legal advice and does "
        "not guarantee regulatory compliance."
    )
    path = "annex_iv_draft.docx"
    document.save(path)
    return path


# ---------------------------------------------------------------------------
# Group admin helpers
# ---------------------------------------------------------------------------


async def is_group_admin(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int) -> bool:
    """True if user_id is an admin/creator of chat_id. Never trust a hardcoded list \u2014
    always check live against Telegram, so admin rights follow the group's actual settings."""
    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        return member.status in ("administrator", "creator")
    except Exception:
        return False


async def is_bot_admin_here(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> bool:
    try:
        me = await context.bot.get_me()
        member = await context.bot.get_chat_member(chat_id, me.id)
        return member.status in ("administrator", "creator")
    except Exception:
        return False


async def delete_later(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int, delay: float):
    context.job_queue.run_once(
        lambda ctx: ctx.bot.delete_message(chat_id, message_id), delay
    )


async def clean_command_exchange(update: Update, context: ContextTypes.DEFAULT_TYPE, reply_text: str):
    """Sends an admin command's confirmation, then (if clean_service is on for this chat)
    deletes both the invoking /command message and this confirmation shortly after \u2014
    keeps the chat from filling up with command clutter."""
    chat_id = update.effective_chat.id
    sent = await update.message.reply_text(reply_text)
    if get_clean_service(chat_id):
        try:
            await update.message.delete()
        except Exception:
            pass
        await delete_later(context, chat_id, sent.message_id, COMMAND_AUTODELETE_SECONDS)


# ---------------------------------------------------------------------------
# Welcome new members (single message, auto-deleted; join service notice removed)
# ---------------------------------------------------------------------------


async def welcome_new_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.new_chat_members:
        return
    chat_id = update.effective_chat.id
    clean = get_clean_service(chat_id)
    real_members = [m for m in update.message.new_chat_members if not m.is_bot]

    if real_members:
        names = ", ".join(m.first_name or m.username or "there" for m in real_members)
        sent = await update.message.reply_text(
            f"Welcome, {names}! This group is about Attestly \u2014 EU AI Act compliance "
            "generated from your AI agents' traces.\n\n"
            "Try asking a question (e.g. \"what is annex iv\" or \"how much does it cost\"), "
            "or message @Attestly_bot directly for /riskcheck and /generate."
        )
        if clean:
            await delete_later(context, chat_id, sent.message_id, WELCOME_AUTODELETE_SECONDS)

    # Remove Telegram's own "X joined the group" service notice so only one message
    # (our welcome, above) marks the join \u2014 not two.
    if clean:
        try:
            await update.message.delete()
        except Exception:
            pass


async def clean_other_service_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Removes Telegram's own service messages (left member, pinned notice, title/photo
    changes, etc.) when clean_service is on for the chat. New-member joins are handled
    separately by welcome_new_members so they aren't double-deleted here."""
    if not update.message:
        return
    chat_id = update.effective_chat.id
    if not get_clean_service(chat_id):
        return
    try:
        await update.message.delete()
    except Exception:
        pass


async def cleanservice_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    requester = update.effective_user
    if chat.type == "private":
        await update.message.reply_text("This only works in a group.")
        return
    if not await is_group_admin(context, chat.id, requester.id):
        await update.message.reply_text("Only group admins can use /cleanservice.")
        return
    if not context.args or context.args[0].lower() not in ("on", "off"):
        current = "on" if get_clean_service(chat.id) else "off"
        await update.message.reply_text(
            f"Clean-service mode is currently {current}. Usage: /cleanservice on|off"
        )
        return
    enabled = context.args[0].lower() == "on"
    set_clean_service(chat.id, enabled)
    await update.message.reply_text(
        f"Clean-service mode turned {'on' if enabled else 'off'}. "
        + ("Join notices and command clutter will now be auto-removed."
           if enabled else
           "Join notices, welcome messages, and command messages will stay visible.")
    )


# ---------------------------------------------------------------------------
# /purge (admin-only): bulk delete messages
# ---------------------------------------------------------------------------


async def purge_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    requester = update.effective_user
    if chat.type == "private":
        await update.message.reply_text("This only works in a group.")
        return
    if not await is_group_admin(context, chat.id, requester.id):
        await update.message.reply_text("Only group admins can use /purge.")
        return

    command_msg_id = update.message.message_id

    if update.message.reply_to_message:
        start_id = update.message.reply_to_message.message_id
        end_id = command_msg_id
        count = end_id - start_id + 1
        if count > PURGE_MAX_MESSAGES:
            await update.message.reply_text(
                f"That range is {count} messages \u2014 capped at {PURGE_MAX_MESSAGES} per run. "
                "Reply to a more recent message and try again."
            )
            return
        ids_to_delete = list(range(start_id, end_id + 1))
    elif context.args and context.args[0].isdigit():
        n = min(int(context.args[0]), PURGE_MAX_MESSAGES)
        ids_to_delete = list(range(command_msg_id - n, command_msg_id + 1))
    else:
        await update.message.reply_text(
            "Reply to a message with /purge to delete everything from there to now, "
            f"or use /purge <N> to delete the last N messages (max {PURGE_MAX_MESSAGES})."
        )
        return

    deleted = 0
    for mid in ids_to_delete:
        try:
            await context.bot.delete_message(chat.id, mid)
            deleted += 1
        except Exception:
            pass  # message already gone, too old, or never existed \u2014 skip silently

    confirmation = await context.bot.send_message(chat.id, f"Purged {deleted} message(s).")
    await delete_later(context, chat.id, confirmation.message_id, COMMAND_AUTODELETE_SECONDS)


# ---------------------------------------------------------------------------
# /ban and /promote (admin-only, live-checked against Telegram)
# ---------------------------------------------------------------------------


async def ban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    requester = update.effective_user
    if chat.type == "private":
        await update.message.reply_text("This only works in a group.")
        return
    if not await is_group_admin(context, chat.id, requester.id):
        await update.message.reply_text("Only group admins can use /ban.")
        return
    if not update.message.reply_to_message:
        await update.message.reply_text("Reply to the user's message with /ban to remove them.")
        return
    target = update.message.reply_to_message.from_user
    if await is_group_admin(context, chat.id, target.id):
        await update.message.reply_text("I won't ban another admin.")
        return
    try:
        await context.bot.ban_chat_member(chat.id, target.id)
        await clean_command_exchange(update, context, f"Banned {target.first_name or target.username}.")
    except Exception as e:
        await update.message.reply_text(
            f"Couldn't ban that user: {e}\n"
            "Make sure I have 'Ban users' permission in this group's admin settings."
        )


async def promote_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    requester = update.effective_user
    if chat.type == "private":
        await update.message.reply_text("This only works in a group.")
        return
    if not await is_group_admin(context, chat.id, requester.id):
        await update.message.reply_text("Only group admins can use /promote.")
        return
    if not update.message.reply_to_message:
        await update.message.reply_text("Reply to the user's message with /promote to make them an admin.")
        return
    target = update.message.reply_to_message.from_user
    try:
        await context.bot.promote_chat_member(
            chat.id,
            target.id,
            can_delete_messages=True,
            can_restrict_members=True,
            can_pin_messages=True,
            can_invite_users=True,
            can_manage_chat=True,
        )
        await clean_command_exchange(update, context, f"Promoted {target.first_name or target.username} to admin.")
    except Exception as e:
        await update.message.reply_text(
            f"Couldn't promote that user: {e}\n"
            "Make sure I have 'Add new admins' permission in this group's admin settings."
        )


async def demote_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    requester = update.effective_user
    if chat.type == "private":
        await update.message.reply_text("This only works in a group.")
        return
    if not await is_group_admin(context, chat.id, requester.id):
        await update.message.reply_text("Only group admins can use /demote.")
        return
    if not update.message.reply_to_message:
        await update.message.reply_text("Reply to the user's message with /demote to remove their admin rights.")
        return
    target = update.message.reply_to_message.from_user
    try:
        await context.bot.promote_chat_member(
            chat.id,
            target.id,
            can_delete_messages=False,
            can_restrict_members=False,
            can_pin_messages=False,
            can_invite_users=False,
            can_manage_chat=False,
            can_promote_members=False,
            can_change_info=False,
            can_manage_video_chats=False,
        )
        await clean_command_exchange(update, context, f"Demoted {target.first_name or target.username}.")
    except Exception as e:
        await update.message.reply_text(f"Couldn't demote that user: {e}")


async def unban_user_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    requester = update.effective_user
    if chat.type == "private":
        await update.message.reply_text("This only works in a group.")
        return
    if not await is_group_admin(context, chat.id, requester.id):
        await update.message.reply_text("Only group admins can use /unban.")
        return
    args = context.args
    target_id = None
    if update.message.reply_to_message:
        target_id = update.message.reply_to_message.from_user.id
    elif args and args[0].lstrip("-").isdigit():
        target_id = int(args[0])
    if target_id is None:
        await update.message.reply_text(
            "Reply to the banned user's message with /unban, or use /unban <user_id> "
            "(needed since a banned user has no recent message to reply to)."
        )
        return
    try:
        await context.bot.unban_chat_member(chat.id, target_id, only_if_banned=True)
        await clean_command_exchange(update, context, "Unbanned. They can rejoin now.")
    except Exception as e:
        await update.message.reply_text(f"Couldn't unban: {e}")


async def kick_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Removes the user from the group without a permanent ban (they can rejoin via invite link)."""
    chat = update.effective_chat
    requester = update.effective_user
    if chat.type == "private":
        await update.message.reply_text("This only works in a group.")
        return
    if not await is_group_admin(context, chat.id, requester.id):
        await update.message.reply_text("Only group admins can use /kick.")
        return
    if not update.message.reply_to_message:
        await update.message.reply_text("Reply to the user's message with /kick to remove them (not a permanent ban).")
        return
    target = update.message.reply_to_message.from_user
    if await is_group_admin(context, chat.id, target.id):
        await update.message.reply_text("I won't kick another admin.")
        return
    try:
        await context.bot.ban_chat_member(chat.id, target.id)
        await context.bot.unban_chat_member(chat.id, target.id, only_if_banned=True)
        await clean_command_exchange(
            update, context, f"Kicked {target.first_name or target.username} (they can rejoin via invite link)."
        )
    except Exception as e:
        await update.message.reply_text(f"Couldn't kick that user: {e}")


def parse_duration(text: str) -> int | None:
    """Parses '10m', '2h', '1d' into seconds. Returns None if unparseable."""
    m = re.fullmatch(r"(\d+)([smhd])", text.strip().lower())
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2)
    return n * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]


async def mute_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    requester = update.effective_user
    if chat.type == "private":
        await update.message.reply_text("This only works in a group.")
        return
    if not await is_group_admin(context, chat.id, requester.id):
        await update.message.reply_text("Only group admins can use /mute.")
        return
    if not update.message.reply_to_message:
        await update.message.reply_text(
            "Reply to the user's message with /mute (optionally /mute 10m, 2h, or 1d "
            "for a timed mute; no duration = muted until /unmute)."
        )
        return
    target = update.message.reply_to_message.from_user
    if await is_group_admin(context, chat.id, target.id):
        await update.message.reply_text("I won't mute another admin.")
        return

    until_date = None
    if context.args:
        seconds = parse_duration(context.args[0])
        if seconds is None:
            await update.message.reply_text("Couldn't parse duration \u2014 use e.g. 10m, 2h, or 1d.")
            return
        until_date = datetime.now(timezone.utc).timestamp() + seconds

    try:
        await context.bot.restrict_chat_member(
            chat.id,
            target.id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=until_date,
        )
        duration_note = f" for {context.args[0]}" if context.args else ""
        await clean_command_exchange(update, context, f"Muted {target.first_name or target.username}{duration_note}.")
    except Exception as e:
        await update.message.reply_text(
            f"Couldn't mute that user: {e}\n"
            "Make sure I have 'Ban users' / restrict-members permission in this group's admin settings."
        )


async def unmute_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    requester = update.effective_user
    if chat.type == "private":
        await update.message.reply_text("This only works in a group.")
        return
    if not await is_group_admin(context, chat.id, requester.id):
        await update.message.reply_text("Only group admins can use /unmute.")
        return
    if not update.message.reply_to_message:
        await update.message.reply_text("Reply to the user's message with /unmute to restore their permissions.")
        return
    target = update.message.reply_to_message.from_user
    try:
        await context.bot.restrict_chat_member(
            chat.id,
            target.id,
            permissions=ChatPermissions(
                can_send_messages=True,
                can_send_audios=True,
                can_send_documents=True,
                can_send_photos=True,
                can_send_videos=True,
                can_send_video_notes=True,
                can_send_voice_notes=True,
                can_send_polls=True,
                can_send_other_messages=True,
                can_add_web_page_previews=True,
            ),
        )
        await clean_command_exchange(update, context, f"Unmuted {target.first_name or target.username}.")
    except Exception as e:
        await update.message.reply_text(f"Couldn't unmute that user: {e}")


async def pin_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    requester = update.effective_user
    if chat.type == "private":
        await update.message.reply_text("This only works in a group.")
        return
    if not await is_group_admin(context, chat.id, requester.id):
        await update.message.reply_text("Only group admins can use /pin.")
        return
    if not update.message.reply_to_message:
        await update.message.reply_text("Reply to the message you want to pin with /pin.")
        return
    silent = bool(context.args and context.args[0].lower() in ("silent", "quiet"))
    try:
        await context.bot.pin_chat_message(
            chat.id, update.message.reply_to_message.message_id, disable_notification=silent
        )
        await clean_command_exchange(update, context, "Pinned.")
    except Exception as e:
        await update.message.reply_text(
            f"Couldn't pin that message: {e}\n"
            "Make sure I have 'Pin messages' permission in this group's admin settings."
        )


async def unpin_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    requester = update.effective_user
    if chat.type == "private":
        await update.message.reply_text("This only works in a group.")
        return
    if not await is_group_admin(context, chat.id, requester.id):
        await update.message.reply_text("Only group admins can use /unpin.")
        return
    try:
        if update.message.reply_to_message:
            await context.bot.unpin_chat_message(chat.id, update.message.reply_to_message.message_id)
        else:
            await context.bot.unpin_chat_message(chat.id)  # unpins the most recent pin
        await clean_command_exchange(update, context, "Unpinned.")
    except Exception as e:
        await update.message.reply_text(f"Couldn't unpin: {e}")


# ---------------------------------------------------------------------------
# Link filter + basic spam control + keyword FAQ
# One handler covers all group text so moderation always runs before FAQ replies.
# ---------------------------------------------------------------------------


def extract_domains(text: str):
    urls = re.findall(r"(?:https?://|www\.)[^\s]+", text, flags=re.IGNORECASE)
    domains = []
    for u in urls:
        candidate = u if u.startswith("http") else "http://" + u
        try:
            netloc = urlparse(candidate).netloc.lower()
            netloc = netloc.split("@")[-1]  # strip userinfo if present
            domains.append(netloc)
        except Exception:
            continue
    return domains


def is_allowed_domain(domain: str) -> bool:
    return any(domain == d or domain.endswith("." + d) for d in ALLOWED_LINK_DOMAINS)


async def delete_with_notice(update: Update, context: ContextTypes.DEFAULT_TYPE, reason: str):
    chat_id = update.effective_chat.id
    try:
        await update.message.delete()
    except Exception:
        return  # bot probably lacks delete permission; don't also spam a notice
    user = update.effective_user
    name = user.first_name or user.username or "there"
    notice = await context.bot.send_message(chat_id, f"Removed a message from {name}: {reason}")
    context.job_queue.run_once(
        lambda ctx: ctx.bot.delete_message(chat_id, notice.message_id),
        WARNING_AUTODELETE_SECONDS,
    )


def match_faq(text: str):
    text_l = text.lower()
    best = None
    best_score = 0
    for topic in TOPICS:
        score = sum(1 for kw in topic["keywords"] if kw in text_l)
        if score > best_score:
            best = topic
            best_score = score
    return best if best_score > 0 else None


async def group_message_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Runs on every non-command group text message: enforces link/spam rules first,
    then (if nothing was removed) tries a keyword-matched FAQ reply."""
    message = update.message
    if not message or not message.text:
        return
    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        return  # moderation/FAQ only applies in groups, not DMs
    user = update.effective_user
    text = message.text

    sender_is_admin = await is_group_admin(context, chat.id, user.id)

    # 1) link filter: remove links to any domain that isn't attestly.online, unless sender is admin
    if not sender_is_admin:
        domains = extract_domains(text)
        bad_domains = [d for d in domains if not is_allowed_domain(d)]
        if bad_domains:
            await delete_with_notice(
                update, context,
                f"links to {', '.join(sorted(set(bad_domains)))} aren't allowed here \u2014 "
                "only attestly.online links.",
            )
            return
        if len(domains) > MAX_LINKS_PER_MESSAGE:
            await delete_with_notice(update, context, "too many links (looks like spam).")
            return

    # 2) duplicate-message spam control (same user repeating identical text quickly)
    if not sender_is_admin:
        key = (chat.id, user.id)
        prev = _last_message_by_user.get(key)
        now = time.time()
        if prev and prev[0] == text.strip() and (now - prev[1]) < DUPLICATE_WINDOW_SECONDS:
            await delete_with_notice(update, context, "duplicate message (spam control).")
            return
        _last_message_by_user[key] = (text.strip(), now)

    # 3) keyword-triggered FAQ (fixed answers, no LLM), with a per-chat cooldown
    topic = match_faq(text)
    if topic:
        now = time.time()
        if now - _last_faq_reply_at[chat.id] >= FAQ_COOLDOWN_SECONDS:
            await message.reply_text(topic["answer"])
            _last_faq_reply_at[chat.id] = now


# ---------------------------------------------------------------------------
# Channel announcements: /announce (manual, works today) +
# background site-watcher (auto-posts once attestly.online has a blog/changelog)
# ---------------------------------------------------------------------------

# Fixed hashtags always attached, plus dynamic ones matched against FAQ topics
# so announcements are discoverable via Telegram's in-app search on those terms.
CORE_HASHTAGS = "#EUAIAct #AICompliance #Attestly"
TOPIC_HASHTAGS = {
    "annex_iv": "#AnnexIV",
    "risk_tiers": "#AIRiskAssessment",
    "prohibited_practices": "#Article5",
    "high_risk_annex_iii": "#AnnexIII #HighRiskAI",
    "gpai": "#GPAI",
    "transparency": "#AITransparency",
    "fines": "#RegTech",
}


def build_hashtags(text: str) -> str:
    text_l = text.lower()
    tags = [CORE_HASHTAGS]
    for topic in TOPICS:
        if topic["id"] in TOPIC_HASHTAGS and any(kw in text_l for kw in topic["keywords"]):
            tags.append(TOPIC_HASHTAGS[topic["id"]])
    return " ".join(dict.fromkeys(tags))  # de-dupe, keep order


async def announce(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """DM this to the bot: /announce <text>. Posts to ANNOUNCE_CHANNEL_ID if the
    requester is an admin of that channel. Works today, independent of the watcher."""
    if not ANNOUNCE_CHANNEL_ID:
        await update.message.reply_text("No announcement channel is configured (ANNOUNCE_CHANNEL_ID).")
        return
    requester = update.effective_user
    if not await is_group_admin(context, ANNOUNCE_CHANNEL_ID, requester.id):
        await update.message.reply_text(
            "Only admins of the announcement channel can use /announce."
        )
        return
    text = update.message.text.partition(" ")[2].strip()
    if not text:
        await update.message.reply_text("Usage: /announce <your update text>")
        return
    try:
        await context.bot.send_message(
            ANNOUNCE_CHANNEL_ID, f"{text}\n\n{build_hashtags(text)}"
        )
        await update.message.reply_text("Posted to the channel.")
    except Exception as e:
        await update.message.reply_text(
            f"Couldn't post to the channel: {e}\n"
            "Make sure the bot is an admin of the channel with 'Post Messages' permission."
        )


def get_watch_hash(url: str):
    conn = db()
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT content_hash FROM site_watch WHERE url=?", (url,)).fetchone()
    conn.close()
    return row["content_hash"] if row else None


def set_watch_hash(url: str, content_hash: str):
    conn = db()
    conn.execute(
        """
        INSERT INTO site_watch (url, content_hash, last_checked) VALUES (?, ?, ?)
        ON CONFLICT(url) DO UPDATE SET content_hash=excluded.content_hash, last_checked=excluded.last_checked
        """,
        (url, content_hash, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()


def fetch_page_hash(url: str) -> str | None:
    try:
        resp = requests.get(url, timeout=15, headers={"User-Agent": "AttestlyBot/1.0"})
        resp.raise_for_status()
        # Strip tags/scripts for a rough text-only hash so markup-only changes
        # (ads, timestamps, nonce attributes) don't trigger false positives.
        text_only = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", resp.text, flags=re.S | re.I)
        text_only = re.sub(r"<[^>]+>", " ", text_only)
        text_only = re.sub(r"\s+", " ", text_only).strip()
        return hashlib.sha256(text_only.encode("utf-8")).hexdigest()
    except Exception as e:
        log.warning(f"site-watch fetch failed for {url}: {e}")
        return None


async def check_for_updates(context: ContextTypes.DEFAULT_TYPE):
    """Runs on a schedule. For each WATCH_URLS entry: if its content changed since
    last check, post an alert to ANNOUNCE_CHANNEL_ID. First-ever check on a URL just
    records a baseline (no post), so adding a new URL doesn't trigger a false alert."""
    if not WATCH_URLS or not ANNOUNCE_CHANNEL_ID:
        return
    for url in WATCH_URLS:
        new_hash = fetch_page_hash(url)
        if new_hash is None:
            continue
        old_hash = get_watch_hash(url)
        if old_hash is None:
            set_watch_hash(url, new_hash)
            continue
        if new_hash != old_hash:
            set_watch_hash(url, new_hash)
            try:
                text = f"Attestly update \u2014 something changed here:\n{url}"
                await context.bot.send_message(ANNOUNCE_CHANNEL_ID, f"{text}\n\n{build_hashtags(text)}")
            except Exception as e:
                log.warning(f"couldn't post site-watch update: {e}")


# ---------------------------------------------------------------------------
# /buy: generation packs via Telegram Stars (instant, no setup) or Ammer Pay (USD)
# /upgrade: monthly Pro subscription (Telegram Stars \u2014 recurring subscriptions
# are Stars-only on Telegram's platform, fiat providers don't support them)
# ---------------------------------------------------------------------------

SUBSCRIPTION_PRICE_STARS = min(
    int(os.environ.get("SUBSCRIPTION_PRICE_STARS", "2500")), 2500
)  # Telegram enforces a hard 2500-Star cap on any single subscription \u2014 this is the max possible


def buy_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(
            f"\u2b50 {GENERATION_PACK_STARS} Stars \u2014 {GENERATION_PACK_SIZE} generations",
            callback_data="buy_stars",
        )]
    ]
    if AMMER_PAY_TOKEN:
        buttons.append([InlineKeyboardButton(
            f"\U0001f4b3 ${GENERATION_PACK_USD_CENTS/100:.2f} \u2014 {GENERATION_PACK_SIZE} generations",
            callback_data="buy_fiat",
        )])
    return InlineKeyboardMarkup(buttons)


async def send_buy_menu(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    await context.bot.send_message(
        chat_id,
        f"\U0001f4b0 Get {GENERATION_PACK_SIZE} more Annex IV generations:",
        reply_markup=buy_keyboard(),
    )


async def buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    upsert_user(user.id, user.username)
    await send_buy_menu(context, update.effective_chat.id)


async def buy_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await send_buy_menu(context, query.message.chat_id)


async def buy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    title = f"{GENERATION_PACK_SIZE} Attestly generations"
    description = f"Adds {GENERATION_PACK_SIZE} extra Annex IV doc-generations to your account."

    if query.data == "buy_stars":
        await context.bot.send_invoice(
            chat_id=chat_id,
            title=title,
            description=description,
            payload=f"stars_pack_{GENERATION_PACK_SIZE}",
            provider_token="",  # empty for Telegram Stars
            currency="XTR",
            prices=[LabeledPrice(title, GENERATION_PACK_STARS)],
        )
    elif query.data == "buy_fiat":
        if not AMMER_PAY_TOKEN:
            await context.bot.send_message(chat_id, "Card payments aren't configured yet \u2014 try Stars instead.")
            return
        await context.bot.send_invoice(
            chat_id=chat_id,
            title=title,
            description=description,
            payload=f"fiat_pack_{GENERATION_PACK_SIZE}",
            provider_token=AMMER_PAY_TOKEN,
            currency=AMMER_PAY_CURRENCY,
            prices=[LabeledPrice(title, GENERATION_PACK_USD_CENTS)],
        )


async def precheckout_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.pre_checkout_query
    # No inventory/stock to check for a digital generation pack or subscription \u2014 always approve.
    await query.answer(ok=True)


async def successful_payment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    payment = update.message.successful_payment
    user = update.effective_user

    if payment.invoice_payload == "attestly_pro_monthly":
        log_payment(user.id, user.username, "subscription", payment.currency, payment.total_amount)
        new_until = extend_subscription(user.id, days=30)
        until_str = datetime.fromisoformat(new_until).strftime("%b %d, %Y")
        await update.message.reply_text(
            f"\u2b50 Welcome to Attestly Pro! Unlimited generations active until {until_str}. "
            "This won't auto-renew \u2014 run /upgrade again in 30 days to continue."
        )
    else:
        log_payment(user.id, user.username, "generation_pack", payment.currency, payment.total_amount)
        add_purchased_generations(user.id, GENERATION_PACK_SIZE)
        method = "Stars" if payment.currency == "XTR" else payment.currency
        await update.message.reply_text(
            f"\u2705 Payment received ({method}) \u2014 {GENERATION_PACK_SIZE} generations added. "
            "Check /status or run /generate now."
        )


# ---------------------------------------------------------------------------
# /upgrade
# ---------------------------------------------------------------------------


async def send_upgrade_invoice(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    await context.bot.send_invoice(
        chat_id=chat_id,
        title="Attestly Pro (30 Days)",
        description=(
            f"Unlimited Annex IV documentation for one AI system, for 30 days "
            f"({SUBSCRIPTION_PRICE_STARS} Stars, one-time \u2014 not auto-renewing; "
            f"run /upgrade again after 30 days to continue). Multiple AI systems "
            f"need separate plans."
        ),
        payload="attestly_pro_monthly",
        provider_token="",  # Stars only
        currency="XTR",
        prices=[LabeledPrice("Attestly Pro (30 Days)", SUBSCRIPTION_PRICE_STARS)],
    )


async def upgrade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_upgrade_invoice(context, update.effective_chat.id)


async def upgrade_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await send_upgrade_invoice(context, query.message.chat_id)


# ---------------------------------------------------------------------------
# /adminpanel and /grantpro (bot-owner only, not tied to any one group)
# ---------------------------------------------------------------------------


def is_bot_owner(user_id: int) -> bool:
    return user_id in BOT_OWNER_IDS


async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_bot_owner(update.effective_user.id):
        return  # silent - don't reveal this command exists to non-owners
    stats = get_admin_stats()

    earnings_lines = []
    for row in stats["earnings"]:
        if row["currency"] == "XTR":
            earnings_lines.append(f"\u2b50 {row['total']} Stars total ({row['n']} payments)")
        else:
            earnings_lines.append(
                f"\U0001f4b3 {row['total']/100:.2f} {row['currency']} total ({row['n']} payments)"
            )
    earnings_text = "\n".join(earnings_lines) if earnings_lines else "No payments yet"

    user_lines = []
    for u in stats["users"]:
        handle = f"@{u['username']}" if u["username"] else f"id:{u['telegram_id']}"
        subbed = ""
        if u["subscription_until"]:
            try:
                if datetime.fromisoformat(u["subscription_until"]) > datetime.now(timezone.utc):
                    subbed = " \u2b50Pro"
            except Exception:
                pass
        user_lines.append(
            f"{handle} (id {u['telegram_id']}) \u2014 {u['generations_used']} used, "
            f"+{u['purchased_generations']} bought{subbed}"
        )
    users_text = "\n".join(user_lines) if user_lines else "No users yet"

    text = (
        f"\U0001f4ca Attestly Admin Panel\n\n"
        f"Total users: {stats['total_users']}\n"
        f"Active Pro subscribers: {stats['pro_count']}\n\n"
        f"Earnings\n{earnings_text}\n\n"
        f"Users (most recent 50)\n{users_text}\n\n"
        f"To grant Pro manually: /grantpro @username or /grantpro <telegram_id> [days]"
    )
    # Telegram messages cap at 4096 chars; trim gracefully if the user list gets long.
    if len(text) > 4000:
        text = text[:3990] + "\n\u2026(truncated)"
    await update.message.reply_text(text)


async def grant_pro(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_bot_owner(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Usage: /grantpro @username [days] or /grantpro <telegram_id> [days]")
        return

    target_arg = context.args[0]
    days = 30
    if len(context.args) > 1 and context.args[1].isdigit():
        days = int(context.args[1])

    if target_arg.startswith("@"):
        target_id = find_user_by_username(target_arg)
        if target_id is None:
            await update.message.reply_text(
                f"Couldn't find {target_arg} \u2014 they need to have messaged the bot at least "
                "once (e.g. /start) before I have their account on file."
            )
            return
    elif target_arg.isdigit():
        target_id = int(target_arg)
        upsert_user(target_id, None)  # ensure a row exists so the grant has somewhere to land
    else:
        await update.message.reply_text("Give me a @username or a numeric Telegram ID.")
        return

    new_until = extend_subscription(target_id, days=days)
    until_str = datetime.fromisoformat(new_until).strftime("%b %d, %Y")
    await update.message.reply_text(f"Granted Pro to {target_arg} until {until_str} ({days} days).")
    try:
        await context.bot.send_message(
            target_id,
            f"\u2b50 You've been upgraded to Attestly Pro (unlimited generations) until {until_str}."
        )
    except Exception:
        pass  # they may not have started a chat with the bot; grant still applies


# ---------------------------------------------------------------------------
# Command menu (native Telegram "/" autocomplete), scoped by role
# ---------------------------------------------------------------------------

PUBLIC_COMMANDS = [
    BotCommand("start", "Main menu"),
    BotCommand("riskcheck", "Free EU AI Act risk check"),
    BotCommand("generate", "Draft an Annex IV section from a trace file"),
    BotCommand("status", "Your plan and usage"),
    BotCommand("buy", "Buy extra generations"),
    BotCommand("upgrade", "Upgrade to Pro (unlimited)"),
    BotCommand("help", "Show the main menu again"),
]

GROUP_ADMIN_COMMANDS = PUBLIC_COMMANDS + [
    BotCommand("ban", "Reply to a message to ban that user"),
    BotCommand("unban", "Unban a user by reply or ID"),
    BotCommand("kick", "Remove a user (not a permanent ban)"),
    BotCommand("mute", "Silence a user, optionally timed"),
    BotCommand("unmute", "Restore a muted user's permissions"),
    BotCommand("promote", "Make a user a group admin"),
    BotCommand("demote", "Remove a user's admin rights"),
    BotCommand("pin", "Pin a replied-to message"),
    BotCommand("unpin", "Unpin a message"),
    BotCommand("purge", "Bulk-delete messages"),
    BotCommand("cleanservice", "Toggle auto-cleanup of clutter"),
    BotCommand("announce", "Post an update to the Attestly channel"),
]

OWNER_COMMANDS = GROUP_ADMIN_COMMANDS + [
    BotCommand("adminpanel", "Bot stats, earnings, users"),
    BotCommand("grantpro", "Manually grant a user Pro"),
]


async def setup_command_menus(app: Application):
    await app.bot.set_my_commands(PUBLIC_COMMANDS, scope=BotCommandScopeDefault())
    await app.bot.set_my_commands(GROUP_ADMIN_COMMANDS, scope=BotCommandScopeAllChatAdministrators())
    for owner_id in BOT_OWNER_IDS:
        try:
            await app.bot.set_my_commands(OWNER_COMMANDS, scope=BotCommandScopeChat(chat_id=owner_id))
        except Exception as e:
            log.warning(f"couldn't set owner command menu for {owner_id}: {e}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(setup_command_menus).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("upgrade", upgrade))
    app.add_handler(CommandHandler("generate", generate_start))
    app.add_handler(CommandHandler("ban", ban_user))
    app.add_handler(CommandHandler("unban", unban_user_cmd))
    app.add_handler(CommandHandler("kick", kick_user))
    app.add_handler(CommandHandler("promote", promote_user))
    app.add_handler(CommandHandler("demote", demote_user))
    app.add_handler(CommandHandler("mute", mute_user))
    app.add_handler(CommandHandler("unmute", unmute_user))
    app.add_handler(CommandHandler("pin", pin_message))
    app.add_handler(CommandHandler("unpin", unpin_message))
    app.add_handler(CommandHandler("announce", announce))
    app.add_handler(CommandHandler("purge", purge_messages))
    app.add_handler(CommandHandler("cleanservice", cleanservice_toggle))
    app.add_handler(CommandHandler("adminpanel", admin_panel))
    app.add_handler(CommandHandler("grantpro", grant_pro))
    app.add_handler(CommandHandler("buy", buy))
    app.add_handler(CallbackQueryHandler(buy_callback, pattern="^buy_"))
    app.add_handler(CallbackQueryHandler(menu_admin_button, pattern="^menu_admin$"))
    app.add_handler(CallbackQueryHandler(status_button, pattern="^menu_status$"))
    app.add_handler(CallbackQueryHandler(generate_button, pattern="^menu_generate$"))
    app.add_handler(CallbackQueryHandler(upgrade_button, pattern="^menu_upgrade$"))
    app.add_handler(CallbackQueryHandler(buy_button, pattern="^menu_buy$"))
    app.add_handler(PreCheckoutQueryHandler(precheckout_callback))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_callback))
    app.add_handler(MessageHandler(filters.Document.ALL, generate_receive_file))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_new_members))
    app.add_handler(
        MessageHandler(
            filters.StatusUpdate.ALL & ~filters.StatusUpdate.NEW_CHAT_MEMBERS,
            clean_other_service_messages,
        )
    )
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.GROUPS, group_message_router)
    )

    riskcheck_conv = ConversationHandler(
        entry_points=[
            CommandHandler("riskcheck", riskcheck_start),
            CallbackQueryHandler(riskcheck_start_button, pattern="^menu_riskcheck$"),
        ],
        states={
            Q_PROHIBITED: [CallbackQueryHandler(q_prohibited)],
            Q_HIGH_RISK: [CallbackQueryHandler(q_high_risk)],
            Q_GPAI: [CallbackQueryHandler(q_gpai)],
            Q_TRANSPARENCY: [CallbackQueryHandler(q_transparency)],
        },
        fallbacks=[CommandHandler("cancel", riskcheck_cancel)],
    )
    app.add_handler(riskcheck_conv)

    if WATCH_URLS and ANNOUNCE_CHANNEL_ID:
        app.job_queue.run_repeating(
            check_for_updates, interval=WATCH_INTERVAL_HOURS * 3600, first=60
        )
        log.info(f"Site watcher enabled for {WATCH_URLS} -> {ANNOUNCE_CHANNEL_ID}")
    else:
        log.info("Site watcher disabled (no WATCH_URLS configured yet)")

    log.info("Attestly bot starting (polling)...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
