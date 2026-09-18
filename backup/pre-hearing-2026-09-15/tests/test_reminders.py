"""xyrus.reminders - the sched_proto.py scenario, v1 test_calendar.py's reminder checks, exactly-once across a
restart, alert wording, catch-up, snooze / acknowledge / 2-minute repeat, quiet hours. Temp stores only."""
from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

_TMP_ROOT = tempfile.mkdtemp(prefix="xyrus_t2_rem_")
os.environ.setdefault("XYRUS_DATA_DIR", _TMP_ROOT)

from xyrus import reminders as R  # noqa: E402
from xyrus.store import CalendarStore  # noqa: E402

D = dt.date
T = lambda *a: dt.datetime(2026, 9, 13, *a)       # noqa: E731  Sunday 13 Sep 2026


def new_store() -> CalendarStore:
    return CalendarStore(Path(tempfile.mkdtemp(dir=_TMP_ROOT)) / "calendar.json")


def got(alerts):
    return [(a.item["title"], a.kind) for a in alerts]


def tearDownModule():
    shutil.rmtree(_TMP_ROOT, ignore_errors=True)


class SchedProtoScenario(unittest.TestCase):
    """SCRATCH/sched_proto.py re-expressed against the real store + reminders."""

    def test_scenario(self):
        s = new_store()
        dentist = s.add_event("Dentist", D(2026, 9, 13), dt.time(15, 0), 30, now=T(10, 0))
        s.add_event("Pay rent", D(2026, 9, 13), None, 0, now=T(8, 0))                    # all-day -> 09:00
        s.add_event("Standup", D(2026, 9, 10), dt.time(9, 30), 0, repeat="daily", now=T(8, 0))
        s.add_event("No reminder", D(2026, 9, 13), dt.time(11, 0), None)
        s.add_note(D(2026, 9, 13), "buy milk", now=T(8, 1))

        # stale on first tick: standup of the 10th-12th was never fired (PC off) -> silently marked now
        self.assertEqual(got(R.due_alerts(s, T(8, 59))), [("Standup", "stale")] * 3)
        self.assertEqual(R.due_alerts(s, T(8, 59, 30)), [])
        # all-day at 09:00
        self.assertEqual(got(R.due_alerts(s, T(9, 0, 10))), [("Pay rent", "due")])
        # never twice
        self.assertEqual(R.due_alerts(s, T(9, 0, 40)), [])
        self.assertEqual(got(R.due_alerts(s, T(9, 30, 5))), [("Standup", "due")])
        # restart: a fresh store from the same file remembers what fired
        s2 = CalendarStore(s.path)
        self.assertEqual(R.due_alerts(s2, T(9, 31)), [])
        # late after sleep: asleep 14:00 -> 15:10, the 14:30 reminder is 40 min late
        self.assertEqual(got(R.due_alerts(s2, T(15, 10))), [("Dentist", "late")])
        # stale next day: off since yesterday, standup 09:30 is 150 min late at 12:00
        self.assertEqual(got(R.due_alerts(s2, dt.datetime(2026, 9, 14, 12, 0))), [("Standup", "stale")])
        # edit clears fired: the moved dentist fires again at its new time
        s2.update_item(dentist["id"], date="2026-09-14", time="16:00")
        self.assertEqual(got(R.due_alerts(s2, dt.datetime(2026, 9, 14, 15, 30, 20))), [("Dentist", "due")])
        saved = json.loads(s2.path.read_text("utf8"))
        marks = [m for it in saved["items"] for m in it["fired"].values()]
        self.assertTrue(any(m.startswith("missed@") for m in marks))
        self.assertTrue(next(it for it in saved["items"] if it["title"] == "Dentist")["fired"]["2026-09-14"]
                        .startswith("fired@"))


