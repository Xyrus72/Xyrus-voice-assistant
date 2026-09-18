"""Recording fakes for every side effect (§4.16) + make_test_engine(). Owned by T1, used by everyone.

Never prints; never touches real data (make_test_engine points XYRUS_DATA_DIR at a temp dir).
"""
from __future__ import annotations

import copy
import datetime as dt
import os
import queue
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

# ----------------------------------------------------------------------------- FakeActions
_ACTION_DEFAULTS: dict[str, Any] = {
    # power
    "shutdown": None, "restart": None, "abort_shutdown": None, "sleep": None, "hibernate": True, "lock": None,
    "sign_out": None, "screen_off": None, "screen_on": None, "keep_awake": None,
    # sound + media (volume/mute are stateful, see below)
    "volume_get": None, "volume_set": None, "volume_step": None, "mute_get": None, "mute_set": None,
    "media": None, "app_volume": True,
    # display
    "brightness_get": None, "brightness_set": None, "set_theme": None,
    # windows
    "windowed_apps": [], "focus_app": True, "close_app": True, "kill_app": True, "window_cmd": None,
    "snap": None, "show_desktop": None, "desktop": None, "foreground_is_self": False,
    # apps / web
    "open_target": None, "find_app": None, "find_song": None, "find_songs": None,
    "weather": {"temp_c": 28, "desc": "clear", "city": "Dhaka"},
    # input / clipboard
    "keys": None, "type_text": None, "run_command": None, "clip_get": "", "clip_set": None,
    # songs
    "browser_playing": False, "ensure_playing": "autoplay",
    # info
    "system_status": {"cpu": 12, "mem": 48, "battery": None, "uptime_s": 11400},
    "disks": [{"drive": "C", "free_gb": 120, "total_gb": 480, "pct": 75}],
    "top_processes": ["chrome", "code", "discord"],
    "screenshot": Path("C:/Users/test/Pictures/Xyrus/Screenshot_2026-09-13_10-30-00.png"),
    "last_screenshot": None,
}
ACTION_NAMES = tuple(_ACTION_DEFAULTS)


class FakeActions:
    """Implements SystemActions; records calls. FakeActions(volume_get=40, find_song=("Alone by Alan Walker",
    "https://www.youtube.com/watch?v=x")). A return value that is an exception instance is raised; a callable
    is called with the method's arguments. `fail[name] = exc` also raises."""

    def __init__(self, **returns: Any):
        unknown = set(returns) - set(_ACTION_DEFAULTS)
        if unknown:
            raise TypeError(f"FakeActions has no method(s) {sorted(unknown)}")
        self.calls: list[tuple[str, tuple, dict]] = []
        self.returns: dict[str, Any] = dict(returns)
        self.fail: dict[str, BaseException] = {}
        self.state = {"volume": 50, "muted": False, "brightness": 50}

    # ---- helpers for tests
    def called(self, name: str) -> list[tuple]:
        return [args for n, args, _ in self.calls if n == name]

    def names(self) -> list[str]:
        return [n for n, _, _ in self.calls]

    def reset(self) -> None:
        self.calls.clear()

    # ---- dispatch
    def _call(self, name: str, args: tuple, kw: dict) -> Any:
        self.calls.append((name, args, kw))
        if name in self.fail:
            raise self.fail[name]
        if name in self.returns:
            r = self.returns[name]
            if isinstance(r, BaseException):
                raise r
            return r(*args, **kw) if callable(r) else copy.deepcopy(r)
        stateful = getattr(self, "_default_" + name, None)
        if stateful is not None:
            return stateful(*args, **kw)
        return copy.deepcopy(_ACTION_DEFAULTS[name])

    def _default_volume_get(self):
        return self.state["volume"]

    def _default_volume_set(self, pct):
        self.state["volume"] = max(0, min(100, int(pct)))

    def _default_volume_step(self, delta_pct):
        self.state["volume"] = max(0, min(100, self.state["volume"] + int(delta_pct)))
        return self.state["volume"]

    def _default_mute_get(self):
        return self.state["muted"]

    def _default_mute_set(self, muted):
        self.state["muted"] = bool(muted)

    def _default_brightness_get(self):
        return self.state["brightness"]

    def _default_brightness_set(self, value):
        if isinstance(value, str):
            self.state["brightness"] = max(0, min(100, self.state["brightness"] + int(value)))
        else:
            self.state["brightness"] = max(0, min(100, int(value)))
        return self.state["brightness"]

    def _default_find_song(self, query):
        return (query, "https://www.youtube.com/watch?v=xxxxxxxxxxx")

    def _default_find_songs(self, query, limit=6):
        """Three ranked results; the first is the default find_song URL (so 'not that one' moves on)."""
        base = "https://www.youtube.com/watch?v="
        return [(query, base + "xxxxxxxxxxx", query), (f"{query} live", base + "yyyyyyyyyyy", f"{query} (Live)"),
                (f"{query} lyrics", base + "zzzzzzzzzzz", f"{query} (Lyrics)")][:limit]


