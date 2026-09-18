"""Numbers, durations, percent, expressions and the slot-type table (§4.6).

Pure functions over (already normalised) token lists; any thread. The `when` and `day` slots delegate to
xyrus.dates (T2) through a lazy import.
"""
from __future__ import annotations

import datetime as dt
import logging
import math
import re
from dataclasses import dataclass
from typing import Any, Callable

from xyrus import normalize as N

log = logging.getLogger("xyrus.parsing")

# ----------------------------------------------------------------------------- number tables
UNIT_WORDS = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
              "eight": 8, "nine": 9}
TEEN_WORDS = {"ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
              "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19}
TENS_WORDS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80,
              "ninety": 90}
SCALE_WORDS = {"hundred": 100, "thousand": 1000, "million": 1_000_000}

# v1 arc.py table (used verbatim by parse_duration) + zero
_V1_NUMBERS = {"a": 1, "an": 1, **{k: v for k, v in UNIT_WORDS.items() if k != "zero"}, **TEEN_WORDS,
               "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80,
               "ninety": 90, "half": 0.5}
NUMBER_WORDS: dict[str, float] = {**_V1_NUMBERS, "zero": 0}
_V1_TENS = {20, 30, 40, 50, 60, 70, 80, 90}
DURATION_UNITS = {"second": 1, "seconds": 1, "minute": 60, "minutes": 60, "hour": 3600, "hours": 3600}

_DIGIT_RE = re.compile(r"^\d+(?:\.\d+)?$")


def _cls(w: str) -> str | None:
    if _DIGIT_RE.match(w):
        return "digit"
    if w in UNIT_WORDS:
        return "unit"
    if w in TEEN_WORDS:
        return "teen"
    if w in TENS_WORDS:
        return "tens"
    if w in SCALE_WORDS:
        return "scale"
    return None


def _word_val(w: str) -> float:
    return float(UNIT_WORDS.get(w, TEEN_WORDS.get(w, TENS_WORDS.get(w, 0))))


def _scan_number(tokens: list[str], i: int) -> tuple[float, int] | None:
    """Parse the maximal number group starting at tokens[i] -> (value, end index) or None."""
    n = len(tokens)
    total, current, prev, j = 0.0, 0.0, None, i
    while j < n:
        w = tokens[j]
        c = _cls(w)
        if c is None and w in ("a", "an") and prev is None and j + 1 < n and _cls(tokens[j + 1]) == "scale":
            current, prev, j = 1.0, "unit", j + 1           # "a hundred"
            continue
        if c == "digit":
            if prev is not None:
                break
            current, prev, j = float(w), "digit", j + 1
            continue
        if c in ("unit", "teen"):
            v = _word_val(w)
            if prev in (None, "scale", "and"):
                current, prev, j = current + v, c, j + 1
                continue
            if prev == "tens" and c == "unit" and v >= 1:
                current, prev, j = current + v, "unit2", j + 1
                continue
            break
        if c == "tens":
            if prev in (None, "scale", "and"):
                current, prev, j = current + _word_val(w), "tens", j + 1
                continue
            break
        if c == "scale":
            if prev in ("decimal", "and"):
                break
            sv = SCALE_WORDS[w]
            if sv == 100:
                current = (current or 1.0) * 100
            else:
                total += (current or 1.0) * sv
                current = 0.0
            prev, j = "scale", j + 1
            continue
        if w == "and" and prev == "scale" and j + 1 < n and _cls(tokens[j + 1]) in ("unit", "teen", "tens"):
            prev, j = "and", j + 1
            continue
        if (w == "point" and prev not in (None, "and", "decimal") and j + 1 < n
                and (tokens[j + 1] in UNIT_WORDS or tokens[j + 1].isdigit())):
            j += 1
            digits = ""
            while j < n and (tokens[j] in UNIT_WORDS or tokens[j].isdigit()):
                digits += tokens[j] if tokens[j].isdigit() else str(UNIT_WORDS[tokens[j]])
                j += 1
            current += float("0." + digits)
            prev = "decimal"
            break
        break
    if j == i:
        return None
    return total + current, j


def numbers_in(tokens: list[str]) -> list[tuple[int, float]]:
    """(index, value) of each maximal number group, left to right."""
    out, i = [], 0
    while i < len(tokens):
        hit = _scan_number(tokens, i)
        if hit is None:
            i += 1
            continue
        out.append((i, hit[0]))
        i = hit[1]
    return out


