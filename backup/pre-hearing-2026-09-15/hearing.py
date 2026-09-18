"""Whisper for the words the small English model can't hear: song names ("Ishwar by Vikings"),
search text and calendar titles. Runs offline (faster-whisper "small", int8 on the CPU), loads in the
background, and is only used for free text - the wake word and commands stay on Vosk (fast).

If the model or the runtime isn't installed, available() stays False and callers fall back to Vosk."""
import logging
import os
import re
import sys
import threading
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
# Best installed model first (user, Sep 14 2026: "download as many things if needed, it should listen perfect"):
# large-v3-turbo (multilingual, ~1.6 GB) when it is there, else the small model the first setup installed.
TURBO_DIR = BASE / "models" / "whisper-large-v3-turbo"
SMALL_DIR = BASE / "models" / "whisper-small"
MODEL_DIR = TURBO_DIR if (TURBO_DIR / "model.bin").exists() else SMALL_DIR
# Measured Sep 14 2026 on the 38 command phrases (RTX 5060 Ti, faint mic level): turbo GPU 29/38 in 0.15 s,
# small GPU 24/38 in 0.06 s, turbo CPU 30/38 in 3.9 s (too slow to wait for), small CPU 1.2 s. So: turbo on the
# GPU; without a working GPU (no CUDA DLLs, driver trouble) the small model on the CPU as before. The CUDA
# runtime comes from the pip packages nvidia-cublas-cu12 / nvidia-cudnn-cu12 (DLLs under site-packages/nvidia).
CANDIDATES = tuple((d, dev, ct) for d, dev, ct in ((TURBO_DIR, "cuda", "float16"), (SMALL_DIR, "cuda", "float16"),
                                                  (SMALL_DIR, "cpu", "int8"), (TURBO_DIR, "cpu", "int8"))
                   if (d / "model.bin").exists())
device = ""              # "cuda" / "cpu" once loaded
model_name = ""          # folder name of the loaded model
# Shows Whisper the shape of a request (wake word, "play", a title). Tested Sep 14 2026: a prompt naming the
# user's songs steered other requests toward them ("is war by viking" -> "Ishwar"); this one didn't, and
# YouTube still finds the right video for its spellings ("Ishvar by Vikings", "Tom Hiho", "Kasariya").
PROMPT = "Xyrus, play Shape of You by Ed Sheeran. Remind me to call the doctor tomorrow at five."
NO_SPEECH = 0.6          # segments Whisper itself thinks are silence are dropped (it hallucinates on noise)

log = logging.getLogger("xyrus.hearing")
_model = None
_lock = threading.Lock()
_ready = threading.Event()
_dll_dirs = []           # keep the add_dll_directory handles alive
error = None


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


def _load():
    global _model, error, device, model_name
    try:
        if not CANDIDATES:
            error = "model not installed"
            log.info("whisper: %s (%s)", error, MODEL_DIR)
            return
        _add_runtime_dlls()
        from faster_whisper import WhisperModel
        import numpy as np
        last = None
        for model_dir, dev, compute in CANDIDATES:
            t = time.time()
            try:
                model = WhisperModel(str(model_dir), device=dev, compute_type=compute)
                # warm-up: the first decode is slow (on the GPU it also compiles kernels, ~7 s once per start)
                list(model.transcribe(np.zeros(16000, dtype=np.float32), language="en")[0])
            except Exception as e:    # no GPU / CUDA DLLs missing / driver trouble: the next candidate
                last = e
                log.warning("whisper %s on %s (%s) failed: %r", model_dir.name, dev, compute, e)
                continue
            _model, device, model_name = model, dev, model_dir.name
            log.info("whisper ready in %.1f s: %s on %s (%s)", time.time() - t, model_name, dev, compute)
            return
        raise RuntimeError(f"no model could be loaded: {last!r}")
    except Exception as e:            # never stop Xyrus from starting
        error = repr(e)
        log.warning("whisper unavailable: %s", error)
    finally:
        _ready.set()


def load_async():
    threading.Thread(target=_load, name="whisper-load", daemon=True).start()


def available(wait: float = 0.0) -> bool:
    if wait:
        _ready.wait(wait)
    return _model is not None


