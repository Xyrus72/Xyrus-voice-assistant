"""Calendar tab of the Xyrus window: month grid, day agenda (to-dos, events, notes), quick-add and
the next-7-days list.

Everything here runs on the Tk main thread. The data lives in calendar_store.CalendarStore, which the
voice thread also changes; poll() (called from XyrusWindow._poll every 150 ms) compares store.version
and re-renders only what is stale. customtkinter widgets are slow to create, so the 42 day cells are
built once and re-configured, and agenda / upcoming rows come from pools that only ever grow.
"""
from __future__ import annotations

import datetime as dt
import re

import customtkinter as ctk

import when as W
from ui import ACCENT, ACCENT_HOVER, GREEN, RED, MUTED, CARD, BG

ACCENT_SOFT = "#a996ff"      # events on dark cells / time column
AMBER = "#f2b84b"            # notes
SURFACE = "#1d1d25"          # a day cell of the shown month
SURFACE_HOVER = "#272731"
DIM = "#4d4d59"              # numbers of the neighbouring months
RANGE = "#262042"            # cells of a highlighted range ("this week")
LINE = "#2b2b36"             # neutral buttons (same grey as ui.py's secondary buttons)
TEXT = "#ececf2"
FAINT = "#6e6e7a"

REMINDERS = [("No reminder", None), ("At time", 0), ("10 min before", 10),
             ("30 min before", 30), ("1 hour before", 60)]
KINDS = ("To-do", "Event", "Note")
PLACEHOLDERS = {"To-do": "e.g. buy milk, call mom tomorrow at 5",
                "Event": "e.g. dentist 3pm, gym every monday at 6pm",
                "Note": "e.g. wifi password is on the router"}
MAX_ROWS = 60                # agenda rows drawn for one day before "... and N more"
UP_ROWS = 10                 # upcoming rows in the pool
RELATIVE_TIME = re.compile(r"\bin\s+(?:\w+\s+)?(?:minutes?|mins?|hours?|hrs?)\b|\bin\s+half\s+an?\s+hour\b", re.I)


# ------------------------------------------------------------------ helpers -- #
def _date(x) -> dt.date:
    if isinstance(x, dt.datetime):
        return x.date()
    return x if isinstance(x, dt.date) else dt.date.fromisoformat(str(x))


def short_day(d: dt.date) -> str:
    """'Mon 14 Sep'."""
    return f"{d:%a} {d.day} {d:%b}"


def long_day(d: dt.date, today: dt.date) -> str:
    """'Sunday, 13 September' (+ year when it isn't this year)."""
    s = f"{d:%A}, {d.day} {d:%B}"
    return s if d.year == today.year else f"{s} {d.year}"


def rem_label(m) -> str:
    if m is None:
        return "No reminder"
    for label, val in REMINDERS:
        if val == m:
            return label
    if m % 1440 == 0:
        return f"{m // 1440} day{'s' if m != 1440 else ''} before"
    if m % 60 == 0:
        return f"{m // 60} hours before"
    return f"{m} min before"


def rem_value(label: str):
    for lab, val in REMINDERS:
        if lab == label:
            return val
    m = re.match(r"(\d+)\s*(min|hour|day)", label or "")
    if not m:
        return None
    return int(m.group(1)) * {"min": 1, "hour": 60, "day": 1440}[m.group(2)]


def rem_short(m) -> str:
    """Bell tag for an agenda row: '🔔', '🔔 10m', '🔔 1h'."""
    if m is None:
        return ""
    if m == 0:
        return "🔔"
    if m % 1440 == 0:
        return f"🔔 {m // 1440}d"
    if m % 60 == 0:
        return f"🔔 {m // 60}h"
    return f"🔔 {m}m"


def display(title: str) -> str:
    """Voice titles arrive lower-case; show them with a capital first letter (the data is untouched)."""
    return title[:1].upper() + title[1:] if title else title


def original_case(text: str, title: str) -> str:
    """parse_when lower-cases the title; take the same words from what was typed when possible."""
    i = text.lower().find(title)
    return text[i:i + len(title)] if i >= 0 else display(title)


def clip(s: str, n: int = 30) -> str:
    return s if len(s) <= n else s[:n - 1] + "…"


