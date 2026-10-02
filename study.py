"""Build a day-by-day study schedule for upcoming exams, modeled on the plan at
the top of the HTML study guides: work backward from each exam date, spread the
units across the days before it (new material -> practice -> full review the day
before), give the weaker-grade course more time, and keep each day realistic
around class times.

Rule-based and deterministic (no AI needed). The output feeds the dashboard's
Study schedule card and the single "Do today" list.
"""

import re
from datetime import datetime, timedelta


def _task_id(date_iso, text):
    h = 2166136261
    for ch in (date_iso + '|' + text).encode('utf-8'):
        h = ((h ^ ch) * 16777619) & 0xFFFFFFFF
    return 's' + format(h, '08x')

DAY_NAMES = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
DEFAULT_DAILY_CAP = 150  # minutes of studying on a free day
MIN_DAILY_CAP = 45
# per-unit base minutes for each task kind
NEW_MATERIAL_MIN = 40
PRACTICE_MIN = 30
REVIEW_MIN = 60
HORIZON = 7  # today + next 6 days


def _fmt_day(d):
    return f"{d.strftime('%a %b')} {d.day}"


def _parse_date(s):
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(str(s).strip(), fmt).date()
        except (ValueError, TypeError):
            continue
    return None


def _classes_on(now, day, settings):
    """(total class minutes, [labels]) for a given date from schedule.classes."""
    sched = settings.get("schedule") or {}
    entries = sched.get("classes", []) if isinstance(sched, dict) else []
    total, labels = 0, []
    for e in entries if isinstance(entries, list) else []:
        if not isinstance(e, dict):
            continue
        days = str(e.get("days", "")).lower()
        if not any(name in days and DAY_NAMES[name] == day.weekday() for name in DAY_NAMES):
            continue
        start = _hhmm(e.get("start"))
        end = _hhmm(e.get("end"))
        if start is not None and end is not None and end > start:
            total += end - start
        labels.append(e.get("name", "Class"))
    return total, labels


def _hhmm(text):
    text = str(text or "").strip().upper()
    for fmt in ("%H:%M", "%I:%M %p", "%I %p", "%I:%M%p"):
        try:
            t = datetime.strptime(text, fmt)
            return t.hour * 60 + t.minute
        except ValueError:
            continue
    return None


def _grade_factor(course, data):
    """Lower grade -> more time. Returns a multiplier ~0.8..1.4."""
    score = None
    for c in data.get("courses", {}).values():
        if _same_course(c.get("name", ""), course) and c.get("score") is not None:
            score = c["score"]
    if score is None:
        return 1.0
    if score < 70:
        return 1.4
    if score < 80:
        return 1.2
    if score < 90:
        return 1.0
    return 0.8


def _same_course(a, b):
    squash = lambda t: re.sub(r"[^a-z0-9]", "", (t or "").lower())
    a, b = squash(a), squash(b)
    return bool(a) and bool(b) and (a in b or b in a)


def collect_exams(settings, data, now, guides_dir=None):
    """Exams from study-guide HTML files, my_settings.json, and Canvas, deduped."""
    exams = []
    # 1) study guides dropped into the folder (each guide = one exam)
    if guides_dir is not None:
        try:
            from guides import scan_guides
            parsed, _ = scan_guides(guides_dir, now)
            for g in parsed:
                exams.append({
                    "course": g["course"], "name": g["name"], "date": g["date"],
                    "units": g["units"], "covers": g["covers"], "url": g["url"],
                })
        except Exception:
            pass
    for e in settings.get("exams", []) if isinstance(settings.get("exams"), list) else []:
        if not isinstance(e, dict):
            continue
        d = _parse_date(e.get("date"))
        if not d:
            continue
        if any(x["date"] == d and _same_course(x["course"], e.get("course", "")) for x in exams):
            continue
        units = e.get("units") or []
        exams.append(
            {
                "course": e.get("course", ""),
                "name": e.get("name", "Exam"),
                "date": d,
                "units": [str(u) for u in units] if isinstance(units, list) else [str(units)],
                "covers": e.get("covers", ""),
                "url": e.get("url", ""),
            }
        )
    # Canvas-side exams/quizzes (so ones without a settings entry still appear)
    for a in data.get("upcoming", []):
        title = a.get("title", "")
        # site homework (WeBWorK) and Labflow pre-labs aren't the lecture exams/quizzes to plan around
        if a.get("source") in ("WeBWorK", "Labflow", "Reminder"):
            continue
        if re.search(r"pre-?lab|practice|reading|homework|discussion", title, re.I):
            continue
        is_exam = re.search(r"\b(exam|midterm|final|test)\b", title, re.I) or (
            a.get("type") == "quiz" and re.search(r"\bquiz\b", title, re.I)
        )
        if not is_exam or not a.get("due"):
            continue
        d = a["due"].date() if hasattr(a["due"], "date") else _parse_date(a["due"])
        if not d:
            continue
        if any(x["date"] == d and _same_course(x["course"], a.get("course", "")) for x in exams):
            continue  # already covered by a settings entry
        exams.append(
            {
                "course": a.get("course", ""),
                "name": title,
                "date": d,
                "units": [],
                "covers": "",
                "url": a.get("url", ""),
            }
        )
    exams.sort(key=lambda x: x["date"])
    return exams


