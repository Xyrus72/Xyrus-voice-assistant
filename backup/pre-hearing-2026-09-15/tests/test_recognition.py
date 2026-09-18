"""T3 - recognizer.py + audio.py. §7.4 recognition with synthesized speech (Recognizer.decode_pcm, same code
path as the mic), the T-rec loop fed through a fake capture queue, and AudioCapture with a fake PortAudio
stream (FakeMic) plus one short real-microphone check.

Uses the real REGISTRY + commands when T1/T4 have landed (set XYRUS_STUB_REGISTRY=1 to force the stub);
otherwise the §3 phrase table in tests/wav_util.py."""
from __future__ import annotations

import datetime as dt
import os
import shutil
import statistics
import tempfile
import threading
import time
import unittest
import wave
from pathlib import Path

import sounddevice as sd

from tests import wav_util as W
from tests.wav_util import FakeCapture, GateSpeaker, RecordingChime, StubConfig, StubRegistry, song_title
from xyrus import audio
from xyrus.grammar import GrammarBuilder, Vocab
from xyrus.interfaces import ListenSpec, Transcript
from xyrus.recognizer import MAX_UTT_BYTES, Recognizer

MODEL_DIR = Path(__file__).resolve().parent.parent / "model"
WAKE = {"cyrus", "zeros", "virus", "cirrus"}

# §7.4.1 - the v1 test_arc.py phrases ("yes" is a confirm-mode answer in v2) ...
V1_PHRASES = ["xyrus shut down", "xyrus sleep", "xyrus screen off", "xyrus wake up", "xyrus cancel",
              "xyrus volume up", "xyrus open chrome", "xyrus open youtube", "xyrus set a timer for five minutes",
              "xyrus what time is it", "xyrus take a screenshot", "xyrus tell me a joke", "xyrus play",
              "xyrus set a timer for seven minutes", "xyrus timer twelve minutes", "xyrus timer thirty seconds"]
# ... plus one or more phrases for every other must command of §3.
MORE_PHRASES = [
    "xyrus restart", "xyrus lock the computer", "xyrus go to sleep", "xyrus cancel the shutdown",
    "xyrus turn off the screen", "xyrus volume down", "xyrus set the volume to forty percent", "xyrus volume max",
    "xyrus what's the volume", "xyrus mute", "xyrus sound on", "xyrus pause the music", "xyrus next track",
    "xyrus previous song", "xyrus show desktop", "xyrus close this window", "xyrus minimize this",
    "xyrus maximize this", "xyrus snap left", "xyrus snap right", "xyrus switch to chrome", "xyrus close notepad",
    "xyrus open task manager", "xyrus open calculator", "xyrus open settings", "xyrus what's the date",
    "xyrus system status", "xyrus how much time is left", "xyrus cancel the timer", "xyrus stop listening",
    "xyrus what can you do", "xyrus repeat that", "xyrus what did you hear", "xyrus add an event",
    "xyrus remind me", "xyrus take a note", "xyrus read my notes", "xyrus what's next", "xyrus show my calendar",
    "xyrus what do i have today", "xyrus read my to do list", "xyrus undo that", "xyrus good morning",
    "xyrus good night", "xyrus remember that", "xyrus what do you remember", "xyrus thank you",
    "xyrus who are you", "xyrus set a timer for ten minutes", "xyrus timer one and a half hours",
    "xyrus search for cheap keyboards", "xyrus play some music"]
SONGS = [("xyrus play alone", "alone"), ("xyrus play shape of you", "shape of you"),
         ("xyrus play believer by imagine dragons", "believer by imagine dragons"),
         ("xyrus play faded by alan walker", "faded by alan walker"),
         ("xyrus play blinding lights", "blinding lights")]
WHEN_PHRASES = ["tomorrow at three pm", "next friday at ten am", "the fifteenth of october", "in forty five minutes"]
CONFIRM_PHRASES = ["yes", "no", "cancel", "correct"]
FREE_PHRASES = ["dentist appointment", "buy milk and eggs"]

class StubStartMenu:
    """T4's StartMenuIndex surface (names, lookup: exact, difflib 0.75, unique prefix) over fixed names."""
    NAMES = ("google chrome", "obs studio", "visual studio code", "steam", "whatsapp", "zoom")

    def names(self):
        return sorted(self.NAMES)

    def lookup(self, spoken):
        import difflib
        q = " ".join(str(spoken).lower().split())
        if not q:
            return None
        hit = q if q in self.NAMES else next(iter(difflib.get_close_matches(q, self.NAMES, n=1, cutoff=0.75)), None)
        if hit is None:
            starts = [n for n in self.NAMES if n.startswith(q + " ")]
            hit = starts[0] if len(starts) == 1 else None
        return (hit, f"C:/StartMenu/{hit}.lnk") if hit else None


