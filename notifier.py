"""
Sends messages to Telegram.

Everything inserted into a message is HTML-escaped, because a subject line
containing & or < would otherwise be rejected by Telegram and abort the run.
Long messages are split, and failures are reported rather than raised, so one
bad message can never stop the rest of a run.
"""

import html
import time

import requests

import config

TELEGRAM_MAX_CHARS = 4096
CHUNK_SIZE = 3800
MAX_ATTEMPTS = 3

CATEGORY_EMOJI = {
    "tender_or_business_critical": "\U0001F4CB",  # clipboard
    "needs_reply": "✍️",  # writing hand
    "marketing_or_spam": "\U0001F4E2",  # loudspeaker
    "routine_fyi": "\U0001F4C4",  # page
}


def esc(value):
    """Make any text safe to place inside a Telegram HTML message."""
    return html.escape(str(value if value is not None else ""), quote=False)


def _split(text):
    """Break a long message on line boundaries so nothing is silently cut off."""
    if len(text) <= TELEGRAM_MAX_CHARS:
        return [text]
    chunks, current = [], ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > CHUNK_SIZE:
            if current:
                chunks.append(current)
            current = line[:CHUNK_SIZE]
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    return chunks


def send_message(text, chat_id=None):
    """Send one message (splitting if needed). Returns True if it all went out."""
    try:
        token = config.require_env("TELEGRAM_BOT_TOKEN")
        target = chat_id or config.require_env("TELEGRAM_CHAT_ID")
        target = "".join(ch for ch in str(target) if ch.isdigit() or ch == "-") or target
    except RuntimeError as exc:
        print(f"[telegram] not configured: {exc}")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    ok = True

    for chunk in _split(text):
        for attempt in range(MAX_ATTEMPTS):
            try:
                response = requests.post(
                    url,
                    json={
                        "chat_id": target,
                        "text": chunk,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True,
                    },
                    timeout=20,
                )
                if response.status_code == 429:
                    # Telegram tells us exactly how long to wait.
                    retry_after = 2
                    try:
                        retry_after = int(response.json()["parameters"]["retry_after"])
                    except Exception:
                        pass
                    time.sleep(min(retry_after + 1, 30))
                    continue
                if response.status_code == 400:
                    # Almost always a formatting problem — resend as plain text
                    # so the content still reaches its destination.
                    plain = requests.post(
                        url,
                        json={"chat_id": target, "text": _plain(chunk), "disable_web_page_preview": True},
                        timeout=20,
                    )
                    if plain.ok:
                        break
                    print(f"[telegram] rejected: {plain.text[:200]}")
                    ok = False
                    break
                response.raise_for_status()
                break
            except requests.RequestException as exc:
                if attempt == MAX_ATTEMPTS - 1:
                    print(f"[telegram] send failed: {exc}")
                    ok = False
                else:
                    time.sleep(2 ** attempt)
    return ok


def _plain(text):
    """Strip the formatting tags we add, for the plain-text retry."""
    for tag in ("<b>", "</b>", "<i>", "</i>", "<code>", "</code>"):
        text = text.replace(tag, "")
    return html.unescape(text)


def send_alert(company, sender, subject, summary, category):
    emoji = CATEGORY_EMOJI.get(category, "\U0001F4E7")
    text = (
        f"{emoji} <b>{esc(company)}</b>\n"
        f"From: {esc(sender)}\n"
        f"Subject: {esc(subject)}\n"
        f"{esc(summary)}"
    )
    return send_message(text)
