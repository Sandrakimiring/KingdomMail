"""
Answers questions asked over Telegram.

Takes the structured instruction from agent.py, queries the relevant mailboxes
(in parallel, because there are a lot of them) and writes a readable reply.
"""

from concurrent.futures import ThreadPoolExecutor, as_completed

import config
import search
from agent import parse_request
from notifier import esc

MAX_PARALLEL = 8
PER_MAILBOX_LIMIT = 3
MAX_LINES_SHOWN = 12

BOT_NAME = config.setting("bot_name", "Kingdom Group Mailer")

_MAILBOX_COUNT = len(config.mailboxes())

WELCOME_TEXT = (
    f"<b>{BOT_NAME}</b>\n\n"
    f"Watching {_MAILBOX_COUNT} inboxes. Mail arrives here with a summary "
    "and what it needs from you.\n\n"
    "<b>Ask for anything</b>\n"
    "New mail?\n"
    "Anything from KRA?\n"
    "What came in today?\n"
    "Unread for Geowells?\n"
    "Beacon - anything on the tender?\n\n"
    "Name a company to narrow it down.\n"
    "Read-only access. Nothing is sent, replied to or deleted."
)

HELP_TEXT = WELCOME_TEXT

PRIVATE_NOTICE = (
    "Private system. Access is by approval only."
)


def _select_mailboxes(company_hint):
    """Usable mailboxes, narrowed to a company when one was named."""
    usable, _ = config.configured_mailboxes()
    if not company_hint:
        return usable, False
    hint = company_hint.lower()
    matched = [
        mb for mb in usable
        if hint in mb["company"].lower() or hint in mb["email"].lower()
    ]
    if matched:
        return matched, True
    # Nothing matched that name — check everything rather than answer nothing.
    return usable, False


def _query_all(mailboxes, query_fn):
    """Run query_fn(mailbox) across mailboxes in parallel. Returns (results, errors)."""
    results, errors = [], []
    if not mailboxes:
        return results, errors

    with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL, len(mailboxes))) as pool:
        futures = {pool.submit(query_fn, mb): mb for mb in mailboxes}
        for future in as_completed(futures):
            mailbox = futures[future]
            try:
                total, messages = future.result()
                if total:
                    results.append({
                        "company": mailbox["company"],
                        "total": total,
                        "messages": messages,
                    })
            except Exception as exc:
                errors.append(f"{mailbox['company']}: {type(exc).__name__}")
                print(f"[query error] {mailbox['email']}: {exc}")
    results.sort(key=lambda r: r["company"])
    return results, errors


def _format(results, errors, heading, empty_text):
    if not results:
        text = empty_text
        if errors:
            text += f"\n\n({len(errors)} unreachable)"
        return text

    total = sum(r["total"] for r in results)
    box = "inbox" if len(results) == 1 else "inboxes"
    lines = [f"<b>{total}</b> {heading} - {len(results)} {box}\n"]

    shown = 0
    for result in results:
        if shown >= MAX_LINES_SHOWN:
            break
        extra = result["total"] - len(result["messages"])
        lines.append(f"<b>{esc(result['company'])}</b> ({result['total']})")
        for message in result["messages"]:
            if shown >= MAX_LINES_SHOWN:
                break
            mark = "• " if not message.get("unread") else "🔵 "
            lines.append(
                f"{mark}{esc(message['from'])}\n"
                f"   {esc(message['subject'])}\n"
                f"   {esc(message['date'])}"
            )
            shown += 1
        if extra > 0:
            lines.append(f"   …and {extra} more")
        lines.append("")

    if errors:
        lines.append(f"({len(errors)} unreachable)")
    return "\n".join(lines).strip()


def _status_text():
    import main  # here to avoid a circular import at module load

    usable, skipped = config.configured_mailboxes()
    last = main.last_run()
    interval = config.setting("poll_interval_minutes", 30)

    lines = [
        "✅ <b>Running</b>",
        f"{len(usable)} inboxes - checked every {interval} min",
    ]
    if skipped:
        names = ", ".join(m["company"] for m in skipped)
        lines.append(f"⚠️ No password: {esc(names)}")
    if last:
        lines.append("")
        lines.append(f"<b>Last check</b> {esc(last['finished_at'])}")
        lines.append(f"{last['new_messages']} new, {last['alerted']} sent")
        if last.get("errors"):
            lines.append(f"{len(last['errors'])} error(s)")
    else:
        lines.append("")
        lines.append("No check run yet since last restart.")
    return "\n".join(lines)

def handle_command(text):
    """Answer a slash command directly, without asking the model. None if not one."""
    command = text.strip().split()[0].lower().lstrip("/").split("@")[0]
    if command in {"start", "help"}:
        return WELCOME_TEXT
    if command == "status":
        return _status_text()
    return None


def handle_message(text, notify=None):
    """
    Work out what was asked and return the reply.

    `notify` is an optional callable used to send a short progress note before
    a slow lookup starts.
    """
    if text.strip().startswith("/"):
        direct = handle_command(text)
        if direct:
            return direct

    instruction = parse_request(text)
    intent = instruction["intent"]

    if intent == "status":
        return _status_text()

    if intent == "other":
        return HELP_TEXT

    mailboxes, narrowed = _select_mailboxes(instruction["company_hint"])
    if not mailboxes:
        return "No inboxes configured."

    scope = f"{mailboxes[0]['company']}" if narrowed and len(mailboxes) == 1 else f"{len(mailboxes)} mailboxes"

    if intent == "search":
        keyword = instruction["keyword"]
        if not keyword:
            return HELP_TEXT
        if notify:
            notify(f"Searching {esc(scope)}...")
        results, errors = _query_all(
            mailboxes,
            lambda mb: search.search_mailbox(
                mb["imap_host"], mb["imap_port"], mb["email"],
                config.mailbox_password(mb), keyword, limit=PER_MAILBOX_LIMIT,
            ),
        )
        return _format(results, errors, f"matches for “{esc(keyword)}”",
                       f"No matches for “{esc(keyword)}”.")

    if intent == "unread":
        if notify:
            notify(f"Checking {esc(scope)}...")
        results, errors = _query_all(
            mailboxes,
            lambda mb: search.unread_mail(
                mb["imap_host"], mb["imap_port"], mb["email"],
                config.mailbox_password(mb), limit=PER_MAILBOX_LIMIT,
            ),
        )
        return _format(results, errors, "unread", "Nothing unread.")

    # intent == "recent"
    hours = instruction["hours"] or 24
    window = "the last 24 hours" if hours == 24 else f"the last {hours} hours"
    if notify:
        notify(f"Checking {esc(scope)}...")
    results, errors = _query_all(
        mailboxes,
        lambda mb: search.recent_mail(
            mb["imap_host"], mb["imap_port"], mb["email"],
            config.mailbox_password(mb), hours=hours, limit=PER_MAILBOX_LIMIT,
        ),
    )
    return _format(results, errors, f"new in {window}",
                   f"Nothing new in {window}.")
