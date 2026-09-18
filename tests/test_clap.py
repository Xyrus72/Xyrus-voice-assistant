"""Clap to switch the display: the detector fires on three claps (the default) and ignores fewer claps, longer
rhythms, slow claps, thuds (putting the microphone down, a knock on the desk), typing, music with drums and
speech; nothing fires while Xyrus is talking; the toggle turns the display off, back on, and off again when you
woke the screen yourself."""
import math
import random
import unittest
from array import array

from xyrus.clap import ClapDetector, ClapToggle

RATE = 16000


def bed(seconds, amp=60, seed=1):
    rnd = random.Random(seed)
    return [rnd.randint(-amp, amp) for _ in range(int(seconds * RATE))]


def clap(amp=0.6, tau=0.010, reverb=0.08, seed=2):
    """A hand clap: noise burst, 2 ms attack, ~10 ms decay, plus a faint room tail."""
    rnd = random.Random(seed)
    out = []
    for k in range(int(0.3 * RATE)):
        t = k / RATE
        env = amp * min(1.0, t / 0.002) * math.exp(-t / tau) + reverb * amp * math.exp(-t / 0.15)
        out.append(int(32767 * env * rnd.uniform(-1, 1)))
    return out


def thud(amp=0.9, freq=90, tau=0.03, seed=3):
    """Setting a microphone down / a knock on the desk: a low boom with a little rattle."""
    rnd = random.Random(seed)
    out = []
    for k in range(int(0.3 * RATE)):
        t = k / RATE
        env = amp * min(1.0, t / 0.003) * math.exp(-t / tau)
        v = env * math.sin(2 * math.pi * freq * t) + 0.03 * env * rnd.uniform(-1, 1)
        out.append(int(max(-1.0, min(1.0, v)) * 32767))
    return out


def mix(base, burst, at):
    i = int(at * RATE)
    for k, v in enumerate(burst):
        if i + k < len(base):
            base[i + k] = max(-32768, min(32767, base[i + k] + v))
    return base


def run(samples, det=None, chunk=4000):
    det = det or ClapDetector()
    pcm = array("h", samples).tobytes()
    fired, t = [], 100.0
    for i in range(0, len(pcm), chunk * 2):
        piece = pcm[i:i + chunk * 2]
        t += len(piece) / 2 / RATE
        if det.feed(piece, t):
            fired.append(round(t - 100.0, 2))
    return fired


def sounds_at(times, maker=clap, seconds=4.0, **kw):
    s = bed(seconds)
    for n, at in enumerate(times):
        mix(s, maker(seed=10 + n, **kw), at)
    return s


class Detector(unittest.TestCase):
    def test_three_claps_fire_once(self):
        self.assertEqual(len(run(sounds_at([1.0, 1.35, 1.7]))), 1)

    def test_three_claps_different_spacing(self):
        for gaps in ((0.15, 0.15), (0.3, 0.4), (0.5, 0.65), (0.25, 0.6)):
            with self.subTest(gaps=gaps):
                t1 = 1.0 + gaps[0]
                self.assertEqual(len(run(sounds_at([1.0, t1, t1 + gaps[1]]))), 1)

    def test_softer_claps_still_count(self):
        self.assertEqual(len(run(sounds_at([1.0, 1.4, 1.8], amp=0.25))), 1)

    def test_two_sets_fire_twice(self):
        self.assertEqual(len(run(sounds_at([0.8, 1.1, 1.4, 3.8, 4.1, 4.4], seconds=6.0))), 2)

    def test_two_claps_are_not_enough(self):
        self.assertEqual(run(sounds_at([1.0, 1.35])), [])

    def test_one_clap_ignored(self):
        self.assertEqual(run(sounds_at([1.0])), [])

    def test_four_or_more_in_a_row_ignored(self):
        self.assertEqual(run(sounds_at([1.0, 1.3, 1.6, 1.9])), [])
        self.assertEqual(run(sounds_at([1.0, 1.3, 1.6, 1.9, 2.2, 2.5])), [])

    def test_slow_claps_ignored(self):
        self.assertEqual(run(sounds_at([1.0, 1.5, 2.6])), [])

    def test_too_quiet_ignored(self):
        self.assertEqual(run(sounds_at([1.0, 1.35, 1.7], amp=0.05)), [])

    def test_putting_the_mic_down_is_not_a_clap(self):
        """User report: setting the microphone down switched the display off. Thuds are low booms."""
        for freq in (60, 90, 150, 250):
            with self.subTest(freq=freq):
                self.assertEqual(run(sounds_at([1.0, 1.3, 1.6], maker=thud, freq=freq)), [])
        self.assertEqual(run(sounds_at([1.0, 1.2, 1.35, 1.8], maker=thud)), [], "a rattle of thuds")

    def test_thuds_mixed_with_two_claps_dont_make_three(self):
        s = sounds_at([1.0, 1.35])
        mix(s, thud(seed=99), 1.7)
        self.assertEqual(run(s), [])

    def test_configurable_count(self):
        for n, times in ((1, [1.0]), (2, [1.0, 1.35]), (4, [1.0, 1.3, 1.6, 1.9])):
            with self.subTest(claps=n):
                det = ClapDetector()
                det.configure(claps=n)
                self.assertEqual(len(run(sounds_at(times), det)), 1)

    def test_typing_ignored(self):
        for amp in (0.06, 0.3):                         # quiet keyboard, and a loud one near the mic
            with self.subTest(amp=amp):
                s = bed(4.0)
                for n in range(24):
                    mix(s, clap(amp=amp, tau=0.004, reverb=0.0, seed=50 + n), 0.5 + n * 0.13)
                self.assertEqual(run(s), [])

    def test_music_with_drums_ignored(self):
        rnd = random.Random(7)
        s = [int(32767 * (0.12 * math.sin(2 * math.pi * 220 * k / RATE) + 0.06 * math.sin(2 * math.pi * 330 * k / RATE)
                          + 0.04 * rnd.uniform(-1, 1))) for k in range(int(5 * RATE))]
        for n in range(9):
            mix(s, clap(amp=0.6, seed=80 + n), 0.3 + n * 0.5)
        self.assertEqual(run(s), [])

    def test_speech_ignored(self):
        from tests import wav_util
        phrases = ["xyrus what time is it", "turn it down a bit please", "take a screenshot",
                   "set a timer for five minutes", "xyrus play ishwar by vikings", "okay thank you"]
        clips = wav_util.synth_many(phrases)
        for p in phrases:
            with self.subTest(phrase=p):
                pcm = array("h")
                pcm.frombytes(clips[p])
                s = bed(0.8) + list(pcm) + bed(1.2)
                self.assertEqual(run(s), [])