class V1ReminderChecks(unittest.TestCase):
    """v1 test_calendar.py 'reminders and briefing' (reminder part)."""

    def test_call_mom_at_five(self):
        s = new_store()
        s.add_task("Call mom", D(2026, 9, 16), dt.time(17, 0), 0, source="voice")
        self.assertEqual(R.due_alerts(s, dt.datetime(2026, 9, 16, 16, 59, 30)), [], "nothing before it's due")
        alerts = R.due_alerts(s, dt.datetime(2026, 9, 16, 17, 0, 10))
        self.assertEqual(got(alerts), [("Call mom", "due")])
        self.assertEqual(R.alert_text(alerts[0], dt.datetime(2026, 9, 16, 17, 0, 10)), "Sir, reminder: Call mom at 5 pm.")
        self.assertEqual(R.due_alerts(s, dt.datetime(2026, 9, 16, 17, 0, 40)), [], "never twice")


class ExactlyOnce(unittest.TestCase):
    def test_marked_before_return_and_across_restart(self):
        s = new_store()
        it = s.add_event("Dentist", D(2026, 9, 13), "15:00", 10)
        alerts = R.due_alerts(s, T(14, 50, 3))
        self.assertEqual(got(alerts), [("Dentist", "due")])
        on_disk = json.loads(s.path.read_text("utf8"))["items"][0]["fired"]
        self.assertEqual(on_disk, {"2026-09-13": "fired@2026-09-13T14:50:03"}, "persisted before speaking")
        for restart_at in (T(14, 50, 30), T(14, 55), T(15, 0, 5), T(16, 0)):
            self.assertEqual(R.due_alerts(CalendarStore(s.path), restart_at), [])
        self.assertEqual(R.due_alerts(s, T(15, 0, 5)), [])
        self.assertEqual(it["reminder_min"], 10)

    def test_recurring_fires_once_per_occurrence(self):
        s = new_store()
        s.add_event("Standup", D(2026, 9, 13), "09:30", 0, repeat="daily")
        self.assertEqual(got(R.due_alerts(s, T(9, 30, 1))), [("Standup", "due")])
        self.assertEqual(R.due_alerts(s, T(9, 31)), [])
        nxt = dt.datetime(2026, 9, 14, 9, 30, 2)
        self.assertEqual(got(R.due_alerts(CalendarStore(s.path), nxt)), [("Standup", "due")])


