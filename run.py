"""
Entry point: `python run.py`

Runs the Attestly bot with the persistent bottom keyboard menu (menu.py).
bot.py itself is unchanged: bot.main() looks up `start`, `help_cmd` and
`setup_command_menus` by name when it builds the app, so we swap in the
menu-aware versions first, then hand over to bot.main().
"""

import logging

import bot
import menu

# Telegram bot tokens are embedded in API URLs, which httpx logs at INFO level.
# Keep those request lines out of the logs.
logging.getLogger("httpx").setLevel(logging.WARNING)

_original_setup = bot.setup_command_menus


async def _setup_with_menu(app):
    await _original_setup(app)
    menu.install(app)


bot.setup_command_menus = _setup_with_menu
bot.start = menu.start
bot.help_cmd = menu.start

if __name__ == "__main__":
    bot.main()
