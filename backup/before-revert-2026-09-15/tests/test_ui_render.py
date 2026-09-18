"""T5 UI render tests (§7.6): XyrusWindow against a FakeApp (exactly the §9.1 App surface), screenshots of
all six tabs (dark and light), Commands count/filter, snapshot polling (Home feed, "Paused · mic off",
banner), ui_queue messages, show event, close-to-tray, per-tab behaviour, Light/Dark switching, and the
lifted Calendar tab's v1 checks.

Run from D:\\arc:  venv\\Scripts\\python.exe -m unittest tests.test_ui_render -v
Screenshots: tests/_out/*.png (+ a copy in $XYRUS_SHOT_DIR when set).

Isolation (lead, round 2): nothing happens at import time. Each test class gets its own temp
XYRUS_DATA_DIR (restored afterwards) and patches winutil.force_foreground with mock.patch (stopped in
tearDownClass); every test builds its own window and closes it with XyrusWindow.close(), which cancels
all pending after() jobs first. Waits poll for a condition with a timeout instead of sleeping.
Safety: no key/mouse injection; freshly mapped windows hand the keyboard focus back to the user's window.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import datetime as dt
import os
import queue
import random
import re
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))
OUT = BASE / "tests" / "_out"
REAL_CAL = BASE / "data" / "calendar.json"
REAL_CFG = BASE / "config.json"
REAL_DATA = BASE / "data"

import customtkinter as ctk  # noqa: E402
from PIL import ImageGrab  # noqa: E402

from xyrus import winutil  # noqa: E402
from xyrus.clock import FakeClock  # noqa: E402
from xyrus.config import Config  # noqa: E402
from xyrus.interfaces import DialogueView, EngineSnapshot, Event, TimerView  # noqa: E402
from xyrus.store import CalendarStore  # noqa: E402
from xyrus.ui import calendar_tab as CT  # noqa: E402
from xyrus.ui import routines_tab as RT  # noqa: E402
from xyrus.ui import theme  # noqa: E402
from xyrus.ui.commands_tab import sayable  # noqa: E402
from xyrus.ui.home_tab import timer_title  # noqa: E402
from xyrus.ui.window import XyrusWindow  # noqa: E402

NOW = dt.datetime(2026, 9, 13, 10, 30)
u32 = ctypes.windll.user32


def shot_dir() -> Path | None:
    return Path(os.environ["XYRUS_SHOT_DIR"]) if os.environ.get("XYRUS_SHOT_DIR") else None


def _ours(hwnd) -> bool:
    pid = wt.DWORD()
    u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value == os.getpid()


class keep_user_focus:
    """A freshly mapped test window/dialog may be activated by Windows; hand the keyboard focus straight
    back to whatever the user had in front (no key injection involved)."""

    def __enter__(self):
        self.prev = u32.GetForegroundWindow()
        return self

    def __exit__(self, *exc):
        cur = u32.GetForegroundWindow()
        if self.prev and cur != self.prev and _ours(cur) and not _ours(self.prev):
            u32.SetForegroundWindow(self.prev)
        return False


def _mtime(p: Path):
    return p.stat().st_mtime if p.exists() else None


def _real_state():
    return (_mtime(REAL_CAL), _mtime(REAL_CFG), sorted(os.listdir(REAL_DATA)) if REAL_DATA.exists() else None)


# ------------------------------------------------------------------ fakes -----
class FakeCapture:
    def __init__(self):
        self.value = 0.18
        self.calls = []

    def level(self):
        return self.value

    def status(self):
        return "listening"

    def start(self):
        self.calls.append("start")

    def stop(self):
        self.calls.append("stop")


class FakeRecognizer:
    def __init__(self):
        self.saved = []

    def save_last_seconds(self, path, seconds=30):
        Path(path).write_bytes(b"RIFF")
        self.saved.append((Path(path), seconds))


class FakeVocab:
    UNKNOWN = {"kubernetes", "xylophonic"}

    def unknown_words(self, phrase):
        return [w for w in phrase.lower().split() if w in self.UNKNOWN]


class FakeHotkeys:
    def status(self):
        return {"show_window": True, "push_to_talk": False, "stop_speaking": True}


class FakeTray:
    def __init__(self):
        self.states, self.tips = [], []

    def set_state(self, s):
        self.states.append(s)

    def set_tooltip(self, t):
        self.tips.append(t)


class FakeShowEvent:
    def __init__(self):
        self.pending = 0

    def poll(self):
        if self.pending:
            self.pending -= 1
            return True
        return False


class RecordingSpeaker:
    def __init__(self):
        self.said, self.stopped, self.voice, self.rate = [], 0, None, 0

    def say(self, text, *, priority=1, force=False, on_done=None):
        self.said.append((text, force))
        if on_done:
            on_done()

    def stop(self):
        self.stopped += 1

    def is_speaking(self):
        return False

    def is_quiet_at(self, t):
        return True

    def voices(self):
        return ["Microsoft David Desktop", "Microsoft Zira Desktop"]

    def set_voice(self, name):
        self.voice = name

    def set_rate(self, rate):
        self.rate = rate


class FakeNotifier:
    def __init__(self):
        self.notes = []

    def notify(self, title, body):
        self.notes.append((title, body))


class StubTimers:
    def __init__(self, engine):
        self.engine = engine
        self.cancelled = []

    def cancel(self, name=None, all=False):
        self.cancelled.append(name)
        self.engine.timer_views = tuple(t for t in self.engine.timer_views if t.name != name)
        self.engine.touch()
        return 1

    def stop_ringing(self):
        return 0


class StubEngine:
    """Publishes real EngineSnapshot objects (a new one only when something changed, like the engine)."""

    def __init__(self, store=None):
        self.store = store
        self.events: list[Event] = []
        self.paused = False
        self.mode = "idle"
        self.status = "listening"
        self.speaking = False
        self.window_left_s = 0
        self.dialogue: DialogueView | None = None
        self.timer_views: tuple[TimerView, ...] = ()
        self.next_event: str | None = "Dentist appointment at 3 pm · in 4 h"
        self.submitted: list[tuple[str, str]] = []
        self.cancelled = 0
        self.timers = StubTimers(self)
        self._snap = None
        self._n = 0

    def touch(self):
        self._snap = None

    def event(self, kind, text, meta=""):
        self._n += 1
        self.events.append(Event(f"10:{30 + self._n // 60:02d}:{self._n % 60:02d}", kind, text, meta))
        del self.events[:-300]
        self.touch()

    def snapshot(self):
        if self._snap is None:
            self._snap = EngineSnapshot(
                mode=self.mode, listen="paused" if self.paused else "wake", paused=self.paused, status=self.status,
                window_left_s=self.window_left_s, speaking=self.speaking, dialogue=self.dialogue,
                timers=self.timer_views, next_event=self.next_event, last_reply="", events=tuple(self.events),
                missed=0, store_version=getattr(self.store, "version", 0))
        return self._snap

    def submit_text(self, text, source="typed"):
        self.submitted.append((text, source))
        self.event("heard", text)

    def cancel_dialogue(self):
        self.cancelled += 1
        self.dialogue = None
        self.touch()

    def set_paused(self, p):
        self.paused = bool(p)
        self.status = "paused" if p else "listening"
        self.touch()

    def post(self, fn):
        fn()


def _real_engine_available() -> bool:
    if os.environ.get("XYRUS_UI_STUB"):
        return False
    try:
        import xyrus.engine  # noqa: F401
        from xyrus.testing import make_test_engine  # noqa: F401
        return True
    except Exception:
        return False


def _registry():
    from xyrus.registry import REGISTRY
    try:
        from xyrus import commands
        commands.load_all(REGISTRY)
    except Exception as e:                                # a teammate's module still landing
        print(f"  (commands.load_all failed: {e!r})")
    return REGISTRY


def make_app(tmp: Path, *, store="new", clock=None, real_engine=None, appearance=None):
    """FakeApp with exactly the §9.1 attributes, all state under `tmp`."""
    sub = Path(tempfile.mkdtemp(dir=tmp))
    real = _real_engine_available() if real_engine is None else real_engine
    ns = None
    if real:
        from xyrus.testing import make_test_engine
        engine, ns = make_test_engine(now=NOW, tmpdir=sub)
        config, registry, clock = ns.config, ns.registry, ns.clock
        st = ns.store if store == "new" else store
    else:
        config = Config(sub / "config.json").load()
        registry = _registry()
        clock = clock or FakeClock(NOW)
        st = CalendarStore(sub / "calendar.json") if store == "new" else store
        engine = StubEngine(st)
    if appearance:
        config.set("ui.appearance", appearance)
    ui_q = ns.ui_queue if ns is not None else queue.Queue()
    app = SimpleNamespace(
        config=config, clock=clock, registry=registry, engine=engine, store=st,
        memory=getattr(ns, "memory", None), speaker=RecordingSpeaker(), chime=SimpleNamespace(play=lambda k: None),
        capture=FakeCapture(), recognizer=FakeRecognizer(), vocab=FakeVocab(), hotkeys=FakeHotkeys(),
        tray=FakeTray(), notifier=FakeNotifier(), show_event=FakeShowEvent(), ui_queue=ui_q)
    app.paused_calls = []
    app.quit_calls = 0

    def set_paused(p):
        app.paused_calls.append(p)
        engine.set_paused(p)

    def quit_():
        app.quit_calls += 1
        ui_q.put("quit")

    app.set_paused = set_paused
    app.quit = quit_
    app.real_engine = real
    return app


# ------------------------------------------------------------- window utils ----
def pump(win, sec=0.3):
    """Let Tk render for a moment (screenshots only - assertions use wait_for)."""
    end = time.monotonic() + sec
    while time.monotonic() < end:
        win.update()
        time.sleep(0.015)


def wait_for(win, cond, timeout=5.0) -> bool:
    """Pump Tk until cond() is true (or the timeout passes)."""
    end = time.monotonic() + timeout
    while True:
        win.update()
        try:
            if cond():
                return True
        except Exception:
            pass
        if time.monotonic() > end:
            return False
        time.sleep(0.015)


def hwnd_of(win) -> int:
    return int(win.frame(), 16)


def _save(img, name: str) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / name
    img.save(path)
    sd = shot_dir()
    if sd:
        sd.mkdir(parents=True, exist_ok=True)
        img.save(sd / name)
    return path


def shoot(win, name: str) -> Path:
    """Grab the window's own rect (never FindWindowW(None, "Xyrus"): the live v1 app has that title)."""
    h = hwnd_of(win)
    r = wt.RECT()
    for _ in range(3):
        if u32.IsIconic(h) or win.state() != "normal":
            with keep_user_focus():
                win.deiconify()
                wait_for(win, lambda: win.state() == "normal", 2)
        win.lift()
        win.attributes("-topmost", True)
        pump(win, 0.5)
        u32.GetWindowRect(h, ctypes.byref(r))
        if r.right - r.left > 400:
            break
    return _save(ImageGrab.grab(bbox=(r.left, r.top, r.right, r.bottom)), name)


