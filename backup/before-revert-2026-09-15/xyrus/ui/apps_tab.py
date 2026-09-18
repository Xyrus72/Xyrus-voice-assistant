"""Apps tab: the editable spoken-name -> target list (v1), each name checked against the speech model's
vocabulary (red hint), Save -> config.set("apps", ...). Below: the read-only Start Menu index with a filter
(shown when the app exposes one)."""
from __future__ import annotations

import logging
import re

import customtkinter as ctk

from xyrus.ui.theme import ACCENT, ACCENT_HOVER, AMBER, BG, GREEN, LINE, LINE_HOVER, MUTED, RED, TEXT

log = logging.getLogger("xyrus.ui.apps")

WAKE_LIKE = {"xyrus", "cyrus", "zeros", "virus", "cirrus", "sirius", "serious"}


def clean_name(name: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w' ]", " ", name.lower())).strip()


def name_problem(name: str, vocab, wake_words=()) -> str:
    """'' if the spoken name is usable, else a short reason (shown in red under the row)."""
    words = clean_name(name).split()
    if not words:
        return ""
    wake = WAKE_LIKE | {w.lower() for w in wake_words or ()}
    hit = next((w for w in words if w in wake), None)
    if hit:
        return f"'{hit}' sounds like the wake word — pick another name."
    if vocab is not None:
        try:
            unknown = vocab.unknown_words(" ".join(words))
        except Exception:
            log.debug("vocab check failed", exc_info=True)
            unknown = []
        if unknown:
            return f"The speech model doesn't know '{unknown[0]}' — pick another word."
    return ""


class AppsTab(ctk.CTkFrame):
    def __init__(self, master, app):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self.rows: list[dict] = []
        self._jobs: dict[int, str] = {}
        small = ctk.CTkFont(size=12)

        top = ctk.CTkFrame(self, fg_color="transparent")
        top.pack(fill="x", padx=8, pady=(8, 0))
        ctk.CTkLabel(top, text='Say "Xyrus, open <name>". A target can be an .exe, a URL, a URI like spotify: '
                               "or a shell: folder.", text_color=MUTED).pack(side="left", padx=4)
        ctk.CTkButton(top, text="Save", width=80, fg_color=ACCENT, hover_color=ACCENT_HOVER,
                      command=self.save).pack(side="right", padx=4)
        ctk.CTkButton(top, text="+ Add", width=80, fg_color=LINE, hover_color=LINE_HOVER, text_color=TEXT,
                      command=lambda: self.add_row("", "", focus=True)).pack(side="right", padx=4)

        self.body = ctk.CTkFrame(self, fg_color="transparent")
        self.body.pack(fill="both", expand=True)
        self.body.grid_columnconfigure(0, weight=1)
        self.body.grid_rowconfigure(0, weight=3)

        self.list = ctk.CTkScrollableFrame(self.body, fg_color=BG, scrollbar_button_color=LINE,
                                           scrollbar_button_hover_color=LINE_HOVER)
        self.list.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)
        self.list.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(self.list, text="say", width=190, anchor="w", text_color=MUTED, font=small).grid(
            row=0, column=0, sticky="w", padx=(4, 0))
        ctk.CTkLabel(self.list, text="opens", anchor="w", text_color=MUTED, font=small).grid(
            row=0, column=1, sticky="w", padx=8)
        self._next_row = 1
        apps = app.config.get("apps", {}) or {}
        for name, target in apps.items():
            self.add_row(name, target)

        self.msg = ctk.CTkLabel(self, text="", text_color=GREEN, anchor="w")
        self.msg.pack(fill="x", padx=12, pady=(0, 6))
        self._build_index()

    # ----------------------------------------------------------------- rows --
    def add_row(self, name: str, target: str, focus: bool = False) -> dict:
        r = self._next_row
        self._next_row += 2
        e1 = ctk.CTkEntry(self.list, width=190, placeholder_text="name", fg_color=CARD_BG, border_color=LINE)
        e2 = ctk.CTkEntry(self.list, placeholder_text="chrome  /  https://…  /  spotify:", fg_color=CARD_BG,
                          border_color=LINE)
        btn = ctk.CTkButton(self.list, text="✕", width=32, fg_color=LINE, hover_color=RED, text_color=TEXT)
        hint = ctk.CTkLabel(self.list, text="", text_color=RED, anchor="w", font=ctk.CTkFont(size=11), height=14)
        e1.grid(row=r, column=0, sticky="ew", padx=(4, 0), pady=(3, 0))
        e2.grid(row=r, column=1, sticky="ew", padx=8, pady=(3, 0))
        btn.grid(row=r, column=2, padx=(0, 4), pady=(3, 0))
        row = {"name": e1, "target": e2, "delete": btn, "hint": hint, "r": r}
        btn.configure(command=lambda: self.remove_row(row))
        if name:
            e1.insert(0, name)
        if target:
            e2.insert(0, target)
        e1.bind("<KeyRelease>", lambda e: self._schedule_check(row), add="+")
        self.rows.append(row)
        self.check_row(row)
        if focus:
            e1.focus_set()
            self.after_idle(lambda: self.list._parent_canvas.yview_moveto(1.0))
        return row

    def remove_row(self, row: dict):
        self.rows = [r for r in self.rows if r is not row]
        for k in ("name", "target", "delete", "hint"):
            row[k].destroy()

    def _schedule_check(self, row: dict):
        key = id(row)
        if key in self._jobs:
            self.after_cancel(self._jobs[key])
        self._jobs[key] = self.after(350, lambda: (self._jobs.pop(key, None), self.check_row(row)))

    def check_row(self, row: dict) -> str:
        problem = name_problem(row["name"].get(), getattr(self.app, "vocab", None),
                               self.app.config.get("wake_words", ()) or ())
        row["hint"].configure(text=problem)
        if problem:
            row["hint"].grid(row=row["r"] + 1, column=0, columnspan=2, sticky="w", padx=6)
            row["name"].configure(border_color=RED)
        else:
            row["hint"].grid_remove()
            row["name"].configure(border_color=LINE)
        return problem

    def values(self) -> dict[str, str]:
        apps = {}
        for row in self.rows:
            n, t = clean_name(row["name"].get()), row["target"].get().strip()
            if n and t:
                apps[n] = t
        return apps

    def save(self):
        apps = self.values()
        bad = sum(1 for row in self.rows if row["target"].get().strip() and self.check_row(row))
        try:
            self.app.config.set("apps", apps)
        except Exception as e:
            log.exception("saving apps failed")
            self.msg.configure(text=f"Could not save: {e}", text_color=RED)
            return
        text = f"Saved {len(apps)} apps — Xyrus now knows the new names."
        if bad:
            text += f"  {bad} name{'s' if bad != 1 else ''} can only be typed, not said (see the red hints)."
        self.msg.configure(text=text, text_color=AMBER if bad else GREEN)
        self.after(6000, lambda: self.msg.configure(text=""))

    # ------------------------------------------------------- Start Menu index --
    def _index(self):
        idx = getattr(self.app, "app_index", None)
        if idx is None:
            idx = getattr(getattr(self.app, "engine", None), "app_index", None)
        return idx if idx is not None and hasattr(idx, "names") else None

    def _build_index(self):
        self.index_card = None
        idx = self._index()
        if idx is None:
            return
        # SPEC-AMBIGUITY: §9.1's App has no app_index; the Start Menu list is shown when app.app_index or
        # app.engine.app_index exists (StartMenuIndex.names()).
        self.body.grid_rowconfigure(1, weight=2)
        card = self.index_card = ctk.CTkFrame(self.body, fg_color=BG, corner_radius=12)
        card.grid(row=1, column=0, sticky="nsew", padx=8, pady=(0, 8))
        head = ctk.CTkFrame(card, fg_color="transparent")
        head.pack(fill="x", padx=10, pady=(8, 4))
        ctk.CTkLabel(head, text="Start Menu apps", font=ctk.CTkFont(size=13, weight="bold"),
                     text_color=TEXT).pack(side="left")
        ctk.CTkLabel(head, text='  say "open <name>" — no alias needed', text_color=MUTED,
                     font=ctk.CTkFont(size=12)).pack(side="left")
        self.index_filter = ctk.CTkEntry(head, width=200, placeholder_text="filter…", fg_color=CARD_BG,
                                         border_color=LINE)
        self.index_filter.pack(side="right")
        self.index_filter.bind("<KeyRelease>", lambda e: self.render_index(), add="+")
        self.index_box = ctk.CTkTextbox(card, fg_color=BG, text_color=MUTED, wrap="none", height=80)
        self.index_box.pack(fill="both", expand=True, padx=6, pady=(0, 8))
        self.render_index()

    def render_index(self):
        idx = self._index()
        if idx is None or self.index_card is None:
            return
        try:
            names = sorted(idx.names(), key=str.lower)
        except Exception:
            names = []
        q = self.index_filter.get().strip().lower()
        shown = [n for n in names if q in n.lower()]
        self.index_box.configure(state="normal")
        self.index_box.delete("1.0", "end")
        self.index_box.insert("end", "\n".join(shown) if shown else "(nothing matches)")
        self.index_box.configure(state="disabled")


CARD_BG = BG