def _make_action(name: str) -> Callable:
    def method(self, *args, **kw):
        return self._call(name, args, kw)
    method.__name__ = name
    return method


for _n in ACTION_NAMES:
    setattr(FakeActions, _n, _make_action(_n))


# ----------------------------------------------------------------------------- FakeSpeaker
class FakeSpeaker:
    """Implements Speaker. said: every text passed to say() (in order). auto_done=True calls on_done
    synchronously inside say(); auto_done=False keeps them pending until finish()."""

    def __init__(self, auto_done: bool = True):
        self.auto_done = auto_done
        self.said: list[str] = []
        self.log: list[dict] = []                 # {"text", "priority", "force"}
        self.pending: list[tuple[str, Callable[[], None] | None]] = []
        self.stops = 0
        self.voice: str | None = None
        self.rate = 0

    def say(self, text: str, *, priority: int = 1, force: bool = False,
            on_done: Callable[[], None] | None = None) -> None:
        self.said.append(text)
        self.log.append({"text": text, "priority": priority, "force": force})
        if self.auto_done:
            if on_done:
                on_done()
        else:
            self.pending.append((text, on_done))

    def forced(self) -> list[str]:
        return [e["text"] for e in self.log if e["force"]]

    def finish(self, n: int | None = None) -> int:
        """Complete the oldest n pending utterances (all when n is None). Returns how many finished."""
        count = 0
        while self.pending and (n is None or count < n):
            _, cb = self.pending.pop(0)
            count += 1
            if cb:
                cb()
        return count

    def stop(self) -> None:
        self.stops += 1
        self.finish()

    def is_speaking(self) -> bool:
        return bool(self.pending)

    def is_quiet_at(self, t_mono: float) -> bool:
        return not self.pending

    def voices(self) -> list[str]:
        return ["Microsoft David Desktop", "Microsoft Zira Desktop"]

    def set_voice(self, name: str | None) -> None:
        self.voice = name

    def set_rate(self, rate: int) -> None:
        self.rate = rate


class FakeChime:
    def __init__(self):
        self.played: list[str] = []
        self.stops = 0

    def play(self, kind: str) -> None:
        self.played.append(kind)

    def stop(self) -> None:
        self.stops += 1


class FakeNotifier:
    def __init__(self):
        self.notes: list[tuple[str, str]] = []

    def notify(self, title: str, body: str) -> None:
        self.notes.append((title, body))


class FakeAppIndex:
    """Stand-in for actions.apps.StartMenuIndex: lookup(spoken) -> (name, path) | None."""

    def __init__(self, entries: dict[str, str] | None = None):
        self.entries = dict(entries or {})

    def names(self) -> list[str]:
        return sorted(self.entries)

    def lookup(self, spoken: str):
        import difflib
        hit = difflib.get_close_matches(spoken, list(self.entries), n=1, cutoff=0.75)
        return (hit[0], self.entries[hit[0]]) if hit else None


