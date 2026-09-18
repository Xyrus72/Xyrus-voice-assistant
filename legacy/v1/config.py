"""Persistent settings for Xyrus (config.json next to this file)."""
import json
import threading
from pathlib import Path

BASE = Path(__file__).resolve().parent
CONFIG_FILE = BASE / "config.json"

DEFAULTS = {
    "voice_replies": True,       # speak replies out loud
    "confirm_shutdown": True,    # ask "yes?" before shutdown / restart
    # spoken name -> what to launch (anything `start` understands: exe, URL, URI)
    "apps": {
        "chrome": "chrome",
        "browser": "chrome",
        "edge": "msedge",
        "youtube": "https://www.youtube.com",
        "spotify": "spotify:",
        "notepad": "notepad",
        "calculator": "calc",
        "explorer": "explorer",
        "files": "explorer",
        "code": "code",
        "settings": "ms-settings:",
        "task manager": "taskmgr",
        "terminal": "wt",
        "discord": "discord:",
    },
}

_lock = threading.Lock()
_data: dict = {}


def load() -> dict:
    global _data
    with _lock:
        data = json.loads(json.dumps(DEFAULTS))  # deep copy
        if CONFIG_FILE.exists():
            try:
                saved = json.loads(CONFIG_FILE.read_text(encoding="utf8"))
                for k, v in saved.items():
                    data[k] = v
            except Exception:
                pass
        _data = data
        return _data


def get(key, default=None):
    if not _data:
        load()
    return _data.get(key, default)


def set_(key, value):
    if not _data:
        load()
    with _lock:
        _data[key] = value
        CONFIG_FILE.write_text(json.dumps(_data, indent=2), encoding="utf8")


def all_():
    if not _data:
        load()
    return _data
