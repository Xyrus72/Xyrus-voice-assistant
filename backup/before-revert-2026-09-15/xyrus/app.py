"""Composition root (SPEC §4.15, §9.1): builds the App, starts the threads, runs the window, cleans up.

    python -m xyrus.app [--tray] [--dry-run] [--debug]     the app (arc.py is a 3-line shim for this)
    python -m xyrus.app --text [--speak] [--dry-run]       typed REPL through Engine.handle (python.exe only)
    python -m xyrus.app --selftest [--full]                construct everything with fakes / no mic, drive a few
                                                           commands, exit code (--full: + the unit test suite)
    python -m xyrus.app --wav PATH                         decode a 16 kHz mono WAV in wake/command/free modes

Threads (§4.2): T-main (Tk), T-engine (engine.run_forever), T-rec (Vosk), T-speak (SAPI), T-exec (actions),
T-watch (mic watchdog), T-hotkey, pystray, T-mic (serial mic stop/start for pause), T-index (Start Menu index
refresh), whisper-load (hearing.py). Never prints except in --text / --selftest / --wav (G2).

Test hooks (not user features): --sandbox (no mic, no SAPI, no chime, no tray, no hotkeys, no Whisper,
FakeActions; used by tests/test_app_smoke.py), XYRUS_INSTANCE=<tag> (private single-instance names - v1 holds
the real ones until the switch-over), XYRUS_DATA_DIR (data, config and logs in a temp dir), XYRUS_NO_HEARING=1.
"""
from __future__ import annotations

import argparse
import logging
import os
import queue
import re
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable

from xyrus import paths

log = logging.getLogger("xyrus.app")

INSTANCE_ENV = "XYRUS_INSTANCE"
NO_HEARING_ENV = "XYRUS_NO_HEARING"
GREETING_MAX_WAIT_S = 5.0        # greeting once the recognizer + mic report listening, or after 5 s (§4.15)
INDEX_REFRESH_S = 540            # < StartMenuIndex.REFRESH_S (600): lookups never find it stale and walk inline
MODEL_MISSING = "Speech model missing. Run install.bat."
MIC_LOST = "Microphone lost — reconnecting."
MIC_BACK = "Microphone back."
MIC_NONE = "No microphone found — plug one in."
MODEL_FAILED = "The speech model didn't load — see arc.log."
MIC_ERRORS = ("mic error, retrying", "no microphone")


# ============================================================================= small stand-ins
class NullCapture:
    """AudioCapture surface without an audio device (--selftest, --sandbox, --wav, tests). Nothing is captured
    unless a test calls feed(); start/stop only report status like the real capture ("microphone: <status>")."""
    SAMPLE_RATE = 16000

    def __init__(self, on_status: Callable[[str], None] | None = None):
        self.chunks: "queue.Queue[tuple[bytes, float]]" = queue.Queue(maxsize=200)
        self._on_status = on_status
        self._status = "paused"
        self._running = False
        self.starts = 0
        self.stops = 0

    def _set(self, status: str) -> None:
        if status == self._status:
            return
        self._status = status
        log.info("microphone: %s (no audio device)", status)
        if self._on_status is not None:
            try:
                self._on_status(status)
            except Exception:
                log.exception("on_status callback failed")

    def start(self) -> None:
        self.starts += 1
        self._running = True
        self._set("listening")

    def stop(self) -> None:
        self.stops += 1
        self._running = False
        while True:
            try:
                self.chunks.get_nowait()
            except queue.Empty:
                break
        self._set("paused")

    def restart(self) -> None:
        if self._running:
            self._set("listening")

    def running(self) -> bool:
        return self._running

    def level(self) -> float:
        return 0.0

    def status(self) -> str:
        return self._status

    def close(self) -> None:
        self.stop()

    def feed(self, pcm: bytes) -> None:
        """Tests: push audio as 0.25 s chunks, stamped now."""
        step = self.SAMPLE_RATE // 2          # 0.25 s of int16 = 8000 bytes
        for i in range(0, len(pcm), step):
            self.chunks.put((pcm[i:i + step], time.monotonic()))


class NullTray:
    """Tray + Notifier surface without an icon (sandbox). Toasts are logged and kept in `notes`."""

    def __init__(self):
        self.notes: list[tuple[str, str]] = []
        self.state = "listening"
        self.tooltip = "Xyrus — listening"

    def start(self) -> None: pass
    def stop(self) -> None: pass
    def update_menu(self) -> None: pass

    def set_state(self, state: str) -> None:
        self.state = state

    def set_tooltip(self, text: str) -> None:
        self.tooltip = text

    def notify(self, title: str, body: str) -> None:
        self.notes.append((title, body))
        log.info("toast: %s — %s", title, body)


