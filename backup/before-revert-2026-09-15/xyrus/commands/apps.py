"""Open apps / sites / settings pages (§3.5): Apps-tab aliases first, then the Start Menu index."""
from __future__ import annotations

from xyrus import replies as R
from xyrus.commands import app_label, nice_name, say
from xyrus.registry import command

SECTION = "Apps & web"


@command("open_app", "open <app>", "launch <app>", "start <app>", section=SECTION,
         help="opens an app, site or settings page from the Apps tab (or anything in the Start menu)")
def open_app(ctx, m):
    ref = m.slots["app"]
    target = getattr(ref, "target", None)
    if target:
        label = app_label(ref)
        ctx.do(ctx.actions.open_target, target, then=lambda _: say(ctx, R.OPENING, app=label))
        return
    spoken = getattr(ref, "spoken", str(ref))

    def found(hit):
        if not hit:
            say(ctx, R.UNKNOWN_APP, name=spoken)
            return
        name, path = hit
        ctx.do(ctx.actions.open_target, path, then=lambda _: say(ctx, R.OPENING, app=nice_name(name)))
    ctx.do(ctx.actions.find_app, spoken, then=found)
