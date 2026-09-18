"""Injected time (G8). Engine-side modules read time only through a Clock."""
from __future__ import annotations

import datetime as dt
import time


class SystemClock:
    def now(self) -> dt.datetime:
        return dt.datetime.now().replace(microsecond=0)

    def mono(self) -> float:
        return time.monotonic()


class FakeClock:
    """Test clock. mono() advances in step with now(); it never goes backwards."""

    def __init__(self, start: dt.datetime | None = None, mono_start: float = 1000.0):
        self._now = start or dt.datetime(2026, 9, 13, 10, 30)
        self._mono = float(mono_start)

    def now(self) -> dt.datetime:
        return self._now

    def mono(self) -> float:
        return self._mono

    def advance(self, seconds: float) -> None:
        self._now += dt.timedelta(seconds=seconds)
        self._mono += max(0.0, float(seconds))

    def set(self, when: dt.datetime) -> None:
        delta = (when - self._now).total_seconds()
        self._now = when
        if delta > 0:
            self._mono += delta
