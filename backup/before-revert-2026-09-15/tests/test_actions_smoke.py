"""T4 — actions unit tests (always) + the safe live smoke test of §7.5 (XYRUS_LIVE=1 only).

Live safety rules: never shutdown/restart/sleep/hibernate/lock/sign out/screen off/kill/theme/abort a
shutdown (a guard around WinActions raises on those names); everything changed (volume, mute, brightness,
clipboard, keep-awake) is read first and restored in `finally`; the only window touched is a Notepad this
test launches itself on a temp file (skipped if any Notepad window already exists), closed by PID.
Run:  set XYRUS_LIVE=1 && venv\\Scripts\\python.exe -m unittest tests.test_actions_smoke -v
"""
from __future__ import annotations

import os
import socket
import subprocess
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

if not os.environ.get("XYRUS_DATA_DIR"):
    os.environ["XYRUS_DATA_DIR"] = tempfile.mkdtemp(prefix="xyrus_test_")

from xyrus import winutil
from xyrus.actions import WinActions
from xyrus.actions import apps, keys, web, windows

LIVE = bool(os.environ.get("XYRUS_LIVE"))
# Lead rule (2026-09-13): no live test may inject keys/typing/clicks or change window focus on the user's PC.
# The Notepad part of §7.5 runs only with XYRUS_LIVE_INPUT=1, which nobody sets without the user's OK.
LIVE_INPUT = LIVE and bool(os.environ.get("XYRUS_LIVE_INPUT"))
FORBIDDEN = frozenset({"shutdown", "restart", "sleep", "hibernate", "lock", "sign_out", "screen_off",
                       "kill_app", "set_theme", "abort_shutdown", "run_command", "open_target"})


def online() -> bool:
    try:
        socket.create_connection(("1.1.1.1", 53), 1.5).close()
        return True
    except OSError:
        return False


class _LASTINPUTINFO(__import__("ctypes").Structure):
    _fields_ = [("cbSize", __import__("ctypes").c_uint), ("dwTime", __import__("ctypes").c_uint)]


def _last_input_tick() -> int:
    import ctypes
    lii = _LASTINPUTINFO()
    lii.cbSize = ctypes.sizeof(_LASTINPUTINFO)
    ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii))
    return lii.dwTime


def _idle_s() -> float:
    import ctypes
    return ((ctypes.windll.kernel32.GetTickCount() - _last_input_tick()) & 0xFFFFFFFF) / 1000.0


def _wait_idle(need_s: float, max_wait_s: float) -> bool:
    deadline = time.monotonic() + max_wait_s
    while time.monotonic() < deadline:
        if _idle_s() >= need_s:
            return True
        time.sleep(0.25)
    return False


class _InputGuard:
    """Sends one injected input at a time. Before each: our window must be foreground and there must be no
    input since our previous injection (i.e. the user touched nothing); otherwise the test is skipped."""

    def __init__(self, test: unittest.TestCase, hwnd: int):
        self.test, self.hwnd, self.last = test, hwnd, _last_input_tick()

    def send(self, fn) -> None:
        if _last_input_tick() != self.last:
            self.test.skipTest("user input detected during the key test; stopped injecting")
        if windows.foreground_hwnd() != self.hwnd:
            self.test.skipTest("notepad lost the foreground; stopped injecting")
        fn()
        time.sleep(0.12)
        self.last = _last_input_tick()


class GuardedActions:
    """Wraps WinActions; any forbidden method raises before it can run."""

    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        if name in FORBIDDEN:
            raise AssertionError(f"smoke test must never call {name}()")
        return getattr(self._inner, name)


