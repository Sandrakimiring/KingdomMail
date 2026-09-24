# Engineering log

The full story of how this was built on 23-24 Sep 2026: every bug, how it was
actually found, why each decision went the way it did, and what was rejected.

`OPERATIONS.md` tells you how to run it. This tells you why it is the way it is,
and how the problems were tracked down, which is the part worth reusing on the
next project.

---

## The method, in one paragraph

Almost every bug here was invisible from reading the code and obvious within
seconds of running it. A careful code review found eleven real issues. Running
the thing found five more that were worse, including three that meant it could
never have worked at all. **Reading code tells you what it intends. Running it
tells you what it does.** Everything below follows from taking that seriously:
never assert a fix works, demonstrate it; never trust a model id, a hostname or
a version pin, check it; and when something looks fine but behaves oddly, get
data before forming a theory.

---

## Part 1 - The starting point

The original code was seven Python files that looked reasonable. Read-only IMAP,
Groq classification, Telegram alerts, a Flask app with a webhook. The
architecture was sound and the read-only guarantee was genuinely respected, and
that part survived untouched.

It had never successfully run.

### It crashed on import

`main.py` imported `classifier` on line 18 and called `load_dotenv()` on line 21.
`classifier.py` read `os.environ["GROQ_API_KEY"]` at module level. So the key was
read three lines before the file that defines it was loaded.

Found by running `python -c "import main"` with the environment cleared. Not by
reading: the import order looks perfectly normal until you notice `load_dotenv()`
sits below the imports, which is exactly where every style guide puts it.

Fixed with `config.py`, imported first everywhere, which loads `.env` at import
time before anything else can touch `os.environ`. The Groq client also became
lazy, so a missing key gives a clear message instead of killing the process at
import.

### The dependencies could not coexist

`groq==0.11.0` passes a `proxies` argument that `httpx` 0.28 removed, giving a
`TypeError` on client construction. `requirements.txt` did not pin `httpx`, so a
fresh install anywhere reproduced it.

### The first run would have processed every email ever received

`state.json` did not exist, so `last_uid` was 0 for every mailbox, so the poller
selected every UID in every inbox and fetched them in one command.

The review flagged this as a risk. The number made it real: **7,401 messages
across the 21 inboxes.** At the free tier's 30 requests per minute that is four
hours of API calls and thousands of Telegram messages, and it would have been
killed by the worker timeout long before finishing.

Fixed by bootstrapping: the first time a mailbox is seen, record where it is and
alert on nothing.

### Gunicorn would have killed every run

`/run-check` did the entire 21-inbox loop inside the web request. Gunicorn's
default worker timeout is 30 seconds; the loop takes minutes.

Fixed by answering `202` immediately and doing the work on a background thread.
Measured afterwards at 0.006 seconds.

### Any single failure lost the whole run

State was saved once, after the loop, and `classify_email` and `send_alert` were
not wrapped. A Groq 429, a Telegram flood limit, or the timeout above meant
nothing was saved, so the next run reprocessed the same emails, hit the same
failure, and alerted on everything again. A permanent duplicate loop.

Fixed by saving position per mailbox as each one finishes, and isolating errors
per email.

### The rest

Ephemeral disk wiping state; MIME bodies fed to the classifier as raw base64;
unescaped HTML crashing a run on a subject containing an ampersand; strict UTF-8
header decoding raising on 8-bit bytes; no UIDVALIDITY check, so a server
renumbering would silently stop all alerts forever; no auth on `/run-check`;
config drift between the README's 13 mailboxes and the actual 21.

The MIME one was the worst for quality. The old code sliced 400 characters off
the raw body, which for any multipart email is boundaries and base64:

    --_000_abc_ Content-Type: text/html; charset=utf-8 ... PGh0bWw+PGJvZHk

So the classifier was judging on sender and subject alone, sometimes with noise
actively confusing it. `import email` was present in `poller.py` and unused: the
body parsing had been intended and never written.

---

## Part 2 - What only running it revealed

