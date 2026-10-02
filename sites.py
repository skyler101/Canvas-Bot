"""Read assignments from outside sites that you open through Canvas (WeBWorK, Labflow).

These sites have no API you can use, and you only get in by clicking their link
inside Canvas. So the bot does the same thing: it opens that Canvas link in a
hidden browser using your saved Canvas login, waits for the site to load, and
reads the page.

Each site is set up in my_settings.json with the Canvas link you click to open
it (right-click the link in Canvas -> "Copy link address").
"""

import re
import sys
import time
from urllib.parse import urlsplit
from datetime import datetime, timedelta

# Any of these in a page's address means we've reached the site
SITE_HOSTS = {"webwork": ("webwork",), "labflow": ("catalystedu.com", "labflow.com")}
NEW_WINDOW = re.compile(r"in a new (browser )?(window|tab)", re.I)
SITE_READY = {
    "webwork": re.compile(r"\bDue\b|Will open|Answers available|Closed", re.I),
    "labflow": re.compile(r"\b(Opened|Opens|Closes|Closed)\s+\d\d/\d\d/\d{4}", re.I),
}
SITE_LABELS = {"webwork": "WeBWorK", "labflow": "Labflow"}

# Runs inside the WeBWorK page: every link to a problem set, with the text around it
WEBWORK_JS = """() => {
  const skip = new Set(["hardcopy", "grades", "options", "instructor", "achievements", "logout", "feedback", "show_me_another"]);
  const out = [];
  for (const a of document.querySelectorAll("a[href]")) {
    let u; try { u = new URL(a.href, location.href); } catch { continue; }
    const parts = u.pathname.split("/").filter(Boolean);
    const i = parts.indexOf("webwork2");
    if (i < 0 || parts.length !== i + 3 || skip.has(parts[i + 2])) continue;
    const box = a.closest("li") || a.closest("tr") || (a.parentElement && a.parentElement.parentElement) || a.parentElement;
    out.push({ name: a.textContent.trim(), text: box ? box.innerText : "" });
  }
  return out;
}"""


# ---------------------------------------------------------------- Parsers


def _parse_dt(s, tz, formats):
    s = re.sub(r"\s+", " ", s.replace(",", " ").replace(" at ", " ")).strip()
    for fmt in formats:
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=tz)
        except ValueError:
            continue
    return None


WW_DATE = r"([A-Z][a-z]+ \d{1,2}, \d{4},? (?:at )?\d{1,2}:\d{2}(?::\d{2})? ?[AP]M)"
WW_FORMATS = ["%B %d %Y %I:%M:%S %p", "%B %d %Y %I:%M %p", "%b %d %Y %I:%M:%S %p", "%b %d %Y %I:%M %p"]


def parse_webwork(sets, tz):
    """[{name, text}] from the WeBWorK sets page -> list of assignments."""
    out, seen = [], set()
    for s in sets:
        name, text = s["name"], s.get("text", "")
        if not name or name in seen or name.lower().startswith("download"):
            continue
        seen.add(name)
        due = re.search(r"\bDue (?:on )?" + WW_DATE, text)
        reduced = re.search(r"reduced credit[^.]*?until " + WW_DATE, text, re.I)
        opens = re.search(r"(?:Will open|Opens) (?:on )?" + WW_DATE, text, re.I)
        item = {
            "title": name.replace("_", " "),
            "due": _parse_dt(due.group(1), tz, WW_FORMATS) if due else None,
            "late_until": _parse_dt(reduced.group(1), tz, WW_FORMATS) if reduced else None,
            "opens": _parse_dt(opens.group(1), tz, WW_FORMATS) if opens else None,
            "closed": bool(re.search(r"Answers available|\bClosed\b", text)),
        }
        if item["due"] or item["opens"]:
            out.append(item)
    return out


LF_DATE = r"(\d\d/\d\d/\d{4} \d{1,2}:\d\d ?[AP]M)"
LF_FORMATS = ["%m/%d/%Y %I:%M%p", "%m/%d/%Y %I:%M %p"]
LF_ROW = re.compile(
    r"(?P<title>.*?)\s*(?:Opened|Opens)\s+" + LF_DATE + r"\s*\(?[A-Z]{2,4}\)?\s*(?:Closed|Closes)\s+" + LF_DATE,
    re.I,
)


def parse_labflow(text, tz):
    """Labflow course page text -> list of assignments (with done status)."""
    lines = [l.strip() for l in text.splitlines()]
    lines = [l for l in lines if l]
    out, seen = [], set()
    for i, line in enumerate(lines):
        m = LF_ROW.search(line)
        if not m and i + 1 < len(lines):
            # title and dates can be split across two lines
            m2 = LF_ROW.search(line + " " + lines[i + 1])
            if m2 and not LF_ROW.search(lines[i + 1]):
                m = m2
        if not m:
            continue
        title, title_idx = m.group("title").strip(), i
        if not title and i > 0:
            title, title_idx = lines[i - 1], i - 1
        if not title or title in seen:
            continue
        seen.add(title)
        # Labflow puts a "done" check mark line right above finished activities
        done = any(lines[j].lower() == "done" for j in range(max(0, title_idx - 2), title_idx))
        out.append(
            {
                "title": title,
                "opens": _parse_dt(m.group(2).upper(), tz, LF_FORMATS),
                "due": _parse_dt(m.group(3).upper(), tz, LF_FORMATS),
                "done": done,
                "late_until": None,
            }
        )
    # "Coming up" panel lists late cut-offs: "<title>...", "Open: ...", "Close: ...", "Cut-Off: ..."
    for i, line in enumerate(lines):
        cut = re.match(r"Cut-?Off:\s*" + LF_DATE, line, re.I)
        if not cut:
            continue
        for j in range(i - 1, max(-1, i - 6), -1):
            for item in out:
                if item["title"] in lines[j] and not item["late_until"]:
                    item["late_until"] = _parse_dt(cut.group(1).upper(), tz, LF_FORMATS)
                    break
            else:
                continue
            break
    return out


