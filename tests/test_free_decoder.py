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


# ============================================================================ hearing rebuild (Sep 15 2026)
from array import array                                     # noqa: E402

from tests.wav_util import StubConfig                       # noqa: E402
from xyrus import capture as C                              # noqa: E402
from xyrus.interfaces import ListenSpec                     # noqa: E402
from xyrus import recognizer as R                           # noqa: E402


class CannedHeard:
    """hearing.Heard with canned answers: English text + avg_logprob + fallback + n-best, the primary model's
    Bengali hypothesis, the detected language. Every decode is logged in `calls`."""

    def __init__(self, en="", lp=-0.1, fb=False, nbest=(), bn="", bn_lp=-0.3, detected=("en", 0.99, [("en", 0.99)]),
                 calls=None):
        self.en, self.lp, self.fb, self.nbest = en, lp, fb, list(nbest)
        self.bn, self.bn_lp, self.detected = bn, bn_lp, detected
        self.calls = calls if calls is not None else []
        self.avg_logprob, self.fallback_used = None, False

    def text(self, language="en", prompt="<default>", min_logprob=None):
        self.calls.append(("text", language, prompt, min_logprob))
        if language == "bn":
            self.avg_logprob, self.fallback_used = self.bn_lp, False
            return self.bn
        self.avg_logprob, self.fallback_used = self.lp, self.fb
        if min_logprob is not None and self.lp < min_logprob:
            return ""
        return self.en

    def text_nbest(self, language="en", prompt=None, n=5):
        self.calls.append(("nbest", language))
        if language == "bn":
            return [(self.bn, self.bn_lp)] if self.bn else []
        return ([(self.en, self.lp)] + self.nbest)[:n]

    def language(self):
        self.calls.append(("language",))
        return self.detected


class CannedHearing:
    """The plugged command hearing (app.CommandHearing surface + listen_bn / bn_available)."""

    def __init__(self, specialist=None, **heard):
        self.heard = heard
        self.calls: list = []
        self.pcms: list[bytes] = []
        self.bn_pcms: list[bytes] = []
        self.specialist = specialist                 # a CannedHeard for the Bengali model, or None

    def available(self):
        return True

    def listen(self, pcm):
        self.pcms.append(pcm)
        return CannedHeard(calls=self.calls, **self.heard)

    def bn_available(self):
        return self.specialist is not None

    def listen_bn(self, pcm):
        self.bn_pcms.append(pcm)
        return self.specialist

    def bn_decodes(self):
        return [c for c in self.calls if c[0] in ("text", "nbest") and c[1] == "bn"]


class Hooks:
    """An engine with the rebuild's hooks (interfaces.ON_CAPTURE_TIMEOUT / ON_WAKE_WITH_COMMAND)."""

    def __init__(self):
        self.woke: list[tuple[str, Transcript]] = []
        self.timeouts = 0

    def on_wake_with_command(self, rest, tr):
        self.woke.append((rest, tr))

    def on_capture_timeout(self):
        self.timeouts += 1


def bare_rec(hearing=None, config=None, got=None, spec=ListenSpec("command", generation=1), grammar=None):
    """A recognizer for the capture path / unit calls - no Vosk model is touched."""
    import types
    got = [] if got is None else got
    rec = Recognizer(paths.MODEL_DIR, None, grammar or types.SimpleNamespace(version=lambda: 1),
                     get_spec=lambda: spec, on_transcript=got.append, speaker=FakeSpeaker(), chime=FakeChime(),
                     config=config or StubConfig())
    if hearing is not None:
        rec.set_command_hearing(hearing)
    return rec, got


def utt(pcm=PCM, peak=0.3, vad=1.0, speech=1.0):
    return C.Utterance(pcm=pcm, t_start=10.0, t_end=11.0, peak=peak, speech_s=speech, reason="silence",
                       vad_speech_s=vad)


