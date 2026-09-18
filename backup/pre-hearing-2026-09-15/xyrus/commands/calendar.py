"""Calendar, reminders, notes and to-dos by voice (SPEC §3.9, §5.5). Handlers run on T-engine.

D14: v1 D:\\arc\\arc.py is the verified calendar behaviour - its vocabulary (ADD_START, DONE_START, QUERY_PHRASES,
...), its intent/title helpers (cal_intent, add_kind, clean_cal_title, align_intent_word) and its reply wording
("I'll remind you today at 5 pm: Call mom.", "Next up: ...", "Done: Buy milk. Nice work, sir.") are lifted here and
win where §3.9 differs. v2 adds: the slot-filling dialogue for whatever is missing, delete/clear confirmations,
duplicate / in-the-past guards, the 15 s "undo that" follow-up, "and after that", notes read-back, and the
alert follow-up answers (dismiss / snooze / remind me again).

Routing: every add / tick-off / delete is a keyword matcher (like v1's cal_intent) scored MATCH_ADD, above any
registered pattern, so the handler always receives the full-vocabulary re-decode (`free_text`, §5.3) and parses
title + date from the WHOLE utterance with dates.parse_when (a free slot in the middle of a pattern, like
"remind me <when> to <text>", can't be recovered from the re-decode). The §3.9 phrases are registered as
patterns too, for the Commands tab and the grammar.
"""
from __future__ import annotations

import datetime as dt
import difflib
import logging
import weakref

from xyrus import assistant as A
from xyrus import dates as W
from xyrus import normalize as N
from xyrus import persona as P
from xyrus import reminders as R
from xyrus import replies as RP
from xyrus.parsing import DURATION_UNITS, NUMBER_WORDS
from xyrus.registry import command

log = logging.getLogger("xyrus.commands.calendar")

SECTION = "Calendar & reminders"
MATCH_ADD = 8          # > the most literal words of any pattern: keyword adds always win and get free_text
MATCH_TODOS = 3
MATCH_QUERY = 2        # v1 query/next phrases found anywhere - any exact pattern beats them
LOW = -10              # follow-up words (okay, thanks, stop, more) lose ties with small talk in full scope
ADDED_FOLLOW_UP_S = 15
RECENT_NOTE_S = 60

# Follow-up whitelists (§5.5): command names or pattern texts. The engine opens the alert / timer ones.
FOLLOW_UP_ADDED = ("undo_add", "thanks")
FOLLOW_UP_NEXT = ("and_after_that", "thanks")
FOLLOW_UP_DAY = ("hear_notes", "and_after_that", "thanks")
ALERT_FOLLOW_UP = ("dismiss", "snooze", "remind_again", "whats_next", "and_after_that")
TIMER_FOLLOW_UP = ("dismiss", "snooze")

# ----------------------------------------------------------------------------- v1 vocabulary (arc.py)
UNDO_PHRASES = {"undo", "undo that", "scratch that", "delete that", "remove that"}
ADD_START = ("add", "put", "remind", "schedule", "note", "make a note", "take a note", "write down",
             "set a reminder", "create", "new event", "new task",
             # v2 (§3.9 phrases)
             "new to do", "new meeting", "new appointment", "new reminder", "new note", "reminder")
DONE_START = ("mark", "tick off", "check off", "cross off", "complete", "i finished", "i have finished",
              "i did", "i am done with", "done with", "finished")
DELETE_START = ("delete", "remove")
NEXT_PHRASES = ("what is next", "whats next", "next up", "what is my next", "whats my next",
                # v2
                "next event", "when is my next", "anything coming up")
# SPEC-AMBIGUITY: v1 read "what's coming up" as what's-next; §3.9 lists it under Agenda ("next 7 days"). Spec wins
# for this phrase (it still answers, with the week ahead instead of one item).
TODO_PHRASES = ("to do list", "my to do", "my to dos", "my tasks", "my todo", "todo list")
QUERY_PHRASES = ("what do i have", "what have i got", "what is on my", "whats on my", "on my calendar",
                 "my schedule", "my calendar", "my agenda", "my day", "what is planned", "anything planned",
                 "do i have anything", "am i busy", "am i free", "what is happening", "whats happening",
                 "what is coming up", "whats coming up")
LIST_PHRASES = ("to my to do list", "on my to do list", "in my to do list", "to the to do list", "to my to do",
                "to my list", "on my list", "to the list", "to my calendar", "on my calendar", "in my calendar",
                "to the calendar", "on the calendar", "to my schedule", "on my schedule")
TITLE_PREFIXES = sorted((
    "add a to do", "add to do", "add a task", "add task", "add an event", "add event", "add an appointment",
    "add a meeting", "add a note", "add note", "make a note", "take a note", "write down", "set a reminder to",
    "set a reminder", "remind me to", "remind me about", "remind me", "note that", "note", "put", "add",
    "create", "schedule", "new event", "new task", "a to do", "to do", "task",
    # v2
    "new to do", "new meeting", "new appointment", "new reminder", "new note", "reminder to", "reminder",
    "create an event", "create a", "add a reminder"), key=len, reverse=True)
