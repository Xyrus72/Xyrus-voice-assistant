"""Spoken date-time parser for Xyrus v2 — lifted verbatim from v1 D:\\arc\\when.py (SPEC §4.11, D14).

Additions (additive only; v1 behaviour and its 57-case table stay green, see tests/test_dates.py):
  1. `waking=(7, 23)` parameter of parse_when / infer_hour / parse_range (config.calendar.waking_hours).
  2. normalise: "add" -> "at" before a number word / noon / midnight / half / quarter (verified mishearing
     "add half past seven"); an ordinal right after "at" reads as an hour ("at eighth" -> 8).
  3. INTENT_PREFIXES gains the v2 command prefixes.
  4. parse_typed(text, now): parse_when, then parsedatetime - only for the typed quick-add preview.
The self-test table that v1 ran under __main__ now lives in tests/test_dates.py.

parse_when(text, now) -> When | None
  When(start: datetime, all_day: bool, end: date|None (ranges), repeat: str|None,
       reminder_min: int|None, title: str, consumed: list[str])

Input is lowercase spoken text (number WORDS, no punctuation) or typed text
(digits, '3pm', '15:30', 'oct 3', '10/3' also accepted).  Purpose-built over the
closed vocabulary; parsedatetime is only a fallback for typed free-form text.
"""
from __future__ import annotations
import datetime as dt
import re
from dataclasses import dataclass, field

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
WD_ABBR = {"mon": 0, "tue": 1, "tues": 1, "wed": 2, "thu": 3, "thur": 3, "thurs": 3, "fri": 4, "sat": 5, "sun": 6}
MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august",
          "september", "october", "november", "december"]
MO_ABBR = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "sept": 9,
           "oct": 10, "nov": 11, "dec": 12}
UNITS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
         "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
         "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19}
TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50}
ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7,
            "eighth": 8, "ninth": 9, "tenth": 10, "eleventh": 11, "twelfth": 12, "thirteenth": 13,
            "fourteenth": 14, "fifteenth": 15, "sixteenth": 16, "seventeenth": 17, "eighteenth": 18,
            "nineteenth": 19, "twentieth": 20, "thirtieth": 30}
DAYPART = {"morning": ("am", 9), "afternoon": ("pm", 14), "evening": ("pm", 18), "night": ("pm", 20),
           "tonight": ("pm", 20)}
WAKING = (7, 23)          # bare hours are assumed to fall inside [07:00, 23:00)

# v2 (T3 grammar.py, lead-approved): every spoken word the parser understands, for the `when` / command grammars.
# Typed-only abbreviations (mon, oct, 3pm) are left out; the recognizer drops words the model doesn't know.
DATE_WORDS = frozenset(
    set(WEEKDAYS) | set(MONTHS) | set(ORDINALS)
    | {"today", "tonight", "tomorrow", "yesterday", "day", "days", "week", "weeks", "weekend", "month",
       "this", "next", "last", "every", "daily", "weekly", "after", "the", "of", "on", "in", "morning",
       "afternoon", "evening", "night", "rest", "few", "couple", "several", "these"})
TIME_WORDS = frozenset(
    set(UNITS) | set(TENS)
    | {"at", "am", "pm", "noon", "midday", "midnight", "half", "quarter", "past", "to", "o'clock", "minute",
       "minutes", "hour", "hours", "before", "in", "a", "an"})
DEFAULT_DAY_TIME = 9      # "tomorrow" with no time -> all-day event; reminders for it fire at 09:00


@dataclass
class When:
    start: dt.datetime
    all_day: bool = True
    end: dt.date | None = None          # inclusive end date for ranges ("this week")
    repeat: str | None = None           # None | "daily" | "weekly"
    reminder_min: int | None = None     # explicit "... ten minutes before"
    title: str = ""
    consumed: list = field(default_factory=list)
    range_name: str | None = None       # "today" | "tomorrow" | "this week" ... for spoken replies


