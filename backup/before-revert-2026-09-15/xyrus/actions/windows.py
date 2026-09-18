"""Top-level windows: list, find by app, focus, close, kill, min/max/restore, snap, topmost, desktops.
F7/F8 snippets (DWM cloak filter, ALT-tap foreground, SetWindowPos snap with DWM border compensation,
WM_CLOSE, taskkill). Never speaks (G10). Thread: T-exec (or the smoke test)."""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import logging
import os
import re

from xyrus import winutil
from xyrus.actions import keys

log = logging.getLogger("xyrus.actions.windows")

_u = ctypes.windll.user32
_dwm = ctypes.windll.dwmapi
EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
_u.GetWindowTextLengthW.argtypes = [wt.HWND]
_u.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
_u.IsWindowVisible.argtypes = [wt.HWND]
_u.GetForegroundWindow.restype = wt.HWND
_u.GetWindow.argtypes = [wt.HWND, ctypes.c_uint]
_u.GetWindow.restype = wt.HWND
_u.PostMessageW.argtypes = [wt.HWND, ctypes.c_uint, wt.WPARAM, wt.LPARAM]
_u.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
_u.IsZoomed.argtypes = [wt.HWND]
_u.IsIconic.argtypes = [wt.HWND]
_u.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
_u.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
_u.MonitorFromWindow.argtypes = [wt.HWND, wt.DWORD]
_u.MonitorFromWindow.restype = wt.HANDLE
_dwm.DwmGetWindowAttribute.argtypes = [wt.HWND, wt.DWORD, ctypes.c_void_p, wt.DWORD]

SW_SHOWNORMAL, SW_MAXIMIZE, SW_MINIMIZE, SW_RESTORE = 1, 3, 6, 9
WM_CLOSE = 0x0010
GW_OWNER = 4
DWMWA_EXTENDED_FRAME_BOUNDS, DWMWA_CLOAKED = 9, 14
MONITOR_DEFAULTTONEAREST = 2
SWP_NOSIZE, SWP_NOMOVE, SWP_NOZORDER, SWP_NOACTIVATE = 0x1, 0x2, 0x4, 0x10
HWND_TOPMOST, HWND_NOTOPMOST = -1, -2
VK_LWIN, VK_CONTROL, VK_LEFT, VK_RIGHT, VK_D, VK_F4 = 0x5B, 0x11, 0x25, 0x27, 0x44, 0x73

EXCLUDED = {"explorer", "applicationframehost", "textinputhost", "shellexperiencehost",
            "searchhost", "startmenuexperiencehost", "lockapp"}
# config alias target -> the process stem that actually owns its window
TARGET_PROCESS = {"calc": "calculatorapp", "wt": "windowsterminal", "code": "code", "msedge": "msedge",
                  "taskmgr": "taskmgr", "notepad": "notepad", "chrome": "chrome", "spotify": "spotify",
                  "discord": "discord"}


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT), ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]


def _stem(name: str) -> str:
    n = name.lower().strip()
    return n[:-4] if n.endswith(".exe") else n


def _proc_name(pid: int, cache: dict[int, str]) -> str:
    if pid not in cache:
        try:
            import psutil
            cache[pid] = _stem(psutil.Process(pid).name())
        except Exception:
            cache[pid] = ""
    return cache[pid]


def _title(hwnd: int) -> str:
    n = _u.GetWindowTextLengthW(hwnd)
    if not n:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    _u.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def _cloaked(hwnd: int) -> bool:
    c = ctypes.c_int(0)
    try:
        _dwm.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(c), ctypes.sizeof(c))
    except Exception:
        return False
    return bool(c.value)


def list_windows() -> list[tuple[int, str, int, str]]:
    """Visible, titled, uncloaked, unowned top-level windows: (hwnd, title, pid, process stem)."""
    raw: list[tuple[int, str, int]] = []

    def cb(hwnd, _):
        if _u.IsWindowVisible(hwnd) and not _u.GetWindow(hwnd, GW_OWNER):
            t = _title(hwnd)
            if t and not _cloaked(hwnd):
                raw.append((int(hwnd), t, winutil.window_pid(hwnd)))
        return True

    _u.EnumWindows(EnumWindowsProc(cb), 0)
    cache: dict[int, str] = {}
    return [(h, t, pid, _proc_name(pid, cache)) for h, t, pid in raw]


