"""Slot-filling dialogues (§4.9), lifted from SCRATCH/dialogue_proto.py.

Kept from the prototype: construction order (the engine holds the dialogue before start()), the noise rule
(unparseable <= 1 word is swallowed on mic input), attempts (2 failures -> GIVE_UP), nudge once at
dialogue_timeout_s then LEAVE, cancel words anywhere (not "no"), yes-set only as the whole utterance, FIX by
keywords title/date/day/time. Changed: dates.parse_when adapter, ctx.say(q, then=on_speech_done), deadlines on
clock.mono(), on_complete callback instead of Engine.save, summary via persona.speak_when, strings from
replies.py.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Callable, Iterable

from xyrus import normalize as N
from xyrus import persona
from xyrus import replies as R
from xyrus.interfaces import DialogueView

log = logging.getLogger("xyrus.dialogue")

CANCEL_WORDS = N.CANCEL_WORDS
YES_WORDS = N.YES_WORDS
NO_WORDS = N.NO_WORDS
NOISE_MAX_WORDS = 1
MAX_ATTEMPTS = 2
ALL_DAY_ANSWERS = ("all day", "no time", "any time")
FIX_WORDS = ("title", "date", "day", "time", "change", "the")
NEEDS_CONFIRM = ("event", "reminder", "memory", "name")
SLOT_KINDS = ("event", "reminder", "task", "note", "memory", "name")

TITLE_QUESTIONS = {
    "event": (R.Q_EVENT_TITLE, R.HINT_TITLE),
    "reminder": (R.Q_REMINDER_TITLE, R.HINT_TITLE),
    "task": (R.Q_TODO, R.HINT_TITLE),
    "note": (R.Q_NOTE, R.HINT_NOTE),
    "memory": (R.Q_MEMORY, R.HINT_MEMORY),
    "name": (R.Q_NAME, R.HINT_FREE),
}


# ----------------------------------------------------------------------------- date adapter
def _fallback_parse_when(text: str, now: dt.datetime):
    """The prototype's fake parser; used only when xyrus.dates (T2) is not importable."""
    words = text.split()
    date = time = None
    rest: list[str] = []
    hours = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
             "ten": 10, "eleven": 11, "twelve": 12}
    i = 0
    while i < len(words):
        w = words[i]
        if w == "tomorrow":
            date = (now + dt.timedelta(days=1)).date()
        elif w == "today":
            date = now.date()
        elif w == "at" and i + 1 < len(words) and words[i + 1] in hours:
            h = hours[words[i + 1]]
            i += 1
            if i + 1 < len(words) and words[i + 1] in ("am", "pm"):
                i += 1
                if words[i] == "pm" and h < 12:
                    h += 12
                if words[i] == "am" and h == 12:
                    h = 0
            elif h < 12 and (date is None or date == now.date()) and now.hour >= h:
                h += 12
            time = dt.time(h, 0)
        elif w == "noon":
            time = dt.time(12, 0)
        else:
            rest.append(w)
        i += 1
    return date, time, rest, {"date_mentioned": date is not None}


def parse_when(text: str, now: dt.datetime, cfg: Any = None):
    """Adapter over dates.parse_when -> (date|None, time|None, leftover_words, extra).
    extra: date_mentioned (bool), all_day (explicit 'all day'), repeat, reminder_min."""
    text = " ".join(w for w in text.split() if w != "[unk]")
    explicit_all_day = any(N.phrase_in(text, p) for p in ALL_DAY_ANSWERS)
    try:
        from xyrus import dates
    except ImportError:
        d, t, rest, extra = _fallback_parse_when(text, now)
        extra["all_day"] = explicit_all_day
        return d, t, rest, extra
    waking = None
    try:
        wh = cfg.get("calendar.waking_hours") if cfg is not None else None
        waking = tuple(wh) if wh else None
    except Exception:
        waking = None
    try:
        w = dates.parse_when(text, now, waking=waking) if waking else dates.parse_when(text, now)
    except TypeError:
        w = dates.parse_when(text, now)
    except Exception:
        log.exception("dates.parse_when failed on %r", text)
        w = None
    if w is None:
        return None, None, text.split(), {"date_mentioned": False, "all_day": explicit_all_day}
    try:
        mentioned = bool(dates.mentions_date(text))
    except Exception:
        mentioned = True
    time = None if w.all_day else w.start.time()
    date = w.start.date() if (mentioned or time is not None) else None
    rest = (w.title or "").split()
    extra = {"date_mentioned": mentioned, "all_day": explicit_all_day, "repeat": getattr(w, "repeat", None),
             "reminder_min": getattr(w, "reminder_min", None)}
    return date, time, rest, extra


