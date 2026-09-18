"""Display (§3.4): brightness, the Xyrus window's light/dark appearance, and the Windows-wide theme.

User request (2026-09-13): bare "dark mode" / "light mode" (and synonyms) switch the XYRUS WINDOW via
ctx.ui("theme:dark|light|system") (T5 handles it, config ui.appearance); the Windows-wide theme moved to
"windows dark mode" / "windows light mode" (set_theme)."""
from __future__ import annotations

from xyrus import normalize as N
from xyrus import replies as R
from xyrus.commands import say
from xyrus.registry import command

SECTION = "Display & windows"
BRIGHTNESS_TIMEOUT = 4.0      # DDC/CI takes 100-500 ms
STEP, SMALL_STEP, BIG_STEP = 15, 5, 25
DIM_LEVEL = 20
FOLLOWING_WINDOWS = "Following Windows{sir}."
WINDOWS_DARK = "Windows is in dark mode{sir}."
WINDOWS_LIGHT = "Windows is in light mode{sir}."


def _step(m) -> int:
    kind = N.step_modifier(m.text.split())
    return SMALL_STEP if kind == "small" else BIG_STEP if kind == "large" else STEP


def _set(ctx, value, reply: bool) -> None:
    def done(level):
        if level is None:
            say(ctx, R.NO_BRIGHTNESS)
        elif reply:
            say(ctx, R.BRIGHTNESS_AT, pct=level if isinstance(value, str) else value)
    ctx.do(ctx.actions.brightness_set, value, then=done, timeout=BRIGHTNESS_TIMEOUT)


@command("set_brightness", "set [the] brightness to <percent>", "brightness <percent>", section=SECTION,
         help="sets the screen brightness")
def set_brightness(ctx, m):
    _set(ctx, int(m.slots["percent"]), reply=True)


@command("brighter", "brightness up", "brighter", "increase the brightness", "increase brightness",
         "raise the brightness", "turn up the brightness", "turn the brightness up", "make the screen brighter",
         "more brightness", section=SECTION, help="brightness +15 % (a bit: +5 %, a lot: +25 %)")
def brighter(ctx, m):
    _set(ctx, f"+{_step(m)}", reply=False)


@command("dimmer", "brightness down", "dimmer", "decrease the brightness", "decrease brightness",
         "lower the brightness", "lower brightness", "reduce the brightness", "turn down the brightness",
         "turn the brightness down", "make the screen darker", "less brightness", section=SECTION,
         help="brightness -15 % (a bit: -5 %, a lot: -25 %)")
def dimmer(ctx, m):
    _set(ctx, f"-{_step(m)}", reply=False)


@command("dim_screen", "dim the screen", section=SECTION, help="brightness to 20 %")
def dim_screen(ctx, m):
    _set(ctx, DIM_LEVEL, reply=False)


@command("max_brightness", "brightness max", "max brightness", "maximum brightness", "full brightness",
         section=SECTION, help="brightness to 100 %")
def max_brightness(ctx, m):
    _set(ctx, 100, reply=True)


@command("query_brightness", "what's the brightness", "what is the brightness", "brightness level",
         section=SECTION, help="says the brightness")
def query_brightness(ctx, m):
    def done(level):
        say(ctx, R.NO_BRIGHTNESS if level is None else R.BRIGHTNESS_IS, pct=level)
    ctx.do(ctx.actions.brightness_get, then=done, timeout=BRIGHTNESS_TIMEOUT)


# ----------------------------------------------------------------------------- Xyrus window appearance
@command("app_dark", "dark mode", "night mode", "dark theme", "turn on dark mode", "switch to dark mode",
         "use dark mode", "make the app dark", "make it dark", section=SECTION,
         help="Xyrus's window in dark colours")
def app_dark(ctx, m):
    ctx.ui("theme:dark")
    say(ctx, R.DARK_MODE)


@command("app_light", "light mode", "white mode", "light theme", "turn on light mode", "switch to light mode",
         "use light mode", "make the app white", "make the app light", "make it white", "make it light",
         section=SECTION, help="Xyrus's window in light colours")
def app_light(ctx, m):
    ctx.ui("theme:light")
    say(ctx, R.LIGHT_MODE)


@command("app_follow_windows", "follow windows theme", "follow the windows theme", "use the windows theme",
         "match windows theme", "same theme as windows", section=SECTION,
         help="Xyrus's window follows the Windows light/dark setting")
def app_follow_windows(ctx, m):
    ctx.ui("theme:system")
    say(ctx, FOLLOWING_WINDOWS)


# ----------------------------------------------------------------------------- Windows-wide theme
@command("windows_dark", "windows dark mode", "switch windows to dark mode", "make windows dark",
         "set windows to dark mode", "turn on windows dark mode", section=SECTION,
         help="switches all of Windows to dark mode")
def windows_dark(ctx, m):
    ctx.do(ctx.actions.set_theme, True, then=lambda _: say(ctx, WINDOWS_DARK))


@command("windows_light", "windows light mode", "switch windows to light mode", "make windows light",
         "set windows to light mode", "turn on windows light mode", section=SECTION,
         help="switches all of Windows to light mode")
def windows_light(ctx, m):
    ctx.do(ctx.actions.set_theme, False, then=lambda _: say(ctx, WINDOWS_LIGHT))