# ---------------------------------------------------------------- Browser


def _where(context):
    """Where each open window/frame ended up: address (without login keys) and title."""
    seen = []
    for page in context.pages:
        try:
            title = page.title()
        except Exception:
            title = ""
        for frame in page.frames:
            u = urlsplit(frame.url)
            if u.scheme not in ("http", "https"):
                continue
            spot = f"{u.netloc}{u.path}"
            if frame is page.main_frame and title:
                spot += f' ("{title[:60]}")'
            if spot not in seen:
                seen.append(spot)
    return " | ".join(seen) or "a blank page"


def _click_new_window(context):
    """Click Canvas's 'Load <tool> in a new window' button (not the sentence above it)."""
    for page in context.pages:
        for frame in page.frames:
            try:
                target = frame.locator("button, a, input[type=submit], [role=button]").filter(has_text=NEW_WINDOW)
                if not target.count():
                    target = frame.locator("input[type=submit][value*='new window' i]")
                if target.count() and target.first.is_visible():
                    target.first.click()
                    return True
            except Exception:
                pass
    return False


def _find_frame(context, hosts, ready, timeout):
    """Wait until some page or iframe on one of `hosts` shows text matching `ready`."""
    deadline = time.time() + timeout
    clicks, next_click = 0, time.time() + 1
    while time.time() < deadline:
        for page in context.pages:
            for frame in page.frames:
                if not any(h in frame.url for h in hosts):
                    continue
                try:
                    if ready.search(frame.inner_text("body", timeout=2000)):
                        return frame
                except Exception:
                    pass
        # Tools like Labflow only open in a new window: press Canvas's button.
        # A first click sometimes does nothing, so press again if no window opened.
        if clicks < 6 and time.time() >= next_click:
            reached = any(any(h in f.url for h in hosts) for pg in context.pages for f in pg.frames)
            if not reached and len(context.pages) < 2 and _click_new_window(context):
                clicks += 1
                next_click = time.time() + 4
        time.sleep(0.5)
    return None


def fetch_sites(sites, session_file, tz, debug_dir, show_browser=False):
    """Open each configured site through Canvas and return {site index: [assignments]}.

    A site that fails is skipped with a message (and a screenshot in debug_dir),
    so one broken site never stops the whole brief.
    """
    from playwright.sync_api import sync_playwright

    results, errors = {}, []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not show_browser)
        try:
            for idx, site in enumerate(sites):
                kind = site.get("type", "").lower()
                label = SITE_LABELS.get(kind, kind)
                if kind not in SITE_HOSTS or not site.get("canvas_link"):
                    errors.append(f"{label or 'site'}: needs a type (webwork/labflow) and a canvas_link")
                    continue
                context = browser.new_context(storage_state=str(session_file))
                try:
                    page = context.new_page()
                    page.goto(site["canvas_link"], wait_until="domcontentloaded", timeout=60_000)
                    canvas_host = urlsplit(site["canvas_link"]).netloc
                    start = urlsplit(page.url)
                    if start.netloc == canvas_host and start.path.startswith("/login"):
                        raise RuntimeError("Canvas showed its login page. Run --login again")
                    frame = _find_frame(context, SITE_HOSTS[kind], SITE_READY[kind], timeout=90)
                    if frame is None:
                        raise RuntimeError(f"couldn't read {label}. The bot ended up at: {_where(context)}")
                    if kind == "webwork":
                        results[idx] = parse_webwork(frame.evaluate(WEBWORK_JS), tz)
                    else:
                        results[idx] = parse_labflow(frame.inner_text("body"), tz)
                    print(f"✓ {label}: found {len(results[idx])} assignments", file=sys.stderr)
                except Exception as e:
                    errors.append(f"{label} ({site.get('course', '')}): {e}")
                    try:
                        debug_dir.mkdir(exist_ok=True)
                        for n, pg in enumerate(context.pages):
                            pg.screenshot(path=str(debug_dir / f"{kind}-{n}.png"), full_page=True)
                    except Exception:
                        pass
                finally:
                    context.close()
        finally:
            browser.close()
    return results, errors


def site_items(kind, parsed, now, days_ahead):
    """Turn parsed site assignments into (upcoming, missing) brief items.

    Labflow shows what you've finished, so late unfinished work counts as missing.
    WeBWorK's set list doesn't, so a set in its reduced-credit window stays in
    the upcoming list (due at the end of that window) with a note.
    """
    upcoming, missing = [], []
    end = now + timedelta(days=days_ahead)
    for a in parsed:
        if a.get("done") or not a["due"]:
            continue
        late = a.get("late_until")
        if a["due"] >= now:
            if a["due"] <= end:
                if a.get("opens") and a["opens"] > now:
                    a["note"] = f"Opens {a['opens'].strftime('%a %b')} {a['opens'].day}"
                upcoming.append(a)
        elif late and late >= now:
            if kind == "labflow":
                a["note"] = "Late: cut-off " + late.strftime("%a %b ") + str(late.day)
                missing.append(a)
            else:
                a["note"] = "Due date passed, reduced credit until then. Skip if you finished it"
                a["due"] = late
                upcoming.append(a)
    return upcoming, missing