# ----------------------------------------------------------------------------- tokens
def normalise(text: str) -> list[str]:
    """Lowercase, split, turn number words into digit tokens, typed forms into spoken ones.
    'twenty five' -> '25'; '3pm' -> '3 pm'; '15:30' -> '15 30'; '3:05' -> '3 05'; 'oct 3rd' -> 'october 3'."""
    text = text.lower().replace("'", "")
    text = re.sub(r"(\d)(am|pm)\b", r"\1 \2", text)
    text = re.sub(r"(\d{1,2}):(\d{2})", r"\1 \2", text)
    text = re.sub(r"(\d+)(st|nd|rd|th)\b", r"\1th", text)      # keep the ordinal marker: '20th' stays a day
    text = re.sub(r"\b(\d{1,2})/(\d{1,2})\b", lambda m: f"{MONTHS[int(m.group(1)) - 1]} {int(m.group(2))}"
                  if 1 <= int(m.group(1)) <= 12 else m.group(0), text)
    text = re.sub(r"[^\w\s]", " ", text)
    words = text.split()
    out, i = [], 0
    while i < len(words):
        w = words[i]
        if w == "add" and _add_means_at(words, i):             # §4.11 (2): "add half past seven"
            out.append("at"); i += 1; continue
        if w in ORDINALS and out and out[-1] == "at" and ORDINALS[w] <= 12:
            out.append(str(ORDINALS[w])); i += 1; continue     # §4.11 (2): "at eighth" -> hour 8
        if w in ("a", "an") and i + 1 < len(words) and words[i + 1] in ("minute", "hour", "day", "week", "half"):
            out.append("1"); i += 1; continue
        if w in TENS:
            n = TENS[w]
            if i + 1 < len(words) and words[i + 1] in UNITS and UNITS[words[i + 1]] <= 9:
                n += UNITS[words[i + 1]]; i += 1
            elif i + 1 < len(words) and words[i + 1] in ORDINALS and ORDINALS[words[i + 1]] <= 9:
                n += ORDINALS[words[i + 1]]; i += 1
                out.append(f"{n}th"); i += 1; continue          # "twenty first" -> ordinal 21
            out.append(str(n)); i += 1; continue
        if w in UNITS:
            out.append(str(UNITS[w])); i += 1; continue
        if w in ORDINALS:
            out.append(f"{ORDINALS[w]}th"); i += 1; continue   # ordinal marker
        if w == "oclock":
            i += 1; continue
        if w in WD_ABBR:
            out.append(WEEKDAYS[WD_ABBR[w]]); i += 1; continue
        if w in MO_ABBR:
            out.append(MONTHS[MO_ABBR[w] - 1]); i += 1; continue
        out.append(w); i += 1
    return out


_AT_NEXT = {"noon", "midnight", "half", "quarter"}
_AT_AFTER = {"", "am", "pm", "past", "to", "oclock", "in", "at", "tonight", "today", "tomorrow", "this", "on"}


def _numberish(w: str) -> bool:
    return w.isdigit() or w in UNITS or w in TENS or w in ORDINALS


def _add_means_at(words: list[str], i: int) -> bool:
    """'add' heard for 'at': the next word is a number word / digit or noon|midnight|half|quarter."""
    nxt = words[i + 1] if i + 1 < len(words) else ""
    if not (_numberish(nxt) or nxt in _AT_NEXT):
        return False
    if i > 0:
        return True
    # SPEC-AMBIGUITY: §4.11 states the rule without a position. A LEADING "add" is also the add command
    # ("add two eggs to my to do list"), so there it only reads as "at" when what follows the number is
    # time-like ("add seven", "add half past seven", "add three pm", "add seven thirty").
    j = i + 2
    if nxt in TENS and j < len(words) and words[j] in UNITS:
        j += 1
    after = words[j] if j < len(words) else ""
    return after in _AT_AFTER or _numberish(after)


def _is_num(tok): return tok.isdigit()
def _is_ord(tok): return tok.endswith("th") and tok[:-2].isdigit()
def _dnum(tok): return int(tok[:-2]) if _is_ord(tok) else int(tok)


# ----------------------------------------------------------------------------- am/pm inference
def infer_hour(h: int, minute: int, ampm: str | None, day: dt.date, now: dt.datetime,
               waking: tuple[int, int] = WAKING) -> int:
    """Return a 24h hour for a spoken hour.
    Rules: explicit am/pm or day-part wins.  h >= 13 is already 24h.
    Bare hour on TODAY: the next h o'clock that is in the future, preferring one inside waking
    hours (07-23).  Bare hour on a future day: 1-6 -> pm, 7-11 -> am, 12 -> noon."""
    if h >= 13:
        return h
    if ampm == "am":
        return 0 if h == 12 else h
    if ampm == "pm":
        return 12 if h == 12 else h + 12
    cands = sorted({h % 12, h % 12 + 12})            # e.g. 7 -> [7, 19];  12 -> [0, 12]
    if day == now.date():
        future = [c for c in cands if dt.datetime.combine(day, dt.time(c, minute)) > now]
        in_waking = [c for c in future if waking[0] <= c < waking[1]]
        if in_waking:
            return in_waking[0]
        if future:
            return future[0]
        # both already passed today -> caller rolls the date to tomorrow; use the waking default
    if h == 12:
        return 12
    return h + 12 if 1 <= h <= 6 else h