### The model did not exist

Both AI modules used `llama-3.3-70b-versatile`. Groq had retired it. Every
classification would have returned a 404.

Found by listing the account's available models rather than assuming the id in
the code was current. That habit, check the external thing actually exists,
caught it in one call. It is a documented step in `OPERATIONS.md` now, because
it will happen again.

Replaced with `openai/gpt-oss-120b`, verified with JSON mode at 0.6s.
`openai/gpt-oss-20b` was tried and rejected: it failed JSON mode outright.

### IMAPClient did not work on the installed Python

`AttributeError: property 'file' of 'IMAP4_TLS' object has no setter`. Python
3.13 made an `imaplib` attribute read-only and IMAPClient 3.x assigns to it. The
venv was on 3.14, so nothing could connect.

This only surfaced *after* the TLS problem below was solved, because the
connection failed earlier for a different reason. Layered failures hide each
other, which is why "it still does not work" after a fix is not evidence the fix
was wrong.

### Every IMAP hostname in the config was wrong

All 21 mailboxes failed certificate verification, and five also failed DNS.

Diagnosis, in order:

1. Inspected the certificate with hostname checking off. It was issued for
   `*.web-hosting.com`: shared hosting, not the domain.
2. Reverse DNS on the IP gave `business61-1.web-hosting.com`, which the wildcard
   covers.
3. Confirmed with a real login. Full verification passed.
4. Probed all 21 in parallel to check they were on the same server. They were,
   including the five whose `mail.<domain>` did not resolve at all.

The tempting shortcut was to disable certificate verification. That would have
worked and been wrong: it removes the protection that makes it safe to send
mailbox passwords over the wire. Finding the correct hostname kept verification
intact.

### State does not survive a restart

Three consecutive runs on Render told the story:

| Run | Bootstrapped |
|---|---|
| 14:56 | 21 |
| 16:24, after a restart | **21 again** |
| 16:25, same container | 0 |

State persists inside a container and dies with it. Render's free tier sleeps
after about 15 minutes idle and the scheduled check was 30 minutes apart, so
every check would have woken a fresh container, re-bootstrapped, and alerted on
nothing. **The service would have looked perfectly healthy and never told anyone
about a single email.**

This was the most dangerous bug in the project and it was only visible by
comparing three runs. A single successful run proves nothing about a system
whose failure mode is silence.

Fixed by changing what "no stored position" means. It used to mean "alert on
nothing". It now means "re-check the last 40 minutes", because on a host that
restarts, the old behaviour is indistinguishable from being broken. A restart
costs a repeated alert instead of a missed email, which is the right way round.

Verified by deleting the state file and running against the live mailboxes: 17
quiet inboxes recorded their position silently and the 2 with recent mail
recovered and alerted on 4 real emails the old code would have dropped.

### A trailing comma broke half the system

`TELEGRAM_CHAT_ID` in `.env` ended with a comma. Telegram's send API tolerated
it, so alerts arrived normally. The webhook's allow-list did an exact string
comparison, so **every question the owner asked was silently ignored.** Half the
product was dead and the working half hid it.

Found because the same request succeeded through Flask's test client and failed
through curl. That difference is the clue: identical code, different input, so
the bug is in the input, not the logic. The shell variable had captured the
comma.

Fixed in the data and in the code, which now compares digits only. A config
typo should not be able to disable a feature silently.

### The company filter ignored the company

Asking about one company searched all 21 inboxes. Two causes stacked.

First, the model was returning nothing at all for short messages. `gpt-oss`
reasons before answering and those tokens come out of `max_tokens`, so a
200-token budget left no room for the JSON and the call failed validation with
an empty generation. The error said `json_validate_failed`, which points at the
prompt, not at the budget. Confirmed by running the same call at 200 and 800
tokens.

Second, and worse, the design leaned on the model to notice a company name at
all. When the call failed, the fallback never looked for one.