DONE_SUFFIXES = (" off my list", " from my list", " as completed", " as complete", " as done", " is done",
                 " complete", " done", " off")
CAL_KEYWORDS = ("event", "meeting", "appointment", "reminder", "task", "to do", "note", "calendar", "schedule")
KIND_WORDS = ("event", "events", "appointment", "meeting", "reminder")
_DUR_WORDS = set(NUMBER_WORDS) | set(DURATION_UNITS) | {"and"}
ORDINAL_WORDS = ("First", "Second", "Third", "Fourth", "Fifth")
_PICK = {"first": 0, "one": 0, "1": 0, "second": 1, "two": 1, "2": 1, "third": 2, "three": 2, "3": 2,
         "fourth": 3, "four": 3, "4": 3, "fifth": 4, "five": 4, "5": 4}


def phrase_in(text: str, phrase: str) -> bool:
    return f" {phrase} " in f" {text} "


def _norm_cmd(text: str) -> str:
    return " ".join(text.lower().replace("'", "").replace("-", " ").split())


def _timerish(words: list[str]) -> bool:
    """'add five minutes (to the timer)' belongs to the timer, not the calendar."""
    if "timer" in words or "timers" in words:
        return True
    rest = [w for w in words[1:] if w not in ("to", "the", "my", "on", "more", "unk", "[unk]")]
    return bool(rest) and all(w in _DUR_WORDS or w.isdigit() for w in rest) and any(w in DURATION_UNITS for w in rest)


def cal_intent(rest: str):
    """v1: which calendar command this is - add / done / delete / query / next / todos / undo, or None."""
    r = _norm_cmd(rest)
    if not r:
        return None
    words = r.split()
    if r in UNDO_PHRASES:
        return "undo"
    if any(r == p or r.startswith(p + " ") for p in ADD_START) and not r.startswith("remind me again"):
        # v2 guards so the keyword add doesn't steal other areas' phrases
        if words[0] == "put" and not any(phrase_in(r, p) for p in LIST_PHRASES + ("calendar",)):
            return None
        if words[0] == "create" and not any(phrase_in(r, k) for k in CAL_KEYWORDS):
            return None
        if words[0] == "add" and _timerish(words):
            return None
        return "add"
    if (any(r.startswith(p + " ") for p in DONE_START) or (r.endswith(" is done") and len(words) > 2)) \
            and not r.endswith("not done"):
        return "done"
    if any(r.startswith(p + " ") for p in DELETE_START) and r.split()[1:] != ["that"] \
            and not ({"note", "notes"} & set(words)):
        return "delete"
    if words[0] == "cancel" and len(words) >= 3 and words[-1] in KIND_WORDS:
        return "delete"
    if any(phrase_in(r, p) for p in NEXT_PHRASES):
        return "next"
    if any(phrase_in(r, p) for p in TODO_PHRASES):
        return "todos"
    if any(phrase_in(r, p) for p in QUERY_PHRASES):
        return "query"
    return None


def align_intent_word(free_words, grammar_rest):
    """v1: the full-vocabulary pass can mishear the command word ('add' -> 'ad', 'mark' -> 'mac') that the
    grammar pass got right. When they disagree about the command, put the grammar's word back:
    replace a sound-alike, otherwise prepend (so no title word is lost)."""
    g = _norm_cmd(grammar_rest).split()
    if not g or not free_words or cal_intent(" ".join(free_words)) == cal_intent(grammar_rest):
        return free_words
    if difflib.SequenceMatcher(None, free_words[0], g[0]).ratio() >= 0.5:
        return [g[0]] + free_words[1:]
    return [g[0]] + free_words


def add_kind(text: str):
    """v1: (kind, reminding) - kind 'note' / 'task' / 'event', or None = decide by whether a time was given."""
    t = _norm_cmd(text)
    reminding = t.startswith(("remind", "set a reminder", "new reminder", "add a reminder"))
    if t.startswith(("note", "add a note", "add note", "make a note", "take a note", "write down", "new note")):
        return "note", False
    if reminding or any(phrase_in(t, p) for p in ("to do", "task", "my list", "the list")):
        return "task", reminding
    if t.startswith("schedule") or any(phrase_in(t, p) for p in ("event", "appointment", "meeting", "calendar")):
        return "event", False
    return None, False


def clean_cal_title(title: str) -> str:
    """v1: 'add buy milk to my to do list' -> 'buy milk'; 'remind me to call mom' -> 'call mom'."""
    t = " " + _norm_cmd(title) + " "
    for p in LIST_PHRASES:
        t = t.replace(" " + p + " ", " ")
    t = " ".join(w for w in t.split() if w not in N.WAKE_LIKE and w not in ("unk", "[unk]"))
    for _ in range(2):
        for p in TITLE_PREFIXES:
            if t == p or t.startswith(p + " "):
                t = t[len(p):].strip()
                break
    words = t.split()
    while words and words[0] in W.LEAD_FILLER:
        words.pop(0)
    while words and words[-1] in W.TRAIL_FILLER:
        words.pop()
    return " ".join(words)