def transcribe(pcm: bytes, prompt: str = PROMPT, language: str = "en") -> str:
    """16 kHz mono 16-bit PCM -> text as Whisper wrote it ('' if unavailable or only silence)."""
    if _model is None or not pcm:
        return ""
    import numpy as np
    audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    with _lock:                       # one decode at a time; the model isn't re-entrant
        segs, _info = _model.transcribe(audio, language=language, beam_size=5, initial_prompt=prompt,
                                        vad_filter=False, condition_on_previous_text=False,
                                        without_timestamps=True)
        return " ".join(s.text.strip() for s in segs if s.no_speech_prob < NO_SPEECH).strip()


def detect_language(pcm: bytes):
    """(language, probability, [(language, probability), ...]) of 16 kHz mono int16 PCM; None if unavailable."""
    heard = listen(pcm)
    return heard.language() if heard is not None else None


class Heard:
    """One utterance, encoded ONCE: text(language) and language() reuse the encoder output. On this PC the
    encoder is ~1 s of the ~1.2 s a transcribe() costs, and detect_language() alone costs as much again - so a
    song request heard in English, checked for Bangla and heard again in Bengali costs ~1.5 s, not ~3.7 s.
    Decoding matches transcribe(): beam 5, temperature fallback, no timestamps, segments Whisper itself
    calls silence (no_speech_prob >= NO_SPEECH) are dropped."""

    def __init__(self, model, pcm: bytes):
        import numpy as np
        from faster_whisper.audio import pad_or_trim
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        t = time.time()
        with _lock:
            feats = model.feature_extractor(audio)
            self._enc = model.encode(pad_or_trim(feats[:, :model.feature_extractor.nb_max_frames]))
        self._model = model
        self.encode_s = time.time() - t
        self.no_speech: float | None = None

    def language(self):
        with _lock:
            res = self._model.model.detect_language(self._enc)[0]
        probs = [(tok[2:-2], float(p)) for tok, p in res]
        return probs[0][0], probs[0][1], probs

    def text(self, language: str = "en", prompt: str | None = PROMPT) -> str:
        from faster_whisper.tokenizer import Tokenizer
        from faster_whisper.transcribe import TranscriptionOptions, get_suppressed_tokens
        m = self._model
        tok = Tokenizer(m.hf_tokenizer, m.model.is_multilingual, task="transcribe", language=language)
        opts = TranscriptionOptions(
            beam_size=5, best_of=5, patience=1.0, length_penalty=1.0, repetition_penalty=1.0,
            no_repeat_ngram_size=0, log_prob_threshold=-1.0, no_speech_threshold=NO_SPEECH,
            compression_ratio_threshold=2.4, condition_on_previous_text=False, prompt_reset_on_temperature=0.5,
            temperatures=[0.0, 0.2, 0.4, 0.6, 0.8, 1.0], initial_prompt=prompt, prefix=None, suppress_blank=True,
            suppress_tokens=get_suppressed_tokens(tok, [-1]), without_timestamps=True, max_initial_timestamp=1.0,
            word_timestamps=False, prepend_punctuations="\"'“¿([{-", append_punctuations="\"'.。,，!！?？:：”)]}、",
            multilingual=False, max_new_tokens=None, clip_timestamps=[0.0], hallucination_silence_threshold=None,
            hotwords=None)
        previous = tok.encode(" " + prompt.strip()) if prompt else []
        with _lock:
            tokens = m.get_prompt(tok, previous, without_timestamps=True)
            result, _avg, _temp, ratio = m.generate_with_fallback(self._enc, tokens, tok, opts)
        self.no_speech = float(result.no_speech_prob)
        if result.no_speech_prob >= NO_SPEECH or ratio > 2.4:
            return ""
        return tok.decode(result.sequences_ids[0]).strip()


def listen(pcm: bytes) -> Heard | None:
    """Encode an utterance once for several decodes (see Heard); None if Whisper isn't ready."""
    model = _model
    if model is None or not pcm:
        return None
    return Heard(model, pcm)


def clean(text: str) -> str:
    """'Xyrus, play Ishwar by Vikings.' -> 'xyrus play ishwar by vikings'. Bengali words stay whole (re's \\w
    does not match Bengali vowel signs)."""
    return " ".join(re.sub(r"[^\w\s'ঀ-৿]", " ", text.lower()).split())
