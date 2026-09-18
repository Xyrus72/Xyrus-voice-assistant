"""hearing.py after the hearing rebuild (Sep 15 2026): the prompt, the echo guard, the confidence fields, n-best,
the Bengali specialist hook and the runtime degrade.

CPU-only (always run, no model): the prompt_echo table, min_logprob applied only when passed, the PROMPT
alias, the compression / repeat drops, the degrade chain with fake models, the Bengali specialist absent.

SLOW GPU (skipped when no Whisper model is installed): the B1 prompt A/B on synthesized faint speech
(peak 0.04 in rms-0.0033 room hiss - the user's seat), noise clips, faint "xyrus", avg_logprob separation,
n-best. No calendar / reminder phrase is spoken anywhere in this file (user rule).

B1 result (large-v3 fp16, Sep 15 2026, see the lead's report): the shipped PROMPT_CMD had the best faint WER
of the prompts with <= 1/45 sound-alike name hits. The A/B test re-measures it and fails if that stops holding.
Run alone: venv\\Scripts\\python.exe -m unittest tests.test_hearing -v
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

if not os.environ.get("XYRUS_DATA_DIR"):
    os.environ["XYRUS_DATA_DIR"] = tempfile.mkdtemp(prefix="xyrus_test_")

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

import hearing as H  # noqa: E402
from tests import wav_util as W  # noqa: E402

RATE = 16000
HISS = 0.0033
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
]
NAME_PHRASES = ["xyrus", "hey xyrus", "xyrus are you there", "xyrus what time is it", "xyrus open chrome"]
SOUND_ALIKES = ["serious", "sirius", "iris", "virus", "cirrus"]
NAME_RE = re.compile(r"\b[xzcs](?:y|i|e|ai)r(?:u|o|a)s\b")
FORBIDDEN_IN_PROMPT = ("remind", "doctor", "sheeran", "shape of you", "believer", "faded", "walker", "vikings",
                       "ishwar", "tomorrow")


# ============================================================================ helpers
def _have_gpu_model() -> bool:
    if not H.CANDIDATES:
        return False
    try:
        H._add_runtime_dlls()
        import faster_whisper  # noqa: F401
        return True
    except Exception:                                        # noqa: BLE001
        return False


def _have_faster_whisper() -> bool:
    try:
        H._add_runtime_dlls()
        import faster_whisper.audio  # noqa: F401
        import numpy  # noqa: F401
        return True
    except Exception:                                        # noqa: BLE001
        return False


_ONES = ("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
         "seventeen eighteen nineteen").split()
_TENS = {2: "twenty", 3: "thirty", 4: "forty", 5: "fifty", 6: "sixty", 7: "seventy", 8: "eighty", 9: "ninety"}


def _num_words(n: int) -> str:
    if n < 20:
        return _ONES[n]
    if n < 100:
        return _TENS[n // 10] + ("" if n % 10 == 0 else " " + _ONES[n % 10])
    return "one hundred" if n == 100 else str(n)


def norm(text: str) -> str:
    """Compare-ready: lower, '%' -> percent, digits -> words, no punctuation (number formatting normalised)."""
    t = text.lower().replace("%", " percent ")
    t = re.sub(r"(\d)\s*[x×]\s*(\d)", r"\1 times \2", t)
    t = re.sub(r"[^\w\s']", " ", t)
    return " ".join(_num_words(int(w)) if w.isdigit() and int(w) <= 100 else w for w in t.split())


def wer(ref: str, hyp: str) -> tuple[int, int]:
    r, h = ref.split(), hyp.split()
    d = list(range(len(h) + 1))
    for i in range(1, len(r) + 1):
        prev, d[0] = d[0], i
        for j in range(1, len(h) + 1):
            cur = d[j]
            d[j] = min(d[j] + 1, d[j - 1] + 1, prev + (r[i - 1] != h[j - 1]))
            prev = cur
    return d[len(h)], len(r)


def mix(a: bytes, b: bytes) -> bytes:
    import numpy as np
    x = np.frombuffer(a, dtype=np.int16).astype(np.int32)
    y = np.frombuffer(b, dtype=np.int16).astype(np.int32)
    out = np.zeros(max(len(x), len(y)), dtype=np.int32)
    out[:len(x)] += x
    out[:len(y)] += y
    return np.clip(out, -32768, 32767).astype(np.int16).tobytes()


def faint_clip(phrase: str, peak: float = 0.04, seed: int = 0, pad: float = 0.5) -> bytes:
    """The phrase scaled to `peak`, `pad` s either side, all inside the room's hiss."""
    pcm = W.silence(pad) + W.faint(W.synth(phrase), peak) + W.silence(pad)
    return mix(pcm, W.hiss(len(pcm) / (RATE * 2), HISS, seed=seed))