# ======================================================================= unit tests (no side effects)
class TestKeysParse(unittest.TestCase):
    def test_chords(self):
        self.assertEqual(keys.parse_chord("ctrl+shift+t"), [0x11, 0x10, 0x54])
        self.assertEqual(keys.parse_chord("enter"), [0x0D])
        self.assertEqual(keys.parse_chord("F11"), [0x7A])
        self.assertEqual(keys.parse_chord("alt + f4"), [0x12, 0x73])
        self.assertEqual(keys.parse_chord("win+ctrl+d"), [0x5B, 0x11, 0x44])
        self.assertEqual(keys.parse_chord("page up"), [0x21])
        self.assertEqual(keys.parse_chord("esc"), keys.parse_chord("escape"))
        self.assertEqual(keys.parse_chord("ctrl+0"), [0x11, 0x30])

    def test_every_documented_name(self):
        for name in ("enter tab escape esc space up down left right home end delete insert pageup pagedown "
                     "ctrl alt shift win").split() + [f"f{i}" for i in range(1, 13)] + list("az09"):
            self.assertEqual(len(keys.parse_chord(name)), 1, name)

    def test_bad(self):
        for bad in ("", "ctrl+banana", "hyper"):
            with self.assertRaises(ValueError):
                keys.parse_chord(bad)


class TestWebPure(unittest.TestCase):
    def test_spoken_title(self):
        self.assertEqual(web.spoken_title("Alan Walker - Alone (Official Music Video)"), "Alone by Alan Walker")
        self.assertEqual(web.spoken_title("Ed Sheeran - Perfect [Official Video] | 4K"), "Perfect by Ed Sheeran")
        self.assertEqual(web.spoken_title("Blinding Lights"), "Blinding Lights")
        self.assertLessEqual(len(web.spoken_title(" ".join(["word"] * 30)).split()), 10)

    def test_urls(self):
        self.assertEqual(web.youtube_search_url("shape of you"),
                         "https://www.youtube.com/results?search_query=shape+of+you")
        self.assertEqual(web.google_url("cheap keyboards & mice"),
                         "https://www.google.com/search?q=cheap+keyboards+%26+mice")
        self.assertEqual(web.spotify_search_uri("see you again"), "spotify:search:see%20you%20again")

    def test_secs(self):
        self.assertEqual(web._secs("3:41"), 221)
        self.assertEqual(web._secs("1:02:03"), 3723)
        self.assertIsNone(web._secs(None))
        self.assertIsNone(web._secs("LIVE"))

    def _fake_page(self, html):
        class R:
            def __init__(s, b): s.b = b
            def read(s): return s.b
            def __enter__(s): return s
            def __exit__(s, *a): return False
        return mock.patch("urllib.request.urlopen", lambda req, timeout=0: R(html.encode()))

    def test_search_parses_both_page_variants_and_skips_long_videos(self):
        data = ('{"contents": [{"videoRenderer": {"videoId": "AAAAAAAAAAA", "title": {"runs": [{"text": "Mix 1h"}]},'
                ' "ownerText": {"runs": [{"text": "X"}]}, "lengthText": {"simpleText": "1:00:00"}}},'
                ' {"videoRenderer": {"videoId": "BBBBBBBBBBB", "title": {"runs": [{"text": "Alan Walker - Alone"}]},'
                ' "ownerText": {"runs": [{"text": "Alan Walker"}]}, "lengthText": {"simpleText": "2:41"}}}]}')
        for prefix in ("var ytInitialData = ", 'window["ytInitialData"] = '):
            with self._fake_page(f"<script>{prefix}{data};</script>"):
                res = web.youtube_search("alone")
                self.assertEqual([r[0] for r in res], ["AAAAAAAAAAA", "BBBBBBBBBBB"])
                self.assertEqual(res[1][3], 161)
                self.assertEqual(web.find_song("alone"),       # (spoken, url, video title) — v1 round-2 shape
                                 ("Alone by Alan Walker", "https://www.youtube.com/watch?v=BBBBBBBBBBB",
                                  "Alan Walker - Alone"))

    def test_fallback_ids_and_no_results(self):
        with self._fake_page('junk "videoId":"CCCCCCCCCCC" junk "videoId":"CCCCCCCCCCC"'), \
                mock.patch.object(web, "video_title", return_value="Alan Walker - Alone"):
            self.assertEqual(web.youtube_search("x"), [("CCCCCCCCCCC", "", "", None)])
            self.assertEqual(web.find_song("alone")[0], "Alone by Alan Walker")
        with self._fake_page("<html>nothing</html>"):
            with self.assertRaises(LookupError):
                web.find_song("zzzz")

    def test_weather_parse_and_cache(self):
        body = ('{"current_condition":[{"temp_C":"28","FeelsLikeC":"31","weatherDesc":[{"value":"Clear "}]}],'
                '"weather":[{"maxtempC":"31","mintempC":"26","hourly":[{"chanceofrain":"10"},{"chanceofrain":"70"}]}],'
                '"nearest_area":[{"areaName":[{"value":"Dhaka"}]}]}')
        web._wcache.clear()
        with self._fake_page(body):
            w = web.weather("dhaka")
        self.assertEqual(w, {"city": "Dhaka", "temp_c": 28, "feels_c": 31, "desc": "Clear", "max_c": 31,
                             "min_c": 26, "rain_chance": 70})
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("cache miss")):
            self.assertEqual(web.weather("Dhaka")["temp_c"], 28)
        with self.assertRaises(LookupError):
            web.weather("  ")


