"""Launching things (v1 open_target) and the Start Menu index (§3.5). Never speaks (G10)."""
from __future__ import annotations

import difflib
import logging
import os
import re
import threading
import time
from pathlib import Path

from xyrus import winutil

log = logging.getLogger("xyrus.actions.apps")

SKIP_WORDS = ("uninstall", "readme", "help", "website", "documentation")
REFRESH_S = 600


def open_target(target: str) -> None:
    """Launch an exe / URL / URI / shell: folder / .lnk the way `start` would (v1).
    .lnk files, existing paths and URLs use os.startfile (no cmd.exe metacharacter issues with '&' in
    URLs); everything else goes through `cmd /c start "" <target>` which also resolves App Paths."""
    t = (target or "").strip()
    if not t:
        raise ValueError("empty target")
    # SPEC-AMBIGUITY: §3.5 says startfile only for .lnk and `cmd start` otherwise; URLs are routed to
    # startfile too because cmd would split a URL at '&' (behaviour is otherwise identical).
    if t.lower().endswith(".lnk") or "://" in t or os.path.exists(t):
        winutil.open_url(t)
    else:
        winutil.popen(["cmd", "/c", "start", "", t])


def _clean_name(stem: str) -> str:
    s = re.sub(r"[^\w\s]", " ", stem.lower())
    return " ".join(s.replace("_", " ").split())


def start_menu_dirs() -> list[Path]:
    dirs = []
    for env in ("ProgramData", "APPDATA"):
        base = os.environ.get(env)
        if base:
            dirs.append(Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs")
    return dirs


class StartMenuIndex:
    """Name -> .lnk path for everything in both Start Menu folders. Thread-safe."""

    def __init__(self, dirs: list[Path] | None = None):
        self._dirs = dirs
        self._lock = threading.Lock()
        self._items: dict[str, str] = {}
        self._stamp = 0.0

    def refresh(self) -> int:
        items: dict[str, str] = {}
        for d in (self._dirs if self._dirs is not None else start_menu_dirs()):
            if not d.is_dir():
                continue
            for p in d.rglob("*.lnk"):
                name = _clean_name(p.stem)
                if not name or any(w in name for w in SKIP_WORDS):
                    continue
                items.setdefault(name, str(p))
        with self._lock:
            self._items = items
            self._stamp = time.monotonic()
        log.info("start menu index: %d entries", len(items))
        return len(items)

    def _ensure(self) -> None:
        if not self._stamp or time.monotonic() - self._stamp > REFRESH_S:
            self.refresh()

    def names(self) -> list[str]:
        self._ensure()
        with self._lock:
            return sorted(self._items)

    def lookup(self, spoken: str) -> tuple[str, str] | None:
        """Exact name, then difflib close match (cutoff 0.75), then a unique name starting with the query."""
        q = _clean_name(spoken)
        if not q:
            return None
        self._ensure()
        with self._lock:
            items = dict(self._items)
        if q in items:
            return q, items[q]
        hit = difflib.get_close_matches(q, list(items), n=1, cutoff=0.75)
        if hit:
            return hit[0], items[hit[0]]
        starts = [n for n in items if n.startswith(q + " ")]
        if len(starts) == 1:
            return starts[0], items[starts[0]]
        return None


_default_index: StartMenuIndex | None = None
_default_lock = threading.Lock()


def default_index() -> StartMenuIndex:
    global _default_index
    with _default_lock:
        if _default_index is None:
            _default_index = StartMenuIndex()
        return _default_index


def find_app(spoken: str, index: StartMenuIndex | None = None) -> tuple[str, str] | None:
    return (index or default_index()).lookup(spoken)