# --------------------------------------------------------------- day cell --- #
class _Cell:
    """One of the 42 month-grid cells: a CTkFrame (anti-aliased rounded rect) whose own canvas also
    carries the day number and the count dots, so a cell is a single widget."""

    def __init__(self, tab: "CalendarTab", parent):
        self.tab = tab
        self.frame = ctk.CTkFrame(parent, width=36, height=30, corner_radius=8, border_width=2,
                                  fg_color=SURFACE, border_color=SURFACE)
        self.cv = self.frame._canvas          # CTkFrame draws itself on this canvas; our text sits on top
        self.date = None
        self.state = None                    # last text state drawn - skip no-op redraws
        self.colors = (SURFACE, SURFACE)
        self.hover = False
        self.info = None
        self.cv.bind("<Button-1>", lambda e: self.date and tab.select(self.date), add="+")
        self.cv.bind("<Enter>", lambda e: self._set_hover(True), add="+")
        self.cv.bind("<Leave>", lambda e: self._set_hover(False), add="+")
        self.cv.bind("<MouseWheel>", lambda e: tab.shift_month(-1 if e.delta > 0 else 1), add="+")
        self.cv.bind("<Configure>", lambda e: self._draw_text(), add="+")

    def _set_hover(self, on):
        self.hover = on
        if self.info:
            self.show(*self.info)

    def show(self, d, in_month, is_today, selected, in_range, counts):
        self.info = (d, in_month, is_today, selected, in_range, counts)
        self.date = d
        if selected:
            fg = ACCENT
        elif self.hover:
            fg = SURFACE_HOVER
        elif in_range:
            fg = RANGE
        else:
            fg = SURFACE if in_month else BG
        border = ("#cfc4ff" if selected else ACCENT) if is_today else fg
        if (fg, border) != self.colors:
            self.colors = (fg, border)
            self.frame.configure(fg_color=fg, border_color=border)
            self.cv.tag_raise("cal")
        key = (d, in_month, is_today, selected, counts and tuple(sorted(counts.items())))
        if key != self.state:
            self.state = key
            self._draw_text()

    def _draw_text(self):
        cv = self.cv
        cv.delete("cal")
        if not self.info:
            return
        d, in_month, is_today, selected, _in_range, counts = self.info
        w, h = cv.winfo_width(), cv.winfo_height()
        if w < 10 or h < 10:
            return
        t = self.tab
        s = t.scale
        pad = round(7 * s)
        if selected:
            num = "white"
        elif is_today:
            num = ACCENT_SOFT
        else:
            num = TEXT if in_month else DIM
        num_id = cv.create_text(pad, round(4 * s), text=str(d.day), anchor="nw", fill=num,
                                font=t.cfont(12, "bold" if (is_today or selected) else "normal"), tags="cal")
        nb = cv.bbox(num_id)
        parts = []
        if counts:
            dim = not in_month and not selected
            ev = counts.get("event", 0)
            open_ = counts.get("task_open", 0)
            done = counts.get("task", 0) - open_
            nt = counts.get("note", 0)
            if ev:
                parts.append(("●", "white" if selected else (DIM if dim else ACCENT_SOFT), ev))
            if open_:
                parts.append(("●", DIM if dim else GREEN, open_))
            elif done:
                parts.append(("✓", "#d9d2ff" if selected else DIM if dim else MUTED, done))
            if nt:
                parts.append(("●", DIM if dim else AMBER, nt))
        if parts:
            compact = h < round(38 * s)                  # short cells: dots go bottom-right
            y = h - round(4 * s)
            for with_counts in (True, False):
                cv.delete("dots")
                if compact:
                    x = w - pad + round(2 * s)
                    for glyph, color, n in reversed(parts):
                        if with_counts and n > 1:
                            cv.create_text(x, y + 1, text=str(n), anchor="se", fill=color,
                                           font=t.cfont(9, "bold"), tags=("cal", "dots"))
                            x = cv.bbox("dots")[0] - 1
                        cv.create_text(x, y, text=glyph, anchor="se", fill=color,
                                       font=t.cfont(8), tags=("cal", "dots"))
                        x = cv.bbox("dots")[0] - round(2 * s)
                else:
                    x = pad - round(1 * s)
                    for glyph, color, n in parts:
                        cv.create_text(x, y, text=glyph, anchor="sw", fill=color,
                                       font=t.cfont(8), tags=("cal", "dots"))
                        x = cv.bbox("dots")[2] + 1
                        if with_counts and n > 1:
                            cv.create_text(x, y + 1, text=str(n), anchor="sw", fill=color,
                                           font=t.cfont(9, "bold"), tags=("cal", "dots"))
                            x = cv.bbox("dots")[2]
                        x += round(3 * s)
                b = cv.bbox("dots")
                clear_of_number = b[0] > nb[2] or b[1] >= nb[3] - round(3 * s)
                if b[0] >= 1 and b[2] <= w - 1 and clear_of_number:
                    break
        cv.tag_raise("cal")


