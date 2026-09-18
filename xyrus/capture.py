"""Command capture (lead request, Sep 14 2026; hearing rebuild Sep 15): after "Waiting for your command, sir."
the recognizer records ONE utterance - no time limit to start, until the user has finished talking - and Whisper
hears it.

- Endpointer: two endpointers behind one interface.
  * Silero VAD (use_vad=True, the default; xyrus.vad): one speech probability per 32 ms window (512 samples),
    LSTM state carried across chunks. Speech starts when p >= 0.5 on 2 of the last 3 windows and the sound
    reached min_peak; a window counts as speech (vad_speech_s) at p >= 0.35; the utterance ends when <= 8 % of
    the last END_SILENCE_S (1.3 s) is speech. Measured live Sep 15: at the user's seat speech peaks at only
    0.03-0.05 over a room floor of rms ~0.0033, where the energy onset never fired (23/38 captured at 0.03);
    Silero finds the phrase in 6 ms and nothing on the hiss.
  * Energy (use_vad=False, or when Silero is unavailable / "vad": "energy"): 20 ms frames, a 100 ms smoothed
    level over the running noise floor and ceiling (20th / 90th percentile of the last 2 s). The level is
    measured on a notched copy (2nd-order biquad notch at 273 Hz: half the room floor is a 273 Hz whine) - the
    PCM that goes out is always the raw mic signal. A 280 Hz high-pass was tried first and measured WORSE than
    no filter (it barely dents a 273 Hz tone but strips the voice's low end): at peak 0.03 over a hiss+whine
    floor it captured 18/38 vs 22/38 unfiltered, the notch 31/38 (tests/test_capture.py FaintBench).
  Both: 0.4 s pre-roll, 30 s hard cap, a 0.3 s tail after the last speech frame; an utterance needs
  >= MIN_SPEECH_S (0.25 s) of speech and a real peak or it is dropped as noise. Vosk's own endpointing is not
  used (in this room it keeps utterances open 13-19 s).
- CapturedTranscript: what the recognizer emits for such an utterance (a Transcript subclass, so every
  consumer keeps working). The engine routes it as a command with the wake word already given. confidence /
  alternates / bn_text / vad_speech_s / fallback_used carry what the hearing rebuild learned about it.
- clean_text / only_filler / repeated_tokens / looks_like_song / strip_wake_words: Whisper text -> words
  (Bengali-safe: re's \\w misses Bengali vowel signs), the hallucination staples Whisper produces on noise
  ("Thank you.", "you", "Bye. Bye. Bye. Bye.", the initial prompt echoed back), and the request shapes.

No Vosk, no Whisper, no threads; the VAD session (xyrus.vad) is the only native piece, and it is optional.
"""
from __future__ import annotations

import logging
import re
from collections import Counter, deque
from dataclasses import dataclass

from xyrus.interfaces import Transcript
from xyrus.normalize import ARTICLES, FILLER_WORDS, MEANING_WORDS, PLAY_LIKE, WAKE_LIKE, canonical

log = logging.getLogger("xyrus.capture")

RATE = 16000
FRAME = 320                       # 20 ms of int16 mono (energy path)
FRAME_S = FRAME / RATE
VAD_FRAME = 512                   # 32 ms: Silero's window (xyrus.vad.WINDOW)
PREROLL_S = 0.4                   # 0.3 clipped the first consonant of faint onsets the VAD hears a window late
END_SILENCE_S = 1.3               # the user pauses inside a request ("can you please … play …"); 0.9 s cut it
TAIL_S = 0.3                      # silence kept after the last speech frame (Whisper hallucinates on long tails)
MAX_S = 30.0
MIN_SPEECH_S = 0.25               # measured on vad_speech_s with the VAD ("volume up" is ~0.4 s)
ENERGY_SMEAR_S = 0.05             # added on the energy path (0.3 s there, as before the rebuild)
SMOOTH_FRAMES = 5                 # the level is the RMS of the last 100 ms (single frames are too spiky)
NOISE_WINDOW_S = 2.0              # noise statistics over the last 2 s outside speech
FLOOR_PCT, CEIL_PCT = 20, 90
# Measured live Sep 14 (room floor rms ~0.0033): a voice from across the room reaches the mic only ~7 dB over
# the floor. The old 10 dB onset never started, and with no time limit Xyrus waited deaf forever after the
# wake reply. 20 s of plain room noise started nothing at these values; 1.8 / 1.4 did.
ONSET_OVER_FLOOR = 2.0            # speech starts ~6 dB over the floor ...
ONSET_OVER_CEIL = 1.5             # ... and ~3.5 dB over the loudest usual noise
VOICE_OVER_FLOOR = 1.6            # a frame counts as speech (MIN_SPEECH_S) a little lower: the quiet ends
VOICE_OVER_CEIL = 1.2             #   of words are speech too
MIN_ONSET_RMS = 0.0015            # 0.003 blocked a peak-0.03 voice once the whine no longer lifted the level
MIN_HISTORY_FRAMES = 10           # noise history before anything may start (0.2 s energy / 0.32 s VAD)
ONSET_FRAMES, ONSET_OF = 3, 4     # 3 loud frames out of the last 4 start an utterance (60 ms)
END_OVER_CEIL = 1.3
END_REL = 0.15                    # ~ -16 dB below the utterance's own mean speech level
END_BUMP_FRACTION = 0.08          # an utterance ends when <= 8 % of the last END_SILENCE_S is loud: a chair creak
                                  # or a noise bump no longer restarts the wait (unbroken silence never came)
