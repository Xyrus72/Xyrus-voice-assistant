"""T3 - grammar.py: word lists per listening mode (§4.12, §5.2, D1) and the vocabulary check (D13)."""
from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from tests.wav_util import DEFAULT_APPS, StubConfig, StubRegistry
from xyrus.grammar import UNK, GrammarBuilder, Vocab

MODEL_DIR = Path(__file__).resolve().parent.parent / "model"
D13_UNKNOWN = ["unmute", "unmuted", "whatsapp", "backspace", "hotkey", "lofi", "vlc", "oclock", "xyrus",
               "a.m.", "p.m."]


def fake_checker(unknown=("unmute", "kubernetes", "whatsapp")):
    def check(words):
        check.calls.append(list(words))
        return {w: w not in unknown for w in words}
    check.calls = []
    return check


class TempDataDir(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="xyrus_t3_")
        self._old_env = os.environ.get("XYRUS_DATA_DIR")
        os.environ["XYRUS_DATA_DIR"] = self.tmp
        self.cache = Path(self.tmp) / "vocab_cache.json"

    def tearDown(self):
        if self._old_env is None:
            os.environ.pop("XYRUS_DATA_DIR", None)
        else:
            os.environ["XYRUS_DATA_DIR"] = self._old_env
        shutil.rmtree(self.tmp, ignore_errors=True)