def shoot_toplevel(win, top, name: str) -> Path:
    top.attributes("-topmost", True)                  # the test main window is topmost too
    top.lift()
    pump(win, 0.4)
    r = wt.RECT()
    u32.GetWindowRect(int(top.frame(), 16), ctypes.byref(r))
    return _save(ImageGrab.grab(bbox=(r.left, r.top, r.right, r.bottom)), name)


def not_uniform(path: Path) -> bool:
    from PIL import Image
    with Image.open(path) as im:
        lo, hi = im.convert("L").getextrema()
        colors = im.convert("RGB").getcolors(maxcolors=1 << 20)
    return hi - lo > 60 and (colors is None or len(colors) > 50)


def new_window(app, title="XyrusUITest", geometry="1000x700"):
    with keep_user_focus():
        win = XyrusWindow(app, start_hidden=False)
        win.title(title)
        win.geometry(geometry)
        win.attributes("-topmost", True)
        wait_for(win, lambda: win.state() == "normal" and win.winfo_width() > 400, 5)
        pump(win, 0.2)
    return win


def close_window(win):
    try:
        win.close()
    except Exception:
        pass


def select_tab(win, name, timeout=3.0) -> bool:
    win._select_tab(name)
    return wait_for(win, lambda: win.tab_ready(name), timeout)


def seed_calendar(st: CalendarStore):
    D = lambda m, d: dt.date(2026, m, d)
    st.add_event("Team standup", D(9, 13), "09:30", 10)
    st.add_event("Dentist appointment", D(9, 13), "15:00", 30)
    st.add_event("Gym", D(9, 7), "18:00", 0, repeat="weekly")
    st.add_task("Buy groceries", D(9, 13))
    st.add_task("Submit lab report", D(9, 11))
    st.add_note(D(9, 13), "Parking is on level 3, spot B12")
    st.add_note(D(9, 13), "Ask Sara about the conference tickets")


def seed_busy_day(st: CalendarStore, day=dt.date(2026, 9, 17)):
    for i, (t, h) in enumerate([("Review PR #42", "09:00"), ("Lunch with Imran", "13:00"), ("Pick up parcel", None),
                                ("Client call", "14:30"), ("Write weekly report", None), ("Guitar lesson", "17:00"),
                                ("Reply to emails", None), ("Laundry", None), ("Movie night", "21:00")]):
        (st.add_event if h and i % 2 == 0 else st.add_task)(t, day, h, 0 if h else None)
    st.add_note(day, "Bring the charger")


def seed_engine(app, tmp: Path):
    """3 events + 2 notes (store), 1 timer, a pending dialogue, some activity (§7.6)."""
    seed_calendar(app.store)
    eng = app.engine
    if app.real_engine:
        eng.handle("set a timer for five minutes", "typed")
        eng.handle("what time is it", "typed")
        eng.handle("add an event", "typed")
        return
    for kind, text, meta in [("heard", "cyrus what time is it", ""), ("cmd", "time", ""),
                             ("say", "It's 10:30, sir.", ""), ("noise", "how", ""),
                             ("heard", "cyrus set a timer for five minutes", ""), ("cmd", "set_timer 300", ""),
                             ("say", "Timer set for five minutes, sir.", ""),
                             ("info", "Screenshot saved", str(tmp / "shot.png")),
                             ("alert", "Sir, dentist appointment is at 3 pm, in 30 minutes.", ""),
                             ("remind", "Reminder: call the bank at 4 pm.", ""),
                             ("ask", "What's the event?", ""), ("error", "That didn't work, sir. It's in the log.", "")]:
        eng.event(kind, text, meta)
    eng.timer_views = (TimerView("5 minute", 299),)
    eng.dialogue = DialogueView(kind="event", state="when", question="When is the dentist?",
                                hint='Say a day and time, like "tomorrow at 3 pm", or "cancel".',
                                slots={"title": "Dentist"}, seconds_left=14, listening="when")
    eng.touch()


# ================================================================ base class ==
class UICase(unittest.TestCase):
    """Per class: a fresh XYRUS_DATA_DIR and a force_foreground recorder (T4's real one taps ALT).
    Per test: its own window via self.open(), closed in cleanup."""

    @classmethod
    def setUpClass(cls):
        cls.real_before = _real_state()
        cls.tmp = Path(tempfile.mkdtemp(prefix="xyrus_ui_"))
        cls._env_before = os.environ.get("XYRUS_DATA_DIR")
        os.environ["XYRUS_DATA_DIR"] = str(cls.tmp)
        cls.fg_calls = []
        calls = cls.fg_calls
        cls._fg_patch = mock.patch.object(winutil, "force_foreground",
                                          new=lambda hwnd: (calls.append(int(hwnd)), True)[1])
        cls._fg_patch.start()

    @classmethod
    def tearDownClass(cls):
        cls._fg_patch.stop()
        if cls._env_before is None:
            os.environ.pop("XYRUS_DATA_DIR", None)
        else:
            os.environ["XYRUS_DATA_DIR"] = cls._env_before
        ctk.set_appearance_mode("dark")
        shutil.rmtree(cls.tmp, ignore_errors=True)
        assert _real_state() == cls.real_before, "real data/ or config.json was touched"

    def open(self, app, title="XyrusUITest", geometry="1000x700"):
        win = new_window(app, title, geometry)
        self.addCleanup(close_window, win)
        return win


