# Email → Telegram Monitor

Watches 21 company mailboxes read-only and sends a Telegram message when mail
arrives, with a summary of what it says and what it needs from you. You can
also just ask it things — "is there new mail?", "balcom", "anything from KRA?".

Each person can choose to receive every email or only what matters: tenders,
invoices, deadlines and messages waiting on a reply.

Nothing is ever sent, deleted, moved or marked as read. The inboxes are
untouched; this only ever reads.

**Cost: $0/month.** Telegram is free, Groq's classification API is free at this
volume, and a free web service tier covers hosting.

## Other documents

- **OPERATIONS.md** - running it, troubleshooting, and the things that were
  painful to work out the first time
- **ROADMAP.md** - improvements not built yet, with the reasoning behind each
- **ENGINEERING-LOG.md** - the full build history: every bug, how it was found,
  and why each decision went the way it did

## How it works

Two halves, sharing the same config and mail-reading code:

**The watcher** — an external scheduler calls `/run-check` every 30 minutes:

    for each mailbox:  read what's new since last time (read-only IMAP)
                       ↓
                       ask Groq: is this important?
                       ↓
                       if yes → Telegram alert
                       ↓
                       record position, so nothing is ever alerted twice

**The secretary** — you message the bot, Telegram posts to `/telegram-webhook`:

    your question → Groq works out what you meant
                  → the right mailboxes are searched, in parallel
                  → you get a reply

### Files

| File | What it does |
|---|---|
| `config.py` | Loads `.env` and `config.yaml`. Imported first, everywhere. |
| `main.py` | The periodic check. Run directly for a one-off pass. |
| `poller.py` | Reads new mail from one mailbox. |
| `classifier.py` | Asks Groq whether one email matters. Rate limited. |
| `notifier.py` | Sends Telegram messages. |
| `agent.py` | Turns a plain-language question into an instruction. |
| `secretary.py` | Answers the question by querying mailboxes. |
| `search.py` | Keyword search, recent mail, unread mail. |
| `store.py` | Remembers position per mailbox, written atomically. |
| `mailutil.py` | Shared IMAP connection and email parsing. |
| `app.py` | The web service. |

## Going live — the whole checklist

Run `python show_setup.py` first. It prints every value you need below, plus
your ready-made webhook and scheduler URLs. Keep that output private.

**1. Create the web service**

On render.com: **New → Web Service** → connect `Sandrakimiring/KingdomMail`.

| Field | Value |
|---|---|
| Build command | `pip install -r requirements.txt` |
| Start command | `gunicorn app:app --workers 1 --threads 4 --timeout 120` |
| Instance type | Free |

One worker. More than one means duplicate alerts.

**2. Add the environment variables**

Under **Environment**, add every line `show_setup.py` printed: the 21 mailbox
passwords, `GROQ_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`,
`BOT_PASSCODE`, `RUN_TOKEN`, `TELEGRAM_WEBHOOK_SECRET`.

Deploy. When it finishes you get a URL like `https://kingdommail.onrender.com`.

**3. Check it started**

Open `https://YOUR-APP.onrender.com/status`. You should see
`"mailboxes_watched": 21` and an empty `mailboxes_missing_password`. If any
mailbox is listed there, its password did not make it into the environment.

**4. Connect Telegram**

Open the `setWebhook` URL from `show_setup.py` once in a browser. You should
see `{"ok":true,...}`. Then message the bot: send the passcode, then `/start`.

**5. Schedule the checks**

On cron-job.org, create a job:

- URL: the `/run-check?token=...` URL from `show_setup.py`
- Every 30 minutes

**6. Confirm it is alive**

Wait for one cycle, then ask the bot **are you working?**. It should report 21
mailboxes and the time of the last check. That is it — it now runs by itself.

### If something is wrong

| Symptom | Cause |
|---|---|
| Bot silent | Webhook not set, or the secret does not match |
| "Locked" | Send the passcode; sessions last 24 hours |
| A company missing from `/status` | Its password is not in the environment |
| No alerts ever | Check the scheduler is actually firing `/run-check` |
| Classification failing | The Groq model was retired — see section 2 |

## 1. IMAP details for each mailbox

In cPanel → **Email Accounts** → **Connect Devices**, use the hostname shown
under **Secure SSL Settings**, not `mail.yourdomain.com`.

