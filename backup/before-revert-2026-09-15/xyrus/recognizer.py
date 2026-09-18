"""T-rec: the recognizer thread - the only Vosk user (SPEC §4.12, §5.2-§5.4, D1, D2, G5).

Per chunk: listen spec -> grammar switch at an utterance boundary -> echo gate -> utterance PCM buffer
(20 s cap, 0.25 s pre-roll; 3 s while a song plays) -> AcceptWaveform -> wake chime on the partial -> on a
final: normalise, wake re-decode with the command grammar, free re-decode with the unrestricted recognizer
(§5.3) -> on_transcript(Transcript). `decode_pcm` runs the same final-result code on a whole utterance (tests,
--wav).

Hearing rebuild (Sep 15 2026) - measured at the user's seat: speech peaks 0.03-0.05 over a room floor of rms
~0.0033. The small Vosk wake grammar heard a faint "xyrus" 1/6 where Whisper heard it 6/6, so Whisper is the
wake authority: every short idle utterance (Silero VAD, xyrus.vad) gets a Whisper name check, and doubtful Vosk
wakes (long, [unk]-padded, faint, low word confidence) are confirmed by Whisper before Xyrus answers. A name
said together with a command ("Xyrus, take a screenshot") is handed to engine.on_wake_with_command whole. Every
re-decode works on the speech span (<= 6 s), never on the 20 s buffer.
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

try:                                                      # capture helper (hearing rebuild); local copy until then
    from xyrus.capture import repeated_tokens
except ImportError:                                       # pragma: no cover - older capture.py
    def repeated_tokens(text: str) -> bool:
        words = clean_text(text).split()
        if len(words) < 3:
            return False
        n = Counter(words).most_common(1)[0][1]
        return n >= 3 and n >= 0.6 * len(words)

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
# Idle Whisper second opinion on the name (lead, Sep 14; widened Sep 15): live, a voice from across the room
# said "xyrus" 3 times; the Vosk wake grammar heard nothing all 3 times, Whisper wrote "Xyrus." all 3. The old
# 2.5 s speech gate skipped one-breath requests ("Xyrus, play Shape of You by Ed Sheeran" is ~3 s of speech),
# so a call may now carry a command; talk longer than this still never costs a Whisper decode.
WAKE_CHECK_MAX_SPEECH_S = 5.0
WAKE_CHECK_MAX_S = 7.0
WAKE_MIN_SPEECH_S = 0.15                  # the idle name check's Endpointer: a clipped faint "xyrus" is ~0.2 s
WHISPER_NAMES = frozenset({"xyrus", "cyrus", "sirus", "zyrus", "xirus", "cirus", "sairus", "cyrius",
                           "xyris", "cyris", "zirus", "zyras", "zyris", "xiris", "zyra's", "xyrus's"})
# An English word Whisper also writes for the name: only when it is sure of it (a cirrus cloud).
# "serious" / "sirius" are NOT the name (wake bench, Sep 15): Whisper writes the spoken words "serious" and
# "sirius" as "Serious." / "Sirius." at lp -0.05..-0.21 - more sure than of a faint real "xyrus" ("Zyrus.",
# lp -0.47..-0.56) - so no logprob gate separates them; 6/9 false wakes each with them accepted.
WEAK_NAMES = frozenset({"cirrus"})
# What Whisper writes for a FAINT name at the start of a one-breath request ("Sirens play Shape of You by Ed
# Sheeran." at peak 0.035 - wake bench, Sep 15 2026): a call only when Vosk heard the name in the same breath, or
# when a request follows it ("Sirens are loud" / "Sirens." stay words)
CALL_NAMES = frozenset({"sirens", "siren"})
REQUEST_STARTERS = frozenset({"play", "open", "close", "what", "what's", "whats", "set", "turn", "take", "tell",
                              "search", "show", "mute", "unmute", "volume", "can", "could", "would", "please",
                              "start", "stop", "pause", "resume", "next", "previous", "lock", "shut", "put",
                              "listen", "find", "how", "is", "switch", "minimize", "maximize", "go", "make"})
WEAK_NAME_LP = -0.3
WAKE_LEADERS = frozenset({"a", "hey", "hi", "okay", "ok", "hayes"})
# avg_logprob separates noise (-0.5 .. -1.9) from speech (>= -0.35) on this room's clips (measured Sep 15)
WAKE_LP_MIN = -0.9
WAKE_COMMAND_LP = -0.7                    # a command heard with the name runs only when Whisper is this sure
# a Vosk grammar wake Whisper double-checks (config wake_confirm): long, padded, faint or unsure
CONFIRM_LONG_S = 3.0
CONFIRM_PEAK = 0.08
WEAK_DOUBTS = frozenset({"faint", "name lost"})
# ... and only when Whisper itself was unsure: the lost calls read "Hi, Ross." (lp -0.89), "Sorry, Ross." (-0.37),
# "Seroths." (-0.80); a confidently heard other word ("Serious." -0.05..-0.21) still drops the wake
KEEP_WHISPER_LP = -0.3


def _strong_doubt(why: str) -> bool:
    """A reason Whisper's "that's not the name" may act on (a sound-alike from a video, [unk], extra words, a long
    or low-confidence decode) - not only "faint" / "name lost", which every call from the user's seat has."""
    return any(r and r not in WEAK_DOUBTS for r in why.split(", "))
VOSK_CONF_MIN = 0.6                       # 'white undo top match' -> "undo" came with a mean word conf of 0.3
VOSK_UNK_MAX = 0.3
LATE_FINAL_S = 4.0                        # a Vosk final about audio this old no longer owns the Endpointer's utterance
REDECODE_MAX_S = 6.0                      # every re-decode: the speech span (+-0.3 s), at most this long
REDECODE_PAD_S = 0.3
PLAYBACK_UTT_S = 3.0                      # music keeps Vosk's utterance open 14 s: cut idle utterances while it plays
PLAYBACK_UTT_BYTES = int(PLAYBACK_UTT_S * BYTES_PER_S)
ACK_BUFFER_S = 2.0                        # speech that starts while the engine handles a capture is kept this long
ACK_TAIL_S = 0.5                          # ... and only when it is still going on (a peak in the last 0.5 s)
STATS_EVERY_S = 600.0
# the Bangla check on a song request: Whisper unsure of its English, a temperature fallback, or Bangla detected
BN_TRIGGER_LP = -0.30
SHORT_FAINT_S = 0.4                       # "Go to sleep" from a 0.5 s peak-0.03 clip suspended the PC
SHORT_FAINT_PEAK = 0.1
# confirm / when captures expect an answer: the filler / loop / short-faint drops apply to command and free only
ANSWER_MODES = frozenset({"confirm", "when"})
YES_NO_ANSWERS = frozenset({"yes", "yeah", "yep", "yup", "okay", "ok", "sure", "no", "nope", "nah", "cancel",
                            "stop"})
