"""Routines tab: custom commands (§3.10). List on the left; editor on the right with the phrase (live
vocabulary check), ordered steps (action + argument + ↑ ↓ ✕), Enabled switch, Test / Save / Delete, and
"Add template". Saved to config.custom_commands; the registry picks changes up via config.on_change."""
from __future__ import annotations

import copy
import logging
import re

import customtkinter as ctk

from xyrus.ui.theme import (ACCENT, ACCENT_HOVER, BG, CARD, FAINT, GREEN, LINE, LINE_HOVER, MENU, MUTED,
                            ON_ACCENT, RED, RED_BG, TEXT)

log = logging.getLogger("xyrus.ui.routines")

ACTIONS = ["open", "run", "keys", "type", "say", "command", "wait"]
ARG_HINTS = {"open": "chrome, https://…, spotify:", "run": "a command line", "keys": "ctrl+shift+t, f11",
             "type": "text to type", "say": "what to say", "command": "e.g. set the volume to forty percent",
             "wait": "seconds (1-30)"}
TEMPLATES = {        # §3.10 shipped templates (not active until saved)
    "movie mode": [("command", "set brightness to forty percent"), ("command", "set the volume to sixty percent"),
                   ("open", "https://www.netflix.com"), ("wait", "5"), ("keys", "f11")],
    "work mode": [("open", "code"), ("open", "terminal"), ("open", "chrome")],
    "focus mode": [("command", "mute"), ("say", "Focus mode on, sir.")],
}
WAKE_LIKE = {"xyrus", "cyrus", "zeros", "virus", "cirrus", "sirius", "serious"}
MAX_WAIT = 30


