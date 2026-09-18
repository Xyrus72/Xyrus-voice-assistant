"""Start with Windows: a shortcut in the user's Startup folder (v1 set_autostart, moved here).

The shortcut runs `venv\\Scripts\\pythonw.exe "<BASE>\\arc.py" --tray` from BASE with xyrus.ico, the
same target v1 created, so an existing v1 shortcut stays valid. Every function takes an optional
`folder` so tests can use a temp folder instead of the real Startup folder.
Shortcuts are written through WScript.Shell (pywin32 COM) - no PowerShell window, no subprocess.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

log = logging.getLogger("xyrus.autostart")

try:
    from xyrus.paths import BASE, ICON_FILE
except ImportError:                                  # pragma: no cover - before T1's paths.py lands
    BASE = Path(__file__).resolve().parent.parent
    ICON_FILE = BASE / "xyrus.ico"

NAME = "Xyrus"
LNK_NAME = f"{NAME}.lnk"


def startup_dir() -> Path:
    return Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def shortcut_path(folder: Path | str | None = None) -> Path:
    return Path(folder or startup_dir()) / LNK_NAME


def pythonw_path() -> Path:
    """venv\\Scripts\\pythonw.exe of this install (falls back to the running interpreter's pythonw)."""
    venv = BASE / "venv" / "Scripts" / "pythonw.exe"
    if venv.exists():
        return venv
    return Path(sys.executable).with_name("pythonw.exe")


def _shell():
    import pythoncom
    import win32com.client
    try:
        pythoncom.CoInitialize()        # no-op (S_FALSE) when this thread already has COM
    except pythoncom.com_error:         # thread is MTA already - that's fine for WScript.Shell
        pass
    return win32com.client.Dispatch("WScript.Shell")


def _write_lnk(path: Path, args: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    s = _shell().CreateShortcut(str(path))
    s.TargetPath = str(pythonw_path())
    s.Arguments = args
    s.WorkingDirectory = str(BASE)
    s.IconLocation = str(ICON_FILE)
    s.Description = f"{NAME} voice assistant"
    s.Save()


def read_shortcut(path: Path | str) -> dict | None:
    """{target, arguments, workdir} of a .lnk, or None if missing/unreadable."""
    path = Path(path)
    if not path.exists():
        return None
    try:
        s = _shell().CreateShortcut(str(path))
        return {"target": s.TargetPath, "arguments": s.Arguments, "workdir": s.WorkingDirectory}
    except Exception as e:
        log.warning("cannot read shortcut %s: %s", path, e)
        return None


def enabled(folder: Path | str | None = None) -> bool:
    return shortcut_path(folder).exists()


def set_enabled(on: bool, folder: Path | str | None = None) -> None:
    """Create (or repair) / remove the Startup shortcut. Raises OSError on failure."""
    lnk = shortcut_path(folder)
    if not on:
        lnk.unlink(missing_ok=True)
        log.info("autostart disabled")
        return
    try:
        _write_lnk(lnk, f'"{BASE / "arc.py"}" --tray')
    except Exception as e:
        raise OSError(f"could not create {lnk}: {e}") from e
    log.info("autostart enabled -> %s", lnk)


def _same(a: str, b: Path) -> bool:
    try:
        return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(str(b)))
    except Exception:
        return False


def target_ok(folder: Path | str | None = None) -> bool:
    """True if the Startup shortcut exists and starts THIS folder's arc.py with --tray."""
    info = read_shortcut(shortcut_path(folder))
    if not info:
        return False
    args = info["arguments"] or ""
    script = str(BASE / "arc.py")
    return (os.path.basename(info["target"]).lower() == "pythonw.exe"
            and _same(info["target"], pythonw_path())
            and os.path.normcase(script) in os.path.normcase(args)
            and "--tray" in args)


def create_desktop_shortcut(folders: list[Path | str] | None = None) -> list[Path]:
    """`Xyrus.lnk` (no --tray: opens the window) on the desktop and in the install folder (§8.2)."""
    if folders is None:
        folders = [Path(_shell().SpecialFolders("Desktop")), BASE]
    made = []
    for f in folders:
        lnk = Path(f) / LNK_NAME
        _write_lnk(lnk, f'"{BASE / "arc.py"}"')
        made.append(lnk)
    return made
