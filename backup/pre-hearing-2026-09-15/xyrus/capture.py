"""Command capture (lead request, Sep 14 2026): after "Waiting for your command, sir." the recognizer records
ONE utterance - no time limit to start, until the user has finished talking - and Whisper hears it.

- Endpointer: energy endpointing on 20 ms frames (the 0.25 s mic chunks are split), on a 100 ms smoothed
  level. Speech starts when the level rises well above the running noise floor and ceiling (20th / 90th
  percentile of the last 2 s) and the frame peak reaches min_speech_peak; it ends after END_SILENCE_S below a
  threshold relative to that utterance's own speech level (and the noise ceiling). 0.3 s pre-roll, 30 s hard
  cap. Room noise is rejected: an utterance needs >= MIN_SPEECH_S of speech-level frames and a real peak.
  Vosk's own endpointing is not used (in this room it keeps utterances open 13-19 s).
- CapturedTranscript: what the recognizer emits for such an utterance (a Transcript subclass, so every
  consumer keeps working; interfaces.py is unchanged). The engine routes it as a command with the wake word
  already given.
- clean_text / only_filler: Whisper text -> words (Bengali-safe: re's \\w misses Bengali vowel signs), and the
  hallucination staples Whisper produces on noise ("Thank you.", "you", "Thanks for watching").

Pure: no Vosk, no Whisper, no threads.
"""
from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass

from xyrus.interfaces import Transcript
from xyrus.normalize import PLAY_LIKE, WAKE_LIKE

RATE = 16000
FRAME = 320                       # 20 ms of int16 mono
FRAME_S = FRAME / RATE
PREROLL_S = 0.3
END_SILENCE_S = 1.3               # the user pauses inside a request ("can you please … play …"); 0.9 s cut it
TAIL_S = 0.3                      # silence kept after the last speech frame (Whisper hallucinates on long tails)
MAX_S = 30.0
MIN_SPEECH_S = 0.3
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
MIN_ONSET_RMS = 0.003
MIN_HISTORY_FRAMES = 10           # 0.2 s of noise history before anything may start
ONSET_FRAMES, ONSET_OF = 3, 4     # 3 loud frames out of the last 4 start an utterance (60 ms)
END_OVER_CEIL = 1.3
END_REL = 0.15                    # ~ -16 dB below the utterance's own mean speech level
END_BUMP_FRACTION = 0.08          # an utterance ends when <= 8 % of the last END_SILENCE_S is loud: a chair creak
                                  # or a noise bump no longer restarts the wait (unbroken silence never came)
MIN_FLOOR = 1e-4

CAPTURE_MODES = frozenset({"command", "confirm", "when", "free"})

# Whisper's staples on noise / silence / a cough. An utterance made only of these is dropped, never a command.
HALLUCINATIONS = frozenset({
    "you", "thank you", "thanks", "thank you very much", "thanks for watching", "thank you for watching",
    "thanks for watching and see you next time", "please subscribe", "subscribe", "bye", "bye bye",
    "oh", "uh", "um", "hmm", "mm", "huh", "ah", "so", "the", "i", "a", "and", "music", "applause", "laughter",
    "silence", "foreign", "ধন্যবাদ",
})
_CLEAN_RE = re.compile(r"[^\w\s'ঀ-৿]")


@dataclass(frozen=True)
class CapturedTranscript(Transcript):
    """A Transcript heard by the command capture (Whisper), not by the Vosk grammar."""
    captured: bool = True
    language: str = "en"             # "en", or "bn" (Bengali script in text/free_text)
    song_hint: bool = False          # the English pass heard a play request / the song question was open
    raw: str = ""                    # Whisper's own text (with case and punctuation)


def clean_text(text: str) -> str:
    """'Xyrus, open Chrome.' -> 'xyrus open chrome'; Bengali words stay whole ('আমার ভিনদেশী তারা')."""
    return " ".join(_CLEAN_RE.sub(" ", str(text or "").lower()).split())


