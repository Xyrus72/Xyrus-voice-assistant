"""T6 - pluggable free-text hearing (INTEGRATION_NOTES "Whisper"): Recognizer.set_free_decoder(fn) is used for
free TEXT only (song titles, "what should I play?" answers, search queries, calendar titles/notes); commands,
<app> names and the wake word stay on Vosk; None / "" / an exception falls back to the Vosk free decode.
app.plug_hearing wires the lead's hearing.py and never breaks startup. No test here loads Whisper.

Run: venv\\Scripts\\python.exe -m unittest tests.test_free_decoder -v
"""
from __future__ import annotations

import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

if not os.environ.get("XYRUS_DATA_DIR"):
    os.environ["XYRUS_DATA_DIR"] = tempfile.mkdtemp(prefix="xyrus_test_")

from xyrus import app as A
from xyrus import paths
from xyrus.grammar import GrammarBuilder, Vocab
from xyrus.interfaces import Transcript
from xyrus.recognizer import Recognizer
from xyrus.testing import FakeActions, FakeChime, FakeSpeaker, make_test_engine

HAVE_MODEL = (paths.MODEL_DIR / "am" / "final.mdl").exists()
PCM = b"\x10\x27" * 16000                     # 1 s of a loud constant tone (peak ~0.3)


class Decoder:
    """Stand-in for Whisper: records the PCM it got, answers `text` (or raises it when it is an exception)."""

    def __init__(self, text):
        self.text = text
        self.calls: list[bytes] = []

    def __call__(self, pcm: bytes):
        self.calls.append(pcm)
        if isinstance(self.text, BaseException):
            raise self.text
        return self.text


def make_rec(engine=None, ns=None):
    if engine is None:
        engine, ns = make_test_engine(tmpdir=Path(tempfile.mkdtemp(prefix="xyrus_fd_")))
    vocab = Vocab(paths.MODEL_DIR, cache_file=Path(tempfile.mkdtemp()) / "vocab_cache.json")
    rec = Recognizer(paths.MODEL_DIR, None, GrammarBuilder(ns.registry, ns.config, vocab),
                     get_spec=engine.listen_spec, on_transcript=lambda tr: None, speaker=FakeSpeaker(),
                     chime=FakeChime(), config=ns.config)
    return rec, engine, ns


