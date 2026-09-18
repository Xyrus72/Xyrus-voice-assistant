"""T6 app smoke (SPEC §7.7, adapted while v1 is live): the full App built by xyrus/app.py.

- config v1 -> v2 migration on temp copies (the real D:\\arc\\config.json, when it exists, is only read);
- the full App in this process (sandbox: no mic, no SAPI, no tray, no hotkeys, FakeActions) with its real
  window: typed commands through the threaded engine, UI-queue messages, pause, theme, quit from another thread,
  then no leaked threads or windows;
- subprocesses: `--text` prints a reply starting "It's", the arc.py shim, `--selftest` exits 0, and two
  instances (private XYRUS_INSTANCE names - v1 holds the real ones): the second exits 0 and the first's window
  (found by PID, not by title - the live v1 window is also called "Xyrus") becomes visible.

Never touches real data/logs (XYRUS_DATA_DIR temp dirs), never injects keys (force_foreground is disabled),
kills only the processes it started.
Run: venv\\Scripts\\python.exe -m unittest tests.test_app_smoke -v
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

if not os.environ.get("XYRUS_DATA_DIR"):
    os.environ["XYRUS_DATA_DIR"] = tempfile.mkdtemp(prefix="xyrus_test_")

BASE = Path(__file__).resolve().parent.parent
PY = str(BASE / "venv" / "Scripts" / "python.exe") if (BASE / "venv" / "Scripts" / "python.exe").exists() \
    else sys.executable
NO_WINDOW = 0x08000000
HAVE_MODEL = (BASE / "model" / "am" / "final.mdl").exists()
REAL_CONFIG = BASE / "config.json"
SHIM = BASE / "arc.py"                       # the v2 shim (docs/staging/arc.py before the switch-over)
if not (SHIM.exists() and "xyrus.app" in SHIM.read_text(encoding="utf8", errors="replace")):
    SHIM = BASE / "docs" / "staging" / "arc.py"

# v1 config.py DEFAULTS as a v1 install writes them (no "version"), with user edits and a key v2 doesn't know
V1_FIXTURE = {
    "voice_replies": False,
    "confirm_shutdown": False,
    "briefing": False,
    "apps": {"chrome": "chrome", "browser": "chrome", "edge": "msedge", "youtube": "https://www.youtube.com",
             "spotify": "spotify:", "notepad": "notepad", "calculator": "calc", "explorer": "explorer",
             "files": "explorer", "code": "code", "settings": "ms-settings:", "task manager": "taskmgr",
             "terminal": "wt", "discord": "discord:", "obs": "obs64"},
    "some_future_key": {"x": 1},
}

_u32 = ctypes.WinDLL("user32")
_u32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
_u32.GetWindowTextLengthW.argtypes = [wt.HWND]
_u32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
_u32.IsWindowVisible.argtypes = [wt.HWND]
_ENUM = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
_u32.EnumWindows.argtypes = [_ENUM, wt.LPARAM]


def windows_of(pid: int, title: str = "Xyrus") -> list[tuple[int, bool]]:
    """(hwnd, visible) of the top-level windows of process `pid` titled `title`."""
    out: list[tuple[int, bool]] = []

    def cb(hwnd, _):
        p = wt.DWORD()
        _u32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
        if p.value == pid:
            n = _u32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(n + 1)
            _u32.GetWindowTextW(hwnd, buf, n + 1)
            if buf.value == title:
                out.append((int(hwnd), bool(_u32.IsWindowVisible(hwnd))))
        return True
    _u32.EnumWindows(_ENUM(cb), 0)
    return out


def windows_of_tree(pid: int, title: str = "Xyrus") -> list[tuple[int, bool]]:
    """windows_of for a process and its children: venv\\Scripts\\python.exe is a launcher that runs the real
    interpreter as a child process, which is the one that owns the window."""
    import psutil
    pids = [pid]
    try:
        pids += [c.pid for c in psutil.Process(pid).children(recursive=True)]
    except psutil.Error:
        pass
    return [w for p in pids for w in windows_of(p, title)]


def child_env(tmp: Path, **extra) -> dict:
    env = dict(os.environ)
    env.update({"XYRUS_DATA_DIR": str(tmp), "XYRUS_NO_HEARING": "1", "PYTHONIOENCODING": "utf-8"})
    env.update(extra)
    return env


def wait_until(cond, timeout: float, step: float = 0.1) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            if cond():
                return True
        except Exception:
            pass
        time.sleep(step)
    return False


# ============================================================================ config migration
class ConfigMigration(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="xyrus_cfg_"))
        self.real_before = REAL_CONFIG.stat().st_mtime if REAL_CONFIG.exists() else None

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        self.assertEqual(self.real_before, REAL_CONFIG.stat().st_mtime if REAL_CONFIG.exists() else None,
                         "the real config.json was touched")

    def v1_file(self, data=None) -> Path:
        p = self.tmp / "config.json"
        p.write_text(json.dumps(V1_FIXTURE if data is None else data, indent=2), encoding="utf8")
        return p

    def test_v1_keys_are_mapped_and_unknown_keys_kept(self):
        from xyrus.config import DEFAULTS, Config
        cfg = Config(self.v1_file()).load()
        self.assertEqual(cfg.migrated_from, 1)
        self.assertEqual(cfg.get("version"), 2)
        self.assertIs(cfg.get("voice_replies"), False)
        self.assertIs(cfg.get("confirm_shutdown"), False)
        self.assertIs(cfg.get("calendar.morning_brief"), False)              # v1 "briefing"
        self.assertEqual(cfg.get("calendar.morning_brief_time"), "08:30")    # rest of the section from DEFAULTS
        self.assertEqual(cfg.get("some_future_key"), {"x": 1})               # unknown keys survive
        apps = cfg.get("apps")
        self.assertEqual(apps["obs"], "obs64")                               # the user's own alias
        self.assertEqual(apps["bluetooth settings"], "ms-settings:bluetooth")  # new v2 deep links merged in
        self.assertEqual(cfg.get("wake_words"), DEFAULTS["wake_words"])
        self.assertNotIn("briefing", cfg.all())

    def test_migrate_file_is_atomic_and_keeps_the_original(self):
        from xyrus.config import Config
        path = self.v1_file()
        original = path.read_bytes()
        cfg = Config(path).load()
        self.assertTrue(cfg.migrate_file())
        saved = json.loads(path.read_text(encoding="utf8"))
        self.assertEqual(saved["version"], 2)
        self.assertEqual(saved["apps"]["obs"], "obs64")
        self.assertEqual((self.tmp / "config.v1.json").read_bytes(), original)
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), ["config.json", "config.v1.json"])  # no .tmp
        again = Config(path).load()
        self.assertIsNone(again.migrated_from)
        self.assertEqual(again.all(), cfg.all())
        self.assertFalse(again.migrate_file())                               # v2 file: nothing to do
        self.assertEqual((self.tmp / "config.v1.json").read_bytes(), original)

    def test_a_v2_file_is_not_migrated(self):
        from xyrus.config import Config
        p = self.tmp / "config.json"
        p.write_text(json.dumps({"version": 2, "apps": {"chrome": "chrome"}, "voice_replies": False}), encoding="utf8")
        cfg = Config(p).load()
        self.assertIsNone(cfg.migrated_from)
        self.assertEqual(cfg.get("apps"), {"chrome": "chrome"})              # v2 apps replace the defaults
        self.assertFalse(cfg.migrate_file())

    @unittest.skipUnless(REAL_CONFIG.exists(), "no real config.json on this PC (v1 never saved settings)")
    def test_temp_copy_of_the_real_file(self):
        from xyrus.config import Config
        copy = self.tmp / "config.json"
        shutil.copyfile(REAL_CONFIG, copy)
        before = json.loads(REAL_CONFIG.read_text(encoding="utf8"))
        cfg = Config(copy).load()
        if "version" not in before:
            self.assertTrue(cfg.migrate_file())
        for key in ("voice_replies", "confirm_shutdown"):
            if key in before:
                self.assertEqual(cfg.get(key), before[key])
        for name, target in (before.get("apps") or {}).items():
            self.assertEqual(cfg.get("apps")[name], target)

    def test_app_startup_migrates(self):
        from xyrus.app import App
        self.v1_file()
        with mock.patch.dict(os.environ, {"XYRUS_DATA_DIR": str(self.tmp)}):
            app = App(sandbox=True)
            try:
                self.assertEqual(json.loads((self.tmp / "config.json").read_text(encoding="utf8"))["version"], 2)
                self.assertTrue((self.tmp / "config.v1.json").exists())
                self.assertIs(app.config.get("voice_replies"), False)
            finally:
                app.shutdown()


# ============================================================================ the App in this process
@unittest.skipUnless(HAVE_MODEL, "speech model missing")
class AppInProcess(unittest.TestCase):
    """Full App (sandbox) + real XyrusWindow on this thread; steps run from Tk after() callbacks."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="xyrus_app_"))
        self._env = mock.patch.dict(os.environ, {"XYRUS_DATA_DIR": str(self.tmp), "XYRUS_NO_HEARING": "1"})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_full_app_round_trip(self):
        from xyrus.app import App, NullCapture
        before = set(threading.enumerate())
        results: list[tuple[str, bool, str]] = []
        with mock.patch("xyrus.winutil.force_foreground", lambda hwnd: False):   # never tap ALT in a test
            app = App(sandbox=True, start_hidden=True)
            self.assertIsInstance(app.capture, NullCapture)
            for attr in ("config", "clock", "registry", "engine", "store", "memory", "speaker", "chime", "capture",
                         "recognizer", "vocab", "hotkeys", "tray", "notifier", "show_event", "ui_queue",
                         "app_index"):
                self.assertTrue(hasattr(app, attr), attr)
            app.start()
            said = app.speaker.said

            def said_since(n, prefix):
                return lambda: any(s.startswith(prefix) for s in said[n:])

            def step(name, action, cond, timeout=8.0):
                return name, action, cond, timeout

            n = {"said": 0}

            def mark():
                n["said"] = len(said)

            steps = [
                step("mic listening", None, lambda: app.engine.snapshot().status == "listening"),
                step("startup greeting", None, lambda: any("online" in s for s in said)),
                step("what time is it", lambda: (mark(), app.engine.submit_text("what time is it")),
                     lambda: said_since(n["said"], "It's")()),
                step("play alone", lambda: (mark(), app.engine.submit_text("play alone")),
                     lambda: said_since(n["said"], "Playing")()),
                step("not that one", lambda: (mark(), app.engine.submit_text("not that one")),
                     lambda: said_since(n["said"], "Trying")()),
                step("timer", lambda: (mark(), app.engine.submit_text("set a tea timer for three minutes")),
                     lambda: app.engine.snapshot().timers and app.window.side_lbl.cget("text").startswith("timer")),
                step("show:Commands", lambda: app.ui_queue.put("show:Commands"),
                     lambda: app.window.tab_ready("Commands")),
                step("theme light", lambda: app.ui_queue.put("theme:light"),
                     lambda: app.window.appearance == "light" and app.config.get("ui.appearance") == "light"),
                step("theme dark", lambda: app.ui_queue.put("theme:dark"), lambda: app.window.appearance == "dark"),
                step("pause", lambda: app.set_paused(True),
                     lambda: app.window.status_text() == "Paused · mic off" and app.capture.status() == "paused"),
                step("resume", lambda: app.set_paused(False),
                     lambda: app.capture.status() == "listening" and app.window.status_text() != "Paused · mic off"),
                step("stop hotkey", lambda: (setattr(app, "_stops0", app.speaker.stops),
                                             app._on_hotkey("stop_speaking")),
                     lambda: app.speaker.stops > app._stops0),
                step("show hotkey", lambda: app._on_hotkey("show_window"), lambda: app.window.state() == "normal"),
                step("hide", lambda: app.ui_queue.put("hide"), lambda: app.window.state() == "withdrawn"),
            ]

            def driver(win):
                it = iter(steps)
                state = {"cur": None, "deadline": 0.0}

                def tick():
                    cur = state["cur"]
                    if cur is not None:
                        name, _, cond, _ = cur
                        ok = False
                        try:
                            ok = bool(cond())
                        except Exception as e:
                            ok = False
                            state["err"] = repr(e)
                        if not ok and time.monotonic() < state["deadline"]:
                            win.after(50, tick)
                            return
                        results.append((name, ok, state.pop("err", "") if not ok else ""))
                    nxt = next(it, None)
                    if nxt is None:
                        threading.Thread(target=app.quit, name="test-quit").start()   # quit() from another thread
                        return
                    state["cur"] = nxt
                    state["deadline"] = time.monotonic() + nxt[3]
                    if nxt[1] is not None:
                        try:
                            nxt[1]()
                        except Exception as e:
                            state["err"] = repr(e)
                    win.after(50, tick)

                win.after(100, tick)
                win.after(90_000, lambda: app.ui_queue.put("quit"))          # never hang the suite

            app.run(on_ready=driver)
            window = app.window
            app.shutdown()

        failed = [f"{name} {err}" for name, ok, err in results if not ok]
        self.assertFalse(failed, f"failed steps: {failed}; said={said[-8:]}")
        self.assertEqual(len(results), len(steps), results)
        try:
            exists = bool(window.winfo_exists())
        except Exception:
            exists = False
        self.assertFalse(exists, "window still exists after quit")
        self.assertEqual([v for _, v in windows_of(os.getpid()) if v], [], "a visible Xyrus window leaked")
        leaked = []
        for t in set(threading.enumerate()) - before:
            t.join(3.0)
            if t.is_alive():
                leaked.append(t.name)
        self.assertEqual(leaked, [], "threads still running after shutdown")


