"""Whisper for everything the small Vosk grammar can't hear: the whole command after the wake reply, song
names ("Ishwar by Vikings"), search text, the faint "xyrus" second opinion. Runs offline (faster-whisper on
the GPU, the small model on the CPU as the last resort), loads in the background, and never stops Xyrus from
starting: if the model or the runtime isn't installed, available() stays False and callers fall back to Vosk.

Hearing rebuild (Sep 15 2026): large-v3 first (the user's seat gives speech peaks of 0.03-0.05 over a room floor
of rms ~0.0033 and large-v3 hears such clips where turbo/small lose a third of them), a command-shaped prompt
that cannot leak a sentence into the transcript, avg_logprob / compression stored on every decode, n-best
hypotheses for the song matcher, a Bengali specialist model when it is installed, and runtime degrade: a model
that starts failing on the GPU is replaced by the next candidate without a restart."""
import hashlib
import json
import logging
import os
import re
import sys
import threading
import time
import wave
from pathlib import Path

BASE = Path(__file__).resolve().parent
LARGE_DIR = BASE / "models" / "whisper-large-v3"
TURBO_DIR = BASE / "models" / "whisper-large-v3-turbo"
SMALL_DIR = BASE / "models" / "whisper-small"
BN_DIR = BASE / "models" / "whisper-large-v3-bn"       # Bengali fine-tune (CT2 fp16), optional
# Measured Sep 15 2026, faint 38 (peak 0.04 in rms-0.0033 hiss), encode+decode per Heard.text(), RTX 5060 Ti
# CONTENDED (the live Xyrus and other processes shared the GPU, ~94 % busy at the end):
#   large-v3 float16       36/38 exact  WER 1.4 %  median 0.258 s  p95 0.476 s  max 0.510 s
#   large-v3 int8_float16  35/38 exact  WER 2.1 %  median 0.271 s  p95 0.493 s  max 0.559 s
#   large-v3-turbo float16 33/38 exact  WER 4.1 %  median 0.198 s  p95 0.323 s  max 0.339 s
# float16 is inside the gate (median <= 0.30 s, p95 <= 0.6 s), so it stays candidate #1; int8_float16 is #2
# (same weights, survives a GPU too full for fp16).
# The CUDA runtime comes from the pip packages nvidia-cublas-cu12 / nvidia-cudnn-cu12 (DLLs under
# site-packages/nvidia); without a working GPU the small model on the CPU as before.
_ALL_CANDIDATES = ((LARGE_DIR, "cuda", "float16"), (LARGE_DIR, "cuda", "int8_float16"),
                   (TURBO_DIR, "cuda", "float16"), (SMALL_DIR, "cuda", "float16"), (SMALL_DIR, "cpu", "int8"))
CANDIDATES = tuple(c for c in _ALL_CANDIDATES if (c[0] / "model.bin").exists())
MODEL_DIR = CANDIDATES[0][0] if CANDIDATES else LARGE_DIR
# A pin (XYRUS_WHISPER env, else config "hearing_model") moves one candidate to the front; "auto" = no pin.
_PINS = {"large": (LARGE_DIR, "cuda", "float16"), "large-v3": (LARGE_DIR, "cuda", "float16"),
         "large-v3-int8": (LARGE_DIR, "cuda", "int8_float16"), "turbo": (TURBO_DIR, "cuda", "float16"),
         "small": (SMALL_DIR, "cuda", "float16")}
device = ""              # "cuda" / "cpu" once loaded