def parse_number(tokens: list[str]) -> float | None:
    """The whole token list as one number: 'two hundred and fifty'=250, 'three point five'=3.5, '25'."""
    toks = [t for t in tokens if t]
    if not toks:
        return None
    hit = _scan_number(toks, 0)
    if hit is None or hit[1] != len(toks):
        return None
    return hit[0]


def parse_duration(tokens: list[str]) -> int:
    """v1 algorithm verbatim: 'seven minutes' -> 420, 'twenty five seconds' -> 25, 'half an hour' -> 1800.
    Numbers reset unless they chain like 'twenty' + 'five', so the last number spoken wins (survives 'for'
    heard as 'four'). Additive: digit tokens count as numbers (typed input). 0 = no duration."""
    # Compound durations add up ("one minute and thirty seconds" -> 90, "an hour and a half" -> 5400,
    # "1 hour 30 minutes" -> 5400); within one unit the last number still wins ("four ten minutes" -> 600).
    number, prev, unit, article = 0.0, None, 60, False
    total, last_unit = 0.0, None
    for w in tokens:
        if w in ("a", "an"):
            article = True
        elif w in _V1_NUMBERS or w.isdigit():
            n = _V1_NUMBERS[w] if w in _V1_NUMBERS else float(w)
            if prev in _V1_TENS and 1 <= n <= 9 and w in _V1_NUMBERS:
                number += n
            elif w == "half" and number > 0:
                number += 0.5
            else:
                number = n
            prev = n
        elif w in DURATION_UNITS:
            unit = DURATION_UNITS[w]
            if number == 0 and article:
                number = 1
            if number:
                total += number * unit
                last_unit = unit
            number, prev, article = 0.0, None, False
    if total and number:                        # a trailing number after a unit
        if number == 0.5:
            total += 0.5 * last_unit            # "an hour and a half"
        else:
            total += number * {3600: 60, 60: 1}.get(last_unit, 1)   # "one minute thirty" -> 90
        number = 0.0
    if total:
        return int(round(total))
    if number == 0 and article:
        number = 1
    if number == 0.5 and unit == 1:
        number = 30
        unit = 1
    return int(round(number * unit))


def parse_percent(tokens: list[str]) -> int | None:
    """Last number group in 0..100; a 'to'/'two' directly before a number word is dropped
    ('set volume two forty' -> 40, 'set the volume to forty for set' -> 40)."""
    cleaned = []
    for i, w in enumerate(tokens):
        nxt = tokens[i + 1] if i + 1 < len(tokens) else ""
        if w in ("to", "two", "too") and _cls(nxt) in ("unit", "teen", "tens", "digit"):
            continue
        cleaned.append(w)
    groups = numbers_in(cleaned)
    if not groups:
        return None
    v = groups[-1][1]
    if 0 <= v <= 100:
        return int(round(v))
    return None


# ----------------------------------------------------------------------------- expressions
_OPS = sorted([
    (("plus",), "+"), (("+",), "+"), (("add",), "+"),
    (("minus",), "-"), (("-",), "-"),
    (("times",), "*"), (("x",), "*"), (("*",), "*"), (("multiplied", "by"), "*"), (("multiply", "by"), "*"),
    (("divided", "by"), "/"), (("divide", "by"), "/"), (("over",), "/"), (("/",), "/"),
    (("percent", "of"), "%of"), (("per", "cent", "of"), "%of"),
    (("squared",), "sq"), (("cubed",), "cube"),
    (("square", "root", "of"), "sqrt"), (("the", "square", "root", "of"), "sqrt"), (("square", "root"), "sqrt"),
], key=lambda o: -len(o[0]))
EXPR_WORDS = frozenset({"plus", "minus", "times", "x", "multiplied", "by", "divided", "over", "percent", "of",
                        "squared", "cubed", "square", "root", "point"})
UNDEFINED = float("nan")          # division by zero / sqrt of a negative -> "That's undefined{sir}."


def _lex_expr(tokens: list[str]) -> list[tuple[str, Any]] | None:
    out: list[tuple[str, Any]] = []
    i, n = 0, len(tokens)
    while i < n:
        for words, op in _OPS:
            if tuple(tokens[i:i + len(words)]) == words:
                out.append(("op", op))
                i += len(words)
                break
        else:
            w = tokens[i]
            after_op = not out or out[-1][0] == "op"
            if w == "for" and after_op and (not out or out[-1][1] not in ("sq", "cube")):
                out.append(("num", 4.0))                   # "twelve times for" (F5)
                i += 1
                continue
            hit = _scan_number(tokens, i)
            if hit is not None:
                out.append(("num", hit[0]))
                i = hit[1]
                continue
            if w in ("the", "equals", "equal", "is", "what's", "what"):
                i += 1
                continue
            return None
    return out