def noise_clips() -> list[tuple[str, bytes]]:
    """50 noise-only clips: 0.6/0.8/1.5/3.0 s x 10 seeds at the room floor + 10 x 1.5 s at 3x louder."""
    out = [(f"hiss {s}s seed {seed}", W.hiss(s, HISS, seed=seed)) for s in (0.6, 0.8, 1.5, 3.0) for seed in range(10)]
    out += [(f"hiss 1.5s x3 seed {seed}", W.hiss(1.5, HISS * 3, seed=100 + seed)) for seed in range(10)]
    return out


def filler(text: str, prompt) -> bool:
    """capture.only_filler (agent A's) when importable, else a minimal local stand-in."""
    try:
        from xyrus import capture as C
        fn = getattr(C, "only_filler", None)
    except Exception:                                        # noqa: BLE001
        fn = None
    if fn is not None:
        try:
            return bool(fn(text, prompt))
        except TypeError:
            return bool(fn(text))
    t = H.clean(text)
    return not t or t in {"you", "thank you", "thanks for watching", "bye", "amen", "okay", "oh", "uh", "um"}


def ensure_loaded(wait: float = 300) -> bool:
    """One load per process (test_whisper_commands shares the module): start it only if nobody has."""
    if H._model is None and not any(t.name in ("whisper-load", "whisper-reload") for t in threading.enumerate()):
        H._ready.clear()
        H.load_async()
    return H.available(wait=wait)


class _Globals:
    """Snapshot / restore of hearing's module state around the fake-model tests."""
    NAMES = ("_model", "_model_name", "_compute", "device", "_cand_pos", "_fails", "error", "_bn_model",
             "_bn_fails")

    def __enter__(self):
        self.saved = {n: getattr(H, n) for n in self.NAMES}
        self.ready = H._ready.is_set()
        return self

    def __exit__(self, *exc):
        for n, v in self.saved.items():
            setattr(H, n, v)
        (H._ready.set if self.ready else H._ready.clear)()
        return False


# ============================================================================ fakes (CPU)
class FakeFeatures:
    nb_max_frames = 3000

    def __call__(self, audio):
        import numpy as np
        return np.zeros((80, 100), dtype=np.float32)


class FakeResult:
    def __init__(self, no_speech=0.0):
        self.sequences_ids = [[1, 2, 3]]
        self.no_speech_prob = no_speech


class FakeTok:
    non_speech_tokens = ()
    transcribe, translate, sot, sot_prev, sot_lm, no_speech = 1, 2, 3, 4, 5, 6

    def __init__(self, text):
        self.text = text

    def decode(self, ids):
        return self.text


class FakeModel:
    """Enough of a WhisperModel for Heard: encode + generate_with_fallback, either of which can be broken."""

    def __init__(self, name, text="Open Chrome.", avg=-0.1, ratio=1.2):
        self.name, self.text, self.avg, self.ratio = name, text, avg, ratio
        self.feature_extractor = FakeFeatures()
        self.broken_encode = False
        self.broken_decode = False
        self.decodes = 0

    def encode(self, feats):
        if self.broken_encode:
            raise RuntimeError(f"CUDA failed ({self.name})")
        return ("enc", self.name)

    def generate_with_fallback(self, enc, tokens, tok, opts):
        if self.broken_decode:
            raise RuntimeError(f"CUDA failed ({self.name})")
        self.decodes += 1
        tok.text = self.text
        return FakeResult(), self.avg, 0.0, self.ratio