def _date_phrase(d: dt.date, today: dt.date) -> str:
    if d == today:
        return "today"
    if d == today + dt.timedelta(days=1):
        return "tomorrow"
    return f"on {d.strftime('%B').lower()} {d.day}"


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:] if s else s


# ============================================================================= Dialogue
class Dialogue:
    """Slot filling for kinds event | reminder | task | note | memory | name."""

    def __init__(self, ctx, kind: str, *, slots: dict | None = None, on_complete: Callable[[dict], None],
                 trigger: str = "add an event"):
        self.ctx = ctx
        self.engine = ctx.engine
        self.clock = ctx.clock
        self.config = ctx.config
        self.kind = kind
        self.slots: dict[str, Any] = {"title": None, "date": None, "time": None, "all_day": False,
                                      "repeat": None, "reminder_min": None}
        self.slots.update(slots or {})
        self.on_complete = on_complete
        self.trigger = trigger
        self.state: str | None = None
        self.attempts = 0
        self.nudged = False
        self.deadline: float | None = None
        self.listen_mode = "free"
        self.question = ""
        self.hint = ""
        self.finished = False
        self._qid = 0
        # NOTE: do not ask here. The engine must hold self.dialogue before the first question is spoken,
        # otherwise on_speech_done() never arms the deadline (the prototype caught this bug).

    # ---- lifecycle
    def start(self) -> None:
        self._next()

    def _timeout(self) -> float:
        return float(self.config.get("dialogue_timeout_s", 20))

    def _now(self) -> dt.datetime:
        return self.clock.now()

    def _missing(self) -> str | None:
        if not self.slots.get("title"):
            return "title"
        if self.kind in ("note", "memory", "name", "task"):
            return None
        if not self.slots.get("date"):
            return "when"
        if self.slots.get("time") is None and self.kind in ("event", "reminder") and not self.slots.get("all_day"):
            return "time"
        return None

    def _next(self) -> None:
        slot = self._missing()
        if slot is None:
            if self.kind in NEEDS_CONFIRM:
                self.state = "confirm"
                self._ask()
            else:
                self._complete()
            return
        self.state = slot
        self._ask()

    def _question(self) -> tuple[str, str, str]:
        if self.state == "title":
            q, hint = TITLE_QUESTIONS.get(self.kind, (R.Q_EVENT_TITLE, R.HINT_TITLE))
            return q, hint, "free"
        if self.state == "when":
            return R.Q_WHEN, R.HINT_WHEN, "when"
        if self.state == "time":
            return R.Q_TIME, R.HINT_TIME, "when"
        if self.state == "fix":
            return R.Q_FIX, R.HINT_FIX, "confirm"
        if self.state == "confirm":
            if self.kind == "memory":
                return R.MEMORY_CORRECT.format(text=self.slots.get("title") or ""), R.HINT_CONFIRM, "confirm"
            if self.kind == "name":
                return R.NAME_CORRECT.format(name=_cap(self.slots.get("title") or "")), R.HINT_CONFIRM, "confirm"
            return R.CORRECT.format(summary=self.summary()), R.HINT_CONFIRM, "confirm"
        return "", "", "command"

    def _ask(self, *, repeat: bool = False, prefix: str = "") -> None:
        q, hint, mode = self._question()
        self.question, self.hint, self.listen_mode = q, hint, mode
        self.deadline = None                 # re-armed when the question finished speaking
        self._qid += 1
        qid = self._qid
        text = q
        if repeat:
            text = R.NUDGE + q
        if prefix:
            text = f"{prefix} {text}"
        self.ctx.say(text, then=lambda: self.on_speech_done(qid), _kind="ask")

    def on_speech_done(self, qid: int | None = None) -> None:
        if self.finished or (qid is not None and qid != self._qid):
            return
        timeout = self._timeout()
        self.deadline = self.clock.mono() + timeout if timeout > 0 else None     # 0 = no limit

    def tick(self) -> None:
        if self.finished or self.deadline is None:
            return
        if self.clock.mono() >= self.deadline:
            if not self.nudged:
                self.nudged = True
                self._ask(repeat=True)
            else:
                self._end(R.LEAVE.replace("{trigger}", self.trigger))

    def cancel(self, *, say: bool = True) -> None:
        if self.finished:
            return
        self._end(persona.pick("cancelled", R.CANCELLED, self.config) if say else None)

    def _end(self, message: str | None) -> None:
        self.finished = True
        self.deadline = None
        self.engine.end_dialogue(self)
        if message:
            self.ctx.say(message)

    def _complete(self) -> None:
        self.finished = True
        self.deadline = None
        self.engine.end_dialogue(self)
        try:
            self.on_complete(dict(self.slots))
        except Exception:
            log.exception("on_complete of %s dialogue failed", self.kind)
            self.ctx.say(R.ACTION_FAILED)

    # ---- input
    def _is_cancel(self, text: str) -> bool:
        return any(N.phrase_in(text, c) for c in CANCEL_WORDS)

    def handle(self, text: str, *, free_text: str | None = None, source: str = "mic") -> bool:
        """Returns True if the utterance was consumed (always, while the dialogue is active)."""
        if self.finished:
            return False
        t = " ".join(w for w in text.split() if w != "[unk]")
        words = t.split()
        if self._is_cancel(t):
            self.cancel(say=True)
            return True
        if self.state == "title":
            return self._handle_title(t, free_text, source)
        if self.state == "confirm":
            if t in YES_WORDS:
                self._complete()
                return True
            if t in NO_WORDS:
                if self.kind in ("memory", "name"):
                    self.slots["title"] = None
                    self._next()
                else:
                    self.state = "fix"
                    self._ask()
                return True
            if self._apply_fix(words):
                return True
            return self._not_understood(words, source)
        if self.state == "fix":
            if self._apply_fix(words):
                return True
            return self._not_understood(words, source)
        if self.state in ("when", "time"):
            return self._handle_when(t, words, source)
        return False

    def _handle_title(self, t: str, free_text: str | None, source: str) -> bool:
        src = " ".join(w for w in (free_text or t).split() if w != "[unk]").strip()
        if not src:
            return True                                           # empty free decode: keep waiting
        if source == "mic" and len(src.split()) <= NOISE_MAX_WORDS and src in N.NOISE_WORDS:
            return True                                           # "how" / "half": room noise
        if self.kind in ("event", "reminder", "task"):
            date, time, rest, extra = parse_when(src, self._now(), self.config)
            self.slots["title"] = " ".join(rest).strip() or src
            if date is not None and (extra.get("date_mentioned") or not self.slots.get("date")):
                self.slots["date"] = date
            if time is not None:
                self.slots["time"] = time
            if extra.get("all_day"):
                self.slots["all_day"] = True
            if extra.get("repeat"):
                self.slots["repeat"] = extra["repeat"]
            if extra.get("reminder_min") is not None:
                self.slots["reminder_min"] = extra["reminder_min"]
        else:
            self.slots["title"] = src
        self.attempts = 0
        self.nudged = False
        self._next()
        return True

    def _handle_when(self, t: str, words: list[str], source: str) -> bool:
        if t in ALL_DAY_ANSWERS and self.kind in ("event", "note", "reminder", "task"):
            self.slots["all_day"] = True
            self.slots["time"] = None
            self.attempts = 0
            self.nudged = False
            self._next()
            return True
        now = self._now()
        text = t
        if self.state == "time" and self.slots.get("date"):
            date, time, rest, extra = parse_when(text, now, self.config)
            if time is not None and not extra.get("date_mentioned"):
                # re-read the hour against the chosen day ("at seven" means 7 AM tomorrow, not 7 PM today)
                d2, t2, _, _ = parse_when(f"{text} {_date_phrase(self.slots['date'], now.date())}", now,
                                          self.config)
                if t2 is not None and (d2 is None or d2 == self.slots["date"]):
                    time = t2
        else:
            date, time, rest, extra = parse_when(text, now, self.config)
        if date is None and time is None:
            return self._not_understood(words, source)
        if date is not None and (extra.get("date_mentioned") or not self.slots.get("date")):
            self.slots["date"] = date
        if time is not None:
            self.slots["time"] = time
            self.slots["all_day"] = False
            if not self.slots.get("date"):
                self.slots["date"] = now.date() if time > now.time() else (now + dt.timedelta(days=1)).date()
        if extra.get("repeat"):
            self.slots["repeat"] = extra["repeat"]
        self.attempts = 0
        self.nudged = False
        self._next()
        return True

    def _apply_fix(self, words: list[str]) -> bool:
        if "title" in words:
            self.slots["title"] = None
        elif "date" in words or "day" in words:
            self.slots["date"] = None
        elif "time" in words:
            self.slots["time"] = None
            self.slots["all_day"] = False
        else:
            return False
        self.attempts = 0
        self.nudged = False
        self._next()
        return True

    def _not_understood(self, words: list[str], source: str) -> bool:
        if source == "mic" and len(words) <= NOISE_MAX_WORDS:
            return True                       # swallow mic noise like "how" / "half"; keep waiting
        self.attempts += 1
        if self.attempts >= MAX_ATTEMPTS:
            self._end(R.GIVE_UP)
            return True
        self._ask(prefix=R.SORRY_PREFIX)      # the question text carries its own example
        return True

    # ---- views
    def summary(self) -> str:
        """'Dentist, tomorrow at 3 PM.' / 'Standup, every day at 9:30 AM.'"""
        title = _cap(self.slots.get("title") or "")
        d, t = self.slots.get("date"), self.slots.get("time")
        now = self._now()
        rep = self.slots.get("repeat")
        if rep == "daily":
            when = "every day"
            if t:
                when += f" at {persona.speak_time(t)}"
        elif rep == "weekly" and d:
            when = f"every {d.strftime('%A')}"
            if t:
                when += f" at {persona.speak_time(t)}"
        elif d:
            start = dt.datetime.combine(d, t or dt.time(0, 0))
            when = persona.speak_when(start, t is None, now, relative=False)
        else:
            when = ""
        return f"{title}, {when}." if when else f"{title}."

    def display_slots(self) -> dict[str, str]:
        out: dict[str, str] = {}
        now = self._now()
        if self.slots.get("title"):
            out["title"] = _cap(self.slots["title"])
        if self.slots.get("date"):
            out["date"] = persona.speak_day(self.slots["date"], now.date())
        if self.slots.get("time"):
            out["time"] = persona.speak_time(self.slots["time"])
        elif self.slots.get("all_day"):
            out["time"] = "all day"
        return out

    def view(self) -> DialogueView:
        left = 0
        if self.deadline is not None:
            left = max(0, round(self.deadline - self.clock.mono()))
        return DialogueView(kind=self.kind, state=self.state or "", question=self.question, hint=self.hint,
                            slots=self.display_slots(), seconds_left=int(left), listening=self.listen_mode)

    def answer_words(self) -> tuple[str, ...]:
        cancel = tuple(w for c in CANCEL_WORDS for w in c.split())
        if self.listen_mode == "confirm":
            yes_no = tuple(w for p in (*YES_WORDS, *NO_WORDS) for w in p.split())
            return tuple(sorted(set(yes_no + cancel + FIX_WORDS)))
        if self.listen_mode == "when":
            return tuple(sorted(set(cancel + ("all", "day", "no", "any", "time"))))
        return ()