class NullHotkeys:
    def __init__(self):
        self.bindings: dict[str, str] = {}

    def start(self, timeout: float = 2.0) -> None: pass
    def stop(self, timeout: float = 2.0) -> None: pass

    def status(self) -> dict[str, bool]:
        return {}

    def rebind(self, bindings: dict[str, str]) -> dict[str, bool]:
        self.bindings = dict(bindings or {})
        return {}


class NoShowEvent:
    def poll(self) -> bool:
        return False

    def close(self) -> None:
        pass


class PrintSpeaker:
    """--text: prints every reply; with --speak it is also spoken through SAPI. on_done is called at once (the
    headless engine runs on this thread, so completion must not come back from T-speak)."""

    def __init__(self, inner: Any = None):
        self.inner = inner
        self.said: list[str] = []

    def say(self, text: str, *, priority: int = 1, force: bool = False,
            on_done: Callable[[], None] | None = None) -> None:
        self.said.append(text)
        print(text, flush=True)
        if self.inner is not None:
            try:
                self.inner.say(text, priority=priority, force=force)
            except Exception:
                log.exception("speaking failed")
        if on_done is not None:
            on_done()

    def stop(self) -> None:
        if self.inner is not None:
            self.inner.stop()

    def is_speaking(self) -> bool:
        return False

    def is_quiet_at(self, t_mono: float) -> bool:
        return True

    def voices(self) -> list[str]:
        return self.inner.voices() if self.inner is not None else []

    def set_voice(self, name: str | None) -> None:
        if self.inner is not None:
            self.inner.set_voice(name)

    def set_rate(self, rate: int) -> None:
        if self.inner is not None:
            self.inner.set_rate(rate)

    def close(self) -> None:
        if self.inner is not None:
            self.inner.close()


class PrintNotifier:
    def notify(self, title: str, body: str) -> None:
        print(f"[toast] {title}: {body}", flush=True)


class _Serial:
    """One daemon thread running submitted callables in order (mic stop/start on pause, hotkey rebinds): the
    engine thread never blocks on PortAudio, and a quick pause/resume can't be reordered."""

    def __init__(self, name: str):
        self._q: "queue.Queue[Callable[[], None] | None]" = queue.Queue()
        self._t = threading.Thread(target=self._run, name=name, daemon=True)
        self._t.start()

    def submit(self, fn: Callable[[], None]) -> None:
        self._q.put(fn)

    def _run(self) -> None:
        while True:
            fn = self._q.get()
            if fn is None:
                return
            try:
                fn()
            except Exception:
                log.exception("%s job failed", self._t.name)

    def stop(self, timeout: float = 2.0) -> None:
        self._q.put(None)
        if self._t is not threading.current_thread():
            self._t.join(timeout)


# ============================================================================= helpers
def instance_names() -> tuple[str, str]:
    """(mutex, show-event) names; XYRUS_INSTANCE=<tag> gives tests private ones (v1 holds the real names)."""
    from xyrus import single_instance as si
    tag = re.sub(r"[^A-Za-z0-9_-]", "", os.environ.get(INSTANCE_ENV, ""))[:48]
    if not tag:
        return si.MUTEX_NAME, si.EVENT_NAME
    return f"{si.MUTEX_NAME}-{tag}", f"{si.EVENT_NAME}-{tag}"


def model_ok(model_dir: Path | None = None) -> bool:
    model_dir = Path(model_dir or paths.MODEL_DIR)
    return (model_dir / "am" / "final.mdl").exists()


class CommandHearing:
    """hearing.py as the recognizer's command hearing (Recognizer.set_command_hearing): available(), and
    listen(pcm) -> a handle with text(language, prompt) and language() that share one Whisper encoding
    (hearing.listen). An older hearing.py without listen() gets one transcribe() per decode."""

    def __init__(self, mod: Any):
        self.mod = mod

    def available(self) -> bool:
        try:
            return bool(self.mod.available())
        except Exception:
            return False

    def listen(self, pcm: bytes) -> Any:
        if not pcm or not self.available():
            return None
        fn = getattr(self.mod, "listen", None)
        return fn(pcm) if fn is not None else _PlainHeard(self.mod, pcm)


_DEFAULT_PROMPT = object()


class _PlainHeard:
    def __init__(self, mod: Any, pcm: bytes):
        self.mod, self.pcm = mod, pcm

    def text(self, language: str = "en", prompt: Any = _DEFAULT_PROMPT) -> str:
        kw = {} if prompt is _DEFAULT_PROMPT else {"prompt": prompt}
        return self.mod.transcribe(self.pcm, language=language, **kw)

    def language(self) -> Any:
        fn = getattr(self.mod, "detect_language", None)
        return fn(self.pcm) if fn is not None else None


