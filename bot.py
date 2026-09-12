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
import json
import logging
import sqlite3
from datetime import datetime, timezone

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    ConversationHandler,
    filters,
)

from docx import Document

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
    "/upgrade \\- see paid plans on attestly\\.online\n"
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
    app.add_handler(MessageHandler(filters.Document.ALL, generate_receive_file))

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

    log.info("Attestly bot starting (polling)...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
