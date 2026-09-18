"""Small Windows helpers shared by actions/, ui/ and the shell (§4.4).

Every subprocess goes through run()/popen() so it never flashes a console (G2). Thread: any."""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import logging
import os
import subprocess

log = logging.getLogger("xyrus.winutil")

NO_WINDOW = 0x08000000          # CREATE_NO_WINDOW
DETACHED_PROCESS = 0x00000008

_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32
_user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
_user32.GetWindowThreadProcessId.restype = wt.DWORD
_user32.GetForegroundWindow.restype = wt.HWND
_user32.SetForegroundWindow.argtypes = [wt.HWND]
_user32.BringWindowToTop.argtypes = [wt.HWND]
_user32.IsIconic.argtypes = [wt.HWND]
_user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
_user32.AttachThreadInput.argtypes = [wt.DWORD, wt.DWORD, wt.BOOL]

SW_RESTORE = 9
VK_MENU = 0x12
KEYEVENTF_KEYUP = 0x0002


def run(args: list[str] | str, *, timeout: float = 15, capture: bool = False,
        shell: bool = False) -> subprocess.CompletedProcess:
    """Run a command to completion without a console window. capture=True returns text stdout/stderr."""
    kw: dict = {"creationflags": NO_WINDOW, "timeout": timeout, "shell": shell, "stdin": subprocess.DEVNULL}
    if capture:
        kw.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors="replace")
    else:
        kw.update(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return subprocess.run(args, **kw)


def popen(args: list[str] | str, *, shell: bool = False) -> subprocess.Popen:
    """Detached launch (fire and forget) with DEVNULL handles and no console window."""
    return subprocess.Popen(args, shell=shell, creationflags=NO_WINDOW, close_fds=True,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def open_url(url_or_path: str) -> None:
    """Open a URL / file / URI with its default handler (os.startfile). Logs and re-raises OSError."""
    try:
        os.startfile(url_or_path)  # type: ignore[attr-defined]
    except OSError as e:
        log.warning("open_url(%r) failed: %s", url_or_path, e)
        raise


def window_pid(hwnd: int) -> int:
    pid = wt.DWORD()
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def force_foreground(hwnd: int) -> bool:
    """F7 snippet: restore if iconic, ALT tap (unlocks SetForegroundWindow), then the AttachThreadInput
    fallback. True if the window ended up in the foreground."""
    if not hwnd:
        return False
    if _user32.IsIconic(hwnd):
        _user32.ShowWindow(hwnd, SW_RESTORE)
    if _user32.GetForegroundWindow() == hwnd:
        return True
    # Try without the ALT tap first: a stray ALT press/release lands in the current foreground window and
    # switches WinUI apps (Windows 11 Notepad) into access-key mode, which then swallows typed keys.
    if _user32.SetForegroundWindow(hwnd) and _user32.GetForegroundWindow() == hwnd:
        return True
    _user32.keybd_event(VK_MENU, 0, 0, 0)
    _user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
    ok = _user32.SetForegroundWindow(hwnd)
    if not ok or _user32.GetForegroundWindow() != hwnd:
        fg = _user32.GetForegroundWindow()
        fg_tid = _user32.GetWindowThreadProcessId(fg, None) if fg else 0
        my_tid = _kernel32.GetCurrentThreadId()
        attached = bool(fg_tid) and fg_tid != my_tid and _user32.AttachThreadInput(my_tid, fg_tid, True)
        try:
            _user32.BringWindowToTop(hwnd)
            _user32.SetForegroundWindow(hwnd)
        finally:
            if attached:
                _user32.AttachThreadInput(my_tid, fg_tid, False)
    return _user32.GetForegroundWindow() == hwnd


# Stable reference to the real implementation (another suite replaces `force_foreground` with a recorder).
_force_foreground_impl = force_foreground

_OWN: set[int] | None = None


def own_pids() -> set[int]:
    """os.getpid() plus the parent when it is python/pythonw (the venv launcher is a 2-process pair)."""
    global _OWN
    if _OWN is None:
        pids = {os.getpid()}
        try:
            import psutil
            parent = psutil.Process(os.getpid()).parent()
            if parent is not None and parent.name().lower().startswith("python"):
                pids.add(parent.pid)
        except Exception as e:  # psutil missing / access denied: own pid only
            log.debug("own_pids parent lookup failed: %s", e)
        _OWN = pids
    return set(_OWN)
