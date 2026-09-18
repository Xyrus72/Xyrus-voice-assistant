"""parsing.py + normalize.py (§4.6, §5.6)."""
import datetime as dt
import math
import os
import tempfile
import unittest

os.environ.setdefault("XYRUS_DATA_DIR", tempfile.mkdtemp(prefix="xyrus_test_"))

from xyrus import normalize as N
from xyrus import parsing as P


def toks(s):
    return N.normalize(s).split()


class NumberTests(unittest.TestCase):
    def test_parse_number(self):
        cases = {"two hundred and fifty": 250, "three point five": 3.5, "twenty five": 25, "40": 40,
                 "seven": 7, "a hundred": 100, "one thousand two hundred": 1200, "zero": 0,
                 "point": None, "hello": None, "one two": None, "ninety nine": 99, "3.5": 3.5,
                 "two million": 2_000_000, "twelve": 12}
        for text, want in cases.items():
            with self.subTest(text=text):
                self.assertEqual(P.parse_number(text.split()), want)

    def test_numbers_in(self):
        self.assertEqual(P.numbers_in("pick one and ten".split()), [(1, 1), (3, 10)])
        self.assertEqual(P.numbers_in("twenty five and forty".split()), [(0, 25), (3, 40)])
        self.assertEqual(P.numbers_in("four seven".split()), [(0, 4), (1, 7)])

    def test_say_number(self):
        self.assertEqual(P.say_number(48.0), "48")
        self.assertEqual(P.say_number(3.5), "3.5")
        self.assertEqual(P.say_number(1 / 3), "0.333333")
        self.assertEqual(P.say_number(float("nan")), "undefined")


class DurationTests(unittest.TestCase):
    V1_TIMER_CASES = {   # v1 test_arc.py (E7)
        "set a timer for five minutes": 300, "timer twenty five seconds": 25, "timer four seven minutes": 420,
        "set timer four ten minutes": 600, "timer seven minutes": 420, "timer forty five seconds": 45,
        "timer half an hour": 1800, "timer one and a half hours": 5400, "timer a minute": 60,
        "timer two hours": 7200, "timer ninety seconds": 90, "set a timer for seven minutes": 420,
    }

    def test_v1_table(self):
        for text, want in self.V1_TIMER_CASES.items():
            with self.subTest(text=text):
                self.assertEqual(P.parse_duration(toks(text)), want)

    def test_spec_examples(self):
        cases = {"seven minutes": 420, "twenty five seconds": 25, "half an hour": 1800,
                 "one and a half hours": 5400, "a minute": 60, "four seven minutes": 420,
                 "ninety seconds": 90, "two hours": 7200, "four can minutes": 600, "5 minutes": 300,
                 "set a timer": 60}
        for text, want in cases.items():
            with self.subTest(text=text):
                self.assertEqual(P.parse_duration(toks(text)), want)

    def test_duration_slot_needs_a_value(self):
        env = P.SlotEnv(config=None, now=dt.datetime(2026, 9, 13, 10, 30))
        slot = P.SLOT_TYPES["duration"]
        self.assertIsNone(slot.parse(["set", "a"], env))
        self.assertEqual(slot.parse(["five", "minutes"], env), 300)
        self.assertEqual(slot.parse(["a", "minute"], env), 60)
        self.assertEqual(slot.parse(["five"], env), 300)


class PercentTests(unittest.TestCase):
    def test_percent(self):
        cases = {"forty percent": 40, "two forty": 40, "to forty": 40, "forty for set": 40,
                 "a hundred percent": 100, "seventy": 70, "two hundred": None, "zero": 0, "hello": None,
                 "the volume to forty percent": 40, "fifty five": 55, "two": 2}
        for text, want in cases.items():
            with self.subTest(text=text):
                self.assertEqual(P.parse_percent(text.split()), want)


class ExprTests(unittest.TestCase):
    def test_expr(self):
        cases = {"twelve times for": 48, "twelve times four": 48, "fifteen percent of eighty": 12,
                 "square root of nine": 3, "the square root of sixteen": 4, "two plus three times four": 14,
                 "three point five plus one": 4.5, "five squared": 25, "ten minus four": 6,
                 "a hundred divided by four": 25, "nine over three": 3, "six multiplied by seven": 42,
                 "twelve divided by for": 3}
        for text, want in cases.items():
            with self.subTest(text=text):
                self.assertAlmostEqual(P.parse_expr(text.split()), want)

    def test_expr_rejects(self):
        for text in ("love", "the time", "the date", "the volume", "plus", "", "times times"):
            with self.subTest(text=text):
                self.assertIsNone(P.parse_expr(text.split()))

    def test_division_by_zero(self):
        self.assertTrue(math.isnan(P.parse_expr("ten divided by zero".split())))


