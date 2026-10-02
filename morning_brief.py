"""Morning brief: pulls what's due from Canvas and turns it into a study plan.

Usage:
    python morning_brief.py            # build the brief, print it, and deliver it
    python morning_brief.py --dry-run  # build and print only, skip delivery
    python morning_brief.py --login    # log in to Canvas in a browser window once,
                                       # so the script can reuse your session
    python morning_brief.py --no-ai    # skip the AI study plan

AI study plan: uses Claude Code (your Claude subscription) if the `claude`
command is installed and logged in, or the API if ANTHROPIC_API_KEY is set.

Configuration comes from environment variables (or a .env file); see .env.example.
"""

import argparse
import os
import re
import json
import shutil
import smtplib
import subprocess
import sys
from pathlib import Path
from collections import Counter
from urllib.parse import urlencode
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from zoneinfo import ZoneInfo

import requests

HERE = Path(__file__).resolve().parent

try:
    from dotenv import load_dotenv

    load_dotenv(HERE / ".env")
except ImportError:
    pass

CANVAS_BASE_URL = os.environ.get("CANVAS_BASE_URL", "").rstrip("/")
CANVAS_TOKEN = os.environ.get("CANVAS_TOKEN", "")
# Alternative to a token: Canvas -> Calendar -> "Calendar Feed" link (.ics URL)
CANVAS_ICS_URL = os.environ.get("CANVAS_ICS_URL", "")
TZ = ZoneInfo(os.environ.get("TIMEZONE", "America/New_York"))
DAYS_AHEAD = int(os.environ.get("DAYS_AHEAD", "7"))
CLAUDE_MODEL = "claude-opus-5-5"
# Saved browser login (cookies) for --login mode. Anyone with this file is
# logged in as you, so it lives outside the repo by default.
SESSION_FILE = Path(
    os.environ.get("CANVAS_SESSION_FILE", Path.home() / ".canvas-bot" / "session.json")
)


class LoginExpired(Exception):
    pass


# ---------------------------------------------------------------- Canvas API


_browser_api = None  # Playwright request context when using a saved browser login


def _http_get(url):
    """GET a URL with whichever auth is configured; returns (status, headers, text)."""
    if _browser_api is not None:
        resp = _browser_api.get(url, max_redirects=0, timeout=30_000)
        return resp.status, resp.headers, resp.text()
    resp = requests.get(
        url, headers={"Authorization": f"Bearer {CANVAS_TOKEN}"}, timeout=30, allow_redirects=False
    )
    return resp.status_code, resp.headers, resp.text


def canvas_get(path, params=None):
    """GET a Canvas API endpoint and follow pagination (Link: rel="next")."""
    url = f"{CANVAS_BASE_URL}/api/v1{path}"
    if params:
        url += "?" + urlencode(params, doseq=True)
    results = []
    while url:
        status, headers, text = _http_get(url)
        if status in (301, 302, 303, 401):
            raise LoginExpired(f"Canvas returned {status} for {path}")
        if status >= 400:
            raise RuntimeError(f"Canvas API error {status} for {path}: {text[:200]}")
        # Cookie-authenticated API responses are prefixed to block JSON hijacking
        data = json.loads(text.removeprefix("while(1);"))
        if not isinstance(data, list):
            return data
        results.extend(data)
        links = requests.utils.parse_header_links(headers.get("link", "") or "")
        url = next((l["url"] for l in links if l.get("rel") == "next"), None)
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


def fmt_day(dt, weekday="%a"):
    """'Thu Oct 2' — built by hand because %-d isn't supported on Windows."""
    return f"{dt.strftime(weekday + ' %b')} {dt.day}"


def fmt_clock(dt):
    return f"{dt.hour % 12 or 12}:{dt.minute:02d} {dt.strftime('%p')}"


def fmt_due(dt, now):
    days = (dt.date() - now.date()).days
    when = {0: "Today", 1: "Tomorrow"}.get(days, fmt_day(dt))
    return f"{when} {fmt_clock(dt)}"


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

    return {
        "courses": courses,
        "upcoming": upcoming,
        "missing": missing,
        "announcements": announcements,
        "has_status": True,
    }


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
    return {"courses": {}, "upcoming": upcoming, "missing": [], "announcements": [], "has_status": False}


