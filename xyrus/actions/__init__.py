"""WinActions: the real SystemActions (interfaces.py) — every Windows side effect, delegating to the area
modules. Methods may block; they are called only on T-exec (via ctx.do) or by the smoke test. They raise on
failure and never speak (G10)."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from xyrus import winutil
from xyrus.actions import apps as _apps
from xyrus.actions import audio as _audio
from xyrus.actions import clipboard as _clip
from xyrus.actions import display as _display
from xyrus.actions import keys as _keys
from xyrus.actions import media as _media
from xyrus.actions import power as _power
from xyrus.actions import screenshot as _shot
from xyrus.actions import sysinfo as _sys
from xyrus.actions import web as _web
from xyrus.actions import windows as _win

log = logging.getLogger("xyrus.actions")


class WinActions:
    """config: optional Config (for app alias targets when matching windows);
    app_index: optional apps.StartMenuIndex; screenshot_dir: override (tests)."""

    def __init__(self, config: Any = None, app_index: _apps.StartMenuIndex | None = None,
                 screenshot_dir: Path | None = None):
        self.config = config
        self.app_index = app_index or _apps.default_index()
        self.screenshot_dir = screenshot_dir

    # ---- helpers
    def _alias_target(self, app: str) -> str | None:
        if self.config is None:
            return None
        try:
            apps = self.config.get("apps", {}) or {}
        except Exception:
            return None
        return apps.get(app.lower().strip())

    # ---- power
    def shutdown(self, delay_s: int) -> None: _power.shutdown(delay_s)
    def restart(self, delay_s: int) -> None: _power.restart(delay_s)
    def abort_shutdown(self) -> None: _power.abort_shutdown()
    def sleep(self) -> None: _power.sleep()
    def hibernate(self) -> bool: return _power.hibernate()
    def lock(self) -> None: _power.lock()
    def sign_out(self) -> None: _power.sign_out()
    def screen_off(self) -> None: _power.screen_off()
    def screen_on(self) -> None: _power.screen_on()
    def keep_awake(self, on: bool) -> None: _power.keep_awake(on)

    # ---- sound + media
    def volume_get(self) -> int: return _audio.volume_get()
    def volume_set(self, pct: int) -> None: _audio.volume_set(pct)
    def volume_step(self, delta_pct: int) -> int: return _audio.volume_step(delta_pct)
    def mute_get(self) -> bool: return _audio.mute_get()
    def mute_set(self, muted: bool) -> None: _audio.mute_set(muted)
    def media(self, key: str) -> None: _media.media(key)

    def app_volume(self, app: str, pct: int | None = None, mute: bool | None = None) -> bool:
        return _audio.app_volume(app, pct, mute, alias_exe=_win._alias_process(self._alias_target(app)))

    # ---- display
    def brightness_get(self) -> int | None: return _display.brightness_get()
    def brightness_set(self, value: int | str) -> int | None: return _display.brightness_set(value)
    def set_theme(self, dark: bool) -> None: _display.set_theme(dark)

    # ---- windows
    def windowed_apps(self) -> list[str]: return _win.windowed_apps()
    def focus_app(self, app: str) -> bool: return _win.focus_app(app, self._alias_target(app))
    def close_app(self, app: str) -> bool: return _win.close_app(app, self._alias_target(app))
    def kill_app(self, app: str) -> bool: return _win.kill_app(app, self._alias_target(app))
    def window_cmd(self, cmd: str) -> None: _win.window_cmd(cmd)
    def snap(self, side: str) -> None: _win.snap(side)
    def show_desktop(self) -> None: _win.show_desktop()
    def desktop(self, cmd: str) -> None: _win.desktop(cmd)
    def foreground_is_self(self) -> bool: return _win.foreground_is_self()

    # ---- apps / web
    def open_target(self, target: str) -> None: _apps.open_target(target)
    def find_app(self, spoken: str) -> tuple[str, str] | None: return self.app_index.lookup(spoken)
    def find_song(self, query: str, alternates: tuple[str, ...] = (), bn_text: str | None = None):
        """web.SongPick - unpacks as (spoken, url, video title); alternates / bn_text are the hearing's other
        readings of the title (n-best, the Bengali decode), tried when the first search result doesn't match."""
        return _web.find_song(query, alternates, bn_text)

    def find_songs(self, query: str, limit: int = 6) -> list[tuple[str, str, str]]:
        """Ranked results for 'not that one' (normal-length videos first)."""
        return _web.find_songs(query, limit)
    def weather(self, city: str) -> dict: return _web.weather(city)

    def media_sounding(self) -> bool: return _web.media_sounding()

    def browser_playing(self) -> bool:
        """A web browser is making sound right now (pycaw sessions). Not in interfaces.SystemActions yet."""
        return _web.browser_playing()

    def ensure_playing(self, video_title: str, trust_state: bool = True) -> str:
        """After opening a video: 'autoplay' | 'started' | 'blocked' | 'no-window' (v1, see actions/web.py)."""
        return _web.ensure_playing(video_title, trust_state=trust_state)

    def run_command(self, cmdline: str) -> None:
        """Routine `run` step (§3.10): a shell command line, detached. Not in interfaces.SystemActions yet."""
        winutil.popen(cmdline, shell=True)

    # ---- input
    def keys(self, chord: str) -> None: _keys.chord(chord)
    def type_text(self, text: str) -> None: _keys.type_text(text)

    # ---- clipboard
    def clip_get(self) -> str: return _clip.clip_get()
    def clip_set(self, text: str) -> None: _clip.clip_set(text)
    def clip_clear(self) -> None: _clip.clip_clear()

    # ---- info
    def system_status(self) -> dict: return _sys.system_status()
    def disks(self) -> list[dict]: return _sys.disks()
    def top_processes(self, n: int = 3) -> list[str]: return _sys.top_processes(n)
    def screenshot(self, window: bool = False) -> Path: return _shot.screenshot(window, self.screenshot_dir)
    def last_screenshot(self) -> Path | None: return _shot.last_screenshot(self.screenshot_dir)


__all__ = ["WinActions"]
