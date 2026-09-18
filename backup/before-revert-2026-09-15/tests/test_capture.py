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
import sys
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
    """hearing.Heard's surface (CONTRACT, Sep 15): text() with min_logprob, the decode stats, text_nbest()."""
    avg_logprob = -0.2
    compression = 1.2
    no_speech = 0.0
    fallback_used = False

    def __init__(self, owner, pcm):
        self.owner, self.pcm = owner, pcm
        self._heard: dict[str, str] = {}              # language -> what text() returned for THIS utterance

    def text(self, language="en", prompt="<default>", min_logprob=None, **_kw):
        self.owner.decodes.append((language, prompt))
        answer = self.owner.answers.get(language, "")
        if isinstance(answer, list):
            answer = answer.pop(0) if answer else ""
        self._heard[language] = answer
        return answer

    def text_nbest(self, language="en", prompt=None, n=5):
        # the same utterance's reading: never pop the next utterance's answer (the recognizer asks for
        # alternates right after text())
        if language in self._heard:
            t = self._heard[language]
        else:
            t = self.text(language, prompt)
        return [(t, self.avg_logprob)] if t else []

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

    def listen_bn(self, pcm):
        return None                                   # no Bengali specialist in these tests

    def bn_available(self):
        return False

    def model_name(self):
        return "fake"


def wire(engine, rec):
    """What app.App does."""
    engine.request_free_decode = rec.request_free_decode
    engine.capture_available = rec.capture_ready
    engine.on_capture_handled = rec.capture_ack
    engine.request_language_check = rec.request_language_check
    rec.on_capture_state = engine.set_capture_state
    rec.song_question = engine.song_question_open
    rec.ack_required = True


def whine_floor(seconds: float, rms: float = 0.0033, seed: int = 0) -> bytes:
    """The user's measured room floor: rms ~0.0033, half of it hiss and half a 273 Hz whine."""
    n = int(seconds * RATE)
    h = np.frombuffer(W.hiss(seconds, rms / 2 ** 0.5, seed=seed), dtype=np.int16).astype(np.float32)[:n]
    tone = np.sin(2 * np.pi * 273 * np.arange(len(h)) / RATE) * rms * 32768     # rms of the tone: rms / sqrt(2)
    return np.clip(h + tone, -32768, 32767).astype(np.int16).tobytes()


HAVE_VAD = C.vad_available()


