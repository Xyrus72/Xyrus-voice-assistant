"""Media keys (VK_MEDIA_*) via keys.tap. Never speaks (G10)."""
from __future__ import annotations

from xyrus.actions import keys

MEDIA_VK = {"play_pause": 0xB3, "next": 0xB0, "prev": 0xB1, "stop": 0xB2}


def media(key: str) -> None:
    if key not in MEDIA_VK:
        raise ValueError(f"unknown media key {key!r}")
    keys.tap(MEDIA_VK[key])


def play_pause() -> None: media("play_pause")
def next_track() -> None: media("next")
def prev_track() -> None: media("prev")
def stop() -> None: media("stop")
