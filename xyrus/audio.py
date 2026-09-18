"""Microphone capture (SPEC §4.12). Pause = stream closed (D5); devices from MME + DirectSound only,
persisted as {name, hostapi} and resolved to an index at open (D8, F14: WASAPI rejects 16 kHz).

Threads: the PortAudio callback (T-audio-cb) only enqueues + updates the peak; start/stop/restart are
callable from any thread (RLock); the watchdog (T-watch, 1 s period) reopens a stalled or dead stream."""
from __future__ import annotations

import logging
import queue
import threading
import time
from array import array
from dataclasses import dataclass
from typing import Any, Callable

import sounddevice as sd

log = logging.getLogger("xyrus.audio")

ALLOWED_HOSTAPIS = ("MME", "Windows DirectSound")
MME_NAME_MAX = 31                 # MME truncates device names to 31 characters
STALL_S = 3.0                     # no chunk for this long while started -> reopen
WATCH_PERIOD_S = 1.0
BACKOFF_S = (1, 2, 5, 10)         # delays between failed reopen attempts; the first reopen is immediate


@dataclass(frozen=True)
class MicDevice:
    index: int
    name: str
    hostapi: str
    default_samplerate: float


def list_mics() -> list[MicDevice]:
    """Input devices on the MME and DirectSound host APIs (D8)."""
    try:
        apis = sd.query_hostapis()
        devices = sd.query_devices()
    except Exception as e:
        log.warning("cannot list audio devices: %s", e)
        return []
    out: list[MicDevice] = []
    for i, d in enumerate(devices):
        try:
            if d["max_input_channels"] <= 0:
                continue
            api = apis[d["hostapi"]]["name"]
        except (KeyError, IndexError, TypeError):
            continue
        if api in ALLOWED_HOSTAPIS:
            out.append(MicDevice(i, str(d["name"]), api, float(d["default_samplerate"])))
    return out


def _same_name(a: str, b: str) -> bool:
    if a == b:
        return True
    short, long_ = sorted((a, b), key=len)
    return len(short) >= MME_NAME_MAX and long_.startswith(short)


def resolve_mic(name: str | None, hostapi: str | None) -> int | None:
    """Exact name + api, else the same name on any allowed api (MME preferred), else the default input
    (sd.default.device[0], mapped onto an allowed api if needed). None when there is no input at all."""
    mics = list_mics()
    if not mics:
        return None
    if name:
        for m in mics:
            if m.name == name and m.hostapi == hostapi:
                return m.index
        same = [m for m in mics if _same_name(m.name, name)]
        if same:
            same.sort(key=lambda m: (m.hostapi != hostapi, m.hostapi != "MME"))
            return same[0].index
        log.warning("microphone %r (%s) not found - using the default input", name, hostapi)
    allowed = {m.index for m in mics}
    try:
        idx = sd.default.device[0]
    except Exception:
        idx = -1
    if isinstance(idx, int) and idx in allowed:
        return idx
    try:                              # default is on another api (or unset): same device via MME/DirectSound
        default_name = str(sd.query_devices(kind="input")["name"])
        same = sorted((m for m in mics if _same_name(m.name, default_name)), key=lambda m: m.hostapi != "MME")
        if same:
            return same[0].index
    except Exception:
        pass
    mme = [m for m in mics if m.hostapi == "MME"]
    return (mme or mics)[0].index