# Shows Whisper the shape of a request: the name, an app command, a question, a media verb. Command-shaped on
# purpose - the old prompt (a song title and a reminder sentence) leaked whole: a 0.6 s noise blip came back as
# "I'm going to call the doctor tomorrow at five." and other song requests drifted toward Ed Sheeran. The
# shipped string never names a song, an artist or a reminder; what it can leak is guarded by prompt_echo().
# Chosen by the B1 bench (large-v3 fp16, Sep 15 2026; tests/test_hearing.py re-measures it). Whisper reads a
# multi-sentence prompt's LAST sentence back on noise: "Xyrus, open Chrome. What time is it? Play a song." gave
# "Play a song." on 49/50 noise clips (lp ~-0.5, a real command, so no guard can drop it) and 2/45 sound-alike
# name hits. One sentence can only leak whole (dropped by prompt_echo) or not at all:
#   prompt                                      faint38  WER   alike hits  faint xyrus  noise survivors
#   "Xyrus, open Chrome and play a song."        35/38   2.1%    1/45        9/9          0/50   <- shipped
#   "Xyrus, open Chrome. What time is it? ..."   36/38   1.4%    2/45        5/9         49/50
#   "Xyrus."                                     33/38   4.8%    2/45        3/9          0/50
#   None                                         33/38   4.8%    0/45        3/9          0/50
#   "Xyrus, play a song. What time is it?"       34/38   4.1%    0/45        4/9         30/50
#   legacy (song + doctor)                       33/38   8.9%    0/45        9/9          (leaks)
PROMPT_CMD = "Xyrus, open Chrome and play a song."
PROMPT = PROMPT_CMD      # alias: older callers pass hearing.PROMPT
_PROMPT_LEGACY = "Xyrus, play Shape of You by Ed Sheeran. Remind me to call the doctor tomorrow at five."
NO_SPEECH = 0.6          # advisory only: Whisper's no_speech_prob is 0.00 on this room's noise (measured)
# avg_logprob (faster-whisper's formula) separates this room's noise (-0.5..-1.9) from faint speech
# (>= -0.35). DROP is what a caller may enforce (Heard.text(min_logprob=LOGPROB_DROP)); UNSURE marks a
# transcript worth a "did you mean" instead of a straight run. Neither is enforced here unless asked.
LOGPROB_DROP = -0.9
LOGPROB_UNSURE = -0.45
COMPRESSION_MAX = 2.4    # "Bye. Bye. Bye. Bye." from 0.3 s of noise: repeats compress; dropped always
# Exact command phrases that happen to be in the prompt: a transcript that IS one of these is a command,
# never an echo (a noise blip that reads "what time is it" tells the time - harmless).
EXEMPT = frozenset({"what time is it", "open chrome", "play a song", "what's the time"})
_NAME_RE = re.compile(r"^[xzcs](?:y|i|e|ai)r(?:u|o|a)s(?:'s)?$")     # xyrus / cyrus / zyrus / xerus ...
_NAME_WORDS = frozenset({"xyrus", "cyrus", "zyrus", "xerus", "zeros", "zairus", "xyrus's"})
RECOVER_S = 10.0         # a decode that lost its model is redone on the next one while the utterance is fresh

log = logging.getLogger("xyrus.hearing")
_model = None            # the primary WhisperModel
_model_name = ""         # folder name of the loaded primary
_compute = ""
_lock = threading.Lock()         # one decode at a time; the model isn't re-entrant
_state = threading.Lock()        # guards _model / _fails / _cand_pos hand-overs
_ready = threading.Event()
_cand_pos = -1           # index into _ordered() of the loaded primary (the degrade path starts after it)
_fails = 0               # consecutive decode failures of the primary
_dll_dirs = []           # keep the add_dll_directory handles alive
error = None
_bn_model = None         # the Bengali specialist (second WhisperModel), or None
_bn_lock = threading.Lock()
_bn_fails = 0


def _add_runtime_dlls():
    """ctranslate2.dll needs msvcp140.dll. The msvc-runtime package puts Microsoft's DLLs inside the venv,
    which Windows doesn't search on its own - add every folder that has it."""
    if sys.platform != "win32":
        return
    root = Path(sys.prefix)
    dirs = [d for d in (root, root / "Scripts") if (d / "msvcp140.dll").exists()]
    if not dirs:                      # unusual layout: search (slow, but only once, on the loader thread)
        dirs = sorted({p.parent for p in root.rglob("msvcp140.dll")})
    # CUDA: ctranslate2 loads cublas64_12.dll / cudnn64_9.dll itself through the plain search path (PATH), not
    # the add_dll_directory list - both are set so either lookup finds them
    nvidia = root / "Lib" / "site-packages" / "nvidia"
    cuda_dirs = [d for d in (nvidia / "cublas" / "bin", nvidia / "cudnn" / "bin", nvidia / "cuda_nvrtc" / "bin")
                 if d.is_dir()]
    if cuda_dirs:
        os.environ["PATH"] = os.pathsep.join([str(d) for d in cuda_dirs] + [os.environ.get("PATH", "")])
    for d in map(str, dirs + cuda_dirs):
        try:
            _dll_dirs.append(os.add_dll_directory(d))
        except OSError:
            pass


