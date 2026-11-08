"""
Entry point: `python run.py`

Runs the Attestly bot with the persistent bottom keyboard menu (menu.py) and
the public updates feed (changelog.py). bot.py itself is unchanged: bot.main()
looks names up at call time, so we swap in the menu-aware versions first, then
hand over to bot.main().

On hosts that give the process a PORT (e.g. Render web services) it also
serves /health (for the host and uptime monitors) and /github-webhook (the
updates feed). Without PORT it does nothing extra.
"""

import logging
import os
import threading
from http.server import ThreadingHTTPServer

import bot
import changelog
import menu

# Telegram bot tokens are embedded in API URLs, which httpx logs at INFO level.
# Keep those request lines out of the logs.
logging.getLogger("httpx").setLevel(logging.WARNING)


def _start_web_server():
    port = int(os.environ.get("PORT") or 0)
    if not port:
        return
    server = ThreadingHTTPServer(("0.0.0.0", port), changelog.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()


# The updates channel is for changelog posts only: keep it out of /announce
# and site-watch broadcasts even though the bot is an admin there.
def _excluded_ids():
    ids = set(changelog.EXTRA_EXCLUDED_IDS)
    live_id = changelog.channel_chat_id()
    if live_id is not None:
        ids.add(live_id)
    return ids


_orig_targets = bot.get_broadcast_targets
_orig_list = bot.list_all_broadcast_channels
_orig_upsert = bot.upsert_broadcast_channel


def _targets():
    blocked = _excluded_ids()
    return [r for r in _orig_targets() if r["chat_id"] not in blocked]


def _list_all():
    blocked = _excluded_ids()
    return [r for r in _orig_list() if r["chat_id"] not in blocked]


def _upsert(chat_id, title, chat_type, active):
    if chat_id in _excluded_ids():
        return
    _orig_upsert(chat_id, title, chat_type, active)


bot.get_broadcast_targets = _targets
bot.list_all_broadcast_channels = _list_all
bot.upsert_broadcast_channel = _upsert

_original_setup = bot.setup_command_menus


async def _setup_with_menu(app):
    await _original_setup(app)
    menu.install(app)


bot.setup_command_menus = _setup_with_menu
bot.start = menu.start
bot.help_cmd = menu.start

if __name__ == "__main__":
    _start_web_server()
    bot.main()
