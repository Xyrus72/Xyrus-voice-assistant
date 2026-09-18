"""Speaker (SAPI.SpVoice owned by T-speak; D10, F6) and Chime (winmm.PlaySoundW from module-level
buffers; D9, F9). Implements interfaces.Speaker / interfaces.Chime. Never pyttsx3 (G3); the voice is
only ever touched on T-speak (G5) - a cross-thread purge does not work on this machine (F6)."""
from __future__ import annotations

import ctypes
import io
import itertools
import logging
import math
import queue
import struct
import threading
import time
import wave
from collections import deque
from typing import Any, Callable

log = logging.getLogger("xyrus.speech")

SVSF_ASYNC = 1               # SVSFlagsAsync
SVSF_PURGE = 2               # SVSFPurgeBeforeSpeak
GATE_BEFORE_S = 0.05         # is_quiet_at window: [start - 0.05, end + 0.35] (interfaces.Speaker)
GATE_AFTER_S = 0.35
INTERVALS_KEPT = 8


def _call(fn: Callable[[], None] | None) -> None:
    if fn is None:
        return
    try:
        fn()
    except Exception:
        log.exception("speech on_done callback failed")


class SpeechIntervals:
    """The last N speech intervals for the echo gate. Thread-safe; an open interval has end = +inf."""

    def __init__(self, keep: int = INTERVALS_KEPT, before: float = GATE_BEFORE_S, after: float = GATE_AFTER_S):
        self._lock = threading.Lock()
        self._iv: deque[list[float]] = deque(maxlen=keep)
        self.before = before
        self.after = after

    def open(self, t: float) -> list[float]:
        iv = [t, math.inf]
        with self._lock:
            self._iv.append(iv)
        return iv

    def close(self, iv: list[float], t: float) -> None:
        with self._lock:
            iv[1] = t

    def quiet_at(self, t: float) -> bool:
        with self._lock:
            for start, end in self._iv:
                if start - self.before <= t <= end + self.after:
                    return False
        return True

    def snapshot(self) -> list[tuple[float, float]]:
        with self._lock:
            return [(s, e) for s, e in self._iv]


class _Item:
    __slots__ = ("text", "force", "on_done", "gen")

    def __init__(self, text: str, force: bool, on_done: Callable[[], None] | None, gen: int):
        self.text, self.force, self.on_done, self.gen = text, force, on_done, gen


_WAKE = object()     # wakes T-speak to apply voice/rate/volume changes
_CLOSE = object()


