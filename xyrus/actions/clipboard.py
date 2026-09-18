"""Clipboard via ctypes with an OpenClipboard retry (F10). Never speaks (G10)."""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import time

_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

_kernel32.GlobalLock.restype = ctypes.c_void_p
_kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
_kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
_kernel32.GlobalAlloc.restype = ctypes.c_void_p
_kernel32.GlobalAlloc.argtypes = [wt.UINT, ctypes.c_size_t]
_kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
_kernel32.GlobalFree.restype = ctypes.c_void_p
_user32.OpenClipboard.argtypes = [wt.HWND]
_user32.GetClipboardData.restype = ctypes.c_void_p
_user32.GetClipboardData.argtypes = [wt.UINT]
_user32.SetClipboardData.argtypes = [wt.UINT, ctypes.c_void_p]
_user32.SetClipboardData.restype = ctypes.c_void_p


def _open() -> None:
    for _ in range(5):
        if _user32.OpenClipboard(None):
            return
        time.sleep(0.02)
    raise OSError("OpenClipboard failed")


def clip_get() -> str:
    _open()
    try:
        h = _user32.GetClipboardData(CF_UNICODETEXT)
        if not h:
            return ""
        p = _kernel32.GlobalLock(h)
        if not p:
            return ""
        try:
            return ctypes.wstring_at(p)
        finally:
            _kernel32.GlobalUnlock(h)
    finally:
        _user32.CloseClipboard()


def clip_clear() -> None:
    _open()
    try:
        _user32.EmptyClipboard()
    finally:
        _user32.CloseClipboard()


def clip_set(text: str) -> None:
    """Replace the clipboard with text. An empty string clears it."""
    if not text:
        clip_clear()
        return
    data = text.encode("utf-16-le") + b"\x00\x00"
    h = _kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
    if not h:
        raise OSError("GlobalAlloc failed")
    p = _kernel32.GlobalLock(h)
    ctypes.memmove(p, data, len(data))
    _kernel32.GlobalUnlock(h)
    try:
        _open()
    except OSError:
        _kernel32.GlobalFree(h)
        raise
    try:
        _user32.EmptyClipboard()
        if not _user32.SetClipboardData(CF_UNICODETEXT, h):
            _kernel32.GlobalFree(h)
            raise OSError("SetClipboardData failed")
    finally:
        _user32.CloseClipboard()