# ============================================================================ subprocesses
class Subprocesses(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="xyrus_proc_"))
        self.procs: list[subprocess.Popen] = []

    def tearDown(self):
        for p in self.procs:                                    # kill only what this test started
            if p.poll() is None:
                p.kill()
                p.wait(10)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_text(self, args, stdin: str) -> subprocess.CompletedProcess:
        return subprocess.run([PY, *args], input=stdin, capture_output=True, text=True, encoding="utf-8",
                              timeout=90, cwd=str(BASE), env=child_env(self.tmp), creationflags=NO_WINDOW)

    def test_text_mode_prints_replies(self):
        r = self.run_text(["-m", "xyrus.app", "--text", "--dry-run"],
                          "what time is it\nxyrus set a timer for five minutes\nwhat can you do\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = r.stdout.splitlines()
        self.assertTrue(any(line.startswith("It's") for line in lines), r.stdout)
        self.assertIn("Timer set for five minutes, sir.", lines)
        self.assertIn("  [window: show:Commands]", lines)
        self.assertTrue((self.tmp / "arc.log").exists(), "logs go to XYRUS_DATA_DIR in tests")

    @unittest.skipUnless(SHIM.exists(), "v2 arc.py shim not written yet")
    def test_arc_shim(self):
        code = ("import runpy, sys; sys.argv = ['arc.py', '--text', '--dry-run']; "
                f"runpy.run_path(r'{SHIM}', run_name='__main__')")
        r = self.run_text(["-c", code], "what time is it\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(any(line.startswith("It's") for line in r.stdout.splitlines()), r.stdout)

    @unittest.skipUnless(HAVE_MODEL, "speech model missing")
    def test_selftest_exits_zero(self):
        r = subprocess.run([PY, "-m", "xyrus.app", "--selftest"], capture_output=True, text=True, encoding="utf-8",
                           timeout=180, cwd=str(BASE), env=child_env(self.tmp), creationflags=NO_WINDOW)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("self-test passed", r.stdout)

    @unittest.skipUnless(HAVE_MODEL, "speech model missing")
    def test_second_instance_shows_the_first(self):
        tag = f"test-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        env = child_env(self.tmp, XYRUS_INSTANCE=tag)
        first = subprocess.Popen([PY, "-m", "xyrus.app", "--tray", "--sandbox"], cwd=str(BASE), env=env,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
        self.procs.append(first)
        log_file = self.tmp / "arc.log"

        def logged(text):
            return log_file.exists() and text in log_file.read_text(encoding="utf8", errors="replace")

        self.assertTrue(wait_until(lambda: logged("microphone: listening"), 15),
                        f"no 'listening' in the log; rc={first.poll()}")
        self.assertTrue(wait_until(lambda: windows_of_tree(first.pid), 15), "the first instance built no window")
        self.assertFalse(any(v for _, v in windows_of_tree(first.pid)), "--tray must start hidden")
        t0 = time.monotonic()
        second = subprocess.run([PY, "-m", "xyrus.app"], cwd=str(BASE), env=env, capture_output=True, timeout=30,
                                creationflags=NO_WINDOW)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertLess(time.monotonic() - t0, 5.0 + 5.0, "the second instance took too long")  # incl. imports
        self.assertTrue(wait_until(lambda: any(v for _, v in windows_of_tree(first.pid)), 5),
                        "the first instance's window did not become visible")
        self.assertTrue(logged("already running"), "the second instance did not report the running one")
        self.assertIsNone(first.poll(), "the first instance died")


if __name__ == "__main__":
    unittest.main()
