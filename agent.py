"""
Turns a plain-language Telegram message into a structured instruction.

Examples of what it handles:
  "find email from KRA"                -> search for a keyword
  "is there new mail?"                 -> recent arrivals across all mailboxes
  "anything unread for Geowells?"      -> unread mail, one company
  "are you working?"                   -> service status
"""

import json

import config
import classifier

INTENTS = {"search", "recent", "unread", "status", "other"}

SYSTEM_PROMPT = """You turn a person's casual request into a structured instruction for an
email assistant that watches several company inboxes. Reply with ONLY a JSON object:

{
  "intent": "search" | "recent" | "unread" | "status" | "other",
  "keyword": "the sender, organisation or subject word to look for, or null",
  "company_hint": "a company name mentioned, or null",
  "hours": a number of hours to look back, or null
}

Choose the intent this way:
- "recent": asking whether anything new or recent has arrived, or what came in.
  Set "hours" from the wording: today or since this morning = 12, no period given = 24,
  this week = 168.
- "unread": specifically asking about unread or unopened mail.
- "search": looking for a particular sender, organisation or topic. Set "keyword".
- "status": asking whether the assistant itself is working or running.
- "other": greetings and anything unrelated to mail.

Examples:
"find email from KRA" -> {"intent":"search","keyword":"KRA","company_hint":null,"hours":null}
"any mail from Beacon about the tender?" -> {"intent":"search","keyword":"tender","company_hint":"Beacon","hours":null}
"is there new mail" -> {"intent":"recent","keyword":null,"company_hint":null,"hours":24}
"anything come in today?" -> {"intent":"recent","keyword":null,"company_hint":null,"hours":12}
"what came in this week for Geowells" -> {"intent":"recent","keyword":null,"company_hint":"Geowells","hours":168}
"any unread mail?" -> {"intent":"unread","keyword":null,"company_hint":null,"hours":null}
"are you still running?" -> {"intent":"status","keyword":null,"company_hint":null,"hours":null}
"hi" -> {"intent":"other","keyword":null,"company_hint":null,"hours":null}"""


# Words that appear in several company names and so identify none of them.
def _company_index():
    """Maps each distinctive word to the one company it belongs to."""
    names = {}
    counts = {}
    for mailbox in config.mailboxes():
        company = mailbox["company"]
        words = set(w for w in company.lower().replace("-", " ").split() if len(w) > 2)
        words.add(mailbox["email"].split("@")[1].split(".")[0].lower())
        for word in words:
            counts[word] = counts.get(word, 0) + 1
            names.setdefault(word, company)
    # Keep only words that point at exactly one company.
    return {w: c for w, c in names.items() if counts[w] == 1}


_INDEX = None


def detect_company(text):
    """
    Find a company named in the message without asking the model.

    Matches the full name first, then any word unique to one company, so
    "kingdom" stays ambiguous while "balcom" or "geowells" does not.
    """
    global _INDEX
    if _INDEX is None:
        _INDEX = _company_index()

    lowered = (text or "").lower()

    for mailbox in config.mailboxes():
        if mailbox["company"].lower() in lowered:
            return mailbox["company"]

    best = None
    for word, company in _INDEX.items():
        if word in lowered and (best is None or len(word) > len(best[0])):
            best = (word, company)
    return best[1] if best else None


def _clean(value):
    """Treat null, 'null' and empty strings as absent."""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"null", "none", "n/a"}:
        return None
    return text


def _fallback(user_text):
    """Keyword matching for when the model is unavailable, so the bot still replies."""
    lowered = (user_text or "").lower()
    company = detect_company(user_text)
    if any(word in lowered for word in ("unread", "unopened", "not read")):
        return {"intent": "unread", "keyword": None, "company_hint": company, "hours": None}
    if any(word in lowered for word in ("new mail", "new email", "anything new",
                                        "any mail", "recent", "today", "come in")):
        return {"intent": "recent", "keyword": None, "company_hint": company, "hours": 24}
    if any(word in lowered for word in ("status", "working", "running", "alive")):
        return {"intent": "status", "keyword": None, "company_hint": None, "hours": None}
    if company:
        return {"intent": "recent", "keyword": None, "company_hint": company, "hours": 24}
    return {"intent": "other", "keyword": None, "company_hint": None, "hours": None}


def parse_request(user_text):
    """Returns {intent, keyword, company_hint, hours}. Never raises."""
    try:
        parsed = classifier._ask(SYSTEM_PROMPT, user_text, max_tokens=900)
    except Exception as exc:
        print(f"[agent] falling back to keyword matching: {exc}")
        return _fallback(user_text)

    intent = str(parsed.get("intent", "")).strip().lower()
    if intent not in INTENTS:
        return _fallback(user_text)

    hours = parsed.get("hours")
    try:
        hours = int(hours) if hours is not None else None
    except (TypeError, ValueError):
        hours = None
    if hours is not None:
        hours = max(1, min(hours, 720))  # keep it between an hour and a month

    # A company named in the text wins over the model's guess, which
    # sometimes drops it entirely.
    company = detect_company(user_text) or _clean(parsed.get("company_hint"))
    keyword = _clean(parsed.get("keyword"))

    # "balcom" or "new mail balcom" names an inbox, not a word to search for.
    # Searching that inbox for its own name would find nothing useful.
    if company and keyword and keyword.lower() in company.lower():
        keyword = None
    if intent == "search" and not keyword:
        intent = "recent" if company else "other"
        hours = hours or 24

    return {
        "intent": intent,
        "keyword": keyword,
        "company_hint": company,
        "hours": hours,
    }
