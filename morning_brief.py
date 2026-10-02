"""Morning brief: pulls what's due from Canvas and turns it into a study plan.

Usage:
    python morning_brief.py            # build the brief, open the dashboard, and deliver it
    python morning_brief.py --dry-run  # build and open the dashboard only, skip delivery
    python morning_brief.py --login    # log in to Canvas in a browser window once,
                                       # so the script can reuse your session
    python morning_brief.py --no-ai    # skip the AI study plan
    python morning_brief.py --no-open  # don't pop the dashboard open in the browser
    python morning_brief.py --install-startup  # run automatically when you log in (Windows)

Extra sites (WeBWorK, Labflow), reminders, hobbies and quotes are set in
my_settings.json (copy my_settings.example.json to start).

The brief is saved as dashboard.html (interactive, opens in your browser) and
brief.md (plain text, used for email/Discord/phone notifications).

AI study plan: uses Claude Code (your Claude subscription) if the `claude`
command is installed and logged in, or the API if ANTHROPIC_API_KEY is set.

Configuration comes from environment variables (or a .env file); see .env.example.
"""

import argparse
import json
import os
import re
import shutil
import smtplib
import subprocess
import sys
import time
import webbrowser
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urlencode
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
# One grade snapshot per day, so the dashboard can show changes between runs
GRADE_SNAPSHOTS = HERE / "grade_snapshots.json"
DASHBOARD_TEMPLATE = HERE / "dashboard_template.html"
DASHBOARD_FILE = HERE / "dashboard.html"
SETTINGS_FILE = HERE / "my_settings.json"
DEBUG_DIR = HERE / "debug"


