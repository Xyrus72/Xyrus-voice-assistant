"""SLOW (GPU, ~5-10 min; loads Whisper): the wake bench for the hearing rebuild (Sep 15 2026).

Real Vosk + real Whisper through Recognizer._process (the CaptureLoop pattern of tests/test_capture.py),
synthesized speech (System.Speech) scaled to the user's seat (speech peaks 0.035 / 0.05 over a hiss floor of
rms 0.0032):
  1. faint wake: 'xyrus', 'hey xyrus' and three one-breath requests x 2 levels x 6 seeds -> woken >= 5/6 per
     phrase per level; the one-breath forms reach engine.on_wake_with_command with the exact command >= 5/6;
     the wake decision <= 2.5 s of audio after the speech ends.
  2. false wakes: 5 min of room noise (amp 800-4000) + 80 bursts -> 0 wakes, <= 20 Whisper decodes; 38 faint
     commands without the name -> 0 wakes; sound-alikes (serious / sirius / iris / virus / zeros) at 0.035 and
     0.3 -> <= 1/9 per word.
  3. playback: 30 s of a music-like bed with ListenSpec(playback=True) + 'xyrus' at 0.5 -> no idle utterance
     longer than 3 s, the wake decision <= 3.5 s.

Skipped when the Vosk model, hearing.py or a Whisper model is missing. No calendar / reminder phrase anywhere.
Run alone: venv\\Scripts\\python.exe -m unittest tests.test_wake_bench -v
"""
from __future__ import annotations

import math
import os
import random
import sys
import tempfile
import time
import unittest
from array import array
from pathlib import Path

if not os.environ.get("XYRUS_DATA_DIR"):
    os.environ["XYRUS_DATA_DIR"] = tempfile.mkdtemp(prefix="xyrus_test_")

from tests import wav_util as W
from xyrus import paths

HAVE_VOSK = (paths.MODEL_DIR / "am" / "final.mdl").exists()
HAVE_WHISPER = any((paths.BASE / "models" / d / "model.bin").exists()
                   for d in ("whisper-large-v3", "whisper-large-v3-turbo", "whisper-small"))
RATE = 16000
HISS_RMS = 0.0032
LEVELS = (0.035, 0.05)
SEEDS = range(6)
WAKE_PHRASES = {"xyrus": None, "hey xyrus": None, "xyrus what time is it": "what time is it",
                "xyrus play shape of you by ed sheeran": "play shape of you by ed sheeran",
                "xyrus take a screenshot": "take a screenshot"}
SOUND_ALIKES = ("serious", "sirius", "iris", "virus", "zeros")
H = None                  # hearing module
MODEL = None              # the Vosk model, shared
GB = None
CFG = None
COMMANDS: list[str] = []


def setUpModule():
    global H, GB, CFG, COMMANDS
    if not HAVE_VOSK:
        raise unittest.SkipTest("Vosk model missing")
    if not HAVE_WHISPER:
        raise unittest.SkipTest("no Whisper model in models/")
    base = str(paths.BASE)
    if base not in sys.path:
        sys.path.insert(0, base)
    try:                                   # the VAD session BEFORE ctranslate2 (app.plug_hearing's order)
        from xyrus import vad
        vad.warm()
    except Exception:                      # noqa: BLE001 - the energy path is benchmarked then
        pass
    try:
        import hearing
    except Exception as e:                 # noqa: BLE001
        raise unittest.SkipTest(f"hearing.py unavailable: {e!r}")
    hearing.load_async()
    if not hearing.available(wait=300):
        raise unittest.SkipTest(f"Whisper unavailable: {getattr(hearing, 'error', '?')}")
    H = hearing
    from tests.test_whisper_commands import COMMANDS as cmds
    COMMANDS = list(cmds)
    from xyrus.grammar import GrammarBuilder, Vocab
    from xyrus.testing import make_test_engine
    _, ns = make_test_engine(tmpdir=Path(tempfile.mkdtemp(prefix="xyrus_wb_")))
    CFG = ns.config
    vocab = Vocab(paths.MODEL_DIR, cache_file=Path(tempfile.mkdtemp()) / "vocab_cache.json")
    GB = GrammarBuilder(ns.registry, ns.config, vocab)
    phrases = list(WAKE_PHRASES) + COMMANDS
    for w in SOUND_ALIKES:
        phrases += [w, f"that is {w}", f"{w} again"]
    W.synth_many(phrases)
    Harness(playback=False)                # loads the Vosk model, attaches the vocabulary


# ============================================================================ audio
def mix(a: bytes, b: bytes) -> bytes:
    x, y = array("h"), array("h")
    x.frombytes(a)
    y.frombytes(b)
    if len(y) < len(x):
        y.extend([0] * (len(x) - len(y)))
    return array("h", (max(-32768, min(32767, p + q)) for p, q in zip(x, y))).tobytes()


