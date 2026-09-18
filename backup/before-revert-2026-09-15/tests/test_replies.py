"""G6 (no wake alias in any reply; 'Xyrus' only in whitelisted templates), persona rules, persona helpers."""
import ast
import datetime as dt
import os
import re
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("XYRUS_DATA_DIR", tempfile.mkdtemp(prefix="xyrus_test_"))

from xyrus import persona
from xyrus import replies as R
from xyrus.config import DEFAULTS, Config

BASE = Path(__file__).resolve().parent.parent
ALIASES = set(DEFAULTS["wake_words"]) | {"cyrus", "zeros", "virus", "cirrus", "sirius", "serious"}
WAKE_RE = re.compile(r"\b(" + "|".join(sorted(ALIASES)) + r")\b", re.I)
NAME_RE = re.compile(r"\bxyrus\b", re.I)


def reply_strings():
    for name in dir(R):
        if name.startswith("_"):
            continue
        val = getattr(R, name)
        if isinstance(val, str):
            yield name, val
        elif isinstance(val, tuple) and all(isinstance(v, str) for v in val):
            for v in val:
                yield name, v


def said_literals():
    """Every string literal passed as the first argument of a .say(...) call anywhere in xyrus/."""
    for path in (BASE / "xyrus").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "say"
                    and node.args):
                arg = node.args[0]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    yield f"{path.name}:{node.lineno}", arg.value
                elif isinstance(arg, ast.JoinedStr):
                    text = "".join(v.value for v in arg.values if isinstance(v, ast.Constant))
                    yield f"{path.name}:{node.lineno}", text


class G6(unittest.TestCase):
    def test_no_wake_alias_in_replies(self):
        for name, text in reply_strings():
            with self.subTest(name=name):
                self.assertIsNone(WAKE_RE.search(text), f"{name}: {text!r}")

    def test_name_only_in_whitelisted_templates(self):
        for name, text in reply_strings():
            if name in R.NAME_WHITELIST or name == "NAME_WHITELIST":
                continue
            with self.subTest(name=name):
                self.assertIsNone(NAME_RE.search(text), f"{name}: {text!r}")
        for name in R.NAME_WHITELIST:
            self.assertRegex(getattr(R, name), NAME_RE)

    def test_no_wake_alias_in_any_said_literal(self):
        for where, text in said_literals():
            with self.subTest(where=where):
                self.assertIsNone(WAKE_RE.search(text), f"{where}: {text!r}")
                self.assertIsNone(NAME_RE.search(text), f"{where}: name outside the whitelist: {text!r}")

    def test_default_wake_replies_clean(self):
        for text in DEFAULTS["wake_replies"] + list(R.WAKE_REPLIES):
            self.assertIsNone(WAKE_RE.search(text))
            self.assertIsNone(NAME_RE.search(text))

    def test_instructions_never_say_the_wake_word(self):
        self.assertIn("Say cancel to stop it.", R.SHUTTING_DOWN)


class PersonaRules(unittest.TestCase):
    def test_no_exclamation_except_times_up(self):
        for name, text in reply_strings():
            if name in ("TIMES_UP", "JOKES"):
                continue
            with self.subTest(name=name):
                self.assertNotIn("!", text)
        self.assertEqual(R.TIMES_UP, "Time's up{sir}!")

    def test_spec_strings(self):
        self.assertEqual(R.NOT_HEARD, "Sorry, I didn't catch that{sir}.")
        self.assertEqual(R.DID_YOU_MEAN, "Did you mean {phrase}?")
        self.assertEqual(R.NUDGE, "Still there{sir}? ")
        self.assertGreaterEqual(len(R.JOKES), 20)
        self.assertEqual(len(set(R.JOKES)), len(R.JOKES))
        self.assertEqual(R.WAKE_REPLIES, tuple(DEFAULTS["wake_replies"]))

    def test_every_template_renders(self):
        cfg = Config(os.path.join(tempfile.mkdtemp(), "config.json")).load()
        for name, text in reply_strings():
            out = persona.render(text, cfg)
            self.assertNotIn("{sir}", out, name)

    def test_honorific(self):
        cfg = Config(os.path.join(tempfile.mkdtemp(), "config.json")).load()
        self.assertEqual(persona.render(R.NOT_HEARD, cfg), "Sorry, I didn't catch that, sir.")
        cfg.set("address_as", "")
        self.assertEqual(persona.render(R.NOT_HEARD, cfg), "Sorry, I didn't catch that.")
        cfg.set("address_as", "boss")
        self.assertEqual(persona.render(R.TIMER_SET, cfg, duration="five minutes"),
                         "Timer set for five minutes, boss.")

    def test_pick_never_repeats(self):
        cfg = Config(os.path.join(tempfile.mkdtemp(), "config.json")).load()
        prev = None
        for _ in range(50):
            cur = persona.pick("thanks-test", R.THANKS, cfg)
            self.assertNotEqual(cur, prev)
            prev = cur
        self.assertEqual(persona.pick("single", ("Only{sir}.",), cfg), "Only, sir.")