class HearCommandGates(unittest.TestCase):
    """_hear_command: what Whisper wrote -> dropped silently, or a CapturedTranscript with what it knew."""

    def hear(self, mode="command", u=None, config=None, **heard):
        h = CannedHearing(**heard)
        rec, _ = bare_rec(h, config=config)
        return rec._hear_command(u or utt(), ListenSpec(mode, generation=1)), rec, h

    def assertDropped(self, reason, **kw):
        tr, rec, _ = self.hear(**kw)
        self.assertIsNone(tr, tr)
        self.assertEqual(dict(rec.drop_reasons), {reason: 1})

    def test_the_drops(self):
        self.assertDropped("empty", en="")
        self.assertDropped("filler", en="Thank you.")
        self.assertDropped("repeated", en="Bye. Bye. Bye. Bye.")
        self.assertDropped("filler", en="I'm going to call the doctor tomorrow at five.")      # the old prompt leaked
        self.assertDropped("short_faint", en="Go to sleep.", u=utt(peak=0.03, vad=0.3))

    def test_short_or_faint_alone_is_kept(self):
        for u in (utt(peak=0.3, vad=0.3), utt(peak=0.03, vad=0.6)):
            tr, _, _ = self.hear(en="Go to sleep.", u=u)
            self.assertIsNotNone(tr)
            self.assertEqual(tr.vad_speech_s, u.vad_speech_s)

    def test_energy_path_uses_speech_seconds(self):
        tr, _, _ = self.hear(en="Volume up.", u=utt(peak=0.04, vad=0.0, speech=0.8))     # no VAD: speech_s
        self.assertIsNotNone(tr)

    def test_low_logprob_is_logged_not_dropped_unless_enforced(self):
        tr, _, h = self.hear(en="Volume up.", lp=-1.2)
        self.assertEqual((tr.text, tr.confidence), ("volume up", -1.2))
        self.assertIsNone(h.calls[0][3], "min_logprob not passed by default")
        tr, rec, h = self.hear(en="Volume up.", lp=-1.2, config=StubConfig(logprob_enforce=True))
        self.assertIsNone(tr)
        self.assertEqual(h.calls[0][3], -0.9)
        self.assertEqual(dict(rec.drop_reasons), {"empty": 1})

    def test_fields_and_alternates(self):
        tr, _, h = self.hear(en="Play Tired by Ellen Hook.", lp=-0.2, fb=True,
                             nbest=[("Play Tired by Alan Walker.", -0.3), ("Play tired by Ellen hook.", -0.4),
                                    ("Thank you.", -0.5), ("Bye. Bye. Bye. Bye.", -0.6)])
        self.assertTrue(tr.captured)
        # same words as the kept text, fillers and loops are not alternates
        self.assertEqual(tr.alternates, ("Play Tired by Alan Walker.",))
        self.assertIn(("nbest", "en"), h.calls)
        self.assertEqual((tr.confidence, tr.fallback_used, tr.vad_speech_s), (-0.2, True, 1.0))
        self.assertEqual(tr.free_text, "play tired by ellen hook")

    def test_answers_are_never_filler_in_confirm_and_when(self):
        for mode in ("confirm", "when"):
            for answer in ("Yeah.", "Okay.", "No.", "Yes."):
                with self.subTest(mode=mode, answer=answer):
                    tr, _, _ = self.hear(mode=mode, en=answer, u=utt(peak=0.03, vad=0.2))
                    self.assertIsNotNone(tr)
                    self.assertEqual(tr.mode, mode)
        self.assertDropped("filler", en="Yeah.")                                  # command mode: filler
        tr, _, _ = self.hear(mode="confirm", en="")
        self.assertIsNone(tr)