def clean_phrase(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w' ]", " ", (text or "").lower())).strip()


def _normalize(phrase: str) -> str:
    try:
        from xyrus.normalize import normalize
        return normalize(phrase)
    except Exception:
        return phrase


def builtin_clash(phrase: str, registry) -> bool:
    """Does the phrase equal or contain a built-in slot-less phrase? (§3.10)"""
    text = _normalize(phrase)
    padded = f" {text} "
    try:
        cmds = registry.commands()
    except Exception:
        return False
    for cmd in cmds:
        if getattr(cmd, "custom", False):
            continue
        for p in cmd.patterns:
            if p.slots:
                continue
            rx = getattr(p, "regex", None)
            if rx is not None:
                if rx.match(text):
                    return True
            else:
                from xyrus.ui.commands_tab import sayable
                s = sayable(p.text)
                if s and f" {s} " in padded:
                    return True
    return False


def validate(phrase: str, steps: list[tuple[str, str]], *, registry=None, vocab=None, wake_words=(),
             others=()) -> str | None:
    """None if the routine can be saved, else the message for the user (§3.10)."""
    phrase = clean_phrase(phrase)
    words = phrase.split()
    if not words:
        return "Type the phrase you'll say."
    if len(words) < 2 and len(words[0]) < 5:
        return "Use at least two words, or one word of five letters or more."
    wake = WAKE_LIKE | {w.lower() for w in wake_words or ()}
    if wake & set(words):
        return "Leave the wake word out — you say it before the phrase."
    if vocab is not None:
        try:
            unknown = vocab.unknown_words(phrase)
        except Exception:
            unknown = []
        if unknown:
            return f"The speech model doesn't know '{unknown[0]}' — pick another word."
    if registry is not None and builtin_clash(phrase, registry):
        return "That's already a built-in command."
    if phrase in {clean_phrase(o) for o in others}:
        return "You already have a routine with that phrase."
    if not steps:
        return "Add at least one step."
    for i, (action, arg) in enumerate(steps, 1):
        arg = (arg or "").strip()
        if action not in ACTIONS:
            return f"Step {i}: pick an action."
        if not arg:
            return f"Step {i}: fill in what to {action}."
        if action == "wait":
            try:
                secs = float(arg)
            except ValueError:
                return f"Step {i}: wait needs a number of seconds."
            if not 0 < secs <= MAX_WAIT:
                return f"Step {i}: wait 1 to {MAX_WAIT} seconds."
        if action == "keys":
            try:
                from xyrus.actions.keys import parse_chord
            except ImportError:
                continue
            try:
                parse_chord(arg)
            except Exception:
                return f"Step {i}: I don't know the keys '{arg}'."
    return None


def to_config(phrase: str, enabled: bool, steps: list[tuple[str, str]]) -> dict:
    out = []
    for action, arg in steps:
        arg = arg.strip()
        if action == "wait":
            n = float(arg)
            out.append({"action": action, "arg": int(n) if n == int(n) else n})
        else:
            out.append({"action": action, "arg": arg})
    return {"phrase": clean_phrase(phrase), "enabled": bool(enabled), "steps": out}


class _StepRow:
    def __init__(self, tab: "RoutinesTab", action: str, arg: str):
        self.tab = tab
        f = self.frame = ctk.CTkFrame(tab.steps_box, fg_color="transparent")
        f.grid_columnconfigure(2, weight=1)
        self.num = ctk.CTkLabel(f, text="", width=22, text_color=FAINT, font=tab.f_small)
        self.num.grid(row=0, column=0, padx=(2, 2))
        self.action = ctk.CTkOptionMenu(f, values=ACTIONS, width=104, height=28, font=tab.f_small,
                                        dropdown_font=tab.f_small, dynamic_resizing=False,
                                        command=lambda v: self._hint(), **MENU)
        self.action.set(action if action in ACTIONS else "open")
        self.action.grid(row=0, column=1, padx=(0, 6), pady=3)
        self.arg = ctk.CTkEntry(f, height=28, fg_color=BG, border_color=LINE, text_color=TEXT)
        self.arg.grid(row=0, column=2, sticky="ew", pady=3)
        if arg:
            self.arg.insert(0, str(arg))
        self.arg.bind("<KeyRelease>", lambda e: tab._changed(), add="+")
        b = dict(width=26, height=26, corner_radius=6, fg_color="transparent", hover_color=LINE,
                 text_color=MUTED, font=tab.f_small)
        ctk.CTkButton(f, text="↑", command=lambda: tab.move_step(self, -1), **b).grid(row=0, column=3, padx=(6, 0))
        ctk.CTkButton(f, text="↓", command=lambda: tab.move_step(self, 1), **b).grid(row=0, column=4)
        ctk.CTkButton(f, text="✕", command=lambda: tab.remove_step(self),
                      **{**b, "hover_color": RED}).grid(row=0, column=5, padx=(0, 2))
        self._hint()

    def _hint(self):
        self.arg.configure(placeholder_text=ARG_HINTS.get(self.action.get(), ""))
        self.tab._changed()

    def value(self) -> tuple[str, str]:
        return self.action.get(), self.arg.get()


class RoutinesTab(ctk.CTkFrame):
    def __init__(self, master, app):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self.index: int | None = None        # position in config.custom_commands being edited (None = new)
        self.steps: list[_StepRow] = []
        self.list_buttons: list[ctk.CTkButton] = []
        self.dirty = False
        self._check_job = None
        self.f_small = ctk.CTkFont(size=12)
        self.f_head = ctk.CTkFont(size=13, weight="bold")

        self.grid_columnconfigure(0, weight=0, minsize=240)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self._build_list()
        self._build_editor()
        routines = self._routines()
        if routines:
            self.edit(0)
        else:
            self.new()

    # ---------------------------------------------------------------- data --
    def _routines(self) -> list[dict]:
        return list(self.app.config.get("custom_commands", []) or [])

    def _save_routines(self, lst: list[dict]) -> bool:
        try:
            self.app.config.set("custom_commands", lst)
            return True
        except Exception as e:
            log.exception("saving routines failed")
            self.flash(f"Could not save: {e}", RED)
            return False

    # ------------------------------------------------------------- building --
    def _build_list(self):
        left = ctk.CTkFrame(self, fg_color=BG, corner_radius=12)
        left.grid(row=0, column=0, sticky="nsew", padx=(8, 4), pady=8)
        ctk.CTkLabel(left, text="Your routines", font=self.f_head, text_color=TEXT).pack(anchor="w", padx=12,
                                                                                         pady=(10, 2))
        ctk.CTkLabel(left, text="One phrase runs several steps.", font=self.f_small,
                     text_color=MUTED).pack(anchor="w", padx=12)
        self.list = ctk.CTkScrollableFrame(left, fg_color="transparent", scrollbar_button_color=LINE)
        self.list.pack(fill="both", expand=True, padx=4, pady=6)
        self.empty = ctk.CTkLabel(self.list, text="No routines yet.\nStart from a template below.",
                                  text_color=FAINT, font=self.f_small, justify="left")
        bottom = ctk.CTkFrame(left, fg_color="transparent")
        bottom.pack(fill="x", padx=8, pady=(0, 10))
        ctk.CTkButton(bottom, text="+ New", width=70, height=28, fg_color=LINE, hover_color=LINE_HOVER,
                      text_color=TEXT, command=self.new).pack(side="left")
        self.template_menu = ctk.CTkOptionMenu(bottom, values=list(TEMPLATES), width=140, height=28,
                                               font=self.f_small, dropdown_font=self.f_small,
                                               dynamic_resizing=False, command=self.add_template, **MENU)
        self.template_menu.set("Add template")
        self.template_menu.pack(side="right")

    def _build_editor(self):
        right = ctk.CTkFrame(self, fg_color=BG, corner_radius=12)
        right.grid(row=0, column=1, sticky="nsew", padx=(4, 8), pady=8)
        right.grid_columnconfigure(0, weight=1)
        right.grid_rowconfigure(3, weight=1)

        head = ctk.CTkFrame(right, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=14, pady=(12, 0))
        head.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(head, text="When I say", text_color=MUTED, font=self.f_small).grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(head, text="Xyrus,", text_color=FAINT, font=ctk.CTkFont(size=14)).grid(row=1, column=0,
                                                                                              sticky="w", padx=(0, 6))
        self.phrase = ctk.CTkEntry(head, height=34, fg_color=CARD, border_color=LINE, font=ctk.CTkFont(size=14),
                                   text_color=TEXT, placeholder_text="movie mode")
        self.phrase.grid(row=1, column=1, sticky="ew")
        self.phrase.bind("<KeyRelease>", lambda e: (self._changed(), self._schedule_check()), add="+")
        self.enabled = ctk.CTkSwitch(head, text="Enabled", progress_color=ACCENT, font=self.f_small,
                                     command=self._changed)
        self.enabled.grid(row=1, column=2, padx=(12, 0))
        self.phrase_hint = ctk.CTkLabel(head, text="", font=ctk.CTkFont(size=11), text_color=MUTED, anchor="w",
                                        height=16)
        self.phrase_hint.grid(row=2, column=1, sticky="w")

        ctk.CTkLabel(right, text="Steps (run in order)", text_color=MUTED, font=self.f_small).grid(
            row=2, column=0, sticky="w", padx=14, pady=(8, 0))
        self.steps_box = ctk.CTkScrollableFrame(right, fg_color="transparent", scrollbar_button_color=LINE)
        self.steps_box.grid(row=3, column=0, sticky="nsew", padx=8, pady=(2, 0))
        self.steps_box.grid_columnconfigure(0, weight=1)
        self.add_step_btn = ctk.CTkButton(right, text="+ Step", width=80, height=28, fg_color=LINE,
                                          hover_color=LINE_HOVER, text_color=TEXT,
                                          command=lambda: self.add_step("open", ""))
        self.add_step_btn.grid(row=4, column=0, sticky="w", padx=14, pady=(4, 0))

        bar = ctk.CTkFrame(right, fg_color="transparent")
        bar.grid(row=5, column=0, sticky="ew", padx=14, pady=(8, 12))
        self.msg = ctk.CTkLabel(bar, text="", anchor="w", font=self.f_small, text_color=MUTED)
        self.msg.pack(side="left", fill="x", expand=True)
        self.save_btn = ctk.CTkButton(bar, text="Save", width=80, fg_color=ACCENT, hover_color=ACCENT_HOVER,
                                      text_color=ON_ACCENT, command=self.save)
        self.save_btn.pack(side="right")
        ctk.CTkButton(bar, text="Test", width=70, fg_color=LINE, hover_color=LINE_HOVER, text_color=TEXT,
                      command=self.test).pack(side="right", padx=6)
        self.delete_btn = ctk.CTkButton(bar, text="Delete", width=70, fg_color="transparent", border_width=1,
                                        border_color=RED, text_color=RED, hover_color=RED_BG,
                                        command=self.delete)
        self.delete_btn.pack(side="right", padx=(0, 6))

    # ----------------------------------------------------------------- list --
    def render_list(self):
        for b in self.list_buttons:
            b.destroy()
        self.list_buttons = []
        routines = self._routines()
        if not routines:
            self.empty.pack(anchor="w", padx=8, pady=8)
        else:
            self.empty.pack_forget()
        for i, r in enumerate(routines):
            on = r.get("enabled", True)
            sel = i == self.index
            b = ctk.CTkButton(self.list, text=(r.get("phrase") or "(no phrase)") + ("" if on else "  · off"),
                              anchor="w", height=30, corner_radius=8,
                              fg_color=ACCENT if sel else "transparent", hover_color=LINE if not sel else ACCENT_HOVER,
                              text_color=ON_ACCENT if sel else (TEXT if on else FAINT),
                              command=lambda i=i: self.edit(i))
            b.pack(fill="x", padx=2, pady=1)
            self.list_buttons.append(b)

    # --------------------------------------------------------------- editor --
    def _load(self, phrase: str, enabled: bool, steps: list[tuple[str, str]]):
        self.phrase.delete(0, "end")
        if phrase:
            self.phrase.insert(0, phrase)
        if enabled:
            self.enabled.select()
        else:
            self.enabled.deselect()
        for s in self.steps:
            s.frame.destroy()
        self.steps = []
        for action, arg in steps:
            self.add_step(action, arg, mark=False)
        self._renumber()
        self.dirty = False
        self.check_phrase()
        self.delete_btn.configure(state="normal" if self.index is not None else "disabled")
        self.render_list()

    def edit(self, i: int):
        routines = self._routines()
        if not 0 <= i < len(routines):
            return
        r = routines[i]
        self.index = i
        self._load(r.get("phrase", ""), r.get("enabled", True),
                   [(s.get("action", "open"), str(s.get("arg", ""))) for s in r.get("steps", [])])
        self.flash("", MUTED)

    def new(self):
        self.index = None
        self._load("", True, [("open", "")])
        self.flash("New routine — give it a phrase and steps, then Save.", MUTED)

    def add_template(self, name: str):
        self.template_menu.set("Add template")
        steps = TEMPLATES.get(name)
        if not steps:
            return
        self.index = None
        self._load(name, True, list(steps))
        self.dirty = True
        self.flash(f"Template “{name}” loaded — Save to turn it on.", MUTED)

    def add_step(self, action: str, arg: str, mark: bool = True) -> _StepRow:
        row = _StepRow(self, action, arg)
        self.steps.append(row)
        self._renumber()
        if mark:
            self._changed()
        return row

    def remove_step(self, row: _StepRow):
        self.steps.remove(row)
        row.frame.destroy()
        self._renumber()
        self._changed()

    def move_step(self, row: _StepRow, delta: int):
        i = self.steps.index(row)
        j = i + delta
        if not 0 <= j < len(self.steps):
            return
        self.steps[i], self.steps[j] = self.steps[j], self.steps[i]
        self._renumber()
        self._changed()

    def _renumber(self):
        for i, s in enumerate(self.steps):
            s.frame.grid(row=i, column=0, sticky="ew", pady=0)
            s.num.configure(text=f"{i + 1}.")

    def _changed(self):
        self.dirty = True

    def step_values(self) -> list[tuple[str, str]]:
        return [s.value() for s in self.steps]

    def _schedule_check(self):
        if self._check_job:
            self.after_cancel(self._check_job)
        self._check_job = self.after(350, self.check_phrase)

    def check_phrase(self):
        """Live vocabulary check of the phrase (red when the model can't hear a word)."""
        self._check_job = None
        phrase = clean_phrase(self.phrase.get())
        vocab = getattr(self.app, "vocab", None)
        unknown = []
        if phrase and vocab is not None:
            try:
                unknown = vocab.unknown_words(phrase)
            except Exception:
                unknown = []
        if unknown:
            self.phrase_hint.configure(text=f"The speech model doesn't know '{unknown[0]}' — pick another word.",
                                       text_color=RED)
            self.phrase.configure(border_color=RED)
        else:
            self.phrase_hint.configure(text=f'Say: "Xyrus, {phrase}"' if phrase else "", text_color=FAINT)
            self.phrase.configure(border_color=LINE)
        return unknown

    def _others(self) -> list[str]:
        return [r.get("phrase", "") for i, r in enumerate(self._routines()) if i != self.index]

    def validation_error(self) -> str | None:
        return validate(self.phrase.get(), self.step_values(), registry=getattr(self.app, "registry", None),
                        vocab=getattr(self.app, "vocab", None),
                        wake_words=self.app.config.get("wake_words", ()) or (), others=self._others())

    # -------------------------------------------------------------- actions --
    def save(self) -> bool:
        err = self.validation_error()
        if err:
            self.flash(err, RED)
            return False
        entry = to_config(self.phrase.get(), bool(self.enabled.get()), self.step_values())
        routines = copy.deepcopy(self._routines())
        if self.index is not None and self.index < len(routines):
            routines[self.index] = entry
        else:
            routines.append(entry)
            self.index = len(routines) - 1
        if not self._save_routines(routines):
            return False
        self.dirty = False
        self.render_list()
        self.delete_btn.configure(state="normal")
        self.flash(f'Saved — say "Xyrus, {entry["phrase"]}".', GREEN)
        return True

    def test(self):
        if self.dirty and not self.save():
            return
        phrase = clean_phrase(self.phrase.get())
        if not phrase:
            return
        try:
            self.app.engine.submit_text(phrase, "typed")
            self.flash(f"Running “{phrase}” — see Home.", MUTED)
        except Exception:
            log.exception("test routine failed")
            self.flash("Could not run it.", RED)

    def delete(self):
        if self.index is None:
            self.new()
            return
        routines = copy.deepcopy(self._routines())
        if self.index < len(routines):
            gone = routines.pop(self.index)
            if not self._save_routines(routines):
                return
            self.flash(f"Deleted “{gone.get('phrase', '')}”.", MUTED)
        if routines:
            self.edit(0)
        else:
            self.new()

    def flash(self, text: str, color: str):
        self.msg.configure(text=text, text_color=color)
