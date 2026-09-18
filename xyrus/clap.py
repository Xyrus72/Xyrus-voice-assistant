"""Clap to switch the display (user request, Sep 14 2026).

Three sharp claps (configurable 1-4, Settings -> "Claps needed") toggle the monitor: off, and back on.
Detection runs on the recognizer thread for every 0.25 s mic chunk, in 10 ms frames, in pure Python (SPEC G9:
no numpy) - about a millisecond of CPU per chunk. A clap is a frame that is
  1. loud enough (min_peak, Settings -> "Clap loudness"),
  2. a sudden jump over the 50 ms before it,
  3. gone again 60-90 ms later,
  4. far above the recent background, and
  5. bright: a clap is a broadband crack (many zero crossings); a thud - putting the microphone down, a knock
     on the desk, a door - is a low boom with few zero crossings (user report: setting the mic down switched
     the display off).
Speech fails 3, music fails 4, thuds fail 5, typing is too quiet or too regular. The claps must come 0.12-0.7 s
apart with nothing else near them: a longer rhythm is never a command.
"""
from __future__ import annotations

import math
from array import array
from typing import Callable

RATE = 16000
FRAME = 160                    # 10 ms
FRAME_S = FRAME / RATE
PRE = 5                        # the 50 ms before an onset must be much quieter (the frame right before is
                               # skipped: an attack can straddle two frames)
DECAY = (6, 9)                 # frames 60-90 ms after the onset must have died down
LOOKAHEAD = DECAY[1] + 1       # frames that must exist after a candidate before it can be judged
FLOOR_FRAMES = 200             # background = low percentile of the last 2 s
MIN_ZCR = 0.12                 # zero crossings per sample: a clap ~0.3-0.5, a low thud < 0.05
DEFAULT_CLAPS = 3


class ClapDetector:
    def __init__(self, min_peak: float = 0.12, claps: int = DEFAULT_CLAPS):
        self.min_peak = float(min_peak)
        self.claps = self._clamp(claps)
        self.gap = (0.12, 0.7)         # seconds between consecutive claps
        self.isolation = 0.45          # nothing else this close before the first / after the last clap
        self.cooldown = 1.5            # seconds after a trigger before another can fire
        self.reset()

    @staticmethod
    def _clamp(claps) -> int:
        try:
            return max(1, min(4, int(claps)))
        except (TypeError, ValueError):
            return DEFAULT_CLAPS

    def configure(self, *, min_peak: float | None = None, claps: int | None = None) -> None:
        if min_peak is not None:
            self.min_peak = float(min_peak)
        if claps is not None:
            self.claps = self._clamp(claps)

    def reset(self) -> None:
        self._t: list[float] = []      # frame end times
        self._rms: list[float] = []
        self._peak: list[float] = []
        self._zcr: list[float] = []
        self._next = 0                 # index of the next frame to judge
        self._onsets: list[float] = []
        self._last_trigger = -1e9

    # ------------------------------------------------------------------ input
    def feed(self, pcm: bytes, t_end: float) -> bool:
        """One mic chunk (16 kHz mono int16) ending at monotonic time t_end. True = the clap pattern just
        completed (at most once per cooldown)."""
        a = array("h")
        a.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
        n = len(a) // FRAME
        t0 = t_end - n * FRAME_S
        for k in range(n):
            seg = a[k * FRAME:(k + 1) * FRAME]
            hi, lo = max(seg), min(seg)
            self._peak.append(max(hi, -lo) / 32768.0)
            self._rms.append(math.sqrt(sum(x * x for x in seg) / FRAME) / 32768.0)
            crossings = sum(1 for j in range(1, FRAME) if (seg[j - 1] < 0) != (seg[j] < 0))
            self._zcr.append(crossings / FRAME)
            self._t.append(t0 + (k + 1) * FRAME_S)
        self._judge()
        self._trim()
        return self._decide(t_end)

    # ------------------------------------------------------------------ onsets
    def _floor(self, i: int) -> float:
        lo = max(0, i - FLOOR_FRAMES)
        window = sorted(self._rms[lo:max(lo, i - PRE)])
        return window[len(window) // 5] if window else 0.0

    def _judge(self) -> None:
        last = len(self._t) - LOOKAHEAD
        i = max(self._next, PRE)
        while i <= last:
            if self._peak[i] >= self.min_peak:
                top = max(self._rms[i], self._rms[i + 1])
                before = max(self._rms[i - PRE:i - 1])
                after = max(self._rms[i + DECAY[0]:i + DECAY[1] + 1])
                bright = max(self._zcr[i], self._zcr[i + 1]) >= MIN_ZCR
                if (top > 0 and bright and before <= 0.25 * top and after <= 0.3 * top
                        and self._floor(i) <= 0.12 * top):
                    t = self._t[i]
                    if not self._onsets or t - self._onsets[-1] > 0.1:
                        self._onsets.append(t)
                    i += DECAY[1]               # the rest of this clap can't be a new onset
                    continue
            i += 1
        self._next = i

    def _trim(self) -> None:
        extra = len(self._t) - (FLOOR_FRAMES + 60)
        if extra > 100:
            del self._t[:extra]
            del self._rms[:extra]
            del self._peak[:extra]
            del self._zcr[:extra]
            self._next = max(0, self._next - extra)

    # ------------------------------------------------------------------ pattern
    def _decide(self, now: float) -> bool:
        ons = self._onsets
        while ons and now - ons[0] > 5.0:
            ons.pop(0)
        if now - self._last_trigger < self.cooldown:
            return False
        need = self.claps
        for i in range(len(ons) - need + 1):
            group = ons[i:i + need]
            last = group[-1]
            if now - last < self.isolation:
                return False                    # too early to know that nothing follows
            if any(not (self.gap[0] <= b - a <= self.gap[1]) for a, b in zip(group, group[1:])):
                continue
            if i > 0 and group[0] - ons[i - 1] < self.isolation:
                continue                        # part of a longer run: rhythm, music, typing
            if i + need < len(ons) and ons[i + need] - last < self.isolation:
                continue
            self._last_trigger = now
            del ons[:i + need]
            return True
        return False


class ClapToggle:
    """Claps: display off; claps again: back on. If you woke the screen yourself (mouse or keys) in between,
    the next claps turn it off again instead of doing nothing."""

    def __init__(self, screen_off: Callable[[], None], screen_on: Callable[[], None],
                 tick_now: Callable[[], int], last_input_tick: Callable[[], int]):
        self.screen_off = screen_off
        self.screen_on = screen_on
        self.tick_now = tick_now
        self.last_input_tick = last_input_tick
        self._off_tick: int | None = None

    def toggle(self) -> str:
        """Returns 'off' or 'on'."""
        if self._off_tick is not None:
            since = (self.last_input_tick() - self._off_tick) % 2 ** 32     # GetTickCount wraps at 49.7 days
            if not (0 < since < 2 ** 31):                                   # no input since we turned it off
                self._off_tick = None
                self.screen_on()
                return "on"
        self.screen_off()
        self._off_tick = self.tick_now()
        return "off"
