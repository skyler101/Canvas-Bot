"""Read assignments from outside sites that you open through Canvas (WeBWorK, Labflow).

These sites have no API you can use, and you only get in by clicking their link
inside Canvas. So the bot does the same thing: it opens that Canvas link in a
hidden browser using your saved Canvas login, waits for the site to load, and
reads the page.

Each site is set up in my_settings.json with the Canvas link you click to open
it (right-click the link in Canvas -> "Copy link address").
"""

import json
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
    "labflow": re.compile(r"\b(Opened|Opens|Closes|Closed)\s+\d\d/\d\d/\d{4}|\bClose:\s*\d\d/\d\d/\d{4}", re.I),
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
    s = s.replace(",", " ").replace(" at ", " ").replace(".", "")
    s = re.sub(r"(\d)\s*([AaPp][Mm])\b", lambda m: f"{m.group(1)} {m.group(2).upper()}", s)
    s = re.sub(r"\s+", " ", s).strip()
    for fmt in formats:
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=tz)
        except ValueError:
            continue
    return None


# "October 1, 2026, 11:59:00 PM", "Oct 1, 2026 at 11:59 PM" or "10/01/2026 at 11:59pm"
WW_DATE = (
    r"((?:[A-Z][a-z]+\.? \d{1,2},? \d{4}|\d{1,2}/\d{1,2}/\d{2,4}),?\s+(?:at\s+)?"
    r"\d{1,2}:\d{2}(?::\d{2})?\s*[AaPp]\.?[Mm]\.?)"
)
WW_FORMATS = [
    f"{d} {t}"
    for d in ("%B %d %Y", "%b %d %Y", "%m/%d/%Y", "%m/%d/%y")
    for t in ("%I:%M:%S %p", "%I:%M %p")
]


def parse_webwork(sets, tz):
    """[{name, text}] from the WeBWorK sets page -> list of assignments."""
    out, seen = [], set()
    for s in sets:
        name, text = s["name"], s.get("text", "")
        if not name or name in seen or name.lower().startswith("download"):
            continue
        seen.add(name)
        due = re.search(r"\b(?:Due|Closes)(?: on)?:? " + WW_DATE, text, re.I)
        reduced = re.search(r"reduced credit[^.]*?until " + WW_DATE, text, re.I)
        opens = re.search(r"(?:Will open|Opens)(?: on)?:? " + WW_DATE, text, re.I)
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
    # The "Coming Up" panel is often the only place with dates (modules can be
    # collapsed). Each entry: title line, "Open: ...", "Close: ...", "Cut-Off: ...".
    for item in parse_labflow_coming_up(lines, tz):
        match = next((o for o in out if o["title"] == item["title"] and o["due"] == item["due"]), None)
        if match:
            match["late_until"] = match["late_until"] or item["late_until"]
            match["done"] = match["done"] or item["done"]
        else:
            out.append(item)
    return out


LF_NOISE = re.compile(
    r"^(calendar_today|lock|lock_open|loop.*|\d+ of \d+ attempts? left|attempts remaining.*|arrow_forward|[*•])$", re.I
)


LF_STATUS = re.compile(r"^(check|check_?circle)?\s*(attempted|submitted|completed|graded|done)$", re.I)
LF_LAB = re.compile(r"^Lab \d+\s*:", re.I)


def parse_labflow_coming_up(lines, tz):
    """Read the Coming Up panel. Each entry is a small block of lines, e.g.:

        checkAttempted                          <- status (optional)
        Pre-Lab Quiz - Inorganic Nomenclature   <- assignment name
        Lab 5: Inorganic Nomenclature           <- which lab (may share the line above)
        loop2 of 2 attempts left / calendar_today
        Open: ... / Close: ... / Cut-Off: ...
    """
    items, block_start = [], 0
    for i, line in enumerate(lines):
        if re.match(r"Coming Up", line, re.I):
            block_start = i + 1
        mo = re.match(r"Open:\s*" + LF_DATE, line, re.I)
        if not mo:
            continue
        close = cut = None
        last = i
        for j in range(i + 1, min(i + 4, len(lines))):
            mc = re.match(r"Close:\s*" + LF_DATE, lines[j], re.I)
            mx = re.match(r"Cut-?Off:\s*" + LF_DATE, lines[j], re.I)
            if mc or mx:
                last = j
            close = close or (mc and mc.group(1))
            cut = cut or (mx and mx.group(1))
        if not close:
            continue
        # the lines between the previous entry and this "Open:" line describe this entry
        block = [l.lstrip("*• ").strip() for l in lines[max(block_start, i - 8) : i]]
        block_start = last + 1
        done = any(LF_STATUS.match(l) or re.match(r"^check\s*Attempted", l, re.I) for l in block)
        names = []
        for l in block:
            if not l or LF_NOISE.match(l) or LF_STATUS.match(l):
                continue
            l = re.sub(r"^check\s*Attempted\s*", "", l, flags=re.I)
            if LF_LAB.match(l):
                continue  # "Lab 5: ..." on its own line
            l = re.split(r"(?=Lab \d+\s*:)", l)[0].strip()  # "...NomenclatureLab 5: ..." on one line
            if l:
                names.append(l)
        if not names:
            continue
        items.append(
            {
                "title": names[-1],
                "opens": _parse_dt(mo.group(1).upper(), tz, LF_FORMATS),
                "due": _parse_dt(close.upper(), tz, LF_FORMATS),
                "late_until": _parse_dt(cut.upper(), tz, LF_FORMATS) if cut else None,
                "done": done,
            }
        )
    return items


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


