"""Brightness (F2, screen_brightness_control over DDC/CI) and dark/light theme. Never speaks (G10)."""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import logging
import winreg

log = logging.getLogger("xyrus.actions.display")

PERSONALIZE = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
HWND_BROADCAST = 0xFFFF
WM_SETTINGCHANGE = 0x001A
SMTO_ABORTIFHUNG = 0x0002


def brightness_get() -> int | None:
    """Brightness of the first display 0..100, or None when no display supports it."""
    try:
        import screen_brightness_control as sbc
        vals = sbc.get_brightness()
        return int(vals[0]) if vals else None
    except Exception as e:      # ScreenBrightnessError, WMI/DDC failures
        log.info("brightness_get unsupported: %s", e)
        return None


def brightness_set(value: int | str) -> int | None:
    """value = 50 or a relative '+15' / '-15'. Returns the new level, None if unsupported."""
    try:
        import screen_brightness_control as sbc
        if isinstance(value, str):
            v = value.strip()
            if not v or v[0] not in "+-" or not v[1:].isdigit():
                raise ValueError(f"bad relative brightness {value!r}")
        else:
            v = max(0, min(100, int(value)))
        sbc.set_brightness(v)
        vals = sbc.get_brightness()
        return int(vals[0]) if vals else None
    except ValueError:
        raise
    except Exception as e:
        log.info("brightness_set unsupported: %s", e)
        return None


def theme_get() -> bool | None:
    """True if apps use the dark theme, None if unknown."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, PERSONALIZE) as k:
            return winreg.QueryValueEx(k, "AppsUseLightTheme")[0] == 0
    except OSError:
        return None


def set_theme(dark: bool) -> None:
    val = 0 if dark else 1
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, PERSONALIZE, 0, winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, "AppsUseLightTheme", 0, winreg.REG_DWORD, val)
        winreg.SetValueEx(k, "SystemUsesLightTheme", 0, winreg.REG_DWORD, val)
    res = wt.DWORD()
    ctypes.windll.user32.SendMessageTimeoutW(HWND_BROADCAST, WM_SETTINGCHANGE, 0, "ImmersiveColorSet",
                                             SMTO_ABORTIFHUNG, 100, ctypes.byref(res))