# ----------------------------------------------------------------------------- shared helpers
def utterance(ctx, m) -> str:
    """The whole command as heard: the full-vocabulary re-decode (wake word dropped, command word realigned
    with the grammar's, v1) when there is one, else the matched text (typed input / grammar only)."""
    rest = m.text
    free = (m.slots or {}).get("free_text") or getattr(ctx, "free_text", None)
    if free:
        fw = N.normalize(free).split()
        at = next((i for i, w in enumerate(fw[:3]) if w in N.WAKE_LIKE), -1)
        fw = align_intent_word(fw[at + 1:], rest)
        if fw:
            return " ".join(fw)
    return rest


def _sir(ctx) -> str:
    return A.sir(ctx.config)


def _say(ctx, template: str, **kw) -> None:
    ctx.say(P.render(template, ctx.config, **kw))


def _waking(ctx):
    wh = A.cfg_get(ctx.config, "calendar.waking_hours", None)
    return tuple(wh) if wh else W.WAKING


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:]


def _minutes_words(m: int) -> str:
    if m % 1440 == 0:
        n = m // 1440
        return f"{P.number_words(n)} day{'s' if n != 1 else ''}"
    if m >= 60 and m % 60 == 0:
        n = m // 60
        return f"{P.number_words(n)} hour{'s' if n != 1 else ''}"
    return f"{P.number_words(m)} minute{'s' if m != 1 else ''}"


def _engine_state(ctx, key: str) -> dict:
    """Per-engine conversational state (what's next / notes span / recall paging), kept on T-engine only."""
    try:
        bag = _STATE.setdefault(ctx.engine, {})
    except TypeError:                                   # engine not weak-referenceable (a bare stub)
        bag = _STATE_BY_ID.setdefault(id(ctx.engine), {})
    return bag.setdefault(key, {})


_STATE: "weakref.WeakKeyDictionary[object, dict[str, dict]]" = weakref.WeakKeyDictionary()
_STATE_BY_ID: dict[int, dict[str, dict]] = {}


def _follow_up(ctx, kind: str, whitelist, *, seconds: float | None = None, payload=None) -> None:
    """ctx.follow_up, but a failure to open the window never turns a finished command into 'That didn't work'."""
    try:
        getattr(ctx, "follow_up")(kind, whitelist, seconds=seconds, payload=payload)
    except Exception:
        log.exception("could not open the %s follow-up window", kind)


def set_next_state(ctx, after: dt.datetime, seen=()) -> None:
    st = _engine_state(ctx, "next")
    st.clear()
    st.update(after=after, seen=set(seen))


def _after_add(ctx, kind: str, title: str, date, at=None) -> None:
    ctx.event("info", f"added {kind}: {title} ({date}{' ' + at.strftime('%H:%M') if at else ''})")
    _follow_up(ctx,"added", FOLLOW_UP_ADDED, seconds=ADDED_FOLLOW_UP_S)


def _duplicate(st, title: str, date: dt.date, at: dt.time | None) -> bool:
    hhmm = at.strftime("%H:%M") if at else None
    return any(it["title"].lower() == title.lower() and it["time"] == hhmm and not it.get("done")
               for _, it in st.items_between(date, date))


# ----------------------------------------------------------------------------- adding
def _add(ctx, m, hint: str) -> None:
    text = utterance(ctx, m)
    kind, reminding = add_kind(text)
    if hint == "note":
        kind, reminding = "note", False
    elif hint == "reminder":
        kind, reminding = "task", True          # D14/v1: a reminder is a to-do with a time that reminds
    elif kind is None and hint == "task":
        kind = "task"
    now = ctx.clock.now()
    today = now.date()
    norm = _norm_cmd(text)
    if kind == "note":
        toks = norm.split()
        norm = " ".join(w for i, w in enumerate(toks)
                        if not (i > 0 and toks[i - 1] == "note" and w in ("for", "four", "on")))
    w = W.parse_when(norm, now, _waking(ctx))
    title = _cap(clean_cal_title(w.title if w else text))

    if kind == "note":
        day = w.start.date() if (w and W.mentions_date(norm)) else today
        if title:
            _save_note(ctx, day, title)
        else:
            ctx.ask(RP.Q_NOTE, "text", lambda ans: _save_note(ctx, day, _cap(clean_cal_title(ans) or ans)),
                    listen="free", hint=RP.HINT_NOTE)
        return
    if not title:
        if reminding:
            _dialogue(ctx, "reminder", None, w)
        elif kind == "event":
            _dialogue(ctx, "event", None, w)
        elif kind == "task":
            ctx.ask(RP.Q_TODO, "text", lambda ans: _todo_answer(ctx, ans, w), listen="free", hint=RP.HINT_FREE)
        else:
            ctx.say(f"I didn't catch what to add{_sir(ctx)}. Try: add buy milk to my to-do list.")
        return
    if w is None and (reminding or kind == "event"):
        _dialogue(ctx, "reminder" if reminding else "event", title, None)      # §3.9: ask only what's missing
        return
    _commit(ctx, kind, reminding, title, w, now)