class NormalizeTests(unittest.TestCase):
    ROWS = [  # §5.6, in order
        ("hey cyrus open chrome", "cyrus open chrome"),
        ("hi cyrus", "cyrus"),
        ("okay cyrus what time is it", "cyrus what time is it"),
        ("whats the time", "what's the time"),
        ("remind me at five p m", "remind me at five pm"),
        ("at nine a m", "at nine am"),
        ("at seven o'clock", "at seven"),
        ("at seven oclock", "at seven"),
        ("un mute", "un mute"),
        ("sound on", "sound on"),
        ("turn the sound on", "turn the sound on"),
        ("add in event", "add an event"),
        ("add and event tomorrow", "add an event tomorrow"),
        ("add a event", "add an event"),
        ("remembered at the car is on level three", "remember that the car is on level three"),
        ("cyrus remember at", "cyrus remember that"),
        ("what did are ask you to remember", "what did i ask you to remember"),
        ("timer four can minutes", "timer for ten minutes"),
        ("add note four tomorrow", "add note for tomorrow"),
        ("set timer four ten minutes", "set timer for ten minutes"),
        ("cyrus shut down", "cyrus shut down"),
        ("What time is it?", "what time is it"),
        ("Xyrus, open Notepad.", "xyrus open notepad"),
        ("set volume to 40%", "set volume to 40 percent"),
        ("add a to-do buy milk", "add a to do buy milk"),
        ("what is 3.5 plus 1", "what is 3.5 plus 1"),
    ]

    def test_rows(self):
        for heard, want in self.ROWS:
            with self.subTest(heard=heard):
                self.assertEqual(N.normalize(heard), want)

    def test_two_too_to(self):
        rows = [("switch two chrome", "switch to chrome"), ("go two youtube", "go to youtube"),
                ("cyrus which to chrome", "cyrus switch to chrome"), ("which to chrome", "switch to chrome"),
                ("which day is it", "which day is it"),
                ("bring too chrome", "bring to chrome"), ("remind me two call mom", "remind me to call mom"),
                ("set timer two minutes", "set timer two minutes"), ("roll two dice", "roll two dice"),
                ("the next two weeks", "the next two weeks"), ("what is two plus two", "what is two plus two"),
                ("remind me at two tomorrow", "remind me at two tomorrow"), ("it's too loud", "it's too loud"),
                ("set volume two forty", "set volume two forty"), ("volume two", "volume two"),
                ("two and a half hours", "two and a half hours")]
        for heard, want in rows:
            with self.subTest(heard=heard):
                self.assertEqual(N.normalize(heard), want)

    def test_canonical(self):
        rows = {"can you lower the sound": "volume down", "the volume, lower it": "volume down",
                "lower the volume": "volume down", "volume down": "volume down",
                "decrease the sound please": "volume down", "turn it up": "volume up",
                "turn off the sound": "@mute", "turn off the computer": "shut down",
                "turn off the screen": "screen off", "what's volume": "volume level",
                "set the volume to forty": "set volume forty", "take a screenshot": "screenshot",
                "what time is it": "time", "launch spotify": "open spotify", "quit chrome": "close chrome"}
        for text, want in rows.items():
            with self.subTest(text=text):
                self.assertEqual(N.canonical(N.normalize(text)), want)
        self.assertEqual(N.canonical("turn off the lights"), "turn off lights")      # unknown object: no meaning
        self.assertEqual(N.step_modifier("lower the volume a bit".split()), "small")
        self.assertEqual(N.step_modifier("turn it up a lot".split()), "large")
        self.assertIsNone(N.step_modifier("volume down".split()))

    def test_synonym_words_exported(self):
        for w in ("lower", "decrease", "reduce", "raise", "increase", "sound", "audio", "launch", "quit",
                  "screenshot", "brightness"):
            self.assertIn(w, N.SYNONYM_WORDS)
        self.assertFalse(any(w.startswith("@") for w in N.SYNONYM_WORDS))

    def test_four_stays_a_number_elsewhere(self):
        self.assertEqual(N.normalize("timer four minutes"), "timer four minutes")
        self.assertEqual(N.normalize("volume four"), "volume four")

    def test_can_only_before_unit(self):
        self.assertEqual(N.normalize("can you hear me"), "can you hear me")

    def test_strip_wake(self):
        self.assertEqual(N.strip_wake("cyrus open chrome"), (True, "open chrome"))
        self.assertEqual(N.strip_wake("hey cyrus sleep"), (True, "sleep"))
        self.assertEqual(N.strip_wake("xyrus lock"), (True, "lock"))
        self.assertEqual(N.strip_wake("shut down"), (False, "shut down"))
        self.assertEqual(N.strip_wake("sirius play", ["sirius"]), (True, "play"))
        self.assertEqual(N.strip_wake("cyrus"), (True, ""))

    def test_song_helpers(self):
        self.assertEqual(N.clean_song_title("the song shape of you on youtube please"), "shape of you")
        self.assertEqual(N.clean_song_title("[unk] alone [unk]"), "alone")
        self.assertEqual(N.words_after_play("sirius lay alone".split()), ["alone"])
        self.assertIn("how", N.NOISE_WORDS)
        self.assertIn("some music", N.SONG_ASK)