def only_filler(text: str) -> bool:
    """Nothing but Whisper's hallucination staples (or nothing at all)."""
    t = clean_text(text)
    if not t:
        return True
    if t in HALLUCINATIONS:
        return True
    words = t.split()
    return len(words) <= 3 and all(w in HALLUCINATIONS for w in words)


NAME_FORMS = WAKE_LIKE | {"zyrus", "xirus", "sirus", "cyprus", "cirus", "sairus", "virus", "zeros", "cirrus"}


def strip_wake_words(words: list[str], wakes=()) -> list[str]:
    """Leading 'hey' / 'okay' and the name as Whisper writes it ('Cyrus', 'Sirius', 'Xyrus') are dropped."""
    names = set(wakes) | NAME_FORMS
    out = list(words)
    for _ in range(3):
        if out and out[0] in names:
            out = out[1:]
        elif len(out) > 1 and out[0] in ("hey", "hi", "okay", "ok") and out[1] in names:
            out = out[2:]
        else:
            break
    return out


def looks_like_song(text: str) -> bool:
    """'play amar bhindeshi tara' / 'xyrus play …' - a play request with a title after it."""
    words = strip_wake_words(clean_text(text).split())
    for i, w in enumerate(words[:2]):
        if w in PLAY_LIKE and len(words) > i + 1:
            rest = words[i + 1:]
            return rest not in (["pause"], ["next"], ["previous"], ["the", "next", "song"], ["again"])
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


# ============================================================================ endpointing
@dataclass
class Utterance:
    pcm: bytes
    t_start: float
    t_end: float
    peak: float
    speech_s: float
    reason: str                     # "silence" | "cap"