def _todo_answer(ctx, answer: str, trigger_when) -> None:
    now = ctx.clock.now()
    w = W.parse_when(_norm_cmd(answer), now, _waking(ctx)) or trigger_when
    title = _cap(clean_cal_title(w.title if (w is not None and w is not trigger_when) else answer) or answer)
    _commit(ctx, "task", False, title, w, now)


def _commit(ctx, kind, reminding, title, w, now) -> None:
    """v1 _cal_add from the point where title and date are known."""
    st = ctx.store
    today = now.date()
    timed = w is not None and not w.all_day
    date = w.start.date() if w else today
    at = w.start.time().replace(second=0, microsecond=0) if timed else None
    if kind is None:
        kind = "event" if timed else "task"
    default_ev = int(A.cfg_get(ctx.config, "calendar.default_reminder_min", 10))
    if w is not None and w.reminder_min is not None:
        rem = w.reminder_min
    elif kind == "task":
        rem = 0 if (timed or (reminding and date > today)) else None
    else:
        rem = default_ev if timed else None
    repeat = w.repeat if (w and kind == "event") else None
    when_txt = W.say_when(w, now) if w else "today"
    if repeat:
        when_txt += " every day" if repeat == "daily" else " every " + w.start.strftime("%A")
    if _duplicate(st, title, date, at):
        _say(ctx, RP.DUPLICATE)
        return

    def save():
        if kind == "task":
            st.add_task(title, date, at, rem, source="voice", now=now)
            if timed:
                msg = f"I'll remind you {when_txt}: {title}."
            else:
                msg = f"Added to your to-do list for {W.day_name(date, today)}{_sir(ctx)}: {title}."
                if rem is not None:
                    msg += " I'll remind you that morning."
                elif reminding:
                    msg += " Tell me a time if you want a reminder."
        else:
            st.add_event(title, date, at, rem, repeat=repeat, source="voice", now=now)
            msg = f"Added to your calendar: {title}, {when_txt}." + (
                f" I'll remind you {_minutes_words(rem)} before." if rem else "")
        ctx.say(msg)
        _after_add(ctx, kind, title, date, at)

    past = (timed and dt.datetime.combine(date, at) < now - dt.timedelta(minutes=1)) or (not timed and date < today)
    if past and not repeat:
        ctx.confirm(P.render(RP.IN_THE_PAST, ctx.config), save)
        return
    save()


def _save_note(ctx, day: dt.date, text: str) -> None:
    text = (text or "").strip()
    if not text:
        return
    now = ctx.clock.now()
    ctx.store.add_note(day, text, source="voice", now=now)
    ctx.say(f"Noted for {W.day_name(day, now.date())}: {text}.")          # v1 wording
    _after_add(ctx, "note", text, day)


def _dialogue(ctx, kind: str, title, w) -> None:
    """Slot-filling dialogue (T1 dialogue.Dialogue, §4.9) for whatever the utterance didn't carry."""
    from xyrus.dialogue import Dialogue
    slots = {"title": title or None, "date": None, "time": None}
    if w is not None:
        slots["date"] = w.start.date()
        if not w.all_day:
            slots["time"] = w.start.time().replace(second=0, microsecond=0)
        if w.repeat:
            slots["repeat"] = w.repeat
    trigger = "remind me" if kind == "reminder" else "add an event"
    ctx.start_dialogue(Dialogue(ctx, kind, slots=slots, on_complete=lambda s: _dialogue_done(ctx, kind, s),
                                trigger=trigger))


def _as_date(v, default: dt.date) -> dt.date:
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    if isinstance(v, str) and v:
        return dt.date.fromisoformat(v)
    return default


def _as_time(v):
    if isinstance(v, dt.datetime):
        return v.time().replace(second=0, microsecond=0)
    if isinstance(v, dt.time):
        return v.replace(second=0, microsecond=0)
    if isinstance(v, str) and v:
        return dt.time.fromisoformat(v)
    return None


def _dialogue_done(ctx, kind: str, slots: dict) -> None:
    st = ctx.store
    now = ctx.clock.now()
    today = now.date()
    title = _cap(str(slots.get("title") or "").strip())
    date = _as_date(slots.get("date"), today)
    at = None if slots.get("all_day") else _as_time(slots.get("time"))
    if not title:
        return
    if _duplicate(st, title, date, at):
        _say(ctx, RP.DUPLICATE)
        return
    explicit = slots.get("reminder_min")
    if kind == "reminder":
        rem = explicit if explicit is not None else (0 if (at is not None or date > today) else None)
        st.add_task(title, date, at, rem, source="voice", now=now)
        w = W.When(start=dt.datetime.combine(date, at or dt.time(W.DEFAULT_DAY_TIME, 0)), all_day=at is None)
        ctx.say(f"I'll remind you {W.say_when(w, now)}: {title}.")
        _after_add(ctx, "task", title, date, at)
        return
    rem = explicit if explicit is not None else (
        int(A.cfg_get(ctx.config, "calendar.default_reminder_min", 10)) if at is not None else None)
    st.add_event(title, date, at, rem, repeat=slots.get("repeat"), source="voice", now=now)
    if rem:
        _say(ctx, RP.EVENT_SAVED, lead=_minutes_words(rem))
    else:
        _say(ctx, RP.EVENT_SAVED_NO_REMINDER)
    _after_add(ctx, "event", title, date, at)