# ============================================================================ endpointing
class EndpointerTests(unittest.TestCase):
    """Run on the energy path (use_vad=False); SileroEndpointerTests runs the same cases on the VAD path."""
    USE_VAD = False

    @classmethod
    def setUpClass(cls):
        cls.sp = trim(W.synth("set the volume to forty percent"))
        cls.dur = len(cls.sp) / (RATE * 2)

    def ep(self, **kw) -> Endpointer:
        ep = Endpointer(use_vad=self.USE_VAD, **kw)
        self.assertEqual(ep.vad, "silero" if self.USE_VAD else "energy")
        return ep

    def layout(self, amp, lead=3.0, tail=3.0, seed=3):
        base = W.silence(lead) + self.sp + W.silence(tail)
        pcm = mix(base, W.room_noise(len(base) / (RATE * 2), amp, seed=seed)) if amp else base
        return pcm, lead, lead + self.dur

    def test_starts_and_ends_with_the_speech(self):
        for amp in (0, 800, 1500, 2500, 4000):
            with self.subTest(noise=amp):
                pcm, start, end = self.layout(amp)
                got = feed(self.ep(min_peak=0.02), pcm)
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
        u, _ = feed(self.ep(), pcm)[0]
        first, _ = bounds(u.pcm)
        self.assertGreaterEqual(first, 0.3, "PREROLL_S 0.4: the soft first consonant is in")
        self.assertLessEqual(first, 0.6)

    def test_a_pause_inside_the_sentence_is_not_the_end(self):
        a, b = trim(W.synth("set a timer")), trim(W.synth("for five minutes"))
        pcm = W.silence(2) + a + W.silence(0.6) + b + W.silence(2)
        got = feed(self.ep(), pcm)
        self.assertEqual(len(got), 1)
        self.assertGreaterEqual(got[0][0].t_end, 2 + (len(a) + len(b)) / (RATE * 2) + 0.6 - 0.15)

    def test_thirty_second_cap(self):
        talk = (self.sp + W.silence(0.2)) * 20                     # never stops talking (~40 s)
        pcm = W.silence(2) + talk
        got = feed(self.ep(), pcm)
        self.assertTrue(got)
        u, decided = got[0]
        self.assertEqual(u.reason, "cap")
        self.assertLessEqual(len(u.pcm) / (RATE * 2), C.MAX_S + 0.001)
        self.assertLessEqual(decided, 2 + C.MAX_S + 0.5)

    def test_noise_only_never_triggers(self):
        for amp in (300, 1500, 4000, 8000):
            for seed in range(3):
                with self.subTest(amp=amp, seed=seed):
                    ep = self.ep()
                    pcm = W.room_noise(12, amp, seed=seed) + W.noise(3, amp // 3, seed=seed)
                    self.assertEqual(feed(ep, pcm), [])

    def test_a_click_or_cough_is_not_a_command(self):
        ep = self.ep()
        self.assertEqual(feed(ep, W.silence(2) + W.noise(0.15, 12000, seed=1) + W.silence(2)), [])
        if self.USE_VAD:
            self.assertLessEqual(ep.discarded, 1)             # Silero mostly never starts on a click at all
        else:
            self.assertEqual(ep.discarded, 1)

    def test_too_quiet_is_not_speech(self):
        self.assertEqual(feed(self.ep(min_peak=0.02), W.silence(2) + scale(self.sp, 0.012) + W.silence(2)), [])

    def test_raw_pcm_goes_out_unchanged(self):
        """The level is measured on a filtered copy; the utterance is the mic's own bytes."""
        pcm, _, _ = self.layout(1500)
        u, _ = feed(self.ep(), pcm)[0]
        self.assertIn(u.pcm, pcm)
        self.assertEqual(len(u.pcm) % 2, 0)

    def test_reset_drops_the_utterance_in_progress(self):
        ep = self.ep()
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
        ep = self.ep(min_peak=0.02)
        ep.observe(hiss(2, 2), 2.0)
        got = feed(ep, pcm, t0=2.0)
        self.assertEqual(len(got), 1, f"discarded={ep.discarded}")
        self.assertEqual(got[0][0].reason, "silence")

    def test_room_hiss_with_bumps_never_starts(self):
        ep = self.ep(min_peak=0.02)
        pcm = mix(W.noise(20, 187, seed=4), W.room_noise(20, 500, seed=4))
        self.assertEqual(feed(ep, pcm), [])

    def test_observe_learns_the_noise_without_starting(self):
        ep = self.ep()
        ep.observe(W.room_noise(2, 2500, seed=1), 2.0)
        self.assertGreater(ep.ceiling, 0.005)
        self.assertFalse(ep.in_speech)

    def test_utterance_fields(self):
        pcm, start, end = self.layout(800)
        ep = self.ep()
        u, _ = feed(ep, pcm)[0]
        self.assertLessEqual(u.t_start, u.t_speech_start)
        self.assertLess(u.t_speech_start, u.t_speech_end)
        self.assertLessEqual(u.t_speech_end, u.t_end + 1e-6)
        self.assertAlmostEqual(u.t_speech_start, start, delta=0.3)
        self.assertAlmostEqual(u.t_speech_end, end, delta=0.3)
        if self.USE_VAD:
            self.assertAlmostEqual(u.vad_speech_s, self.dur, delta=0.25 * self.dur)
            self.assertEqual(u.vad_speech_s, u.speech_s)
            self.assertGreater(u.vad_max, 0.7)
        else:
            self.assertEqual((u.vad_speech_s, u.vad_max), (0.0, 0.0))

    def test_speech_slice(self):
        pcm, start, end = self.layout(0, lead=3.0, tail=3.0)
        ep = self.ep()
        u, _ = feed(ep, pcm)[0]
        sl = ep.speech_slice(u.pcm)
        self.assertIn(sl, u.pcm)
        if self.USE_VAD:                                       # the VAD span +- 0.3 s
            self.assertLess(len(sl), len(u.pcm) + 1)
            self.assertAlmostEqual(len(sl) / (RATE * 2), (u.t_speech_end - u.t_speech_start) + 0.6, delta=0.35)
        else:                                                  # no VAD span: the last 6 s (all of it here)
            self.assertEqual(sl, u.pcm[-int(C.SLICE_FALLBACK_S * RATE) * 2:])
        long = W.silence(10)                                   # PCM that is not the last utterance's
        self.assertEqual(len(ep.speech_slice(long)), int(C.SLICE_FALLBACK_S * RATE) * 2)


@unittest.skipUnless(HAVE_VAD, "Silero VAD unavailable")
class SileroEndpointerTests(EndpointerTests):
    USE_VAD = True


# ---- the faint bench (Sep 15): the 38 phrases at the user's seat levels over the room hiss
PEAKS = (0.02, 0.03, 0.036, 0.05)
SEEDS = (0, 1, 2)
LEAD, TAIL = 2.0, 2.5


def _bench(use_vad: bool, floor, peaks=PEAKS) -> dict:
    """-> {(peak, seed): dict(captured, start_ok, end_ok, ratios)} over tests.test_whisper_commands.COMMANDS."""
    from tests.test_whisper_commands import COMMANDS
    sp = {t: trim(p) for t, p in W.synth_many(COMMANDS).items()}
    floors = {s: floor(LEAD + TAIL + 6, seed=s) for s in SEEDS}
    pre = {s: floor(2, seed=100 + s) for s in SEEDS}
    out = {}
    for peak in peaks:
        clips = {t: W.faint(sp[t], peak) for t in COMMANDS}
        for seed in SEEDS:
            r = dict(captured=0, start_ok=0, end_ok=0, ratios=[], misses=[])
            for text in COMMANDS:
                clip = clips[text]
                dur = len(clip) / (RATE * 2)
                base = W.silence(LEAD) + clip + W.silence(TAIL)
                ep = Endpointer(min_peak=0.02, use_vad=use_vad)
                ep.observe(pre[seed], 2.0)                    # the recognizer observes while Vosk listens
                got = feed(ep, mix(base, floors[seed][:len(base)]), t0=2.0)
                s, e = 2.0 + LEAD, 2.0 + LEAD + dur
                hit = [u for u, _ in got if u.t_start <= e and u.t_end >= s]
                if not hit:
                    r["misses"].append(text)
                    continue
                u = hit[0]
                r["captured"] += 1
                r["start_ok"] += u.t_start <= s + 0.1
                r["end_ok"] += u.t_end >= e - 0.02
                if use_vad:
                    r["ratios"].append(u.vad_speech_s / dur)
            out[(peak, seed)] = r
    return out


def _summary(name: str, res: dict) -> str:
    rows = []
    for peak in sorted({p for p, _ in res}):
        caps = [res[(peak, s)]["captured"] for s in SEEDS]
        starts = [res[(peak, s)]["start_ok"] for s in SEEDS]
        rows.append(f"peak {peak}: captured {caps}/38 start<=0.1s {starts}")
    return f"[capture bench] {name}: " + "; ".join(rows)


@unittest.skipUnless(HAVE_VAD, "Silero VAD unavailable")
class FaintBenchSilero(unittest.TestCase):
    """VAD over hiss rms 0.0033: >= 30/38 @0.02, >= 36/38 @0.03, 38/38 @0.036+; start clipped <= 0.1 s in
    >= 36/38 (0.03+); the end never clipped; vad_speech_s within +-25 % of the phrase (0.03+).
    Measured Sep 15: 37-38/38 @0.02, 38/38 from 0.03 up, starts 38/38 from 0.03, ratios 0.90-1.20."""

    @classmethod
    def setUpClass(cls):
        cls.res = _bench(True, lambda s, seed: W.hiss(s, seed=seed))
        print("\n" + _summary("silero / hiss", cls.res), file=sys.stderr)

    def test_captured(self):
        need = {0.02: 30, 0.03: 36, 0.036: 38, 0.05: 38}
        for (peak, seed), r in self.res.items():
            with self.subTest(peak=peak, seed=seed):
                self.assertGreaterEqual(r["captured"], need[peak], r["misses"])

    def test_start_not_clipped(self):
        for (peak, seed), r in self.res.items():
            if peak >= 0.03:
                with self.subTest(peak=peak, seed=seed):
                    self.assertGreaterEqual(r["start_ok"], 36)

    def test_end_never_clipped(self):
        for (peak, seed), r in self.res.items():
            with self.subTest(peak=peak, seed=seed):
                self.assertEqual(r["end_ok"], r["captured"])

    def test_vad_speech_matches_the_phrase(self):
        for (peak, seed), r in self.res.items():
            if peak >= 0.03:
                with self.subTest(peak=peak, seed=seed):
                    self.assertGreaterEqual(min(r["ratios"]), 0.75)
                    self.assertLessEqual(max(r["ratios"]), 1.25)


class FaintBenchEnergy(unittest.TestCase):
    """The fallback. Over the measured room floor (hiss + 273 Hz whine) the notch lets it hear what it could not:
    measured Sep 15 at peak 0.03: 280 Hz high-pass 18/38, unfiltered 22/38, notch 30-32/38; 0.036: 36-37/38.
    The brief's 34/38 @0.03 is not reached with the energy thresholds unchanged (the per-frame min_peak 0.02
    is the limit: a peak-0.03 voice reaches it on few 20 ms frames). Over plain white hiss the notch changes
    nothing (0.03: ~11/38) - that is what the VAD is for."""

    @classmethod
    def setUpClass(cls):
        cls.whine = _bench(False, whine_floor, peaks=(0.03, 0.036, 0.05))
        cls.hiss = _bench(False, lambda s, seed: W.hiss(s, seed=seed), peaks=(0.05,))
        print("\n" + _summary("energy+notch / hiss+whine", cls.whine), file=sys.stderr)
        print(_summary("energy+notch / hiss", cls.hiss), file=sys.stderr)

    def test_captured_over_the_room_floor(self):
        need = {0.03: 28, 0.036: 34, 0.05: 38}
        for (peak, seed), r in self.whine.items():
            with self.subTest(peak=peak, seed=seed):
                self.assertGreaterEqual(r["captured"], need[peak], r["misses"])
                # the energy end is relative to the utterance's own level: a soft last syllable at peak 0.03
                # can fall under it (measured: <= 2 of 38); "never clipped" is the VAD path's promise
                self.assertGreaterEqual(r["end_ok"], r["captured"] - 3)

    def test_captured_over_hiss_when_audible(self):
        for (peak, seed), r in self.hiss.items():
            with self.subTest(peak=peak, seed=seed):
                self.assertEqual(r["captured"], 38, r["misses"])


class PauseInsideRequest(unittest.TestCase):
    """'Can you please?' was cut at a pause (live). 'can you please' + gap + 'play shape of you by ed sheeran':
    one utterance up to a 1.2 s pause in >= 5/6, two at 1.4 s with the second half intact."""

    @classmethod
    def setUpClass(cls):
        cls.a = trim(W.synth("can you please"))
        cls.b = trim(W.synth("play shape of you by ed sheeran"))

    def run_gap(self, gap, peak, seed, use_vad):
        body = W.faint(self.a + W.silence(gap) + self.b, peak)
        base = W.silence(2) + body + W.silence(2.5)
        ep = Endpointer(use_vad=use_vad)
        ep.observe(W.hiss(2, seed=50 + seed), 2.0)
        got = [u for u, _ in feed(ep, mix(base, W.hiss(len(base) / (RATE * 2), seed=seed)), t0=2.0)]
        b_start = 4.0 + len(self.a) / (RATE * 2) + gap
        return got, b_start, 4.0 + len(body) / (RATE * 2)

    def check(self, use_vad, peaks):
        for peak in peaks:
            for gap in (1.0, 1.2):
                with self.subTest(peak=peak, gap=gap):
                    ones = sum(len(self.run_gap(gap, peak, s, use_vad)[0]) == 1 for s in range(6))
                    self.assertGreaterEqual(ones, 5)
            with self.subTest(peak=peak, gap=1.4):
                for s in range(6):
                    got, b_start, b_end = self.run_gap(1.4, peak, s, use_vad)
                    self.assertEqual(len(got), 2, s)
                    self.assertLessEqual(got[1].t_start, b_start + 0.1, "the second half's start is in")
                    self.assertGreaterEqual(got[1].t_end, b_end - 0.02, "and its end")

    @unittest.skipUnless(HAVE_VAD, "Silero VAD unavailable")
    def test_silero(self):
        self.check(True, (0.036, 0.5))

    def test_energy_when_audible(self):
        self.check(False, (0.5,))                              # the energy path never starts at 0.036 on hiss


class RoomNoiseNeverCaptures(unittest.TestCase):
    """8 min of stationary room noise (amp 800-4000) -> 0 utterances; 80 transient bursts -> <= 10.
    Measured Sep 15 with Silero: 0 and 0 (the energy path: 0 and ~58 - bursts up to 0.4 s are 'loud')."""

    @classmethod
    def setUpClass(cls):
        cls.room = [W.room_noise(120, amp, seed=amp) for amp in (800, 1500, 2500, 4000)]
        rnd = np.random.default_rng(7)
        pcm = b""
        for i in range(80):
            pcm += W.hiss(float(rnd.uniform(1.5, 3.0)), seed=i)
            pcm += W.noise(float(rnd.uniform(0.03, 0.4)), int(rnd.integers(3000, 16000)), seed=i)
        cls.bursts = pcm + W.hiss(3, seed=99)

    def test_stationary_room_noise(self):
        for use_vad in ((True, False) if HAVE_VAD else (False,)):
            with self.subTest(vad=use_vad):
                self.assertEqual(sum(len(feed(Endpointer(use_vad=use_vad), r)) for r in self.room), 0)

    @unittest.skipUnless(HAVE_VAD, "Silero VAD unavailable")
    def test_transient_bursts(self):
        self.assertLessEqual(len(feed(Endpointer(), self.bursts)), 10)


class EndpointerChoice(unittest.TestCase):
    def test_use_vad_false_is_energy(self):
        self.assertEqual(Endpointer(use_vad=False).vad, "energy")
        self.assertFalse(Endpointer(use_vad=False).use_vad)

    def test_default_follows_availability(self):
        self.assertEqual(Endpointer().vad, "silero" if HAVE_VAD else "energy")
        self.assertIsInstance(C.vad_available(), bool)

    def test_constants(self):
        self.assertEqual((C.PREROLL_S, C.MIN_SPEECH_S, C.END_SILENCE_S, C.MIN_ONSET_RMS), (0.4, 0.25, 1.3, 0.0015))

    def test_captured_transcript_defaults(self):
        t = CapturedTranscript(text="volume up", mode="command")
        self.assertEqual((t.confidence, t.alternates, t.bn_text, t.vad_speech_s, t.fallback_used),
                         (None, (), None, 0.0, False))
        self.assertTrue(t.captured)


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

    def test_looks_like_song_table(self):
        """The meaning layer's rewrites and filler drop run first, so the phrasing is free."""
        for t in ("Can you play Shape of You?", "Put on Believer.", "Listen to Faded by Alan Walker.",
                  "I want to hear Alone by Alan Walker.", "Xyrus, can you play Shape of You?",
                  "Can you play any Justin Bieber song?", "Could you please play Ishwar by Vikings",
                  "Play Tired by Alan Walker."):
            self.assertTrue(C.looks_like_song(t), t)
        for t in ("Play.", "Play pause.", "Pause the music.", "Next song.", "Play next.", "Volume up.",
                  "Open Chrome.", "Can you please?"):
            self.assertFalse(C.looks_like_song(t), t)

    def test_repeated_tokens(self):
        for t in ("Bye. Bye. Bye. Bye.", "no no no", "you you you you volume"):
            self.assertTrue(C.repeated_tokens(t), t)
        for t in ("volume up up", "play shape of you", "bye bye", "", "turn it down a bit"):
            self.assertFalse(C.repeated_tokens(t), t)

    def test_guard_table_dropped(self):
        """Whisper on a blip / breath / the prompt read back: never a command (live Sep 15: 'Bye. Bye. Bye. Bye.'
        from 0.3 s; 'I'm going to call the doctor tomorrow at five.' from a 0.6 s blip)."""
        for t in ("Bye. Bye. Bye. Bye.", "Thank you.", "you", "... ...", "...", "Amen.", "Okay.", "Yeah.",
                  "Hmm.", "You're welcome.", "See you.", "Goodbye.", "Bye bye.", "Thank you so much.",
                  "Subtitles by the Amara.org community", "I'm going to call the doctor tomorrow at five.",
                  "Call the doctor tomorrow at five."):
            self.assertTrue(C.only_filler(t), t)

    def test_guard_table_kept(self):
        from tests.test_whisper_commands import COMMANDS
        for t in COMMANDS + ["can you play any justin bieber song", "Play Shape of You by Ed Sheeran.",
                             "What time is it?", "Open Chrome.", "Xyrus, play a song.", "Thank you for the music",
                             "Okay, volume up."]:
            self.assertFalse(C.only_filler(t), t)
            self.assertFalse(C.only_filler(t, "Xyrus, open Chrome. What time is it? Play a song."), t)

    def test_wake_look_alikes_that_are_english(self):
        self.assertEqual(C.strip_wake_words("zero the volume".split()), ["zero", "the", "volume"])
        self.assertEqual(C.strip_wake_words("serious question what time is it".split())[0], "serious")
        self.assertEqual(C.strip_wake_words("virus open chrome".split()), ["open", "chrome"])
        self.assertEqual(C.strip_wake_words("zero can you play believer".split())[0], "can")
        self.assertEqual(C.strip_wake_words(["zero"]), [])
        self.assertEqual(C.strip_wake_words("zeros take a screenshot".split()), ["take", "a", "screenshot"])

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
        from xyrus.recognizer import WAKE_CHECK_MAX_S
        talk = gap.join(trim(self.pcm[k]) for k in ("what time is it", "volume up", "open chrome") * 4)
        self.assertGreater(len(talk) / (RATE * 2), WAKE_CHECK_MAX_S + 1)   # longer than any name check
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
