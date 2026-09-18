"""Proactive alerts for Xyrus v2 (SPEC §4.10, §3.9 "Proactive alerts" / "Missed while away").

Lifted from v1 D:\\arc\\calendar_store.py (D14): reminder_time, due_reminders and reminder_text are v1's
functions (verbatim apart from the configurable honorific and the config-driven catch-up window / all-day
time), wrapped as due_alerts / alert_text / missed_summary.

Model (v1): one reminder per occurrence at start - reminder_min (all-day items at 09:00); reminder_min None
never fires; done to-dos never fire. Each occurrence is marked fired in calendar.json BEFORE it is returned,
so a crash can lose one spoken reminder but never repeat one - and a restart never repeats it either.
Classification: 'due' (< 1.5 min late), 'late' (<= catch_up_min, spoken "While I was away, sir: ..."),
'stale' (older: Activity only).

v2 additions kept in calendar.json["state"] (same file, extra keys only): "acked" (via store.acknowledge),
"snoozed" [{id, occ, at}], "repeated" {id: [occ]} (the once-only 2-minute repeat), "last_alert" {id, occ, at}.

G8: never reads the clock - every function takes `now`.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Literal

from xyrus import dates as W
from xyrus.assistant import address, cap, cfg_get, item_phrase, missed_phrase, sir

CATCH_UP_MIN = 120                  # a reminder missed by <= 2 h is still spoken ("while I was away ...")
ALL_DAY_REMIND_AT = dt.time(9, 0)   # all-day items with a reminder fire at 09:00
REPEAT_AFTER_MIN = 2                # unacknowledged due alert repeats once after this (should)
DEFAULT_SNOOZE_MIN = 10


@dataclass(frozen=True)
class Alert:
    item: dict
    occ: dt.date
    kind: Literal["due", "late", "stale"]
    again: bool = False             # a snoozed alert coming back ("remind me again in ...")

    @property
    def key(self) -> str:
        return self.occ.isoformat()


# ---------------------------------------------------------------- v1 (lifted) --------------- #
def reminder_time(it, occ, all_day_at=ALL_DAY_REMIND_AT):
    """When this occurrence should remind, or None (no reminder, or a finished to-do)."""
    if it.get("reminder_min") is None or (it["kind"] == "task" and it.get("done")):
        return None
    at = all_day_at if it["all_day"] else dt.time.fromisoformat(it["time"])
    return dt.datetime.combine(occ, at) - dt.timedelta(minutes=it["reminder_min"])


def due_reminders(store, now, catch_up_min=CATCH_UP_MIN, all_day_at=ALL_DAY_REMIND_AT):
    """The scheduler's one step: [(item, occurrence_date, kind)] with kind 'due' (on time), 'late'
    (missed by <= catch_up_min) or 'stale' (older - log only). Each is marked fired before returning,
    so a reminder can be lost in a crash but never repeated."""
    out = []
    lo, hi = now.date() - dt.timedelta(days=8), now.date() + dt.timedelta(days=1)
    with store.lock:
        for it in list(store.data["items"]):
            for occ, key in store.occurrences(it, lo, hi):
                if key in it["fired"]:
                    continue
                at = reminder_time(it, occ, all_day_at)
                if at is None or at > now:
                    continue
                late_min = (now - at).total_seconds() / 60
                kind = "due" if late_min < 1.5 else ("late" if late_min <= catch_up_min else "stale")
                store.mark_fired(it["id"], key, now, "fired" if kind != "stale" else "missed")
                out.append((it, occ, kind))
    return out


def _in_minutes(m: int) -> str:
    if m < 60:
        return f"in {m} minute{'s' if m != 1 else ''}"
    if m % 1440 == 0:
        return f"in {m // 1440} day{'s' if m != 1440 else ''}"
    if m % 60 == 0:
        return f"in {m // 60} hour{'s' if m != 60 else ''}"
    return f"in {m} minutes"


def _lead(cfg, body: str) -> str:
    """v1 starts alerts with 'Sir, ...'; a blank honorific drops it and capitalises the body."""
    a = address(cfg)
    return f"{cap(a)}, {body}" if a else cap(body)


def reminder_text(it, occ, kind, now, cfg=None):
    title = it["title"]
    when = f" at {W.fmt_time(it['time'])}" if it["time"] else ""
    if kind == "late":
        return f"While I was away{sir(cfg)}: {title}{when}, {W.day_name(occ, now.date())}."
    if it["time"] and it.get("reminder_min"):
        return _lead(cfg, f"{title} is at {W.fmt_time(it['time'])}, {_in_minutes(it['reminder_min'])}.")
    return _lead(cfg, f"reminder: {title}{when}.")


# ---------------------------------------------------------------- v2 wrappers ---------------- #
def _catch_up(cfg, catch_up_min):
    if catch_up_min is not None:
        return catch_up_min
    return int(cfg_get(cfg, "calendar.catch_up_min", CATCH_UP_MIN))


def _all_day_at(cfg, all_day_at):
    if all_day_at is not None:
        return all_day_at
    raw = cfg_get(cfg, "calendar.all_day_reminder_time", None)
    try:
        return dt.time.fromisoformat(raw) if raw else ALL_DAY_REMIND_AT
    except (TypeError, ValueError):
        return ALL_DAY_REMIND_AT


def due_alerts(store, now: dt.datetime, *, cfg=None, catch_up_min: int | None = None,
               all_day_at: dt.time | None = None) -> list[Alert]:
    """= v1 due_reminders (marks fired before returning), plus snoozed alerts whose time has come
    (again=True). Records the newest spoken one as state["last_alert"] for the alert follow-up."""
    if not hasattr(store, "data"):                  # a stub store (engine unit tests): nothing to schedule
        return []
    out = [Alert(it, occ, kind)
           for it, occ, kind in due_reminders(store, now, _catch_up(cfg, catch_up_min), _all_day_at(cfg, all_day_at))]
    out += _due_snoozes(store, now)
    spoken = [a for a in out if a.kind != "stale"]
    if spoken:
        a = spoken[-1]
        store.set_state("last_alert", {"id": a.item["id"], "occ": a.key, "at": now.isoformat(timespec="seconds")})
    return out


def alert_text(a: Alert, now: dt.datetime, cfg=None) -> str:
    """= v1 reminder_text, honorific from config: 'Sir, Dentist is at 3 pm, in 10 minutes.' /
    'Sir, reminder: Call mom at 5 pm.' / 'While I was away, sir: ...'. A snoozed alert uses the at-time form."""
    if a.again:
        it = a.item
        when = f" at {W.fmt_time(it['time'])}" if it["time"] else ""
        return _lead(cfg, f"reminder: {it['title']}{when}.")
    return reminder_text(a.item, a.occ, a.kind, now, cfg)


def missed_summary(alerts: list[Alert], now: dt.datetime, cfg=None) -> str:
    """One sentence for several 'late' alerts at startup (v1 wording for a single one); "" if none."""
    return missed_phrase(alerts, now, cfg)


def is_lead(a: Alert) -> bool:
    """A 'heads-up' before the event (reminder_min > 0), as opposed to one at the time."""
    return a.kind == "due" and not a.again and bool(a.item.get("reminder_min"))


def in_quiet_hours(now: dt.datetime, cfg=None) -> bool:
    q = cfg_get(cfg, "quiet_hours", None)
    if not isinstance(q, dict) or not q.get("from") or not q.get("to"):
        return False
    try:
        lo, hi = dt.time.fromisoformat(q["from"]), dt.time.fromisoformat(q["to"])
    except (TypeError, ValueError):
        return False
    t = now.time()
    return lo <= t < hi if lo <= hi else (t >= lo or t < hi)


def should_speak(a: Alert, now: dt.datetime, cfg=None) -> bool:
    """Stale alerts are Activity-only; quiet hours skip lead alerts only (due alerts still speak)."""
    if a.kind == "stale":
        return False
    return not (is_lead(a) and in_quiet_hours(now, cfg))


# ---------------------------------------------------------------- acknowledge / snooze ------ #
def acknowledge(store, a: Alert) -> None:
    if hasattr(store, "acknowledge"):
        store.acknowledge(a.item["id"], a.key)


def is_acked(store, a: Alert) -> bool:
    fn = getattr(store, "is_acked", None)
    return bool(fn(a.item["id"], a.key)) if fn else False


def snooze(store, a: Alert, now: dt.datetime, minutes: int = DEFAULT_SNOOZE_MIN) -> dt.datetime:
    """'snooze' / 'remind me again in <duration>': the same alert comes back from due_alerts at now+minutes."""
    at = now + dt.timedelta(minutes=minutes)
    with store.lock:
        snoozed = [z for z in store.get_state("snoozed", []) or []
                   if not (z.get("id") == a.item["id"] and z.get("occ") == a.key)]
        snoozed.append({"id": a.item["id"], "occ": a.key, "at": at.isoformat(timespec="seconds")})
        store.set_state("snoozed", snoozed)
        # no 2-minute repeat for a snoozed alert (not an acknowledgement: "okay" after it still means "Noted")
        repeated = store.get_state("repeated", {}) or {}
        keys = repeated.setdefault(a.item["id"], [])
        if a.key not in keys:
            keys.append(a.key)
        store.set_state("repeated", repeated)
    return at


def _due_snoozes(store, now) -> list[Alert]:
    with store.lock:
        snoozed = store.get_state("snoozed", []) or []
        if not snoozed:
            return []
        out, keep = [], []
        for z in snoozed:
            try:
                at = dt.datetime.fromisoformat(z["at"])
            except (KeyError, TypeError, ValueError):
                continue
            if at > now:
                keep.append(z)
                continue
            it = store.get_item(z.get("id"))
            if it is None or (it["kind"] == "task" and it.get("done")):
                continue
            out.append(Alert(it, dt.date.fromisoformat(z["occ"]), "due", again=True))
        store.set_state("snoozed", keep)
        return out


def last_alert(store, now: dt.datetime, within_min: int = 30) -> Alert | None:
    """The alert the user is answering ('okay', 'snooze') - the newest spoken one within `within_min`."""
    rec = store.get_state("last_alert")
    if not isinstance(rec, dict):
        return None
    try:
        at = dt.datetime.fromisoformat(rec["at"])
        occ = dt.date.fromisoformat(rec["occ"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (dt.timedelta(0) <= now - at <= dt.timedelta(minutes=within_min)):
        return None
    it = store.get_item(rec.get("id"))
    return Alert(it, occ, "due") if it is not None else None


# ---------------------------------------------------------------- repeats / still pending --- #
def _fired_at(mark: str) -> dt.datetime | None:
    if not isinstance(mark, str) or not mark.startswith("fired@"):
        return None
    try:
        return dt.datetime.fromisoformat(mark[len("fired@"):])
    except ValueError:
        return None


def due_repeats(store, now: dt.datetime, after_min: int = REPEAT_AFTER_MIN, window_min: int = 10,
                cfg=None) -> list[Alert]:
    """(should) An unacknowledged 'due' alert repeats ONCE, `after_min` minutes after it fired (not later
    than `window_min`). Marked in state["repeated"] before returning, so it never repeats twice."""
    all_day_at = _all_day_at(cfg, None)
    out = []
    if not hasattr(store, "data"):
        return out
    with store.lock:
        repeated = store.get_state("repeated", {}) or {}
        for it in list(store.data["items"]):
            if it["kind"] == "task" and it.get("done"):
                continue
            for key, mark in list(it.get("fired", {}).items()):
                fired = _fired_at(mark)
                if fired is None or key in repeated.get(it["id"], []) or store.is_acked(it["id"], key):
                    continue
                occ = dt.date.fromisoformat(key)
                at = reminder_time(it, occ, all_day_at)
                if at is None or (fired - at).total_seconds() / 60 >= 1.5:      # only on-time ('due') alerts
                    continue
                age = (now - fired).total_seconds() / 60
                if after_min <= age <= window_min:
                    repeated.setdefault(it["id"], []).append(key)
                    out.append(Alert(it, occ, "due"))
        if out:
            store.set_state("repeated", repeated)
    return out


def repeat_text(a: Alert, cfg=None) -> str:
    return f"{a.item['title']} — still pending{sir(cfg)}."


def unacked_overdue(store, now: dt.datetime, within_min: int = 60) -> list[tuple[dict, dt.date, int]]:
    """Reminders (reminder_min 0) that fired, whose time has passed within `within_min`, and that nobody
    acknowledged: [(item, occurrence, minutes ago)], newest first. Used by "what's next"."""
    out = []
    if not hasattr(store, "data"):
        return out
    with store.lock:
        for it in store.data["items"]:
            if it.get("reminder_min") != 0 or (it["kind"] == "task" and it.get("done")):
                continue
            for key, mark in it.get("fired", {}).items():
                if _fired_at(mark) is None or store.is_acked(it["id"], key):
                    continue
                occ = dt.date.fromisoformat(key)
                at = reminder_time(it, occ)
                if at is None:
                    continue
                ago = (now - at).total_seconds() / 60
                if 0 <= ago <= within_min:
                    out.append((it, occ, int(ago)))
    return sorted(out, key=lambda x: x[2])


def still_pending_text(it: dict, minutes_ago: int) -> str:
    """'You still have a reminder: Call the bank, from 20 minutes ago.'"""
    m = minutes_ago if minutes_ago <= 20 else 5 * round(minutes_ago / 5)
    ago = "just now" if m < 1 else f"from {m} minute{'s' if m != 1 else ''} ago"
    return f"You still have a reminder: {it['title']}, {ago}."


__all__ = ["Alert", "CATCH_UP_MIN", "ALL_DAY_REMIND_AT", "reminder_time", "due_reminders", "reminder_text",
           "due_alerts", "alert_text", "missed_summary", "is_lead", "in_quiet_hours", "should_speak",
           "acknowledge", "snooze", "last_alert", "due_repeats", "repeat_text", "unacked_overdue",
           "still_pending_text", "item_phrase"]
