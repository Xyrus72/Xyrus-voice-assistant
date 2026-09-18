"""Global hotkeys via user32.RegisterHotKey on a dedicated message thread (D6).

    hk = Hotkeys({"show_window": "ctrl+alt+x"}, on_hotkey=lambda name: ...)
    hk.start()          # returns once registration is done, so status() is meaningful
    hk.status()         # {"show_window": True}; False = the combo is taken by another program
    hk.stop()           # WM_QUIT to the thread, which unregisters everything

on_hotkey(name) runs on T-hotkey: it must only enqueue / call thread-safe methods.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import logging
import threading
from typing import Callable

log = logging.getLogger("xyrus.hotkeys")

MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000
WM_HOTKEY, WM_QUIT, WM_USER = 0x0312, 0x0012, 0x0400
PM_NOREMOVE = 0

DEFAULT_BINDINGS = {"show_window": "ctrl+alt+x", "push_to_talk": "ctrl+alt+space", "stop_speaking": "ctrl+alt+s"}

MODS = {"ctrl": MOD_CONTROL, "control": MOD_CONTROL, "alt": MOD_ALT, "shift": MOD_SHIFT, "win": MOD_WIN}
KEYS = {"space": 0x20, "enter": 0x0D, "tab": 0x09, "escape": 0x1B, "esc": 0x1B, "home": 0x24, "end": 0x23,
        "insert": 0x2D, "delete": 0x2E, "pageup": 0x21, "pagedown": 0x22,
        "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27, "pause": 0x13}
KEYS.update({f"f{i}": 0x6F + i for i in range(1, 13)})

_u32 = ctypes.WinDLL("user32", use_last_error=True)
_u32.RegisterHotKey.argtypes = [wt.HWND, ctypes.c_int, wt.UINT, wt.UINT]
_u32.RegisterHotKey.restype = wt.BOOL
_u32.UnregisterHotKey.argtypes = [wt.HWND, ctypes.c_int]
_u32.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT]
_u32.GetMessageW.restype = wt.BOOL
_u32.PeekMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT, wt.UINT]
_u32.PostThreadMessageW.argtypes = [wt.DWORD, wt.UINT, wt.WPARAM, wt.LPARAM]
_u32.PostThreadMessageW.restype = wt.BOOL
_k32 = ctypes.WinDLL("kernel32")
_k32.GetCurrentThreadId.restype = wt.DWORD


def parse(combo: str) -> tuple[int, int]:
    """'ctrl+alt+x' -> (MOD_CONTROL|MOD_ALT, 0x58). Raises ValueError on a bad combo."""
    mods, vk = 0, None
    for part in (p.strip().lower() for p in combo.split("+") if p.strip()):
        if part in MODS:
            mods |= MODS[part]
        elif part in KEYS:
            vk = KEYS[part]
        elif len(part) == 1 and part.isalnum():
            vk = ord(part.upper())
        else:
            raise ValueError(f"unknown key {part!r} in {combo!r}")
    if vk is None:
        raise ValueError(f"no key in {combo!r}")
    return mods, vk


class Hotkeys:
    ID_BASE = 0xB100            # app hotkey ids must be < 0xC000

    def __init__(self, bindings: dict[str, str] | None, on_hotkey: Callable[[str], None]):
        self.bindings = dict(DEFAULT_BINDINGS if bindings is None else bindings)
        self.on_hotkey = on_hotkey
        self._status: dict[str, bool] = {}
        self._thread: threading.Thread | None = None
        self._tid = 0
        self._ready = threading.Event()
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ public --
    def start(self, timeout: float = 2.0) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._ready.clear()
        self._thread = threading.Thread(target=self._run, name="T-hotkey", daemon=True)
        self._thread.start()
        self._ready.wait(timeout)

    def status(self) -> dict[str, bool]:
        with self._lock:
            return dict(self._status)

    def stop(self, timeout: float = 2.0) -> None:
        t = self._thread
        if not t:
            return
        if self._tid:
            _u32.PostThreadMessageW(self._tid, WM_QUIT, 0, 0)
        t.join(timeout)
        self._thread = None
        self._tid = 0

    def rebind(self, bindings: dict[str, str]) -> dict[str, bool]:
        """Settings changed a combo: re-register everything, return the new status."""
        self.stop()
        self.bindings = dict(bindings)
        self.start()
        return self.status()

    # -------------------------------------------------------------- T-hotkey ----
    def _run(self) -> None:
        msg = wt.MSG()
        self._tid = _k32.GetCurrentThreadId()
        _u32.PeekMessageW(ctypes.byref(msg), None, WM_USER, WM_USER, PM_NOREMOVE)   # create the queue
        ids: dict[int, str] = {}
        status: dict[str, bool] = {}
        for i, (name, combo) in enumerate(self.bindings.items()):
            try:
                mods, vk = parse(combo)
            except ValueError as e:
                log.warning("hotkey %s: %s", name, e)
                status[name] = False
                continue
            hid = self.ID_BASE + i
            if _u32.RegisterHotKey(None, hid, mods | MOD_NOREPEAT, vk):
                ids[hid] = name
                status[name] = True
            else:
                log.info("hotkey %s (%s) not registered: error %s (in use by another program)",
                         name, combo, ctypes.get_last_error())
                status[name] = False
        with self._lock:
            self._status = status
        self._ready.set()
        try:
            while _u32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY:
                    name = ids.get(int(msg.wParam))
                    if name:
                        try:
                            self.on_hotkey(name)
                        except Exception:
                            log.exception("hotkey handler %s failed", name)
        finally:
            for hid in ids:
                _u32.UnregisterHotKey(None, hid)
            with self._lock:
                self._status = {n: False for n in self._status}
