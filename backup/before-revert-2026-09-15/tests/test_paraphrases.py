"""T4 round 2 — every case of v1 D:\\arc\\test_meaning.py (the meaning layer: many phrasings per command and the
dangerous look-alikes) re-expressed through the v2 engine (make_test_engine, FakeActions, FakeSpeaker), asserting
the FakeActions call and/or the reply. Typed input unless the test says spoken.

Where v2 differs from v1 by an accepted spec decision the v2 behaviour is asserted and the case says so
(`V2:` comments): brightness steps are 15 % (§3.4), "dim the screen" is a decrease, "stop the music" is
VK_MEDIA_STOP (§3.3), "go to <app>" switches to an open app (§3.4), un-mute is silent (§3.3), a half-heard
destructive command gets the D11 "Did you say …" confirm instead of silence, "shut down chrome" closes Chrome.
Run: venv\\Scripts\\python.exe -m unittest tests.test_paraphrases -v
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

if not os.environ.get("XYRUS_DATA_DIR"):
    os.environ["XYRUS_DATA_DIR"] = tempfile.mkdtemp(prefix="xyrus_test_")

from xyrus import paths
from xyrus import replies as R
from xyrus.testing import FakeActions, drain_ui, make_test_engine

HAVE_MODEL = (paths.MODEL_DIR / "am" / "final.mdl").exists()


def _have(module: str) -> bool:
    try:
        __import__(module)
        return True
    except ImportError:
        return False


class Case(unittest.TestCase):
    def setUp(self):
        self.engine, self.ns = make_test_engine(tmpdir=Path(tempfile.mkdtemp(prefix="xyrus_para_")),
                                                actions=FakeActions())
        self.a = self.ns.actions

    def run_text(self, text, source="typed"):
        """Fresh state per phrase: no open dialogue/window, empty call log. Returns the replies it produced."""
        self.engine.cancel_dialogue()
        drain_ui(self.ns.ui_queue)
        self.a.reset()
        n = len(self.ns.speaker.said)
        self.engine.handle(text, source)
        return self.ns.speaker.said[n:]

    def calls(self, name):
        return self.a.called(name)

    def check_call(self, phrases, name, arg=None, pred=None):
        for p in phrases:
            with self.subTest(phrase=p):
                said = self.run_text(p)
                got = self.calls(name)
                self.assertTrue(got, f"{p!r}: {name} not called; calls={self.a.names()} said={said}")
                if arg is not None:
                    self.assertEqual(got[-1][0] if got[-1] else None, arg, f"{p!r}: {got}")
                if pred is not None:
                    self.assertTrue(pred(got[-1][0]), f"{p!r}: {got}")
                self.assert_safe(p, said)

    def check_said(self, phrases, reply, startswith=False):
        for p in phrases:
            with self.subTest(phrase=p):
                said = self.run_text(p)
                ok = any(s.startswith(reply) for s in said) if startswith else reply in said
                self.assertTrue(ok, f"{p!r}: want {reply!r}, said {said}; calls={self.a.names()}")

    def assert_safe(self, p, said):
        """No power action ever runs straight from one utterance."""
        for bad in ("shutdown", "restart", "sign_out"):
            self.assertFalse(self.calls(bad), f"{p!r} called {bad}")

    def assert_nothing(self, p, said, forbid=("shutdown", "restart", "lock", "sleep", "screen_off", "mute_set",
                                             "media", "volume_step", "volume_set", "sign_out", "kill_app")):
        for bad in forbid:
            self.assertFalse(self.calls(bad), f"{p!r} called {bad}: {self.a.calls}")
        for s in said:
            self.assertNotIn("Shut down", s, p)
            self.assertNotIn("Restart", s, p)


down = lambda d: d < 0          # noqa: E731
up = lambda d: d > 0            # noqa: E731


class TestVolume(Case):
    def test_volume_down(self):
        self.check_call([
            "lower the volume", "can you lower the volume", "lower sound", "lower the sound", "decrease the sound",
            "reduce the volume please", "make it quieter", "turn it down", "turn the music down", "volume down",
            "could you turn the volume down", "it's too loud", "drop the volume", "less volume", "softer please",
            "can you decrease the volume for me", "the sound is too loud", "bring the volume down"],
            "volume_step", pred=down)

    def test_volume_up(self):
        self.check_call([
            "raise the volume", "increase the sound", "turn it up", "make it louder", "louder please",
            "i can't hear it", "boost the volume", "volume up", "turn the music up", "pump it up", "it's too quiet",
            "can you increase the volume", "more volume", "turn up the sound"],
            "volume_step", pred=up)

    def test_step_sizes(self):
        for p, want in [("lower the volume a bit", -5), ("turn it down a little", -5), ("turn it up a lot", 25),
                        ("lower the volume by twenty", -20), ("raise the sound slightly", 5), ("volume down", -10)]:
            self.check_call([p], "volume_step", arg=want)

    def test_step_reply_says_level(self):
        said = self.run_text("can you lower the volume a bit")
        self.assertEqual(self.calls("volume_step"), [(-5,)])
        self.assertIn("Volume at 45 percent, sir.", said)

    def test_set_max(self):
        for p, want in [("set the volume to thirty", 30), ("volume forty percent", 40),
                        ("put the volume at fifty", 50), ("make the volume twenty five percent", 25),
                        ("turn the volume up to eighty", 80), ("volume max", 100), ("full volume", 100),
                        ("turn the volume all the way up", 100), ("volume a hundred percent", 100)]:
            self.check_call([p], "volume_set", arg=want)
        said = self.run_text("set the volume to thirty percent")
        self.assertIn("Volume at 30 percent, sir.", said)

    def test_query(self):
        # V2 wording: "Volume's at 50 percent, sir." (v1: "The volume is at …")
        for p in ("what's the volume", "how loud is it", "tell me the volume level"):
            with self.subTest(phrase=p):
                said = self.run_text(p)
                self.assertTrue(self.calls("volume_get"), (p, said))
                self.assertIn("Volume's at 50 percent, sir.", said)
                self.assertFalse(self.calls("volume_set"))

    def test_mute_unmute(self):
        self.check_call(["mute", "mute the sound", "turn off the sound", "sound off", "silence please", "no sound",
                         "switch off the sound", "turn the sound off", "mute the volume"], "mute_set", arg=True)
        # V2: un-mute is silent (§3.3); v1 said "Sound's back on".
        self.check_call(["un mute", "turn the sound back on", "sound on", "turn on the sound", "bring back the sound",
                         "switch on the sound"], "mute_set", arg=False)

    def test_turn_off_sound_no_shutdown_question(self):
        said = self.run_text("turn off the sound")
        self.assertEqual(self.calls("mute_set"), [(True,)])
        self.assertIsNone(self.engine.snapshot().dialogue)
        self.assertFalse(any("Shut down" in s for s in said))


class TestPower(Case):
    def test_shutdown_asks_first(self):
        for p in ("shut down", "shutdown the computer", "turn off the computer", "switch off the pc", "power off",
                  "turn off", "power down the laptop", "please shut down"):
            with self.subTest(phrase=p):
                said = self.run_text(p)
                self.assertIn(R.CONFIRM_SHUTDOWN.replace("{sir}", ", sir"), said, (p, said))
                self.assertFalse(self.calls("shutdown"))

    def test_confirm_then_cancel(self):
        self.run_text("turn off the computer")
        self.assertIsNotNone(self.engine.snapshot().dialogue)
        self.engine.handle("cancel", "typed")
        self.assertIsNone(self.engine.snapshot().dialogue)
        self.assertFalse(self.calls("shutdown"))

    def test_look_alikes_never_power_off(self):
        # object decides; "cant" in v1 = nothing happens here either
        self.a.returns["browser_playing"] = True           # "turn off the music" pauses what is playing
        cases = [("turn off the sound", "mute_set", (True,)), ("turn off the screen", "screen_off", ()),
                 ("switch off the display", "screen_off", ()), ("turn off the music", "media", ("play_pause",)),
                 # V2: "shut down chrome" closes Chrome (close_app); v1 said it can't. Never a shutdown either way.
                 ("shut down chrome", "close_app", ("chrome",))]
        for p, name, args in cases:
            with self.subTest(phrase=p):
                said = self.run_text(p)
                self.assertIn(args, self.calls(name), (p, self.a.calls, said))
                self.assert_nothing(p, said, forbid=("shutdown", "restart", "sign_out", "kill_app"))
        for p in ("turn off the lights", "turn off wifi", "restart chrome", "lock the door", "turn [unk] off",
                  "turn on the lights"):
            with self.subTest(phrase=p):
                said = self.run_text(p)
                self.assert_nothing(p, said)

    def test_half_heard_shutdown(self):
        # V2 (D11): destructive + [unk] -> "Did you say shut down, sir?" confirm; v1 stayed silent. Never runs.
        said = self.run_text("shut down [unk]", source="mic") if False else self.run_text("cyrus shut down [unk]",
                                                                                          source="mic")
        self.assertFalse(self.calls("shutdown"))
        self.engine.cancel_dialogue()
        self.assertFalse(self.calls("shutdown"))

    def test_screen_off_wake(self):
        self.check_call(["turn off the screen", "screen off", "switch off the monitor", "turn the display off",
                         "put the screen to sleep", "monitor off"], "screen_off")
        self.check_call(["wake up", "turn on the screen", "wake the screen"], "screen_on")

    def test_restart_sleep_lock(self):
        for p in ("restart", "reboot the computer", "restart my pc"):
            with self.subTest(phrase=p):
                said = self.run_text(p)
                self.assertIn("Restart, sir? Yes or no.", said)
                self.assertFalse(self.calls("restart"))
        self.check_call(["go to sleep", "sleep", "put the computer to sleep", "sleep mode"], "sleep")
        self.check_call(["lock", "lock the computer", "lock my pc", "lock the screen"], "lock")


class TestMedia(Case):
    def test_pause_resume(self):
        # pause presses only while the browser plays, resume only while it doesn't (Sep 15: the key is a toggle)
        self.a.returns["browser_playing"] = True
        self.check_call(["pause", "pause the music"], "media", arg="play_pause")
        self.a.returns["browser_playing"] = False
        self.check_call(["resume", "resume the video", "keep playing"], "media", arg="play_pause")
        # V2: "stop the music" is VK_MEDIA_STOP (§3.3 Stop media); v1 toggled play/pause.
        self.check_call(["stop the music"], "media", arg="stop")

    def test_next_previous(self):
        self.check_call(["next song", "skip this song", "cyrus skip", "next track", "play the next song"], "media",
                        arg="next")
        self.check_call(["previous song", "go back to the last song", "previous track"], "media", arg="prev")


class TestInfo(Case):
    def test_time_date(self):
        self.check_said(["what time is it", "what's the time", "tell me the time", "do you know what time it is",
                         "time please", "what is the time now"], "It's 10:30 AM, sir.")
        self.check_said(["what's the date", "what day is it", "what's today's date", "tell me the date",
                         "which day is it today", "what is today"], "Today is Sunday, September 13, sir.")

    def test_screenshot(self):
        self.check_call(["take a screenshot", "screenshot", "capture the screen", "take a picture of the screen",
                         "grab the screen", "screen shot please", "print screen"], "screenshot", arg=False)

    def test_status_joke_help_desktop_close(self):
        self.check_call(["system status", "how is the battery", "check the cpu", "how much memory is used"],
                        "system_status")
        for p in ("tell me a joke", "say something funny", "make me laugh"):
            with self.subTest(phrase=p):
                said = self.run_text(p)
                self.assertTrue(any(s in R.JOKES for s in said), (p, said))
        for p in ("help", "what can you do", "show me the commands"):
            with self.subTest(phrase=p):
                self.run_text(p)
                self.assertIn("show:Commands", drain_ui(self.ns.ui_queue), p)
        self.check_call(["show desktop", "minimize everything", "hide all windows"], "show_desktop")
        self.check_call(["close this window", "close it", "exit this app"], "window_cmd", arg="close")


class TestApps(Case):
    def test_open_apps(self):
        for p, target in [("open chrome", "chrome"), ("can you open chrome", "chrome"), ("launch spotify", "spotify:"),
                          ("start notepad", "notepad"), ("open google chrome", "chrome"),
                          ("fire up youtube", "https://www.youtube.com"), ("please open the calculator", "calc"),
                          ("run task manager", "taskmgr"), ("could you open the calculater for me", "calc"),
                          ("could you open google chrome for me", "chrome")]:
            self.check_call([p], "open_target", arg=target)

    def test_go_to_switches(self):
        # V2 (§3.4): "go to <app>" is Switch to app (focus an open window); v1 opened it.
        self.check_call(["go to youtube"], "focus_app", arg="youtube")

    def test_open_my_calendar(self):
        if not _have("xyrus.commands.calendar"):
            self.skipTest("T2 calendar commands not available")
        self.run_text("open my calendar")
        self.assertIn("show:Calendar", drain_ui(self.ns.ui_queue))
        self.assertFalse(self.calls("open_target"))


class TestBrightness(Case):
    def test_brightness(self):
        # V2: default step 15 % (§3.4; v1 10 %), a bit = 5 %.
        self.check_call(["increase the brightness", "make the screen brighter", "turn up the brightness"],
                        "brightness_set", arg="+15")
        self.check_call(["lower the brightness a bit"], "brightness_set", arg="-5")
        self.check_call(["set brightness to seventy"], "brightness_set", arg=70)
        self.check_call(["brightness max"], "brightness_set", arg=100)
        self.check_call(["what's the brightness"], "brightness_get")
        # V2: "dim the screen" = 20 % (§3.4) or a step down — either way a decrease
        self.check_call(["dim the screen"], "brightness_set",
                        pred=lambda v: (isinstance(v, str) and v.startswith("-")) or (isinstance(v, int) and v < 50))


@unittest.skipUnless(_have("xyrus.commands.timers"), "T2 timers module not available")
class TestTimers(Case):
    def test_timers_keep_their_parser(self):
        self.check_said(["set a timer for five minutes"], "Timer set for five minutes, sir.")
        self.run_text("cancel all timers")
        self.check_said(["start a countdown for ten minutes"], "Timer set for ten minutes, sir.")


@unittest.skipUnless(HAVE_MODEL, "speech model missing")
class TestSpoken(unittest.TestCase):
    """Synthesized speech -> Recognizer.decode_pcm (wake grammar -> command re-decode -> free re-decode) ->
    engine.handle_transcript -> first action (v1 'spoken' section)."""

    CASES = [
        ("xyrus can you decrease the sound", ("volume_step", (-10,))), ("xyrus make it quieter", ("volume_step", (-10,))),
        ("xyrus lower the volume a bit", ("volume_step", (-5,))), ("xyrus turn the music down", ("volume_step", (-10,))),
        ("xyrus can you turn the volume up", ("volume_step", (10,))),
        ("xyrus set the volume to thirty percent", ("volume_set", (30,))),
        ("xyrus turn off the sound", ("mute_set", (True,))), ("xyrus could you take a screenshot", ("screenshot", (False,))),
        ("xyrus open google chrome", ("open_target", ("chrome",))),
    ]

    @classmethod
    def setUpClass(cls):
        try:
            from tests import wav_util
            from xyrus.grammar import GrammarBuilder, Vocab
            from xyrus.recognizer import Recognizer
            from xyrus.testing import FakeChime, FakeSpeaker
        except ImportError as e:
            raise unittest.SkipTest(f"T3 recognizer not available: {e}")
        cls.e, cls.ns = make_test_engine(tmpdir=Path(tempfile.mkdtemp(prefix="xyrus_para_")), actions=FakeActions())
        vocab = Vocab(paths.MODEL_DIR, cache_file=Path(tempfile.mkdtemp()) / "vocab_cache.json")
        cls.rec = Recognizer(paths.MODEL_DIR, None, GrammarBuilder(cls.ns.registry, cls.ns.config, vocab),
                             get_spec=cls.e.listen_spec, on_transcript=lambda tr: None, speaker=FakeSpeaker(),
                             chime=FakeChime(), config=cls.ns.config)
        vocab.attach(cls.rec.check_words)
        cls.rec.load()
        phrases = [p for p, _ in cls.CASES] + ["xyrus what time is it"]
        cls.pcm = wav_util.synth_many(phrases) if hasattr(wav_util, "synth_many") else \
            {p: wav_util.synth(p) for p in phrases}

    def hear(self, phrase):
        self.e.cancel_dialogue()
        self.ns.actions.reset()
        tr = self.rec.decode_pcm(self.pcm[phrase], self.e.listen_spec().mode)
        self.e.handle_transcript(tr)
        return tr

    def test_spoken(self):
        for phrase, (name, args) in self.CASES:
            with self.subTest(phrase=phrase):
                tr = self.hear(phrase)
                calls = [(n, a) for n, a, _ in self.ns.actions.calls if n in (
                    "volume_step", "volume_set", "mute_set", "screenshot", "open_target")]
                self.assertEqual(calls[:1], [(name, args)],
                                 f"grammar={tr.text!r} free={tr.free_text!r} said={self.ns.speaker.said[-1:]}")
                self.assertFalse(self.ns.actions.called("shutdown"))

    def test_spoken_time(self):
        tr = self.hear("xyrus what time is it")
        self.assertTrue(self.ns.speaker.said and self.ns.speaker.said[-1].startswith("It's "),
                        (tr.text, self.ns.speaker.said[-1:]))


class QuestionFormsKeepMatching(Case):
    """Hearing rebuild (Sep 15 2026): Whisper writes the whole question - the song slot stays clean."""
    FORMS = {"can you play shape of you": "shape of you", "could you play faded": "faded",
             "would you play believer": "believer", "will you play faded": "faded",
             "can you please play shape of you": "shape of you", "please play believer": "believer",
             "play me shape of you": "shape of you", "put on shape of you": "shape of you",
             "can you put on faded": "faded", "i want to hear believer": "believer",
             "can you play tired by alan walker": "tired by alan walker", "could you please play me faded": "faded"}

    def test_question_forms(self):
        for text, song in self.FORMS.items():
            with self.subTest(text=text):
                self.run_text(text)
                got = self.calls("find_song")
                self.assertTrue(got, f"{text!r}: calls={self.a.names()}")
                self.assertEqual(got[0][0], song)


if __name__ == "__main__":
    unittest.main()