def fake_heard(text, avg=-0.1, ratio=1.2):
    model = FakeModel("fake", text=text, avg=avg, ratio=ratio)
    h = H.Heard.__new__(H.Heard)
    h._pcm, h._lock, h._primary, h.created = b"", threading.Lock(), False, time.time()
    h.avg_logprob = h.compression = h.no_speech = None
    h.fallback_used, h.encode_s = False, 0.0
    h._enc, h._model = ("enc",), model
    h._tokenizer = lambda language: FakeTok(text)
    h._prompt_tokens = lambda tok, prompt: []
    return h


# ============================================================================ CPU tests
class PromptConstants(unittest.TestCase):
    def test_prompt_alias_and_constants(self):
        self.assertIs(H.PROMPT, H.PROMPT_CMD)
        self.assertEqual(H.PROMPT_CMD, "Xyrus, open Chrome and play a song.")     # B1 winner, see hearing.py
        self.assertEqual((H.LOGPROB_DROP, H.LOGPROB_UNSURE), (-0.9, -0.45))
        self.assertIn("doctor", H._PROMPT_LEGACY)
        for fn in ("prompt_echo", "model_name", "bn_available", "listen", "listen_bn", "_add_runtime_dlls"):
            self.assertTrue(callable(getattr(H, fn)), fn)

    def test_shipped_prompt_names_no_song_artist_or_reminder(self):
        low = H.PROMPT.lower()
        for word in FORBIDDEN_IN_PROMPT:
            self.assertNotIn(word, low)

    def test_candidates_and_model_name(self):
        wanted = [(H.LARGE_DIR, "cuda", "float16"), (H.LARGE_DIR, "cuda", "int8_float16"),
                  (H.TURBO_DIR, "cuda", "float16"), (H.SMALL_DIR, "cuda", "float16"), (H.SMALL_DIR, "cpu", "int8")]
        self.assertEqual(list(H.CANDIDATES), [c for c in wanted if (c[0] / "model.bin").exists()])
        self.assertIsInstance(H.model_name(), str)

    def test_pin_moves_candidate_first(self):
        if len(H.CANDIDATES) < 2:
            self.skipTest("needs two installed candidates")
        with mock.patch.dict(os.environ, {"XYRUS_WHISPER": "large-v3-int8"}):
            order = H._ordered()
        if (H.LARGE_DIR, "cuda", "int8_float16") in H.CANDIDATES:
            self.assertEqual(order[0], (H.LARGE_DIR, "cuda", "int8_float16"))
            self.assertEqual(sorted(map(str, order)), sorted(map(str, H.CANDIDATES)))
        with mock.patch.dict(os.environ, {"XYRUS_WHISPER": "auto"}):
            self.assertEqual(H._ordered(), H.CANDIDATES)
        with mock.patch.dict(os.environ, {"XYRUS_WHISPER": ""}), \
                mock.patch.object(H, "_config_value", lambda k, d: "turbo" if k == "hearing_model" else d):
            if (H.TURBO_DIR, "cuda", "float16") in H.CANDIDATES:
                self.assertEqual(H._ordered()[0], (H.TURBO_DIR, "cuda", "float16"))

    def test_config_value_defaults_on_missing_file(self):
        self.assertEqual(H._config_value("no_such_key_xyz", "dflt"), "dflt")


