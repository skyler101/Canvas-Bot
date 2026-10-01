# Canvas Bot — Automated Morning Brief

Every morning, pulls what's due from Canvas and sends you a brief:

- ⚠️ **Missing** work
- 📅 Everything **due in the next 7 days** (skips stuff you already submitted)
- 📣 Recent **announcements**
- 📊 Current **grades** (lowest first)
- 🧠 Optional **AI study plan** (via Claude): what to do today, in what order, and what to study

Delivered by **email**, **Discord**, and/or **phone push notification** (ntfy).

## 1. Get a Canvas access token

1. Log in to Canvas → **Account** → **Settings**
2. Scroll to **Approved Integrations** → **+ New Access Token**
3. Copy the token (you only see it once). Treat it like a password.

Your `CANVAS_BASE_URL` is the address you log in at, e.g. `https://yourschool.instructure.com`.

> Some schools disable student tokens. If so, the alternative is the Canvas
> **calendar feed** (Calendar → *Calendar Feed* link, an `.ics` URL) — ask and
> this bot can be adapted to read that instead.

## 2. Try it locally

```bash
pip install -r requirements.txt
cp .env.example .env      # fill in CANVAS_BASE_URL and CANVAS_TOKEN
python morning_brief.py --dry-run
```

## 3. Run it automatically every morning (free, via GitHub Actions)

1. In this repo on GitHub: **Settings → Secrets and variables → Actions**
2. Add **secrets**: `CANVAS_BASE_URL`, `CANVAS_TOKEN`, plus any delivery ones you want:
   - Email: `SMTP_HOST`, `SMTP_USER`, `SMTP_PASSWORD`, `EMAIL_TO`
     (Gmail: `smtp.gmail.com`, and create an [App Password](https://myaccount.google.com/apppasswords))
   - Discord: `DISCORD_WEBHOOK_URL` (Channel → Edit → Integrations → Webhooks)
   - Phone push: `NTFY_TOPIC` — install the [ntfy app](https://ntfy.sh), subscribe to a long random topic name
   - AI study plan: `ANTHROPIC_API_KEY` (from <https://console.anthropic.com>)
3. Optionally add **variables**: `TIMEZONE` (e.g. `America/Chicago`), `DAYS_AHEAD`
4. Edit the `cron` time in `.github/workflows/morning-brief.yml` (it's in UTC)
5. Test: **Actions → Morning Brief → Run workflow**

> Keep the repo **private** — the workflow logs print your brief.
