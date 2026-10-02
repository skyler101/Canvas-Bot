"""Read study-guide HTML files and turn each into an exam the scheduler can plan.

You keep authoring guides in Claude and dropping the .html into the study_guides/
folder. Two ways the bot reads one:

1. A metadata block (reliable). Put this near the top of the guide:

     <script type="application/study-guide+json">
     { "course": "CHMY 141", "unit": "Unit 4", "exam": "Exam 2",
       "exam_date": "2026-10-20",
       "sections": ["4.1 Empirical formulas", "4.2 Hydrates", "4.3 Combustion"],
       "practice_total": 40 }
     </script>

2. No block? The bot falls back to reading the <title>/<h1>, an "Exam ..." fact
   or a "Exam is <Month> <day>" line, and the numbered section headings.

Either way it produces {course, name, date, units, covers, url} for study.py.
"""

import json
import re
from datetime import date, datetime
from html import unescape
from pathlib import Path

META_RE = re.compile(
    r'<script[^>]*type=["\']application/study-guide\+json["\'][^>]*>(.*?)</script>',
    re.I | re.S,
)
MONTHS = {
    m: i
    for i, m in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1
    )
}
COURSE_RE = re.compile(r"\b([A-Z]{1,5})\s?(\d{3})\b")
# "4.1 Title" or "Unit 4" style section headings
SECTION_RE = re.compile(r"<h[1-4][^>]*>\s*(\d+\.\d+[^<]*)</h[1-4]>", re.I)


def _text(html_fragment):
    return unescape(re.sub(r"<[^>]+>", " ", html_fragment)).strip()


def _title(html):
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    if m:
        return _text(m.group(1))
    m = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.I | re.S)
    return _text(m.group(1)) if m else ""


def _course_from(text):
    m = COURSE_RE.search(text)
    return f"{m.group(1)} {m.group(2)}" if m else ""


def _unit_from(text):
    m = re.search(r"\b(Unit|Exam|Midterm|Final|Chapter|Ch)\s+(\w+)", text, re.I)
    return f"{m.group(1).title()} {m.group(2)}" if m else ""


def _parse_date(text, default_year):
    text = text.replace(",", " ")
    m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", text)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    m = re.search(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b", text)
    if m:
        y = m.group(3)
        year = default_year if not y else (2000 + int(y) if len(y) == 2 else int(y))
        try:
            return date(year, int(m.group(1)), int(m.group(2)))
        except ValueError:
            return None
    months = "jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec"
    m = re.search(rf"\b({months})[a-z]*\.?\s+(\d{{1,2}})\b", text, re.I)
    if m:
        try:
            return date(default_year, MONTHS[m.group(1)[:3].lower()], int(m.group(2)))
        except ValueError:
            return None
    return None


def _group_sections(sections, unit_label):
    """Many '1.1, 1.2, ... 2.1' sections -> a few chunks like 'Ch 1 (1.1-1.12)'.

    Keeps a short, plannable set of units instead of 30 tiny rows."""
    numbered = [s for s in sections if re.match(r"\s*\d+\.\d+", s)]
    if len(numbered) <= 8:
        return sections[:8] if sections else ([unit_label] if unit_label else [])
    groups = {}
    for s in numbered:
        major = re.match(r"\s*(\d+)\.", s).group(1)
        groups.setdefault(major, []).append(re.match(r"\s*(\d+\.\d+)", s).group(1))
    out = []
    for major, subs in groups.items():
        out.append(f"Ch {major} ({subs[0]}–{subs[-1]})" if len(subs) > 1 else subs[0])
    return out


def parse_guide(path, now=None):
    """Return {course, name, date(date|None), units, covers, url, title} for one guide."""
    now = now or datetime.now()
    html = Path(path).read_text(encoding="utf-8", errors="replace")
    title = _title(html)

    meta = {}
    m = META_RE.search(html)
    if m:
        try:
            meta = json.loads(m.group(1).strip())
        except ValueError:
            meta = {}

    course = meta.get("course") or _course_from(title) or _course_from(html[:4000])
    unit = meta.get("unit") or _unit_from(title)
    exam_name = meta.get("exam") or (f"{unit} exam" if unit else "Exam")

    exam_date = None
    if meta.get("exam_date"):
        exam_date = _parse_date(str(meta["exam_date"]), now.year)
    if exam_date is None:
        # look for an "Exam ..." fact or an "Exam is <date>" line in the text
        body = _text(html)
        for mm in re.finditer(r"(?i)\b(?:exam|midterm|final|test)\b[^.]{0,60}", body):
            d = _parse_date(mm.group(0), now.year)
            if d:
                exam_date = d
                break
    # if the parsed date already passed this year, assume next occurrence isn't our job —
    # leave it; the scheduler only shows exams within its window

    if meta.get("sections"):
        # metadata sections pass through as-is (string, or {"topic","minutes"})
        units = [u for u in meta["sections"] if (isinstance(u, dict) and u.get("topic")) or (isinstance(u, str) and u.strip())]
    else:
        sections = [_text(s) for s in SECTION_RE.findall(html)]
        sections = [s for s in sections if s]
        units = _group_sections(sections, unit)
    if not meta.get("sections") and (len(units) > 8 or len(units) <= 2):
        covers_text = ""
        for span in re.findall(r'class="fact"[^>]*>(.*?)</span>', html, re.I | re.S):
            txt = _text(span)
            if re.match(r"(?i)covers?\b", txt):
                covers_text = re.sub(r"(?i)^covers?\b[:\s]*", "", txt)
                break
        if not covers_text:
            cov = re.search(r"(?i)\bcovers?\b[:\s]*([A-Za-z0-9 .,&–\-]{3,100})", _text(html))
            covers_text = cov.group(1) if cov else ""
        parts = [u.strip() for u in re.split(r"[;,]", covers_text) if u.strip()]
        if len(parts) >= 3:
            units = parts[:12]
    def _label(u):
        return u.get("topic") or u.get("title") or u.get("unit") or "" if isinstance(u, dict) else str(u)
    covers = meta.get("covers") or (", ".join(_label(u) for u in units) if units else "")

    return {
        "course": course,
        "name": exam_name,
        "date": exam_date,
        "units": units,
        "covers": covers,
        "url": str(path),
        "title": title,
    }


def scan_guides(folder, now=None):
    """Parse every .html in `folder`. Returns (exams_with_dates, skipped[])."""
    folder = Path(folder)
    if not folder.is_dir():
        return [], []
    exams, skipped = [], []
    for path in sorted(folder.glob("*.html")):
        try:
            g = parse_guide(path, now)
        except Exception as e:
            skipped.append(f"{path.name}: {e}")
            continue
        if g["date"]:
            exams.append(g)
        else:
            skipped.append(f"{path.name}: couldn't find an exam date (add exam_date in the metadata block)")
    return exams, skipped