This matters. On shared hosting the certificate is issued for the hosting
provider's own name (here `*.web-hosting.com`), so connecting as
`mail.yourdomain.com` fails certificate verification. All the mailboxes here
are on `business61-1.web-hosting.com`. If an account moves to another server,
update `imap_host` in `config.yaml`.

Port is `993`. The mailbox's normal password works for IMAP — no need to reset it.

## 2. Free Groq API key (for classification)

1. Sign up at console.groq.com — no credit card
2. Create an API key, put it in `.env` as `GROQ_API_KEY`
3. Free tier: 30 requests/minute. The code stays under this on purpose.

The model is set by `GROQ_MODEL` (default `openai/gpt-oss-120b`). Groq retires
models from time to time — if classification starts failing, list the current
ones and update that value:

```bash
python -c "import config;from groq import Groq;print([m.id for m in Groq(api_key=config.require_env('GROQ_API_KEY')).models.list().data])"
```

## 3. Free Telegram bot (for alerts)

1. Message **@BotFather** → `/newbot` → follow prompts → put the token in
   `.env` as `TELEGRAM_BOT_TOKEN`
2. Send your new bot any message, so it has a chat open with you
3. Visit `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` and find
   `"chat":{"id": ...}` — that number goes in `.env` as `TELEGRAM_CHAT_ID`

`TELEGRAM_CHAT_ID` must be digits only. A stray comma or space is a common
copy-paste slip; the code strips it, but it's worth getting right.

## 4. Install and test locally

```bash
python -m venv venv
venv\Scripts\activate          # Windows
source venv/bin/activate       # macOS / Linux
pip install -r requirements.txt
cp .env.example .env           # then fill in real values
python main.py
```

The first run against a mailbox only records where that mailbox currently is
and alerts on nothing. That's deliberate — without it, the first run would try
to classify every email already sitting in every inbox. Real alerts start from
the second run onward.

## 5. Deploy

1. Push this folder to a GitHub repo (`.env` is git-ignored — confirm with
   `git status` before the first push)
2. New → Web Service → connect the repo
3. Set:
   - **Build command:** `pip install -r requirements.txt`
   - **Start command:** `gunicorn app:app --workers 1 --threads 4 --timeout 120`
   - **Instance type:** Free
4. Add every `.env` value under the **Environment** tab
5. Deploy

Use **one worker**. The run lock and the state file assume a single process;
more workers means overlapping runs and duplicate alerts.

### State and restarts

Position is kept in `state.json` next to the code. On a host with an ephemeral
filesystem that file is lost on every redeploy, and each mailbox re-records its
position — so you get no flood, but mail that arrived during the restart is not
alerted on. To keep it across restarts, set `STATE_DIR` to a persistent path.

## 6. Schedule it

Use a free scheduler (cron-job.org) to call `/run-check` every 30 minutes. This
triggers the check and keeps a free service from sleeping.

- URL: `https://your-app.onrender.com/run-check?token=YOUR_RUN_TOKEN`
- Schedule: every 30 minutes

Set `RUN_TOKEN` in the environment to match. Without it the endpoint is open to
anyone who learns the URL. The endpoint replies immediately and does the work in
the background, so the scheduler will not time out.

## 7. Secretary mode

Point Telegram at the webhook, including the secret:

```
https://api.telegram.org/bot<YOUR_BOT_TOKEN>/setWebhook?url=https://your-app.onrender.com/telegram-webhook&secret_token=YOUR_WEBHOOK_SECRET
```

`YOUR_WEBHOOK_SECRET` must match `TELEGRAM_WEBHOOK_SECRET` in the environment.
You should see `{"ok":true,...}`.

Then message the bot. `/start` gives the welcome and the full list:

- **is there new mail?**
- **anything come in today?**
- **any unread mail?**
- **find email from KRA**
- **any mail from Beacon about the tender?**
- **are you working?** — mailbox count, last check, alerts sent

Mailboxes are queried in parallel, so a question across all of them takes
roughly 10-20 seconds.

`/status` in a browser gives the same health information as JSON.

## Who can use the bot

Three layers, outermost first.