MIN_FLOOR = 1e-4
NOTCH_F0, NOTCH_Q = 273.0, 2.0   # Hz; the room whine the energy level ignores (Q 2: ~140 Hz wide, short ringing)
# Silero (xyrus.vad) - probabilities per 32 ms window
VAD_ONSET_P = 0.35                # onset: p >= this on VAD_ONSET_N of the last VAD_ONSET_OF windows - live
                                  # Sep 15: faint phrases at the user's seat score only 0.45-0.76 (0.5 lost half)
VAD_ONSET_N, VAD_ONSET_OF = 2, 3
VAD_SPEECH_P = 0.35               # ... a window counts as speech at this (the soft ends of words)
SLICE_PAD_S = 0.3                 # speech_slice(): the VAD span padded by this on both sides
SLICE_FALLBACK_S = 6.0            # speech_slice() without a VAD span: the last 6 s

CAPTURE_MODES = frozenset({"command", "confirm", "when", "free"})

# Whisper's staples on noise / silence / a cough. An utterance made only of these is dropped, never a command.
# (Sep 15: "okay" / "yeah" are on the list at the lead's request - Whisper writes them on breath noise.)
HALLUCINATIONS = frozenset({
    "you", "thank you", "thanks", "thank you very much", "thanks for watching", "thank you for watching",
    "thanks for watching and see you next time", "please subscribe", "subscribe", "bye", "bye bye",
    "oh", "uh", "um", "hmm", "mm", "huh", "ah", "so", "the", "i", "a", "and", "music", "applause", "laughter",
    "silence", "foreign", "ধন্যবাদ",
    "okay", "ok", "yeah", "you're welcome", "see you", "goodbye", "subtitles by", "amen", "thank you so much",
})
_CLEAN_RE = re.compile(r"[^\w\s'ঀ-৿]")
# The prompt Whisper used until Sep 15 leaked from short blips ("I'm going to call the doctor tomorrow at
# five." from 0.6 s of noise). hearing.prompt_echo() is the real guard; this is the local copy for when hearing
# isn't importable (tests without Whisper) - only the doctor sentence, never the song one (a real request).
_LEGACY_PROMPT_MARKS = ("call the doctor tomorrow", "the doctor tomorrow at five")
_LEGACY_PROMPT = "xyrus play shape of you by ed sheeran remind me to call the doctor tomorrow at five"


@dataclass(frozen=True)
class CapturedTranscript(Transcript):
    """A Transcript heard by the command capture (Whisper), not by the Vosk grammar."""
    captured: bool = True
    language: str = "en"             # "en", or "bn" (Bengali script in text/free_text)
    song_hint: bool = False          # the English pass heard a play request / the song question was open
    raw: str = ""                    # Whisper's own text (with case and punctuation)
    confidence: float | None = None  # Whisper's avg_logprob of the kept text (None: unknown)
    alternates: tuple[str, ...] = () # other n-best readings (song lookups try them)
    bn_text: str | None = None       # the Bengali specialist's text for the same audio, when it ran
    vad_speech_s: float = 0.0        # seconds of Silero speech windows in the utterance (0 on the energy path)
    fallback_used: bool = False      # Whisper needed a temperature fallback (a hint that it guessed)


