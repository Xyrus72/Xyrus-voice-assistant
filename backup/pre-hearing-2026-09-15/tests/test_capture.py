"""Command capture (lead request, Sep 14 2026): after "Xyrus" -> "Waiting for your command, sir." ONE command is
recorded - no time limit to start, until the user has finished talking (capture.Endpointer) - and heard by
Whisper; exactly one command runs, then Xyrus is idle again. Question answers use the same path.

No test here loads Whisper: FakeHearing stands in for it. The recognizer tests use the real Vosk model for the
wake word and synthesized speech (tests/wav_util.synth) fed chunk by chunk through Recognizer._process.
tests/test_whisper_commands.py has the (slow) evidence with the real Whisper.

Run: venv\\Scripts\\python.exe -m unittest tests.test_capture -v
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

if not os.environ.get("XYRUS_DATA_DIR"):
    os.environ["XYRUS_DATA_DIR"] = tempfile.mkdtemp(prefix="xyrus_test_")

import numpy as np

from tests import wav_util as W
from xyrus import capture as C
from xyrus import paths
from xyrus.capture import CapturedTranscript, Endpointer
from xyrus.interfaces import ListenSpec, Transcript
from xyrus.testing import FakeActions, FakeChime, FakeSpeaker, make_test_engine

HAVE_MODEL = (paths.MODEL_DIR / "am" / "final.mdl").exists()
RATE = 16000


# ============================================================================ helpers
def mix(a: bytes, b: bytes) -> bytes:
    x = np.frombuffer(a, dtype=np.int16).astype(np.int32)
    y = np.frombuffer(b, dtype=np.int16).astype(np.int32)
    n = max(len(x), len(y))
    out = np.zeros(n, dtype=np.int32)
    out[:len(x)] += x
    out[:len(y)] += y
    return np.clip(out, -32768, 32767).astype(np.int16).tobytes()


def scale(pcm: bytes, peak: float) -> bytes:
    a = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    m = float(np.max(np.abs(a))) or 1.0
    return np.clip(a / m * peak * 32767, -32768, 32767).astype(np.int16).tobytes()


def bounds(pcm: bytes, thr: float = 0.02) -> tuple[float, float]:
    """(first, last) second where |sample| > thr (the speech inside a synthesized clip)."""
    a = np.abs(np.frombuffer(pcm, dtype=np.int16).astype(np.float32)) / 32768.0
    idx = np.nonzero(a > thr)[0]
    return float(idx[0]) / RATE, float(idx[-1]) / RATE


def trim(pcm: bytes) -> bytes:
    s, e = bounds(pcm)
    return pcm[int(s * RATE) * 2:int(e * RATE) * 2 + 2]


def feed(ep: Endpointer, pcm: bytes, t0: float = 0.0):
    """-> [(Utterance, chunk time at which it was decided)]"""
    out, t = [], t0
    for c in W.chunks(pcm):
        t += len(c) / (RATE * 2)
        u = ep.feed(c, t)
        if u is not None:
            out.append((u, t))
    return out


def said(text: str, mode: str = "command", **kw) -> CapturedTranscript:
    t = C.clean_text(text)
    return CapturedTranscript(text=t, mode=mode, free_text=t, peak=0.3, raw=text, **kw)


def cap_engine(**kw):
    overrides = {"command_window_s": 0, **kw.pop("config_overrides", {})}
    e, ns = make_test_engine(tmpdir=Path(tempfile.mkdtemp(prefix="xyrus_cap_")), config_overrides=overrides, **kw)
    e.capture_available = lambda: True
    return e, ns


def cmds(e) -> list[str]:
    return [ev.text for ev in e.events if ev.kind == "cmd"]


class FakeHeard:
    def __init__(self, owner, pcm):
        self.owner, self.pcm = owner, pcm

    def text(self, language="en", prompt="<default>"):
        self.owner.decodes.append((language, prompt))
        answer = self.owner.answers.get(language, "")
        if isinstance(answer, list):
            return answer.pop(0) if answer else ""
        return answer

    def language(self):
        self.owner.detections += 1
        return self.owner.detected


class FakeHearing:
    """Stands in for Whisper (app.CommandHearing surface). en / bn: the text (or a list, one per utterance)."""

    def __init__(self, en="", bn="", detected=("en", 0.99, [("en", 0.99)]), available=True):
        self.answers = {"en": en, "bn": bn}
        self.detected = detected
        self._available = available
        self.pcms: list[bytes] = []
        self.decodes: list[tuple] = []
        self.detections = 0

    def available(self):
        return self._available

    def listen(self, pcm):
        self.pcms.append(pcm)
        return FakeHeard(self, pcm)


def wire(engine, rec):
    """What app.App does."""
    engine.request_free_decode = rec.request_free_decode
    engine.capture_available = rec.capture_ready
    engine.on_capture_handled = rec.capture_ack
    engine.request_language_check = rec.request_language_check
    rec.on_capture_state = engine.set_capture_state
    rec.song_question = engine.song_question_open
    rec.ack_required = True


# ============================================================================ endpointing
class EndpointerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sp = trim(W.synth("set the volume to forty percent"))
        cls.dur = len(cls.sp) / (RATE * 2)

    def layout(self, amp, lead=3.0, tail=3.0, seed=3):
        base = W.silence(lead) + self.sp + W.silence(tail)
        pcm = mix(base, W.room_noise(len(base) / (RATE * 2), amp, seed=seed)) if amp else base
        return pcm, lead, lead + self.dur

    def test_starts_and_ends_with_the_speech(self):
        for amp in (0, 800, 1500, 2500, 4000):
            with self.subTest(noise=amp):
                pcm, start, end = self.layout(amp)
                got = feed(Endpointer(min_peak=0.02), pcm)
                self.assertEqual(len(got), 1, got)
                u, decided = got[0]
                self.assertEqual(u.reason, "silence")
                self.assertLessEqual(u.t_start, start - 0.15, "pre-roll: the first word is never clipped")
                self.assertGreaterEqual(u.t_start, start - 0.6)
                self.assertGreaterEqual(u.t_end, end - 0.15, "the last word is in")
                self.assertLessEqual(u.t_end, end + C.TAIL_S + 0.2, "a short tail, not the silence")
                self.assertGreaterEqual(decided - end, C.END_SILENCE_S - 0.2, "not cut off in a pause")
                self.assertLessEqual(decided - end, C.END_SILENCE_S + 0.45, "ends soon after the talking stops")
                self.assertAlmostEqual(len(u.pcm) / (RATE * 2), u.t_end - u.t_start, delta=0.03)

    def test_preroll_keeps_the_first_word(self):
        pcm, start, _ = self.layout(0)
        u, _ = feed(Endpointer(), pcm)[0]
        first, _ = bounds(u.pcm)
        self.assertGreaterEqual(first, 0.25)
        self.assertLessEqual(first, 0.45)

    def test_a_pause_inside_the_sentence_is_not_the_end(self):
        a, b = trim(W.synth("set a timer")), trim(W.synth("for five minutes"))
        pcm = W.silence(2) + a + W.silence(0.6) + b + W.silence(2)
        got = feed(Endpointer(), pcm)
        self.assertEqual(len(got), 1)
        self.assertGreaterEqual(got[0][0].t_end, 2 + (len(a) + len(b)) / (RATE * 2) + 0.6 - 0.15)

    def test_thirty_second_cap(self):
        pcm = W.silence(2) + W.noise(40, 8000, seed=5)             # never stops "talking"
        got = feed(Endpointer(), pcm)
        self.assertTrue(got)
        u, decided = got[0]
        self.assertEqual(u.reason, "cap")
        self.assertLessEqual(len(u.pcm) / (RATE * 2), C.MAX_S + 0.001)
        self.assertLessEqual(decided, 2 + C.MAX_S + 0.3)

    def test_noise_only_never_triggers(self):
        for amp in (300, 1500, 4000, 8000):
            for seed in range(3):
                with self.subTest(amp=amp, seed=seed):
                    ep = Endpointer()
                    pcm = W.room_noise(12, amp, seed=seed) + W.noise(3, amp // 3, seed=seed)
                    self.assertEqual(feed(ep, pcm), [])

    def test_a_click_or_cough_is_not_a_command(self):
        ep = Endpointer()
        self.assertEqual(feed(ep, W.silence(2) + W.noise(0.15, 12000, seed=1) + W.silence(2)), [])
        self.assertEqual(ep.discarded, 1)

    def test_too_quiet_is_not_speech(self):
        self.assertEqual(feed(Endpointer(min_peak=0.02), W.silence(2) + scale(self.sp, 0.012) + W.silence(2)), [])

    def test_reset_drops_the_utterance_in_progress(self):
        ep = Endpointer()
        half = len(self.sp) // 4 * 2
        feed(ep, W.silence(2) + self.sp[:half])
        self.assertTrue(ep.in_speech)
        ep.reset()
        self.assertFalse(ep.in_speech)
        self.assertEqual(feed(ep, W.silence(2), t0=10), [])

    def test_a_voice_from_across_the_room_is_heard(self):
        """Live Sep 14: the voice reached the mic ~7 dB over the room hiss (rms ~0.0033, speech peak ~0.036);
        the old 10 dB onset never started and Xyrus waited deaf forever after the wake reply."""
        hiss = lambda s, seed: W.noise(s, 187, seed=seed)                   # rms ~0.0033, like the room
        quiet = scale(self.sp, 0.036)
        pcm = mix(W.silence(3) + quiet + W.silence(3), hiss(6 + len(quiet) / (RATE * 2), 1))
        ep = Endpointer(min_peak=0.02)
        ep.observe(hiss(2, 2), 2.0)
        got = feed(ep, pcm, t0=2.0)
        self.assertEqual(len(got), 1, f"discarded={ep.discarded}")
        self.assertEqual(got[0][0].reason, "silence")

    def test_room_hiss_with_bumps_never_starts(self):
        ep = Endpointer(min_peak=0.02)
        pcm = mix(W.noise(20, 187, seed=4), W.room_noise(20, 500, seed=4))
        self.assertEqual(feed(ep, pcm), [])

    def test_observe_learns_the_noise_without_starting(self):
        ep = Endpointer()
        ep.observe(W.room_noise(2, 2500, seed=1), 2.0)
        self.assertGreater(ep.ceiling, 0.005)
        self.assertFalse(ep.in_speech)


class TextHelpers(unittest.TestCase):
    def test_clean_text(self):
        self.assertEqual(C.clean_text("Xyrus, open Chrome."), "xyrus open chrome")
        self.assertEqual(C.clean_text("Set the volume to 40%."), "set the volume to 40")
        self.assertEqual(C.clean_text("আমার ভিনদেশী তারা।"), "আমার ভিনদেশী তারা")

    def test_only_filler(self):
        for t in ("", "Thank you.", "you", ".", "Thanks for watching!", "Bye.", "um", "ধন্যবাদ"):
            self.assertTrue(C.only_filler(t), t)
        for t in ("What time is it?", "Volume up.", "Thank you for the music, play it again"):
            self.assertFalse(C.only_filler(t), t)

    def test_strip_wake_words(self):
        self.assertEqual(C.strip_wake_words("sirius open chrome".split()), ["open", "chrome"])
        self.assertEqual(C.strip_wake_words("hey cyrus volume up".split()), ["volume", "up"])
        self.assertEqual(C.strip_wake_words(["xyrus"]), [])
        self.assertEqual(C.strip_wake_words("open zero".split()), ["open", "zero"])

    def test_looks_like_song(self):
        self.assertTrue(C.looks_like_song("Play Shape of You."))
        self.assertTrue(C.looks_like_song("Xyrus, play amar bhindeshi tara"))
        self.assertTrue(C.looks_like_song("Amar bhindeshi tara bajao"))
        for t in ("Play.", "Play pause", "What time is it?", "Pause the music."):
            self.assertFalse(C.looks_like_song(t), t)

    def test_bangla_likely(self):
        self.assertTrue(C.bangla_likely(("bn", 0.8, [("bn", 0.8), ("en", 0.1)])))
        self.assertTrue(C.bangla_likely(("hi", 0.4, [("hi", 0.4), ("bn", 0.3), ("en", 0.2)])))
        self.assertTrue(C.bangla_likely({"en": 0.45, "bn": 0.3, "hi": 0.25}))
        self.assertFalse(C.bangla_likely(("en", 0.97, [("en", 0.97), ("bn", 0.01)])))
        self.assertFalse(C.bangla_likely(None))


# ============================================================================ engine flow
class EngineFlow(unittest.TestCase):
    def test_config_default_is_no_limit(self):
        from xyrus.config import DEFAULTS
        self.assertEqual(DEFAULTS["command_window_s"], 0)

    def test_wake_then_ten_minutes_of_silence_then_the_command(self):
        e, ns = cap_engine()
        e.handle("cyrus", "mic")
        self.assertIn(ns.speaker.said[-1], ns.config.get("wake_replies"))
        snap = e.snapshot()
        self.assertEqual((snap.mode, snap.window_left_s, snap.listen), ("armed", 0, "command"))
        self.assertIsNone(e.mode_until)
        for _ in range(40):                                   # 10 minutes, ticking like T-engine
            ns.clock.advance(15)
            e.tick()
        self.assertEqual(e.snapshot().mode, "armed")
        e.handle_transcript(said("What time is it?"))
        self.assertTrue(ns.speaker.said[-1].startswith("It's"), ns.speaker.said)
        self.assertEqual((e.snapshot().mode, e.listen_spec().mode), ("idle", "wake"))

    def test_exactly_one_command_per_call(self):
        e, ns = cap_engine()
        e.handle("cyrus", "mic")
        e.handle_transcript(said("Volume up."))
        e.handle_transcript(said("Volume up."))                # a stray second utterance: the call is over
        self.assertEqual(cmds(e), ["volume_up"])
        self.assertEqual(ns.actions.state["volume"], 60)
        self.assertEqual(e.snapshot().mode, "idle")

    def test_nothing_matched_says_sorry_once_and_goes_idle(self):
        e, ns = cap_engine()
        e.handle("cyrus", "mic")
        e.handle_transcript(said("Purple elephants dance on Tuesdays."))
        self.assertEqual(ns.speaker.said[-1], "Sorry, I didn't catch that, sir.")
        self.assertEqual((e.snapshot().mode, e.listen_spec().mode), ("idle", "wake"))
        n = len(ns.speaker.said)
        e.handle_transcript(said("More nonsense words here."))
        self.assertEqual(len(ns.speaker.said), n, "no loop of 'Sorry'")

    def test_cancel_words_end_it_quietly(self):
        for phrase in ("Never mind.", "Cancel.", "Nothing.", "Nothing, thanks.", "Forget it."):
            with self.subTest(phrase=phrase):
                e, ns = cap_engine()
                e.handle("cyrus", "mic")
                e.handle_transcript(said(phrase))
                self.assertEqual(len(ns.speaker.said), 1, ns.speaker.said)        # only the wake reply
                self.assertEqual(e.snapshot().mode, "idle")
                self.assertEqual(cmds(e), [])

    def test_cancel_during_a_shutdown_countdown_still_cancels_it(self):
        e, ns = cap_engine()
        e.shutdown_until = ns.clock.mono() + 10
        e.handle("cyrus", "mic")
        e.handle_transcript(said("Cancel."))
        self.assertIn("abort_shutdown", ns.actions.names(), (cmds(e), ns.speaker.said))

    def test_the_name_again_is_answered_again(self):
        e, ns = cap_engine()
        e.handle("cyrus", "mic")
        e.handle_transcript(said("Xyrus?"))
        self.assertEqual(len(ns.speaker.said), 2)
        self.assertEqual(e.snapshot().mode, "armed")

    def test_without_whisper_the_vosk_window_is_60_s(self):
        e, ns = make_test_engine(tmpdir=Path(tempfile.mkdtemp(prefix="xyrus_cap_")),
                                 config_overrides={"command_window_s": 0})
        e.handle("cyrus", "mic")
        self.assertEqual(e.snapshot().window_left_s, 60)
        ns.clock.advance(61)
        e.tick()
        self.assertEqual(e.snapshot().mode, "idle")

    def test_a_set_window_still_works_with_whisper(self):
        e, ns = cap_engine(config_overrides={"command_window_s": 20})
        e.handle("cyrus", "mic")
        self.assertEqual(e.snapshot().window_left_s, 20)

    def test_deadlines_wait_while_the_user_is_talking(self):
        e, ns = cap_engine(config_overrides={"command_window_s": 15})
        e.handle("cyrus", "mic")
        e.set_capture_state("hearing")
        ns.clock.advance(40)
        e.tick()
        self.assertEqual(e.snapshot().mode, "armed", "no expiry mid-sentence")
        e.set_capture_state("thinking")
        ns.clock.advance(5)
        e.tick()
        e.handle_transcript(said("What time is it?"))
        e.set_capture_state("listening")
        self.assertTrue(ns.speaker.said[-1].startswith("It's"))
        # a question's deadline too
        e.handle("shut down the computer", "typed")
        self.assertEqual(e.snapshot().mode, "dialogue")
        e.set_capture_state("hearing")
        ns.clock.advance(60)
        e.tick()
        self.assertIsNotNone(e.dialogue)
        e.handle_transcript(said("Yes.", mode="confirm"))
        self.assertIn("shutdown", ns.actions.names())

    def test_confirm_answers_in_whole_sentences(self):
        for answer, yes in (("Yeah, go ahead.", True), ("Yes, please do it.", True), ("No, not now.", False),
                            ("Nope.", False)):
            with self.subTest(answer=answer):
                e, ns = cap_engine()
                e.handle("shut down the computer", "typed")
                e.handle_transcript(said(answer, mode="confirm"))
                self.assertEqual("shutdown" in ns.actions.names(), yes, ns.speaker.said)
                self.assertIsNone(e.dialogue)

    def test_song_question_waits_without_limit_and_takes_the_answer(self):
        e, ns = cap_engine()
        e.handle("cyrus", "mic")
        e.handle_transcript(said("Play some music."))
        self.assertEqual(ns.speaker.said[-1], "What should I play, sir?")
        self.assertTrue(e.song_question_open())
        self.assertEqual(e.listen_spec().mode, "free")
        for _ in range(40):
            ns.clock.advance(15)
            e.tick()
        self.assertIsNotNone(e.dialogue, "the song question has no time limit either")
        e.handle_transcript(said("Believer by Imagine Dragons.", mode="free"))
        self.assertEqual(ns.actions.called("find_song"), [("believer by imagine dragons",)])
        self.assertFalse(e.song_question_open())

    def test_an_unknown_app_name_is_heard_again(self):
        e, ns = cap_engine()
        asked, acks = [], []
        e.on_capture_handled = lambda: acks.append(1)

        def hook(tr, cb):                                       # recognizer.request_free_decode's app re-decode
            asked.append(tr.text)
            self.assertEqual(acks, [], "no new capture before the name is settled")
            cb("open chrome")
            return True
        e.request_free_decode = hook
        e.handle("cyrus", "mic")
        e.handle_transcript(said("Open crow."))
        self.assertEqual(asked, ["open crow"])
        self.assertEqual(len(cmds(e)), 1)
        self.assertIn("target='chrome'", cmds(e)[0])
        self.assertEqual((e.snapshot().mode, acks), ("idle", [1]))

    def test_an_unknown_app_name_stays_when_nothing_better_is_heard(self):
        e, ns = cap_engine()
        e.request_free_decode = lambda tr, cb: cb("open crow") or True
        e.handle("cyrus", "mic")
        e.handle_transcript(said("Open crow."))
        self.assertEqual(len(cmds(e)), 1)
        self.assertIn("spoken='crow'", cmds(e)[0])
        self.assertEqual(e.snapshot().mode, "idle")

    def test_ui_shows_listening_and_thinking(self):
        from xyrus.ui.window import status_text
        e, ns = cap_engine()
        e.handle("cyrus", "mic")
        e.set_capture_state("hearing")
        self.assertEqual(e.snapshot().status, "hearing")
        self.assertEqual(status_text(e.snapshot())[0], "Listening…")
        e.set_capture_state("thinking")
        self.assertEqual(status_text(e.snapshot())[0], "Thinking…")
        e.set_capture_state("listening")
        self.assertEqual(status_text(e.snapshot())[0], "Listening for your command")
        e.set_status("mic error, retrying")
        e.set_capture_state("thinking")
        self.assertEqual(status_text(e.snapshot())[0], "Mic error, retrying", "errors win")

    def test_ack_comes_after_the_spec_is_published(self):
        e, ns = cap_engine()
        seen = []
        e.on_capture_handled = lambda: seen.append(e.listen_spec().mode)
        e.handle("cyrus", "mic")
        e.handle_transcript(said("Volume up."))
        self.assertEqual(seen, ["wake"])

    def test_no_ack_while_the_bangla_check_is_pending(self):
        e, ns = cap_engine()
        seen, pending = [], []
        e.on_capture_handled = lambda: seen.append(1)
        e.request_language_check = lambda tr, cb: pending.append(cb) or True
        e.handle("cyrus", "mic")
        e.handle_transcript(said("Purple elephants dance on Tuesdays."))
        self.assertEqual((seen, len(pending)), ([], 1))
        pending[0](None)                                      # not Bangla either
        self.assertEqual(seen, [1])
        self.assertEqual(ns.speaker.said[-1], "Sorry, I didn't catch that, sir.")


# ============================================================================ recognizer loop
@unittest.skipUnless(HAVE_MODEL, "speech model missing")
class CaptureLoop(unittest.TestCase):
    MODEL = None

    @classmethod
    def setUpClass(cls):
        from xyrus.recognizer import Recognizer
        cls.pcm = W.synth_many(["xyrus", "what time is it", "volume up", "xyrus what time is it", "open chrome"])
        cls.Recognizer = Recognizer

    def make(self, hearing, speaker=None, window=0):
        from xyrus.grammar import GrammarBuilder, Vocab
        e, ns = cap_engine(config_overrides={"command_window_s": window})
        e.capture_available = None
        vocab = Vocab(paths.MODEL_DIR, cache_file=Path(tempfile.mkdtemp()) / "vocab_cache.json")
        rec = self.Recognizer(paths.MODEL_DIR, None, GrammarBuilder(ns.registry, ns.config, vocab),
                              get_spec=e.listen_spec, on_transcript=e.handle_transcript,
                              speaker=speaker or FakeSpeaker(), chime=FakeChime(), config=ns.config)
        if CaptureLoop.MODEL is None:
            CaptureLoop.MODEL = rec.load()
        rec._model = CaptureLoop.MODEL
        vocab.attach(rec.check_words)
        rec.set_command_hearing(hearing)
        wire(e, rec)
        return e, ns, rec

    @staticmethod
    def play(rec, pcm, t=100.0):
        for c in W.chunks(pcm):
            t += len(c) / (RATE * 2)
            rec._process(c, t)
        return t

    def test_wake_then_the_command_heard_whole_by_whisper(self):
        h = FakeHearing(en=["What time is it?"])
        e, ns, rec = self.make(h)
        cmd = self.pcm["what time is it"]
        self.play(rec, W.silence(1) + self.pcm["xyrus"] + W.silence(1.5) + cmd + W.silence(2))
        self.assertEqual(len(h.pcms), 1, "one Whisper call for the one command")
        heard_s, cmd_s = len(h.pcms[0]) / (RATE * 2), (bounds(cmd)[1] - bounds(cmd)[0])
        self.assertGreater(heard_s, cmd_s, "the whole command")
        self.assertLess(heard_s, cmd_s + 1.0, "only the command, not the silence around it")
        self.assertEqual(len(ns.speaker.said), 2, ns.speaker.said)
        self.assertTrue(ns.speaker.said[1].startswith("It's"), ns.speaker.said)
        self.assertEqual(e.snapshot().mode, "idle")
        self.assertEqual(rec.stats["captures"], 1)
        self.assertEqual(h.detections, 0, "no language check for a plain command")

    def test_thirty_seconds_of_room_noise_before_the_command(self):
        h = FakeHearing(en=["Volume up."])
        e, ns, rec = self.make(h)
        pcm = W.silence(1) + self.pcm["xyrus"] + W.silence(0.5)
        pcm += mix(W.room_noise(30, 800, seed=4) + self.pcm["volume up"] + W.silence(2),
                   W.room_noise(30 + len(self.pcm["volume up"]) / (RATE * 2) + 2, 800, seed=9))
        self.play(rec, pcm)
        self.assertEqual(len(h.pcms), 1)
        self.assertEqual(cmds(e), ["volume_up"])

    def test_room_noise_after_the_reply_is_never_a_command(self):
        h = FakeHearing(en=["Thank you."])
        e, ns, rec = self.make(h)
        self.play(rec, W.silence(1) + self.pcm["xyrus"] + W.silence(0.5) + W.room_noise(20, 2500, seed=2))
        self.assertEqual(h.pcms, [])
        self.assertEqual(len(ns.speaker.said), 1)
        self.assertEqual(e.snapshot().mode, "armed")

    def test_whisper_fillers_are_dropped_and_it_keeps_listening(self):
        h = FakeHearing(en=["Thank you.", "What time is it?"])
        e, ns, rec = self.make(h)
        cmd = self.pcm["what time is it"]
        self.play(rec, W.silence(1) + self.pcm["xyrus"] + W.silence(1.5) + cmd + W.silence(2) + cmd + W.silence(2))
        self.assertEqual(len(h.pcms), 2)
        self.assertEqual(rec.stats["capture_dropped"], 1)
        self.assertTrue(ns.speaker.said[-1].startswith("It's"))

    def test_echo_gate_never_captures_xyrus_itself(self):
        from tests.wav_util import GateSpeaker
        gate = GateSpeaker()
        h = FakeHearing(en=["Volume up."])
        e, ns, rec = self.make(h, speaker=gate)
        wake = W.silence(1) + self.pcm["xyrus"] + W.silence(1.5)
        cmd = self.pcm["what time is it"]
        t_wake_end = 100.0 + len(wake) / (RATE * 2)
        gate.loud = [(t_wake_end, t_wake_end + len(cmd) / (RATE * 2) + 0.1)]    # "Xyrus is talking"
        self.play(rec, wake + cmd + W.silence(1.5) + self.pcm["volume up"] + W.silence(2))
        self.assertEqual(len(h.pcms), 1)
        self.assertLess(len(h.pcms[0]) / (RATE * 2), len(self.pcm["volume up"]) / (RATE * 2) + 0.6)
        self.assertEqual(cmds(e), ["volume_up"])

    def test_without_whisper_it_is_the_vosk_command_path(self):
        h = FakeHearing(en=["never used"], available=False)
        e, ns, rec = self.make(h)
        self.play(rec, W.silence(1) + self.pcm["xyrus"] + W.silence(1.5) + self.pcm["what time is it"]
                  + W.silence(2))
        self.assertEqual(h.pcms, [])
        self.assertEqual(rec.stats["captures"], 0)
        self.assertTrue(ns.speaker.said[-1].startswith("It's"), ns.speaker.said)
        self.assertEqual(e.snapshot().mode, "idle")

    def test_one_breath_stays_on_vosk(self):
        h = FakeHearing(en=["never used"])
        e, ns, rec = self.make(h)
        self.play(rec, W.silence(1) + self.pcm["xyrus what time is it"] + W.silence(2))
        self.assertEqual(h.pcms, [])
        self.assertTrue(ns.speaker.said[-1].startswith("It's"), ns.speaker.said)

    def test_a_misheard_app_name_is_fixed_with_the_app_grammar(self):
        h = FakeHearing(en=["Open crow.", "Open crow."])            # Whisper's real slip on "open chrome"
        e, ns, rec = self.make(h)
        self.play(rec, W.silence(1) + self.pcm["xyrus"] + W.silence(1.5) + self.pcm["open chrome"] + W.silence(2))
        self.assertEqual(len(cmds(e)), 1, (cmds(e), ns.speaker.said))
        self.assertIn("target='chrome'", cmds(e)[0])
        self.assertEqual(rec.stats["app_redecodes"], 1)

    def test_one_breath_retry_is_whisper(self):
        h = FakeHearing(en="Xyrus, open Chrome.")
        e, ns, rec = self.make(h)
        rec._redecode = lambda pcm, mode: {"text": "vosk words"}
        rec._remember(b"\x10\x27" * 16000, 5.0, 6.0)
        e.handle_transcript(Transcript(text="cyrus [unk] [unk]", mode="command", peak=0.3, t_start=5.0, t_end=6.0))
        self.assertEqual(len(h.pcms), 1)
        self.assertTrue(cmds(e) and cmds(e)[0].startswith("open_app"), (cmds(e), ns.speaker.said))

    # ---- idle Whisper second opinion on the name (live Sep 14: Vosk 0/3 on a faint "xyrus", Whisper 3/3)
    def test_whisper_hears_the_name_vosk_missed(self):
        h = FakeHearing(en=["Xyrus."])
        e, ns, rec = self.make(h)
        self.play(rec, W.silence(1) + trim(self.pcm["volume up"]) + W.silence(2))   # Vosk's wake grammar: nothing
        self.assertEqual(rec.stats["wake_whisper"], 1, dict(rec.stats))
        self.assertEqual(len(ns.speaker.said), 1, ns.speaker.said)                  # the wake reply, once
        self.assertEqual(e.snapshot().mode, "armed")

    def test_the_name_is_answered_once_when_both_hear_it(self):
        h = FakeHearing(en=["Xyrus.", "Xyrus.", "Xyrus."])
        e, ns, rec = self.make(h)
        self.play(rec, W.silence(1) + self.pcm["xyrus"] + W.silence(3))
        self.assertEqual(len(ns.speaker.said), 1, ns.speaker.said)
        self.assertEqual(e.snapshot().mode, "armed")

    def test_other_short_speech_is_not_a_call(self):
        h = FakeHearing(en=["Volume up."])
        e, ns, rec = self.make(h)
        self.play(rec, W.silence(1) + trim(self.pcm["volume up"]) + W.silence(2))
        self.assertEqual(ns.speaker.said, [])
        self.assertEqual(cmds(e), [], "no command without the name")
        self.assertEqual(e.snapshot().mode, "idle")

    def test_long_talk_is_never_checked(self):
        h = FakeHearing(en=["Xyrus."])
        e, ns, rec = self.make(h)
        gap = W.silence(0.2)
        talk = gap.join(trim(self.pcm[k]) for k in ("what time is it", "volume up", "open chrome",
                                                    "what time is it", "volume up"))
        self.assertGreater(len(talk) / (RATE * 2), C.MAX_S / 10 + 1)
        self.play(rec, W.silence(1) + talk + W.silence(2))
        self.assertEqual(h.pcms, [], "a conversation never costs a Whisper decode")
        self.assertEqual(ns.speaker.said, [])

    def test_name_and_command_in_one_breath_by_whisper(self):
        h = FakeHearing(en=["Xyrus, what time is it?"])
        e, ns, rec = self.make(h)
        self.play(rec, W.silence(1) + trim(self.pcm["volume up"]) + W.silence(2))
        self.assertTrue(ns.speaker.said and ns.speaker.said[-1].startswith("It's"), ns.speaker.said)

    def test_the_next_capture_waits_for_the_engine(self):
        import types
        h = FakeHearing(en=["Volume up.", "Volume up."])
        got = []
        grammar = types.SimpleNamespace(version=lambda: 1)            # never used: capture mode feeds no Vosk
        rec = self.Recognizer(paths.MODEL_DIR, None, grammar, get_spec=lambda: ListenSpec("command", generation=5),
                              on_transcript=got.append, speaker=FakeSpeaker(), chime=FakeChime(),
                              config=make_test_engine()[1].config)
        rec.set_command_hearing(h)
        rec.ack_required = True
        cmd = self.pcm["volume up"]
        rec_t = self.play(rec, W.silence(1) + cmd + W.silence(1.5))
        rec_t = self.play(rec, cmd + W.silence(1.5), t=rec_t)
        self.assertEqual(len(got), 1, "no second capture before the engine handled the first")
        rec.capture_ack()
        self.play(rec, cmd + W.silence(1.5), t=rec_t)
        self.assertEqual(len(got), 2)
        self.assertTrue(all(getattr(tr, "captured", False) for tr in got))


if __name__ == "__main__":
    unittest.main()