class BanglaTrigger(unittest.TestCase):
    """bn is decoded exactly when the request is a song AND (lp < -0.30 or a fallback or Bangla detected); the
    Bengali specialist only when the primary model's Bengali is unusable."""
    BN = ("bn", 0.7, [("bn", 0.7), ("en", 0.2)])

    def hear(self, en, specialist=None, **kw):
        h = CannedHearing(specialist=specialist, en=en, **kw)
        rec, _ = bare_rec(h)
        return rec._hear_command(utt(), ListenSpec("command", generation=1)), h

    def test_trigger_table(self):
        rows = [  # en, lp, fallback, detected, bn decoded?
            ("Play Ishwar by Vikings.", -0.5, False, None, True),
            ("Play Ishwar by Vikings.", -0.1, True, None, True),
            ("Play Ishwar by Vikings.", -0.1, False, self.BN, True),
            ("Play shape of you.", -0.05, False, None, False),
            ("Shape of You by Ed Sheeran.", -0.5, False, None, True),       # a bare title is a song too
            ("What time is it?", -0.8, True, self.BN, False),               # not a song: never
        ]
        for en, lp, fb, detected, want in rows:
            with self.subTest(en=en, lp=lp, fb=fb, detected=bool(detected)):
                kw = {"detected": detected} if detected else {}
                tr, h = self.hear(en, lp=lp, fb=fb, bn="ঈশ্বর", bn_lp=lp, **kw)
                self.assertEqual(bool(h.bn_decodes()), want, h.calls)
                self.assertEqual(len(h.bn_decodes()), 1 if want else 0)
                self.assertEqual(tr.bn_text, "ঈশ্বর" if want else None)
                self.assertEqual(tr.text.split()[:1] != [], True)

    def test_garbage_bengali_is_rejected(self):
        for bn in ("কে স্র স্র স্র", "PLAY ISHVAR BY VIKINGS", "াাাাাাাা", ""):
            with self.subTest(bn=bn):
                tr, h = self.hear("Play if shown you by icons.", lp=-0.6, bn=bn, bn_lp=-0.5)
                self.assertEqual(len(h.bn_decodes()), 1)
                self.assertIsNone(tr.bn_text)
                self.assertEqual(tr.language, "en")
        tr, _ = self.hear("Play if shown you by icons.", lp=-0.6, bn="ঈশ্বর", bn_lp=-0.5)
        self.assertEqual(tr.bn_text, "ঈশ্বর")

    def test_specialist_only_when_the_primary_fails(self):
        special = CannedHeard(bn="ঈশ্বর", bn_lp=-0.4)
        tr, h = self.hear("Play if shown you by icons.", specialist=special, lp=-0.6, bn="াাাাাাাা")
        self.assertEqual((tr.bn_text, len(h.bn_pcms)), ("ঈশ্বর", 1))
        tr, h = self.hear("Play if shown you by icons.", specialist=special, lp=-0.6, bn="ঈশ্বর")
        self.assertEqual((tr.bn_text, len(h.bn_pcms)), ("ঈশ্বর", 0))
        tr, h = self.hear("Play if shown you by icons.", specialist=CannedHeard(bn="াাাাাাাা"), lp=-0.6,
                          bn="কে স্র স্র স্র")
        self.assertIsNone(tr.bn_text)

    def test_language_check_path_rejects_noise_too(self):
        rec, _ = bare_rec(CannedHearing())
        noisy = CannedHeard(detected=self.BN, bn="াাাাাাাা")
        self.assertIsNone(rec._bangla_text(noisy))
        self.assertEqual(rec._bangla_text(CannedHeard(detected=self.BN, bn="আমার ভিনদেশী তারা বাজাও", bn_lp=-0.3,
                                                      lp=-0.4)), "আমার ভিনদেশী তারা বাজাও")