def workload_by_day(data, now):
    """[(date, item_count, points)] for each of the next DAYS_AHEAD days."""
    counts, points = Counter(), Counter()
    for a in data["upcoming"]:
        if a["type"] == "event":
            continue
        counts[a["due"].date()] += 1
        points[a["due"].date()] += a["points"] or 0
    days = [(now + timedelta(days=i)).date() for i in range(DAYS_AHEAD + 1)]
    return [(d, counts[d], points[d]) for d in days]


def render_plain(data, now):
    lines = [f"# Morning Brief — {now.strftime('%A, %B')} {now.day}", ""]
    if data.get("has_status"):
        lines += ["_Already-submitted work is excluded._", ""]
    else:
        lines += ["_Submission status unknown: some items may already be turned in._", ""]

    if data["missing"]:
        lines.append(f"## ⚠️ Missing ({len(data['missing'])})")
        for a in data["missing"]:
            due = f" (was due {a['due'].strftime('%b')} {a['due'].day})" if a["due"] else ""
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

    load = [w for w in workload_by_day(data, now) if w[1]]
    if load:
        lines.append("## 📈 Workload by day")
        for d, n, pts in load:
            pts_txt = f", {pts:g} pts" if pts else ""
            lines.append(f"- {fmt_day(d)}: {n} item{'s' if n != 1 else ''}{pts_txt}")
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


AI_INSTRUCTIONS = (
    "You are writing a student's morning brief from their Canvas data (below). Write:\n"
    "1. **This week**: 2 sentences on how heavy the week is and the crunch points.\n"
    "2. **Today's plan**: a prioritized checklist of what to work on today, with rough "
    "time estimates, weighing due date, points, missing work, and weak grades. "
    "Spread big items across days so nothing piles up on the busiest day.\n"
    "3. **What to study**: specific courses/topics to review and why (upcoming "
    "quizzes/exams, low grades).\n"
    "Only if the data says submission status is unknown, add one line reminding them "
    "to skip anything already turned in. Keep it under 250 words, concise and encouraging. Use Markdown headings "
    "and bullet lists, no tables. Don't invent assignments that aren't listed."
)


def ai_prompt(plain_brief, now):
    return f"It is {fmt_day(now, '%A')}, {fmt_clock(now)}.\n\n{plain_brief}"


def ai_via_api(plain_brief, now):
    """Claude API (needs ANTHROPIC_API_KEY; billed to your API account)."""
    import anthropic

    client = anthropic.Anthropic()
    response = client.beta.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=16000,
        system=AI_INSTRUCTIONS,
        output_config={"effort": "low"},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        messages=[{"role": "user", "content": ai_prompt(plain_brief, now)}],
    )
    if response.stop_reason == "refusal":
        return None
    return "".join(b.text for b in response.content if b.type == "text").strip() or None


def ai_via_claude_code(plain_brief, now):
    """Claude Code CLI in print mode (uses your Claude subscription's usage)."""
    exe = shutil.which("claude")
    result = subprocess.run(
        # --tools "": Claude only reads the text we pipe in; it can't run anything
        [exe, "-p", AI_INSTRUCTIONS, "--tools", "", "--no-session-persistence"],
        input=ai_prompt(plain_brief, now),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=300,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout).strip()[:300])
    return result.stdout.strip() or None