class AudioCapture:
    """16 kHz int16 mono capture into `chunks` (Queue[(pcm_bytes, time.monotonic())]); 0.25 s blocks."""
    SAMPLE_RATE = 16000
    BLOCK = 4000                  # 0.25 s int16 mono -> 8000-byte blocks (F14)
    QUEUE_MAX = 200               # 50 s; on Full the oldest chunk is dropped and counted

    def __init__(self, get_device: Callable[[], tuple[str | None, str | None]],
                 on_status: Callable[[str], None]):
        self._get_device = get_device
        self._on_status = on_status
        self.chunks: "queue.Queue[tuple[bytes, float]]" = queue.Queue(maxsize=self.QUEUE_MAX)
        self.drops = 0
        self.callbacks = 0            # raw PortAudio callback invocations (diagnostics / tests)
        self.opens = 0                # successful stream opens
        self.device_index: int | None = None
        self._lock = threading.RLock()
        self._stream: Any = None
        self._want = False            # started (not paused)
        self._status = "paused"
        self._level = 0.0
        self._last_chunk = 0.0
        self._retry_at = 0.0
        self._retry_n = 0
        self._watch: threading.Thread | None = None
        self._watch_stop = threading.Event()

    # ------------------------------------------------------------------ T-audio-cb
    def _callback(self, indata, frames, t, status) -> None:
        self.callbacks += 1
        if not self._want:
            return
        data = bytes(indata)
        now = time.monotonic()
        try:
            self.chunks.put_nowait((data, now))
        except queue.Full:
            try:
                self.chunks.get_nowait()
            except queue.Empty:
                pass
            self.drops += 1
            try:
                self.chunks.put_nowait((data, now))
            except queue.Full:
                pass
        samples = array("h")
        samples.frombytes(data[: len(data) & ~1])
        every8 = samples[::8]
        peak = max(max(every8), -min(every8)) / 32768.0 if every8 else 0.0
        self._level = peak if peak > self._level else self._level * 0.7 + peak * 0.3
        self._last_chunk = now

    # ------------------------------------------------------------------ public (any thread)
    def start(self) -> None:
        with self._lock:
            self._want = True
            if self._stream is None:
                self._retry_n = 0
                self._retry_at = 0.0
                self._try_open()
            self._ensure_watchdog()

    def stop(self) -> None:
        """Pause (D5): closes the stream - nothing is captured until start(); clears the queue."""
        with self._lock:
            self._want = False
            self._close_stream(abort=False)
            self._clear_queue()
            self._level = 0.0
            self._set_status("paused")

    def restart(self) -> None:
        """Reopen with the current device setting (e.g. after a mic change in Settings). No-op while paused."""
        with self._lock:
            if not self._want:
                return
            self._close_stream(abort=False)
            self._clear_queue()
            self._retry_n = 0
            self._retry_at = 0.0
            self._try_open()

    def running(self) -> bool:
        return self._want and self._stream is not None

    def level(self) -> float:
        if not self.running() or time.monotonic() - self._last_chunk > 1.0:
            return 0.0
        return min(1.0, self._level)

    def status(self) -> str:
        return self._status

    def close(self) -> None:
        """App shutdown: stop capturing and stop the watchdog."""
        self.stop()
        self._watch_stop.set()
        w = self._watch
        if w is not None and w is not threading.current_thread():
            w.join(2.0)

    # ------------------------------------------------------------------ internals (lock held)
    def _set_status(self, status: str) -> None:
        if status == self._status:
            return
        self._status = status
        log.info("microphone: %s", status)
        try:
            self._on_status(status)
        except Exception:
            log.exception("on_status callback failed")

    def _clear_queue(self) -> None:
        while True:
            try:
                self.chunks.get_nowait()
            except queue.Empty:
                return

    def _close_stream(self, *, abort: bool) -> None:
        stream, self._stream = self._stream, None
        if stream is None:
            return
        try:
            stream.abort() if abort else stream.stop()
        except Exception as e:
            log.debug("stream stop: %s", e)
        try:
            stream.close()
        except Exception as e:
            log.debug("stream close: %s", e)

    def _schedule_retry(self) -> None:
        delay = BACKOFF_S[min(self._retry_n, len(BACKOFF_S) - 1)]
        self._retry_n += 1
        self._retry_at = time.monotonic() + delay

    def _try_open(self) -> bool:
        try:
            name, api = self._get_device() or (None, None)
        except Exception:
            log.exception("get_device failed")
            name = api = None
        idx = resolve_mic(name, api)
        if idx is None:
            self._set_status("no microphone")
            self._schedule_retry()
            return False
        stream = None
        try:
            stream = sd.RawInputStream(device=idx, samplerate=self.SAMPLE_RATE, blocksize=self.BLOCK,
                                       dtype="int16", channels=1, callback=self._callback)
            stream.start()
        except Exception as e:
            log.warning("cannot open microphone %s: %s", idx, e)
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass
            self._set_status("mic error, retrying")
            self._schedule_retry()
            return False
        self._stream = stream
        self.device_index = idx
        self.opens += 1
        self._retry_n = 0
        self._last_chunk = time.monotonic()      # grace period until the first block arrives
        log.info("microphone open: device %s (%s, %s)", idx, name or "default", api or "-")
        self._set_status("listening")
        return True

    @staticmethod
    def _reinit_portaudio() -> None:
        # Private sounddevice API, the only way to refresh PortAudio's device list (§4.12).
        try:
            sd._terminate()
            sd._initialize()
        except Exception as e:
            log.debug("PortAudio re-init failed: %s", e)

    # ------------------------------------------------------------------ T-watch
    def _ensure_watchdog(self) -> None:
        if self._watch is not None and self._watch.is_alive():
            return
        self._watch_stop.clear()
        self._watch = threading.Thread(target=self._watch_loop, name="T-watch", daemon=True)
        self._watch.start()

    def _watch_loop(self) -> None:
        while not self._watch_stop.wait(WATCH_PERIOD_S):
            try:
                self._check()
            except Exception:
                log.exception("microphone watchdog failed")

    def _check(self) -> None:
        with self._lock:
            if not self._want:
                return
            now = time.monotonic()
            if self._stream is not None:
                try:
                    active = bool(self._stream.active)
                except Exception:
                    active = False
                silent_for = now - self._last_chunk
                if active and silent_for <= STALL_S:
                    return
                log.warning("microphone %s (no audio for %.1f s) - reopening",
                            "stalled" if active else "stopped", silent_for)
                self._close_stream(abort=True)
                self._clear_queue()
                self._set_status("mic error, retrying")
                self._retry_n = 0
                self._retry_at = now
            if now < self._retry_at:
                return
            self._reinit_portaudio()
            self._try_open()
