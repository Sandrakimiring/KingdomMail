"""
Reads new mail from one mailbox over IMAP.

Strictly read-only: the folder is opened with readonly=True and the body is
fetched with BODY.PEEK, so nothing is ever marked as read, moved or deleted.
Position is tracked by UID in the state file rather than by the unread flag,
so the service never interferes with how the inbox is actually used.
"""

from datetime import datetime, timedelta, timezone

import config
import mailutil

# Most messages handled in a single pass for one mailbox. Anything above this
# waits for the next run, so a sudden backlog can't stall everything else.
MAX_PER_RUN = 40


def _read_messages(client, uids, address, since=None):
    """Fetch and parse the given UIDs, optionally keeping only those after `since`."""
    if not uids:
        return []

    response = client.fetch(
        uids, ["ENVELOPE", "INTERNALDATE", f"BODY.PEEK[]<0.{mailutil.BODY_PEEK_BYTES}>"]
    )

    messages = []
    for uid in uids:
        data = response.get(uid)
        if not data:
            continue
        try:
            envelope = data[b"ENVELOPE"]
            when = mailutil.to_utc(
                getattr(envelope, "date", None) or data.get(b"INTERNALDATE")
            )
            if since is not None and (when is None or when < since):
                continue
            messages.append({
                "uid": uid,
                "from": mailutil.envelope_sender(envelope),
                "subject": mailutil.envelope_subject(envelope),
                "snippet": mailutil.extract_snippet(mailutil.body_bytes(data)),
                "date": mailutil.friendly_date(when),
            })
        except Exception as exc:
            print(f"[warn] {address}: could not read UID {uid}: {exc}")
    return messages


def fetch_new_emails(host, port, address, password, last_uid, last_uidvalidity,
                     max_count=MAX_PER_RUN, timeout=mailutil.DEFAULT_TIMEOUT):
    """
    Fetch messages newer than last_uid.

    Returns a dict:
      messages      list of {uid, from, subject, snippet, date}
      last_uid      the UID to record once these have been handled
      uidvalidity   the folder's current UIDVALIDITY
      bootstrapped  True when this run only recorded a starting point
      remaining     messages left over for the next run
    """
    with mailutil.imap_connect(host, port, address, password, timeout) as client:
        folder = client.select_folder("INBOX", readonly=True)
        uidvalidity = folder.get(b"UIDVALIDITY")
        if uidvalidity is not None:
            uidvalidity = int(uidvalidity)

        first_time = last_uid is None
        renumbered = (
            not first_time
            and last_uidvalidity is not None
            and uidvalidity is not None
            and uidvalidity != last_uidvalidity
        )

        if first_time or renumbered:
            existing = client.search(["ALL"])
            newest = max(existing) if existing else 0

            # On a host with no persistent disk the state file is lost on every
            # restart. Alerting on nothing each time would mean never alerting
            # at all, so by default we look back a short window instead. Only a
            # genuinely first install should start from silence.
            behaviour = config.setting("on_missing_state", "lookback")

            if behaviour != "lookback" or not existing:
                return {
                    "messages": [], "last_uid": newest, "uidvalidity": uidvalidity,
                    "bootstrapped": True, "renumbered": renumbered,
                    "recovered": False, "remaining": 0,
                }

            minutes = int(config.setting("lookback_minutes", 40))
            cutoff = datetime.now(timezone.utc) - timedelta(minutes=minutes)
            try:
                recent = client.search(["SINCE", (cutoff - timedelta(days=1)).date()])
            except Exception:
                recent = []

            candidates = sorted(u for u in recent if u <= newest)[-max_count:]
            messages = _read_messages(client, candidates, address, since=cutoff)

            return {
                "messages": messages, "last_uid": newest, "uidvalidity": uidvalidity,
                "bootstrapped": not messages, "renumbered": renumbered,
                "recovered": True, "remaining": 0,
            }

        # Ask the server for the new range instead of listing every UID.
        # A "N:*" search always returns at least the highest UID even when it
        # is below N, so the result still has to be filtered here.
        candidates = client.search(["UID", f"{last_uid + 1}:*"])
        new_uids = sorted(u for u in candidates if u > last_uid)

        if not new_uids:
            return {
                "messages": [], "last_uid": last_uid, "uidvalidity": uidvalidity,
                "bootstrapped": False, "renumbered": False,
                "recovered": False, "remaining": 0,
            }

        # Oldest first, so the state advances steadily and any backlog drains
        # across runs instead of being skipped.
        batch = new_uids[:max_count]
        remaining = len(new_uids) - len(batch)

        messages = _read_messages(client, batch, address)

        return {
            "messages": messages,
            "last_uid": max(batch),
            "uidvalidity": uidvalidity,
            "bootstrapped": False,
            "renumbered": False,
            "recovered": False,
            "remaining": remaining,
        }
