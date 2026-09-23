"""
Who is allowed to use the bot, and a record of who asked what.

Layers, outermost first:
  1. The webhook secret, so only Telegram can post to the endpoint at all.
  2. An allow-list of chat ids, so only named people get answers.
  3. A per-person rate limit, so an approved account cannot hammer the mailboxes.
Anything from an unknown account is refused, logged, and reported to the owner.
"""

import hmac
import json
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone

import config

# Most questions one person may ask per minute.
MAX_QUESTIONS_PER_MINUTE = 10

# How long a chat stays unlocked after the passcode is accepted.
UNLOCK_HOURS = 24

_SESSION_FILE = "sessions.json"

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


# --- Passcode -----------------------------------------------------------
# An optional fourth layer. The allow-list already stops strangers; this also
# covers the case where someone picks up an unlocked phone that belongs to an
# approved person. Set BOT_PASSCODE to switch it on.


def passcode():
    return config.env("BOT_PASSCODE", "").strip()


def passcode_required():
    return bool(passcode())


def _load_sessions():
    path = config.state_path(_SESSION_FILE)
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_sessions(sessions):
    path = config.state_path(_SESSION_FILE)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(sessions, f, indent=2)
    except OSError as exc:
        print(f"[warn] could not save sessions: {exc}")


def is_unlocked(chat_id):
    """True when this chat has entered the passcode recently enough."""
    if not passcode_required():
        return True
    key = normalise_id(chat_id)
    with _lock:
        sessions = _load_sessions()
        expires = sessions.get(key, 0)
    return time.time() < expires


def try_unlock(chat_id, text):
    """Accept the passcode. Compared in constant time so it cannot be guessed by timing."""
    supplied = (text or "").strip()
    if not hmac.compare_digest(supplied, passcode()):
        return False
    key = normalise_id(chat_id)
    with _lock:
        sessions = _load_sessions()
        sessions[key] = time.time() + UNLOCK_HOURS * 3600
        # Drop anything already expired while we are here.
        now = time.time()
        sessions = {k: v for k, v in sessions.items() if v > now}
        _save_sessions(sessions)
    return True


def lock(chat_id):
    """End this chat's session immediately."""
    key = normalise_id(chat_id)
    with _lock:
        sessions = _load_sessions()
        sessions.pop(key, None)
        _save_sessions(sessions)


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
    if not passcode_required():
        warnings.append(
            "BOT_PASSCODE is not set — anyone holding an approved person's unlocked "
            "phone could ask the bot questions."
        )
    if not allowed_chat_ids():
        warnings.append(
            "No TELEGRAM_CHAT_ID or TELEGRAM_ALLOWED_CHAT_IDS set — the bot will answer nobody."
        )
    return warnings