def plug_hearing(recognizer: Any) -> Any:
    """Whisper for free text (the lead's D:\\arc\\hearing.py): free-text decodes use
    hearing.clean(hearing.transcribe(pcm)) while hearing.available(), else the Vosk free decode.
    A missing or broken hearing.py / model / runtime never breaks startup. Returns the module or None."""
    if os.environ.get(NO_HEARING_ENV):
        return None
    base = str(paths.BASE)
    if base not in sys.path:
        sys.path.insert(0, base)
    try:
        import hearing                                   # noqa: PLC0415 - optional, lead-owned
    except Exception as e:                               # ImportError, OSError (DLLs), SyntaxError ...
        log.info("whisper hearing not available: %r", e)
        return None
    # the capture's Silero VAD (xyrus/vad.py, onnxruntime) is created BEFORE the Whisper load: a probe that
    # created it after ctranslate2 crashed at interpreter teardown (0xC0000409); absent / disabled -> the
    # recognizer's Endpointers use the energy path (hearing rebuild, Sep 15 2026)
    try:
        from xyrus import vad as _vad                    # noqa: PLC0415 - optional
        _vad.warm()
    except Exception as e:                               # ImportError, OSError (DLLs) ...
        log.info("capture VAD not available (%r) - energy endpointing", e)
    try:
        hearing.load_async()
    except Exception:
        log.exception("hearing.load_async failed - free text stays on Vosk")
        return None
    clean = getattr(hearing, "clean", None) or recognizer.clean_free

    def decode(pcm: bytes) -> str | None:
        if not hearing.available():
            return None
        return clean(hearing.transcribe(pcm))

    recognizer.set_free_decoder(decode)
    set_command = getattr(recognizer, "set_command_hearing", None)
    if set_command is not None:              # the whole command after the wake reply (capture.py)
        set_command(CommandHearing(hearing))
    log.info("commands after the wake reply and free text: Whisper when loaded (hearing.py), Vosk until then")
    return hearing


def _setup_logging(debug: bool) -> None:
    from xyrus import log as xlog
    env = os.environ.get("XYRUS_DATA_DIR")
    if env:                                              # tests: never write the real arc.log
        d = Path(env)
        xlog.setup(debug, log_file=d / "arc.log", crash_file=d / "crash.log", data_dir=d)
    else:
        xlog.setup(debug)


