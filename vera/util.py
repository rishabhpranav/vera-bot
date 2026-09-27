"""Small helpers: formatting, dates, language, lookups."""
from __future__ import annotations

import re
from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))

SOUTH_CITIES = {"chennai", "bangalore", "bengaluru", "coimbatore", "kochi", "madurai", "mysore"}


# ---------------------------------------------------------------- numbers
def inr(v) -> str:
    """₹4999 -> ₹4,999 (Indian grouping)."""
    try:
        n = int(round(float(v)))
    except (TypeError, ValueError):
        return f"₹{v}"
    s = str(abs(n))
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        s = ",".join(parts + [tail])
    return f"₹{s}"


def num(v) -> str:
    try:
        n = float(v)
    except (TypeError, ValueError):
        return str(v)
    if n.is_integer():
        n = int(n)
        s = str(abs(n))
        if len(s) > 3:
            head, tail = s[:-3], s[-3:]
            parts = []
            while len(head) > 2:
                parts.insert(0, head[-2:])
                head = head[:-2]
            if head:
                parts.insert(0, head)
            s = ",".join(parts + [tail])
        return ("-" if n < 0 else "") + s
    return f"{n:g}"


def pct(v, signed=False) -> str:
    """0.18 -> '18%'; signed -> '+18%'."""
    try:
        x = float(v)
    except (TypeError, ValueError):
        return str(v)
    if abs(x) <= 1.5:  # fraction
        x *= 100
    s = f"{abs(x):.0f}%" if abs(x) >= 1 else f"{abs(x):.1f}%"
    if signed:
        return ("+" if x >= 0 else "-") + s
    return s


def rate(v) -> str:
    """0.021 -> '2.1%'."""
    try:
        return f"{float(v) * 100:.1f}%"
    except (TypeError, ValueError):
        return str(v)


# ---------------------------------------------------------------- dates
def parse_dt(s) -> datetime | None:
    if not s or not isinstance(s, str):
        return None
    try:
        s2 = s.strip().replace("Z", "+00:00")
        if len(s2) == 10:
            return datetime.fromisoformat(s2).replace(tzinfo=IST)
        dt = datetime.fromisoformat(s2)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def nice_date(s, with_dow=False) -> str:
    dt = parse_dt(s)
    if not dt:
        return str(s)
    dt = dt.astimezone(IST)
    out = f"{dt.day} {dt.strftime('%b')}"
    if with_dow:
        out = f"{dt.strftime('%a')} {out}"
    return out


def nice_time(s) -> str:
    dt = parse_dt(s)
    if not dt:
        return ""
    dt = dt.astimezone(IST)
    h = dt.hour % 12 or 12
    m = f":{dt.minute:02d}" if dt.minute else ""
    return f"{h}{m}{'am' if dt.hour < 12 else 'pm'}"


def days_between(a: datetime | None, b: datetime | None) -> int | None:
    if not a or not b:
        return None
    return (b.date() - a.date()).days


# ---------------------------------------------------------------- language
HINDI_TOKENS = {
    "hai", "hain", "kya", "nahi", "nahin", "haan", "han", "karo", "kar", "karna", "mujhe", "aap",
    "aapka", "aapki", "chahiye", "theek", "thik", "acha", "accha", "achha", "bhai", "ji", "bhejo",
    "bhej", "kaise", "kab", "kitna", "kitne", "abhi", "baad", "mein", "main", "hum", "humara",
    "mera", "meri", "sab", "jaldi", "chalo", "chalega", "dekho", "batao", "bataiye", "samjha",
    "shukriya", "dhanyavaad", "haanji", "ho", "gaya", "raha", "rahi", "wala", "wali", "koi",
}


def is_hinglish_text(text: str) -> bool:
    if not text:
        return False
    if re.search(r"[ऀ-ॿ]", text):
        return True
    words = re.findall(r"[a-zA-Z]+", text.lower())
    if not words:
        return False
    hits = sum(1 for w in words if w in HINDI_TOKENS)
    return hits >= 2 or (hits >= 1 and len(words) <= 4)


def merchant_prefers_hinglish(merchant: dict) -> bool:
    ident = (merchant or {}).get("identity", {}) or {}
    langs = [str(x).lower() for x in ident.get("languages", []) or []]
    if "hi" not in langs:
        return False
    city = str(ident.get("city", "")).lower()
    return city not in SOUTH_CITIES


def customer_lang(customer: dict) -> str:
    """'hi' (mostly Hindi), 'hinglish', or 'en'."""
    pref = str(((customer or {}).get("identity", {}) or {}).get("language_pref", "")).lower()
    if pref in ("hi", "hindi"):
        return "hi"
    if "hi" in pref.split("-") or pref.startswith("hi") or "hindi" in pref:
        return "hinglish"
    return "en"


# ---------------------------------------------------------------- lookups
def humanize(slug) -> str:
    return str(slug).replace("_", " ").strip()


def first_sentence(text: str) -> str:
    if not text:
        return ""
    m = re.match(r"(.+?[.!?])(\s|$)", text.strip())
    return (m.group(1) if m else text).strip()


def strip_trailing_period(s: str) -> str:
    return s.rstrip().rstrip(".")
