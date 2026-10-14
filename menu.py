"""
Persistent bottom-of-screen menu for @Attestly_bot.

Uses Telegram's reply keyboard (the button panel that sits above the typing
box and stays put) instead of inline buttons attached to the welcome message.
Tapping a button sends its label as a normal text message, so each label is
routed here to the same logic the slash commands already use.

Only shown in private chats; groups stay clean.
"""

import re

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

import bot

BTN_RISK = "\U0001f50d Risk Check"
BTN_GENERATE = "\U0001f4c4 Generate"
BTN_STATUS = "\U0001f4ca Status"
BTN_PRO = "\u2b50 Upgrade to Pro"
BTN_ADMIN = "\U0001f6e1 Admin Commands"
BTN_SITE = "\U0001f310 Website"

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        [BTN_RISK, BTN_GENERATE],
        [BTN_STATUS, BTN_PRO],
        [BTN_ADMIN, BTN_SITE],
    ],
    resize_keyboard=True,
    is_persistent=True,
    input_field_placeholder="Pick an option, or ask about the EU AI Act\u2026",
)


def _label(text: str):
    return filters.TEXT & filters.ChatType.PRIVATE & filters.Regex(f"^{re.escape(text)}$")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Replaces bot.start / bot.help_cmd: welcome text + persistent menu in DMs."""
    user = update.effective_user
    bot.upsert_user(user.id, user.username)
    if update.effective_chat.type == "private":
        await update.message.reply_markdown_v2(bot.WELCOME, reply_markup=MAIN_KEYBOARD)
    else:
        await update.message.reply_markdown_v2(bot.WELCOME)


async def on_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await bot.send_status(context, update.effective_chat.id, update.effective_user)


async def on_generate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await bot.send_generate_prompt(context, update.effective_chat.id, update.effective_user)


async def on_upgrade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await bot.send_upgrade_invoice(context, update.effective_chat.id)


async def on_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(bot.ADMIN_INFO_TEXT)


async def on_site(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Everything Attestly, in one place:",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("\U0001f310 Open attestly.online", url="https://www.attestly.online")]]
        ),
    )


async def on_risk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["risk_answers"] = {}
    context.user_data.pop("risk_verdict", None)
    await update.message.reply_text(bot.RISKCHECK_Q1, reply_markup=bot.yn_keyboard())
    return bot.Q_PROHIBITED


def install(app):
    """Registers the keyboard-button handlers. Called once at startup."""
    app.add_handler(MessageHandler(_label(BTN_STATUS), on_status))
    app.add_handler(MessageHandler(_label(BTN_GENERATE), on_generate))
    app.add_handler(MessageHandler(_label(BTN_PRO), on_upgrade))
    app.add_handler(MessageHandler(_label(BTN_ADMIN), on_admin))
    app.add_handler(MessageHandler(_label(BTN_SITE), on_site))

    # The risk check is a multi-step conversation, so the keyboard button gets
    # its own entry point into the same question flow the /riskcheck command uses.
    app.add_handler(
        ConversationHandler(
            entry_points=[MessageHandler(_label(BTN_RISK), on_risk)],
            states={
                bot.Q_PROHIBITED: [CallbackQueryHandler(bot.q_prohibited)],
                bot.Q_HIGH_RISK: [CallbackQueryHandler(bot.q_high_risk)],
                bot.Q_GPAI: [CallbackQueryHandler(bot.q_gpai)],
                bot.Q_TRANSPARENCY: [CallbackQueryHandler(bot.q_transparency)],
            },
            fallbacks=[CommandHandler("cancel", bot.riskcheck_cancel)],
        )
    )
