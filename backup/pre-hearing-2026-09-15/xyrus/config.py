"""Settings (config.json, schema version 2, §6.1). Thread-safe (RLock); atomic save (G7).

Imports no xyrus module except paths.
"""
from __future__ import annotations

import copy
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Callable

from xyrus import paths

log = logging.getLogger("xyrus.config")

DEFAULTS: dict = {
    "version": 2,
    "voice_replies": True,
    "confirm_shutdown": True,
    "shutdown_delay_s": 10,
    "address_as": "sir",
    "user_name": "",
    "wake_words": ["cyrus", "zeros", "virus", "cirrus"],
    "wake_replies": ["Waiting for your command, sir.", "At your service, sir.", "Yes, sir?", "I'm listening, sir."],
    # wait this long for the command after "Waiting for your command"; 0 = no limit (Whisper hears the command
    # once the user has finished talking; without Whisper the Vosk fallback waits 60 s)
    "command_window_s": 0,
    "confirm_window_s": 12,
    "follow_up": True,
    "follow_up_s": 8,
    "follow_up_after_alert_s": 20,
    "dialogue_timeout_s": 20,
    "chime_on_wake": False,          # off: its tone fired on room noise that half-sounded like the name
    "startup_greeting": "speak",
    "briefing_on_startup": True,
    "min_speech_peak": 0.02,
    "mic": {"name": None, "hostapi": "MME"},
    "clap": {"enabled": True, "claps": 3, "min_peak": 0.12},   # clap to switch the display (claps: 1-4)
    "tts": {"voice": None, "rate": 0, "volume": 100},
    "youtube_lookup": True,
    "hotkeys": {"show_window": "ctrl+alt+x", "push_to_talk": "ctrl+alt+space", "stop_speaking": "ctrl+alt+s"},
    "apps": {
        "chrome": "chrome", "browser": "chrome", "edge": "msedge", "youtube": "https://www.youtube.com",
        "spotify": "spotify:", "notepad": "notepad", "calculator": "calc", "explorer": "explorer", "files": "explorer",
        "code": "code", "settings": "ms-settings:", "task manager": "taskmgr", "terminal": "wt", "discord": "discord:",
        "bluetooth settings": "ms-settings:bluetooth", "sound settings": "ms-settings:sound",
        "display settings": "ms-settings:display", "network settings": "ms-settings:network",
        "windows update": "ms-settings:windowsupdate", "downloads": "shell:Downloads", "documents": "shell:Personal",
        "pictures": "shell:My Pictures", "recycle bin": "shell:RecycleBinFolder", "device manager": "devmgmt.msc",
        "control panel": "control",
    },
    "custom_commands": [],
    "calendar": {
        "default_reminder_min": 10, "all_day_reminder_time": "09:00", "catch_up_min": 120, "snooze_minutes": 10,
        "morning_brief": True, "morning_brief_time": "08:30", "waking_hours": [7, 23],
        "repeat_unacked_after_min": 2, "toast": True,
    },
    "quiet_hours": None,
    "weather": {"enabled": False, "city": ""},
    "window": {"geometry": None, "last_tab": "Home", "show_noise": False},
}

V1_KEPT_KEYS = ("voice_replies", "confirm_shutdown", "apps")     # same name and meaning in v2
# v1 keys whose v2 home is elsewhere (dotted v2 key). v1 "briefing" = the automatic once-a-day briefing.
V1_RENAMED = {"briefing": "calendar.morning_brief"}
V1_BACKUP_NAME = "config.v1.json"
# Maps the user edits as a whole (a v2 file's value replaces the default instead of merging into it,
# so an app deleted in the Apps tab stays deleted).
REPLACED_MAPS = ("apps",)


