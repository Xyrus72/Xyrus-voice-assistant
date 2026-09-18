"""Briefing, good night, remember / recall / forget, your name (SPEC §3.9). Handlers run on T-engine.
The spoken summaries come from xyrus.assistant (v1 wording, D14)."""
from __future__ import annotations

import datetime as dt

from xyrus import assistant as A
from xyrus import persona as P
from xyrus import replies as RP
from xyrus.commands import calendar as cal
from xyrus.registry import command

SECTION = "Assistant"
MATCH_MEMORY = 8
LOW = cal.LOW
GREETING_FOLLOW_UP = ("what's my day like", "whats_next", "thanks")      # engine opens it after the startup greeting
FOLLOW_UP_BRIEFING = ("and_after_that", "hear_notes", "thanks")
FOLLOW_UP_RECALL = ("recall_more", "thanks")
RECALL_WORDS = {"what", "did", "tell", "list", "read", "recall"}
REMEMBER_LEADS = (("keep", "in", "mind", "that"), ("keep", "in", "mind"), ("remembered", "that"),
                  ("remember", "that"), ("remember", "this"), ("remembered",), ("remember",))
PAGE = 3


def _say(ctx, template: str, **kw) -> None:
    ctx.say(P.render(template, ctx.config, **kw))


@command("briefing", "good morning", "good afternoon", "good evening", "brief me", "briefing", "daily briefing",
         "what's my day like",
         section=SECTION, help="today's plan (part of day from the clock); also automatic once a morning",
         follow_up=True)
def briefing(ctx, m):
    now = ctx.clock.now()
    ctx.say(A.briefing(ctx.store, now, ctx.config))
    today = now.date()
    state = cal._engine_state(ctx, "notes")
    state.clear()
    state.update(start=today, end=today, label="today")
    cal.set_next_state(ctx, dt.datetime.combine(today + dt.timedelta(days=1), dt.time.min))
    cal._follow_up(ctx, "briefing", FOLLOW_UP_BRIEFING)


@command("good_night", "good night", "goodnight", "going to bed",
         section=SECTION, help="says what tomorrow starts with")
def good_night(ctx, m):
    ctx.say(A.good_night(ctx.clock.now(), ctx.store, ctx.config))


# ----------------------------------------------------------------------------- memory
def _m_remember(tokens):
    t = [w for w in tokens if w != "[unk]"]
    if not t or "memories" in t:
        return None
    if t[0] in ("remember", "remembered") or t[:3] == ["keep", "in", "mind"]:
        if len(t) > 1 and t[1] in RECALL_WORDS:
            return None
        return {}
    return None


def _m_recall(tokens):
    t = [w for w in tokens if w != "[unk]"]
    if not t or t[0] in ("remember", "keep"):
        return None
    if ({"remember", "remembered", "memories"} & set(t)) and (RECALL_WORDS & set(t)):
        return {}
    return None


def _memory_text(ctx, m) -> str:
    words = cal.utterance(ctx, m).split()
    for lead in REMEMBER_LEADS:
        if tuple(words[:len(lead)]) == lead:
            words = words[len(lead):]
            break
    while words and words[0] in ("that", "to", "[unk]"):
        words = words[1:]
    return " ".join(w for w in words if w != "[unk]").strip()


def _confirm_memory(ctx, text: str) -> None:
    text = (text or "").strip()
    if not text:
        return

    def save():
        ctx.memory.add(text, "voice")
        _say(ctx, RP.REMEMBERED)

    ctx.confirm(P.render(RP.MEMORY_CORRECT, ctx.config, text=text), save)


@command("remember", "remember that", "remember this", "remember", "keep in mind",
         section=SECTION, help="keeps a note of anything you tell it (asks you to confirm)",
         matcher=_m_remember, matcher_score=MATCH_MEMORY, free_trigger_words=("remember", "remembered", "keep in mind"))
def remember(ctx, m):
    text = _memory_text(ctx, m)
    if text:
        _confirm_memory(ctx, text)
    else:
        ctx.ask(RP.Q_MEMORY, "text", lambda ans: _confirm_memory(ctx, str(ans or "")), listen="free",
                hint=RP.HINT_MEMORY)


def _read_page(ctx, offset: int) -> None:
    total = ctx.memory.count()
    mems = ctx.memory.newest(offset, PAGE)
    ctx.say(A.describe_memories(mems, total, offset, ctx.config))
    state = cal._engine_state(ctx, "recall")
    state.clear()
    state.update(offset=offset + len(mems))
    if offset + len(mems) < total:
        cal._follow_up(ctx, "recall", FOLLOW_UP_RECALL)


@command("recall", "what did i ask you to remember", "what do you remember", "read my memories", "what did i tell you",
         section=SECTION, help="reads what you asked it to remember, three at a time (then 'more')",
         matcher=_m_recall, matcher_score=MATCH_MEMORY)
def recall(ctx, m):
    _read_page(ctx, 0)


@command("recall_more", "more", "the rest", "next ones",
         section=SECTION, help="after recall: the next three", follow_up=True, priority=LOW)
def recall_more(ctx, m):
    state = cal._engine_state(ctx, "recall")
    _read_page(ctx, int(state.get("offset", 0)) if state else 0)


@command("forget", "forget that", "forget the last one", "forget everything",
         section=SECTION, help="asks, then forgets the last thing (or everything) you asked it to remember")
def forget(ctx, m):
    n = ctx.memory.count()
    if "everything" in m.text.split() and n > 1:
        if not n:
            _say(ctx, "There's nothing to forget{sir}.")
            return

        def wipe():
            ctx.memory.clear()
            _say(ctx, RP.FORGOTTEN)

        ctx.confirm(f"Forget all {A.number_word(n)} thing{'s' if n != 1 else ''}{A.sir(ctx.config)}? Yes or no.", wipe)
        return
    last = ctx.memory.newest(0, 1)
    if not last:
        _say(ctx, "There's nothing to forget{sir}.")
        return
    mem = last[0]

    def drop():
        ctx.memory.delete(mem["id"])
        _say(ctx, RP.FORGOTTEN)

    ctx.confirm(f"Forget '{mem['text']}'? Yes or no.", drop)


@command("my_name", "what's my name",
         section=SECTION, help="the name from Settings")
def my_name(ctx, m):
    name = str(A.cfg_get(ctx.config, "user_name", "") or "").strip()
    if name:
        _say(ctx, RP.YOUR_NAME, name=name)
    else:
        _say(ctx, RP.NO_NAME)