def _frame_stats(frame: bytes) -> tuple[float, float]:
    """(mean square, peak) of int16 PCM, as 0..1 units."""
    try:
        import numpy as np
        a = np.frombuffer(frame, dtype=np.int16).astype(np.float32) / 32768.0
        if not a.size:
            return 0.0, 0.0
        return float(np.mean(a * a)), float(np.max(np.abs(a)))
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
    recognizer calls it while the wake grammar listens, so the noise estimate is warm when a capture starts)."""

    def __init__(self, *, min_peak: float = 0.02, end_silence_s: float = END_SILENCE_S, max_s: float = MAX_S,
                 preroll_s: float = PREROLL_S, min_speech_s: float = MIN_SPEECH_S):
        self.min_peak = float(min_peak)
        self.end_frames = max(1, round(end_silence_s / FRAME_S))
        self.max_frames = max(1, round(max_s / FRAME_S))
        self.min_speech_frames = max(1, round(min_speech_s / FRAME_S))
        self.end_bumps = round(self.end_frames * END_BUMP_FRACTION)   # loud frames a closing window may hold
        self.tail_frames = round(TAIL_S / FRAME_S)
        self._levels: deque[float] = deque(maxlen=round(NOISE_WINDOW_S / FRAME_S))
        self._energy: deque[float] = deque(maxlen=SMOOTH_FRAMES)
        self._pre: deque[tuple[bytes, float, float, float]] = deque(maxlen=round(preroll_s / FRAME_S) + ONSET_OF)
        self._loud: deque[bool] = deque(maxlen=ONSET_OF)
        self._rest = b""
        self._floor = MIN_FLOOR
        self._ceil = MIN_FLOOR
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
        self._on_thr = 0.0
        self._voice_thr = 0.0
        self._noise_ceil = 0.0

    def reset(self) -> None:
        """Drop any utterance in progress (echo gate, mode change); the noise estimate is kept."""
        self._reset_utt()
        self._loud.clear()
        self._pre.clear()
        self._energy.clear()
        self._rest = b""

    @property
    def floor(self) -> float:
        return self._floor

    @property
    def ceiling(self) -> float:
        return self._ceil

    def _update_noise(self) -> None:
        if not self._levels:
            return
        s = sorted(self._levels)
        n = len(s) - 1
        self._floor = max(MIN_FLOOR, s[n * FLOOR_PCT // 100])
        self._ceil = max(self._floor, s[n * CEIL_PCT // 100])

    def _frames(self, chunk: bytes, t_end: float):
        """-> (frame, time of its end, smoothed level, frame peak) for every whole 20 ms frame."""
        data = self._rest + chunk
        n = len(data) // (FRAME * 2)
        self._rest = data[n * FRAME * 2:]
        t0 = t_end - (len(data) / 2) / RATE
        for i in range(n):
            f = data[i * FRAME * 2:(i + 1) * FRAME * 2]
            ms, peak = _frame_stats(f)
            self._energy.append(ms)
            level = (sum(self._energy) / len(self._energy)) ** 0.5
            yield f, t0 + (i + 1) * FRAME_S, level, peak

    # ---- feeding
    def observe(self, chunk: bytes, t_end: float) -> None:
        """Learn the noise / keep the pre-roll without starting an utterance."""
        if self.in_speech:
            self._reset_utt()
        for f, tf, level, peak in self._frames(chunk, t_end):
            self._levels.append(level)
            self._pre.append((f, tf, level, peak))
        self._update_noise()
        self._loud.clear()

    def feed(self, chunk: bytes, t_end: float) -> Utterance | None:
        done: Utterance | None = None
        self._update_noise()
        for f, tf, level, peak in self._frames(chunk, t_end):
            if not self.in_speech:
                on_thr = max(self._floor * ONSET_OVER_FLOOR, self._ceil * ONSET_OVER_CEIL, MIN_ONSET_RMS)
                loud = level > on_thr and peak >= self.min_peak and len(self._levels) >= MIN_HISTORY_FRAMES
                self._levels.append(level)
                self._loud.append(loud)
                self._pre.append((f, tf, level, peak))
                if sum(self._loud) >= ONSET_FRAMES:
                    self._start(on_thr)
                continue
            u = self._speech_frame(f, tf, level, peak)
            if u is not None and done is None:
                done = u
        return done

    def _start(self, on_thr: float) -> None:
        self.in_speech = True
        self._on_thr = on_thr
        self._voice_thr = max(self._floor * VOICE_OVER_FLOOR, self._ceil * VOICE_OVER_CEIL, MIN_ONSET_RMS)
        self._noise_ceil = self._ceil
        self._tail.clear()
        for f, tf, level, peak in self._pre:
            self._buf.append((f, tf))
            self._peak = max(self._peak, peak)
            if level > self._voice_thr:
                self._voiced += 1
                self._level_sum += level
                self._last_speech = len(self._buf) - 1
        self._pre.clear()
        self._loud.clear()

    def _speech_frame(self, f: bytes, tf: float, level: float, peak: float) -> Utterance | None:
        self._buf.append((f, tf))
        self._peak = max(self._peak, peak)
        if level > self._voice_thr:
            self._voiced += 1
            self._level_sum += level
        mean = self._level_sum / self._voiced if self._voiced else self._on_thr
        end_thr = max(self._noise_ceil * END_OVER_CEIL, mean * END_REL)
        loud = level >= end_thr
        self._tail.append(loud)
        if loud:
            self._last_speech = len(self._buf) - 1
        if len(self._buf) >= self.max_frames:
            return self._finish("cap")
        if len(self._tail) == self.end_frames and sum(self._tail) <= self.end_bumps:
            return self._finish("silence")
        return None

    def _finish(self, reason: str) -> Utterance | None:
        buf, peak, voiced = self._buf, self._peak, self._voiced
        last = self._last_speech if self._last_speech >= 0 else len(buf) - 1
        keep = buf if reason == "cap" else buf[: min(len(buf), last + 1 + self.tail_frames)]
        self._reset_utt()
        if voiced < self.min_speech_frames or peak < self.min_peak or not keep:
            self.discarded += 1
            return None
        return Utterance(pcm=b"".join(f for f, _ in keep), t_start=keep[0][1] - FRAME_S, t_end=keep[-1][1],
                         peak=peak, speech_s=voiced * FRAME_S, reason=reason)
