"""Daily picture, quote and hobby for the top of the dashboard.

- Pictures: put your own photos (mountains, hobbies, anything) in the photos/
  folder and a different one shows each day. With no photos, the dashboard
  draws a new mountain landscape every day.
- Quotes: a built-in list plus any you add in my_settings.json.
- Hobbies: list them in my_settings.json; one is featured each day.
"""

from pathlib import Path
from urllib.parse import quote as urlquote

PHOTO_TYPES = {".jpg", ".jpeg", ".png", ".webp", ".gif"}

QUOTES = [
    ("The mountains are calling and I must go.", "John Muir"),
    ("Great things are done when men and mountains meet.", "William Blake"),
    ("Climb the mountain so you can see the world, not so the world can see you.", "David McCullough Jr."),
    ("It is not the mountain we conquer, but ourselves.", "Sir Edmund Hillary"),
    ("Getting to the top is optional. Getting down is mandatory.", "Ed Viesturs"),
    ("Success is the sum of small efforts, repeated day in and day out.", "Robert Collier"),
    ("We are what we repeatedly do. Excellence, then, is not an act, but a habit.", "Will Durant"),
    ("A journey of a thousand miles begins with a single step.", "Lao Tzu"),
    ("Fall seven times, stand up eight.", "Japanese proverb"),
    ("Small deeds done are better than great deeds planned.", "Peter Marshall"),
    ("Hard choices, easy life. Easy choices, hard life.", "Jerzy Gregorek"),
    ("Education is the passport to the future, for tomorrow belongs to those who prepare for it today.", "Malcolm X"),
    ("Perseverance is not a long race; it is many short races one after the other.", "Walter Elliot"),
    ("Nothing in the world is worth having or worth doing unless it means effort, pain, difficulty.", "Theodore Roosevelt"),
    ("The best view comes after the hardest climb.", "Unknown"),
    ("The man who moves a mountain begins by carrying away small stones.", "Proverb"),
    ("Do the best you can until you know better. Then when you know better, do better.", "Maya Angelou"),
    ("Keep close to Nature's heart... and break clear away, once in a while, and climb a mountain.", "John Muir"),
]

HOBBY_EMOJI = {
    "ski": "⛷️", "snowboard": "🏂", "hik": "🥾", "climb": "🧗", "fish": "🎣", "hunt": "🦌",
    "camp": "🏕️", "bike": "🚵", "cycl": "🚴", "run": "🏃", "gym": "🏋️", "lift": "🏋️",
    "music": "🎸", "guitar": "🎸", "game": "🎮", "photo": "📷", "read": "📚", "cook": "🍳",
    "paint": "🎨", "draw": "✏️", "soccer": "⚽", "basketball": "🏀", "football": "🏈",
    "golf": "⛳", "kayak": "🛶", "raft": "🛶", "swim": "🏊", "skate": "🛹", "horse": "🐎",
}


def _day_number(now):
    return now.toordinal()


def daily_pick(now, settings, here):
    """Pick today's photo, quote, and hobby (the same all day, different each day)."""
    day = _day_number(now)

    photos = sorted(
        p for p in (Path(here) / "photos").glob("*") if p.suffix.lower() in PHOTO_TYPES
    )
    photo = None
    if photos:
        # step through photos in a shuffled-looking but repeatable order
        photo = "photos/" + urlquote(photos[(day * 7) % len(photos)].name)

    quotes = list(QUOTES)
    for q in settings.get("quotes", []):
        if isinstance(q, dict) and q.get("text"):
            quotes.append((q["text"], q.get("by", "")))
        elif isinstance(q, str) and q.strip():
            quotes.append((q.strip(), ""))
    text, by = quotes[(day * 11) % len(quotes)]

    hobbies = [h for h in settings.get("hobbies", []) if isinstance(h, str) and h.strip()]
    hobby = hobbies[day % len(hobbies)].strip() if hobbies else None
    emoji = ""
    if hobby:
        emoji = next((e for k, e in HOBBY_EMOJI.items() if k in hobby.lower()), "⭐")

    return {
        "photo": photo,
        "quote": text,
        "by": by,
        "hobby": hobby,
        "hobbyEmoji": emoji,
        "seed": day,
    }