def load_settings():
    """Personal settings (sites, reminders, hobbies, quotes).

    Uses my_settings.json, or the example file until you've made your own copy.
    """
    path = SETTINGS_FILE if SETTINGS_FILE.exists() else HERE / "my_settings.example.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError as e:
        sys.exit(
            f"{path.name} has a typo near line {getattr(e, 'lineno', '?')}: {e}\n"
            "Common causes: a missing comma between items, or a comma after the last item."
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


def full_url(u):
    return CANVAS_BASE_URL + u if u and u.startswith("/") else (u or "")


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
        out[c["id"]] = {
            "name": c.get("course_code") or c["name"],
            "score": score,
            "weighted": bool(c.get("apply_assignment_group_weights")),
        }
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


# ---------------------------------------------------------------- Grade history


def course_grade(earned, possible, weights, weighted):
    """Course percentage from per-assignment-group totals (ignores drop-lowest rules)."""
    groups = [g for g in possible if possible[g] > 0]
    if weighted and sum(weights.get(g, 0) for g in groups) > 0:
        total_w = sum(weights.get(g, 0) for g in groups)
        return 100 * sum(weights.get(g, 0) * earned[g] / possible[g] for g in groups) / total_w
    total = sum(possible[g] for g in groups)
    return 100 * sum(earned[g] for g in groups) / total if total else None


def fetch_grade_history(courses):
    """Rebuild each course's grade over time by replaying graded work in order.

    Returns ({course name: [points]}, [grade changes]). It's an estimate: it
    follows assignment-group weights but not drop-lowest or other special rules.
    """
    history, changes = {}, []
    for cid, c in courses.items():
        try:
            groups = canvas_get(
                f"/courses/{cid}/assignment_groups",
                {"include[]": ["assignments", "submission"], "per_page": 100},
            )
        except RuntimeError as e:  # some courses hide grades; skip them
            print(f"Grade history skipped for {c['name']}: {e}", file=sys.stderr)
            continue

        events = []
        for g in groups:
            for a in g.get("assignments") or []:
                sub = a.get("submission") or {}
                pts = a.get("points_possible") or 0
                if a.get("omit_from_final_grade") or not pts or sub.get("excused"):
                    continue
                if sub.get("score") is None or not sub.get("graded_at"):
                    continue
                events.append(
                    (
                        parse_time(sub["graded_at"]),
                        g["id"],
                        g.get("group_weight") or 0,
                        sub["score"],
                        pts,
                        a.get("name", "(untitled)"),
                        full_url(a.get("html_url")),
                    )
                )
        events.sort(key=lambda e: e[0])

        earned, possible, weights = defaultdict(float), defaultdict(float), {}
        series, prev = [], None
        for when, gid, weight, score, pts, name, url in events:
            earned[gid] += score
            possible[gid] += pts
            weights[gid] = weight
            grade = course_grade(earned, possible, weights, c["weighted"])
            if grade is None:
                continue
            series.append(
                {"t": when.isoformat(), "grade": round(grade, 2), "label": name, "score": f"{score:g}/{pts:g}"}
            )
            changes.append(
                {
                    "course": c["name"],
                    "title": name,
                    "when": when.isoformat(),
                    "score": score,
                    "possible": pts,
                    "before": None if prev is None else round(prev, 2),
                    "after": round(grade, 2),
                    "url": url,
                }
            )
            prev = grade
        if series:
            history[c["name"]] = series

    changes.sort(key=lambda ch: ch["when"], reverse=True)
    return history, changes


def update_snapshots(courses, now):
    """Save today's grades and return {course: change since the previous snapshot}."""
    try:
        snaps = json.loads(GRADE_SNAPSHOTS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        snaps = {}
    today = now.date().isoformat()
    snaps[today] = {c["name"]: c["score"] for c in courses.values() if c["score"] is not None}
    earlier = sorted(d for d in snaps if d < today)
    deltas = {}
    if earlier:
        last = snaps[earlier[-1]]
        for name, score in snaps[today].items():
            if name in last and last[name] is not None and abs(score - last[name]) >= 0.01:
                deltas[name] = {"delta": round(score - last[name], 2), "since": earlier[-1]}
    GRADE_SNAPSHOTS.write_text(json.dumps(snaps, indent=1, sort_keys=True), encoding="utf-8")
    return deltas


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


def empty_data():
    return {"courses": {}, "upcoming": [], "missing": [], "announcements": [], "has_status": False}


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
                "url": full_url(item.get("html_url")),
            }
        )
    upcoming.sort(key=lambda a: a["due"])

    missing = [
        {
            "course": courses.get(a.get("course_id"), {}).get("name", ""),
            "title": a.get("name", "(untitled)"),
            "due": parse_time(a.get("due_at")),
            "points": a.get("points_possible"),
            "url": full_url(a.get("html_url")),
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
            "url": full_url(a.get("html_url")),
        }
        for a in fetch_announcements(list(courses), now)
    ]

    history, changes = fetch_grade_history(courses)

    return {
        "courses": courses,
        "upcoming": upcoming,
        "missing": missing,
        "announcements": announcements,
        "has_status": True,
        "grade_history": history,
        "grade_changes": changes,
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
    data = empty_data()
    data["upcoming"] = upcoming
    return data


# ---------------------------------------------------------------- Extras


def match_course(label, data):
    """Map a label like 'M 151' to the Canvas course name, so colors line up."""
    def squash(t):  # "CHMY 142" and "CHMY_142_521_202670" both become "chmy142..."
        return re.sub(r"[^a-z0-9]", "", (t or "").lower())

    key = squash(label)
    names = {c["name"] for c in data["courses"].values()} | {a["course"] for a in data["upcoming"]}
    for n in sorted(names):
        if key and key in squash(n):
            return n
    return label or ""


def add_sites(data, settings, now):
    """Pull assignments from WeBWorK/Labflow etc. (needs the --login browser session)."""
    from sites import SITE_LABELS, fetch_sites, site_items

    configured, errors = [], []
    for site in settings.get("sites", []):
        if not isinstance(site, dict):
            continue
        if not str(site.get("canvas_link", "")).startswith("http"):
            errors.append(f"{site.get('type', 'site')} ({site.get('course', '')}): add its Canvas link in my_settings.json")
        else:
            from sites import clean_link

            configured.append(dict(site, canvas_link=clean_link(site["canvas_link"])))
    if not configured:
        return errors
    if not SESSION_FILE.exists():
        return ["WeBWorK/Labflow need the browser login. Run `python morning_brief.py --login` once."]
    print("Checking WeBWorK/Labflow…", file=sys.stderr)
    results, fetch_errors = fetch_sites(
        configured, SESSION_FILE, TZ, DEBUG_DIR,
        interactive=sys.stdin.isatty(), cache_file=HERE / "site_cache.json",
    )
    errors += fetch_errors
    for idx, parsed in results.items():
        site = configured[idx]
        kind = site["type"].lower()
        course = match_course(site.get("course", ""), data)
        upcoming, missing = site_items(kind, parsed, now, DAYS_AHEAD)
        for a, bucket in [(a, "upcoming") for a in upcoming] + [(a, "missing") for a in missing]:
            data[bucket].append(
                {
                    "course": course,
                    "title": a["title"],
                    "type": kind,
                    "points": None,
                    "due": a["due"],
                    "url": site["canvas_link"],
                    "note": a.get("note", ""),
                    "source": SITE_LABELS.get(kind, kind),
                }
            )
    data["upcoming"].sort(key=lambda a: a["due"])
    return errors


def add_reminders(data, settings, now):
    """Extra to-dos derived from Canvas items, e.g. 'post Packback question a day early'."""
    added = []
    for r in settings.get("reminders", []):
        if not isinstance(r, dict) or not r.get("match_title"):
            continue
        for a in data["upcoming"] + data["missing"]:
            if a.get("type") == "reminder" or r["match_title"].lower() not in a["title"].lower():
                continue
            if r.get("match_course") and r["match_course"].lower() not in a["course"].lower():
                continue
            if not a["due"]:
                continue
            due = a["due"] - timedelta(days=float(r.get("days_before", 1)))
            if due < now:
                continue
            added.append(
                {
                    "course": a["course"],
                    "title": r.get("task") or f"Reminder: {a['title']}",
                    "type": "reminder",
                    "points": None,
                    "due": due,
                    "url": a["url"],
                    "note": r.get("note") or f"Before: {a['title']}",
                    "source": "Reminder",
                }
            )
    data["upcoming"] = sorted(data["upcoming"] + added, key=lambda a: a["due"])


def hash_str(s):
    """Small stable string hash (Python's hash() changes between runs)."""
    h = 2166136261
    for ch in s.encode("utf-8"):
        h = ((h ^ ch) * 16777619) & 0xFFFFFFFF
    return h


def assign_ids(data):
    """Give every item a stable id so the plan, checklist, and dashboard can refer to it."""
    for prefix, key in (("u", "upcoming"), ("m", "missing")):
        for a in data[key]:
            raw = f"{a['course']}|{a['title']}|{a['due'].isoformat() if a['due'] else ''}"
            a["id"] = prefix + format(hash_str(raw), "08x")


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


def default_today(data, now):
    """A simple 'do today' list for when there's no AI plan."""
    tasks, seen = [], set()

    def add(item, task, why):
        if item["id"] not in seen:
            seen.add(item["id"])
            tasks.append(
                {"task": task, "course": item["course"], "minutes": 0, "why": why, "item_id": item["id"]}
            )

    for a in data["missing"][:3]:
        add(a, f"Turn in or ask about: {a['title']}", "Missing. Check if late work is accepted.")
    work = [a for a in data["upcoming"] if a["type"] != "event"]
    for a in work:
        if (a["due"] - now) <= timedelta(hours=36):
            add(a, a["title"], f"Due {fmt_due(a['due'], now)}")
    load = [w for w in workload_by_day(data, now)[1:4] if w[1] >= 2]
    if load:
        busiest = max(load, key=lambda w: (w[1], w[2]))[0]
        big = [a for a in work if a["due"].date() == busiest]
        if big:
            a = max(big, key=lambda a: a["points"] or 0)
            add(a, f"Get a head start: {a['title']}", f"{fmt_day(busiest)} is a busy day")
    return tasks


def render_plain(data, now, plan):
    lines = [f"# Morning Brief — {now.strftime('%A, %B')} {now.day}", ""]
    if not data.get("has_status"):
        lines += ["_Submission status unknown: some items may already be turned in._", ""]

    if plan and plan.get("week"):
        lines += [plan["week"], ""]

    today = plan["today"] if plan else default_today(data, now)
    if today:
        lines.append("## ✅ Do today")
        for t in today:
            mins = f" (~{t['minutes']} min)" if t.get("minutes") else ""
            course = f"{t['course']}: " if t.get("course") else ""
            lines.append(f"- [ ] {course}{t['task']}{mins}")
        lines.append("")

    if data["missing"]:
        lines.append(f"## ⚠️ Missing ({len(data['missing'])})")
        for a in data["missing"]:
            due = f" (was due {a['due'].strftime('%b')} {a['due'].day})" if a["due"] else ""
            note = f" — {a['note']}" if a.get("note") else ""
            lines.append(f"- **{a['course']}** — {a['title']}{due}{note}")
        lines.append("")

    lines.append(f"## 📅 Due in the next {DAYS_AHEAD} days ({len(data['upcoming'])})")
    if not data["upcoming"]:
        lines.append("- Nothing due. 🎉")
    for a in data["upcoming"]:
        pts = f" · {a['points']:g} pts" if a["points"] else ""
        course = f"{a['course']}: " if a["course"] else ""
        tag = " (class event)" if a["type"] == "event" else ""
        tag += f" [{a['source']}]" if a.get("source") else ""
        tag += f" — {a['note']}" if a.get("note") else ""
        lines.append(f"- **{fmt_due(a['due'], now)}** — {course}{a['title']}{pts}{tag}")
    lines.append("")

    if plan and plan.get("study"):
        lines.append("## 🧠 What to study")
        for s in plan["study"]:
            lines.append(f"- **{s['course']}**: {s['focus']} — {s['why']}")
        lines.append("")

    changes = data.get("grade_changes", [])[:5]
    if changes:
        lines.append("## 📝 Recently graded")
        for ch in changes:
            delta = ""
            if ch["before"] is not None:
                delta = f" → course grade {ch['after'] - ch['before']:+.1f}"
            lines.append(f"- {ch['course']}: {ch['title']} {ch['score']:g}/{ch['possible']:g}{delta}")
        lines.append("")

    if data["announcements"]:
        lines.append("## 📣 Recent announcements")
        for a in data["announcements"]:
            lines.append(f"- {a['course']}: {a['title']}")
        lines.append("")

    graded = [(c["name"], c["score"]) for c in data["courses"].values() if c["score"] is not None]
    if graded:
        lines.append("## 📊 Current grades")
        deltas = data.get("grade_deltas", {})
        for name, score in sorted(graded, key=lambda x: x[1]):
            d = deltas.get(name)
            change = f" ({d['delta']:+.1f})" if d else ""
            lines.append(f"- {name}: {score:.1f}%{change}")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------- AI study plan


AI_INSTRUCTIONS = (
    "You plan a college student's day from their Canvas data (below). Each item has an "
    "id in [brackets]. Fill in:\n"
    "- week: 1-2 sentences on how heavy the week is and where the crunch is.\n"
    "- today: 3-6 concrete tasks for TODAY in priority order, weighing due dates, points, "
    "missing work, weak grades, and spreading big items so the busiest day doesn't pile up. "
    "For each: a short task (e.g. 'Finish Problem Set 5'), the course code, a time estimate "
    "in minutes, a short why, and the item_id it relates to ('' if none).\n"
    "- study: 1-4 courses/topics to review and why (upcoming quizzes/exams, low or "
    "falling grades, recent low scores).\n"
    "- note: one short encouraging line.\n"
    "Only if submission status is unknown, mention in 'week' to skip anything already "
    "turned in. Plain text, no Markdown. Don't invent assignments that aren't listed."
)

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "week": {"type": "string"},
        "today": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "task": {"type": "string"},
                    "course": {"type": "string"},
                    "minutes": {"type": "integer"},
                    "why": {"type": "string"},
                    "item_id": {"type": "string"},
                },
                "required": ["task", "course", "minutes", "why", "item_id"],
                "additionalProperties": False,
            },
        },
        "study": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "course": {"type": "string"},
                    "focus": {"type": "string"},
                    "why": {"type": "string"},
                },
                "required": ["course", "focus", "why"],
                "additionalProperties": False,
            },
        },
        "note": {"type": "string"},
    },
    "required": ["week", "today", "study", "note"],
    "additionalProperties": False,
}