# ============================================================================= Confirm
class ConfirmDialogue(Dialogue):
    """kind 'confirm': one CONFIRM state, listen 'confirm', window confirm_window_s, no nudge; expiry ->
    silent cancel + Activity 'confirm timed out'."""

    def __init__(self, ctx, question: str, on_yes: Callable[[], None], on_no: Callable[[], None] | None = None,
                 *, window_s: float | None = None, action: str | None = None):
        super().__init__(ctx, "confirm", on_complete=lambda _slots: on_yes(), trigger="")
        if action is None:                         # UI header "Confirm shutdown?" reads slots["action"]
            m = getattr(ctx, "match", None)
            action = m.command.name.replace("_", " ") if m is not None else ""
        self.action = action
        self._q = question
        self.on_yes = on_yes
        self.on_no = on_no
        self.window_s = window_s
        self.state = "confirm"
        self.listen_mode = "confirm"

    def start(self) -> None:
        self._ask()

    def _question(self) -> tuple[str, str, str]:
        return self._q, R.HINT_CONFIRM, "confirm"

    def _timeout(self) -> float:
        return float(self.window_s if self.window_s is not None else self.config.get("confirm_window_s", 12))

    def tick(self) -> None:
        if self.finished or self.deadline is None:
            return
        if self.clock.mono() >= self.deadline:
            self._end(None)
            self.ctx.event("info", "confirm timed out")

    def handle(self, text: str, *, free_text: str | None = None, source: str = "mic") -> bool:
        if self.finished:
            return False
        t = " ".join(w for w in text.split() if w != "[unk]")
        if t in YES_WORDS:
            self._complete()
            return True
        if t in NO_WORDS or self._is_cancel(t):
            self._end(None)
            if self.on_no is not None:
                try:
                    self.on_no()
                except Exception:
                    log.exception("on_no failed")
            else:
                self.ctx.say(persona.pick("cancelled", R.CANCELLED, self.config))
            return True
        words = t.split()
        if source == "mic" and len(words) <= NOISE_MAX_WORDS:
            return True
        self.attempts += 1
        if self.attempts >= MAX_ATTEMPTS:
            self._end(persona.pick("cancelled", R.CANCELLED, self.config))
            return True
        self._ask(prefix=R.SORRY_PREFIX)
        return True

    def answer_words(self) -> tuple[str, ...]:
        cancel = tuple(w for c in CANCEL_WORDS for w in c.split())
        yes_no = tuple(w for p in (*YES_WORDS, *NO_WORDS) for w in p.split())
        return tuple(sorted(set(yes_no + cancel)))

    def display_slots(self) -> dict[str, str]:
        return {"action": self.action} if self.action else {}