def clean_text(text: str) -> str:
    """'Xyrus, open Chrome.' -> 'xyrus open chrome'; Bengali words stay whole ('আমার ভিনদেশী তারা')."""
    return " ".join(_CLEAN_RE.sub(" ", str(text or "").lower()).split())


def repeated_tokens(text: str) -> bool:
    """One token said >= 3 times making >= 60 % of the utterance ('Bye. Bye. Bye. Bye.'): Whisper looping on
    a blip, not a request."""
    words = clean_text(text).split()
    if len(words) < 3:
        return False
    _tok, n = Counter(words).most_common(1)[0]
    return n >= 3 and n >= 0.6 * len(words)


def _prompt_echo(text: str, prompt: str | None) -> bool:
    """hearing.prompt_echo() when hearing is importable (it knows the prompts), plus the local legacy guard."""
    try:
        import hearing as H                                 # top-level module next to arc.py
        fn = getattr(H, "prompt_echo", None)
        if fn is not None and fn(text, prompt):
            return True
    except Exception:                                       # noqa: BLE001 - absent / mid-edit / signature
        pass
    if text == _LEGACY_PROMPT:
        return True
    return any(f" {mark} " in f" {text} " for mark in _LEGACY_PROMPT_MARKS)


def only_filler(text: str, prompt: str | None = None) -> bool:
    """Nothing but Whisper's hallucination staples (or nothing at all), a looping token, or the initial prompt
    read back - never a command."""
    t = clean_text(text)
    if not t:
        return True
    if t in HALLUCINATIONS or t.startswith("subtitles by"):
        return True
    words = t.split()
    if len(words) <= 3 and all(w in HALLUCINATIONS for w in words):
        return True
    if repeated_tokens(t):
        return True
    return _prompt_echo(t, prompt)


NAME_FORMS = WAKE_LIKE | {"zyrus", "xirus", "sirus", "cyprus", "cirus", "sairus", "virus", "zeros", "cirrus"}
# Name forms that are also English words: "zero the volume", "serious question, what time is it?" keep them.
# They are the name only when alone or followed by the shape of a request.
ENGLISH_NAMES = frozenset({"zero", "serious", "virus"})
_NOT_REQUEST = ARTICLES | frozenset({
    "a", "an", "it", "is", "its", "it's", "of", "to", "for", "and", "are", "be", "so", "like", "i", "me", "in",
    "on", "at", "by", "this", "that's", "there's", "u", "time", "day", "date", "last", "back", "mode", "percent",
    "some", "very", "again", "right", "now", "real", "really", "id", "i'd", "maybe", "actually", "you're",
    "gonna", "wanna", "then", "ahead", "quickly", "sir", "you", "off", "down", "up", "hi", "hello"})
REQUEST_WORDS = frozenset(
    (MEANING_WORDS | PLAY_LIKE | {
        "flip", "roll", "tell", "show", "take", "minimize", "minimise", "maximize", "maximise", "restore", "snap",
        "type", "press", "read", "remember", "what", "what's", "who", "who's", "how", "where", "when", "why", "help",
        "repeat", "status", "battery", "disk", "lock", "sleep", "hibernate", "restart", "reboot", "shut", "shutdown",
        "go", "volume", "brightness", "open", "close", "pause", "resume", "next", "previous", "stop", "cancel",
        "search", "google", "youtube", "switch", "bring", "launch", "start", "set", "turn", "make", "put", "mute",
        "unmute", "screen", "screenshot", "wake", "dim", "give", "find", "add", "new", "note", "timer", "remind",
        "reminder", "good", "thank", "thanks", "never", "forget", "nothing", "please", "can", "could", "would",
        "will", "kindly", "just", "want", "need", "do", "let", "let's", "lets", "hey"})
    - _NOT_REQUEST)


def strip_wake_words(words: list[str], wakes=()) -> list[str]:
    """Leading 'hey' / 'okay' and the name as Whisper writes it ('Cyrus', 'Sirius', 'Xyrus') are dropped. The
    English look-alikes (zero / serious / virus) only when alone or followed by a request word."""
    names = set(wakes) | NAME_FORMS
    out = list(words)
    for _ in range(3):
        if out and out[0] in names and (out[0] not in ENGLISH_NAMES or len(out) == 1
                                        or out[1] in REQUEST_WORDS):
            out = out[1:]
        elif len(out) > 1 and out[0] in ("hey", "hi", "okay", "ok") and out[1] in names:
            out = out[2:]
        else:
            break
    return out


