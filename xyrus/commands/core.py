"""Basics (§3.1) and small talk (§3.9): help, repeat, what did you hear, stop talking, pause listening."""
from __future__ import annotations

import logging
import threading

from xyrus import normalize as N
from xyrus import persona, replies as R
from xyrus.commands import say
from xyrus.registry import command

log = logging.getLogger("xyrus.commands.core")
SECTION = "Basics"


@command("help", "what can you do", "help", "what are your commands", "show me the commands",
         "show the commands", "list your commands",
         section=SECTION, help="opens this list of everything I can do")
def help_(ctx, m):
    ctx.ui("show:Commands")
    say(ctx, R.HELP)


@command("repeat", "repeat", "repeat that", "say that again", section=SECTION, help="says my last reply again")
def repeat(ctx, m):
    last = (getattr(ctx.engine, "last_reply", "") or "").strip()
    if last:
        ctx.say(last)
    else:
        say(ctx, R.REPEAT_NOTHING)


def _previous_heard(ctx) -> str:
    """The utterance before this one ('what did you hear' itself is engine.last_heard by now)."""
    # SPEC-AMBIGUITY: §3.1 wants "the previous heard text"; Engine.last_heard already holds the current
    # utterance when the handler runs, so the previous one comes from the Activity events (kind "heard").
    prev = getattr(ctx.engine, "prev_heard", None)
    if prev:
        return prev
    try:
        heard = [e.text for e in ctx.engine.snapshot().events if e.kind == "heard"]
    except Exception:
        heard = []
    return heard[-2] if len(heard) >= 2 else ""


@command("what_did_you_hear", "what did you hear", section=SECTION, help="repeats what I heard before this")
def what_did_you_hear(ctx, m):
    prev = _previous_heard(ctx)
    had, rest = N.strip_wake(prev, ctx.config.get("wake_words"))
    rest = " ".join(w for w in (rest if had else prev).split() if w != "[unk]")
    if rest:
        say(ctx, R.HEARD, text=rest)
    else:
        say(ctx, R.HEARD_NOTHING)


def stop_everything(ctx) -> None:
    """Stop speech, clear the say-queue and silence a ringing timer (§3.1 'Stop talking')."""
    for obj, meth in ((getattr(ctx.engine, "speaker", None), "stop"), (getattr(ctx.engine, "chime", None), "stop")):
        try:
            if obj is not None:
                getattr(obj, meth)()
        except Exception:
            log.exception("stop failed")
    try:
        if ctx.timers is not None and ctx.timers.ringing():
            ctx.timers.stop_ringing()
    except Exception:
        log.exception("timer stop_ringing failed")


@command("stop_talking", "stop", "quiet", "be quiet", "stop talking", section=SECTION,
         help="stops talking (and a ringing timer)", armed_ok_single_word=False, priority=1)
def stop_talking(ctx, m):
    stop_everything(ctx)
    ctx.event("info", "stopped talking")


@command("pause_listening", "stop listening", "pause listening", section=SECTION,
         help="turns the microphone off; resume from the window, tray or hotkey")
def pause_listening(ctx, m):
    say(ctx, R.PAUSING, then=lambda: ctx.engine.set_paused(True))


def _schedule(seconds: float, fn) -> threading.Timer:
    """Wall-time one-shot (patched in tests). The callback only posts to the engine thread."""
    t = threading.Timer(seconds, fn)
    t.daemon = True
    t.start()
    return t


@command("pause_listening_for", "stop listening for <duration>", "pause listening for <duration>",
         section=SECTION, help="turns the microphone off for a while, then back on")
def pause_listening_for(ctx, m):
    secs = int(m.slots["duration"])
    engine = ctx.engine

    def resume():
        def on_engine():
            engine.set_paused(False)
            say(ctx, R.BACK)
        engine.post(on_engine)

    def pause():
        engine.set_paused(True)
        _schedule(secs, resume)

    say(ctx, R.MIC_OFF_FOR, duration=persona.speak_duration(secs), then=pause)


# ----------------------------------------------------------------------------- small talk (§3.9)
@command("thanks", "thank you", "thanks", "thanks a lot", section=SECTION, help="you're welcome",
         follow_up=True)
def thanks(ctx, m):
    ctx.say(persona.pick("thanks", R.THANKS, ctx.config))


@command("who_are_you", "who are you", "what's your name", "what are you", section=SECTION,
         help="who I am")
def who_are_you(ctx, m):
    say(ctx, R.WHO_ARE_YOU)


@command("how_are_you", "how are you", "how are you doing", section=SECTION, help="how I'm doing")
def how_are_you(ctx, m):
    ctx.do(ctx.actions.system_status, then=lambda st: say(ctx, R.HOW_ARE_YOU, cpu=st.get("cpu", 0)))


@command("hello", "hello", "hi", section=SECTION, help="says hello and waits for a command")
def hello(ctx, m):
    say(ctx, R.HELLO)
    ctx.arm()


@command("are_you_there", "are you there", "you there", section=SECTION, help="checks I'm listening")
def are_you_there(ctx, m):
    say(ctx, R.ARE_YOU_THERE)