class _ExprParser:
    def __init__(self, lex):
        self.lex, self.i = lex, 0

    def peek(self):
        return self.lex[self.i] if self.i < len(self.lex) else (None, None)

    def take(self):
        tok = self.peek()
        self.i += 1
        return tok

    def expr(self):
        v = self.term()
        while self.peek() in (("op", "+"), ("op", "-")):
            op = self.take()[1]
            r = self.term()
            v = v + r if op == "+" else v - r
        return v

    def term(self):
        v = self.factor()
        while self.peek() in (("op", "*"), ("op", "/")):
            op = self.take()[1]
            r = self.factor()
            if op == "*":
                v = v * r
            else:
                v = UNDEFINED if r == 0 else v / r
        return v

    def factor(self):
        kind, val = self.peek()
        if (kind, val) == ("op", "sqrt"):
            self.take()
            r = self.factor()
            return UNDEFINED if r < 0 else math.sqrt(r)
        if (kind, val) == ("op", "-"):
            self.take()
            return -self.factor()
        if kind != "num":
            raise ValueError("number expected")
        self.take()
        v = val
        while True:
            k, op = self.peek()
            if (k, op) == ("op", "sq"):
                self.take()
                v = v * v
            elif (k, op) == ("op", "cube"):
                self.take()
                v = v ** 3
            elif (k, op) == ("op", "%of"):
                self.take()
                v = v / 100.0 * self.factor()
            else:
                return v


def parse_expr(tokens: list[str]) -> float | None:
    """Recursive descent (no eval): plus minus times 'multiplied by' 'divided by' over 'percent of' squared
    'square root of'; 'for' after an operator = 4. Division by zero -> UNDEFINED (nan)."""
    lex = _lex_expr([t for t in tokens if t and t != "[unk]"])
    if not lex or not any(k == "num" for k, _ in lex):
        return None
    p = _ExprParser(lex)
    try:
        v = p.expr()
    except (ValueError, OverflowError):
        return None
    if p.i != len(lex):
        return None
    return float(v)


def say_number(x: float) -> str:
    """'48', '3.5' (digits; SAPI reads them). nan -> 'undefined'."""
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "undefined"
    if isinstance(x, float) and math.isinf(x):
        return "infinity"
    if abs(x - round(x)) < 1e-9:
        return str(int(round(x)))
    return f"{x:.6f}".rstrip("0").rstrip(".")


# ----------------------------------------------------------------------------- slot types
@dataclass(frozen=True)
class SlotEnv:
    config: Any
    now: dt.datetime
    free_text: str | None = None
    app_index: Any = None


@dataclass(frozen=True)
class SlotType:
    name: str
    parse: Callable[[list[str], SlotEnv], Any | None]
    words: frozenset[str]
    free: bool
    doc: str


@dataclass(frozen=True)
class AppRef:
    spoken: str                 # what was said ("chrome", "task manager", or the Start-Menu name)
    alias: str | None           # config.apps key when it resolved through the Apps list
    target: str | None          # launch target (config value or .lnk path); None = unresolved
    # hearing rebuild (Sep 15 2026): a near miss the engine should ask about ("note hat" -> "Did you mean
    # Notepad?") instead of opening it or giving up; only set while target is None
    suggest: str | None = None
    suggest_target: str | None = None


# App near-miss band (hearing rebuild): difflib >= APP_AUTO opens; APP_ASK..APP_AUTO with APP_MARGIN over the
# runner-up asks; a "tie" - the spoken word is a longer word that merely contains the alias ("stream" /
# "steam", "world" / "word": an added letter makes a different real word, a dropped one ("crome") is a
# mishearing) - always asks, never opens. Spaces are ignored too ("note hat" / "notepad").
APP_AUTO, APP_ASK, APP_MARGIN = 0.75, 0.68, 0.10


def _app_ratio(spoken: str, alias: str) -> float:
    import difflib
    r = difflib.SequenceMatcher(None, spoken, alias).ratio()
    a, b = spoken.replace(" ", ""), alias.replace(" ", "")
    return max(r, difflib.SequenceMatcher(None, a, b).ratio())


def _app_tie(spoken: str, alias: str) -> bool:
    a, b = spoken.replace(" ", ""), alias.replace(" ", "")
    if a == b or len(a) <= len(b):
        return False
    it = iter(a)
    return all(ch in it for ch in b)              # the alias is a subsequence of the longer spoken word


