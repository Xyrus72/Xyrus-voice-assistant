"""T4 — every §3.1–3.7 and §3.10 row through the real engine (make_test_engine, FakeActions, FakeSpeaker),
the §7.2 cases E4, E10–E18, E20, E22, E25, and ≥ 8 paraphrases per common intent (lead requirement).

Run: venv\\Scripts\\python.exe -m unittest tests.test_commands -v
"""
from __future__ import annotations

import datetime as dt
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

if not os.environ.get("XYRUS_DATA_DIR"):
    os.environ["XYRUS_DATA_DIR"] = tempfile.mkdtemp(prefix="xyrus_test_")

from xyrus import normalize as N
from xyrus import replies as R
from xyrus.testing import FakeActions, drain_ui, make_test_engine

try:
    import xyrus.engine  # noqa: F401  (T1 M1)
    HAVE_ENGINE = True
except ImportError:
    HAVE_ENGINE = False

WATCH = "https://www.youtube.com/watch?v=4bt-z4gxKqI"


def _have(module: str) -> bool:
    try:
        __import__(module)
        return True
    except ImportError:
        return False


@unittest.skipUnless(HAVE_ENGINE, "xyrus.engine (T1 M1) not available yet")
class Base(unittest.TestCase):
    config: dict = {}

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="xyrus_cmd_")
        self.engine, self.ns = make_test_engine(tmpdir=Path(self.tmp), actions=self.make_actions(),
                                                config_overrides=dict(self.config))
        self.a = self.ns.actions

    def make_actions(self):
        return FakeActions(find_song=("Alone by Alan Walker", WATCH))

    def tearDown(self):
        try:
            self.ns.registry.unregister_custom()
        except Exception:
            pass

    # ---- helpers
    def typed(self, text):
        self.engine.handle(text, "typed")

    def mic(self, text, free_text=None):
        self.engine.handle(text, "mic", free_text=free_text)

    @property
    def said(self):
        return self.ns.speaker.said

    def last(self):
        return self.said[-1] if self.said else ""

    def calls(self, name):
        return self.a.called(name)

    def assertCalled(self, name, *args):
        got = self.calls(name)
        self.assertTrue(got, f"{name} not called; calls={self.a.names()} said={self.said}")
        if args:
            self.assertIn(tuple(args), got, f"{name} calls {got}")

    def assertNotCalled(self, name):
        self.assertFalse(self.calls(name), f"{name} unexpectedly called: {self.calls(name)}")

    def assertSaid(self, text):
        self.assertIn(text, self.said, f"said={self.said}")

    def run_case(self, text, call=None, args=None, reply=None):
        self.a.reset()
        n = len(self.said)
        self.typed(text)
        if call:
            if args is None:
                self.assertCalled(call)
            else:
                self.assertCalled(call, *args)
        if reply is not None:
            new = self.said[n:]
            self.assertTrue(any(reply == s or (reply.endswith("*") and s.startswith(reply[:-1])) for s in new),
                            f"{text!r}: expected reply {reply!r}, got {new}")


# ============================================================================ §7.2 engine cases
class TestE(Base):
    def test_E4_one_breath_open(self):
        self.mic("cyrus open chrome")
        self.assertCalled("open_target", "chrome")
        self.assertSaid("Opening Chrome, sir.")

    def test_E10_shutdown_confirm_countdown_cancel(self):
        self.mic("cyrus shut down")
        self.assertSaid("Shut down, sir? Yes or no.")
        self.assertNotCalled("shutdown")
        self.mic("yes")
        self.assertCalled("shutdown", 10)
        self.assertSaid("Shutting down in 10 seconds, sir. Say cancel to stop it.")
        self.assertEqual(self.engine.snapshot().mode, "follow_up")
        self.mic("cancel")                                  # no wake word, inside the follow-up
        self.assertCalled("abort_shutdown")
        self.assertEqual(self.last(), "Cancelled, sir.")

    def test_E10b_stop_during_countdown(self):
        self.mic("cyrus restart")
        self.mic("yes")
        self.assertCalled("restart", 10)
        self.mic("stop")
        self.assertCalled("abort_shutdown")

    def test_E11_confirm_expires(self):
        self.mic("cyrus shut down")
        self.ns.clock.advance(13)
        self.engine.tick()
        self.mic("yes")
        self.assertNotCalled("shutdown")

    def test_E12_destructive_with_unk(self):
        self.mic("cyrus shut [unk] down")
        self.assertSaid("Did you say shut down, sir?")
        self.assertNotCalled("shutdown")

    def test_E13_set_volume(self):
        for text in ("cyrus set volume to forty percent", "cyrus set volume two forty"):
            self.a.reset()
            self.mic(text)
            self.assertCalled("volume_set", 40)
            self.assertEqual(self.last(), "Volume at 40 percent, sir.")

    def test_E14_bare_play_is_media(self):
        # pause presses only while sound plays, resume only while it doesn't (Sep 15: the key is a toggle)
        for text, playing in (("cyrus play", True), ("cyrus pause", True), ("cyrus resume", False)):
            self.a.reset()
            self.a.returns["media_sounding"] = playing
            self.mic(text)
            self.assertCalled("media", "play_pause")
            self.assertNotCalled("find_song")

    def test_E15_song_from_free_text(self):
        self.mic("cyrus play eleven", free_text="cyrus play alone")
        self.assertCalled("find_song", "alone")
        self.assertCalled("open_target", WATCH)
        self.assertSaid("Playing Alone by Alan Walker, sir.")

    def test_E16_ask_noise_answer(self):
        self.mic("cyrus play some music")
        self.assertEqual(self.last(), "What should I play, sir?")
        self.mic("how")
        self.assertNotCalled("find_song")
        self.mic("blinding lights", free_text="blinding lights")
        self.assertCalled("find_song", "blinding lights")

    def test_E17_ask_cancel(self):
        self.mic("cyrus play music")
        self.mic("cancel")
        self.assertEqual(self.last(), "Okay, sir.")
        self.assertNotCalled("find_song")
        self.assertIsNone(self.engine.snapshot().dialogue)

    def test_E20_next_event_vs_next(self):
        self.mic("cyrus next")
        self.assertCalled("media", "next")
        if _have("xyrus.commands.calendar"):
            self.a.reset()
            self.mic("cyrus next event")
            self.assertNotCalled("media")

    def test_E22_typed_open_notepad(self):
        self.typed("open notepad")
        self.assertCalled("open_target", "notepad")
        self.assertSaid("Opening Notepad, sir.")

    def test_E25_calculator_for(self):
        self.mic("cyrus what is twelve times for")
        self.assertEqual(self.last(), "48, sir.")