def _looks_like_login(url):
    u = urlsplit(url)
    path = u.path.lower()
    return u.netloc.startswith("login.") or any(k in path for k in ("/login", "/cas/", "/idp/", "/saml"))


def _find_frame(context, hosts, ready, timeout, stop_at_login=True):
    """Wait until some page or iframe on one of `hosts` shows text matching `ready`.

    Returns (frame, None) on success, (None, "login") if stuck on a sign-in page
    (e.g. MSU NetID + Duo), or (None, "timeout").
    """
    deadline = time.time() + timeout
    clicks, next_click = 0, time.time() + 1
    login_since = None
    while time.time() < deadline:
        for page in context.pages:
            for frame in page.frames:
                if not any(h in frame.url for h in hosts):
                    continue
                try:
                    if ready.search(frame.inner_text("body", timeout=2000)):
                        return frame, None
                except Exception:
                    pass
        # A sign-in page that stays put for a few seconds needs a person
        on_login = any(_looks_like_login(pg.url) for pg in context.pages)
        login_since = (login_since or time.time()) if on_login else None
        if stop_at_login and login_since and time.time() - login_since > 6:
            return None, "login"
        # Tools like Labflow only open in a new window: press Canvas's button.
        # A first click sometimes does nothing, so press again if no window opened.
        if clicks < 6 and time.time() >= next_click:
            reached = any(any(h in f.url for h in hosts) for pg in context.pages for f in pg.frames)
            if not reached and len(context.pages) < 2 and _click_new_window(context):
                clicks += 1
                next_click = time.time() + 4
        time.sleep(0.5)
    return None, "timeout"


def clean_link(link):
    """Drop temporary login keys (key=, ltik=, user=...) so a saved link never goes stale."""
    from urllib.parse import parse_qsl, urlencode, urlunsplit

    u = urlsplit(link)
    keep = [(k, v) for k, v in parse_qsl(u.query) if k.lower() not in ("key", "ltik", "user", "effectiveuser")]
    return urlunsplit((u.scheme, u.netloc, u.path, urlencode(keep), u.fragment))


def link_goes_to_site(kind, link):
    return any(h in urlsplit(link).netloc for h in SITE_HOSTS.get(kind, ()))


EMAIL = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")


def _read(kind, frame, tz, debug_dir=None):
    text = frame.inner_text("body")
    links = frame.evaluate(WEBWORK_JS) if kind == "webwork" else []
    items = parse_webwork(links, tz) if kind == "webwork" else parse_labflow(text, tz)
    if debug_dir is not None:
        # What the bot saw, for troubleshooting (stays on your computer; emails removed)
        try:
            debug_dir.mkdir(exist_ok=True)
            u = urlsplit(frame.url)
            dump = [f"page: {u.netloc}{u.path}", f"set links found: {len(links)}" if kind == "webwork" else ""]
            for l in links[:40]:
                dump.append(f"LINK {l['name']!r}: {' '.join(l['text'].split())[:160]}")
            dump += ["", "--- page text ---", text[:20000]]
            (debug_dir / f"{kind}-seen.txt").write_text(EMAIL.sub("[email]", "\n".join(dump)), encoding="utf-8")
        except Exception:
            pass
    return items


def _cache_key(site):
    return f"{site.get('type', '').lower()}|{site.get('canvas_link', '')}"