class WakeDecisions(unittest.TestCase):
    """The Whisper name check, the wake + command hand-off, and the Vosk wake confirmation (no Vosk model:
    the command re-decode is stubbed)."""

    def rec(self, en, lp=-0.1, hooks=True):
        h = CannedHearing(en=en, lp=lp)
        rec, got = bare_rec(h, config=StubConfig(), spec=ListenSpec("wake"))
        rec.grammar.words = lambda mode, extra=(): []
        rec.engine = Hooks() if hooks else object()
        return rec, got, h

    def check(self, en, lp=-0.1):
        rec, _, _ = self.rec(en, lp)
        return rec._whisper_name_check(PCM)

    def test_name_check(self):
        for en, lp, ok in (("Xyrus.", -0.2, True), ("Hey Xyrus, what time is it?", -0.3, True),
                           ("Hayes, Xyrus.", -0.3, True), ("Zyra's.", -0.3, True), ("Xyrus's", -0.3, True),
                           ("Serious.", -0.5, False), ("Serious.", -0.05, False), ("Sirius.", -0.05, False),
                           ("Cirrus.", -0.35, False), ("Cirrus.", -0.2, True), ("Zyrus.", -0.55, True),
                           ("Virus.", -0.1, False), ("Iris.", -0.1, False), ("Xyrus.", -1.0, False),
                           ("Thank you.", -0.2, False), ("", -0.2, False)):
            with self.subTest(en=en, lp=lp):
                self.assertEqual(self.check(en, lp)[0], ok)

    def test_wake_with_command_hands_the_exact_rest_to_the_engine(self):
        rec, got, _ = self.rec("")
        self.assertIsNone(rec._deliver_wake("take a screenshot".split(), -0.2, "Xyrus, take a screenshot.", 0.05,
                                            1.0, 2.0))
        (rest, tr), = rec.engine.woke
        self.assertEqual(rest, "take a screenshot")
        self.assertTrue(tr.wake_redecoded)

    def test_without_the_hook_the_one_breath_transcript(self):
        rec, _, _ = self.rec("", hooks=False)
        tr = rec._deliver_wake("take a screenshot".split(), -0.2, "Xyrus, take a screenshot.", 0.05, 1.0, 2.0)
        self.assertEqual((tr.mode, tr.text.split()[0]), ("command", "cyrus"))
        self.assertIn("screenshot", tr.text)

    def test_bare_wake_when_unsure_or_short(self):
        for after, lp in (("take a screenshot".split(), -0.8), (["stop"], -0.1), (["[unk]", "[unk]"], -0.1),
                          ([], -0.1)):
            with self.subTest(after=after, lp=lp):
                rec, _, _ = self.rec("")
                tr = rec._deliver_wake(after, lp, "x", 0.05, 1.0, 2.0)
                self.assertEqual((tr.text, tr.mode), ("cyrus", "command"))
                self.assertEqual(rec.engine.woke, [])

    def build(self, rec, cmd_text, conf=1.0, peak=0.3, wake="cyrus [unk]", span=(0.5, 1.9)):
        words = [{"word": w, "start": span[0], "end": span[1], "conf": 1.0} for w in wake.split()]
        rec._redecode = lambda pcm, mode: {"text": cmd_text, "result": [
            {"word": w, "start": 0.5, "end": 1.0, "conf": conf} for w in cmd_text.split()]}
        rec._free_for_command = lambda pcm, toks: None
        return rec._build({"text": wake, "result": words}, "wake", PCM, peak=peak, t0=5.0, t1=7.0, live=True)

    def test_vosk_conf_gate(self):
        """'white undo top match' (mean conf 0.3) is never run from the grammar: Whisper decides."""
        rec, got, h = self.rec("Xyrus, what time is it?")
        self.assertIsNone(self.build(rec, "cyrus white undo top match", conf=0.3))
        self.assertEqual([r for r, _ in rec.engine.woke], ["what time is it"])
        rec, _, h = self.rec("White undo top match.")
        self.assertIsNone(self.build(rec, "cyrus white undo top match", conf=0.3))
        self.assertEqual((rec.engine.woke, rec.stats["wake_rejected"]), ([], 1))

    def test_doubtful_vosk_wakes_are_confirmed(self):
        cases = {"[unk] padded": dict(cmd_text="cyrus [unk] take any screenshot zeros"),
                 "longer than 3 s": dict(cmd_text="cyrus", span=(0.2, 3.6)),
                 "sound-alike": dict(cmd_text="zeros", wake="zeros")}
        for name, kw in cases.items():
            with self.subTest(name):
                rec, _, h = self.rec("Volume up.")
                self.assertIsNone(self.build(rec, **kw))                       # Whisper: not the name
                self.assertEqual(len(h.pcms), 1)
                rec, _, h = self.rec("Xyrus.")
                tr = self.build(rec, **kw)
                self.assertEqual(tr.text, "cyrus")

    def test_faint_clear_name_is_heard_by_whisper_but_never_dropped(self):
        """Sep 15: only "faint" / "name lost" -> Whisper hears it (better command text), but an unsure "no" keeps
        the wake: from a distance it misspelled the real name ("Hi, Rus.") and calls were lost."""
        rec, _, h = self.rec("Hi, Rus.", lp=-1.0)
        tr = self.build(rec, cmd_text="cyrus", peak=0.05)
        self.assertEqual(len(h.pcms), 1, "Whisper was asked")
        self.assertIsNotNone(tr, "kept")
        self.assertIn("cyrus", tr.text)
        rec, _, h = self.rec("Xyrus.")
        self.assertEqual(self.build(rec, cmd_text="cyrus", peak=0.05).text, "cyrus")
        rec, _, h = self.rec("Here we go.", lp=-0.1)             # Whisper was sure it was something else
        self.assertIsNone(self.build(rec, cmd_text="cyrus", peak=0.05))

    def test_name_after_two_lead_in_words(self):
        rec, _, h = self.rec("Hi, I'm Xyrus.")
        self.assertIsNotNone(self.build(rec, cmd_text="cyrus", peak=0.05))

    def test_short_clean_wake_keeps_the_fast_path(self):
        rec, _, h = self.rec("never used")
        tr = self.build(rec, "cyrus what time is it", wake="cyrus [unk]")
        self.assertEqual((tr.text, h.pcms), ("cyrus what time is it", []))
        rec.set_command_hearing(None)                                          # no Whisper: as before
        self.assertIn("undo", self.build(rec, "cyrus white undo top match", conf=0.3).text)
        rec2, _, _ = self.rec("never used")
        rec2._cfg = lambda k, d=None: False if k == "wake_confirm" else StubConfig().get(k, d)
        self.assertIn("undo", self.build(rec2, "cyrus white undo top match", conf=0.3).text)

    def test_idle_check_length_bounds(self):
        rec, _, h = self.rec("Xyrus.")
        for speech, dur, checked in ((4.5, 6.5, True), (5.5, 6.5, False), (3.0, 7.5, False)):
            u = C.Utterance(pcm=PCM, t_start=100.0 * speech, t_end=100.0 * speech + dur, peak=0.05,
                            speech_s=speech, reason="silence")
            n = len(h.pcms)
            rec._whisper_wake(u)
            self.assertEqual(len(h.pcms) - n, int(checked), (speech, dur))

    def test_late_final_keeps_the_endpointer(self):
        from unittest import mock
        rec, _, _ = self.rec("")
        rec._idle_endpointer = mock.Mock()
        self.assertTrue(rec._claim_wake(100.0, 106.0, speech=(100.2, 101.0), now=106.0))   # 5 s old
        rec._idle_endpointer.reset.assert_not_called()
        self.assertTrue(rec._claim_wake(110.0, 112.0, speech=(110.2, 111.0), now=112.0))
        rec._idle_endpointer.reset.assert_called_once()


