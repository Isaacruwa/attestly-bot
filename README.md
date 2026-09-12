# Attestly Telegram Bot

Free-tier lead-gen + upsell bot for attestly.online. No Attestly API needed — state lives in a local SQLite file.

## Commands
- `/start` — welcome
- `/riskcheck` — free 4-question EU AI Act risk classifier (lead magnet)
- `/generate` — upload a trace `.json` file (currently disabled without an API key — see note below)
- `/status` — shows saved risk result + free generations remaining
- `/upgrade` — links to attestly.online/pricing

## Run it locally

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env   # then fill in your real token
export $(cat .env | xargs)

python bot.py
```

## Deploying so it runs 24/7 (Railway)

1. Push this repo to GitHub (already done).
2. Create a Railway project → Deploy from GitHub repo → select this repo.
3. Settings → Deploy → Custom Start Command: `python bot.py`
4. Variables tab → add `TELEGRAM_BOT_TOKEN` (your @BotFather token).
5. Save — Railway redeploys automatically. Check Deployments logs for
   "Attestly bot starting (polling)..." to confirm it's live.

## Note on /generate

This build intentionally ships without Anthropic wired in (no API key
required to run). `/generate` will politely tell users the feature isn't
configured yet. To enable it later: add `ANTHROPIC_API_KEY` as an
environment variable and reinstall with `anthropic` added back to
requirements.txt.

## Monetization loop

1. `/riskcheck` is the free lead magnet.
2. `/generate` would give real value for free up to a limit, then
3. point to `attestly.online/pricing` once the limit is hit.