# ----------------------------------------------------------------------------- parser
def parse_when(text: str, now: dt.datetime | None = None, waking=WAKING) -> When | None:
    now = now or dt.datetime.now()
    waking = tuple(waking) if waking else WAKING
    toks = normalise(text)
    used = [False] * len(toks)
    today = now.date()

    date: dt.date | None = None
    end: dt.date | None = None
    range_name = None
    hour = minute = None
    ampm = None
    daypart_default = None
    repeat = None
    reminder = None
    relative: dt.timedelta | None = None
    explicit_day = False

    def take(*idx):
        for i in idx:
            used[i] = True

    def next_weekday(wd: int, strict_next=False, this=False) -> dt.date:
        monday = today - dt.timedelta(days=today.weekday())
        d = monday + dt.timedelta(days=wd)
        if strict_next:
            return d + dt.timedelta(days=7)
        if d < today or (d == today and not this and hour is None):
            # bare "friday" on a friday: keep today only if a time follows and it is still ahead; decided later
            return d if d == today else d + dt.timedelta(days=7)
        return d

    n = len(toks)
    i = 0
    while i < n:
        t = toks[i]
        nxt = toks[i + 1] if i + 1 < n else ""
        nxt2 = toks[i + 2] if i + 2 < n else ""

        # --- recurrence
        if t == "every" and nxt in ("day", "morning", "evening", "night"):
            repeat = "daily"; take(i, i + 1)
            if nxt in DAYPART and hour is None:
                ampm, daypart_default = DAYPART[nxt]
            i += 2; continue
        if t == "every" and nxt == "week":
            repeat = "weekly"; take(i, i + 1); i += 2; continue
        if t == "every" and nxt in WEEKDAYS:
            repeat = "weekly"; date = next_weekday(WEEKDAYS.index(nxt)); explicit_day = True
            take(i, i + 1); i += 2; continue
        if t in ("daily", "weekly"):
            repeat = t; take(i); i += 1; continue

        # --- reminder offset: "<n> minutes|hours|days before"
        if _is_num(t) and nxt in ("minute", "minutes", "hour", "hours", "day", "days") and nxt2 == "before":
            mult = {"minute": 1, "minutes": 1, "hour": 60, "hours": 60, "day": 1440, "days": 1440}[nxt]
            reminder = int(t) * mult; take(i, i + 1, i + 2); i += 3; continue

        # --- relative: "in <n> minutes|hours|days|weeks"
        if t == "in" and _is_num(nxt) and nxt2 in ("minute", "minutes", "hour", "hours", "day", "days", "week", "weeks"):
            q = int(nxt)
            relative = {"minute": dt.timedelta(minutes=q), "minutes": dt.timedelta(minutes=q),
                        "hour": dt.timedelta(hours=q), "hours": dt.timedelta(hours=q),
                        "day": dt.timedelta(days=q), "days": dt.timedelta(days=q),
                        "week": dt.timedelta(weeks=q), "weeks": dt.timedelta(weeks=q)}[nxt2]
            take(i, i + 1, i + 2); i += 3; continue
        if t == "in" and nxt == "half" and nxt2 in ("1", "an", "a") and i + 3 < n and toks[i + 3] == "hour":
            relative = dt.timedelta(minutes=30); take(i, i + 1, i + 2, i + 3); i += 4; continue

        # --- day words
        if t == "today":
            date = today; range_name = "today"; take(i); i += 1; continue
        if t == "tonight":
            date = today; range_name = "tonight"; ampm, daypart_default = DAYPART["tonight"]; take(i); i += 1; continue
        if t == "tomorrow":
            date = today + dt.timedelta(days=1); range_name = "tomorrow"; take(i); i += 1; continue
        if t == "yesterday":
            date = today - dt.timedelta(days=1); range_name = "yesterday"; take(i); i += 1; continue
        if t == "day" and nxt == "after" and nxt2 == "tomorrow":
            date = today + dt.timedelta(days=2); range_name = "the day after tomorrow"
            take(i, i + 1, i + 2)
            if i > 0 and toks[i - 1] == "the": take(i - 1)
            i += 3; continue
        if t == "this" and nxt in ("morning", "afternoon", "evening"):
            date = date or today; ampm, daypart_default = DAYPART[nxt]; take(i, i + 1); i += 2; continue
        # bare day-part: "tomorrow morning", "in the evening", "at night", "morning" (title words like
        # "movie night" also land here and get 20:00 — acceptable)
        if t in ("morning", "afternoon", "evening", "night") and hour is None:
            ampm, daypart_default = DAYPART[t]; take(i)
            if i > 1 and toks[i - 1] == "the" and toks[i - 2] == "in": take(i - 1, i - 2)
            elif i > 0 and toks[i - 1] == "at" and t == "night": take(i - 1)
            i += 1; continue
        if t in ("this", "next", "last") and nxt in ("week", "weekend", "month"):
            if nxt == "week":
                monday = today - dt.timedelta(days=today.weekday())
                if t == "next": monday += dt.timedelta(days=7)
                if t == "last": monday -= dt.timedelta(days=7)
                date = max(monday, today) if t == "this" else monday
                end = monday + dt.timedelta(days=6)
            elif nxt == "weekend":
                sat = today + dt.timedelta(days=(5 - today.weekday()) % 7)
                if t == "next": sat += dt.timedelta(days=7)
                date, end = sat, sat + dt.timedelta(days=1)
            else:
                first = today.replace(day=1)
                if t == "next":
                    first = (first + dt.timedelta(days=32)).replace(day=1)
                nxt_first = (first + dt.timedelta(days=32)).replace(day=1)
                date, end = (max(first, today) if t == "this" else first), nxt_first - dt.timedelta(days=1)
            range_name = f"{t} {nxt}"; take(i, i + 1); i += 2; continue
        if t in ("this", "next") and nxt in WEEKDAYS:
            date = next_weekday(WEEKDAYS.index(nxt), strict_next=(t == "next"), this=True)
            range_name = nxt; explicit_day = "weekday" if t == "this" else True; take(i, i + 1); i += 2; continue
        if t in WEEKDAYS:
            date = next_weekday(WEEKDAYS.index(t)); range_name = t; explicit_day = "weekday"; take(i)
            if i > 0 and toks[i - 1] == "on": take(i - 1)
            i += 1
            if i < n and toks[i] in ("morning", "afternoon", "evening", "night"):
                ampm, daypart_default = DAYPART[toks[i]]; take(i); i += 1
            continue

        # --- "<month> <n>" / "<n> [of] <month>" / "the <n>" / "the <n> of <month>"
        if t in MONTHS and (_is_num(nxt) or _is_ord(nxt)):
            mo, d = MONTHS.index(t) + 1, _dnum(nxt)
            date = _month_day(today, mo, d); explicit_day = True; take(i, i + 1)
            if i > 0 and toks[i - 1] in ("on", "of"): take(i - 1)
            i += 2; continue
        if (_is_ord(t) or _is_num(t)) and (nxt in MONTHS or (nxt == "of" and nxt2 in MONTHS)):
            d = _dnum(t)
            mo = MONTHS.index(nxt if nxt in MONTHS else nxt2) + 1
            date = _month_day(today, mo, d); explicit_day = True
            take(i, i + 1); (take(i + 2) if nxt == "of" else None)
            if i > 0 and toks[i - 1] in ("the", "on"): take(i - 1)
            if i > 1 and toks[i - 1] == "the" and toks[i - 2] == "on": take(i - 2)
            i += 3 if nxt == "of" else 2; continue
        if t == "the" and (_is_ord(nxt) or (_is_num(nxt) and 1 <= int(nxt) <= 31 and nxt2 not in ("am", "pm"))) \
                and nxt2 != "of":
            d = _dnum(nxt); date = _month_day(today, None, d); explicit_day = True; take(i, i + 1)
            if i > 0 and toks[i - 1] == "on": take(i - 1)
            i += 2; continue

        # --- fixed times
        if t in ("noon", "midday"):
            hour, minute, ampm = 12, 0, "pm"; take(i)
            if i > 0 and toks[i - 1] == "at": take(i - 1)
            i += 1; continue
        if t == "midnight":
            hour, minute, ampm = 0, 0, "am"; take(i)
            if i > 0 and toks[i - 1] == "at": take(i - 1)
            i += 1; continue
        # "half past seven", "quarter past seven", "quarter to eight"
        if t in ("half", "quarter") and nxt in ("past", "to") and _is_num(nxt2):
            h = int(nxt2)
            if t == "half": hour, minute = h, 30
            elif nxt == "past": hour, minute = h, 15
            else: hour, minute = (h - 1) % 24, 45
            take(i, i + 1, i + 2)
            if i > 0 and toks[i - 1] == "at": take(i - 1)
            i += 3
            if i < n and toks[i] in ("am", "pm"): ampm = toks[i]; take(i); i += 1
            elif i < n and toks[i] == "in" and i + 2 < n and toks[i + 1] == "the" and toks[i + 2] in DAYPART:
                ampm, daypart_default = DAYPART[toks[i + 2]]; take(i, i + 1, i + 2); i += 3
            continue
        # "at <h> [<mm>] [am|pm]" or bare "<h> pm" or "<h> in the morning"
        if _is_num(t) and 0 <= int(t) <= 24 and not _is_ord(t):
            has_at = i > 0 and toks[i - 1] == "at"
            mm = None
            j = i + 1
            if j < n and _is_num(toks[j]) and int(toks[j]) < 60 and (has_at or (j + 1 < n and toks[j + 1] in ("am", "pm"))):
                mm = int(toks[j]); j += 1
            ap = None
            if j < n and toks[j] in ("am", "pm"):
                ap = toks[j]; j += 1
            elif j + 2 < n and toks[j] == "in" and toks[j + 1] == "the" and toks[j + 2] in DAYPART:
                ap = DAYPART[toks[j + 2]][0]; take(j, j + 1, j + 2); j += 3
            elif j + 1 < n and toks[j] == "at" and toks[j + 1] == "night":
                ap = "pm"; take(j, j + 1); j += 2
            if has_at or ap:
                # this is a time, not a day number
                hour, minute = int(t), (mm or 0)
                if ap: ampm = ap
                take(*range(i, j))
                if has_at: take(i - 1)
                i = j; continue
        i += 1

    # --- nothing date-ish at all?
    if date is None and end is None and hour is None and relative is None and daypart_default is None and repeat is None:
        return None

    all_day = True
    if relative is not None:
        start = now + relative
        all_day = relative >= dt.timedelta(days=1) and hour is None
        if hour is not None:
            start = start.replace(hour=infer_hour(hour, minute or 0, ampm, start.date(), now, waking), minute=minute or 0)
            all_day = False
        start = start.replace(second=0, microsecond=0)
    else:
        if date is None:
            date = today
        if hour is None and daypart_default is not None:
            hour, minute = daypart_default, 0
        if hour is not None:
            h24 = infer_hour(hour, minute or 0, ampm, date, now, waking)
            start = dt.datetime.combine(date, dt.time(h24, minute or 0))
            if start <= now and end is None:
                if not explicit_day and range_name in (None, "today"):
                    # "at 7" said at 21:30 -> tomorrow 07:00 (next occurrence)
                    date = date + dt.timedelta(days=1)
                    h24 = infer_hour(hour, minute or 0, ampm, date, now, waking)
                    start = dt.datetime.combine(date, dt.time(h24, minute or 0))
                    range_name = "tomorrow"
                elif explicit_day == "weekday" and date == today:
                    # "friday at 3 pm" said on Friday at 4 pm -> next Friday
                    date = date + dt.timedelta(days=7)
                    start = dt.datetime.combine(date, dt.time(h24, minute or 0))
                # absolute dates ("october 3") stay put — the caller may say "that's in the past"
            all_day = False
        else:
            start = dt.datetime.combine(date, dt.time(DEFAULT_DAY_TIME, 0))
            if repeat is None and end is None and date < today and not explicit_day:
                pass
        if end is None and date == today and range_name is None:
            range_name = "today"

    title_words = [w for w, u in zip(toks, used) if not u]
    return When(start=start, all_day=all_day, end=end, repeat=repeat, reminder_min=reminder,
                title=_clean_title(title_words), consumed=[w for w, u in zip(toks, used) if u],
                range_name=range_name)