# ============================================================================ configuration
def _config_value(key: str, default):
    """One key from the file the app loads (xyrus.paths.config_file(); the live Config is not importable as a
    singleton). Read lazily, every call - it is only consulted at load time. Any failure -> default."""
    try:
        try:
            from xyrus import paths
            file = paths.config_file()
        except Exception:
            env = os.environ.get("XYRUS_DATA_DIR")
            file = Path(env) / "config.json" if env else BASE / "config.json"
        with open(file, encoding="utf-8") as f:
            data = json.load(f)
        value = data.get(key, default) if isinstance(data, dict) else default
        return default if value is None else value
    except Exception:
        return default


def _pin():
    """The pinned candidate (XYRUS_WHISPER first, then config hearing_model) or None for "auto" / unknown."""
    name = (os.environ.get("XYRUS_WHISPER") or "").strip().lower()
    if not name:
        name = str(_config_value("hearing_model", "auto") or "auto").strip().lower()
    return _PINS.get(name)


def _ordered():
    """CANDIDATES with the pinned one first (when it is installed)."""
    pin = _pin()
    if pin is None or pin not in CANDIDATES:
        return CANDIDATES
    return (pin,) + tuple(c for c in CANDIDATES if c != pin)


def _warmup_clip():
    """The synthesized "xyrus" from tests/_cache when present (a real decode, not just the kernels), else 1 s
    of silence. Returns float32 audio."""
    import numpy as np
    path = BASE / "tests" / "_cache" / (hashlib.sha1(b"xyrus").hexdigest()[:16] + ".wav")
    try:
        with wave.open(str(path), "rb") as w:
            if w.getframerate() == 16000 and w.getnchannels() == 1 and w.getsampwidth() == 2:
                pcm = w.readframes(w.getnframes())
                return np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    except Exception:
        pass
    return np.zeros(16000, dtype=np.float32)


# ============================================================================ loading
def _try_load(model_dir: Path, dev: str, compute: str):
    """One candidate: build it, decode silence and the warm-up clip (the first decode compiles the CUDA
    kernels, ~7 s once per start, and a model that loads but cannot run - OOM, cudnn, PTX - fails HERE, not on
    the user's first command). Returns the model or None (logged WARNING)."""
    from faster_whisper import WhisperModel
    import numpy as np
    t = time.time()
    try:
        model = WhisperModel(str(model_dir), device=dev, compute_type=compute)
        list(model.transcribe(np.zeros(16000, dtype=np.float32), language="en")[0])
        list(model.transcribe(_warmup_clip(), language="en", beam_size=5, initial_prompt=PROMPT_CMD,
                              vad_filter=False, condition_on_previous_text=False, without_timestamps=True)[0])
    except Exception as e:            # no GPU / CUDA DLLs missing / driver trouble / OOM: the next candidate
        log.warning("whisper %s on %s (%s) failed after %.1f s: %r", model_dir.name, dev, compute,
                    time.time() - t, e)
        return None
    log.info("whisper ready in %.1f s: %s on %s (%s)", time.time() - t, model_dir.name, dev, compute)
    return model


