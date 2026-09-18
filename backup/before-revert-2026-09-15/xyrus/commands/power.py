"""Power (§3.2): shutdown / restart (confirm, countdown follow-up, delayed), cancel, sleep, hibernate, lock,
sign out, screen off/on, keep awake."""
from __future__ import annotations

import datetime as dt
import weakref

from xyrus import normalize as N
from xyrus import persona, replies as R
from xyrus.commands import say, text
from xyrus.registry import command

SECTION = "Power"
FOLLOW_UP_MAX_S = 120                   # a countdown longer than this gets no no-wake follow-up window

# per engine: {"kind": "shutdown"|"restart", "at": datetime, "until": mono deadline}
_countdowns: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()


def _state(engine) -> dict | None:
    try:
        st = _countdowns.get(engine)
    except TypeError:
        return None
    if not st:
        return None
    clock = getattr(engine, "clock", None)
    if clock is not None and clock.mono() > st["until"]:
        _countdowns.pop(engine, None)
        return None
    return st


def countdown_active(engine) -> bool:
    """Context predicate: a shutdown/restart is pending (no wake word needed to cancel it)."""
    return _state(engine) is not None


def _set_countdown(ctx, kind: str, delay_s: int) -> None:
    try:
        _countdowns[ctx.engine] = {"kind": kind, "at": ctx.clock.now() + dt.timedelta(seconds=delay_s),
                                   "until": ctx.clock.mono() + delay_s + 5}
    except TypeError:
        pass


def _clear_countdown(ctx) -> None:
    try:
        _countdowns.pop(ctx.engine, None)
    except TypeError:
        pass


def _needs_confirm(ctx, m) -> bool:
    return bool(ctx.config.get("confirm_shutdown", True)) or bool(getattr(m, "has_unk", False))


def _power_off(ctx, m, kind: str, delay: int | None = None) -> None:
    """kind = shutdown | restart; delay None = config.shutdown_delay_s with the spoken countdown."""
    delay_s = int(delay if delay is not None else ctx.config.get("shutdown_delay_s", 10))

    def done(_):
        _set_countdown(ctx, kind, delay_s)
        if delay is None:
            say(ctx, R.SHUTTING_DOWN if kind == "shutdown" else R.RESTARTING, seconds=delay_s)
        else:
            at = persona.speak_time(ctx.clock.now() + dt.timedelta(seconds=delay_s))
            say(ctx, R.SHUTDOWN_AT if kind == "shutdown" else R.RESTART_AT, time=at)
        if delay_s <= FOLLOW_UP_MAX_S:
            ctx.follow_up("shutdown", ("cancel_shutdown", "shutdown_stop"), seconds=delay_s + 5)

    def fire():
        ctx.do(getattr(ctx.actions, kind), delay_s, then=done)

    if not _needs_confirm(ctx, m):
        fire()
        return
    if delay is None:
        q = text(ctx, R.CONFIRM_SHUTDOWN if kind == "shutdown" else R.CONFIRM_RESTART)
    else:
        q = text(ctx, R.CONFIRM_SHUTDOWN_IN if kind == "shutdown" else R.CONFIRM_RESTART_IN,
                 duration=persona.speak_duration(delay_s))
    ctx.confirm(q, fire)


@command("shutdown", "shut down", "shutdown", "power off", "turn off the pc", "turn off the computer",
         "shut down the pc", "shut down the computer", "shut down now", "shut down the system",
         "power off the pc", "turn the pc off", "turn the computer off",
         section=SECTION, help="asks you to confirm, then shuts down in 10 s", destructive=True)
def shutdown(ctx, m):
    _power_off(ctx, m, "shutdown")


@command("restart", "restart", "reboot", "restart the pc", "restart the computer",
         section=SECTION, help="asks you to confirm, then restarts in 10 s", destructive=True)
def restart(ctx, m):
    _power_off(ctx, m, "restart")


@command("shutdown_in", "shut down in <duration>", "shutdown in <duration>", section=SECTION,
         help="shuts down later, e.g. in thirty minutes", destructive=True)
def shutdown_in(ctx, m):
    _power_off(ctx, m, "shutdown", int(m.slots["duration"]))


@command("restart_in", "restart in <duration>", section=SECTION, help="restarts later", destructive=True)
def restart_in(ctx, m):
    _power_off(ctx, m, "restart", int(m.slots["duration"]))


def _abort(ctx) -> None:
    def done(_):
        _clear_countdown(ctx)
        say(ctx, R.SHUTDOWN_CANCELLED)
    ctx.do(ctx.actions.abort_shutdown, then=done)


