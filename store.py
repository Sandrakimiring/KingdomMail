"""
Remembers what has already been handled, so nothing gets alerted twice.

Two small JSON files:
  state.json          {mailbox: {"uid": <last processed UID>, "uidvalidity": <int>}}
  known_senders.json  {mailbox: [sender, ...]}  (only used by new_senders_only mailboxes)

Writes are atomic (temp file + rename), so a crash mid-write cannot leave a
half-written file that breaks the next run.
"""

import json
import os
import tempfile
import threading

import config

# Keep the sender list from growing without limit.
MAX_SENDERS_PER_MAILBOX = 2000


def _read(filename, default):
    path = config.state_path(filename)
    if not path.exists():
        return default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError, ValueError):
        # A corrupt state file should not stop the service. Starting from a
        # clean slate re-bootstraps rather than re-alerting old mail.
        print(f"[warn] {filename} was unreadable — starting it fresh")
        return default


def _write(filename, data):
    path = config.state_path(filename)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class State:
    """Load once, save after each mailbox so a later failure can't undo earlier work."""

    def __init__(self):
        self._state_file = config.setting("state_file", "state.json")
        self._senders_file = config.setting("known_senders_file", "known_senders.json")
        self._state = _read(self._state_file, {})
        self._senders = _read(self._senders_file, {})
        self._lock = threading.Lock()

    def position(self, address):
        """(last_uid, uidvalidity). last_uid is None when the mailbox is brand new."""
        record = self._state.get(address)
        if record is None:
            return None, None
        if isinstance(record, int):
            # Older format stored a bare integer. Treat the UIDVALIDITY as
            # unknown so the next run records it without skipping anything.
            return record, None
        return record.get("uid"), record.get("uidvalidity")

    def set_position(self, address, uid, uidvalidity):
        with self._lock:
            self._state[address] = {
                "uid": int(uid),
                "uidvalidity": int(uidvalidity) if uidvalidity else None,
            }
            _write(self._state_file, self._state)

    def known_senders(self, address):
        return set(self._senders.get(address, []))

    def remember_senders(self, address, senders):
        with self._lock:
            existing = self._senders.get(address, [])
            seen = set(existing)
            for sender in senders:
                if sender not in seen:
                    existing.append(sender)
                    seen.add(sender)
            if len(existing) > MAX_SENDERS_PER_MAILBOX:
                existing = existing[-MAX_SENDERS_PER_MAILBOX:]
            self._senders[address] = existing
            _write(self._senders_file, self._senders)

    def summary(self):
        """Plain snapshot for the /status endpoint."""
        out = {}
        for address, record in self._state.items():
            uid = record if isinstance(record, int) else record.get("uid")
            out[address] = uid
        return out
