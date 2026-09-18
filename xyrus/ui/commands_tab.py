"""Commands tab: every command and every way to say it, generated from registry.sections(config) only
(v1 behaviour): count label "N commands · M ways to say them", filter box, sections, a Try button for
phrases without slots. Rows are built once per registry version; filtering only re-packs them.
"""
from __future__ import annotations

import logging
import tkinter as tk

import customtkinter as ctk

from xyrus.ui.theme import ACCENT, ACCENT_HOVER, BG, FAINT, LINE, LINE_HOVER, MONO, MUTED, TEXT, c

log = logging.getLogger("xyrus.ui.commands")

LEGEND = "Slots"          # the legend section (§4.5) - listed, but not counted as commands


def sayable(pattern: str) -> str | None:
    """A pattern the user can say as-is: no slots; optional words kept, first alternative taken.
    "what's [the] time" -> "what's the time"; "set a|an timer" -> "set a timer"; "<duration> timer" -> None."""
    if "<" in pattern or not pattern.strip():
        return None
    words = []
    for tok in pattern.split():
        tok = tok.strip("[]").split("|")[0]
        if tok:
            words.append(tok)
    return " ".join(words) or None


def count_label(n_cmd: int, n_phr: int) -> str:
    return f"{n_cmd} commands · {n_phr} ways to say them"


class _Row:
    def __init__(self, tab: "CommandsTab", section: str, phrases: str, help_: str):
        self.section, self.phrases, self.help = section, phrases, help_
        self.legend = section == LEGEND
        self.n_patterns = 0 if self.legend else phrases.count(" / ") + 1
        self.haystack = f"{phrases}\n{help_}".lower()
        self.try_phrase = None if self.legend else next(
            (s for s in (sayable(p) for p in phrases.split(" / ")) if s), None)
        f = self.frame = tk.Frame(tab.body)
        f.grid_columnconfigure(1, weight=1)
        text = phrases if self.legend else f'"Xyrus, {phrases}"'
        self.phrase_lbl = tk.Label(f, text=text, font=tab.f_mono, anchor="w", justify="left")
        self.help_lbl = tk.Label(f, text=help_, font=tab.f_help, anchor="w", justify="left")
        self.recolor()
        self.phrase_lbl.grid(row=0, column=0, sticky="nw", padx=(0, 10), pady=3)
        self.help_lbl.grid(row=0, column=1, sticky="nw", pady=3)
        self.try_btn = None
        if self.try_phrase:
            self.try_btn = ctk.CTkButton(f, text="Try", width=44, height=22, corner_radius=6, fg_color=LINE,
                                         hover_color=ACCENT_HOVER, text_color=TEXT, font=tab.f_btn,
                                         command=lambda: tab.try_phrase(self.try_phrase))
            self.try_btn.grid(row=0, column=2, sticky="ne", padx=(8, 2), pady=2)

    def recolor(self):
        """Plain tk labels: resolve the (light, dark) pairs for the current mode."""
        bg = c(BG)
        self.frame.configure(bg=bg)
        self.phrase_lbl.configure(bg=bg, fg=c(TEXT))
        self.help_lbl.configure(bg=bg, fg=c(MUTED))

    def matches(self, q: str) -> bool:
        return not q or q in self.haystack

    def wrap(self, width: int):
        pw = max(160, int(width * 0.46))
        self.phrase_lbl.configure(wraplength=pw)
        self.help_lbl.configure(wraplength=max(120, width - pw - 80))