# ----------------------------------------------------------------------------- stub stores
class StubStore:
    """Minimal in-memory Store for engine/dialogue tests when xyrus.store (T2) is unavailable or unwanted."""

    def __init__(self):
        import threading
        self.lock = threading.RLock()
        self.version = 0
        self.items: list[dict] = []
        self.notes: list[dict] = []
        self.state: dict = {}
        self._undo: list[tuple[str, str]] = []
        self._n = 0

    @property
    def data(self) -> dict:
        """v1-shaped view (reminders.due_alerts walks store.data["items"])."""
        return {"version": 1, "items": self.items, "notes": self.notes, "state": self.state}

    def occurrences(self, it, lo, hi):
        d = dt.date.fromisoformat(it["date"])
        return [(d, d.isoformat())] if lo <= d <= hi else []

    def mark_fired(self, item_id, key, now, how="fired"):
        it = self.get_item(item_id)
        if it is not None:
            it.setdefault("fired", {})[key] = f"{how}@{now.isoformat(timespec='seconds')}"

    def acknowledge(self, item_id, key):
        self.state.setdefault("acked", {}).setdefault(item_id, []).append(key)

    def is_acked(self, item_id, key):
        return key in self.state.get("acked", {}).get(item_id, [])

    def _id(self, prefix):
        self._n += 1
        return f"{prefix}_{self._n:08x}"

    @staticmethod
    def _iso(d):
        return d.isoformat() if hasattr(d, "isoformat") else str(d)

    @staticmethod
    def _hhmm(t):
        if t is None:
            return None
        return t.strftime("%H:%M") if hasattr(t, "strftime") else str(t)

    def _add(self, kind, title, date, time, reminder_min, repeat, source):
        it = {"id": self._id("it"), "kind": kind, "title": title, "date": self._iso(date),
              "time": self._hhmm(time), "all_day": time is None, "reminder_min": reminder_min, "repeat": repeat,
              "done": False, "source": source, "created": "", "fired": {}}
        self.items.append(it)
        if source == "voice":
            self._undo.append(("item", it["id"]))
        self.version += 1
        return it

    def add_event(self, title, date, time=None, reminder_min=None, repeat=None, source="ui", now=None):
        return self._add("event", title, date, time, reminder_min, repeat, source)

    def add_task(self, title, date, time=None, reminder_min=None, source="ui", now=None):
        return self._add("task", title, date, time, reminder_min, None, source)

    def get_item(self, item_id):
        return next((i for i in self.items if i["id"] == item_id), None)

    def update_item(self, item_id, **fields):
        it = self.get_item(item_id)
        if it:
            it.update(fields)
            self.version += 1
        return it

    def set_done(self, item_id, done=True):
        return self.update_item(item_id, done=done)

    def delete_item(self, item_id):
        before = len(self.items)
        self.items = [i for i in self.items if i["id"] != item_id]
        self.version += 1
        return len(self.items) != before

    def items_between(self, start, end, include_done=True):
        out = []
        for it in self.items:
            d = dt.date.fromisoformat(it["date"])
            if start <= d <= end and (include_done or not it["done"]):
                out.append((d, it))
        return sorted(out, key=lambda p: (p[0], p[1]["time"] or ""))

    def open_tasks(self, until=None):
        return [i for i in self.items if i["kind"] == "task" and not i["done"]]

    def overdue_tasks(self, today):
        return [i for i in self.open_tasks() if dt.date.fromisoformat(i["date"]) < today]

    def counts_by_day(self, start, end):
        return {}

    def next_item(self, now, days=60):
        items = self.items_between(now.date(), now.date() + dt.timedelta(days=days), include_done=False)
        return items[0] if items else None

    def find(self, text, kinds=("event", "task"), only_open=False, cutoff=0.55):
        import difflib
        pool = [i for i in self.items if i["kind"] in kinds and not (only_open and i["done"])]
        titles = [i["title"].lower() for i in pool]
        hit = difflib.get_close_matches(text.lower(), titles, n=1, cutoff=cutoff)
        return pool[titles.index(hit[0])] if hit else None

    def add_note(self, date, text, source="ui", now=None):
        n = {"id": self._id("nt"), "date": self._iso(date), "text": text, "source": source, "created": ""}
        self.notes.append(n)
        if source == "voice":
            self._undo.append(("note", n["id"]))
        self.version += 1
        return n

    def update_note(self, note_id, text):
        n = next((x for x in self.notes if x["id"] == note_id), None)
        if n:
            n["text"] = text
            self.version += 1
        return n

    def delete_note(self, note_id):
        before = len(self.notes)
        self.notes = [n for n in self.notes if n["id"] != note_id]
        self.version += 1
        return len(self.notes) != before

    def notes_on(self, date):
        return [n for n in self.notes if n["date"] == self._iso(date)]

    def notes_between(self, start, end):
        return [n for n in self.notes if start.isoformat() <= n["date"] <= end.isoformat()]

    def clear_notes(self, date):
        mine = self.notes_on(date)
        self.notes = [n for n in self.notes if n not in mine]
        self.version += 1
        return len(mine)

    def undo_last(self):
        while self._undo:
            kind, oid = self._undo.pop()
            if kind == "item":
                it = self.get_item(oid)
                if it:
                    self.delete_item(oid)
                    return it["title"]
            else:
                n = next((x for x in self.notes if x["id"] == oid), None)
                if n:
                    self.delete_note(oid)
                    return n["text"]
        return None

    def get_state(self, key, default=None):
        return self.state.get(key, default)

    def set_state(self, key, value):
        self.state[key] = value


