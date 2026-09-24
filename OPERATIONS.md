# Running it

Day-to-day notes, and the things that were painful to work out the first time.
For setup, see README.md. For future ideas, see ROADMAP.md.

## What is where

| | |
|---|---|
| Code | github.com/Sandrakimiring/KingdomMail, branch `main` |
| Service | https://kingdommail.onrender.com (Render, free tier) |
| Start command | `gunicorn app:app --workers 1 --threads 4 --timeout 120` |
| Mail server | `business61-1.web-hosting.com:993`, all 21 inboxes |
| Classification | Groq, `openai/gpt-oss-120b` |
| Alerts | Telegram |
| Scheduler | cron-job.org |

Render redeploys automatically on every push to `main`. Environment variable
changes restart the service on their own — no push needed.

## Endpoints

| | |
|---|---|
| `/` | Health check, 22 bytes. Used by the keep-alive job |
| `/status` | Inbox count, missing passwords, last run, whether a passcode is set |
| `/run-check?token=...` | Runs a check. Answers `202` immediately, works in the background |
| `/telegram-webhook` | Telegram posts here. Requires the secret header |

## Scheduled jobs

Two are needed, and the second is not optional:

| Job | URL | Interval |
|---|---|---|
| Check | `/run-check?token=...` | 30 minutes |
| Keep-alive | `/` | **10 minutes** |

Render's free tier sleeps the container after about 15 minutes idle. Waking it
takes 30 to 50 seconds — measured at 33s — and cron-job.org gives up at 30, so
**without the keep-alive every scheduled check fails.** Pinging under the
15 minute threshold keeps it awake, which also keeps `state.json` alive between
checks.

## Telegram commands

Anyone approved:

| | |
|---|---|
| `/start` | The welcome message |
| `/status` | Health and last check |
| `/lock` | Ends the session early |
| plain questions | "is there new mail?", "balcom", "anything from KRA?" |

Owner only, and these work even while locked so you cannot lock yourself out of
managing access:

| | |
|---|---|
| `/who` | Everyone approved |
| `/approve <id> <name>` | Adds someone |
| `/revoke <id>` | Removes them and stops their alerts |
| `/alerts` | Who receives pushed alerts, at which level |
| `/alerts <id> important｜all｜off` | Sets one person |

## Things that cost time to work out

**The IMAP hostname is not `mail.<domain>`.** This is shared hosting and the
certificate is issued for `*.web-hosting.com`, so connecting as
`mail.balcom.solutions` fails verification for all 21 inboxes. The working name
is the server's own, `business61-1.web-hosting.com`, shown in cPanel under
Email Accounts, Connect Devices, Secure SSL Settings. If an account is moved to
another server, find the new name the same way, or by reverse DNS on the IP.

**Groq retires models.** `llama-3.3-70b-versatile` disappeared and every
classification broke with a 404. To see what is currently available:

    python -c "import config;from groq import Groq;print([m.id for m in Groq(api_key=config.require_env('GROQ_API_KEY')).models.list().data])"

Then set `GROQ_MODEL` in the environment. No code change needed.

**`gpt-oss` is a reasoning model and its thinking counts against `max_tokens`.**
Too small a budget and it returns an empty string, which surfaces as
`json_validate_failed`, not as a length error. If parsing starts failing for
short messages, raise `max_tokens` before suspecting the prompt.

**`TELEGRAM_CHAT_ID` must be digits only.** A trailing comma from copy-paste
still works for sending alerts, because Telegram is lenient, but silently
breaks the webhook's allow-list check — so alerts arrive while questions are
ignored. The code strips non-digits now, but it is a confusing failure.

**IMAPClient 3.x does not work on Python 3.13+**, and `groq` below 1.x does not
work with `httpx` 0.28+. Both are pinned in requirements.txt with a comment.

**The disk is wiped on every restart.** See ROADMAP.md item 1. The 40 minute
lookback in `config.yaml` covers it: a mailbox with no stored position
re-checks recent mail rather than starting silently. A restart therefore costs
a repeated alert instead of a missed email.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Scheduled checks failing | Keep-alive job missing; cron times out on a cold start |
| `x-render-routing: no-deploy` | A deploy was in progress. Transient |
| Bot silent | Instance asleep — first message takes ~30s. Send it again |
| "Locked" | Send the passcode. Sessions last 24 hours |
| Approved people vanished | Service restarted. Put them in `TELEGRAM_ALLOWED_CHAT_IDS` |
| An inbox missing from `/status` | Its password is not in the environment |
| Every run says "bootstrapped" | Container is restarting between runs. Check the keep-alive |
| Classification failing | Model retired. See above |
| A question checks all inboxes | The company name is ambiguous. "kingdom" and "salem" span several, so they check everything on purpose |

## Checking on it

    curl https://kingdommail.onrender.com/status

`last_run` should be recent and `errors` empty. If `bootstrapped` is 21 on
consecutive runs, the container is being recycled between them.

Locally, `python main.py` runs one check against the live mailboxes and prints
what it finds. It is read-only and safe to run at any time, though it will send
real alerts for anything genuinely new.

`python show_setup.py` prints every environment value plus the ready-made
webhook and scheduler URLs. Output contains secrets, so keep it local. Both it
and `render_env.txt` are git-ignored.

## Safety properties

Worth not breaking:

- Every folder is opened `readonly=True`, and bodies are fetched with
  `BODY.PEEK`, so nothing is ever marked read, moved, or deleted
- There is no SMTP anywhere — the service cannot send mail as anyone
- Position is tracked by UID, never by the unread flag, so the inbox can be
  used normally without affecting what gets alerted
- The bot answers only approved chat ids, and only with sender, subject and
  date — never message bodies or attachments
