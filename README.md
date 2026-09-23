# Email → Telegram Monitor

Watches several mailboxes read-only and sends a free Telegram message only for
emails an AI decides are important. You can also just ask it questions in
Telegram — "is there new mail?", "find email from KRA".

Nothing is ever sent, deleted, moved or marked as read. The owner's inbox is
untouched; this only ever reads.

**Cost: $0/month.** Telegram is free, Groq's classification API is free at this
volume, and a free web service tier covers hosting.

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

### Adding someone

Ask them to message the bot once. You will get an alert with their chat id.
Add it to `TELEGRAM_ALLOWED_CHAT_IDS` (comma separated) in the environment and
restart. Remove the id to revoke access.

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