class TestE18Offline(Base):
    def make_actions(self):
        return FakeActions(find_song=OSError("no network"))

    def test_E18_offline_fallback(self):
        self.typed("play alone")
        self.assertSaid("I couldn't reach YouTube, sir. Opening the search instead.")
        self.assertCalled("open_target", "https://www.youtube.com/results?search_query=alone")


class TestSongNotFound(Base):
    def make_actions(self):
        return FakeActions(find_song=LookupError("zzz"))

    def test_not_found(self):
        self.typed("play zzqx")
        self.assertSaid("I couldn't find zzqx on YouTube, sir. Here's the search.")
        self.assertCalled("open_target", "https://www.youtube.com/results?search_query=zzqx")


class TestYoutubeLookupOff(Base):
    config = {"youtube_lookup": False}

    def test_lookup_off_opens_results(self):
        self.typed("play alone")
        self.assertNotCalled("find_song")
        self.assertCalled("open_target", "https://www.youtube.com/results?search_query=alone")
        self.assertSaid("Here's YouTube for alone, sir.")


# ============================================================================ §3.1 listening core
class TestCore(Base):
    def test_help(self):
        self.typed("what can you do")
        self.assertIn("show:Commands", drain_ui(self.ns.ui_queue))
        self.assertSaid("Here's everything I can do, sir.")
        for t in ("help", "what are your commands"):
            self.run_case(t, reply="Here's everything I can do, sir.")

    def test_repeat(self):
        self.typed("what time is it")
        first = self.last()
        for t in ("repeat", "repeat that", "say that again"):
            self.typed(t)
            self.assertEqual(self.last(), first)

    def test_repeat_nothing(self):
        self.typed("repeat")
        self.assertEqual(self.last(), "I haven't said anything yet, sir.")

    def test_what_did_you_hear(self):
        self.mic("cyrus open chrome")
        self.mic("cyrus what did you hear")
        self.assertEqual(self.last(), "I heard: open chrome.")

    def test_stop_talking(self):
        for t in ("cyrus stop", "quiet", "be quiet", "stop talking"):
            before = self.ns.speaker.stops
            self.typed(t)
            self.assertGreater(self.ns.speaker.stops, before, t)

    def test_pause_listening(self):
        for t in ("stop listening", "pause listening"):
            self.engine.set_paused(False)
            self.typed(t)
            self.assertEqual(self.last(), "Pausing. Mic off, sir.")
            self.assertTrue(self.engine.snapshot().paused)

    def test_pause_listening_for(self):
        from xyrus.commands import core
        scheduled = []
        with mock.patch.object(core, "_schedule", lambda secs, fn: scheduled.append((secs, fn))):
            self.typed("stop listening for ten minutes")
        self.assertEqual(self.last(), "Mic off for ten minutes, sir.")
        self.assertTrue(self.engine.snapshot().paused)
        self.assertEqual(scheduled[0][0], 600)
        scheduled[0][1]()                           # the timer fires
        self.assertFalse(self.engine.snapshot().paused)
        self.assertEqual(self.last(), "I'm back, sir.")

    def test_small_talk(self):
        for t in ("thank you", "thanks", "thanks a lot"):
            self.run_case(t)
            self.assertIn(self.last(), ("Anytime, sir.", "My pleasure, sir.", "Of course."))
        for t in ("who are you", "what's your name", "what are you"):
            self.run_case(t, reply="I'm Xyrus, sir. Your offline assistant — nothing I hear leaves this PC.")
        for t in ("how are you", "how are you doing"):
            self.run_case(t, "system_status", reply="Running smoothly, sir. CPU at 12 percent.")
        for t in ("are you there", "you there"):
            self.run_case(t, reply="Always, sir.")

    def test_hello_arms(self):
        self.mic("cyrus hello")
        self.assertSaid("Hello, sir.")
        self.assertIn(self.engine.snapshot().mode, ("armed", "arming"))


