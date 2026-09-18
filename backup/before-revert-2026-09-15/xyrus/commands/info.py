"""Info and tools (§3.7): time, date, status, battery, disks, CPU users, screenshots, calculator, weather."""
from __future__ import annotations

import math

from xyrus import parsing, persona, replies as R
from xyrus.commands import join_and, nice_name, say
from xyrus.registry import command

SECTION = "Info & tools"
WEATHER_TIMEOUT = 9.0                   # wttr.in: 3 s observed, action timeout 8 s (F11)
RAIN_LIKELY = "Probably{sir}. There's a {chance} percent chance of rain today."
RAIN_UNLIKELY = "Probably not{sir}. The chance of rain today is {chance} percent."
NEARLY_FULL_ONE = "{drive} is nearly full."


@command("time", "what time is it", "what's the time", "what is the time", "the time", "time",
         section=SECTION, help="says the time")
def time_(ctx, m):
    say(ctx, R.TIME_IS, time=persona.speak_time(ctx.clock.now()))


@command("date", "what day is it", "what's the date", "what is the date", "the date", "what's today",
         section=SECTION, help="says the date")
def date_(ctx, m):
    now = ctx.clock.now()
    say(ctx, R.DATE_IS, date=f"{now:%A}, {now:%B} {now.day}")


def _uptime(seconds: int) -> str:
    s = max(0, int(seconds))
    d, rem = divmod(s, 86400)
    h, rem = divmod(rem, 3600)
    mins = rem // 60

    def unit(n, w):
        return f"{n} {w}{'s' if n != 1 else ''}"
    if d:
        return unit(d, "day") + (f" {unit(h, 'hour')}" if h else "")
    if h:
        return unit(h, "hour") + (f" {unit(mins, 'minute')}" if mins else "")
    return unit(mins, "minute")


def _battery_phrase(bat) -> str:
    if not bat:
        return "no battery"
    return f"battery at {bat['pct']} percent" + (", charging" if bat.get("plugged") else "")


@command("system_status", "system status", "status", "system report", "check the cpu", "cpu usage",
         "how much memory is used", "memory usage", "how is the computer doing", section=SECTION,
         help="CPU, memory, battery and uptime")
def system_status(ctx, m):
    def done(st):
        say(ctx, R.STATUS, cpu=st["cpu"], mem=st["mem"], battery=_battery_phrase(st.get("battery")),
            uptime=_uptime(st.get("uptime_s", 0)))
    ctx.do(ctx.actions.system_status, then=done)


@command("battery", "battery", "battery level", "how much battery", section=SECTION, help="battery level")
def battery(ctx, m):
    def done(st):
        bat = st.get("battery")
        if not bat:
            say(ctx, R.NO_BATTERY)
        else:
            say(ctx, R.BATTERY, pct=bat["pct"], charging=", charging" if bat.get("plugged") else "")
    ctx.do(ctx.actions.system_status, then=done)


@command("disk_space", "disk space", "how much space is left", section=SECTION, help="free space per drive")
def disk_space(ctx, m):
    def done(disks):
        disks = list(disks or [])[:3]
        if not disks:
            ctx.say(persona.render(R.ACTION_FAILED, ctx.config))
            return
        parts = [persona.render(R.DISK, ctx.config, drive=d["drive"], free=d["free_gb"], total=d["total_gb"])
                 if i == 0 else f"{d['drive']} has {d['free_gb']} free of {d['total_gb']}."
                 for i, d in enumerate(disks)]
        full = [d for d in disks if d.get("pct", 0) > 90]
        if full:
            parts.append(R.DISK_NEARLY_FULL if len(disks) == 1 else
                         " ".join(NEARLY_FULL_ONE.format(drive=d["drive"]) for d in full))
        ctx.say(" ".join(parts))
    ctx.do(ctx.actions.disks, then=done)


@command("top_cpu", "what's using the cpu", "what is using the cpu", section=SECTION,
         help="the apps using the most CPU")
def top_cpu(ctx, m):
    def done(names):
        labels = []
        for n in names or []:
            label = nice_name(n)
            if label not in labels:
                labels.append(label)
        say(ctx, R.CPU_USERS, apps=join_and(labels) or "Nothing much")
    ctx.do(ctx.actions.top_processes, 3, then=done)


def _shot(ctx, window: bool) -> None:
    def done(path):
        ctx.event("info", str(path), meta=str(path))
        say(ctx, R.SCREENSHOT_SAVED)
    ctx.do(ctx.actions.screenshot, window, then=done)


@command("screenshot", "take a screenshot", "screenshot", "take a picture of the screen", "capture the screen",
         "screen capture", "take a screen shot", "screen shot", "grab the screen", "print screen",
         "save the screen", section=SECTION,
         help="saves a screenshot to Pictures\\Xyrus")
def screenshot(ctx, m):
    _shot(ctx, False)


@command("screenshot_window", "screenshot this window", section=SECTION, help="screenshots the focused window")
def screenshot_window(ctx, m):
    _shot(ctx, True)


@command("open_last_screenshot", "open the last screenshot", "show the screenshot", "open the screenshot",
         section=SECTION, help="opens the newest screenshot")
def open_last_screenshot(ctx, m):
    def done(path):
        if path is None:
            say(ctx, R.NO_SCREENSHOT)
        else:
            ctx.do(ctx.actions.open_target, str(path))
    ctx.do(ctx.actions.last_screenshot, then=done)


@command("calculate", "what is <expr>", "what's <expr>", "calculate <expr>", section=SECTION,
         help="a quick sum: twelve times four, fifteen percent of eighty")
def calculate(ctx, m):
    v = m.slots["expr"]
    if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))):
        say(ctx, R.UNDEFINED)
    else:
        say(ctx, R.CALC_RESULT, value=parsing.say_number(v))


def _weather(ctx, rain: bool) -> None:
    if not ctx.config.get("weather.enabled", False) or not (ctx.config.get("weather.city", "") or "").strip():
        say(ctx, R.WEATHER_OFF)
        return
    city = ctx.config.get("weather.city").strip()

    def done(w):
        if rain:
            chance = int(w.get("rain_chance", 0))
            say(ctx, RAIN_LIKELY if chance >= 50 else RAIN_UNLIKELY, chance=chance)
        else:
            say(ctx, R.WEATHER, temp=w["temp_c"], desc=str(w.get("desc", "")).lower(), city=w.get("city", city))

    def failed(e):
        say(ctx, R.WEATHER_FAILED)
    ctx.do(ctx.actions.weather, city, then=done, error=failed, timeout=WEATHER_TIMEOUT)


@command("weather", "what's the weather", "what is the weather", "weather", section=SECTION,
         help="the weather (opt-in, needs the internet)")
def weather(ctx, m):
    _weather(ctx, rain=False)


@command("will_it_rain", "will it rain today", "will it rain", section=SECTION,
         help="chance of rain today (opt-in, needs the internet)")
def will_it_rain(ctx, m):
    _weather(ctx, rain=True)