class PromptEchoTable(unittest.TestCase):
    ECHO = [
        ("I'm going to call the doctor tomorrow at five.", H.PROMPT_CMD),     # the leak the user saw
        ("Call the doctor.", H.PROMPT_CMD),
        ("The doctor tomorrow at five.", None),
        ("Xyrus, open Chrome and play a song.", H.PROMPT_CMD),           # the whole prompt read back
        ("Open Chrome and play a song.", H.PROMPT_CMD),
        ("Chrome and play.", H.PROMPT_CMD),
        ("Xyrus, open Chrome and play a song.", "Xyrus."),               # 4-word window, any prompt
        ("Chrome and play a", None),
        ("Play Shape of You by Ed Sheeran.", H._PROMPT_LEGACY),          # the old prompt, while it is in use
        ("Shape of you by Ed Sheeran.", H._PROMPT_LEGACY),
    ]
    NOT_ECHO = [
        ("", H.PROMPT_CMD), ("Xyrus.", H.PROMPT_CMD), ("Xyrus.", "Xyrus."), ("Cyrus?", H.PROMPT_CMD),
        ("What time is it?", H.PROMPT_CMD), ("what time is it", H.PROMPT_CMD), ("Open Chrome.", H.PROMPT_CMD),
        ("Play a song.", H.PROMPT_CMD), ("What's the time?", H.PROMPT_CMD), ("Xyrus, what time is it?", H.PROMPT_CMD),
        ("Xyrus, open Chrome.", H.PROMPT_CMD), ("What time is it now?", H.PROMPT_CMD),
        ("Play Shape of You by Ed Sheeran.", H.PROMPT_CMD), ("Play Shape of You.", H.PROMPT_CMD),
        ("Shape of you.", H.PROMPT_CMD), ("Can you play Shape of You by Ed Sheeran?", H.PROMPT_CMD),
        ("Tell me a joke.", H.PROMPT_CMD), ("Set a timer for five minutes.", H.PROMPT_CMD),
        ("Open Chrome.", H._PROMPT_LEGACY), ("Play Tired by Alan Walker.", H._PROMPT_LEGACY),
        ("Open notepad.", None), ("Play a song by Vikings.", H.PROMPT_CMD),
        # real requests that merely contain a 4-word window of the prompt (review, Sep 15: the window test
        # alone dropped these silently and Xyrus looked deaf)
        ("Can you open Chrome and play something?", H.PROMPT_CMD), ("Open Spotify and play a song.", H.PROMPT_CMD),
        ("Xyrus, open YouTube and play a song.", H.PROMPT_CMD), ("Go ahead and play a song.", H.PROMPT_CMD),
        ("Open Chrome and play Faded by Alan Walker.", H.PROMPT_CMD),
    ]

    def test_table(self):
        for text, prompt in self.ECHO:
            self.assertTrue(H.prompt_echo(text, prompt), (text, prompt))
        for text, prompt in self.NOT_ECHO:
            self.assertFalse(H.prompt_echo(text, prompt), (text, prompt))

    def test_every_three_word_window_of_the_legacy_prompt(self):
        words = H.clean(H._PROMPT_LEGACY).split()
        for i in range(len(words) - 2):
            window = " ".join(words[i:i + 3])
            self.assertTrue(H.prompt_echo(window, H._PROMPT_LEGACY), window)
            if "doctor" in window:                                     # the leak guard holds under any prompt
                for prompt in (H.PROMPT_CMD, None, "Xyrus."):
                    self.assertTrue(H.prompt_echo(window, prompt), (window, prompt))

    def test_every_four_word_window_of_the_shipped_prompt(self):
        words = H.clean(H.PROMPT).split()
        for i in range(len(words) - 3):
            window = " ".join(words[i:i + 4])
            if window in H.EXEMPT:                                     # "what time is it" is a command
                self.assertFalse(H.prompt_echo(window, H.PROMPT), window)
                continue
            self.assertTrue(H.prompt_echo(window, H.PROMPT), window)
            self.assertEqual(fake_heard(window.capitalize() + ".").text(prompt=H.PROMPT), "", window)

    def test_exempt_phrases_never_echo(self):
        for phrase in H.EXEMPT:
            for prompt in (H.PROMPT_CMD, H._PROMPT_LEGACY, None, phrase):
                self.assertFalse(H.prompt_echo(phrase, prompt), (phrase, prompt))