def _user_windows() -> list[tuple[int, str, int, str]]:
    own = winutil.own_pids()
    return [w for w in list_windows() if w[2] not in own and w[3] and w[3] not in EXCLUDED]


def windowed_apps() -> list[str]:
    """Process names (lowercase, no .exe) that own a visible window, excluding shell hosts and Xyrus."""
    seen: dict[str, None] = {}
    for _h, _t, _pid, name in _user_windows():
        seen.setdefault(name, None)
    return list(seen)


def _alias_process(alias_target: str | None) -> str | None:
    if not alias_target:
        return None
    t = alias_target.strip().strip('"')
    if "://" in t or t.startswith(("ms-settings:", "shell:")):
        return None
    if re.fullmatch(r"[a-z0-9-]+:", t.lower()):          # 'spotify:' / 'discord:' URIs
        return t[:-1].lower()
    base = _stem(os.path.basename(t))
    return TARGET_PROCESS.get(base, base) or None


def find_app_windows(app: str, alias_target: str | None = None) -> list[tuple[int, str, int, str]]:
    """Windows of the app: alias target process, then exact process stem, then prefix/substring of the
    stem (spaces removed, 'note pad' -> notepad), then window-title substring. Best tier only."""
    spoken = app.lower().strip()
    squashed = spoken.replace(" ", "")
    if not squashed:
        return []
    wins = _user_windows()
    proc = _alias_process(alias_target)
    tiers = []
    if proc:
        tiers.append([w for w in wins if w[3] == proc])
    tiers.append([w for w in wins if w[3] == squashed])
    if len(squashed) >= 3:
        tiers.append([w for w in wins if w[3].startswith(squashed) or squashed in w[3]])
        tiers.append([w for w in wins if spoken in w[1].lower() or squashed in w[1].lower().replace(" ", "")])
    for tier in tiers:
        if tier:
            return tier
    return []


def find_app_window(app: str, alias_target: str | None = None) -> int | None:
    wins = find_app_windows(app, alias_target)
    return wins[0][0] if wins else None


def focus_app(app: str, alias_target: str | None = None) -> bool:
    hwnd = find_app_window(app, alias_target)
    if hwnd is None:
        return False
    winutil.force_foreground(hwnd)
    return True


def close_app(app: str, alias_target: str | None = None) -> bool:
    """WM_CLOSE to every visible top-level window of the matching process(es). False if none is open."""
    wins = find_app_windows(app, alias_target)
    if not wins:
        return False
    procs = {w[3] for w in wins}
    for hwnd, _t, _pid, name in _user_windows():
        if name in procs:
            _u.PostMessageW(hwnd, WM_CLOSE, 0, 0)
    return True


def kill_app(app: str, alias_target: str | None = None) -> bool:
    """taskkill /F every process with the matching image name (never shell hosts or Xyrus itself)."""
    wins = find_app_windows(app, alias_target)
    names = {w[3] for w in wins} or ({app.lower().replace(" ", "")} - EXCLUDED)
    import psutil
    own = winutil.own_pids()
    pids = []
    for p in psutil.process_iter(["pid", "name"]):
        try:
            if _stem(p.info["name"] or "") in names and p.info["pid"] not in own:
                pids.append(p.info["pid"])
        except Exception:
            continue
    if not pids:
        return False
    args = ["taskkill", "/F"]
    for pid in pids:
        args += ["/PID", str(pid)]
    r = winutil.run(args, capture=True, timeout=10)
    if r.returncode != 0:
        log.warning("taskkill %s -> %s %s", names, r.returncode, (r.stderr or r.stdout).strip())
    return r.returncode == 0


def foreground_hwnd() -> int:
    return int(_u.GetForegroundWindow() or 0)


