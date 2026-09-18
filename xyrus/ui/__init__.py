"""Xyrus window (customtkinter). Tk main thread only (G4).

Small helpers shared by the tabs live here so every tab reads/writes config the same way.
"""
from __future__ import annotations

import copy
import logging
import os
from pathlib import Path
from typing import Any

log = logging.getLogger("xyrus.ui")

try:                                    # T1's paths.py; the fallback mirrors §4.4 exactly
    from xyrus import paths as _paths
    BASE: Path = _paths.BASE
    LOG_FILE: Path = _paths.LOG_FILE
    ICON_FILE: Path = _paths.ICON_FILE
    DATA_DIR: Path = _paths.DATA_DIR
except ImportError:                     # pragma: no cover - only before T1 lands
    BASE = Path(__file__).resolve().parent.parent.parent
    LOG_FILE = BASE / "arc.log"
    ICON_FILE = BASE / "xyrus.ico"
    DATA_DIR = Path(os.environ.get("XYRUS_DATA_DIR", BASE / "data"))


def data_dir() -> Path:
    """DATA_DIR resolved now, not at import (tests change XYRUS_DATA_DIR after xyrus.paths was imported)."""
    try:
        from xyrus import paths
        return paths.data_dir()
    except (ImportError, AttributeError):
        env = os.environ.get("XYRUS_DATA_DIR")
        return Path(env) if env else BASE / "data"


def cfg_get(config, key: str, default: Any = None) -> Any:
    """Dotted read ("calendar.snooze_minutes"); a stored null also yields `default`."""
    try:
        val = config.get(key, None)
    except Exception:
        log.exception("config.get(%s) failed", key)
        return default
    return default if val is None else val


def cfg_set(config, key: str, value: Any) -> bool:
    """Dotted write through Config.set (atomic save + on_change callbacks for exactly this key).
    Never raises into a Tk callback; returns False on failure."""
    try:
        config.set(key, copy.deepcopy(value))
        return True
    except Exception:
        log.exception("config.set(%s) failed", key)
        return False


def open_path(target: str) -> None:
    """Open a file/folder/URL with the shell (winutil.open_url when T4's module is there)."""
    try:
        from xyrus import winutil
        winutil.open_url(str(target))
    except ImportError:
        os.startfile(str(target))            # noqa: S606 - same as winutil.open_url


def sir_text(config, template: str) -> str:
    """Render {sir} for the few UI-spoken lines (Test voice)."""
    try:
        from xyrus import persona
        return persona.render(template, config)
    except Exception:
        addr = (config.get("address_as", "sir") or "").strip()
        return template.replace("{sir}", f", {addr}" if addr else "")
