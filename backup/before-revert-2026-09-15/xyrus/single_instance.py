"""Single instance + "second launch shows the window" (F13).

The first instance holds the named mutex and owns an auto-reset event; a second instance opens that
event, signals it and exits 0. The window's 150 ms poll calls ShowEvent.poll() (never blocks).
Names are parameters so tests can use private names while the live app holds the real ones.
"""
from __future__ import annotations

import ctypes
import logging

log = logging.getLogger("xyrus.single_instance")

MUTEX_NAME = "Local\\XyrusVoiceAssistant"
EVENT_NAME = "Local\\XyrusShowWindow"

ERROR_ALREADY_EXISTS = 183
EVENT_MODIFY_STATE = 0x0002
SYNCHRONIZE = 0x00100000
WAIT_OBJECT_0 = 0

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.CreateMutexW.restype = ctypes.c_void_p
_k32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
_k32.CreateEventW.restype = ctypes.c_void_p
_k32.CreateEventW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_bool, ctypes.c_wchar_p]
_k32.OpenEventW.restype = ctypes.c_void_p
_k32.OpenEventW.argtypes = [ctypes.c_uint32, ctypes.c_bool, ctypes.c_wchar_p]
_k32.SetEvent.argtypes = [ctypes.c_void_p]
_k32.SetEvent.restype = ctypes.c_bool
_k32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
_k32.WaitForSingleObject.restype = ctypes.c_uint32
_k32.CloseHandle.argtypes = [ctypes.c_void_p]
_k32.CloseHandle.restype = ctypes.c_bool

_held: dict[str, int] = {}          # mutex name -> handle, kept alive for the process lifetime


def acquire(name: str = MUTEX_NAME) -> bool:
    """True if this process is the first instance (the mutex handle is kept open until exit)."""
    if name in _held:
        return True
    h = _k32.CreateMutexW(None, False, name)
    err = ctypes.get_last_error()
    if not h:
        log.error("CreateMutexW(%s) failed: error %s - running anyway", name, err)
        return True
    if err == ERROR_ALREADY_EXISTS:
        _k32.CloseHandle(h)
        return False
    _held[name] = h
    return True


def release(name: str = MUTEX_NAME) -> None:
    """Drop the mutex (tests; the app just exits)."""
    h = _held.pop(name, None)
    if h:
        _k32.CloseHandle(h)


def signal_existing(name: str = EVENT_NAME) -> bool:
    """Second instance: ask the first one to show its window. False if nobody listens."""
    h = _k32.OpenEventW(EVENT_MODIFY_STATE | SYNCHRONIZE, False, name)
    if not h:
        log.info("OpenEventW(%s) failed: error %s", name, ctypes.get_last_error())
        return False
    try:
        return bool(_k32.SetEvent(h))
    finally:
        _k32.CloseHandle(h)


class ShowEvent:
    """First instance: the auto-reset event a second launch signals. poll() never blocks."""

    def __init__(self, name: str = EVENT_NAME):
        self.name = name
        self.handle = _k32.CreateEventW(None, False, False, name)     # auto-reset, initially unset
        if not self.handle:
            log.error("CreateEventW(%s) failed: error %s", name, ctypes.get_last_error())

    def poll(self) -> bool:
        return bool(self.handle) and _k32.WaitForSingleObject(self.handle, 0) == WAIT_OBJECT_0

    def close(self) -> None:
        if self.handle:
            _k32.CloseHandle(self.handle)
            self.handle = None