def ai_input(data, now):
    """Compact, id-tagged version of the data for the AI."""
    lines = [f"Now: {fmt_day(now, '%A')}, {fmt_clock(now)}"]
    lines.append(
        "Submission status: already-submitted work is excluded."
        if data.get("has_status")
        else "Submission status: UNKNOWN (some items may already be turned in)."
    )
    lines.append("\nMissing work:")
    for a in data["missing"]:
        due = fmt_day(a["due"]) if a["due"] else "?"
        note = f" | {a['note']}" if a.get("note") else ""
        lines.append(f"[{a['id']}] {a['course']} | {a['title']} | was due {due} | {a['points'] or '?'} pts{note}")
    if not data["missing"]:
        lines.append("(none)")
    lines.append(f"\nDue in the next {DAYS_AHEAD} days:")
    for a in data["upcoming"]:
        pts = f"{a['points']:g} pts" if a["points"] else "? pts"
        note = f" | {a['note']}" if a.get("note") else ""
        lines.append(f"[{a['id']}] {fmt_due(a['due'], now)} | {a['course']} | {a['title']} | {a['type']} | {pts}{note}")
    if not data["upcoming"]:
        lines.append("(none)")
    graded = [(c["name"], c["score"]) for c in data["courses"].values() if c["score"] is not None]
    if graded:
        lines.append("\nCurrent grades:")
        for name, score in graded:
            lines.append(f"{name}: {score:.1f}%")
    recent = data.get("grade_changes", [])[:8]
    if recent:
        lines.append("\nRecently graded:")
        for ch in recent:
            lines.append(
                f"{ch['course']} | {ch['title']} | {ch['score']:g}/{ch['possible']:g} | {ch['when'][:10]}"
            )
    if data["announcements"]:
        lines.append("\nAnnouncements:")
        for a in data["announcements"]:
            lines.append(f"{a['course']}: {a['title']}")
    if data.get("hobby"):
        lines.append(f"\nToday's featured hobby (you may nod to it in the note): {data['hobby']}")
    lines.append(
        "\nNotes: For Labflow pre-lab quizzes, the note lists the PDF and videos to finish first "
        "(with video minutes); include that prep time in the estimate. "
        "WeBWorK items don't show whether they're finished. Reminder items are "
        "the student's own rules (e.g. post a Packback question a day early); treat them as real tasks."
    )
    return "\n".join(lines)