def _month_day(today: dt.date, month: int | None, day: int) -> dt.date:
    """Next occurrence of day-of-month (this month if still ahead, else next), or of month+day (this year
    if still ahead, else next year).  Clamps invalid days (Feb 30) to the month's last day."""
    def mk(y, m, d):
        while True:
            try:
                return dt.date(y, m, d)
            except ValueError:
                d -= 1
    if month is None:
        cand = mk(today.year, today.month, day)
        if cand < today:
            y, m = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
            cand = mk(y, m, day)
        return cand
    cand = mk(today.year, month, day)
    if cand < today:
        cand = mk(today.year + 1, month, day)
    return cand


# Command prefixes are stripped as whole phrases (longest first) so nouns like "meeting" survive as titles.
INTENT_PREFIXES = sorted((
    "add an event", "add a event", "add event", "new event", "create an event", "create event", "schedule an event",
    "schedule a", "schedule", "put on my calendar", "add to my calendar", "add an appointment", "add appointment",
    "add a meeting", "add meeting", "set a reminder to", "set a reminder", "set reminder", "remind me to",
    "remind me that", "remind me about", "remind me", "reminder to", "reminder", "add a note that", "add a note",
    "add note", "note that", "note", "make a note", "event",
    # §4.11 (3) v2 additions
    "add in event", "add and event", "remind me again", "take a note", "note for", "add a note for",
    "add a to do", "add a task", "new to do", "to do", "on my to do list", "to my to do list",
), key=len, reverse=True)
LEAD_FILLER = ("to", "that", "at", "for", "in", "of", "and", "please", "about", "with", "on")   # articles are kept: "the rent is due"
TRAIL_FILLER = ("at", "on", "in", "for", "the", "to", "please", "and", "of", "a", "an", "with", "that")


