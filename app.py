"""
Web service. Three jobs:

  /run-check         the periodic check, triggered by an external scheduler
  /telegram-webhook  incoming questions from Telegram
  /status            a quick health and activity read-out

Both /run-check and /telegram-webhook answer immediately and do the real work
on a background thread. That matters: the work takes longer than a web request
is allowed to last, and a slow reply makes Telegram resend the same message.

Start command:
    gunicorn app:app --workers 1 --threads 4 --timeout 120
Use a single worker — the run lock and the state file assume one process.
"""

import config  # must be first: this loads .env before anything reads it

import threading
from collections import deque

from flask import Flask, jsonify, request

import auth
import main as monitor
import secretary
from notifier import esc, send_message

app = Flask(__name__)

# Telegram resends an update if it doesn't get a prompt 200. Remembering the
# recent ids means a resend can't trigger the same search twice.
_recent_updates = deque(maxlen=200)
_recent_lock = threading.Lock()


def _normalise_id(value):
    """Compare chat ids by digits only, so a stray space or comma in .env
    cannot silently stop every message from being answered."""
    return "".join(ch for ch in str(value or "") if ch.isdigit() or ch == "-")


def _already_handled(update_id):
    if update_id is None:
        return False
    with _recent_lock:
        if update_id in _recent_updates:
            return True
        _recent_updates.append(update_id)
        return False


def _in_background(target, *args):
    threading.Thread(target=target, args=args, daemon=True).start()


@app.route("/")
def health():
    return "email monitor is alive", 200


@app.route("/status")
def status():
    usable, missing = config.configured_mailboxes()
    return jsonify({
        "mailboxes_watched": len(usable),
        "mailboxes_missing_password": [m["company"] for m in missing],
        "check_interval_minutes": config.setting("poll_interval_minutes", 30),
        "last_run": monitor.last_run(),
    })


@app.route("/run-check")
def run_check():
    # When RUN_TOKEN is set, the scheduler must present it. Without this the
    # endpoint is open to anyone who learns the URL.
    expected = config.env("RUN_TOKEN")
    if expected and request.args.get("token") != expected:
        return "unauthorized", 401

    _in_background(monitor.run_once)
    return "check started", 202


def _handle_question(chat_id, text):
    """Answer one Telegram message. Runs on a background thread."""
    try:
        reply = secretary.handle_message(
            text, notify=lambda note: send_message(note, chat_id=chat_id)
        )
        send_message(reply, chat_id=chat_id)
    except Exception as exc:
        print(f"[webhook error] {type(exc).__name__}: {exc}")
        send_message("That request failed. Try again.", chat_id=chat_id)


@app.route("/telegram-webhook", methods=["POST"])
def telegram_webhook():
    # Layer 1: only Telegram can post here. Without this, anyone who learns the
    # URL could send a fake message claiming to be an approved chat id.
    secret = config.env("TELEGRAM_WEBHOOK_SECRET")
    if secret and request.headers.get("X-Telegram-Bot-Api-Secret-Token") != secret:
        print("[audit] rejected webhook call with a bad or missing secret")
        return "ignored", 200

    update = request.get_json(silent=True) or {}
    message = update.get("message") or update.get("edited_message") or {}
    text = (message.get("text") or "").strip()
    chat_id = (message.get("chat") or {}).get("id")

    if not text or chat_id is None:
        return "ignored", 200

    # Layer 2: only people on the allow-list get answers. Telegram supplies the
    # real sender's id, so this cannot be faked by someone messaging the bot.
    if not auth.is_allowed(chat_id):
        auth.log("refused", message, repr(text[:60]))
        send_message(secretary.PRIVATE_NOTICE, chat_id=chat_id)
        if auth.should_report_stranger(chat_id):
            owner = auth.owner_chat_id()
            if owner:
                send_message(
                    "⚠️ Someone outside the approved list messaged the bot:\n"
                    f"{esc(auth.describe(message))}\n"
                    f"They asked: {esc(text[:120])}\n\n"
                    "They were refused. Add them to TELEGRAM_ALLOWED_CHAT_IDS if they belong.",
                    chat_id=owner,
                )
        return "ignored", 200

    # Layer 3: the passcode, when one is set. This covers an approved person's
    # phone being picked up by someone else.
    if auth.passcode_required() and not auth.is_unlocked(chat_id):
        if auth.try_unlock(chat_id, text):
            auth.log("unlocked", message)
            send_message(
                "🔓 Unlocked for " + str(auth.UNLOCK_HOURS) + " hours. "
                "Delete your passcode message.\n"
                "/lock to end early.",
                chat_id=chat_id,
            )
            send_message(secretary.WELCOME_TEXT, chat_id=chat_id)
        else:
            auth.log("locked-out", message)
            send_message(
                "🔒 Locked. Enter passcode.", chat_id=chat_id
            )
        return "ok", 200

    if text.strip().lower().lstrip("/") == "lock":
        auth.lock(chat_id)
        auth.log("locked", message)
        send_message("🔒 Locked.",
                     chat_id=chat_id)
        return "ok", 200

    # Layer 4: an approved account still cannot hammer the mailboxes.
    if not auth.within_rate_limit(chat_id):
        auth.log("rate-limited", message)
        send_message("Too many requests. Wait a minute.", chat_id=chat_id)
        return "ok", 200

    if _already_handled(update.get("update_id")):
        return "duplicate", 200

    auth.log("question", message, repr(text[:80]))

    # Answer Telegram straight away; the lookup happens in the background.
    _in_background(_handle_question, chat_id, text)
    return "ok", 200


for _warning in auth.startup_warnings():
    print(f"[SECURITY] {_warning}")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(config.env("PORT", "8080")))