def ai_via_api(text):
    """Claude API (needs ANTHROPIC_API_KEY; billed to your API account)."""
    import anthropic

    client = anthropic.Anthropic()
    response = client.beta.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=16000,
        system=AI_INSTRUCTIONS,
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": PLAN_SCHEMA}},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        messages=[{"role": "user", "content": text}],
    )
    if response.stop_reason in ("refusal", "max_tokens"):
        return None
    return json.loads("".join(b.text for b in response.content if b.type == "text"))


def ai_via_claude_code(text):
    """Claude Code CLI in print mode (uses your Claude subscription's usage)."""
    result = subprocess.run(
        # --tools "": Claude only reads the text we pipe in; it can't run anything
        [
            shutil.which("claude"),
            "-p",
            AI_INSTRUCTIONS,
            "--tools",
            "",
            "--no-session-persistence",
            "--output-format",
            "json",
            "--json-schema",
            json.dumps(PLAN_SCHEMA),
        ],
        input=text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=300,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout).strip()[:300])
    out = json.loads(result.stdout)
    if out.get("is_error"):
        raise RuntimeError(str(out.get("result", ""))[:300])
    return out.get("structured_output") or json.loads(out["result"])


def ai_study_plan(data, now):
    """Pick an AI backend: API key if set, else Claude Code if installed, else none."""
    text = ai_input(data, now)
    if os.environ.get("ANTHROPIC_API_KEY"):
        plan = ai_via_api(text)
    elif shutil.which("claude"):
        plan = ai_via_claude_code(text)
    else:
        return None
    if not plan:
        return None
    # Only keep links to items that actually exist
    ids = {a["id"] for a in data["upcoming"] + data["missing"]}
    for t in plan.get("today", []):
        if t.get("item_id") not in ids:
            t["item_id"] = ""
    return plan