@unittest.skipUnless(_have_faster_whisper(), "faster-whisper not importable")
class TextDropsCPU(unittest.TestCase):
    def test_min_logprob_only_when_passed(self):
        with self.assertLogs("xyrus.hearing", "INFO") as logs:
            self.assertEqual(fake_heard("Open notepad.", avg=-1.2).text(), "Open notepad.")     # advisory only
        self.assertTrue(any("whisper drop:" in m and "advisory" in m for m in logs.output), logs.output)
        self.assertEqual(fake_heard("Open notepad.", avg=-1.2).text(min_logprob=H.LOGPROB_DROP), "")
        self.assertEqual(fake_heard("Open notepad.", avg=-0.5).text(min_logprob=H.LOGPROB_DROP), "Open notepad.")
        self.assertEqual(fake_heard("Open notepad.", avg=-0.5).text(min_logprob=-0.4), "")

    def test_fields_after_text(self):
        h = fake_heard("Open notepad.", avg=-0.2, ratio=1.1)
        h.text()
        self.assertAlmostEqual(h.avg_logprob, -0.2)
        self.assertAlmostEqual(h.compression, 1.1)
        self.assertEqual(h.no_speech, 0.0)
        self.assertFalse(h.fallback_used)

    def test_compression_and_repeat_dropped(self):
        self.assertEqual(fake_heard("Open notepad.", ratio=2.6).text(), "")
        self.assertEqual(fake_heard("Bye. Bye. Bye. Bye.").text(), "")
        self.assertEqual(fake_heard("No, no, no.").text(), "No, no, no.")          # three is still a reply

    def test_echo_dropped_by_text(self):
        self.assertEqual(fake_heard("I'm going to call the doctor tomorrow at five.").text(), "")
        self.assertEqual(fake_heard("What time is it?").text(), "What time is it?")


@unittest.skipUnless(_have_faster_whisper(), "faster-whisper not importable")
class DegradeCPU(unittest.TestCase):
    """Fake models on fake candidates: two failures in a row replace the model, the utterance is redone."""

    def setUp(self):
        self.a, self.b = FakeModel("A"), FakeModel("B")
        self.cands = ((Path("A"), "cuda", "float16"), (Path("B"), "cuda", "int8_float16"))
        models = {"A": self.a, "B": self.b}
        self.g = _Globals().__enter__()
        self.patches = [mock.patch.object(H, "CANDIDATES", self.cands),
                        mock.patch.object(H, "_ordered", lambda: self.cands),
                        mock.patch.object(H, "_try_load", lambda d, dev, ct: models[d.name]),
                        mock.patch.object(H, "BN_DIR", Path(tempfile.mkdtemp()) / "absent"),
                        mock.patch.object(H.Heard, "_tokenizer", lambda self, language: FakeTok("")),
                        mock.patch.object(H.Heard, "_prompt_tokens", lambda self, tok, prompt: [])]
        for p in self.patches:
            p.start()
        H._model, H._bn_model = None, None
        H._load(0)
        self.assertEqual(H.model_name(), "A")

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        for t in threading.enumerate():                 # let a reload thread finish before restoring
            if t.name == "whisper-reload":
                t.join(5)
        self.g.__exit__(None, None, None)

    def test_encode_failing_twice_loads_next_candidate(self):
        self.a.broken_encode = True
        with self.assertLogs("xyrus.hearing", "WARNING") as logs:
            heard = H.listen(W.silence(0.5))
        self.assertIsNotNone(heard)
        self.assertIs(heard._model, self.b)
        self.assertEqual(H.model_name(), "B")
        self.assertTrue(any("ERROR" in m and "dead" in m for m in logs.output), logs.output)

    def test_decode_failing_twice_is_redone_on_next_candidate(self):
        heard = H.listen(W.silence(0.5))
        self.assertIs(heard._model, self.a)
        self.a.broken_decode = True
        self.assertEqual(heard.text(), "Open Chrome.")
        self.assertEqual(self.b.decodes, 1)
        self.assertEqual(H.model_name(), "B")

    def test_one_failure_is_retried_on_the_same_model(self):
        calls = {"n": 0}
        real = self.a.encode

        def flaky(feats):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("transient")
            return real(feats)
        self.a.encode = flaky
        heard = H.listen(W.silence(0.5))
        self.assertIs(heard._model, self.a)
        self.assertEqual(H._fails, 0)

    def test_all_dead_listen_returns_none(self):
        self.a.broken_encode = self.b.broken_encode = True
        self.assertIsNone(H.listen(W.silence(0.5)))
        self.assertIsNone(H.listen(W.silence(0.5)))
        self.assertFalse(H.available())
        self.assertEqual(H.model_name(), "")


