"""T-rec: the recognizer thread - the only Vosk user (SPEC §4.12, §5.2-§5.4, D1, D2, G5).

Per chunk: listen spec -> grammar switch at an utterance boundary -> echo gate -> utterance PCM buffer
(20 s cap, 0.25 s pre-roll) -> AcceptWaveform -> wake chime on the partial -> on a final: normalise,
wake re-decode with the command grammar, free re-decode with the unrestricted recognizer (§5.3) ->
on_transcript(Transcript). `decode_pcm` runs the same final-result code on a whole utterance (tests, --wav).
"""
from __future__ import annotations

import dataclasses
import difflib
import hashlib
import json
import logging
import queue
import re
import threading
import time
import wave
from array import array
from collections import Counter, deque
from pathlib import Path
from typing import Any, Callable, Iterable

from xyrus.banglish import has_bengali
from xyrus.capture import (CAPTURE_MODES, CapturedTranscript, Endpointer, bangla_likely, clean_text,
                           looks_like_song, only_filler, strip_wake_words)
from xyrus.interfaces import ListenSpec, Transcript, Word
from xyrus.normalize import NOISE_WORDS, PLAY_LIKE, normalize

log = logging.getLogger("xyrus.recognizer")

SAMPLE_RATE = 16000
BYTES_PER_S = SAMPLE_RATE * 2
CHUNK_S = 0.25
MAX_UTT_BYTES = 20 * BYTES_PER_S          # per-utterance PCM cap (v1 MAX_UTTERANCE_BYTES)
RING_S = 30                               # save_last_seconds ring buffer
UNK = "[unk]"
DEFAULT_WAKE = ("cyrus", "zeros", "virus", "cirrus")
OPEN_WORDS = frozenset({"open", "launch", "start"})
APP_FILLER = frozenset({"the", "my", "a", "an", "please"})
APP_MATCH_CUTOFF = 0.6
RECENT_UTTERANCES = 4                     # PCM kept for request_free_decode
# SPEC-AMBIGUITY: §5.3 says the words after the wake "start with" a free trigger. "i finished <text>" and
# "please add ..." put one word before the trigger, so the trigger may start at token 0 or 1 of the rest.
TRIGGER_WINDOW = 2
REC_CACHE_MAX = 12
ACK_TIMEOUT_S = 10.0                      # a capture waits this long (audio time) for the engine's ack at most
# Idle Whisper second opinion on the name (lead, Sep 14): live, a voice from across the room said "xyrus" 3 times;
# the Vosk wake grammar heard nothing all 3 times, Whisper wrote "Xyrus." all 3 (~0.9 s each). Only short
# utterances are checked (a call, not a conversation), and only these spellings count (not "virus" / "zeros").
WAKE_CHECK_MAX_SPEECH_S = 2.5
WAKE_CHECK_MAX_S = 4.0
WHISPER_NAMES = frozenset({"xyrus", "cyrus", "sirus", "sirius", "zyrus", "xirus", "cirus", "sairus", "cyrius",
                           "xyris", "cyris", "zirus"})


def pcm_peak(pcm: bytes) -> float:
    """Max |sample| / 32768 of int16 PCM."""
    if len(pcm) < 2:
        return 0.0
    a = array("h")
    a.frombytes(pcm[: len(pcm) & ~1])
    return max(max(a), -min(a)) / 32768.0


def literal_prefixes(pattern_text: str) -> set[tuple[str, ...]]:
    """Every literal word sequence before the first <slot> of a registry pattern (options expanded)."""
    prefixes: list[tuple[str, ...]] = [()]
    for tok in pattern_text.lower().split():
        if tok.startswith("<"):
            break
        optional = tok.startswith("[") and tok.endswith("]")
        alts = [a for a in tok.strip("[]").split("|") if a]
        grown: list[tuple[str, ...]] = []
        for p in prefixes:
            if optional:
                grown.append(p)
            grown.extend(p + (a,) for a in alts)
        prefixes = grown
    return {p for p in prefixes if p}