class TestAppsPure(unittest.TestCase):
    def test_start_menu_index(self):
        d = Path(tempfile.mkdtemp(prefix="xyrus_sm_"))
        (d / "Sub").mkdir()
        for name in ("Visual Studio Code.lnk", "Sub/OBS Studio.lnk", "Uninstall Foo.lnk", "Foo Help.lnk",
                     "Spotify.lnk", "Notepad++.lnk"):
            (d / name).write_bytes(b"")
        idx = apps.StartMenuIndex([d])
        self.assertEqual(idx.refresh(), 4)
        self.assertEqual(idx.names(), ["notepad", "obs studio", "spotify", "visual studio code"])
        self.assertEqual(idx.lookup("visual studio code")[0], "visual studio code")
        self.assertEqual(idx.lookup("obs studios")[0], "obs studio")       # close match
        self.assertEqual(idx.lookup("visual studio")[0], "visual studio code")   # unique prefix
        self.assertTrue(idx.lookup("spotify")[1].endswith("Spotify.lnk"))
        self.assertIsNone(idx.lookup("kubernetes"))
        self.assertIsNone(idx.lookup(""))

    def test_open_target_routing(self):
        with mock.patch.object(winutil, "open_url") as ou, mock.patch.object(winutil, "popen") as po:
            apps.open_target("https://www.youtube.com/results?search_query=a&b=c")
            apps.open_target(r"C:\x\Spotify.lnk")
            apps.open_target("chrome")
            apps.open_target("ms-settings:bluetooth")
        self.assertEqual([c.args[0] for c in ou.call_args_list],
                         ["https://www.youtube.com/results?search_query=a&b=c", r"C:\x\Spotify.lnk"])
        self.assertEqual([c.args[0] for c in po.call_args_list],
                         [["cmd", "/c", "start", "", "chrome"], ["cmd", "/c", "start", "", "ms-settings:bluetooth"]])
        with self.assertRaises(ValueError):
            apps.open_target("  ")


class TestWindowsPure(unittest.TestCase):
    def test_alias_process(self):
        self.assertEqual(windows._alias_process("chrome"), "chrome")
        self.assertEqual(windows._alias_process("msedge"), "msedge")
        self.assertEqual(windows._alias_process("calc"), "calculatorapp")
        self.assertEqual(windows._alias_process("wt"), "windowsterminal")
        self.assertEqual(windows._alias_process("spotify:"), "spotify")
        self.assertEqual(windows._alias_process(r"C:\Apps\Foo Bar.exe"), "foo bar")
        self.assertIsNone(windows._alias_process("ms-settings:"))
        self.assertIsNone(windows._alias_process("https://www.youtube.com"))
        self.assertIsNone(windows._alias_process(None))

    def test_find_app_windows_matching(self):
        fake = [(1, "Untitled - Notepad", 11, "notepad"), (2, "YouTube - Google Chrome", 12, "chrome"),
                (3, "Visual Studio Code", 13, "code"), (4, "Calculator", 14, "calculatorapp")]
        with mock.patch.object(windows, "_user_windows", return_value=fake):
            self.assertEqual([w[0] for w in windows.find_app_windows("note pad")], [1])
            self.assertEqual([w[0] for w in windows.find_app_windows("chrome")], [2])
            self.assertEqual([w[0] for w in windows.find_app_windows("calculator", "calc")], [4])
            self.assertEqual([w[0] for w in windows.find_app_windows("youtube")], [2])      # title substring
            self.assertEqual(windows.find_app_windows("spotify"), [])
            self.assertEqual(windows.find_app_windows(""), [])

    def test_user_windows_excludes_shell_and_self(self):
        own = winutil.own_pids()
        fake = [(1, "Program Manager", 5, "explorer"), (2, "Xyrus", next(iter(own)), "pythonw"),
                (3, "Notepad", 7, "notepad"), (4, "x", 8, "applicationframehost"), (5, "y", 9, "textinputhost")]
        with mock.patch.object(windows, "list_windows", return_value=fake):
            self.assertEqual(windows.windowed_apps(), ["notepad"])

    def test_own_pids(self):
        self.assertIn(os.getpid(), winutil.own_pids())


