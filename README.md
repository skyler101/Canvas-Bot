# Canvas Bot — Automated Morning Brief

Every morning, pulls what's due from Canvas and sends you a brief:

- ⚠️ **Missing** work
- 📅 Everything **due in the next 7 days** (skips stuff you already submitted)
- 📣 Recent **announcements**
- 📊 Current **grades** (lowest first)
- 🧠 Optional **AI study plan** (via Claude): what to do today, in what order, and what to study

Delivered by **email**, **Discord**, and/or **phone push notification** (ntfy).

## 1. Connect Canvas

### Option A — Calendar Feed (works even if access tokens are greyed out)

1. Open Canvas → **Calendar** (left sidebar)
2. At the bottom of the right-hand column, click **Calendar Feed**
3. Copy the link (looks like `https://yourschool.instructure.com/feeds/calendars/user_abc123.ics`)
4. Use it as `CANVAS_ICS_URL`. It's a private link, so don't share it.

The feed has every assignment/quiz due date and class event, but **not** whether
you've submitted something, missing work, grades, or announcements. Turn on
Canvas **Notifications** (Account → Notifications → email/push) for those.

### Option B — Access token (more detail, if your school allows it)

Canvas → **Account** → **Settings** → **Approved Integrations** → **+ New Access Token**.
Set `CANVAS_BASE_URL` (e.g. `https://yourschool.instructure.com`) and `CANVAS_TOKEN`.
This adds submission status, missing work, grades, and announcements.

## 2. Try it locally

```bash
pip install -r requirements.txt
cp .env.example .env      # fill in CANVAS_ICS_URL (or CANVAS_BASE_URL + CANVAS_TOKEN)
python morning_brief.py --dry-run
```

## 3. Run it automatically every morning (free, via GitHub Actions)

1. In this repo on GitHub: **Settings → Secrets and variables → Actions**
2. Add **secrets**: `CANVAS_ICS_URL` (or `CANVAS_BASE_URL` + `CANVAS_TOKEN`), plus any delivery ones you want:
   - Email: `SMTP_HOST`, `SMTP_USER`, `SMTP_PASSWORD`, `EMAIL_TO`
     (Gmail: `smtp.gmail.com`, and create an [App Password](https://myaccount.google.com/apppasswords))
   - Discord: `DISCORD_WEBHOOK_URL` (Channel → Edit → Integrations → Webhooks)
   - Phone push: `NTFY_TOPIC` — install the [ntfy app](https://ntfy.sh), subscribe to a long random topic name
   - AI study plan: `ANTHROPIC_API_KEY` (from <https://console.anthropic.com>)
3. Optionally add **variables**: `TIMEZONE` (e.g. `America/Chicago`), `DAYS_AHEAD`
4. Edit the `cron` time in `.github/workflows/morning-brief.yml` (it's in UTC)
5. Test: **Actions → Morning Brief → Run workflow**

> Keep the repo **private** — the workflow logs print your brief.
