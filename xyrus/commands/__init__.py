"""Command registrations (§4.14). `load_all(registry)` imports every command module once (idempotent);
each module registers its handlers with the one `xyrus.registry.command` decorator.

Also small helpers shared by the T4 command modules (rendering replies, display names)."""
from __future__ import annotations

import importlib
import logging
from typing import Any, Callable, Iterable

log = logging.getLogger("xyrus.commands")

MODULES = ("core", "power", "sound", "display", "windows", "apps", "web", "keys", "clipboard", "info", "fun",
           "timers", "calendar", "assistant", "custom")


def load_all(registry: Any = None):
    """Import every command module (registration side effects into registry.REGISTRY). If another Registry
    instance is passed, the built-in commands are copied into it. Returns the registry used.
    Strict (T6): a missing or broken command module raises."""
    from xyrus.registry import REGISTRY
    for name in MODULES:
        importlib.import_module(f"xyrus.commands.{name}")
    if registry is not None and registry is not REGISTRY:
        have = {c.name for c in registry.commands()}
        for cmd in REGISTRY.commands():
            if not cmd.custom and cmd.name not in have:
                registry.register(cmd)
        return registry
    return REGISTRY


# ----------------------------------------------------------------------------- helpers for handlers
def say(ctx, template: str, *, then: Callable[[], None] | None = None, force: bool = False, **kw) -> None:
    """Render named fields (and {sir}) of a replies template, then speak it."""
    from xyrus import persona
    ctx.say(persona.render(template, ctx.config, **kw), then=then, force=force)


def text(ctx, template: str, **kw) -> str:
    from xyrus import persona
    return persona.render(template, ctx.config, **kw)


_NICE = {"youtube": "YouTube", "msedge": "Edge", "edge": "Edge", "code": "Code", "vs code": "VS Code",
         "wt": "Terminal", "windowsterminal": "Terminal", "calculatorapp": "Calculator", "calc": "Calculator",
         "taskmgr": "Task Manager", "qbittorrent": "qBittorrent", "obs64": "OBS", "vlc": "VLC",
         "pdf": "PDF", "tv": "TV"}


def nice_name(name: str) -> str:
    """'chrome' -> 'Chrome', 'task manager' -> 'Task Manager', 'msedge' -> 'Edge'."""
    n = " ".join((name or "").split())
    if n.lower() in _NICE:
        return _NICE[n.lower()]
    return " ".join(w if any(c.isupper() for c in w) else w[:1].upper() + w[1:] for w in n.split())


def app_label(ref: Any) -> str:
    """Display name of an AppRef (alias when it resolved through the Apps list, else what was said)."""
    return nice_name(getattr(ref, "alias", None) or getattr(ref, "spoken", None) or str(ref))


def app_key(ref: Any) -> str:
    return (getattr(ref, "alias", None) or getattr(ref, "spoken", None) or str(ref)).strip().lower()


def join_and(items: Iterable[str]) -> str:
    items = [i for i in items if i]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " and " + items[-1]
