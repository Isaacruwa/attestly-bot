# Attestly Telegram Bot

Telegram companion bot for [Attestly](https://www.attestly.online): EU AI Act
Annex IV documentation generated from AI agent traces.

## What it does
- Persistent bottom-of-screen menu: Risk Check, Generate, Status, Upgrade to Pro, Admin Commands, Website
- Free EU AI Act risk check, Annex IV drafting from a trace file, Pro plan and generation packs (Telegram Stars)
- Group toolkit: keyword answers, link/spam filter, ban/mute/kick/pin/purge and more
- Owner tools: `/adminpanel`, `/grantpro`, `/announce` (text or media to every registered chat)
- Live updates feed: every push to `main` is posted as a few short lines to the public updates channel

## Files
- `run.py` - entry point (`python run.py`); wires everything together and serves `/health` and `/github-webhook`
- `bot.py` - the bot itself
- `menu.py` - bottom keyboard menu
- `changelog.py` - updates feed (commit subjects only; anything secret-looking becomes "Security update")
- `faq_data.py` - keyword answers

## Configuration (environment variables)
- `TELEGRAM_BOT_TOKEN`, `DATABASE_URL` (Postgres) - required
- `AMMER_PAY_TOKEN` - optional card payments
- `GITHUB_WEBHOOK_SECRET` - required for the updates feed
- `CHANGELOG_CHANNEL` (default `@Botfixed`), `CHANGELOG_BOT_NAMES` - optional

Never commit secrets; set them on the host.