class RoutingWithoutModel(unittest.TestCase):
    """Vosk re-decodes are stubbed: 'vosk words' is what the unrestricted Vosk recognizer would have said."""

    def setUp(self):
        self.rec, self.engine, self.ns = make_rec()
        self.vosk_calls: list[str] = []

        def redecode(pcm, mode):
            self.vosk_calls.append(mode)
            return {"text": "vosk words"}
        self.rec._redecode = redecode
        self.rec._app_redecode = lambda pcm, toks: None            # no Start Menu / app grammar here

    def free_for(self, text):
        return self.rec._free_for_command(PCM, text.split())

    def test_song_uses_the_plugged_decoder(self):
        dec = Decoder("Xyrus, play Ishwar by Vikings.")
        self.rec.set_free_decoder(dec)
        self.assertEqual(self.free_for("cyrus play [unk] [unk]"), "xyrus play ishwar by vikings")
        self.assertEqual(dec.calls, [PCM], "the decoder gets the whole utterance PCM")
        self.assertEqual(self.vosk_calls, [], "no Vosk free decode when the plugged one answered")
        self.assertEqual(self.rec.stats["plugged_free"], 1)

    def test_calendar_and_search_text_use_it_too(self):
        for text, heard in (("cyrus remind me [unk] tomorrow", "Xyrus, remind me to call Anika tomorrow."),
                            ("cyrus add a note for tomorrow [unk]", "Xyrus add a note for tomorrow: bring the charger"),
                            ("cyrus search for [unk]", "Xyrus, search for Rabindra Sangeet."),
                            ("cyrus add [unk] to my to do list", "Xyrus, add Ishwar tickets to my to-do list.")):
            with self.subTest(text=text):
                dec = Decoder(heard)
                self.rec.set_free_decoder(dec)
                self.assertEqual(self.free_for(text), Recognizer.clean_free(heard))   # full text: unchanged
                self.assertEqual(len(dec.calls), 1)

    def test_title_only_text_gets_the_trigger_back(self):
        self.rec.set_free_decoder(Decoder("Ishwar by Vikings"))
        self.assertEqual(self.free_for("cyrus play [unk] [unk]"), "cyrus play ishwar by vikings")
        self.rec.set_free_decoder(Decoder("call Anika tomorrow at five"))
        got = self.free_for("cyrus remind me [unk] tomorrow")
        self.assertTrue(got.startswith("cyrus remind") and got.endswith("call anika tomorrow at five"), got)
        self.rec.set_free_decoder(Decoder("Sirius, lay Ishwar by Vikings"))       # a play-like word is enough
        self.assertEqual(self.free_for("cyrus play [unk]"), "sirius lay ishwar by vikings")

    def test_fallback_to_vosk(self):
        for answer in (None, "", "  ", RuntimeError("whisper broke")):
            with self.subTest(answer=answer):
                self.vosk_calls.clear()
                self.rec.set_free_decoder(Decoder(answer))
                self.assertEqual(self.free_for("cyrus play [unk]"), "vosk words")
                self.assertEqual(self.vosk_calls, ["free"])

    def test_unplugged_is_plain_vosk(self):
        self.rec.set_free_decoder(None)
        self.assertEqual(self.free_for("cyrus play [unk]"), "vosk words")

    def test_commands_and_apps_stay_on_vosk(self):
        dec = Decoder("never used")
        self.rec.set_free_decoder(dec)
        self.assertIsNone(self.free_for("cyrus volume up"))                    # a command: no free decode at all
        self.assertEqual(self.free_for("cyrus open [unk]"), "vosk words")      # <app> fallback: Vosk, not Whisper
        self.assertEqual(self.free_for("cyrus close [unk]"), "vosk words")
        self.assertEqual(dec.calls, [])

    def test_free_mode_answers(self):
        dec = Decoder("Ishwar by Vikings")
        self.rec.set_free_decoder(dec)
        tr = self.rec._build({"text": "is war by vikings"}, "free", PCM, peak=0.3, t0=1.0, t1=2.0)
        self.assertEqual((tr.mode, tr.free_text, tr.text), ("free", "ishwar by vikings", "ishwar by vikings"))
        # room noise never reaches Whisper: a lone noise word, or a near-silent utterance
        for raw, peak in (("the", 0.3), ("huh", 0.3), ("blinding lights", 0.001)):
            with self.subTest(raw=raw):
                n = len(dec.calls)
                tr = self.rec._build({"text": raw}, "free", PCM, peak=peak, t0=1.0, t1=2.0)
                self.assertEqual(tr.free_text, raw)
                self.assertEqual(len(dec.calls), n)
        dec.text = None                                                        # not loaded yet: Vosk's text
        tr = self.rec._build({"text": "blinding lights"}, "free", PCM, peak=0.3, t0=1.0, t1=2.0)
        self.assertEqual(tr.free_text, "blinding lights")

    def test_engine_retry_hook(self):
        dec = Decoder("xyrus please play ishwar by vikings")
        self.rec.set_free_decoder(dec)
        got = []
        self.rec._remember(PCM, 5.0, 6.0)
        tr = Transcript(text="cyrus please could play [unk]", mode="command", t_start=5.0, t_end=6.0)
        self.assertTrue(self.rec.request_free_decode(tr, got.append))          # inline: T-rec isn't running
        self.assertEqual(got, ["xyrus please play ishwar by vikings"])
        got.clear()
        tr2 = Transcript(text="cyrus [unk] [unk]", mode="command", t_start=5.0, t_end=6.0)
        self.assertTrue(self.rec.request_free_decode(tr2, got.append))         # meaning-layer retry: Vosk
        self.assertEqual(got, ["vosk words"])
        self.assertEqual(len(dec.calls), 1)

    def test_song_reaches_play_song(self):
        """The recognizer's free_text (from the plugged decoder) becomes the <song> slot."""
        dec = Decoder("ishwar by vikings")
        self.rec.set_free_decoder(dec)
        free = self.free_for("cyrus play [unk] [unk]")
        self.engine.handle_transcript(Transcript(text="cyrus play [unk] [unk]", mode="command", free_text=free,
                                                 peak=0.3))
        self.assertEqual(self.ns.actions.called("find_song"), [("ishwar by vikings",)])


