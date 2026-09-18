"""Calendar, to-do list and day notes for Xyrus. Owns data/calendar.json.

Every public method takes the lock; every change bumps .version (the UI polls it) and is written
atomically (tmp file + os.replace), so a crash can't leave a half-written file. Reminder logic
(due_reminders) takes the caller's clock, so it is testable without waiting.

Item schema (events and to-dos share one list):
  id "it_xxxxxxxx", kind "event" | "task", title, date "YYYY-MM-DD", time "HH:MM" | None, all_day bool,
  reminder_min int | None (None = no reminder, 0 = at the time), repeat None | "daily" | "weekly" (events only),
  done bool (to-dos), source "voice" | "ui", created ISO, fired {occurrence-date: "fired@ISO" | "missed@ISO"}
Note schema: id "nt_xxxxxxxx", date "YYYY-MM-DD", text, source, created ISO.
"""
from __future__ import annotations

import datetime as dt
import difflib
import json
import os
import secrets
import threading
from pathlib import Path

import when as W

DATA_FILE = Path(__file__).resolve().parent / "data" / "calendar.json"
CATCH_UP_MIN = 120                  # a reminder missed by <= 2 h is still spoken ("while I was away ...")
ALL_DAY_REMIND_AT = dt.time(9, 0)   # all-day items with a reminder fire at 09:00
MAX_SPOKEN = 12                     # items read aloud before "...and N more"


def _iso(d):
    return d.isoformat() if isinstance(d, dt.date) else d


def _hhmm(t):
    return t.strftime("%H:%M") if isinstance(t, dt.time) else t


