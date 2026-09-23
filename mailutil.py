"""
Shared IMAP + email parsing helpers.

Everything here is read-only and defensive: real inboxes contain malformed
headers, odd character sets and huge attachments, and none of that should be
able to stop a run.
"""

import contextlib
import email
import time
import html as html_module
import re
from datetime import datetime, timezone
from email.header import decode_header, make_header

from imapclient import IMAPClient

# How much of each message to download. Enough to reach the readable text in
# almost every email, small enough that a large attachment is never pulled.
BODY_PEEK_BYTES = 16384
DEFAULT_TIMEOUT = 30


# A home or office connection drops briefly now and then. Retrying a couple of
# times turns a transient blip into a slight delay instead of a failed mailbox.
CONNECT_ATTEMPTS = 3
# Someone is waiting on a Telegram reply, so those queries do not retry.
INTERACTIVE_ATTEMPTS = 1
INTERACTIVE_TIMEOUT = 15


@contextlib.contextmanager
def imap_connect(host, port, address, password, timeout=DEFAULT_TIMEOUT, attempts=CONNECT_ATTEMPTS):
    """Connect, log in, and always close cleanly — even on error."""
    client = None
    last_error = None

    for attempt in range(max(1, attempts)):
        try:
            client = IMAPClient(host, port=port, ssl=True, timeout=timeout)
            client.login(address, password)
            break
        except Exception as exc:
            last_error = exc
            if client is not None:
                try:
                    client.shutdown()
                except Exception:
                    pass
                client = None
            # A rejected password will not start working on a retry.
            if "AUTHENTICATIONFAILED" in str(exc).upper() or "INVALID CREDENTIALS" in str(exc).upper():
                raise
            if attempt < attempts - 1:
                time.sleep(2 ** attempt)

    if client is None:
        raise last_error

    try:
        yield client
    finally:
        try:
            client.logout()
        except Exception:
            try:
                client.shutdown()
            except Exception:
                pass


def decode_header_value(value):
    """Turn an encoded email header into readable text. Never raises."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError:
            value = value.decode("latin-1", errors="replace")
    try:
        return str(make_header(decode_header(value))).strip()
    except Exception:
        return str(value).strip()


def _decode_bytes(value):
    if not value:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def envelope_sender(envelope):
    """'Name <user@host>' from an ENVELOPE, falling back gracefully."""
    addresses = getattr(envelope, "from_", None)
    if not addresses:
        return "unknown sender"
    first = addresses[0]
    name = decode_header_value(first.name) if first.name else ""
    mailbox = _decode_bytes(first.mailbox)
    host = _decode_bytes(first.host)
    if mailbox and host:
        address = f"{mailbox}@{host}"
    else:
        address = mailbox or host or "unknown sender"
    return f"{name} <{address}>" if name else address


def envelope_subject(envelope):
    subject = decode_header_value(getattr(envelope, "subject", None))
    return subject or "(no subject)"


def to_utc(value):
    """Normalise a datetime to UTC. Naive values are assumed to be UTC."""
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def format_date(value):
    moment = to_utc(value)
    return moment.strftime("%Y-%m-%d %H:%M UTC") if moment else ""


def body_bytes(fetch_data):
    """Pull the raw message bytes out of a fetch response, whatever key was used."""
    for key, value in fetch_data.items():
        if isinstance(key, bytes) and key.startswith(b"BODY[") and isinstance(value, bytes):
            return value
    return b""


_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)


def strip_html(text):
    if not text:
        return ""
    text = _SCRIPT_RE.sub(" ", text)
    text = _TAG_RE.sub(" ", text)
    return html_module.unescape(text)


def _decode_part(part):
    payload = part.get_payload(decode=True)
    if not payload:
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except (LookupError, UnicodeDecodeError):
        return payload.decode("utf-8", errors="replace")


def extract_snippet(raw, limit=600):
    """
    Readable text from a raw MIME message.

    Prefers the text/plain part, falls back to stripped HTML, and ignores
    attachments entirely — so the classifier never sees base64 or MIME markers.
    """
    if not raw:
        return ""
    if isinstance(raw, str):
        raw = raw.encode("utf-8", errors="replace")

    try:
        message = email.message_from_bytes(raw)
    except Exception:
        return collapse_whitespace(raw.decode("utf-8", errors="replace"))[:limit]

    plain, rich = "", ""
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_maintype() == "multipart":
                continue
            if part.get_content_disposition() == "attachment":
                continue
            content_type = part.get_content_type()
            if content_type == "text/plain" and not plain:
                plain = _decode_part(part)
            elif content_type == "text/html" and not rich:
                rich = _decode_part(part)
    else:
        text = _decode_part(message)
        if message.get_content_type() == "text/html":
            rich = text
        else:
            plain = text

    body = plain or strip_html(rich)
    return collapse_whitespace(drop_quoted_reply(body))[:limit]


def drop_quoted_reply(text):
    """Trim the quoted history off a reply so the new content leads."""
    if not text:
        return ""
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(">"):
            continue
        if re.match(r"^-+\s*(original message|forwarded message)\s*-+$", stripped, re.I):
            break
        if re.match(r"^on .{10,80}\bwrote:$", stripped, re.I):
            break
        lines.append(line)
    return "\n".join(lines)


def collapse_whitespace(text):
    return " ".join((text or "").split())
