"""Clipboard (§3.5): read, clear, copy the date / time."""
from __future__ import annotations

from xyrus import persona, replies as R
from xyrus.commands import say
from xyrus.registry import command

SECTION = "Keys & clipboard"
READ_WORDS = 40


@command("read_clipboard", "read the clipboard", "read my clipboard", "what's on the clipboard",
         "what is on the clipboard", section=SECTION, help="reads the clipboard text aloud")
def read_clipboard(ctx, m):
    def done(clip):
        words = (clip or "").split()
        if not words:
            say(ctx, R.CLIPBOARD_EMPTY)
            return
        spoken = " ".join(words[:READ_WORDS]) + ("…" if len(words) > READ_WORDS else "")
        ctx.say(spoken)
    ctx.do(ctx.actions.clip_get, then=done)


@command("clear_clipboard", "clear the clipboard", section=SECTION, help="empties the clipboard")
def clear_clipboard(ctx, m):
    # SystemActions has no clip_clear (lead, 2026-09-13: keep this): clip_set("") empties the clipboard
    # (WinActions.clip_set -> actions.clipboard.clip_clear for an empty string).
    ctx.do(ctx.actions.clip_set, "", then=lambda _: say(ctx, R.CLEARED))


@command("copy_date", "copy the date", section=SECTION, help="puts today's date on the clipboard")
def copy_date(ctx, m):
    now = ctx.clock.now()
    ctx.do(ctx.actions.clip_set, f"{now:%A}, {now:%B} {now.day}, {now.year}", then=lambda _: say(ctx, R.COPIED))


@command("copy_time", "copy the time", section=SECTION, help="puts the time on the clipboard")
def copy_time(ctx, m):
    ctx.do(ctx.actions.clip_set, persona.speak_time(ctx.clock.now()), then=lambda _: say(ctx, R.COPIED))