def _m_add(pred):
    def matcher(tokens):
        r = _norm_cmd(" ".join(tokens))
        if cal_intent(r) != "add":
            return None
        kind, reminding = add_kind(r)
        return {} if pred(kind, reminding) else None
    return matcher


@command("add_event", "add an event", "add an event <when>", "new event", "create an event", "schedule <text>",
         "put <text> on my calendar", "add a meeting <when>", "add an appointment <when>",
         section=SECTION, help="adds an event (asks for anything missing) and reminds you 10 minutes before",
         matcher=_m_add(lambda k, rem: k in ("event", None) and not rem), matcher_score=MATCH_ADD,
         free_trigger_words=("add", "schedule", "put", "new", "create"))
def add_event(ctx, m):
    _add(ctx, m, "event")


@command("add_reminder", "remind me <when> to <text>", "remind me to <text> <when>", "remind me <when>",
         "remind me to <text>", "remind me", "set a reminder", "set a reminder for <when>",
         section=SECTION, help="a to-do that speaks and pops up at that time",
         matcher=_m_add(lambda k, rem: rem), matcher_score=MATCH_ADD,
         free_trigger_words=("remind", "reminder", "set a reminder"))
def add_reminder(ctx, m):
    _add(ctx, m, "reminder")


@command("add_note", "add a note", "add a note for <day>", "take a note", "make a note", "note for <day>",
         "note that <text>",
         section=SECTION, help="a note on that day (today if you don't say one)",
         matcher=_m_add(lambda k, rem: k == "note"), matcher_score=MATCH_ADD,
         free_trigger_words=("note", "take a note", "make a note", "write down"))
def add_note(ctx, m):
    _add(ctx, m, "note")


@command("add_todo", "add a to do <text>", "add <text> to my to do list", "add a task <text>", "new to do <text>",
         "put <text> on my to do list", "add a to do",
         section=SECTION, help="adds a to-do (today if you don't say a day)",
         matcher=_m_add(lambda k, rem: k == "task" and not rem), matcher_score=MATCH_ADD,
         free_trigger_words=("task", "to do"))
def add_todo(ctx, m):
    _add(ctx, m, "task")


# ----------------------------------------------------------------------------- reading
def _span(ctx, m, default_label="today"):
    now = ctx.clock.now()
    today = now.date()
    span = (m.slots or {}).get("day")
    if span:
        return span
    words = _norm_cmd(m.text)
    if phrase_in(words, "coming up"):
        return today, today + dt.timedelta(days=6), "in the next 7 days"
    return W.parse_range(words, now, _waking(ctx)) or (today, today, default_label)


def _m_intent(name):
    def matcher(tokens):
        return {} if cal_intent(" ".join(tokens)) == name else None
    return matcher


@command("agenda", "what's on <day>", "what's on today", "what do i have <day>", "what do i have today",
         "my schedule", "my schedule for <day>", "anything on <day>", "what's coming up", "agenda",
         "what have i got <day>", "anything <day>",
         section=SECTION, help="reads your events and to-dos for any day or span: today, on friday, this week, "
                               "these three days, the next ten days, rest of the week",
         matcher=_m_intent("query"), matcher_score=MATCH_QUERY)
def agenda(ctx, m):
    now = ctx.clock.now()
    start, end, label = _span(ctx, m)
    text, overflow = A.describe_range(ctx.store, start, end, label, now, ctx.config)
    ctx.say(text)
    if overflow:
        ctx.ui(f"show:Calendar:{start.isoformat()}")
    notes = _engine_state(ctx, "notes")
    notes.clear()
    notes.update(start=start, end=end, label=label)
    set_next_state(ctx, dt.datetime.combine(end + dt.timedelta(days=1), dt.time.min))
    _follow_up(ctx,"day", FOLLOW_UP_DAY)


@command("hear_notes", "read the notes", "read it",
         section=SECTION, help="after an agenda: reads that span's notes", follow_up=True, priority=LOW)
def hear_notes(ctx, m):
    now = ctx.clock.now()
    span = _engine_state(ctx, "notes")
    start, end, label = (span["start"], span["end"], span["label"]) if span else (now.date(), now.date(), "today")
    text, overflow = A.describe_notes(ctx.store, start, end, now, ctx.config, label=label)
    ctx.say(text)
    if overflow:
        ctx.ui(f"show:Calendar:{start.isoformat()}")


@command("whats_next", "what's next", "next event", "what's my next event", "when is my next event",
         "anything coming up",
         section=SECTION, help="your next event or to-do; then 'and after that' works without the wake word",
         matcher=_m_intent("next"), matcher_score=MATCH_QUERY, follow_up=True)
