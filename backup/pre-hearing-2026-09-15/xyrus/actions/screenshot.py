"""Screenshots (v1 code + foreground-window bbox, §3.7). Never speaks (G10)."""
from __future__ import annotations

import ctypes
import datetime as dt
from pathlib import Path


def default_dir() -> Path:
    try:
        from xyrus import paths
        return Path(paths.SCREENSHOT_DIR)
    except (ImportError, AttributeError):        # paths.py not there yet (parallel build)
        return Path.home() / "Pictures" / "Xyrus"


def enable_dpi_awareness() -> None:
    """Call once at startup (app.py) so window bboxes are in physical pixels (§3.7)."""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass


def screenshot(window: bool = False, folder: Path | None = None) -> Path:
    from PIL import ImageGrab
    folder = Path(folder) if folder else default_dir()
    folder.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("Screenshot_%Y-%m-%d_%H-%M-%S")
    path = folder / f"{stamp}.png"
    n = 2
    while path.exists():
        path = folder / f"{stamp}_{n}.png"
        n += 1
    if window:
        from xyrus.actions import windows
        h = windows.foreground_hwnd()
        if not h:
            raise RuntimeError("no foreground window")
        l, t, r, b = windows.visible_rect(h)
        if r - l < 2 or b - t < 2:
            raise RuntimeError("window has no size")
        img = ImageGrab.grab(bbox=(l, t, r, b), all_screens=True)
    else:
        img = ImageGrab.grab(all_screens=True)
    img.save(path)
    return path


def last_screenshot(folder: Path | None = None) -> Path | None:
    folder = Path(folder) if folder else default_dir()
    if not folder.is_dir():
        return None
    shots = [p for p in folder.glob("*.png") if p.is_file()]
    return max(shots, key=lambda p: p.stat().st_mtime) if shots else None
