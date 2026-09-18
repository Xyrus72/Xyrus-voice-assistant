"""Calendar, to-do list and day notes for Xyrus v2. Owns data/calendar.json.

Lifted verbatim from v1 D:\\arc\\calendar_store.py (SPEC §4.10, D14) - the file format is unchanged, so v2
opens v1's data/calendar.json as is. Changes: DATA_FILE -> paths.DATA_DIR/"calendar.json" (resolved when a
store is created, so XYRUS_DATA_DIR works in tests); `import when` -> `xyrus.dates`; the reminder functions
moved to xyrus/reminders.py and the spoken summaries to xyrus/assistant.py; added acknowledge() / is_acked()
/ prune(). Never speaks, never fires alerts, never imports engine/ui.

Every public method takes the lock; every change bumps .version (the UI polls it) and is written
atomically (tmp file + os.replace), so a crash can't leave a half-written file.

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

from xyrus import dates as W  # noqa: F401  (kept from v1's `import when as W`; used by lifters of this module)


def data_file() -> Path:
    """paths.DATA_DIR/"calendar.json". Falls back to the §4.4 formula while paths.py isn't importable."""
    try:
        from xyrus import paths
        return Path(paths.data_dir() if hasattr(paths, "data_dir") else paths.DATA_DIR) / "calendar.json"
    except ImportError:
        base = Path(__file__).resolve().parent.parent
        return Path(os.environ.get("XYRUS_DATA_DIR", base / "data")) / "calendar.json"


def _iso(d):
    return d.isoformat() if isinstance(d, dt.date) else d


def _hhmm(t):
    return t.strftime("%H:%M") if isinstance(t, dt.time) else t


class CalendarStore:
    def __init__(self, path=None):
        self.path = Path(path) if path else data_file()
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

    # -- v2 additions (SPEC §4.10) ----------------------------------------------------
    def acknowledge(self, item_id, occ_key):
        """The user acknowledged this occurrence's alert ("okay", "got it"): it won't repeat or be offered
        again by "what's next". Stored as state["acked"][item_id] = [occurrence keys]."""
        occ_key = _iso(occ_key)
        with self.lock:
            keys = self.data["state"].setdefault("acked", {}).setdefault(item_id, [])
            if occ_key not in keys:
                keys.append(occ_key)
                self._save()

    def is_acked(self, item_id, occ_key) -> bool:
        with self.lock:
            return _iso(occ_key) in self.data["state"].get("acked", {}).get(item_id, [])

    def prune(self, today=None, keep_days=60) -> int:
        """Drop fired marks older than `keep_days` on recurring items (they would grow forever) and the
        acked / repeated / snoozed bookkeeping of deleted items or of occurrences that old. Run on startup
        and daily; records state["pruned"]. Returns the number of entries removed."""
        today = today or dt.date.today()
        cutoff = (today - dt.timedelta(days=keep_days)).isoformat()
        removed = 0
        with self.lock:
            ids = set()
            for it in self.data["items"]:
                ids.add(it["id"])
                if it.get("repeat"):
                    old = [k for k in it.get("fired", {}) if k < cutoff]
                    for k in old:
                        del it["fired"][k]
                    removed += len(old)
            st = self.data["state"]
            for key in ("acked", "repeated"):
                book = st.get(key)
                if not isinstance(book, dict):
                    continue
                for iid in list(book):
                    keep = [k for k in book[iid] if k >= cutoff] if iid in ids else []
                    removed += len(book[iid]) - len(keep)
                    if keep:
                        book[iid] = keep
                    else:
                        del book[iid]
            snoozed = st.get("snoozed")
            if isinstance(snoozed, list):
                keep = [z for z in snoozed if z.get("id") in ids]
                removed += len(snoozed) - len(keep)
                st["snoozed"] = keep
            if removed or st.get("pruned") != today.isoformat():
                st["pruned"] = today.isoformat()
                self._save()
        return removed
