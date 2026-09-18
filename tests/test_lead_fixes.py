"""Fixes found in the switch-over check (Sep 14 2026):
- the startup greeting said "Good evening" at 3:34 AM;
- a command given while a yes/no question was open ("remember that ...? " -> "volume up") was swallowed as a
  wrong answer instead of being run."""
import datetime as dt
import unittest

from xyrus import paths, persona
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



class HandedOffWakeNeverLost(unittest.TestCase):
    """Review, Sep 15: a doubtful Vosk wake (every wake from the user's seat - peak 0.03-0.05 is below the 0.08
    confirm line) handed to the idle Whisper check was dropped when that idle utterance had begun earlier (TV,
    another sentence without a pause) and ran past WAKE_CHECK_MAX_* - no reply, nothing logged."""

    def make(self):
        from types import SimpleNamespace
        from tests.test_capture import FakeHearing
        from xyrus import recognizer as R

        class Grammar:
            registry = None

            def version(self):
                return 1

            def words(self, mode, extra):
                return []

        calls = []
        eng = SimpleNamespace(on_wake_with_command=lambda text, tr: calls.append(text))
        rec = R.Recognizer(paths.MODEL_DIR, None, Grammar(), lambda: R.ListenSpec("wake"), lambda tr: None,
                           SimpleNamespace(is_quiet_at=lambda t: True), SimpleNamespace(play=lambda k: None), {})
        rec.engine = eng
        rec.set_command_hearing(FakeHearing(en="Xyrus, what time is it?"))
        rec._redecode = lambda pcm, mode: {"text": "cyrus",
                                           "result": [{"word": "cyrus", "start": 0.5, "end": 0.9, "conf": 1.0}]}
        rec._free_for_command = lambda pcm, toks: None
        rec._idle_endpointer = SimpleNamespace(in_speech=True, reset=lambda: None)
        return rec, calls

    def test_long_breath_after_a_handed_off_wake_is_still_checked(self):
        from types import SimpleNamespace
        rec, calls = self.make()
        pcm = b"\x00\x10" * 16000 * 8                                     # 8 s
        res = {"text": "cyrus", "result": [{"word": "cyrus", "start": 0.5, "end": 0.9, "conf": 0.9}]}
        self.assertIsNone(rec._build(res, "wake", pcm[:64000], peak=0.04, t0=100.0, t1=102.0, live=True))
        self.assertTrue(rec._deferred, "a faint Vosk wake inside a breath is handed to the idle check")
        rec._deferred_at = 100.5                                           # what _process records
        utt = SimpleNamespace(speech_s=5.6, t_start=95.0, t_end=102.5, pcm=pcm, peak=0.04, reason="silence")
        rec._whisper_wake(utt, None)
        self.assertEqual(calls, ["what time is it"])
        self.assertEqual(rec.stats["wake_check_long"], 0)

    def test_long_talk_without_a_handed_off_wake_is_still_skipped(self):
        from types import SimpleNamespace
        rec, calls = self.make()
        utt = SimpleNamespace(speech_s=5.6, t_start=95.0, t_end=102.5, pcm=b"\x00\x10" * 16000 * 8, peak=0.04,
                              reason="silence")
        rec._whisper_wake(utt, None)
        self.assertEqual(calls, [])
        self.assertEqual(rec.stats["wake_check_long"], 1)


class StrictConfirmForDestructive(unittest.TestCase):
    """Review, Sep 15: Whisper writes "Okay." / "Yeah." on breath noise; a shutdown must never be confirmed by
    those alone from the mic. "yes" / "do it" / "go ahead" still confirm; typed "okay" still does; a harmless
    confirm still takes "okay"."""

    def setUp(self):
        self.e, self.ns = make_test_engine(config_overrides={"confirm_shutdown": True})

    def ask_shutdown(self):
        self.e.handle("shut down", "typed")
        self.assertIsNotNone(self.e.dialogue, "shut down asks first")
        self.assertTrue(getattr(self.e.dialogue, "strict", False))

    def shut(self):
        return self.ns.actions.called("shutdown") or self.ns.actions.called("shutdown_in")

    def test_spoken_okay_does_not_shut_down(self):
        self.ask_shutdown()
        self.e.handle("okay", "mic")
        self.assertFalse(self.shut())

    def test_captured_yeah_yeah_does_not_shut_down(self):
        from xyrus.capture import CapturedTranscript
        self.ask_shutdown()
        self.e.handle_transcript(CapturedTranscript(text="yeah yeah", mode="confirm", free_text="yeah yeah",
                                                    peak=0.04, raw="Yeah, yeah."))
        self.assertFalse(self.shut())

    def test_spoken_yes_shuts_down(self):
        self.ask_shutdown()
        self.e.handle("yes", "mic")
        self.e.tick() if hasattr(self.e, "tick") else None
        self.assertTrue(self.shut(), self.ns.actions.names())

    def test_typed_okay_still_confirms(self):
        self.ask_shutdown()
        self.e.handle("okay", "typed")
        self.assertTrue(self.shut(), self.ns.actions.names())

    def test_harmless_confirm_takes_okay(self):
        self.e.handle("remember that the spare keys are in the drawer", "typed")
        self.assertIsNotNone(self.e.dialogue)
        self.assertFalse(getattr(self.e.dialogue, "strict", True))
        self.e.handle("okay", "mic")
        self.assertIsNone(self.e.dialogue)


class FaintNameReadAsSirens(unittest.TestCase):
    """Wake bench, Sep 15: at peak 0.035 Whisper wrote the faint name of a one-breath request as "Sirens" ("Sirens
    play Shape of You by Ed Sheeran.") and 2/6 wakes were lost. A call when a request follows or Vosk heard the name
    in the same breath; a plain word otherwise."""

    def check(self, whisper_text, handed=False):
        from types import SimpleNamespace
        from tests.test_capture import FakeHearing
        from xyrus import recognizer as R

        class Grammar:
            registry = None

            def version(self):
                return 1

            def words(self, mode, extra):
                return []
        rec = R.Recognizer(paths.MODEL_DIR, None, Grammar(), lambda: R.ListenSpec("wake"), lambda tr: None,
                           SimpleNamespace(is_quiet_at=lambda t: True), SimpleNamespace(play=lambda k: None), {})
        rec.set_command_hearing(FakeHearing(en=whisper_text))
        return rec._whisper_name_check(bytes(32000), handed=handed)

    def test_sirens_before_a_request_is_the_name(self):
        ok, rest, _raw, _lp = self.check("Sirens play Shape of You by Ed Sheeran.")
        self.assertTrue(ok)
        self.assertEqual(rest, ["play", "shape", "of", "you", "by", "ed", "sheeran"])

    def test_sirens_when_vosk_heard_the_name_is_the_name(self):
        self.assertTrue(self.check("Sirens.", handed=True)[0])

    def test_sirens_as_a_word_is_not_a_call(self):
        for text in ("Sirens.", "Sirens are loud today.", "Siren going past."):
            with self.subTest(text=text):
                self.assertFalse(self.check(text)[0])

    def test_sound_alikes_still_rejected(self):
        for text in ("Serious play the song.", "Sirius play the song.", "Iris play the song."):
            with self.subTest(text=text):
                self.assertFalse(self.check(text)[0])

if __name__ == "__main__":
    unittest.main()
