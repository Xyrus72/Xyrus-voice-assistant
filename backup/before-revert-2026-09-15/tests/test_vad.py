"""xyrus.vad (hearing rebuild, Sep 15 2026): Silero driven as a stream with the LSTM state carried by hand.

- chunked push() (0.25 s mic chunks, odd sizes) == one whole-clip run, window for window;
- faint speech (peak 0.03 over the room's rms 0.0033 hiss) scores high, the hiss / room rumble low;
- available is False (and the Endpointer falls back to energy) when onnxruntime can't be imported or the user
  chose "energy";
- a SUBPROCESS that imports the VAD and hearing, warms the VAD, decodes one clip with Whisper and exits: it must
  exit 0 (a probe once died with 0xC0000409 at interpreter teardown with both native runtimes loaded).

Run: venv\\Scripts\\python.exe -m unittest tests.test_vad -v
"""
from __future__ import annotations

import builtins
import logging
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

if not os.environ.get("XYRUS_DATA_DIR"):
    os.environ["XYRUS_DATA_DIR"] = tempfile.mkdtemp(prefix="xyrus_test_")

import numpy as np

from tests import wav_util as W
from xyrus import vad as V

BASE = Path(__file__).resolve().parent.parent
RATE = 16000
PHRASES = ["set the volume to forty percent", "volume up", "open chrome", "what time is it",
           "play shape of you by ed sheeran", "take a screenshot"]


def mix(a: bytes, b: bytes) -> bytes:
    x = np.frombuffer(a, dtype=np.int16).astype(np.int32)
    y = np.frombuffer(b, dtype=np.int16).astype(np.int32)
    n = min(len(x), len(y))
    return np.clip(x[:n] + y[:n], -32768, 32767).astype(np.int16).tobytes()


def faint_clip(text: str, peak: float, seed: int) -> bytes:
    body = W.silence(0.8) + W.faint(W.synth(text), peak) + W.silence(0.8)
    return mix(body, W.hiss(len(body) / (RATE * 2) + 0.1, seed=seed))