class PlugHearing(unittest.TestCase):
    def setUp(self):
        self.rec, _, _ = make_rec()
        self._env = mock.patch.dict(os.environ, {}, clear=False)
        self._env.start()
        os.environ.pop(A.NO_HEARING_ENV, None)

    def tearDown(self):
        self._env.stop()

    def fake_hearing(self, available=True, load_raises=False):
        mod = types.ModuleType("hearing")
        mod.loads = 0

        def load_async():
            mod.loads += 1
            if load_raises:
                raise RuntimeError("no runtime")
        mod.load_async = load_async
        mod.available = lambda wait=0.0: available
        mod.transcribe = lambda pcm, prompt=None, language="en": "Xyrus, play Ishwar by Vikings."
        mod.clean = lambda text: Recognizer.clean_free(text)
        return mod

    def test_available_whisper_is_plugged(self):
        mod = self.fake_hearing()
        with mock.patch.dict(sys.modules, {"hearing": mod}):
            self.assertIs(A.plug_hearing(self.rec), mod)
        self.assertEqual(mod.loads, 1)
        self.assertEqual(self.rec._free_decoder(PCM), "xyrus play ishwar by vikings")

    def test_not_loaded_yet_means_vosk(self):
        mod = self.fake_hearing(available=False)
        with mock.patch.dict(sys.modules, {"hearing": mod}):
            A.plug_hearing(self.rec)
        self.assertIsNone(self.rec._free_decoder(PCM))

    def test_missing_or_broken_never_breaks_startup(self):
        with mock.patch.dict(sys.modules, {"hearing": None}):                 # import hearing -> ImportError
            self.assertIsNone(A.plug_hearing(self.rec))
        self.assertIsNone(self.rec._free_decoder)
        with mock.patch.dict(sys.modules, {"hearing": self.fake_hearing(load_raises=True)}):
            self.assertIsNone(A.plug_hearing(self.rec))
        self.assertIsNone(self.rec._free_decoder)

    def test_env_switch(self):
        os.environ[A.NO_HEARING_ENV] = "1"
        with mock.patch.dict(sys.modules, {"hearing": self.fake_hearing()}):
            self.assertIsNone(A.plug_hearing(self.rec))
        self.assertIsNone(self.rec._free_decoder)


@unittest.skipUnless(HAVE_MODEL, "speech model missing")
class SpokenWithFakeWhisper(unittest.TestCase):
    """Real synthesized speech through the recognizer's final-result path with a fake Whisper:
    "xyrus play ishwar by vikings" -> play_song("ishwar by vikings")."""

    @classmethod
    def setUpClass(cls):
        from tests import wav_util
        cls.pcm = wav_util.synth_many(["xyrus play ishwar by vikings", "ishwar by vikings",
                                       "xyrus play some music"])

    def setUp(self):
        self.engine, self.ns = make_test_engine(tmpdir=Path(tempfile.mkdtemp(prefix="xyrus_fd_")),
                                                actions=FakeActions())
        self.rec, _, _ = make_rec(self.engine, self.ns)
        self.rec.grammar.vocab.attach(self.rec.check_words)

    def test_one_breath_song(self):
        dec = Decoder("ishwar by vikings")
        self.rec.set_free_decoder(dec)
        tr = self.rec.decode_pcm(self.pcm["xyrus play ishwar by vikings"], "wake")
        self.assertTrue(tr.wake_redecoded, tr)
        self.assertTrue(tr.free_text.endswith(" ishwar by vikings"), tr)       # anchored: "cyrus play ishwar …"
        self.assertEqual(len(dec.calls), 1)
        self.engine.handle_transcript(tr)
        self.assertEqual(self.ns.actions.called("find_song"), [("ishwar by vikings",)], self.ns.speaker.said)

    def test_what_should_i_play_answer(self):
        dec = Decoder(None)                                  # Whisper not loaded for the question itself
        self.rec.set_free_decoder(dec)
        self.engine.handle_transcript(self.rec.decode_pcm(self.pcm["xyrus play some music"], "wake"))
        self.assertEqual(self.ns.speaker.said[-1], "What should I play, sir?")
        self.assertEqual(self.engine.listen_spec().mode, "free")
        dec.text = "Ishwar, by Vikings!"
        tr = self.rec.decode_pcm(self.pcm["ishwar by vikings"], "free")
        self.assertEqual(tr.free_text, "ishwar by vikings")
        self.engine.handle_transcript(tr)
        self.assertEqual(self.ns.actions.called("find_song"), [("ishwar by vikings",)])


if __name__ == "__main__":
    unittest.main()