def _clean_title(words: list[str]) -> str:
    text = " ".join(w for w in words if w != "[unk]")
    for p in INTENT_PREFIXES:
        if text == p or text.startswith(p + " "):
            text = text[len(p):].strip(); break
    words = text.split()
    while words and words[0] in LEAD_FILLER:
        words.pop(0)
    while words and words[-1] in TRAIL_FILLER:
        words.pop()
    return " ".join(words).strip()


# ----------------------------------------------------------------------------- spoken rendering
def say_when(w: When, now: dt.datetime | None = None) -> str:
    """'tomorrow at 3 pm', 'Friday at half past 7 in the evening', 'October 3'."""
    now = now or dt.datetime.now()
    d = w.start.date()
    delta = (d - now.date()).days
    if delta == 0: day = "today"
    elif delta == 1: day = "tomorrow"
    elif 1 < delta < 7: day = d.strftime("%A")
    elif d.year == now.year: day = d.strftime("%A, %B %d").replace(" 0", " ")
    else: day = d.strftime("%B %d %Y").replace(" 0", " ")
    if w.all_day:
        return day
    h, m = w.start.hour, w.start.minute
    ap = "am" if h < 12 else "pm"
    h12 = h % 12 or 12
    t = f"{h12} {ap}" if m == 0 else f"{h12}:{m:02d} {ap}"
    if h == 12 and m == 0: t = "noon"
    if h == 0 and m == 0: t = "midnight"
    return f"{day} at {t}"