# ============================================================================ §3.2 power
class TestPower(Base):
    def test_shutdown_phrases_confirm(self):
        for t in ("shut down", "shutdown", "power off", "turn off the pc", "turn off the computer",
                  "shut down the computer"):
            self.engine.cancel_dialogue()
            self.run_case(t, reply="Shut down, sir? Yes or no.")
            self.assertNotCalled("shutdown")

    def test_restart(self):
        for t in ("restart", "reboot", "restart the pc", "restart the computer"):
            self.engine.cancel_dialogue()
            self.run_case(t, reply="Restart, sir? Yes or no.")
        self.typed("yes")
        self.assertCalled("restart", 10)
        self.assertSaid("Restarting in 10 seconds, sir. Say cancel to stop it.")

    def test_no_confirm_when_disabled(self):
        self.ns.config.set("confirm_shutdown", False)
        self.typed("shut down")
        self.assertCalled("shutdown", 10)

    def test_cancel(self):
        for t in ("cancel", "abort", "cancel shutdown", "cancel the shutdown", "stop the shutdown"):
            self.run_case(t, "abort_shutdown", reply="Cancelled, sir.")

    def test_shutdown_in_and_when(self):
        self.typed("shut down in thirty minutes")
        self.assertSaid("Shut down in thirty minutes, sir? Yes or no.")
        self.typed("yes")
        self.assertCalled("shutdown", 1800)
        self.assertSaid("Done. Shutting down at 11 AM, sir.")
        self.typed("when is the shutdown")
        self.assertEqual(self.last(), "At 11 AM, sir.")
        self.typed("cancel")
        self.typed("when is the shutdown")
        self.assertEqual(self.last(), "There's no shutdown planned, sir.")

    def test_restart_in(self):
        self.typed("restart in ten minutes")
        self.typed("yes")
        self.assertCalled("restart", 600)

    def test_sleep_lock_screen(self):
        for t in ("go to sleep", "sleep", "sleep mode"):
            self.run_case(t, "sleep", reply="Going to sleep, sir.")
        for t in ("lock", "lock screen", "lock the pc", "lock the computer"):
            self.run_case(t, "lock", reply="Locking, sir.")
        for t in ("screen off", "display off", "monitor off", "turn off the screen"):
            self.run_case(t, "screen_off", reply="Screen off, sir.")
        for t in ("wake up", "screen on", "display on", "turn on the screen"):
            self.run_case(t, "screen_on", reply="I'm here, sir.")

    def test_lock_after_speech(self):
        engine, ns = make_test_engine(tmpdir=Path(tempfile.mkdtemp()), auto_done=False)
        engine.handle("lock", "typed")
        self.assertEqual(ns.actions.called("lock"), [])
        ns.speaker.finish()
        self.assertEqual(ns.actions.called("lock"), [()])

    def test_hibernate(self):
        self.run_case("hibernate", "hibernate", reply="Hibernating, sir.")

    def test_sign_out(self):
        for t in ("sign out", "log out", "log off"):
            self.engine.cancel_dialogue()
            self.run_case(t, reply="Sign out, sir? Yes or no.")
        self.typed("yes")
        self.assertCalled("sign_out")
        self.assertSaid("Signing out.")

    def test_keep_awake(self):
        for t in ("keep the pc awake", "stay awake"):
            self.run_case(t, "keep_awake", (True,), reply="I'll keep it awake, sir.")
        self.run_case("let it sleep", "keep_awake", (False,), reply="Okay, sleep is allowed again.")


class TestHibernateOff(Base):
    def make_actions(self):
        return FakeActions(hibernate=False)

    def test_hibernate_off(self):
        self.typed("hibernate")
        self.assertSaid("Hibernate is turned off on this PC, sir.")


# ============================================================================ §3.3 sound and media
class TestSound(Base):
    def test_volume_steps_reply_level(self):
        self.run_case("volume up", "volume_step", (10,), reply="Volume at 60 percent, sir.")
        self.run_case("volume down", "volume_step", (-10,), reply="Volume at 50 percent, sir.")
        self.run_case("turn it up a bit", "volume_step", (5,), reply="Volume at 55 percent, sir.")
        self.run_case("turn it down a lot", "volume_step", (-25,), reply="Volume at 30 percent, sir.")
        for t in ("louder", "turn it up"):
            self.run_case(t, "volume_step", (10,))
        for t in ("quieter", "turn it down"):
            self.run_case(t, "volume_step", (-10,))

    def test_volume_by(self):
        self.run_case("volume up by twenty", "volume_step", (20,))
        self.run_case("volume down by five", "volume_step", (-5,))

    def test_set_and_max(self):
        for t in ("set the volume to forty", "set volume to forty", "volume forty", "set volume forty percent"):
            self.run_case(t, "volume_set", (40,), reply="Volume at 40 percent, sir.")
        for t in ("volume max", "full volume", "max volume", "maximum volume"):
            self.run_case(t, "volume_set", (100,))

    def test_query(self):
        for t in ("what's the volume", "what is the volume", "volume level"):
            self.run_case(t, "volume_get", reply="Volume's at 50 percent, sir.")
        self.a.state["muted"] = True
        self.run_case("what's the volume", reply="Volume's at 50 percent and muted, sir.")

    def test_mute_unmute(self):
        for t in ("mute", "mute the sound"):
            self.run_case(t, "mute_set", (True,))
        for t in ("un mute", "sound on", "turn the sound on"):
            self.run_case(t, "mute_set", (False,))

    def test_media(self):
        # pause presses only while sound plays, resume only while it doesn't (Sep 15: the key is a toggle)
        for t, playing in (("play", True), ("pause", True), ("resume", False), ("play pause", True),
                           ("pause the music", True), ("resume the music", False), ("pause it", True)):
            self.a.returns["media_sounding"] = playing
            self.run_case(t, "media", ("play_pause",))
        for t in ("next", "next track", "next song", "cyrus skip"):
            self.run_case(t, "media", ("next",))
        for t in ("previous", "previous track", "previous song", "go back"):
            self.run_case(t, "media", ("prev",))
        for t in ("stop the music", "stop playback"):
            self.run_case(t, "media", ("stop",))

    def test_app_volume(self):
        self.run_case("mute chrome", "app_volume", ("chrome", None, True), reply="Chrome muted, sir.")
        self.run_case("set spotify volume to thirty", "app_volume", ("spotify", 30, None),
                      reply="Spotify at 30 percent, sir.")


class TestAppVolumeMissing(Base):
    def make_actions(self):
        return FakeActions(app_volume=False)

    def test_no_session(self):
        self.typed("mute chrome")
        self.assertSaid("Chrome isn't playing any sound, sir.")


