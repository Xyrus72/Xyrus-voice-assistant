"""Filesystem locations. Constants only; nothing is created on import.

XYRUS_DATA_DIR (tests) redirects DATA_DIR and CONFIG_FILE. The constants are read once at import;
code that may run after a test changed the env var should call data_dir() / config_file().
"""
from __future__ import annotations

import os
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
MODEL_DIR = BASE / "model"


def data_dir() -> Path:
    env = os.environ.get("XYRUS_DATA_DIR")
    return Path(env) if env else BASE / "data"


def config_file() -> Path:
    env = os.environ.get("XYRUS_DATA_DIR")
    return Path(env) / "config.json" if env else BASE / "config.json"


DATA_DIR = data_dir()
CONFIG_FILE = config_file()
LOG_FILE = BASE / "arc.log"
CRASH_FILE = BASE / "crash.log"
ICON_FILE = BASE / "xyrus.ico"
SCREENSHOT_DIR = Path.home() / "Pictures" / "Xyrus"