class Recognizer:
    def __init__(self, model_dir: Path, capture: Any, grammar: Any,
                 get_spec: Callable[[], ListenSpec], on_transcript: Callable[[Transcript], None],
                 speaker: Any, chime: Any, config: Any, *, app_index: Any = None):
        self.model_dir = Path(model_dir)
        # anything with names() -> list[str]: actions.apps.StartMenuIndex (injected by app.py, never walked here)
        self._app_index = app_index
        # pluggable free-TEXT hearing (T6): fn(pcm16k) -> text | None; the app plugs Whisper (hearing.py)
        self._free_decoder: Callable[[bytes], str | None] | None = None
        self.capture = capture
        self.grammar = grammar
        self.get_spec = get_spec
        self.on_transcript = on_transcript
        self.on_clap: Callable[[], None] | None = None    # app.py: clap to switch the display (clap.py)
        self._clap: Any = None
        self.speaker = speaker
        self.chime = chime
        self.config = config
        self._model: Any = None
        self._recent: deque[tuple[float, float, bytes]] = deque(maxlen=RECENT_UTTERANCES)
        self._recent_lock = threading.Lock()
        self.load_error: BaseException | None = None
        self._load_lock = threading.Lock()
        self._inline_lock = threading.RLock()    # serialises Vosk use when T-rec is not running
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.ready = threading.Event()           # model loaded (or failed) on T-rec
        self._requests: "queue.Queue[tuple[Callable[[], Any], threading.Event, dict]]" = queue.Queue()
        self._live_cache: dict[tuple, Any] = {}   # (mode, words-hash) -> KaldiRecognizer (streaming)
        self._aux_cache: dict[tuple, Any] = {}    # same, for re-decodes and decode_pcm
        self._free_aux: Any = None               # unrestricted recognizer for the free re-decode
        self._cache_version: Any = object()
        self._triggers: tuple[set, set] | None = None
        self._ring: deque[bytes] = deque(maxlen=int(RING_S / CHUNK_S))
        self._ring_lock = threading.Lock()
        self.stats: Counter = Counter()
        self.last_timing: dict[str, float] = {}
        self.heartbeat = 0.0
        self._spec_error_logged = False
        # streaming state (T-rec only)
        self._rec: Any = None
        self._active: tuple[str, tuple[str, ...]] | None = None
        self._rebuild = False                    # grammar version changed: switch at the next boundary
        self._utt = bytearray()
        self._preroll = b""
        self._utt_t0 = 0.0
        self._utt_t1 = 0.0
        self._utt_peak = 0.0
        self._chimed = False
        self._fed_s = 0.0                        # seconds fed to self._rec since its last reset
        self._utt_rec_t0 = 0.0                   # recognizer time of the utterance buffer start
        # command capture (lead, Sep 14; capture.py): after the wake reply ONE utterance is recorded until the user
        # has finished and Whisper hears it. Hooks wired by app.py; None / unset = the Vosk grammars as before.
        self._command_hearing: Any = None
        self.on_capture_state: Callable[[str], None] | None = None   # engine.set_capture_state
        self.song_question: Callable[[], bool] | None = None          # engine.song_question_open
        self.ack_required = False                # True once engine.on_capture_handled -> capture_ack is wired
        self._endpointer: Endpointer | None = None
        self._cap_state = ""
        self._await_ack = False
        self._ack_gen = -1
        self._ack_t = 0.0
        self._last_heard: tuple[float, float, Any] | None = None     # (t_start, t_end, Whisper handle)
        self._vosk_wakes: deque[tuple[float, float]] = deque(maxlen=8)      # spans the Vosk grammar heard the name in
        self._whisper_wakes: deque[tuple[float, float]] = deque(maxlen=8)   # spans Whisper's second opinion answered

    # ------------------------------------------------------------------ lifecycle
    def load(self) -> Any:
        """Load the Vosk model once (heavy). Safe to call repeatedly."""
        with self._load_lock:
            if self._model is None:
                from vosk import Model, SetLogLevel
                SetLogLevel(-1)
                t = time.monotonic()
                self._model = Model(str(self.model_dir))
                log.info("speech model loaded in %.1f s", time.monotonic() - t)
        return self._model

    def start(self) -> None:
        """Start T-rec; the model is loaded on T-rec so the caller (UI startup) never waits for it."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="T-rec", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        th = self._thread
        if th is not None and th.is_alive() and th is not threading.current_thread():
            th.join(timeout)

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ------------------------------------------------------------------ thread-safe requests
    def _on_rec(self, fn: Callable[[], Any], timeout: float) -> Any:
        th = self._thread
        if th is None or not th.is_alive() or threading.current_thread() is th:
            with self._inline_lock:
                return fn()
        done = threading.Event()
        box: dict[str, Any] = {}
        self._requests.put((fn, done, box))
        if not done.wait(timeout):
            raise TimeoutError("recognizer thread did not answer")
        if "error" in box:
            raise box["error"]
        return box.get("value")

    def _service_requests(self) -> None:
        while True:
            try:
                fn, done, box = self._requests.get_nowait()
            except queue.Empty:
                return
            try:
                box["value"] = fn()
            except BaseException as e:
                box["error"] = e
            finally:
                done.set()

    def check_words(self, words: list[str]) -> dict[str, bool]:
        """word -> known to the model (vosk_model_find_word >= 0). Thread-safe; 2 s timeout once T-rec runs."""
        wanted = [str(w).lower() for w in words]
        th = self._thread
        if th is not None and th.is_alive() and threading.current_thread() is not th:
            self.ready.wait(20.0)                # the first call may race the model load

        def work() -> dict[str, bool]:
            model = self.load()
            return {w: (w == UNK or model.vosk_model_find_word(w) >= 0) for w in wanted}
        return self._on_rec(work, 2.0)

    def decode_pcm(self, pcm: bytes, mode: str, extra: Iterable[str] = ()) -> Transcript:
        """Decode one whole utterance through the same final-result path as the mic (no audio device).
        Empty results are returned (text "") instead of being dropped."""
        if mode == "paused":
            raise ValueError("cannot decode in paused mode")
        pcm = bytes(pcm)
        extra_t = tuple(extra)

        def work() -> Transcript:
            self.load()
            self._check_version()
            rec = self._get_rec(self._aux_cache, mode, extra_t)
            rec.AcceptWaveform(pcm)
            res = json.loads(rec.FinalResult())
            rec.Reset()
            now = time.monotonic()
            tr = self._build(res, mode, pcm, peak=pcm_peak(pcm), t0=now - len(pcm) / BYTES_PER_S, t1=now,
                             keep_empty=True)
            assert tr is not None
            return tr
        return self._on_rec(work, 120.0)

    def request_free_decode(self, tr: Transcript, cb: Callable[[str], None]) -> bool:
        """Engine hook (T1): re-decode the PCM of an earlier mic transcript with the unrestricted recognizer and
        call cb(text) - from T-rec when it runs, inline otherwise (tests, --wav). Never blocks the caller while
        T-rec runs. False when that utterance's PCM is no longer kept (only the last few are)."""
        with self._recent_lock:
            pcm = next((p for t0, t1, p in reversed(self._recent)
                        if t0 == tr.t_start and t1 == tr.t_end), None)
        if pcm is None:
            return False

        def work() -> None:
            try:
                self.load()
                toks = tr.text.split()
                text = (self._app_redecode(pcm, toks) or "") if self.app_split(toks) is not None else ""
                if not text and self._free_decoder is not None and self.wants_free_text(toks, window=len(toks)):
                    text = self._anchor(self._plugged(pcm), toks, window=len(toks))   # free text: plugged first
                if not text and self.capture_ready():                  # Whisper is the retry (lead, Sep 14)
                    text = self._whisper_retry(pcm)
                if not text:
                    text = str(self._redecode(pcm, "free").get("text", "")).strip().lower()
                self.stats["hook_free_decodes"] += 1
            except Exception:
                log.exception("requested free decode failed")
                text = ""
            try:
                cb(text)
            except Exception:
                log.exception("free decode callback failed")

        th = self._thread
        if th is not None and th.is_alive() and threading.current_thread() is not th:
            self._requests.put((work, threading.Event(), {}))
        else:
            with self._inline_lock:
                work()
        return True

    def save_last_seconds(self, path: Path, seconds: int = 30) -> None:
        """Write the last `seconds` of captured audio (before the echo gate) as a 16 kHz mono WAV."""
        with self._ring_lock:
            chunks = list(self._ring)
        n = max(1, int(seconds / CHUNK_S))
        data = b"".join(chunks[-n:])
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SAMPLE_RATE)
            w.writeframes(data)

    # ------------------------------------------------------------------ T-rec loop
    def _run(self) -> None:
        try:
            self.load()
        except Exception as e:
            self.load_error = e
            log.exception("speech model failed to load from %s", self.model_dir)
            self.ready.set()
            return
        self.ready.set()
        while not self._stop.is_set():
            self.heartbeat = time.monotonic()
            self._service_requests()
            try:
                # 0.1 s: check_words requests (UI name validation) are answered promptly even with no audio
                # (paused / no mic); with a live mic a chunk arrives every 0.25 s anyway (T6)
                chunk, t = self.capture.chunks.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                self._process(chunk, t)
            except Exception:
                log.exception("recognizer failed on a chunk - resetting")
                self._rec = None
                self._active = None
                self._reset_utt()
        self._service_requests()

    def _cfg(self, key: str, default: Any = None) -> Any:
        try:
            value = self.config.get(key, default)
        except Exception:
            return default
        return default if value is None else value

    def _wake_words(self) -> set[str]:
        return {str(w).lower() for w in self._cfg("wake_words", DEFAULT_WAKE)}

    def _norm(self, text: str) -> str:
        return normalize(text, self._wake_words())

    def _remember(self, pcm: bytes, t0: float, t1: float) -> None:
        with self._recent_lock:
            self._recent.append((t0, t1, pcm))

    def _read_spec(self) -> ListenSpec:
        try:
            spec = self.get_spec()
            if spec is not None:
                return spec
        except Exception:
            if not self._spec_error_logged:
                self._spec_error_logged = True
                log.exception("get_spec failed - listening for the wake word")
        return ListenSpec("wake")

    def _quiet(self, t: float) -> bool:
        try:
            return bool(self.speaker.is_quiet_at(t))
        except Exception:
            log.exception("speaker.is_quiet_at failed")
            return True

    def _reset_utt(self) -> None:
        self._utt = bytearray()
        self._utt_peak = 0.0
        self._chimed = False

    def _check_version(self) -> None:
        try:
            version = self.grammar.version()
        except Exception:
            log.exception("grammar.version failed")
            version = None
        if version != self._cache_version:
            self._cache_version = version
            self._live_cache.clear()
            self._aux_cache.clear()
            self._triggers = None
            self._rebuild = True         # rebuild the live recognizer at the next utterance boundary

    def _get_rec(self, cache: dict, mode: str, extra: tuple[str, ...], words: list[str] | None = None) -> Any:
        from vosk import KaldiRecognizer
        if words is None:
            words = [] if mode == "free" else self.grammar.words(mode, extra)
        key = (mode, hashlib.md5(json.dumps(words).encode("utf8")).hexdigest())
        rec = cache.get(key)
        if rec is None:
            if len(cache) >= REC_CACHE_MAX:
                cache.clear()
            model = self.load()
            rec = (KaldiRecognizer(model, SAMPLE_RATE, json.dumps(words)) if words
                   else KaldiRecognizer(model, SAMPLE_RATE))
            rec.SetWords(True)
            cache[key] = rec
        else:
            rec.Reset()
        return rec

    def _switch(self, want: tuple[str, tuple[str, ...]]) -> None:
        self._rec = self._get_rec(self._live_cache, want[0], want[1])
        self._active = want
        self._rebuild = False
        self._fed_s = 0.0
        self._reset_utt()
        log.debug("recognizer mode %s extra=%s", want[0], want[1])

    def _partial_busy(self) -> bool:
        return bool(json.loads(self._rec.PartialResult()).get("partial", "").strip())

    def _process(self, chunk: bytes, t: float) -> None:
        with self._ring_lock:
            self._ring.append(chunk)
        self.stats["chunks"] += 1
        spec = self._read_spec()
        if spec.mode == "paused":                          # the stream is closed; drain whatever is left
            if self._rec is not None or self._utt:
                self._rec = None
                self._active = None
                self._reset_utt()
            self.stats["paused_drops"] += 1
            self._capture_off()
            return
        self._clap_check(chunk, t)
        self._check_version()
        if spec.mode in CAPTURE_MODES and self.capture_ready():
            self._capture_chunk(chunk, t, spec)           # Whisper hears the command / answer (no Vosk)
            return
        if self._cap_state:
            self._capture_off()
        want = (spec.mode, tuple(spec.extra_words))
        if self._rec is None or self._active is None or \
                ((want != self._active or self._rebuild) and not self._partial_busy()):
            self._switch(want)                            # only at an utterance boundary (§4.12 loop 1)
        if not self._quiet(t):                            # echo gate (§5.4 rule 8)
            self._rec.Reset()
            self._fed_s = 0.0
            self._reset_utt()
            self._preroll = b""
            self.stats["gated"] += 1
            return
        idle_utt = self._idle_cut(chunk, t, spec.mode)    # the capture's noise estimate + idle utterances
        if not self._utt:
            self._utt += self._preroll
            self._utt_t0 = t
            self._utt_rec_t0 = self._fed_s - len(self._preroll) / BYTES_PER_S
        self._utt += chunk
        if len(self._utt) > MAX_UTT_BYTES:
            del self._utt[: len(self._utt) - MAX_UTT_BYTES]
        self._utt_t1 = t
        peak = pcm_peak(chunk)
        if peak > self._utt_peak:
            self._utt_peak = peak
        final = self._rec.AcceptWaveform(chunk)
        self._fed_s += len(chunk) / BYTES_PER_S
        mode = self._active[0]
        if final:
            res = json.loads(self._rec.Result())
            pcm, peak, t0, t1, off = bytes(self._utt), self._utt_peak, self._utt_t0, self._utt_t1, self._utt_rec_t0
            self._reset_utt()
            self._preroll = chunk
            self.stats["finals"] += 1
            tr = self._build(res, mode, pcm, peak=peak, t0=t0, t1=t1, word_offset=off)
            if tr is not None and self._wake_words() & set(tr.text.split()) and not self._claim_wake(t0, t1):
                tr = None                                 # Whisper's second opinion already answered this name
            if tr is not None:
                self._emit(tr)
        # the wake chime is off by default (user report: random "click click" from room noise whose *partial*
        # result looked like the name); when on, only for real speech, never for faint noise
        elif mode == "wake" and not self._chimed and self._cfg("chime_on_wake", False) \
                and self._utt_peak >= float(self._cfg("min_speech_peak", 0.02)):
            partial = json.loads(self._rec.PartialResult()).get("partial", "")
            if self._wake_words() & set(partial.split()):
                self._chimed = True                       # first wake partial of this utterance (F5)
                self.stats["chimes"] += 1
                try:
                    self.chime.play("wake")
                except Exception:
                    log.exception("wake chime failed")
        if idle_utt is not None:                          # after Vosk: its own wake result for this chunk wins
            self._whisper_wake(idle_utt)

    def _clap_check(self, chunk: bytes, t: float) -> None:
        """Clap to switch the display (clap.py). Never while Xyrus itself is talking (the same echo gate)."""
        if self.on_clap is None or not self._cfg("clap.enabled", True):
            return
        if self._clap is None:
            from xyrus.clap import ClapDetector
            self._clap = ClapDetector()
        self._clap.configure(min_peak=float(self._cfg("clap.min_peak", 0.12)),
                             claps=int(self._cfg("clap.claps", 3)))
        if not self._quiet(t):
            self._clap.reset()
            return
        if self._clap.feed(chunk, t):
            self.stats["claps"] += 1
            log.info("clap pattern heard - switching the display")
            try:
                self.on_clap()
            except Exception:
                log.exception("on_clap failed")

    def _emit(self, tr: Transcript) -> None:
        self.stats["transcripts"] += 1
        dur = max(0.0, float(getattr(tr, "t_end", 0.0) or 0.0) - float(getattr(tr, "t_start", 0.0) or 0.0))
        log.info("heard %r [%s%s] (%.1f s of audio)%s", tr.text, tr.mode, ", wake re-decode" if tr.wake_redecoded else "",
                 dur, f" free={tr.free_text!r}" if tr.free_text else "")
        try:
            self.on_transcript(tr)
        except Exception:
            log.exception("on_transcript failed")

    # ------------------------------------------------------------------ command capture (lead, Sep 14; capture.py)
    def set_command_hearing(self, hearing: Any) -> None:
        """Plug Whisper for whole commands and question answers: an object with available() -> bool and
        listen(pcm) -> handle | None, where handle.text(language="en", prompt=...) -> str and handle.language()
        -> (lang, prob, [(lang, prob), ...]) (app.CommandHearing wraps hearing.py). While it is available, the
        command / confirm / when / free listen modes record ONE utterance with capture.Endpointer - no time
        limit to start, until the user has finished talking; Vosk isn't fed - and emit a
        capture.CapturedTranscript. The wake word stays on Vosk. None unplugs it (the Vosk grammars as before)."""
        self._command_hearing = hearing

    def capture_ready(self) -> bool:
        """Whisper is plugged and loaded (any thread)."""
        h = self._command_hearing
        if h is None:
            return False
        try:
            return bool(h.available())
        except Exception:
            return False

    def capture_ack(self) -> None:
        """Engine hook (any thread): the captured utterance was handled and the engine's new listen spec is
        published - a new capture may start (one command per call; a question asked again is heard again)."""
        self._await_ack = False

    def _ep(self) -> Endpointer:
        peak = float(self._cfg("min_speech_peak", 0.02))
        if self._endpointer is None:
            self._endpointer = Endpointer(min_peak=peak)
        self._endpointer.min_peak = peak
        return self._endpointer

    def _observe(self, chunk: bytes, t: float) -> None:
        try:
            self._ep().observe(chunk, t)
        except Exception:
            log.exception("endpointer observe failed")

    def _set_cap_state(self, state: str) -> None:
        if state == self._cap_state:
            return
        self._cap_state = state
        fn = self.on_capture_state
        if fn is not None:
            try:
                fn(state)
            except Exception:
                log.exception("on_capture_state failed")

    def _capture_off(self) -> None:
        if self._endpointer is not None and (self._endpointer.in_speech or self._cap_state):
            self._endpointer.reset()
        self._await_ack = False
        self._set_cap_state("")

    def _capture_chunk(self, chunk: bytes, t: float, spec: ListenSpec) -> None:
        if self._rec is not None or self._utt:             # leaving Vosk: its next mode starts fresh
            self._rec = None
            self._active = None
            self._reset_utt()
        ep = self._ep()
        if not self._quiet(t):                             # echo gate: never Xyrus's own voice
            ep.reset()
            self.stats["gated"] += 1
            self._set_cap_state("listening")
            return
        if self._await_ack:                                # the last capture isn't handled yet
            if spec.generation != self._ack_gen or t - self._ack_t > ACK_TIMEOUT_S:
                self._await_ack = False
            else:
                ep.observe(chunk, t)
                return
        utt = ep.feed(chunk, t)
        if utt is None:
            self._set_cap_state("hearing" if ep.in_speech else "listening")
            return
        self._set_cap_state("thinking")
        t0 = time.monotonic()
        try:
            tr = self._hear_command(utt, spec)
        except Exception:
            log.exception("command capture: Whisper failed")
            tr = None
        self.last_timing["capture_decode"] = time.monotonic() - t0
        self.last_timing["capture_endpoint_at"] = t
        if tr is None:
            self.stats["capture_dropped"] += 1
            self._set_cap_state("listening")
            return
        self.stats["captures"] += 1
        self._remember(utt.pcm, tr.t_start, tr.t_end)
        self._await_ack, self._ack_gen, self._ack_t = self.ack_required, spec.generation, t
        self._emit(tr)
        self._set_cap_state("listening")                  # after the transcript: no deadline fires in between

    # ------------------------------------------------------------------ idle Whisper second opinion on the name
    def _idle_cut(self, chunk: bytes, t: float, mode: str) -> Any:
        """Vosk modes: keep the capture's noise estimate warm. Idle ("wake") with Whisper ready: the Endpointer
        also cuts utterances, returned when one has ended (else None) for _whisper_wake."""
        if mode != "wake" or not self._cfg("whisper_wake", True) or not self.capture_ready():
            self._observe(chunk, t)
            return None
        try:
            return self._ep().feed(chunk, t)
        except Exception:
            log.exception("endpointer feed failed")
            return None

    @staticmethod
    def _overlaps(spans: Iterable[tuple[float, float]], t0: float, t1: float) -> bool:
        return any(a < t1 and t0 < b for a, b in spans)

    def _claim_wake(self, t0: float, t1: float) -> bool:
        """Vosk heard the name in [t0, t1] (chunk END times): False when Whisper already answered it, else the
        span is kept so Whisper skips it, and the Endpointer's copy of the same words is dropped."""
        span = (t0 - CHUNK_S - len(self._preroll) / BYTES_PER_S - 0.25, t1)
        if self._overlaps(self._whisper_wakes, *span):
            self.stats["wake_dupes"] += 1
            log.debug("Vosk heard the name Whisper already answered - dropped")
            return False
        self._vosk_wakes.append(span)
        if self._endpointer is not None:
            self._endpointer.reset()
        return True

    def _whisper_wake(self, utt: Any) -> None:
        """A short idle utterance Vosk didn't take as the name: Whisper hears it; when it starts with the name
        ("Xyrus." / "Hey Xyrus, what time is it?") it is emitted like the Vosk wake re-decode would be."""
        if utt.speech_s > WAKE_CHECK_MAX_SPEECH_S or utt.t_end - utt.t_start > WAKE_CHECK_MAX_S:
            self.stats["wake_check_long"] += 1            # talk, not a call: never costs a Whisper decode
            return
        if self._overlaps(self._vosk_wakes, utt.t_start, utt.t_end):
            return                                        # Vosk already heard the name in it
        t0 = time.monotonic()
        try:
            heard = self._command_hearing.listen(utt.pcm)
            raw = str(heard.text(language="en") or "") if heard is not None else ""
        except Exception:
            log.exception("Whisper wake check failed")
            return
        self.stats["wake_checks"] += 1
        self.last_timing["wake_check"] = time.monotonic() - t0
        words = clean_text(raw).split()
        i = 1 if len(words) > 1 and words[0] in ("hey", "hi", "okay", "ok") else 0
        if i >= len(words) or words[i] not in WHISPER_NAMES:
            log.debug("Whisper wake check: %r is not the name", raw)
            return
        rest = strip_wake_words(words[i + 1:])
        wake_tok = next(iter(self._cfg("wake_words", DEFAULT_WAKE)), DEFAULT_WAKE[0])
        self._whisper_wakes.append((utt.t_start, utt.t_end))
        self._remember(utt.pcm, utt.t_start, utt.t_end)
        self.stats["wake_whisper"] += 1
        log.info("heard the name (Whisper second opinion, %.1f s of speech, %.2f s): %r", utt.speech_s,
                 self.last_timing["wake_check"], raw)
        self._emit(Transcript(text=self._norm(" ".join([str(wake_tok).lower()] + rest)), mode="command",
                              source="mic", free_text=" ".join(words) if rest else None, peak=utt.peak,
                              t_start=utt.t_start, t_end=utt.t_end, wake_redecoded=True))

    def _song_question_open(self) -> bool:
        fn = self.song_question
        try:
            return bool(fn is not None and fn())
        except Exception:
            return False

    def _hear_command(self, utt: Any, spec: ListenSpec) -> CapturedTranscript | None:
        """Whisper on one captured utterance: English first; a song request (or the answer to "What should I
        play?") is checked for Bangla on the same encoding and, when it is Bangla, heard again in Bengali."""
        heard = self._command_hearing.listen(utt.pcm)
        if heard is None:
            return None
        raw = str(heard.text(language="en") or "")
        text = clean_text(raw)
        if only_filler(text):
            log.info("command capture: dropped %r (%.1f s of speech) - silence or a Whisper filler", raw,
                     utt.speech_s)
            return None
        song = (spec.mode == "free" and self._song_question_open()) or looks_like_song(text)
        lang = "en"
        if song:
            bn = self._bangla_text(heard)
            if bn is not None:
                raw, text, lang = bn, clean_text(bn), "bn"
        self._last_heard = (utt.t_start, utt.t_end, heard)
        log.info("heard (command capture, %s, %.1f s of speech, peak %.2f, ended by %s): %r", lang, utt.speech_s,
                 utt.peak, utt.reason, raw)
        return CapturedTranscript(text=self._norm(text) if lang == "en" else text, mode=spec.mode, source="mic",
                                  free_text=text, peak=utt.peak, t_start=utt.t_start, t_end=utt.t_end,
                                  language=lang, song_hint=song, raw=raw)

    def _bangla_text(self, heard: Any) -> str | None:
        """Whisper's Bengali-script text when it thinks the utterance is Bangla (or Hindi), else None."""
        try:
            detected = heard.language()
        except Exception:
            log.exception("language detection failed")
            return None
        if not bangla_likely(detected):
            return None
        bn = str(heard.text(language="bn", prompt=None) or "")
        cleaned = clean_text(bn)
        if not cleaned or not has_bengali(cleaned) or only_filler(cleaned):
            return None
        return bn

    def _whisper_retry(self, pcm: bytes) -> str:
        """One-breath commands the grammar missed ("xyrus open …"): Whisper's text, "" when it heard nothing."""
        try:
            heard = self._command_hearing.listen(pcm)
            text = clean_text(heard.text(language="en")) if heard is not None else ""
        except Exception:
            log.exception("Whisper retry failed - using Vosk")
            return ""
        if only_filler(text):
            return ""
        self.stats["whisper_retries"] += 1
        log.info("heard (Whisper retry): %s", text)
        return text

    def request_language_check(self, tr: Transcript, cb: Callable[[Any], None]) -> bool:
        """Engine hook: English Whisper heard nothing Xyrus knows - maybe it was Bangla ("আমার ভিনদেশী তারা
        বাজাও"). The kept utterance is checked again: cb(CapturedTranscript in Bengali script) or cb(None).
        Never blocks the caller while T-rec runs; False when the utterance is gone or Whisper isn't ready."""
        if not getattr(tr, "captured", False) or not self.capture_ready():
            return False
        last = self._last_heard
        heard = last[2] if last is not None and (last[0], last[1]) == (tr.t_start, tr.t_end) else None
        with self._recent_lock:
            pcm = next((p for t0, t1, p in reversed(self._recent) if t0 == tr.t_start and t1 == tr.t_end), None)
        if heard is None and pcm is None:
            return False

        def work() -> None:
            out = None
            try:
                h = heard if heard is not None else self._command_hearing.listen(pcm)
                bn = self._bangla_text(h) if h is not None else None
                if bn is not None:
                    text = clean_text(bn)
                    out = dataclasses.replace(tr, text=text, free_text=text, language="bn", raw=bn)
            except Exception:
                log.exception("Bangla check failed")
            try:
                cb(out)
            except Exception:
                log.exception("Bangla check callback failed")

        th = self._thread
        if th is not None and th.is_alive() and threading.current_thread() is not th:
            self._requests.put((work, threading.Event(), {}))
        else:
            with self._inline_lock:
                work()
        return True

    # ------------------------------------------------------------------ final result -> Transcript (§5.3)
    @staticmethod
    def _words(res: dict, offset: float) -> tuple[Word, ...]:
        out = []
        for w in res.get("result", ()) or ():
            try:
                out.append(Word(str(w["word"]), max(0.0, float(w["start"]) - offset),
                                max(0.0, float(w["end"]) - offset), float(w.get("conf", 0.0))))
            except (KeyError, TypeError, ValueError):
                continue
        return tuple(out)

    def _redecode(self, pcm: bytes, mode: str) -> dict:
        if mode == "free":
            if self._free_aux is None:
                from vosk import KaldiRecognizer
                self._free_aux = KaldiRecognizer(self.load(), SAMPLE_RATE)
                self._free_aux.SetWords(True)
            rec = self._free_aux
            rec.Reset()
        else:
            rec = self._get_rec(self._aux_cache, mode, ())
        rec.AcceptWaveform(pcm)
        res = json.loads(rec.FinalResult())
        rec.Reset()
        return res

    def _free_trigger_sets(self) -> tuple[set, set]:
        if self._triggers is None:
            registry = getattr(self.grammar, "registry", None)
            triggers: set[tuple[str, ...]] = set()
            app_triggers: set[tuple[str, ...]] = set()
            try:
                for t in registry.free_triggers():
                    trig = tuple(t.lower().split()) if isinstance(t, str) else tuple(str(x).lower() for x in t)
                    if trig:
                        triggers.add(trig)
            except Exception:
                log.exception("registry.free_triggers failed")
            try:
                for cmd in registry.commands():
                    for p in cmd.patterns:
                        if getattr(p, "free_slot", None) == "app":
                            app_triggers |= literal_prefixes(p.text)
            except Exception:
                log.debug("registry.commands unavailable for app triggers")
            self._triggers = (triggers, app_triggers)
        return self._triggers

    def wants_free(self, toks: list[str]) -> bool:
        """§5.3 step 2: after the wake token comes a free trigger + >= 1 token, or open|launch|start + a tail
        containing [unk]."""
        wake = self._wake_words()
        rest = list(toks)
        for i, tk in enumerate(toks):
            if tk in wake:
                rest = list(toks[i + 1:])
                break
        triggers, app_triggers = self._free_trigger_sets()
        for s in range(min(TRIGGER_WINDOW, len(rest))):
            head = rest[s:]
            if head[0] in PLAY_LIKE and len(head) > 1:
                return True
            if head[0] in OPEN_WORDS:
                if UNK in head[1:]:
                    return True
                continue
            # SPEC-AMBIGUITY: §5.3 names only open|launch|start, but §4.6 makes every <app> slot free "only when
            # the grammar tail has [unk]" - so "close/switch to/kill <app>" with an [unk] tail are re-decoded too
            # (their prefixes come from registry.commands(); registry.free_triggers() leaves app slots out).
            for trig in app_triggers:
                n = len(trig)
                if tuple(head[:n]) == trig and UNK in head[n:]:
                    return True
            for trig in triggers:
                n = len(trig)
                if trig not in app_triggers and tuple(head[:n]) == trig and len(head) > n:
                    return True
        return False

    def _free_text_trigger(self, toks: list[str], window: int = TRIGGER_WINDOW) -> tuple[int, int] | None:
        """(index in toks, length) of the free-TEXT trigger (a play-like token, or a non-<app> free trigger,
        longest first) followed by >= 1 more token; None when there is none. `window` = how many tokens after
        the wake may precede it."""
        wake = self._wake_words()
        base = 0
        for i, tk in enumerate(toks):
            if tk in wake:
                base = i + 1
                break
        rest = list(toks[base:])
        triggers, app_triggers = self._free_trigger_sets()
        ordered = sorted((t for t in triggers if t not in app_triggers), key=lambda t: (-len(t), t))
        for s in range(min(max(1, window), len(rest))):
            head = rest[s:]
            if head[0] in PLAY_LIKE and len(head) > 1:
                return base + s, 1
            if head[0] in OPEN_WORDS:
                continue
            for trig in ordered:
                n = len(trig)
                if tuple(head[:n]) == trig and len(head) > n:
                    return base + s, n
        return None

    def wants_free_text(self, toks: list[str], window: int = TRIGGER_WINDOW) -> bool:
        """wants_free() for a free TEXT slot only (song / query / text / calendar, to-do, note and memory adds).
        <app> names are never free text (they are matched against a restricted grammar)."""
        return self._free_text_trigger(toks, window) is not None

    def _anchor(self, text: str, toks: list[str], window: int = TRIGGER_WINDOW) -> str:
        """A plugged decoder may return only the free part ('ishwar by vikings'), but the registry takes the slot
        from the words after the command's trigger - so when the text lacks the trigger, the grammar's own words
        up to it ('cyrus play') go back in front. Whisper's usual full-utterance text is returned unchanged."""
        hit = self._free_text_trigger(toks, window)
        if hit is None or not text:
            return text
        start, n = hit
        trig = toks[start]
        probe = ({trig} | set(PLAY_LIKE)) if trig in PLAY_LIKE else {trig}
        words = text.split()
        if any(w in probe for w in words[: n + 3]):
            return text
        head = [t for t in toks[: start + n] if t != UNK]
        return " ".join(head + words)

    # ------------------------------------------------------------------ pluggable free-text hearing (T6)
    def set_free_decoder(self, fn: Callable[[bytes], str | None] | None) -> None:
        """Plug a better decoder for free TEXT only - song titles, "what should I play?" answers, search
        queries, calendar titles and notes (the app plugs Whisper from D:\\arc\\hearing.py). fn(pcm) gets 16 kHz
        mono int16 PCM and returns the text; None, "" or an exception falls back to the Vosk unrestricted
        decode. The wake word, commands and <app> names always stay on Vosk (fast). fn runs on T-rec (inline
        in decode_pcm / request_free_decode when T-rec isn't running). None unplugs it."""
        self._free_decoder = fn

    @staticmethod
    def clean_free(text: Any) -> str:
        """'Xyrus, play Ishwar by Vikings.' -> 'xyrus play ishwar by vikings' (same rule as hearing.clean)."""
        return " ".join(re.sub(r"[^\w\s']", " ", str(text or "").lower()).split())

    def _plugged(self, pcm: bytes) -> str:
        """The plugged decoder's cleaned text, or "" (not plugged / nothing heard / it failed)."""
        fn = self._free_decoder
        if fn is None or not pcm:
            return ""
        t = time.monotonic()
        try:
            text = self.clean_free(fn(pcm))
        except Exception:
            log.exception("free-text decoder failed - using Vosk")
            text = ""
        self.last_timing["plugged"] = time.monotonic() - t
        if text:
            self.stats["plugged_free"] += 1
            log.info("heard (free-text decoder, %.1f s): %s", self.last_timing["plugged"], text)
        else:
            self.stats["plugged_fallback"] += 1
        return text

    def _vosk_free(self, pcm: bytes) -> str:
        t = time.monotonic()
        free = self._redecode(pcm, "free")
        self.last_timing["free"] = time.monotonic() - t
        self.stats["free_redecodes"] += 1
        return str(free.get("text", "")).strip().lower()

    def _worth_plugging(self, raw: str, peak: float) -> bool:
        """Free mode: skip the (slow) plugged decoder for what the engine drops anyway - near-silence and a lone
        noise word - so Whisper never runs (or hallucinates) on room noise."""
        words = [w for w in raw.split() if w != UNK]
        if not words or (len(words) == 1 and words[0] in NOISE_WORDS):
            return False
        return peak >= float(self._cfg("min_speech_peak", 0.02))

    # ------------------------------------------------------------------ <app> re-decode (lead, round 2)
    def set_app_index(self, index: Any) -> None:
        """Start-Menu names for the <app> grammar (anything with names() -> list[str])."""
        self._app_index = index

    def _app_names(self) -> list[str]:
        names = [str(k).lower() for k in (self._cfg("apps", {}) or {})]
        index = self._app_index
        if index is not None:
            try:
                names += [str(n).lower() for n in index.names()]
            except Exception:
                log.exception("app index names() failed")
        return list(dict.fromkeys(n for n in names if n.strip()))

    def app_split(self, toks: list[str]) -> tuple[list[str], list[str]] | None:
        """(tokens up to and including an <app> trigger, the tail after it) or None. Triggers = the literal
        prefixes of every registry pattern whose free slot is `app`, plus open|launch|start."""
        wake = self._wake_words()
        start = 0
        for i, tk in enumerate(toks):
            if tk in wake:
                start = i + 1
                break
        rest = list(toks[start:])
        _, app_triggers = self._free_trigger_sets()
        triggers = sorted(set(app_triggers) | {(w,) for w in OPEN_WORDS}, key=len, reverse=True)
        for s in range(min(TRIGGER_WINDOW, len(rest))):
            head = rest[s:]
            for trig in triggers:
                n = len(trig)
                if tuple(head[:n]) == trig and len(head) > n:
                    return list(toks[:start + s + n]), list(head[n:])
        return None

    @staticmethod
    def _match_app(tail: list[str], names: list[str], phrase_to_name: dict[str, str]) -> str | None:
        words = [t for t in tail if t != UNK and t not in APP_FILLER]
        if not words:
            return None
        q = " ".join(words)
        if q in names:
            return q
        if q in phrase_to_name:
            return phrase_to_name[q]
        pool = list(dict.fromkeys(list(names) + list(phrase_to_name)))
        close = difflib.get_close_matches(q, pool, n=1, cutoff=APP_MATCH_CUTOFF)
        if close:
            return close[0] if close[0] in names else phrase_to_name[close[0]]
        return None

    def _app_redecode(self, pcm: bytes, toks: list[str]) -> str | None:
        """Re-decode an <app> command with a grammar restricted to known app names (config apps + Start Menu),
        fuzzy-match the tail and return "<prefix> <app name>" for Transcript.free_text; None if nothing matched."""
        split = self.app_split(toks)
        names = self._app_names()
        if split is None or not names:
            return None
        _, app_triggers = self._free_trigger_sets()
        prefix_words = {w for trig in app_triggers for w in trig} | set(OPEN_WORDS) | set(APP_FILLER)
        t = time.monotonic()
        grammar, phrase_to_name = self.grammar.app_grammar(names, prefix_words)
        rec = self._get_rec(self._aux_cache, "app", (), words=grammar)
        rec.AcceptWaveform(pcm)
        res = json.loads(rec.FinalResult())
        rec.Reset()
        self.last_timing["app"] = time.monotonic() - t
        self.stats["app_redecodes"] += 1
        heard = str(res.get("text", "")).strip().lower().split()
        heard_split = self.app_split(heard)
        wake = self._wake_words()
        tail = heard_split[1] if heard_split else [w for w in heard if w not in wake and w not in prefix_words]
        name = self._match_app(tail, names, phrase_to_name)
        log.debug("app re-decode heard %r -> %r", " ".join(heard), name)
        if name is None:
            return None
        return " ".join(split[0] + name.split())

    def _free_for_command(self, pcm: bytes, toks: list[str]) -> str | None:
        split = self.app_split(toks)
        if split is not None and UNK in split[1]:
            app_text = self._app_redecode(pcm, toks)
            if app_text:
                return app_text
        if self.wants_free(toks):                              # unrestricted (also the <app> fallback)
            if self.wants_free_text(toks):                     # song / query / calendar text: plugged first
                plugged = self._plugged(pcm)
                if plugged:
                    return self._anchor(plugged, toks)
            return self._vosk_free(pcm) or None
        return None

    def _build(self, res: dict, mode: str, pcm: bytes, *, peak: float, t0: float, t1: float,
               keep_empty: bool = False, word_offset: float = 0.0) -> Transcript | None:
        raw = str(res.get("text", "")).strip().lower()
        words = self._words(res, word_offset)
        if mode == "free":                                     # §5.3 step 3
            if not raw and not keep_empty:
                self.stats["empty"] += 1
                return None
            free = raw
            if raw and self._free_decoder is not None and self._worth_plugging(raw, peak):
                free = self._plugged(pcm) or raw               # free mode = free text (answers, titles)
            self._remember(pcm, t0, t1)
            return Transcript(text=self._norm(free) if free else "", mode="free", source="mic",
                              free_text=free, words=words, peak=peak, t_start=t0, t_end=t1)
        text = self._norm(raw) if raw else ""
        toks = text.split()
        if not keep_empty and (not toks or all(tk == UNK for tk in toks)):
            self.stats["empty"] += 1                           # §5.4 rule 3
            log.debug("dropped empty result %r (%s)", raw, mode)
            return None
        wake = self._wake_words()
        out_mode, redecoded, free_text = mode, False, None
        if mode == "wake" and wake & set(toks):                # §5.3 step 1 - wake re-decode
            t = time.monotonic()
            cmd = self._redecode(pcm, "command")
            self.last_timing["command"] = time.monotonic() - t
            self.stats["wake_redecodes"] += 1
            cmd_text = self._norm(str(cmd.get("text", "")).strip().lower())
            if wake & set(cmd_text.split()):
                text, toks, out_mode, redecoded = cmd_text, cmd_text.split(), "command", True
                words = self._words(cmd, 0.0)
        if out_mode == "command":                              # §5.3 step 2 - free re-decode
            free_text = self._free_for_command(pcm, toks)
        self._remember(pcm, t0, t1)
        return Transcript(text=text, mode=out_mode, source="mic", free_text=free_text, words=words,
                          peak=peak, t_start=t0, t_end=t1, wake_redecoded=redecoded)