# ======================================================================= tests ==
class WindowRenderTests(UICase):
    """§7.6: a fresh 1000x700 window per test, make_test_engine seeded with 3 events, 2 notes, 1 timer and a
    pending dialogue."""

    def setUp(self):
        self.app = make_app(self.tmp)
        seed_engine(self.app, self.tmp)
        t0 = time.perf_counter()
        self.win = self.open(self.app)
        self.build_s = time.perf_counter() - t0

    def test_01_six_tab_screenshots(self):
        win = self.win
        for name in ("Home", "Calendar", "Commands", "Apps", "Routines", "Settings"):
            self.assertTrue(select_tab(win, name), f"{name} tab did not come up")
            path = shoot(win, f"tab_{name}.png")
            self.assertTrue(win.tab_ready(name), f"{name} tab lost while shooting")
            self.assertTrue(not_uniform(path), f"{name} screenshot is blank")
        for attr in ("home_tab", "calendar_tab", "commands_tab", "apps_tab", "routines_tab", "settings_tab"):
            self.assertIsNotNone(getattr(win, attr), f"{attr} failed to build")
        self.assertLess(self.build_s, 8.0, "window build too slow")

    def test_02_commands_count_and_filter(self):
        win, tab = self.win, self.win.commands_tab
        self.assertTrue(select_tab(win, "Commands"))
        m = re.fullmatch(r"(\d+) commands · (\d+) ways to say them", tab.count.cget("text"))
        self.assertIsNotNone(m, tab.count.cget("text"))
        n, k = int(m.group(1)), int(m.group(2))
        self.assertEqual((n, k), self.app.registry.counts(), "label must match the registry exactly")
        sections = [s for s, _ in self.app.registry.sections(self.app.config)]
        self.assertEqual(list(tab.headers), sections, "sections come from registry.sections only")
        tab.filter.insert(0, "timer")
        tab.apply_filter()
        rows = tab.visible_rows()
        self.assertTrue(rows, "filter 'timer' shows nothing")
        for r in rows:
            self.assertIn("timer", r.haystack, f"non-timer row visible: {r.phrases}")
        shoot(win, "commands_filter_timer.png")
        mf = re.fullmatch(r"(\d+) commands · (\d+) ways to say them", tab.count.cget("text"))
        self.assertLess(int(mf.group(1)), n)
        try_rows = [r for r in rows if r.try_btn is not None]
        self.assertTrue(all(sayable(r.try_phrase) == r.try_phrase for r in try_rows))
        self.assertTrue(all("<" not in r.try_phrase for r in try_rows), "Try only for slot-less phrases")
        self.assertGreaterEqual(n, 60, "§7.6 wants >= 60 commands")

    def test_03_banner_and_header(self):
        win = self.win
        snap = self.app.engine.snapshot()
        self.assertIsNotNone(snap.dialogue, "seeded dialogue missing")
        self.assertTrue(wait_for(win, lambda: win.banner.winfo_manager() == "pack"), "banner hidden")
        self.assertEqual(win.banner_q.cget("text"), snap.dialogue.question)
        self.assertLess(win.banner.winfo_height(), 240, "banner must stay compact")
        self.assertTrue(win.side_lbl.cget("text").startswith("timer "), win.side_lbl.cget("text"))
        self.assertTrue(wait_for(win, lambda: self.app.tray.states and self.app.tray.states[-1] == "active"))
        self.assertTrue(select_tab(win, "Home"))
        shoot(win, "banner_home.png")

    def test_04_snapshot_poll_feed_and_paused(self):
        """§7.6: feed engine events -> Home feed row count; paused -> "Paused · mic off"."""
        win, eng, home = self.win, self.app.engine, self.win.home_tab
        if self.app.real_engine:
            eng.cancel_dialogue()
            for text in ("what time is it", "what's the date", "volume up", "flip a coin", "thank you"):
                eng.handle(text, "typed")
        else:
            for i in range(30):
                eng.event("heard" if i % 3 else "noise", f"utterance {i}")
        visible = lambda: [e for e in eng.snapshot().events if e.kind != "noise"]
        self.assertTrue(wait_for(win, lambda: home.row_count() == min(200, len(visible()))),
                        f"{home.row_count()} rows for {len(visible())} events")
        self.assertGreater(len(visible()), 5)
        self.assertEqual(home.rows[-1].event, visible()[-1], "newest event is the last feed row")
        self.assertTrue(wait_for(win, lambda: not win.banner.winfo_manager()), "banner gone once the dialogue ended")
        self.app.set_paused(True)
        self.assertTrue(wait_for(win, lambda: win.status_text() == "Paused · mic off"), win.status_text())
        self.assertTrue(eng.snapshot().paused)
        self.assertEqual(win.pause_btn.cget("text"), "Resume")
        self.assertTrue(wait_for(win, lambda: self.app.tray.states[-1] == "paused"))
        shoot(win, "paused_home.png")
        win.pause_btn.invoke()
        self.assertEqual(self.app.paused_calls[-1], False)
        self.assertTrue(wait_for(win, lambda: win.status_text() != "Paused · mic off"))

    def test_05_header_states(self):
        from xyrus.ui.window import side_text, status_text
        base = self.app.engine.snapshot()
        mk = lambda **kw: base.__class__(**{**base.__dict__, **kw})
        self.assertEqual(status_text(mk(paused=True, dialogue=None))[0], "Paused · mic off")
        self.assertEqual(status_text(mk(dialogue=None, status="mic error, retrying"))[0], "Mic error, retrying")
        self.assertEqual(status_text(mk(dialogue=None, mode="armed", window_left_s=12))[0],
                         "Listening for your command · 12 s")
        self.assertEqual(status_text(mk(dialogue=None, mode="follow_up", window_left_s=6))[0],
                         "Listening for a follow-up · 6 s")
        self.assertEqual(status_text(mk(dialogue=None, speaking=True))[0], "Speaking")
        conf = DialogueView("confirm", "confirm", "Shut down the PC?", "", {"action": "shutdown"}, 10, "confirm")
        self.assertEqual(status_text(mk(dialogue=conf))[0], "Confirm shutdown?")
        self.assertEqual(status_text(mk(dialogue=None, mode="idle", status="listening"))[0], "Listening")
        self.assertEqual(side_text(mk(timers=(TimerView("5 minute", 0, ringing=True),))),
                         "5 minutes timer · ringing")

    def test_06_ui_queue_messages(self):
        win, q = self.win, self.app.ui_queue
        cal = win.calendar_tab
        self.assertTrue(select_tab(win, "Home"))
        q.put("show:Calendar")
        self.assertTrue(wait_for(win, lambda: win.tab_ready("Calendar")))
        q.put("show:Calendar:2026-09-20")
        self.assertTrue(wait_for(win, lambda: cal.selected == dt.date(2026, 9, 20)))
        q.put("calendar:2026-09-14:2026-09-20")
        self.assertTrue(wait_for(win, lambda: cal.range == (dt.date(2026, 9, 14), dt.date(2026, 9, 20))))
        q.put("show:Commands")
        q.put("show:Settings")                            # two switches inside CTkTabview's 100 ms window
        self.assertTrue(wait_for(win, lambda: win.tab_ready("Settings")))
        pump(win, 0.3)                                    # past the 100 ms grid_forget of the first switch
        self.assertTrue(win.tab_ready("Settings"), "second quick switch blanked the tab")
        self.assertFalse(win.tabs.tab("Commands").winfo_manager())
        q.put("show:nonsense")
        q.put("bogus message")
        self.assertTrue(wait_for(win, lambda: q.empty()))  # ignored, polling continues
        self.assertEqual(self.app.config.get("window.last_tab"), "Settings")

    def test_07_show_event_and_close_to_tray(self):
        win = self.win
        win.tk.call(win.protocol("WM_DELETE_WINDOW"))     # what the title-bar close button runs
        self.assertTrue(wait_for(win, lambda: win.state() == "withdrawn"))
        self.assertEqual(self.app.notifier.notes, [("Xyrus", "Still running in the tray.")])
        self.assertTrue(self.app.config.get("window.geometry"))
        with keep_user_focus():
            self.app.show_event.pending = 1               # a second launch signalled us
            self.assertTrue(wait_for(win, lambda: win.state() == "normal"))
        self.assertTrue(u32.IsWindowVisible(hwnd_of(win)))
        self.assertEqual(self.fg_calls[-1], hwnd_of(win), "show() hands the frame HWND to force_foreground")
        win.hide()
        self.assertEqual(len(self.app.notifier.notes), 1, "tray toast is one-time")
        with keep_user_focus():
            self.app.ui_queue.put("show")
            self.assertTrue(wait_for(win, lambda: win.state() == "normal"))
        self.app.ui_queue.put("hide")                     # T4: "close window" while Xyrus is in front
        self.assertTrue(wait_for(win, lambda: win.state() == "withdrawn"))
        with keep_user_focus():
            self.app.ui_queue.put("show:Home")
            self.assertTrue(wait_for(win, lambda: win.state() == "normal" and win.tab_ready("Home")))
        self.app.ui_queue.put("quit")                     # the tray's Quit: the window closes itself
        wait_for(win, lambda: not win.winfo_exists(), 3) if False else None
        end = time.monotonic() + 3
        while time.monotonic() < end and not win._closing:
            try:
                win.update()
            except Exception:
                break
            time.sleep(0.015)
        self.assertTrue(win._closing, "quit message did not close the window")


