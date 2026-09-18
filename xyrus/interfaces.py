"""Shared types. Every module codes against these. Changing this file requires the lead's OK."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal, Protocol, runtime_checkable

Source = Literal["mic", "typed", "test", "hotkey", "routine", "alert", "ipc"]
ListenMode = Literal["wake", "command", "confirm", "when", "free", "paused"]
EngineMode = Literal["idle", "arming", "armed", "follow_up", "dialogue"]
EventKind = Literal["heard", "noise", "cmd", "say", "ask", "alert", "remind", "info", "error"]
ChimeKind = Literal["wake", "alert", "error"]


@dataclass(frozen=True)
class Word:
    text: str
    start: float          # seconds from utterance start
    end: float
    conf: float           # logged only, never used as a gate (§1 non-goals)


@dataclass(frozen=True)
class Transcript:
    text: str                         # grammar result, normalised (§5.6), may contain "[unk]"
    mode: ListenMode                  # mode whose grammar produced `text` ("command" after a wake re-decode)
    source: Source = "mic"
    free_text: str | None = None      # unrestricted re-decode of the same PCM (§5.3), lowercase
    words: tuple[Word, ...] = ()
    peak: float = 0.0                 # max chunk peak 0..1 over the utterance
    t_start: float = 0.0              # time.monotonic() of first chunk
    t_end: float = 0.0
    wake_redecoded: bool = False      # True if produced by wake -> command re-decode


@dataclass(frozen=True)
class ListenSpec:
    """What the recognizer should listen for next. Published by the engine, read by T-rec."""
    mode: ListenMode
    extra_words: tuple[str, ...] = ()  # context words added to the wake/confirm/when grammars
    generation: int = 0                # bumps on every change; T-rec re-reads when it differs
    playback: bool = False             # a song is playing (music keeps Vosk's endpointer open: cap idle utterances)


# Engine methods the recognizer calls by name (getattr(engine, NAME, None); absent = feature off) - hearing
# rebuild, Sep 15 2026: the armed capture saw no speech within arm_timeout_s / Whisper heard the name AND a
# command in one breath ("Xyrus, take a screenshot") -> the engine runs it without a second call.
ON_CAPTURE_TIMEOUT = "on_capture_timeout"
ON_WAKE_WITH_COMMAND = "on_wake_with_command"


@dataclass(frozen=True)
class Event:
    ts: str                            # "HH:MM:SS"
    kind: EventKind
    text: str
    meta: str = ""                     # e.g. screenshot path, "noise"


@dataclass(frozen=True)
class DialogueView:
    kind: str                          # event|reminder|task|note|memory|forget|name|confirm|timer|play|pick
    state: str                         # title|when|time|confirm|fix|pick|free|parked
    question: str
    hint: str
    slots: dict[str, str]              # display strings: {"title": "Dentist", "date": "tomorrow", "time": "3 PM"}
    seconds_left: int                  # 0 until the question finished speaking
    listening: ListenMode
    parked: bool = False


@dataclass(frozen=True)
class TimerView:
    name: str                          # "tea" or "5 minute"
    remaining_s: int
    ringing: bool = False


@dataclass(frozen=True)
class EngineSnapshot:
    mode: EngineMode
    listen: ListenMode
    paused: bool
    status: str                        # "listening" | "paused" | "mic error, retrying" | "starting" (set by app)
    window_left_s: int                 # armed / follow-up seconds left (0 otherwise)
    speaking: bool
    dialogue: DialogueView | None
    timers: tuple[TimerView, ...]
    next_event: str | None             # "Dentist at 3 PM · in 2 h"
    last_reply: str
    events: tuple[Event, ...]          # newest last, max 300
    missed: int
    store_version: int                 # CalendarStore.version, for UI refresh


@runtime_checkable
class Clock(Protocol):
    def now(self) -> dt.datetime: ...          # local naive wall clock
    def mono(self) -> float: ...               # monotonic seconds


class Speaker(Protocol):
    def say(self, text: str, *, priority: int = 1, force: bool = False,
            on_done: Callable[[], None] | None = None) -> None:
        """Queue text. priority 0 = alerts (spoken before queued priority-1 replies, never interrupts).
        force=True speaks even when config.voice_replies is False. on_done is called exactly once, from
        T-speak, when the text finished, was purged, or was skipped (voice off) — immediately in that case."""
    def stop(self) -> None: ...                # purge current + queued (D10)
    def is_speaking(self) -> bool: ...
    def is_quiet_at(self, t_mono: float) -> bool:
        """False if t_mono falls inside [start-0.05, end+0.35] of any of the last 8 speech intervals
        (the open interval has end = +inf)."""
    def voices(self) -> list[str]: ...         # SAPI descriptions, e.g. "Microsoft Zira Desktop"
    def set_voice(self, name: str | None) -> None: ...
    def set_rate(self, rate: int) -> None: ... # -10..10


class Chime(Protocol):
    def play(self, kind: ChimeKind) -> None: ...   # async, returns in ~1 ms, safe from any thread
    def stop(self) -> None: ...


class Notifier(Protocol):
    def notify(self, title: str, body: str) -> None: ...  # toast; safe from any thread; never raises


class Executor(Protocol):
    def submit(self, fn: Callable[[], Any], *, timeout: float = 15.0, name: str = "",
               on_done: Callable[[Any, BaseException | None], None] | None = None) -> None:
        """Run fn on T-exec. on_done(result, error) is called on T-exec (the engine wraps it to post
        back to T-engine). Timeout -> on_done(None, TimeoutError(name)); the stuck worker is abandoned."""


class SystemActions(Protocol):
    """Every side effect on Windows. WinActions = real; testing.FakeActions records calls.
    Methods may block (run them via ctx.do). They raise on failure; they never speak."""
    # power
    def shutdown(self, delay_s: int) -> None: ...
    def restart(self, delay_s: int) -> None: ...
    def abort_shutdown(self) -> None: ...
    def sleep(self) -> None: ...
    def hibernate(self) -> bool: ...
    def lock(self) -> None: ...
    def sign_out(self) -> None: ...
    def screen_off(self) -> None: ...
    def screen_on(self) -> None: ...
    def keep_awake(self, on: bool) -> None: ...
    # sound + media
    def volume_get(self) -> int: ...
    def volume_set(self, pct: int) -> None: ...
    def volume_step(self, delta_pct: int) -> int: ...     # returns new level
    def mute_get(self) -> bool: ...
    def mute_set(self, muted: bool) -> None: ...
    def media(self, key: Literal["play_pause", "next", "prev", "stop"]) -> None: ...
    def app_volume(self, app: str, pct: int | None = None, mute: bool | None = None) -> bool: ...
    # display
    def brightness_get(self) -> int | None: ...
    def brightness_set(self, value: int | str) -> int | None: ...   # 50 or "+15"; None if unsupported
    def set_theme(self, dark: bool) -> None: ...
    # windows
    def windowed_apps(self) -> list[str]: ...              # process names, lowercase, no ".exe"
    def focus_app(self, app: str) -> bool: ...
    def close_app(self, app: str) -> bool: ...
    def kill_app(self, app: str) -> bool: ...
    def window_cmd(self, cmd: Literal["minimize", "maximize", "restore", "close", "topmost", "notopmost"]) -> None: ...
    def snap(self, side: Literal["left", "right"]) -> None: ...
    def show_desktop(self) -> None: ...
    def desktop(self, cmd: Literal["new", "next", "prev", "close"]) -> None: ...
    def foreground_is_self(self) -> bool: ...
    # apps / web
    def open_target(self, target: str) -> None: ...        # exe / URL / URI / shell: / .lnk path
    def find_app(self, spoken: str) -> tuple[str, str] | None: ...  # Start Menu index -> (name, lnk path)
    def find_song(self, query: str, alternates: tuple[str, ...] = (), bn_text: str | None = None) -> tuple: ...  # SongPick (spoken, url, video title); LookupError / OSError
    def weather(self, city: str) -> dict: ...
    # input
    def keys(self, chord: str) -> None: ...                # "ctrl+shift+t", "enter", "f11"
    def type_text(self, text: str) -> None: ...
    def run_command(self, cmdline: str) -> None: ...       # routine `run` step (§3.10); lead-approved addition
    # songs (lead-approved additions, v1 autoplay port)
    def browser_playing(self) -> bool: ...                 # a browser is making sound right now (pycaw)
    def ensure_playing(self, video_title: str, trust_state: bool = True) -> str: ...  # autoplay|started|blocked|no-window
    def find_songs(self, query: str, limit: int = 6) -> list[tuple[str, str, str]]: ...  # ranked (spoken, url, video title), for "not that one"
    # clipboard
    def clip_get(self) -> str: ...
    def clip_set(self, text: str) -> None: ...
    # info
    def system_status(self) -> dict: ...                   # {"cpu":12,"mem":48,"battery":None|{"pct":80,"plugged":True},"uptime_s":11400}
    def disks(self) -> list[dict]: ...                     # [{"drive":"C","free_gb":120,"total_gb":480,"pct":75}]
    def top_processes(self, n: int = 3) -> list[str]: ...
    def screenshot(self, window: bool = False) -> Path: ...
    def last_screenshot(self) -> Path | None: ...


class Store(Protocol):
    """CalendarStore surface, lifted from v1 D:/arc/calendar_store.py (§4.10). Thread-safe (RLock).
    Items (events and to-dos) share one list; item/note dicts follow §6.2."""
    version: int                       # bumps on every save; the UI polls it
    lock: Any
    def add_event(self, title: str, date: dt.date | str, time: dt.time | str | None = None,
                  reminder_min: int | None = None, repeat: str | None = None, source: str = "ui",
                  now: dt.datetime | None = None) -> dict: ...
    def add_task(self, title: str, date: dt.date | str, time: dt.time | str | None = None,
                 reminder_min: int | None = None, source: str = "ui", now: dt.datetime | None = None) -> dict: ...
    def get_item(self, item_id: str) -> dict | None: ...
    def update_item(self, item_id: str, **fields: Any) -> dict | None: ...   # moving it re-arms its reminder
    def set_done(self, item_id: str, done: bool = True) -> dict | None: ...
    def delete_item(self, item_id: str) -> bool: ...
    def items_between(self, start: dt.date, end: dt.date, include_done: bool = True) -> list[tuple[dt.date, dict]]: ...
    def open_tasks(self, until: dt.date | None = None) -> list[dict]: ...
    def overdue_tasks(self, today: dt.date) -> list[dict]: ...
    def counts_by_day(self, start: dt.date, end: dt.date) -> dict[dt.date, dict[str, int]]: ...  # event/task/task_open/note
    def next_item(self, now: dt.datetime, days: int = 60) -> tuple[dt.date, dict] | None: ...
    def find(self, text: str, kinds: tuple[str, ...] = ("event", "task"), only_open: bool = False,
             cutoff: float = 0.55) -> dict | None: ...
    def add_note(self, date: dt.date | str, text: str, source: str = "ui", now: dt.datetime | None = None) -> dict: ...
    def update_note(self, note_id: str, text: str) -> dict | None: ...
    def delete_note(self, note_id: str) -> bool: ...
    def notes_on(self, date: dt.date | str) -> list[dict]: ...
    def notes_between(self, start: dt.date, end: dt.date) -> list[dict]: ...
    def clear_notes(self, date: dt.date | str) -> int: ...
    def undo_last(self) -> str | None: ...         # removes the newest voice-added item/note, returns its title
    def get_state(self, key: str, default: Any = None) -> Any: ...
    def set_state(self, key: str, value: Any) -> None: ...
