# Attestly Telegram Bot

Free-tier lead-gen + upsell bot for attestly.online. No Attestly API needed —
state lives in a local SQLite file.

## Commands
- `/start` — welcome
- `/riskcheck` — free 4-question EU AI Act risk classifier (lead magnet)
- `/generate` — upload a trace `.json` file → get a drafted Annex IV
  paragraph back as a `.docx` (free for 3 uses per user, then points to pricing)
- `/status` — shows saved risk result + free generations remaining
- `/upgrade` — links to attestly.online/pricing
- `/ban` — (group admins only) reply to a user's message to ban them
- `/unban` — (group admins only) reply to their message, or `/unban <user_id>` (needed
  since a banned user leaves no recent message to reply to)
- `/kick` — (group admins only) reply to remove someone without a permanent ban — they
  can rejoin via invite link
- `/mute` — (group admins only) reply to silence someone; optionally `/mute 10m`, `/mute 2h`,
  `/mute 1d` for a timed mute, or no duration for indefinite (until `/unmute`)
- `/unmute` — (group admins only) reply to restore a muted user's permissions
- `/promote` — (group admins only) reply to a user's message to make them a group admin
- `/demote` — (group admins only) reply to remove someone's admin rights
- `/pin` — (group admins only) reply to a message to pin it (add `silent` to pin quietly)
- `/unpin` — (group admins only) reply to unpin a specific message, or run with no reply
  to unpin the most recent pin
- `/announce <text>` — (channel admins only, DM the bot) posts an update to the Attestly
  Telegram channel with auto-attached hashtags for discoverability

## Channel announcements

Two ways updates reach the Telegram channel (default: `@AI_Act_Compliance`, set via
`ANNOUNCE_CHANNEL_ID`):

1. **Manual, works today**: any admin of that channel can DM the bot `/announce <text>`
   and it posts immediately, with hashtags like `#EUAIAct #AICompliance` (plus topic-specific
   ones like `#AnnexIV` or `#GPAI` when the text matches those subjects) so the post is
   discoverable via Telegram's in-app search.
2. **Automatic, dormant until you have content to watch**: set `WATCH_URLS` (comma-separated
   page URLs, e.g. a future `attestly.online/blog` or `/changelog`) and the bot checks them
   every `WATCH_INTERVAL_HOURS` (default 6) for content changes, posting an alert to the
   channel when something changes. **attestly.online has no blog/changelog yet**, so this is
   off by default — there's nothing meaningful to watch until one exists. The first check on
   any URL just records a baseline (no post), so adding a URL won't trigger a false alert.

For either to work, the bot needs to be an **admin of the channel** with **Post Messages**
permission — same "human has to grant it" rule as groups (Administrators → Add Admin →
select the bot).

## Group features (no LLM — fixed keyword matching)

Add the bot to a group and it will:
- **Welcome** new members with a short intro
- **Answer questions automatically** by matching keywords against a fixed knowledge base
  (`faq_data.py`) covering Attestly and general EU AI Act topics — no AI API calls, no cost
- **Delete links** to any domain other than attestly.online, unless the sender is a group admin
- **Delete spam**: messages with more than 2 links, or a user repeating the same message
  within 30 seconds
- **Never bans/promotes/deletes for an existing admin** — admin status is always checked
  live against Telegram, never a hardcoded list

### Required: grant the bot admin rights in the group

Telegram doesn't let a bot grant itself admin rights — a human admin has to do this once
per group:

1. Open the group → group name → **Administrators** → **Add Admin** → select the bot
2. Enable at minimum:
   - **Delete messages** (for link/spam filtering)
   - **Ban users** (for `/ban`, `/kick`, `/mute`, `/unmute`)
   - **Add new admins** (for `/promote`/`/demote` — Telegram requires this specific permission)
   - **Pin messages** (for `/pin`/`/unpin`)
3. Save

Without these, the bot still welcomes members and answers FAQ questions, but the admin
commands above will reply with an error, and it won't be able to delete spam/link messages.

### Adding or editing FAQ answers

Edit `faq_data.py` — each topic is a list of trigger keywords plus a fixed answer. No
other code changes needed; new topics are picked up automatically on the next restart.

## Run it locally

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env   # then fill in your real token(s)
export $(cat .env | xargs)   # or use python-dotenv if you prefer

python bot.py
```

You already have a bot token from @BotFather (the one linked in Composio) —
use that same token here. `ANTHROPIC_API_KEY` is only required if you want
`/generate` to work; without it, the bot still runs and just tells users
that feature isn't configured yet.

## Deploying so it runs 24/7

This script uses long-polling (`run_polling`), so it just needs to stay
running somewhere — no public URL or webhook required. Cheapest options:

1. **Railway.app** or **Render.com** — push this folder to a GitHub repo,
   create a new "Background Worker" / "Worker" service pointing at
   `python bot.py`, add the two environment variables in their dashboard.
   Both have free tiers sufficient for a bot like this.
2. **A small VPS** (e.g. a $5/mo box) — run it under `systemd` or `tmux`/`screen`
   so it survives reboots and disconnects.
3. **Fly.io** — similar to Railway, deploy as a worker process.

Composio's Telegram connection (which you already set up) is separate from
this — it's useful for testing sends manually, but this standalone script is
what makes the bot respond automatically, all the time, to any user.

## Monetization loop this implements

1. User runs `/riskcheck` for free → gets a classification → sees a nudge
   toward `/generate` and the full site.
2. `/generate` gives real value (an actual drafted Annex IV section) for
   free up to `FREE_GENERATIONS` (default 3, set in `bot.py`).
3. Once they hit the limit, every `/generate` attempt points straight to
   `attestly.online/pricing`.

## Notes / next steps

- The risk-check logic here is a simplified 4-question version of the EU AI
  Act criteria (prohibited practices → Annex III high-risk areas → GPAI →
  transparency obligations). It's meant as a fast lead magnet, not a
  substitute for the fuller checker on your site.
- If you later build a real Attestly API, swap the SQLite calls here for API
  calls so bot users and web users share the same account/data.
- Consider adding `/link <email>` once you have an API, so a Telegram user's
  free-generation count and risk result tie back to their real Attestly
  account instead of living only in this bot's local database.