class TabBehaviourTests(UICase):
    """Header/Home/Apps/Routines/Settings/EventDialog behaviour; a fresh stub-engine window per test."""

    def setUp(self):
        self.app = make_app(self.tmp, real_engine=False)
        seed_calendar(self.app.store)
        self.win = self.open(self.app, title="XyrusUITest2")

    def test_feed_pool_and_noise(self):
        win, eng, home = self.win, self.app.engine, self.win.home_tab
        for i in range(30):
            eng.event("heard" if i % 3 else "noise", f"utterance {i}")
        eng.event("info", "Screenshot saved", str(self.tmp / "shot.png"))
        visible = [e for e in eng.snapshot().events if e.kind != "noise"]
        self.assertTrue(wait_for(win, lambda: home.row_count() == len(visible)))
        self.assertIsNotNone(home.rows[-1].open_btn, "info rows with a path get an Open button")
        for i in range(260):                              # overflow: pool stays at 200, newest last
            eng.event("say", f"line {i}")
        self.assertTrue(wait_for(win, lambda: home.rows[-1].event.text == "line 259"))
        self.assertEqual(home.row_count(), 200)
        for i in range(5):
            eng.event("noise", f"hum {i}")
        wait_for(win, lambda: home._last_event.text == "hum 4")
        home.noise_sw.toggle()                            # show noise -> re-render incl. noise rows
        self.assertTrue(wait_for(win, lambda: home.rows[-1].event.text == "hum 4"))
        self.assertTrue(home.show_noise)
        self.assertIs(self.app.config.get("window.show_noise"), True)
        home.noise_sw.toggle()
        self.assertTrue(wait_for(win, lambda: home.rows[-1].event.text == "line 259"))

    def test_header_buttons_and_answer(self):
        win, eng = self.win, self.app.engine
        eng.speaking = True
        eng.touch()
        self.assertTrue(wait_for(win, lambda: win.stop_btn.cget("state") == "normal"))
        win.stop_btn.invoke()
        self.assertEqual(self.app.speaker.stopped, 1)
        eng.speaking = False
        eng.dialogue = DialogueView("event", "when", "When is it?", "", {"title": "Gym"}, 9, "when", parked=True)
        eng.touch()
        self.assertTrue(wait_for(win, lambda: win.banner.winfo_manager() == "pack"))
        self.assertIn("on hold", win.banner_caption.cget("text"))
        win.answer.insert(0, "tomorrow at 6 pm")
        win._answer()
        self.assertEqual(eng.submitted[-1], ("tomorrow at 6 pm", "typed"))
        win._cancel_dialogue()
        self.assertEqual(eng.cancelled, 1)
        self.assertTrue(wait_for(win, lambda: not win.banner.winfo_manager()), "banner stays after the dialogue")

    def test_home_input_and_timers(self):
        win, eng, home = self.win, self.app.engine, self.win.home_tab
        self.assertEqual([timer_title(n) for n in ("5 minute", "1 minute", "90 second", "2 hour", "5 minute 2",
                                                   "tea")],
                         ["5 minutes", "1 minute", "90 seconds", "2 hours", "5 minutes (2)", "tea"])
        self.assertTrue(select_tab(win, "Home"))
        eng.timer_views = (TimerView("tea", 125), TimerView("5 minute", 299), TimerView("1 minute 2", 0, True))
        eng.touch()
        shown = lambda: [r["label"].cget("text") for r in home._timer_rows if r["label"].winfo_manager()]
        self.assertTrue(wait_for(win, lambda: len(shown()) == 3), shown())
        self.assertEqual(shown(), ["tea · 02:05", "5 minutes · 04:59", "1 minute (2) · ringing"])
        home._timer_rows[0]["button"].invoke()
        self.assertEqual(eng.timers.cancelled, ["tea"])
        home.entry.insert(0, "what time is it")
        home.submit()
        home.entry.insert(0, "volume up")
        home.submit()
        self.assertEqual(eng.submitted[-2:], [("what time is it", "typed"), ("volume up", "typed")])
        home._history(-1)
        self.assertEqual(home.entry.get(), "volume up")
        home._history(-1)
        self.assertEqual(home.entry.get(), "what time is it")
        home._history(1)
        home._history(1)
        self.assertEqual(home.entry.get(), "")
        self.assertTrue(home.try_phrase.cget("text").startswith('"Xyrus, '), home.try_phrase.cget("text"))
        home._try_current()
        self.assertEqual(eng.submitted[-1], (home._try_text, "typed"))
        row = next(r for r in win.commands_tab.rows if r.try_btn is not None)
        row.try_btn.invoke()
        self.assertEqual(eng.submitted[-1], (row.try_phrase, "typed"))

    def test_apps_tab_validation_and_save(self):
        win, tab = self.win, self.win.apps_tab
        self.assertTrue(select_tab(win, "Apps"))
        n0 = len(tab.rows)
        self.assertEqual(n0, len(self.app.config.get("apps")))
        row = tab.add_row("kubernetes", "https://k8s.io")
        self.assertIn("kubernetes", tab.check_row(row))
        ok = tab.add_row("Movie  Night", "https://www.netflix.com")
        self.assertEqual(tab.check_row(ok), "")
        wake = tab.add_row("cyrus box", "calc")
        self.assertIn("wake word", tab.check_row(wake))
        win.update_idletasks()
        tab.list._parent_canvas.yview_moveto(1.0)
        shoot(win, "apps_validation.png")
        tab.remove_row(wake)
        tab.save()
        apps = self.app.config.get("apps")
        self.assertEqual(apps["movie night"], "https://www.netflix.com")
        self.assertEqual(apps["kubernetes"], "https://k8s.io")
        self.assertEqual(len(apps), n0 + 2)
        self.assertIn("Saved", tab.msg.cget("text"))

    def test_routines_validate_rules(self):
        reg, vocab = self.app.registry, FakeVocab()
        v = lambda phrase, steps=(("open", "code"),), **kw: RT.validate(phrase, list(steps), registry=reg,
                                                                         vocab=vocab, **kw)
        self.assertIsNone(v("movie mode", RT.TEMPLATES["movie mode"]))
        self.assertIsNone(v("gaming"))                       # one word of >= 5 letters
        self.assertIn("two words", v("stop"))
        self.assertIn("kubernetes", v("kubernetes mode"))
        self.assertIn("wake word", v("cyrus mode"))
        self.assertIn("Add at least one step", v("work mode", ()))
        self.assertIn("wait 1 to 30", v("work mode", (("wait", "45"),)))
        self.assertIn("number", v("work mode", (("wait", "soon"),)))
        self.assertIn("fill in", v("work mode", (("open", " "),)))
        self.assertIn("keys", v("work mode", (("keys", "ctrl+banana"),)))
        self.assertIn("already have", v("work mode", others=["Work Mode"]))
        builtin = next((sayable(p.text) for cmd in reg.commands() if not cmd.custom for p in cmd.patterns
                        if not p.slots and sayable(p.text) and len(sayable(p.text).split()) >= 2), None)
        if builtin:
            self.assertEqual(v(builtin), "That's already a built-in command.")
            self.assertEqual(v(f"please {builtin} now"), "That's already a built-in command.")
        cfg = RT.to_config("Movie Mode", True, [("wait", "5"), ("keys", "f11")])
        self.assertEqual(cfg, {"phrase": "movie mode", "enabled": True,
                               "steps": [{"action": "wait", "arg": 5}, {"action": "keys", "arg": "f11"}]})

    def test_routines_tab_flow(self):
        win, tab, cfg = self.win, self.win.routines_tab, self.app.config
        self.assertTrue(select_tab(win, "Routines"))
        tab.add_template("movie mode")
        self.assertEqual(len(tab.steps), 5)
        self.assertTrue(tab.save(), tab.msg.cget("text"))
        saved = cfg.get("custom_commands")
        self.assertEqual(saved[0]["phrase"], "movie mode")
        self.assertEqual(saved[0]["steps"][3], {"action": "wait", "arg": 5})
        tab.move_step(tab.steps[4], -1)                   # keys f11 before wait
        tab.add_step("say", "Enjoy the film.")
        self.assertTrue(tab.save())
        steps = cfg.get("custom_commands")[0]["steps"]
        self.assertEqual([s["action"] for s in steps], ["command", "command", "open", "keys", "wait", "say"])
        tab.new()
        tab.phrase.insert(0, "kubernetes mode")
        self.assertFalse(tab.save())
        self.assertIn("kubernetes", tab.msg.cget("text"))
        self.assertEqual(tab.check_phrase(), ["kubernetes"])
        shoot(win, "routines_error.png")
        tab.edit(0)
        tab.test()
        self.assertEqual(self.app.engine.submitted[-1], ("movie mode", "typed"))
        tab.delete()
        self.assertEqual(cfg.get("custom_commands"), [])

    def test_settings_live_apply(self):
        win, tab, cfg = self.win, self.win.settings_tab, self.app.config
        self.assertTrue(select_tab(win, "Settings"))
        self.assertTrue(wait_for(win, lambda: "Microsoft Zira Desktop" in tab.voice_menu.cget("values")),
                        "voices never arrived from the worker")
        sw = tab.w["voice_replies"]
        before = cfg.get("voice_replies")
        sw.toggle()
        self.assertEqual(cfg.get("voice_replies"), not before)
        sw.toggle()
        for w, cb in tab.wake_boxes.items():
            if cb.get():
                cb.toggle()
        self.assertEqual(cfg.get("wake_words"), ["cyrus"], "last wake word can't be removed")
        tab.wake_boxes["zeros"].toggle()
        self.assertEqual(cfg.get("wake_words"), ["cyrus", "zeros"])
        tab.w["command_window_s"].set(20)
        tab.w["command_window_s"]._command(20)
        self.assertTrue(wait_for(win, lambda: cfg.get("command_window_s") == 20), "debounced write never came")
        tab.w["tts.rate"]._command(3)
        self.assertTrue(wait_for(win, lambda: (cfg.get("tts.rate"), self.app.speaker.rate) == (3, 3)))
        tab._pick_voice("Microsoft Zira Desktop")
        self.assertEqual((cfg.get("tts.voice"), self.app.speaker.voice), ("Microsoft Zira Desktop",) * 2)
        tab.w["calendar.snooze_minutes"]._command("15")
        self.assertEqual(cfg.get("calendar.snooze_minutes"), 15)
        tab.quiet_from.delete(0, "end")
        tab.quiet_from.insert(0, "25:00")
        tab.quiet_sw.select()
        tab._quiet_changed()
        self.assertIsNone(cfg.get("quiet_hours"))
        self.assertIn("HH:MM", tab.quiet_sub.cget("text"))
        tab.quiet_from.delete(0, "end")
        tab.quiet_from.insert(0, "22:30")
        tab._quiet_changed()
        self.assertEqual(cfg.get("quiet_hours"), {"from": "22:30", "to": "07:00"})
        tab.test_voice()
        self.assertEqual(self.app.speaker.said[-1], ("Hello, sir. This is how Xyrus sounds.", True))
        tab.save_audio()
        self.assertTrue(wait_for(win, lambda: "Saved" in tab.audio_sub.cget("text")), tab.audio_sub.cget("text"))
        saved_path = self.app.recognizer.saved[0][0]
        self.assertTrue(str(saved_path).startswith(str(self.tmp)), f"audio saved outside the test dir: {saved_path}")
        tab.test_mic()
        self.assertTrue(wait_for(win, lambda: self.app.capture.calls[-2:] == ["stop", "start"]))
        texts = {k: st.cget("text") for k, (_c, st) in tab.hotkey_labels.items()}
        self.assertEqual(texts["push_to_talk"], "✕ in use by another program")
        self.assertEqual(texts["show_window"], "✓ active")
        self.app.capture.value = 0.3
        self.assertTrue(wait_for(win, lambda: tab.level.get() > 0.5))
        shoot(win, "settings_live.png")

    def test_event_dialog_edit_and_new(self):
        win, cal, st = self.win, self.win.calendar_tab, self.app.store
        self.assertTrue(select_tab(win, "Calendar"))
        cal.select(dt.date(2026, 9, 13))
        row = next(r for r in cal.rows if r.obj is not None and r.kind == "event" and r.raw_text() == "Team standup")
        with keep_user_focus():
            dlg = cal.edit_dialog(row)
            wait_for(win, lambda: dlg.winfo_ismapped(), 3)
        self.assertEqual(dlg.title_entry.get(), "Team standup")
        self.assertEqual((dlg.hour.get(), dlg.minute.get(), dlg.ampm.get()), ("9", "30", "AM"))
        self.assertEqual(dlg.reminder.get(), "10 minutes before")
        shoot_toplevel(win, dlg, "event_dialog_edit.png")
        dlg.hour.set("10")
        dlg.minute.set("15")
        dlg.reminder.set("15 minutes before")
        dlg.repeat.set("Daily")
        dlg.title_entry.delete(0, "end")
        self.assertIsNone(dlg.save())
        self.assertEqual(dlg.error.cget("text"), "Give it a title.")
        dlg.title_entry.insert(0, "Standup")
        dlg.save()
        it = st.find("standup")
        self.assertEqual((it["title"], it["time"], it["reminder_min"], it["repeat"]),
                         ("Standup", "10:15", 15, "daily"))
        self.assertIsNone(cal.dialog)
        with keep_user_focus():
            dlg = cal.new_item_dialog(dt.date(2026, 9, 16))
            wait_for(win, lambda: dlg.winfo_ismapped(), 3)
        dlg.kind.set("To-do")
        dlg._sync()
        dlg.title_entry.insert(0, "Pack bags")
        dlg.all_day.deselect()
        dlg._sync()
        dlg.hour.set("7")
        dlg.minute.set("00")
        dlg.ampm.set("PM")
        dlg.reminder.set("At the time")
        dlg._shift(1)
        dlg.save()
        it = st.find("pack bags")
        self.assertEqual((it["kind"], it["date"], it["time"], it["reminder_min"], it["repeat"]),
                         ("task", "2026-09-17", "19:00", 0, None))
        self.assertEqual(cal.selected, dt.date(2026, 9, 17))
        with keep_user_focus():
            dlg = cal.new_item_dialog()
            wait_for(win, lambda: dlg.winfo_ismapped(), 3)
        dlg.date_entry.delete(0, "end")
        dlg.date_entry.insert(0, "17/09/2026")
        dlg.title_entry.insert(0, "x")
        self.assertIsNone(dlg.save())
        self.assertIn("2026-09-14", dlg.error.cget("text"))
        shoot_toplevel(win, dlg, "event_dialog_error.png")
        dlg.cancel()
        self.assertTrue(wait_for(win, lambda: cal.dialog is None))


