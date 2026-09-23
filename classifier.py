"""
Decides whether a single email is worth interrupting someone for.

Uses Groq's free API. The client is built on first use (not at import time) so
a missing key produces a clear error instead of crashing the whole service on
startup, and calls are rate limited so a busy run cannot trip the free tier.
"""

import json
import threading
import time

from groq import Groq

import config

# Groq's free tier allows 30 requests/minute. Staying under it avoids the
# rejections that used to abort an entire run.
REQUESTS_PER_MINUTE = 20
MAX_ATTEMPTS = 3

MODEL = config.env("GROQ_MODEL", "openai/gpt-oss-120b")

VALID_CATEGORIES = {
    "tender_or_business_critical",
    "needs_reply",
    "marketing_or_spam",
    "routine_fyi",
}
IMPORTANT_CATEGORIES = {"tender_or_business_critical", "needs_reply"}

SYSTEM_PROMPT = """You brief a busy owner who runs several small companies (tenders, oil and gas supply, general trade) on their incoming email. You are given a sender, subject and part of the body. Reply with ONLY a JSON object:

{
  "category": one of ["tender_or_business_critical", "needs_reply", "marketing_or_spam", "routine_fyi"],
  "summary": "2 to 3 short sentences saying what this email actually says and why it matters",
  "action": "what the owner should do about it, under 10 words",
  "deadline": "any date or deadline stated in the email, or null"
}

Categories:
- "tender_or_business_critical": tenders, bids, contracts, invoices, payments, legal
  or regulatory notices, anything with money or a deadline attached.
- "needs_reply": a real person is asking something or waiting on a response.
- "marketing_or_spam": newsletters, promotions, cold sales outreach, bulk mail.
- "routine_fyi": automated receipts, notifications, no-action updates.

For "summary", say what is actually in the email. Name the amounts, reference numbers,
dates and people mentioned. Do not just restate the subject line. Write plainly, as
you would tell someone in person.

For "action", be specific and practical: "Reply with your availability",
"Submit bid documents by Friday", "Pay KES 45,000", "No action needed".

Be conservative: if genuinely unsure between an important and unimportant category,
choose "needs_reply" so nothing critical is missed."""


class _RateLimiter:
    """Spaces calls out evenly. Shared by every thread in the process."""

    def __init__(self, per_minute):
        self._interval = 60.0 / max(per_minute, 1)
        self._lock = threading.Lock()
        self._next_at = 0.0

    def wait(self):
        with self._lock:
            now = time.monotonic()
            if now < self._next_at:
                time.sleep(self._next_at - now)
                now = time.monotonic()
            self._next_at = now + self._interval


_limiter = _RateLimiter(REQUESTS_PER_MINUTE)
_client = None
_client_lock = threading.Lock()


def get_client():
    global _client
    with _client_lock:
        if _client is None:
            _client = Groq(api_key=config.require_env("GROQ_API_KEY"), timeout=30.0, max_retries=0)
        return _client


def _ask(system_prompt, user_content, max_tokens=250):
    """One JSON call to Groq, with backoff. Raises if every attempt fails."""
    last_error = None
    for attempt in range(MAX_ATTEMPTS):
        _limiter.wait()
        try:
            response = get_client().chat.completions.create(
                model=MODEL,
                max_tokens=max_tokens,
                temperature=0,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
            )
            raw = (response.choices[0].message.content or "").strip()
            raw = raw.replace("```json", "").replace("```", "").strip()
            return json.loads(raw)
        except Exception as exc:  # rate limits, network blips, bad JSON
            last_error = exc
            if attempt < MAX_ATTEMPTS - 1:
                time.sleep(2 ** attempt * 2)
    raise last_error


def classify_email(sender, subject, snippet):
    """
    Returns {category, important, one_line_summary}.

    If classification fails for any reason the email is treated as important,
    so a technical problem never silently hides a real message.
    """
    user_content = f"From: {sender}\nSubject: {subject}\nBody snippet: {snippet}"

    try:
        parsed = _ask(SYSTEM_PROMPT, user_content, max_tokens=500)
    except Exception as exc:
        return {
            "category": "needs_reply",
            "important": True,
            "summary": (subject or "")[:120],
            "action": "Check this one - it could not be summarised",
            "deadline": "",
            "error": f"{type(exc).__name__}: {exc}",
        }

    category = str(parsed.get("category", "")).strip()
    summary = str(parsed.get("summary", "") or "").strip()
    action = str(parsed.get("action", "") or "").strip()
    deadline = parsed.get("deadline")
    deadline = str(deadline).strip() if deadline and str(deadline).lower() != "null" else ""

    if category not in VALID_CATEGORIES:
        return {
            "category": "needs_reply",
            "important": True,
            "summary": summary or (subject or "")[:120],
            "action": action or "Check this one",
            "deadline": deadline,
            "error": f"unexpected category: {category!r}",
        }

    return {
        "category": category,
        "important": category in IMPORTANT_CATEGORIES,
        "summary": summary or (subject or "")[:120],
        "action": action,
        "deadline": deadline,
    }
