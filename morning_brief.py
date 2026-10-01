"""Morning brief: pulls what's due from Canvas and turns it into a study plan.

Usage:
    python morning_brief.py            # build the brief, print it, and deliver it
    python morning_brief.py --dry-run  # build and print only, skip delivery

Configuration comes from environment variables (or a .env file); see .env.example.
"""

import argparse
import os
import re
import smtplib
import sys
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from zoneinfo import ZoneInfo

import requests

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

CANVAS_BASE_URL = os.environ.get("CANVAS_BASE_URL", "").rstrip("/")
CANVAS_TOKEN = os.environ.get("CANVAS_TOKEN", "")
# Alternative to a token: Canvas -> Calendar -> "Calendar Feed" link (.ics URL)
CANVAS_ICS_URL = os.environ.get("CANVAS_ICS_URL", "")
TZ = ZoneInfo(os.environ.get("TIMEZONE", "America/New_York"))
DAYS_AHEAD = int(os.environ.get("DAYS_AHEAD", "7"))
CLAUDE_MODEL = "claude-opus-5-5"


# ---------------------------------------------------------------- Canvas API


def canvas_get(path, params=None):
    """GET a Canvas API endpoint and follow pagination (Link: rel="next")."""
    url = f"{CANVAS_BASE_URL}/api/v1{path}"
    headers = {"Authorization": f"Bearer {CANVAS_TOKEN}"}
    results = []
    while url:
        resp = requests.get(url, headers=headers, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, list):
            return data
        results.extend(data)
        url = resp.links.get("next", {}).get("url")
        params = None  # the "next" URL already carries the query string
    return results


def fetch_courses():
    courses = canvas_get(
        "/courses",
        {"enrollment_state": "active", "include[]": "total_scores", "per_page": 100},
    )
    out = {}
    for c in courses:
        if "name" not in c:  # restricted/concluded courses come back as stubs
            continue
        score = None
        for e in c.get("enrollments", []):
            if e.get("computed_current_score") is not None:
                score = e["computed_current_score"]
        out[c["id"]] = {"name": c.get("course_code") or c["name"], "score": score}
    return out


def fetch_planner_items(now):
    """Assignments, quizzes, discussions etc. due in the next DAYS_AHEAD days."""
    return canvas_get(
        "/planner/items",
        {
            "start_date": now.astimezone(timezone.utc).isoformat(),
            "end_date": (now + timedelta(days=DAYS_AHEAD)).astimezone(timezone.utc).isoformat(),
            "per_page": 100,
        },
    )


def fetch_missing():
    return canvas_get(
        "/users/self/missing_submissions",
        {"filter[]": "submittable", "per_page": 100},
    )


def fetch_announcements(course_ids, now):
    if not course_ids:
        return []
    params = [("context_codes[]", f"course_{cid}") for cid in course_ids]
    params += [
        ("start_date", (now - timedelta(days=2)).date().isoformat()),
        ("end_date", now.date().isoformat()),
        ("per_page", 50),
    ]
    return canvas_get("/announcements", params)


# ---------------------------------------------------------------- Build brief


