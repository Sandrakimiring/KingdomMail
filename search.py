"""
Read-only mailbox queries used to answer questions asked over Telegram:
keyword search, recent arrivals, and unread mail.

None of these change anything: the folder is opened read-only and only
envelopes are fetched, so unread mail stays unread.
"""

from datetime import datetime, timedelta, timezone

import mailutil


def _read_envelopes(client, uids, limit):
    """Fetch envelopes for the newest `limit` UIDs and return them newest first."""
    chosen = sorted(uids, reverse=True)[:limit]
    if not chosen:
        return []

    response = client.fetch(chosen, ["ENVELOPE", "INTERNALDATE", "FLAGS"])
    results = []
    for uid in chosen:
        data = response.get(uid)
        if not data:
            continue
        try:
            envelope = data[b"ENVELOPE"]
            when = mailutil.to_utc(
                getattr(envelope, "date", None) or data.get(b"INTERNALDATE")
            )
            flags = data.get(b"FLAGS", ()) or ()
            results.append({
                "uid": uid,
                "from": mailutil.envelope_sender(envelope),
                "subject": mailutil.envelope_subject(envelope),
                "date": mailutil.format_date(when),
                "sort_key": when,
                "unread": b"\\Seen" not in flags,
            })
        except Exception as exc:
            print(f"[warn] could not read UID {uid}: {exc}")

    results.sort(key=lambda r: (r["sort_key"] is not None, r["sort_key"]), reverse=True)
    return results


def search_mailbox(host, port, address, password, keyword, limit=3,
                   timeout=mailutil.INTERACTIVE_TIMEOUT):
    """Search FROM, SUBJECT and body text for a keyword. Newest matches first."""
    with mailutil.imap_connect(host, port, address, password, timeout,
                               attempts=mailutil.INTERACTIVE_ATTEMPTS) as client:
        client.select_folder("INBOX", readonly=True)

        uids = set()
        for criteria in (["FROM", keyword], ["SUBJECT", keyword], ["TEXT", keyword]):
            try:
                uids.update(client.search(criteria))
            except Exception:
                # Some servers reject certain criteria or non-ASCII terms.
                pass

        total = len(uids)
        return total, _read_envelopes(client, uids, limit)


def recent_mail(host, port, address, password, hours=24, limit=3,
                timeout=mailutil.INTERACTIVE_TIMEOUT):
    """Everything that arrived in the last `hours`. Returns (total, newest few)."""
    with mailutil.imap_connect(host, port, address, password, timeout,
                               attempts=mailutil.INTERACTIVE_ATTEMPTS) as client:
        client.select_folder("INBOX", readonly=True)

        # IMAP's SINCE only has day resolution, so search a day wide and then
        # filter to the exact cut-off here.
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        search_date = (cutoff - timedelta(days=1)).date()

        try:
            uids = client.search(["SINCE", search_date])
        except Exception:
            uids = client.search(["ALL"])

        # Look at a bounded slice of the newest candidates, not the whole box.
        candidates = sorted(uids, reverse=True)[:200]
        found = _read_envelopes(client, candidates, len(candidates))

        fresh = [m for m in found if m["sort_key"] and m["sort_key"] >= cutoff]
        return len(fresh), fresh[:limit]


def unread_mail(host, port, address, password, limit=3,
                timeout=mailutil.INTERACTIVE_TIMEOUT):
    """Unread messages. Reading the envelope does not mark anything as read."""
    with mailutil.imap_connect(host, port, address, password, timeout,
                               attempts=mailutil.INTERACTIVE_ATTEMPTS) as client:
        client.select_folder("INBOX", readonly=True)
        uids = client.search(["UNSEEN"])
        return len(uids), _read_envelopes(client, uids, limit)