# ============================================================================= Ask
# per-slot defaults (v1 song ask flow, §3.6 step 4; timer duration ask, §3.8)
_ASK_DEFAULTS: dict[str, dict] = {
    "song": {"kind": "play", "cancel_words": tuple(N.SONG_CANCEL), "cancel_reply": R.OKAY, "nudge": False,
             "ignore": tuple(N.SONG_ASK), "window_key": "command_window_s"},
    "duration": {"kind": "timer", "hint": R.HINT_DURATION},
}


class AskDialogue(Dialogue):
    """One question, one slot parsed with SLOT_TYPES[slot] (or the raw text when `slot` is not a slot type).
    Options (additive): kind, cancel_words, cancel_reply, nudge, window_s, trigger."""

    def __init__(self, ctx, question: str, slot: str, on_answer: Callable[[Any], None], *, listen: str = "when",
                 hint: str = "", ignore: Iterable[str] = frozenset(), kind: str | None = None,
                 cancel_words: Iterable[str] | None = None, cancel_reply: str | None = None,
                 nudge: bool | None = None, window_s: float | None = None, trigger: str = ""):
        d = _ASK_DEFAULTS.get(slot, {})
        super().__init__(ctx, kind or d.get("kind", slot), on_complete=lambda _s: None, trigger=trigger)
        self._q = question
        self.slot = slot
        self.on_answer = on_answer
        self._listen = listen
        self._hint = hint or d.get("hint", R.HINT_FREE if listen == "free" else R.HINT_WHEN)
        self.ignore = set(ignore) | set(d.get("ignore", ()))
        self.cancel_words = tuple(cancel_words) if cancel_words is not None else tuple(
            d.get("cancel_words", CANCEL_WORDS))
        self.cancel_reply = cancel_reply if cancel_reply is not None else d.get("cancel_reply")
        self.nudge = nudge if nudge is not None else d.get("nudge", True)
        if window_s is None and d.get("window_key"):
            window = getattr(self.engine, "command_window", None)      # 0 = no limit while Whisper captures
            window_s = float(window() if d["window_key"] == "command_window_s" and callable(window)
                             else self.config.get(d["window_key"], 15))
        self.window_s = window_s
        self.state = "free" if listen == "free" else "when"
        self.listen_mode = listen
        self.value: Any = None

    def start(self) -> None:
        self._ask()

    def _question(self) -> tuple[str, str, str]:
        return self._q, self._hint, self._listen

    def _timeout(self) -> float:
        return float(self.window_s if self.window_s is not None else self.config.get("dialogue_timeout_s", 20))

    def tick(self) -> None:
        if self.finished or self.deadline is None:
            return
        if self.clock.mono() >= self.deadline:
            if self.nudge and not self.nudged:
                self.nudged = True
                self._ask(repeat=True)
            elif self.nudge:
                self._end(R.LEAVE.replace("{trigger}", self.trigger) if self.trigger else None)
            else:
                self._end(None)                   # silent end (song ask, §3.6)

    def handle(self, text: str, *, free_text: str | None = None, source: str = "mic") -> bool:
        if self.finished:
            return False
        use_free = self._listen == "free" and free_text
        raw = free_text if use_free else text
        toks = [w for w in (raw or "").split() if w != "[unk]"]
        wakes = set(self.config.get("wake_words") or N.WAKE_ALIASES) | {N.TYPED_WAKE}
        while toks and toks[0] in wakes:          # a leading wake word only ("zero" may be a real title word)
            toks = toks[1:]
        t_nowake = " ".join(toks)
        if t_nowake in self.cancel_words or any(N.phrase_in(t_nowake, c) for c in self.cancel_words if " " in c):
            self._end(self.cancel_reply)
            return True
        if not t_nowake or t_nowake in self.ignore:
            return True                           # keep waiting
        words = t_nowake.split()
        if source == "mic" and len(words) == 1 and words[0] in N.NOISE_WORDS:
            return True
        value = self._parse(words)
        if value is None:
            return self._not_understood(words, source)
        self.value = value
        self.finished = True
        self.deadline = None
        self.engine.end_dialogue(self)
        try:
            self.on_answer(value)
        except Exception:
            log.exception("on_answer of ask(%s) failed", self.slot)
            self.ctx.say(R.ACTION_FAILED)
        return True

    def _parse(self, words: list[str]) -> Any:
        from xyrus.parsing import SLOT_TYPES, SlotEnv
        st = SLOT_TYPES.get(self.slot)
        if st is None:
            return " ".join(words) or None
        env = SlotEnv(config=self.config, now=self._now(), free_text=None,
                      app_index=getattr(self.engine, "app_index", None))
        try:
            return st.parse(words, env)
        except Exception:
            log.exception("ask slot <%s> parse failed", self.slot)
            return None

    def answer_words(self) -> tuple[str, ...]:
        cancel = tuple(w for c in self.cancel_words for w in c.split())
        return tuple(sorted(set(cancel)))

    def display_slots(self) -> dict[str, str]:
        return {}


