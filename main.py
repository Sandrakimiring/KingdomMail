"""
The periodic check: look at every mailbox, classify what's new, alert on what matters.

Run it directly for a one-off check:
    python main.py

Two things make this safe to run repeatedly:
  - The first time a mailbox is seen, its current position is recorded and
    nothing is alerted, so it never floods with the whole mail history.
  - Progress is saved per mailbox, so a failure part-way through never causes
    the same emails to be alerted again on the next run.
"""

import config  # must be first: this loads .env before anything reads it

import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError, as_completed
from datetime import datetime, timezone

import poller
import store
from classifier import classify_email
from notifier import send_alert

MAX_PARALLEL_FETCH = 6

# A whole run must finish inside this. Without a ceiling, one mailbox stuck on a
# dead connection would hold the run lock and block every later check.
RUN_DEADLINE_SECONDS = 600

_run_lock = threading.Lock()
_last_run = None
_last_run_lock = threading.Lock()


def last_run():
    with _last_run_lock:
        return dict(_last_run) if _last_run else None


def _record_run(summary):
    global _last_run
    with _last_run_lock:
        _last_run = summary


def _fetch_one(mailbox, state):
    """Read new mail from one mailbox. Runs in a worker thread."""
    address = mailbox["email"]
    last_uid, last_uidvalidity = state.position(address)
    result = poller.fetch_new_emails(
        mailbox["imap_host"], mailbox["imap_port"], address,
        config.mailbox_password(mailbox), last_uid, last_uidvalidity,
    )
    return mailbox, result


def _handle_messages(mailbox, result, state, summary):
    """Classify and alert for one mailbox, then record how far we got."""
    company = mailbox["company"]
    address = mailbox["email"]
    mode = mailbox.get("mode", "all")
    known = state.known_senders(address) if mode == "new_senders_only" else set()
    new_senders = []

    for message in result["messages"]:
        sender = message["from"]

        # Mailboxes in new_senders_only mode should only raise a flag for
        # people who have not written before.
        if mode == "new_senders_only" and sender in known:
            continue

        try:
            verdict = classify_email(sender, message["subject"], message["snippet"])
        except Exception as exc:
            # Should not happen (classify_email handles its own errors), but a
            # surprise here must not cost us the rest of the mailbox.
            summary["errors"].append(f"{company}: classify failed: {exc}")
            verdict = {"category": "needs_reply", "important": True,
                       "summary": message["subject"][:120],
                       "action": "Check this one", "deadline": ""}

        if verdict.get("error"):
            summary["errors"].append(f"{company}: {verdict['error']}")

        important = verdict["important"]

        flag = "IMPORTANT" if important else "   fyi   "
        print(f"[{flag}] {company}: {message['subject'][:52]} -> {verdict['category']}")

        # Every email is offered; each recipient's own level decides who is
        # actually messaged.
        delivered = send_alert(
            company=company,
            sender=sender,
            subject=message["subject"],
            summary=verdict.get("summary", ""),
            category=verdict.get("category", ""),
            action=verdict.get("action", ""),
            deadline=verdict.get("deadline", ""),
            date=message.get("date", ""),
            important=important,
        )
        if delivered:
            summary["alerted"] += 1


        new_senders.append(sender)

    if new_senders and mode == "new_senders_only":
        state.remember_senders(address, new_senders)

    # Saved only after the messages above have been dealt with, so an
    # interruption means they are retried rather than lost.
    state.set_position(address, result["last_uid"], result["uidvalidity"])


def run_once():
    """One full pass over every mailbox. Returns a summary dict."""
    if not _run_lock.acquire(blocking=False):
        print("[skip] a check is already running")
        return {"status": "already_running"}

    started = datetime.now(timezone.utc)
    summary = {
        "status": "ok",
        "checked": 0,
        "new_messages": 0,
        "alerted": 0,
        "bootstrapped": [],
        "skipped": [],
        "errors": [],
        "backlog": {},
    }

    try:
        state = store.State()
        usable, missing = config.configured_mailboxes()

        for mailbox in missing:
            note = f"{mailbox['company']}: no password set ({mailbox.get('password_env')})"
            summary["skipped"].append(note)
            print(f"[skip] {note}")

        if not usable:
            summary["status"] = "no_mailboxes_configured"
            return summary

        # Reading mailboxes is mostly waiting on the network, so do it in
        # parallel. Classification afterwards stays sequential and rate limited.
        fetched = []
        pool = ThreadPoolExecutor(max_workers=min(MAX_PARALLEL_FETCH, len(usable)))
        try:
            futures = {pool.submit(_fetch_one, mb, state): mb for mb in usable}
            try:
                for future in as_completed(futures, timeout=RUN_DEADLINE_SECONDS):
                    mailbox = futures[future]
                    try:
                        fetched.append(future.result())
                        summary["checked"] += 1
                    except Exception as exc:
                        message = f"{mailbox['company']}: {type(exc).__name__}: {exc}"
                        summary["errors"].append(message)
                        print(f"[error] could not read {mailbox['email']}: {exc}")
            except TimeoutError:
                # Whatever did not finish in time is simply picked up next run,
                # because their positions were never advanced.
                unfinished = [mb["company"] for f, mb in futures.items() if not f.done()]
                for company in unfinished:
                    summary["errors"].append(f"{company}: timed out, will retry next run")
                print(f"[warn] run deadline reached; {len(unfinished)} mailbox(es) unfinished")
        finally:
            # Do not block on stuck threads; they are daemon work and the run
            # must be able to end so the next one can start.
            pool.shutdown(wait=False, cancel_futures=True)

        for mailbox, result in sorted(fetched, key=lambda item: item[0]["company"]):
            company = mailbox["company"]

            if result.get("recovered") and result["messages"]:
                print(f"[recover] {company}: state was lost; re-checking recent mail")
                summary.setdefault("recovered", []).append(company)

            if result["bootstrapped"]:
                reason = "UIDs were renumbered by the server" if result["renumbered"] \
                    else "first time seeing this mailbox"
                summary["bootstrapped"].append(company)
                print(f"[start] {company}: {reason} — noting position, not alerting on existing mail")
                state.set_position(mailbox["email"], result["last_uid"], result["uidvalidity"])
                continue

            if not result["messages"]:
                print(f"[ok] {company}: nothing new")
                state.set_position(mailbox["email"], result["last_uid"], result["uidvalidity"])
                continue

            summary["new_messages"] += len(result["messages"])
            if result["remaining"]:
                summary["backlog"][company] = result["remaining"]
                print(f"[note] {company}: {result['remaining']} more waiting for the next run")

            try:
                _handle_messages(mailbox, result, state, summary)
            except Exception as exc:
                summary["errors"].append(f"{company}: {type(exc).__name__}: {exc}")
                print(f"[error] {company}: {exc}")

    finally:
        finished = datetime.now(timezone.utc)
        summary["finished_at"] = finished.strftime("%Y-%m-%d %H:%M UTC")
        summary["seconds"] = round((finished - started).total_seconds(), 1)
        _record_run(summary)
        _run_lock.release()

    print(
        f"[done] {summary['checked']} mailbox(es), {summary['new_messages']} new, "
        f"{summary['alerted']} alert(s), {len(summary['errors'])} error(s) "
        f"in {summary['seconds']}s"
    )
    return summary


# Kept so anything that imported the old name still works.
main = run_once


if __name__ == "__main__":
    run_once()