def _load(start: int = 0):
    """Loader thread: the first candidate from `start` that loads and runs becomes the primary; then the
    Bengali specialist (never before the primary is ready - the primary is what every command waits for)."""
    global _model, _model_name, _compute, error, device, _cand_pos, _fails
    try:
        order = _ordered()
        if not CANDIDATES:
            error = "model not installed"
            log.info("whisper: %s (%s)", error, MODEL_DIR)
            return
        _add_runtime_dlls()
        for i in range(start, len(order)):
            model_dir, dev, compute = order[i]
            model = _try_load(model_dir, dev, compute)
            if model is None:
                continue
            with _state:
                _model, _model_name, _compute, device, _cand_pos, _fails = model, model_dir.name, compute, dev, i, 0
                error = None
            _ready.set()
            _load_bn(dev)
            return
        error = "no model could be loaded" if start == 0 else "every Whisper candidate failed at runtime"
        log.error("whisper unavailable: %s", error)
    except Exception as e:            # never stop Xyrus from starting
        error = repr(e)
        log.warning("whisper unavailable: %s", error)
    finally:
        _ready.set()


def _load_bn(primary_device: str):
    """The Bengali specialist, on the loader thread after the primary: cuda float16, then cpu int8 only when
    the primary is on the CPU too (a CPU large model next to a GPU primary would only add seconds). Any
    failure leaves bn_available() False and the primary untouched."""
    global _bn_model, _bn_fails
    try:
        if _bn_model is not None or not (BN_DIR / "model.bin").exists():
            return
        if str(_config_value("bangla_model", "auto") or "auto").strip().lower() == "off":
            log.info("bangla model off (config bangla_model)")
            return
        tries = [("cuda", "float16")] + ([("cpu", "int8")] if primary_device == "cpu" else [])
        for dev, compute in tries:
            model = _try_load(BN_DIR, dev, compute)
            if model is not None:
                with _state:
                    _bn_model, _bn_fails = model, 0
                return
        log.warning("bangla model %s not loaded; Bengali stays on the primary model", BN_DIR.name)
    except Exception as e:
        log.warning("bangla model unavailable: %r", e)


def load_async():
    threading.Thread(target=_load, name="whisper-load", daemon=True).start()


def available(wait: float = 0.0) -> bool:
    if wait:
        _ready.wait(wait)
    return _model is not None


def model_name() -> str:
    """Folder name of the loaded primary ("" until it is ready / after every candidate died)."""
    return _model_name if _model is not None else ""


def bn_available() -> bool:
    return _bn_model is not None


# ============================================================================ runtime degrade
def _note_failure(model, exc) -> bool:
    """A decode raised (RuntimeError / OSError: CUDA errors surface as RuntimeError from ctranslate2). Two in a
    row mark the model dead: the primary is replaced by the next candidate on the loader thread (the
    specialist is simply dropped). Returns True when the model is (now) dead."""
    global _fails, _model, _bn_fails, _bn_model
    with _state:
        if model is _bn_model and model is not None:
            _bn_fails += 1
            log.warning("bangla decode failed (%d): %r", _bn_fails, exc)
            if _bn_fails < 2:
                return False
            _bn_model = None
            log.error("bangla model dropped after %d consecutive failures: %r", _bn_fails, exc)
            return True
        if model is not _model:                       # already replaced by another thread's failure
            return True
        _fails += 1
        log.warning("whisper decode failed (%d) on %s: %r", _fails, _model_name, exc)
        if _fails < 2:
            return False
        log.error("whisper %s dead after %d consecutive failures - loading the next candidate: %r",
                  _model_name, _fails, exc)
        _model = None
        _ready.clear()
        start = _cand_pos + 1
    threading.Thread(target=_load, args=(start,), name="whisper-reload", daemon=True).start()
    return True


def _note_success(model):
    global _fails, _bn_fails
    if model is _model:
        _fails = 0
    elif model is _bn_model:
        _bn_fails = 0


def _replacement(created: float):
    """The next primary, if it arrives while the utterance is < RECOVER_S old; else None."""
    left = RECOVER_S - (time.time() - created)
    if left <= 0 or not _ready.wait(left):
        return None
    return _model if time.time() - created < RECOVER_S else None


# ============================================================================ text helpers
def clean(text: str) -> str:
    """'Xyrus, play Ishwar by Vikings.' -> 'xyrus play ishwar by vikings'. Bengali words stay whole (re's \\w
    does not match Bengali vowel signs)."""
    return " ".join(re.sub(r"[^\w\s'ঀ-৿]", " ", text.lower()).split())