# ============================================================================= App
class App:
    """The §9.1 App surface: config, clock, registry, engine, store, memory, speaker, chime, capture,
    recognizer, vocab, hotkeys, tray, notifier, show_event, ui_queue, set_paused(bool), quit() - plus app_index
    (Start Menu index, for the Apps tab), actions, executor, grammar, window, hearing, stop_event.

    Construction builds every object (no mic stream, no engine thread yet); start() starts the threads; run()
    creates the window and runs mainloop (T-main); shutdown() cleans up after mainloop returns.
    sandbox=True: NullCapture, FakeSpeaker, FakeChime, NullTray, NullHotkeys, FakeActions, no Whisper.
    Keyword overrides replace single parts (tests): config, clock, speaker, chime, capture, actions, tray,
    hotkeys, app_index, store, memory, registry."""

    def __init__(self, *, start_hidden: bool = False, dry_run: bool = False, sandbox: bool = False,
                 show_event: Any = None, hearing: bool | None = None, **overrides: Any):
        from xyrus import commands
        from xyrus.actions import apps as app_actions
        from xyrus.clock import SystemClock
        from xyrus.commands import custom
        from xyrus.config import Config
        from xyrus.engine import Engine
        from xyrus.executor import Executor
        from xyrus.grammar import GrammarBuilder, Vocab
        from xyrus.memory import MemoryStore
        from xyrus.recognizer import Recognizer
        from xyrus.registry import REGISTRY
        from xyrus.store import CalendarStore
        from xyrus.testing import FakeActions, FakeChime, FakeSpeaker

        unknown = set(overrides) - {"config", "clock", "speaker", "chime", "capture", "actions", "tray", "hotkeys",
                                    "app_index", "store", "memory", "registry"}
        if unknown:
            raise TypeError(f"unknown App override(s): {sorted(unknown)}")
        self.start_hidden = start_hidden
        self.sandbox = sandbox
        self.dry_run = dry_run or sandbox
        self.use_hearing = (not sandbox) if hearing is None else hearing
        self.stop_event = threading.Event()
        self.ui_queue: "queue.Queue[str]" = queue.Queue()
        self.show_event = show_event if show_event is not None else NoShowEvent()
        self.window: Any = None
        self.hearing: Any = None
        self._threads: list[threading.Thread] = []
        self._started = False
        self._shut = False
        self._quit_requested = False
        self._listening = threading.Event()
        self._mic_status = "starting"
        data = paths.data_dir()

        # settings + data (config first: everything else reads it)
        self.config = overrides.get("config") or Config(paths.config_file()).load()
        if getattr(self.config, "migrated_from", None) == 1:
            try:
                self.config.migrate_file()                  # v1 -> v2 on disk, original kept as config.v1.json
            except OSError as e:
                log.error("config migration could not be saved: %s", e)
        self.clock = overrides.get("clock") or SystemClock()
        self.registry = overrides.get("registry") or REGISTRY
        commands.load_all(self.registry)                     # strict: a broken command module fails startup
        self.store = overrides.get("store") or CalendarStore(data / "calendar.json")
        self.memory = overrides.get("memory") or MemoryStore(data / "memory.json")

        # output side
        if "speaker" in overrides:
            self.speaker = overrides["speaker"]
        elif sandbox:
            self.speaker = FakeSpeaker()
        else:
            from xyrus.speech import SapiSpeaker
            self.speaker = SapiSpeaker(self.config)
        if "chime" in overrides:
            self.chime = overrides["chime"]
        elif sandbox:
            self.chime = FakeChime()
        else:
            from xyrus.speech import WinmmChime
            self.chime = WinmmChime(self.config)
        if "tray" in overrides:
            self.tray = overrides["tray"]
        elif sandbox:
            self.tray = NullTray()
        else:
            from xyrus.tray import Tray
            self.tray = Tray(self)
        self.notifier = self.tray
        self.executor = Executor()

        # actions (WinActions with the config so aliases resolve to the right process)
        if "app_index" in overrides:
            self.app_index = overrides["app_index"]
        elif sandbox:
            self.app_index = app_actions.StartMenuIndex(dirs=[])
        else:
            self.app_index = app_actions.default_index()
        if "actions" in overrides:
            self.actions = overrides["actions"]
        elif self.dry_run:
            self.actions = FakeActions()
        else:
            from xyrus.actions import WinActions
            self.actions = WinActions(config=self.config, app_index=self.app_index)

        # the engine (T-engine; app_index is handed over after the first Start Menu walk, off T-engine)
        self.engine = Engine(config=self.config, registry=self.registry, clock=self.clock, speaker=self.speaker,
                             chime=self.chime, actions=self.actions, executor=self.executor, store=self.store,
                             memory=self.memory, notifier=self.notifier, ui_queue=self.ui_queue, threaded=True,
                             app_index=None)

        # hearing: vocabulary, custom commands, microphone, grammar, recognizer
        from xyrus.paths import MODEL_DIR
        self.vocab = Vocab(MODEL_DIR, cache_file=data / "vocab_cache.json")
        # custom commands are registered before the checker is attached: startup never waits for the model;
        # an unverifiable word counts as known (the Routines tab validates live once the model is up)
        custom.install(self.registry, self.config, self.engine, self.vocab)
        if "capture" in overrides:
            self.capture = overrides["capture"]
        elif sandbox:
            self.capture = NullCapture(self._on_mic_status)
        else:
            from xyrus.audio import AudioCapture
            self.capture = AudioCapture(lambda: (self.config.get("mic.name"), self.config.get("mic.hostapi")),
                                        self._on_mic_status)
        self.grammar = GrammarBuilder(self.registry, self.config, self.vocab)
        self.recognizer = Recognizer(MODEL_DIR, self.capture, self.grammar, get_spec=self.engine.listen_spec,
                                     on_transcript=self.engine.submit_transcript, speaker=self.speaker,
                                     chime=self.chime, config=self.config)
        self.vocab.attach(self.recognizer.check_words)

        # engine hooks
        self._mic = _Serial("T-mic")
        self.engine.request_free_decode = self.recognizer.request_free_decode
        # command capture (capture.py): once Whisper is loaded it hears the whole command after the wake reply
        self.engine.capture_available = self.recognizer.capture_ready
        self.engine.on_capture_handled = self.recognizer.capture_ack
        self.engine.request_language_check = self.recognizer.request_language_check
        self.recognizer.on_capture_state = self.engine.set_capture_state
        self.recognizer.song_question = self.engine.song_question_open
        self.recognizer.ack_required = True
        self.engine.on_pause = self._on_pause
        # clap to switch the display (clap.py): the recognizer spots the claps, a short thread flips the monitor
        from xyrus.actions import power as _power
        from xyrus.clap import ClapToggle
        self._clap_toggle = ClapToggle(self.actions.screen_off, self.actions.screen_on,
                                       _power.tick_now, _power.last_input_tick)
        self.recognizer.on_clap = self._on_clap

        # hotkeys (show_window -> the window; everything else -> the engine)
        if "hotkeys" in overrides:
            self.hotkeys = overrides["hotkeys"]
        elif sandbox:
            self.hotkeys = NullHotkeys()
        else:
            from xyrus.hotkeys import Hotkeys
            self.hotkeys = Hotkeys(self.config.get("hotkeys"), self._on_hotkey)

        # settings the tabs store but don't apply themselves
        self.config.on_change("tts.volume", self._on_tts_volume)
        self.config.on_change("hotkeys", lambda key, value: self._mic.submit(
            lambda: self.hotkeys.rebind(self.config.get("hotkeys") or {})))

    # ------------------------------------------------------------------ §9.1 methods (thread-safe)
    def set_paused(self, paused: bool) -> None:
        self.engine.set_paused(bool(paused))

    def quit(self) -> None:
        """Any thread: the window closes itself from its poll; cleanup runs after mainloop returns."""
        self._quit_requested = True
        self.ui_queue.put("quit")

    # ------------------------------------------------------------------ callbacks
    def _on_hotkey(self, name: str) -> None:            # T-hotkey
        if name == "show_window":
            self.ui_queue.put("show")
        else:
            self.engine.submit_hotkey(name)

    def _on_clap(self) -> None:                         # T-rec: never block the recognizer
        threading.Thread(target=self._clap_switch, name="T-clap", daemon=True).start()

    def _clap_switch(self) -> None:
        try:
            state = self._clap_toggle.toggle()
        except Exception:
            log.exception("clap toggle failed")
            return
        log.info("clap: display %s", state)
        self.engine.post(lambda: self.engine._event("info", f"clap: display {state}"))

    def _on_pause(self, paused: bool) -> None:          # T-engine -> T-mic (never blocks the engine)
        self._mic.submit(self.capture.stop if paused else self.capture.start)

    def _on_tts_volume(self, key: str, value: Any) -> None:
        fn = getattr(self.speaker, "set_volume", None)
        if fn is not None:
            try:
                fn(int(self.config.get("tts.volume", 100) or 0))
            except Exception:
                log.exception("set_volume failed")

    def _on_mic_status(self, status: str) -> None:      # capture thread(s)
        prev, self._mic_status = self._mic_status, status
        self.engine.set_status(status)
        if status == "listening":
            self._listening.set()
            if prev in MIC_ERRORS:
                self._toast(MIC_BACK)
        elif status == "mic error, retrying" and prev not in MIC_ERRORS:
            self._toast(MIC_LOST)
        elif status == "no microphone" and prev not in MIC_ERRORS:
            self._toast(MIC_NONE)

    def _toast(self, body: str) -> None:
        try:
            self.notifier.notify("Xyrus", body)
        except Exception:
            log.exception("toast failed")

    # ------------------------------------------------------------------ lifecycle
    def _thread(self, target: Callable[[], None], name: str) -> None:
        t = threading.Thread(target=target, name=name, daemon=True)
        self._threads.append(t)
        t.start()

    def start(self) -> None:
        """Start every thread. The recognizer loads the model on T-rec; nothing here waits for it."""
        if self._started:
            return
        self._started = True
        try:
            from xyrus.actions import screenshot
            screenshot.enable_dpi_awareness()
        except Exception:
            log.exception("DPI awareness")
        try:
            self.tray.start()
        except Exception:
            log.exception("tray failed to start - running without it")
        try:
            self.hotkeys.start()
        except Exception:
            log.exception("hotkeys failed to start")
        self._thread(lambda: self.engine.run_forever(self.stop_event), "T-engine")
        self.recognizer.start()
        self._mic.submit(self.capture.start)
        self._thread(self._index_loop, "T-index")
        if self.use_hearing:
            self.hearing = plug_hearing(self.recognizer)
        self._thread(self._greet_when_ready, "T-greet")
        log.info("Xyrus %s started (%s%s)", _version(), "sandbox" if self.sandbox else "live",
                 ", dry run" if self.dry_run and not self.sandbox else "")

    def _index_loop(self) -> None:
        """Start Menu index: first walk now (off T-main / T-engine / T-rec), then every 9 min."""
        idx, first = self.app_index, True
        while not self.stop_event.is_set():
            refresh = getattr(idx, "refresh", None)
            if refresh is not None:
                try:
                    refresh()
                except Exception:
                    log.exception("Start Menu index refresh failed")
            if first:
                first = False
                self.recognizer.set_app_index(idx)
                eng = self.engine
                eng.post(lambda: setattr(eng, "app_index", idx))
            if self.stop_event.wait(INDEX_REFRESH_S):
                return

    def _greet_when_ready(self) -> None:
        deadline = time.monotonic() + GREETING_MAX_WAIT_S
        self.recognizer.ready.wait(max(0.0, deadline - time.monotonic()))
        self._listening.wait(max(0.0, deadline - time.monotonic()))
        if self.stop_event.is_set():
            return
        if self.recognizer.ready.is_set() and self.recognizer.load_error is not None:
            self.engine.set_status("speech model error")
            self._toast(MODEL_FAILED)
        self.engine.startup()

    def warm_vocab(self, timeout: float = 5.0) -> None:
        """One batched model lookup for the words the tabs validate row by row (Apps aliases, routine phrases),
        so building the window never waits on T-rec once per word (the vocab cache is empty on a new PC)."""
        rec = self.recognizer
        if not rec.ready.wait(timeout) or rec.load_error is not None:
            return
        from xyrus.grammar import tokens
        words: set[str] = set()
        for name in (self.config.get("apps") or {}):
            words.update(tokens(str(name)))
        for c in self.config.get("custom_commands") or []:
            if isinstance(c, dict):
                words.update(tokens(str(c.get("phrase", ""))))
        try:
            self.vocab.check(sorted(words))
        except Exception:
            log.exception("vocabulary warm-up failed")

    def run(self, on_ready: Callable[[Any], None] | None = None) -> None:
        """T-main: create the window and run mainloop until it closes (tray Quit / app.quit())."""
        if self._quit_requested:
            return
        from xyrus.ui.window import XyrusWindow
        self.warm_vocab()
        self.window = XyrusWindow(self, start_hidden=self.start_hidden)
        if on_ready is not None:
            on_ready(self.window)
        self.window.mainloop()

    def shutdown(self, timeout: float = 2.0) -> None:
        """After mainloop returned (or headless): stop every thread, 2 s joins. Idempotent."""
        if self._shut:
            return
        self._shut = True
        self.stop_event.set()
        steps = (("hotkeys", lambda: self.hotkeys.stop()),
                 ("recognizer", lambda: self.recognizer.stop(timeout)),
                 ("capture", lambda: self.capture.close()),
                 ("speaker", lambda: getattr(self.speaker, "close", lambda *a: None)()),
                 ("chime", lambda: self.chime.stop()),
                 ("executor", lambda: self.executor.stop()),
                 ("tray", lambda: self.tray.stop()),
                 ("mic worker", lambda: self._mic.stop(timeout)),
                 ("show event", lambda: getattr(self.show_event, "close", lambda: None)()))
        for name, fn in steps:
            try:
                fn()
            except Exception:
                log.exception("shutdown: %s", name)
        for t in self._threads:
            if t is not threading.current_thread():
                t.join(timeout)
        log.info("Xyrus stopped")