# ------------------------------------------------------------ agenda row --- #
class _Row:
    """A pooled agenda row: [tick] [time / ✎] title ........ [🔔 10m ↻ weekly] [✕]."""

    def __init__(self, tab: "CalendarTab", parent):
        self.tab = tab
        self.kind = None           # "task" | "event" | "note"
        self.obj = None            # the item / note dict this row shows
        self.editor = None
        f = self.frame = ctk.CTkFrame(parent, fg_color="transparent", corner_radius=0, height=30)
        f.grid_columnconfigure(2, weight=1)
        self.check = ctk.CTkCheckBox(f, text="", width=22, checkbox_width=18, checkbox_height=18,
                                     corner_radius=5, border_width=2, fg_color=GREEN, hover_color="#2fb86c",
                                     border_color="#5b5b68", checkmark_color=BG,
                                     command=lambda: tab._toggle(self))
        self.lead = ctk.CTkLabel(f, text="", width=62, anchor="w", font=tab.f_lead)
        self.title = ctk.CTkLabel(f, text="", anchor="w", justify="left", font=tab.f_row, text_color=TEXT)
        self.meta = ctk.CTkLabel(f, text="", anchor="e", font=tab.f_small, text_color=MUTED)
        self.delete = ctk.CTkButton(f, text="✕", width=24, height=24, corner_radius=6, fg_color="transparent",
                                    hover_color=RED, text_color=FAINT, font=tab.f_small,
                                    command=lambda: tab._delete(self))
        self.check.grid(row=0, column=0, padx=(6, 2), sticky="w")
        self.lead.grid(row=0, column=1, padx=(4, 4), sticky="w")
        self.title.grid(row=0, column=2, sticky="ew")
        self.meta.grid(row=0, column=3, padx=(6, 2), sticky="e")
        self.delete.grid(row=0, column=4, padx=(2, 4))
        self.title.bind("<Double-Button-1>", lambda e: tab._start_edit(self))
        self._shape = None
        self._wrapped = None

    def fit(self):
        """Wrap the title so the tick / time, the meta tags and ✕ always keep their room."""
        lw = self.tab._list_w
        if not lw or not self.obj:
            return
        used = 34 + 18 + (30 if self._shape == "task" else 72 if self._shape == "event" else 30)
        meta = self.meta.cget("text")
        if meta:
            used += self.tab.f_small.measure(meta) + 14
        wrap = max(60, lw - used)
        if wrap != self._wrapped:
            self._wrapped = wrap
            self.title.configure(wraplength=wrap)

    def _layout(self, shape):
        """shape: 'task' (tick, no lead) | 'event' (time lead) | 'note' (narrow ✎ lead)."""
        if shape == self._shape:
            return
        self._shape = shape
        if shape == "task":
            self.check.grid()
            self.lead.grid_remove()
        else:
            self.check.grid_remove()
            self.lead.grid()
            self.lead.configure(width=62 if shape == "event" else 20)

    def show_task(self, it, overdue_day=None):
        self.kind, self.obj = "task", it
        self._layout("task")
        done = bool(it.get("done"))
        if done:
            self.check.select()
        else:
            self.check.deselect()
        self.title.configure(text=display(it["title"]), text_color=MUTED if done else TEXT,
                             font=self.tab.f_row_done if done else self.tab.f_row)
        meta = []
        if overdue_day:
            meta.append(overdue_day)
        if it.get("time"):
            meta.append(W.fmt_time(it["time"]))
        if it.get("reminder_min") is not None and not done:
            meta.append(rem_short(it["reminder_min"]))
        self.meta.configure(text="  ".join(meta), text_color=RED if overdue_day else MUTED)
        self.fit()

    def show_event(self, it):
        self.kind, self.obj = "event", it
        self._layout("event")
        self.lead.configure(text=W.fmt_time(it["time"]) if it.get("time") else "All day",
                            text_color=ACCENT_SOFT if it.get("time") else MUTED)
        self.title.configure(text=display(it["title"]), text_color=TEXT, font=self.tab.f_row)
        meta = []
        if it.get("reminder_min") is not None:
            meta.append(rem_short(it["reminder_min"]))
        if it.get("repeat"):
            meta.append(f"↻ {it['repeat']}")
        self.meta.configure(text="  ".join(meta), text_color=MUTED)
        self.fit()

    def show_note(self, nt):
        self.kind, self.obj = "note", nt
        self._layout("note")
        self.lead.configure(text="✎", text_color=AMBER)
        self.title.configure(text=nt["text"], text_color=TEXT, font=self.tab.f_row)
        self.meta.configure(text="")
        self.fit()

    def raw_text(self):
        return self.obj["text"] if self.kind == "note" else self.obj["title"]


