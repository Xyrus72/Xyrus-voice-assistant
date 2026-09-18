"""Fixes found in the switch-over check (Sep 14 2026):
- the startup greeting said "Good evening" at 3:34 AM;
- a command given while a yes/no question was open ("remember that ...? " -> "volume up") was swallowed as a
  wrong answer instead of being run."""
import datetime as dt
import unittest

from xyrus import persona
from xyrus.testing import make_test_engine


class GreetingSmallHours(unittest.TestCase):
    def test_hello_before_five(self):
        self.assertEqual(persona.greeting(dt.datetime(2026, 9, 14, 3, 34), {}), "Hello, sir.")
        self.assertEqual(persona.greeting(dt.datetime(2026, 9, 14, 4, 59), {}), "Hello, sir.")

    def test_day_parts_unchanged(self):
        self.assertEqual(persona.greeting(dt.datetime(2026, 9, 14, 5, 0), {}), "Good morning, sir.")
        self.assertEqual(persona.greeting(dt.datetime(2026, 9, 14, 13, 0), {}), "Good afternoon, sir.")
        self.assertEqual(persona.greeting(dt.datetime(2026, 9, 14, 20, 0), {}), "Good evening, sir.")


class StartupMessageSmallHours(unittest.TestCase):
    """The spoken startup message comes from assistant.startup_summary, not persona.greeting."""

    def _summary(self, hour):
        import tempfile
        from pathlib import Path
        from xyrus import assistant
        from xyrus.store import CalendarStore
        store = CalendarStore(Path(tempfile.mkdtemp()) / "calendar.json")
        return assistant.startup_summary(dt.datetime(2026, 9, 14, hour, 34), store, [], {})

    def test_hello_at_3_am(self):
        self.assertEqual(self._summary(3), "Hello, sir. Xyrus online. Nothing on the calendar today.")

    def test_evening_unchanged(self):
        self.assertEqual(self._summary(20), "Good evening, sir. Xyrus online. Nothing on the calendar today.")


class NewCommandWhileAQuestionIsOpen(unittest.TestCase):
    def setUp(self):
        self.e, self.ns = make_test_engine()

    def _ask_remember(self):
        self.e.handle("remember that the spare keys are in the drawer", "typed")
        self.assertIsNotNone(self.e.dialogue, "remember should ask to confirm first")

    def test_command_drops_the_question_and_runs(self):
        self._ask_remember()
        self.e.handle("volume up", "typed")
        self.assertIsNone(self.e.dialogue, "the question should be dropped")
        self.assertTrue(self.ns.actions.called("volume_step"), self.ns.actions.names())

    def test_spoken_command_with_wake_word_too(self):
        self._ask_remember()
        self.e.handle("cyrus volume up", "mic")
        self.assertIsNone(self.e.dialogue)
        self.assertTrue(self.ns.actions.called("volume_step"), self.ns.actions.names())

    def test_yes_still_answers(self):
        self._ask_remember()
        self.e.handle("yes", "typed")
        self.assertIsNone(self.e.dialogue)
        self.assertFalse(self.ns.actions.called("volume_step"))
        self.assertTrue(self.ns.memory.all() if hasattr(self.ns.memory, "all") else True)

    def test_no_still_answers(self):
        self._ask_remember()
        self.e.handle("no", "typed")
        self.assertFalse(self.ns.actions.called("volume_step"))

    def test_free_text_answer_is_not_stolen(self):
        self.e.handle("add an event", "typed")
        d = self.e.dialogue
        self.assertIsNotNone(d, "add an event should ask for the title")
        self.e.handle("dentist appointment", "typed")
        self.assertIs(self.e.dialogue, d, "a title answer must stay with the question")
        self.assertEqual(d.slots.get("title"), "dentist appointment")


class CompoundDurations(unittest.TestCase):
    """Found by the hearing agent: 'one minute and thirty seconds' set a 30 s timer."""

    def test_compound(self):
        from xyrus.parsing import parse_duration
        cases = {"one minute and thirty seconds": 90, "an hour and a half": 5400, "1 hour 30 minutes": 5400,
                 "two hours and ten minutes": 7800, "one minute thirty": 90}
        for text, want in cases.items():
            with self.subTest(text=text):
                self.assertEqual(parse_duration(text.split()), want)

    def test_old_behaviour_kept(self):
        from xyrus.parsing import parse_duration
        cases = {"five minutes": 300, "four ten minutes": 600, "twenty five seconds": 25, "half an hour": 1800,
                 "a minute": 60, "seven": 420, "ninety seconds": 90}
        for text, want in cases.items():
            with self.subTest(text=text):
                self.assertEqual(parse_duration(text.split()), want)


class BanglaOnlyForBangla(unittest.TestCase):
    """A Hindi song must not be searched in Bengali script."""

    def test_hindi_stays_english_path(self):
        from xyrus.capture import bangla_likely
        self.assertFalse(bangla_likely({"hi": 0.8, "bn": 0.05, "en": 0.1}))
        self.assertFalse(bangla_likely({"hi": 0.55, "bn": 0.2, "en": 0.2}))

    def test_bangla_detected(self):
        from xyrus.capture import bangla_likely
        self.assertTrue(bangla_likely({"bn": 0.7, "hi": 0.2}))
        self.assertTrue(bangla_likely({"hi": 0.45, "bn": 0.35}))     # confused, but Bangla is strong
        self.assertTrue(bangla_likely({"as": 0.5, "bn": 0.2}))
        self.assertFalse(bangla_likely({"en": 0.9, "bn": 0.03}))


if __name__ == "__main__":
    unittest.main()