def parse_time(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(TZ) if s else None


def fmt_due(dt, now):
    days = (dt.date() - now.date()).days
    when = {0: "Today", 1: "Tomorrow"}.get(days, dt.strftime("%a %b %-d"))
    return f"{when} {dt.strftime('%-I:%M %p')}"


def collect(now):
    courses = fetch_courses()

    upcoming = []
    for item in fetch_planner_items(now):
        p = item.get("plannable", {})
        sub = item.get("submissions") or {}
        if sub.get("submitted") or sub.get("graded"):
            continue
        if (item.get("planner_override") or {}).get("marked_complete"):
            continue
        due = parse_time(p.get("due_at") or item.get("plannable_date"))
        if not due:
            continue
        upcoming.append(
            {
                "course": courses.get(item.get("course_id"), {}).get("name")
                or item.get("context_name", ""),
                "title": p.get("title", "(untitled)"),
                "type": item.get("plannable_type", ""),
                "points": p.get("points_possible"),
                "due": due,
                "url": CANVAS_BASE_URL + item["html_url"]
                if item.get("html_url", "").startswith("/")
                else item.get("html_url", ""),
            }
        )
    upcoming.sort(key=lambda a: a["due"])

    missing = [
        {
            "course": courses.get(a.get("course_id"), {}).get("name", ""),
            "title": a.get("name", "(untitled)"),
            "due": parse_time(a.get("due_at")),
            "points": a.get("points_possible"),
            "url": a.get("html_url", ""),
        }
        for a in fetch_missing()
    ]

    announcements = [
        {
            "course": courses.get(int(a.get("context_code", "course_0").split("_")[1]), {}).get(
                "name", ""
            ),
            "title": a.get("title", ""),
            "posted": parse_time(a.get("posted_at")),
        }
        for a in fetch_announcements(list(courses), now)
    ]

    return {"courses": courses, "upcoming": upcoming, "missing": missing, "announcements": announcements}


# ---------------------------------------------------------------- Calendar feed


def collect_from_ics(now):
    """Build the brief from the Canvas calendar feed when API tokens are disabled.

    The feed has due dates for assignments/quizzes plus course events, but no
    submission status, grades, or announcements.
    """
    from icalendar import Calendar

    # webcal:// links are just https under another name
    url = CANVAS_ICS_URL.replace("webcal://", "https://", 1)
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    cal = Calendar.from_ical(resp.content)

    end = now + timedelta(days=DAYS_AHEAD)
    upcoming = []
    for ev in cal.walk("VEVENT"):
        start = ev.get("DTSTART")
        if not start:
            continue
        due = start.dt
        if isinstance(due, datetime):
            due = (due if due.tzinfo else due.replace(tzinfo=timezone.utc)).astimezone(TZ)
        else:  # all-day event: treat as due at 11:59 PM that day
            due = datetime(due.year, due.month, due.day, 23, 59, tzinfo=TZ)
        if not (now <= due <= end):
            continue

        # Canvas summaries look like "Essay 2 [ENGL101-01]"
        summary = str(ev.get("SUMMARY", "(untitled)"))
        title, course = summary, ""
        m = re.match(r"^(.*)\s*\[(.+)\]\s*$", summary)
        if m:
            title, course = m.group(1).strip(), m.group(2)
        uid = str(ev.get("UID", ""))
        upcoming.append(
            {
                "course": course,
                "title": title,
                "type": "assignment" if "assignment" in uid else "event",
                "points": None,
                "due": due,
                "url": str(ev.get("URL", "")),
            }
        )
    upcoming.sort(key=lambda a: a["due"])
    return {"courses": {}, "upcoming": upcoming, "missing": [], "announcements": []}


def render_plain(data, now):
    lines = [f"# Morning Brief — {now.strftime('%A, %B %-d')}", ""]

    if data["missing"]:
        lines.append(f"## ⚠️ Missing ({len(data['missing'])})")
        for a in data["missing"]:
            due = f" (was due {a['due'].strftime('%b %-d')})" if a["due"] else ""
            lines.append(f"- **{a['course']}** — {a['title']}{due}")
        lines.append("")

    lines.append(f"## 📅 Due in the next {DAYS_AHEAD} days ({len(data['upcoming'])})")
    if not data["upcoming"]:
        lines.append("- Nothing due. 🎉")
    for a in data["upcoming"]:
        pts = f" · {a['points']:g} pts" if a["points"] else ""
        course = f"{a['course']}: " if a["course"] else ""
        tag = " (class event)" if a["type"] == "event" else ""
        lines.append(f"- **{fmt_due(a['due'], now)}** — {course}{a['title']}{pts}{tag}")
    lines.append("")

    if data["announcements"]:
        lines.append("## 📣 Recent announcements")
        for a in data["announcements"]:
            lines.append(f"- {a['course']}: {a['title']}")
        lines.append("")

    graded = [(c["name"], c["score"]) for c in data["courses"].values() if c["score"] is not None]
    if graded:
        lines.append("## 📊 Current grades")
        for name, score in sorted(graded, key=lambda x: x[1]):
            lines.append(f"- {name}: {score:.1f}%")
        lines.append("")

    return "\n".join(lines)


def ai_study_plan(plain_brief, now):
    """Ask Claude to turn the raw list into a prioritized plan for today."""
    import anthropic

    client = anthropic.Anthropic()
    prompt = (
        f"It is {now.strftime('%A %B %-d, %-I:%M %p')}. Below is my Canvas data for the "
        "coming week. Write a short morning brief for a student:\n"
        "1. A 2-sentence overview of the week.\n"
        "2. 'Today's plan': a prioritized list of what to work on today, with rough "
        "time estimates, weighing due date, points, missing work, and weak grades.\n"
        "3. 'What to study': specific topics/courses to review and why (upcoming quizzes/"
        "exams, low grades).\n"
        "If the data has no submission status, remind me to skip anything already "
        "turned in. Be concise and practical. Use Markdown. Don't invent assignments that aren't listed.\n\n"
        + plain_brief
    )
    response = client.beta.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=16000,
        output_config={"effort": "low"},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        messages=[{"role": "user", "content": prompt}],
    )
    if response.stop_reason == "refusal":
        return None
    return "".join(b.text for b in response.content if b.type == "text").strip() or None