def _version() -> str:
    try:
        from xyrus import version
        return version
    except Exception:
        return "?"


# ============================================================================= --text
def run_text(args: argparse.Namespace) -> int:
    """Typed REPL through Engine.handle (no audio, tray or window; InlineExecutor)."""
    from xyrus import commands
    from xyrus.clock import SystemClock
    from xyrus.commands import custom
    from xyrus.config import Config
    from xyrus.engine import Engine
    from xyrus.executor import InlineExecutor
    from xyrus.memory import MemoryStore
    from xyrus.registry import REGISTRY
    from xyrus.store import CalendarStore
    from xyrus.testing import FakeActions, FakeChime

    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    data = paths.data_dir()
    config = Config(paths.config_file()).load()
    commands.load_all(REGISTRY)
    inner = None
    if args.speak:
        from xyrus.speech import SapiSpeaker
        inner = SapiSpeaker(config)
    speaker = PrintSpeaker(inner)
    if args.dry_run:
        actions = FakeActions()
    else:
        from xyrus.actions import WinActions
        actions = WinActions(config=config)
    ui_q: "queue.Queue[str]" = queue.Queue()
    engine = Engine(config=config, registry=REGISTRY, clock=SystemClock(), speaker=speaker, chime=FakeChime(),
                    actions=actions, executor=InlineExecutor(), store=CalendarStore(data / "calendar.json"),
                    memory=MemoryStore(data / "memory.json"), notifier=PrintNotifier(), ui_queue=ui_q,
                    threaded=False)
    custom.install(REGISTRY, config, engine)
    interactive = sys.stdin is not None and sys.stdin.isatty()
    if interactive:
        print("Xyrus text mode - type a command (the wake word is optional); 'quit' leaves.", flush=True)
    while True:
        if interactive:
            print("> ", end="", flush=True)
        line = sys.stdin.readline() if sys.stdin is not None else ""
        if not line:
            break
        text = line.strip()
        if text.lower() in ("quit", "exit", "q"):
            break
        if text:
            engine.handle(text, "typed")
        engine.tick()
        while True:
            try:
                msg = ui_q.get_nowait()
            except queue.Empty:
                break
            print(f"  [window: {msg}]", flush=True)
    if inner is not None:
        inner.close()
    return 0