def whats_next(ctx, m):
    now = ctx.clock.now()
    st = ctx.store
    parts = []
    pending = R.unacked_overdue(st, now)
    if pending:
        it, _, ago = pending[0]
        parts.append(R.still_pending_text(it, ago))
    parts.append(A.describe_next(st, now, ctx.config))
    ctx.say(" ".join(parts))
    nxt = st.next_item(now)
    if nxt:
        d, it = nxt
        set_next_state(ctx, A.occ_start(d, it), {(it["id"], d.isoformat())})
        _follow_up(ctx,"next", FOLLOW_UP_NEXT)


@command("and_after_that", "and after that", "what's after that", "next one",
         section=SECTION, help="after 'what's next': the one after it", follow_up=True, priority=LOW)
def and_after_that(ctx, m):
    now = ctx.clock.now()
    state = _engine_state(ctx, "next")
    if not state:
        return whats_next(ctx, m)
    nxt = A.next_after_item(ctx.store, now, state["after"], state["seen"])
    ctx.say(A.next_after(now, ctx.store, state["after"], ctx.config, state["seen"]))
    if nxt:
        d, it = nxt
        state["after"] = A.occ_start(d, it)
        state["seen"].add((it["id"], d.isoformat()))
        _follow_up(ctx,"next", FOLLOW_UP_NEXT)


@command("read_todos", "what's on my to do list", "what are my to dos", "read my to do list", "my to do list",
         "what do i need to do",
         section=SECTION, help="open to-dos: overdue, today, later",
         matcher=_m_intent("todos"), matcher_score=MATCH_TODOS)
def read_todos(ctx, m):
    ctx.say(A.describe_todos(ctx.store, ctx.clock.now(), ctx.config))


@command("read_notes", "read my notes", "read my notes for <day>", "what are my notes", "what are my notes for <day>",
         "any notes for <day>", "notes for <day>",
         section=SECTION, help="reads the notes for a day (today if you don't say one)")
def read_notes(ctx, m):
    now = ctx.clock.now()
    start, end, label = _span(ctx, m)
    text, overflow = A.describe_notes(ctx.store, start, end, now, ctx.config, label=label)
    ctx.say(text)
    if overflow:
        ctx.ui(f"show:Calendar:{start.isoformat()}")


@command("show_calendar", "show my calendar", "open my calendar", "show the calendar",
         section=SECTION, help="opens the Calendar tab")
def show_calendar(ctx, m):
    _say(ctx, "Here you are{sir}.")
    ctx.ui("show:Calendar")


# ----------------------------------------------------------------------------- changing
@command("clear_notes", "clear my notes for <day>", "clear today's notes", "delete my notes for <day>",
         section=SECTION, help="asks, then clears the notes of that day")
def clear_notes(ctx, m):
    now = ctx.clock.now()
    start, end, label = _span(ctx, m)
    notes = ctx.store.notes_between(start, end)
    where = f"for {W.day_name(start, now.date())}" if start == end else (
        label if label.startswith(("in ", "for ")) else f"for {label}")
    if not notes:
        ctx.say(f"No notes {where}{_sir(ctx)}.")
        return
    n = len(notes)

    def yes():
        d = start
        while d <= end:
            ctx.store.clear_notes(d)
            d += dt.timedelta(days=1)
        ctx.say("Cleared.")

    ctx.confirm(f"Clear {A.number_word(n)} note{'s' if n != 1 else ''} {where}{_sir(ctx)}? Yes or no.", yes)


def _title_score(q: str, title: str) -> float:
    """store.find's scoring (v1), for listing every close match."""
    t = title.lower()
    score = difflib.SequenceMatcher(None, q, t).ratio()
    qw, tw = set(q.split()), set(t.split())
    if qw and qw <= tw:
        score = max(score, 0.9)
    elif qw & tw:
        score = max(score, 0.5 + 0.4 * len(qw & tw) / len(qw | tw))
    return score


def _candidates(st, text: str, kinds=("event", "task"), cutoff=0.55) -> list[dict]:
    q = " ".join(w for w in text.lower().split() if w not in ("the", "a", "an", "my"))
    if not hasattr(st, "data"):                         # a stub store: its own best match only
        it = st.find(text, kinds=kinds, cutoff=cutoff)
        return [it] if it else []
    with st.lock:
        scored = [(_title_score(q, it["title"]), it) for it in st.data["items"] if it["kind"] in kinds]
    scored = [(s, it) for s, it in scored if s >= cutoff]
    if not scored:
        return []
    best = max(s for s, _ in scored)
    return [it for s, it in sorted(scored, key=lambda x: (-x[0], x[1]["date"], x[1]["time"] or ""))
            if s >= best - 0.02][:5]


def _occurrence(st, it: dict, today: dt.date) -> dt.date:
    """The occurrence a spoken reference means: the next one from today (recurring), else its date."""
    first = dt.date.fromisoformat(it["date"])
    if not it.get("repeat") or first >= today:
        return first
    for d, _ in st.occurrences(it, today, today + dt.timedelta(days=7)):
        return d
    return first


