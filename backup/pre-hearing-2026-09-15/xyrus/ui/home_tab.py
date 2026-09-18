"""Home tab: conversation feed (newest at the bottom, max 200 rows, noise hidden unless "Show noise"),
text command box with history, running timers with Cancel, and a rotating "Try saying" card.

Feed rows are plain tk widgets from a recycled pool (customtkinter widgets are ~10x slower to create
and the feed can receive 300 events at once on startup); their colours are resolved per mode with
theme.c() and re-applied by on_theme().
"""
from __future__ import annotations

import logging
import random
import re
import tkinter as tk
from collections import deque

import customtkinter as ctk

from xyrus.ui import cfg_get, cfg_set, open_path
from xyrus.ui.commands_tab import sayable
from xyrus.ui.theme import (ACCENT, ACCENT_HOVER, AMBER, BG, CARD, FAINT, GREEN, LINE, LINE_HOVER, MONO,
                            MUTED, ON_ACCENT, RED, TEXT, c)

log = logging.getLogger("xyrus.ui.home")

MAX_ROWS = 200
HISTORY = 20
TRY_EVERY_MS = 8000
STYLE = {                     # kind -> (glyph, colour)   (§4.15)
    "heard": ("", MUTED),
    "noise": ("noise ·", FAINT),
    "cmd": ("→", ACCENT),
    "say": ("💬", TEXT),
    "ask": ("?", ACCENT),
    "alert": ("!", AMBER),
    "remind": ("🔔", ACCENT),
    "info": ("✓", GREEN),
    "error": ("✕", RED),
}
PATHLIKE = re.compile(r"^(?:[a-zA-Z]:[\\/]|\\\\|https?://|shell:)")
DURATION_NAME = re.compile(r"^(\d+) (second|minute|hour)s?( \d+)?$")


def fmt_secs(s: int) -> str:
    s = max(0, int(s))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


def timer_title(name: str) -> str:
    """Display name of a timer: unnamed timers are called '5 minute' / '90 second' / '5 minute 2' by
    TimerService (that form reads well in speech: 'the 5 minute timer'); on screen they are pluralised:
    '5 minutes', '90 seconds', '1 hour', '5 minutes (2)'. Named timers ('tea') are shown as they are."""
    m = DURATION_NAME.match(name or "")
    if not m:
        return name
    n, unit, dup = int(m.group(1)), m.group(2), m.group(3)
    text = f"{n} {unit}{'' if n == 1 else 's'}"
    return f"{text} ({dup.strip()})" if dup else text


class _FeedRow:
    def __init__(self, tab: "HomeTab"):
        self.tab = tab
        self.event = None
        f = self.frame = tk.Frame(tab.feed, bg=c(BG))
        f.grid_columnconfigure(2, weight=1)
        self.ts = tk.Label(f, font=tab.f_ts, anchor="nw", width=8)
        self.glyph = tk.Label(f, font=tab.f_row, anchor="nw", width=2)
        self.text = tk.Label(f, font=tab.f_row, anchor="w", justify="left", wraplength=tab._wrap)
        self.ts.grid(row=0, column=0, sticky="nw", padx=(4, 2), pady=2)
        self.glyph.grid(row=0, column=1, sticky="nw", pady=2)
        self.text.grid(row=0, column=2, sticky="nw", padx=(2, 4), pady=2)
        self.open_btn = None

    def show(self, ev):
        """Fill the row for an event; also re-applies every colour for the current mode."""
        self.event = ev
        glyph, color = STYLE.get(ev.kind, ("", TEXT))
        text = f"“{ev.text}”" if ev.kind in ("heard", "noise") else ev.text
        if ev.kind == "noise":
            glyph, text = "", f"noise · {ev.text}"
        bg = c(BG)
        self.frame.configure(bg=bg)
        self.ts.configure(text=ev.ts, bg=bg, fg=c(FAINT))
        self.glyph.configure(text=glyph, bg=bg, fg=c(color))
        self.text.configure(text=text, bg=bg, fg=c(TEXT if ev.kind == "say" else color),
                            font=self.tab.f_noise if ev.kind == "noise" else self.tab.f_row)
        path = ev.meta if ev.kind == "info" and ev.meta and PATHLIKE.match(ev.meta) else None
        if path:
            if self.open_btn is None:
                self.open_btn = ctk.CTkButton(self.frame, text="Open", width=48, height=20, corner_radius=6,
                                              fg_color=LINE, hover_color=ACCENT_HOVER, text_color=TEXT,
                                              bg_color=BG, font=self.tab.f_small)
            self.open_btn.configure(command=lambda p=path: self.tab.open(p))
            self.open_btn.grid(row=0, column=3, sticky="ne", padx=(4, 6), pady=1)
        elif self.open_btn is not None:
            self.open_btn.grid_remove()


