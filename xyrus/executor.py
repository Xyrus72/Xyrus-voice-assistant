"""T-exec: runs blocking jobs (SystemActions, YouTube lookup, psutil, brightness) off the engine thread.

Executor: one worker thread at a time (COM initialised), a job queue, and a watchdog. A job that overruns its
timeout gets on_done(None, TimeoutError(name)); its worker is abandoned (it exits after the stuck call
returns) and a fresh worker takes the queue. InlineExecutor runs jobs synchronously (tests, --text).

Jobs named in SUSPEND_JOBS ("sleep", "hibernate") run with NO timeout: the call returns only after the PC
wakes, and the monotonic clock (QueryPerformanceCounter) keeps counting through the suspend, so the 15 s
watchdog used to fire a false TimeoutError ("That's taking too long") 25 s later when the machine came back.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Any, Callable

log = logging.getLogger("xyrus.executor")

OnDone = Callable[[Any, BaseException | None], None]
SUSPEND_JOBS = frozenset({"sleep", "hibernate"})    # never watched: they block across the suspend by design


class _Job:
    __slots__ = ("fn", "timeout", "name", "on_done", "started", "settled", "lock")

    def __init__(self, fn, timeout, name, on_done):
        self.fn, self.timeout, self.name, self.on_done = fn, timeout, name, on_done
        self.started: float | None = None
        self.settled = False                 # on_done already delivered (result, error or timeout)
        self.lock = threading.Lock()

    def settle(self, result, error) -> None:
        with self.lock:
            if self.settled:
                return
            self.settled = True
        if self.on_done is not None:
            try:
                self.on_done(result, error)
            except Exception:
                log.exception("on_done of job %s failed", self.name)


class _Worker:
    def __init__(self, ex: "Executor", n: int):
        self.ex = ex
        self.abandoned = False
        self.current: _Job | None = None
        self.thread = threading.Thread(target=self._run, name=f"{ex.name}-{n}", daemon=True)

    def _run(self) -> None:
        try:
            import comtypes
            comtypes.CoInitialize()
        except Exception:
            pass
        while not self.abandoned and not self.ex._stop.is_set():
            try:
                job = self.ex._q.get(timeout=0.5)
            except queue.Empty:
                continue
            if job is None:
                break
            job.started = time.monotonic()
            self.current = job
            try:
                result = job.fn()
                error = None
            except BaseException as e:          # noqa: BLE001 - reported to the caller
                result, error = None, e
            self.current = None
            if error is not None and not job.settled:
                log.warning("job %s failed: %r", job.name, error)
            job.settle(result, error)


class Executor:
    def __init__(self, name: str = "exec", max_abandoned: int = 2, watch_period: float = 0.05):
        self.name = name
        self.max_abandoned = max_abandoned
        self.watch_period = watch_period
        self.abandoned = 0
        self._q: "queue.Queue[_Job | None]" = queue.Queue()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._n = 0
        self._worker = self._new_worker()
        self._watch = threading.Thread(target=self._watchdog, name=f"{name}-watch", daemon=True)
        self._watch.start()

    def _new_worker(self) -> _Worker:
        self._n += 1
        w = _Worker(self, self._n)
        w.thread.start()
        return w

    def submit(self, fn: Callable[[], Any], *, timeout: float | None = 15.0, name: str = "",
               on_done: OnDone | None = None) -> None:
        name = name or getattr(fn, "__name__", "job")
        if name in SUSPEND_JOBS:
            timeout = None                   # the watchdog skips it (see the module docstring)
        self._q.put(_Job(fn, timeout, name, on_done))

    def _watchdog(self) -> None:
        while not self._stop.wait(self.watch_period):
            with self._lock:
                w = self._worker
                job = w.current
                if job is None or job.started is None or job.settled or job.timeout is None:
                    continue
                if time.monotonic() - job.started <= job.timeout:
                    continue
                w.abandoned = True
                self.abandoned += 1
                level = logging.ERROR if self.abandoned > self.max_abandoned else logging.WARNING
                log.log(level, "job %s timed out after %.1f s; abandoning worker %s (%d abandoned)",
                        job.name, job.timeout, w.thread.name, self.abandoned)
                self._worker = self._new_worker()
            job.settle(None, TimeoutError(job.name))

    def stop(self) -> None:
        self._stop.set()
        self._q.put(None)

    def idle(self) -> bool:
        """True when no job is queued or running (tests)."""
        return self._q.empty() and self._worker.current is None


class InlineExecutor:
    """Runs jobs synchronously on the caller's thread; timeouts are not enforced."""

    def __init__(self):
        self.jobs: list[str] = []

    def submit(self, fn: Callable[[], Any], *, timeout: float = 15.0, name: str = "",
               on_done: OnDone | None = None) -> None:
        self.jobs.append(name or getattr(fn, "__name__", "job"))
        try:
            result, error = fn(), None
        except BaseException as e:              # noqa: BLE001
            result, error = None, e
        if on_done is not None:
            on_done(result, error)

    def stop(self) -> None:
        pass
