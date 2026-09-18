"""Silero VAD as a stream (hearing rebuild, Sep 15 2026): one speech probability per 512-sample window (32 ms),
driven by hand so the LSTM state survives between mic chunks.

Why not faster_whisper's SileroVADModel.__call__: it zeroes h/c on every call (vad.py ~319-347), which is fine
for a whole clip but degrades the faint onsets this room produces when it is fed 0.25 s chunks - the first
window of every chunk starts "cold". Here the 64-sample context and the h/c state are carried across push()
calls, so a chunked run and a whole-clip run give the same probabilities (tests/test_vad.py).

The model file is faster_whisper's own asset (assets/silero_vad_v6.onnx, onnxruntime CPU); the session is
created once per process (warm()), shared by every SileroStream and never deleted. Measured Sep 15: the faint
38-phrase set (peaks 0.03-0.05 over rms 0.0033 hiss) scores >= 0.7 in speech, ~0 on the hiss alone.

available is False - and every Endpointer falls back to the energy path - when onnxruntime or the model
can't be loaded, or when the user chose the energy endpointer (XYRUS_VAD=energy / config "vad": "energy").
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
from pathlib import Path

log = logging.getLogger("xyrus.vad")

RATE = 16000
WINDOW = 512                      # samples per probability (32 ms at 16 kHz) - the model's fixed frame
CONTEXT = 64                      # samples of the previous window the model sees before each new one
STATE_SHAPE = (1, 1, 128)

available: bool = False           # a session exists and the VAD may be used (set by warm())
error: str | None = None          # why not, once warm() has tried
_session = None                   # the one onnxruntime.InferenceSession (module-global; never del'd)
_state_names: tuple[str, ...] = ("h", "c")   # ("h", "c") or ("state",) - read from the model's input names
_tried = False
_lock = threading.Lock()          # session creation + session.run (several streams may share it)
_dll_dirs: list = []              # keep the add_dll_directory handles alive


def _add_runtime_dlls() -> None:
    """onnxruntime's DLL (like ctranslate2's) needs msvcp140.dll, which the msvc-runtime package puts inside
    the venv where Windows doesn't look. Same search as hearing._add_runtime_dlls(), kept here so the VAD can
    load before / without Whisper."""
    if sys.platform != "win32":
        return
    root = Path(sys.prefix)
    dirs = [d for d in (root, root / "Scripts") if (d / "msvcp140.dll").exists()]
    if not dirs:                  # unusual layout: search (slow, but only once)
        dirs = sorted({p.parent for p in root.rglob("msvcp140.dll")})
    for d in map(str, dirs):
        try:
            _dll_dirs.append(os.add_dll_directory(d))
        except OSError:
            pass


def energy_forced() -> bool:
    """The user asked for the energy endpointer: XYRUS_VAD=energy, or "vad": "energy" in config.json."""
    if os.environ.get("XYRUS_VAD", "").strip().lower() == "energy":
        return True
    try:
        from xyrus.config import DEFAULTS
        from xyrus.paths import config_file
        choice = DEFAULTS.get("vad", "silero")
        path = config_file()
        if path.exists():          # read directly: Config.load() renames a corrupt file, a probe must not
            saved = json.loads(path.read_text(encoding="utf8"))
            if isinstance(saved, dict) and "vad" in saved:
                choice = saved["vad"]
        return str(choice).strip().lower() == "energy"
    except Exception:              # no config / unreadable: the default (silero)
        return False


def _load():
    """-> (session, state input names). ImportError / OSError / anything: the caller reports it once."""
    _add_runtime_dlls()
    import onnxruntime                                   # noqa: F401  (must come after the DLL dirs)
    from faster_whisper.vad import get_vad_model
    session = get_vad_model().session                    # intra_op_num_threads=1, CPU provider
    names = {i.name for i in session.get_inputs()}
    if "input" not in names:
        raise RuntimeError(f"unexpected VAD model inputs {sorted(names)}")
    if {"h", "c"} <= names:
        state = ("h", "c")
    elif "state" in names:
        state = ("state",)
    else:
        raise RuntimeError(f"unexpected VAD model state inputs {sorted(names)}")
    return session, state


def warm() -> bool:
    """Create the session once (any thread); True when the VAD may be used. Failures are logged ONCE."""
    global _session, _state_names, available, error, _tried
    with _lock:
        if _tried:
            return available
        _tried = True
        if energy_forced():
            error = "energy endpointing chosen (XYRUS_VAD / config vad)"
            log.info("vad: %s", error)
            available = False
            return False
        try:
            _session, _state_names = _load()
        except Exception as e:                           # noqa: BLE001 - never stop Xyrus from starting
            error = repr(e)
            available = False
            log.warning("vad: Silero unavailable (%s) - energy endpointing", error)
            return False
        available = True
        error = None
        log.info("vad: Silero ready (%s)", os.path.basename(getattr(_session, "_model_path", "silero")))
        return True


# Measured LIVE Sep 15 2026: carried forever, the LSTM state collapses in this room within ~20 s - a faint
# "never mind" at the mic scored p 0.001-0.012 on the carried stream against 0.63-0.74 on a fresh one, and the
# command capture never started again (Xyrus "deaf" after answering). Synthetic hiss only sagged it (0.94 -> 0.7).
# So the state is forgotten after QUIET_RESET_S with no window reaching QUIET_P: only in quiet, never mid-speech.
QUIET_P = 0.2                     # clearly quiet: a faint word's onset ramp crosses this fast (0.35 let a
QUIET_RESET_S = 2.0               #   reset land inside the ramp and cost a short word its score)


class SileroStream:
    """push(int16 PCM bytes) -> one probability per whole 512-sample window; the remainder (< 512 samples) is
    carried into the next push. The LSTM state is carried across pushes but forgotten after QUIET_RESET_S of
    quiet (see above). reset() forgets everything (a new utterance after an echo gate / mode change).
    Without a session (available False) push() returns [] - the Endpointer never calls it then."""

    def __init__(self):
        self.ok = warm()
        self._rest = b""
        self._context = None
        self._state = None
        self._quiet = 0                                  # windows in a row below QUIET_P
        self._quiet_max = max(1, round(QUIET_RESET_S * 16000 / WINDOW))
        self.quiet_resets = 0
        self.reset()

    def reset(self) -> None:
        self._rest = b""
        self._forget()

    def _forget(self) -> None:
        """The model state and context only (not the carried remainder)."""
        import numpy as np
        self._quiet = 0
        self._context = np.zeros((1, CONTEXT), dtype=np.float32)
        if _state_names == ("state",):
            self._state = {"state": np.zeros((2, 1, 128), dtype=np.float32)}
        else:
            self._state = {"h": np.zeros(STATE_SHAPE, dtype=np.float32),
                           "c": np.zeros(STATE_SHAPE, dtype=np.float32)}

    def push(self, pcm: bytes) -> list[float]:
        session = _session
        if not self.ok or session is None or not pcm:
            return []
        import numpy as np
        data = self._rest + pcm
        n = len(data) // (WINDOW * 2)
        self._rest = data[n * WINDOW * 2:]
        if not n:
            return []
        audio = np.frombuffer(data[: n * WINDOW * 2], dtype=np.int16).astype(np.float32) / 32768.0
        windows = audio.reshape(n, WINDOW)
        out: list[float] = []
        for w in windows:
            x = np.concatenate([self._context, w[None, :]], axis=1)
            feeds = {"input": x, **self._state}
            with _lock:
                res = session.run(None, feeds)
            prob, rest = res[0], res[1:]
            p = float(np.asarray(prob).reshape(-1)[0])
            out.append(p)
            self._state = dict(zip(_state_names, rest))
            self._context = w[None, -CONTEXT:]
            self._quiet = self._quiet + 1 if p < QUIET_P else 0
            if self._quiet >= self._quiet_max:           # a quiet second: start fresh (see QUIET_RESET_S)
                self._forget()
                self.quiet_resets += 1
        return out

    def run_all(self, pcm: bytes) -> list[float]:
        """A whole clip from a fresh state in one go (the reference the chunked path is checked against)."""
        self.reset()
        out = self.push(pcm)
        self._rest = b""
        return out