# ---------------------------------------------------------------- Dashboard


DAY_NAMES = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def _parse_hhmm(text, now):
    """'14:30' or '2:30 PM' -> a datetime today, or None."""
    text = str(text or "").strip()
    for fmt in ("%H:%M", "%I:%M %p", "%I %p", "%I:%M%p"):
        try:
            t = datetime.strptime(text.upper(), fmt).time()
            return now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
        except ValueError:
            continue
    return None


def climb_config(settings, now):
    """Config for the hero climb timer: whether to show it, the next objective,
    and today's busy windows (class times etc.) so the timer can skip over them."""
    today_idx = now.weekday()
    busy = []
    sched = settings.get("schedule") or {}
    entries = sched.get("classes", []) if isinstance(sched, dict) else []
    for e in entries if isinstance(entries, list) else []:
        if not isinstance(e, dict):
            continue
        days = str(e.get("days", "")).lower()
        hit = any(name in days and DAY_NAMES[name] == today_idx for name in DAY_NAMES)
        start, end = _parse_hhmm(e.get("start"), now), _parse_hhmm(e.get("end"), now)
        if hit and start and end and end > start:
            busy.append({"start": start.isoformat(), "end": end.isoformat(), "label": e.get("name", "Class")})
    busy.sort(key=lambda b: b["start"])

    obj = settings.get("next_objective")
    objective = None
    if isinstance(obj, dict) and obj.get("name") and obj.get("date"):
        objective = {"name": obj["name"], "date": obj["date"]}

    return {
        "show": bool(settings.get("show_climb_timer", True)),
        "objective": objective,
        "busyToday": busy,
    }