# ============================================================================= --selftest
SELFTEST_SKIP = {"test_recognition", "test_ui_render", "test_app_smoke", "test_shell", "test_actions_smoke"}
SELFTEST_COMMANDS = (                      # (typed command, the reply must start with / ui message)
    ("what time is it", "It's"),
    ("set a timer for five minutes", "Timer set for five minutes"),
    ("play alone", "Playing"),
    ("add a to do buy milk tomorrow", "Added to your to-do list"),
    ("what can you do", "show:Commands"),
)


def _wait(cond: Callable[[], bool], timeout: float) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            if cond():
                return True
        except Exception:
            pass
        time.sleep(0.02)
    return False


def selftest_app(failures: list[str], *, window: bool = True) -> None:
    """Build the full App in sandbox mode in the current XYRUS_DATA_DIR, start it, check the speech model,
    drive SELFTEST_COMMANDS through the threaded engine, build the window hidden, shut down."""
    app = App(sandbox=True, start_hidden=True, hearing=False)
    try:
        app.start()
        if not app.recognizer.ready.wait(60):
            failures.append("speech model: not loaded after 60 s")
        elif app.recognizer.load_error is not None:
            failures.append(f"speech model: {app.recognizer.load_error!r}")
        else:
            unknown = app.vocab.unknown_words("open kubernetes")
            if unknown != ["kubernetes"]:
                failures.append(f"vocabulary check: {unknown!r}")
        for text, expect in SELFTEST_COMMANDS:
            before = len(app.speaker.said)
            app.engine.submit_text(text, "typed")
            if expect.startswith("show:"):
                got: list[str] = []

                def seen() -> bool:
                    while True:
                        try:
                            got.append(app.ui_queue.get_nowait())
                        except queue.Empty:
                            return expect in got
                ok = _wait(seen, 5)
            else:
                ok = _wait(lambda: any(s.startswith(expect) for s in app.speaker.said[before:]), 5)
            if not ok:
                failures.append(f"{text!r}: expected {expect!r}, got {app.speaker.said[before:]!r}")
        if window:
            try:
                from xyrus.ui.window import TABS, XyrusWindow
                app.warm_vocab()
                win = XyrusWindow(app, start_hidden=True)
                try:
                    for _ in range(5):
                        win.update()
                        time.sleep(0.05)
                    broken = [n for n, t in zip(TABS, (win.home_tab, win.calendar_tab, win.commands_tab,
                                                        win.apps_tab, win.routines_tab, win.settings_tab))
                              if t is None]
                    if broken:
                        failures.append(f"window tabs failed to build: {broken}")
                finally:
                    win.close()
            except Exception as e:
                log.exception("self-test window")
                failures.append(f"window: {e!r}")
    finally:
        app.shutdown()