class Rules(unittest.TestCase):
    def test_no_reminder_and_done_todos_never_fire(self):
        s = new_store()
        s.add_event("Quiet", D(2026, 9, 13), "11:00", None)
        t = s.add_task("Buy milk", D(2026, 9, 13), "11:00", 0)
        s.set_done(t["id"])
        s.add_task("Someday", D(2026, 9, 13))                   # to-do without a time: reminder None
        self.assertEqual(R.due_alerts(s, T(11, 0, 5)), [])
        self.assertIsNone(R.reminder_time(s.get_item(t["id"]), D(2026, 9, 13)))

    def test_all_day_time_and_catch_up_from_config(self):
        s = new_store()
        s.add_event("Rent", D(2026, 9, 13), None, 0)
        cfg = {"calendar": {"all_day_reminder_time": "08:00", "catch_up_min": 30}}
        self.assertEqual(R.due_alerts(s, T(7, 59), cfg=cfg), [])
        self.assertEqual(got(R.due_alerts(s, T(8, 0, 20), cfg=cfg)), [("Rent", "due")])
        s.add_event("Call", D(2026, 9, 13), "10:00", 0)
        self.assertEqual(got(R.due_alerts(s, T(10, 40), cfg=cfg)), [("Call", "stale")], "40 min > catch_up 30")

    def test_classification_edges(self):
        s = new_store()
        s.add_event("A", D(2026, 9, 13), "10:00", 0)         # 1.3 min late  -> due   (< 1.5)
        s.add_event("B", D(2026, 9, 13), "08:05", 0)         # 116.3 min     -> late  (<= 120)
        s.add_event("C", D(2026, 9, 13), "08:00", 0)         # 121.3 min     -> stale (> 120)
        kinds = dict(got(R.due_alerts(s, T(10, 1, 20))))
        self.assertEqual(kinds, {"A": "due", "B": "late", "C": "stale"})

    def test_wording(self):
        now = T(14, 50)
        s = new_store()
        dent = s.add_event("Dentist", D(2026, 9, 13), "15:00", 10)
        mom = s.add_task("Call mom", D(2026, 9, 13), "17:00", 0)
        rent = s.add_event("Pay rent", D(2026, 9, 13), None, 0)
        hour = s.add_event("Flight", D(2026, 9, 13), "18:00", 60)
        A = R.Alert
        self.assertEqual(R.alert_text(A(dent, D(2026, 9, 13), "due"), now), "Sir, Dentist is at 3 pm, in 10 minutes.")
        self.assertEqual(R.alert_text(A(mom, D(2026, 9, 13), "due"), now), "Sir, reminder: Call mom at 5 pm.")
        self.assertEqual(R.alert_text(A(rent, D(2026, 9, 13), "due"), now), "Sir, reminder: Pay rent.")
        self.assertEqual(R.alert_text(A(dent, D(2026, 9, 13), "late"), now), "While I was away, sir: Dentist at 3 pm, today.")
        self.assertEqual(R.alert_text(A(hour, D(2026, 9, 13), "due"), now), "Sir, Flight is at 6 pm, in 1 hour.")
        blank = {"address_as": ""}
        self.assertEqual(R.alert_text(A(dent, D(2026, 9, 13), "due"), now, blank), "Dentist is at 3 pm, in 10 minutes.")
        self.assertEqual(R.alert_text(A(mom, D(2026, 9, 13), "due"), now, blank), "Reminder: Call mom at 5 pm.")
        self.assertEqual(R.alert_text(A(dent, D(2026, 9, 13), "late"), now, blank), "While I was away: Dentist at 3 pm, today.")
        self.assertEqual(R.alert_text(A(mom, D(2026, 9, 13), "due"), now, {"address_as": "boss"}),
                         "Boss, reminder: Call mom at 5 pm.")
        self.assertEqual(R.alert_text(A(dent, D(2026, 9, 13), "due", again=True), now), "Sir, reminder: Dentist at 3 pm.")
        self.assertEqual(R.repeat_text(A(dent, D(2026, 9, 13), "due")), "Dentist — still pending, sir.")

    def test_missed_summary(self):
        s = new_store()
        dent = s.add_event("Dentist", D(2026, 9, 13), "15:00", 10)
        stand = s.add_event("Standup", D(2026, 9, 13), "09:30", 0)
        gym = s.add_event("Gym", D(2026, 9, 12), "18:00", 0)
        now = T(16, 0)
        A = R.Alert
        self.assertEqual(R.missed_summary([], now), "")
        self.assertEqual(R.missed_summary([A(stand, D(2026, 9, 13), "stale")], now), "")
        self.assertEqual(R.missed_summary([A(dent, D(2026, 9, 13), "late")], now),
                         "While I was away, sir: Dentist at 3 pm, today.")
        self.assertEqual(R.missed_summary([A(dent, D(2026, 9, 13), "late"), A(stand, D(2026, 9, 13), "late")], now),
                         "While I was away, sir: Dentist at 3 pm and Standup at 9:30 am.")
        three = [A(dent, D(2026, 9, 13), "late"), A(gym, D(2026, 9, 12), "late"), A(stand, D(2026, 9, 13), "late")]
        self.assertEqual(R.missed_summary(three, now),
                         "While I was away, sir: Dentist at 3 pm, Gym at 6 pm yesterday and 1 more.")