# ------------------------------------------------------------- light / dark ----
def _lum(color: str) -> float:
    h = color.lstrip("#")
    ch = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    lin = [v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4 for v in ch]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast(fg: str, bg: str) -> float:
    a, b = sorted((_lum(fg), _lum(bg)), reverse=True)
    return (a + 0.05) / (b + 0.05)


CONTRAST_PAIRS = [   # (name, fg, bg, minimum in light). Dark mode keeps the v1 values and must reach 3.0.
    ("text on bg", theme.TEXT, theme.BG, 7.0), ("text on card", theme.TEXT, theme.CARD, 7.0),
    ("title on card", theme.TITLE, theme.CARD, 7.0), ("muted on bg", theme.MUTED, theme.BG, 4.5),
    ("muted on card", theme.MUTED, theme.CARD, 4.5), ("faint on bg", theme.FAINT, theme.BG, 3.0),
    ("accent text on bg", theme.ACCENT, theme.BG, 4.5), ("accent text on card", theme.ACCENT, theme.CARD, 4.5),
    ("white on accent", theme.ON_ACCENT, theme.ACCENT, 4.5), ("segment text on selected", theme.SEG_TEXT,
                                                               theme.SEG_SEL, 4.5),
    ("segment text on unselected", theme.SEG_TEXT, theme.LINE, 4.5), ("text on grey button", theme.TEXT, theme.LINE, 4.5),
    ("red on bg", theme.RED, theme.BG, 4.5), ("green on bg", theme.GREEN, theme.BG, 4.5),
    ("amber on bg", theme.AMBER, theme.BG, 4.5), ("note on bg", theme.NOTE, theme.BG, 4.5),
    ("event time on cell", theme.ACCENT_SOFT, theme.SURFACE, 4.5), ("text on cell", theme.TEXT, theme.SURFACE, 7.0),
    ("text on range", theme.TEXT, theme.RANGE, 7.0), ("amber on parked banner", theme.AMBER, theme.AMBER_BG, 4.5),
    ("red on delete hover", theme.RED, theme.RED_BG, 4.5), ("weekend label on bg", theme.WEEKEND, theme.BG, 3.0),
]


