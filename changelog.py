"""
Public updates feed.

GitHub calls POST /github-webhook on every push; we turn the commit subjects
into a few short lines and post them to the public updates channel
(@Botfixed by default). One bot token, one channel, many repos: point any
repo's webhook here and its updates show up in the same feed.

Only commit *subject lines* are ever used - never diffs, file contents or
commit bodies - and anything that looks like (or talks about) a secret is
replaced with a generic "Security update" line.
"""

import hashlib
import hmac
import json
import logging
import os
import re
from http.server import BaseHTTPRequestHandler

import requests

log = logging.getLogger("changelog")

CHANNEL = os.environ.get("CHANGELOG_CHANNEL", "@Botfixed")
MAX_LINES = 6
MAX_TEXT = 80

# Channel ids that must never receive marketing broadcasts (/announce etc.).
# The live id is also resolved from CHANNEL at runtime.
EXTRA_EXCLUDED_IDS = {-1003629547911, -2073629547911}

# repo name -> what to call it in the feed. Add more with the
# CHANGELOG_BOT_NAMES env var, e.g. {"my-repo": "@MyBot"}.
BOT_LABELS = {"attestly-bot": "@Attestly_bot"}
try:
    BOT_LABELS.update(json.loads(os.environ.get("CHANGELOG_BOT_NAMES", "{}")))
except ValueError:
    pass

_SCRUBBERS = [
    re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{25,}"),
    re.compile(r"\b\d{6,}:(?:LIVE|TEST):[A-Za-z0-9_-]+"),
    re.compile(r"postgres(?:ql)?://\S+", re.I),
    re.compile(r"\b(?:sk|pk|rk)[-_][A-Za-z0-9_-]{12,}"),
    re.compile(r"\bnpg_[A-Za-z0-9]+"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}|github_pat_\w+"),
]
_SENSITIVE_WORDS = re.compile(
    r"(token|secret|password|passwd|credential|api[ _-]?key|private key|"
    r"env var|connection string|\.env\b)",
    re.I,
)

_STARTS_FIX = re.compile(r"^\s*(fix|fixed|fixes)\b", re.I)
_STARTS_NEW = re.compile(
    r"^\s*(add|added|adds|new|introduce|introduces|implement|implements|"
    r"create|creates|feat|feature|support)\b",
    re.I,
)
_HAS_FIX = re.compile(
    r"\b(fix|fixed|fixes|bug|crash|crashes|broken|repair|resolve|resolved|regression)\b",
    re.I,
)
_FIX_PREFIX = re.compile(r"^\s*fix(?:ed|es)?\b\s*[:\-]?\s*", re.I)
_NEW_PREFIX = re.compile(
    r"^\s*(?:add(?:ed|s)?|feat(?:ure)?|new|introduce[sd]?|implement(?:s|ed)?)\b\s*[:\-]?\s*",
    re.I,
)
_CONVENTIONAL_PREFIX = re.compile(r"^\s*(?:chore|docs?|refactor|perf|style)\s*[:\-]\s*", re.I)


def _is_sensitive(subject: str) -> bool:
    if _SENSITIVE_WORDS.search(subject):
        return True
    return any(p.search(subject) for p in _SCRUBBERS)


def _tidy(text: str) -> str:
    text = re.split(r";| \u2014 | -- ", text)[0].strip().rstrip(".")
    if len(text) > MAX_TEXT:
        text = text[: MAX_TEXT - 1].rsplit(" ", 1)[0].rstrip(",:;-") + "\u2026"
    return text[:1].upper() + text[1:] if text else text


def summarize(subject: str):
    """One commit subject -> one short feed line (or None to skip it)."""
    subject = (subject or "").strip()
    if not subject or subject.lower().startswith("merge"):
        return None
    if _is_sensitive(subject):
        return "\U0001f512 Security update"
    if _STARTS_FIX.match(subject) or (not _STARTS_NEW.match(subject) and _HAS_FIX.search(subject)):
        text = _tidy(_FIX_PREFIX.sub("", subject, count=1))
        return f"\U0001f41b Fixed: {text}" if text else None
    if _STARTS_NEW.match(subject):
        text = _tidy(_NEW_PREFIX.sub("", subject, count=1))
        return f"\u2728 New: {text}" if text else None
    text = _tidy(_CONVENTIONAL_PREFIX.sub("", subject, count=1))
    return f"\U0001f504 Improved: {text}" if text else None


def build_message(payload: dict):
    repo = payload.get("repository") or {}
    default_branch = repo.get("default_branch") or "main"
    if payload.get("deleted") or payload.get("ref") != f"refs/heads/{default_branch}":
        return None  # only updates that land on the main branch

    commits = payload.get("commits") or []
    if not commits and payload.get("head_commit"):
        commits = [payload["head_commit"]]

    lines = []
    for c in commits:
        subject = (c.get("message") or "").splitlines()[0] if c.get("message") else ""
        line = summarize(subject)
        if line and line not in lines:
            lines.append(line)
    if not lines:
        return None

    extra = len(lines) - MAX_LINES
    lines = lines[:MAX_LINES]
    if extra > 0:
        lines.append(f"\u2026and {extra} more change{'s' if extra > 1 else ''}")

    name = repo.get("name") or "repository"
    label = BOT_LABELS.get(name)
    head = f"\U0001f6e0 {label or name} \u2014 update"
    text = head + "\n\n" + "\n".join(lines)
    if label and label.startswith("@"):
        text += f"\n\nSend /start to {label} to see what changed."
    return text


def post(text: str):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": CHANNEL, "text": text, "disable_web_page_preview": True},
        timeout=15,
    )
    if not r.ok:  # never raise_for_status: its message contains the token URL
        raise RuntimeError(f"telegram returned {r.status_code}")


_cached_channel_id = None


def channel_chat_id():
    """Numeric id of the updates channel (cached once resolved)."""
    global _cached_channel_id
    if _cached_channel_id is not None:
        return _cached_channel_id
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        return None
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{token}/getChat",
            params={"chat_id": CHANNEL},
            timeout=10,
        )
        if r.ok:
            _cached_channel_id = int(r.json()["result"]["id"])
    except Exception:
        pass
    return _cached_channel_id


class Handler(BaseHTTPRequestHandler):
    """GET/HEAD /health for the host + uptime monitor; POST /github-webhook."""

    def _reply(self, code, body=b""):
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self):
        self._reply(200, b"ok")

    def do_HEAD(self):
        self._reply(200)

    def do_POST(self):
        if self.path != "/github-webhook":
            return self._reply(404)
        secret = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
        if not secret:
            return self._reply(503, b"webhook not configured")
        length = int(self.headers.get("Content-Length") or 0)
        if length > 2_000_000:
            return self._reply(413)
        raw = self.rfile.read(length)
        expected = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(self.headers.get("X-Hub-Signature-256", ""), expected):
            return self._reply(401)
        event = self.headers.get("X-GitHub-Event", "")
        if event == "ping":
            return self._reply(200, b"pong")
        if event != "push":
            return self._reply(200, b"ignored")
        try:
            text = build_message(json.loads(raw))
            if text:
                post(text)
            self._reply(200, b"ok")
        except Exception as e:
            log.warning("update post failed: %s", type(e).__name__)
            self._reply(502, b"error")

    def log_message(self, *args):  # keep health pings out of the logs
        pass