class FollowUp(unittest.TestCase):
    def setUp(self):
        self.s = new_store()
        self.mom = self.s.add_task("Call mom", D(2026, 9, 13), "17:00", 0, source="voice")
        (self.alert,) = R.due_alerts(self.s, T(17, 0, 10))

    def test_last_alert(self):
        a = R.last_alert(self.s, T(17, 5))
        self.assertEqual((a.item["id"], a.occ), (self.mom["id"], D(2026, 9, 13)))
        self.assertIsNone(R.last_alert(self.s, T(17, 45)), "too old to be what the user answers")
        self.assertEqual(R.last_alert(CalendarStore(self.s.path), T(17, 1)).item["id"], self.mom["id"])

    def test_snooze_comes_back_once_and_survives_restart(self):
        at = R.snooze(self.s, self.alert, T(17, 0, 30), 10)
        self.assertEqual(at, T(17, 10, 30))
        self.assertEqual(R.due_repeats(self.s, T(17, 2, 30)), [], "a snoozed alert doesn't also repeat")
        self.assertFalse(R.is_acked(self.s, self.alert), "snoozing is not acknowledging")
        self.assertEqual(R.due_alerts(self.s, T(17, 5)), [])
        s2 = CalendarStore(self.s.path)
        back = R.due_alerts(s2, T(17, 10, 31))
        self.assertEqual([(a.item["title"], a.kind, a.again) for a in back], [("Call mom", "due", True)])
        self.assertEqual(R.alert_text(back[0], T(17, 10, 31)), "Sir, reminder: Call mom at 5 pm.")
        self.assertEqual(R.due_alerts(s2, T(17, 11)), [])
        self.assertEqual(R.last_alert(s2, T(17, 12)).item["id"], self.mom["id"])

    def test_snoozed_then_done_is_dropped(self):
        R.snooze(self.s, self.alert, T(17, 0, 30), 5)
        self.s.set_done(self.mom["id"])
        self.assertEqual(R.due_alerts(self.s, T(17, 6)), [])

    def test_repeat_once_after_two_minutes_unless_acked(self):
        self.assertEqual(R.due_repeats(self.s, T(17, 1)), [])
        rep = R.due_repeats(self.s, T(17, 2, 30))
        self.assertEqual(got(rep), [("Call mom", "due")])
        self.assertEqual(R.due_repeats(self.s, T(17, 3)), [], "repeats once")
        self.assertEqual(R.due_repeats(CalendarStore(self.s.path), T(17, 4)), [])

    def test_acknowledged_alert_does_not_repeat(self):
        R.acknowledge(self.s, self.alert)
        self.assertEqual(R.due_repeats(self.s, T(17, 2, 30)), [])
        self.assertEqual(R.unacked_overdue(self.s, T(17, 20)), [])

    def test_late_alerts_do_not_repeat(self):
        s = new_store()
        s.add_event("Dentist", D(2026, 9, 13), "15:00", 0)
        self.assertEqual(got(R.due_alerts(s, T(15, 40))), [("Dentist", "late")])
        self.assertEqual(R.due_repeats(s, T(15, 42, 30)), [])

    def test_still_pending(self):
        pend = R.unacked_overdue(self.s, T(17, 20))
        self.assertEqual([(it["title"], occ, ago) for it, occ, ago in pend], [("Call mom", D(2026, 9, 13), 20)])
        self.assertEqual(R.still_pending_text(pend[0][0], pend[0][2]), "You still have a reminder: Call mom, from 20 minutes ago.")
        self.assertEqual(R.still_pending_text(pend[0][0], 33), "You still have a reminder: Call mom, from 35 minutes ago.")
        self.assertEqual(R.unacked_overdue(self.s, T(18, 30)), [], "older than an hour")


class QuietHours(unittest.TestCase):
    def test_quiet_hours_skip_lead_alerts_only(self):
        cfg = {"quiet_hours": {"from": "23:00", "to": "07:00"}}
        self.assertTrue(R.in_quiet_hours(T(23, 30), cfg))
        self.assertTrue(R.in_quiet_hours(T(6, 59), cfg))
        self.assertFalse(R.in_quiet_hours(T(7, 0), cfg))
        self.assertFalse(R.in_quiet_hours(T(23, 30), {"quiet_hours": None}))
        self.assertFalse(R.in_quiet_hours(T(23, 30), None))
        s = new_store()
        lead = R.Alert(s.add_event("Flight", D(2026, 9, 14), "00:30", 60), D(2026, 9, 14), "due")
        at_time = R.Alert(s.add_task("Pills", D(2026, 9, 13), "23:30", 0), D(2026, 9, 13), "due")
        stale = R.Alert(at_time.item, D(2026, 9, 13), "stale")
        self.assertFalse(R.should_speak(lead, T(23, 30), cfg))
        self.assertTrue(R.should_speak(at_time, T(23, 30), cfg))
        self.assertTrue(R.should_speak(lead, T(12, 0), cfg))
        self.assertFalse(R.should_speak(stale, T(12, 0), cfg))


if __name__ == "__main__":
    unittest.main()