class LightModeTests(UICase):
    """ui.appearance: light palette, screenshots, live switching (Settings, "theme:*" messages, Windows)."""

    def setUp(self):
        self.app = make_app(self.tmp, real_engine=False, appearance="light")
        seed_engine(self.app, self.tmp)
        seed_busy_day(self.app.store)
        self.win = self.open(self.app, title="XyrusUITestLight")

    def test_contrast_of_both_palettes(self):
        bad = []
        for name, fg, bg, minimum in CONTRAST_PAIRS:
            for mode, i, need in (("light", 0, minimum), ("dark", 1, 3.0)):
                r = contrast(fg[i], bg[i])
                if r < need:
                    bad.append(f"{mode} {name}: {fg[i]} on {bg[i]} = {r:.2f} < {need}")
        self.assertFalse(bad, "\n".join(bad))

    def test_light_screenshots(self):
        win, home, cal = self.win, self.win.home_tab, self.win.calendar_tab
        self.assertEqual(theme.mode(), "light")
        self.assertEqual(win.appearance, "light")
        for name in ("Home", "Calendar", "Commands", "Apps", "Routines", "Settings"):
            self.assertTrue(select_tab(win, name), f"{name} tab did not come up")
            path = shoot(win, f"light_tab_{name}.png")
            self.assertTrue(not_uniform(path), f"light {name} screenshot is blank")
        self.assertTrue(select_tab(win, "Home"))
        self.assertTrue(wait_for(win, lambda: win.banner.winfo_manager() == "pack"))
        shoot(win, "light_banner.png")
        self.assertTrue(select_tab(win, "Calendar"))
        cal.select(dt.date(2026, 9, 17))
        shoot(win, "light_cal_busy_day.png")
        # plain-tk parts resolved to the light palette
        self.assertEqual(home.rows[-1].frame.cget("bg"), theme.BG[0])
        self.assertEqual(home.rows[-1].ts.cget("fg"), theme.FAINT[0])
        self.assertEqual(win.commands_tab.rows[0].phrase_lbl.cget("fg"), theme.TEXT[0])
        cell = next(c for c in cal.cells if c.date == dt.date(2026, 9, 1))
        self.assertEqual(cell.cv.itemcget(cell.cv.find_withtag("cal")[0], "fill"), theme.TEXT[0])
        edit = next(r for r in cal.rows if r.obj is not None and r.kind == "task").edit
        self.assertEqual(edit.cget("bg"), theme.BG[0])

    def test_switch_live_and_follow_windows(self):
        win, home, cal, st_tab = self.win, self.win.home_tab, self.win.calendar_tab, self.win.settings_tab
        cfg, q = self.app.config, self.app.ui_queue
        cell = next(c for c in cal.cells if c.date == dt.date(2026, 9, 1))
        number_fill = lambda: cell.cv.itemcget(cell.cv.find_withtag("cal")[0], "fill")
        self.assertTrue(select_tab(win, "Calendar"))       # canvases only draw once they have a size
        self.assertTrue(wait_for(win, lambda: cell.cv.find_withtag("cal")), "day numbers never drawn")
        self.assertEqual(number_fill(), theme.TEXT[0])
        with keep_user_focus():
            q.put("theme:dark")                           # what voice sends
            self.assertTrue(wait_for(win, lambda: theme.mode() == "dark"))
            self.assertTrue(wait_for(win, lambda: home.rows[-1].frame.cget("bg") == theme.BG[1]))
        self.assertEqual(cfg.get("ui.appearance"), "dark")
        self.assertEqual(number_fill(), theme.TEXT[1], "calendar canvas re-drawn for dark")
        self.assertEqual(win.commands_tab.rows[0].frame.cget("bg"), theme.BG[1])
        self.assertEqual(st_tab.appearance_seg.get(), "Dark", "Settings follows a voice change")
        with keep_user_focus():
            st_tab._pick_appearance("Light")              # the Settings control
            self.assertTrue(wait_for(win, lambda: theme.mode() == "light" and number_fill() == theme.TEXT[0]))
        self.assertEqual(cfg.get("ui.appearance"), "light")
        # a hidden (tray) window stays hidden when the theme changes
        win.withdraw()
        with keep_user_focus():
            q.put("theme:dark")
            self.assertTrue(wait_for(win, lambda: theme.mode() == "dark"))
            pump(win, 0.3)
        self.assertEqual(win.state(), "withdrawn", "theme change re-showed a hidden window")
        with keep_user_focus():
            win.deiconify()
            wait_for(win, lambda: win.state() == "normal")
        # Follow Windows: resolved from AppsUseLightTheme and re-checked while running
        with mock.patch.object(theme, "windows_light", return_value=True), keep_user_focus():
            q.put("theme:system")
            self.assertTrue(wait_for(win, lambda: theme.mode() == "light"))
        self.assertEqual((cfg.get("ui.appearance"), win.appearance), ("system", "system"))
        self.assertEqual(st_tab.appearance_seg.get(), "Follow Windows")
        with mock.patch.object(theme, "windows_light", return_value=False), keep_user_focus():
            win._next_system_check = 0                    # don't wait the 3 s poll interval
            self.assertTrue(wait_for(win, lambda: theme.mode() == "dark" and number_fill() == theme.TEXT[1]))
        q.put("theme:purple")                             # ignored
        self.assertTrue(wait_for(win, lambda: q.empty()))
        self.assertEqual(win.appearance, "system")