def run_selftest(args: argparse.Namespace) -> int:
    import shutil
    failures: list[str] = []
    saved = os.environ.get("XYRUS_DATA_DIR")
    tmp = tempfile.mkdtemp(prefix="xyrus_selftest_")
    os.environ["XYRUS_DATA_DIR"] = tmp                   # never the real calendar / config
    try:
        if not model_ok():
            failures.append(f"speech model missing at {paths.MODEL_DIR}")
        else:
            try:
                selftest_app(failures)
            except Exception as e:
                log.exception("self-test")
                failures.append(f"construction: {e!r}")
        if args.full:
            failures += _run_unit_tests()
    finally:
        if saved is None:
            os.environ.pop("XYRUS_DATA_DIR", None)
        else:
            os.environ["XYRUS_DATA_DIR"] = saved
        shutil.rmtree(tmp, ignore_errors=True)
    if failures:
        print("self-test FAILED:")
        for f in failures:
            print("  -", f)
            log.error("self-test: %s", f)
        return 1
    print("self-test passed")
    log.info("self-test passed")
    return 0


def _run_unit_tests() -> list[str]:
    import unittest
    tests_dir = paths.BASE / "tests"
    if not tests_dir.is_dir():
        return ["tests folder missing"]
    if str(paths.BASE) not in sys.path:
        sys.path.insert(0, str(paths.BASE))
    names = sorted(f"tests.{p.stem}" for p in tests_dir.glob("test_*.py") if p.stem not in SELFTEST_SKIP)
    suite = unittest.TestLoader().loadTestsFromNames(names)
    result = unittest.TextTestRunner(stream=sys.stdout, verbosity=1).run(suite)
    return [f"{t.id()}" for t, _ in result.failures + result.errors]