Fixed on both sides: the budget went up, and the company is now matched locally
against `config.yaml` before the model is asked, with that match winning. Only
words belonging to exactly one company count, so `balcom` and `geowells` resolve
while `kingdom` and `salem` stay ambiguous and check everything, which is the
safe answer. Twelve phrasings were tested; all pass.

The lesson worth keeping: **do not ask a model to do something a lookup can do.**
The list of companies is right there in the config.

### The cron kept failing and the service was fine

Scheduled checks failed for hours. The service answered `202` in under half a
second whenever it was checked by hand.

The resolution came from one measurement: a plain request to the root URL took
**33 seconds.** The instance had been asleep. cron-job.org gives up at 30. Every
scheduled check was waking a sleeping container and timing out.

Nothing was wrong with the code. The fix is a second scheduled job hitting the
root every 10 minutes, under the 15-minute sleep threshold, which also keeps the
state file alive between checks and so fixes the restart problem above in
practice.

Worth noting how close this came to being misdiagnosed. Every manual check
passed, because checking it woke it up.

---

## Part 3 - Why things are the way they are

**Position is tracked by UID, never by the unread flag.** The inboxes are used by
real people. If alerts depended on read status, reading an email in Outlook
would change what the service does, and the service would be pressured into
marking things read. Tracking UIDs keeps the two completely independent: 223
unread in one inbox, 1 pending alert, no relationship between them.

**Everything is read-only, structurally.** Folders open `readonly=True`, bodies
fetch with `BODY.PEEK`, and there is no SMTP dependency anywhere in the project.
Not as a policy but as an inability. This was the one property the original code
got right and it was preserved through every rewrite.

**Fetching is parallel, classification is serial.** Reading 21 mailboxes is
network waiting, so it runs on six threads. Classification goes through a shared
rate limiter at 20 requests per minute to stay inside the free tier. Mixing those
concerns would either waste time or trip the limit.

**Failures fail loud.** A classification error marks the email important so a
human sees it. An unparseable category does the same. A mailbox with no position
re-checks recent mail rather than starting silently. In a system whose job is to
tell you about things, silence is the expensive mistake and a duplicate is cheap.

**Access is approved from inside Telegram.** The original plan needed each
person's numeric chat id up front, and there is no practical way to look one up
for someone else. Now anyone messages the bot, gets refused, and the owner
receives their details with a ready `/approve` command. The friction disappeared
by moving the identity discovery into the flow instead of ahead of it.

**Alerts and access are separate.** Being approved lets someone ask questions.
Receiving pushed alerts is a second, explicit step with its own level. Whoever
runs the inboxes wants everything; someone senior wants tenders and invoices
without 21 inboxes of marketing. One switch could not serve both.

**Security is four independent layers.** The webhook secret stops forged posts;
the allow-list stops strangers, and cannot be faked because Telegram supplies the
real sender id; the passcode covers an approved person's unlocked phone; the rate
limit stops an approved account hammering the mail server. Each one assumes the
others might fail.

---

## Part 4 - Decisions that went the other way

**Disabling certificate verification** to fix the TLS errors. Rejected: it would
have shipped 21 mailbox passwords over unverified connections. Finding the real
hostname took twenty minutes.

**A daily digest instead of individual alerts.** Rejected on data. Measured
volume is about 17 emails a day, which is comfortable as individual messages. It
was a solution to a problem that did not exist. Written into `ROADMAP.md` with
the number, so the judgement can be rechecked when volume changes.

**Skipping the AI on obvious bulk mail** using the `List-Unsubscribe` header, to
save API calls. Built as `mailutil.looks_like_bulk`, then left unused: measured
usage is 1.7% of the daily free limit, so there was nothing to save, and the risk
of silently hiding a real email was not worth it. The function stays for when
volume justifies it.

**A shared enrollment code** so new people could add themselves. Rejected because
it turns one guessable string into full access to company email metadata. Owner
approval keeps a human in the loop and costs one command.

**Letting the model decide everything** about a question. Steadily walked back.
The company is matched locally, the category is validated against a fixed set,
and `important` is derived from the category rather than trusting a separate
boolean that could contradict it. The model is good at summarising and bad at
being a reliable component.

