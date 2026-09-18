"""Master volume / mute / per-app volume via pycaw (F1). COM is per thread (G5): the executor thread
calls CoInitialize; _com() makes it safe from any other thread too (smoke test). Never speaks (G10)."""
from __future__ import annotations

import logging
import threading

log = logging.getLogger("xyrus.actions.audio")

_tls = threading.local()


def _com() -> None:
    if not getattr(_tls, "com", False):
        import comtypes
        comtypes.CoInitialize()
        _tls.com = True


def _endpoint():
    _com()
    from pycaw.utils import AudioUtilities     # the 20251023 API (old Activate()+cast raises AttributeError)
    return AudioUtilities.GetSpeakers().EndpointVolume


def _clamp(pct: float) -> int:
    return max(0, min(100, int(round(pct))))


def volume_get() -> int:
    return _clamp(_endpoint().GetMasterVolumeLevelScalar() * 100)


def volume_set(pct: int) -> None:
    _endpoint().SetMasterVolumeLevelScalar(_clamp(pct) / 100.0, None)


def volume_step(delta_pct: int) -> int:
    """± delta, clamped 0..100; returns the new level. Exact pycaw control only.
    SPEC-AMBIGUITY: §3.3 allowed a VK-tap fallback on COMError; the lead's 2026-09-13 requirement says level
    changes must never brute-force media keys, so a COM error propagates (ACTION_FAILED)."""
    ep = _endpoint()
    new = _clamp(ep.GetMasterVolumeLevelScalar() * 100 + delta_pct)
    ep.SetMasterVolumeLevelScalar(new / 100.0, None)
    return new


def mute_get() -> bool:
    return bool(_endpoint().GetMute())


def mute_set(muted: bool) -> None:
    """Explicit state, never a blind toggle (D13)."""
    _endpoint().SetMute(1 if muted else 0, None)


def _norm(name: str) -> str:
    n = name.lower().replace(" ", "")
    return n[:-4] if n.endswith(".exe") else n


def app_volume(app: str, pct: int | None = None, mute: bool | None = None, *, alias_exe: str | None = None) -> bool:
    """Set volume and/or mute on every audio session whose process matches app (or alias_exe).
    Returns False when no session matches."""
    _com()
    from pycaw.utils import AudioUtilities
    want = {_norm(app)}
    if alias_exe:
        want.add(_norm(alias_exe))
    hit = False
    for s in AudioUtilities.GetAllSessions():
        proc = getattr(s, "Process", None)
        if proc is None:
            continue
        try:
            pname = _norm(proc.name())
        except Exception:
            continue
        if not any(w and (pname == w or pname.startswith(w)) for w in want):
            continue
        vol = s.SimpleAudioVolume
        if pct is not None:
            vol.SetMasterVolume(_clamp(pct) / 100.0, None)
        if mute is not None:
            vol.SetMute(1 if mute else 0, None)
        hit = True
    return hit