def _windows(words, n: int):
    return {tuple(words[i:i + n]) for i in range(len(words) - n + 1)}


def _sentences(prompt: str):
    return [clean(s).split() for s in re.split(r"[.?!]", prompt) if clean(s)]


def _is_name(word: str) -> bool:
    return word in _NAME_WORDS or bool(_NAME_RE.match(word))


def _cmd_windows():
    """4-word windows of PROMPT_CMD, less any that is (inside) an EXEMPT command phrase - "what time is it" is
    a whole command, and a user saying "what time is it now" is not an echo."""
    exempt = set()
    for phrase in EXEMPT:
        exempt |= _windows(clean(phrase).split(), 4)
    return _windows(clean(PROMPT_CMD).split(), 4) - exempt


def _legacy_windows(prompt: str | None):
    """3-word windows of the old prompt that only an echo produces: the doctor sentence's windows that name the
    doctor (the leak the user saw was "I'm going to call the doctor tomorrow at five."; "tomorrow at five" alone
    is an ordinary tail), plus the song sentence's windows only while the old prompt is the one in use - with
    the new prompt "play shape of you" is a real request, never an echo."""
    out = set()
    for sent in _sentences(_PROMPT_LEGACY):
        wins = _windows(sent, 3)
        if "doctor" in sent:
            out |= {w for w in wins if "doctor" in w}
        elif prompt and clean(prompt) == clean(_PROMPT_LEGACY):
            out |= wins
    return out


def prompt_echo(text: str, prompt: str | None = PROMPT_CMD) -> bool:
    """True when `text` is the prompt talking, not the user: (a) after a leading name the cleaned text is a
    word-substring (>= 2 words) of `prompt`, (b) it shares a sentence-crossing 4-word window with PROMPT_CMD,
    or (c) it shares a 3-word window with the old prompt (see _legacy_windows for which ones). Never True for
    the name alone or when the whole text is an EXEMPT command phrase ("what time is it", "open chrome", ...) -
    those are in the prompt on purpose and are commands."""
    cleaned = clean(text)
    if not cleaned:
        return False
    words = cleaned.split()
    stripped = list(words)
    while stripped and _is_name(stripped[0]):
        stripped.pop(0)
    if not stripped:                                  # "Xyrus." alone: the name, never an echo
        return False
    body = " ".join(stripped)
    if body in EXEMPT or cleaned in EXEMPT:
        return False
    if prompt:
        p = " " + clean(prompt) + " "
        if len(stripped) >= 2 and " " + body + " " in p:
            return True
    # (b) only for text made of nothing but prompt words: a real request that merely contains a prompt window
    # ("can you open Chrome and play something", "open Spotify and play a song") is the user, not an echo
    # (review, Sep 15: the window test alone dropped those silently and Xyrus looked deaf)
    if set(stripped) <= _cmd_words() and _windows(words, 4) & _cmd_windows():
        return True
    return bool(_windows(words, 3) & _legacy_windows(prompt))


def _cmd_words():
    return set(clean(PROMPT_CMD).split())


def _looping(text: str) -> bool:
    """'Bye. Bye. Bye. Bye.' - one word said >= 4 times making >= 60 % of the text. Too short for the
    compression ratio to notice (19 bytes compress to ~15), so it gets its own check."""
    words = clean(text).split()
    if len(words) < 4:
        return False
    top = max(words.count(w) for w in set(words))
    return top >= 4 and top >= 0.6 * len(words)


# ============================================================================ decoding
def transcribe(pcm: bytes, prompt: str | None = PROMPT_CMD, language: str = "en") -> str:
    """16 kHz mono 16-bit PCM -> text as Whisper wrote it ('' if unavailable, only noise, or a prompt echo)."""
    heard = listen(pcm)
    return heard.text(language=language, prompt=prompt) if heard is not None else ""


def detect_language(pcm: bytes):
    """(language, probability, [(language, probability), ...]) of 16 kHz mono int16 PCM; None if unavailable."""
    heard = listen(pcm)
    return heard.language() if heard is not None else None


