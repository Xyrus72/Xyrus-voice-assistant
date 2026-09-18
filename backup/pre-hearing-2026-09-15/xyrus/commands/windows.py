"""Windows (§3.4): show desktop, close / min / max / restore / snap the focused window, switch to / close /
force-close an app, keep on top, what's open, virtual desktops."""
from __future__ import annotations

from xyrus import replies as R
from xyrus.commands import app_key, app_label, join_and, nice_name, say, text
from xyrus.registry import command

SECTION = "Display & windows"
MAX_OPEN_NAMES = 6


def _win(ctx, cmd: str) -> None:
    ctx.do(ctx.actions.window_cmd, cmd)


@command("show_desktop", "show desktop", "minimize everything", "minimise everything", "hide all windows",
         "minimize all windows", "go to the desktop", section=SECTION,
         help="minimises everything")
def show_desktop(ctx, m):
    ctx.do(ctx.actions.show_desktop)


@command("close_window", "close window", "close this", "close this window", "close it", "exit this app",
         "close this app", "close the window", section=SECTION,
         help="closes the focused window")
def close_window(ctx, m):
    a = ctx.actions

    def close():
        if a.foreground_is_self():
            return False
        a.window_cmd("close")
        return True

    def done(ok):
        if not ok:
            say(ctx, R.OWN_WINDOW)
            # SPEC-AMBIGUITY: §3.4 says "hide it instead"; §4.8 lists no "hide" ui message. "hide" is posted
            # to ui_queue for the window to handle (reported to the lead / T5).
            ctx.ui("hide")
    ctx.do(close, then=done)


@command("minimize", "minimize", "minimise", "minimize this", "minimise this", section=SECTION,
         help="minimises the focused window")
def minimize(ctx, m):
    _win(ctx, "minimize")


@command("maximize", "maximize", "maximise", "maximize this", "maximise this", section=SECTION,
         help="maximises the focused window")
def maximize(ctx, m):
    _win(ctx, "maximize")


@command("restore_window", "restore this", "normal size", section=SECTION, help="normal size again")
def restore_window(ctx, m):
    _win(ctx, "restore")


@command("snap_left", "snap left", "snap this left", section=SECTION, help="left half of the screen")
def snap_left(ctx, m):
    ctx.do(ctx.actions.snap, "left")


@command("snap_right", "snap right", "snap this right", section=SECTION, help="right half of the screen")
def snap_right(ctx, m):
    ctx.do(ctx.actions.snap, "right")


def _open_ref(ctx, ref, label: str) -> None:
    target = getattr(ref, "target", None)
    if target:
        ctx.do(ctx.actions.open_target, target, then=lambda _: say(ctx, R.OPENING, app=label))
    else:
        say(ctx, R.UNKNOWN_APP, name=label)


@command("switch_to", "switch to <app>", "go to <app>", "bring up <app>", section=SECTION,
         help="brings an open app to the front (offers to open it)")
def switch_to(ctx, m):
    ref = m.slots["app"]
    label = app_label(ref)

    def done(ok):
        if ok:
            say(ctx, R.SWITCHED_TO, app=label)
        else:
            ctx.confirm(text(ctx, R.NOT_OPEN_OFFER, app=label), lambda: _open_ref(ctx, ref, label))
    ctx.do(ctx.actions.focus_app, app_key(ref), then=done)


@command("close_app", "close <app>", "quit <app>", "exit <app>", "shut down <app>", "shut <app>",
         section=SECTION,
         help="closes every window of an app (like clicking X)")
def close_app(ctx, m):
    ref = m.slots["app"]
    label = app_label(ref)
    ctx.do(ctx.actions.close_app, app_key(ref),
           then=lambda ok: say(ctx, R.CLOSING_APP if ok else R.NOT_OPEN, app=label))


@command("force_close", "force close <app>", "kill <app>", section=SECTION,
         help="asks, then force-closes an app (unsaved work is lost)", destructive=True)
def force_close(ctx, m):
    ref = m.slots["app"]
    label = app_label(ref)

    def fire():
        ctx.do(ctx.actions.kill_app, app_key(ref),
               then=lambda ok: say(ctx, R.DONE if ok else R.NOT_OPEN, app=label))
    ctx.confirm(text(ctx, R.CONFIRM_FORCE_CLOSE, app=label), fire)


@command("keep_on_top", "keep this on top", section=SECTION, help="pins the focused window on top")
def keep_on_top(ctx, m):
    ctx.do(ctx.actions.window_cmd, "topmost", then=lambda _: say(ctx, R.PINNED))


@command("stop_on_top", "stop keeping this on top", section=SECTION, help="unpins it")
def stop_on_top(ctx, m):
    ctx.do(ctx.actions.window_cmd, "notopmost", then=lambda _: say(ctx, R.UNPINNED))


@command("whats_open", "what's open", "what is open", "what windows are open", section=SECTION,
         help="names the open apps")
def whats_open(ctx, m):
    def done(apps):
        names = []
        for a in apps or []:
            n = nice_name(a)
            if n not in names:
                names.append(n)
        if not names:
            say(ctx, R.NOTHING_OPEN)
            return
        names = names[:MAX_OPEN_NAMES]
        say(ctx, R.WHATS_OPEN, apps=join_and(names), verb="is" if len(names) == 1 else "are")
    ctx.do(ctx.actions.windowed_apps, then=done)


@command("new_desktop", "new desktop", section=SECTION, help="a new virtual desktop")
def new_desktop(ctx, m):
    ctx.do(ctx.actions.desktop, "new")


@command("next_desktop", "next desktop", section=SECTION, help="the virtual desktop to the right")
def next_desktop(ctx, m):
    ctx.do(ctx.actions.desktop, "next")


@command("previous_desktop", "previous desktop", section=SECTION, help="the virtual desktop to the left")
def previous_desktop(ctx, m):
    ctx.do(ctx.actions.desktop, "prev")


@command("close_desktop", "close this desktop", section=SECTION, help="closes the current virtual desktop")
def close_desktop(ctx, m):
    ctx.do(ctx.actions.desktop, "close")