class HomeTab(ctk.CTkFrame):
    def __init__(self, master, app):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self.f_row = ctk.CTkFont(size=13)
        self.f_noise = ctk.CTkFont(size=12, slant="italic")
        self.f_ts = ctk.CTkFont(family=MONO, size=11)
        self.f_small = ctk.CTkFont(size=11)
        self.f_title = ctk.CTkFont(size=13, weight="bold")
        self._wrap = 500
        self.rows: deque[_FeedRow] = deque()
        self._last_event = None
        self._events: tuple = ()
        self._timers: tuple | None = None
        self._timer_rows: list[dict] = []
        self.history: list[str] = []
        self._hist_i = 0
        self.show_noise = bool(cfg_get(app.config, "window.show_noise", False))

        self.grid_columnconfigure(0, weight=1)
        self.grid_columnconfigure(1, weight=0, minsize=270)
        self.grid_rowconfigure(0, weight=1)
        self._build_feed()
        self._build_side()
        self._build_input()
        self._rotate_try()

    # ------------------------------------------------------------- building --
    def _build_feed(self):
        card = ctk.CTkFrame(self, fg_color=BG, corner_radius=12)
        card.grid(row=0, column=0, sticky="nsew", padx=(8, 4), pady=(8, 4))
        head = ctk.CTkFrame(card, fg_color="transparent")
        head.pack(fill="x", padx=12, pady=(8, 0))
        ctk.CTkLabel(head, text="Conversation", font=self.f_title, text_color=TEXT).pack(side="left")
        self.noise_sw = ctk.CTkSwitch(head, text="Show noise", font=self.f_small, text_color=MUTED,
                                      progress_color=ACCENT, switch_width=32, switch_height=16,
                                      command=self._toggle_noise)
        if self.show_noise:
            self.noise_sw.select()
        self.noise_sw.pack(side="right")
        self.feed = ctk.CTkScrollableFrame(card, fg_color=BG, scrollbar_button_color=LINE,
                                           scrollbar_button_hover_color=LINE_HOVER)
        self.feed.pack(fill="both", expand=True, padx=4, pady=(2, 6))
        self.feed.bind("<Configure>", self._feed_resized, add="+")
        self.placeholder = ctk.CTkLabel(self.feed, text='Say "Xyrus" then a command, or type one below.\n'
                                                        "Everything heard and said shows up here.",
                                        text_color=MUTED, justify="center")
        self.placeholder.pack(pady=40)

    def _build_side(self):
        side = ctk.CTkFrame(self, fg_color="transparent")
        side.grid(row=0, column=1, sticky="nsew", padx=(4, 8), pady=(8, 4))
        side.grid_columnconfigure(0, weight=1)

        tcard = ctk.CTkFrame(side, fg_color=BG, corner_radius=12)
        tcard.grid(row=0, column=0, sticky="ew")
        ctk.CTkLabel(tcard, text="Timers", font=self.f_title, text_color=TEXT).pack(anchor="w", padx=12, pady=(8, 2))
        self.timer_box = ctk.CTkFrame(tcard, fg_color="transparent")
        self.timer_box.pack(fill="x", padx=8, pady=(0, 8))
        self.timer_box.grid_columnconfigure(0, weight=1)
        self.no_timers = ctk.CTkLabel(self.timer_box, text='None running. Try "set a timer for five minutes".',
                                      text_color=MUTED, font=self.f_small, wraplength=230, justify="left")
        self.no_timers.grid(row=0, column=0, sticky="w", padx=4)

        ycard = ctk.CTkFrame(side, fg_color=BG, corner_radius=12)
        ycard.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        ctk.CTkLabel(ycard, text="Try saying", font=self.f_title, text_color=TEXT).pack(anchor="w", padx=12, pady=(8, 2))
        self.try_phrase = ctk.CTkLabel(ycard, text="", font=ctk.CTkFont(family=MONO, size=13), text_color=ACCENT,
                                       wraplength=240, justify="left", anchor="w")
        self.try_phrase.pack(fill="x", padx=12)
        self.try_help = ctk.CTkLabel(ycard, text="", font=self.f_small, text_color=MUTED, wraplength=240,
                                     justify="left", anchor="w")
        self.try_help.pack(fill="x", padx=12, pady=(2, 4))
        self.try_btn = ctk.CTkButton(ycard, text="Try it", width=64, height=24, corner_radius=6, fg_color=LINE,
                                     hover_color=ACCENT_HOVER, text_color=TEXT, font=self.f_small,
                                     command=self._try_current)
        self.try_btn.pack(anchor="w", padx=12, pady=(0, 10))
        self._try_text = None

    def _build_input(self):
        bar = ctk.CTkFrame(self, fg_color=CARD, corner_radius=12)
        bar.grid(row=1, column=0, columnspan=2, sticky="ew", padx=8, pady=(4, 8))
        bar.grid_columnconfigure(0, weight=1)
        self.entry = ctk.CTkEntry(bar, height=34, corner_radius=8, fg_color=BG, border_color=LINE, text_color=TEXT,
                                  placeholder_text="Type a command… (Enter to send, ↑ for history)")
        self.entry.grid(row=0, column=0, sticky="ew", padx=(8, 6), pady=8)
        ctk.CTkButton(bar, text="Send", width=70, height=34, corner_radius=8, fg_color=ACCENT,
                      hover_color=ACCENT_HOVER, text_color=ON_ACCENT,
                      command=self.submit).grid(row=0, column=1, padx=(0, 8), pady=8)
        self.entry.bind("<Return>", lambda e: self.submit())
        self.entry.bind("<KP_Enter>", lambda e: self.submit())
        self.entry.bind("<Up>", lambda e: self._history(-1))
        self.entry.bind("<Down>", lambda e: self._history(1))

    # ----------------------------------------------------------------- theme --
    def on_theme(self):
        """Light/Dark switched: the pooled tk rows re-resolve their colours."""
        for r in self.rows:
            if r.event is not None:
                r.show(r.event)

    # ----------------------------------------------------------------- input --
    def submit(self, text: str | None = None):
        text = (self.entry.get() if text is None else text).strip()
        if not text:
            return
        try:
            self.app.engine.submit_text(text, "typed")
        except Exception:
            log.exception("submit_text failed")
        if not self.history or self.history[-1] != text:
            self.history.append(text)
            del self.history[:-HISTORY]
        self._hist_i = len(self.history)
        self.entry.delete(0, "end")

    def _history(self, step: int):
        if not self.history:
            return "break"
        self._hist_i = max(0, min(len(self.history), self._hist_i + step))
        self.entry.delete(0, "end")
        if self._hist_i < len(self.history):
            self.entry.insert(0, self.history[self._hist_i])
        return "break"

    # ------------------------------------------------------------------ feed --
    def _visible(self, ev) -> bool:
        return self.show_noise or ev.kind != "noise"

    def update_snapshot(self, snap):
        """Called by the window poll when the snapshot changed."""
        self._update_feed(snap.events)
        self._update_timers(snap.timers)

    def _update_feed(self, events: tuple):
        self._events = events
        if not events:
            return
        last = self._last_event
        if last is not None and events[-1] is last:
            return
        start = 0
        if last is not None:
            idx = next((i for i in range(len(events) - 1, -1, -1) if events[i] is last), None)
            if idx is None:
                idx = next((i for i in range(len(events) - 1, -1, -1) if events[i] == last), None)
            start = 0 if idx is None else idx + 1
        new = [e for e in events[start:] if self._visible(e)]
        self._last_event = events[-1]
        self._append(new[-MAX_ROWS:])

    def _append(self, new):
        if not new:
            return
        at_bottom = self._at_bottom()
        self.placeholder.pack_forget()
        for ev in new:
            if len(self.rows) >= MAX_ROWS:
                row = self.rows.popleft()          # recycle the oldest row
                row.frame.pack_forget()
            else:
                row = _FeedRow(self)
            row.show(ev)
            row.frame.pack(fill="x", anchor="w")
            self.rows.append(row)
        if at_bottom:
            # the scrollregion is only updated once the inner frame's <Configure> ran, so scroll after layout
            self.after_idle(self._scroll_bottom)
            self.after(80, self._scroll_bottom)

    def _rerender(self):
        for r in self.rows:
            r.frame.pack_forget()
            r.frame.destroy()
        self.rows.clear()
        self._last_event = None
        if not self._events:
            self.placeholder.pack(pady=40)
            return
        self._update_feed(self._events)
        if not self.rows:
            self.placeholder.pack(pady=40)

    def _toggle_noise(self):
        self.show_noise = bool(self.noise_sw.get())
        try:
            cfg_set(self.app.config, "window.show_noise", self.show_noise)
        except Exception:
            log.exception("saving show_noise failed")
        self._rerender()

    def row_count(self) -> int:
        return len(self.rows)

    def _at_bottom(self) -> bool:
        try:
            return self.feed._parent_canvas.yview()[1] >= 0.98
        except Exception:
            return True

    def _scroll_bottom(self):
        try:
            self.feed._parent_canvas.yview_moveto(1.0)
        except Exception:
            pass

    def _feed_resized(self, event):
        wrap = max(200, int(event.width) - 150)
        if abs(wrap - self._wrap) > 8:
            self._wrap = wrap
            for r in self.rows:
                r.text.configure(wraplength=wrap)

    def open(self, path: str):
        try:
            open_path(path)
        except Exception:
            log.exception("open %s failed", path)

    # ---------------------------------------------------------------- timers --
    def _update_timers(self, timers: tuple):
        if timers == self._timers:
            return
        self._timers = timers
        while len(self._timer_rows) < len(timers):
            i = len(self._timer_rows)
            lbl = ctk.CTkLabel(self.timer_box, text="", anchor="w", font=ctk.CTkFont(family=MONO, size=13))
            btn = ctk.CTkButton(self.timer_box, text="Cancel", width=64, height=24, corner_radius=6,
                                fg_color=LINE, hover_color=RED, text_color=TEXT, font=self.f_small)
            self._timer_rows.append({"label": lbl, "button": btn, "i": i})
        for i, row in enumerate(self._timer_rows):
            if i < len(timers):
                t = timers[i]
                title = timer_title(t.name)
                if t.ringing:
                    row["label"].configure(text=f"{title} · ringing", text_color=AMBER)
                    row["button"].configure(text="Stop", command=self._stop_ringing)
                else:
                    row["label"].configure(text=f"{title} · {fmt_secs(t.remaining_s)}", text_color=TEXT)
                    row["button"].configure(text="Cancel", command=lambda n=t.name: self._cancel_timer(n))
                row["label"].grid(row=i, column=0, sticky="w", padx=4, pady=2)
                row["button"].grid(row=i, column=1, sticky="e", padx=4, pady=2)
            else:
                row["label"].grid_remove()
                row["button"].grid_remove()
        if timers:
            self.no_timers.grid_remove()
        else:
            self.no_timers.grid(row=0, column=0, sticky="w", padx=4)

    def _cancel_timer(self, name: str):
        eng = self.app.engine
        # SPEC-AMBIGUITY: the App surface has no cancel-timer call; TimerService is engine-thread only,
        # so the cancel is posted to T-engine (Engine.post is thread-safe, §4.7).
        eng.post(lambda: eng.timers.cancel(name=name))

    def _stop_ringing(self):
        eng = self.app.engine
        eng.post(lambda: eng.timers.stop_ringing())

    # ------------------------------------------------------------ try saying --
    def _candidates(self) -> list[tuple[str, str]]:
        out = []
        try:
            for cmd in self.app.registry.commands():
                if getattr(cmd, "destructive", False) or getattr(cmd, "section", "") == "Power":
                    continue
                for p in cmd.patterns:
                    s = sayable(p.text)
                    if s:
                        out.append((s, cmd.help))
        except Exception:
            log.debug("registry not ready for Try saying", exc_info=True)
        return out

    def _rotate_try(self):
        cands = self._candidates()
        if cands:
            choices = [x for x in cands if x[0] != self._try_text] or cands
            phrase, help_ = random.choice(choices)
            self._try_text = phrase
            self.try_phrase.configure(text=f'"Xyrus, {phrase}"')
            self.try_help.configure(text=help_)
        self.after(TRY_EVERY_MS, self._rotate_try)

    def _try_current(self):
        if self._try_text:
            self.submit(self._try_text)