# ============================================================================ §3.4 display and windows
class TestDisplay(Base):
    def test_brightness(self):
        for t in ("set the brightness to fifty", "brightness fifty", "set brightness to fifty percent"):
            self.run_case(t, "brightness_set", (50,), reply="Brightness at 50 percent, sir.")
        for t in ("brightness up", "brighter"):
            self.run_case(t, "brightness_set", ("+15",))
        for t in ("brightness down", "dimmer"):
            self.run_case(t, "brightness_set", ("-15",))
        self.run_case("dim the screen", "brightness_set", (20,))
        for t in ("what's the brightness", "brightness level"):
            self.run_case(t, "brightness_get", reply="Brightness is at 20 percent, sir.")

    APP_THEME = [
        ("theme:dark", "Dark mode, sir.", ("dark mode", "night mode", "dark theme", "turn on dark mode",
                                           "switch to dark mode", "can you make the app dark")),
        ("theme:light", "Light mode, sir.", ("light mode", "white mode", "light theme", "turn on light mode",
                                             "switch to light mode", "can you make the app white",
                                             "make the app light")),
        ("theme:system", "Following Windows, sir.", ("follow windows theme", "follow the windows theme",
                                                     "use the windows theme")),
    ]

    def test_app_theme_switches_xyrus_window_only(self):
        for msg, reply, phrases in self.APP_THEME:
            for t in phrases:
                with self.subTest(phrase=t):
                    drain_ui(self.ns.ui_queue)
                    self.run_case(t, reply=reply)
                    self.assertIn(msg, drain_ui(self.ns.ui_queue), t)
                    self.assertNotCalled("set_theme")          # bare "dark mode" never touches Windows

    def test_windows_theme(self):
        for t in ("windows dark mode", "switch windows to dark mode", "make windows dark"):
            self.run_case(t, "set_theme", (True,), reply="Windows is in dark mode, sir.")
            self.assertNotIn("theme:dark", drain_ui(self.ns.ui_queue))
        for t in ("windows light mode", "switch windows to light mode", "make windows light"):
            self.run_case(t, "set_theme", (False,), reply="Windows is in light mode, sir.")

    def test_brightness_extras(self):
        self.run_case("brightness max", "brightness_set", (100,), reply="Brightness at 100 percent, sir.")
        self.run_case("turn up the brightness a bit", "brightness_set", ("+5",))
        self.run_case("lower the brightness a lot", "brightness_set", ("-25",))


class TestNoBrightness(Base):
    def make_actions(self):
        return FakeActions(brightness_set=None, brightness_get=None)

    def test_unsupported(self):
        self.run_case("set brightness to fifty", reply="This screen doesn't let me change brightness, sir.")
        self.run_case("what's the brightness", reply="This screen doesn't let me change brightness, sir.")


class TestWindows(Base):
    def test_window_commands(self):
        for t in ("show desktop", "minimize everything"):
            self.run_case(t, "show_desktop")
        for t in ("close window", "close this", "close this window"):
            self.run_case(t, "window_cmd", ("close",))
        for t in ("minimize", "minimise", "minimize this"):
            self.run_case(t, "window_cmd", ("minimize",))
        for t in ("maximize", "maximise", "maximize this"):
            self.run_case(t, "window_cmd", ("maximize",))
        for t in ("restore this", "normal size"):
            self.run_case(t, "window_cmd", ("restore",))
        for t in ("snap left", "snap this left"):
            self.run_case(t, "snap", ("left",))
        for t in ("snap right", "snap this right"):
            self.run_case(t, "snap", ("right",))
        self.run_case("keep this on top", "window_cmd", ("topmost",), reply="Pinned on top, sir.")
        self.run_case("stop keeping this on top", "window_cmd", ("notopmost",))
        for t, c in (("new desktop", "new"), ("next desktop", "next"), ("previous desktop", "prev"),
                     ("close this desktop", "close")):
            self.run_case(t, "desktop", (c,))

    def test_switch_close(self):
        for t in ("switch to chrome", "go to chrome", "bring up chrome"):
            self.run_case(t, "focus_app", ("chrome",), reply="Chrome, sir.")
        for t in ("close chrome", "quit chrome", "exit chrome"):
            self.run_case(t, "close_app", ("chrome",), reply="Closing Chrome, sir.")

    def test_force_close(self):
        for t in ("force close chrome", "kill chrome"):
            self.engine.cancel_dialogue()
            self.run_case(t, reply="Force close Chrome, sir? Unsaved work is lost. Yes or no.")
            self.assertNotCalled("kill_app")
        self.typed("yes")
        self.assertCalled("kill_app", "chrome")
        self.assertEqual(self.last(), "Done, sir.")

    def test_whats_open(self):
        self.a.returns["windowed_apps"] = ["chrome", "notepad", "spotify"]
        for t in ("what's open", "what windows are open"):
            self.run_case(t, "windowed_apps", reply="Chrome, Notepad and Spotify are open, sir.")
        self.a.returns["windowed_apps"] = []
        self.run_case("what's open", reply="Nothing's open, sir.")


class TestWindowsNotOpen(Base):
    def make_actions(self):
        return FakeActions(focus_app=False, close_app=False, foreground_is_self=True)

    def test_switch_offers_open(self):
        self.typed("switch to chrome")
        self.assertSaid("Chrome isn't open, sir. Want me to open it?")
        self.typed("yes")
        self.assertCalled("open_target", "chrome")
        self.assertEqual(self.last(), "Opening Chrome, sir.")

    def test_close_not_open(self):
        self.typed("close chrome")
        self.assertEqual(self.last(), "Chrome isn't open, sir.")

    def test_close_own_window_hides(self):
        self.typed("close window")
        self.assertNotIn(("close",), self.calls("window_cmd"))
        self.assertEqual(self.last(), "That's my own window, sir. I'll hide it instead.")
        self.assertIn("hide", drain_ui(self.ns.ui_queue))


