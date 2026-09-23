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

WELCOME_TEXT = (
    f"👋 Welcome to <b>{BOT_NAME}</b>.\n\n"
    "I watch the company inboxes and message you when something important "
    "arrives. You can also just ask me things:\n\n"
    "📥 <b>is there new mail?</b>\n"
    "📅 <b>anything come in today?</b>\n"
    "🔵 <b>any unread mail?</b>\n"
    "🔍 <b>find email from KRA</b>\n"
    "🏢 <b>any mail from Beacon about the tender?</b>\n"
    "✅ <b>are you working?</b>\n\n"
    "Ask in plain words, no commands needed. Name a company and I will "
    "check only that inbox, otherwise I check them all.\n\n"
    "I only ever read mail. I never send, reply to, delete or open anything."
)

HELP_TEXT = WELCOME_TEXT

PRIVATE_NOTICE = (
    "This assistant is private and is not available for general use."
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
            text += f"\n\n({len(errors)} mailbox(es) could not be reached)"
        return text

    total = sum(r["total"] for r in results)
    lines = [f"{heading} — <b>{total}</b> across {len(results)} mailbox(es):\n"]

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
        lines.append(f"({len(errors)} mailbox(es) could not be reached)")
    return "\n".join(lines).strip()


def _status_text():
    import main  # imported here to avoid a circular import at module load

    usable, skipped = config.configured_mailboxes()
    last = main.last_run()

    lines = [
        "✅ <b>Running.</b>",
        f"Mailboxes watched: <b>{len(usable)}</b>",
    ]
    if skipped:
        lines.append(f"Missing a password: {len(skipped)} — {esc(', '.join(m['company'] for m in skipped))}")
    if last:
        lines.append(
            f"\nLast check: {esc(last['finished_at'])}\n"
            f"Checked {last['checked']} mailbox(es), "
            f"{last['new_messages']} new, {last['alerted']} alert(s)"
        )
        if last.get("errors"):
            lines.append(f"Errors last run: {len(last['errors'])}")
    else:
        lines.append("\nNo check has run yet since the service started.")
    lines.append(f"Checks run every {config.setting('poll_interval_minutes', 30)} minutes.")
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
        return "No mailboxes are configured with a password yet."

    scope = f"{mailboxes[0]['company']}" if narrowed and len(mailboxes) == 1 else f"{len(mailboxes)} mailboxes"

    if intent == "search":
        keyword = instruction["keyword"]
        if not keyword:
            return HELP_TEXT
        if notify:
            notify(f"Searching {esc(scope)} for “{esc(keyword)}”…")
        results, errors = _query_all(
            mailboxes,
            lambda mb: search.search_mailbox(
                mb["imap_host"], mb["imap_port"], mb["email"],
                config.mailbox_password(mb), keyword, limit=PER_MAILBOX_LIMIT,
            ),
        )
        return _format(results, errors, f"Matches for “{esc(keyword)}”",
                       f"No matches for “{esc(keyword)}”.")

    if intent == "unread":
        if notify:
            notify(f"Checking unread mail in {esc(scope)}…")
        results, errors = _query_all(
            mailboxes,
            lambda mb: search.unread_mail(
                mb["imap_host"], mb["imap_port"], mb["email"],
                config.mailbox_password(mb), limit=PER_MAILBOX_LIMIT,
            ),
        )
        return _format(results, errors, "Unread mail", "Nothing unread — all clear.")

    # intent == "recent"
    hours = instruction["hours"] or 24
    window = "the last 24 hours" if hours == 24 else f"the last {hours} hours"
    if notify:
        notify(f"Checking {esc(scope)} for mail from {window}…")
    results, errors = _query_all(
        mailboxes,
        lambda mb: search.recent_mail(
            mb["imap_host"], mb["imap_port"], mb["email"],
            config.mailbox_password(mb), hours=hours, limit=PER_MAILBOX_LIMIT,
        ),
    )
    return _format(results, errors, f"New mail in {window}",
                   f"Nothing new in {window}.")