class TestGuard(unittest.TestCase):
    def test_guard_blocks_dangerous_calls(self):
        g = GuardedActions(WinActions())
        for name in ("shutdown", "restart", "sleep", "hibernate", "lock", "sign_out", "screen_off", "kill_app",
                     "set_theme"):
            with self.assertRaises(AssertionError):
                getattr(g, name)
        self.assertTrue(callable(g.volume_get))

    def test_winactions_implements_system_actions(self):
        from xyrus import interfaces
        proto = [n for n, v in vars(interfaces.SystemActions).items() if callable(v) and not n.startswith("_")]
        self.assertGreater(len(proto), 40)
        missing = [n for n in proto if not callable(getattr(WinActions, n, None))]
        self.assertEqual(missing, [])


class FakeUser32:
    """Records keybd_event / SetForegroundWindow / SetWindowPos instead of touching the desktop."""

    def __init__(self, down=(), fg=100, set_fg_ok=True):
        self.events: list[tuple[int, int]] = []          # (vk, flags)
        self.down = set(down)
        self.fg = fg
        self.set_fg_ok = set_fg_ok
        self.set_fg_calls: list[int] = []
        self.swp: list[tuple] = []
        self.shown: list[tuple[int, int]] = []

    def keybd_event(self, vk, scan, flags, extra):
        self.events.append((vk, flags))

    def MapVirtualKeyW(self, vk, kind):
        return 0x1E

    def GetAsyncKeyState(self, vk):
        return -32768 if vk in self.down else 0

    def GetForegroundWindow(self):
        return self.fg

    def SetForegroundWindow(self, hwnd):
        self.set_fg_calls.append(hwnd)
        if self.set_fg_ok:
            self.fg = hwnd
        return int(self.set_fg_ok)

    def IsIconic(self, hwnd):
        return 0

    def IsZoomed(self, hwnd):
        return 0

    def ShowWindow(self, hwnd, cmd):
        self.shown.append((hwnd, cmd))
        return 1

    def GetWindowThreadProcessId(self, hwnd, p):
        return 7

    def AttachThreadInput(self, a, b, c):
        return 1

    def BringWindowToTop(self, hwnd):
        return 1

    def SetWindowPos(self, *args):
        self.swp.append(args)
        return 1