# ------------------------------------------------------------------- tab ---- #
class CalendarTab(ctk.CTkFrame):
    """store: a CalendarStore (or None -> a disabled notice). now: clock callable (arc.now)."""

    def __init__(self, master, store, now=None):
        super().__init__(master, fg_color="transparent")
        self.store = store
        self._now = now or dt.datetime.now
        self.scale = ctk.ScalingTracker.get_widget_scaling(self)
        if store is None:
            self._build_unavailable()
            return

        today = self._now().date()
        self.today = today
        self.selected = today
        self.month = (today.year, today.month)
        self.range = None                     # (start, end) highlighted after "what do I have this week"
        self._seen_version = None
        self._minute = None
        self._dirty = {"grid", "day", "upcoming"}
        self._editing = None                  # the _Row being edited inline
        self._flash_job = None
        self._undo = None                     # ("item" | "note", dict) of the last delete
        self._rem_user = False                # the reminder menu was picked by hand
        self._up_fit = 3
        self._list_w = 0                      # agenda width (unscaled px) for title wrapping
        self._up_shown = True

        self.f_month = ctk.CTkFont(size=15, weight="bold")
        self.f_day = ctk.CTkFont(size=19, weight="bold")
        self.f_section = ctk.CTkFont(size=11, weight="bold")
        self.f_row = ctk.CTkFont(size=13)
        self.f_row_done = ctk.CTkFont(size=13, overstrike=True)
        self.f_lead = ctk.CTkFont(size=13, weight="bold")
        self.f_small = ctk.CTkFont(size=12)
        self.f_tiny = ctk.CTkFont(size=11)
        self._family = self.f_row.cget("family")
        self._cfonts = {}

        self.grid_columnconfigure(0, weight=9, uniform="cal")
        self.grid_columnconfigure(1, weight=11, uniform="cal")
        self.grid_rowconfigure(0, weight=1)
        left = ctk.CTkFrame(self, fg_color=BG, corner_radius=12)
        left.grid(row=0, column=0, sticky="nsew", padx=(8, 4), pady=8)
        right = ctk.CTkFrame(self, fg_color=BG, corner_radius=12)
        right.grid(row=0, column=1, sticky="nsew", padx=(4, 8), pady=8)
        self._build_month(left)
        self._build_upcoming(left)
        self._build_day(right)
        self._build_quick_add(right)
        self._kind_changed("To-do")
        self._flush()

    def cfont(self, size, weight="normal"):
        """Tk font tuple for canvas text, scaled like customtkinter scales its widgets."""
        key = (size, weight)
        if key not in self._cfonts:
            self._cfonts[key] = (self._family, -round(size * self.scale), weight)
        return self._cfonts[key]

    # ------------------------------------------------------------ building -- #
    def _build_unavailable(self):
        box = ctk.CTkFrame(self, fg_color=BG, corner_radius=12)
        box.pack(fill="both", expand=True, padx=8, pady=8)
        ctk.CTkLabel(box, text="Calendar unavailable", font=ctk.CTkFont(size=17, weight="bold"),
                     text_color="white").place(relx=0.5, rely=0.45, anchor="center")
        ctk.CTkLabel(box, text="Xyrus started without its calendar store, so there is nothing to show here.",
                     text_color=MUTED).place(relx=0.5, rely=0.55, anchor="center")

    def _build_month(self, left):
        left.grid_columnconfigure(0, weight=1)
        head = ctk.CTkFrame(left, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=10, pady=(8, 0))
        nav = dict(width=28, height=28, corner_radius=8, fg_color="transparent", hover_color=LINE,
                   text_color=TEXT, font=ctk.CTkFont(size=20))
        ctk.CTkButton(head, text="‹", command=lambda: self.shift_month(-1), **nav).pack(side="left")
        self.month_lbl = ctk.CTkLabel(head, text="", width=138, font=self.f_month, text_color="white")
        self.month_lbl.pack(side="left", padx=2)
        ctk.CTkButton(head, text="›", command=lambda: self.shift_month(1), **nav).pack(side="left")
        ctk.CTkButton(head, text="Today", width=62, height=26, corner_radius=8, fg_color=LINE,
                      hover_color=ACCENT_HOVER, font=self.f_small, command=self.go_today).pack(side="right")

        wk = ctk.CTkFrame(left, fg_color="transparent")
        wk.grid(row=1, column=0, sticky="ew", padx=8)
        for c, name in enumerate(("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")):
            wk.grid_columnconfigure(c, weight=1, uniform="wd")
            ctk.CTkLabel(wk, text=name, height=18, font=self.f_tiny,
                         text_color=MUTED if c < 5 else "#7d7d8c").grid(row=0, column=c)

        grid = ctk.CTkFrame(left, fg_color="transparent", height=180)
        grid.grid(row=2, column=0, sticky="nsew", padx=8, pady=(2, 0))
        grid.grid_propagate(False)
        left.grid_rowconfigure(2, weight=3)
        self.cells = []
        for r in range(6):
            grid.grid_rowconfigure(r, weight=1, uniform="wr")
            for c in range(7):
                grid.grid_columnconfigure(c, weight=1, uniform="wc")
                cell = _Cell(self, grid)
                cell.frame.grid(row=r, column=c, padx=2, pady=2, sticky="nsew")
                self.cells.append(cell)

    def _build_upcoming(self, left):
        head = self.up_head = ctk.CTkFrame(left, fg_color="transparent")
        head.grid(row=3, column=0, sticky="ew", padx=14, pady=(6, 0))
        ctk.CTkLabel(head, text="NEXT 7 DAYS", height=20, font=self.f_section, text_color=MUTED).pack(side="left")
        self.up_count = ctk.CTkLabel(head, text="", height=20, font=self.f_tiny, text_color=FAINT)
        self.up_count.pack(side="left", padx=6)
        box = self.up_box = ctk.CTkFrame(left, fg_color="transparent", height=40)
        box.grid(row=4, column=0, sticky="nsew", padx=10, pady=(0, 8))
        box.grid_propagate(False)
        box.grid_columnconfigure(2, weight=1)
        left.grid_rowconfigure(4, weight=1, minsize=round((3 * 22 + 10) * self.scale))   # >= 3 rows + pady
        self.up_rows = []
        for r in range(UP_ROWS):
            day = ctk.CTkLabel(box, text="", width=44, height=22, anchor="w", font=self.f_small, text_color=MUTED)
            tm = ctk.CTkLabel(box, text="", width=62, height=22, anchor="w", font=self.f_small)
            title = ctk.CTkLabel(box, text="", height=22, anchor="w", font=self.f_small, text_color=TEXT)
            row = {"day": day, "time": tm, "title": title, "date": None}
            for w in (day, tm, title):
                w.bind("<Button-1>", lambda e, row=row: row["date"] and self.select(row["date"]))
            self.up_rows.append(row)
        box.bind("<Configure>", self._up_resized, add="+")
        self.left = left
        left.bind("<Configure>", self._left_resized, add="+")

    def _left_resized(self, event):
        """Below ~300 px of height the month grid needs every pixel; the upcoming list goes."""
        show = event.height >= round(300 * self.scale)
        if show == self._up_shown:
            return
        self._up_shown = show
        if show:
            self.up_head.grid()
            self.up_box.grid()
            self.left.grid_rowconfigure(4, weight=1, minsize=round((3 * 22 + 10) * self.scale))
        else:
            self.up_head.grid_remove()
            self.up_box.grid_remove()
            self.left.grid_rowconfigure(4, weight=0, minsize=0)

    def _up_resized(self, event):
        fit = max(0, min(UP_ROWS, int(event.height // round(22 * self.scale))))
        if fit != self._up_fit:
            self._up_fit = fit
            self._dirty.add("upcoming")
            self.after_idle(self._flush)

    def _build_day(self, right):
        right.grid_columnconfigure(0, weight=1)
        right.grid_rowconfigure(1, weight=1)
        bar = ctk.CTkFrame(right, fg_color="transparent")
        bar.grid(row=0, column=0, sticky="ew", padx=14, pady=(10, 2))
        self.day_title = ctk.CTkLabel(bar, text="", font=self.f_day, text_color="white", height=30)
        self.day_title.pack(side="left")
        self.day_tag = ctk.CTkLabel(bar, text="", height=22, corner_radius=8, fg_color=ACCENT,
                                    text_color="white", font=ctk.CTkFont(size=11, weight="bold"))
        self.day_sub = ctk.CTkLabel(bar, text="", height=22, font=self.f_small, text_color=MUTED)
        self.day_sub.pack(side="right")
        self.day_bar = bar
        bar.bind("<Configure>", lambda e: self._fit_bar(), add="+")

        self.list = ctk.CTkScrollableFrame(right, fg_color="transparent", height=80,
                                           scrollbar_button_color=LINE, scrollbar_button_hover_color="#3a3a48")
        self.list.grid(row=1, column=0, sticky="nsew", padx=(6, 4), pady=(0, 2))
        self.list.grid_columnconfigure(0, weight=1)
        self.list.bind("<Configure>", self._list_resized, add="+")
        self.list._parent_canvas.bind("<Configure>", lambda e: self.after_idle(self._fit_scrollbar), add="+")
        self.rows: list[_Row] = []
        self.headers: list[ctk.CTkLabel] = []
        self.more_lbl = ctk.CTkLabel(self.list, text="", font=self.f_small, text_color=MUTED, anchor="w")
        self.empty = ctk.CTkFrame(self.list, fg_color="transparent")
        ctk.CTkLabel(self.empty, text="✦", font=ctk.CTkFont(size=26), text_color="#3a3358").pack(pady=(24, 0))
        self.empty_title = ctk.CTkLabel(self.empty, text="Nothing planned", font=ctk.CTkFont(size=15, weight="bold"),
                                        text_color=TEXT)
        self.empty_title.pack()
        ctk.CTkLabel(self.empty, text='Add a to-do, event or note below,\nor say "Xyrus, remind me to ... tomorrow at 5".',
                     font=self.f_small, text_color=MUTED, justify="center").pack(pady=(2, 10))

    def _list_resized(self, event):
        lw = int(event.width / self.scale)
        if abs(lw - self._list_w) > 4:
            self._list_w = lw
            for r in self.rows:
                r.fit()

    def _build_quick_add(self, right):
        qa = ctk.CTkFrame(right, fg_color=CARD, corner_radius=10)
        qa.grid(row=2, column=0, sticky="ew", padx=8, pady=(2, 8))
        qa.grid_columnconfigure(0, weight=1)
        top = ctk.CTkFrame(qa, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 0))
        self.kind_seg = ctk.CTkSegmentedButton(top, values=list(KINDS), height=26, font=self.f_small,
                                               selected_color=ACCENT, selected_hover_color=ACCENT_HOVER,
                                               unselected_color=LINE, unselected_hover_color="#363644",
                                               fg_color=LINE, command=self._kind_changed)
        self.kind_seg.set("To-do")
        self.kind_seg.pack(side="left")
        self.rem_menu = ctk.CTkOptionMenu(top, values=[lab for lab, _ in REMINDERS], width=132, height=26,
                                          font=self.f_small, dropdown_font=self.f_small, fg_color=LINE,
                                          button_color=LINE, button_hover_color="#363644",
                                          dynamic_resizing=False, command=self._rem_picked)
        self.rem_menu.set("No reminder")
        self.rem_icon = ctk.CTkLabel(top, text="🔔", font=self.f_small, text_color=MUTED, width=18)

        mid = ctk.CTkFrame(qa, fg_color="transparent")
        mid.grid(row=1, column=0, sticky="ew", padx=8, pady=(6, 0))
        self.add_btn = ctk.CTkButton(mid, text="Add", width=60, height=32, corner_radius=8, fg_color=ACCENT,
                                     hover_color=ACCENT_HOVER, font=ctk.CTkFont(size=13, weight="bold"),
                                     command=self.quick_add)
        self.add_btn.pack(side="right", padx=(6, 0))
        self.entry = ctk.CTkEntry(mid, height=32, corner_radius=8, fg_color=BG, border_color=LINE,
                                  font=self.f_row, placeholder_text=PLACEHOLDERS["To-do"])
        self.entry.pack(side="left", fill="x", expand=True)
        self.entry.bind("<Return>", lambda e: self.quick_add())
        self.entry.bind("<KP_Enter>", lambda e: self.quick_add())
        self.entry.bind("<Escape>", lambda e: (self.entry.delete(0, "end"), self._preview()))
        self.entry.bind("<KeyRelease>", lambda e: self._preview(), add="+")

        bottom = ctk.CTkFrame(qa, fg_color="transparent")
        bottom.grid(row=2, column=0, sticky="ew", padx=10, pady=(2, 6))
        self.status = ctk.CTkLabel(bottom, text="", height=18, anchor="w", font=self.f_tiny, text_color=MUTED)
        self.status.pack(side="left", fill="x", expand=True)
        self.undo_btn = ctk.CTkButton(bottom, text="Undo", width=44, height=18, corner_radius=6, fg_color=LINE,
                                      hover_color=ACCENT_HOVER, font=self.f_tiny, command=self.undo_delete)

    # ------------------------------------------------------------ navigation -- #
    def select(self, d, keep_range=False):
        d = _date(d)
        if not keep_range:
            self.range = None
        if self._editing:
            self._commit_edit()
        changed = d != self.selected
        self.selected = d
        self.month = (d.year, d.month)
        self._dirty |= {"grid", "day"}
        self._flush()
        if changed:
            try:
                self.list._parent_canvas.yview_moveto(0)
            except Exception:
                pass
        self._preview()

    def shift_month(self, delta):
        y, m = self.month
        m += delta
        y, m = y + (m - 1) // 12, (m - 1) % 12 + 1
        self.month = (y, m)
        self._dirty.add("grid")
        self._flush()

    def go_today(self):
        self.select(self._now().date())

    def show_range(self, start, end=None):
        """Jump to `start` and, for a multi-day range, tint the range's cells (voice: 'this week')."""
        start = _date(start)
        end = _date(end) if end else start
        if end < start:
            start, end = end, start
        self.select(start)
        self.range = (start, end) if end != start else None
        self._dirty.add("grid")
        self._flush()

    # -------------------------------------------------------------- polling -- #
    def poll(self):
        """Called by XyrusWindow._poll on the Tk thread every 150 ms."""
        if self.store is None:
            return
        now = self._now()
        if now.date() != self.today:                  # midnight: 'today' moves, keep following it
            if self.selected == self.today:
                self.selected = now.date()
                self.month = (now.year, now.month)
            self.today = now.date()
            self._dirty |= {"grid", "day", "upcoming"}
        if self.store.version != self._seen_version:
            self._dirty |= {"grid", "day", "upcoming"}
        if now.minute != self._minute:
            self._minute = now.minute
            self._dirty.add("upcoming")
        if self._dirty:
            self._flush()

    def _flush(self):
        if self.store is None:
            return
        self._seen_version = self.store.version
        dirty, self._dirty = self._dirty, set()
        with self.store.lock:
            if "grid" in dirty:
                self._render_grid()
            if "day" in dirty:
                if self._editing:
                    self._dirty.add("day")            # don't yank the row out from under the editor
                else:
                    self._render_day()
            if "upcoming" in dirty:
                self._render_upcoming()

    # ------------------------------------------------------------- rendering -- #
    def _render_grid(self):
        y, m = self.month
        first = dt.date(y, m, 1)
        start = first - dt.timedelta(days=first.weekday())
        days = [start + dt.timedelta(days=i) for i in range(42)]
        counts = self.store.counts_by_day(days[0], days[-1])
        self.month_lbl.configure(text=f"{first:%B} {y}")
        lo, hi = self.range or (None, None)
        for cell, d in zip(self.cells, days):
            cell.show(d, d.month == m, d == self.today, d == self.selected,
                      bool(lo and lo <= d <= hi), counts.get(d))

    def _row(self, i) -> _Row:
        while len(self.rows) <= i:
            self.rows.append(_Row(self, self.list))
        return self.rows[i]

    def _header(self, i) -> ctk.CTkLabel:
        while len(self.headers) <= i:
            self.headers.append(ctk.CTkLabel(self.list, text="", height=22, anchor="w", font=self.f_section))
        return self.headers[i]

    def day_entries(self):
        """What the agenda shows for the selected day: [(kind, obj, extra)] - also used by tests."""
        d, today = self.selected, self.today
        occ = [it for _d, it in self.store.items_between(d, d)]
        tasks = sorted((i for i in occ if i["kind"] == "task"), key=lambda i: bool(i.get("done")))
        events = [i for i in occ if i["kind"] == "event"]
        notes = self.store.notes_on(d)
        overdue = self.store.overdue_tasks(today) if d == today else []
        out = []
        if overdue:
            out.append(("header", f"OVERDUE  ·  {len(overdue)}", RED))
            out += [("task", t, short_day(_date(t["date"]))) for t in overdue]
        if tasks:
            n_done = sum(1 for t in tasks if t.get("done"))
            out.append(("header", f"TO-DO  ·  {n_done}/{len(tasks)} done" if n_done else f"TO-DO  ·  {len(tasks)}",
                        GREEN))
            out += [("task", t, None) for t in tasks]
        if events:
            out.append(("header", f"EVENTS  ·  {len(events)}", ACCENT_SOFT))
            out += [("event", e, None) for e in events]
        if notes:
            out.append(("header", f"NOTES  ·  {len(notes)}", AMBER))
            out += [("note", n, None) for n in notes]
        return out

    def _render_day(self):
        d, today = self.selected, self.today
        self.day_title.configure(text=long_day(d, today))
        tag = {0: "Today", 1: "Tomorrow", -1: "Yesterday"}.get((d - today).days)
        if tag:
            self.day_tag.configure(text=f"  {tag}  ", fg_color=ACCENT if tag == "Today" else LINE)
            self.day_tag.pack(side="left", padx=(10, 0))
        else:
            self.day_tag.pack_forget()

        entries = self.day_entries()
        n_open = sum(1 for k, o, x in entries if k == "task" and not o.get("done"))
        n_ev = sum(1 for k, o, x in entries if k == "event")
        sub = []
        if n_open:
            sub.append(f"{n_open} open")
        if n_ev:
            sub.append(f"{n_ev} event{'s' if n_ev != 1 else ''}")
        self.day_sub.configure(text="  ·  ".join(sub))
        self.after_idle(self._fit_bar)

        more = 0
        if sum(1 for e in entries if e[0] != "header") > MAX_ROWS:
            kept, n = [], 0
            for e in entries:
                if e[0] != "header":
                    n += 1
                    if n > MAX_ROWS:
                        more += 1
                        continue
                kept.append(e)
            entries = kept

        ri = hi = 0
        grid_row = 0
        for kind, obj, extra in entries:
            if kind == "header":
                h = self._header(hi)
                hi += 1
                h.configure(text=obj, text_color=extra)
                h.grid(row=grid_row, column=0, sticky="ew", padx=(10, 4), pady=(8 if grid_row else 2, 0))
            else:
                r = self._row(ri)
                ri += 1
                if kind == "task":
                    r.show_task(obj, extra)
                elif kind == "event":
                    r.show_event(obj)
                else:
                    r.show_note(obj)
                r.frame.grid(row=grid_row, column=0, sticky="ew", padx=(2, 2), pady=0)
            grid_row += 1
        for r in self.rows[ri:]:
            r.frame.grid_remove()
            r.obj = None
        for h in self.headers[hi:]:
            h.grid_remove()
        if more:
            self.more_lbl.configure(text=f"... and {more} more")
            self.more_lbl.grid(row=grid_row, column=0, sticky="w", padx=12, pady=4)
        else:
            self.more_lbl.grid_remove()
        if entries:
            self.empty.grid_remove()
        else:
            self.empty_title.configure(text="Nothing planned" if d >= today else "Nothing was planned")
            self.empty.grid(row=0, column=0, sticky="ew")
        self.after_idle(self._fit_scrollbar)

    def _fit_bar(self):
        need = self.day_title.winfo_reqwidth() + self.day_sub.winfo_reqwidth() + 16
        if self.day_tag.winfo_manager():
            need += self.day_tag.winfo_reqwidth() + 10
        fits = need <= self.day_bar.winfo_width()
        if fits and not self.day_sub.winfo_manager():
            self.day_sub.pack(side="right")
        elif not fits and self.day_sub.winfo_manager():
            self.day_sub.pack_forget()

    def _fit_scrollbar(self):
        """CTkScrollableFrame always shows its scrollbar; hide it while the agenda fits."""
        try:
            canvas, bar = self.list._parent_canvas, self.list._scrollbar
            need = self.list.winfo_reqheight() > canvas.winfo_height() + 1
            if need and not bar.winfo_manager():
                bar.grid()
            elif not need and bar.winfo_manager():
                bar.grid_remove()
        except Exception:
            pass

    def upcoming_entries(self):
        now = self._now()
        today = now.date()
        hhmm = now.strftime("%H:%M")
        return [(d, it) for d, it in self.store.items_between(today, today + dt.timedelta(days=6), include_done=False)
                if not (d == today and it["kind"] == "event" and it.get("time") and it["time"] < hhmm)]

    def _render_upcoming(self):
        items = self.upcoming_entries()
        today = self._now().date()
        self.up_count.configure(text=str(len(items)) if items else "")
        fit = self._up_fit
        shown = items[:fit]
        overflow = len(items) - len(shown)
        if overflow and fit:
            shown = items[:fit - 1]
            overflow = len(items) - len(shown)
        rows = []
        for d, it in shown:
            day = "Today" if d == today else "Tmrw" if (d - today).days == 1 else f"{d:%a}"
            tm = W.fmt_time(it["time"]) if it.get("time") else "all day"
            color = (ACCENT_SOFT if it["kind"] == "event" else GREEN) if it.get("time") else FAINT
            rows.append((day, tm, color, ("" if it["kind"] == "event" else "☐ ") + display(it["title"]), d))
        if overflow:
            rows.append(("", "", MUTED, f"+ {overflow} more", shown[-1][0] if shown else None))
        if not items and fit:
            rows.append(("", "", MUTED, "Nothing in the next 7 days", None))
        for i, row in enumerate(self.up_rows):
            if i < len(rows):
                day, tm, color, title, d = rows[i]
                row["day"].configure(text=day)
                row["time"].configure(text=tm, text_color=color)
                row["title"].configure(text=title, text_color=MUTED if d is None or title.startswith("+ ") else TEXT)
                row["date"] = d
                row["day"].grid(row=i, column=0, sticky="w")
                row["time"].grid(row=i, column=1, sticky="w")
                row["title"].grid(row=i, column=2, sticky="ew")
            else:
                for k in ("day", "time", "title"):
                    row[k].grid_remove()
                row["date"] = None

    # ----------------------------------------------------------- row actions -- #
    def _toggle(self, row: _Row):
        if row.kind == "task" and row.obj:
            self.store.set_done(row.obj["id"], row.check.get() == 1)
            self._dirty |= {"grid", "day", "upcoming"}
            self._flush()

    def _delete(self, row: _Row):
        obj = row.obj
        if not obj:
            return
        if self._editing:
            self._cancel_edit()
        if row.kind == "note":
            ok = self.store.delete_note(obj["id"])
            what = "note"
        else:
            ok = self.store.delete_item(obj["id"])
            what = ({"weekly": "weekly ", "daily": "daily "}.get(obj.get("repeat"), "")
                    + ("to-do" if obj["kind"] == "task" else "event"))
        if ok:
            self._undo = ("note" if row.kind == "note" else "item", dict(obj))
            label = obj["text"] if row.kind == "note" else obj["title"]
            self._flash(f"Deleted {what}: {display(clip(label, 34))}", MUTED, undo=True)
        self._dirty |= {"grid", "day", "upcoming"}
        self._flush()

    def undo_delete(self):
        if not self._undo:
            return
        kind, obj = self._undo
        self._undo = None
        if kind == "note":
            self.store.add_note(obj["date"], obj["text"], source=obj.get("source", "ui"))
        else:
            add = self.store.add_event if obj["kind"] == "event" else self.store.add_task
            extra = {"repeat": obj.get("repeat")} if obj["kind"] == "event" else {}
            new = add(obj["title"], obj["date"], obj.get("time"), obj.get("reminder_min"),
                      source=obj.get("source", "ui"), **extra)
            # keep its done state and its already-fired reminders (no second "while I was away")
            self.store.update_item(new["id"], done=bool(obj.get("done")), fired=dict(obj.get("fired") or {}))
        self.select(obj["date"])
        self._flash("Restored.", GREEN)

    def _start_edit(self, row: _Row):
        if not row.obj:
            return
        if self._editing:
            self._commit_edit()
            if not row.obj:
                return
        if row.editor is None:
            row.editor = ctk.CTkEntry(row.frame, height=26, corner_radius=6, font=self.f_row,
                                      fg_color=BG, border_color=ACCENT, border_width=1)
            row.editor.bind("<Return>", self._commit_edit)
            row.editor.bind("<KP_Enter>", self._commit_edit)
            row.editor.bind("<Escape>", self._cancel_edit)
            row.editor.bind("<FocusOut>", self._commit_edit, add="+")
        ed = row.editor
        ed.delete(0, "end")
        ed.insert(0, row.raw_text())
        row.title.grid_remove()
        ed.grid(row=0, column=2, sticky="ew", padx=(0, 4), pady=1)
        self._editing = row
        ed.focus_set()
        ed.select_range(0, "end")
        ed.icursor("end")

    def _end_edit(self):
        row, self._editing = self._editing, None
        text = row.editor.get().strip()
        row.editor.grid_remove()
        row.title.grid()
        return row, text

    def _commit_edit(self, event=None):
        if self._editing is None:
            return "break"
        row, text = self._end_edit()
        if row.obj and text and text != row.raw_text():
            if row.kind == "note":
                self.store.update_note(row.obj["id"], text)
            else:
                self.store.update_item(row.obj["id"], title=text)
            self._flash("Saved.", GREEN)
        self._dirty |= {"grid", "day", "upcoming"}
        self._flush()
        return "break"

    def _cancel_edit(self, event=None):
        if self._editing is not None:
            self._end_edit()
            self._dirty.add("day")
            self._flush()
        return "break"

    # ------------------------------------------------------------- quick add -- #
    def _kind_changed(self, kind):
        if kind == "Note":
            self.rem_menu.pack_forget()
            self.rem_icon.pack_forget()
        else:
            self.rem_menu.pack(side="right")
            self.rem_icon.pack(side="right", padx=(0, 4))
        if not self.entry.get():
            self.entry.configure(placeholder_text=PLACEHOLDERS[kind])
        self._preview()

    def _rem_picked(self, label):
        self._rem_user = True
        self._preview()

    def plan(self, text=None, kind=None):
        """What quick-add would do with the text -> dict(kind, title, date, time, repeat, reminder)."""
        text = (self.entry.get() if text is None else text).strip()
        kind = kind or self.kind_seg.get()
        now = self._now()
        w = W.parse_when(text, now) if text else None
        names_day = bool(text) and (W.mentions_date(text) or bool(RELATIVE_TIME.search(text)))
        if kind == "Note":
            d = w.start.date() if (w and names_day) else self.selected
            return dict(kind="note", title=text, date=d, time=None, repeat=None, reminder=None)
        if w is None:
            title, d, tm, repeat, rem_text = text, self.selected, None, None, None
        else:
            title = original_case(text, w.title) if w.title else text
            d = w.start.date() if names_day else self.selected
            tm = None if w.all_day else w.start.strftime("%H:%M")
            repeat = w.repeat if kind == "Event" else None
            rem_text = w.reminder_min
        if self._rem_user:
            rem = rem_value(self.rem_menu.get())
        elif rem_text is not None:
            rem = rem_text
        else:
            rem = 0 if tm else None
        return dict(kind="event" if kind == "Event" else "task", title=title, date=d, time=tm,
                    repeat=repeat, reminder=rem)

    @staticmethod
    def _describe(p):
        bits = [short_day(p["date"])]
        if p["time"]:
            bits.append(W.fmt_time(p["time"]))
        if p["repeat"]:
            bits.append(f"every {p['date']:%A}" if p["repeat"] == "weekly" else "every day")
        return " · ".join(bits)

    def _preview(self):
        """Live hint under the entry ('Event “Dentist” → Tue 15 Sep · 3 pm') + the automatic reminder."""
        text = self.entry.get().strip()
        kind = self.kind_seg.get()
        if not text:
            if not self._rem_user and kind != "Note":
                self.rem_menu.set("No reminder")
            if not self._flash_job:                  # a confirmation is showing; it restores the hint itself
                self.status.configure(text=f"Adds to {short_day(self.selected)} unless you name a day.",
                                      text_color=FAINT)
            return
        if self._flash_job:                          # typing the next item replaces the confirmation
            self.after_cancel(self._flash_job)
            self._flash_job = None
            self.undo_btn.pack_forget()
        p = self.plan(text, kind)
        if not self._rem_user and kind != "Note":
            self.rem_menu.set(rem_label(p["reminder"]))
        noun = {"task": "To-do", "event": "Event", "note": "Note"}[p["kind"]]
        self.status.configure(text=f"{noun} “{display(clip(p['title']))}” → {self._describe(p)}", text_color=MUTED)

    def quick_add(self):
        text = self.entry.get().strip()
        if not text:
            self._flash("Type what to add first.", RED)
            return None
        p = self.plan(text)
        if p["kind"] == "note":
            obj = self.store.add_note(p["date"], p["title"], source="ui")
        elif p["kind"] == "event":
            obj = self.store.add_event(p["title"], p["date"], p["time"], p["reminder"], p["repeat"], source="ui")
        else:
            obj = self.store.add_task(p["title"], p["date"], p["time"], p["reminder"], source="ui")
        self.entry.delete(0, "end")
        self._rem_user = False
        self._undo = None
        self.select(p["date"])
        noun = {"task": "to-do", "event": "event", "note": "note"}[p["kind"]]
        self._flash(f"Added {noun}: {display(clip(p['title']))} – {self._describe(p)}", GREEN)
        return obj

    def _flash(self, text, color, undo=False):
        if self._flash_job:
            self.after_cancel(self._flash_job)
        self.status.configure(text=text, text_color=color)
        if undo:
            self.undo_btn.pack(side="right")
        else:
            self.undo_btn.pack_forget()
        self._flash_job = self.after(6000 if undo else 3500, self._unflash)

    def _unflash(self):
        self._flash_job = None
        self.undo_btn.pack_forget()
        self._preview()