class CalendarStore:
    def __init__(self, path=DATA_FILE):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.version = 0
        self.data = {"version": 1, "items": [], "notes": [], "state": {}}
        self._undo = []                 # (kind, id, title) of things added by voice, newest last
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text("utf8"))
            except Exception:
                self.path.replace(self.path.with_suffix(".corrupt.json"))   # keep the evidence, start clean
                loaded = {}
            for key in ("items", "notes", "state"):
                if key in loaded:
                    self.data[key] = loaded[key]
            for e in loaded.get("events", []):      # prototype files called them "events"
                e.setdefault("kind", "event")
                e.setdefault("done", False)
                self.data["items"].append(e)

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), "utf8")
        os.replace(tmp, self.path)
        self.version += 1

    # -- items (events + to-dos) ------------------------------------------------
    def _add(self, kind, title, date, time, reminder_min, repeat, source, now):
        it = {"id": "it_" + secrets.token_hex(4), "kind": kind, "title": title.strip(), "date": _iso(date),
              "time": _hhmm(time), "all_day": time is None, "reminder_min": reminder_min,
              "repeat": repeat if kind == "event" else None, "done": False, "source": source,
              "created": (now or dt.datetime.now()).isoformat(timespec="seconds"), "fired": {}}
        with self.lock:
            self.data["items"].append(it)
            if source == "voice":
                self._undo.append(("item", it["id"], it["title"]))
            self._save()
        return it

    def add_event(self, title, date, time=None, reminder_min=None, repeat=None, source="ui", now=None) -> dict:
        """date: datetime.date or 'YYYY-MM-DD'; time: datetime.time, 'HH:MM' or None (all day)."""
        return self._add("event", title, date, time, reminder_min, repeat, source, now)

    def add_task(self, title, date, time=None, reminder_min=None, source="ui", now=None) -> dict:
        """A to-do due on `date` (optionally at `time`)."""
        return self._add("task", title, date, time, reminder_min, None, source, now)

    def get_item(self, item_id):
        return next((i for i in self.data["items"] if i["id"] == item_id), None)

    def update_item(self, item_id, **fields):
        """Change any fields (title, date, time, reminder_min, repeat, done). Moving it re-arms its reminder."""
        with self.lock:
            it = self.get_item(item_id)
            if it is None:
                return None
            if "date" in fields:
                fields["date"] = _iso(fields["date"])
            if "time" in fields:
                fields["time"] = _hhmm(fields["time"])
                fields["all_day"] = fields["time"] is None
            moved = any(k in fields and fields[k] != it.get(k) for k in ("date", "time", "reminder_min", "repeat"))
            it.update(fields)
            if moved:
                it["fired"] = {}
            self._save()
            return it

    def set_done(self, item_id, done=True):
        return self.update_item(item_id, done=bool(done))

    def delete_item(self, item_id) -> bool:
        with self.lock:
            before = len(self.data["items"])
            self.data["items"] = [i for i in self.data["items"] if i["id"] != item_id]
            if len(self.data["items"]) == before:
                return False
            self._save()
            return True

    def mark_fired(self, item_id, key, when, how="fired"):
        with self.lock:
            it = self.get_item(item_id)
            if it is not None:
                it["fired"][key] = f"{how}@{when.isoformat(timespec='seconds')}"
                self._save()

    # -- queries -----------------------------------------------------------------
    @staticmethod
    def occurrences(it, start, end):
        """Yield (date, key) for each occurrence of an item in [start, end]; key == date.isoformat()."""
        first = dt.date.fromisoformat(it["date"])
        rep = it.get("repeat")
        if not rep:
            if start <= first <= end:
                yield first, first.isoformat()
            return
        step = dt.timedelta(days=1 if rep == "daily" else 7)
        d = first
        if d < start:
            d += step * ((start - d).days // step.days)
        while d <= end:
            if d >= start:                # the jump above can land just before the range
                yield d, d.isoformat()
            d += step

    def items_between(self, start, end, include_done=True):
        """Sorted [(date, item)] for every occurrence in [start, end] - all-day first, then by time."""
        out = []
        with self.lock:
            for it in self.data["items"]:
                if not include_done and it.get("done"):
                    continue
                for d, _ in self.occurrences(it, start, end):
                    out.append((d, it))
        return sorted(out, key=lambda x: (x[0], x[1]["time"] or "", x[1]["kind"] != "event", x[1]["title"].lower()))

    def open_tasks(self, until=None):
        """Not-done to-dos (optionally due on or before `until`), oldest first."""
        with self.lock:
            ts = [i for i in self.data["items"] if i["kind"] == "task" and not i.get("done")
                  and (until is None or i["date"] <= _iso(until))]
        return sorted(ts, key=lambda i: (i["date"], i["time"] or "", i["title"].lower()))

    def overdue_tasks(self, today):
        return [t for t in self.open_tasks() if t["date"] < today.isoformat()]

    def counts_by_day(self, start, end):
        """{date: {"event": n, "task": n, "task_open": n, "note": n}} for days that have anything."""
        out = {}
        blank = lambda: {"event": 0, "task": 0, "task_open": 0, "note": 0}
        for d, it in self.items_between(start, end):
            c = out.setdefault(d, blank())
            c[it["kind"]] += 1
            if it["kind"] == "task" and not it.get("done"):
                c["task_open"] += 1
        for n in self.notes_between(start, end):
            out.setdefault(dt.date.fromisoformat(n["date"]), blank())["note"] += 1
        return out

    def next_item(self, now, days=60):
        """(date, item) of the next not-done thing after `now`, or None."""
        today = now.date()
        for d, it in self.items_between(today, today + dt.timedelta(days=days), include_done=False):
            if it["time"]:
                if dt.datetime.combine(d, dt.time.fromisoformat(it["time"])) > now:
                    return d, it
            elif d > today:
                return d, it
        return None

    def find(self, text, kinds=("event", "task"), only_open=False, cutoff=0.55):
        """Best fuzzy title match for a spoken name ('the dentist' -> 'Dentist appointment'), or None."""
        q = " ".join(w for w in text.lower().split() if w not in ("the", "a", "an", "my"))
        best, best_score = None, 0.0
        with self.lock:
            for it in self.data["items"]:
                if it["kind"] not in kinds or (only_open and it.get("done")):
                    continue
                t = it["title"].lower()
                score = difflib.SequenceMatcher(None, q, t).ratio()
                qw, tw = set(q.split()), set(t.split())
                if qw and qw <= tw:
                    score = max(score, 0.9)
                elif qw & tw:
                    score = max(score, 0.5 + 0.4 * len(qw & tw) / len(qw | tw))
                if score > best_score:
                    best, best_score = it, score
        return best if best_score >= cutoff else None

    # -- notes -------------------------------------------------------------------
    def add_note(self, date, text, source="ui", now=None) -> dict:
        nt = {"id": "nt_" + secrets.token_hex(4), "date": _iso(date), "text": text.strip(), "source": source,
              "created": (now or dt.datetime.now()).isoformat(timespec="seconds")}
        with self.lock:
            self.data["notes"].append(nt)
            if source == "voice":
                self._undo.append(("note", nt["id"], nt["text"]))
            self._save()
        return nt

    def update_note(self, note_id, text):
        with self.lock:
            for n in self.data["notes"]:
                if n["id"] == note_id:
                    n["text"] = text.strip()
                    self._save()
                    return n
        return None

    def delete_note(self, note_id) -> bool:
        with self.lock:
            before = len(self.data["notes"])
            self.data["notes"] = [n for n in self.data["notes"] if n["id"] != note_id]
            if len(self.data["notes"]) == before:
                return False
            self._save()
            return True

    def notes_on(self, date):
        return [n for n in self.data["notes"] if n["date"] == _iso(date)]

    def notes_between(self, start, end):
        lo, hi = _iso(start), _iso(end)
        return sorted((n for n in self.data["notes"] if lo <= n["date"] <= hi), key=lambda n: (n["date"], n["created"]))

    def clear_notes(self, date) -> int:
        with self.lock:
            before = len(self.data["notes"])
            self.data["notes"] = [n for n in self.data["notes"] if n["date"] != _iso(date)]
            removed = before - len(self.data["notes"])
            if removed:
                self._save()
            return removed

    # -- undo / state --------------------------------------------------------------
    def undo_last(self):
        """Remove the most recent thing added by voice. Returns its title, or None."""
        with self.lock:
            while self._undo:
                kind, id_, title = self._undo.pop()
                if (self.delete_item(id_) if kind == "item" else self.delete_note(id_)):
                    return title
        return None

    def get_state(self, key, default=None):
        return self.data["state"].get(key, default)

    def set_state(self, key, value):
        with self.lock:
            self.data["state"][key] = value
            self._save()


# ---------------------------------------------------------------- reminders -- #
def reminder_time(it, occ):
    """When this occurrence should remind, or None (no reminder, or a finished to-do)."""
    if it.get("reminder_min") is None or (it["kind"] == "task" and it.get("done")):
        return None
    at = ALL_DAY_REMIND_AT if it["all_day"] else dt.time.fromisoformat(it["time"])
    return dt.datetime.combine(occ, at) - dt.timedelta(minutes=it["reminder_min"])


def due_reminders(store, now):
    """The scheduler's one step: [(item, occurrence_date, kind)] with kind 'due' (on time), 'late'
    (missed by <= CATCH_UP_MIN) or 'stale' (older - log only). Each is marked fired before returning,
    so a reminder can be lost in a crash but never repeated."""
    out = []
    lo, hi = now.date() - dt.timedelta(days=8), now.date() + dt.timedelta(days=1)
    with store.lock:
        for it in list(store.data["items"]):
            for occ, key in store.occurrences(it, lo, hi):
                if key in it["fired"]:
                    continue
                at = reminder_time(it, occ)
                if at is None or at > now:
                    continue
                late_min = (now - at).total_seconds() / 60
                kind = "due" if late_min < 1.5 else ("late" if late_min <= CATCH_UP_MIN else "stale")
                store.mark_fired(it["id"], key, now, "fired" if kind != "stale" else "missed")
                out.append((it, occ, kind))
    return out


def reminder_text(it, occ, kind, now):
    title = it["title"]
    when = f" at {W.fmt_time(it['time'])}" if it["time"] else ""
    if kind == "late":
        return f"While I was away, sir: {title}{when}, {W.day_name(occ, now.date())}."
    if it["time"] and it.get("reminder_min"):
        return f"Sir, {title} is at {W.fmt_time(it['time'])}, in {it['reminder_min']} minutes."
    return f"Sir, reminder: {title}{when}."


# ---------------------------------------------------------------- speech ----- #
def item_phrase(it) -> str:
    return it["title"] + (f" at {W.fmt_time(it['time'])}" if it["time"] else "")


def join_and(parts) -> str:
    parts = list(parts)
    if len(parts) <= 1:
        return "".join(parts)
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def count_phrase(n_ev, n_task) -> str:
    parts = []
    if n_ev:
        parts.append(f"{n_ev} event{'s' if n_ev != 1 else ''}")
    if n_task:
        parts.append(f"{n_task} to-do{'s' if n_task != 1 else ''}")
    return join_and(parts)


def describe_range(store, start, end, label, now):
    """Spoken answer to 'what do I have <label>'. Returns (text, overflowed) - overflowed means
    not everything was read out, so the caller should show the Calendar tab."""
    today = now.date()
    by_day = {}
    for d, it in store.items_between(start, end, include_done=False):
        by_day.setdefault(d, []).append(it)
    notes = store.notes_between(start, end)
    n_ev = sum(1 for items in by_day.values() for i in items if i["kind"] == "event")
    n_task = sum(1 for items in by_day.values() for i in items if i["kind"] == "task")
    parts, overflow = [], False

    if not by_day:
        parts.append(f"You have nothing planned {label}, sir.")
        nxt = store.next_item(dt.datetime.combine(end, dt.time(23, 59)))
        if nxt:
            d, it = nxt
            parts.append(f"The next thing is {item_phrase(it)}, {W.day_name(d, today)}.")
    else:
        head = f"{label[:1].upper() + label[1:]} you have {count_phrase(n_ev, n_task)}, sir"
        spoken = 0
        if start == end:
            phrases = [item_phrase(i) for d in sorted(by_day) for i in by_day[d]]
            if len(phrases) > MAX_SPOKEN:
                phrases, overflow = phrases[:MAX_SPOKEN] + [f"{len(phrases) - MAX_SPOKEN} more"], True
            parts.append(f"{head}: {join_and(phrases)}.")
        else:
            parts.append(head + ".")
            for d in sorted(by_day):
                room = MAX_SPOKEN - spoken
                if room <= 0:
                    overflow = True
                    break
                phrases = [item_phrase(i) for i in by_day[d]][:room]
                spoken += len(phrases)
                name = W.day_name(d, today)
                parts.append(f"{name[:1].upper() + name[1:]}: {join_and(phrases)}.")
            left = n_ev + n_task - spoken
            if left > 0:
                overflow = True
                parts.append(f"And {left} more. They're on the Calendar tab.")

    if start <= today <= end:
        overdue = store.overdue_tasks(today)
        if overdue:
            names = join_and(t["title"] for t in overdue[:3]) + (" and more" if len(overdue) > 3 else "")
            parts.append(f"You also have {len(overdue)} overdue to-do{'s' if len(overdue) != 1 else ''}: {names}.")
    if notes:
        if start == end and len(notes) <= 3:
            parts.append("Notes: " + "; ".join(n["text"] for n in notes) + ".")
        else:
            parts.append(f"Plus {len(notes)} note{'s' if len(notes) != 1 else ''}.")
    return " ".join(parts), overflow


def describe_next(store, now) -> str:
    nxt = store.next_item(now)
    if not nxt:
        return "Nothing coming up, sir. Your calendar is clear."
    d, it = nxt
    return f"Next up: {item_phrase(it)}, {W.day_name(d, now.date())}."


def describe_todos(store, now) -> str:
    today = now.date()
    tasks = store.open_tasks()
    if not tasks:
        return "Your to-do list is empty, sir."
    overdue = [t for t in tasks if t["date"] < today.isoformat()]
    due_today = [t for t in tasks if t["date"] == today.isoformat()]
    later = [t for t in tasks if t["date"] > today.isoformat()]
    parts = [f"You have {len(tasks)} to-do{'s' if len(tasks) != 1 else ''}, sir."]
    if overdue:
        parts.append("Overdue: " + join_and(item_phrase(t) for t in overdue[:5]) + ".")
    if due_today:
        parts.append("Today: " + join_and(item_phrase(t) for t in due_today[:6]) + ".")
    if later:
        parts.append("Later: " + join_and(
            f"{t['title']} {W.day_label(dt.date.fromisoformat(t['date']), today)}" for t in later[:5]) + ".")
    return " ".join(parts)


def briefing(store, now) -> str:
    greet = "Good morning" if now.hour < 12 else "Good afternoon" if now.hour < 17 else "Good evening"
    text, _ = describe_range(store, now.date(), now.date(), "today", now)
    return f"{greet}, sir. " + text.replace(", sir", "", 1)
