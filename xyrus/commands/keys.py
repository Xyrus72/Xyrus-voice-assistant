"""Keys and editing shortcuts (§3.5). Silent. Single-word ones need the wake word in the same utterance
(armed_ok_single_word=False, §5.4) and are never follow-up phrases."""
from __future__ import annotations

from xyrus.registry import command

SECTION = "Keys & clipboard"

# (name, chord, phrases, help, armed_ok_single_word)
KEY_COMMANDS = (
    ("press_enter", "enter", ("press enter",), "presses Enter", True),
    ("press_escape", "escape", ("press escape",), "presses Esc", True),
    ("press_tab", "tab", ("press tab",), "presses Tab", True),
    ("press_space", "space", ("press space",), "presses Space", True),
    ("select_all", "ctrl+a", ("select all",), "Ctrl+A", True),
    ("copy", "ctrl+c", ("copy", "copy that"), "Ctrl+C", False),
    ("cut", "ctrl+x", ("cut",), "Ctrl+X", False),
    ("paste", "ctrl+v", ("paste", "paste it"), "Ctrl+V", False),
    ("undo", "ctrl+z", ("undo",), "Ctrl+Z", False),
    ("redo", "ctrl+y", ("redo",), "Ctrl+Y", False),
    ("save", "ctrl+s", ("save", "save this"), "Ctrl+S", False),
    ("new_tab", "ctrl+t", ("new tab",), "Ctrl+T", True),
    ("close_tab", "ctrl+w", ("close tab",), "Ctrl+W", True),
    ("reopen_tab", "ctrl+shift+t", ("reopen tab",), "Ctrl+Shift+T", True),
    ("refresh", "f5", ("refresh",), "F5", False),
    ("full_screen", "f11", ("full screen",), "F11", True),
)


def _make(chord: str):
    def handler(ctx, m):
        ctx.do(ctx.actions.keys, chord)
    return handler


for _name, _chord, _phrases, _help, _single in KEY_COMMANDS:
    _h = _make(_chord)
    _h.__name__ = _name
    command(_name, *_phrases, section=SECTION, help=_help, armed_ok_single_word=_single)(_h)
