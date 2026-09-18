"""T5 shell tests: single instance (two processes), global hotkeys (real injection + conflict),
autostart shortcut (temp folder only), tray images/menu/notifier.

Run from D:\\arc:  venv\\Scripts\\python.exe -m unittest tests.test_shell -v
Never touches the live app's mutex/event names, the real Startup folder or real data files.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
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
from types import SimpleNamespace

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from xyrus import autostart, hotkeys, single_instance as si, tray  # noqa: E402

NO_WINDOW = 0x08000000
PY = sys.executable
u32 = ctypes.windll.user32


def _names() -> tuple[str, str]:
    tag = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
    return f"Local\\XyrusTestMutex-{tag}", f"Local\\XyrusTestShow-{tag}"


FIRST = r'''
import ctypes, sys, time
sys.path.insert(0, sys.argv[1])
from xyrus import single_instance as si
import tkinter as tk
mutex, event = sys.argv[2], sys.argv[3]
if not si.acquire(mutex):
    print("not-first", flush=True); sys.exit(3)
ev = si.ShowEvent(event)
root = tk.Tk(); root.title("XyrusSingleInstanceTest"); root.geometry("220x60+30+30"); root.withdraw()
print("ready", flush=True)
deadline = time.time() + 12
def poll():
    if ev.poll():
        root.deiconify(); root.update()
        hwnd = int(root.frame(), 16)
        print("shown", int(bool(ctypes.windll.user32.IsWindowVisible(hwnd))), flush=True)
        root.after(200, root.destroy); return
    if time.time() > deadline:
        print("timeout", flush=True); root.destroy(); return
    root.after(150, poll)
root.after(150, poll)
root.mainloop()
'''

SECOND = r'''
import sys
sys.path.insert(0, sys.argv[1])
from xyrus import single_instance as si
if si.acquire(sys.argv[2]):
    print("became-first", flush=True); sys.exit(3)
print("signalled" if si.signal_existing(sys.argv[3]) else "nobody", flush=True)
sys.exit(0)
'''


class SingleInstanceTests(unittest.TestCase):
    def test_acquire_is_exclusive_and_releasable(self):
        mutex, _ = _names()
        self.assertTrue(si.acquire(mutex))
        self.assertTrue(si.acquire(mutex), "same process asking again keeps it")
        # a second handle to the same name reports ERROR_ALREADY_EXISTS
        h = si._k32.CreateMutexW(None, False, mutex)
        self.assertEqual(ctypes.get_last_error(), si.ERROR_ALREADY_EXISTS)
        si._k32.CloseHandle(h)
        si.release(mutex)
        self.assertTrue(si.acquire(mutex), "free again after release")
        si.release(mutex)

    def test_signal_without_first_instance(self):
        _, event = _names()
        self.assertFalse(si.signal_existing(event))

    def test_show_event_poll_is_auto_reset(self):
        _, event = _names()
        ev = si.ShowEvent(event)
        try:
            self.assertFalse(ev.poll())
            self.assertTrue(si.signal_existing(event))
            self.assertTrue(ev.poll())
            self.assertFalse(ev.poll(), "auto-reset: one signal = one show")
        finally:
            ev.close()

    def test_default_names_are_the_live_apps(self):
        self.assertEqual(si.MUTEX_NAME, "Local\\XyrusVoiceAssistant")
        self.assertEqual(si.EVENT_NAME, "Local\\XyrusShowWindow")

    def test_two_processes_second_shows_first(self):
        mutex, event = _names()
        first = subprocess.Popen([PY, "-c", FIRST, str(BASE), mutex, event], stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True, creationflags=NO_WINDOW)
        killer = threading.Timer(20, first.kill)
        killer.start()
        try:
            self.assertEqual(first.stdout.readline().strip(), "ready")
            t0 = time.monotonic()
            second = subprocess.run([PY, "-c", SECOND, str(BASE), mutex, event], capture_output=True,
                                    text=True, timeout=15, creationflags=NO_WINDOW)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(second.stdout.strip(), "signalled")
            out, err = first.communicate(timeout=15)
            self.assertIn("shown 1", out, f"first instance output: {out!r} {err!r}")
            self.assertLess(time.monotonic() - t0, 8)
        finally:
            killer.cancel()
            if first.poll() is None:
                first.kill()


# ------------------------------------------------------------------ hotkeys ----
VK_CONTROL, VK_MENU, VK_X = 0x11, 0x12, 0x58


def inject(*vks):
    """keybd_event with real scan codes (F3/F12), pressed in order, released in reverse."""
    for v in vks:
        u32.keybd_event(v, u32.MapVirtualKeyW(v, 0), 0, 0)
    time.sleep(0.03)
    for v in reversed(vks):
        u32.keybd_event(v, u32.MapVirtualKeyW(v, 0), 2, 0)


class Blocker(threading.Thread):
    """Another 'program' holding a combo: registers it on its own thread until released."""

    def __init__(self, combo: str):
        super().__init__(daemon=True)
        self.mods, self.vk = hotkeys.parse(combo)
        self.ok = False
        self.ready = threading.Event()
        self.release = threading.Event()

    def run(self):
        self.ok = bool(hotkeys._u32.RegisterHotKey(None, 0xB0F0, self.mods | hotkeys.MOD_NOREPEAT, self.vk))
        self.ready.set()
        self.release.wait(10)
        if self.ok:
            hotkeys._u32.UnregisterHotKey(None, 0xB0F0)


class HotkeyTests(unittest.TestCase):
    def test_parse(self):
        M = hotkeys
        self.assertEqual(M.parse("ctrl+alt+x"), (M.MOD_CONTROL | M.MOD_ALT, 0x58))
        self.assertEqual(M.parse("Ctrl+Alt+Space"), (M.MOD_CONTROL | M.MOD_ALT, 0x20))
        self.assertEqual(M.parse("ctrl+shift+f9"), (M.MOD_CONTROL | M.MOD_SHIFT, 0x78))
        self.assertEqual(M.parse("ctrl+alt+s")[1], ord("S"))
        with self.assertRaises(ValueError):
            M.parse("ctrl+alt")
        with self.assertRaises(ValueError):
            M.parse("ctrl+banana")
        self.assertEqual(M.DEFAULT_BINDINGS, {"show_window": "ctrl+alt+x", "push_to_talk": "ctrl+alt+space",
                                              "stop_speaking": "ctrl+alt+s"})

    def test_injected_ctrl_alt_x_fires_show_window(self):
        got, hit = [], threading.Event()

        def on_hotkey(name):
            got.append((name, time.monotonic(), threading.current_thread().name))
            hit.set()

        hk = hotkeys.Hotkeys({"show_window": "ctrl+alt+x"}, on_hotkey)
        hk.start()
        try:
            if not hk.status().get("show_window"):
                self.skipTest("Ctrl+Alt+X is registered by another program on this PC")   # never inject then
            # Safety (lead): inject only right after RegisterHotKey succeeded, so the system consumes the chord.
            # Alt goes down before Ctrl so the foreground app never sees a lone Alt tap (menu-bar activation).
            t0 = time.monotonic()
            inject(VK_MENU, VK_CONTROL, VK_X)
            self.assertTrue(hit.wait(1.0), "no WM_HOTKEY within 1 s")
            name, t, thread = got[0]
            self.assertEqual(name, "show_window")
            self.assertLess(t - t0, 1.0)
            self.assertEqual(thread, "T-hotkey")
        finally:
            hk.stop()
        self.assertEqual(hk.status(), {"show_window": False}, "stop() unregisters")
        # really unregistered: another thread can take the combo now
        b = Blocker("ctrl+alt+x")
        b.start()
        b.ready.wait(2)
        b.release.set()
        b.join(2)
        self.assertTrue(b.ok, "combo still held after stop()")

    def test_conflict_reported(self):
        b = Blocker("ctrl+alt+s")
        b.start()
        self.assertTrue(b.ready.wait(2))
        if not b.ok:
            b.release.set()
            self.skipTest("Ctrl+Alt+S is registered by another program on this PC")
        hk = hotkeys.Hotkeys({"stop_speaking": "ctrl+alt+s", "show_window": "ctrl+alt+x"}, lambda n: None)
        try:
            hk.start()
            st = hk.status()
            self.assertIs(st["stop_speaking"], False, "conflict must be reported")
            self.assertIn("show_window", st)
        finally:
            hk.stop()
            b.release.set()
            b.join(2)
        hk2 = hotkeys.Hotkeys({"stop_speaking": "ctrl+alt+s"}, lambda n: None)
        try:
            hk2.start()
            self.assertEqual(hk2.status(), {"stop_speaking": True}, "free once the other holder let go")
        finally:
            hk2.stop()

    def test_bad_binding_is_a_conflict_not_a_crash(self):
        hk = hotkeys.Hotkeys({"push_to_talk": "ctrl+alt+nokey"}, lambda n: None)
        try:
            hk.start()
            self.assertEqual(hk.status(), {"push_to_talk": False})
        finally:
            hk.stop()


# ---------------------------------------------------------------- autostart ----
class AutostartTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="xyrus_startup_"))
        self.real = autostart.shortcut_path()
        self.real_before = self.real.stat().st_mtime if self.real.exists() else None

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        real_after = self.real.stat().st_mtime if self.real.exists() else None
        self.assertEqual(self.real_before, real_after, "the real Startup shortcut was touched")

    def test_create_check_remove(self):
        lnk = self.tmp / "Xyrus.lnk"
        self.assertFalse(autostart.enabled(self.tmp))
        self.assertFalse(autostart.target_ok(self.tmp))
        autostart.set_enabled(True, self.tmp)
        self.assertTrue(lnk.exists())
        self.assertTrue(autostart.enabled(self.tmp))
        self.assertTrue(autostart.target_ok(self.tmp))
        info = autostart.read_shortcut(lnk)
        self.assertTrue(info["target"].lower().endswith("pythonw.exe"), info)
        self.assertIn(f'"{BASE / "arc.py"}"', info["arguments"])
        self.assertIn("--tray", info["arguments"])
        self.assertEqual(os.path.normcase(info["workdir"]), os.path.normcase(str(BASE)))
        autostart.set_enabled(False, self.tmp)
        self.assertFalse(lnk.exists())
        self.assertFalse(autostart.enabled(self.tmp))
        autostart.set_enabled(False, self.tmp)          # idempotent

    def test_stale_shortcut_detected_and_repaired(self):
        lnk = self.tmp / "Xyrus.lnk"
        autostart._write_lnk(lnk, '"C:\\SomewhereElse\\arc.py" --tray')
        self.assertTrue(autostart.enabled(self.tmp))
        self.assertFalse(autostart.target_ok(self.tmp), "points at another folder")
        autostart._write_lnk(lnk, f'"{BASE / "arc.py"}"')
        self.assertFalse(autostart.target_ok(self.tmp), "missing --tray")
        autostart.set_enabled(True, self.tmp)
        self.assertTrue(autostart.target_ok(self.tmp))

    def test_desktop_shortcut_opens_window(self):
        made = autostart.create_desktop_shortcut([self.tmp])
        self.assertEqual(made, [self.tmp / "Xyrus.lnk"])
        info = autostart.read_shortcut(made[0])
        self.assertIn("arc.py", info["arguments"])
        self.assertNotIn("--tray", info["arguments"])


# --------------------------------------------------------------------- tray ----
class FakeIcon:
    def __init__(self):
        self.icon = None
        self.title = ""
        self.notes = []

    def notify(self, body, title):
        self.notes.append((title, body))


class TrayTests(unittest.TestCase):
    def test_state_images_render(self):
        imgs = {s: tray.make_icon_image(s) for s in tray.STATES}
        for s, im in imgs.items():
            self.assertEqual(im.size, (64, 64), s)
            self.assertEqual(im.mode, "RGBA", s)
            lo, hi = im.convert("L").getextrema()
            self.assertGreater(hi - lo, 50, f"{s} image is flat")
        px = lambda s, xy: imgs[s].getpixel(xy)[:3]
        self.assertEqual(px("listening", (12, 32)), (124, 92, 255))
        self.assertEqual(px("paused", (12, 32)), (110, 110, 120))
        self.assertEqual(px("error", (12, 32)), (255, 92, 92))
        self.assertEqual(px("active", (3, 32)), (61, 220, 132), "active has the green ring")
        self.assertEqual(px("active", (14, 32)), (124, 92, 255))
        self.assertEqual(len({imgs[s].tobytes() for s in tray.STATES}), 4, "four distinct images")
        self.assertEqual(tray.make_icon_image("paused", 16).size, (16, 16))
        out = Path(tempfile.mkdtemp(prefix="xyrus_tray_"))
        try:
            for s, im in imgs.items():
                im.save(out / f"tray_{s}.png")
            shot_dir = os.environ.get("XYRUS_SHOT_DIR")
            if shot_dir:
                Path(shot_dir).mkdir(parents=True, exist_ok=True)
                for s, im in imgs.items():
                    im.resize((128, 128)).save(Path(shot_dir) / f"tray_{s}.png")
        finally:
            shutil.rmtree(out, ignore_errors=True)

    def test_ensure_icon_file(self):
        out = Path(tempfile.mkdtemp(prefix="xyrus_ico_"))
        try:
            ico = tray.ensure_icon_file(out / "xyrus.ico")
            self.assertTrue(ico.exists())
            from PIL import Image
            with Image.open(ico) as im:
                sizes = set(im.info.get("sizes", ()))
            self.assertTrue({(16, 16), (32, 32), (48, 48), (64, 64)} <= sizes, sizes)
            before = ico.stat().st_mtime
            tray.ensure_icon_file(ico)
            self.assertEqual(ico.stat().st_mtime, before, "existing icon kept")
        finally:
            shutil.rmtree(out, ignore_errors=True)

    def _app(self):
        cfg = {"voice_replies": True}
        app = SimpleNamespace(ui_queue=queue.Queue(), paused_calls=[])
        app.config = SimpleNamespace(get=lambda k, d=None: cfg.get(k, d), set=lambda k, v: cfg.__setitem__(k, v))
        app.engine = SimpleNamespace(snapshot=lambda: SimpleNamespace(paused=False))
        app.set_paused = app.paused_calls.append
        app._cfg = cfg
        return app

    def test_notifier_queues_until_ready(self):
        t = tray.Tray(self._app())
        t.notify("Xyrus", "early toast")                    # icon not created yet -> queued
        self.assertEqual(t._pending, [("Xyrus", "early toast")])
        t._icon = FakeIcon()
        t._ready_at = time.monotonic() - 0.1
        t._flush()
        self.assertEqual(t._icon.notes, [("Xyrus", "early toast")])
        t.notify("Xyrus", "later")
        self.assertEqual(t._icon.notes[-1], ("Xyrus", "later"))
        t._icon.notify = lambda *a: (_ for _ in ()).throw(RuntimeError("boom"))
        t.notify("Xyrus", "never raises")                   # swallowed + logged
        t.set_state("active")
        self.assertIs(t._icon.icon, t._images["active"])
        t.set_state("bogus")
        self.assertEqual(t.state, "active")
        t.set_tooltip("Xyrus — next: " + "x" * 300)
        self.assertEqual(len(t._icon.title), tray.TOOLTIP_MAX)

    def test_real_icon_menu(self):
        app = self._app()
        t = tray.Tray(app)
        t.start()
        try:
            time.sleep(0.3)
            items = list(t._icon.menu.items)
            texts = [i.text for i in items]
            self.assertEqual(texts[0], "Open Xyrus")
            self.assertEqual(texts[1], "Pause listening")
            self.assertEqual(texts[2], "Mute replies")
            self.assertEqual(texts[3], "Start with Windows")
            self.assertEqual(texts[-1], "Quit Xyrus")
            self.assertTrue(items[0].default)
            items[0](t._icon)
            self.assertEqual(app.ui_queue.get_nowait(), "show")
            items[1](t._icon)
            self.assertEqual(app.paused_calls, [True])
            items[2](t._icon)
            self.assertIs(app._cfg["voice_replies"], False)
            self.assertTrue(items[2].checked)
            items[-1](t._icon)
            self.assertEqual(app.ui_queue.get_nowait(), "quit")
            for s in ("active", "paused", "error", "listening"):
                t.set_state(s)
                self.assertIs(t._icon.icon, t._images[s])
            t.set_tooltip("Xyrus — next: dentist at 3 PM")
            self.assertEqual(t._icon.title, "Xyrus — next: dentist at 3 PM")
        finally:
            t.stop()


if __name__ == "__main__":
    unittest.main()