class CalendarTabV1Checks(UICase):
    """The v1 Calendar tab's 64 end-to-end checks (scratchpad caltab/test_caltab.py), re-expressed against
    the lifted tab inside XyrusWindow + FakeApp, plus the v2 additions (keyboard, ✎ dialog, inline delete
    confirm, todo prefix, 14-day upcoming, show:Calendar:<iso>)."""

    def test_v1_calendar_checks(self):
        results = []

        def check(cond, msg):
            results.append((bool(cond), msg))

        D = lambda m, d: dt.date(2026, m, d)
        TODAY = D(9, 13)
        clock = FakeClock(NOW)
        st = CalendarStore(Path(tempfile.mkdtemp(dir=self.tmp)) / "calendar.json")
        st.add_task("Submit lab report", D(9, 11))
        st.add_task("Renew library books", D(9, 12), "18:00", 0)
        t_groc = st.add_task("Buy groceries", TODAY)
        st.add_task("Call the bank", TODAY, "16:00", 0)
        t_water = st.add_task("water the plants", TODAY, source="voice")
        st.set_done(t_water["id"], True)
        st.add_event("Team standup", TODAY, "09:30", 10)
        e_dent = st.add_event("Dentist appointment", TODAY, "15:00", 30)
        st.add_event("Dinner with Sara", TODAY, "19:30", 60)
        st.add_event("Gym", D(9, 7), "18:00", 0, repeat="weekly")
        st.add_note(TODAY, "Parking is on level 3, spot B12")
        n_long = st.add_note(TODAY, "Ask Sara about the conference tickets before Friday, the early-bird price ends soon")
        st.add_task("Pay electricity bill", D(9, 14), "10:00", 0)
        st.add_event("Project review", D(9, 14), "11:00", 10)
        st.add_task("Book flights", D(9, 18))
        st.add_event("Exam: Operating Systems", D(9, 22), "09:00", 60)
        st.add_note(D(9, 25), "Mom's birthday next week - order the cake")
        st.add_event("Rent due", D(9, 30))
        seed_busy_day(st)
        busy_n = len(st.items_between(D(9, 17), D(9, 17)))

        app = make_app(self.tmp, store=st, clock=clock, real_engine=False)
        t0 = time.perf_counter()
        win = new_window(app, title="XyrusCalTest", geometry="860x580")
        build_s = time.perf_counter() - t0
        tab = win.calendar_tab
        try:
            p = lambda s=0.4: pump(win, s)
            w = lambda cond, t=3.0: wait_for(win, cond, t)

            def rows():
                return [r for r in tab.rows if r.obj is not None]

            def row_by(text):
                return next((r for r in rows() if r.raw_text() == text), None)

            def cell_of(d):
                return next(c for c in tab.cells if c.date == d)

            check(select_tab(win, "Calendar"), "Calendar tab comes up")
            check(tab.store is st, "tab bound to app.store")
            check(tab.month_lbl.cget("text") == "September 2026", "month header shows September 2026")
            check(tab.selected == TODAY and tab.day_title.cget("text") == "Sunday, 13 September",
                  "today selected, title 'Sunday, 13 September'")
            check(tab.day_tag.winfo_manager() and "Today" in tab.day_tag.cget("text"), "Today tag shown")
            titles = [r.raw_text() for r in rows()]
            check(titles[:2] == ["Submit lab report", "Renew library books"], f"overdue section first: {titles[:2]}")
            hdrs = [h.cget("text") for h in tab.headers if h.winfo_manager()]
            check([h.split()[0] for h in hdrs] == ["OVERDUE", "TO-DO", "EVENTS", "NOTES"], f"sections {hdrs}")
            check(row_by("water the plants").check.get() == 1
                  and row_by("water the plants").title.cget("font") is tab.f_row_done, "done to-do ticked + struck through")
            check(row_by("Dentist appointment").meta.cget("text") == "🔔 30m", "event bell tag '🔔 30m'")
            c13 = cell_of(TODAY)
            check(c13.colors[0] == CT.ACCENT and c13.colors[1] == CT.SEL_RING, "today cell filled (selected) + outlined")
            check(cell_of(D(9, 1)).colors[0] == CT.SURFACE and cell_of(D(8, 31)).colors[0] == CT.BG,
                  "other-month cells dimmed")
            check(w(lambda: len(cell_of(D(9, 21)).cv.find_withtag("dots")) > 0),
                  "weekly Gym repeat shows a dot on Mon 21 Sep")
            up = [(d, it["title"]) for d, it in tab.upcoming_entries()]
            check(("Team standup" not in [t for _, t in up]) and (TODAY, "Dentist appointment") in up,
                  "upcoming skips today's past events, keeps later ones")
            shoot(win, "cal_1_month_today.png")

            cell_of(D(9, 17)).cv.event_generate("<Button-1>", x=6, y=6)
            check(w(lambda: tab.selected == D(9, 17)), "clicking the 17th cell selects it")
            check(len([r for r in rows() if r.kind != "note"]) == busy_n, f"busy day shows all {busy_n} items")
            shoot(win, "cal_2_busy_day.png")

            tab.shift_month(1)
            check(tab.month_lbl.cget("text") == "October 2026" and tab.selected == D(9, 17),
                  "› moves to October, selection kept")
            tab.shift_month(-2)
            check(tab.month_lbl.cget("text") == "August 2026", "‹‹ moves to August")
            tab.go_today()
            check(tab.month_lbl.cget("text") == "September 2026" and tab.selected == TODAY, "Today button returns")

            tab.select(D(9, 29))
            check(not rows() and tab.empty.winfo_manager(), "empty state on Tue 29 Sep")
            shoot(win, "cal_3_empty_day.png")

            def qa(kind, text, rem=None, via_key=False):
                tab.kind_seg.set(kind)
                tab._kind_changed(kind)
                tab.entry.delete(0, "end")
                tab.entry.insert(0, text)
                tab._preview()
                if rem:
                    tab.rem_menu.set(rem)
                    tab._rem_picked(rem)
                before_v = st.version
                if via_key:                               # Tk-internal event: no OS input, no focus grab
                    tab.entry._entry.event_generate("<Return>")
                    win.update()
                    if st.version == before_v:
                        tab.quick_add()
                else:
                    tab.quick_add()
                win.update()
                return st.version != before_v

            tab.select(D(9, 15))
            qa("To-do", "buy milk", via_key=True)
            it = st.find("buy milk")
            check(it and it["kind"] == "task" and it["date"] == "2026-09-15" and it["time"] is None
                  and it["reminder_min"] is None, "to-do w/o date -> selected day 15th, all-day, no reminder")
            check(tab.status.cget("text").startswith("Added to-do: Buy milk – Tue 15 Sep"),
                  f"flash: {tab.status.cget('text')!r}")

            qa("To-do", "Call Mom tomorrow at 5")
            it = st.find("call mom")
            check(it and it["title"] == "Call Mom" and it["date"] == "2026-09-14" and it["time"] == "17:00"
                  and it["reminder_min"] == 0, "to-do with date: 'Call Mom' Mon 14 17:00 remind at time")
            check(tab.selected == D(9, 14), "after add the item's day is selected")

            tab.select(D(9, 15))
            tab.kind_seg.set("Event")
            tab._kind_changed("Event")
            tab.entry.delete(0, "end")
            tab.entry.insert(0, "Dentist 3pm")
            tab._preview()
            check(tab.rem_menu.get() == "At time", f"reminder menu auto-set to 'At time': {tab.rem_menu.get()}")
            check("Tue 15 Sep · 3 pm" in tab.status.cget("text"), f"live preview: {tab.status.cget('text')!r}")
            shoot(win, "cal_4a_quickadd_preview.png")
            qa("Event", "Dentist 3pm")
            it = next(i for i in st.data["items"] if i["title"] == "Dentist" and i["kind"] == "event")
            check(it["date"] == "2026-09-15" and it["time"] == "15:00" and it["reminder_min"] == 0,
                  "event w/o date keeps parsed time on the selected day")
            shoot(win, "cal_4b_quickadd_confirm.png")

            qa("Event", "gym every monday at 6pm")
            it = next(i for i in st.data["items"] if i["title"] == "gym")
            check(it["repeat"] == "weekly" and it["date"] == "2026-09-14" and it["time"] == "18:00",
                  f"recurring event: {it['repeat'], it['date'], it['time']}")

            qa("Event", "flight oct 3 at 14:20 30 minutes before")
            it = st.find("flight")
            check(it["date"] == "2026-10-03" and it["time"] == "14:20" and it["reminder_min"] == 30
                  and tab.month_lbl.cget("text") == "October 2026", "typed reminder offset + month jump")

            tab.select(D(9, 15))
            qa("Event", "Parent meeting 11am", rem="10 min before")
            it = st.find("parent meeting")
            check(it["reminder_min"] == 10 and it["time"] == "11:00", f"hand-picked reminder wins: {it['reminder_min']}")
            qa("Event", "Holiday")
            it = st.find("holiday")
            check(it["time"] is None and it["reminder_min"] is None and it["date"] == "2026-09-15",
                  "all-day event: no reminder by default")

            qa("Note", "wifi password is on the router")
            n = next(n for n in st.data["notes"] if n["text"] == "wifi password is on the router")
            check(n["date"] == "2026-09-15", "note w/o date -> selected day")
            qa("Note", "pay rent on the 20th")
            n = next(n for n in st.data["notes"] if n["text"] == "pay rent on the 20th")
            check(n["date"] == "2026-09-20" and tab.selected == D(9, 20), "note naming a date -> that date (whole text kept)")

            tab.select(D(9, 15))
            qa("To-do", "check the oven in 20 minutes")
            it = st.find("check the oven")
            check(it["date"] == "2026-09-13" and it["time"] == "10:50",
                  f"'in 20 minutes' goes on today, not the selected day: {(it['date'], it['time'])}")

            v = st.version
            tab.entry.delete(0, "end")
            tab.quick_add()
            check(st.version == v and "Type what" in tab.status.cget("text"), "empty quick-add refused")

            # ------------------------------------------------- toggle / edit / del
            tab.select(TODAY)
            r = row_by("Buy groceries")
            r.check.toggle()
            check(st.get_item(t_groc["id"])["done"] is True, "ticking a to-do -> set_done(True)")
            r = row_by("Buy groceries")
            check(r.check.get() == 1 and r.title.cget("text_color") == CT.MUTED, "re-rendered as done (muted)")
            r.check.toggle()
            check(st.get_item(t_groc["id"])["done"] is False, "unticking -> set_done(False)")
            r = row_by("Submit lab report")
            r.check.toggle()
            check(st.find("submit lab report")["done"] and row_by("Submit lab report") is None,
                  "ticking an overdue to-do clears it from Overdue")

            r = row_by("Dentist appointment")
            tab._start_edit(r)
            check(tab._editing is r and r.editor.winfo_manager(), "double-click opens the inline editor")
            r.editor.delete(0, "end")
            r.editor.insert(0, "Dentist (Dr. Rahman)")
            tab._commit_edit()
            check(st.get_item(e_dent["id"])["title"] == "Dentist (Dr. Rahman)" and row_by("Dentist (Dr. Rahman)") is not None,
                  "Enter saves the edited title via update_item")
            r = row_by(n_long["text"])
            tab._start_edit(r)
            r.editor.delete(0, "end")
            r.editor.insert(0, "SHOULD NOT SAVE")
            tab._cancel_edit()
            check(st.notes_on(TODAY)[1]["text"] == n_long["text"], "Esc cancels the edit")
            r = row_by("Parking is on level 3, spot B12")
            tab._start_edit(r)
            r.editor.delete(0, "end")
            r.editor.insert(0, "Parking: level 3, B12")
            tab._commit_edit()
            check(any(n["text"] == "Parking: level 3, B12" for n in st.notes_on(TODAY)), "note edit via update_note")

            r = row_by("Dinner with Sara")
            dinner_id = r.obj["id"]
            r.delete.invoke()                                   # v2: events ask inline first
            check(w(lambda: r.confirming and r.confirm.winfo_manager()) and st.get_item(dinner_id) is not None,
                  "✕ on an event asks 'Delete? Yes' inline")
            shoot(win, "cal_5_delete_confirm.png")
            r.confirm_yes.invoke()
            check(st.get_item(dinner_id) is None and row_by("Dinner with Sara") is None, "Yes deletes the event")
            check(w(lambda: tab.undo_btn.winfo_manager()) and "Deleted event" in tab.status.cget("text"),
                  "delete offers Undo")
            tab.undo_btn.invoke()
            back = st.find("dinner with sara")
            check(back and back["time"] == "19:30" and back["reminder_min"] == 60 and row_by("Dinner with Sara"),
                  "Undo restores it")
            r = row_by("Parking: level 3, B12")
            r.delete.invoke()
            check(not any(n["text"].startswith("Parking") for n in st.notes_on(TODAY)), "✕ deletes a note")

            # ----------------------------------------------------------- ui_queue
            select_tab(win, "Home")
            app.ui_queue.put("calendar:2026-09-14:2026-09-20")
            check(w(lambda: win.tab_ready("Calendar")), "calendar: message switches to the Calendar tab")
            check(w(lambda: tab.selected == D(9, 14) and tab.range == (D(9, 14), D(9, 20))),
                  f"range selected {tab.selected} {tab.range}")
            check(cell_of(D(9, 16)).colors[0] == CT.RANGE and cell_of(D(9, 21)).colors[0] != CT.RANGE, "range cells tinted")
            shoot(win, "cal_6_range_week.png")
            app.ui_queue.put("calendar:2026-11-02")
            check(w(lambda: tab.month_lbl.cget("text") == "November 2026" and tab.selected == D(11, 2)
                    and tab.range is None), "single-day calendar: message")
            select_tab(win, "Home")
            app.ui_queue.put("show:Calendar")
            check(w(lambda: win.tabs.get() == "Calendar"), "show:Calendar still works")
            p(0.3)
            check(win.tab_ready("Calendar"), "tab switch right after another switch is not left blank")
            app.ui_queue.put("show:Commands")
            app.ui_queue.put("calendar:2026-09-16")
            check(w(lambda: tab.selected == D(9, 16)), "calendar: after show:Commands selects the 16th")
            p(0.3)
            check(win.tab_ready("Calendar") and not win.tabs.tab("Commands").winfo_manager(),
                  "two tab requests in one poll -> Calendar shown, Commands hidden")
            tab.select(TODAY)
            first_up = next(r for r in tab.up_rows if r["date"])
            first_up["title"]._label.event_generate("<Button-1>", x=3, y=3)
            check(w(lambda: tab.selected == first_up["date"]), f"clicking an upcoming entry selects {first_up['date']}")

            # ------------------------------------------------- voice-thread change
            tab.select(TODAY)
            win.update()
            seen = tab._seen_version
            th = threading.Thread(target=lambda: st.add_task("pick up dry cleaning", TODAY, "12:00", 0, source="voice"))
            th.start()
            th.join()
            check(row_by("pick up dry cleaning") is None, "not rendered before the poll")
            check(w(lambda: row_by("pick up dry cleaning") is not None) and tab._seen_version == st.version != seen,
                  "voice add re-rendered on the next poll")
            check(any(it["title"] == "pick up dry cleaning" for _, it in tab.upcoming_entries())
                  and tab.up_count.cget("text") == str(len(tab.upcoming_entries())), "upcoming list + count updated too")
            th = threading.Thread(target=lambda: st.delete_item(st.find("pick up dry cleaning")["id"]))
            th.start()
            th.join()
            check(w(lambda: row_by("pick up dry cleaning") is None), "voice delete re-rendered")
            cnt_before = cell_of(D(9, 24)).state
            st.add_event("Voice event", D(9, 24), "10:00", None, source="voice")
            check(w(lambda: cell_of(D(9, 24)).state != cnt_before), "grid counts refresh on voice change")

            clock.set(dt.datetime(2026, 9, 14, 0, 0, 5))
            check(w(lambda: tab.today == D(9, 14) and tab.selected == D(9, 14)), "midnight: 'today' follows the clock")
            clock.set(NOW)
            w(lambda: tab.today == TODAY)
            tab.select(TODAY)

            # ------------------------------------------------------------ 200 items
            random.seed(7)
            for i in range(200):
                d = D(9, 1) + dt.timedelta(days=random.randrange(0, 61))
                tm = random.choice([None, "08:00", "12:30", "18:00", "20:15"])
                if i % 3:
                    st.add_task(f"Task number {i}", d, tm, 0 if tm else None)
                else:
                    st.add_event(f"Event number {i}", d, tm, 10 if tm else None, repeat="weekly" if i % 40 == 0 else None)
            t0 = time.perf_counter()
            tab._dirty |= {"grid", "day", "upcoming"}
            tab._flush()
            full = time.perf_counter() - t0
            days = [D(9, 3), D(9, 17), D(10, 5), D(9, 22), TODAY]

            def timed_pass():
                out = []
                for d in days:
                    t = time.perf_counter()
                    tab.select(d)
                    win.update_idletasks()
                    out.append(time.perf_counter() - t)
                return out

            # The first pass also grows the pooled agenda rows (a one-off per session, like v1); the
            # responsiveness claim is about steady state, so it is judged on the second pass, with a
            # generous bound on the cold one (CPU contention from earlier test modules made the old
            # single-pass 500 ms bound flaky: 528 ms in the combined run).
            cold = timed_pass()
            warm = timed_pass()
            t0 = time.perf_counter()
            for _ in range(50):
                tab.poll()
            idle = (time.perf_counter() - t0) / 50
            print(f"\n  calendar: build {build_s * 1000:.0f} ms, full re-render {full * 1000:.0f} ms, "
                  f"select day cold max {max(cold) * 1000:.0f} ms / warm max {max(warm) * 1000:.0f} ms, "
                  f"idle poll {idle * 1e6:.0f} us")
            # warm bound: 0.5 s alone; 0.8 s in a combined run (T6: 506 ms once in the full suite with other
            # modules' leftovers competing for the CPU; 301 ms alone on the same machine)
            combined = any(re.fullmatch(r"(tests\.)?test_\w+", m) and not m.endswith("test_ui_render")
                           for m in sys.modules)
            check(max(warm) < (0.8 if combined else 0.5) and max(cold) < 2.0 and full < 1.0 and idle < 0.002,
                  "responsive with ~230 items")
            tab.select(TODAY)
            shoot(win, "cal_7_month_230_items.png")

            win.geometry("960x640")
            w(lambda: abs(win.winfo_width() - 960) < 40)
            shoot(win, "cal_8_at_960x640.png")
            win.geometry("760x520")
            w(lambda: abs(win.winfo_width() - 760) < 40)
            shoot(win, "cal_9_at_min_760x520.png")
            check(tab.cells[0].frame.winfo_ismapped() and tab.entry.winfo_ismapped(), "usable at the minimum size")
            win.geometry("860x580")
            select_tab(win, "Commands")
            secs = [s for s, _ in app.registry.sections(app.config)]
            check(list(win.commands_tab.headers) == secs, f"Commands tab renders registry sections: {secs[:4]}…")

            # ------------------------------------------------------ v2 additions
            select_tab(win, "Calendar")
            tab.select(TODAY)
            tab._on_key(SimpleNamespace(widget=win), lambda: tab.step_day(1))
            check(tab.selected == D(9, 14), "Right arrow -> next day")
            tab._on_key(SimpleNamespace(widget=win), lambda: tab.step_day(7))
            check(tab.selected == D(9, 21), "Down arrow -> +1 week")
            tab._on_key(SimpleNamespace(widget=win), lambda: tab.select(CT.add_months(tab.selected, 1)))
            check(tab.selected == D(10, 21) and tab.month_lbl.cget("text") == "October 2026", "PgDn -> +1 month")
            tab._on_key(SimpleNamespace(widget=win), tab.go_today)
            check(tab.selected == TODAY, "Home -> today")
            tab._on_key(SimpleNamespace(widget=tab.entry._entry), lambda: tab.step_day(1))
            check(tab.selected == TODAY, "arrows ignored while typing in quick-add")
            check(CT.add_months(dt.date(2026, 1, 31), 1) == dt.date(2026, 2, 28), "month step clamps the day")
            qa("Event", "todo water the garden tomorrow")
            it = st.find("water the garden")
            check(it and it["kind"] == "task" and it["date"] == "2026-09-14", "leading 'todo' makes a to-do")
            check(len(tab.upcoming_entries()) and max(d for d, _ in tab.upcoming_entries()) <= D(9, 26)
                  and any(d > D(9, 19) for d, _ in tab.upcoming_entries()), "upcoming covers 14 days")
            tab.select(TODAY)
            r = row_by("Buy groceries")
            with keep_user_focus():
                dlg = tab.edit_dialog(r)
                w(lambda: dlg.winfo_ismapped())
            check(dlg is not None and dlg.title_entry.get() == "Buy groceries" and dlg.kind.get() == "To-do",
                  "✎ opens the EventDialog for the to-do")
            dlg.cancel()
            w(lambda: tab.dialog is None)
            r = row_by("Buy groceries")
            r.delete.invoke()
            check(st.get_item(t_groc["id"]) is None, "✕ on a to-do deletes at once (Undo offered)")
            app.ui_queue.put("show:Calendar:2026-10-05")
            check(w(lambda: tab.selected == D(10, 5)), "show:Calendar:<iso> selects that day")
            close_window(win)
            win = None

            # --------------------------------------------------- unavailable state
            app2 = make_app(self.tmp, store=None, real_engine=False)
            win = new_window(app2, title="XyrusCalTest", geometry="860x580")
            app2.ui_queue.put("calendar:2026-09-14:2026-09-20")
            check(wait_for(win, lambda: win.tab_ready("Calendar")) and win.calendar_tab is not None
                  and win.calendar_tab.store is None,
                  "no app.store -> disabled notice, no crash, calendar: message handled")
            shoot(win, "cal_10_unavailable.png")
        finally:
            if win is not None:
                close_window(win)
        check(_real_state() == self.real_before, "real data/calendar.json + config.json untouched")
        bad = [m for ok, m in results if not ok]
        print(f"  calendar checks: {len(results) - len(bad)}/{len(results)} passed")
        self.assertFalse(bad, "\n".join(bad))
        self.assertGreaterEqual(len(results), 64)


if __name__ == "__main__":
    unittest.main()