# ============================================================================= --wav
def run_wav(args: argparse.Namespace) -> int:
    import wave
    from xyrus import commands
    from xyrus.config import Config
    from xyrus.grammar import GrammarBuilder, Vocab
    from xyrus.interfaces import ListenSpec
    from xyrus.recognizer import Recognizer
    from xyrus.registry import REGISTRY
    from xyrus.testing import FakeChime, FakeSpeaker

    with wave.open(args.wav, "rb") as w:
        if (w.getframerate(), w.getnchannels(), w.getsampwidth()) != (16000, 1, 2):
            print("need a 16 kHz mono 16-bit WAV")
            return 2
        pcm = w.readframes(w.getnframes())
    config = Config(paths.config_file()).load()
    commands.load_all(REGISTRY)
    vocab = Vocab(paths.MODEL_DIR, cache_file=paths.data_dir() / "vocab_cache.json")
    rec = Recognizer(paths.MODEL_DIR, NullCapture(), GrammarBuilder(REGISTRY, config, vocab),
                     get_spec=lambda: ListenSpec("wake"), on_transcript=lambda tr: None, speaker=FakeSpeaker(),
                     chime=FakeChime(), config=config)
    vocab.attach(rec.check_words)
    for mode in ("wake", "command", "free"):
        tr = rec.decode_pcm(pcm, mode)
        print(f"{mode:8s} {tr.text!r}  free={tr.free_text!r}")
    hearing = plug_hearing(rec)
    if hearing is not None and hearing.available(wait=90):
        rec.set_free_decoder(lambda p: hearing.clean(hearing.transcribe(p)))
        print(f"{'whisper':8s} {rec.decode_pcm(pcm, 'free').free_text!r}")
    return 0


# ============================================================================= main
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="arc.py", description="Xyrus - offline voice assistant")
    p.add_argument("--tray", action="store_true", help="start hidden in the tray (the Startup shortcut)")
    p.add_argument("--text", action="store_true", help="typed REPL (python.exe only)")
    p.add_argument("--speak", action="store_true", help="--text: also speak the replies")
    p.add_argument("--selftest", action="store_true", help="construct everything without a mic, exit code")
    p.add_argument("--full", action="store_true", help="--selftest: also run the unit test suite")
    p.add_argument("--dry-run", action="store_true", help="record actions instead of doing them")
    p.add_argument("--debug", action="store_true", help="debug logging")
    p.add_argument("--wav", metavar="PATH", help="decode a 16 kHz mono WAV and print the transcripts")
    p.add_argument("--sandbox", action="store_true", help=argparse.SUPPRESS)
    return p.parse_args(argv)


def main(argv: list[str] | None = None, *, hard_exit: bool = True) -> int:
    args = parse_args(argv)
    _setup_logging(args.debug)
    if args.text:
        return run_text(args)
    if args.selftest:
        return run_selftest(args)
    if args.wav:
        return run_wav(args)

    from xyrus import single_instance as si
    mutex, event = instance_names()
    if not si.acquire(mutex):
        shown = si.signal_existing(event)
        log.info("already running - %s", "asked it to show its window" if shown else "could not signal it")
        return 0
    show_event = si.ShowEvent(event)
    if not model_ok():
        log.error("speech model missing at %s", paths.MODEL_DIR)
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, MODEL_MISSING, "Xyrus", 0x10)
        except Exception:
            pass
        return 1
    if args.sandbox:
        from xyrus import winutil                        # this process only: never tap ALT for a test window
        winutil.force_foreground = lambda hwnd: False
    app = App(start_hidden=args.tray, dry_run=args.dry_run, sandbox=args.sandbox, show_event=show_event)
    code = 0
    try:
        app.start()
        app.run()
    except Exception:
        log.exception("Xyrus crashed")
        code = 1
    finally:
        app.shutdown()
    if hard_exit:                     # pystray / COM threads must not keep the process alive (§5.7 rule 6)
        logging.shutdown()
        os._exit(code)
    return code


if __name__ == "__main__":
    sys.exit(main())