# ----------------------------------------------------------------------------- ranges & labels (v1 calendar)
FEW = {"few": 3, "couple": 2, "several": 4}
DATE_TOKENS = ({"today", "tonight", "tomorrow", "yesterday", "week", "weekend", "month", "every", "daily", "weekly"}
               | set(WEEKDAYS) | set(MONTHS))


def fmt_time(hhmm) -> str:
    """'15:00' -> '3 pm', '09:30' -> '9:30 am', '12:00' -> 'noon'."""
    if not hhmm:
        return ""
    h, m = map(int, str(hhmm).split(":")[:2])
    if (h, m) == (12, 0):
        return "noon"
    if (h, m) == (0, 0):
        return "midnight"
    ap = "am" if h < 12 else "pm"
    h12 = h % 12 or 12
    return f"{h12} {ap}" if m == 0 else f"{h12}:{m:02d} {ap}"


def day_name(d, today) -> str:
    """'today' / 'tomorrow' / 'yesterday' / 'Friday' (within a week) / 'Saturday, October 3'."""
    delta = (d - today).days
    if delta == 0:
        return "today"
    if delta == 1:
        return "tomorrow"
    if delta == -1:
        return "yesterday"
    if 1 < delta < 7:
        return d.strftime("%A")
    s = d.strftime("%A, %B %d").replace(" 0", " ")
    return s if d.year == today.year else s + d.strftime(" %Y")