_PROMPT_CMD = "Xyrus, open Chrome. What time is it? Play a song."    # hearing.PROMPT_CMD when importable
_LOGPROB_DROP = -0.9                                                 # hearing.LOGPROB_DROP when importable
_BN_LETTER = re.compile(r"[\u0985-\u09B9\u09DC-\u09DF]")             # a Bengali vowel or consonant, not a sign
STAT_KEYS = ("wake_checks", "wake_check_long", "wake_whisper", "whisper_retries", "captures", "capture_dropped",
             "wake_confirms", "wake_rejected", "wake_with_command", "playback_flushes", "bn_decodes",
             "arm_timeouts")


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


def last_seconds(pcm: bytes, seconds: float = REDECODE_MAX_S) -> bytes:
    """The last `seconds` of int16 PCM (a re-decode never gets the whole 20 s buffer)."""
    n = int(seconds * SAMPLE_RATE) * 2
    return pcm[-n:] if len(pcm) > n else pcm


def trim_to_words(pcm: bytes, words: tuple[Word, ...] | list[Word]) -> tuple[bytes, float]:
    """(PCM of the words' span +-REDECODE_PAD_S - at most REDECODE_MAX_S from the first word -, its start in
    seconds from the start of `pcm`). No word times: the last REDECODE_MAX_S seconds."""
    n = len(pcm)
    cap = int(REDECODE_MAX_S * SAMPLE_RATE) * 2
    if words:
        a = max(0, int(max(0.0, words[0].start - REDECODE_PAD_S) * SAMPLE_RATE) * 2)
        b = min(n, int((words[-1].end + REDECODE_PAD_S) * SAMPLE_RATE) * 2)
        if b - a >= 2 and a < n:
            b = min(b, a + cap)
            return pcm[a:b], a / BYTES_PER_S
    a = max(0, n - cap)
    return pcm[a:], a / BYTES_PER_S