class CommandsTab(ctk.CTkFrame):
    def __init__(self, master, app):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self.f_mono = ctk.CTkFont(family=MONO, size=13)
        self.f_help = ctk.CTkFont(size=12)
        self.f_btn = ctk.CTkFont(size=11)
        self.f_head = ctk.CTkFont(size=15, weight="bold")

        top = ctk.CTkFrame(self, fg_color="transparent")
        top.pack(fill="x", padx=8, pady=(8, 0))
        self.count = ctk.CTkLabel(top, text="", text_color=MUTED)
        self.count.pack(side="left", padx=4)
        self.filter = ctk.CTkEntry(top, width=240, placeholder_text="filter…", fg_color=BG, border_color=LINE,
                                   text_color=TEXT)
        self.filter.pack(side="right", padx=4)
        self.filter.bind("<KeyRelease>", lambda e: self._schedule_filter(), add="+")
        self.filter.bind("<Escape>", lambda e: (self.filter.delete(0, "end"), self.apply_filter()), add="+")
        self.flash = ctk.CTkLabel(top, text="", text_color=FAINT, font=self.f_help)
        self.flash.pack(side="right", padx=10)

        self.scroll = ctk.CTkScrollableFrame(self, fg_color=BG, scrollbar_button_color=LINE,
                                             scrollbar_button_hover_color=LINE_HOVER)
        self.scroll.pack(fill="both", expand=True, padx=8, pady=8)
        self.body = self.scroll                       # rows are packed straight into the scrollable frame
        self.headers: dict[str, ctk.CTkLabel] = {}
        self.rows: list[_Row] = []
        self.empty = ctk.CTkLabel(self.body, text="No command matches that.", text_color=MUTED)
        self._version = None
        self._filter_job = None
        self._width = 0
        self.scroll.bind("<Configure>", self._resized, add="+")
        self.rebuild()

    # ------------------------------------------------------------- building --
    def _sections(self):
        try:
            return self.app.registry.sections(self.app.config)
        except Exception:
            log.exception("registry.sections failed")
            return []

    def _registry_version(self):
        try:
            return self.app.registry.version()
        except Exception:
            return None

    def rebuild(self):
        """(Re)create every row from registry.sections(config)."""
        self._version = self._registry_version()
        for w in list(self.headers.values()):
            w.destroy()
        for r in self.rows:
            r.frame.destroy()
        self.headers, self.rows = {}, []
        for section, rows in self._sections():
            self.headers[section] = ctk.CTkLabel(self.body, text=section, font=self.f_head, text_color=ACCENT,
                                                 anchor="w")
            for phrases, help_ in rows:
                self.rows.append(_Row(self, section, phrases, help_))
        if self._width:
            for r in self.rows:
                r.wrap(self._width)
        self.apply_filter()

    def on_theme(self):
        for r in self.rows:
            r.recolor()

    def poll(self):
        """Re-render when the registry changed (custom commands saved, apps edited)."""
        v = self._registry_version()
        if v != self._version:
            self.rebuild()

    # -------------------------------------------------------------- filtering --
    def _schedule_filter(self):
        if self._filter_job:
            self.after_cancel(self._filter_job)
        self._filter_job = self.after(120, self.apply_filter)

    def apply_filter(self):
        self._filter_job = None
        q = self.filter.get().strip().lower()
        for w in self.headers.values():
            w.pack_forget()
        for r in self.rows:
            r.frame.pack_forget()
        self.empty.pack_forget()
        n_cmd = n_phr = 0
        shown_any = False
        for section, header in self.headers.items():
            rows = [r for r in self.rows if r.section == section and r.matches(q)]
            if not rows:
                continue
            shown_any = True
            header.pack(fill="x", padx=12, pady=(12, 4))
            for r in rows:
                r.frame.pack(fill="x", padx=12, pady=0)
                if not r.legend:
                    n_cmd += 1
                    n_phr += r.n_patterns
        if not shown_any:
            self.empty.pack(pady=30)
        self.count.configure(text=count_label(n_cmd, n_phr))
        try:
            self.scroll._parent_canvas.yview_moveto(0)
        except Exception:
            pass

    def visible_rows(self) -> list[_Row]:
        return [r for r in self.rows if r.frame.winfo_manager()]

    # ---------------------------------------------------------------- actions --
    def try_phrase(self, phrase: str):
        try:
            self.app.engine.submit_text(phrase, "typed")
            self.flash.configure(text=f"sent “{phrase}” — see Home")
        except Exception:
            log.exception("Try %r failed", phrase)
            self.flash.configure(text="could not send that")
        self.after(4000, lambda: self.flash.configure(text=""))

    def _resized(self, event):
        w = int(event.width)
        if abs(w - self._width) > 8:
            self._width = w
            for r in self.rows:
                r.wrap(w)