# ============================================================================= Pick
_PICK_WORDS = {"first": 0, "one": 0, "second": 1, "two": 1, "third": 2, "three": 2, "fourth": 3, "four": 3,
               "fifth": 4, "five": 4, "last": -1}
_ORDINAL_LABELS = ("First", "Second", "Third", "Fourth", "Fifth")


class PickDialogue(Dialogue):
    """'Which one? First: … Second: …' -> first|second|third|one|two|three|all. on_pick(index) or on_pick('all')."""

    def __init__(self, ctx, options: list[str], on_pick: Callable[[Any], None], *, question: str | None = None,
                 allow_all: bool = False, window_s: float | None = None):
        super().__init__(ctx, "pick", on_complete=lambda _s: None, trigger="")
        self.options = list(options)[:5]
        self.on_pick = on_pick
        self.allow_all = allow_all
        self.window_s = window_s
        if question is None:
            listed = " ".join(f"{_ORDINAL_LABELS[i]}: {o}." for i, o in enumerate(self.options))
            question = R.Q_WHICH.format(sir="{sir}", options=listed)
        self._q = question
        self.state = "pick"
        self.listen_mode = "confirm"

    def start(self) -> None:
        self._ask()

    def _question(self) -> tuple[str, str, str]:
        return self._q, R.HINT_PICK, "confirm"

    def _timeout(self) -> float:
        return float(self.window_s if self.window_s is not None else self.config.get("dialogue_timeout_s", 20))

    def tick(self) -> None:
        if self.finished or self.deadline is None:
            return
        if self.clock.mono() >= self.deadline:
            self._end(None)

    def handle(self, text: str, *, free_text: str | None = None, source: str = "mic") -> bool:
        if self.finished:
            return False
        t = " ".join(w for w in text.split() if w != "[unk]")
        if self._is_cancel(t) or t in NO_WORDS:
            self.cancel(say=True)
            return True
        words = t.split()
        choice: Any = None
        if self.allow_all and ("all" in words or "both" in words):
            choice = "all"
        else:
            for w in words:
                if w in _PICK_WORDS:
                    idx = _PICK_WORDS[w]
                    idx = len(self.options) - 1 if idx < 0 else idx
                    if idx < len(self.options):
                        choice = idx
                    break
        if choice is None:
            return self._not_understood(words, source)
        self.finished = True
        self.deadline = None
        self.engine.end_dialogue(self)
        try:
            self.on_pick(choice)
        except Exception:
            log.exception("on_pick failed")
            self.ctx.say(R.ACTION_FAILED)
        return True

    def answer_words(self) -> tuple[str, ...]:
        cancel = tuple(w for c in CANCEL_WORDS for w in c.split())
        return tuple(sorted(set(cancel + tuple(_PICK_WORDS) + ("the", "all", "both", "no"))))

    def display_slots(self) -> dict[str, str]:
        return {f"{i + 1}": o for i, o in enumerate(self.options)}