@unittest.skipUnless(_have_faster_whisper(), "faster-whisper not importable")
class BanglaAbsentCPU(unittest.TestCase):
    def test_absent_dir_leaves_specialist_off(self):
        with _Globals(), mock.patch.object(H, "BN_DIR", Path(tempfile.mkdtemp()) / "whisper-large-v3-bn"):
            H._bn_model = None
            H._load_bn("cuda")
            self.assertFalse(H.bn_available())
            self.assertIsNone(H.listen_bn(W.silence(0.5)))

    def test_config_off_skips_load(self):
        d = Path(tempfile.mkdtemp())
        (d / "model.bin").write_bytes(b"")
        loads = []
        with _Globals(), mock.patch.object(H, "BN_DIR", d), \
                mock.patch.object(H, "_config_value", lambda k, dflt: "off" if k == "bangla_model" else dflt), \
                mock.patch.object(H, "_try_load", lambda *a: loads.append(a)):
            H._bn_model = None
            H._load_bn("cuda")
            self.assertFalse(H.bn_available())
        self.assertEqual(loads, [])

    def test_cpu_fallback_only_when_primary_on_cpu(self):
        d = Path(tempfile.mkdtemp())
        (d / "model.bin").write_bytes(b"")
        for primary, want in (("cuda", [("cuda", "float16")]), ("cpu", [("cuda", "float16"), ("cpu", "int8")])):
            loads = []
            with _Globals(), mock.patch.object(H, "BN_DIR", d), \
                    mock.patch.object(H, "_config_value", lambda k, dflt: dflt), \
                    mock.patch.object(H, "_try_load", lambda md, dev, ct: loads.append((dev, ct))):
                H._bn_model = None
                H._load_bn(primary)
            self.assertEqual(loads, want, primary)

    def test_specialist_failures_never_touch_the_primary(self):
        with _Globals():
            primary, bn = FakeModel("P"), FakeModel("BN")
            H._model, H._bn_model, H._bn_fails, H._fails = primary, bn, 0, 0
            bn.broken_encode = True
            self.assertIsNone(H.listen_bn(W.silence(0.5)))
            self.assertIsNone(H.listen_bn(W.silence(0.5)))
            self.assertFalse(H.bn_available())
            self.assertIs(H._model, primary)
            self.assertEqual(H._fails, 0)