class TestKeysInjectionPatched(unittest.TestCase):
    """Key/typing code exercised with a fake user32 (no real input is ever injected)."""

    def test_chord_order_and_flags(self):
        fake = FakeUser32()
        with mock.patch.object(keys, "_user32", fake), mock.patch.object(keys.time, "sleep"):
            keys.chord("ctrl+shift+t")
        self.assertEqual(fake.events, [(0x11, 0), (0x10, 0), (0x54, 0), (0x54, 2), (0x10, 2), (0x11, 2)])

    def test_extended_keys(self):
        fake = FakeUser32()
        with mock.patch.object(keys, "_user32", fake), mock.patch.object(keys.time, "sleep"):
            keys.chord("win+ctrl+right")
        self.assertEqual(fake.events, [(0x5B, 1), (0x11, 0), (0x27, 1), (0x27, 3), (0x11, 2), (0x5B, 3)])

    def test_release_stuck_modifiers_first(self):
        fake = FakeUser32(down={0x10, 0xA0})
        with mock.patch.object(keys, "_user32", fake), mock.patch.object(keys.time, "sleep"):
            self.assertEqual(sorted(keys.release_stuck_modifiers()), [0x10, 0xA0])
            fake.events.clear()
            keys.chord("enter")
        self.assertEqual(fake.events[:2], [(0x10, 2), (0xA0, 2)])        # key-ups before the Enter tap
        self.assertEqual(fake.events[2:], [(0x0D, 0), (0x0D, 2)])

    def test_tap_repeat(self):
        fake = FakeUser32()
        with mock.patch.object(keys, "_user32", fake), mock.patch.object(keys.time, "sleep"):
            keys.tap(0xAF, times=3)
        self.assertEqual(fake.events, [(0xAF, 1), (0xAF, 3)] * 3)

    def test_type_text_uses_keyboard_write(self):
        import sys
        import types
        fake_kb = types.SimpleNamespace(calls=[])
        fake_kb.write = lambda text, delay=0: fake_kb.calls.append((text, delay))
        fake = FakeUser32()
        with mock.patch.dict(sys.modules, {"keyboard": fake_kb}), mock.patch.object(keys, "_user32", fake):
            keys.type_text("xyrus test")
        self.assertEqual(fake_kb.calls, [("xyrus test", 0.01)])

    def test_media_and_desktop_use_taps(self):
        from xyrus.actions import media
        fake = FakeUser32()
        with mock.patch.object(keys, "_user32", fake), mock.patch.object(keys.time, "sleep"):
            media.media("play_pause")
            windows.desktop("new")
            windows.show_desktop()
        self.assertEqual([vk for vk, fl in fake.events if not fl & 2], [0xB3, 0x5B, 0x11, 0x44, 0x5B, 0x44])
        with self.assertRaises(ValueError):
            media.media("rewind")
        with self.assertRaises(ValueError):
            windows.desktop("sideways")


class TestForegroundPatched(unittest.TestCase):
    def setUp(self):
        # tests/test_ui_render.py (T5) replaces winutil.force_foreground with a recorder at import time; put the
        # real implementation back for these tests only (mock.patch, restored by addCleanup).
        p = mock.patch.object(winutil, "force_foreground", winutil._force_foreground_impl)
        p.start()
        self.addCleanup(p.stop)

    def test_already_foreground_no_alt(self):
        fake = FakeUser32(fg=42)
        with mock.patch.object(winutil, "_user32", fake):
            self.assertTrue(winutil.force_foreground(42))
        self.assertEqual(fake.events, [])
        self.assertEqual(fake.set_fg_calls, [])

    def test_plain_set_foreground_first(self):
        fake = FakeUser32(fg=1)
        with mock.patch.object(winutil, "_user32", fake):
            self.assertTrue(winutil.force_foreground(42))
        self.assertEqual(fake.events, [])                   # no ALT tap when SetForegroundWindow works

    def test_alt_fallback(self):
        fake = FakeUser32(fg=1, set_fg_ok=False)
        with mock.patch.object(winutil, "_user32", fake):
            self.assertFalse(winutil.force_foreground(42))
        self.assertEqual(fake.events, [(0x12, 0), (0x12, 2)])
        self.assertGreaterEqual(len(fake.set_fg_calls), 2)

    def test_zero_hwnd(self):
        self.assertFalse(winutil.force_foreground(0))


