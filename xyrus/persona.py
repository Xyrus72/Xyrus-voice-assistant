"""Persona helpers (§3.9 persona rules, §4.4). Pure functions; config passed in; any thread.
G8: never reads the wall clock itself; callers pass `now`.
"""
from __future__ import annotations

import datetime as dt
import random
import threading
from typing import Literal, Sequence

_last_pick: dict[str, str] = {}
_pick_lock = threading.Lock()

_ONES = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve",
         "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"]
_TENS = {2: "twenty", 3: "thirty", 4: "forty", 5: "fifty", 6: "sixty", 7: "seventy", 8: "eighty", 9: "ninety"}
_ORD_SUFFIX = {1: "st", 2: "nd", 3: "rd"}


def _cfg_get(cfg, key: str, default=None):
    if cfg is None:
        return default
    try:
        return cfg.get(key, default)
    except Exception:
        return default


def sir(cfg) -> str:
    """', sir' from config.address_as, or '' when it is blank. Never the user's name (that's greetings only)."""
    a = (_cfg_get(cfg, "address_as", "sir") or "").strip()
    return f", {a}" if a else ""


def render(template: str, cfg, **kw) -> str:
    """Fill {sir} and named fields. Unknown/missing fields are left as written rather than raising."""
    if "{" not in template:
        return template
    fields = dict(kw)
    fields.setdefault("sir", sir(cfg))
    try:
        return template.format(**fields)
    except (KeyError, IndexError, ValueError):
        out = template
        for k, v in fields.items():
            out = out.replace("{" + k + "}", str(v))
        return out


def pick(key: str, options: Sequence[str], cfg, **kw) -> str:
    """Random variant, never the previous choice for this key (when there is more than one)."""
    opts = list(options)
    if not opts:
        return ""
    with _pick_lock:
        prev = _last_pick.get(key)
        choices = [o for o in opts if o != prev] or opts
        choice = random.choice(choices)
        _last_pick[key] = choice
    return render(choice, cfg, **kw)


def number_words(n: int) -> str:
    """0..999 as words ('twenty five'); larger numbers as digits."""
    n = int(n)
    if n < 0:
        return "minus " + number_words(-n)
    if n < 20:
        return _ONES[n]
    if n < 100:
        t, o = divmod(n, 10)
        return _TENS[t] + ("" if o == 0 else " " + _ONES[o])
    if n < 1000:
        h, r = divmod(n, 100)
        return _ONES[h] + " hundred" + ("" if r == 0 else " and " + number_words(r))
    return str(n)


def ordinal(n: int) -> str:
    """1 -> '1st', 20 -> '20th', 23 -> '23rd' (SAPI reads these naturally)."""
    suffix = "th" if 10 <= n % 100 <= 20 else _ORD_SUFFIX.get(n % 10, "th")
    return f"{n}{suffix}"


def speak_time(t: dt.time | dt.datetime) -> str:
    """'3 PM', '9:30 AM', 'noon', 'midnight'.
    SPEC-AMBIGUITY: §4.11 says speak_time uses dates.fmt_time, which yields lowercase '3 pm'; §4.4 and the
    §7.3 flow tests spell '3 PM'. This follows §4.4 (same numbers, SAPI reads both identically); the v1-lifted
    assistant summaries keep fmt_time's lowercase wording (D14)."""
    if isinstance(t, dt.datetime):
        t = t.time()
    h, m = t.hour, t.minute
    if (h, m) == (12, 0):
        return "noon"
    if (h, m) == (0, 0):
        return "midnight"
    ap = "AM" if h < 12 else "PM"
    h12 = h % 12 or 12
    return f"{h12} {ap}" if m == 0 else f"{h12}:{m:02d} {ap}"


def speak_duration(seconds: int) -> str:
    """'seven minutes', 'one hour and thirty minutes', 'twenty five seconds', 'one minute and ten seconds'."""
    s = max(0, int(round(seconds)))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    parts = []
    if h:
        parts.append(f"{number_words(h)} hour{'s' if h != 1 else ''}")
    if m:
        parts.append(f"{number_words(m)} minute{'s' if m != 1 else ''}")
    if sec or not parts:
        parts.append(f"{number_words(sec)} second{'s' if sec != 1 else ''}")
    if len(parts) == 1:
        return parts[0]
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def speak_left(seconds: int) -> str:
    """Timer remaining, as people say it: 'three minutes forty', 'forty seconds', 'one hour ten minutes'."""
    s = max(0, int(round(seconds)))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{number_words(h)} hour{'s' if h != 1 else ''}" + (
            f" {number_words(m)} minute{'s' if m != 1 else ''}" if m else "")
    if m:
        return f"{number_words(m)} minute{'s' if m != 1 else ''}" + (f" {number_words(sec)}" if sec else "")
    return f"{number_words(sec)} second{'s' if sec != 1 else ''}"


def _relative(delta_s: float) -> str:
    mins = int(round(delta_s / 60))
    if mins < 1:
        return "now"
    if mins > 20:
        mins = int(5 * round(mins / 5))
    h, m = divmod(mins, 60)
    if h and m:
        return f"in {number_words(h)} hour{'s' if h != 1 else ''} {number_words(m)} minutes"
    if h:
        return f"in {number_words(h)} hour{'s' if h != 1 else ''}"
    return f"in {number_words(m)} minute{'s' if m != 1 else ''}"


def speak_day(d: dt.date, today: dt.date) -> str:
    """'today', 'tomorrow', 'Friday' (within 6 days), 'Friday the 20th' (same month), 'October 3rd'."""
    delta = (d - today).days
    if delta == 0:
        return "today"
    if delta == 1:
        return "tomorrow"
    if delta == -1:
        return "yesterday"
    if 1 < delta <= 6:
        return d.strftime("%A")
    if d.year == today.year and d.month == today.month:
        return f"{d.strftime('%A')} the {ordinal(d.day)}"
    s = f"{d.strftime('%B')} {ordinal(d.day)}"
    return s if d.year == today.year else f"{s}, {d.year}"


def speak_when(start: dt.datetime, all_day: bool, now: dt.datetime, *, relative: bool = True) -> str:
    """Shortest true form: 'today', 'tomorrow at 3 PM', 'Friday the 20th', plus 'in two hours' when the
    start is less than 12 h away ('today at 3 PM, in two hours')."""
    if isinstance(start, dt.date) and not isinstance(start, dt.datetime):
        start = dt.datetime.combine(start, dt.time(0, 0))
        all_day = True
    day = speak_day(start.date(), now.date())
    if all_day:
        return day
    out = f"{day} at {speak_time(start)}"
    delta = (start - now).total_seconds()
    if relative and 0 < delta < 12 * 3600:
        out += ", " + _relative(delta)
    return out


def part_of_day(now: dt.datetime) -> Literal["morning", "afternoon", "evening"]:
    if 5 <= now.hour < 12:
        return "morning"
    if 12 <= now.hour < 17:
        return "afternoon"
    return "evening"


def greeting(now: dt.datetime, cfg) -> str:
    """'Good evening, Refath.' when a user name is set, else 'Good evening, sir.' (or 'Good evening.')."""
    name = (_cfg_get(cfg, "user_name", "") or "").strip()
    who = f", {name}" if name else sir(cfg)
    if now.hour < 5:                       # 3 AM is neither "evening" nor really "morning"
        return f"Hello{who}."
    return f"Good {part_of_day(now)}{who}."