# ============================================================================ GPU tests
@unittest.skipUnless(_have_gpu_model(), "no Whisper model / faster-whisper")
class HearingOnGPU(unittest.TestCase):
    """SLOW: the real model on synthesized faint speech in room hiss."""

    @classmethod
    def setUpClass(cls):
        if not ensure_loaded():
            raise unittest.SkipTest(f"Whisper unavailable: {H.error}")
        W.synth_many(COMMANDS + NAME_PHRASES + SOUND_ALIKES)
        cls.faint38 = [(p, faint_clip(p, 0.04, seed=i)) for i, p in enumerate(COMMANDS)]
        cls.large = H.model_name() == "whisper-large-v3"

    def _need_large(self):
        if not self.large:
            self.skipTest(f"thresholds are for whisper-large-v3, loaded {H.model_name()!r}")

    def test_prompt_ab(self):
        """B1: PROMPT_CMD vs 'Xyrus.' vs None vs the legacy prompt - faint WER, name phrases, sound-alike hits."""
        self._need_large()
        prompts = [("PROMPT_CMD", H.PROMPT_CMD), ("3-sentence", "Xyrus, open Chrome. What time is it? Play a song."),
                   ("Xyrus.", "Xyrus."), ("None", None), ("legacy", H._PROMPT_LEGACY)]
        names = [(p, faint_clip(p, 0.04, seed=50 + i)) for i, p in enumerate(NAME_PHRASES)]
        alikes, k = [], 0
        for w in SOUND_ALIKES:
            for peak in (0.03, 0.04, 0.06):
                for seed in range(3):
                    alikes.append((w, faint_clip(w, peak, seed=200 + k)))
                    k += 1
        self.assertEqual(len(alikes), 45)
        stats = {label: {"ok": 0, "edits": 0, "words": 0, "names": 0, "hits": 0} for label, _ in prompts}
        for phrase, pcm in self.faint38:
            heard = H.listen(pcm)
            for label, prompt in prompts:
                got = norm(heard.text(prompt=prompt))
                e, n = wer(norm(phrase), got)
                s = stats[label]
                s["ok"] += got == norm(phrase)
                s["edits"] += e
                s["words"] += n
        for phrase, pcm in names:
            heard = H.listen(pcm)
            for label, prompt in prompts:
                stats[label]["names"] += bool(NAME_RE.search(heard.text(prompt=prompt).lower()))
        for w, pcm in alikes:
            heard = H.listen(pcm)
            for label, prompt in prompts:
                stats[label]["hits"] += bool(NAME_RE.search(heard.text(prompt=prompt).lower()))
        print(f"\n  B1 prompt A/B on {H.model_name()} ({H._compute}), faint peak 0.04:")
        for label, _ in prompts:
            s = stats[label]
            s["wer"] = 100 * s["edits"] / s["words"]
            print(f"    {label:11} faint38 exact {s['ok']:2}/38 WER {s['wer']:4.1f}% | name phrases {s['names']}/5 | "
                  f"sound-alike name hits {s['hits']}/45")
        shipped = next(label for label, p in prompts if p == H.PROMPT)
        ok = [label for label, _ in prompts if stats[label]["hits"] <= 1]
        self.assertIn(shipped, ok)
        self.assertLessEqual(stats[shipped]["wer"], 7.0)
        self.assertLessEqual(stats[shipped]["wer"], min(stats[label]["wer"] for label in ok) + 1.0)
        self.assertGreaterEqual(stats[shipped]["ok"], stats["legacy"]["ok"] - 1)

    def test_noise_clips_never_survive(self):
        survivors = []
        for label, pcm in noise_clips():
            raw = H.listen(pcm).text(prompt=H.PROMPT)
            if raw and not filler(raw, H.PROMPT):
                survivors.append((label, raw))
        self.assertEqual(survivors, [])

    def test_noise_logprob_or_filler(self):
        clips = noise_clips()[:20]                                     # 0.6 s and 0.8 s x 10 seeds
        caught = 0
        for _label, pcm in clips:
            heard = H.listen(pcm)
            raw = heard.text(prompt=H.PROMPT)
            caught += (not raw) or filler(raw, H.PROMPT) or (heard.avg_logprob or 0) < H.LOGPROB_DROP
        self.assertGreaterEqual(caught, 16)

    def test_faint_speech_logprob_above_drop(self):
        self._need_large()
        low = []
        for phrase, pcm in self.faint38:
            heard = H.listen(pcm)
            heard.text()
            if heard.avg_logprob < H.LOGPROB_DROP:
                low.append((phrase, heard.avg_logprob))
        self.assertEqual(low, [])

    def test_command_phrases_in_the_prompt_are_not_dropped(self):
        for i, phrase in enumerate(("what time is it", "open chrome")):
            text = H.transcribe(faint_clip(phrase, 0.04, seed=400 + i))
            self.assertEqual(norm(text), phrase)

    def test_faint_xyrus_heard_as_a_name(self):
        self._need_large()
        got = [H.listen(faint_clip("xyrus", 0.04, seed=300 + i)).text() for i in range(9)]
        self.assertEqual(sum(bool(NAME_RE.search(t.lower())) for t in got), 9, got)

    def test_text_nbest(self):
        heard = H.listen(faint_clip("play faded by alan walker", 0.04, seed=500))
        hyps = heard.text_nbest(prompt=None, n=5)
        self.assertEqual(len(hyps), 5, hyps)
        scores = [s for _t, s in hyps]
        self.assertEqual(scores, sorted(scores, reverse=True))
        text = heard.text(prompt=None)
        self.assertFalse(heard.fallback_used)
        self.assertEqual(hyps[0][0], text)
        bn = heard.text_nbest(language="bn", prompt=None, n=5)        # same encoding, another language
        self.assertGreaterEqual(len(bn), 1)
        self.assertTrue(all(isinstance(t, str) and isinstance(s, float) for t, s in bn))

    def test_bangla_specialist_when_absent(self):
        if (H.BN_DIR / "model.bin").exists():
            self.skipTest("Bengali model installed - the absent path is covered by BanglaAbsentCPU")
        self.assertFalse(H.bn_available())
        self.assertIsNone(H.listen_bn(faint_clip("open chrome", 0.04)))


if __name__ == "__main__":
    unittest.main()