class GrammarModes(TempDataDir):
    def setUp(self):
        super().setUp()
        self.checker = fake_checker()
        self.vocab = Vocab(MODEL_DIR, cache_file=self.cache, checker=self.checker)
        self.reg = StubRegistry()
        self.cfg = StubConfig()
        self.gb = GrammarBuilder(self.reg, self.cfg, self.vocab)

    def test_idle_is_wake_only(self):                       # D1
        self.assertEqual(self.gb.words("wake"), ["cirrus", "cyrus", "virus", "zeros", UNK])

    def test_wake_extra_words(self):                        # timer / shutdown-countdown context words (§5.2)
        w = self.gb.words("wake", ["cancel", "stop", "timer", "the all"])
        for word in ("cancel", "stop", "timer", "the", "all", "cyrus"):
            self.assertIn(word, w)
        self.assertNotIn("volume", w)

    def test_wake_words_follow_config(self):
        self.cfg.set("wake_words", ["cyrus", "sirius"])
        self.assertEqual(self.gb.words("wake"), ["cyrus", "sirius", UNK])

    def test_free_and_paused_have_no_grammar(self):
        self.assertEqual(self.gb.words("free"), [])
        self.assertEqual(self.gb.words("paused"), [])

    def test_lists_are_sorted_unique_and_end_with_unk(self):
        for mode in ("wake", "command", "confirm", "when"):
            w = self.gb.words(mode, ["first"])
            self.assertEqual(w[-1], UNK, mode)
            self.assertEqual(w.count(UNK), 1, mode)
            self.assertEqual(w[:-1], sorted(set(w[:-1])), mode)
            json.dumps(w)                                    # JSON-ready

    def test_command_grammar_contents(self):
        w = set(self.gb.words("command"))
        literal = {"volume", "shutdown", "screenshot", "brightness", "snap", "joke", "remind", "calendar"}
        slot = {"seven", "twenty", "minutes", "percent", "hundred", "point", "times", "divided", "tea"}
        apps = {"chrome", "task", "manager", "recycle", "bin", "notepad", "bluetooth"}
        date_answer = {"tomorrow", "friday", "october", "yes", "no", "cancel", "correct", "nope"}
        wake = {"cyrus", "zeros", "virus", "cirrus"}
        for group in (literal, slot, apps, date_answer, wake):
            self.assertLessEqual(group, w, group - w)

    def test_answer_and_synonym_words_come_from_normalize(self):
        from xyrus import normalize as N
        from xyrus.grammar import _split_all
        answers = _split_all(N.YES_WORDS) | _split_all(N.NO_WORDS) | _split_all(N.CANCEL_WORDS)
        self.assertLessEqual(answers, set(self.gb.words("command")))
        self.assertLessEqual(answers, set(self.gb.words("confirm")))
        self.assertLessEqual(_split_all(N.CANCEL_WORDS), set(self.gb.words("when")))
        synonyms = GrammarBuilder.synonym_words()
        self.assertLessEqual(synonyms, set(self.gb.raw_words("command")))
        if not synonyms:
            self.skipTest("normalize exports no SYNONYM_WORDS / SYNONYMS yet")

    def test_slot_words_come_from_parsing(self):
        from xyrus.grammar import _split_all
        from xyrus.parsing import SLOT_TYPES
        raw = self.gb.raw_words("command")
        for name in self.reg.slot_types_used():
            if name in SLOT_TYPES:
                self.assertLessEqual(_split_all(SLOT_TYPES[name].words), raw, name)

    def test_app_grammar_phrases(self):
        grammar, to_name = self.gb.app_grammar(["Google Chrome", "OBS Studio", "WhatsApp Desktop", "chrome"],
                                               ["open", "switch to"])
        self.assertEqual(grammar[-1], UNK)
        for entry in ("cyrus", "open", "switch", "to", "google chrome", "obs studio", "desktop", "chrome"):
            self.assertIn(entry, grammar)
        self.assertNotIn("whatsapp desktop", grammar)            # the unknown word is removed from the phrase
        self.assertNotIn("volume", grammar)
        self.assertEqual(to_name["desktop"], "whatsapp desktop")
        self.assertEqual(to_name["obs studio"], "obs studio")
        self.assertEqual(self.gb.app_grammar(["obs studio"], ["open"])[0][-2:], ["obs studio", UNK])

    def test_command_grammar_has_custom_phrase_words(self):
        self.cfg.set("custom_commands", [{"phrase": "movie mode", "enabled": True, "steps": []}])
        self.assertIn("movie", self.gb.words("command"))

    def test_confirm_grammar(self):
        w = set(self.gb.words("confirm", ["first", "second"]))
        self.assertLessEqual({"yes", "no", "cancel", "correct", "yeah", "nope", "title", "date", "day", "time",
                              "change", "the", "first", "second", "cyrus"}, w)
        self.assertNotIn("volume", w)
        self.assertNotIn("tomorrow", w)

    def test_when_grammar(self):
        w = set(self.gb.words("when"))
        self.assertLessEqual({"tomorrow", "friday", "october", "pm", "am", "at", "in", "fifteenth", "forty", "five",
                              "minutes", "next", "all", "any", "no", "time", "cancel", "cyrus"}, w)
        self.assertNotIn("volume", w)

    def test_unknown_words_are_dropped_and_logged_once(self):
        self.cfg.set("custom_commands", [{"phrase": "kubernetes mode", "enabled": True, "steps": []}])
        with self.assertLogs("xyrus.grammar", logging.WARNING) as cm:
            w1 = self.gb.words("command")
            w2 = self.gb.words("command", ["first"])
        self.assertNotIn("kubernetes", w1)
        self.assertNotIn("kubernetes", w2)
        self.assertIn("mode", w1)
        self.assertEqual(self.gb.dropped("command"), ["kubernetes"])
        self.assertEqual(sum("kubernetes" in m for m in cm.output), 1, cm.output)

    def test_version_changes_rebuild_the_words(self):
        v1 = self.gb.version()
        self.assertNotIn("steam", self.gb.words("command"))
        self.cfg.set("apps", dict(DEFAULT_APPS, steam="steam"))
        self.assertNotEqual(self.gb.version(), v1)
        self.assertIn("steam", self.gb.words("command"))
        v2 = self.gb.version()
        self.reg.bump()
        self.assertNotEqual(self.gb.version(), v2)

    def test_words_are_memoised_one_checker_round_trip(self):
        self.gb.words("command")
        calls = len(self.checker.calls)
        self.assertEqual(calls, 1)                           # one batch for the whole command set
        self.gb.words("command")
        self.gb.words("wake")                                # already cached words -> no new call
        self.assertEqual(len(self.checker.calls), calls)