def day_label(d, today) -> str:
    """day_name as it follows a verb: 'today', 'tomorrow', 'on Friday', 'on Saturday, October 3'."""
    n = day_name(d, today)
    return n if n in ("today", "tomorrow", "yesterday") else "on " + n


def mentions_date(text) -> bool:
    """Does the text name a day? (A quick-add without one goes on the selected day.)"""
    toks = normalise(text)
    for i, t in enumerate(toks):
        if t in DATE_TOKENS or _is_ord(t):
            return True
        if t == "in" and i + 2 < len(toks) and _is_num(toks[i + 1]) and toks[i + 2] in ("day", "days", "week", "weeks"):
            return True
        if (t == "the" and i + 1 < len(toks) and _is_num(toks[i + 1]) and 1 <= int(toks[i + 1]) <= 31
                and not (i + 2 < len(toks) and toks[i + 2] in ("am", "pm"))):
            return True
    return False


def parse_range(text, now=None, waking=WAKING):
    """The date range a schedule question is about -> (start, end, label) or None.
    'this week', 'next three days', 'these 3 days', 'the next 10 days', 'next two weeks', 'a week',
    'rest of the week', 'tomorrow', 'on friday', 'on the twentieth', 'this weekend', 'this month'.
    label reads after 'you have nothing planned ...': 'today', 'on Friday', 'in the next 3 days'."""
    now = now or dt.datetime.now()
    today = now.date()
    toks = normalise(text)
    n = len(toks)
    for i, t in enumerate(toks):
        if t == "rest" and "week" in toks[i + 1:i + 4]:
            return today, today + dt.timedelta(days=6 - today.weekday()), "for the rest of the week"
        if t == "rest" and "month" in toks[i + 1:i + 4]:
            last = (today.replace(day=1) + dt.timedelta(days=32)).replace(day=1) - dt.timedelta(days=1)
            return today, last, "for the rest of the month"
        count, j = None, i + 1
        if _is_num(t):
            count = int(t)
        elif t in FEW:
            count = FEW[t]
            if j < n and toks[j] == "of":
                j += 1
        if count is None or count <= 0 or j >= n or toks[j] not in ("day", "days", "week", "weeks"):
            continue
        if i > 0 and toks[i - 1] == "in":
            continue                                    # "in three days" is one day, handled below
        weeks = toks[j].startswith("week")
        days = count * (7 if weeks else 1)
        if days == 1:
            return today, today, "today"
        unit = "week" if weeks else "day"
        label = f"in the next {count} {unit}s" if count > 1 else f"in the next {unit}"
        return today, today + dt.timedelta(days=days - 1), label
    if "weekend" in toks and not ({"this", "next", "last"} & set(toks)):
        text = "this weekend"                          # "at the weekend" / "on the weekend"
    w = parse_when(text, now, waking)
    if w is None:
        return None
    start = w.start.date()
    if w.end is not None:
        return start, w.end, w.range_name or "then"
    return start, start, day_label(start, today)


# ----------------------------------------------------------------------------- typed quick-add (v2, §4.11 (4))
def parse_typed(text, now=None, waking=WAKING):
    """Typed quick-add preview only (never speech, never saves without the preview): parse_when first,
    then parsedatetime for free-form typed text it doesn't know. Returns When | None."""
    now = now or dt.datetime.now()
    w = parse_when(text, now, waking)
    if w is not None or not text or not text.strip():
        return w
    try:
        import parsedatetime                      # optional (§8.1); absent -> no fallback
    except ImportError:
        return None
    try:
        found = parsedatetime.Calendar().nlp(text, sourceTime=now)
    except Exception:
        return None
    if not found:
        return None
    start, flags, s, e, _matched = found[0]
    if not flags:
        return None
    all_day = flags == 1                          # 1 = date only, 2 = time only, 3 = date and time
    if all_day:
        start = dt.datetime.combine(start.date(), dt.time(DEFAULT_DAY_TIME, 0))
    start = start.replace(second=0, microsecond=0)
    rest = normalise(text[:s] + " " + text[e:])
    return When(start=start, all_day=all_day, title=_clean_title(rest),
                consumed=normalise(text[s:e]), range_name=None)
