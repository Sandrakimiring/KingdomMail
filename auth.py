"""
Who is allowed to use the bot, and a record of who asked what.

Layers, outermost first:
  1. The webhook secret, so only Telegram can post to the endpoint at all.
  2. An allow-list of chat ids, so only named people get answers.
  3. A per-person rate limit, so an approved account cannot hammer the mailboxes.
Anything from an unknown account is refused, logged, and reported to the owner.
"""

import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone

import config

# Most questions one person may ask per minute.
MAX_QUESTIONS_PER_MINUTE = 10

_lock = threading.Lock()
_recent_questions = defaultdict(deque)
_reported_strangers = set()


def normalise_id(value):
    """Compare ids by digits only, so a stray comma or space in .env is harmless."""
    return "".join(ch for ch in str(value or "") if ch.isdigit() or ch == "-")


def allowed_chat_ids():
    """
    Everyone permitted to ask questions.

    TELEGRAM_ALLOWED_CHAT_IDS holds a comma-separated list. TELEGRAM_CHAT_ID is
    always included, since that is where alerts go.
    """
    ids = set()
    for raw in config.env("TELEGRAM_ALLOWED_CHAT_IDS", "").split(","):
        cleaned = normalise_id(raw)
        if cleaned:
            ids.add(cleaned)
    owner = normalise_id(config.env("TELEGRAM_CHAT_ID", ""))
    if owner:
        ids.add(owner)
    return ids


def owner_chat_id():
    return normalise_id(config.env("TELEGRAM_CHAT_ID", ""))


def is_allowed(chat_id):
    return normalise_id(chat_id) in allowed_chat_ids()


def describe(message):
    """A readable label for whoever sent a message, for the log and for alerts."""
    user = message.get("from") or {}
    name = " ".join(part for part in (user.get("first_name"), user.get("last_name")) if part)
    handle = user.get("username")
    chat_id = (message.get("chat") or {}).get("id")
    label = name or "unknown"
    if handle:
        label += f" (@{handle})"
    return f"{label} [id {chat_id}]"


def within_rate_limit(chat_id):
    """False once someone exceeds the per-minute allowance."""
    key = normalise_id(chat_id)
    now = time.monotonic()
    with _lock:
        seen = _recent_questions[key]
        while seen and now - seen[0] > 60:
            seen.popleft()
        if len(seen) >= MAX_QUESTIONS_PER_MINUTE:
            return False
        seen.append(now)
        return True


def should_report_stranger(chat_id):
    """True the first time an unknown account makes contact, so the owner hears once."""
    key = normalise_id(chat_id)
    with _lock:
        if key in _reported_strangers:
            return False
        _reported_strangers.add(key)
        return True


def log(event, message, detail=""):
    """One audit line per request. Shows up in the host's logs."""
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[audit {stamp}] {event}: {describe(message)} {detail}".rstrip())


def startup_warnings():
    """Configuration gaps worth shouting about when the service starts."""
    warnings = []
    if not config.env("TELEGRAM_WEBHOOK_SECRET"):
        warnings.append(
            "TELEGRAM_WEBHOOK_SECRET is not set — anyone who learns the webhook URL "
            "could post fake messages to it. Set it and pass it to setWebhook."
        )
    if not config.env("RUN_TOKEN"):
        warnings.append(
            "RUN_TOKEN is not set — /run-check can be triggered by anyone who learns the URL."
        )
    if not allowed_chat_ids():
        warnings.append(
            "No TELEGRAM_CHAT_ID or TELEGRAM_ALLOWED_CHAT_IDS set — the bot will answer nobody."
        )
    return warnings