def _guide_for(course, unit, settings):
    """Find a study guide for a unit: {title, url} or None."""
    for g in settings.get("study_guides", []) if isinstance(settings.get("study_guides"), list) else []:
        if not isinstance(g, dict):
            continue
        if _same_course(g.get("course", ""), course) and (
            not unit or str(g.get("unit", "")).lower() in unit.lower() or unit.lower() in str(g.get("unit", "")).lower()
        ):
            return {"title": g.get("title", g.get("unit", "guide")), "url": g.get("file") or g.get("url", "")}
    return None


def unit_parts(u):
    """Normalize a unit entry -> (label, minutes_override|None)."""
    if isinstance(u, dict):
        label = str(u.get("topic") or u.get("title") or u.get("unit") or "").strip()
        mins = u.get("minutes")
        try:
            mins = int(mins) if mins is not None else None
        except (TypeError, ValueError):
            mins = None
        return label or "the material", mins
    return str(u), None


def _exam_tasks(exam, now, settings, data):
    """Backward plan for one exam: {offset_days_from_today: [task,...]}."""
    today = now.date()
    days_until = (exam["date"] - today).days
    if days_until < 0:
        return {}
    factor = _grade_factor(exam["course"], data)
    code = exam["course"] or exam["name"]
    url = exam.get("url", "")
    units = exam["units"] or ["the material"]
    by_offset = {}

    def add(offset, text, minutes, unit=None, scale=True):
        guide = _guide_for(exam["course"], unit or "", settings)
        by_offset.setdefault(offset, []).append(
            {
                "course": code,
                "text": text,
                "minutes": int(round(minutes * factor)) if scale else int(round(minutes)),
                "url": (guide or {}).get("url") or url,
                "exam": f"{code} {exam['name']}",
            }
        )

    # the day before the exam (or exam day if it's tomorrow/today) is full review
    review_offset = max(0, days_until - 1)
    # study days available for new material + practice: today .. day before review
    work_offsets = [o for o in range(0, review_offset)] or [review_offset]

    # one "new material" task and one "practice" task per unit, spread in order
    unit_tasks = []
    for u in units:
        label, override = unit_parts(u)
        unit_tasks.append(("new", label, override))
        unit_tasks.append(("practice", label, override))
    for i, (kind, label, override) in enumerate(unit_tasks):
        offset = work_offsets[i % len(work_offsets)]
        if kind == "new":
            mins = override if override is not None else NEW_MATERIAL_MIN
            add(offset, f"{code} {label} — read the guide / notes", mins, label, scale=override is None)
        else:
            add(offset, f"{code} {label} — practice problems", PRACTICE_MIN, label)

    if days_until >= 1:
        covers = f" (all units)" if len(units) > 1 else ""
        add(review_offset, f"{code} — full review{covers}, day before the exam", REVIEW_MIN)
    return by_offset


def build_study_schedule(settings, data, now, guides_dir=None):
    """Return the study schedule: exams, a day-by-day plan, today's tasks, and a flag."""
    exams = collect_exams(settings, data, now, guides_dir)
    relevant = [e for e in exams if 0 <= (e["date"] - now.date()).days <= 21]
    if not relevant:
        return None

    # merge every exam's tasks by day offset
    per_day = {o: [] for o in range(HORIZON)}
    overflow_flags = []
    for exam in relevant:
        for offset, tasks in _exam_tasks(exam, now, settings, data).items():
            if offset < HORIZON:
                per_day[offset].extend(tasks)

    days = []
    for offset in range(HORIZON):
        d = (now + timedelta(days=offset)).date()
        class_min, class_labels = _classes_on(now, d, settings)
        cap = max(MIN_DAILY_CAP, DEFAULT_DAILY_CAP - class_min)
        tasks = per_day[offset]
        for t in tasks:
            t["id"] = _task_id(d.isoformat(), t["text"])
        total = sum(t["minutes"] for t in tasks)
        behind = total > cap + 30 and offset <= 1  # today/tomorrow clearly overloaded
        if behind:
            overflow_flags.append(d)
        days.append(
            {
                "date": d.isoformat(),
                "label": "Today" if offset == 0 else ("Tomorrow" if offset == 1 else _fmt_day(d)),
                "weekday": d.strftime("%a"),
                "classes": class_labels,
                "tasks": tasks,
                "totalMinutes": total,
                "capMinutes": cap,
                "over": total > cap,
            }
        )

    # today's study tasks, shaped like Do-today items so they feed the one list
    today_tasks = [
        {
            "task": t["text"],
            "course": t["course"],
            "minutes": t["minutes"],
            "why": t["exam"],
            "item_id": t["id"],
            "url": t["url"],
            "source": "Study",
        }
        for t in days[0]["tasks"]
    ]

    exam_cards = [
        {
            "course": e["course"],
            "name": e["name"],
            "date": e["date"].isoformat(),
            "daysLeft": (e["date"] - now.date()).days,
            "covers": e["covers"] or ", ".join(unit_parts(u)[0] for u in e["units"]) if e["units"] else e["covers"],
        }
        for e in relevant
    ]

    behind_msg = ""
    if overflow_flags:
        behind_msg = (
            "Heads up: the next day or two are packed tighter than a normal study load. "
            "Start the earliest exam's units today so it doesn't bunch up."
        )

    return {
        "exams": exam_cards,
        "days": days,
        "todayTasks": today_tasks,
        "behind": behind_msg,
    }
