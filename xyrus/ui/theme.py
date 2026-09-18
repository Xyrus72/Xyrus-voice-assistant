"""Palette, fonts and Light/Dark handling of the Xyrus window.

Every colour is a customtkinter (light, dark) pair, so CTk widgets switch by themselves when
ctk.set_appearance_mode() changes. Plain tk widgets and canvas drawings (feed rows, command rows, the
calendar's day numbers and dots) resolve a pair with c() and re-render on the window's theme callback.
The dark values are the v1 palette (§4.1); the light palette keeps the #7c5cff accent family but darker
where it carries text, so everything stays readable on the off-white ground.
"""
from __future__ import annotations

import logging

import customtkinter as ctk

log = logging.getLogger("xyrus.ui.theme")

APPEARANCES = ("dark", "light", "system")          # config ui.appearance

#                 light       dark
ACCENT = ("#6246ea", "#7c5cff")            # fills with white text AND accent-coloured text
ACCENT_HOVER = ("#5136d4", "#6a4ce6")
ACCENT_SOFT = ("#5a44c8", "#a996ff")       # event times, today's number
GREEN = ("#0f7a43", "#3ddc84")            # light values all >= 4.5:1 on BG (tests/test_ui_render contrast)
GREEN_HOVER = ("#0b6336", "#2fb86c")
RED = ("#c42525", "#ff5c5c")
RED_HOVER = ("#a81c1c", "#e04848")
RED_BG = ("#fde8e8", "#3a1c22")
AMBER = ("#9a5a00", "#ffb347")
NOTE = ("#9c5f00", "#f2b84b")              # notes (v1 calendar amber)
AMBER_BG = ("#fff4e0", "#2a2216")          # parked dialogue banner
MUTED = ("#62626f", "#9a9aa8")
FAINT = ("#80808c", "#6e6e7a")
TEXT = ("#1b1b22", "#ececf2")
TITLE = ("#101016", "#ffffff")
ON_ACCENT = ("#ffffff", "#ffffff")         # text on accent fills
ON_ACCENT_SOFT = ("#e6e0ff", "#d9d2ff")    # done-tick on the selected cell
CARD = ("#ffffff", "#1b1b22")
BG = ("#f3f3f7", "#121216")
SURFACE = ("#ffffff", "#1d1d25")           # a day cell of the shown month
SURFACE_HOVER = ("#ece8ff", "#272731")
DIM = ("#b0b0bc", "#4d4d59")               # neighbouring months
RANGE = ("#e4ddff", "#262042")             # highlighted range ("this week")
SEL_RING = ("#c2b4ff", "#cfc4ff")          # today's outline while selected
LINE = ("#e2e2ea", "#2b2b36")              # neutral buttons, borders
LINE_HOVER = ("#d2d2dd", "#363644")
WEEKEND = ("#85859a", "#7d7d8c")
CHECK_BORDER = ("#9c9cab", "#5b5b68")
STAR = ("#cbc2f2", "#3a3358")
SEG_SEL = ("#d9cfff", "#7c5cff")           # selected segment: lavender chip on light, accent on dark
SEG_SEL_HOVER = ("#cbbdff", "#6a4ce6")
SEG_TEXT = ("#1b1b22", "#ffffff")
TAB_UNSEL = ("#e4e4ec", "gray29")          # CTkTabview segments (dark = CTk's default look)
TAB_UNSEL_HOVER = ("#d6d6e0", "gray41")

MONO = "Consolas"

# customtkinter's default button / option-menu text is near-white in BOTH modes; always pass these.
SEG = dict(selected_color=SEG_SEL, selected_hover_color=SEG_SEL_HOVER, unselected_color=LINE,
           unselected_hover_color=LINE_HOVER, fg_color=LINE, text_color=SEG_TEXT)
MENU = dict(fg_color=LINE, button_color=LINE, button_hover_color=LINE_HOVER, text_color=TEXT)


# ------------------------------------------------------------------ appearance --
def mode() -> str:
    """The effective mode right now: "light" | "dark"."""
    return "light" if str(ctk.get_appearance_mode()).lower() == "light" else "dark"


def c(color):
    """Resolve a (light, dark) pair for plain tk widgets / canvases; plain strings pass through."""
    if isinstance(color, (tuple, list)):
        return color[0] if mode() == "light" else color[1]
    return color


def windows_light() -> bool | None:
    """Windows' app theme (HKCU ...\\Personalize\\AppsUseLightTheme); None if it can't be read."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            return bool(winreg.QueryValueEx(k, "AppsUseLightTheme")[0])
    except OSError:
        return None


def effective(setting: str | None) -> str:
    setting = (setting or "dark").lower()
    if setting == "light":
        return "light"
    if setting == "system":
        return "light" if windows_light() else "dark"
    return "dark"


def apply(setting: str | None) -> str:
    """Set customtkinter's mode for a config value ("dark" | "light" | "system"); returns the effective one.
    "system" is resolved here (not by CTk) so the window decides when to follow Windows."""
    eff = effective(setting)
    ctk.set_appearance_mode(eff)
    return eff


# ------------------------------------------------------------------------ fonts --
_fonts: dict[tuple, ctk.CTkFont] = {}


def font(size: int = 13, weight: str = "normal", family: str | None = None, **kw) -> ctk.CTkFont:
    """Shared CTkFont instances (need a Tk root to exist)."""
    key = (size, weight, family, tuple(sorted(kw.items())))
    f = _fonts.get(key)
    if f is None:
        args = dict(size=size, weight=weight, **kw)
        if family:
            args["family"] = family
        f = _fonts[key] = ctk.CTkFont(**args)
    return f


def reset_fonts() -> None:
    """Forget cached fonts (a destroyed root invalidates them; tests build several windows)."""
    _fonts.clear()


def button(master, text: str, command=None, *, primary: bool = False, danger: bool = False,
           width: int = 90, height: int = 30, **kw) -> ctk.CTkButton:
    """The two button looks of v1: accent (primary) and grey (secondary)."""
    if primary:
        colors = dict(fg_color=ACCENT, hover_color=ACCENT_HOVER, text_color=ON_ACCENT)
    else:
        colors = dict(fg_color=LINE, hover_color=RED if danger else LINE_HOVER, text_color=TEXT)
    colors.update(kw)
    return ctk.CTkButton(master, text=text, command=command, width=width, height=height,
                         corner_radius=8, **colors)
