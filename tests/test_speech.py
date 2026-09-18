"""T3 - speech.py: SapiSpeaker (D10, F6) and WinmmChime (D9, F9); §7.4.6 echo gate.

Silent by design: the logic tests use a fake SpVoice; the real-SAPI tests speak on the output device at
Volume 0 (real-time timing, no sound) or into an SpMemoryStream."""
from __future__ import annotations

import io
import re
import statistics
import subprocess
import sys
import threading
import time
import unittest
import wave
from pathlib import Path

from tests.wav_util import StubConfig
from xyrus import speech
from xyrus.speech import SapiSpeaker, SpeechIntervals, WinmmChime

BASE = Path(__file__).resolve().parent.parent


def other_test_modules() -> list[str]:
    """Test modules already imported into this process besides this one (a combined `discover` run)."""
    return sorted(m for m in sys.modules
                  if re.fullmatch(r"(tests\.)?test_\w+", m) and m.split(".")[-1] != __name__.split(".")[-1])

LONG = ("This is a deliberately long sentence that keeps going for many seconds so that the stop command "
        "has plenty of time to cut it off well before the end of all of these words.")


# ============================================================================ fake SpVoice
class _Tok:
    def __init__(self, desc):
        self.desc = desc

    def GetDescription(self):
        return self.desc


class _Tokens:
    def __init__(self, descs):
        self.items = [_Tok(d) for d in descs]
        self.Count = len(self.items)

    def Item(self, i):
        return self.items[i]


class FakeVoice:
    """Minimal SpVoice: an utterance 'plays' for len(text) * per_char seconds."""

    def __init__(self, per_char=0.004, fail_first=False):
        self.per_char = per_char
        self.fail = fail_first
        self.done_at = 0.0
        self.spoken: list[str] = []
        self.purges = 0
        self.thread_ids: set[int] = set()
        self.tokens = _Tokens(["Fake David Desktop", "Fake Zira Desktop"])
        self.Voice = self.tokens.items[0]
        self.Rate = 0
        self.Volume = 100

    def GetVoices(self):
        return self.tokens

    def Speak(self, text, flags):
        self.thread_ids.add(threading.get_ident())
        if self.fail:
            self.fail = False
            raise OSError("simulated COMError")
        if flags & speech.SVSF_PURGE:
            self.purges += 1
            self.done_at = time.monotonic()
            return
        self.spoken.append(text)
        self.done_at = time.monotonic() + len(text) * self.per_char

    def WaitUntilDone(self, ms):
        self.thread_ids.add(threading.get_ident())
        left = self.done_at - time.monotonic()
        if left > 0:
            time.sleep(min(left, ms / 1000))
        return self.done_at <= time.monotonic()