def _when_phrase(it: dict, d: dt.date, today: dt.date) -> str:
    return W.day_name(d, today) + (f" at {W.fmt_time(it['time'])}" if it["time"] else "")


def _confirm_delete(ctx, it: dict) -> None:
    today = ctx.clock.now().date()
    d = _occurrence(ctx.store, it, today)
    if it.get("repeat"):
        every = "every day" if it["repeat"] == "daily" else "every " + dt.date.fromisoformat(it["date"]).strftime("%A")
        question = f"Delete {it['title']} {every}? This removes all of them."
    else:
        question = f"Delete {it['title']}, {_when_phrase(it, d, today)}{_sir(ctx)}?"

    def yes():
        ctx.store.delete_item(it["id"])
        ctx.say(f"Deleted {it['title']}, {W.day_name(dt.date.fromisoformat(it['date']), today)}.")   # v1 wording

    ctx.confirm(question, yes)


def _pick(ctx, items: list[dict], then) -> None:
    """'Which one, sir? First: Dentist at 3 pm. Second: Gym at 6 pm.' -> then(item) (T1 PickDialogue)."""
    from xyrus.dialogue import PickDialogue
    items = items[:5]

    def picked(idx):
        if isinstance(idx, int) and 0 <= idx < len(items):
            then(items[idx])

    today = ctx.clock.now().date()
    options = [f"{A.item_phrase(it)}, {W.day_name(_occurrence(ctx.store, it, today), today)}" for it in items]
    ctx.start_dialogue(PickDialogue(ctx, options, picked))


def _recent_voice_note(st, now: dt.datetime, seconds: int = RECENT_NOTE_S):
    newest = None
    for n in getattr(st, "data", {}).get("notes", []):
        if n.get("source") != "voice":
            continue
        try:
            created = dt.datetime.fromisoformat(n["created"])
        except (KeyError, ValueError):
            continue
        if 0 <= (now - created).total_seconds() <= seconds and (newest is None or n["created"] >= newest["created"]):
            newest = n
    return newest


@command("delete_event", "delete the <text> event", "delete <text>", "delete the next event",
         "cancel the <text> event", "cancel the <text> appointment", "cancel the <text> meeting",
         "cancel the <text> reminder",
         # SPEC-AMBIGUITY: §3.9 also lists "delete <day>'s event", which the pattern syntax can't express (a slot
         # glued to "'s"); "delete tomorrow's event" is served by "delete <text>" and the handler below.
         section=SECTION, help="asks, then removes an event or to-do (say which one if several match)",
         matcher=_m_intent("delete"), matcher_score=MATCH_ADD,
         free_trigger_words=("delete", "remove", "cancel the"))
def delete_event(ctx, m):
    st = ctx.store
    now = ctx.clock.now()
    today = now.date()
    t = _norm_cmd(utterance(ctx, m))
    for p in DELETE_START + ("cancel",):
        if t.startswith(p + " "):
            t = t[len(p):].strip()
            break
    raw = t
    words = t.split()
    if words and words[-1] in KIND_WORDS and len(words) > 1:
        t = " ".join(words[:-1])
    if t in ("next", "the next", "next one", "my next"):
        nxt = st.next_item(now)
        if nxt is None:
            _say(ctx, "There's nothing coming up to delete{sir}.")
            return
        return _confirm_delete(ctx, nxt[1])
    # "tomorrow's event" / "friday's meeting"
    if raw.split() and raw.split()[-1] in KIND_WORDS:
        day_words = t[:-1] if t.endswith("s") and W.parse_range(t[:-1], now) else t
        span = W.parse_range(day_words, now, _waking(ctx)) if W.mentions_date(day_words) else None
        if span:
            items = [it for _, it in st.items_between(span[0], span[1], include_done=False)
                     if it["kind"] == "event"][:5]
            if not items:
                ctx.say(f"You have nothing planned {span[2]}{_sir(ctx)}.")
            elif len(items) == 1:
                _confirm_delete(ctx, items[0])
            else:
                _pick(ctx, items, lambda it: _confirm_delete(ctx, it))
            return
    t = clean_cal_title(t) or t
    found = _candidates(st, t) or (_candidates(st, raw) if raw != t else [])
    if not found:
        ctx.say(f"I couldn't find {t} on your calendar{_sir(ctx)}.")        # v1 wording
        return
    if len(found) == 1:
        _confirm_delete(ctx, found[0])
    else:
        _pick(ctx, found, lambda it: _confirm_delete(ctx, it))


@command("mark_done", "mark <text> as done", "mark <text> done", "tick off <text>", "cross off <text>",
         "i finished <text>", "<text> is done",
         section=SECTION, help="ticks off a to-do (the Calendar tab's tick boxes do the same)",
         matcher=_m_intent("done"), matcher_score=MATCH_ADD,
         free_trigger_words=("mark", "tick", "cross", "finished", "i finished", "complete"))