class TestSnapPatched(unittest.TestCase):
    def test_snap_math_with_dwm_borders(self):
        fake = FakeUser32(fg=5)
        with mock.patch.object(windows, "_u", fake), \
                mock.patch.object(windows, "work_area", return_value=(0, 0, 2560, 1400)), \
                mock.patch.object(windows, "window_rect", return_value=(293, 200, 1207, 807)), \
                mock.patch.object(windows, "visible_rect", return_value=(300, 200, 1200, 800)):
            windows.snap("left")
            windows.snap("right", hwnd=9)
        (h1, _, x1, y1, w1, hgt1, _f1), (h2, _, x2, y2, w2, hgt2, _f2) = fake.swp
        self.assertEqual((h1, x1, y1, w1, hgt1), (5, -7, 0, 1280 + 14, 1400 + 7))
        self.assertEqual((h2, x2, w2), (9, 1280 - 7, 1294))
        with self.assertRaises(ValueError):
            windows.snap("up")

    def test_window_cmd(self):
        fake = FakeUser32(fg=5)
        with mock.patch.object(windows, "_u", fake), \
                mock.patch.object(winutil, "window_pid", return_value=-1):
            windows.window_cmd("minimize")
            windows.window_cmd("maximize")
            windows.window_cmd("restore")
            windows.window_cmd("topmost")
        self.assertEqual(fake.shown, [(5, 6), (5, 3), (5, 1)])
        self.assertEqual(fake.swp[0][1], -1)
        with self.assertRaises(ValueError):
            windows.window_cmd("explode")

    def test_close_refuses_own_window(self):
        fake = FakeUser32(fg=5)
        with mock.patch.object(windows, "_u", fake), \
                mock.patch.object(winutil, "window_pid", return_value=os.getpid()):
            with self.assertRaises(RuntimeError):
                windows.window_cmd("close")


