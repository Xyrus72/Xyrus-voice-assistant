"""Timers by voice (SPEC §3.8). Handlers run on T-engine and only touch ctx.timers (TimerService, in-memory,
clock-driven, T1). Firing ("Time's up, sir!", chime, toast, screen on, 60 s follow-up) is done by the engine's
tick; the follow-up answers ('stop', 'okay', 'thanks', 'snooze') are the `dismiss` / `snooze` commands in
commands/calendar.py, shared with calendar alerts.
"""
from __future__ import annotations

from xyrus import persona as P
from xyrus import replies as RP
from xyrus.parsing import TIMER_NAMES
from xyrus.registry import command

SECTION = "Timers"


def _say(ctx, template: str, **kw) -> None:
    ctx.say(P.render(template, ctx.config, **kw))


def _timer_name(text: str) -> str | None:
    return next((w for w in text.split() if w in TIMER_NAMES), None)


def _start(ctx, secs: int, name: str | None = None) -> None:
    ctx.timers.add(secs, name=name)
    if name:
        _say(ctx, RP.NAMED_TIMER_SET, Name=name.capitalize(), duration=P.speak_duration(secs))
    else:
        _say(ctx, RP.TIMER_SET, duration=P.speak_duration(secs))
    ctx.event("info", f"timer {name + ' ' if name else ''}{secs} s")


@command("set_timer", "set a timer for <duration>", "set timer for <duration>", "set timer <duration>",
         "timer for <duration>", "timer <duration>", "start a timer for <duration>", "<duration> timer",
         section=SECTION, help="starts a timer; several can run at once")
def set_timer(ctx, m):
    _start(ctx, m.slots["duration"], _timer_name(m.text))


@command("named_timer", "<name> timer <duration>", "set a <name> timer for <duration>",
         section=SECTION, help="a named timer: tea, pasta, coffee, pizza, egg, laundry, oven, break, work")
def named_timer(ctx, m):
    _start(ctx, m.slots["duration"], m.slots["name"])


@command("timer_missing", "set a timer", "timer", "start a timer",
         section=SECTION, help="asks for how long")
def timer_missing(ctx, m):
    name = _timer_name(m.text)

    def answered(secs):
        if secs:
            _start(ctx, int(secs), name)

    ctx.ask(P.render(RP.TIMER_ASK, ctx.config), "duration", answered, listen="when", hint=RP.HINT_DURATION)


def _label(v) -> str:
    return v.name.capitalize() if v.name in TIMER_NAMES else f"The {v.name} timer"


@command("time_left", "how long is left", "how much time is left", "time left", "how long on the timer",
         section=SECTION, help="how long the timers have left")
def time_left(ctx, m):
    views = [v for v in ctx.timers.views() if not v.ringing]
    if not views:
        _say(ctx, RP.NO_TIMERS)
    elif len(views) == 1:
        _say(ctx, RP.TIMER_LEFT, left=P.speak_left(views[0].remaining_s).capitalize())
    else:
        ctx.say(" ".join(P.render(RP.TIMER_LEFT_ITEM, ctx.config, Name=_label(v), left=P.speak_left(v.remaining_s))
                         for v in sorted(views, key=lambda v: v.remaining_s)))


@command("add_time", "add <duration>", "add <duration> to the timer",
         section=SECTION, help="adds time to the soonest timer")
def add_time(ctx, m):
    secs = m.slots["duration"]
    if ctx.timers.add_time(secs) is None:
        _say(ctx, RP.NO_TIMER)
    else:
        _say(ctx, RP.TIMER_ADDED, duration=P.speak_duration(secs))


def _timer_context(engine) -> bool:
    timers = getattr(engine, "timers", None)
    return bool(timers is not None and (timers.any() or timers.ringing()))


@command("cancel_timer", "cancel timer", "stop timer", "cancel the timer", "stop the timer", "cancel all timers",
         "cancel the <name> timer",
         section=SECTION, help="cancels a timer", wake_free=True, context=_timer_context,
         wake_free_note=" — works without the wake word while a timer runs")
def cancel_timer(ctx, m):
    everything = "all" in m.text.split() or "timers" in m.text.split()
    ringing = ctx.timers.ringing()
    if ringing:
        ctx.timers.stop_ringing()
    n = ctx.timers.cancel(name=m.slots.get("name"), all=everything)
    if everything and n > 1:
        _say(ctx, RP.TIMERS_CANCELLED)
    elif n or ringing:
        _say(ctx, RP.TIMER_CANCELLED)
    else:
        _say(ctx, RP.NO_TIMER)