def app_near_miss(spoken: str, aliases) -> tuple[str | None, str | None]:
    """(alias to open, alias to ask about) for a spoken name against the Apps-tab aliases."""
    scored = sorted(((_app_ratio(spoken, a.lower()), a) for a in aliases), key=lambda x: -x[0])
    if not scored:
        return None, None
    best_r, best = scored[0]
    runner = scored[1][0] if len(scored) > 1 else 0.0
    if best_r >= APP_AUTO:
        return (None, best) if _app_tie(spoken, best.lower()) else (best, None)
    if best_r >= APP_ASK and best_r - runner >= APP_MARGIN:
        return None, best
    return None, None


TIMER_NAMES = ("tea", "pasta", "coffee", "pizza", "egg", "laundry", "oven", "break", "work")

_NUM_1_99 = frozenset(k for k in (*UNIT_WORDS, *TEEN_WORDS, *TENS_WORDS) if k != "zero")
_DATE_WORDS_STATIC = frozenset({
    "today", "tonight", "tomorrow", "yesterday", "morning", "afternoon", "evening", "night", "noon", "midnight",
    "am", "pm", "at", "in", "on", "next", "this", "last", "the", "of", "a", "an", "and", "half", "quarter",
    "past", "to", "before", "after", "every", "daily", "weekly", "day", "days", "week", "weeks", "weekend",
    "month", "months", "hour", "hours", "minute", "minutes", "from", "now", "all", "o'clock",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
    "november", "december",
    "first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth", "tenth", "eleventh",
    "twelfth", "thirteenth", "fourteenth", "fifteenth", "sixteenth", "seventeenth", "eighteenth", "nineteenth",
    "twentieth", "thirtieth"}) | _NUM_1_99


def _date_words() -> frozenset[str]:
    words = set(_DATE_WORDS_STATIC)
    try:
        from xyrus import dates as _d          # T2; optional at import time
        for name in ("DATE_WORDS", "TIME_WORDS", "DATE_TOKENS"):
            words |= set(getattr(_d, name, ()) or ())
        words |= set(getattr(_d, "WEEKDAYS", ())) | set(getattr(_d, "MONTHS", ())) | set(getattr(_d, "ORDINALS", ()))
    except Exception:
        pass
    return frozenset(w for w in words if isinstance(w, str) and w and " " not in w)


def _p_duration(tokens: list[str], env: SlotEnv) -> int | None:
    has_value = any(t in DURATION_UNITS or t.isdigit() or (t in _V1_NUMBERS and t not in ("a", "an"))
                    for t in tokens)
    secs = parse_duration(tokens)
    return secs if has_value and secs > 0 else None


def _p_percent(tokens: list[str], env: SlotEnv) -> int | None:
    return parse_percent(tokens)


def _p_number(tokens: list[str], env: SlotEnv) -> float | None:
    return parse_number(tokens)


def _p_expr(tokens: list[str], env: SlotEnv) -> float | None:
    return parse_expr(tokens)


def _p_app(tokens: list[str], env: SlotEnv) -> AppRef | None:
    spoken = " ".join(tokens).strip()
    if not spoken:
        return None
    apps = {}
    if env.config is not None:
        try:
            apps = env.config.get("apps", {}) or {}
        except Exception:
            apps = {}
    for alias in sorted(apps, key=len, reverse=True):
        if N.phrase_in(spoken, alias.lower()):
            return AppRef(spoken, alias, apps[alias])
    stripped = " ".join(w for w in tokens if w not in N.APP_NOISE and w not in N.ARTICLES
                        and w not in N.FILLER_WORDS)
    # not an app: "kill the sound" is mute, "start a countdown for ten minutes" is a timer (meaning layer)
    words = stripped.split()
    if words and (all(w in N.NOT_APP_OBJECTS for w in words) or any(w in N.NOT_APP_ANY for w in words)):
        return None
    for alias in sorted(apps, key=len, reverse=True):
        if stripped and N.phrase_in(stripped, alias.lower()):
            return AppRef(spoken, alias, apps[alias])
    # v1 match_app ('the calculater' -> calculator), now with the ask band and the tie rule (app_near_miss)
    open_alias, ask_alias = app_near_miss(stripped or spoken, list(apps))
    if open_alias is not None:
        return AppRef(spoken, open_alias, apps[open_alias])
    if env.app_index is not None:
        try:
            hit = env.app_index.lookup(spoken)
        except Exception:
            log.exception("app index lookup failed")
            hit = None
        if hit:
            return AppRef(hit[0], None, hit[1])
    if ask_alias is not None:
        return AppRef(spoken, None, None, suggest=ask_alias, suggest_target=apps[ask_alias])
    return AppRef(spoken, None, None)