class SlotTests(unittest.TestCase):
    def setUp(self):
        from xyrus.config import Config
        self.cfg = Config(os.path.join(tempfile.mkdtemp(), "config.json")).load()
        self.env = P.SlotEnv(config=self.cfg, now=dt.datetime(2026, 9, 13, 10, 30))

    def test_app(self):
        ref = P.SLOT_TYPES["app"].parse(["task", "manager"], self.env)
        self.assertEqual((ref.alias, ref.target), ("task manager", "taskmgr"))
        ref = P.SLOT_TYPES["app"].parse(["chrome"], self.env)
        self.assertEqual(ref.target, "chrome")
        ref = P.SLOT_TYPES["app"].parse(["photoshop"], self.env)
        self.assertEqual((ref.spoken, ref.alias, ref.target), ("photoshop", None, None))

    def test_app_index(self):
        from xyrus.testing import FakeAppIndex
        env = P.SlotEnv(config=self.cfg, now=self.env.now, app_index=FakeAppIndex({"obs studio": "C:/obs.lnk"}))
        ref = P.SLOT_TYPES["app"].parse(["obs", "studio"], env)
        self.assertEqual((ref.spoken, ref.target), ("obs studio", "C:/obs.lnk"))

    def test_song(self):
        s = P.SLOT_TYPES["song"]
        self.assertEqual(s.parse(["alone"], self.env), "alone")
        self.assertIsNone(s.parse(["some", "music"], self.env))
        self.assertIsNone(s.parse(["something"], self.env))
        self.assertIsNone(s.parse(["me", "something"], self.env))

    def test_name(self):
        self.assertEqual(P.SLOT_TYPES["name"].parse(["tea"], self.env), "tea")
        self.assertIsNone(P.SLOT_TYPES["name"].parse(["banana"], self.env))

    def test_table_shape(self):
        for name in ("duration", "percent", "number", "expr", "app", "song", "query", "text", "when", "day", "name"):
            st = P.SLOT_TYPES[name]
            self.assertEqual(st.name, name)
            self.assertIsInstance(st.words, frozenset)
            self.assertTrue(st.doc)
        self.assertIn("minutes", P.SLOT_TYPES["duration"].words)
        self.assertIn("percent", P.SLOT_TYPES["percent"].words)
        self.assertIn("tomorrow", P.SLOT_TYPES["when"].words)
        self.assertTrue(P.SLOT_TYPES["song"].free and not P.SLOT_TYPES["duration"].free)

    def test_when_and_day_delegate_to_dates(self):
        try:
            import xyrus.dates  # noqa: F401  (T2)
        except ImportError:
            self.skipTest("xyrus.dates (T2) not present yet")
        w = P.SLOT_TYPES["when"].parse("tomorrow at three pm".split(), self.env)
        self.assertEqual(w.start, dt.datetime(2026, 9, 14, 15, 0))
        start, end, label = P.SLOT_TYPES["day"].parse("this week".split(), self.env)
        self.assertLessEqual(start, end)


class SongCarriers(unittest.TestCase):
    """Whisper writes the whole request; the carrier words in front of the title are not the title."""

    def test_carriers_trimmed(self):
        cases = {"me shape of you": "shape of you", "please put on faded": "faded", "put on faded": "faded",
                 "listen to believer": "believer", "want to hear believer": "believer", "to believer": "believer",
                 "you put on faded": "faded", "you raise me up": "you raise me up", "you belong with me": "you belong with me", "shape of you": "shape of you", "tired by alan walker": "tired by alan walker"}
        for text, want in cases.items():
            with self.subTest(text=text):
                self.assertEqual(P._p_song(toks(text), None), want)


class AppNearMissBand(unittest.TestCase):
    """0.75 opens; 0.68-0.75 with a 0.10 margin asks; a longer word containing the alias always asks."""

    def setUp(self):
        from xyrus.config import DEFAULTS
        self.aliases = list(DEFAULTS["apps"]) + ["steam", "word"]

    def test_band(self):
        self.assertEqual(P.app_near_miss("note hat", self.aliases), (None, "notepad"))
        self.assertEqual(P.app_near_miss("crome", self.aliases), ("chrome", None))
        self.assertEqual(P.app_near_miss("stream", self.aliases), (None, "steam"))
        self.assertEqual(P.app_near_miss("world", self.aliases), (None, "word"))
        self.assertEqual(P.app_near_miss("zzzzqx", self.aliases), (None, None))

    def test_engine_asks_about_the_near_miss(self):
        from xyrus import persona
        from xyrus import replies as R
        from xyrus.testing import make_test_engine
        e, ns = make_test_engine()
        e.handle("open note hat", "typed")
        self.assertEqual(ns.speaker.said[-1], persona.render(R.DID_YOU_MEAN, ns.config, phrase="Notepad"))
        self.assertFalse(ns.actions.called("open_target"))
        e.handle("yes", "typed")
        self.assertIn(("notepad",), ns.actions.called("open_target"))

    def test_engine_opens_the_close_one(self):
        from xyrus.testing import make_test_engine
        e, ns = make_test_engine()
        e.handle("open crome", "typed")
        self.assertIn(("chrome",), ns.actions.called("open_target"))


if __name__ == "__main__":
    unittest.main()