class PersonaHelpers(unittest.TestCase):
    NOW = dt.datetime(2026, 9, 13, 10, 30)       # Sunday

    def test_speak_time(self):
        cases = {dt.time(15, 0): "3 PM", dt.time(9, 30): "9:30 AM", dt.time(12, 0): "noon",
                 dt.time(0, 0): "midnight", dt.time(12, 30): "12:30 PM", dt.time(0, 5): "12:05 AM"}
        for t, want in cases.items():
            self.assertEqual(persona.speak_time(t), want)

    def test_speak_duration(self):
        self.assertEqual(persona.speak_duration(420), "seven minutes")
        self.assertEqual(persona.speak_duration(25), "twenty five seconds")
        self.assertEqual(persona.speak_duration(5400), "one hour and thirty minutes")
        self.assertEqual(persona.speak_duration(60), "one minute")
        self.assertEqual(persona.speak_duration(3600), "one hour")
        self.assertEqual(persona.speak_duration(70), "one minute and ten seconds")
        self.assertEqual(persona.speak_duration(3725), "one hour, two minutes and five seconds")

    def test_speak_left(self):
        self.assertEqual(persona.speak_left(220), "three minutes forty")
        self.assertEqual(persona.speak_left(180), "three minutes")
        self.assertEqual(persona.speak_left(40), "forty seconds")

    def test_speak_when(self):
        n = self.NOW
        self.assertEqual(persona.speak_when(dt.datetime(2026, 9, 13, 0, 0), True, n), "today")
        self.assertEqual(persona.speak_when(dt.datetime(2026, 9, 14, 15, 0), False, n), "tomorrow at 3 PM")
        self.assertEqual(persona.speak_when(dt.datetime(2026, 9, 18, 0, 0), True, n), "Friday")
        self.assertEqual(persona.speak_when(dt.datetime(2026, 9, 25, 0, 0), True, n), "Friday the 25th")
        self.assertEqual(persona.speak_when(dt.datetime(2026, 10, 3, 0, 0), True, n), "October 3rd")
        self.assertEqual(persona.speak_when(dt.datetime(2026, 9, 13, 12, 30), False, n),
                         "today at 12:30 PM, in two hours")
        self.assertEqual(persona.speak_when(dt.datetime(2026, 9, 13, 11, 13), False, n),
                         "today at 11:13 AM, in forty five minutes")
        self.assertEqual(persona.speak_when(dt.datetime(2026, 9, 13, 10, 45), False, n),
                         "today at 10:45 AM, in fifteen minutes")
        self.assertEqual(persona.speak_when(dt.datetime(2026, 9, 13, 12, 30), False, n, relative=False),
                         "today at 12:30 PM")

    def test_greeting(self):
        cfg = Config(os.path.join(tempfile.mkdtemp(), "config.json")).load()
        self.assertEqual(persona.greeting(dt.datetime(2026, 9, 13, 20, 0), cfg), "Good evening, sir.")
        cfg.set("user_name", "Refath")
        self.assertEqual(persona.greeting(dt.datetime(2026, 9, 13, 8, 0), cfg), "Good morning, Refath.")
        self.assertEqual(persona.part_of_day(dt.datetime(2026, 9, 13, 15, 0)), "afternoon")


class HearingRebuildReplies(unittest.TestCase):
    def test_new_replies_render(self):
        cfg = Config(os.path.join(tempfile.mkdtemp(), "config.json")).load()
        self.assertEqual(persona.render(R.GO_AHEAD, cfg), "Go ahead, sir.")
        self.assertEqual(persona.render(R.SAY_AGAIN, cfg), "Say that again, sir?")
        self.assertEqual(persona.render(R.CONFIRM_POWER_HEARD, cfg, phrase="go to sleep"),
                         "Did you say go to sleep? Yes or no.")
        self.assertEqual(persona.render(R.PLAYING_MAYBE, cfg, title="Faded"),
                         'Playing Faded - say "not that one" if it is wrong, sir.')
        self.assertEqual(persona.render(R.PLAY_TITLE_Q, cfg, phrase="stand by me"), "Play stand by me?")


if __name__ == "__main__":
    unittest.main()