START_MENU = StubStartMenu()
APP_CASES = [("xyrus switch to chrome", "switch_to", "chrome"), ("xyrus open google chrome", "open_app", "chrome"),
             ("xyrus close spotify", "close_app", "spotify"), ("xyrus open obs studio", "open_app", "obs studio")]

TMP = ""
_OLD_ENV = None
CFG = MATCH = GB = VOCAB = REC = None


def strip_wake(text: str) -> tuple[bool, str]:
    try:
        from xyrus.normalize import strip_wake as real
        return real(text, WAKE)
    except Exception:
        toks = text.split()
        for i, t in enumerate(toks):
            if t in WAKE:
                return True, " ".join(toks[i + 1:])
        return False, text


class Matcher:
    """registry.match -> command name, over the real registry when it exists, else the stub."""

    def __init__(self, cfg):
        self.kind, self.registry, self.env = "stub", StubRegistry(), None
        if os.environ.get("XYRUS_STUB_REGISTRY"):
            return
        try:
            from xyrus import commands
            from xyrus.parsing import SlotEnv
            from xyrus.registry import REGISTRY
            if not REGISTRY.commands():
                commands.load_all(REGISTRY)
            self.env = SlotEnv(config=cfg, now=dt.datetime(2026, 9, 13, 10, 30), app_index=START_MENU)
            self.registry, self.kind = REGISTRY, "real"
        except Exception as e:
            print(f"\n  [test_recognition] real registry unavailable ({type(e).__name__}: {e}) - using the stub")

    def match(self, text: str, free_text: str | None = None):
        return self.registry.match(text, free_text=free_text, scope="full", env=self.env)

    def name(self, text: str, free_text: str | None = None) -> str | None:
        m = self.match(text, free_text)
        return m.command.name if m else None


def setUpModule():
    global TMP, _OLD_ENV, CFG, MATCH, GB, VOCAB, REC
    TMP = tempfile.mkdtemp(prefix="xyrus_t3_rec_")
    _OLD_ENV = os.environ.get("XYRUS_DATA_DIR")
    os.environ["XYRUS_DATA_DIR"] = TMP
    CFG = StubConfig()
    MATCH = Matcher(CFG)
    VOCAB = Vocab(MODEL_DIR, cache_file=Path(TMP) / "vocab_cache.json")
    GB = GrammarBuilder(MATCH.registry, CFG, VOCAB)
    REC = Recognizer(MODEL_DIR, FakeCapture(), GB, lambda: ListenSpec("wake"), lambda tr: None, GateSpeaker(),
                     RecordingChime(), CFG)
    VOCAB.attach(REC.check_words)
    REC.set_app_index(START_MENU)
    REC.load()
    W.synth_many(V1_PHRASES + ["yes"] + MORE_PHRASES + [s for s, _ in SONGS] + WHEN_PHRASES + CONFIRM_PHRASES
                 + FREE_PHRASES + ["xyrus", "xyrus what time is it"] + [p for p, _, _ in APP_CASES])


def tearDownModule():
    if _OLD_ENV is None:
        os.environ.pop("XYRUS_DATA_DIR", None)
    else:
        os.environ["XYRUS_DATA_DIR"] = _OLD_ENV
    shutil.rmtree(TMP, ignore_errors=True)


def new_recognizer(capture, get_spec, got, speaker=None, chime=None, grammar=None) -> Recognizer:
    rec = Recognizer(MODEL_DIR, capture, grammar or GB, get_spec, got.append, speaker or GateSpeaker(),
                     chime or RecordingChime(), CFG)
    rec._model = REC.load()                     # share the loaded model (heavy); recognizers stay per-thread
    return rec


def reverse_pcm(pcm: bytes) -> bytes:
    from array import array
    a = array("h")
    a.frombytes(pcm)
    a.reverse()
    return a.tobytes()


def wait_for(pred, timeout):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