# ============================================================================ §3.5 apps, web, keys, clipboard
class TestApps(Base):
    def test_open_aliases(self):
        for t in ("open chrome", "launch chrome", "start chrome"):
            self.run_case(t, "open_target", ("chrome",), reply="Opening Chrome, sir.")
        self.run_case("open youtube", "open_target", ("https://www.youtube.com",), reply="Opening YouTube, sir.")
        self.run_case("open bluetooth settings", "open_target", ("ms-settings:bluetooth",))
        self.run_case("open recycle bin", "open_target", ("shell:RecycleBinFolder",))
        self.run_case("open task manager", "open_target", ("taskmgr",), reply="Opening Task Manager, sir.")

    def test_unknown(self):
        self.run_case("open kubernetes", "find_app", ("kubernetes",),
                      reply="I don't know how to open kubernetes, sir. Add it in the Apps tab.")


class TestStartMenu(Base):
    def make_actions(self):
        return FakeActions(find_app=("obs studio", r"C:\Start\OBS Studio.lnk"))

    def test_start_menu_hit(self):
        self.typed("open obs studio")
        self.assertCalled("open_target", r"C:\Start\OBS Studio.lnk")
        self.assertEqual(self.last(), "Opening Obs Studio, sir.")


class TestWeb(Base):
    def test_search(self):
        for t in ("search for cheap keyboards", "google cheap keyboards", "search the web for cheap keyboards"):
            self.run_case(t, "open_target", ("https://www.google.com/search?q=cheap+keyboards",),
                          reply="Searching for cheap keyboards, sir.")
        for t in ("search youtube for lofi beats", "youtube lofi beats"):
            self.run_case(t, "open_target", ("https://www.youtube.com/results?search_query=lofi+beats",),
                          reply="Here's YouTube for lofi beats, sir.")

    def test_play_variants(self):
        self.run_case("play shape of you on youtube", "find_song", ("shape of you",))
        self.run_case("play see you again on spotify", "open_target", ("spotify:search:see%20you%20again",),
                      reply="Searching Spotify for see you again, sir.")
        for t in ("play a song", "play something", "play anything", "play a track"):
            self.engine.cancel_dialogue()
            self.run_case(t, reply="What should I play, sir?")
        self.typed("believer by imagine dragons")
        self.assertCalled("find_song", "believer by imagine dragons")

    def test_type(self):
        self.run_case("type hello world", "type_text", ("hello world",))


class TestTypeOwnWindow(Base):
    def make_actions(self):
        return FakeActions(foreground_is_self=True)

    def test_refuse(self):
        self.typed("type hello")
        self.assertNotCalled("type_text")
        self.assertEqual(self.last(), "I won't type into my own window, sir.")


class TestKeysClipboard(Base):
    def test_keys(self):
        from xyrus.commands.keys import KEY_COMMANDS
        for _name, chord, phrases, _help, _single in KEY_COMMANDS:
            for p in phrases:
                self.run_case(("cyrus " + p) if len(p.split()) == 1 else p, "keys", (chord,))

    def test_clipboard(self):
        self.a.returns["clip_get"] = "hello world"
        for t in ("read the clipboard", "read my clipboard", "what's on the clipboard"):
            self.run_case(t, "clip_get", reply="hello world")
        self.a.returns["clip_get"] = " ".join(f"w{i}" for i in range(50))
        self.run_case("read the clipboard", reply=" ".join(f"w{i}" for i in range(40)) + "…")
        self.a.returns["clip_get"] = ""
        self.run_case("read the clipboard", reply="The clipboard is empty, sir.")
        self.run_case("clear the clipboard", "clip_set", ("",), reply="Cleared, sir.")
        self.run_case("copy the date", "clip_set", ("Sunday, September 13, 2026",), reply="Copied, sir.")
        self.run_case("copy the time", "clip_set", ("10:30 AM",), reply="Copied, sir.")


# ============================================================================ §3.7 info and tools
class TestInfo(Base):
    def test_time_date(self):
        for t in ("what time is it", "what's the time", "what is the time", "the time", "time"):
            self.run_case(t, reply="It's 10:30 AM, sir.")
        for t in ("what day is it", "what's the date", "what is the date", "the date", "what's today"):
            self.run_case(t, reply="Today is Sunday, September 13, sir.")

    def test_status(self):
        for t in ("system status", "status", "system report"):
            self.run_case(t, "system_status",
                          reply="CPU at 12 percent, memory at 48 percent, no battery, up 3 hours 10 minutes, sir.")
        for t in ("battery", "battery level", "how much battery"):
            self.run_case(t, reply="There's no battery, this is a desktop, sir.")
        for t in ("disk space", "how much space is left"):
            self.run_case(t, "disks", reply="C has 120 gigabytes free of 480, sir.")
        self.run_case("what's using the cpu", "top_processes", reply="Chrome, Code and Discord, sir.")

    def test_screenshots(self):
        for t in ("take a screenshot", "screenshot", "take a picture of the screen"):
            self.run_case(t, "screenshot", (False,), reply="Screenshot saved, sir.")
        ev = [e for e in self.engine.snapshot().events if e.kind == "info"]
        self.assertTrue(ev and ev[-1].meta.endswith(".png"))
        self.run_case("screenshot this window", "screenshot", (True,))
        for t in ("open the last screenshot", "show the screenshot"):
            self.run_case(t, "last_screenshot", reply="There's no screenshot yet, sir.")
        self.a.returns["last_screenshot"] = Path(r"C:\p\s.png")
        self.run_case("open the last screenshot", "open_target", (r"C:\p\s.png",))

    def test_calculator(self):
        self.run_case("what is fifteen percent of eighty", reply="12, sir.")
        self.run_case("calculate two hundred and fifty plus five", reply="255, sir.")
        self.run_case("what's seven times six", reply="42, sir.")
        self.run_case("what is five divided by zero", reply="That's undefined, sir.")
        self.run_case("what is three point five times two", reply="7, sir.")

    def test_fun(self):
        seen = []
        for t in ("tell me a joke", "joke", "tell me another", "tell me a joke"):
            self.run_case(t)
            self.assertIn(self.last(), R.JOKES)
            seen.append(self.last())
        self.assertTrue(all(a != b for a, b in zip(seen, seen[1:])), "same joke twice in a row")
        for t in ("flip a coin", "heads or tails"):
            self.run_case(t)
            self.assertIn(self.last(), ("Heads.", "Tails."))
        for t in ("roll a die", "roll a dice"):
            self.run_case(t)
            self.assertRegex(self.last(), r"^You rolled an? (one|two|three|four|five|six)\.$")
        self.run_case("roll two dice")
        self.assertRegex(self.last(), r"^You rolled an? \w+ and an? \w+\.$")
        for _ in range(5):
            self.run_case("pick a number between one and ten")
            self.assertIn(int(self.last().rstrip(".")), range(1, 11))

    def test_weather_off(self):
        for t in ("what's the weather", "will it rain today"):
            self.run_case(t, reply="Weather needs the internet — turn it on in Settings, sir.")
            self.assertNotCalled("weather")


