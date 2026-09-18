"""Spoken summaries for Xyrus v2 (SPEC §4.10). Pure functions: the store, the time (`now`) and the config are
passed in, so everything is headless-testable with a fake clock and a temp store (G8: this module never reads
the clock itself).

Lifted verbatim from v1 D:\\arc\\calendar_store.py (D14): item_phrase, join_and, count_phrase, describe_range,
describe_next, describe_todos, briefing - the only change is that v1's literal "sir" is the configured honorific
(config "address_as"; blank drops it). v1 wording and time format ("3 pm") win where §3.9 differs.
Added: describe_notes, upcoming / next_after_item / next_after ("and after that"), startup_summary,
good_night, greeting, describe_memories, next_event_label, and the small config/number helpers that
reminders.py and the command modules share.
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Iterable

from xyrus import dates as W

MAX_SPOKEN = 12                     # items read aloud before "...and N more"
MAX_NOTES_SPOKEN = 5
NUM_WORDS = {0: "zero", 1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven",
             8: "eight", 9: "nine", 10: "ten", 11: "eleven", 12: "twelve"}


# ---------------------------------------------------------------- config / wording helpers -- #
def cfg_get(cfg: Any, key: str, default: Any = None) -> Any:
    """Read a (dotted) config key from a xyrus Config, a plain dict or None."""
    if cfg is None:
        return default
    value = None
    try:
        value = cfg.get(key, None)
    except Exception:
        value = None
    if value is None and isinstance(cfg, dict) and "." in key:
        cur: Any = cfg
        for part in key.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        value = cur
    return default if value is None else value


def address(cfg: Any) -> str:
    """The honorific ("sir"), or "" when the user blanked it."""
    return str(cfg_get(cfg, "address_as", "sir")).strip()


def sir(cfg: Any) -> str:
    """", sir" (the configured honorific) or ""."""
    a = address(cfg)
    return f", {a}" if a else ""


def cap(s: str) -> str:
    return s[:1].upper() + s[1:]


def number_word(n: int) -> str:
    return NUM_WORDS.get(n, str(n))


def plural(n: int, word: str) -> str:
    return f"{n} {word}{'s' if n != 1 else ''}"


# ---------------------------------------------------------------- v1 speech (verbatim) ------ #
def item_phrase(it) -> str:
    return it["title"] + (f" at {W.fmt_time(it['time'])}" if it["time"] else "")


def join_and(parts) -> str:
    parts = list(parts)
    if len(parts) <= 1:
        return "".join(parts)
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def count_phrase(n_ev, n_task) -> str:
    parts = []
    if n_ev:
        parts.append(f"{n_ev} event{'s' if n_ev != 1 else ''}")
    if n_task:
        parts.append(f"{n_task} to-do{'s' if n_task != 1 else ''}")
    return join_and(parts)


def describe_range(store, start, end, label, now, cfg=None):
    """Spoken answer to 'what do I have <label>'. Returns (text, overflowed) - overflowed means
    not everything was read out, so the caller should show the Calendar tab."""
    s = sir(cfg)
    today = now.date()
    by_day = {}
    for d, it in store.items_between(start, end, include_done=False):
        by_day.setdefault(d, []).append(it)
    notes = store.notes_between(start, end)
    n_ev = sum(1 for items in by_day.values() for i in items if i["kind"] == "event")
    n_task = sum(1 for items in by_day.values() for i in items if i["kind"] == "task")
    parts, overflow = [], False

    if not by_day:
        parts.append(f"You have nothing planned {label}{s}.")
        nxt = store.next_item(dt.datetime.combine(end, dt.time(23, 59)))
        if nxt:
            d, it = nxt
            parts.append(f"The next thing is {item_phrase(it)}, {W.day_name(d, today)}.")
    else:
        head = f"{label[:1].upper() + label[1:]} you have {count_phrase(n_ev, n_task)}{s}"
        spoken = 0
        if start == end:
            phrases = [item_phrase(i) for d in sorted(by_day) for i in by_day[d]]
            if len(phrases) > MAX_SPOKEN:
                phrases, overflow = phrases[:MAX_SPOKEN] + [f"{len(phrases) - MAX_SPOKEN} more"], True
            parts.append(f"{head}: {join_and(phrases)}.")
        else:
            parts.append(head + ".")
            for d in sorted(by_day):
                room = MAX_SPOKEN - spoken
                if room <= 0:
                    overflow = True
                    break
                phrases = [item_phrase(i) for i in by_day[d]][:room]
                spoken += len(phrases)
                name = W.day_name(d, today)
                parts.append(f"{name[:1].upper() + name[1:]}: {join_and(phrases)}.")
            left = n_ev + n_task - spoken
            if left > 0:
                overflow = True
                parts.append(f"And {left} more. They're on the Calendar tab.")

    if start <= today <= end:
        overdue = store.overdue_tasks(today)
        if overdue:
            names = join_and(t["title"] for t in overdue[:3]) + (" and more" if len(overdue) > 3 else "")
            parts.append(f"You also have {len(overdue)} overdue to-do{'s' if len(overdue) != 1 else ''}: {names}.")
    if notes:
        if start == end and len(notes) <= 3:
            parts.append("Notes: " + "; ".join(n["text"] for n in notes) + ".")
        else:
            parts.append(f"Plus {len(notes)} note{'s' if len(notes) != 1 else ''}.")
    return " ".join(parts), overflow


def describe_next(store, now, cfg=None) -> str:
    nxt = store.next_item(now)
    if not nxt:
        return f"Nothing coming up{sir(cfg)}. Your calendar is clear."
    d, it = nxt
    return f"Next up: {item_phrase(it)}, {W.day_name(d, now.date())}."


def describe_todos(store, now, cfg=None) -> str:
    today = now.date()
    tasks = store.open_tasks()
    if not tasks:
        return f"Your to-do list is empty{sir(cfg)}."
    overdue = [t for t in tasks if t["date"] < today.isoformat()]
    due_today = [t for t in tasks if t["date"] == today.isoformat()]
    later = [t for t in tasks if t["date"] > today.isoformat()]
    parts = [f"You have {len(tasks)} to-do{'s' if len(tasks) != 1 else ''}{sir(cfg)}."]
    if overdue:
        parts.append("Overdue: " + join_and(item_phrase(t) for t in overdue[:5]) + ".")
    if due_today:
        parts.append("Today: " + join_and(item_phrase(t) for t in due_today[:6]) + ".")
    if later:
        parts.append("Later: " + join_and(
            f"{t['title']} {W.day_label(dt.date.fromisoformat(t['date']), today)}" for t in later[:5]) + ".")
    return " ".join(parts)


def briefing(store, now, cfg=None) -> str:
    greet = "Good morning" if now.hour < 12 else "Good afternoon" if now.hour < 17 else "Good evening"
    text, _ = describe_range(store, now.date(), now.date(), "today", now, cfg)
    s = sir(cfg)
    return f"{greet}{s}. " + (text.replace(s, "", 1) if s else text)


# ---------------------------------------------------------------- v2 additions -------------- #
def describe_notes(store, start, end, now, cfg=None, label: str | None = None):
    """'Two notes for today, sir. One: buy milk. Two: call the bank.' / 'No notes for tomorrow, sir.'
    Max 5 read, then 'And three more in the calendar.' Returns (text, overflowed)."""
    s = sir(cfg)
    today = now.date()
    if start == end:
        where = f"for {W.day_name(start, today)}"
    elif label and label.startswith(("in ", "for ")):
        where = label
    else:
        where = f"for {label or 'then'}"
    notes = store.notes_between(start, end)
    if not notes:
        return f"No notes {where}{s}.", False
    if len(notes) == 1:
        return f"One note {where}{s}: {notes[0]['text']}.", False
    parts = [f"{cap(number_word(len(notes)))} notes {where}{s}."]
    for i, n in enumerate(notes[:MAX_NOTES_SPOKEN], 1):
        parts.append(f"{cap(number_word(i))}: {n['text']}.")
    rest = len(notes) - MAX_NOTES_SPOKEN
    if rest > 0:
        parts.append(f"And {number_word(rest)} more in the calendar.")
    return " ".join(parts), rest > 0


def upcoming(store, now, days: int = 60) -> list[tuple[dt.date, dict]]:
    """Every not-done occurrence after `now` in order - the same rule as store.next_item (timed items
    later today, all-day items from tomorrow)."""
    today = now.date()
    out = []
    for d, it in store.items_between(today, today + dt.timedelta(days=days), include_done=False):
        if it["time"]:
            if dt.datetime.combine(d, dt.time.fromisoformat(it["time"])) > now:
                out.append((d, it))
        elif d > today:
            out.append((d, it))
    return out


def occ_start(d: dt.date, it: dict) -> dt.datetime:
    """Start of an occurrence (all-day items count from midnight)."""
    return dt.datetime.combine(d, dt.time.fromisoformat(it["time"]) if it["time"] else dt.time.min)


def next_after_item(store, now, after: dt.datetime, exclude: Iterable[tuple[str, str]] = ()):
    """(date, item) of the first upcoming occurrence starting at/after `after` that isn't in `exclude`
    (a set of (item id, occurrence iso date) already announced), or None."""
    skip = set(exclude)
    for d, it in upcoming(store, now):
        if (it["id"], d.isoformat()) in skip:
            continue
        if occ_start(d, it) >= after:
            return d, it
    return None


def next_after(now, store, after: dt.datetime, cfg=None, exclude: Iterable[tuple[str, str]] = ()) -> str:
    """The 'and after that' follow-up: 'Then Gym at 7 pm, today.' / 'Nothing after that, sir.'"""
    nxt = next_after_item(store, now, after, exclude)
    if not nxt:
        return f"Nothing after that{sir(cfg)}."
    d, it = nxt
    return f"Then {item_phrase(it)}, {W.day_name(d, now.date())}."


def part_of_day(now) -> str:
    return "morning" if 5 <= now.hour < 12 else "afternoon" if 12 <= now.hour < 17 else "evening"


def greeting(now, cfg=None) -> str:
    """'Good evening, Refath.' / 'Good evening, sir.' (the name only in greetings). Before 5 AM it's 'Hello' -
    3 AM is neither evening nor really morning (same rule as persona.greeting)."""
    name = str(cfg_get(cfg, "user_name", "") or "").strip() or address(cfg)
    word = "Hello" if now.hour < 5 else f"Good {part_of_day(now)}"
    return f"{word}, {name}." if name else f"{word}."


def _today_line(store, now) -> str:
    today = now.date()
    items = store.items_between(today, today, include_done=False)
    if not items:
        return "Nothing on the calendar today."
    n = len(items)
    things = f"{n} thing{'s' if n != 1 else ''} today"
    nxt = store.next_item(now)
    if nxt and nxt[0] == today:
        return f"You have {things}; next is {item_phrase(nxt[1])}."
    return f"You have {things}: {join_and(item_phrase(i) for _, i in items[:3])}."


def missed_phrase(alerts, now, cfg=None, max_named: int = 2) -> str:
    """One sentence for the 'late' alerts: 'While I was away, sir: Dentist at 3 pm and Standup at 9:30 am.'"""
    late = [a for a in alerts if getattr(a, "kind", "") == "late"]
    if not late:
        return ""
    today = now.date()
    if len(late) == 1:
        a = late[0]
        it = a.item
        when = f" at {W.fmt_time(it['time'])}" if it["time"] else ""
        return f"While I was away{sir(cfg)}: {it['title']}{when}, {W.day_name(a.occ, today)}."
    named = [item_phrase(a.item) + ("" if a.occ == today else " " + W.day_name(a.occ, today)) for a in late[:max_named]]
    rest = len(late) - len(named)
    if rest > 0:
        named.append(f"{rest} more")
    return f"While I was away{sir(cfg)}: {join_and(named)}."


def startup_summary(now, store, missed, cfg=None) -> str:
    """'Good evening, Refath. Xyrus online. You have 2 things today; next is Gym at 7 pm.' Missed ('late')
    alerts come first after the greeting (max 2 named). G6: 'Xyrus' is whitelisted in this template only."""
    parts = [greeting(now, cfg), "Xyrus online."]
    m = missed_phrase(missed or [], now, cfg)
    if m:
        parts.append(m)
    parts.append(_today_line(store, now))
    return " ".join(parts)


def good_night(now, store, cfg=None) -> str:
    """'Good night, sir. Tomorrow starts with Standup at 9 am.' / '... Nothing scheduled tomorrow.'"""
    tomorrow = now.date() + dt.timedelta(days=1)
    items = store.items_between(tomorrow, tomorrow, include_done=False)
    head = f"Good night{sir(cfg)}."
    if not items:
        return f"{head} Nothing scheduled tomorrow."
    timed = [it for _, it in items if it["time"]]
    first = timed[0] if timed else items[0][1]
    return f"{head} Tomorrow starts with {item_phrase(first)}."


def describe_memories(mems: list[dict], total: int, offset: int = 0, cfg=None) -> str:
    """Recall, three at a time. First page: 'You asked me to remember two things. One: ... Two: ...';
    later pages continue the count ('Four: ...'). Empty: 'Nothing yet, sir. Say 'remember that' ...'."""
    if total == 0 or (not mems and offset == 0):
        return f"Nothing yet{sir(cfg)}. Say 'remember that' and I'll keep it."
    if not mems:
        return f"That's everything{sir(cfg)}."
    parts = []
    if offset == 0:
        parts.append("You asked me to remember one thing." if total == 1
                     else f"You asked me to remember {number_word(total)} things.")
        if total == 1:
            return f"{parts[0][:-1]}: {mems[0]['text']}."
    for i, m in enumerate(mems, offset + 1):
        parts.append(f"{cap(number_word(i))}: {m['text']}.")
    left = total - offset - len(mems)
    if left > 0:
        parts.append(f"There {'is' if left == 1 else 'are'} {number_word(left)} more. Say more to hear them.")
    return " ".join(parts)


def next_event_label(store, now) -> str | None:
    """Header / tray text: 'Dentist at 3 pm · in 2 h', 'Gym at 6 pm · in 25 min', 'Rent · tomorrow'."""
    nxt = store.next_item(now)
    if not nxt:
        return None
    d, it = nxt
    if not it["time"]:
        return f"{it['title']} · {W.day_name(d, now.date())}"
    mins = int((occ_start(d, it) - now).total_seconds() // 60)
    if mins < 60:
        rel = f"in {max(mins, 1)} min"
    elif d == now.date() or mins < 12 * 60:
        rel = f"in {round(mins / 60)} h"
    else:
        rel = W.day_name(d, now.date())
    return f"{item_phrase(it)} · {rel}"