**1. The webhook secret.** `TELEGRAM_WEBHOOK_SECRET` is sent by Telegram as a
header on every call and checked before anything else. Without it, anyone who
learned the public URL could post a fake message claiming to be you. Set it.

**2. The allow-list.** Only chat ids in `TELEGRAM_CHAT_ID` and
`TELEGRAM_ALLOWED_CHAT_IDS` get answers. Telegram supplies the real sender's
id with every message, so this cannot be faked by someone messaging the bot —
finding the bot's username gets them nowhere.

Anyone else gets a short "this assistant is private" reply, the attempt is
written to the log, and you get a one-time message naming them and their chat
id so you can add them if they belong.

**3. A rate limit.** Ten questions per minute per person, so an approved
account cannot hammer the mail server.

### Who receives alerts

Being approved lets someone **ask** questions. Receiving **pushed** alerts is
separate, so a person can have access without their phone lighting up for every
email that arrives.

| Command | What it does |
|---|---|
| `/alerts` | Shows who receives alerts, and at which level |
| `/alerts <id> important` | Only tenders, invoices, deadlines and replies awaited |
| `/alerts <id> all` | Every email |
| `/alerts <id> off` | Stops their alerts; they can still ask questions |

You always receive alerts. Others receive none until you turn them on, and only
an approved person can be a recipient, so revoking access also stops the alerts.

These settings live in `alerts.json`, which is lost when a host without a
persistent disk restarts. `TELEGRAM_ALERT_CHAT_IDS` in the environment is the
durable version and takes the form `111111:all,222222:important`.

### Adding someone

You do not need to know anyone's Telegram id in advance.

1. Ask them to message the bot. Anything will do.
2. They are refused, and you get a message naming them with a ready command:
   `/approve 612345678`
3. Send that command. They are let in immediately and get the welcome message.

Other commands, owner only:

| Command | What it does |
|---|---|
| `/who` | Lists everyone approved |
| `/approve <id> <name>` | Adds someone |
| `/revoke <id>` | Removes someone and ends their session |

Approvals made this way are written to `approved.json` in the state directory.
That file is lost if the host wipes its disk on redeploy, so once someone is
settled, add their id to `TELEGRAM_ALLOWED_CHAT_IDS` in the environment, which
is permanent. `/who` marks which are which.

Only the chat in `TELEGRAM_CHAT_ID` can approve anyone. Owner commands work
even when the bot is locked, so you can never lock yourself out of managing access.

### If the bot token leaks

A stolen token does not expose the mailboxes — mail is only ever read by the
service, and the webhook secret plus the allow-list still apply. But whoever
has it can send messages that look like they came from your bot. Revoke it in
@BotFather with `/revoke`, put the new token in the environment, and call
`setWebhook` again with the secret.

Everything the bot answers is a sender, subject and date — never message
bodies, and never attachments.

## 8. Adding more mailboxes

Add a block under `mailboxes:` in `config.yaml`, add its password to `.env` and
to the host's environment. Nothing else changes. A newly added mailbox records
its position on the next run and alerts from the run after.

Set `mode: "new_senders_only"` on a mailbox to only get alerts from people who
have not written before — intended for a personal inbox. The default is
`mode: "all"`.

## Tuning

| Where | Setting | Default |
|---|---|---|
| `config.yaml` | `poll_interval_minutes` | 30 |
| `poller.py` | `MAX_PER_RUN` — emails handled per mailbox per run | 40 |
| `classifier.py` | `REQUESTS_PER_MINUTE` | 20 |
| `main.py` | `MAX_PARALLEL_FETCH` | 6 |
| `secretary.py` | `MAX_PARALLEL` / `PER_MAILBOX_LIMIT` | 8 / 3 |

A backlog above `MAX_PER_RUN` is not skipped — the oldest are handled first and
the rest come on the next run.

## If you outgrow the free tier

- **Groq → Claude API**: swap the call in `classifier.py` for Anthropic's
- **Telegram → WhatsApp**: swap `notifier.py` for Meta's WhatsApp Cloud API
- **Free → paid hosting**: for an always-on service and a persistent disk

## What this does NOT do

- Does not forward, merge, or change mailbox settings
- Does not send, reply or delete — read-only IMAP, folders opened read-only,
  bodies fetched with `BODY.PEEK` so nothing is marked as read
- Does not store email content long-term — only which UID was last handled