class TestWeatherOn(Base):
    config = {"weather": {"enabled": True, "city": "Dhaka"}}

    def make_actions(self):
        return FakeActions(weather={"temp_c": 28, "desc": "Clear", "city": "Dhaka", "rain_chance": 70})

    def test_weather(self):
        self.run_case("what's the weather", "weather", ("Dhaka",), reply="28 degrees and clear in Dhaka, sir.")
        self.run_case("will it rain today", reply="Probably, sir. There's a 70 percent chance of rain today.")


class TestBattery(Base):
    def make_actions(self):
        return FakeActions(system_status={"cpu": 5, "mem": 30, "battery": {"pct": 80, "plugged": True},
                                          "uptime_s": 1500},
                           disks=[{"drive": "C", "free_gb": 20, "total_gb": 480, "pct": 96},
                                  {"drive": "D", "free_gb": 300, "total_gb": 1000, "pct": 70}])

    def test_battery_and_disks(self):
        self.run_case("battery", reply="Battery at 80 percent, charging, sir.")
        self.run_case("status", reply="CPU at 5 percent, memory at 30 percent, battery at 80 percent, charging, "
                                      "up 25 minutes, sir.")
        self.run_case("disk space", reply="C has 20 gigabytes free of 480, sir. D has 300 free of 1000. C is nearly full.")


# ============================================================================ §3.10 custom commands
ROUTINES = [
    {"phrase": "work mode", "enabled": True, "steps": [
        {"action": "open", "arg": "code"}, {"action": "open", "arg": "https://example.com"},
        {"action": "keys", "arg": "ctrl+shift+t"}, {"action": "type", "arg": "hi"}, {"action": "wait", "arg": 0}]},
    {"phrase": "focus mode", "enabled": True, "steps": [
        {"action": "command", "arg": "mute"}, {"action": "say", "arg": "Focus mode on, sir."}]},
    {"phrase": "broken thing", "enabled": True, "steps": [
        {"action": "open", "arg": "code"}, {"action": "keys", "arg": "ctrl+banana"}, {"action": "open", "arg": "x"}]},
    {"phrase": "disabled one", "enabled": False, "steps": [{"action": "open", "arg": "code"}]},
    {"phrase": "time", "enabled": True, "steps": []},
    {"phrase": "cyrus mode", "enabled": True, "steps": []},
]


class TestCustom(Base):
    config = {"custom_commands": ROUTINES}

    def make_actions(self):
        def keys(chord):
            from xyrus.actions.keys import parse_chord
            parse_chord(chord)                      # raises ValueError on a bad chord, like WinActions
        return FakeActions(keys=keys)

    def setUp(self):
        super().setUp()
        from xyrus.commands import custom
        self.custom = custom
        self.errors = custom.register_custom(self.ns.registry, self.ns.config)

    def test_registration_and_validation(self):
        names = {c.name for c in self.ns.registry.commands() if c.custom}
        self.assertEqual(names, {"custom_work_mode", "custom_focus_mode", "custom_broken_thing"})
        self.assertEqual(dict(self.errors), {"time": self.custom.ERR_SHORT, "cyrus mode": self.custom.ERR_WAKE})
        self.assertIn("Your commands", [s for s, _ in self.ns.registry.sections(self.ns.config)])
        v = self.custom.validate
        reg = self.ns.registry
        self.assertEqual(v("show desktop", reg), self.custom.ERR_BUILTIN)
        self.assertEqual(v("please show desktop now", reg), self.custom.ERR_BUILTIN)
        self.assertEqual(v("go", reg), self.custom.ERR_SHORT)
        self.assertIsNone(v("movie mode", reg))
        self.assertIsNone(v("goodnight routine", reg))

        class Vocab:
            def unknown_words(self, phrase):
                return [w for w in phrase.split() if w == "kubernetes"]
        self.assertEqual(v("kubernetes mode", reg, Vocab()),
                         "The speech model doesn't know 'kubernetes' — pick another word.")

    def test_run_steps(self):
        self.typed("work mode")
        self.assertEqual(self.a.names(), ["open_target", "open_target", "keys", "type_text"])
        self.assertEqual(self.calls("open_target"), [("code",), ("https://example.com",)])
        self.assertEqual(self.last(), "Done, sir.")

    def test_say_and_command_steps(self):
        self.typed("focus mode")
        self.assertCalled("mute_set", True)
        self.assertEqual(self.last(), "Focus mode on, sir.")

    def test_failing_step_stops(self):
        self.typed("broken thing")
        self.assertEqual(self.calls("open_target"), [("code",)])
        self.assertEqual(self.last(), "Step 2 of broken thing failed, sir.")

    def test_reregister_on_change(self):
        self.ns.config.set("custom_commands", [{"phrase": "movie mode", "steps": [{"action": "keys", "arg": "f11"}]}])
        self.custom.register_custom(self.ns.registry, self.ns.config)
        self.assertEqual({c.name for c in self.ns.registry.commands() if c.custom}, {"custom_movie_mode"})
        self.typed("movie mode")
        self.assertCalled("keys", "f11")