def foreground_is_self() -> bool:
    h = foreground_hwnd()
    return bool(h) and winutil.window_pid(h) in winutil.own_pids()


def window_cmd(cmd: str, hwnd: int | None = None) -> None:
    """minimize | maximize | restore (SW_SHOWNORMAL) | close (Alt+F4, v1) | topmost | notopmost."""
    h = hwnd or foreground_hwnd()
    if not h:
        raise RuntimeError("no foreground window")
    if cmd == "minimize":
        _u.ShowWindow(h, SW_MINIMIZE)
    elif cmd == "maximize":
        _u.ShowWindow(h, SW_MAXIMIZE)
    elif cmd == "restore":
        _u.ShowWindow(h, SW_SHOWNORMAL)
    elif cmd == "close":
        if winutil.window_pid(h) in winutil.own_pids():
            raise RuntimeError("refusing to close Xyrus's own window")
        if hwnd:
            _u.PostMessageW(h, WM_CLOSE, 0, 0)
        else:
            keys.chord("alt+f4")
    elif cmd in ("topmost", "notopmost"):
        after = HWND_TOPMOST if cmd == "topmost" else HWND_NOTOPMOST
        if not _u.SetWindowPos(h, after, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE):
            raise OSError("SetWindowPos failed")
    else:
        raise ValueError(f"unknown window command {cmd!r}")


def work_area(hwnd: int) -> tuple[int, int, int, int]:
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(MONITORINFO)
    _u.GetMonitorInfoW(_u.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST), ctypes.byref(mi))
    w = mi.rcWork
    return (w.left, w.top, w.right, w.bottom)


def window_rect(hwnd: int) -> tuple[int, int, int, int]:
    r = wt.RECT()
    _u.GetWindowRect(hwnd, ctypes.byref(r))
    return (r.left, r.top, r.right, r.bottom)


def visible_rect(hwnd: int) -> tuple[int, int, int, int]:
    """The window's visible frame (DWM extended frame bounds, excludes the invisible resize border)."""
    ext = wt.RECT()
    if _dwm.DwmGetWindowAttribute(hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(ext), ctypes.sizeof(ext)) == 0:
        return (ext.left, ext.top, ext.right, ext.bottom)
    return window_rect(hwnd)


def snap(side: str, hwnd: int | None = None) -> None:
    """D7/F7b: SetWindowPos into the left/right half of the monitor work area with DWM border
    compensation. Never Win+arrow (Snap Assist steals focus on Windows 11)."""
    if side not in ("left", "right"):
        raise ValueError(f"bad side {side!r}")
    h = hwnd or foreground_hwnd()
    if not h:
        raise RuntimeError("no foreground window")
    if _u.IsZoomed(h) or _u.IsIconic(h):
        _u.ShowWindow(h, SW_SHOWNORMAL)
    left, top, right, bottom = work_area(h)
    half = (right - left) // 2
    x = left if side == "left" else left + half
    wl, wtp, wr, wb = window_rect(h)
    el, et, er, eb = visible_rect(h)
    bl, bt, br, bb = el - wl, et - wtp, wr - er, wb - eb
    if not _u.SetWindowPos(h, 0, x - bl, top - bt, half + bl + br, (bottom - top) + bt + bb,
                           SWP_NOZORDER | SWP_NOACTIVATE):
        raise OSError("SetWindowPos failed")


def show_desktop() -> None:
    keys.release_stuck_modifiers()
    keys.tap(VK_LWIN, VK_D)


_DESKTOP_KEYS = {"new": VK_D, "next": VK_RIGHT, "prev": VK_LEFT, "close": VK_F4}


def desktop(cmd: str) -> None:
    """Virtual desktops via Win+Ctrl chords (F12) after releasing stuck modifiers."""
    if cmd not in _DESKTOP_KEYS:
        raise ValueError(f"unknown desktop command {cmd!r}")
    keys.release_stuck_modifiers()
    keys.tap(VK_LWIN, VK_CONTROL, _DESKTOP_KEYS[cmd])
