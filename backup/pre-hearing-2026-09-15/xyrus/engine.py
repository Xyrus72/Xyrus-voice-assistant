"""The engine state machine (§4.7) and Ctx, the frozen handler surface (§4.8).

Thread: T-engine in the app (threaded=True: every entry point enqueues to the inbox, run_forever dispatches);
the calling thread in tests (threaded=False: calls run inline; callbacks posted during a dispatch run right
after it, in order, like the real inbox). Never touches Vosk/SAPI/Tk/COM, never calls SystemActions directly
(only via ctx.do), never sleeps, never reads the wall clock except through the injected Clock (G8).
"""
from __future__ import annotations

import collections
import dataclasses
import datetime as dt
import functools
import itertools
import logging
import math
import queue
import threading
from typing import Any, Callable, Iterable, Literal

from xyrus import normalize as N
from xyrus import persona
from xyrus import replies as R
from xyrus.interfaces import EngineSnapshot, Event, ListenSpec, Transcript
from xyrus.parsing import SLOT_TYPES, TIMER_NAMES, SlotEnv
from xyrus.registry import Match, compile_pattern, display_phrase
from xyrus.timers import TimerService

log = logging.getLogger("xyrus.engine")

MAX_DEPTH = 3
PUSH_TO_TALK_S = 8
TIMER_RING_MAX_S = 60
TIMER_CHIME_EVERY_S = 3
REMINDER_EVERY_S = 5
GREETING_FOLLOW_UP_S = 20

# follow-up whitelists (§5.5): pattern texts or command names; (phrase, command_name) pairs map a bare phrase
# onto a command that does not register that phrase itself.
TIMER_FOLLOW_UP = ("stop", "okay", "thanks", "thank you", "got it", "snooze")
ALERT_FOLLOW_UP = ("okay", "ok", "got it", "thanks", "thank you", "done", "dismiss", "snooze",
                   "remind me again in <duration>", "what's next", "and after that")
GREETING_FOLLOW_UP = ("what's my day like", "what's next", "thanks")
BRIEFING_FOLLOW_UP = ("yes", "sure", "read it", "read the notes", "no", "no thanks", "and after that", "thanks")
TIMER_IDLE_WORDS = ("cancel", "stop", "timer", "timers", "the", "all")
SHUTDOWN_IDLE_WORDS = ("cancel", "stop", "abort")
ALWAYS_FOLLOW_UP = ("shutdown", "timer", "alert", "added")   # kinds kept even when config.follow_up is off
# command capture (lead, Sep 14): command_window_s = 0 means "no limit" while Whisper hears the command; the Vosk
# fallback (Whisper not loaded / not installed) keeps a 60 s window so room noise can't hold it open for ever
VOSK_WINDOW_S = 60
CAPTURE_BUSY = ("hearing", "thinking")        # recognizer capture states that hold every deadline
QUIET_CANCEL = frozenset({"never mind", "nevermind", "cancel", "cancel that", "cancel it", "nothing", "no",
                          "nope", "none", "no thanks", "nothing thanks", "forget it", "forget about it",
                          "leave it", "not now", "nothing for now"})
_YES_LEAD = frozenset({"yes", "yeah", "yep", "yup", "sure", "okay", "ok", "correct", "right", "absolutely",
                       "definitely", "affirmative"})
_NO_LEAD = frozenset({"no", "nope", "nah", "wrong", "negative"})
_ANSWER_FILLER = frozenset({"please", "sir", "do", "it", "go", "ahead", "thanks", "thank", "you", "that's", "right",
                            "correct", "sure", "yes", "yeah", "okay", "ok", "of", "course", "i", "am", "it's", "is",
                            "not", "now", "way", "no", "don't", "that", "thats", "fine", "good"})
_CANCEL_SKIP = frozenset({"please", "sir", "thanks", "thank", "you", "oh", "um", "uh", "just", "actually"})


# ============================================================================= Ctx
class Ctx:
    """What every command handler receives. All methods must be called on T-engine."""

    def __init__(self, engine: "Engine", source: str = "typed", match: Match | None = None,
                 free_text: str | None = None, depth: int = 0, payload: Any = None):
        self.engine = engine
        self.config = engine.config
        self.clock = engine.clock
        self.actions = engine.actions
        self.store = engine.store
        self.memory = engine.memory
        self.timers = engine.timers
        self.registry = engine.registry            # additive
        self.source = source
        self.match = match
        self.free_text = free_text                 # additive: unrestricted re-decode of this utterance
        self.depth = depth
        self.payload = payload if payload is not None else engine.follow_payload   # additive

    @property
    def sir(self) -> str:
        return persona.sir(self.config)

    def render(self, text: str, **kw: Any) -> str:
        return persona.render(text, self.config, **kw)

    def say(self, text: str, *, force: bool = False, priority: int = 1,
            then: Callable[[], None] | None = None, _kind: str = "say") -> None:
        self.engine._say(text, force=force, priority=priority, then=then, kind=_kind)

    def do(self, fn: Callable[..., Any], *args: Any, then: Callable[[Any], None] | None = None,
           error: Callable[[BaseException], None] | None = None, timeout: float = 15.0, **kw: Any) -> None:
        self.engine._do(fn, args, kw, then, error, timeout)

    def confirm(self, question: str, on_yes: Callable[[], None], on_no: Callable[[], None] | None = None,
                *, window_s: float | None = None) -> None:
        from xyrus.dialogue import ConfirmDialogue
        self.start_dialogue(ConfirmDialogue(self, question, on_yes, on_no, window_s=window_s))

    def ask(self, question: str, slot: str, on_answer: Callable[[Any], None], *, listen: str = "when",
            hint: str = "", ignore: Iterable[str] = frozenset(), **opts: Any) -> None:
        from xyrus.dialogue import AskDialogue
        self.start_dialogue(AskDialogue(self, question, slot, on_answer, listen=listen, hint=hint,
                                        ignore=ignore, **opts))

    def start_dialogue(self, dialogue: Any) -> None:
        self.engine._start_dialogue(dialogue)

    def follow_up(self, kind: str, whitelist: Iterable[Any], *, payload: Any = None,
                  seconds: float | None = None) -> None:
        self.engine._request_window("follow_up", seconds, kind=kind, whitelist=tuple(whitelist), payload=payload)

    def arm(self, seconds: float | None = None) -> None:
        self.engine._request_window("armed", seconds)

    def ui(self, message: str) -> None:
        self.engine.ui_queue.put(message)

    def event(self, kind: str, text: str, meta: str = "") -> None:
        self.engine._event(kind, text, meta)

    def notify(self, title: str, body: str) -> None:
        self.engine._notify(title, body)

    def chime(self, kind: str) -> None:
        self.engine._chime(kind)

    def handle(self, text: str, source: str = "routine") -> None:
        if self.depth >= MAX_DEPTH:
            log.warning("routine nesting deeper than %d: %r", MAX_DEPTH, text)
            self.say(R.ROUTINE_DEPTH)
            return
        self.engine._handle(text, source, None, self.depth + 1)