# ============================================================================ §7.4 synthesized speech
class SynthesizedSpeech(unittest.TestCase):
    def _command_case(self, phrase):
        pcm = W.synth(phrase)
        tr = REC.decode_pcm(pcm, "wake")
        timing = REC.last_timing.get("command")
        expected = MATCH.name(phrase.split(" ", 1)[1])
        had_wake, rest = strip_wake(tr.text)
        got = MATCH.name(rest, tr.free_text) if had_wake else None
        ok = expected is not None and tr.mode == "command" and tr.wake_redecoded and got == expected
        if not ok and had_wake and got is None and tr.free_text is None:
            # the engine's retry (T1): no match and no free text -> engine.request_free_decode -> handle again
            box: list[str] = []
            if REC.request_free_decode(tr, box.append) and box and box[0]:
                from xyrus.normalize import WAKE_LIKE, normalize
                words = normalize(box[0]).split()
                while words and words[0] in WAKE_LIKE:
                    words = words[1:]
                got = MATCH.name(" ".join(words), box[0])
                ok = got == expected
                if ok:
                    self.retried.append((phrase, tr.text, box[0]))
        return ok, tr, expected, got, timing

    def test_1_command_set(self):
        rows, timings = [], []
        self.retried = []
        for phrase in V1_PHRASES + MORE_PHRASES:
            ok, tr, expected, got, timing = self._command_case(phrase)
            rows.append((phrase, ok, tr, expected, got))
            if timing is not None:
                timings.append(timing)
        # "yes" (v1 phrase) is answered inside a confirm dialogue in v2
        yes = REC.decode_pcm(W.synth("yes"), "confirm")
        rows.append(("yes (confirm mode)", yes.text == "yes", yes, "yes", yes.text))
        # a phrase whose typed form matches no command is a registry gap (reported), not a recognition result
        unregistered = [r[0] for r in rows if r[3] is None]
        rows = [r for r in rows if r[3] is not None]
        fails = [r for r in rows if not r[1]]
        v1_fails = [r for r in fails if r[0] in V1_PHRASES or r[0].startswith("yes")]
        print(f"\n  command set ({MATCH.kind} registry): {len(rows) - len(fails)}/{len(rows)} ok; command "
              f"re-decode median {statistics.median(timings) * 1000:.0f} ms, max {max(timings) * 1000:.0f} ms")
        for phrase, _, tr, expected, got in fails:
            print(f"    FAIL {phrase!r:44} heard {tr.text!r:40} free={tr.free_text!r} -> {got} (want {expected})")
        for phrase, heard, free in self.retried:
            print(f"    ok via free-decode retry: {phrase!r} heard {heard!r} -> free {free!r}")
        if unregistered:
            print(f"    not registered (typed phrase matches no command): {unregistered}")
        self.assertGreaterEqual(len(rows), 60)
        self.assertEqual(v1_fails, [], "the 17 v1 phrases must all pass")
        self.assertGreaterEqual((len(rows) - len(fails)) / len(rows), 0.95)
        self.assertLessEqual(statistics.median(timings), 0.4, "wake -> command re-decode budget (§5.3)")

    def test_2_songs_free_redecode(self):
        timings = []
        for phrase, title in SONGS:
            tr = REC.decode_pcm(W.synth(phrase), "wake")
            timings.append(REC.last_timing.get("free", 0))
            with self.subTest(phrase=phrase):
                self.assertEqual(tr.mode, "command", tr)
                self.assertIsNotNone(tr.free_text, tr)
                self.assertEqual(song_title(tr.free_text), title, f"heard {tr.text!r} free {tr.free_text!r}")
        print(f"\n  free re-decode median {statistics.median(timings) * 1000:.0f} ms, max {max(timings) * 1000:.0f} ms")
        self.assertLessEqual(statistics.median(timings), 0.6, "free re-decode budget (§5.3)")

    def test_3_dialogue_words(self):
        for mode, phrases in (("when", WHEN_PHRASES), ("confirm", CONFIRM_PHRASES), ("free", FREE_PHRASES)):
            for phrase in phrases:
                with self.subTest(mode=mode, phrase=phrase):
                    tr = REC.decode_pcm(W.synth(phrase), mode)
                    self.assertEqual(tr.text, phrase)
                    self.assertEqual(tr.mode, mode)
                    if mode == "free":
                        self.assertEqual(tr.free_text, phrase)

    def test_4_noise_never_wakes(self):
        samples = {"white noise 6 s amp 600": W.noise(6, 600, seed=1),
                   "near-silence 25 s amp 40": W.noise(25, 40, seed=2)}
        transcripts = []
        for name, pcm in samples.items():
            tr = REC.decode_pcm(pcm, "wake")
            transcripts.append(tr)
            with self.subTest(sample=name):
                self.assertFalse(WAKE & set(tr.text.split()), f"{name} -> {tr.text!r}")
        # the same noise streamed through the live loop in 0.25 s chunks
        got = self._stream([W.noise(6, 600, seed=1), W.noise(25, 40, seed=2)])
        self.assertFalse([t.text for t in got if WAKE & set(t.text.split())])
        transcripts += got
        try:
            from xyrus.testing import make_test_engine
        except ImportError:
            self.skipTest("handle_transcript leg needs T1 M1 (xyrus.testing.make_test_engine)")
        engine, ns = make_test_engine(tmpdir=Path(TMP) / "engine")
        for tr in transcripts:
            engine.handle_transcript(tr)
        self.assertEqual(list(ns.speaker.said), [])

    def test_4b_wake_only_grammar_removes_the_room_noise_class(self):
        """D1: synthesized room noise / TV-like babble that the open (command) grammar maps to words yields
        nothing under the idle wake-only grammar ('' for noise; only [unk] for babble, dropped as empty)."""
        noise = {}
        for seed in range(10):
            for amp in (1500, 2500, 4000):
                noise[f"room rumble amp {amp} seed {seed}"] = W.room_noise(4, amp, seed=seed)
        for seed in range(4):
            noise[f"white noise amp 1500 seed {seed}"] = W.noise(4, 1500, seed=seed)
        babble = {f"babble: reversed {t!r}": reverse_pcm(W.synth(t))      # speech-like, no wake word in it
                  for t in ("dentist appointment", "buy milk and eggs", "the fifteenth of october")}
        words_under_full, leaks, noise_not_empty = {}, {}, {}
        for name, pcm in {**noise, **babble}.items():
            full = REC.decode_pcm(pcm, "command").text
            idle = REC.decode_pcm(pcm, "wake").text
            if [t for t in full.split() if t != "[unk]"]:
                words_under_full[name] = (full, idle)
            if [t for t in idle.split() if t != "[unk]"]:
                leaks[name] = idle
            if name in noise and idle != "":
                noise_not_empty[name] = idle
        print(f"\n  mapped to words by the full grammar: {len(words_under_full)}/{len(noise) + len(babble)}")
        for name, (full, idle) in words_under_full.items():
            print(f"    {name:40} full={full!r:26} wake-only={idle!r}")
        self.assertTrue(words_under_full, "nothing produced words under the full grammar")
        self.assertEqual(leaks, {})
        self.assertEqual(noise_not_empty, {})

    def test_5_vocabulary_nothing_silently_dropped(self):
        for mode in ("command", "wake", "confirm", "when"):
            with self.subTest(mode=mode):
                raw = sorted(GB.raw_words(mode))
                known = REC.check_words(raw)
                self.assertEqual([w for w in raw if not known[w]], [])
                self.assertEqual(GB.dropped(mode), [])

    def test_bare_wake_and_empty_results(self):
        tr = REC.decode_pcm(W.synth("xyrus"), "wake")
        self.assertTrue(WAKE & set(tr.text.split()), tr)
        self.assertEqual(strip_wake(tr.text)[1].replace("[unk]", "").strip(), "")
        self.assertIsNone(tr.free_text)
        self.assertEqual(REC.decode_pcm(W.silence(2), "free").text, "")
        self.assertEqual(REC.decode_pcm(W.silence(2), "wake").text, "")
        self.assertLess(REC.decode_pcm(W.silence(1), "wake").peak, 0.001)
        self.assertGreater(REC.decode_pcm(W.synth("xyrus"), "wake").peak, 0.02)

    def test_app_names_resolve(self):                            # lead round 2 (A)
        for phrase, command, app in APP_CASES:
            with self.subTest(phrase=phrase):
                tr = REC.decode_pcm(W.synth(phrase), "wake")
                had_wake, rest = strip_wake(tr.text)
                m = MATCH.match(rest, tr.free_text)
                print(f"\n    {phrase!r:28} heard {tr.text!r:32} free={tr.free_text!r}")
                self.assertTrue(had_wake and m is not None, tr)
                self.assertEqual(m.command.name, command, tr)
                ref = m.slots.get("app")
                if MATCH.kind == "real":
                    self.assertIsNotNone(ref.target, (tr, ref))
                    self.assertIn(app, f"{ref.alias} {ref.spoken}", (tr, ref))
                else:
                    self.assertIn(app, tr.free_text or rest)

    def test_app_redecode_restricts_the_tail_to_app_names(self):
        # 'obs' is a model word but no command word: the command grammar gives "open [unk] ..." -> app grammar
        tr = REC.decode_pcm(W.synth("xyrus open obs studio"), "wake")
        if "[unk]" in tr.text.split():
            self.assertEqual(tr.free_text.split()[-2:], ["obs", "studio"], tr)
        # and the engine's retry hook uses the app grammar first: never "switch to crow"
        tr = REC.decode_pcm(W.synth("xyrus switch to chrome"), "wake")
        got = []
        self.assertTrue(REC.request_free_decode(tr, got.append))
        self.assertEqual(got[0].split()[-1], "chrome", got)
        self.assertEqual(REC._match_app(["[unk]", "the", "google", "crome"], ["google chrome", "steam"], {}),
                         "google chrome")
        self.assertIsNone(REC._match_app(["[unk]"], ["google chrome"], {}))

    def test_app_redecode_falls_back_to_unrestricted(self):
        rec = new_recognizer(FakeCapture(), lambda: ListenSpec("wake"), [])
        rec._app_redecode = lambda pcm, toks: None                 # app grammar found nothing
        tr = rec.decode_pcm(W.synth("xyrus open obs studio"), "wake")
        split = rec.app_split(tr.text.split())
        if split is None or "[unk]" not in split[1]:
            self.skipTest(f"command grammar heard the whole app name: {tr.text!r}")
        self.assertIn("studio", tr.free_text or "", tr)

    def test_request_free_decode_hook_inline(self):
        tr = REC.decode_pcm(W.synth("xyrus what time is it"), "wake")
        self.assertIsNone(tr.free_text)
        got = []
        self.assertTrue(REC.request_free_decode(tr, got.append))
        self.assertEqual(len(got), 1)
        self.assertIn("what time is it", got[0])
        self.assertFalse(REC.request_free_decode(Transcript("cyrus", "command", source="typed"), got.append))
        for phrase in ("yes", "no", "cancel", "correct"):            # push it out of the recent-PCM window
            REC.decode_pcm(W.synth(phrase), "confirm")
        self.assertFalse(REC.request_free_decode(tr, got.append))
        self.assertEqual(len(got), 1)

    def test_free_trigger_rules(self):
        cases = {"cyrus play eleven": True, "cyrus play [unk]": True, "cyrus play": False,
                 "cyrus open chrome": False, "cyrus open [unk]": True, "cyrus launch [unk] [unk]": True,
                 "cyrus close tab": False, "cyrus close [unk]": True, "cyrus switch to [unk]": True,
                 "cyrus remind me to call [unk]": True, "cyrus i finished [unk]": True,
                 "cyrus add a note [unk]": True, "cyrus search for [unk]": True, "cyrus what time is it": False,
                 "cyrus set a timer for five minutes": False, "cyrus start a timer for five minutes": False,
                 "play [unk]": True, "volume up": False}
        # the rules themselves, over the stub registry (§3 phrase table) so they don't depend on other tasks
        rec = new_recognizer(FakeCapture(), lambda: ListenSpec("wake"), [],
                             grammar=GrammarBuilder(StubRegistry(), CFG, VOCAB))
        for text, want in cases.items():
            with self.subTest(registry="stub", text=text):
                self.assertEqual(rec.wants_free(text.split()), want)
        # and the same decisions with whatever registry this run uses
        for text in ("cyrus play eleven", "cyrus open [unk]", "cyrus close [unk]", "cyrus search for [unk]",
                     "cyrus remind me to call [unk]", "cyrus what time is it", "cyrus open chrome", "cyrus play"):
            with self.subTest(registry=MATCH.kind, text=text):
                self.assertEqual(REC.wants_free(text.split()), cases[text])

    def _stream(self, pcms, spec=ListenSpec("wake"), timeout=60):
        cap, got = FakeCapture(), []
        rec = new_recognizer(cap, lambda: spec, got)
        for pcm in pcms:
            cap.feed(W.silence(0.5) + pcm + W.silence(1.5))
        rec.start()
        self.assertTrue(wait_for(lambda: cap.chunks.empty(), timeout))
        time.sleep(0.3)
        rec.stop()
        return got