def looks_like_song(text: str) -> bool:
    """'play amar bhindeshi tara' / 'xyrus can you play …' / 'put on X' / 'i want to hear X' - a play request
    with a title after it. The meaning layer's rewrites and filler drop run first, so the phrasing is free."""
    words = strip_wake_words(clean_text(text).split())
    try:
        toks = canonical(" ".join(words)).split()
    except Exception:                                       # noqa: BLE001 - never lose a request over this
        toks = [w for w in words if w not in FILLER_WORDS and w not in ARTICLES]
    for i, w in enumerate(toks[:2]):
        if w in PLAY_LIKE and len(toks) > i + 1:
            rest = toks[i + 1:]
            return rest not in (["pause"], ["next"], ["previous"], ["next", "song"], ["the", "next", "song"],
                                ["again"])
    return any(w in ("bajao", "chalao", "gaan") for w in words)


def bangla_likely(detected) -> bool:
    """faster-whisper detect_language result -> is this Bengali (or Hindi, which Whisper confuses it with)?
    Accepts (lang, prob, [(lang, prob), ...]) or a {lang: prob} mapping."""
    if not detected:
        return False
    probs: dict[str, float] = {}
    if isinstance(detected, dict):
        probs = {str(k): float(v) for k, v in detected.items()}
    else:
        try:
            probs[str(detected[0])] = float(detected[1])
            if len(detected) > 2 and detected[2]:
                for k, v in detected[2]:
                    probs[str(k)] = float(v)
        except (TypeError, ValueError, IndexError):
            return False
    if not probs:
        return False
    top = max(probs, key=probs.get)
    bn = probs.get("bn", 0.0) + probs.get("as", 0.0)          # Assamese uses the same script
    hi = probs.get("hi", 0.0)
    # Bangla only: a Hindi song ("Tum Hi Ho", "Kesariya") must stay on the English path, which hears it well.
    # Whisper does confuse the two, so Bangla also counts when it scores clearly even with Hindi on top.
    return (top in ("bn", "as") and probs[top] >= 0.35) or (bn >= 0.3 and bn >= 0.5 * hi)


def vad_available() -> bool:
    """Silero can be used (loads it on the first call; False when it can't load or "vad": "energy")."""
    try:
        from xyrus import vad as V
        return bool(V.warm())
    except Exception:                                       # noqa: BLE001
        return False


# ============================================================================ endpointing
@dataclass
class Utterance:
    pcm: bytes
    t_start: float
    t_end: float
    peak: float
    speech_s: float
    reason: str                     # "silence" | "cap"
    vad_speech_s: float = 0.0       # seconds of Silero speech windows (== speech_s on the VAD path)
    t_speech_start: float = 0.0     # first / last speech frame (VAD p >= VAD_SPEECH_P, or energy voiced)
    t_speech_end: float = 0.0
    vad_max: float = 0.0            # the highest window probability seen (0 on the energy path)


class _Notch:
    """2nd-order notch (RBJ biquad) applied as its truncated impulse response: the recursion's poles
    (|z| ~0.97 at 273 Hz, Q 2, 16 kHz) die out within 512 samples (to ~1e-6), so one np.convolve per frame gives
    the filtered frame exactly enough for a level estimate, without a per-sample Python loop."""

    TAPS = 512

    def __init__(self, f0: float = NOTCH_F0, rate: int = RATE, q: float = NOTCH_Q):
        import math
        w0 = 2 * math.pi * f0 / rate
        alpha = math.sin(w0) / (2 * q)
        cw = math.cos(w0)
        a0 = 1 + alpha
        b0, b1, b2 = 1 / a0, -2 * cw / a0, 1 / a0
        a1, a2 = -2 * cw / a0, (1 - alpha) / a0
        h, x1, x2, y1, y2 = [], 0.0, 0.0, 0.0, 0.0
        for n in range(self.TAPS):                          # the impulse response, by the recursion itself
            x = 1.0 if n == 0 else 0.0
            y = b0 * x + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
            h.append(y)
            x2, x1, y2, y1 = x1, x, y1, y
        self._coef = h
        self._h = None
        self._tail = None

    def process(self, a):
        """float32 samples -> the notched samples (same length); state carried between calls."""
        import numpy as np
        if self._h is None:
            self._h = np.asarray(self._coef, dtype=np.float32)
            self._tail = np.zeros(self.TAPS - 1, dtype=np.float32)
        buf = np.concatenate([self._tail, a])
        self._tail = buf[-(self.TAPS - 1):]
        return np.convolve(buf, self._h, mode="valid")