class Trimming(unittest.TestCase):
    def test_trim_to_words(self):
        from xyrus.interfaces import Word
        pcm = b"\x01\x00" * (16000 * 20)
        cut, start = R.trim_to_words(pcm, (Word("cyrus", 12.0, 12.6, 1.0), Word("[unk]", 12.6, 13.5, 1.0)))
        self.assertAlmostEqual(start, 11.7, places=3)
        self.assertAlmostEqual(len(cut) / 32000, 13.8 - 11.7, places=3)
        cut, _ = R.trim_to_words(pcm, (Word("cyrus", 1.0, 1.5, 1.0), Word("[unk]", 1.5, 19.0, 1.0)))
        self.assertLessEqual(len(cut) / 32000, R.REDECODE_MAX_S)
        cut, start = R.trim_to_words(pcm, ())
        self.assertEqual((len(cut) / 32000, start), (R.REDECODE_MAX_S, 14.0))

    def test_every_redecode_is_short(self):
        rec, _ = bare_rec(CannedHearing(en="Xyrus open chrome"))
        long_pcm = b"\x10\x27" * (16000 * 20)
        rec._remember(long_pcm, 1.0, 2.0)
        self.assertLessEqual(len(rec._recent[-1][2]) / 32000, 6.6)
        rec._whisper_retry(long_pcm)
        self.assertLessEqual(len(rec._command_hearing.pcms[-1]) / 32000, 6.6)