---

## Part 5 - Environment traps

Things that wasted time and will again.

**Heredocs here strip backslashes.** `"\n".join(parts)` written through a bash
heredoc becomes a literal newline inside the string, producing
`SyntaxError: unterminated string literal`. It happened five times. Write files
with the editor tool, or build the escape with `chr(92)`.

**Backticks inside a double-quoted `bash -c` are executed.** A Markdown table
full of `` `commands` `` came out with every cell empty, because the shell ran
them and substituted the output. Silent and easy to miss.

**`pkill -f` does not work on Windows.** A stale Flask process kept serving the
old code on port 8080 and every test result was wrong until it was killed by PID
via `netstat`. When a fix provably works in isolation but not through the
server, suspect the server is not the one you just changed.

**Python buffers stdout when not attached to a terminal.** A background run
looked hung for minutes with no output. Use `-u`.

**A laptop sleeping mid-run** produced a 9,611 second duration in the logs. Not a
hang, and worth recognising before debugging it as one.

**Windows consoles are cp1252.** Printing an em dash or an emoji raises
`UnicodeEncodeError`, which looks like a bug in the code being tested rather
than in the printing. Set `PYTHONIOENCODING=utf-8` before concluding anything.

---

## Part 6 - What was verified, and how

Nothing below was assumed.

| Claim | How it was shown |
|---|---|
| Read-only | Searched the whole codebase for STORE, COPY, MOVE, DELETE, EXPUNGE, APPEND and SMTP. None exist |
| All 21 inboxes reachable | Logged into all 21 in parallel with full TLS verification |
| Classification is accurate | Four known emails: tender, marketing, a real question, an automated digest. All four correct |
| MIME parsing works | Built a multipart email with base64 HTML and a large attachment; compared old output (base64 noise) with new (clean text) |
| HTML escaping works | Escaped the exact subject that would have crashed a run |
| Bootstrap prevents the flood | Ran against 7,401 real messages. Zero alerts |
| No duplicate alerts | Ran twice. Second run found nothing new |
| Alerts actually arrive | Confirmed by the recipient, twice, including the new format |
| Endpoint is fast enough | Timed at 0.006s locally, 0.4s deployed |
| Security is enforced | Tested no secret, wrong secret, stranger, non-owner privilege escalation, duplicate delivery, rate limit |
| Passcode works | Locked, wrong code, correct code, lock again |
| Alert levels work | Owner and a second recipient at different levels; confirmed marketing reached only one |
| Company filter works | Twelve phrasings, zero failures |
| State recovery works | Deleted the state file and ran against live mail |
| Secrets never committed | Every real value from `.env` checked against the staged diff before each push |

---

## Part 7 - If you build the next one

What generalises from this project.

**Run it before you review it.** The review was careful and correct and still
missed the three things that made the system non-functional: a retired model, an
incompatible library, and wrong hostnames. None were visible in the source.

**Check every external assumption.** Model ids, hostnames, library versions,
API limits. Three of the worst bugs here were external things the code named
correctly at the time and which had since changed or were never right.

**Measure before designing.** The digest, the bulk filter and the alert-volume
worry all evaporated against one measurement of actual mail volume. Numbers
first, architecture second.

**Design for the failure mode you cannot see.** The worst bug was one where
everything looked healthy and nothing was delivered. Any system that reports by
exception needs a way to prove it is alive, which is why the dead-man's switch
is the top unbuilt item in `ROADMAP.md`.

**Prefer a duplicate to a miss.** Almost every ambiguous decision here resolved
that way: unparseable classification means important, lost state means re-check,
ambiguous company name means search everything.

**Do not ask a model to do a lookup.** The company list is in the config file.
Matching it there is instant, free, deterministic, and does not fail when the
API does.

**Test through the same path the user takes.** The trailing comma bug passed
every direct test and failed only through the real HTTP path. So did the cold
start, which passed every manual check because checking it woke the service up.