def _hearing_module(hearing: Any) -> Any:
    """The hearing.py module behind the plugged command hearing (app.CommandHearing.mod), else the importable
    top-level module, else None."""
    mod = getattr(hearing, "mod", None)
    if mod is not None:
        return mod
    try:
        import hearing as H                                 # top-level module next to arc.py
        return H
    except Exception:                                       # noqa: BLE001 - absent / mid-edit / DLLs
        return None


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
        # the object on_transcript belongs to (engine.on_capture_timeout / on_wake_with_command are looked up on
        # it); None = the owner of a bound on_transcript (app.py passes engine.submit_transcript)
        self.engine: Any = None
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
        self.drop_reasons: Counter = Counter()   # command-capture drops by reason
        self._stats_t: float | None = None
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
        self._utt_start = 0.0                    # audio time of the first byte of self._utt
        self._chimed = False
        self._fed_s = 0.0                        # seconds fed to self._rec since its last reset
        self._utt_rec_t0 = 0.0                   # recognizer time of the utterance buffer start
        # command capture (lead, Sep 14; capture.py): after the wake reply ONE utterance is recorded until the user
        # has finished and Whisper hears it. Hooks wired by app.py; None / unset = the Vosk grammars as before.
        self._command_hearing: Any = None
        self.on_capture_state: Callable[[str], None] | None = None   # engine.set_capture_state
        self.song_question: Callable[[], bool] | None = None          # engine.song_question_open
        self.ack_required = False                # True once engine.on_capture_handled -> capture_ack is wired
        self._endpointer: Endpointer | None = None                    # the command capture's
        self._ep_key: Any = None
        self._idle_endpointer: Endpointer | None = None               # the idle name check's
        self._idle_key: Any = None
        self._idle_gated = False                 # the echo gate closed on the idle path (one reset when it opens)
        self._cap_gated = False                  # ... on the capture path
        self._cap_state = ""
        self._await_ack = False
        self._ack_gen = -1
        self._ack_t = 0.0
        self._ack_buf: deque[tuple[bytes, float]] = deque()
        self._arm_key: Any = None                # (mode, generation) the arm clock runs for
        self._arm_t0 = 0.0
        self._arm_heard = False
        self._arm_expired: Any = None            # a capture spec that timed out: listen for the name instead
        self._deferred = False                   # the last doubtful Vosk wake was left to the idle Whisper check
        self._deferred_at: float | None = None      # where Vosk heard that name (audio time)
        self._deferred_weak = False                 # its doubts were only "faint" / "name lost"
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
        # a retry re-decodes this: never more than the speech span (+ its padding)
        pcm = last_seconds(pcm, REDECODE_MAX_S + 2 * REDECODE_PAD_S)
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
        self._maybe_log_stats(t)
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
        key = (spec.mode, spec.generation)
        if self._arm_expired is not None and self._arm_expired != key:
            self._arm_expired = None                       # the engine moved on: capture as usual again
        if spec.mode in CAPTURE_MODES and self.capture_ready() and self._arm_expired is None:
            self._capture_chunk(chunk, t, spec)           # Whisper hears the command / answer (no Vosk)
            return
        if self._cap_state or self._await_ack or self._ack_buf:
            self._capture_off()
        mode_now = "wake" if self._arm_expired is not None else spec.mode
        want = (mode_now, tuple(spec.extra_words))
        if self._rec is None or self._active is None or \
                ((want != self._active or self._rebuild) and not self._partial_busy()):
            self._switch(want)                            # only at an utterance boundary (§4.12 loop 1)
        playback = bool(getattr(spec, "playback", False)) and self._active[0] == "wake"
        if not self._quiet(t):                            # echo gate (§5.4 rule 8)
            self._rec.Reset()
            self._fed_s = 0.0
            self._reset_utt()
            self._preroll = b""
            self.stats["gated"] += 1
            self._gate_observe(chunk, t, mode_now, playback)
            return
        idle = self._idle_cut(chunk, t, mode_now, playback)   # the capture's noise estimate + idle utterances
        if not self._utt:
            self._utt += self._preroll
            self._utt_t0 = t
            self._utt_rec_t0 = self._fed_s - len(self._preroll) / BYTES_PER_S
            self._utt_start = t - len(chunk) / BYTES_PER_S - len(self._preroll) / BYTES_PER_S
        self._utt += chunk
        if len(self._utt) > MAX_UTT_BYTES:
            cut = len(self._utt) - MAX_UTT_BYTES
            del self._utt[:cut]
            self._utt_rec_t0 += cut / BYTES_PER_S         # word times stay relative to the buffer's first byte
            self._utt_start += cut / BYTES_PER_S
        self._utt_t1 = t
        peak = pcm_peak(chunk)
        if peak > self._utt_peak:
            self._utt_peak = peak
        final = self._rec.AcceptWaveform(chunk)
        self._fed_s += len(chunk) / BYTES_PER_S
        mode = self._active[0]
        res = json.loads(self._rec.Result()) if final else None
        if res is None and playback and (len(self._utt) >= PLAYBACK_UTT_BYTES or idle is not None):
            # a song plays: Vosk's own endpointing keeps the utterance open (14 s measured) - flush at 3 s or at
            # the VAD's end, and start the next one fresh
            res = json.loads(self._rec.FinalResult())
            self._rec.Reset()
            self._fed_s = 0.0
            self.stats["playback_flushes"] += 1
        if res is not None:
            self._vosk_final(res, mode, chunk, t)
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
        if idle is not None:                              # after Vosk: its own wake result for this chunk wins
            self._whisper_wake(*idle)

    def _vosk_final(self, res: dict, mode: str, chunk: bytes, t: float) -> None:
        """One Vosk final: the speech span is cut out of the buffer (word times), the name is claimed, and the
        result built / confirmed / emitted."""
        pcm, peak, t0, t1, off = bytes(self._utt), self._utt_peak, self._utt_t0, self._utt_t1, self._utt_rec_t0
        start = self._utt_start
        self._reset_utt()
        self._preroll = chunk
        self.stats["finals"] += 1
        words = self._words(res, off)
        span_pcm, cut_s = trim_to_words(pcm, words)
        raw = self._norm(str(res.get("text", "")).strip().lower())
        woke = bool(self._wake_words() & set(raw.split()))
        speech = (start + words[0].start, start + words[-1].end) if words else None
        span = self._wake_span(t0, t1, speech)
        if woke and self._overlaps(self._whisper_wakes, *span):
            self.stats["wake_dupes"] += 1
            log.debug("Vosk heard the name Whisper already answered - dropped")
            return
        self._deferred = False
        tr = self._build(res, mode, span_pcm, peak=peak, t0=t0, t1=t1, word_offset=off + cut_s, live=True)
        if self._deferred:
            self._deferred_at = speech[0] if speech is not None else t0
        if woke and not self._deferred:
            self._register_wake(span, speech[1] if speech is not None else t1, t)
        if tr is not None:
            self._emit(tr)

    def _maybe_log_stats(self, t: float) -> None:
        if self._stats_t is None:
            self._stats_t = t
        elif t - self._stats_t >= STATS_EVERY_S:
            self._stats_t = t
            log.info("recognizer stats: %s", self.stats_summary())

    def stats_summary(self) -> dict:
        """The counters the 10-minute INFO line carries (tuning the hearing against real use)."""
        out: dict[str, Any] = {k: int(self.stats.get(k, 0)) for k in STAT_KEYS}
        out["capture_dropped_by_reason"] = dict(self.drop_reasons)
        return out

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

    def _engine_hook(self, name: str) -> Callable[..., Any] | None:
        """engine.<name> (interfaces.ON_CAPTURE_TIMEOUT / ON_WAKE_WITH_COMMAND) or None when it has none."""
        eng = self.engine if self.engine is not None else getattr(self.on_transcript, "__self__", None)
        fn = getattr(eng, name, None) if eng is not None else None
        return fn if callable(fn) else None

    # ------------------------------------------------------------------ command capture (lead, Sep 14; capture.py)
    def set_command_hearing(self, hearing: Any) -> None:
        """Plug Whisper for whole commands and question answers: an object with available() -> bool and
        listen(pcm) -> handle | None, where handle.text(language="en", prompt=..., min_logprob=None) -> str,
        handle.text_nbest(language, prompt, n), handle.avg_logprob / fallback_used and handle.language()
        (app.CommandHearing wraps hearing.py). While it is available, the command / confirm / when / free listen
        modes record ONE utterance with capture.Endpointer - no time limit to start (unless arm_timeout_s),
        until the user has finished talking; Vosk isn't fed - and emit a capture.CapturedTranscript. The wake
        word stays on Vosk, with Whisper as its authority. None unplugs it (the Vosk grammars as before)."""
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

    def _use_vad(self) -> bool:
        return str(self._cfg("vad", "silero")).strip().lower() != "energy"

    @staticmethod
    def _new_endpointer(**kw: Any) -> Endpointer:
        try:
            return Endpointer(**kw)
        except TypeError:                                  # an Endpointer without the rebuild's keywords
            return Endpointer(min_peak=kw.get("min_peak", 0.02))

    def _ep(self) -> Endpointer:
        peak = float(self._cfg("min_speech_peak", 0.02))
        key = self._use_vad()
        if self._endpointer is None or self._ep_key != key:
            self._endpointer = self._new_endpointer(min_peak=peak, use_vad=key)
            self._ep_key = key
        self._endpointer.min_peak = peak
        return self._endpointer

    def _idle_ep(self, playback: bool) -> Endpointer:
        """The idle name check's Endpointer: shorter minimum speech (a faint clipped "xyrus"), and while a song
        plays a 3 s cap (music never ends an utterance by silence)."""
        peak = float(self._cfg("min_speech_peak", 0.02))
        key = (bool(playback), self._use_vad())
        if self._idle_endpointer is None or self._idle_key != key:
            kw: dict[str, Any] = dict(min_peak=peak, min_speech_s=WAKE_MIN_SPEECH_S, use_vad=key[1])
            if playback:
                kw["max_s"] = PLAYBACK_UTT_S
            self._idle_endpointer = self._new_endpointer(**kw)
            self._idle_key = key
        self._idle_endpointer.min_peak = peak
        return self._idle_endpointer

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
        self._ack_buf.clear()
        self._cap_gated = False
        self._arm_key = None
        self._set_cap_state("")

    @staticmethod
    def _slice(ep: Any, pcm: bytes) -> bytes:
        """ep.speech_slice(pcm): the VAD span +-0.3 s. On the energy path there is no span (speech_slice would
        give the last 6 s and cut a long request's start): the Endpointer's utterance is decoded whole."""
        if getattr(ep, "vad", "silero") == "energy":
            return pcm
        fn = getattr(ep, "speech_slice", None)
        if fn is not None:
            try:
                return fn(pcm) or last_seconds(pcm)
            except Exception:
                log.exception("speech_slice failed")
        return last_seconds(pcm)

    def _capture_chunk(self, chunk: bytes, t: float, spec: ListenSpec) -> None:
        if self._rec is not None or self._utt:             # leaving Vosk: its next mode starts fresh
            self._rec = None
            self._active = None
            self._reset_utt()
        ep = self._ep()
        key = (spec.mode, spec.generation)
        if key != self._arm_key:                           # a (re-)armed spec restarts the arm clock
            self._arm_key, self._arm_t0, self._arm_heard = key, t, False
        if not self._quiet(t):                             # echo gate: never Xyrus's own voice
            try:
                ep.observe(chunk, t)                       # keeps the noise estimate and the VAD state going
            except Exception:
                log.exception("endpointer observe failed")
            self._cap_gated = True
            self._arm_t0 = t                               # the clock starts when Xyrus has finished talking
            self.stats["gated"] += 1
            self._set_cap_state("listening")
            return
        if self._cap_gated:                                # one reset when the gate opens
            self._cap_gated = False
            ep.reset()
        if self._await_ack:                                # the last capture isn't handled yet
            if spec.generation != self._ack_gen or t - self._ack_t > ACK_TIMEOUT_S:
                self._await_ack = False
            else:
                self._ack_hold(chunk, t, ep)
                return
        pending = self._ack_release(ep)
        pending.append((chunk, t))
        captured = False
        for c, tc in pending:
            if captured and self._await_ack:               # one capture per ack: the rest waits again
                self._ack_hold(c, tc, ep)
                continue
            utt = ep.feed(c, tc)
            if ep.in_speech or utt is not None:
                self._arm_heard = True
            if utt is not None:
                captured = True
                self._captured(utt, tc, spec, ep)
        if not captured:
            self._set_cap_state("hearing" if ep.in_speech else "listening")
        self._arm_check(t, ep)

    def _ack_hold(self, chunk: bytes, t: float, ep: Endpointer) -> None:
        """While the engine handles the last capture: keep the last ACK_BUFFER_S of audio (the user may start
        the next answer already); older audio only teaches the noise estimate."""
        self._ack_buf.append((chunk, t))
        limit = ACK_BUFFER_S * BYTES_PER_S
        while sum(len(c) for c, _ in self._ack_buf) > limit:
            old, t_old = self._ack_buf.popleft()
            try:
                ep.observe(old, t_old)
            except Exception:
                log.exception("endpointer observe failed")

    def _ack_release(self, ep: Endpointer) -> list[tuple[bytes, float]]:
        """The held audio to feed first - only when speech is still going on at its end (a peak in the last
        ACK_TAIL_S); an utterance that already ended there was said to nobody."""
        if not self._ack_buf:
            return []
        held = list(self._ack_buf)
        self._ack_buf.clear()
        tail = b"".join(c for c, _ in held)[-int(ACK_TAIL_S * SAMPLE_RATE) * 2:]
        if pcm_peak(tail) >= float(self._cfg("min_speech_peak", 0.02)):
            self.stats["ack_buffered"] += 1
            return held
        for c, tc in held:
            try:
                ep.observe(c, tc)
            except Exception:
                log.exception("endpointer observe failed")
        return []

    def _arm_check(self, t: float, ep: Endpointer) -> None:
        """arm_timeout_s > 0 (0, the shipped default, = no limit - the user's choice): a capture spec with no
        speech onset within that much audio time goes back to waiting for the name, silently."""
        try:
            limit = float(self._cfg("arm_timeout_s", 0.0) or 0.0)
        except (TypeError, ValueError):
            limit = 0.0
        if limit <= 0 or self._arm_heard or self._await_ack or ep.in_speech or self._arm_key is None:
            return
        if t - self._arm_t0 < limit:
            return
        self._arm_expired = self._arm_key
        self.stats["arm_timeouts"] += 1
        log.info("command capture: no speech in %.0f s - back to waiting for the name", t - self._arm_t0)
        ep.reset()
        self._arm_key = None
        self._set_cap_state("")
        fn = self._engine_hook("on_capture_timeout")
        if fn is not None:
            try:
                fn()
            except Exception:
                log.exception("on_capture_timeout failed")

    def _captured(self, utt: Any, t: float, spec: ListenSpec, ep: Endpointer) -> None:
        self._set_cap_state("thinking")
        pcm = self._slice(ep, utt.pcm)
        t0 = time.monotonic()
        try:
            tr = self._hear_command(utt, spec, pcm)
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
        self._remember(pcm, tr.t_start, tr.t_end)
        self._await_ack, self._ack_gen, self._ack_t = self.ack_required, spec.generation, t
        self._emit(tr)
        self._set_cap_state("listening")                  # after the transcript: no deadline fires in between

    # ------------------------------------------------------------------ idle: Whisper is the wake authority
    def _idle_cut(self, chunk: bytes, t: float, mode: str, playback: bool) -> tuple[Any, Endpointer] | None:
        """Vosk modes: keep the capture's noise estimate warm. Idle ("wake") with Whisper ready: the idle
        Endpointer also cuts utterances, returned when one has ended (else None) for _whisper_wake."""
        self._observe(chunk, t)
        if mode != "wake" or not self._cfg("whisper_wake", True) or not self.capture_ready():
            return None
        try:
            ep = self._idle_ep(playback)
            if self._idle_gated:                           # one reset when the echo gate opens
                self._idle_gated = False
                ep.reset()
            utt = ep.feed(chunk, t)
        except Exception:
            log.exception("endpointer feed failed")
            return None
        return (utt, ep) if utt is not None else None

    def _gate_observe(self, chunk: bytes, t: float, mode: str, playback: bool) -> None:
        """The echo gate is closed (Xyrus talks): the Endpointers only learn the room - never a reset per chunk,
        which threw the noise estimate and the VAD's context away 4 times a second."""
        self._observe(chunk, t)
        if mode == "wake" and self._command_hearing is not None:
            try:
                self._idle_ep(playback).observe(chunk, t)
                self._idle_gated = True
            except Exception:
                log.exception("endpointer observe failed")

    @staticmethod
    def _overlaps(spans: Iterable[tuple[float, float]], t0: float, t1: float) -> bool:
        return any(a < t1 and t0 < b for a, b in spans)

    def _claim_wake(self, t0: float, t1: float, speech: tuple[float, float] | None = None,
                    now: float | None = None) -> bool:
        """Vosk heard the name in [t0, t1] (chunk END times; `speech` = the words' own span when known): False
        when Whisper already answered it, else the span is kept so Whisper skips it, and the idle Endpointer's
        copy of the same words is dropped - unless the final is about audio older than LATE_FINAL_S (Vosk
        closed a long utterance late: what the Endpointer holds now is newer speech)."""
        span = self._wake_span(t0, t1, speech)
        if self._overlaps(self._whisper_wakes, *span):
            self.stats["wake_dupes"] += 1
            log.debug("Vosk heard the name Whisper already answered - dropped")
            return False
        self._register_wake(span, speech[1] if speech is not None else t1, now)
        return True

    def _wake_span(self, t0: float, t1: float, speech: tuple[float, float] | None) -> tuple[float, float]:
        if speech is not None:
            return speech[0] - 0.25, speech[1] + 0.25
        return t0 - CHUNK_S - len(self._preroll) / BYTES_PER_S - 0.25, t1

    def _register_wake(self, span: tuple[float, float], end: float, now: float | None) -> None:
        """The Vosk wake was answered (or rejected by Whisper): the idle check skips its span."""
        late = now is not None and now - end > LATE_FINAL_S
        if not late and now is not None:
            # the Endpointer restarts on the faint tail of the same breath ("... screenshot"): an idle utterance
            # starting up to 0.5 s after this final belongs to the wake already answered
            span = (span[0], max(span[1], now + 0.5))
        self._vosk_wakes.append(span)
        if late:
            self.stats["late_finals"] += 1
            log.debug("Vosk final about audio %.1f s old - the Endpointer keeps its utterance", now - end)
            return True
        self._fresh_endpointers()
        return True

    def _fresh_endpointers(self) -> None:
        """The name was heard: the idle Endpointer drops its copy of it, and the command capture that follows
        starts without the name in its pre-roll."""
        for ep in (self._idle_endpointer, self._endpointer):
            if ep is not None:
                try:
                    ep.reset()
                except Exception:
                    log.exception("endpointer reset failed")

    # ---- Whisper name check (shared by the idle check and the Vosk wake confirmation)
    def _prompt_cmd(self) -> str:
        mod = _hearing_module(self._command_hearing)
        return str(getattr(mod, "PROMPT_CMD", _PROMPT_CMD) or _PROMPT_CMD)

    def _logprob_drop(self) -> float:
        mod = _hearing_module(self._command_hearing)
        try:
            return float(getattr(mod, "LOGPROB_DROP", _LOGPROB_DROP))
        except (TypeError, ValueError):
            return _LOGPROB_DROP

    @staticmethod
    def _heard_text(heard: Any, language: str, prompt: Any, min_logprob: float | None = None) -> str:
        """heard.text(...) - also for handles without min_logprob / prompt keywords (older hearing, fakes)."""
        try:
            if min_logprob is not None:
                return str(heard.text(language=language, prompt=prompt, min_logprob=min_logprob) or "")
            return str(heard.text(language=language, prompt=prompt) or "")
        except TypeError:
            try:
                return str(heard.text(language=language, prompt=prompt) or "")
            except TypeError:
                return str(heard.text(language=language) or "")

    @staticmethod
    def _lp(heard: Any) -> float | None:
        v = getattr(heard, "avg_logprob", None)
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    def _whisper_name_check(self, pcm: bytes, handed: bool = False) -> tuple[bool, list[str], str, float | None] | None:
        """Whisper on a short span: (it starts with the name, the words after the name, Whisper's text,
        avg_logprob). None when Whisper could not decode it (not loaded / failed). `handed`: Vosk heard the name
        in this breath too (a CALL_NAMES reading then counts)."""
        try:
            heard = self._command_hearing.listen(pcm)
            if heard is None:
                return None
            raw = self._heard_text(heard, "en", self._prompt_cmd())
        except Exception:
            log.exception("Whisper wake check failed")
            return None
        self.stats["wake_checks"] += 1
        lp = self._lp(heard)
        words = clean_text(raw).split()
        i = 1 if len(words) > 1 and words[0] in WAKE_LEADERS else 0
        name = words[i] if i < len(words) else ""
        reason = ""
        if not name:
            reason = "nothing"
        elif name not in WHISPER_NAMES and name not in WEAK_NAMES and name not in CALL_NAMES:
            reason = "not the name"
        elif name in CALL_NAMES and not (handed or (len(words) > i + 2 and words[i + 1] in REQUEST_STARTERS)):
            reason = "a word, not a call"
        elif name in WEAK_NAMES and (lp is None or lp < WEAK_NAME_LP):
            reason = "an English word, not sure enough"
        elif lp is not None and lp < WAKE_LP_MIN:
            reason = "low logprob"
        if reason:
            log.info("Whisper wake check: rejected %r lp=%s (%s)", raw, "?" if lp is None else f"{lp:.2f}", reason)
            return False, [], raw, lp
        return True, words[i + 1:], raw, lp

    def _deliver_wake(self, after: list[str], lp: float | None, raw: str, peak: float, t0: float,
                      t1: float) -> Transcript | None:
        """Whisper heard the name: with a command after it (>= 2 words, lp >= WAKE_COMMAND_LP) the engine gets
        on_wake_with_command(rest, tr) - None is returned; an engine without that hook gets the name + command
        as one transcript (the one-breath path). Otherwise (nothing / [unk] / unsure) the bare wake."""
        rest = [w for w in strip_wake_words(list(after)) if w != UNK]
        wake_tok = str(next(iter(self._cfg("wake_words", DEFAULT_WAKE)), DEFAULT_WAKE[0])).lower()
        words = clean_text(raw).split()
        if len(rest) >= 2 and (lp is None or lp >= WAKE_COMMAND_LP):
            rest_text = " ".join(rest)
            tr = Transcript(text=self._norm(" ".join([wake_tok] + rest)), mode="command", source="mic",
                            free_text=" ".join(words), peak=peak, t_start=t0, t_end=t1, wake_redecoded=True)
            fn = self._engine_hook("on_wake_with_command")
            if fn is None:
                return tr
            self.stats["wake_with_command"] += 1
            self.stats["transcripts"] += 1
            log.info("heard the name with a command (%s): %r", "lp=?" if lp is None else f"lp={lp:.2f}", rest_text)
            try:
                fn(rest_text, tr)
                return None
            except Exception:
                log.exception("on_wake_with_command failed - the one-breath transcript instead")
                return tr
        if rest:
            log.info("the name heard; %r not run (%d words, lp=%s) - just the wake", " ".join(rest), len(rest),
                     "?" if lp is None else f"{lp:.2f}")
        return Transcript(text=self._norm(wake_tok), mode="command", source="mic", peak=peak, t_start=t0,
                          t_end=t1, wake_redecoded=True)

    def _whisper_wake(self, utt: Any, ep: Any = None) -> None:
        """A short idle utterance Vosk didn't take as the name: Whisper hears its speech span; when it starts
        with the name ("Xyrus." / "Hey Xyrus, what time is it?") the wake (or wake + command) is delivered."""
        at, self._deferred_at = self._deferred_at, None
        handed = at is not None and utt.t_start - 0.5 <= at <= utt.t_end
        weak_handed, self._deferred_weak = handed and self._deferred_weak, False
        if utt.speech_s > WAKE_CHECK_MAX_SPEECH_S or utt.t_end - utt.t_start > WAKE_CHECK_MAX_S:
            if not handed:
                self.stats["wake_check_long"] += 1        # talk, not a call: never costs a Whisper decode
                return
            # a doubtful Vosk wake was handed to this check, but the breath began earlier (TV, another
            # sentence without a pause): hear it from the name on, never drop it (review, Sep 15)
            off = max(0, int((at - REDECODE_PAD_S - utt.t_start) * SAMPLE_RATE)) * 2
            pcm = utt.pcm[off:off + int(WAKE_CHECK_MAX_S * SAMPLE_RATE) * 2]
            self.stats["wake_check_handed_long"] += 1
        elif self._overlaps(self._vosk_wakes, utt.t_start, utt.t_end):
            return                                        # Vosk already heard the name in it
        else:
            pcm = self._slice(ep, utt.pcm) if ep is not None else last_seconds(utt.pcm)
        t0 = time.monotonic()
        got = self._whisper_name_check(pcm, handed=handed)
        self.last_timing["wake_check"] = time.monotonic() - t0
        if got is None or not got[0]:
            unsure = got is None or got[3] is None or got[3] < KEEP_WHISPER_LP
            if weak_handed and unsure:                    # Vosk heard the real name; Whisper only misspelled it
                self.stats["wake_kept"] += 1
                log.info("handed-off wake: Whisper wrote %r - the name was clear to Vosk, kept",
                         got[2] if got else None)
                self._whisper_wakes.append((utt.t_start, utt.t_end))
                tr = self._deliver_wake([], None, got[2] if got else "", utt.peak, utt.t_start, utt.t_end)
                if tr is not None:
                    self._emit(tr)
            return
        _, after, raw, lp = got
        self._whisper_wakes.append((utt.t_start, utt.t_end))
        if self._endpointer is not None:
            self._endpointer.reset()                      # the command capture starts without the name
        self._remember(pcm, utt.t_start, utt.t_end)
        self.stats["wake_whisper"] += 1
        log.info("heard the name (Whisper second opinion, %.1f s of speech, %.2f s, lp=%s): %r", utt.speech_s,
                 self.last_timing["wake_check"], "?" if lp is None else f"{lp:.2f}", raw)
        tr = self._deliver_wake(after, lp, raw, utt.peak, utt.t_start, utt.t_end)
        if tr is not None:
            self._emit(tr)

    def _wake_doubt(self, wake_words: tuple[Word, ...], cmd: dict, redecoded: bool, peak: float) -> str:
        """Why a Vosk grammar wake needs Whisper's confirmation ("" = a short clean wake, answered at once)."""
        why = []
        if wake_words and wake_words[-1].end - wake_words[0].start > CONFIRM_LONG_S:
            why.append("long")
        # the grammar's extra name forms (zeros / virus / cirrus) are English words too: "zeros" said is "zeros"
        primary = str(next(iter(self._cfg("wake_words", DEFAULT_WAKE)), DEFAULT_WAKE[0])).lower()
        heard_names = {w.text for w in wake_words} & self._wake_words()
        if heard_names and primary not in heard_names:
            why.append("sound-alike")
        # "faint" and "name lost" are WEAK reasons (WEAK_DOUBTS): the user's own voice at the seat peaks 0.03-0.05
        # (always "faint") and the command re-decode often drops the name. Whisper still hears such a wake (its
        # command text is better), but its "no" never drops it - it misspelled the name ("Hi, Ross.", "Sorry,
        # Ross.") and real calls were lost (Sep 15 2026). See _strong_doubt.
        weak = []
        if peak < CONFIRM_PEAK:
            weak.append("faint")
        toks = self._norm(str(cmd.get("text", "")).strip().lower()).split()
        wake = self._wake_words()
        if not redecoded:
            weak.append("name lost")
        if UNK in toks:
            why.append("[unk]")
        names = [i for i, tk in enumerate(toks) if tk in wake]
        if names and (names[0] > 0 or len(names) > 1):
            why.append("extra words")
        confs = [float(w.get("conf", 1.0)) for w in (cmd.get("result") or ()) if isinstance(w, dict)]
        if confs and sum(confs) / len(confs) < VOSK_CONF_MIN:
            why.append("low conf")
        if toks and sum(1 for tk in toks if tk == UNK) / len(toks) > VOSK_UNK_MAX:
            why.append("mostly [unk]")
        return ", ".join(dict.fromkeys(why + weak))

    def _song_question_open(self) -> bool:
        fn = self.song_question
        try:
            return bool(fn is not None and fn())
        except Exception:
            return False

    # ------------------------------------------------------------------ Whisper on a captured command
    def _drop(self, reason: str, raw: str, lp: float | None, vad_s: float) -> None:
        self.drop_reasons[reason] += 1
        log.info("command capture: dropped %s lp=%.2f vad=%.1fs %r", reason,
                 float("nan") if lp is None else lp, vad_s, raw)

    def _hear_command(self, utt: Any, spec: ListenSpec, pcm: bytes | None = None) -> CapturedTranscript | None:
        """Whisper on one captured utterance (its speech span): the text with its confidence and n-best
        alternates; silence, fillers, loops, prompt echoes and short faint blips are dropped silently. A song
        request Whisper was unsure of (or that sounds Bangla) is also heard in Bengali on the same encoding -
        and by the Bengali specialist when that decode is unusable."""
        pcm = utt.pcm if pcm is None else pcm
        heard = self._command_hearing.listen(pcm)
        if heard is None:
            return None
        prompt = self._prompt_cmd()
        enforce = bool(self._cfg("logprob_enforce", False))
        raw = self._heard_text(heard, "en", prompt, min_logprob=self._logprob_drop() if enforce else None)
        text = clean_text(raw)
        confidence = self._lp(heard)
        fallback = bool(getattr(heard, "fallback_used", False))
        vad_s = float(getattr(utt, "vad_speech_s", 0.0) or 0.0)
        speech_s = vad_s if vad_s > 0 else float(getattr(utt, "speech_s", 0.0) or 0.0)   # energy path: no VAD
        reason = ""
        answer_mode = spec.mode in ANSWER_MODES
        if not text:
            reason = "empty"
        elif answer_mode:
            # a yes/no or time answer is expected: "Yeah." / "Okay." / "No." are answers here, not Whisper's
            # filler (the dialogue decides what it means); only the command / free drops are skipped
            if text in YES_NO_ANSWERS:
                self.stats["answers_kept"] += 1
        elif repeated_tokens(text):
            reason = "repeated"
        elif only_filler(text, prompt):
            reason = "filler"
        elif speech_s < SHORT_FAINT_S and float(utt.peak) < SHORT_FAINT_PEAK:
            reason = "short_faint"
        if reason:
            self._drop(reason, raw, confidence, vad_s)
            return None
        alternates = self._alternates(heard, raw, prompt)
        song = (spec.mode == "free" and self._song_question_open()) or looks_like_song(text) \
            or bool(self._bare_title(text))
        bn_text = None
        lang_box: list = []

        def detected():                                   # heard.language() once per utterance
            if not lang_box:
                try:
                    lang_box.append(heard.language())
                except Exception:
                    log.exception("language detection failed")
                    lang_box.append(None)
            return lang_box[0]
        if song and self._bangla_wanted(heard, confidence, fallback, detected):
            bn_text = self._bn_decode(heard, pcm, confidence, len(text.split()))
        # Whisper's language detection says Bangla: the request IS Bangla - route it in Bengali script (English
        # Whisper translates "আমার ভিনদেশী তারা" into "my foreign star", a useless search). Only the weaker
        # evidence (an unsure English decode) keeps the English text with the Bengali one as a hint.
        said_bangla = bool(bn_text) and bangla_likely(detected())
        self._last_heard = (utt.t_start, utt.t_end, heard)
        log.info("heard (command capture, %s, %.1f s of speech, peak %.2f, ended by %s) lp=%.2f fb=%d vad=%.1fs "
                 "alts=%d%s: %r", "song" if song else "en", utt.speech_s, utt.peak, utt.reason,
                 float("nan") if confidence is None else confidence, int(fallback), vad_s, len(alternates),
                 f" bn={bn_text!r}" if bn_text else "", raw)
        if said_bangla:
            bn_clean = clean_text(bn_text)
            base = dict(text=bn_clean, mode=spec.mode, source="mic", free_text=bn_clean, peak=utt.peak,
                        t_start=utt.t_start, t_end=utt.t_end, language="bn", song_hint=True, raw=bn_text)
        else:
            base = dict(text=self._norm(text), mode=spec.mode, source="mic", free_text=text, peak=utt.peak,
                        t_start=utt.t_start, t_end=utt.t_end, language="en", song_hint=song, raw=raw)
        try:
            return CapturedTranscript(**base, confidence=confidence, alternates=alternates, bn_text=bn_text,
                                      vad_speech_s=vad_s, fallback_used=fallback)
        except TypeError:                                  # a CapturedTranscript without the rebuild's fields
            return CapturedTranscript(**base)

    @staticmethod
    def _alternates(heard: Any, raw: str, prompt: str) -> tuple[str, ...]:
        """Whisper's other readings with the same prompt as text() (song lookups and "did you mean" try them):
        deduped against the kept text by their cleaned words - text_nbest's hypothesis 0 is not always text()'s
        decode - and never a filler / loop / prompt echo (nothing downstream filters them)."""
        fn = getattr(heard, "text_nbest", None)
        if fn is None:
            return ()
        try:
            try:
                hyps = fn("en", prompt, n=5) or []
            except TypeError:
                hyps = fn() or []
        except Exception:
            log.exception("n-best decode failed")
            return ()
        seen = {clean_text(raw)}
        out = []
        for hyp in hyps:
            t = str(hyp[0] if isinstance(hyp, (tuple, list)) else hyp).strip()
            key = clean_text(t)
            if not key or key in seen or only_filler(t, prompt) or repeated_tokens(t):
                continue
            seen.add(key)
            out.append(t)
        return tuple(out)

    @staticmethod
    def _bare_title(text: str) -> str | None:
        try:
            from xyrus.normalize import bare_title
        except ImportError:
            return None
        try:
            return bare_title(text)
        except Exception:
            log.exception("bare_title failed")
            return None

    @staticmethod
    def _bangla_wanted(heard: Any, confidence: float | None, fallback: bool, detected: Any = None) -> bool:
        """Evidence the song may be Bangla: Whisper unsure of its English, a temperature fallback, or its
        language detection (asked last: the cheap evidence first). `detected`: a cached heard.language()."""
        if (confidence is not None and confidence < BN_TRIGGER_LP) or fallback:
            return True
        try:
            return bangla_likely(detected() if detected is not None else heard.language())
        except Exception:
            log.exception("language detection failed")
            return False

    @staticmethod
    def _bn_ok(bn: str | None, bn_lp: float | None, en_lp: float | None, en_tokens: int | None) -> bool:
        """A Bengali decode worth using: real Bengali letters, no loop ('কে স্র স্র স্র', 'াাাাাাাা'), not
        Latin ('PLAY ISHVAR BY VIKINGS'), and banglish.bn_acceptable (absent -> rejected)."""
        cleaned = clean_text(bn or "")
        if not cleaned or not has_bengali(cleaned) or not _BN_LETTER.search(cleaned):
            return False
        if repeated_tokens(cleaned) or only_filler(cleaned):
            return False
        try:
            from xyrus.banglish import bn_acceptable
        except ImportError:
            return False
        try:
            return bool(bn_acceptable(bn, bn_lp, en_lp, en_tokens))
        except Exception:
            log.exception("bn_acceptable failed")
            return False

    @classmethod
    def _bn_primary(cls, heard: Any) -> tuple[str, float | None]:
        """(Bengali text, its avg_logprob) from the primary model's encoding: n-best hypothesis 0."""
        fn = getattr(heard, "text_nbest", None)
        if fn is not None:
            try:
                hyps = fn("bn", prompt=None, n=1) or []
                if hyps:
                    text, lp = hyps[0][0], hyps[0][1]
                    return str(text or ""), (float(lp) if lp is not None else None)
                return "", None
            except TypeError:
                pass
        text = cls._heard_text(heard, "bn", None)
        return text, cls._lp(heard)

    def _bn_decode(self, heard: Any, pcm: bytes | None, en_lp: float | None, en_tokens: int | None) -> str | None:
        """The first acceptable Bengali reading: the primary model's, else the Bengali specialist's."""
        try:
            bn, bn_lp = self._bn_primary(heard)
        except Exception:
            log.exception("Bengali decode failed")
            bn, bn_lp = "", None
        self.stats["bn_decodes"] += 1
        if self._bn_ok(bn, bn_lp, en_lp, en_tokens):
            return bn.strip()
        log.info("Bengali decode rejected: %r lp=%s", bn, "?" if bn_lp is None else f"{bn_lp:.2f}")
        if pcm is None or not self._hearing_call("bn_available", False):
            return None
        try:
            special = self._hearing_call("listen_bn", None, pcm)
            if special is None:
                return None
            bn2 = self._heard_text(special, "bn", None)
            lp2 = self._lp(special)
        except Exception:
            log.exception("Bengali specialist failed")
            return None
        self.stats["bn_specialist"] += 1
        if self._bn_ok(bn2, lp2, en_lp, en_tokens):
            return bn2.strip()
        log.info("Bengali specialist rejected: %r lp=%s", bn2, "?" if lp2 is None else f"{lp2:.2f}")
        return None

    def _hearing_call(self, name: str, default: Any, *args: Any) -> Any:
        """hearing.<name>(*args) on the plugged hearing, else on the hearing module behind it; default when
        neither has it or it fails."""
        h = self._command_hearing
        fn = getattr(h, name, None)
        if fn is None:
            fn = getattr(getattr(h, "mod", None), name, None)
        if not callable(fn):
            return default
        try:
            return fn(*args)
        except Exception:
            log.exception("hearing.%s failed", name)
            return default

    def _bangla_text(self, heard: Any, pcm: bytes | None = None) -> str | None:
        """Whisper's Bengali-script text when it thinks the utterance is Bangla (or Hindi) and the decode is
        acceptable (noise never becomes 'াাাাাাাা'), else None."""
        try:
            detected = heard.language()
        except Exception:
            log.exception("language detection failed")
            return None
        if not bangla_likely(detected):
            return None
        return self._bn_decode(heard, pcm, self._lp(heard), None)

    def _whisper_retry(self, pcm: bytes) -> str:
        """One-breath commands the grammar missed ("xyrus open …"): Whisper's text, "" when it heard nothing."""
        pcm = last_seconds(pcm, REDECODE_MAX_S + 2 * REDECODE_PAD_S)
        try:
            heard = self._command_hearing.listen(pcm)
            text = clean_text(self._heard_text(heard, "en", self._prompt_cmd())) if heard is not None else ""
        except Exception:
            log.exception("Whisper retry failed - using Vosk")
            return ""
        if only_filler(text, self._prompt_cmd()) or repeated_tokens(text):
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
                bn = self._bangla_text(h, pcm) if h is not None else None
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
        pcm = last_seconds(pcm, REDECODE_MAX_S + 2 * REDECODE_PAD_S)
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
        pcm = last_seconds(pcm, REDECODE_MAX_S + 2 * REDECODE_PAD_S)
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
               keep_empty: bool = False, word_offset: float = 0.0, live: bool = False) -> Transcript | None:
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
            # Whisper confirms a doubtful grammar wake before Xyrus answers (hearing rebuild): Vosk's small wake
            # grammar maps faint speech and room noise onto the name, and its command grammar ran "undo" from
            # 'white undo top match' - the grammar's words are never executed then, Whisper's are
            why = self._wake_doubt(words, cmd, redecoded, peak) \
                if live and self.capture_ready() and self._cfg("wake_confirm", True) else ""
            idle_ep = self._idle_endpointer
            if why and idle_ep is not None and idle_ep.in_speech and self._cfg("whisper_wake", True):
                # Vosk closed its utterance at a pause inside the breath ("play shape of you | by ed sheeran"):
                # the idle check hears the whole breath when the Endpointer ends it (<= 1.3 s after the talking)
                self._deferred = True
                self._deferred_weak = not _strong_doubt(why)
                self.stats["wake_deferred"] += 1
                log.info("Vosk wake %r (%s): the breath goes on - Whisper hears it whole at its end",
                         cmd_text or text, why)
                return None
            if why:
                self.stats["wake_confirms"] += 1
                t = time.monotonic()
                got = self._whisper_name_check(pcm, handed=True)            # Vosk heard the name here
                self.last_timing["wake_confirm"] = time.monotonic() - t
                if got is not None:
                    ok, after, wraw, lp = got
                    if not ok and (_strong_doubt(why) or (lp is not None and lp >= KEEP_WHISPER_LP)):
                        self.stats["wake_rejected"] += 1
                        log.info("Vosk wake %r (%s) not confirmed by Whisper: %r", cmd_text or text, why, wraw)
                        return None
                    if not ok:
                        self.stats["wake_kept"] += 1
                        log.info("Vosk wake %r (%s): Whisper wrote %r - the name was clear to Vosk, kept",
                                 cmd_text or text, why, wraw)
                if got is not None and got[0]:
                    ok, after, wraw, lp = got
                    self._whisper_wakes.append((t0, t1))
                    self._remember(pcm, t0, t1)
                    log.info("Vosk wake %r (%s) confirmed by Whisper: %r", cmd_text or text, why, wraw)
                    return self._deliver_wake(after, lp, wraw, peak, t0, t1)
                if got is None:
                    log.info("Vosk wake (%s) unconfirmed - Whisper couldn't decode; the grammar result stands", why)
            if redecoded:
                words = self._words(cmd, 0.0)
        if out_mode == "command":                              # §5.3 step 2 - free re-decode
            free_text = self._free_for_command(pcm, toks)
        self._remember(pcm, t0, t1)
        return Transcript(text=text, mode=out_mode, source="mic", free_text=free_text, words=words,
                          peak=peak, t_start=t0, t_end=t1, wake_redecoded=redecoded)