def migrate_v1(saved: dict) -> dict:
    """v1 settings (a file without "version") -> overrides for the v2 DEFAULTS (§6.1): the kept keys as they
    are (v1 apps are merged over the new default list, user entries win), renamed keys moved to their v2 place,
    every other key kept unchanged (never drop what a user or the lead added to v1)."""
    out: dict = {}
    for key, value in saved.items():
        if key in V1_RENAMED:
            node = out
            parts = V1_RENAMED[key].split(".")
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            node[parts[-1]] = copy.deepcopy(value)
        else:
            out[key] = copy.deepcopy(value)
    out.pop("version", None)
    return out


def _deep_merge(base: dict, over: dict, replace: tuple[str, ...] = ()) -> dict:
    for k, v in over.items():
        if k not in replace and isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = copy.deepcopy(v)
    return base


class Config:
    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else paths.config_file()
        self._lock = threading.RLock()
        self._data: dict = copy.deepcopy(DEFAULTS)
        self._callbacks: list[tuple[str, Callable[[str, Any], None]]] = []
        self.migrated_from: int | None = None     # 1 when load() read a v1 file (not yet written back)

    # ------------------------------------------------------------------ load / save
    def load(self) -> "Config":
        with self._lock:
            data = copy.deepcopy(DEFAULTS)
            self.migrated_from = None
            saved = None
            if self.path.exists():
                try:
                    saved = json.loads(self.path.read_text(encoding="utf8"))
                    if not isinstance(saved, dict):
                        raise ValueError("config root is not an object")
                except Exception as e:
                    log.warning("corrupt config %s (%s); using defaults", self.path, e)
                    try:
                        os.replace(self.path, self.path.with_name("config.corrupt.json"))
                    except OSError as e2:
                        log.warning("could not rename corrupt config: %s", e2)
                    saved = None
            if saved is not None:
                if "version" not in saved:          # v1 file (§6.1 migration)
                    _deep_merge(data, migrate_v1(saved))   # v1 apps merged over the new defaults
                    self.migrated_from = 1
                else:
                    _deep_merge(data, saved, replace=REPLACED_MAPS)
                data["version"] = 2
            self._data = data
        return self

    def migrate_file(self) -> bool:
        """Write a v1 file back in the v2 schema (atomic, G7) after keeping the original next to it as
        config.v1.json (never overwritten). No-op (False) unless load() read a v1 file."""
        with self._lock:
            if self.migrated_from != 1:
                return False
            backup = self.path.with_name(V1_BACKUP_NAME)
            if self.path.exists() and not backup.exists():
                tmp = backup.with_name(backup.name + ".tmp")
                tmp.write_bytes(self.path.read_bytes())
                os.replace(tmp, backup)
            self.save()
            self.migrated_from = None
            log.info("config migrated from v1 to v2 (original kept as %s)", backup.name)
            return True

    def save(self) -> None:
        with self._lock:
            text = json.dumps(self._data, indent=2, ensure_ascii=False)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(text, encoding="utf8")
            os.replace(tmp, self.path)

    # ------------------------------------------------------------------ access
    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            node: Any = self._data
            for part in key.split("."):
                if isinstance(node, dict) and part in node:
                    node = node[part]
                else:
                    return default
            return copy.deepcopy(node) if isinstance(node, (dict, list)) else node

    def set(self, key: str, value: Any, *, save: bool = True) -> None:
        """Set + atomic save + on_change callbacks (synchronously, on the caller's thread)."""
        with self._lock:
            parts = key.split(".")
            node = self._data
            for part in parts[:-1]:
                if not isinstance(node.get(part), dict):
                    node[part] = {}
                node = node[part]
            node[parts[-1]] = copy.deepcopy(value)
            if save:
                try:
                    self.save()
                except OSError as e:
                    log.error("config save failed: %s", e)
            callbacks = list(self._callbacks)
        for prefix, fn in callbacks:
            if (not prefix or key == prefix or key.startswith(prefix + ".")
                    or prefix.startswith(key + ".")):
                try:
                    fn(key, value)
                except Exception:
                    log.exception("config on_change callback failed for %s", key)

    def on_change(self, prefix: str, fn: Callable[[str, Any], None]) -> None:
        with self._lock:
            self._callbacks.append((prefix, fn))

    def all(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._data)
