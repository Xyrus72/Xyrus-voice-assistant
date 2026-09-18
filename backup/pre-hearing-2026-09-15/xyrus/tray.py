"""Tray icon (pystray, run_detached) + toast notifier (F4).

States: listening = purple, active (armed / follow-up / dialogue) = purple with a green ring,
paused = grey, error = red. Menu callbacks run on pystray's thread and only enqueue or call
thread-safe App methods.
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Literal

from PIL import Image, ImageDraw

log = logging.getLogger("xyrus.tray")

try:
    from xyrus.paths import ICON_FILE
except ImportError:                                   # pragma: no cover - before T1's paths.py lands
    ICON_FILE = Path(__file__).resolve().parent.parent / "xyrus.ico"

NAME = "Xyrus"
State = Literal["listening", "active", "paused", "error"]
STATES: tuple[State, ...] = ("listening", "active", "paused", "error")
FILL = {"listening": (124, 92, 255, 255), "active": (124, 92, 255, 255),
        "paused": (110, 110, 120, 255), "error": (255, 92, 92, 255)}
RING = (61, 220, 132, 255)
TOOLTIP_MAX = 127                                     # Windows NOTIFYICONDATA szTip limit


def make_icon_image(state: State = "listening", size: int = 64) -> Image.Image:
    """v1 make_icon_image, extended with the four states (drawn at 64 px, scaled if asked)."""
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if state == "active":
        d.ellipse((1, 1, 63, 63), fill=RING)
        d.ellipse((8, 8, 56, 56), fill=FILL[state])
        d.arc((19, 19, 45, 45), start=200, end=340, fill="white", width=5)
        d.ellipse((28, 28, 36, 36), fill="white")
    elif state == "error":                             # a white "!" - reads as a problem at 16 px
        d.ellipse((4, 4, 60, 60), fill=FILL[state])
        d.rounded_rectangle((27, 13, 37, 39), radius=4, fill="white")
        d.ellipse((27, 43, 37, 53), fill="white")
    else:
        d.ellipse((4, 4, 60, 60), fill=FILL.get(state, FILL["listening"]))
        d.arc((16, 16, 48, 48), start=200, end=340, fill="white", width=6)
        d.ellipse((27, 27, 37, 37), fill="white")
    return img if size == 64 else img.resize((size, size), Image.LANCZOS)


def ensure_icon_file(path: Path | str | None = None) -> Path:
    """Write xyrus.ico (16/32/48/64) if it is missing."""
    path = Path(path or ICON_FILE)
    if not path.exists():
        make_icon_image().save(path, sizes=[(16, 16), (32, 32), (48, 48), (64, 64)])
    return path


class Tray:
    """Tray(app).start(); set_state(); set_tooltip(); notify(title, body) (= interfaces.Notifier)."""

    READY_DELAY = 1.0                                  # icon.notify needs the icon to exist (F4)

    def __init__(self, app):
        self.app = app
        self.state: State = "listening"
        self.tooltip = f"{NAME} — listening"
        self._icon = None
        self._ready_at: float | None = None
        self._pending: list[tuple[str, str]] = []
        self._lock = threading.Lock()
        self._images = {s: make_icon_image(s) for s in STATES}

    # ------------------------------------------------------------------ icon ---
    def _menu(self):
        import pystray
        from xyrus import autostart

        app = self.app

        def paused() -> bool:
            try:
                return bool(app.engine.snapshot().paused)
            except Exception:
                return False

        def show(icon, item):
            app.ui_queue.put("show")

        def toggle_pause(icon, item):
            app.set_paused(not paused())

        def toggle_mute(icon, item):
            app.config.set("voice_replies", not app.config.get("voice_replies", True))

        def toggle_autostart(icon, item):
            try:
                autostart.set_enabled(not autostart.enabled())
            except Exception as e:
                log.error("autostart toggle failed: %s", e)

        def quit_(icon, item):
            app.ui_queue.put("quit")      # the window destroys itself; app.main() cleans up after mainloop

        return pystray.Menu(
            pystray.MenuItem(f"Open {NAME}", show, default=True),
            pystray.MenuItem(lambda item: "Resume listening" if paused() else "Pause listening", toggle_pause),
            pystray.MenuItem("Mute replies", toggle_mute,
                             checked=lambda item: not app.config.get("voice_replies", True)),
            pystray.MenuItem("Start with Windows", toggle_autostart, checked=lambda item: autostart.enabled()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(f"Quit {NAME}", quit_),
        )

    def start(self) -> None:
        import pystray
        self._icon = pystray.Icon("xyrus", self._images[self.state], self.tooltip, self._menu())
        self._icon.run_detached()
        self._ready_at = time.monotonic() + self.READY_DELAY
        threading.Timer(self.READY_DELAY + 0.1, self._flush).start()

    def stop(self) -> None:
        icon, self._icon = self._icon, None
        if icon is not None:
            try:
                icon.stop()
            except Exception as e:
                log.warning("tray stop: %s", e)

    def set_state(self, state: State) -> None:
        if state not in self._images or state == self.state:
            return
        self.state = state
        if self._icon is not None:
            try:
                self._icon.icon = self._images[state]
            except Exception as e:
                log.warning("tray icon swap failed: %s", e)

    def set_tooltip(self, text: str) -> None:
        text = (text or NAME)[:TOOLTIP_MAX]
        if text == self.tooltip:
            return
        self.tooltip = text
        if self._icon is not None:
            try:
                self._icon.title = text
            except Exception as e:
                log.warning("tray tooltip failed: %s", e)

    def update_menu(self) -> None:
        if self._icon is not None:
            try:
                self._icon.update_menu()
            except Exception:
                pass

    # -------------------------------------------------------------- Notifier ---
    def notify(self, title: str, body: str) -> None:
        """Toast from any thread; never raises. Queued until the icon has existed for ~1 s."""
        try:
            with self._lock:
                if self._icon is None or self._ready_at is None or time.monotonic() < self._ready_at:
                    self._pending.append((title, body))
                    return
            self._icon.notify(body, title)
        except Exception as e:
            log.warning("toast failed: %s", e)

    def _flush(self) -> None:
        with self._lock:
            pending, self._pending = self._pending, []
        for title, body in pending:
            self.notify(title, body)