class Done:
    """Collects on_done calls (name, thread, time)."""

    def __init__(self):
        self.calls: list[tuple[str, int, float]] = []
        self.lock = threading.Lock()

    def cb(self, name):
        def fn():
            with self.lock:
                self.calls.append((name, threading.get_ident(), time.monotonic()))
        return fn

    def names(self):
        return [c[0] for c in self.calls]

    def wait(self, n, timeout=5.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if len(self.calls) >= n:
                return True
            time.sleep(0.005)
        return False


def wait_for(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.005)
    return False


class FakeVoiceSpeaker(unittest.TestCase):
    def make(self, cfg=None, **voice_kw):
        self.voices = []

        def factory():
            v = FakeVoice(**voice_kw)
            voice_kw.pop("fail_first", None)
            self.voices.append(v)
            return v
        sp = SapiSpeaker(cfg or StubConfig(), voice_factory=factory)
        self.addCleanup(sp.close)
        return sp

    def test_on_done_once_each_in_order_on_t_speak(self):
        sp, done = self.make(), Done()
        for name in ("a", "b", "c"):
            sp.say(f"reply {name}", on_done=done.cb(name))
        self.assertTrue(done.wait(3))
        time.sleep(0.1)
        self.assertEqual(done.names(), ["a", "b", "c"])
        self.assertEqual({c[1] for c in done.calls}, {sp._thread.ident})
        self.assertEqual(self.voices[0].spoken, ["reply a", "reply b", "reply c"])

    def test_alert_priority_goes_before_queued_replies(self):
        sp, done = self.make(), Done()
        sp.say("x" * 150, on_done=done.cb("long"))            # ~0.6 s
        self.assertTrue(wait_for(sp.is_speaking))
        sp.say("reply one", on_done=done.cb("r1"))
        sp.say("reply two", on_done=done.cb("r2"))
        sp.say("alert", priority=0, on_done=done.cb("alert"))
        self.assertTrue(done.wait(4))
        self.assertEqual(done.names(), ["long", "alert", "r1", "r2"])

    def test_stop_cuts_within_200ms_and_calls_every_pending_on_done(self):
        sp, done = self.make(per_char=0.05), Done()             # the long one would take ~9 s
        sp.say(LONG, on_done=done.cb("long"))
        for i in range(3):
            sp.say(f"queued {i}", on_done=done.cb(f"q{i}"))
        self.assertTrue(wait_for(sp.is_speaking))
        time.sleep(0.2)
        t = time.monotonic()
        sp.stop()
        self.assertTrue(done.wait(4, timeout=2))
        self.assertLess(done.calls[0][2] - t, 0.2)
        self.assertLess(max(c[2] for c in done.calls) - t, 0.3)
        self.assertEqual(sorted(done.names()), ["long", "q0", "q1", "q2"])
        self.assertEqual(self.voices[0].spoken, [LONG])         # nothing queued was spoken
        self.assertEqual(self.voices[0].purges, 1)
        self.assertTrue(wait_for(lambda: not sp.is_speaking(), 1))

    def test_say_after_stop_still_speaks(self):
        sp, done = self.make(), Done()
        sp.stop()
        sp.say("after", on_done=done.cb("after"))
        self.assertTrue(done.wait(1))
        self.assertEqual(self.voices[0].spoken, ["after"])

    def test_voice_replies_off_skips_but_force_speaks(self):
        sp, done = self.make(StubConfig(voice_replies=False)), Done()
        sp.say("quiet reply", on_done=done.cb("skipped"))
        self.assertTrue(done.wait(1))
        sp.say("time's up", force=True, on_done=done.cb("forced"))
        self.assertTrue(done.wait(2))
        self.assertEqual(self.voices[0].spoken, ["time's up"])
        self.assertEqual(len(sp._intervals.snapshot()), 1)      # the skipped reply opened no gate interval

    def test_empty_text_and_bad_callback_do_not_break_the_loop(self):
        sp, done = self.make(), Done()

        def boom():
            raise RuntimeError("callback bug")
        sp.say("   ", on_done=done.cb("empty"))
        sp.say("first", on_done=boom)
        sp.say("second", on_done=done.cb("second"))
        self.assertTrue(done.wait(2))
        self.assertEqual(done.names(), ["empty", "second"])
        self.assertEqual(self.voices[0].spoken, ["first", "second"])

    def test_com_error_recreates_the_voice(self):
        sp, done = self.make(fail_first=True), Done()
        sp.say("lost", on_done=done.cb("lost"))
        sp.say("recovered", on_done=done.cb("recovered"))
        self.assertTrue(done.wait(2))
        self.assertEqual(done.names(), ["lost", "recovered"])
        self.assertEqual(len(self.voices), 2)
        self.assertEqual(self.voices[1].spoken, ["recovered"])

    def test_only_t_speak_touches_the_voice(self):
        sp, done = self.make(per_char=0.05), Done()
        sp.say("hello", on_done=done.cb("hello"))
        self.assertTrue(done.wait(1, 2))
        sp.set_rate(3)
        sp.set_voice("zira")
        sp.say(LONG, on_done=done.cb("long"))
        self.assertTrue(wait_for(sp.is_speaking))
        sp.stop()                                               # purge requested from this (other) thread
        self.assertTrue(done.wait(2, 2))
        self.assertTrue(sp.wait_idle(2))
        self.assertEqual(self.voices[0].purges, 1)
        self.assertEqual(self.voices[0].thread_ids, {sp._thread.ident})

    def test_voices_set_voice_and_rate(self):
        sp = self.make()
        self.assertEqual(sp.voices(), ["Fake David Desktop", "Fake Zira Desktop"])
        v = self.voices[0]
        sp.set_voice("zira")
        sp.set_rate(15)
        self.assertTrue(wait_for(lambda: v.Voice.GetDescription() == "Fake Zira Desktop" and v.Rate == 10, 2))
        sp.set_voice(None)
        self.assertTrue(wait_for(lambda: v.Voice.GetDescription() == "Fake David Desktop", 2))

    def test_config_voice_rate_volume_applied_at_start(self):
        sp = self.make(StubConfig(tts={"voice": "Fake Zira Desktop", "rate": -3, "volume": 20}))
        sp.voices()
        v = self.voices[0]
        self.assertEqual((v.Voice.GetDescription(), v.Rate, v.Volume), ("Fake Zira Desktop", -3, 20))

    def test_is_quiet_at_during_and_after_speech(self):
        sp, done = self.make(per_char=0.02), Done()
        sp.say("x" * 40, on_done=done.cb("x"))                  # ~0.8 s
        self.assertTrue(wait_for(sp.is_speaking))
        now = time.monotonic()
        self.assertFalse(sp.is_quiet_at(now))
        self.assertFalse(sp.is_quiet_at(now + 100))             # open interval: end = +inf
        self.assertTrue(done.wait(1, 3))
        (start, end), = sp._intervals.snapshot()
        self.assertTrue(sp.is_quiet_at(start - 0.06))
        self.assertFalse(sp.is_quiet_at(start - 0.04))
        self.assertFalse(sp.is_quiet_at(end + 0.3))
        self.assertTrue(sp.is_quiet_at(end + 0.4))

    def test_close_still_calls_pending_on_done(self):
        sp, done = self.make(per_char=0.05), Done()
        sp.say(LONG, on_done=done.cb("long"))
        sp.say("pending", on_done=done.cb("pending"))
        self.assertTrue(wait_for(sp.is_speaking))
        sp.close()
        self.assertTrue(done.wait(2, 2))
        sp.say("after close", on_done=done.cb("late"))
        self.assertEqual(sorted(done.names()), ["late", "long", "pending"])


class Intervals(unittest.TestCase):
    def test_window_edges_and_keeps_last_eight(self):
        iv = SpeechIntervals()
        for i in range(10):
            iv.close(iv.open(i * 10.0), i * 10.0 + 1)
        self.assertEqual(len(iv.snapshot()), 8)
        self.assertTrue(iv.quiet_at(0.5))                       # interval 0 was evicted
        self.assertFalse(iv.quiet_at(90.5))
        self.assertFalse(iv.quiet_at(89.96))
        self.assertTrue(iv.quiet_at(89.94))
        self.assertFalse(iv.quiet_at(91.34))
        self.assertTrue(iv.quiet_at(91.36))


class RealSapi(unittest.TestCase):
    """Real SAPI.SpVoice on T-speak, Volume 0 on the output device (silent, real-time).

    Own process only (T6): after some earlier test module in the same interpreter SAPI's Speak returns at
    once (green alone, 3 failures in the combined run - an in-process test interaction, not an app bug), so
    a combined run skips this class here and RealSapiIsolated runs it in a fresh interpreter instead."""

    @classmethod
    def setUpClass(cls):
        others = other_test_modules()
        if others:
            raise unittest.SkipTest("real-SAPI timing tests run in their own process in a combined run "
                                    "(see RealSapiIsolated; SAPI Speak returns at once after earlier modules)")
        cls.sp = SapiSpeaker(StubConfig(tts={"voice": None, "rate": 0, "volume": 0}))

    @classmethod
    def tearDownClass(cls):
        cls.sp.close()

    def test_real_voices_listed(self):
        voices = self.sp.voices()
        self.assertTrue(voices)
        self.assertTrue(any("Microsoft" in v for v in voices), voices)

    def test_real_stop_cuts_within_200ms_and_calls_pending(self):
        sp, done = self.sp, Done()
        sp.say(LONG, on_done=done.cb("long"))
        self.assertTrue(wait_for(sp.is_speaking, 5))
        sp.say("Queued one.", on_done=done.cb("q1"))
        sp.say("Queued two.", priority=0, on_done=done.cb("q2"))   # alerts never interrupt the current one
        spoken_before = sp.spoken
        time.sleep(0.6)
        self.assertTrue(sp.is_speaking())                       # still mid-sentence in real time
        t = time.monotonic()
        sp.stop()
        self.assertTrue(done.wait(3, 2))
        cut = done.calls[0][2] - t
        self.assertEqual(done.calls[0][0], "long")
        self.assertLess(cut, 0.2, f"stop took {cut * 1000:.0f} ms")
        self.assertEqual(sorted(done.names()), ["long", "q1", "q2"])
        self.assertEqual(sp.spoken, spoken_before)              # the queued ones never reached SAPI
        self.assertTrue(wait_for(lambda: not sp.is_speaking(), 1))

    def test_real_echo_gate(self):                              # §7.4.6
        sp, done = self.sp, Done()
        sp.say("This is a short test sentence for the echo gate.", on_done=done.cb("s"))
        self.assertTrue(wait_for(sp.is_speaking, 5))
        time.sleep(0.3)
        self.assertFalse(sp.is_quiet_at(time.monotonic()))
        self.assertTrue(done.wait(1, 10))
        start, end = sp._intervals.snapshot()[-1]
        self.assertGreater(end - start, 1.0)                    # spoken in real time, not skipped
        self.assertFalse(sp.is_quiet_at(end + 0.2))
        time.sleep(0.45)
        self.assertTrue(sp.is_quiet_at(time.monotonic()))
        self.assertTrue(sp.is_quiet_at(end + 0.4))

    def test_real_memory_stream_output(self):
        sp = SapiSpeaker(StubConfig(), audio_output="memory")
        self.addCleanup(sp.close)
        done = Done()
        sp.say("Memory stream test.", on_done=done.cb("m"))
        self.assertTrue(done.wait(1, 5))
        self.assertEqual(sp.spoken, 1)


class RealSapiIsolated(unittest.TestCase):
    """Combined run only: RealSapi in a fresh interpreter (see RealSapi.setUpClass)."""

    def test_real_sapi_in_its_own_process(self):
        if not other_test_modules():
            self.skipTest("RealSapi runs directly in this process")
        r = subprocess.run([sys.executable, "-m", "unittest", "-v", "tests.test_speech.RealSapi"], cwd=str(BASE),
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180,
                           creationflags=0x08000000)
        self.assertEqual(r.returncode, 0, r.stderr[-3000:])
        self.assertIn("Ran 4 tests", r.stderr)
        self.assertIn("OK", r.stderr.strip().splitlines()[-1])


class Chime(unittest.TestCase):
    def test_play_returns_within_5ms(self):
        chime = WinmmChime(volume=0.0)                           # silent buffers, real winmm playback
        first = time.perf_counter()
        chime.play("wake")
        first = time.perf_counter() - first
        times = []
        for _ in range(4):
            for kind in ("wake", "alert", "error"):
                t = time.perf_counter()
                chime.play(kind)
                times.append(time.perf_counter() - t)
        chime.stop()
        self.assertLess(max(times), 0.005, f"max {max(times) * 1000:.2f} ms, median "
                                           f"{statistics.median(times) * 1000:.2f} ms, first {first * 1000:.2f} ms")
        self.assertLess(first, 0.05)

    def test_buffers_are_module_level_wavs(self):
        WinmmChime()
        want = {"wake": 0.2, "alert": 0.48, "error": 0.25}
        for kind, secs in want.items():
            buf = speech.chime_buffer(kind, 1.0)
            self.assertIs(buf, speech._BUFFERS[(kind, 1.0)])     # kept alive at module level (F9)
            self.assertIs(buf, speech.chime_buffer(kind, 1.0))
            with wave.open(io.BytesIO(buf.raw)) as w:
                self.assertEqual((w.getframerate(), w.getnchannels(), w.getsampwidth()), (22050, 1, 2))
                self.assertAlmostEqual(w.getnframes() / 22050, secs, places=2)

    def test_volume_zero_is_silent(self):
        WinmmChime(volume=0.0)
        with wave.open(io.BytesIO(speech.chime_buffer("wake", 0.0).raw)) as w:
            self.assertEqual(set(w.readframes(w.getnframes())), {0})

    def test_any_thread_unknown_kind_and_stop_never_raise(self):
        chime = WinmmChime(volume=0.0)
        errors = []

        def worker():
            try:
                for kind in ("wake", "alert", "error", "nope"):
                    chime.play(kind)
            except Exception as e:                               # pragma: no cover
                errors.append(e)
        threads = [threading.Thread(target=worker) for _ in range(4)]
        with self.assertLogs("xyrus.speech", "WARNING") as cm:
            for th in threads:
                th.start()
            for th in threads:
                th.join(2)
        self.assertEqual(sum("unknown chime" in m for m in cm.output), 4)
        chime.stop()
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