class StubMemory:
    """Minimal MemoryStore stand-in (T2 owns xyrus.memory)."""

    def __init__(self):
        self.items: list[dict] = []
        self.version = 0

    def add(self, text, source="voice"):
        m = {"id": f"m_{len(self.items) + 1:08x}", "text": text, "created": "", "source": source}
        self.items.append(m)
        self.version += 1
        return m

    def newest(self, offset=0, n=3):
        return list(reversed(self.items))[offset:offset + n]

    def count(self):
        return len(self.items)

    def delete(self, mem_id):
        before = len(self.items)
        self.items = [m for m in self.items if m["id"] != mem_id]
        self.version += 1
        return len(self.items) != before

    def delete_last(self):
        if not self.items:
            return None
        self.version += 1
        return self.items.pop()

    def clear(self):
        n = len(self.items)
        self.items = []
        self.version += 1
        return n


# ----------------------------------------------------------------------------- make_test_engine
def make_test_engine(*, now: dt.datetime = dt.datetime(2026, 9, 13, 10, 30), tmpdir: Path | None = None,
                     config_overrides: dict | None = None, actions: Any = None, registry: Any = None,
                     store: Any = None, memory: Any = None, auto_done: bool = True,
                     app_index: Any = None):
    """Engine(threaded=False) with FakeClock, FakeSpeaker, FakeChime, FakeNotifier, FakeActions, InlineExecutor,
    temp CalendarStore/MemoryStore/Config (XYRUS_DATA_DIR=tmpdir), REGISTRY with commands.load_all.

    Additive keywords: registry= (use this registry instead of REGISTRY + load_all), store=/memory= (e.g.
    StubStore()), auto_done=False (drive speech completion with ns.speaker.finish()), app_index=.
    Returns (engine, ns) where ns has clock, speaker, chime, actions, store, memory, notifier, ui_queue,
    config, registry, executor, tmpdir."""
    from xyrus.clock import FakeClock
    from xyrus.config import Config
    from xyrus.engine import Engine
    from xyrus.executor import InlineExecutor

    tmp = Path(tmpdir) if tmpdir else Path(tempfile.mkdtemp(prefix="xyrus_test_"))
    tmp.mkdir(parents=True, exist_ok=True)
    os.environ["XYRUS_DATA_DIR"] = str(tmp)

    cfg = Config(tmp / "config.json").load()
    cfg.set("command_window_s", 15)     # tests are written against a 15 s window; the product default is 60 s
    for key, value in (config_overrides or {}).items():
        cfg.set(key, value)

    if registry is None:
        from xyrus.registry import REGISTRY
        registry = REGISTRY
        try:
            from xyrus import commands
        except ImportError:
            commands = None
        if commands is not None and hasattr(commands, "load_all"):
            commands.load_all(registry)

    if store is None:
        try:
            from xyrus.store import CalendarStore
            store = CalendarStore(tmp / "calendar.json")
        except ImportError:
            store = StubStore()
    if memory is None:
        try:
            from xyrus.memory import MemoryStore
            memory = MemoryStore(tmp / "memory.json")
        except ImportError:
            memory = StubMemory()

    clock = FakeClock(now)
    speaker = FakeSpeaker(auto_done=auto_done)
    chime = FakeChime()
    notifier = FakeNotifier()
    acts = actions if actions is not None else FakeActions()
    ui_q: "queue.Queue[str]" = queue.Queue()
    executor = InlineExecutor()
    engine = Engine(config=cfg, registry=registry, clock=clock, speaker=speaker, chime=chime, actions=acts,
                    executor=executor, store=store, memory=memory, notifier=notifier, ui_queue=ui_q,
                    threaded=False, app_index=app_index)
    ns = SimpleNamespace(clock=clock, speaker=speaker, chime=chime, actions=acts, store=store, memory=memory,
                         notifier=notifier, ui_queue=ui_q, config=cfg, registry=registry, executor=executor,
                         tmpdir=tmp)
    return engine, ns


def drain_ui(q: "queue.Queue[str]") -> list[str]:
    out = []
    while True:
        try:
            out.append(q.get_nowait())
        except queue.Empty:
            return out