def build_dashboard(data, plan, warning, now, hero):
    """Write dashboard.html: the template with this run's data embedded."""
    courses = sorted(
        {c["name"] for c in data["courses"].values()}
        | {a["course"] for a in data["upcoming"] + data["missing"] if a["course"]}
    )
    scores = {c["name"]: c["score"] for c in data["courses"].values()}

    def item(a):
        return {
            "id": a["id"],
            "course": a["course"],
            "title": a["title"],
            "type": a.get("type", "assignment"),
            "points": a["points"],
            "due": a["due"].isoformat() if a["due"] else None,
            "url": a["url"],
            "note": a.get("note", ""),
            "source": a.get("source", ""),
        }

    payload = {
        "generated": now.isoformat(),
        "today": now.date().isoformat(),
        "daysAhead": DAYS_AHEAD,
        "hasStatus": data.get("has_status", False),
        "warning": warning,
        "courses": [
            {"name": n, "score": scores.get(n), "change": data.get("grade_deltas", {}).get(n)}
            for n in courses
        ],
        "upcoming": [item(a) for a in data["upcoming"]],
        "missing": [item(a) for a in data["missing"]],
        "announcements": [
            {
                "course": a["course"],
                "title": a["title"],
                "posted": a["posted"].isoformat() if a["posted"] else None,
                "url": a.get("url", ""),
            }
            for a in data["announcements"]
        ],
        "plan": plan,
        "todayTasks": plan["today"] if plan else default_today(data, now),
        "gradeHistory": data.get("grade_history", {}),
        "gradeChanges": data.get("grade_changes", [])[:40],
        "hero": hero,
        "climb": data.get("climb"),
        "siteErrors": data.get("site_errors", []),
    }
    blob = json.dumps(payload, ensure_ascii=False)
    # Keep the JSON from closing the <script> tag early
    blob = blob.replace("</", "<\\/").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    html = DASHBOARD_TEMPLATE.read_text(encoding="utf-8").replace("/*__BRIEF_DATA__*/null", blob)
    DASHBOARD_FILE.write_text(html, encoding="utf-8")
    return DASHBOARD_FILE


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
                    "Canvas login expired. On your computer, run "
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


STARTUP_BAT = "Canvas Morning Brief.bat"
REFRESH_BAT = "Refresh Morning Brief.bat"


def _bat(args):
    return (
        "@echo off\r\ntitle Canvas Morning Brief\r\n"
        f'cd /d "{HERE}"\r\n'
        f'"{sys.executable}" "{HERE / "morning_brief.py"}" {args}\r\n'
        "if errorlevel 1 pause\r\n"
    )


def _startup_dir():
    return Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def _desktop_dir():
    home = Path(os.environ.get("USERPROFILE", Path.home()))
    for d in (home / "OneDrive" / "Desktop", home / "Desktop"):
        if d.is_dir():
            return d
    return home


def install_startup(remove=False):
    """Windows: run the brief when you log in, plus a 'Refresh' shortcut on the Desktop."""
    if os.name != "nt":
        sys.exit(
            "Automatic startup setup is for Windows. On Mac/Linux, add this with `crontab -e`:\n"
            f"30 7 * * * cd '{HERE}' && '{sys.executable}' morning_brief.py --no-open"
        )
    targets = [(_startup_dir() / STARTUP_BAT, "--startup"), (_desktop_dir() / REFRESH_BAT, "")]
    for path, args in targets:
        if remove:
            path.unlink(missing_ok=True)
            print(f"✓ Removed {path}")
        else:
            path.write_text(_bat(args), encoding="utf-8")
            print(f"✓ Created {path}")
    if not remove:
        print(
            "\nDone! Each time you log in to Windows, the brief runs once (the first time\n"
            "that day) and opens your dashboard. Later logins just reopen it.\n"
            "Double-click 'Refresh Morning Brief' on your Desktop to update it anytime."
        )


def ran_today(now):
    try:
        html = DASHBOARD_FILE.read_text(encoding="utf-8")
    except OSError:
        return False
    m = re.search(r'"today": "(\d{4}-\d\d-\d\d)"', html)
    return bool(m and m.group(1) == now.date().isoformat())


def wait_for_internet(timeout=120):
    """Right after login Wi-Fi may not be connected yet; wait a bit for it."""
    url = CANVAS_BASE_URL or CANVAS_ICS_URL.replace("webcal://", "https://", 1)
    if not url:
        return
    deadline = time.time() + timeout
    while True:
        try:
            requests.head(url, timeout=10)
            return
        except requests.RequestException:
            if time.time() > deadline:
                return
            print("Waiting for internet…", file=sys.stderr)
            time.sleep(5)