def _load_cache(cache_file, tz):
    try:
        raw = json.loads(cache_file.read_text(encoding="utf-8"))
    except (OSError, ValueError, AttributeError):
        return {}
    out = {}
    for key, entry in raw.items():
        items = []
        for a in entry.get("items", []):
            a = dict(a)
            for f in ("due", "opens", "late_until"):
                a[f] = datetime.fromisoformat(a[f]).astimezone(tz) if a.get(f) else None
            items.append(a)
        out[key] = {"saved": entry.get("saved", ""), "items": items}
    return out


def _save_cache(cache_file, cache):
    raw = {}
    for key, entry in cache.items():
        raw[key] = {
            "saved": entry["saved"],
            "items": [
                {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in a.items()}
                for a in entry["items"]
            ],
        }
    try:
        cache_file.write_text(json.dumps(raw, indent=1), encoding="utf-8")
    except (OSError, AttributeError):
        pass


def _sign_in_window(p, site, kind, label, session_file, tz, debug_dir=None):
    """Open a visible browser so you can sign in (NetID + Duo); read the site once you're in."""
    browser = p.chromium.launch(headless=False)
    try:
        context = browser.new_context(storage_state=str(session_file))
        page = context.new_page()
        page.goto(site["canvas_link"], wait_until="domcontentloaded", timeout=60_000)
        print(
            f"\n👉 {label} needs you to sign in. A browser window just opened:\n"
            "   log in there (NetID + Duo; tick 'remember me' if offered).\n"
            "   Waiting up to 4 minutes...\n",
            file=sys.stderr,
        )
        try:
            page.bring_to_front()
        except Exception:
            pass
        frame, _ = _find_frame(context, SITE_HOSTS[kind], SITE_READY[kind], timeout=240, stop_at_login=False)
        if frame is None:
            return None
        items = _read(kind, frame, tz, debug_dir)
        context.storage_state(path=str(session_file))  # keep the sign-in for next time
        return items
    finally:
        browser.close()


def fetch_sites(sites, session_file, tz, debug_dir, show_browser=False, interactive=False, cache_file=None):
    """Open each configured site through Canvas and return ({site index: [assignments]}, [errors]).

    - If a site needs a sign-in (like WeBWorK behind MSU login) and you're at the
      computer (interactive), a visible window opens for you to log in.
    - Otherwise the last successful result is reused, with a note saying when.
    - One broken site never stops the whole brief.
    """
    from playwright.sync_api import sync_playwright

    results, errors = {}, []
    cache = _load_cache(cache_file, tz) if cache_file else {}
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
                problem = None
                try:
                    page = context.new_page()
                    page.goto(site["canvas_link"], wait_until="domcontentloaded", timeout=60_000)
                    canvas_host = urlsplit(site["canvas_link"]).netloc
                    start = urlsplit(page.url)
                    if start.netloc == canvas_host and start.path.startswith("/login"):
                        raise RuntimeError("Canvas showed its login page. Run --login again")
                    frame, reason = _find_frame(context, SITE_HOSTS[kind], SITE_READY[kind], timeout=90)
                    if frame is not None:
                        results[idx] = _read(kind, frame, tz, debug_dir)
                        context.storage_state(path=str(session_file))
                    elif reason == "login":
                        context.close()
                        context = None
                        if interactive:
                            items = _sign_in_window(p, site, kind, label, session_file, tz, debug_dir)
                            if items is not None:
                                results[idx] = items
                            else:
                                problem = f"{label} sign-in didn't finish"
                        else:
                            problem = f"{label} needs you to sign in (MSU login). It'll ask next time you run it at the computer"
                    else:
                        problem = f"couldn't read {label}. The bot ended up at: {_where(context)}"
                except Exception as e:
                    problem = str(e)
                if idx in results:
                    print(f"✓ {label}: found {len(results[idx])} assignments", file=sys.stderr)
                    cache[_cache_key(site)] = {"saved": datetime.now(tz).isoformat(), "items": results[idx]}
                else:
                    msg = f"{label} ({site.get('course', '')}): {problem}"
                    old = cache.get(_cache_key(site))
                    if old and old["items"]:
                        results[idx] = old["items"]
                        saved = datetime.fromisoformat(old["saved"]) if old["saved"] else None
                        when = f"{saved.strftime('%a %b')} {saved.day}" if saved else "earlier"
                        msg += f". Showing what it saw on {when}"
                    errors.append(msg)
                    if context is not None:
                        try:
                            debug_dir.mkdir(exist_ok=True)
                            for n, pg in enumerate(context.pages):
                                pg.screenshot(path=str(debug_dir / f"{kind}-{n}.png"), full_page=True)
                        except Exception:
                            pass
                if context is not None:
                    context.close()
        finally:
            browser.close()
    if cache_file:
        _save_cache(cache_file, cache)
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
