"""Logging setup (§4.4): rotating arc.log, crash dumps, excepthooks, pythonw stdout redirect. Never prints.

Modules log through logging.getLogger("xyrus.<name>"). setup() is idempotent; tests pass temp paths.
"""
from __future__ import annotations

import faulthandler
import logging
import logging.handlers
import sys
import threading
from pathlib import Path

from xyrus import paths

FORMAT = "%(asctime)s %(levelname)s %(threadName)s %(name)s: %(message)s"
_state: dict = {"handler": None, "crash": None, "stdout": None}


def setup(debug: bool = False, *, log_file: Path | None = None, crash_file: Path | None = None,
          data_dir: Path | None = None) -> None:
    log_file = Path(log_file) if log_file else paths.LOG_FILE
    crash_file = Path(crash_file) if crash_file else paths.CRASH_FILE
    data_dir = Path(data_dir) if data_dir else paths.data_dir()

    root = logging.getLogger()
    if _state["handler"] is not None:
        root.removeHandler(_state["handler"])
        _state["handler"].close()
    log_file.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(log_file, maxBytes=1_000_000, backupCount=3, encoding="utf8")
    handler.setFormatter(logging.Formatter(FORMAT))
    root.addHandler(handler)
    root.setLevel(logging.DEBUG if debug else logging.INFO)
    _state["handler"] = handler
    logging.getLogger("comtypes").setLevel(logging.WARNING)

    log = logging.getLogger("xyrus")

    def excepthook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        log.critical("uncaught exception", exc_info=(exc_type, exc, tb))

    def thread_excepthook(args):
        if args.exc_type is SystemExit:
            return
        name = args.thread.name if args.thread is not None else "?"
        log.critical("uncaught exception in thread %s", name, exc_info=(args.exc_type, args.exc_value,
                                                                       args.exc_traceback))

    sys.excepthook = excepthook
    threading.excepthook = thread_excepthook

    try:
        crash_file.parent.mkdir(parents=True, exist_ok=True)
        if _state["crash"] is not None:
            faulthandler.disable()
            _state["crash"].close()
        _state["crash"] = open(crash_file, "a", encoding="utf8")     # kept open for faulthandler
        faulthandler.enable(_state["crash"])
    except OSError as e:
        log.warning("faulthandler unavailable: %s", e)

    if sys.stdout is None or sys.stderr is None:                     # pythonw: no console
        try:
            data_dir.mkdir(parents=True, exist_ok=True)
            out = open(data_dir / "stdout.log", "a", buffering=1, encoding="utf8")
            _state["stdout"] = out
            sys.stdout = sys.stderr = out
        except OSError as e:
            log.warning("could not redirect stdout: %s", e)


def teardown() -> None:
    """Undo setup() (tests)."""
    root = logging.getLogger()
    if _state["handler"] is not None:
        root.removeHandler(_state["handler"])
        _state["handler"].close()
        _state["handler"] = None
    if _state["crash"] is not None:
        faulthandler.disable()
        _state["crash"].close()
        _state["crash"] = None
    sys.excepthook = sys.__excepthook__
    threading.excepthook = threading.__excepthook__