class SapiSpeaker:
    """interfaces.Speaker over SAPI. Loop (§4.12): PriorityQueue[(priority, seq, item)]; skip (on_done at
    once) when not force and not config.voice_replies; else record the interval start, Speak(text, async),
    poll WaitUntilDone(50) and purge on the owner thread when stop() was called; record the end; on_done in
    `finally`. On a COM error the voice is re-created (CoUninitialize/CoInitialize).

    Test hooks (keyword-only, not part of the protocol): audio_output="memory" routes speech into a fresh
    SpMemoryStream per utterance (silent, faster than real time); voice_factory replaces
    CreateObject("SAPI.SpVoice")."""

    def __init__(self, config: Any, *, audio_output: str | None = None,
                 voice_factory: Callable[[], Any] | None = None):
        self.config = config
        self._audio_output = audio_output
        self._voice_factory = voice_factory
        self._q: queue.PriorityQueue = queue.PriorityQueue()
        self._seq = itertools.count()
        self._gen_lock = threading.Lock()
        # SPEC-AMBIGUITY: §4.12 describes stop() as "sets the event and empties the queue". A stop
        # generation counter gives the same behaviour without the clear-the-event race: items queued before
        # stop() carry an older generation and are skipped on T-speak (their on_done still called), and the
        # utterance being spoken is purged within one 50 ms WaitUntilDone poll.
        self._stop_gen = 0
        self._closed = False
        self._ctl: queue.Queue = queue.Queue()
        self._overrides: dict[str, Any] = {}
        self._intervals = SpeechIntervals()
        self._speaking = threading.Event()
        self._voices: list[str] = []
        self._ready = threading.Event()
        self._voice: Any = None
        self._default_token: Any = None
        self.spoken = 0                       # utterances actually sent to SAPI (diagnostics/tests)
        self._thread = threading.Thread(target=self._run, name="T-speak", daemon=True)
        self._thread.start()

    # ------------------------------------------------------------------ public (any thread)
    def say(self, text: str, *, priority: int = 1, force: bool = False,
            on_done: Callable[[], None] | None = None) -> None:
        with self._gen_lock:
            gen, closed = self._stop_gen, self._closed
        if closed:                            # no T-speak any more: honour "on_done exactly once"
            _call(on_done)
            return
        self._q.put((int(priority), next(self._seq), _Item(str(text or ""), bool(force), on_done, gen)))

    def stop(self) -> None:
        with self._gen_lock:
            self._stop_gen += 1

    def is_speaking(self) -> bool:
        return self._speaking.is_set()

    def is_quiet_at(self, t_mono: float) -> bool:
        return self._intervals.quiet_at(t_mono)

    def voices(self) -> list[str]:
        self._ready.wait(5.0)
        return list(self._voices)

    def set_voice(self, name: str | None) -> None:
        self._control("voice", name)

    def set_rate(self, rate: int) -> None:
        self._control("rate", max(-10, min(10, int(rate))))

    def set_volume(self, volume: int) -> None:          # extra (config tts.volume), not in the protocol
        self._control("volume", max(0, min(100, int(volume))))

    def close(self, timeout: float = 2.0) -> None:
        """Stop T-speak (app shutdown / tests). Pending on_done callbacks are still called."""
        with self._gen_lock:
            self._closed = True
            self._stop_gen += 1
        self._q.put((-2, next(self._seq), _CLOSE))
        if self._thread is not threading.current_thread():
            self._thread.join(timeout)

    def wait_idle(self, timeout: float = 10.0) -> bool:
        """Test helper: True once nothing is queued or speaking."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self._q.empty() and not self._speaking.is_set():
                return True
            time.sleep(0.01)
        return False

    # ------------------------------------------------------------------ T-speak
    def _control(self, kind: str, value: Any) -> None:
        self._ctl.put((kind, value))
        self._q.put((-1, next(self._seq), _WAKE))

    def _cfg(self, key: str, default: Any = None) -> Any:
        try:
            value = self.config.get(key, default)
        except Exception:
            return default
        return default if value is None else value

    def _run(self) -> None:
        comtypes = None
        try:
            import comtypes as _comtypes
            _comtypes.CoInitialize()
            comtypes = _comtypes
        except Exception:
            log.exception("CoInitialize failed on T-speak")
        try:
            self._make_voice()
        finally:
            self._ready.set()
        try:
            while True:
                _, _, item = self._q.get()
                if item is _CLOSE:
                    break
                self._apply_controls()
                if item is _WAKE:
                    continue
                self._handle(item)
        finally:
            while True:                        # close(): still honour pending on_done callbacks
                try:
                    _, _, item = self._q.get_nowait()
                except queue.Empty:
                    break
                if isinstance(item, _Item):
                    _call(item.on_done)
            self._voice = None
            self._default_token = None
            if comtypes is not None:
                try:
                    comtypes.CoUninitialize()
                except Exception:
                    pass

    def _make_voice(self) -> None:
        try:
            if self._voice_factory is not None:
                voice = self._voice_factory()
            else:
                import comtypes.client
                voice = comtypes.client.CreateObject("SAPI.SpVoice")
        except Exception:
            log.exception("SAPI voice could not be created")
            self._voice = None
            return
        self._voice = voice
        try:
            self._default_token = voice.Voice
        except Exception:
            self._default_token = None
        try:
            tokens = voice.GetVoices()
            self._voices = [tokens.Item(i).GetDescription() for i in range(tokens.Count)]
        except Exception:
            log.exception("cannot list SAPI voices")
        settings = {"voice": self._cfg("tts.voice"), "rate": self._cfg("tts.rate", 0),
                    "volume": self._cfg("tts.volume", 100)}
        settings.update(self._overrides)
        for kind, value in settings.items():
            self._apply(kind, value)

    def _apply_controls(self) -> None:
        while True:
            try:
                kind, value = self._ctl.get_nowait()
            except queue.Empty:
                return
            self._overrides[kind] = value
            self._apply(kind, value)

    def _apply(self, kind: str, value: Any) -> None:
        voice = self._voice
        if voice is None:
            return
        try:
            if kind == "rate":
                voice.Rate = max(-10, min(10, int(value or 0)))
            elif kind == "volume":
                voice.Volume = max(0, min(100, int(100 if value is None else value)))
            elif kind == "voice":
                self._apply_voice(voice, value)
        except Exception:
            log.exception("cannot apply SAPI %s=%r", kind, value)

    def _apply_voice(self, voice: Any, name: str | None) -> None:
        if not name:
            if self._default_token is not None:
                voice.Voice = self._default_token
            return
        tokens = voice.GetVoices()
        want = str(name).lower()
        items = [tokens.Item(i) for i in range(tokens.Count)]
        for exact in (True, False):
            for tok in items:
                desc = tok.GetDescription().lower()
                if (desc == want) if exact else (want in desc):
                    voice.Voice = tok
                    return
        log.warning("SAPI voice %r not found - keeping the current voice", name)

    def _handle(self, item: _Item) -> None:
        try:
            if item.gen != self._stop_gen:
                return                                        # purged by stop()
            text = item.text.strip()
            if not text:
                return
            if not item.force and not self._cfg("voice_replies", True):
                return                                        # voice replies off: skipped, on_done now
            if self._voice is None:
                self._recreate()
            if self._voice is None:
                return
            self._speak(item, text)
        except Exception:
            log.exception("speech failed")
        finally:
            _call(item.on_done)

    def _speak(self, item: _Item, text: str) -> None:
        voice = self._voice
        if self._audio_output == "memory" and self._voice_factory is None:
            import comtypes.client
            voice.AudioOutputStream = comtypes.client.CreateObject("SAPI.SpMemoryStream")
        iv = self._intervals.open(time.monotonic())
        self._speaking.set()
        try:
            try:
                voice.Speak(text, SVSF_ASYNC)
                self.spoken += 1
                while not voice.WaitUntilDone(50):
                    if item.gen != self._stop_gen:
                        voice.Speak("", SVSF_PURGE)           # purge on the owner thread (D10)
                        break
            except Exception as e:                            # COMError: SAPI died under us
                log.warning("SAPI error (%s) - re-creating the voice", e)
                self._recreate()
        finally:
            self._intervals.close(iv, time.monotonic())
            self._speaking.clear()

    def _recreate(self) -> None:
        self._voice = None
        self._default_token = None
        if self._voice_factory is None:
            try:
                import comtypes
                comtypes.CoUninitialize()
                comtypes.CoInitialize()
            except Exception:
                log.exception("COM re-initialisation failed")
        self._make_voice()


# ============================================================================ chime (D9, F9)
SAMPLE_RATE = 22050
SND_ASYNC = 0x0001
SND_NODEFAULT = 0x0002
SND_MEMORY = 0x0004
BASE_AMP = 0.4
SOUNDS: dict[str, tuple[tuple[int, ...], int]] = {   # kind -> (frequencies in sequence, ms each)
    "wake": ((880, 1320), 100),
    "alert": ((988, 1319, 988, 1319), 120),
    "error": ((220,), 250),
}


def make_wav(freqs: tuple[int, ...], each_ms: int, amp: float = BASE_AMP, rate: int = SAMPLE_RATE) -> bytes:
    """16-bit mono WAV image of consecutive sine tones with a 5 ms attack / 20 ms release per tone."""
    frames = bytearray()
    for f in freqs:
        n = int(rate * each_ms / 1000)
        for i in range(n):
            env = min(1.0, i / (rate * 0.005), (n - i) / (rate * 0.02))
            frames += struct.pack("<h", int(amp * env * math.sin(2 * math.pi * f * i / rate) * 32767))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))
    return buf.getvalue()


# Module-level on purpose: winmm reads the buffer asynchronously after PlaySoundW returns, so it must
# outlive every playback (F9). Keyed by (kind, volume).
_BUFFERS: dict[tuple[str, float], ctypes.Array] = {}
_BUFFERS_LOCK = threading.Lock()

try:
    _winmm = ctypes.WinDLL("winmm")          # private handle: argtypes don't leak into ctypes.windll users
    _PlaySoundW = _winmm.PlaySoundW
    _PlaySoundW.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint]
    _PlaySoundW.restype = ctypes.c_int
except (OSError, AttributeError):             # not Windows
    _PlaySoundW = None


def chime_buffer(kind: str, volume: float = 1.0) -> ctypes.Array:
    vol = round(max(0.0, min(1.0, float(volume))), 3)
    key = (kind, vol)
    buf = _BUFFERS.get(key)
    if buf is None:
        freqs, each_ms = SOUNDS[kind]
        wav = make_wav(freqs, each_ms, BASE_AMP * vol)
        with _BUFFERS_LOCK:
            buf = _BUFFERS.setdefault(key, ctypes.create_string_buffer(wav, len(wav)))
    return buf


class WinmmChime:
    """interfaces.Chime: async, returns in ~1 ms, safe from any thread. `volume` (0..1) scales the tones."""
    SOUNDS = SOUNDS

    def __init__(self, config: Any = None, *, volume: float = 1.0):
        self.config = config
        self.volume = volume
        for kind in SOUNDS:                   # build every buffer now so play() never synthesises
            chime_buffer(kind, volume)

    def play(self, kind: str) -> None:
        if _PlaySoundW is None:
            return
        try:
            buf = chime_buffer(kind, self.volume)
            _PlaySoundW(ctypes.cast(buf, ctypes.c_void_p), None, SND_MEMORY | SND_ASYNC | SND_NODEFAULT)
        except KeyError:
            log.warning("unknown chime %r", kind)
        except Exception:
            log.exception("chime failed")

    def stop(self) -> None:
        if _PlaySoundW is None:
            return
        try:
            _PlaySoundW(None, None, 0)        # NULL sound = stop the current waveform sound
        except Exception:
            log.exception("chime stop failed")