# ============================================================================ T-rec loop
class LiveLoop(unittest.TestCase):
    def setUp(self):
        self.cap, self.got = FakeCapture(), []
        self.spec = ListenSpec("wake")
        self.speaker, self.chime = GateSpeaker(), RecordingChime()
        self.rec = new_recognizer(self.cap, lambda: self.spec, self.got, self.speaker, self.chime)
        self.addCleanup(self.rec.stop)

    def test_one_breath_song_through_the_loop(self):
        t0 = time.monotonic()
        self.cap.feed(W.silence(1) + W.synth("xyrus play alone") + W.silence(1.5), start=t0)
        self.rec.start()
        self.assertTrue(wait_for(lambda: self.got, 20))
        tr = self.got[0]
        self.assertEqual((tr.mode, tr.wake_redecoded), ("command", True), tr)
        self.assertEqual(song_title(tr.free_text or ""), "alone", tr)
        self.assertGreater(tr.peak, 0.02)
        self.assertTrue(t0 < tr.t_start < tr.t_end <= t0 + 5)
        self.assertEqual(self.chime.played, ["wake"])            # chime on the first wake partial, once
        self.assertTrue(tr.words and tr.words[0].start >= 0 and tr.words[-1].end <= 4.5, tr.words)

    def test_chime_can_be_turned_off(self):
        CFG.set("chime_on_wake", False)
        self.addCleanup(CFG.set, "chime_on_wake", True)
        self.cap.feed(W.silence(0.5) + W.synth("xyrus what time is it") + W.silence(1.5))
        self.rec.start()
        self.assertTrue(wait_for(lambda: self.got, 20))
        self.assertEqual(self.chime.played, [])

    def test_echo_gate_drops_own_speech_then_recovers(self):
        pcm = W.silence(0.5) + W.synth("xyrus what time is it") + W.silence(1.5)
        t0 = time.monotonic()
        t_end = self.cap.feed(pcm, start=t0)
        self.speaker.loud = [(t0, t_end + 0.1)]                   # Xyrus was "speaking" the whole time
        self.cap.feed(pcm, start=t_end + 1)
        self.rec.start()
        self.assertTrue(wait_for(lambda: self.got, 20))
        time.sleep(0.5)
        self.assertEqual(len(self.got), 1)
        self.assertGreater(self.got[0].t_start, t_end)
        self.assertGreaterEqual(self.rec.stats["gated"], len(pcm) // 8000)
        self.assertEqual(self.chime.played, ["wake"])

    def test_grammar_switch_waits_for_the_utterance_boundary(self):
        first = W.silence(1) + W.synth("xyrus what time is it") + W.silence(1.5)
        second = W.synth("yes") + W.silence(1.5)
        # switch to confirm mid-way through the first utterance: it must still finish in wake mode
        self.rec.get_spec = lambda: (ListenSpec("confirm", generation=1) if self.rec.stats["chunks"] > 7
                                     else ListenSpec("wake"))
        self.cap.feed(first + second)
        self.rec.start()
        self.assertTrue(wait_for(lambda: len(self.got) >= 2, 20), self.got)
        self.assertEqual(self.got[0].mode, "command")
        self.assertIn("time", self.got[0].text)
        self.assertEqual((self.got[1].mode, self.got[1].text), ("confirm", "yes"))

    def test_spec_change_while_idle_switches_immediately(self):
        self.rec.start()
        self.cap.feed(W.silence(1))
        self.assertTrue(wait_for(lambda: self.cap.chunks.empty(), 5))
        self.spec = ListenSpec("free", generation=2)
        self.cap.feed(W.silence(0.3) + W.synth("buy milk and eggs") + W.silence(1.5))
        self.assertTrue(wait_for(lambda: self.got, 20))
        self.assertEqual((self.got[0].mode, self.got[0].free_text), ("free", "buy milk and eggs"))

    def test_paused_drains_without_decoding(self):
        self.spec = ListenSpec("paused", generation=3)
        pcm = W.silence(0.5) + W.synth("xyrus what time is it") + W.silence(1.5)
        self.cap.feed(pcm)
        self.rec.start()
        self.assertTrue(wait_for(lambda: self.cap.chunks.empty(), 10))
        time.sleep(0.3)
        self.assertEqual(self.got, [])
        self.assertEqual(self.rec.stats["paused_drops"], len(W.chunks(pcm)))

    def test_never_blocks_on_an_empty_queue(self):
        self.rec.start()
        self.assertTrue(self.rec.ready.wait(20))
        worst = 0.0
        end = time.monotonic() + 2.5
        while time.monotonic() < end:
            worst = max(worst, time.monotonic() - self.rec.heartbeat)
            time.sleep(0.05)
        self.assertLess(worst, 1.0)
        t = time.monotonic()
        self.assertEqual(self.rec.check_words(["volume", "unmute"]), {"volume": True, "unmute": False})
        self.assertLess(time.monotonic() - t, 1.0)             # answered by T-rec between queue polls
        t = time.monotonic()
        self.rec.stop()
        self.assertLess(time.monotonic() - t, 1.0)
        self.assertFalse(self.rec.is_running())

    def test_request_free_decode_hook_runs_on_t_rec_without_blocking(self):
        self.cap.feed(W.silence(0.5) + W.synth("xyrus what time is it") + W.silence(1.5))
        self.rec.start()
        self.assertTrue(wait_for(lambda: self.got, 20))
        done, box = threading.Event(), {}

        def cb(text):
            box["text"], box["thread"] = text, threading.current_thread().name
            done.set()
        t = time.monotonic()
        self.assertTrue(self.rec.request_free_decode(self.got[0], cb))
        self.assertLess(time.monotonic() - t, 0.05)              # the engine never blocks > 50 ms
        self.assertTrue(done.wait(3))
        self.assertEqual(box["thread"], "T-rec")
        self.assertIn("what time is it", box["text"])

    def test_decode_pcm_is_routed_to_t_rec_while_running(self):
        self.rec.start()
        self.assertTrue(self.rec.ready.wait(20))
        tr = self.rec.decode_pcm(W.synth("xyrus what time is it"), "wake")
        self.assertEqual(tr.mode, "command")

    def test_grammar_version_change_rebuilds_recognizers(self):
        reg = StubRegistry()
        gb = GrammarBuilder(reg, CFG, VOCAB)
        rec = new_recognizer(self.cap, lambda: self.spec, self.got, grammar=gb)
        self.addCleanup(rec.stop)
        self.cap.feed(W.silence(0.5) + W.synth("xyrus what time is it") + W.silence(1.5))
        rec.start()
        self.assertTrue(wait_for(lambda: self.got, 20))
        self.assertTrue(rec._live_cache)
        old = dict(rec._live_cache)
        reg.bump()
        self.cap.feed(W.silence(0.5) + W.synth("xyrus what time is it") + W.silence(1.5))
        self.assertTrue(wait_for(lambda: len(self.got) >= 2, 20))
        self.assertEqual(self.got[1].mode, "command")
        self.assertFalse(set(map(id, old.values())) & set(map(id, rec._live_cache.values())))

    def test_utterance_buffer_is_capped_at_20s(self):
        rec = new_recognizer(FakeCapture(), lambda: ListenSpec("wake"), self.got)
        t = time.monotonic()
        longest = 0
        for c in W.chunks(W.noise(26, 3000, seed=7)):       # no pauses: may never endpoint
            t += 0.25
            rec._process(c, t)                               # inline, single thread (no T-rec running)
            longest = max(longest, len(rec._utt))
        self.assertLessEqual(longest, MAX_UTT_BYTES)

    def test_save_last_seconds(self):
        fed = W.silence(1) + W.synth("xyrus what time is it") + W.silence(1.5)
        self.cap.feed(fed)
        self.rec.start()
        self.assertTrue(wait_for(lambda: self.rec.stats["chunks"] >= len(W.chunks(fed)), 20))
        path = Path(TMP) / "last.wav"
        self.rec.save_last_seconds(path, 30)
        with wave.open(str(path)) as w:
            self.assertEqual((w.getframerate(), w.getnchannels(), w.getsampwidth()), (16000, 1, 2))
            self.assertEqual(w.getnframes() * 2, len(fed))            # everything captured, gate or not
        self.rec.save_last_seconds(path, 1)
        with wave.open(str(path)) as w:                               # the last four 0.25 s chunks only
            self.assertEqual(w.readframes(w.getnframes()), b"".join(W.chunks(fed)[-4:]))


# ============================================================================ AudioCapture
class FakeMic:
    """Stands in for sounddevice.RawInputStream (v1 test_songs FakeMic trick): a thread calling the callback
    with 8000-byte tone blocks every 50 ms until stopped; `stalled` pauses the callbacks."""
    instances: list["FakeMic"] = []
    fail_next = 0
    BLOCK = W.noise(0.25, 3000, seed=3)

    def __init__(self, device=None, samplerate=None, blocksize=None, dtype=None, channels=None, callback=None,
                 **kw):
        if FakeMic.fail_next:
            FakeMic.fail_next -= 1
            raise sd.PortAudioError("simulated open failure")
        self.args = dict(device=device, samplerate=samplerate, blocksize=blocksize, dtype=dtype, channels=channels)
        self.cb = callback
        self.active = False
        self.stalled = False
        self.closed = False
        self._halt = threading.Event()
        self._th = None
        FakeMic.instances.append(self)

    def start(self):
        self.active = True

        def feed():
            while not self._halt.wait(0.05):
                if not self.stalled:
                    self.cb(self.BLOCK, 4000, None, None)
        self._th = threading.Thread(target=feed, daemon=True)
        self._th.start()

    def stop(self):
        self._halt.set()
        if self._th:
            self._th.join(1)
        self.active = False

    abort = stop

    def close(self):
        self.stop()
        self.closed = True


class CaptureWithFakeMic(unittest.TestCase):
    def setUp(self):
        FakeMic.instances = []
        FakeMic.fail_next = 0
        self._real = audio.sd.RawInputStream
        audio.sd.RawInputStream = FakeMic
        self.statuses = []
        self.cap = audio.AudioCapture(lambda: (None, None), self.statuses.append)

    def tearDown(self):
        self.cap.close()
        audio.sd.RawInputStream = self._real

    def test_start_delivers_16k_int16_blocks(self):
        self.cap.start()
        self.assertTrue(wait_for(lambda: self.cap.chunks.qsize() >= 3, 3))
        items = [self.cap.chunks.get() for _ in range(3)]
        self.assertTrue(all(len(b) == 8000 for b, _ in items))
        self.assertTrue(items[0][1] < items[1][1] < items[2][1])
        mic = FakeMic.instances[0]
        self.assertEqual({k: mic.args[k] for k in ("samplerate", "blocksize", "dtype", "channels")},
                         {"samplerate": 16000, "blocksize": 4000, "dtype": "int16", "channels": 1})
        self.assertIn(mic.args["device"], {m.index for m in audio.list_mics()})
        self.assertEqual(self.cap.status(), "listening")
        self.assertTrue(self.cap.running())
        self.assertGreater(self.cap.level(), 0.05)

    def test_stop_closes_the_stream_no_callbacks_for_2s(self):   # D5
        self.cap.start()
        self.assertTrue(wait_for(lambda: self.cap.chunks.qsize() >= 2, 3))
        self.cap.stop()
        n = self.cap.callbacks
        self.assertTrue(FakeMic.instances[0].closed)
        self.assertEqual((self.cap.status(), self.cap.running(), self.cap.level()), ("paused", False, 0.0))
        self.assertTrue(self.cap.chunks.empty())
        time.sleep(2)
        self.assertEqual(self.cap.callbacks, n)
        self.assertTrue(self.cap.chunks.empty())
        self.cap.start()                                          # resume opens a new stream
        self.assertTrue(wait_for(lambda: not self.cap.chunks.empty(), 3))
        self.assertEqual(len(FakeMic.instances), 2)

    def test_watchdog_reopens_after_a_stall_within_5s(self):
        self.cap.start()
        self.assertTrue(wait_for(lambda: not self.cap.chunks.empty(), 3))
        FakeMic.instances[0].stalled = True
        t = time.monotonic()
        self.assertTrue(wait_for(lambda: len(FakeMic.instances) == 2 and self.cap.chunks.qsize() > 0
                                 and FakeMic.instances[1].active, 6))
        while not self.cap.chunks.empty():                        # everything queued is from the new stream
            self.cap.chunks.get_nowait()
        self.assertTrue(wait_for(lambda: not self.cap.chunks.empty(), 2))
        elapsed = time.monotonic() - t
        self.assertLess(elapsed, 5.0)
        self.assertTrue(FakeMic.instances[0].closed)
        self.assertEqual(self.statuses[-2:], ["mic error, retrying", "listening"])

    def test_open_failure_retries_with_backoff(self):
        FakeMic.fail_next = 2
        t = time.monotonic()
        self.cap.start()
        self.assertEqual(self.cap.status(), "mic error, retrying")
        self.assertTrue(wait_for(lambda: self.cap.status() == "listening", 8))
        self.assertGreater(time.monotonic() - t, 2.0)             # 1 s then 2 s between the failed attempts
        self.assertEqual(len(FakeMic.instances), 1)

    def test_no_microphone_status(self):
        real = audio.list_mics
        audio.list_mics = lambda: []
        try:
            self.cap.start()
            self.assertEqual(self.cap.status(), "no microphone")
            self.assertFalse(self.cap.running())
        finally:
            audio.list_mics = real

    def test_restart_reopens_only_while_running(self):
        self.cap.restart()
        self.assertEqual(FakeMic.instances, [])
        self.cap.start()
        self.cap.restart()
        self.assertEqual(len(FakeMic.instances), 2)
        self.assertTrue(FakeMic.instances[0].closed and FakeMic.instances[1].active)

    def test_full_queue_drops_the_oldest(self):
        self.cap._want = True
        for i in range(210):
            self.cap._callback(bytes([i % 256]) * 8000, 4000, None, None)
        self.assertEqual((self.cap.chunks.qsize(), self.cap.drops), (200, 10))
        self.assertEqual(self.cap.chunks.get_nowait()[0][0], 10)
        self.cap._want = False


class Devices(unittest.TestCase):
    def test_list_mics_mme_and_directsound_only(self):          # D8
        mics = audio.list_mics()
        self.assertTrue(all(m.hostapi in ("MME", "Windows DirectSound") for m in mics), mics)
        devices = sd.query_devices()
        for m in mics:
            self.assertGreater(devices[m.index]["max_input_channels"], 0)

    def test_resolve_mic(self):
        mics = audio.list_mics()
        if not mics:
            self.skipTest("no input devices")
        allowed = {m.index for m in mics}
        for m in mics:
            self.assertEqual(audio.resolve_mic(m.name, m.hostapi),
                             next(x.index for x in mics if x.name == m.name and x.hostapi == m.hostapi))
        name = mics[-1].name
        idx = audio.resolve_mic(name, "Windows WASAPI")          # persisted WASAPI entry -> same device, allowed api
        self.assertIn(idx, allowed)
        self.assertEqual(next(m.name for m in mics if m.index == idx), name)
        self.assertIn(audio.resolve_mic("No Such Microphone", "MME"), allowed)
        self.assertIn(audio.resolve_mic(None, None), allowed)

    def test_mme_truncated_names_match(self):
        long_name = "Microphone Array (Some Very Long Vendor Name Audio)"
        self.assertTrue(audio._same_name(long_name[:31], long_name))
        self.assertFalse(audio._same_name("Microphone", "Microphone Array"))


@unittest.skipUnless(audio.list_mics(), "no microphone")
class RealMicrophone(unittest.TestCase):
    """Short: opens a second capture stream (the live v1 process keeps its own)."""

    def test_real_mic_capture_and_pause_closes_stream(self):
        statuses = []
        cap = audio.AudioCapture(lambda: (None, "MME"), statuses.append)
        self.addCleanup(cap.close)
        cap.start()
        self.assertTrue(wait_for(lambda: cap.chunks.qsize() >= 3, 4), statuses)
        items = [cap.chunks.get() for _ in range(3)]
        self.assertTrue(all(len(b) == 8000 for b, _ in items))
        gaps = [items[i + 1][1] - items[i][1] for i in range(2)]
        self.assertTrue(all(0.1 < g < 0.5 for g in gaps), gaps)
        cap.stop()
        n = cap.callbacks
        time.sleep(2)
        self.assertEqual(cap.callbacks, n)
        self.assertTrue(cap.chunks.empty())
        self.assertEqual(statuses, ["listening", "paused"])


if __name__ == "__main__":
    unittest.main()