# ============================================================================ paraphrases (lead requirement)
PARAPHRASES = {
    ("volume_step", "+"): ["volume up", "louder", "turn it up", "increase the volume", "raise the volume",
                           "make it louder", "can you turn up the volume", "turn the volume up a bit",
                           "please increase volume"],
    ("volume_step", "-"): ["can you lower volume", "lower sound", "decrease the sound", "make it quieter",
                           "turn it down a bit", "reduce the volume", "volume down please", "quieter",
                           "lower the volume"],
    ("mute_set", True): ["mute", "mute the sound", "turn off the sound", "turn the sound off", "sound off",
                         "please mute", "mute the volume", "silence", "can you turn off the sound"],
    ("volume_set", 40): ["set volume to forty", "volume forty percent", "set the volume to 40",
                         "turn the volume to forty", "change the volume to forty percent", "put the volume at forty",
                         "volume to forty", "can you set the volume to forty percent", "set volume two forty"],
    ("brightness_set", "+15"): ["brightness up", "brighter", "increase the brightness", "raise the brightness",
                                "turn up the brightness", "make the screen brighter", "more brightness",
                                "can you increase brightness", "turn the brightness up"],
    ("brightness_set", "-15"): ["brightness down", "dimmer", "decrease the brightness", "lower the brightness",
                                "reduce the brightness", "turn down the brightness", "make the screen darker",
                                "less brightness", "can you lower brightness"],
    ("open_target", "chrome"): ["open chrome", "launch chrome", "start chrome", "please open chrome",
                                "can you open chrome", "open up chrome", "open google chrome",
                                "open the chrome browser", "could you launch chrome please"],
    ("close_app", "chrome"): ["close chrome", "quit chrome", "exit chrome", "shut down chrome",
                              "can you close chrome", "please close chrome", "close google chrome", "shut chrome",
                              "close chrome please"],
    ("screenshot", False): ["take a screenshot", "screenshot", "take a picture of the screen", "capture the screen",
                            "screen capture", "take a screen shot", "grab the screen", "can you take a screenshot",
                            "print screen"],
}


class TestParaphrases(Base):
    def test_paraphrases(self):
        for (call, arg), phrases in PARAPHRASES.items():
            self.assertGreaterEqual(len(phrases), 8)
            for p in phrases:
                self.engine.cancel_dialogue()
                self.a.reset()
                self.typed(p)
                got = self.calls(call)
                self.assertTrue(got, f"{p!r}: {call} not called (calls={self.a.names()}, said={self.said[-1:]})")
                if arg == "+":
                    self.assertGreater(got[-1][0], 0, p)
                elif arg == "-":
                    self.assertLess(got[-1][0], 0, p)
                else:
                    self.assertEqual(got[-1][0], arg, p)
                self.assertNotCalled("shutdown")

    def test_turn_off_object_decides(self):
        for p in ("turn off the sound", "turn the sound off", "turn off the volume", "turn off the screen",
                  "turn the screen off", "turn off the display", "turn off the monitor", "shut down chrome"):
            self.engine.cancel_dialogue()
            self.a.reset()
            n = len(self.said)
            self.typed(p)
            self.assertNotCalled("shutdown")
            self.assertFalse(any("Shut down" in s for s in self.said[n:]), (p, self.said[n:]))
        for p in ("turn off the pc", "turn off the computer", "shut down the computer", "shut down"):
            self.engine.cancel_dialogue()
            self.typed(p)
            self.assertEqual(self.last(), "Shut down, sir? Yes or no.", p)

    @unittest.skipUnless(_have("xyrus.commands.timers"), "T2 timers module not available")
    def test_timer_paraphrases(self):
        for p in ("set a timer for five minutes", "timer five minutes", "five minute timer",
                  "start a timer for five minutes", "set timer for 5 minutes", "timer for five minutes",
                  "can you set a timer for five minutes", "please set a timer for five minutes"):
            self.engine.handle("cancel all timers", "typed")
            self.typed(p)
            self.assertIn("five minutes", self.last(), p)