def speech_end(pcm: bytes, thr: float = 0.02) -> float:
    """Seconds to the last sample above thr of a synthesized clip (before scaling)."""
    a = array("h")
    a.frombytes(pcm)
    lim = thr * 32768
    for i in range(len(a) - 1, -1, -1):
        if abs(a[i]) > lim:
            return i / RATE
    return len(a) / RATE


def over_hiss(clip: bytes, peak: float, seed: int, lead: float = 1.5, tail: float = 3.5) -> tuple[bytes, float]:
    """(lead of hiss + the clip at `peak` + tail, the time its speech ends)."""
    body = W.silence(lead) + W.faint(clip, peak) + W.silence(tail)
    return mix(body, W.hiss(len(body) / (RATE * 2), HISS_RMS, seed=seed)), lead + speech_end(clip)


def music_bed(seconds: float, peak: float = 0.12, seed: int = 0) -> bytes:
    """Chords of harmonics changing every 0.4 s + a bass line + hiss: tonal, never silent, VAD-ambiguous."""
    rnd = random.Random(seed)
    notes = [220.0, 246.9, 261.6, 293.7, 329.6, 349.2, 392.0, 440.0]
    n = int(seconds * RATE)
    out = array("h")
    step = int(0.4 * RATE)
    for s in range(0, n, step):
        chord = rnd.sample(notes, 3)
        bass = rnd.choice(notes[:3]) / 2
        for i in range(min(step, n - s)):
            t = (s + i) / RATE
            env = 0.6 + 0.4 * math.exp(-3.0 * i / step)
            v = sum(math.sin(2 * math.pi * f * t) + 0.3 * math.sin(4 * math.pi * f * t) for f in chord) / 4.0
            v = env * (v + 0.5 * math.sin(2 * math.pi * bass * t)) + rnd.uniform(-0.05, 0.05)
            out.append(max(-32768, min(32767, int(v * peak / 1.3 * 32767))))
    return out.tobytes()


# ============================================================================ harness
class Harness:
    """A recognizer in idle (wake) mode with the real Whisper plugged, an engine that records wakes. Audio time
    starts at 100.0; every decision is stamped with the audio time of the chunk that produced it."""

    def __init__(self, playback: bool):
        global MODEL
        from xyrus.app import CommandHearing
        from xyrus.interfaces import ListenSpec
        from xyrus.recognizer import Recognizer
        from xyrus.testing import FakeChime, FakeSpeaker
        self.t = 100.0
        self.wakes: list[tuple[float, str, str | None]] = []       # (audio time, how, command)
        spec = ListenSpec("wake", playback=playback)
        self.rec = Recognizer(paths.MODEL_DIR, None, GB, get_spec=lambda: spec, on_transcript=self._tr,
                              speaker=FakeSpeaker(), chime=FakeChime(), config=CFG)
        if MODEL is None:
            MODEL = self.rec.load()
            GB.vocab.attach(self.rec.check_words)
        self.rec._model = MODEL
        self.rec.engine = self
        self.rec.set_command_hearing(CommandHearing(H))
        self.max_utt = 0
        self.idle_utts: list[float] = []
        whisper_wake = self.rec._whisper_wake

        def spy(utt, ep=None):
            self.idle_utts.append(utt.t_end - utt.t_start)
            return whisper_wake(utt, ep)
        self.rec._whisper_wake = spy

    def _tr(self, tr):
        if {"cyrus", "zeros", "virus", "cirrus"} & set(tr.text.split()):
            self.wakes.append((self.t, "transcript", tr.text))

    def on_wake_with_command(self, rest, tr):
        self.wakes.append((self.t, "command", rest))

    def play(self, pcm: bytes) -> float:
        start = self.t
        for c in W.chunks(pcm):
            self.t += len(c) / (RATE * 2)
            self.rec._process(c, self.t)
            self.max_utt = max(self.max_utt, len(self.rec._utt))
        return start

    def whisper_decodes(self) -> int:
        return int(self.rec.stats["wake_checks"])


def clean(text: str) -> str:
    from xyrus.capture import clean_text
    return clean_text(text)