class NeverWhileXyrusTalks(unittest.TestCase):
    def _rec(self, speaker, config):
        from xyrus.recognizer import Recognizer
        return Recognizer("unused", None, None, get_spec=lambda: None, on_transcript=lambda tr: None,
                          speaker=speaker, chime=None, config=config)

    def test_echo_gate_blocks_claps(self):
        class Speaker:
            quiet = False

            def is_quiet_at(self, t):
                return self.quiet

        fired = []
        spk = Speaker()
        rec = self._rec(spk, {})
        rec.on_clap = lambda: fired.append(1)
        pcm = array("h", sounds_at([1.0, 1.35, 1.7])).tobytes()

        def feed_all(t):
            for i in range(0, len(pcm), 8000):
                piece = pcm[i:i + 8000]
                t += len(piece) / 2 / RATE
                rec._clap_check(piece, t)

        feed_all(200.0)
        self.assertEqual(fired, [], "claps while Xyrus is speaking must never count")
        spk.quiet = True
        rec._clap = None
        feed_all(300.0)
        self.assertEqual(fired, [1])

    def test_disabled_in_settings(self):
        class Speaker:
            def is_quiet_at(self, t):
                return True

        fired = []
        rec = self._rec(Speaker(), {"clap.enabled": False})
        rec.on_clap = lambda: fired.append(1)
        pcm = array("h", sounds_at([1.0, 1.35, 1.7])).tobytes()
        t = 300.0
        for i in range(0, len(pcm), 8000):
            t += 0.25
            rec._clap_check(pcm[i:i + 8000], t)
        self.assertEqual(fired, [])


class Toggle(unittest.TestCase):
    def setUp(self):
        self.calls, self.tick, self.last_input = [], 1000, 500
        self.tg = ClapToggle(lambda: self.calls.append("off"), lambda: self.calls.append("on"),
                             lambda: self.tick, lambda: self.last_input)

    def test_off_then_on(self):
        self.assertEqual(self.tg.toggle(), "off")
        self.tick = 5000
        self.assertEqual(self.tg.toggle(), "on")
        self.assertEqual(self.calls, ["off", "on"])

    def test_woke_it_yourself_then_claps_turn_it_off_again(self):
        self.tg.toggle()                        # off at tick 1000
        self.last_input = 3000                  # you moved the mouse: the screen woke up
        self.tick = 4000
        self.assertEqual(self.tg.toggle(), "off")
        self.assertEqual(self.calls, ["off", "off"])

    def test_tick_wraparound(self):
        self.tick = 2 ** 32 - 100
        self.tg.toggle()                        # off just before the 32-bit tick wraps
        self.last_input = 2 ** 32 - 500         # last input was before that
        self.assertEqual(self.tg.toggle(), "on")


if __name__ == "__main__":
    unittest.main()
