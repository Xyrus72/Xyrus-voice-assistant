"""SLOW (~2-4 min; loads Whisper): the evidence for the Whisper command capture (lead request, Sep 14 2026).

38 English command phrasings are spoken (System.Speech synthesis) as "Xyrus" -> gap -> command, with room noise
mixed in, through the REAL recognizer (Vosk wake word) into the real engine (FakeActions), twice:
  before = today's path: the Vosk command grammar after the wake reply (+ Whisper for song titles only)
  after  = the new path: capture.Endpointer + Whisper for the whole command
A phrase counts as right when the command (and its slots) equals what the same phrase typed runs. Latency =
audio time from the end of the speech to the chunk that finished it + the time spent on that chunk (decode +
engine) - what the user waits between stopping talking and the command running.
Also: one case with 30 s of room noise between the reply and the command, and 20 s of noise-only.

Skipped when the Vosk model or Whisper (models/whisper-small + faster-whisper) is missing.
Run alone: venv\\Scripts\\python.exe -m unittest tests.test_whisper_commands -v
"""
from __future__ import annotations

import os
import re
import statistics
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

if not os.environ.get("XYRUS_DATA_DIR"):
    os.environ["XYRUS_DATA_DIR"] = tempfile.mkdtemp(prefix="xyrus_test_")

from tests import wav_util as W
from tests.test_capture import bounds, mix, wire
from xyrus import paths
from xyrus.testing import FakeChime, FakeSpeaker, make_test_engine

HAVE_MODEL = (paths.MODEL_DIR / "am" / "final.mdl").exists()
RATE = 16000
GAP_S = 1.2
NOISE_AMP = 700
COMMANDS = [
    "set the volume to forty percent", "volume up", "turn it down a bit", "mute the sound",
    "set the brightness to seventy percent", "make the screen darker", "open chrome", "open notepad",
    "open calculator", "close notepad", "what time is it", "what's the date today", "set a timer for five minutes",
    "set a tea timer for three minutes", "take a screenshot", "what can you do", "tell me a joke",
    "system status", "how much battery do I have", "what's the volume", "turn up the brightness", "open youtube",
    "close calculator", "play shape of you by ed sheeran", "play believer by imagine dragons",
    "play faded by alan walker", "play blinding lights by the weeknd", "play alone by alan walker",
    "search for cheap mechanical keyboards", "search youtube for lofi beats", "pause the music", "next song",
    "previous song", "show desktop", "minimize this window", "what is twelve times four", "flip a coin",
    "roll a dice",
]   # no calendar / reminder / note / memory phrasings in this end-to-end set (user rule, Sep 14)
# Hearing rebuild (Sep 15 2026): request shapes the user actually says - questions around the command and bare
# "<title> by <artist>" - checked at the transcript level (hearing.transcribe, what capture hands the engine).
QUESTION_FORMS = [
    "can you play shape of you by ed sheeran", "could you put on tired by alan walker", "i want to hear faded",
    "listen to believer", "play me shape of you", "can you please play faded", "can you tell me a joke",
    "could you please tell me a joke", "can i tell me a joke", "would you mute the sound",
    "could you take a screenshot", "can you open chrome",
]
BARE_TITLES = ["shape of you by ed sheeran", "tired by alan walker", "stand by me", "believer by imagine dragons"]
HISS_RMS = 0.0033                                            # the user's room floor
FAINT_PEAK = 0.04                                            # the user's seat: speech peaks 0.03-0.05
hearing = None
MODEL = None


def setUpModule():
    global hearing
    base = str(paths.BASE)
    if base not in sys.path:
        sys.path.insert(0, base)
    try:
        import hearing as H
    except Exception as e:                                   # noqa: BLE001
        raise unittest.SkipTest(f"hearing.py unavailable: {e!r}")
    # one load per process (tests.test_hearing may already have started or finished it)
    if H._model is None and not any(t.name in ("whisper-load", "whisper-reload") for t in threading.enumerate()):
        H.load_async()
    if not H.available(wait=240):
        raise unittest.SkipTest(f"Whisper unavailable: {H.error}")
    hearing = H
    W.synth_many(["xyrus"] + COMMANDS + QUESTION_FORMS + BARE_TITLES)


_ONES = ("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
         "seventeen eighteen nineteen").split()
_TENS = {2: "twenty", 3: "thirty", 4: "forty", 5: "fifty", 6: "sixty", 7: "seventy", 8: "eighty", 9: "ninety"}


