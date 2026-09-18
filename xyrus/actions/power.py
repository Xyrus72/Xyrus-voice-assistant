"""Power actions (v1 actions.py moved, §3.2). Runs on T-exec. Never speaks (G10)."""
from __future__ import annotations

import ctypes
import logging
import threading

from xyrus import winutil

log = logging.getLogger("xyrus.actions.power")

_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32
_kernel32.SetThreadExecutionState.restype = ctypes.c_uint
_kernel32.SetThreadExecutionState.argtypes = [ctypes.c_uint]

HWND_BROADCAST = 0xFFFF
WM_SYSCOMMAND = 0x0112
SC_MONITORPOWER = 0xF170
MONITOR_OFF, MONITOR_ON = 2, -1
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002
ES_CONTINUOUS = 0x80000000
MOUSEEVENTF_MOVE = 0x0001
EWX_LOGOFF = 0

ERROR_NO_SHUTDOWN_IN_PROGRESS = 1116


def _shutdown_cmd(*args: str) -> None:
    r = winutil.run(["shutdown", *args], capture=True, timeout=10)
    if r.returncode != 0:
        msg = (r.stderr or r.stdout or "").strip() or f"exit code {r.returncode}"
        raise RuntimeError(f"shutdown {' '.join(args)}: {msg}")


def shutdown(delay_s: int) -> None:
    _shutdown_cmd("/s", "/t", str(max(0, int(delay_s))))


def restart(delay_s: int) -> None:
    _shutdown_cmd("/r", "/t", str(max(0, int(delay_s))))


def abort_shutdown() -> None:
    """shutdown /a. 'No shutdown in progress' (1116) is not an error — plain 'cancel' runs this (v1)."""
    r = winutil.run(["shutdown", "/a"], capture=True, timeout=10)
    if r.returncode not in (0, ERROR_NO_SHUTDOWN_IN_PROGRESS):
        log.info("shutdown /a returned %s: %s", r.returncode, (r.stderr or r.stdout or "").strip())


def sleep() -> None:
    ctypes.windll.powrprof.SetSuspendState(0, 1, 0)     # Hibernate=False, ForceCritical=True


def hibernate() -> bool:
    """False when hibernation is turned off on this PC (SetSuspendState returns 0)."""
    return bool(ctypes.windll.powrprof.SetSuspendState(1, 1, 0))


def lock() -> None:
    if not _user32.LockWorkStation():
        raise OSError("LockWorkStation failed")


def sign_out() -> None:
    if not _user32.ExitWindowsEx(EWX_LOGOFF, 0):
        raise OSError("ExitWindowsEx failed")


SMTO_ABORTIFHUNG = 0x0002


def screen_off() -> None:
    # SendMessageTimeout, not SendMessage: a plain broadcast waits for every top-level window to answer, and one
    # hung window stalled "display off" for 16 s on this PC (Sep 14 2026). Hung windows are skipped; 1 s max each.
    result = ctypes.c_size_t(0)
    _user32.SendMessageTimeoutW(HWND_BROADCAST, WM_SYSCOMMAND, SC_MONITORPOWER, MONITOR_OFF,
                                SMTO_ABORTIFHUNG, 1000, ctypes.byref(result))


def screen_on() -> None:
    _kernel32.SetThreadExecutionState(ES_DISPLAY_REQUIRED | ES_SYSTEM_REQUIRED)
    _user32.mouse_event(MOUSEEVENTF_MOVE, 0, 1, 0, 0)
    _user32.mouse_event(MOUSEEVENTF_MOVE, 0, -1, 0, 0)
    # PostMessage, not SendMessage: a broadcast SendMessage can block on a hung window
    _user32.PostMessageW(HWND_BROADCAST, WM_SYSCOMMAND, SC_MONITORPOWER, MONITOR_ON)


class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]


def tick_now() -> int:
    """Milliseconds since boot, 32-bit - the same clock GetLastInputInfo uses (clap toggle)."""
    return _kernel32.GetTickCount() & 0xFFFFFFFF


def last_input_tick() -> int:
    """tick_now() of the last keyboard or mouse input."""
    info = _LASTINPUTINFO(ctypes.sizeof(_LASTINPUTINFO), 0)
    if not _user32.GetLastInputInfo(ctypes.byref(info)):
        return tick_now()
    return info.dwTime & 0xFFFFFFFF


# ---- keep awake: the execution-state flag is per thread, so a dedicated thread holds it (T-keepawake)
_awake_lock = threading.Lock()
_awake_stop: threading.Event | None = None
_awake_thread: threading.Thread | None = None


def _hold(stop: threading.Event) -> None:
    _kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED)
    try:
        while not stop.wait(30):
            _kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED)
    finally:
        _kernel32.SetThreadExecutionState(ES_CONTINUOUS)


def keep_awake(on: bool) -> None:
    global _awake_stop, _awake_thread
    with _awake_lock:
        if on:
            if _awake_thread is not None and _awake_thread.is_alive():
                return
            _awake_stop = threading.Event()
            _awake_thread = threading.Thread(target=_hold, args=(_awake_stop,), name="T-keepawake", daemon=True)
            _awake_thread.start()
        else:
            if _awake_stop is not None:
                _awake_stop.set()
            if _awake_thread is not None:
                _awake_thread.join(2)
            _awake_stop = _awake_thread = None


def keeping_awake() -> bool:
    with _awake_lock:
        return _awake_thread is not None and _awake_thread.is_alive()