def ai_study_plan(plain_brief, now):
    """Pick an AI backend: API key if set, else Claude Code if installed, else none."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return ai_via_api(plain_brief, now)
    if shutil.which("claude"):
        return ai_via_claude_code(plain_brief, now)
    return None


# ---------------------------------------------------------------- Terminal display


def urgency_style(due, now):
    hours = (due - now).total_seconds() / 3600
    if hours < 24:
        return "bold red"
    if hours < 72:
        return "yellow"
    return "green"


def countdown(due, now):
    hours = (due - now).total_seconds() / 3600
    if hours < 1:
        return "< 1 hr"
    if hours < 24:
        return f"in {int(hours)} hr"
    return f"in {int(hours // 24)}d {int(hours % 24)}h"


def show_terminal(data, plan, warning, now):
    """Pretty, colour-coded view of the brief for the terminal."""
    from rich import box
    from rich.console import Console
    from rich.markdown import Markdown
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    con = Console()
    upcoming = data["upcoming"]
    work = [a for a in upcoming if a["type"] != "event"]
    load = workload_by_day(data, now)
    busiest = max(load, key=lambda w: (w[1], w[2]), default=None)

    stats = [f"[bold]{len(work)}[/] due in {DAYS_AHEAD} days"]
    due_today = sum(1 for a in work if a["due"].date() == now.date())
    if due_today:
        stats.append(f"[bold red]{due_today} due today[/]")
    if data["missing"]:
        stats.append(f"[bold red]{len(data['missing'])} missing[/]")
    if busiest and busiest[1]:
        stats.append(f"busiest: [bold]{fmt_day(busiest[0])}[/]")
    con.print(
        Panel(
            " · ".join(stats),
            title=f"[bold]☀  Morning Brief — {now.strftime('%A, %B')} {now.day}",
            border_style="bright_blue",
            padding=(1, 2),
        )
    )

    if warning:
        con.print(Panel(Markdown(warning), border_style="red"))

    if plan:
        con.print(Panel(Markdown(plan), title="[bold]🧠 Study plan", border_style="magenta", padding=(1, 2)))

    if data["missing"]:
        t = Table(box=box.SIMPLE_HEAD, header_style="bold red", expand=True)
        t.add_column("Course", style="bold")
        t.add_column("Missing assignment")
        t.add_column("Was due", justify="right")
        for a in data["missing"]:
            title = f"[link={a['url']}]{a['title']}[/link]" if a["url"] else a["title"]
            t.add_row(a["course"], title, fmt_day(a["due"]) if a["due"] else "")
        con.print(Panel(t, title="[bold red]⚠  Missing work", border_style="red"))

    t = Table(box=box.SIMPLE_HEAD, header_style="bold", expand=True)
    t.add_column("Due", no_wrap=True)
    t.add_column("", no_wrap=True)
    t.add_column("Course", style="cyan", no_wrap=True)
    t.add_column("Assignment")
    t.add_column("Pts", justify="right")
    for a in upcoming:
        style = "dim" if a["type"] == "event" else urgency_style(a["due"], now)
        title = f"[link={a['url']}]{a['title']}[/link]" if a["url"] else a["title"]
        if a["type"] == "event":
            title += " [dim](event)[/]"
        t.add_row(
            Text(fmt_due(a["due"], now), style=style),
            Text(countdown(a["due"], now), style=style),
            a["course"],
            title,
            f"{a['points']:g}" if a["points"] else "",
        )
    if not upcoming:
        t.add_row("", "", "", "Nothing due — enjoy it 🎉", "")
    con.print(Panel(t, title=f"[bold]📅 Coming up (next {DAYS_AHEAD} days)", border_style="bright_blue"))

    if any(n for _, n, _ in load):
        most = max(n for _, n, _ in load)
        t = Table.grid(padding=(0, 2))
        for d, n, pts in load:
            bar = "█" * round(n / most * 24) if n else "·"
            color = "red" if n == most and n > 1 else "bright_blue"
            label = f"{n} item{'s' if n != 1 else ''}" + (f" · {pts:g} pts" if pts else "") if n else ""
            t.add_row(fmt_day(d), Text(bar, style=color), Text(label, style="dim"))
        con.print(Panel(t, title="[bold]📈 Workload", border_style="bright_blue"))

    if data["announcements"]:
        t = Table.grid(padding=(0, 2))
        for a in data["announcements"]:
            t.add_row(Text(a["course"], style="cyan"), a["title"])
        con.print(Panel(t, title="[bold]📣 Announcements", border_style="bright_blue"))

    graded = sorted(
        ((c["name"], c["score"]) for c in data["courses"].values() if c["score"] is not None),
        key=lambda x: x[1],
    )
    if graded:
        t = Table.grid(padding=(0, 2))
        for name, score in graded:
            color = "red" if score < 70 else "yellow" if score < 80 else "green"
            bar = "█" * round(min(score, 100) / 5)
            t.add_row(Text(name, style="cyan"), Text(f"{bar:<20}", style=color), Text(f"{score:.1f}%", style=f"bold {color}"))
        con.print(Panel(t, title="[bold]📊 Grades", border_style="bright_blue"))

    if not plan:
        con.print("[dim]Tip: install Claude Code (see README) to get an AI study plan here.[/]")


# ---------------------------------------------------------------- Browser login


def browser_login():
    """Open a real browser so you can log in (SSO, Duo, etc.), then save the session."""
    from playwright.sync_api import sync_playwright

    if not CANVAS_BASE_URL:
        sys.exit("Set CANVAS_BASE_URL in .env first (e.g. https://yourschool.instructure.com).")
    SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        page.goto(CANVAS_BASE_URL)
        print(
            "A browser window opened. Log in to Canvas as usual (check 'remember me' /\n"
            "'stay signed in' if offered). When you can see your Canvas dashboard,\n"
            "come back here and press Enter."
        )
        input()
        resp = context.request.get(f"{CANVAS_BASE_URL}/api/v1/users/self", max_redirects=0)
        if resp.status != 200:
            browser.close()
            sys.exit("Doesn't look logged in yet (Canvas said %d). Try again." % resp.status)
        me = json.loads(resp.text().removeprefix("while(1);"))
        context.storage_state(path=str(SESSION_FILE))
        browser.close()
    try:
        SESSION_FILE.chmod(0o600)
    except OSError:
        pass
    print(f"✓ Logged in as {me.get('name')}. Session saved to {SESSION_FILE}")


def open_browser_session(playwright):
    global _browser_api
    _browser_api = playwright.request.new_context(storage_state=str(SESSION_FILE))
    return _browser_api


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


def empty_data():
    return {"courses": {}, "upcoming": [], "missing": [], "announcements": [], "has_status": False}


def load_data(now):
    """Collect data from the best available source. Returns (data, warning)."""
    if CANVAS_BASE_URL and CANVAS_TOKEN:
        return collect(now), None

    if CANVAS_BASE_URL and SESSION_FILE.exists():
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            api = open_browser_session(p)
            try:
                data = collect(now)
                api.storage_state(path=str(SESSION_FILE))  # keep refreshed cookies
                return data, None
            except LoginExpired:
                warning = (
                    "⚠️ **Canvas login expired.** On your computer, run "
                    "`python morning_brief.py --login` to log in again."
                )
                if not CANVAS_ICS_URL:
                    return empty_data(), warning
                print("Canvas login expired; falling back to calendar feed", file=sys.stderr)
                return collect_from_ics(now), warning
            finally:
                api.dispose()

    if CANVAS_ICS_URL:
        return collect_from_ics(now), None

    sys.exit(
        "No Canvas source configured. Either run `python morning_brief.py --login`, "
        "or set CANVAS_ICS_URL / CANVAS_TOKEN (see .env.example)."
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="show only, don't deliver")
    parser.add_argument("--login", action="store_true", help="log in to Canvas in a browser")
    parser.add_argument("--no-ai", action="store_true", help="skip the AI study plan")
    args = parser.parse_args()

    # Don't crash on emoji when output goes to a log file (e.g. Task Scheduler)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass

    if args.login:
        browser_login()
        return

    now = datetime.now(TZ)
    data, warning = load_data(now)
    plain = render_plain(data, now)
    if warning:
        plain = warning + "\n\n" + plain

    plan = None
    if not args.no_ai:
        try:
            if sys.stdout.isatty():
                from rich.console import Console

                with Console().status("Asking Claude for today's study plan…"):
                    plan = ai_study_plan(plain, now)
            else:
                plan = ai_study_plan(plain, now)
        except Exception as e:  # never lose the brief because the AI step failed
            print(f"AI study plan skipped: {e}", file=sys.stderr)

    brief = plan + "\n\n---\n\n" + plain if plan else plain
    (HERE / "brief.md").write_text(brief, encoding="utf-8")

    if sys.stdout.isatty():
        show_terminal(data, plan, warning, now)
    else:
        print(brief)

    if args.dry_run:
        return
    subject = f"Morning Brief — {fmt_day(now)}"
    send_email(subject, brief)
    send_discord(brief)
    send_ntfy(subject, brief)


if __name__ == "__main__":
    main()
