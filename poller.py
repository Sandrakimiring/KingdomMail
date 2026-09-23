"""
Reads new mail from one mailbox over IMAP.

Strictly read-only: the folder is opened with readonly=True and the body is
fetched with BODY.PEEK, so nothing is ever marked as read, moved or deleted.
Position is tracked by UID in the state file rather than by the unread flag,
so the service never interferes with how the inbox is actually used.
"""

import mailutil

# Most messages handled in a single pass for one mailbox. Anything above this
# waits for the next run, so a sudden backlog can't stall everything else.
MAX_PER_RUN = 40


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
            # Either we've never looked at this mailbox, or the server has
            # renumbered its UIDs (which makes the stored one meaningless).
            # Record where the mailbox is now and alert on nothing, so the
            # first run can never flood with the entire mail history.
            existing = client.search(["ALL"])
            return {
                "messages": [],
                "last_uid": max(existing) if existing else 0,
                "uidvalidity": uidvalidity,
                "bootstrapped": True,
                "renumbered": renumbered,
                "remaining": 0,
            }

        # Ask the server for the new range instead of listing every UID.
        # A "N:*" search always returns at least the highest UID even when it
        # is below N, so the result still has to be filtered here.
        candidates = client.search(["UID", f"{last_uid + 1}:*"])
        new_uids = sorted(u for u in candidates if u > last_uid)

        if not new_uids:
            return {
                "messages": [], "last_uid": last_uid, "uidvalidity": uidvalidity,
                "bootstrapped": False, "renumbered": False, "remaining": 0,
            }

        # Oldest first, so the state advances steadily and any backlog drains
        # across runs instead of being skipped.
        batch = new_uids[:max_count]
        remaining = len(new_uids) - len(batch)

        response = client.fetch(
            batch,
            ["ENVELOPE", "INTERNALDATE", f"BODY.PEEK[]<0.{mailutil.BODY_PEEK_BYTES}>"],
        )

        messages = []
        for uid in batch:
            data = response.get(uid)
            if not data:
                continue
            try:
                envelope = data[b"ENVELOPE"]
                when = mailutil.to_utc(
                    getattr(envelope, "date", None) or data.get(b"INTERNALDATE")
                )
                messages.append({
                    "uid": uid,
                    "from": mailutil.envelope_sender(envelope),
                    "subject": mailutil.envelope_subject(envelope),
                    "snippet": mailutil.extract_snippet(mailutil.body_bytes(data)),
                    "date": mailutil.format_date(when),
                })
            except Exception as exc:
                # One unparseable message must not cost us the whole batch.
                print(f"[warn] {address}: could not read UID {uid}: {exc}")

        return {
            "messages": messages,
            "last_uid": max(batch),
            "uidvalidity": uidvalidity,
            "bootstrapped": False,
            "renumbered": False,
            "remaining": remaining,
        }