# ======================================================================= live smoke test (§7.5)
@unittest.skipUnless(LIVE, "set XYRUS_LIVE=1 to run the live Windows actions smoke test")
class TestLiveSmoke(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.shots = Path(tempfile.mkdtemp(prefix="xyrus_shots_"))
        cls.a = GuardedActions(WinActions(screenshot_dir=cls.shots))

    def test_volume_roundtrip(self):
        a = self.a
        orig = a.volume_get()
        orig_mute = a.mute_get()
        try:
            a.volume_set(37)
            time.sleep(0.05)
            self.assertEqual(a.volume_get(), 37)
            self.assertEqual(a.volume_step(10), 47)
            self.assertEqual(a.volume_step(-20), 27)
            a.mute_set(True)
            self.assertTrue(a.mute_get())
            a.mute_set(False)
            self.assertFalse(a.mute_get())
        finally:
            a.volume_set(orig)
            a.mute_set(orig_mute)
        self.assertEqual(a.volume_get(), orig)
        self.assertEqual(a.mute_get(), orig_mute)

    def test_brightness_same_value(self):
        cur = self.a.brightness_get()
        if cur is None:
            self.skipTest("no brightness control on this display")
        try:
            self.assertEqual(self.a.brightness_set(cur), cur)
        finally:
            self.a.brightness_set(cur)

    def test_clipboard_unicode_roundtrip(self):
        a = self.a
        orig = a.clip_get()
        try:
            text = "xyrus clipboard test \u2713 \u00fc\u00f1\u00ed \u0986"
            a.clip_set(text)
            self.assertEqual(a.clip_get(), text)
            a.clip_clear()
            self.assertEqual(a.clip_get(), "")
        finally:
            a.clip_set(orig)
        self.assertEqual(a.clip_get(), orig)

    def test_keep_awake_toggle(self):
        from xyrus.actions import power
        was = power.keeping_awake()
        try:
            self.a.keep_awake(True)
            self.assertTrue(power.keeping_awake())
            self.a.keep_awake(False)
            self.assertFalse(power.keeping_awake())
        finally:
            self.a.keep_awake(was)

    def test_info(self):
        st = self.a.system_status()
        self.assertEqual(set(st), {"cpu", "mem", "battery", "uptime_s"})
        self.assertTrue(0 <= st["cpu"] <= 100 and 0 < st["mem"] <= 100 and st["uptime_s"] > 0)
        d = self.a.disks()
        self.assertTrue(d and d[0]["total_gb"] > 0 and set(d[0]) == {"drive", "free_gb", "total_gb", "pct"})
        self.assertIsInstance(self.a.top_processes(3), list)
        self.assertIsInstance(self.a.windowed_apps(), list)
        self.assertIn(self.a.foreground_is_self(), (True, False))
        from xyrus.actions import display
        self.assertIn(display.theme_get(), (True, False, None))       # read only; set_theme is forbidden
        self.assertTrue(apps.default_index().names())                   # Start Menu has shortcuts

    def test_screenshot(self):
        p = self.a.screenshot()
        self.assertTrue(p.exists() and p.stat().st_size > 1000 and p.parent == self.shots)
        self.assertEqual(self.a.last_screenshot(), p)

    @unittest.skipUnless(online(), "no network")
    def test_find_song_network(self):
        title, url, video = self.a.find_song("alone")
        self.assertIn("watch?v=", url)
        self.assertIn("lone", title.lower())
        self.assertTrue(video)                          # the raw video title ensure_playing looks for

    @unittest.skipUnless(LIVE_INPUT, "XYRUS_LIVE_INPUT=1 only, with the user's OK: launches Notepad, changes "
                                     "focus and injects keystrokes (lead rule: never on the user's live PC)")
    def test_notepad_windows_keys_clipboard(self):
        a = self.a
        if windows.find_app_windows("notepad"):
            self.skipTest("a Notepad window is already open — never touch the user's windows")
        tmp = Path(tempfile.gettempdir()) / f"xyrus_smoke_{uuid.uuid4().hex[:8]}.txt"
        tmp.write_text("hello", encoding="utf8")
        orig_clip = a.clip_get()
        hwnd = pid = None
        proc = subprocess.Popen(["notepad.exe", str(tmp)], creationflags=winutil.NO_WINDOW)
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and hwnd is None:
                for h, title, p, name in windows.list_windows():
                    if tmp.stem in title:
                        hwnd, pid = h, p
                        break
                time.sleep(0.2)
            self.assertIsNotNone(hwnd, "notepad window did not appear")
            time.sleep(0.8)
            self.assertIn("notepad", a.windowed_apps())
            self.assertTrue(a.focus_app("notepad"))
            time.sleep(0.3)
            if windows.foreground_hwnd() != hwnd:
                winutil.force_foreground(hwnd)
                time.sleep(0.3)
            self.assertEqual(windows.foreground_hwnd(), hwnd)

            for side in ("left", "right"):
                a.snap(side)
                time.sleep(0.4)
                l, t, r, b = windows.visible_rect(hwnd)
                wl, wt_, wr, wb = windows.work_area(hwnd)
                half = (wr - wl) // 2
                exp_l = wl if side == "left" else wl + half
                self.assertLessEqual(abs(l - exp_l), 12, (side, (l, t, r, b), (wl, wt_, wr, wb)))
                self.assertLessEqual(abs((r - l) - half), 16, (side, (l, t, r, b)))
                self.assertLessEqual(abs(t - wt_), 12)
                self.assertLessEqual(abs(b - wb), 12)

            a.window_cmd("maximize")
            time.sleep(0.4)
            self.assertTrue(windows._u.IsZoomed(hwnd))
            a.window_cmd("restore")
            time.sleep(0.4)
            self.assertFalse(windows._u.IsZoomed(hwnd))

            # keys + typing: only while the user is idle and OUR notepad is verifiably in front, re-checked
            # right before every single keystroke; any new user input aborts (never type into user windows).
            if windows.foreground_hwnd() != hwnd:
                winutil.force_foreground(hwnd)
                time.sleep(0.3)
            if not _wait_idle(3.0, 20.0):
                self.skipTest("the user is typing/using the mouse; key injection skipped for safety")
            guard = _InputGuard(self, hwnd)
            guard.send(lambda: a.keys("ctrl+a"))
            for ch in "xyrus test":
                guard.send(lambda ch=ch: a.type_text(ch))
            guard.send(lambda: a.keys("ctrl+a"))
            guard.send(lambda: a.keys("ctrl+c"))
            time.sleep(0.3)
            self.assertEqual(a.clip_get(), "xyrus test")

            self.assertTrue(a.close_app("notepad"))           # WM_CLOSE (a save prompt may appear)
            time.sleep(1.0)
        finally:
            try:
                if pid:
                    winutil.run(["taskkill", "/PID", str(pid), "/F"], timeout=10)
                proc.kill()
                proc.wait(5)
            except Exception:
                pass
            a.clip_set(orig_clip)
            time.sleep(0.3)
            try:
                tmp.unlink()
            except OSError:
                pass
        self.assertEqual(a.clip_get(), orig_clip)


if __name__ == "__main__":
    unittest.main()