# ============================================================================= Engine
class Engine:
    def __init__(self, *, config, registry, clock, speaker, chime, actions, executor, store, memory,
                 notifier, ui_queue: "queue.Queue[str]", threaded: bool = True, app_index: Any = None):
        self.config = config
        self.registry = registry
        self.clock = clock
        self.speaker = speaker
        self.chime = chime
        self.actions = actions
        self.executor = executor
        self.store = store
        self.memory = memory
        self.notifier = notifier
        self.ui_queue = ui_queue
        self.threaded = threaded
        self.app_index = app_index

        self.timers = TimerService(clock)
        self.last_reply = ""
        self.last_heard = ""
        self.replies: collections.deque[str] = collections.deque(maxlen=50)
        self.events: collections.deque[Event] = collections.deque(maxlen=300)

        self.mode: Literal["idle", "arming", "armed", "follow_up"] = "idle"
        self.mode_until: float | None = None
        self.follow_kind: str | None = None
        self.follow_whitelist: tuple = ()
        self.follow_payload: Any = None
        self.dialogue: Any = None
        self.paused = False
        self.status = "starting"
        self.missed = 0
        self.shutdown_until: float | None = None    # mono deadline of a shutdown countdown (power commands set it)
        self.on_pause: Callable[[bool], None] | None = None   # app hook (closes/opens the mic)
        # app hook (T3 Recognizer.request_free_decode): fn(tr, cb) -> bool; decodes the utterance's PCM with the
        # unrestricted recognizer and calls cb(text) (any thread, possibly before returning)
        self.request_free_decode: Callable[[Transcript, Callable[[str], None]], bool] | None = None
        self._current_tr: Transcript | None = None
        self._retry_pending: Transcript | None = None
        self._last_prune: dt.date | None = None
        # command capture hooks (app.py wires the recognizer's): Whisper is ready to hear whole commands; the
        # captured utterance was handled (the recognizer may capture again); re-hear an unmatched one as Bangla
        self.capture_available: Callable[[], bool] | None = None
        self.on_capture_handled: Callable[[], None] | None = None
        self.request_language_check: Callable[[Transcript, Callable[[Any], None]], bool] | None = None
        self.capture_state = ""                     # "" | "listening" | "hearing" | "thinking" (recognizer)
        self._ack_pending = False
        self._lang_pending: Transcript | None = None
        self._song_q = False                        # "What should I play?" is open (read by the recognizer)

        self._inbox: "queue.Queue[tuple]" = queue.Queue()
        self._local: collections.deque[Callable[[], None]] = collections.deque()
        self._busy = False
        self._speech_tokens: set[int] = set()
        self._jobs = 0
        self._seq = itertools.count(1)
        self._pending_window: tuple | None = None
        self._alert_queue: list = []
        self._late: list = []
        self._last_reminder_check: float | None = None
        self._timer_last_chime: float | None = None
        self._greet_at: float | None = None
        self._greeted = False
        self._next_event_text: str | None = None
        self._lock = threading.RLock()
        self._spec = ListenSpec(mode="wake", extra_words=(), generation=0)
        self._snap: EngineSnapshot | None = None
        self._publish()

    # ------------------------------------------------------------------ entry points (thread-safe)
    def submit_text(self, text: str, source: str = "typed") -> None:
        if self.threaded:
            self._inbox.put(("text", text, source))
        else:
            self.handle(text, source)

    def submit_transcript(self, tr: Transcript) -> None:
        if self.threaded:
            self._inbox.put(("tr", tr))
        else:
            self.handle_transcript(tr)

    def submit_hotkey(self, name: str) -> None:
        self.post(lambda: self._hotkey(name))

    def post(self, fn: Callable[[], None]) -> None:
        """Run fn on T-engine (after the current dispatch when called from it)."""
        if self.threaded:
            self._inbox.put(("fn", fn))
        else:
            self._local.append(fn)
            if not self._busy:
                self._dispatch(lambda: None)

    def cancel_dialogue(self) -> None:
        self.post(lambda: self.dialogue.cancel(say=False) if self.dialogue is not None else None)

    def set_paused(self, paused: bool) -> None:
        self.post(lambda: self._set_paused(bool(paused)))

    def set_status(self, status: str) -> None:
        self.post(lambda: setattr(self, "status", status))

    def startup(self, delay_s: float = 0.0) -> None:
        """Schedule the startup greeting (the app calls this once the mic reports listening, or after 5 s)."""
        self.post(lambda: setattr(self, "_greet_at", self.clock.mono() + delay_s))

    def set_capture_state(self, state: str) -> None:
        """Recognizer hook: '' (not capturing) | 'listening' (waiting for speech) | 'hearing' (recording) |
        'thinking' (Whisper decoding). While hearing/thinking no window or question deadline expires."""
        self.post(lambda: self._set_capture_state(str(state or "")))

    def song_question_open(self) -> bool:
        """Recognizer hook (any thread): "What should I play?" is waiting for its answer."""
        return self._song_q

    def capture_on(self) -> bool:
        fn = self.capture_available
        try:
            return bool(fn is not None and fn())
        except Exception:
            return False

    def command_window(self) -> float:
        """Seconds to wait for the command after the wake reply; 0 = no limit (needs the Whisper capture)."""
        secs = float(self.config.get("command_window_s", 0) or 0)
        if secs <= 0 and not self.capture_on():
            secs = VOSK_WINDOW_S
        return max(0.0, secs)

    # ------------------------------------------------------------------ synchronous (T-engine / tests)
    def handle(self, text: str, source: str = "typed", *, free_text: str | None = None) -> None:
        """THE single entry for text."""
        self._dispatch(lambda: self._handle(text, source, free_text, 0))

    def handle_transcript(self, tr: Transcript) -> None:
        self._dispatch(lambda: self._handle_transcript(tr))

    def tick(self) -> None:
        self._dispatch(self._tick)

    def run_forever(self, stop: threading.Event) -> None:
        last_tick = self.clock.mono()
        while not stop.is_set():
            try:
                item = self._inbox.get(timeout=0.25)
            except queue.Empty:
                item = None
            if item is not None:
                try:
                    kind = item[0]
                    if kind == "text":
                        self.handle(item[1], item[2])
                    elif kind == "tr":
                        self.handle_transcript(item[1])
                    elif kind == "fn":
                        self._dispatch(item[1])
                except Exception:
                    log.exception("engine inbox item failed")
            mono = self.clock.mono()
            if mono - last_tick >= 0.25:
                last_tick = mono
                self.tick()

    # ------------------------------------------------------------------ read side (thread-safe)
    def snapshot(self) -> EngineSnapshot:
        if not self.threaded:
            return self._build_snapshot()
        with self._lock:
            snap = self._snap
        return dataclasses.replace(snap, speaking=self._speaking_now())

    def listen_spec(self) -> ListenSpec:
        with self._lock:
            return self._spec

    def window_left(self) -> int:
        if self.mode in ("armed", "follow_up") and self.mode_until is not None:
            return max(0, math.ceil(self.mode_until - self.clock.mono() - 1e-9))
        return 0

    # ================================================================== internals
    def _dispatch(self, fn: Callable[[], None]) -> None:
        if self._busy:                     # re-entrant call from inside a dispatch
            fn()
            return
        self._busy = True
        try:
            try:
                fn()
            except Exception:
                log.exception("engine dispatch failed")
            while self._local:
                cb = self._local.popleft()
                try:
                    cb()
                except Exception:
                    log.exception("posted callback failed")
                self._maybe_activate_window()
            self._maybe_activate_window()
        finally:
            self._busy = False
        self._publish()

    def _wake_words(self) -> list[str]:
        return list(self.config.get("wake_words") or N.WAKE_ALIASES)

    def _ctx(self, source: str = "typed", match: Match | None = None, free_text: str | None = None,
             depth: int = 0, payload: Any = None) -> Ctx:
        return Ctx(self, source, match, free_text, depth, payload)

    def _env(self, free_text: str | None) -> SlotEnv:
        return SlotEnv(config=self.config, now=self.clock.now(), free_text=free_text, app_index=self.app_index)

    # ------------------------------------------------------------------ events / output
    def _event(self, kind: str, text: str, meta: str = "") -> None:
        ev = Event(ts=self.clock.now().strftime("%H:%M:%S"), kind=kind, text=text, meta=meta)
        with self._lock:
            self.events.append(ev)
        if kind == "noise":
            log.debug("noise: %s %s", text, meta)
        else:
            log.info("%s: %s%s", kind, text, f" ({meta})" if meta else "")

    def _noise(self, text: str, why: str = "") -> None:
        self._event("noise", text, why)

    def _notify(self, title: str, body: str) -> None:
        try:
            self.notifier.notify(title, body)
        except Exception:
            log.exception("notify failed")

    def _chime(self, kind: str) -> None:
        try:
            self.chime.play(kind)
        except Exception:
            log.exception("chime failed")

    def _speaking_now(self) -> bool:
        try:
            return bool(self.speaker.is_speaking())
        except Exception:
            return False

    def _say(self, text: str, *, force: bool = False, priority: int = 1,
             then: Callable[[], None] | None = None, kind: str = "say") -> None:
        text = persona.render(text or "", self.config).strip()
        if not text:
            if then is not None:
                self.post(then)
            return
        self.last_reply = text
        self.replies.append(text)
        self._event(kind, text)
        token = next(self._seq)
        self._speech_tokens.add(token)

        def on_done() -> None:
            self.post(lambda: self._speech_done(token, then))

        try:
            self.speaker.say(text, priority=priority, force=force, on_done=on_done)
        except Exception:
            log.exception("speaker.say failed")
            self._speech_tokens.discard(token)
            if then is not None:
                self.post(then)

    def _speech_done(self, token: int, then: Callable[[], None] | None) -> None:
        self._speech_tokens.discard(token)
        if then is not None:
            try:
                then()
            except Exception:
                log.exception("speech-done callback failed")

    def _do(self, fn, args, kw, then, error, timeout) -> None:
        name = getattr(fn, "__name__", "job")
        self._jobs += 1

        def job():
            return fn(*args, **kw)

        def on_done(result, err):
            self.post(lambda: self._job_done(name, result, err, then, error))

        try:
            self.executor.submit(job, timeout=timeout, name=name, on_done=on_done)
        except Exception as e:
            log.exception("executor.submit failed")
            self.post(lambda: self._job_done(name, None, e, then, error))

    def _job_done(self, name, result, err, then, error) -> None:
        self._jobs = max(0, self._jobs - 1)
        try:
            if err is not None:
                if error is not None:
                    error(err)
                else:
                    log.error("action %s failed: %r", name, err)
                    self._event("error", f"{name} failed: {err}")
                    self._say(R.TOO_SLOW if isinstance(err, TimeoutError) else R.ACTION_FAILED)
            elif then is not None:
                then(result)
        except Exception:
            log.exception("callback of %s failed", name)
            self._say(R.ACTION_FAILED)

    # ------------------------------------------------------------------ windows
    def _request_window(self, window: str, seconds: float | None, *, kind: str | None = None,
                        whitelist: tuple = (), payload: Any = None) -> None:
        """window: 'armed' | 'follow_up'; kind: the follow-up kind (§5.5)."""
        fu_kind = kind
        if window == "armed":
            secs = float(seconds) if seconds is not None else self.command_window()
            if self.dialogue is None:
                self.mode = "arming"
            self._pending_window = ("armed", secs, None, (), None)
            return
        if not self.config.get("follow_up", True) and fu_kind not in ALWAYS_FOLLOW_UP:
            return
        secs = float(seconds if seconds is not None else self.config.get("follow_up_s", 8))
        self._pending_window = ("follow_up", secs, fu_kind, tuple(whitelist), payload)

    def _maybe_activate_window(self) -> None:
        if self._pending_window is None or self._speech_tokens or self._jobs or self.dialogue is not None:
            return
        kind, secs, fu_kind, wl, payload = self._pending_window
        self._pending_window = None
        self.mode_until = self.clock.mono() + secs if secs > 0 else None      # None: no limit
        if kind == "armed":
            self.mode = "armed"
            self.follow_kind, self.follow_whitelist, self.follow_payload = None, (), None
        else:
            self.mode = "follow_up"
            self.follow_kind, self.follow_whitelist, self.follow_payload = fu_kind, wl, payload

    def _close_window(self) -> None:
        self.mode = "idle"
        self.mode_until = None
        self.follow_kind, self.follow_whitelist, self.follow_payload = None, (), None

    def _expire_window(self) -> None:
        if (self.mode in ("armed", "follow_up") and self.mode_until is not None
                and self.clock.mono() >= self.mode_until):
            log.debug("%s window closed", self.mode)
            self._close_window()

    # ------------------------------------------------------------------ dialogues
    def _start_dialogue(self, d: Any) -> None:
        old = self.dialogue
        if old is not None and old is not d:
            self.dialogue = None
            try:
                old.cancel(say=False)
            except Exception:
                log.exception("cancelling the previous dialogue failed")
        self._pending_window = None
        self._close_window()
        self.dialogue = d            # the engine holds the reference BEFORE start() (prototype bug)
        d.start()

    def end_dialogue(self, d: Any) -> None:
        """Called by a dialogue when it finished, was cancelled or abandoned."""
        if self.dialogue is d:
            self.dialogue = None
            if self.mode == "arming" and self._pending_window is None:
                self.mode = "idle"
            self._flush_alerts()

    # ------------------------------------------------------------------ routing
    def _handle_transcript(self, tr: Transcript) -> None:
        if getattr(tr, "captured", False):
            self._handle_captured(tr)
            return
        if self.paused and tr.source == "mic":
            self._noise(tr.text, "paused")
            return
        text = N.normalize(tr.text or "", self._wake_words())
        toks = text.split()
        in_free = self.dialogue is not None and getattr(self.dialogue, "listen_mode", "") == "free"
        if not toks or (all(t == "[unk]" for t in toks) and not (in_free and tr.free_text)):
            return
        if tr.source == "mic" and tr.peak < float(self.config.get("min_speech_peak", 0.02)):
            self._noise(text, f"peak {tr.peak:.4f}")
            return
        self._current_tr = tr
        try:
            self._handle(tr.text, tr.source or "mic", tr.free_text, 0)
        finally:
            self._current_tr = None

    def _handle(self, text: str, source: str, free_text: str | None, depth: int) -> None:
        wakes = self._wake_words()
        norm = N.normalize(text or "", wakes)
        if not norm:
            return
        if free_text is not None:
            free_text = N.normalize(free_text, wakes)
        had_wake, rest = N.strip_wake(norm, wakes)
        display = rest if had_wake else norm
        if self.paused and source == "mic":
            self._noise(norm, "paused")
            return
        self._expire_window()

        if self.dialogue is not None:
            answer = rest if had_wake else norm
            if not answer.strip() and not free_text:
                self._noise(norm, "dialogue: bare wake")
                return
            if not self._command_instead_of_answer(answer, free_text, explicit=had_wake or source != "mic"):
                self._event("heard", norm)
                self.last_heard = display
                try:
                    self.dialogue.handle(answer, free_text=free_text, source=source)
                except Exception:
                    log.exception("dialogue.handle failed")
                    d = self.dialogue
                    if d is not None:
                        d.cancel(say=False)
                    self._say(R.ACTION_FAILED)
                return
            # the user moved on ("volume up" while "…correct?" was open): drop the question quietly, run the command
            d = self.dialogue
            self._event("info", "question dropped for a new command")
            d.cancel(say=False)
            if self.dialogue is d:
                self.dialogue = None
            had_wake, rest = True, answer

        if source != "mic":
            had_wake = True                    # typed / routine / test: the wake word is optional
        if had_wake:
            heard = self._route_wake(rest, norm, source, free_text, depth)
        elif self.mode in ("armed", "arming"):
            heard = self._route_armed(norm, source, free_text, depth)
        elif self.mode == "follow_up":
            heard = self._route_follow_up(norm, source, free_text, depth)
        else:
            heard = self._route_idle(norm, source, free_text, depth)
        if heard:
            self.last_heard = display

    def _route_wake(self, rest: str, norm: str, source: str, free_text: str | None, depth: int) -> bool:
        toks = rest.split()
        unk = sum(1 for t in toks if t == "[unk]")
        if not toks or (len(toks) == 1 and unk == 1):
            self._event("heard", norm)
            self._bare_wake()
            return True
        if self.mode == "follow_up":
            self._close_window()
        m = self._match(rest, free_text, "full")
        self._event("heard", norm)
        if m is None and free_text:
            m = self._match_free(free_text)
        if m is not None and self._garbled_free(m, free_text, source) and self._request_retry():
            return True
        if m is None and free_text is None and source == "mic" and self._request_retry():
            return True
        if m is not None:
            self._run(m, source, free_text, depth)
        else:
            self._not_heard(rest, source)
        return True

    def _route_armed(self, norm: str, source: str, free_text: str | None, depth: int) -> bool:
        words = [t for t in norm.split() if t != "[unk]"]
        if not words:
            self._noise(norm, "armed: unk only")
            return False
        m = self._match(norm, free_text, "full")
        if m is not None and not m.command.armed_ok_single_word and len(words) <= 1:
            m = None                       # 1-word copy/paste/undo... needs the wake word in the same utterance
        if m is not None:
            self._event("heard", norm)
            if self._garbled_free(m, free_text, source) and self._request_retry():
                return True
            self._run(m, source, free_text, depth)
            return True
        if len(words) <= 1:
            self._noise(norm, "armed: one word, no match")
            return False
        self._event("heard", norm)
        m = self._match_free(free_text) if free_text else None
        if m is not None:
            self._run(m, source, free_text, depth)
            return True
        if free_text is None and source == "mic" and self._request_retry():
            return True
        self._not_heard(norm, source)
        return True

    def _route_follow_up(self, norm: str, source: str, free_text: str | None, depth: int) -> bool:
        # a match from the active whitelist is explicit and always accepted ("stop" during a countdown);
        # the single-word rule applies only to the idle-scope fallback
        m = self._match_follow_up(norm, free_text)
        if m is None:
            m = self._match(norm, free_text, "idle")
            if m is not None and not m.command.armed_ok_single_word and len(norm.split()) <= 1:
                m = None
        if m is None:
            self._noise(norm, f"follow-up {self.follow_kind}: no match")
            return False
        self._event("heard", norm)
        self._run(m, source, free_text, depth)
        return True

    def _route_idle(self, norm: str, source: str, free_text: str | None, depth: int) -> bool:
        m = self._match(norm, free_text, "idle")
        if m is None:
            self._noise(norm, "idle: no wake word")
            return False
        self._event("heard", norm)
        self._run(m, source, free_text, depth)
        return True

    def _match(self, text: str, free_text: str | None, scope: str, follow_up: Iterable[str] | None = None):
        return self.registry.match(text, free_text=free_text, scope=scope, env=self._env(free_text),
                                   engine=self, follow_up=follow_up)

    _FREE_SLOTS = frozenset({"song", "query", "text", "name", "app", "free_text"})

    def _command_instead_of_answer(self, answer: str, free_text: str | None, *, explicit: bool) -> bool:
        """While a question is open: is this clearly a new, fully heard command rather than an answer?
        Only for questions that expect short answers (yes/no, a day, a choice) - never when the question
        wants free text (a title, a song name), where anything the user says is the answer.
        explicit = said with the wake word, or typed: any clean match counts. Without the wake word the bar
        is higher, because room noise decodes to words ("how", "play minute you"): at least two words, an
        exact phrase with nothing left over, and no free-text slot."""
        d = self.dialogue
        if d is None or getattr(d, "listen_mode", "") == "free" or getattr(d, "state", "") == "title":
            return False
        toks = answer.split()
        if not toks or "[unk]" in toks:
            return False
        answers = set(d.answer_words()) if hasattr(d, "answer_words") else set()
        if all(w in answers for w in toks):
            return False                        # "yes", "no", "cancel", "all day" ...
        if not explicit and len(toks) < 2:
            return False                        # one word without the name: an answer or room noise
        m = self._match(answer, free_text, "full")
        if m is None or m.has_unk:
            return False
        if explicit:
            return True
        return m.extra_words == 0 and not (self._FREE_SLOTS & set(m.slots))

    def _match_follow_up(self, norm: str, free_text: str | None):
        names = []
        for entry in self.follow_whitelist:
            if isinstance(entry, (tuple, list)) and len(entry) == 2:
                phrase, cmd_name = entry
                cmd = self.registry.get(cmd_name)
                if cmd is not None and " ".join(norm.split()) == phrase:
                    return Match(command=cmd, pattern=None, slots={}, text=norm, has_unk=False, extra_words=0)
            else:
                names.append(entry)
        if not names:
            return None
        return self._match(norm, free_text, "follow_up", follow_up=names)

    # ---- free re-decode retry (meaning layer: the command grammar missed, try the full vocabulary)
    def _match_free(self, free_text: str | None):
        if not free_text:
            return None
        words = free_text.split()
        wakes = set(self._wake_words()) | N.WAKE_LIKE
        i = 0
        while i < len(words) and i < 3 and (words[i] in wakes or words[i] in N.GREETING_LEAD):
            i += 1
        rest = " ".join(words[i:])
        return self._match(rest, free_text, "full") if rest else None

    @staticmethod
    def _garbled_free(m: Match, free_text: str | None, source: str) -> bool:
        """A free slot (song/query/text) captured from a grammar result with [unk] and no re-decode."""
        return (source == "mic" and free_text is None and m.has_unk and m.pattern is not None
                and m.pattern.free_slot in ("song", "query", "text"))

    def _request_retry(self) -> bool:
        tr, hook = self._current_tr, self.request_free_decode
        if tr is None or hook is None or tr.free_text is not None or (tr.source or "mic") != "mic":
            return False
        self._retry_pending = tr

        def cb(text: str) -> None:
            self.post(lambda: self._free_retry_done(tr, text or ""))

        try:
            ok = bool(hook(tr, cb))
        except Exception:
            log.exception("request_free_decode failed")
            ok = False
        if not ok:
            if self._retry_pending is tr:
                self._retry_pending = None
            return False
        self._event("info", "listening again with the full vocabulary")
        return True

    def _free_retry_done(self, tr: Transcript, text: str) -> None:
        if self._retry_pending is not tr:
            return
        self._retry_pending = None
        if self.dialogue is not None or self.paused:
            return
        self._current_tr = None
        self._handle(tr.text, "mic", text, 0)          # free_text is now set: no second retry

    # ---- command capture (lead, Sep 14): Whisper heard the whole command / answer (capture.CapturedTranscript)
    def _set_capture_state(self, state: str) -> None:
        self.capture_state = state

    def _handle_captured(self, tr: Transcript) -> None:
        """One whole utterance heard by Whisper after the wake reply (or as a question's answer). The wake word
        is already given: exactly one command runs and Xyrus is idle again; nothing matched -> "Sorry, I didn't
        catch that" and idle (no loop); "never mind" / "cancel" / "nothing" end it quietly."""
        self._ack_pending = True
        if self.paused:
            self._noise(tr.text, "paused")
            return
        if tr.peak < float(self.config.get("min_speech_peak", 0.02)):
            self._noise(tr.text, f"peak {tr.peak:.4f}")
            return
        if getattr(tr, "language", "en") == "bn":
            self._handle_bangla(tr)
            return
        from xyrus.capture import strip_wake_words
        wakes = self._wake_words()
        norm = N.normalize(tr.text or "", wakes)
        words = strip_wake_words(norm.split(), wakes)
        text = " ".join(words)
        if self.dialogue is not None:
            if not text:
                self._noise(norm, "dialogue: bare wake")
                return
            answer = self._yes_no(text) if getattr(self.dialogue, "listen_mode", "") == "confirm" else text
            had_name = len(words) < len(norm.split())
            self._handle(f"{N.TYPED_WAKE} {answer}" if had_name else answer, "mic", text, 0)
            return
        if self.mode not in ("armed", "arming"):      # one command per call: the call already ended
            self._noise(norm, f"captured while {self.mode}")
            return
        if not text:                                  # just the name again: answer it again
            self._event("heard", norm)
            self._bare_wake()
            return
        self._event("heard", text)
        self.last_heard = text
        if self._quiet_cancel(text):
            self._pending_window = None
            self._close_window()
            self._event("info", "cancelled - back to listening for the name")
            return
        m = self._match(text, text, "full")
        if m is not None and self._unresolved_app(m) and self._request_app_fix(tr, text, m):
            return                                    # "open crow": re-hear the name among the known apps
        if m is not None:
            self._run(m, "mic", text, 0)
            return
        if self._request_lang_check(tr):              # English Whisper may have turned Bangla into words
            return
        self._captured_not_heard()

    @staticmethod
    def _unresolved_app(m: Match) -> bool:
        ref = m.slots.get("app") if m is not None else None
        return ref is not None and getattr(ref, "target", "") is None

    def _request_app_fix(self, tr: Transcript, text: str, m: Match) -> bool:
        """Whisper spelled an app it doesn't know ("open crow" for "open chrome"): the recognizer re-hears the
        kept utterance with the grammar of known apps (request_free_decode's app re-decode - the fix for the
        one-breath "switch to crow"); the better match runs if it is the same command, else the original."""
        hook = self.request_free_decode
        if hook is None:
            return False
        self._lang_pending = tr                       # holds the recognizer's ack like the Bangla check

        def cb(new_text: str) -> None:
            self.post(lambda: self._app_fix_done(tr, m, text, new_text or ""))

        try:
            ok = bool(hook(dataclasses.replace(tr, text=text), cb))
        except Exception:
            log.exception("app re-decode request failed")
            ok = False
        if not ok and self._lang_pending is tr:
            self._lang_pending = None
        return ok

    def _app_fix_done(self, tr: Transcript, m: Match, text: str, new_text: str) -> None:
        if self._lang_pending is not tr:
            return
        self._lang_pending = None
        self._ack_pending = True
        if self.paused:
            return
        from xyrus.capture import strip_wake_words
        wakes = self._wake_words()
        fixed = " ".join(strip_wake_words(N.normalize(new_text, wakes).split(), wakes))
        m2 = self._match(fixed, fixed, "full") if fixed else None
        if m2 is not None and not self._unresolved_app(m2) and m2.command.name == m.command.name:
            self._event("info", f"app name heard again: {fixed}")
            self._run(m2, "mic", fixed, 0)
        else:
            self._run(m, "mic", text, 0)

    def _captured_not_heard(self) -> None:
        self._pending_window = None
        self._close_window()
        self._say(R.NOT_HEARD)                        # no new window: one command per call

    def _quiet_cancel(self, text: str) -> bool:
        t = " ".join(w for w in text.split() if w not in _CANCEL_SKIP)
        if t not in QUIET_CANCEL:
            return False
        if t.startswith("cancel") and self.shutdown_until is not None and self.clock.mono() < self.shutdown_until:
            return False                              # "cancel" during a shutdown countdown cancels the shutdown
        return True

    @staticmethod
    def _yes_no(text: str) -> str:
        """'yeah do it' -> 'yes', 'no not now' -> 'no' (Whisper writes whole answers, the dialogues want the word)."""
        words = text.split()
        if not words or text in N.YES_WORDS or text in N.NO_WORDS:
            return text
        if all(w in _ANSWER_FILLER for w in words[1:]):
            if words[0] in _YES_LEAD:
                return "yes"
            if words[0] in _NO_LEAD:
                return "no"
        return text

    def _request_lang_check(self, tr: Transcript) -> bool:
        hook = self.request_language_check
        if hook is None or getattr(tr, "language", "en") != "en":
            return False
        self._lang_pending = tr

        def cb(new_tr: Any) -> None:
            self.post(lambda: self._lang_check_done(tr, new_tr))

        try:
            ok = bool(hook(tr, cb))
        except Exception:
            log.exception("request_language_check failed")
            ok = False
        if not ok:
            if self._lang_pending is tr:
                self._lang_pending = None
            return False
        self._event("info", "listening again for Bangla")
        return True

    def _lang_check_done(self, tr: Transcript, new_tr: Any) -> None:
        if self._lang_pending is not tr:
            return
        self._lang_pending = None
        self._ack_pending = True
        if self.paused:
            return
        if new_tr is not None and getattr(new_tr, "language", "") == "bn":
            self._handle_bangla(new_tr)
        else:
            self._captured_not_heard()

    def _handle_bangla(self, tr: Transcript) -> None:
        """A Bengali-script transcript: a song request ("প্লে আমার ভিনদেশী তারা", "আমার ভিনদেশী তারা বাজাও") or
        the answer to "What should I play?". The title stays in Bengali script (searched first, the Banglish
        second - actions/web.find_song); replies speak the Latin / Banglish form."""
        from xyrus import banglish
        from xyrus.registry import Match as _Match
        text = tr.free_text or tr.text or ""
        d = self.dialogue
        song_q = d is not None and getattr(d, "kind", "") == "play"
        title = banglish.song_request(text, hinted=bool(getattr(tr, "song_hint", False)) or song_q)
        shown = banglish.transliterate(text)
        self._event("heard", f"{text} ({shown})")
        self.last_heard = shown
        if d is not None:
            try:
                if song_q:
                    if title:
                        d.handle(title, free_text=title, source="mic")
                    return                            # "একটা গান বাজাও" while being asked: keep waiting
                d.handle(shown, free_text=shown, source="mic")
            except Exception:
                log.exception("dialogue.handle failed (Bangla answer)")
            return
        if self.mode not in ("armed", "arming"):      # one command per call: the call already ended
            self._noise(text, f"captured while {self.mode}")
            return
        if title is None:
            self._captured_not_heard()
            return
        cmd = self.registry.get("play_song" if title else "play_ask")
        if cmd is None:
            self._captured_not_heard()
            return
        m = _Match(command=cmd, pattern=cmd.patterns[0] if cmd.patterns else None,
                   slots={"song": title} if title else {}, text=f"play {title}".strip(), has_unk=False,
                   extra_words=0)
        self._run(m, "mic", title or None, 0)

    def _bare_wake(self) -> None:
        self._pending_window = None
        self._close_window()
        self._armed_misses = 0
        self.mode = "arming"
        options = self.config.get("wake_replies") or list(R.WAKE_REPLIES)
        self._say(persona.pick("wake_reply", options, self.config))
        self._request_window("armed", None)

    def _not_heard(self, text: str, source: str) -> None:
        close = self.registry.close_phrase(text)
        ctx = self._ctx(source)
        if close:
            def yes():
                self._run_text(close, source)

            def no():
                self._say(R.OKAY)
                self._request_window("armed", None)
            ctx.confirm(persona.render(R.DID_YOU_MEAN, self.config, phrase=close), yes, no)
            return
        self._say(R.NOT_HEARD)
        # a second miss in a row closes the window: with a 60 s window, room noise that decodes to words must
        # not keep "Sorry, I didn't catch that" going
        self._armed_misses = getattr(self, "_armed_misses", 0) + 1
        if self._armed_misses < 2:
            self._request_window("armed", None)

    def _run_text(self, text: str, source: str, depth: int = 0) -> None:
        m = self._match(N.normalize(text), None, "full")
        if m is None:
            self._say(R.NOT_HEARD)
            return
        self._run(m, source, None, depth)

    def _run(self, m: Match, source: str, free_text: str | None, depth: int) -> None:
        cmd = m.command
        payload = self.follow_payload if self.mode == "follow_up" else None   # captured before closing
        self._pending_window = None
        if self.mode != "idle":
            self._close_window()
        if cmd.destructive and m.has_unk:
            phrase = display_phrase(cmd.patterns[0]) if cmd.patterns else cmd.name.replace("_", " ")
            ctx = self._ctx(source, m, free_text, depth, payload)
            clean = dataclasses.replace(m, has_unk=False)
            ctx.confirm(persona.render(R.DID_YOU_SAY, self.config, phrase=phrase),
                        lambda: self._invoke(clean, source, free_text, depth, payload))
            return
        self._invoke(m, source, free_text, depth, payload)

    def _invoke(self, m: Match, source: str, free_text: str | None, depth: int, payload: Any = None) -> None:
        cmd = m.command
        ctx = self._ctx(source, m, free_text, depth, payload)
        slots = " ".join(f"{k}={v!r}" for k, v in m.slots.items() if k != "free_text")
        self._event("cmd", f"{cmd.name} {slots}".strip())
        try:
            cmd.handler(ctx, m)
        except Exception:
            log.exception("command %s failed", cmd.name)
            self._event("error", f"{cmd.name} failed")
            self._say(R.ACTION_FAILED)

    # ------------------------------------------------------------------ pause / hotkeys
    def _set_paused(self, paused: bool) -> None:
        if paused == self.paused:
            return
        self.paused = paused
        if paused:
            self._pending_window = None
            self._close_window()
            d = self.dialogue
            if d is not None and getattr(d, "kind", "") == "confirm":
                d.cancel(say=False)
        if self.on_pause is not None:
            try:
                self.on_pause(paused)
            except Exception:
                log.exception("on_pause hook failed")
        self._event("info", "Paused · mic off" if paused else "Listening")

    def _hotkey(self, name: str) -> None:
        if name == "push_to_talk":
            if self.dialogue is not None or self.paused:
                return
            self._chime("wake")
            self._pending_window = None
            self.mode = "armed"
            self.mode_until = self.clock.mono() + PUSH_TO_TALK_S
        elif name == "stop_speaking":
            try:
                self.speaker.stop()
            except Exception:
                log.exception("speaker.stop failed")
            try:
                self.chime.stop()
            except Exception:
                pass
        elif name == "show_window":
            self.ui_queue.put("show")
        elif name == "pause":
            self._set_paused(not self.paused)

    # ------------------------------------------------------------------ tick
    def _tick(self) -> None:
        mono, now = self.clock.mono(), self.clock.now()
        busy = self.capture_state in CAPTURE_BUSY      # the user is talking / Whisper is decoding: no deadline
        if self.dialogue is not None and not busy:
            try:
                self.dialogue.tick()
            except Exception:
                log.exception("dialogue.tick failed")
        if not busy:
            self._expire_window()
        for tv in self.timers.tick():
            self._timer_fired(tv)
        self._timer_ringing_tick(mono)
        if self._last_reminder_check is None or mono - self._last_reminder_check >= REMINDER_EVERY_S:
            self._last_reminder_check = mono
            self._check_reminders(now)
            self._refresh_next_event(now)
            self._morning_brief_tick(now)
        if self._greet_at is not None and mono >= self._greet_at:
            self._greet_at = None
            self._startup_greeting(now)

    # ---- timers (§3.8)
    def _timer_fired(self, tv) -> None:
        total = self.timers.total_of(tv.name) or 0
        short = _short_duration(total)
        label = "Timer" if not tv.name or tv.name[0].isdigit() else f"{tv.name.capitalize()} timer"
        self._do(self.actions.screen_on, (), {}, None, lambda e: log.warning("screen_on failed: %r", e), 5.0)
        self._chime("alert")
        self._timer_last_chime = self.clock.mono()
        self._notify("Xyrus", f"{label} done — {short}")
        self._event("alert", f"{label} done — {short}")
        self._say(R.TIMES_UP, force=True, priority=0, kind="alert")
        if self.dialogue is None:
            self._request_window("follow_up", TIMER_RING_MAX_S, kind="timer", whitelist=TIMER_FOLLOW_UP,
                                 payload=tv)

    def _timer_ringing_tick(self, mono: float) -> None:
        since = self.timers.ringing_since()
        if since is None:
            if self._timer_last_chime is not None:
                self._timer_last_chime = None
                try:
                    self.chime.stop()
                except Exception:
                    pass
            return
        if mono - since >= TIMER_RING_MAX_S:
            self.timers.stop_ringing()
            self._timer_last_chime = None
            try:
                self.chime.stop()
            except Exception:
                pass
            self._event("info", "timer alarm stopped after a minute")
            return
        if self._timer_last_chime is None or mono - self._timer_last_chime >= TIMER_CHIME_EVERY_S:
            self._chime("alert")
            self._timer_last_chime = mono

    # ---- reminders (§3.9 proactive alerts; reminders.py is T2's)
    def _check_reminders(self, now: dt.datetime) -> None:
        if self.store is None:
            return
        try:
            from xyrus import reminders as REM
        except ImportError:
            return
        try:
            try:
                alerts = REM.due_alerts(self.store, now, cfg=self.config)
            except TypeError:
                alerts = REM.due_alerts(self.store, now)
        except Exception:
            log.exception("due_alerts failed")
            return
        for a in alerts:
            kind = getattr(a, "kind", "due")
            title = (getattr(a, "item", None) or {}).get("title", "")
            if kind == "stale":
                self.missed += 1
                self._event("info", f"missed reminder while away: {title}")
            elif kind == "late":
                self.missed += 1
                self._late.append(a)
            elif self.dialogue is not None:
                self._alert_queue.append(a)
            else:
                self._fire_alert(a, now)
        if self._late and self._greeted and self.dialogue is None:
            self._speak_missed(now)
        self._repeat_unacked(REM, now)
        today = now.date()
        if self._last_prune != today and hasattr(self.store, "prune"):
            self._last_prune = today
            try:
                self.store.prune(today)
            except Exception:
                log.exception("store.prune failed")

    def _repeat_unacked(self, REM, now: dt.datetime) -> None:
        """should: an unacknowledged due alert repeats once after calendar.repeat_unacked_after_min."""
        after = self.config.get("calendar.repeat_unacked_after_min", 2)
        if not after or self.dialogue is not None or not hasattr(REM, "due_repeats"):
            return
        try:
            try:
                reps = REM.due_repeats(self.store, now, after, cfg=self.config)
            except TypeError:
                reps = REM.due_repeats(self.store, now, after)
        except Exception:
            log.exception("due_repeats failed")
            return
        for a in reps:
            try:
                text = REM.repeat_text(a, self.config)
            except Exception:
                log.exception("repeat_text failed")
                continue
            self._chime("alert")
            self._say(text, force=True, priority=0, kind="alert")
            self._request_window("follow_up", self.config.get("follow_up_after_alert_s", 20), kind="alert",
                                 whitelist=ALERT_FOLLOW_UP, payload=a)

    def _speak_missed(self, now: dt.datetime) -> None:
        late, self._late = self._late, []
        try:
            from xyrus import reminders as REM
            text = REM.missed_summary(late, now, self.config)
        except Exception:
            log.exception("missed_summary failed")
            return
        if text:
            self._say(text, force=True, priority=0, kind="alert")

    def _in_quiet_hours(self, now: dt.datetime) -> bool:
        q = self.config.get("quiet_hours")
        if not q:
            return False
        try:
            a = dt.time.fromisoformat(q["from"])
            b = dt.time.fromisoformat(q["to"])
        except Exception:
            return False
        t = now.time()
        return (a <= t < b) if a <= b else (t >= a or t < b)

    def _fire_alert(self, a: Any, now: dt.datetime) -> None:
        try:
            from xyrus import reminders as REM
            text = REM.alert_text(a, now, self.config)
        except Exception:
            log.exception("alert_text failed")
            return
        item = getattr(a, "item", None) or {}
        lead = (item.get("reminder_min") or 0) > 0
        should = getattr(REM, "should_speak", None)
        try:
            quiet = (not should(a, now, self.config)) if should else (lead and self._in_quiet_hours(now))
        except Exception:
            quiet = lead and self._in_quiet_hours(now)
        if quiet:
            self._event("remind", text, "quiet hours")
            return
        self._do(self.actions.screen_on, (), {}, None, lambda e: log.warning("screen_on failed: %r", e), 5.0)
        self._chime("alert")
        if self.config.get("calendar.toast", True):
            self._notify("Xyrus reminder", text)
        self._say(text, force=True, priority=0, kind="alert")
        self._request_window("follow_up", self.config.get("follow_up_after_alert_s", 20), kind="alert",
                             whitelist=ALERT_FOLLOW_UP, payload=a)

    def _flush_alerts(self) -> None:
        if not self._alert_queue:
            return
        queued, self._alert_queue = self._alert_queue, []
        now = self.clock.now()
        for a in queued:
            if self.dialogue is not None:
                self._alert_queue.append(a)
            else:
                self._fire_alert(a, now)

    def _refresh_next_event(self, now: dt.datetime) -> None:
        try:
            nx = self.store.next_item(now) if self.store is not None else None
        except Exception:
            log.exception("next_item failed")
            nx = None
        self._next_event_text = _next_event_label(nx, now) if nx else None

    def _morning_brief_tick(self, now: dt.datetime) -> None:
        """should: once a day at calendar.morning_brief_time when there is something today; not after 11:00."""
        if self.store is None or self.dialogue is not None or self.paused:
            return
        if self._greet_at is not None or (self.threaded and not self._greeted):
            return                 # the startup greeting comes first (it includes today's summary)
        if not self.config.get("calendar.morning_brief", True):
            return
        try:
            t = dt.time.fromisoformat(self.config.get("calendar.morning_brief_time", "08:30"))
        except Exception:
            t = dt.time(8, 30)
        if now.time() < t or now.hour >= 11:
            return
        today = now.date().isoformat()
        try:
            if self.store.get_state("last_brief") == today:
                return
            has_items = bool(self.store.items_between(now.date(), now.date(), include_done=False))
            has_notes = bool(self.store.notes_on(now.date()))
            if not (has_items or has_notes):
                return
            from xyrus import assistant
            text = _call_flex(assistant.briefing, self.store, now, self.config)
        except ImportError:
            return
        except Exception:
            log.exception("morning brief failed")
            return
        self.store.set_state("last_brief", today)
        if isinstance(text, tuple):
            text = text[0]
        if text:
            self._say(text)
            self._request_window("follow_up", 20, kind="briefing", whitelist=BRIEFING_FOLLOW_UP)

    def _startup_greeting(self, now: dt.datetime) -> None:
        self._greeted = True
        style = self.config.get("startup_greeting", "speak")
        late, self._late = self._late, []
        text = None
        try:
            from xyrus import assistant
            text = assistant.startup_summary(now, self.store, late, self.config)
        except (ImportError, AttributeError):
            pass
        except Exception:
            log.exception("startup_summary failed")
        if not text:
            text = f"{persona.greeting(now, self.config)} {R.STARTUP_ONLINE}"
            if late:
                try:
                    from xyrus import reminders as REM
                    text += " " + REM.missed_summary(late, now, self.config)
                except Exception:
                    pass
        try:
            t = dt.time.fromisoformat(self.config.get("calendar.morning_brief_time", "08:30"))
            if self.store is not None and t <= now.time() and now.hour < 11:
                self.store.set_state("last_brief", now.date().isoformat())   # the greeting counts as the brief
        except Exception:
            pass
        if style == "off":
            return
        if style == "toast":
            self._notify("Xyrus", text)
            self._event("info", text)
            return
        self._say(text)
        self._request_window("follow_up", GREETING_FOLLOW_UP_S, kind="greeting", whitelist=GREETING_FOLLOW_UP)

    # ------------------------------------------------------------------ publish (snapshot + listen spec)
    def _compute_spec(self) -> tuple[str, tuple[str, ...]]:
        if self.paused:
            return "paused", ()
        if self.dialogue is not None:
            try:
                words = tuple(self.dialogue.answer_words())
            except Exception:
                words = ()
            return getattr(self.dialogue, "listen_mode", "command"), words
        if self.mode in ("arming", "armed"):
            return "command", ()
        if self.mode == "follow_up":
            return "wake", self._follow_words()
        return "wake", self._idle_extra()

    def _idle_extra(self) -> tuple[str, ...]:
        words: set[str] = set()
        if self.timers.any():
            words.update(TIMER_IDLE_WORDS)
            words.update(TIMER_NAMES)
        if self.shutdown_until is not None and self.clock.mono() < self.shutdown_until:
            words.update(SHUTDOWN_IDLE_WORDS)
        words.update(self._live_context_words())
        return tuple(sorted(words))

    def _live_context_words(self) -> set[str]:
        """Words of the wake-free commands whose context predicate is live right now (cancel timer while a timer
        runs, the shutdown countdown, "not that one" for 40 s after a song starts), so the idle wake grammar can
        actually hear them without the wake word (T6)."""
        out: set[str] = set()
        try:
            cmds = self.registry.commands()
        except Exception:
            return out
        for c in cmds:
            if not getattr(c, "wake_free", False) or getattr(c, "context", None) is None:
                continue
            try:
                live = bool(c.context(self))
            except Exception:
                continue
            if live:
                for p in c.patterns:
                    out.update(_cached_pattern_words(p.text))
        return out

    def _follow_words(self) -> tuple[str, ...]:
        words: set[str] = set()
        for entry in self.follow_whitelist:
            if isinstance(entry, (tuple, list)):
                words.update(str(entry[0]).split())
                continue
            cmd = self.registry.get(entry)
            texts = [p.text for p in cmd.patterns] if cmd is not None else [entry]
            for text in texts:
                words.update(_pattern_words(text))
        return tuple(sorted(words))

    def _publish(self) -> None:
        mode, extra = self._compute_spec()
        d = self.dialogue
        with self._lock:
            if (mode, extra) != (self._spec.mode, self._spec.extra_words):
                self._spec = ListenSpec(mode=mode, extra_words=extra, generation=self._spec.generation + 1)
            self._song_q = d is not None and getattr(d, "kind", "") == "play"
            self._snap = self._build_snapshot()
        if self._ack_pending and self._lang_pending is None:
            self._ack_pending = False                  # the new spec is out: the recognizer may capture again
            hook = self.on_capture_handled
            if hook is not None:
                try:
                    hook()
                except Exception:
                    log.exception("on_capture_handled failed")

    def _build_snapshot(self) -> EngineSnapshot:
        d = self.dialogue
        view = None
        if d is not None:
            try:
                view = d.view()
            except Exception:
                log.exception("dialogue.view failed")
        mode = "dialogue" if d is not None else self.mode
        status = "paused" if self.paused else self.status
        low = str(status or "").lower()
        if not self.paused and self.capture_state in CAPTURE_BUSY and "error" not in low and "no mic" not in low:
            status = self.capture_state               # the header shows "Listening…" / "Thinking…"
        return EngineSnapshot(
            mode=mode, listen=self._compute_spec()[0], paused=self.paused,
            status=status, window_left_s=self.window_left(),
            speaking=self._speaking_now(), dialogue=view, timers=self.timers.views(),
            next_event=self._next_event_text, last_reply=self.last_reply, events=tuple(self.events),
            missed=self.missed, store_version=int(getattr(self.store, "version", 0) or 0))