class VocabCache(TempDataDir):
    def test_cache_file_format_and_reload_without_checker(self):
        v = Vocab(MODEL_DIR, cache_file=self.cache, checker=fake_checker())
        self.assertFalse(v.known("unmute"))
        self.assertTrue(v.known("volume"))
        data = json.loads(self.cache.read_text(encoding="utf8"))
        self.assertEqual(data["model"], str(MODEL_DIR))
        self.assertEqual(data["words"], {"unmute": False, "volume": True})
        self.assertEqual([p.name for p in Path(self.tmp).iterdir()], ["vocab_cache.json"])   # no .tmp left (G7)
        offline = Vocab(MODEL_DIR, cache_file=self.cache)      # UI validation with no recognizer
        self.assertFalse(offline.known("unmute"))
        self.assertTrue(offline.known("volume"))

    def test_cache_is_keyed_by_model_path(self):
        Vocab(MODEL_DIR, cache_file=self.cache, checker=fake_checker()).known("unmute")
        other = Vocab(Path(self.tmp) / "other_model", cache_file=self.cache)
        self.assertTrue(other.known("unmute"))                # not verified for that model -> not dropped

    def test_unverified_words_count_as_known_and_are_not_cached(self):
        v = Vocab(MODEL_DIR, cache_file=self.cache)
        self.assertTrue(v.known("zzzq"))
        self.assertFalse(self.cache.exists())

    def test_checker_failure_keeps_words(self):
        def broken(words):
            raise TimeoutError("recognizer busy")
        v = Vocab(MODEL_DIR, cache_file=self.cache, checker=broken)
        with self.assertLogs("xyrus.grammar", logging.WARNING):
            self.assertEqual(v.unknown_words("open whatsapp"), [])
        self.assertFalse(self.cache.exists())

    def test_unknown_words_of_a_phrase(self):
        v = Vocab(MODEL_DIR, cache_file=self.cache, checker=fake_checker())
        self.assertEqual(v.unknown_words("movie mode kubernetes"), ["kubernetes"])
        self.assertEqual(v.unknown_words("Open WhatsApp, please!"), ["whatsapp"])
        self.assertEqual(v.unknown_words("[unk] volume"), [])


class RealModelVocabulary(TempDataDir):
    """The real model through Recognizer.check_words (no audio device)."""

    @classmethod
    def setUpClass(cls):
        from xyrus.recognizer import Recognizer
        cls.rec = Recognizer(MODEL_DIR, capture=None, grammar=None, get_spec=lambda: None,
                             on_transcript=lambda tr: None, speaker=None, chime=None, config=StubConfig())

    def test_d13_words_are_not_in_the_model(self):
        known = self.rec.check_words(D13_UNKNOWN)
        self.assertEqual([w for w in D13_UNKNOWN if known[w]], [])

    def test_wake_aliases_and_replacements_are_in_the_model(self):
        words = ["cyrus", "zeros", "virus", "cirrus", "un", "mute", "sound", "on", "what's", "am", "pm",
                 "minimise", "goodnight", "screenshot", "youtube"]
        known = self.rec.check_words(words)
        self.assertEqual([w for w in words if not known[w]], [])

    def test_grammar_builder_drops_d13_words_via_the_model(self):
        vocab = Vocab(MODEL_DIR, cache_file=self.cache, checker=self.rec.check_words)
        cfg = StubConfig(custom_commands=[{"phrase": "unmute whatsapp", "enabled": True, "steps": []}])
        gb = GrammarBuilder(StubRegistry(), cfg, vocab)
        with self.assertLogs("xyrus.grammar", logging.WARNING):
            w = gb.words("command")
        self.assertNotIn("unmute", w)
        self.assertNotIn("whatsapp", w)
        self.assertEqual(gb.dropped("command"), ["unmute", "whatsapp"])
        self.assertEqual(vocab.unknown_words("turn the sound on"), [])


if __name__ == "__main__":
    unittest.main()