# Whisper writes the whole request: "play me shape of you", "can you please put on faded" - after the exact
# "play <song>" pattern took everything behind "play", these carrier words are not part of the title.
# "you" only inside a carrier: titles start with it ("You Raise Me Up" was searched as "raise me up", Sep 15)
_SONG_CARRIERS = ("want to hear ", "listen to ", "put on ", "please ", "me ", "you put on ", "you play ",
                  "you please ", "to ")


def _p_song(tokens: list[str], env: SlotEnv) -> str | None:
    t = N.clean_song_title(" ".join(tokens))
    changed = True
    while changed:
        changed = False
        for pre in _SONG_CARRIERS:
            if t.startswith(pre) and len(t) > len(pre):
                t, changed = t[len(pre):].strip(), True
    if not t or t in N.SONG_ASK or (t.startswith("me ") and t[3:] in N.SONG_ASK):
        return None
    return t


def _p_query(tokens: list[str], env: SlotEnv) -> str | None:
    t = " ".join(w for w in tokens if w != "[unk]").strip()
    for suf in (" please", " for me", " sir"):
        if t.endswith(suf):
            t = t[: -len(suf)].strip()
    return t or None


def _p_text(tokens: list[str], env: SlotEnv) -> str | None:
    t = " ".join(w for w in tokens if w != "[unk]").strip()
    return t or None


def _waking(env: SlotEnv):
    try:
        wh = env.config.get("calendar.waking_hours") if env.config is not None else None
        return tuple(wh) if wh else None
    except Exception:
        return None


def _p_when(tokens: list[str], env: SlotEnv):
    from xyrus import dates                    # T2 (lazy)
    text = " ".join(tokens)
    waking = _waking(env)
    if waking:
        try:
            return dates.parse_when(text, env.now, waking=waking)
        except TypeError:
            pass
    return dates.parse_when(text, env.now)


def _p_day(tokens: list[str], env: SlotEnv):
    from xyrus import dates                    # T2 (lazy)
    return dates.parse_range(" ".join(tokens), env.now)


def _p_name(tokens: list[str], env: SlotEnv) -> str | None:
    t = " ".join(tokens).strip()
    return t if t in TIMER_NAMES else None


_DATE_WORDS = _date_words()

SLOT_TYPES: dict[str, SlotType] = {
    "duration": SlotType("duration", _p_duration,
                         _NUM_1_99 | {"hundred", "a", "an", "and", "half", *DURATION_UNITS}, False,
                         "a length of time: seven minutes, twenty five seconds, half an hour"),
    "percent": SlotType("percent", _p_percent, _NUM_1_99 | {"zero", "percent", "hundred"}, False,
                        "a level from 0 to 100: forty percent, seventy"),
    "number": SlotType("number", _p_number,
                       _NUM_1_99 | {"zero", "point", "hundred", "thousand", "million"}, False,
                       "a number: seven, two hundred"),
    "expr": SlotType("expr", _p_expr,
                     _NUM_1_99 | {"zero", "hundred", "thousand", "million"} | EXPR_WORDS, False,
                     "a sum: twelve times four, fifteen percent of eighty"),
    "app": SlotType("app", _p_app, frozenset(), True,
                    "an app or place from the Apps tab (or anything in the Start menu)"),
    "song": SlotType("song", _p_song, frozenset(), True,
                     "any song: alone, shape of you, faded by alan walker"),
    "query": SlotType("query", _p_query, frozenset(), True, "anything to search for: cheap mechanical keyboards"),
    "text": SlotType("text", _p_text, frozenset(), True, "free text"),
    "when": SlotType("when", _p_when, _DATE_WORDS, False,
                     "a date or time: tomorrow at three pm, next friday, in twenty minutes"),
    "day": SlotType("day", _p_day,
                    _DATE_WORDS | {"few", "couple", "several", "next", "these", "rest", "day", "days", "week",
                                   "weeks", "month"}, False,
                    "a day or span: today, on friday, this week, these three days, the next ten days, "
                    "rest of the week"),
    "name": SlotType("name", _p_name, frozenset(TIMER_NAMES), False,
                     "a timer name: " + ", ".join(TIMER_NAMES)),
}