@unittest.skipUnless(V.warm(), f"Silero VAD unavailable: {V.error}")
class Stream(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        W.synth_many(PHRASES)
        cls.clips = [faint_clip(t, 0.03, seed=i) for i, t in enumerate(PHRASES)]

    def test_chunked_equals_whole_clip(self):
        for text, clip in zip(PHRASES, self.clips):
            with self.subTest(text=text):
                s = V.SileroStream()
                whole = s.run_all(clip)
                for size in (W.CHUNK_BYTES, 1000, 3334):        # the mic's 0.25 s, and sizes that split windows
                    s.reset()
                    got = []
                    for c in W.chunks(clip, size):
                        got += s.push(c)
                    self.assertEqual(len(got), len(clip) // (V.WINDOW * 2))
                    self.assertLessEqual(max(abs(a - b) for a, b in zip(whole, got)), 0.05, size)

    def test_faint_speech_is_speech(self):
        for text, clip in zip(PHRASES, self.clips):
            with self.subTest(text=text):
                probs = V.SileroStream().run_all(clip)
                self.assertGreaterEqual(max(probs), 0.7)
                self.assertGreaterEqual(sum(p >= 0.5 for p in probs), 3, "several windows, not one blip")

    def test_noise_is_not_speech(self):
        for name, pcm in (("hiss", W.hiss(10, seed=4)), ("hiss loud", W.hiss(10, rms=0.01, seed=5)),
                          ("room", W.room_noise(10, 1500, seed=6)), ("silence", W.silence(3))):
            with self.subTest(noise=name):
                self.assertLessEqual(max(V.SileroStream().run_all(pcm)), 0.2)

    def test_reset_starts_cold_again(self):
        s = V.SileroStream()
        a = s.run_all(self.clips[0])
        s.push(self.clips[1][:12345])
        s.reset()
        self.assertEqual(s.push(self.clips[0])[:len(a)], a)

    def test_state_is_carried_between_pushes(self):
        """The faster_whisper __call__ zeroes h/c per call; a cold restart every 0.25 s gives other numbers."""
        clip = self.clips[0]
        s = V.SileroStream()
        whole = s.run_all(clip)
        cold = []
        for c in W.chunks(clip[: len(clip) // 8192 * 8192], 8192):
            s.reset()
            cold += s.push(c)
        self.assertGreater(max(abs(a - b) for a, b in zip(whole, cold)), 0.01, "the state does matter")


class Availability(unittest.TestCase):
    def setUp(self):
        self.saved = (V._session, V._state_names, V.available, V.error, V._tried)

    def tearDown(self):
        V._session, V._state_names, V.available, V.error, V._tried = self.saved     # never del the session

    def test_import_failure_means_unavailable_and_energy_endpointing(self):
        from xyrus.capture import Endpointer
        real_import = builtins.__import__

        def no_onnx(name, *a, **k):
            if name == "onnxruntime" or name.startswith("onnxruntime."):
                raise ImportError("onnxruntime missing (test)")
            return real_import(name, *a, **k)

        V._tried, V._session, V.available = False, None, False
        with mock.patch.dict(os.environ, {"XYRUS_VAD": ""}), mock.patch("builtins.__import__", no_onnx), \
                self.assertLogs("xyrus.vad", logging.WARNING) as logs:
            self.assertFalse(V.warm())
            self.assertFalse(V.warm())
            stream = V.SileroStream()
            ep = Endpointer()
        self.assertEqual(len([r for r in logs.records if r.levelno >= logging.WARNING]), 1, "warned ONCE")
        self.assertFalse(V.available)
        self.assertIn("onnxruntime", V.error)
        self.assertFalse(stream.ok)
        self.assertEqual(stream.push(W.silence(1)), [])
        self.assertEqual(ep.vad, "energy")

    def test_energy_chosen_by_env(self):
        V._tried, V.available = False, False
        with mock.patch.dict(os.environ, {"XYRUS_VAD": "energy"}):
            self.assertFalse(V.warm())
        self.assertIn("energy", V.error)

    def test_energy_chosen_by_config(self):
        from xyrus.paths import config_file
        path = config_file()
        if path.exists():
            self.skipTest("a config.json exists in the test data dir")
        V._tried, V.available = False, False
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.write_text('{"vad": "energy"}', encoding="utf8")
            with mock.patch.dict(os.environ, {"XYRUS_VAD": ""}):
                self.assertFalse(V.warm())
        finally:
            path.unlink(missing_ok=True)


# ============================================================================ both native runtimes, one process
_SCRIPT = textwrap.dedent(r"""
    import os, sys
    sys.path.insert(0, {base!r})
    import xyrus.vad as V
    try:
        import hearing as H
    except Exception as e:
        print("NOHEARING", repr(e)); sys.exit(4)
    from tests import wav_util as W
    if not V.warm():
        print("NOVAD", V.error); sys.exit(5)
    H.load_async()
    if not H.available(wait=400):
        print("NOWHISPER", H.error); sys.exit(3)
    import threading
    pcm = W.silence(0.5) + W.synth("what time is it") + W.silence(0.5)
    out = {{}}

    def work():                          # the app decodes on T-rec, never on the thread that exits the process
        heard = H.listen(pcm)
        out["text"] = heard.text() if heard is not None else ""
        out["vad"] = max(V.SileroStream().push(pcm))
    t = threading.Thread(target=work, name="T-rec")
    t.start()
    t.join()
    print("TEXT", repr(out.get("text")), "VADMAX", round(out.get("vad", 0.0), 3), flush=True)
""")


def _whisper_model_on_disk() -> bool:
    return any((BASE / "models" / d / "model.bin").exists()
               for d in ("whisper-large-v3", "whisper-large-v3-turbo", "whisper-small"))


@unittest.skipUnless(_whisper_model_on_disk(), "no Whisper model on disk")
@unittest.skipUnless(V.warm(), f"Silero VAD unavailable: {V.error}")
class SameProcessAsWhisper(unittest.TestCase):
    def test_vad_and_whisper_exit_cleanly(self):
        """Measured Sep 15: a process exits 0xC0000409 at teardown when Whisper was loaded on hearing's loader
        thread and then decoded on the MAIN thread (ctranslate2's per-thread CUDA handles outlive the driver) -
        with or without the VAD. The app never does that: it decodes on T-rec and ends with os._exit. This
        probe mirrors the app (VAD session first, never deleted; decode on a worker thread) and must exit 0."""
        script = Path(tempfile.mkdtemp(prefix="xyrus_vadproc_")) / "probe.py"
        script.write_text(_SCRIPT.format(base=str(BASE)), encoding="utf8")
        env = dict(os.environ, XYRUS_DATA_DIR=tempfile.mkdtemp(prefix="xyrus_test_"), XYRUS_VAD="")
        p = subprocess.run([sys.executable, str(script)], cwd=str(BASE), env=env, capture_output=True, text=True,
                           timeout=900, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        out = (p.stdout or "") + (p.stderr or "")[-2000:]
        if p.returncode == 3:
            self.skipTest("Whisper did not load: " + out.strip()[-300:])
        if p.returncode == 4:
            self.skipTest("hearing.py not importable right now: " + out.strip()[-300:])
        self.assertEqual(p.returncode, 0, f"exit {p.returncode:#x}: {out}")
        self.assertIn("TEXT", p.stdout)
        print(f"\n[test_vad] subprocess: {p.stdout.strip()}", file=sys.stderr)



@unittest.skipUnless(V.warm(), f"Silero VAD unavailable: {V.error}")
class LongStreamStaysSensitive(unittest.TestCase):
    """Measured live Sep 15: with the state carried forever a faint phrase scored p ~0.005 after the stream had
    run a while (the capture went deaf); the quiet reset keeps it near a fresh stream's score for minutes."""

    def test_faint_phrase_every_29_s_for_4_minutes(self):
        import numpy as np
        from tests import wav_util as W
        phrase = W.faint(W.synth("never mind"), 0.03)
        st = V.SileroStream()
        best = []
        for k in range(8):
            seg = bytearray(W.hiss(29.0, 0.0033, seed=k))
            ph = np.frombuffer(phrase, dtype=np.int16).astype(np.int32)
            base = np.frombuffer(bytes(seg[:len(phrase)]), dtype=np.int16).astype(np.int32)
            n = min(len(ph), len(base))
            seg[:n * 2] = np.clip(base[:n] + ph[:n], -32768, 32767).astype(np.int16).tobytes()
            top = 0.0
            for i, c in enumerate(W.chunks(bytes(seg))):
                ps = st.push(c)
                if i * 0.25 < 2.5 and ps:
                    top = max(top, max(ps))
            best.append(top)
        self.assertGreaterEqual(min(best), 0.85, [round(b, 2) for b in best])
        self.assertGreater(st.quiet_resets, 0)

    def test_no_reset_while_speech_goes_on(self):
        from tests import wav_util as W
        st = V.SileroStream()
        speech = W.synth("set the volume to forty percent and then tell me a joke")
        st.push(speech)
        self.assertEqual(st.quiet_resets, 0, "a quiet reset never happens inside speech")

if __name__ == "__main__":
    unittest.main()
