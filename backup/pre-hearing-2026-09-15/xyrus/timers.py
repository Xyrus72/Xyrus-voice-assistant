"""In-memory timers (D12), driven by the injected clock (G8). Engine thread; internally locked so the
snapshot can read views() from any thread."""
from __future__ import annotations

import math
import threading
from dataclasses import dataclass

from xyrus.interfaces import TimerView


@dataclass
class _Timer:
    name: str
    total: int
    end: float
    ringing: bool = False
    rang_at: float | None = None


def duration_name(seconds: int) -> str:
    """Name of an unnamed timer: '5 minute', '2 hour', '90 second' ('the five minute timer')."""
    s = int(seconds)
    if s % 3600 == 0:
        return f"{s // 3600} hour"
    if s % 60 == 0:
        return f"{s // 60} minute"
    return f"{s} second"


class TimerService:
    def __init__(self, clock):
        self.clock = clock
        self._timers: list[_Timer] = []
        self._lock = threading.RLock()

    # ---- helpers
    def _view(self, t: _Timer) -> TimerView:
        left = 0 if t.ringing else max(0, math.ceil(t.end - self.clock.mono() - 1e-9))
        return TimerView(name=t.name, remaining_s=int(left), ringing=t.ringing)

    def _unique(self, base: str) -> str:
        names = {t.name for t in self._timers}
        if base not in names:
            return base
        n = 2
        while f"{base} {n}" in names:
            n += 1
        return f"{base} {n}"

    # ---- API (§4.10)
    def add(self, seconds: int, name: str | None = None) -> TimerView:
        seconds = int(seconds)
        with self._lock:
            if name:
                self._timers = [t for t in self._timers if t.name != name]   # a named timer restarts
                tname = name
            else:
                tname = self._unique(duration_name(seconds))
            t = _Timer(name=tname, total=seconds, end=self.clock.mono() + seconds)
            self._timers.append(t)
            return self._view(t)

    def cancel(self, name: str | None = None, all: bool = False) -> int:
        """all -> every timer; name -> that timer; else the ringing ones, or the soonest running one."""
        with self._lock:
            if all:
                n = len(self._timers)
                self._timers = []
                return n
            if name:
                keep = [t for t in self._timers if t.name != name]
                n = len(self._timers) - len(keep)
                self._timers = keep
                return n
            ringing = [t for t in self._timers if t.ringing]
            if ringing:
                self._timers = [t for t in self._timers if not t.ringing]
                return len(ringing)
            running = sorted(self._timers, key=lambda t: t.end)
            if not running:
                return 0
            self._timers.remove(running[0])
            return 1

    def add_time(self, seconds: int) -> TimerView | None:
        """Adds to the soonest running timer."""
        with self._lock:
            running = sorted((t for t in self._timers if not t.ringing), key=lambda t: t.end)
            if not running:
                return None
            t = running[0]
            t.end += int(seconds)
            t.total += int(seconds)
            return self._view(t)

    def snooze(self, seconds: int = 300) -> int:
        """Ringing timers ring again after `seconds` (default 5 min, §3.8 'snooze')."""
        with self._lock:
            n = 0
            for t in self._timers:
                if t.ringing:
                    t.ringing, t.rang_at, t.end = False, None, self.clock.mono() + int(seconds)
                    n += 1
            return n

    def views(self) -> tuple[TimerView, ...]:
        with self._lock:
            ts = sorted(self._timers, key=lambda t: (not t.ringing, t.end))
            return tuple(self._view(t) for t in ts)

    def get(self, name: str) -> TimerView | None:
        with self._lock:
            for t in self._timers:
                if t.name == name:
                    return self._view(t)
            return None

    def any(self) -> bool:
        with self._lock:
            return bool(self._timers)

    def ringing(self) -> bool:
        with self._lock:
            return any(t.ringing for t in self._timers)

    def ringing_since(self) -> float | None:
        with self._lock:
            times = [t.rang_at for t in self._timers if t.ringing and t.rang_at is not None]
            return min(times) if times else None

    def names(self) -> list[str]:
        with self._lock:
            return [t.name for t in self._timers]

    def stop_ringing(self) -> int:
        with self._lock:
            n = sum(1 for t in self._timers if t.ringing)
            self._timers = [t for t in self._timers if not t.ringing]
            return n

    def tick(self) -> list[TimerView]:
        """Newly fired timers (they stay in the list, ringing, until stopped/cancelled/snoozed)."""
        now = self.clock.mono()
        fired = []
        with self._lock:
            for t in self._timers:
                if not t.ringing and t.end <= now:
                    t.ringing, t.rang_at = True, now
                    fired.append(TimerView(name=t.name, remaining_s=0, ringing=True))
        return fired

    def total_of(self, name: str) -> int | None:
        with self._lock:
            for t in self._timers:
                if t.name == name:
                    return t.total
            return None