# ============================================================================= helpers
def _pattern_words(text: str) -> set[str]:
    try:
        p = compile_pattern(text)
    except ValueError:
        return set(text.split())
    words: set[str] = set()
    for kind, val in p.tokens:
        if kind in ("lit", "opt"):
            for alt in val:
                words.update(alt.split())
        elif kind == "slot":
            words.update(SLOT_TYPES[val].words)
    return words


@functools.lru_cache(maxsize=1024)
def _cached_pattern_words(text: str) -> frozenset[str]:
    return frozenset(_pattern_words(text))


def _short_duration(secs: int) -> str:
    """v1 describe_secs: '7 minutes', '1 hour', '90 seconds' (toasts)."""
    secs = int(secs)
    if secs and secs % 3600 == 0:
        n = secs // 3600
        return f"{n} hour{'s' if n != 1 else ''}"
    if secs and secs % 60 == 0:
        n = secs // 60
        return f"{n} minute{'s' if n != 1 else ''}"
    return f"{secs} seconds"


def _next_event_label(nx: Any, now: dt.datetime) -> str | None:
    """'Dentist at 3 PM · in 2 h' / 'Gym · Saturday' for the header and tray tooltip."""
    try:
        day, item = nx
        if isinstance(day, str):
            day = dt.date.fromisoformat(day)
        title = (item.get("title") or "").strip()
        title = title[:1].upper() + title[1:]
        tm = item.get("time")
        if tm:
            t = dt.time.fromisoformat(tm) if isinstance(tm, str) else tm
            start = dt.datetime.combine(day, t)
            label = f"{title} at {persona.speak_time(t)}"
            delta = (start - now).total_seconds()
            if 0 <= delta < 3600:
                return f"{label} · in {max(1, int(delta // 60))} min"
            if 0 <= delta < 12 * 3600:
                return f"{label} · in {int(delta // 3600)} h"
            if day != now.date():
                return f"{label} · {persona.speak_day(day, now.date())}"
            return label
        return f"{title} · {persona.speak_day(day, now.date())}"
    except Exception:
        return None


def _call_flex(fn, store, now, cfg):
    """Call a lifted v1 summary function that may or may not accept the config."""
    try:
        return fn(store, now, cfg)
    except TypeError:
        return fn(store, now)
