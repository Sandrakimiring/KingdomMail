# Future work

Ideas not built yet, most useful first. Each says why it matters, what it
would touch, and roughly how big it is. Nothing here is urgent — the service
works without any of it.

Measured baseline when this was written (23 Sep 2026), so you can judge
whether an idea still applies:

| | |
|---|---|
| Mail volume | ~17 emails/day across all 21 inboxes (118 over 7 days) |
| Busiest inbox | Kingdom Foundation, 73 of those 118 (62%) |
| Existing mail | 7,401 messages, 1,624 unread |
| Groq usage | ~17 calls/day against a 1,000/day free limit (1.7%) |
| A full check | 15–25 seconds for all 21 inboxes |
| A Telegram question | 5s for one inbox, ~18s for all 21 |

---

## 1. Durable storage

**The problem.** `state.json`, `approved.json` and `alerts.json` all live on the
host's disk. Render's free tier has no persistent disk, so every restart wipes
them. Consequences: mailbox positions reset (handled by the 40 minute lookback,
but it means repeated alerts), and anyone approved with `/approve` or given
alerts with `/alerts` silently disappears.

**Why it matters.** This is the only structural weakness left. Everything else
is a feature; this is the thing that will keep biting.

**How.** Replace the file reads and writes in `store.py` and the three
`_load_*`/`_save_*` pairs in `auth.py` with a small key-value interface, then
back it with one of:

- Render free Postgres — same dashboard, no new account, expires and needs
  recreating on the free plan
- Upstash Redis — HTTP REST API, so `requests` is enough and no driver is
  needed, generous free tier, one extra signup

**Effort.** Half a day. The interface is already narrow: everything goes
through `store.State` and the `auth` helpers, so no other module changes.

**Interim workaround, already in place.** Keep the instance awake with a
10 minute keep-alive ping so restarts are rare, and put settled people in
`TELEGRAM_ALLOWED_CHAT_IDS` and `TELEGRAM_ALERT_CHAT_IDS`, which are environment
variables and survive.

---

## 2. A dead-man's switch

**The problem.** If the service stops, nothing says so. Silence from the bot
looks exactly like "no new mail". You could go days assuming it is working.

**Why it matters.** For a system whose whole job is to tell you about things,
failing silently is the worst possible failure. It is also cheap to fix.

**How.** Two options, and the second is better:

- A scheduled job that reads `/status` and messages you if `last_run` is
  missing or older than two hours. Needs somewhere to run.
- An external uptime monitor (healthchecks.io has a free tier) that expects a
  ping from `/run-check` on a schedule and alerts you when one does not arrive.
  Add a `requests.get(HEALTHCHECK_URL)` at the end of a successful `run_once()`.

**Effort.** An hour, mostly signing up.

---

## 3. Attachment visibility

**The problem.** `extract_snippet()` in `mailutil.py` deliberately skips
attachments to keep the classifier fed with readable text. So a tender email
whose entire substance is a PDF looks almost empty.

**Why it matters.** Tenders and invoices are the core of this business and they
arrive as attachments. The alert currently cannot tell you a bid document is
sitting there.

**How.** While walking the MIME parts in `extract_snippet()`, collect the
filename and size of any part with `Content-Disposition: attachment`. Pass the
list into `classify_email()` so the model knows, and add a line to the alert:

    📎 3 attachments: tender_docs.pdf, boq.xlsx, drawings.zip

Reading inside the PDFs is a much larger job and probably not worth it — the
filename alone tells you most of what you need.

**Effort.** Two to three hours. Touches `mailutil.py`, `classifier.py`,
`notifier.py`.

---

## 4. Reply awareness

**The problem.** The service only reads INBOX, so it has no idea whether you
already answered something. A thread you have dealt with keeps being classified
`needs_reply`.

**How.** Read the Sent folder too (read-only, same as INBOX), collect the
`In-Reply-To` and `References` headers, and when an incoming message is part of
a thread that has already been answered, either downgrade it or say so in the
alert. Folder names vary by server — `INBOX.Sent` on this cPanel host — so it
needs a configurable name with a sensible fallback.

**Effort.** Half a day, mostly on thread matching.

---

## 5. Quiet hours

**The problem.** 17 emails a day is comfortable, but some of them land at 3am.

**How.** A `quiet_hours` setting in `config.yaml`, checked in `notifier.py`.
Hold anything that is not `tender_or_business_critical` in a small queue and
release it at the start of the next allowed hour. Needs the queue to survive a
restart, so it is easier after item 1.

**Effort.** Two hours, or an hour if storage is already sorted.

---

## 6. Model fallback

**The problem.** `llama-3.3-70b-versatile` was retired by Groq and every
classification broke. It was only caught by listing the available models by
hand. The same will happen again.

**How.** On a 404 or `model_not_found` from Groq, call `models.list()`, pick the
first from a preference list that is actually available, keep using it, and
message you once to say the model changed. The model id is already a variable
(`GROQ_MODEL`), so this is contained inside `classifier.py`.

**Effort.** Two hours.

---

## 7. Watch more than INBOX

**The problem.** Only INBOX is read. Any cPanel rule that files mail into a
folder makes it invisible to this service.

**How.** An optional `folders` list per mailbox in `config.yaml`, defaulting to
`["INBOX"]`. `poller.py` would need one position per folder rather than per
mailbox, so the state shape changes.

**Effort.** Half a day. Worth checking whether any rules exist first — if none
do, skip this entirely.

---

## Deliberately not doing

- **A daily digest.** At 17 emails a day the individual alerts are fine. Revisit
  if volume triples.
- **Threading or grouping.** Same reason.
- **A web archive of alerts.** Telegram already keeps the history and is
  searchable.
- **Reading inside attachments.** Large effort, and the filename usually says
  enough.