def _frame_stats(frame: bytes, hpf: _Notch | None = None) -> tuple[float, float]:
    """(mean square, peak) of int16 PCM, as 0..1 units. The mean square is measured on the filtered copy
    when a filter is given; the peak is always the raw signal's."""
    try:
        import numpy as np
        a = np.frombuffer(frame, dtype=np.int16).astype(np.float32) / 32768.0
        if not a.size:
            return 0.0, 0.0
        peak = float(np.max(np.abs(a)))
        if hpf is not None:
            a = hpf.process(a)
        return float(np.mean(a * a)), peak
    except ImportError:                                    # numpy ships with faster-whisper; this is a fallback
        from array import array
        a = array("h")
        a.frombytes(frame[: len(frame) & ~1])
        if not a:
            return 0.0, 0.0
        return sum(x * x for x in a) / len(a) / 32768.0 ** 2, max(max(a), -min(a)) / 32768.0


class Endpointer:
    """Feed 16 kHz mono int16 chunks (any size) with the monotonic time of each chunk's END; feed() returns
    an Utterance once one has ended (else None). observe() only learns the noise and keeps the pre-roll (the
    recognizer calls it while the wake grammar listens, so the noise estimate is warm when a capture starts).

    use_vad=True takes a xyrus.vad.SileroStream of its own (the ONNX session is shared, module-global) and
    falls back to the energy path when xyrus.vad.available is False; `vad` says which one runs."""

    def __init__(self, *, min_peak: float = 0.02, end_silence_s: float = END_SILENCE_S, max_s: float = MAX_S,
                 preroll_s: float = PREROLL_S, min_speech_s: float = MIN_SPEECH_S, use_vad: bool = True):
        self.min_peak = float(min_peak)
        self._vad = None
        if use_vad:
            try:
                from xyrus import vad as V
                stream = V.SileroStream()
                if V.available and stream.ok:
                    self._vad = stream
            except Exception as e:                          # noqa: BLE001 - the energy path is always there
                log.warning("capture: VAD unavailable (%r) - energy endpointing", e)
        self.vad = "silero" if self._vad is not None else "energy"
        self.frame = VAD_FRAME if self._vad is not None else FRAME
        self.frame_s = self.frame / RATE
        self.end_frames = max(1, round(end_silence_s / self.frame_s))
        self.max_frames = max(1, int(max_s / self.frame_s + 1e-9))       # floor: 30 s never becomes 30.016 s
        if self._vad is None:          # the 100 ms level smoothing smears every sound by ~50 ms: a 0.15 s click
            min_speech_s += ENERGY_SMEAR_S        # made 0.25 s of 'speech' - the energy path keeps its 0.3 s
        self.min_speech_frames = max(1, round(min_speech_s / self.frame_s))
        self.end_bumps = round(self.end_frames * END_BUMP_FRACTION)   # loud frames a closing window may hold
        self.tail_frames = round(TAIL_S / self.frame_s)
        onset_of = VAD_ONSET_OF if self._vad is not None else ONSET_OF
        self._levels: deque[float] = deque(maxlen=round(NOISE_WINDOW_S / self.frame_s))
        self._energy: deque[float] = deque(maxlen=SMOOTH_FRAMES)
        self._pre: deque[tuple[bytes, float, float, float, float]] = deque(
            maxlen=round(preroll_s / self.frame_s) + onset_of)
        self._loud: deque[bool] = deque(maxlen=onset_of)
        self._peaks: deque[float] = deque(maxlen=onset_of)
        self._hpf = _Notch()
        self._rest = b""
        self._floor = MIN_FLOOR
        self._ceil = MIN_FLOOR
        self._last_slice: tuple[int, int, int] | None = None   # last VAD span: (byte start, byte end, pcm len)
        self.discarded = 0              # utterances dropped as noise (too short / too quiet)
        self._reset_utt()

    # ---- state
    def _reset_utt(self) -> None:
        self.in_speech = False
        self._buf: list[tuple[bytes, float]] = []          # (frame, time of the frame's end)
        self._peak = 0.0
        self._voiced = 0
        self._level_sum = 0.0
        self._tail: deque[bool] = deque(maxlen=self.end_frames)   # loud? for each of the last END_SILENCE_S
        self._last_speech = -1
        self._first_speech = -1
        self._vad_max = 0.0
        self._on_thr = 0.0
        self._voice_thr = 0.0
        self._noise_ceil = 0.0

    def reset(self) -> None:
        """Drop any utterance in progress (echo gate, mode change); the noise estimate is kept (and so is the
        VAD's state: the user may still be talking, only the buffered part is dropped)."""
        self._reset_utt()
        self._loud.clear()
        self._peaks.clear()
        self._pre.clear()
        self._energy.clear()
        self._rest = b""

    @property
    def floor(self) -> float:
        return self._floor

    @property
    def ceiling(self) -> float:
        return self._ceil

    @property
    def use_vad(self) -> bool:
        return self._vad is not None

    def _update_noise(self) -> None:
        if not self._levels:
            return
        s = sorted(self._levels)
        n = len(s) - 1
        self._floor = max(MIN_FLOOR, s[n * FLOOR_PCT // 100])
        self._ceil = max(self._floor, s[n * CEIL_PCT // 100])

    def _frames(self, chunk: bytes, t_end: float):
        """-> (frame, time of its end, smoothed level, frame peak, VAD probability or -1) for every whole frame."""
        data = self._rest + chunk
        size = self.frame * 2
        n = len(data) // size
        self._rest = data[n * size:]
        t0 = t_end - (len(data) / 2) / RATE
        probs: list[float] = []
        if self._vad is not None and n:
            try:
                probs = self._vad.push(data[: n * size])
            except Exception:                               # noqa: BLE001
                log.exception("capture: VAD failed - energy endpointing from here")
                self._vad, self.vad = None, "energy"
        for i in range(n):
            f = data[i * size:(i + 1) * size]
            ms, peak = _frame_stats(f, self._hpf)
            self._energy.append(ms)
            level = (sum(self._energy) / len(self._energy)) ** 0.5
            p = probs[i] if i < len(probs) else -1.0
            yield f, t0 + (i + 1) * self.frame_s, level, peak, p

    # ---- feeding
    def observe(self, chunk: bytes, t_end: float) -> None:
        """Learn the noise / keep the pre-roll without starting an utterance."""
        if self.in_speech:
            self._reset_utt()
        for f, tf, level, peak, p in self._frames(chunk, t_end):
            self._levels.append(level)
            self._pre.append((f, tf, level, peak, p))
        self._update_noise()
        self._loud.clear()
        self._peaks.clear()

    def feed(self, chunk: bytes, t_end: float) -> Utterance | None:
        done: Utterance | None = None
        self._update_noise()
        for f, tf, level, peak, p in self._frames(chunk, t_end):
            if not self.in_speech:
                on_thr = max(self._floor * ONSET_OVER_FLOOR, self._ceil * ONSET_OVER_CEIL, MIN_ONSET_RMS)
                ready = len(self._levels) >= MIN_HISTORY_FRAMES
                self._levels.append(level)
                self._pre.append((f, tf, level, peak, p))
                if self._vad is not None:
                    self._loud.append(p >= VAD_ONSET_P)
                    self._peaks.append(peak)
                    start = ready and sum(self._loud) >= VAD_ONSET_N and max(self._peaks) >= self.min_peak
                else:
                    self._loud.append(level > on_thr and peak >= self.min_peak and ready)
                    start = sum(self._loud) >= ONSET_FRAMES
                if start:
                    self._start(on_thr)
                continue
            u = self._speech_frame(f, tf, level, peak, p)
            if u is not None and done is None:
                done = u
        return done

    def _is_speech(self, level: float, p: float) -> bool:
        if self._vad is not None:
            return p >= VAD_SPEECH_P
        return level > self._voice_thr

    def _start(self, on_thr: float) -> None:
        self.in_speech = True
        self._on_thr = on_thr
        self._voice_thr = max(self._floor * VOICE_OVER_FLOOR, self._ceil * VOICE_OVER_CEIL, MIN_ONSET_RMS)
        self._noise_ceil = self._ceil
        self._tail.clear()
        for f, tf, level, peak, p in self._pre:
            self._buf.append((f, tf))
            self._peak = max(self._peak, peak)
            self._vad_max = max(self._vad_max, p)
            if self._is_speech(level, p):
                self._voiced += 1
                self._level_sum += level
                self._last_speech = len(self._buf) - 1
                if self._first_speech < 0:
                    self._first_speech = self._last_speech
        self._pre.clear()
        self._loud.clear()
        self._peaks.clear()

    def _speech_frame(self, f: bytes, tf: float, level: float, peak: float, p: float) -> Utterance | None:
        self._buf.append((f, tf))
        self._peak = max(self._peak, peak)
        self._vad_max = max(self._vad_max, p)
        speech = self._is_speech(level, p)
        if speech:
            self._voiced += 1
            self._level_sum += level
        if self._vad is not None:
            loud = speech
        else:
            mean = self._level_sum / self._voiced if self._voiced else self._on_thr
            end_thr = max(self._noise_ceil * END_OVER_CEIL, mean * END_REL)
            loud = level >= end_thr
        self._tail.append(loud)
        if loud:
            self._last_speech = len(self._buf) - 1
            if self._first_speech < 0:
                self._first_speech = self._last_speech
        if len(self._buf) >= self.max_frames:
            return self._finish("cap")
        if len(self._tail) == self.end_frames and sum(self._tail) <= self.end_bumps and self._tail_settled():
            return self._finish("silence")
        return None

    def _tail_settled(self) -> bool:
        """VAD path: the bumps END_BUMP_FRACTION allows are noise INSIDE the pause, never the last windows of the
        speech before it nor the first windows of the speech after it - so both edges of the closing window
        must be silent. Without this the end fired ~1.2 s after the last word, or on the next phrase's onset,
        and 'can you please … (1.2 s) … play …' was cut in 2 of 6 faint runs (measured Sep 15)."""
        if self._vad is None:
            return True                                      # the energy path is unchanged
        tail = list(self._tail)
        edge = self.end_bumps + 1
        return not any(tail[:edge]) and not any(tail[-2:])

    def _finish(self, reason: str) -> Utterance | None:
        buf, peak, voiced, vad_max = self._buf, self._peak, self._voiced, self._vad_max
        first, last, on_thr = self._first_speech, self._last_speech, self._on_thr
        last = last if last >= 0 else len(buf) - 1
        first = max(0, min(first, last))
        keep = buf if reason == "cap" else buf[: min(len(buf), last + 1 + self.tail_frames)]
        self._reset_utt()
        if voiced < self.min_speech_frames or peak < self.min_peak or not keep:
            self.discarded += 1
            log.debug("capture: dropped %.2f s (%d speech frames, peak %.3f, vad %.2f max) as noise",
                      len(keep) * self.frame_s, voiced, peak, vad_max)
            return None
        speech_s = voiced * self.frame_s
        pcm = b"".join(f for f, _ in keep)
        size = self.frame * 2
        last = min(last, len(keep) - 1)
        self._last_slice = (first * size, (last + 1) * size, len(pcm)) if self._vad is not None else None
        log.info("capture: %.1f s speech (vad %.2f max) peak %.2f floor %.4f ceil %.4f on_thr %.4f vad=%s",
                 speech_s, max(vad_max, 0.0), peak, self._floor, self._ceil, on_thr, self.vad)
        return Utterance(pcm=pcm, t_start=keep[0][1] - self.frame_s, t_end=keep[-1][1], peak=peak,
                         speech_s=speech_s, reason=reason,
                         vad_speech_s=speech_s if self._vad is not None else 0.0,
                         t_speech_start=keep[first][1] - self.frame_s, t_speech_end=keep[last][1],
                         vad_max=max(vad_max, 0.0))

    def speech_slice(self, pcm: bytes) -> bytes:
        """The part of the last utterance's PCM worth decoding: the VAD's speech span +- SLICE_PAD_S, or (no VAD
        span for this PCM) the last SLICE_FALLBACK_S seconds."""
        span = self._last_slice
        if span is not None and span[2] == len(pcm):
            pad = int(SLICE_PAD_S * RATE) * 2
            a, b = max(0, span[0] - pad), min(len(pcm), span[1] + pad)
            return pcm[a:b]
        return pcm[-int(SLICE_FALLBACK_S * RATE) * 2:]
