"""EventDialog: full editor for one calendar item (event or to-do). Modal CTkToplevel.

v1's calendar tab had no dialog (only inline title edits), so this is new, built to §4.15:
Kind · Title · Date (−1/+1 day) · All day · hour/minute/AM-PM · Reminder · Repeat · Save/Delete/Cancel,
inline red validation. Every write goes through the store.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Callable

import customtkinter as ctk

from xyrus.ui.theme import (ACCENT, ACCENT_HOVER, BG, CARD, FAINT, LINE, LINE_HOVER, MENU, MUTED, ON_ACCENT,
                            RED, RED_BG, SEG, TEXT)

log = logging.getLogger("xyrus.ui.event_dialog")

REMINDERS = [("No reminder", None), ("At the time", 0), ("5 minutes before", 5), ("10 minutes before", 10),
             ("15 minutes before", 15), ("30 minutes before", 30), ("60 minutes before", 60),
             ("1 day before", 1440)]
REPEATS = [("Never", None), ("Daily", "daily"), ("Weekly", "weekly")]
HOURS = [str(h) for h in range(1, 13)]
MINUTES = [f"{m:02d}" for m in range(0, 60, 5)]


def _rem_label(m) -> str:
    for label, val in REMINDERS:
        if val == m:
            return label
    return f"{m} minutes before"


def _rem_value(label: str):
    for lab, val in REMINDERS:
        if lab == label:
            return val
    try:
        return int(label.split()[0])
    except (ValueError, IndexError):
        return None


class EventDialog(ctk.CTkToplevel):
    """EventDialog(master, store, item=None, date=None, kind="event", on_done=cb).
    on_done(item_or_None) runs after Save (saved item), Delete (None) - not after Cancel.
    `result` holds the saved item, "deleted", or None (cancelled)."""

    def __init__(self, master, store, *, item: dict | None = None, date: dt.date | None = None,
                 kind: str = "event", default_reminder: int | None = 10,
                 on_done: Callable[[dict | None], None] | None = None):
        super().__init__(master, fg_color=CARD)
        self.store = store
        self.item = item
        self.on_done = on_done
        self.result = None
        self.title("Edit item" if item else "New item")
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", self.cancel)
        self.bind("<Escape>", lambda e: self.cancel())
        self.bind("<Return>", lambda e: self.save())

        f = ctk.CTkFont(size=13)
        small = ctk.CTkFont(size=12)
        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=18, pady=14)
        body.grid_columnconfigure(1, weight=1)

        def label(r, text):
            ctk.CTkLabel(body, text=text, text_color=MUTED, font=small, anchor="w").grid(
                row=r, column=0, sticky="w", padx=(0, 12), pady=5)

        seg = dict(**SEG, font=small, height=28)
        menu = dict(**MENU, font=small, dropdown_font=small, height=28, dynamic_resizing=False)

        label(0, "Kind")
        self.kind = ctk.CTkSegmentedButton(body, values=["Event", "To-do"], command=lambda v: self._sync(), **seg)
        self.kind.grid(row=0, column=1, sticky="w", pady=5)

        label(1, "Title")
        self.title_entry = ctk.CTkEntry(body, font=f, height=30, fg_color=BG, border_color=LINE, text_color=TEXT)
        self.title_entry.grid(row=1, column=1, sticky="ew", pady=5)

        label(2, "Date")
        drow = ctk.CTkFrame(body, fg_color="transparent")
        drow.grid(row=2, column=1, sticky="w", pady=5)
        nav = dict(width=30, height=28, corner_radius=8, fg_color=LINE, hover_color=LINE_HOVER, text_color=TEXT,
                   font=small)
        ctk.CTkButton(drow, text="−1", command=lambda: self._shift(-1), **nav).pack(side="left")
        self.date_entry = ctk.CTkEntry(drow, width=106, height=28, font=f, fg_color=BG, border_color=LINE,
                                       text_color=TEXT, justify="center")
        self.date_entry.pack(side="left", padx=4)
        ctk.CTkButton(drow, text="+1", command=lambda: self._shift(1), **nav).pack(side="left")
        self.weekday = ctk.CTkLabel(drow, text="", text_color=FAINT, font=small, width=44)
        self.weekday.pack(side="left", padx=(8, 0))
        self.date_entry.bind("<KeyRelease>", lambda e: self._sync(), add="+")

        label(3, "All day")
        self.all_day = ctk.CTkSwitch(body, text="", progress_color=ACCENT, command=self._sync, width=46)
        self.all_day.grid(row=3, column=1, sticky="w", pady=5)

        label(4, "Time")
        trow = ctk.CTkFrame(body, fg_color="transparent")
        trow.grid(row=4, column=1, sticky="w", pady=5)
        self.hour = ctk.CTkOptionMenu(trow, values=HOURS, width=62, **menu)
        self.hour.pack(side="left")
        ctk.CTkLabel(trow, text=":", text_color=MUTED, width=10).pack(side="left", padx=2)
        self.minute = ctk.CTkOptionMenu(trow, values=MINUTES, width=62, **menu)
        self.minute.pack(side="left")
        self.ampm = ctk.CTkSegmentedButton(trow, values=["AM", "PM"], **seg)
        self.ampm.pack(side="left", padx=(8, 0))

        label(5, "Reminder")
        self.reminder = ctk.CTkOptionMenu(body, values=[lab for lab, _ in REMINDERS], width=170, **menu)
        self.reminder.grid(row=5, column=1, sticky="w", pady=5)

        label(6, "Repeat")
        self.repeat = ctk.CTkSegmentedButton(body, values=[lab for lab, _ in REPEATS], **seg)
        self.repeat.grid(row=6, column=1, sticky="w", pady=5)

        self.error = ctk.CTkLabel(body, text="", text_color=RED, font=small, anchor="w", height=18)
        self.error.grid(row=7, column=0, columnspan=2, sticky="ew", pady=(4, 0))

        btns = ctk.CTkFrame(body, fg_color="transparent")
        btns.grid(row=8, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        self.save_btn = ctk.CTkButton(btns, text="Save", width=84, height=30, corner_radius=8, fg_color=ACCENT,
                                      hover_color=ACCENT_HOVER, text_color=ON_ACCENT, command=self.save)
        self.save_btn.pack(side="right")
        ctk.CTkButton(btns, text="Cancel", width=84, height=30, corner_radius=8, fg_color=LINE,
                      hover_color=LINE_HOVER, text_color=TEXT, command=self.cancel).pack(side="right", padx=8)
        self.delete_btn = ctk.CTkButton(btns, text="Delete", width=84, height=30, corner_radius=8,
                                        fg_color="transparent", border_width=1, border_color=RED, text_color=RED,
                                        hover_color=RED_BG, command=self.delete)
        if item:
            self.delete_btn.pack(side="left")

        self._load(item, date or dt.date.today(), kind, default_reminder)
        self._sync()
        self._place(master)
        self.after(60, self._grab)

    # ------------------------------------------------------------------ setup --
    def _load(self, item, date, kind, default_reminder):
        if item:
            kind = item.get("kind", "event")
            self.title_entry.insert(0, item.get("title", ""))
            date = dt.date.fromisoformat(item["date"])
            tm = item.get("time")
            rem = item.get("reminder_min")
            rep = item.get("repeat")
        else:
            tm = "09:00" if kind == "event" else None
            rem = default_reminder if kind == "event" else None
            rep = None
        self.kind.set("To-do" if kind == "task" else "Event")
        self.date_entry.insert(0, date.isoformat())
        if tm:
            self.all_day.deselect()
            h, m = map(int, tm.split(":")[:2])
        else:
            self.all_day.select()
            h, m = 9, 0
        self.hour.set(str(h % 12 or 12))
        mm = f"{m:02d}"
        if mm not in MINUTES:                      # keep an odd minute (14:23) selectable
            self.minute.configure(values=sorted(set(MINUTES) | {mm}))
        self.minute.set(mm)
        self.ampm.set("AM" if h < 12 else "PM")
        if rem is not None and _rem_label(rem) not in [lab for lab, _ in REMINDERS]:
            self.reminder.configure(values=[lab for lab, _ in REMINDERS] + [_rem_label(rem)])
        self.reminder.set(_rem_label(rem))
        self.repeat.set(next(lab for lab, v in REPEATS if v == rep) if rep in ("daily", "weekly") else "Never")

    def _place(self, master):
        try:
            top = master.winfo_toplevel()
            self.transient(top)
            self.update_idletasks()
            w, h = max(self.winfo_reqwidth(), 400), self.winfo_reqheight()
            x = top.winfo_rootx() + (top.winfo_width() - w) // 2
            y = top.winfo_rooty() + max(0, (top.winfo_height() - h) // 3)
            self.geometry(f"+{max(0, x)}+{max(0, y)}")
        except Exception:
            pass

    def _grab(self):
        try:
            self.grab_set()
            self.title_entry.focus_set()
        except Exception:                          # not viewable yet (tests / minimised parent)
            pass

    # -------------------------------------------------------------- behaviour --
    def _date(self) -> dt.date | None:
        try:
            return dt.date.fromisoformat(self.date_entry.get().strip())
        except ValueError:
            return None

    def _shift(self, days: int):
        d = self._date() or dt.date.today()
        self.date_entry.delete(0, "end")
        self.date_entry.insert(0, (d + dt.timedelta(days=days)).isoformat())
        self._sync()

    def _sync(self):
        d = self._date()
        self.weekday.configure(text=f"{d:%a}" if d else "")
        timed = not self.all_day.get()
        for w in (self.hour, self.minute, self.ampm):
            w.configure(state="normal" if timed else "disabled")
        is_event = self.kind.get() == "Event"
        self.repeat.configure(state="normal" if is_event else "disabled")

    def values(self) -> dict | str:
        """The form as store fields, or an error message."""
        title = self.title_entry.get().strip()
        if not title:
            return "Give it a title."
        d = self._date()
        if d is None:
            return "Date must look like 2026-09-14."
        tm = None
        if not self.all_day.get():
            h = int(self.hour.get()) % 12 + (12 if self.ampm.get() == "PM" else 0)
            tm = f"{h:02d}:{int(self.minute.get()):02d}"
        kind = "event" if self.kind.get() == "Event" else "task"
        rep = dict(REPEATS).get(self.repeat.get()) if kind == "event" else None
        return dict(kind=kind, title=title, date=d.isoformat(), time=tm,
                    reminder_min=_rem_value(self.reminder.get()), repeat=rep)

    def save(self):
        v = self.values()
        if isinstance(v, str):
            self.error.configure(text=v)
            return None
        try:
            if self.item:
                saved = self.store.update_item(self.item["id"], **v)
            elif v["kind"] == "event":
                saved = self.store.add_event(v["title"], v["date"], v["time"], v["reminder_min"], v["repeat"],
                                             source="ui")
            else:
                saved = self.store.add_task(v["title"], v["date"], v["time"], v["reminder_min"], source="ui")
        except Exception as e:
            log.exception("saving calendar item failed")
            self.error.configure(text=f"Could not save: {e}")
            return None
        self.result = saved
        self._close(saved)
        return saved

    def delete(self):
        if self.item:
            self.store.delete_item(self.item["id"])
        self.result = "deleted"
        self._close(None)

    def cancel(self):
        self.result = None
        self._finish()

    def _close(self, value):
        cb = self.on_done
        self._finish()
        if cb:
            try:
                cb(value)
            except Exception:
                log.exception("EventDialog callback failed")

    def _finish(self):
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()