# ============================================================================ static checks (no engine needed)
class TestRegistrations(unittest.TestCase):
    T4_MODULES = ("core", "power", "sound", "display", "windows", "apps", "web", "keys", "clipboard", "info", "fun")

    @classmethod
    def setUpClass(cls):
        from xyrus import commands
        from xyrus.registry import REGISTRY
        commands.load_all(REGISTRY)
        cls.reg = REGISTRY
        cls.texts = {p.text for c in REGISTRY.commands() for p in c.patterns}

    def test_every_spec_phrase_registered(self):
        spec = [
            # §3.1
            "stop listening", "pause listening", "stop listening for <duration>", "stop", "quiet", "be quiet",
            "stop talking", "repeat", "repeat that", "say that again", "what did you hear", "what can you do", "help",
            "what are your commands",
            # §3.2
            "shut down", "shutdown", "power off", "turn off the pc", "turn off the computer", "restart", "reboot",
            "restart the pc", "restart the computer", "cancel", "abort", "cancel shutdown", "cancel the shutdown",
            "stop the shutdown", "shut down in <duration>", "restart in <duration>", "when is the shutdown",
            "go to sleep", "sleep", "sleep mode", "hibernate", "lock", "lock screen", "lock the pc",
            "lock the computer", "sign out", "log out", "log off", "screen off", "display off", "monitor off",
            "turn off the screen", "wake up", "screen on", "display on", "turn on the screen", "keep the pc awake",
            "stay awake", "let it sleep",
            # §3.3
            "volume up", "louder", "turn it up", "volume down", "quieter", "turn it down", "volume up by <number>",
            "volume down by <number>", "set [the] volume to <percent>", "volume <percent>", "set volume <percent>",
            "volume max", "full volume", "max volume", "maximum volume", "what's the volume", "what is the volume",
            "volume level", "mute", "mute the sound", "un mute", "sound on", "turn the sound on", "play", "pause",
            "resume", "play pause", "pause the music", "resume the music", "pause it", "next", "next track",
            "next song", "skip", "previous", "previous track", "previous song", "go back", "stop the music",
            "stop playback", "mute <app>", "set <app> volume to <percent>",
            # §3.4
            "set [the] brightness to <percent>", "brightness <percent>", "brightness up", "brighter",
            "brightness down", "dimmer", "dim the screen", "what's the brightness", "brightness level",
            "show desktop", "minimize everything", "close window", "close this", "close this window", "minimize",
            "minimise", "minimize this", "maximize", "maximise", "maximize this", "restore this", "normal size",
            "snap left", "snap right", "snap this left", "snap this right", "switch to <app>", "go to <app>",
            "bring up <app>", "close <app>", "quit <app>", "exit <app>", "force close <app>", "kill <app>",
            "keep this on top", "stop keeping this on top", "what's open", "what windows are open", "new desktop",
            "next desktop", "previous desktop", "close this desktop", "dark mode", "light mode",
            # §3.5
            "open <app>", "launch <app>", "start <app>", "search for <query>", "google <query>",
            "search the web for <query>", "search youtube for <query>", "youtube <query>", "type <text>",
            "press enter", "press escape", "press tab", "press space", "select all", "copy", "copy that", "cut",
            "paste", "paste it", "undo", "redo", "save", "save this", "new tab", "close tab", "reopen tab", "refresh",
            "full screen", "read the clipboard", "read my clipboard", "what's on the clipboard",
            "clear the clipboard", "copy the date", "copy the time",
            # §3.6
            "play <song>", "play <song> on youtube", "play some music", "play music", "play a song",
            "play something", "play anything", "play a track", "play <song> on spotify",
            # §3.7
            "what time is it", "what's the time", "what is the time", "the time", "time", "what day is it",
            "what's the date", "what is the date", "the date", "what's today", "system status", "status",
            "system report", "battery", "battery level", "how much battery", "disk space", "how much space is left",
            "what's using the cpu", "take a screenshot", "screenshot", "take a picture of the screen",
            "screenshot this window", "open the last screenshot", "show the screenshot", "what is <expr>",
            "what's <expr>", "calculate <expr>", "tell me a joke", "joke", "tell me another", "flip a coin",
            "heads or tails", "roll a die", "roll a dice", "roll two dice",
            "pick a number between <number> and <number>", "what's the weather", "will it rain today",
            # §3.9 small talk (core.py)
            "thank you", "thanks", "thanks a lot", "who are you", "what's your name", "what are you", "how are you",
            "how are you doing", "hello", "hi", "are you there", "you there",
        ]
        missing = [p for p in spec if p not in self.texts]
        self.assertEqual(missing, [])

    def test_sections_and_help(self):
        from xyrus.registry import SECTION_ORDER
        for c in self.reg.commands():
            if c.handler.__module__.split(".")[-1] in self.T4_MODULES:
                self.assertIn(c.section, SECTION_ORDER, c.name)
                self.assertTrue(c.help, c.name)

    def test_g6_no_wake_alias_in_t4_literals(self):
        import ast
        import xyrus.commands as pkg
        base = Path(pkg.__file__).parent
        wakes = set(N.WAKE_ALIASES) | {"sirius", "serious"}
        for mod in self.T4_MODULES + ("custom", "__init__"):
            tree = ast.parse((base / f"{mod}.py").read_text(encoding="utf8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    words = set(re.findall(r"[a-z]+", node.value.lower()))
                    if mod == "custom" and node.value.startswith("cyrus"):
                        continue
                    self.assertFalse(words & wakes, f"{mod}.py literal {node.value!r}")

    def test_actions_never_speak(self):
        """G10: actions/ modules have no user-facing reply strings with {sir} and never import replies."""
        import xyrus.actions as pkg
        for f in Path(pkg.__file__).parent.glob("*.py"):
            src = f.read_text(encoding="utf8")
            self.assertNotIn("{sir}", src, f.name)
            self.assertNotIn("replies", src, f.name)
            self.assertNotIn("print(", src, f.name)

    def test_song_helpers(self):
        from xyrus.commands import web
        self.assertTrue(web.is_song_request("play alone"))
        self.assertFalse(web.is_song_request("play"))
        self.assertFalse(web.is_song_request("play pause"))
        self.assertEqual(web.song_title("cyrus play the song shape of you on youtube please"), "shape of you")
        self.assertEqual(web.song_title("sirius lay alone"), "alone")
        from xyrus.actions import web as aweb
        for q in ("shape of you", "a&b"):
            self.assertEqual(web.youtube_search_url(q), aweb.youtube_search_url(q))
            self.assertEqual(web.google_url(q), aweb.google_url(q))


if __name__ == "__main__":
    unittest.main()
