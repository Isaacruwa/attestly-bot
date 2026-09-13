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
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatPermissions
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ChatMemberHandler,
    ContextTypes,
    ConversationHandler,
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
    return conn


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


# ---------------------------------------------------------------------------
# /start, /help
# ---------------------------------------------------------------------------

WELCOME = (
    "Welcome to *Attestly* \\- EU AI Act compliance, generated from what your "
    "AI agents already do\\.\n\n"
    "Here's what I can do:\n"
    "/riskcheck \\- free EU AI Act risk classification for your AI system\n"
    "/generate \\- upload a trace file, get a drafted Annex IV section back as a docx\n"
    "/status \\- see your saved risk result and remaining free generations\n"
    "/upgrade \\- see paid plans on attestly\\.online\n\n"
    "In groups, I also answer questions about Attestly and the EU AI Act "
    "automatically, filter non\\-attestly\\.online links, and \\(for admins\\) "
    "support /ban, /unban, /kick, /mute, /unmute, /promote, /demote, /pin, "
    "and /unpin by replying to a user's message\\.\n\n"
    "/announce \\(channel admins only\\) \\- post an update to the Attestly channel\\.\n"
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    upsert_user(user.id, user.username)
    await update.message.reply_markdown_v2(WELCOME)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_markdown_v2(WELCOME)


# ---------------------------------------------------------------------------
# /riskcheck - conversational EU AI Act risk classifier
# ---------------------------------------------------------------------------


def yn_keyboard():
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Yes", callback_data="yes"),
          InlineKeyboardButton("No", callback_data="no")]]
    )


async def riskcheck_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["risk_answers"] = {}
    await update.message.reply_text(
        "EU AI Act Risk Check \u2014 4 quick questions.\n\n"
        "1) Does your system do any of the following: subliminal manipulation, "
        "social scoring, real-time remote biometric identification by law "
        "enforcement in public spaces, or emotion recognition in workplaces "
        "or schools?",
        reply_markup=yn_keyboard(),
    )
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


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    upsert_user(user.id, user.username)
    row = get_user(user.id)
    risk = row["risk_result"] if row and row["risk_result"] else "not checked yet \u2014 run /riskcheck"
    used = row["generations_used"] if row else 0
    remaining = max(FREE_GENERATIONS - used, 0)
    await update.message.reply_text(
        f"Risk classification: {risk}\n"
        f"Free doc-generations remaining: {remaining}/{FREE_GENERATIONS}\n\n"
        f"Full account + history: {LOGIN_URL}"
    )


# ---------------------------------------------------------------------------
# /generate - upload trace JSON -> drafted Annex IV paragraph -> docx
# ---------------------------------------------------------------------------


async def generate_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    upsert_user(user.id, user.username)
    row = get_user(user.id)
    used = row["generations_used"] if row else 0

    if used >= FREE_GENERATIONS:
        await update.message.reply_text(
            "You've used all your free doc-generations.\n"
            f"See paid plans here: {PRICING_URL}"
        )
        return

    if not ANTHROPIC_API_KEY or Anthropic is None:
        await update.message.reply_text(
            "Doc generation isn't configured on this bot yet (missing API key). "
            f"In the meantime, use the full tool at {LOGIN_URL}"
        )
        return

    await update.message.reply_text(
        "Send me a trace file as a JSON document (OpenTelemetry, LangSmith, "
        "AgentOps export, or plain normalized JSON with tool_call / "
        "human_intervention / error / deployment_change events)."
    )


async def generate_receive_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    row = get_user(user.id)
    used = row["generations_used"] if row else 0
    if used >= FREE_GENERATIONS:
        await update.message.reply_text(
            f"You've used all your free doc-generations. See plans: {PRICING_URL}"
        )
        return

    doc = update.message.document
    if not doc or not (doc.file_name or "").lower().endswith(".json"):
        await update.message.reply_text("Please send a .json trace file.")
        return

    tg_file = await doc.get_file()
    raw = await tg_file.download_as_bytearray()
    try:
        trace_events = json.loads(raw.decode("utf-8"))
    except Exception:
        await update.message.reply_text("That file isn't valid JSON \u2014 please check and resend.")
        return

    await update.message.reply_text("Drafting your Annex IV section\u2026 one moment.")

    try:
        drafted = draft_annex_iv_section(trace_events)
    except Exception as e:
        log.exception("draft failed")
        await update.message.reply_text(f"Couldn't draft that: {e}")
        return

    docx_path = build_docx(drafted, trace_events)
    remaining_after = FREE_GENERATIONS - increment_generations(user.id)

    with open(docx_path, "rb") as f:
        await update.message.reply_document(
            document=f,
            filename="annex_iv_draft.docx",
            caption=(
                f"Drafted section attached. Free generations left: "
                f"{max(remaining_after, 0)}/{FREE_GENERATIONS}. "
                f"Full account + more sections: {LOGIN_URL}"
            ),
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


# ---------------------------------------------------------------------------
# Welcome new members
# ---------------------------------------------------------------------------


async def welcome_new_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.new_chat_members:
        return
    for member in update.message.new_chat_members:
        if member.is_bot:
            continue
        name = member.first_name or member.username or "there"
        await update.message.reply_text(
            f"Welcome, {name}! This group is about Attestly \u2014 EU AI Act compliance "
            "generated from your AI agents' traces.\n\n"
            "Try asking a question (e.g. \"what is annex iv\" or \"how much does it cost\"), "
            "or message @Attestly_bot directly for /riskcheck and /generate."
        )


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
        await update.message.reply_text(f"Banned {target.first_name or target.username}.")
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
        await update.message.reply_text(f"Promoted {target.first_name or target.username} to admin.")
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
        await update.message.reply_text(f"Demoted {target.first_name or target.username}.")
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
        await update.message.reply_text("Unbanned. They can rejoin now.")
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
        await update.message.reply_text(f"Kicked {target.first_name or target.username} (they can rejoin via invite link).")
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
        await update.message.reply_text(f"Muted {target.first_name or target.username}{duration_note}.")
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
        await update.message.reply_text(f"Unmuted {target.first_name or target.username}.")
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
        await update.message.reply_text("Pinned.")
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
        await update.message.reply_text("Unpinned.")
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
# /upgrade
# ---------------------------------------------------------------------------


async def upgrade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"See paid plans here: {PRICING_URL}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

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
    app.add_handler(MessageHandler(filters.Document.ALL, generate_receive_file))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_new_members))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.GROUPS, group_message_router)
    )

    riskcheck_conv = ConversationHandler(
        entry_points=[CommandHandler("riskcheck", riskcheck_start)],
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