def self_check():
    """Test each piece and print plain-English results (safe to paste to someone helping you)."""
    ok = lambda msg: print(f"  OK    {msg}")
    bad = lambda msg: print(f"  PROBLEM  {msg}")
    print(f"\nCanvas Morning Brief self-check  (folder: {HERE.name})\n")

    print(f"  info  Python {sys.version.split()[0]}")
    ok(".env file found") if (HERE / ".env").exists() else bad(
        ".env file missing. Copy .env.example to .env (setup_windows.bat does this)"
    )
    if not CANVAS_BASE_URL:
        bad("CANVAS_BASE_URL is empty in .env. Set it to https://montana.instructure.com")
    elif "yourschool" in CANVAS_BASE_URL:
        bad("CANVAS_BASE_URL is still the example. In .env set it to https://montana.instructure.com")
    elif not CANVAS_BASE_URL.startswith("https://") or "/" in CANVAS_BASE_URL[8:]:
        bad(f"CANVAS_BASE_URL should look like https://montana.instructure.com (yours: {CANVAS_BASE_URL})")
    else:
        ok(f"CANVAS_BASE_URL = {CANVAS_BASE_URL}")
    print(f"  info  TIMEZONE = {TZ.key}")

    logged_in = False
    if not SESSION_FILE.exists():
        bad("Not logged in yet. Run: python morning_brief.py --login")
    elif CANVAS_BASE_URL:
        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as p:
                api = p.request.new_context(storage_state=str(SESSION_FILE))
                r = api.get(f"{CANVAS_BASE_URL}/api/v1/users/self", max_redirects=0, timeout=30_000)
                if r.status == 200:
                    name = json.loads(r.text().removeprefix("while(1);")).get("name")
                    ok(f"Canvas login works (logged in as {name})")
                    logged_in = True
                else:
                    bad(f"Canvas login didn't work (Canvas said {r.status}). Run: python morning_brief.py --login")
                api.dispose()
        except Exception as e:
            bad(f"Couldn't test the Canvas login: {e}")

    path = SETTINGS_FILE if SETTINGS_FILE.exists() else HERE / "my_settings.example.json"
    if not SETTINGS_FILE.exists():
        bad("my_settings.json not found, so the example settings are being used. Copy it: copy my_settings.example.json my_settings.json")
    try:
        settings = json.loads(path.read_text(encoding="utf-8"))
        ok(f"{path.name} reads fine")
    except ValueError as e:
        bad(f"{path.name} has a typo at line {getattr(e, 'lineno', '?')}: {e.msg if hasattr(e, 'msg') else e}")
        settings = {}

    good_sites = []
    for site in settings.get("sites", []):
        link = str(site.get("canvas_link", ""))
        label = f"{site.get('type', '?')} ({site.get('course', '')})"
        if not link.startswith("http"):
            bad(f"{label}: canvas_link still needs to be pasted in")
        else:
            from sites import clean_link, link_goes_to_site

            kind = str(site.get("type", "")).lower()
            if link_goes_to_site(kind, link):
                ok(f"{label}: link goes straight to the site (fine; you'll sign in with MSU when asked)")
            elif CANVAS_BASE_URL and not link.startswith(CANVAS_BASE_URL):
                bad(f"{label}: link should start with {CANVAS_BASE_URL} or the site's own address (yours: {link[:40]}...)")
                continue
            else:
                ok(f"{label}: link looks right")
            if clean_link(link) != link:
                print("  info  (the temporary login key in that link is ignored; it isn't needed)")
            good_sites.append(dict(site, canvas_link=clean_link(link)))

    ok("Claude Code found (AI plan on)") if shutil.which("claude") else print(
        "  info  Claude Code not installed (AI plan off; everything else works)"
    )

    if good_sites and logged_in:
        print("\n  Now opening WeBWorK/Labflow in a visible browser so you can watch...\n")
        from sites import fetch_sites

        results, errors = fetch_sites(
            good_sites, SESSION_FILE, TZ, DEBUG_DIR, show_browser=True, interactive=True,
            cache_file=HERE / "site_cache.json",
        )
        from sites import EMAIL, site_items

        for idx, site in enumerate(good_sites):
            kind = site["type"].lower()
            items = results.get(idx, [])
            print()
            (ok if items else bad)(f"{kind}: understood {len(items)} assignments on the page")
            for a in items[:15]:
                fmt = lambda d: f"{d.strftime('%b')} {d.day} {fmt_clock(d)}" if d else "-"
                print(f"        {'DONE ' if a.get('done') else ''}{a['title']} | due {fmt(a['due'])}"
                      + (f" | late until {fmt(a['late_until'])}" if a.get("late_until") else ""))
                if a.get("note"):
                    print(f"            {a['note']}")
            if items:
                up, miss = site_items(kind, [dict(a) for a in items], datetime.now(TZ), DAYS_AHEAD)
                print(f"        -> {len(up)} due in the next {DAYS_AHEAD} days, {len(miss)} late/missing (these go on the dashboard)")
            seen = DEBUG_DIR / f"{kind}-seen.txt"
            if not any(a.get("due") for a in items) and seen.exists():
                lines = [l.strip() for l in seen.read_text(encoding="utf-8").splitlines() if l.strip()]
                key = re.compile(r"page:|set links|LINK|Due|Open|Close|Cut-?Off|answers|Opens|Closes|Coming", re.I)
                print("        What the bot saw (key lines):")
                for l in [l for l in lines if key.search(l)][:30]:
                    print(f"          {EMAIL.sub('[email]', l)[:150]}")
                print(f"        Full text: {seen}")
        print()
        for e in errors:
            bad(e)
        if errors:
            print(f"\n  Screenshots of what the bot saw are in: {DEBUG_DIR}")
    print("\nCopy everything above and send it if you need help (it has no passwords or keys).\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="don't send email/Discord/push")
    parser.add_argument("--login", action="store_true", help="log in to Canvas in a browser")
    parser.add_argument("--no-ai", action="store_true", help="skip the AI study plan")
    parser.add_argument("--no-open", action="store_true", help="don't open the dashboard")
    parser.add_argument("--startup", action="store_true", help="run once per day, then just reopen")
    parser.add_argument("--install-startup", action="store_true", help="run at Windows login")
    parser.add_argument("--remove-startup", action="store_true", help="undo --install-startup")
    parser.add_argument("--check", action="store_true", help="test each step and show what's wrong")
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
    if args.install_startup or args.remove_startup:
        install_startup(remove=args.remove_startup)
        return
    if args.check:
        self_check()
        return

    settings = load_settings()
    now = datetime.now(TZ)
    if args.startup:
        if ran_today(now):
            print("Already updated today; opening your dashboard.", file=sys.stderr)
            webbrowser.open(DASHBOARD_FILE.as_uri())
            return
        wait_for_internet()

    print("Checking Canvas…", file=sys.stderr)
    data, warning = load_data(now)
    try:
        data["site_errors"] = add_sites(data, settings, now)
    except Exception as e:  # a broken site never stops the brief
        data["site_errors"] = [f"Couldn't check WeBWorK/Labflow: {e}"]
    for err in data["site_errors"]:
        print(f"⚠ {err}", file=sys.stderr)
    add_reminders(data, settings, now)
    assign_ids(data)
    if data["courses"]:
        data["grade_deltas"] = update_snapshots(data["courses"], now)

    from motivation import daily_pick

    hero = daily_pick(now, settings, HERE)
    data["hobby"] = hero["hobby"]
    data["climb"] = climb_config(settings, now)

    plan = None
    if not args.no_ai:
        try:
            if shutil.which("claude") or os.environ.get("ANTHROPIC_API_KEY"):
                print("Asking Claude for today's plan…", file=sys.stderr)
            plan = ai_study_plan(data, now)
        except Exception as e:  # never lose the brief because the AI step failed
            print(f"AI study plan skipped: {e}", file=sys.stderr)

    brief = render_plain(data, now, plan)
    if warning:
        brief = f"⚠️ **{warning}**\n\n{brief}"
    brief += f"\n> {hero['quote']}" + (f" — {hero['by']}" if hero["by"] else "") + "\n"
    (HERE / "brief.md").write_text(brief, encoding="utf-8")

    dashboard = build_dashboard(data, plan, warning, now, hero)
    print(f"✓ Dashboard: {dashboard}", file=sys.stderr)
    if args.startup or (not args.no_open and sys.stdout.isatty()):
        webbrowser.open(dashboard.as_uri())

    if args.dry_run:
        return
    subject = f"Morning Brief — {fmt_day(now)}"
    send_email(subject, brief)
    send_discord(brief)
    send_ntfy(subject, brief)


if __name__ == "__main__":
    main()