def mark_done(ctx, m):
    t = _norm_cmd(utterance(ctx, m))
    for p in sorted(DONE_START, key=len, reverse=True):
        if t.startswith(p + " "):
            t = t[len(p):].strip()
            break
    for suf in DONE_SUFFIXES:
        if t.endswith(suf):
            t = t[: -len(suf)].strip()
            break
    t = clean_cal_title(t) or t
    it = ctx.store.find(t, kinds=("task",), only_open=True)
    if it is None:
        ctx.say(f"I couldn't find {t} on your to-do list{_sir(ctx)}.")      # v1 wording
        return
    ctx.store.set_done(it["id"])
    ctx.say(f"Done: {it['title']}. Nice work{_sir(ctx)}.")                  # v1 wording


@command("mark_not_done", "mark <text> as not done",
         section=SECTION, help="puts a ticked-off to-do back on the list")
def mark_not_done(ctx, m):
    t = clean_cal_title(_norm_cmd(str(m.slots.get("text") or ""))) or str(m.slots.get("text") or "")
    done = [it for it in getattr(ctx.store, "data", {}).get("items", []) if it["kind"] == "task" and it.get("done")]
    best = max(done, key=lambda it: _title_score(t, it["title"]), default=None)
    if best is None or _title_score(t, best["title"]) < 0.55:
        ctx.say(f"I couldn't find {t} on your to-do list{_sir(ctx)}.")
        return
    ctx.store.set_done(best["id"], False)
    ctx.say(f"Okay{_sir(ctx)}. {best['title']} is back on your list.")


@command("undo_add", "undo that", "scratch that", "delete that", "remove that", "undo",
         # SPEC-AMBIGUITY: v1 also read a bare "undo" as this; §3.5 gives "undo" to Ctrl+Z. It is registered here
         # with LOW priority, so with the keys command loaded "undo" is Ctrl+Z after the wake word and this only
         # inside the 15 s "added" follow-up (no wake word).
         section=SECTION, help="removes the last thing you added by voice", follow_up=True, priority=LOW)
def undo_add(ctx, m):
    st = ctx.store
    if "note" in m.text.split():
        n = _recent_voice_note(st, ctx.clock.now())
        if n is not None:
            st.delete_note(n["id"])
            _say(ctx, "Deleted{sir}.")
            return
    title = st.undo_last()
    if title:
        ctx.say(f"Removed: {title}.")                                          # v1 wording
    else:
        _say(ctx, RP.UNDO_NOTHING)


# ----------------------------------------------------------------------------- alert / timer follow-up answers
@command("dismiss", "okay", "ok", "got it", "thanks", "thank you", "done", "dismiss", "stop",
         # priority 1: beats small-talk "thanks" (0) inside the alert follow-up so the alert is acknowledged, and
         # answers "stop" inside the timer follow-up (§3.8). With the wake word "stop" ties with the earlier
         # registered stop_talking (1), which wins. The shutdown follow-up whitelists command names, so this
         # command is never live there.
         section=SECTION, help="after an alert or a timer: acknowledges it", follow_up=True, priority=1)
def dismiss(ctx, m):
    if ctx.timers.ringing():
        ctx.timers.stop_ringing()            # the engine's ringing tick stops the chime
        return
    now = ctx.clock.now()
    a = _alert(ctx, now)
    answering = isinstance(getattr(ctx, "payload", None), R.Alert)      # inside the alert's own follow-up
    if a is not None and (answering or not R.is_acked(ctx.store, a)):
        R.acknowledge(ctx.store, a)
        _say(ctx, "Noted{sir}.")
        return
    if m.text.split()[:1] in (["thanks"], ["thank"]):
        ctx.say(P.pick("thanks", RP.THANKS, ctx.config))
    else:
        _say(ctx, RP.OKAY)


@command("snooze", "snooze", "snooze it",
         section=SECTION, help="after an alert: again in 10 minutes; after a timer: 5 more minutes",
         follow_up=True, priority=LOW)
def snooze(ctx, m):
    if ctx.timers.ringing():
        ctx.timers.snooze(300)               # §3.8: "snooze" = +5 min
        _say(ctx, RP.TIMER_SNOOZED)
        return
    _snooze_alert(ctx, int(A.cfg_get(ctx.config, "calendar.snooze_minutes", 10)) * 60)


def _alert(ctx, now):
    """The alert being answered: the follow-up payload the engine attached, else the newest spoken one."""
    a = getattr(ctx, "payload", None)
    if isinstance(a, R.Alert):
        return a
    return R.last_alert(ctx.store, now)


def _snooze_alert(ctx, seconds: int) -> None:
    now = ctx.clock.now()
    a = _alert(ctx, now)
    if a is None:
        _say(ctx, "There's nothing to snooze{sir}.")
        return
    minutes = max(1, -(-int(seconds) // 60))
    R.snooze(ctx.store, a, now, minutes)
    ctx.say(f"I'll remind you again in {P.speak_duration(minutes * 60)}{_sir(ctx)}.")


@command("remind_again", "remind me again in <duration>",
         section=SECTION, help="after an alert: brings it back after that long", follow_up=True)
def remind_again(ctx, m):
    _snooze_alert(ctx, m.slots["duration"])
