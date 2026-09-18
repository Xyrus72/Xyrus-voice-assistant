"""'Remember that ...' storage for Xyrus v2 (SPEC §4.10, §6.3). Owns data/memory.json:
[{"id": "m_3f9a2c1e", "text": "the wifi password is banana seven", "created": "2026-09-13T14:00:00", "source": "voice"}]

Same rules as CalendarStore: thread-safe (RLock), every change bumps .version and is written atomically
(tmp + os.replace, G7); a corrupt file is kept as memory.corrupt.json and the store starts empty.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import secrets
import threading
from pathlib import Path


def data_file() -> Path:
    """paths.DATA_DIR/"memory.json". Falls back to the §4.4 formula while paths.py isn't importable."""
    try:
        from xyrus import paths
        return Path(paths.data_dir() if hasattr(paths, "data_dir") else paths.DATA_DIR) / "memory.json"
    except ImportError:
        base = Path(__file__).resolve().parent.parent
        return Path(os.environ.get("XYRUS_DATA_DIR", base / "data")) / "memory.json"


class MemoryStore:
    def __init__(self, path=None):
        self.path = Path(path) if path else data_file()
        self.lock = threading.RLock()
        self.version = 0
        self.items: list[dict] = []
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text("utf8"))
                if not isinstance(loaded, list):
                    raise ValueError("memory.json must be a list")
                self.items = [m for m in loaded if isinstance(m, dict) and m.get("text")]
            except Exception:
                self.path.replace(self.path.with_suffix(".corrupt.json"))   # keep the evidence, start clean
                self.items = []

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.items, indent=2, ensure_ascii=False), "utf8")
        os.replace(tmp, self.path)
        self.version += 1

    def add(self, text: str, source: str = "voice", now: dt.datetime | None = None) -> dict:
        m = {"id": "m_" + secrets.token_hex(4), "text": text.strip(),
             "created": (now or dt.datetime.now()).isoformat(timespec="seconds"), "source": source}
        with self.lock:
            self.items.append(m)
            self._save()
        return m

    def newest(self, offset: int = 0, n: int = 3) -> list[dict]:
        """Newest first: newest(0) = the three most recent, newest(3) = the next three."""
        with self.lock:
            ordered = list(reversed(self.items))
        return ordered[offset:offset + n]

    def all(self) -> list[dict]:
        with self.lock:
            return list(self.items)

    def count(self) -> int:
        with self.lock:
            return len(self.items)

    def delete(self, mem_id: str) -> bool:
        with self.lock:
            before = len(self.items)
            self.items = [m for m in self.items if m["id"] != mem_id]
            if len(self.items) == before:
                return False
            self._save()
            return True

    def delete_last(self) -> dict | None:
        with self.lock:
            if not self.items:
                return None
            m = self.items.pop()
            self._save()
            return m

    def clear(self) -> int:
        with self.lock:
            n = len(self.items)
            if n:
                self.items = []
                self._save()
            return n
