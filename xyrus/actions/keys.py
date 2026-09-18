"""Key injection (F12, D6): keybd_event with real scan codes after releasing stuck modifiers.
Text typing uses keyboard.write (F3/F15). Runs on T-exec. Never speaks (G10)."""
from __future__ import annotations

import ctypes
import logging
import time

log = logging.getLogger("xyrus.actions.keys")

_user32 = ctypes.windll.user32
_user32.MapVirtualKeyW.restype = ctypes.c_uint
_user32.MapVirtualKeyW.argtypes = [ctypes.c_uint, ctypes.c_uint]
_user32.GetAsyncKeyState.restype = ctypes.c_short
_user32.GetAsyncKeyState.argtypes = [ctypes.c_int]

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
HOLD_S = 0.03   # between the downs and the ups

VK_SHIFT, VK_CONTROL, VK_MENU, VK_LWIN, VK_RWIN = 0x10, 0x11, 0x12, 0x5B, 0x5C

VK: dict[str, int] = {
    "enter": 0x0D, "return": 0x0D, "tab": 0x09, "escape": 0x1B, "esc": 0x1B, "space": 0x20,
    "backspace": 0x08, "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27, "home": 0x24, "end": 0x23,
    "delete": 0x2E, "del": 0x2E, "insert": 0x2D, "pageup": 0x21, "pagedown": 0x22,
    "ctrl": VK_CONTROL, "control": VK_CONTROL, "alt": VK_MENU, "shift": VK_SHIFT,
    "win": VK_LWIN, "windows": VK_LWIN, "lwin": VK_LWIN, "rwin": VK_RWIN,
    "vol_up": 0xAF, "vol_down": 0xAE, "vol_mute": 0xAD,
    "play_pause": 0xB3, "next": 0xB0, "prev": 0xB1, "stop": 0xB2,
}
VK.update({f"f{i}": 0x6F + i for i in range(1, 13)})              # f1 = 0x70
VK.update({chr(c): c - 0x20 for c in range(ord("a"), ord("z") + 1)})  # 'a' -> 0x41
VK.update({str(d): 0x30 + d for d in range(10)})

MODIFIER_VKS = {VK_SHIFT, VK_CONTROL, VK_MENU, VK_LWIN, VK_RWIN}
# every modifier the OS may consider held (generic + left/right)
_ALL_MODS = (0x10, 0x11, 0x12, 0x5B, 0x5C, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5)
# keys whose scan codes live in the extended set (arrows/nav block, win keys, media/volume keys)
_EXTENDED = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E, 0x5B, 0x5C,
             0xAD, 0xAE, 0xAF, 0xB0, 0xB1, 0xB2, 0xB3, 0xA3, 0xA5}


def parse_chord(spec: str) -> list[int]:
    """'ctrl+shift+t' -> [0x11, 0x10, 0x54]. Also accepts spaces around '+' and 'page up'.
    Raises ValueError on an unknown key name."""
    s = spec.strip().lower().replace("page up", "pageup").replace("page down", "pagedown")
    if not s:
        raise ValueError("empty key chord")
    out: list[int] = []
    for part in s.split("+"):
        name = part.strip().replace(" ", "")
        if name not in VK:
            raise ValueError(f"unknown key {part.strip()!r}")
        out.append(VK[name])
    return out


def _event(vk: int, up: bool) -> None:
    flags = (KEYEVENTF_KEYUP if up else 0) | (KEYEVENTF_EXTENDEDKEY if vk in _EXTENDED else 0)
    _user32.keybd_event(vk, _user32.MapVirtualKeyW(vk, 0) & 0xFF, flags, 0)


def release_stuck_modifiers() -> list[int]:
    """Send key-up for any modifier the OS reports as down (stuck-SHIFT hazard, F3/F12). Returns the VKs released."""
    released = []
    for vk in _ALL_MODS:
        if _user32.GetAsyncKeyState(vk) & 0x8000:
            _event(vk, up=True)
            released.append(vk)
    if released:
        log.info("released stuck modifiers: %s", [hex(v) for v in released])
    return released


def tap(*vks: int, times: int = 1) -> None:
    """Press all vks in order, hold 30 ms, release in reverse order."""
    for n in range(times):
        for v in vks:
            _event(v, up=False)
        time.sleep(HOLD_S if len(vks) > 1 else 0.01)
        for v in reversed(vks):
            _event(v, up=True)
        if times > 1 and n < times - 1:
            time.sleep(0.01)


def chord(spec: str) -> None:
    """Send a chord like 'ctrl+shift+t', 'enter', 'f11' after releasing stuck modifiers."""
    vks = parse_chord(spec)
    release_stuck_modifiers()
    tap(*vks)


def type_text(text: str) -> None:
    """Type text into the focused window (keyboard.write, verified under pythonw)."""
    import keyboard
    release_stuck_modifiers()
    keyboard.write(text, delay=0.01)