def norm(text: str) -> str:
    """Compare-ready transcript: lower, '%' -> percent, '12 x 4' -> times, digits -> words, no punctuation."""
    def words(n: int) -> str:
        if n < 20:
            return _ONES[n]
        if n < 100:
            return _TENS[n // 10] + ("" if n % 10 == 0 else " " + _ONES[n % 10])
        return "one hundred" if n == 100 else str(n)
    t = text.lower().replace("%", " percent ")
    t = re.sub(r"(\d)\s*[x×]\s*(\d)", r"\1 times \2", t)
    t = re.sub(r"[^\w\s']", " ", t)
    return " ".join(words(int(w)) if w.isdigit() and int(w) <= 100 else w for w in t.split())


def word_errors(ref: str, hyp: str) -> tuple[int, int]:
    r, h = ref.split(), hyp.split()
    d = list(range(len(h) + 1))
    for i in range(1, len(r) + 1):
        prev, d[0] = d[0], i
        for j in range(1, len(h) + 1):
            cur = d[j]
            d[j] = min(d[j] + 1, d[j - 1] + 1, prev + (r[i - 1] != h[j - 1]))
            prev = cur
    return d[len(h)], len(r)


def in_room(pcm: bytes, seed: int) -> bytes:
    """pcm with 0.5 s either side, inside the room's hiss."""
    from array import array
    pcm = W.silence(0.5) + pcm + W.silence(0.5)
    a, b = array("h"), array("h")
    a.frombytes(pcm)
    b.frombytes(W.hiss(len(pcm) / (RATE * 2), HISS_RMS, seed=seed))
    return array("h", (max(-32768, min(32767, x + y)) for x, y in zip(a, b))).tobytes()   # local: not test_capture's


def score(phrases, peak: float | None, seed0: int):
    """-> (exact count, word errors, reference words, [(phrase, heard)] misses) for hearing.transcribe."""
    ok, edits, words, misses = 0, 0, 0, []
    for i, phrase in enumerate(phrases):
        speech = W.synth(phrase)
        pcm = in_room(W.faint(speech, peak) if peak is not None else speech, seed0 + i)
        heard = hearing.transcribe(pcm)
        e, n = word_errors(norm(phrase), norm(heard))
        edits, words = edits + e, words + n
        if norm(heard) == norm(phrase):
            ok += 1
        else:
            misses.append((phrase, heard))
    return ok, edits, words, misses


def sig(text: str) -> str:
    return re.sub(r"[^a-z0-9=]", "", text.lower())


def typed_signature(phrase: str) -> str | None:
    e, _ = make_test_engine(tmpdir=Path(tempfile.mkdtemp(prefix="xyrus_ev_")))
    e.handle(phrase, "typed")
    got = [ev.text for ev in e.events if ev.kind == "cmd"]
    return sig(got[0]) if got else None


def build(after: bool):
    global MODEL
    from xyrus.app import CommandHearing
    from xyrus.grammar import GrammarBuilder, Vocab
    from xyrus.recognizer import Recognizer
    e, ns = make_test_engine(tmpdir=Path(tempfile.mkdtemp(prefix="xyrus_ev_")),
                             config_overrides={"command_window_s": 0 if after else 15})
    heard: list = []
    vocab = Vocab(paths.MODEL_DIR, cache_file=Path(tempfile.mkdtemp()) / "vocab_cache.json")

    def on_tr(tr):
        heard.append(tr)
        e.handle_transcript(tr)
    rec = Recognizer(paths.MODEL_DIR, None, GrammarBuilder(ns.registry, ns.config, vocab), get_spec=e.listen_spec,
                     on_transcript=on_tr, speaker=FakeSpeaker(), chime=FakeChime(), config=ns.config)
    if MODEL is None:
        MODEL = rec.load()
    rec._model = MODEL
    vocab.attach(rec.check_words)
    rec.set_free_decoder(lambda pcm: hearing.clean(hearing.transcribe(pcm)))     # today: Whisper for titles
    e.request_free_decode = rec.request_free_decode
    if after:
        rec.set_command_hearing(CommandHearing(hearing))
        wire(e, rec)
    return e, ns, rec, heard


def run_case(phrase: str, after: bool, *, gap: float = GAP_S, amp: int = NOISE_AMP, seed: int = 0):
    """-> (signature or None, what was heard, latency s or None)"""
    e, ns, rec, heard = build(after)
    wake, cmd = W.synth("xyrus"), W.synth(phrase)
    lead = W.silence(0.8) + wake + W.silence(gap)
    pcm = lead + cmd + W.silence(3.0)
    pcm = mix(pcm, W.room_noise(len(pcm) / (RATE * 2), amp, seed=seed))
    t0 = 100.0
    speech_end = t0 + len(lead) / (RATE * 2) + bounds(cmd)[1]
    t, done_at, spent = t0, None, 0.0
    for c in W.chunks(pcm):
        t += len(c) / (RATE * 2)
        n = sum(1 for ev in e.events if ev.kind == "cmd")
        w = time.perf_counter()
        rec._process(c, t)
        if done_at is None and sum(1 for ev in e.events if ev.kind == "cmd") > n:
            done_at, spent = t, time.perf_counter() - w
    got = [ev.text for ev in e.events if ev.kind == "cmd"]
    words = " | ".join((getattr(tr, "raw", "") or tr.text) for tr in heard[1:]) or "-"
    latency = (done_at - speech_end + spent) if done_at is not None else None
    return (sig(got[0]) if got else None), words, latency, e, ns, rec


class WhisperTranscripts(unittest.TestCase):
    """SLOW: real Whisper only (no Vosk), the transcript capture hands the engine, exact after normalising
    number formatting ('40%' = 'forty percent')."""

    def test_normal_and_faint_command_phrases(self):
        n_ok, n_e, n_w, n_miss = score(COMMANDS, None, 0)
        f_ok, f_e, f_w, f_miss = score(COMMANDS, FAINT_PEAK, 100)
        print(f"\n  {hearing.model_name()}: normal {n_ok}/{len(COMMANDS)} WER {100 * n_e / n_w:.1f}% | faint "
              f"(peak {FAINT_PEAK}) {f_ok}/{len(COMMANDS)} WER {100 * f_e / f_w:.1f}%")
        for phrase, heard in n_miss + f_miss:
            print(f"    {phrase!r:44} -> {heard!r}")
        self.assertGreaterEqual(n_ok, 35)
        self.assertGreaterEqual(f_ok, 33)
        self.assertLessEqual(f_e / f_w, 0.07)

    def test_question_forms_and_bare_titles(self):
        for peak, need, seed0 in ((0.5, 11, 200), (FAINT_PEAK, 10, 300)):
            q_ok, _e, _w, q_miss = score(QUESTION_FORMS, peak, seed0)
            b_ok, _e, _w, b_miss = score(BARE_TITLES, peak, seed0 + 50)
            print(f"\n  peak {peak}: question forms {q_ok}/{len(QUESTION_FORMS)}, bare titles "
                  f"{b_ok}/{len(BARE_TITLES)}")
            for phrase, heard in q_miss + b_miss:
                print(f"    {phrase!r:44} -> {heard!r}")
            self.assertGreaterEqual(q_ok, need, f"peak {peak}: {q_miss}")
            self.assertGreaterEqual(b_ok, len(BARE_TITLES) - 1, f"peak {peak}: {b_miss}")

    def test_twenty_seconds_of_noise_is_no_transcript(self):
        from xyrus.capture import only_filler
        for i, pcm in enumerate((W.room_noise(20, 2500, seed=5), W.hiss(20, HISS_RMS, seed=6))):
            heard = hearing.transcribe(pcm)
            self.assertTrue(not heard or only_filler(heard, hearing.PROMPT), (i, heard))


class WhisperCommandEvidence(unittest.TestCase):
    """SLOW: real Vosk + real Whisper, synthesized speech with room noise."""

    @classmethod
    def setUpClass(cls):
        if not HAVE_MODEL:
            raise unittest.SkipTest("speech model missing")

    def test_accuracy_and_latency_before_vs_after(self):
        rows = []
        for i, phrase in enumerate(COMMANDS):
            want = typed_signature(phrase)
            self.assertIsNotNone(want, f"{phrase!r} typed runs no command")
            b_sig, b_heard, b_lat, *_ = run_case(phrase, after=False, seed=i)
            a_sig, a_heard, a_lat, *_ = run_case(phrase, after=True, seed=i)
            rows.append((phrase, want, b_sig == want, b_heard, b_lat, a_sig == want, a_heard, a_lat))
        n = len(rows)
        b_ok = sum(r[2] for r in rows)
        a_ok = sum(r[5] for r in rows)
        b_lat = [r[4] for r in rows if r[2] and r[4] is not None]
        a_lat = [r[7] for r in rows if r[5] and r[7] is not None]
        print(f"\n  {n} commands, room noise amp {NOISE_AMP}, {GAP_S} s after the name")
        print(f"  BEFORE (Vosk command grammar): {b_ok}/{n} right, latency median "
              f"{statistics.median(b_lat):.2f} s max {max(b_lat):.2f} s")
        print(f"  AFTER  (capture + Whisper):    {a_ok}/{n} right, latency median "
              f"{statistics.median(a_lat):.2f} s max {max(a_lat):.2f} s")
        for phrase, want, bok, bh, bl, aok, ah, al in rows:
            if not (bok and aok):
                print(f"    {phrase!r:48} before {'ok ' if bok else 'BAD'} {bh!r:40} after {'ok ' if aok else 'BAD'} "
                      f"{ah!r}")
        self.assertGreaterEqual(n, 30)
        self.assertGreaterEqual(a_ok, b_ok)
        self.assertGreaterEqual(a_ok / n, 0.9)
        self.assertLessEqual(statistics.median(a_lat), 3.0)

    def test_thirty_seconds_of_room_noise_between_reply_and_command(self):
        s, heard, lat, e, ns, rec = run_case("what time is it", after=True, gap=30.0, seed=77)
        print(f"\n  30 s gap: heard {heard!r}, latency {lat:.2f} s")
        self.assertEqual(s, typed_signature("what time is it"))
        self.assertEqual(rec.stats["captures"], 1)

    def test_noise_only_is_never_a_command(self):
        e, ns, rec, heard = build(after=True)
        pcm = W.silence(0.8) + W.synth("xyrus") + W.silence(0.5)
        pcm += W.room_noise(20, 2500, seed=5)
        t = 100.0
        for c in W.chunks(pcm):
            t += len(c) / (RATE * 2)
            rec._process(c, t)
        self.assertEqual(rec.stats["captures"], 0)
        self.assertEqual(len(ns.speaker.said), 1)             # only "Waiting for your command"
        self.assertEqual(e.snapshot().mode, "armed")


if __name__ == "__main__":
    unittest.main()