# ---------------------------------------------------------------- Delivery


def send_email(subject, body):
    host, to = os.environ.get("SMTP_HOST"), os.environ.get("EMAIL_TO")
    if not (host and to):
        return
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = os.environ.get("SMTP_USER", to)
    msg["To"] = to
    msg.set_content(body)
    with smtplib.SMTP(host, int(os.environ.get("SMTP_PORT", "587"))) as s:
        s.starttls()
        if os.environ.get("SMTP_USER"):
            s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
        s.send_message(msg)
    print("✓ Sent email", file=sys.stderr)


def send_discord(body):
    url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not url:
        return
    # Discord caps messages at 2000 chars; send in chunks split on line breaks.
    chunk = ""
    for line in body.splitlines(keepends=True):
        if len(chunk) + len(line) > 1900:
            requests.post(url, json={"content": chunk}, timeout=30).raise_for_status()
            chunk = ""
        chunk += line
    if chunk.strip():
        requests.post(url, json={"content": chunk}, timeout=30).raise_for_status()
    print("✓ Sent to Discord", file=sys.stderr)


def send_ntfy(title, body):
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        return
    requests.post(
        f"https://ntfy.sh/{topic}",
        data=body.encode("utf-8"),
        headers={"Title": title.encode("utf-8"), "Markdown": "yes"},
        timeout=30,
    ).raise_for_status()
    print("✓ Sent push via ntfy", file=sys.stderr)


# ---------------------------------------------------------------- Main


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="print only, don't deliver")
    args = parser.parse_args()

    now = datetime.now(TZ)
    if CANVAS_BASE_URL and CANVAS_TOKEN:
        data = collect(now)
    elif CANVAS_ICS_URL:
        data = collect_from_ics(now)
    else:
        sys.exit("Set CANVAS_ICS_URL, or CANVAS_BASE_URL + CANVAS_TOKEN (see .env.example).")
    plain = render_plain(data, now)

    brief = plain
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            plan = ai_study_plan(plain, now)
            if plan:
                brief = plan + "\n\n---\n\n" + plain
        except Exception as e:  # never lose the brief because the AI step failed
            print(f"AI study plan skipped: {e}", file=sys.stderr)

    print(brief)
    with open("brief.md", "w", encoding="utf-8") as f:
        f.write(brief)

    if args.dry_run:
        return
    subject = f"Morning Brief — {now.strftime('%a %b %-d')}"
    send_email(subject, brief)
    send_discord(brief)
    send_ntfy(subject, brief)


if __name__ == "__main__":
    main()