class Heard:
    """One utterance, encoded ONCE: text(), text_nbest() and language() reuse the encoder output. On this PC
    the encoder is most of a decode's cost, so a song request heard in English, checked for Bangla and heard
    again in Bengali costs one encode, not three. After text(): avg_logprob (faster-whisper's formula),
    compression, no_speech, fallback_used (the T=0.2 retry was used).
    A decode that raises (the GPU going away under the model) is retried once; a second failure marks the model
    dead (see _note_failure) and, while the utterance is < RECOVER_S old, redone on the next candidate."""

    def __init__(self, model, pcm: bytes, lock=None, primary: bool = True):
        self._pcm = pcm
        self._lock = lock or _lock
        self._primary = primary
        self.created = time.time()
        self.avg_logprob: float | None = None
        self.compression: float | None = None
        self.no_speech: float | None = None
        self.fallback_used = False
        self.encode_s = 0.0
        self._encode(model)

    def _encode(self, model):
        import numpy as np
        from faster_whisper.audio import pad_or_trim
        audio = np.frombuffer(self._pcm, dtype=np.int16).astype(np.float32) / 32768.0
        t = time.time()
        with self._lock:
            feats = model.feature_extractor(audio)
            self._enc = model.encode(pad_or_trim(feats[:, :model.feature_extractor.nb_max_frames]))
        self._model = model
        self.encode_s = time.time() - t
        _note_success(model)                  # a retry that worked ends the failure streak

    def _run(self, fn, fallback):
        """fn(model) under the model's lock, with the degrade rules. `fallback` is what a dead model returns."""
        for attempt in range(2):
            try:
                with self._lock:
                    out = fn(self._model)
                _note_success(self._model)
                return out
            except (RuntimeError, OSError) as e:
                if not _note_failure(self._model, e):
                    continue                          # one retry on the same model
                if not self._primary:
                    return fallback
                model = _replacement(self.created)
                if model is None:
                    return fallback
                try:
                    self._encode(model)               # the encoding belongs to the dead model
                except (RuntimeError, OSError) as e2:
                    _note_failure(model, e2)
                    return fallback
                return self._run(fn, fallback)
        return fallback

    def language(self):
        def go(m):
            return m.model.detect_language(self._enc)[0]
        res = self._run(go, None)
        if res is None:
            return None
        probs = [(tok[2:-2], float(p)) for tok, p in res]
        return probs[0][0], probs[0][1], probs

    def _tokenizer(self, language: str):
        from faster_whisper.tokenizer import Tokenizer
        m = self._model
        return Tokenizer(m.hf_tokenizer, m.model.is_multilingual, task="transcribe", language=language)

    def _prompt_tokens(self, tok, prompt: str | None):
        previous = tok.encode(" " + prompt.strip()) if prompt else []
        return self._model.get_prompt(tok, previous, without_timestamps=True)

    def text(self, language: str = "en", prompt: str | None = PROMPT_CMD, min_logprob: float | None = None) -> str:
        """The transcript ('' for nothing / a repeat / a prompt echo / below min_logprob when one is passed).
        Every would-be drop is logged at INFO whether it is enforced or not - the log is how the thresholds
        get tuned against real use."""
        from faster_whisper.transcribe import TranscriptionOptions, get_suppressed_tokens

        def go(m):
            # tokenizer, suppress list and prompt come from the model that decodes: after a degrade hand-over
            # (_run re-encodes on the next candidate) large-v3's token ids are wrong for small (review, Sep 15)
            tok = self._tokenizer(language)
            opts = TranscriptionOptions(
                beam_size=5, best_of=5, patience=1.0, length_penalty=1.0, repetition_penalty=1.0,
                no_repeat_ngram_size=0, log_prob_threshold=-1.0, no_speech_threshold=NO_SPEECH,
                compression_ratio_threshold=COMPRESSION_MAX, condition_on_previous_text=False,
                prompt_reset_on_temperature=0.5, temperatures=[0.0, 0.2], initial_prompt=prompt, prefix=None,
                suppress_blank=True, suppress_tokens=get_suppressed_tokens(tok, [-1]), without_timestamps=True,
                max_initial_timestamp=1.0, word_timestamps=False, prepend_punctuations="\"'“¿([{-",
                append_punctuations="\"'.。,，!！?？:：”)]}、", multilingual=False, max_new_tokens=None,
                clip_timestamps=[0.0], hallucination_silence_threshold=None, hotwords=None)
            tokens = self._prompt_tokens(tok, prompt)
            return m.generate_with_fallback(self._enc, tokens, tok, opts), tok
        got = self._run(go, None)
        if got is None:
            return ""
        (result, avg, temperature, ratio), tok = got
        self.avg_logprob = float(avg)
        self.compression = float(ratio)
        self.no_speech = float(result.no_speech_prob)
        self.fallback_used = temperature > 0
        text = tok.decode(result.sequences_ids[0]).strip()
        if not text:
            return ""
        if ratio > COMPRESSION_MAX:
            log.info("whisper drop: %r lp=%.2f reason=compression %.2f", text, avg, ratio)
            return ""
        if _looping(text):
            log.info("whisper drop: %r lp=%.2f reason=repeat", text, avg)
            return ""
        if prompt_echo(text, prompt):
            log.info("whisper drop: %r lp=%.2f reason=prompt_echo", text, avg)
            return ""
        if avg < LOGPROB_DROP or (min_logprob is not None and avg < min_logprob):
            enforced = min_logprob is not None and avg < min_logprob
            log.info("whisper drop: %r lp=%.2f reason=logprob%s no_speech=%.2f", text, avg,
                     "" if enforced else " (advisory)", self.no_speech)
            if enforced:
                return ""
        return text

    def text_nbest(self, language: str = "en", prompt: str | None = None, n: int = 5):
        """[(text, score), ...] best first: one beam search (beam 5, the T=0 options of text()) returning up
        to n hypotheses; score = faster-whisper's avg_logprob of each (cum / (len + 1)). Hypothesis 0 is what
        text() decodes at T=0 with the same prompt. Works for language="bn" on the same encoding."""
        from faster_whisper.transcribe import get_suppressed_tokens
        n = max(1, int(n))

        def go(m):
            tok = self._tokenizer(language)           # from the decoding model (see text())
            tokens = self._prompt_tokens(tok, prompt)
            return m.model.generate(
                self._enc, [tokens], beam_size=max(5, n), patience=1.0, num_hypotheses=n, length_penalty=1.0,
                repetition_penalty=1.0, no_repeat_ngram_size=0, max_length=m.max_length, return_scores=True,
                return_no_speech_prob=True, suppress_blank=True, suppress_tokens=get_suppressed_tokens(tok, [-1]),
                max_initial_timestamp_index=int(round(1.0 / m.time_precision)))[0], tok
        got = self._run(go, None)
        if got is None:
            return []
        result, tok = got
        out = []
        for ids, score in zip(result.sequences_ids, result.scores):
            length = len(ids)
            out.append((tok.decode(ids).strip(), float(score) * length / (length + 1)))
        out.sort(key=lambda p: p[1], reverse=True)
        return out[:n]


def listen(pcm: bytes) -> Heard | None:
    """Encode an utterance once for several decodes (see Heard); None if Whisper isn't ready or every
    candidate has died."""
    model = _model
    if model is None or not pcm:
        return None
    try:
        return Heard(model, pcm)
    except (RuntimeError, OSError) as e:
        if not _note_failure(model, e):               # one retry on the same model
            try:
                return Heard(model, pcm)
            except (RuntimeError, OSError) as e2:
                if not _note_failure(model, e2):
                    return None
        model = _replacement(time.time())
        if model is None:
            return None
        try:
            return Heard(model, pcm)
        except (RuntimeError, OSError) as e3:
            _note_failure(model, e3)
            return None


def listen_bn(pcm: bytes) -> Heard | None:
    """The Bengali specialist's encoding of an utterance (its own model, its own lock); None when it is not
    loaded. Its failures never touch the primary."""
    model = _bn_model
    if model is None or not pcm:
        return None
    try:
        return Heard(model, pcm, lock=_bn_lock, primary=False)
    except (RuntimeError, OSError) as e:
        _note_failure(model, e)
        return None
