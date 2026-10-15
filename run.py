"""
Entry point: `python run.py`

Runs the Attestly bot with the persistent bottom keyboard menu (menu.py).
bot.py itself is unchanged: bot.main() looks up `start`, `help_cmd` and
`setup_command_menus` by name when it builds the app, so we swap in the
menu-aware versions first, then hand over to bot.main().

On hosts that give the process a PORT (e.g. Render web services), it also
serves a tiny /health endpoint so the platform sees an open port and an
uptime monitor has something to ping. Without PORT it does nothing extra.
"""

import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import bot
import menu

# Telegram bot tokens are embedded in API URLs, which httpx logs at INFO level.
# Keep those request lines out of the logs.
logging.getLogger("httpx").setLevel(logging.WARNING)


class _Health(BaseHTTPRequestHandler):
    def _ok(self, with_body):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        if with_body:
            self.wfile.write(b"ok")

    def do_GET(self):
        self._ok(True)

    def do_HEAD(self):  # some uptime monitors probe with HEAD
        self._ok(False)

    def log_message(self, *args):  # keep pings out of the logs
        pass


def _start_health_server():
    port = int(os.environ.get("PORT") or 0)
    if not port:
        return
    server = HTTPServer(("0.0.0.0", port), _Health)
    threading.Thread(target=server.serve_forever, daemon=True).start()


_original_setup = bot.setup_command_menus


async def _setup_with_menu(app):
    await _original_setup(app)
    menu.install(app)


bot.setup_command_menus = _setup_with_menu
bot.start = menu.start
bot.help_cmd = menu.start

if __name__ == "__main__":
    _start_health_server()
    bot.main()