# ============================================================================ the bench
class WakeBench(unittest.TestCase):
    def test_1_faint_wake(self):
        clips = W.synth_many(list(WAKE_PHRASES))
        rows, worst = [], 0.0
        failures = []
        for phrase, command in WAKE_PHRASES.items():
            for level in LEVELS:
                woke = right = 0
                heard = []
                for seed in SEEDS:
                    hz = Harness(playback=False)
                    pcm, end = over_hiss(clips[phrase], level, seed)
                    start = hz.play(pcm)
                    if hz.wakes:
                        woke += 1
                        t, how, what = hz.wakes[0]
                        decision = t - (start + end)
                        worst = max(worst, decision)
                        if decision > 2.5:
                            failures.append(f"{phrase!r}@{level} seed {seed}: decided {decision:.2f} s after")
                        if command is not None and how == "command" and clean(what) == command:
                            right += 1
                        heard.append(f"{how}:{what}")
                    else:
                        heard.append("-")
                    self.assertLessEqual(len(hz.wakes), 1, (phrase, level, seed, hz.wakes))
                rows.append((phrase, level, woke, right, command, heard))
        print("\n  faint wake (hiss rms %.4f), woken / command exact, per phrase per level:" % HISS_RMS)
        for phrase, level, woke, right, command, heard in rows:
            extra = f"  command {right}/6" if command else ""
            print(f"    {phrase!r:42} @ {level:.3f}: woke {woke}/6{extra}   {heard}")
        print(f"  worst wake decision: {worst:.2f} s of audio after the speech ended")
        for phrase, level, woke, right, command, heard in rows:
            with self.subTest(phrase=phrase, level=level):
                self.assertGreaterEqual(woke, 5, heard)
                if command:
                    self.assertGreaterEqual(right, 5, heard)
        self.assertEqual(failures, [])

    def test_2_false_wakes(self):
        # 5 min of room noise with 80 bursts
        rnd = random.Random(7)
        hz = Harness(playback=False)
        amps = [800, 1500, 2500, 4000]
        seg_s = 5.0
        segments = int(300 / seg_s)
        bursts_per_seg = [0] * segments
        for i in range(80):
            bursts_per_seg[i % segments] += 1
        for i in range(segments):
            seg = W.room_noise(seg_s, amps[i % len(amps)], seed=100 + i)
            seg = mix(seg, W.hiss(seg_s, HISS_RMS, seed=200 + i))
            for _ in range(bursts_per_seg[i]):
                at = rnd.uniform(0.2, seg_s - 0.5)
                burst = W.silence(at) + W.noise(rnd.uniform(0.05, 0.2), rnd.choice([6000, 12000, 20000]),
                                                seed=rnd.randrange(10 ** 6))
                seg = mix(seg, burst)
            hz.play(seg)
        noise_wakes, noise_decodes = list(hz.wakes), hz.whisper_decodes()
        # 38 faint commands without the name
        clips = W.synth_many(COMMANDS)
        hz = Harness(playback=False)
        for i, phrase in enumerate(COMMANDS):
            pcm, _ = over_hiss(clips[phrase], 0.035, seed=300 + i, lead=1.0, tail=2.5)
            hz.play(pcm)
        command_wakes, command_decodes = list(hz.wakes), hz.whisper_decodes()
        # sound-alikes: 3 phrasings x (0.035, 0.3 hiss, 0.3 room noise) per word
        alike: dict[str, list] = {}
        for word in SOUND_ALIKES:
            woke = []
            for j, phrase in enumerate((word, f"that is {word}", f"{word} again")):
                clip = W.synth(phrase)
                for k, level in enumerate((0.035, 0.3, 0.3)):
                    hz = Harness(playback=False)
                    pcm, _ = over_hiss(clip, level, seed=400 + 10 * j + k)
                    if k == 2:
                        pcm = mix(pcm, W.room_noise(len(pcm) / (RATE * 2), 800, seed=500 + j))
                    hz.play(pcm)
                    if hz.wakes:
                        woke.append(f"{phrase}@{level}:{hz.wakes[0][2]}")
            alike[word] = woke
        print(f"\n  5 min room noise + 80 bursts: {len(noise_wakes)} wakes, {noise_decodes} Whisper decodes "
              f"{noise_wakes}")
        print(f"  38 faint commands (0.035): {len(command_wakes)} wakes, {command_decodes} Whisper decodes "
              f"{command_wakes}")
        for word, woke in alike.items():
            print(f"  sound-alike {word!r:10}: {len(woke)}/9 woke {woke}")
        self.assertEqual(noise_wakes, [])
        self.assertLessEqual(noise_decodes, 20)
        self.assertEqual(command_wakes, [])
        for word, woke in alike.items():
            with self.subTest(word=word):
                self.assertLessEqual(len(woke), 1, woke)

    def test_3_playback(self):
        clip = W.synth("xyrus")
        bed = music_bed(30.0, seed=3)
        at = 15.0
        voice = W.silence(at) + W.faint(clip, 0.5)
        pcm = mix(mix(bed, voice), W.hiss(30.0, HISS_RMS, seed=9))
        hz = Harness(playback=True)
        start = hz.play(pcm)
        end = start + at + speech_end(clip)
        longest_idle = max(hz.idle_utts, default=0.0)
        after = [t - end for t, _, _ in hz.wakes if t >= start + at]
        print(f"\n  playback: longest Vosk utterance {hz.max_utt / (RATE * 2):.2f} s, longest idle utterance "
              f"{longest_idle:.2f} s ({len(hz.idle_utts)} checked, {hz.whisper_decodes()} Whisper decodes, "
              f"{hz.rec.stats['playback_flushes']} Vosk flushes); wakes {hz.wakes}")
        self.assertLessEqual(hz.max_utt / (RATE * 2), 3.0 + 0.25)
        self.assertLessEqual(longest_idle, 3.0 + 0.1)
        self.assertTrue(after, hz.wakes)
        self.assertLessEqual(after[0], 3.5)
        self.assertEqual([w for w in hz.wakes if w[0] < start + at], [], "no wake from the music itself")


if __name__ == "__main__":
    unittest.main()