@command("cancel_shutdown", "cancel", "abort", "cancel shutdown", "cancel the shutdown", "stop the shutdown",
         "cancel the restart", section=SECTION, help="stops a pending shutdown or restart",
         follow_up=True, wake_free=True, context=countdown_active,
         wake_free_note=" — works without the wake word during a countdown")
def cancel_shutdown(ctx, m):
    _abort(ctx)


@command("shutdown_stop", "stop", section=SECTION,
         help="during a shutdown countdown: stops it (otherwise: stop talking)",
         follow_up=True, wake_free=True, context=countdown_active, priority=-1,
         armed_ok_single_word=False, wake_free_note=" — works without the wake word during a countdown")
def shutdown_stop(ctx, m):
    if countdown_active(ctx.engine):
        _abort(ctx)
    else:
        from xyrus.commands.core import stop_everything
        stop_everything(ctx)


@command("when_is_shutdown", "when is the shutdown", "when is the restart", section=SECTION,
         help="when a planned shutdown happens")
def when_is_shutdown(ctx, m):
    st = _state(ctx.engine)
    if st:
        say(ctx, R.SHUTDOWN_WHEN, time=persona.speak_time(st["at"]))
    else:
        say(ctx, R.NO_SHUTDOWN)


_POWER_OBJECT_OK = frozenset({"lock", "sleep", "hibernate", "mode", "put", "up", "now"}) | N.PC_WORDS | N.SCREEN_WORDS


def _stray_object(m) -> bool:
    """True when the utterance names something that isn't the PC/screen ("lock the door", "sleep the dog"):
    a 1-word power pattern must not ride on extra words the registry tolerates (look-alike safety)."""
    toks = [t for t in N.canonical(getattr(m, "text", "") or "").split() if not t.startswith("@")]
    return any(t not in _POWER_OBJECT_OK for t in toks)


@command("sleep", "go to sleep", "sleep", "sleep mode", section=SECTION, help="puts the PC to sleep")
def sleep(ctx, m):
    if _stray_object(m):
        say(ctx, R.NOT_HEARD)
        return
    say(ctx, R.SLEEPING, then=lambda: ctx.do(ctx.actions.sleep))


@command("hibernate", "hibernate", section=SECTION, help="hibernates the PC")
def hibernate(ctx, m):
    if _stray_object(m):
        say(ctx, R.NOT_HEARD)
        return

    def after(ok):
        if ok is False:
            say(ctx, R.HIBERNATE_OFF)
    say(ctx, R.HIBERNATING, then=lambda: ctx.do(ctx.actions.hibernate, then=after))


@command("lock", "lock", "lock screen", "lock the pc", "lock the computer", section=SECTION,
         help="locks Windows")
def lock(ctx, m):
    if _stray_object(m):
        say(ctx, R.NOT_HEARD)
        return
    say(ctx, R.LOCKING, then=lambda: ctx.do(ctx.actions.lock))


@command("sign_out", "sign out", "log out", "log off", section=SECTION, help="asks, then signs you out",
         destructive=True)
def sign_out(ctx, m):
    def fire():
        say(ctx, R.SIGNING_OUT, then=lambda: ctx.do(ctx.actions.sign_out))
    if _needs_confirm(ctx, m):
        ctx.confirm(text(ctx, R.CONFIRM_SIGN_OUT), fire)
    else:
        fire()


@command("screen_off", "screen off", "display off", "monitor off", "turn off the screen", "turn the screen off",
         "turn off the display", "turn off the monitor", "turn the monitor off", section=SECTION,
         help="turns the display off; the PC keeps running")
def screen_off(ctx, m):
    say(ctx, R.SCREEN_OFF, then=lambda: ctx.do(ctx.actions.screen_off))


@command("screen_on", "wake up", "screen on", "display on", "turn on the screen", section=SECTION,
         help="turns the display back on")
def screen_on(ctx, m):
    ctx.do(ctx.actions.screen_on, then=lambda _: say(ctx, R.SCREEN_ON))


@command("keep_awake", "keep the pc awake", "stay awake", section=SECTION, help="stops the PC from sleeping")
def keep_awake(ctx, m):
    ctx.do(ctx.actions.keep_awake, True, then=lambda _: say(ctx, R.KEEP_AWAKE))


@command("let_sleep", "let it sleep", section=SECTION, help="allows sleep again")
def let_sleep(ctx, m):
    ctx.do(ctx.actions.keep_awake, False, then=lambda _: say(ctx, R.LET_SLEEP))
