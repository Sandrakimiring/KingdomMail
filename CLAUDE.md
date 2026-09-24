# KingdomMail

Read-only email monitor for 21 company inboxes. Sends Telegram alerts with a
summary and a suggested action, and answers plain-language questions about the
mail. Python, Flask, IMAP, Groq, deployed on Render's free tier.

**Read `OPERATIONS.md` before changing anything** — it records the failures that
cost time to diagnose (wrong IMAP hostname, retired Groq model, reasoning tokens
eating `max_tokens`, a trailing comma in a chat id). `ROADMAP.md` has what is
deliberately not built yet, and why.

## Layout

| File | Role |
|---|---|
| `config.py` | Loads `.env` **before** anything reads `os.environ`. Import it first, everywhere |
| `main.py` | The periodic check. `python main.py` runs one pass |
| `poller.py` | Reads new mail from one inbox |
| `classifier.py` | Groq call. Owns the rate limiter every AI call goes through |
| `agent.py` | Turns a question into an instruction. Matches company names locally |
| `secretary.py` | Answers questions; owns all user-facing copy |
| `notifier.py` | Telegram. Fans alerts out to recipients at their own level |
| `auth.py` | Allow-list, passcode, approvals, alert recipients, audit log |
| `store.py` | Mailbox positions, written atomically |
| `mailutil.py` | IMAP connections and email parsing |
| `app.py` | Flask. Endpoints answer immediately, work happens on a thread |

## Rules

**Never commit secrets.** `.env`, `show_setup.py`, `render_env.txt`,
`state.json`, `approved.json`, `alerts.json`, `sessions.json` are all
git-ignored. Before any push, check the staged diff against the real values in
`.env`, not just the filenames.

**Keep it read-only.** Folders are opened `readonly=True`, bodies fetched with
`BODY.PEEK`, and there is no SMTP anywhere. Position is tracked by UID, never
by the unread flag, so the inboxes can be used normally. Do not break this.

**One gunicorn worker.** The run lock and the state file assume a single
process. More workers means duplicate alerts.

**The host has no persistent disk.** Anything written to disk is lost on
restart. Durable settings belong in environment variables.

**Fail loud, not silent.** A classification error marks the email important so
a human sees it. A mailbox with no stored position re-checks recent mail rather
than starting silently. Prefer a repeated alert over a missed email.

## User-facing copy

Plain and direct. This is read by a CEO on a phone.

No "I can help you with", no explaining how to phrase a question, no
apologising. State what happened. `secretary.py` holds the welcome and reply
text; match its tone.

Dates read `Today 15:21`, not `2026-09-23T15:21Z`. Say `3 inboxes`, not
`3 inbox(es)`.

## Working on this

Verify against the live system rather than reasoning about it — the mailboxes,
Groq and Telegram are all reachable from the dev machine, and most bugs here
were only visible when actually run.

`python main.py` is safe to run any time. It is read-only, though it will send
real alerts for genuinely new mail.

Bash heredocs in this environment strip backslashes, so `\n` inside a heredoc
becomes a real newline and breaks Python string literals. Write such files with
the Write tool, or build the escape with `chr(92)`. Backticks inside a
double-quoted `bash -c` are executed — that will silently gut Markdown tables.

## Commit messages

Explain why the change was needed and what went wrong without it. Describe
behaviour, not a list of edited files. No bullet-point summaries of the diff.