class CapturePath(unittest.TestCase):
    """The armed capture: the arm timeout (off by default), the echo gate, speech held across the engine's
    ack. Synthesized speech, the energy / VAD Endpointer; the Vosk model only after an arm timeout."""

    @classmethod
    def setUpClass(cls):
        from tests import wav_util as W
        cls.W = W
        cls.pcm = W.synth_many(["volume up", "what time is it"])

    def play(self, rec, pcm, t):
        for c in self.W.chunks(pcm):
            t += len(c) / 32000
            rec._process(c, t)
        return t

    def test_arm_timeout_is_off_by_default(self):
        from xyrus.config import DEFAULTS
        self.assertEqual(float(DEFAULTS["arm_timeout_s"]), 0.0)
        rec, got = bare_rec(CannedHearing(en="Volume up."))
        rec.engine = hooks = Hooks()
        self.play(rec, self.W.hiss(40, seed=1), 100.0)
        self.assertEqual((hooks.timeouts, rec.stats["arm_timeouts"]), (0, 0))

    @unittest.skipUnless(HAVE_MODEL, "speech model missing")
    def test_arm_timeout_fires_once_at_the_limit(self):
        spec = {"s": ListenSpec("command", generation=1)}
        engine, ns = make_test_engine(tmpdir=Path(tempfile.mkdtemp(prefix="xyrus_fd_")))
        vocab = Vocab(paths.MODEL_DIR, cache_file=Path(tempfile.mkdtemp()) / "vocab_cache.json")
        got = []
        rec = Recognizer(paths.MODEL_DIR, None, GrammarBuilder(ns.registry, ns.config, vocab),
                         get_spec=lambda: spec["s"], on_transcript=got.append, speaker=FakeSpeaker(),
                         chime=FakeChime(), config=StubConfig(arm_timeout_s=8))
        rec.set_command_hearing(CannedHearing(en="Volume up."))
        fired = []
        hooks = Hooks()
        hooks.on_capture_timeout = lambda: fired.append(t_now[0])
        rec.engine = hooks
        t_now = [100.0]
        for c in self.W.chunks(self.W.hiss(12, seed=2)):
            t_now[0] += len(c) / 32000
            rec._process(c, t_now[0])
        self.assertEqual(len(fired), 1, fired)
        self.assertGreaterEqual(fired[0] - 100.0, 8.0)
        self.assertLessEqual(fired[0] - 100.0, 8.3)
        self.assertEqual(rec._cap_state, "")
        spec["s"] = ListenSpec("command", generation=2)                        # re-armed: the clock restarts
        for c in self.W.chunks(self.W.hiss(9, seed=3)):
            t_now[0] += len(c) / 32000
            rec._process(c, t_now[0])
        self.assertEqual(len(fired), 2)
        self.assertGreaterEqual(fired[1] - fired[0], 8.0)

    def test_speech_during_the_ack_wait_is_kept(self):
        W = self.W
        h = CannedHearing(en="Volume up.")
        rec, got = bare_rec(h)
        rec.ack_required = True
        cmd = W.synth("what time is it")
        t = self.play(rec, W.silence(1) + self.pcm["volume up"] + W.silence(1.6), 100.0)
        self.assertEqual(len(got), 1)
        half = len(cmd) // 4 * 2
        t = self.play(rec, cmd[:half], t)                                     # the user starts before the ack
        rec.capture_ack()
        self.play(rec, cmd[half:] + W.silence(2), t)
        self.assertEqual(len(got), 2)
        a = array("h")
        a.frombytes(cmd)
        loud = [i for i, v in enumerate(a) if abs(v) > 0.02 * 32768]
        speech_s = (loud[-1] - loud[0]) / 16000
        self.assertGreaterEqual(len(h.pcms[1]) / 32000, speech_s, "the start of the answer is in")
        self.assertLessEqual(len(h.pcms[1]) / 32000, 6.6)

    def test_speech_that_ended_before_the_ack_is_not_replayed(self):
        W = self.W
        h = CannedHearing(en="Volume up.")
        rec, got = bare_rec(h)
        rec.ack_required = True
        t = self.play(rec, W.silence(1) + self.pcm["volume up"] + W.silence(1.6), 100.0)
        t = self.play(rec, self.pcm["volume up"] + W.silence(1.5), t)
        rec.capture_ack()
        self.play(rec, W.silence(2), t)
        self.assertEqual(len(got), 1)

    def test_echo_gate_observes_and_resets_once(self):
        from unittest import mock
        W = self.W
        rec, got = bare_rec(CannedHearing(en="Volume up."))
        ep = rec._ep()
        loud = [True]
        rec.speaker = mock.Mock(is_quiet_at=lambda t: not loud[0])
        with mock.patch.object(ep, "reset", wraps=ep.reset) as reset, \
                mock.patch.object(ep, "observe", wraps=ep.observe) as observe:
            t = self.play(rec, W.silence(2), 100.0)
            self.assertEqual((reset.call_count, observe.call_count), (0, 8))
            loud[0] = False
            self.play(rec, W.silence(1) + self.pcm["volume up"] + W.silence(1.6), t)
            self.assertEqual(reset.call_count, 1)
        self.assertEqual(len(got), 1)


if __name__ == "__main__":
    unittest.main()
