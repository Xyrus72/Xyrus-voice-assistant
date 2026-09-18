"""xyrus.assistant - exact spoken summaries (v1 wording, D14) with a temp store and a fixed `now`."""
from __future__ import annotations

import datetime as dt
import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path

_TMP_ROOT = tempfile.mkdtemp(prefix="xyrus_t2_asst_")
os.environ.setdefault("XYRUS_DATA_DIR", _TMP_ROOT)

from xyrus import assistant as A  # noqa: E402
from xyrus import reminders as R  # noqa: E402
from xyrus.store import CalendarStore  # noqa: E402

D = dt.date
WED = dt.datetime(2026, 9, 16, 10, 0)          # Wednesday 16 Sep 2026, 10:00
SUN = dt.datetime(2026, 9, 13, 10, 30)
WAKE = ("cyrus", "zeros", "virus", "cirrus")
BLANK = {"address_as": ""}


def new_store() -> CalendarStore:
    return CalendarStore(Path(tempfile.mkdtemp(dir=_TMP_ROOT)) / "calendar.json")


def tearDownModule():
    shutil.rmtree(_TMP_ROOT, ignore_errors=True)


class V1Summaries(unittest.TestCase):
    """v1 test_calendar.py fixture; exact describe_range / describe_next / describe_todos text."""

    def setUp(self):
        st = self.st = new_store()
        self.milk = st.add_task("Buy milk", D(2026, 9, 16))
        self.report = st.add_task("Finish the report", D(2026, 9, 15))
        st.add_event("Dentist", D(2026, 9, 18), dt.time(15, 0), 10)
        st.add_event("Gym", D(2026, 9, 14), dt.time(18, 0), None, repeat="weekly")
        st.add_note(D(2026, 9, 16), "Bring the charger")

    def test_today(self):
        text, over = A.describe_range(self.st, D(2026, 9, 16), D(2026, 9, 16), "today", WED)
        self.assertEqual(text, "Today you have 1 to-do, sir: Buy milk. You also have 1 overdue to-do: "
                               "Finish the report. Notes: Bring the charger.")
        self.assertFalse(over)

    def test_next_7_days_grouped_by_day(self):
        text, _ = A.describe_range(self.st, D(2026, 9, 16), D(2026, 9, 22), "in the next 7 days", WED)
        self.assertEqual(text, "In the next 7 days you have 2 events and 1 to-do, sir. Today: Buy milk. "
                               "Friday: Dentist at 3 pm. Monday: Gym at 6 pm. You also have 1 overdue to-do: "
                               "Finish the report. Plus 1 note.")

    def test_empty_range_names_the_next_thing(self):
        text, _ = A.describe_range(self.st, D(2026, 9, 19), D(2026, 9, 20), "this weekend", WED)
        self.assertEqual(text, "You have nothing planned this weekend, sir. The next thing is Gym at 6 pm, Monday.")

    def test_finished_todos_drop_out(self):
        self.st.set_done(self.milk["id"])
        self.assertNotIn("Buy milk", A.describe_range(self.st, D(2026, 9, 16), D(2026, 9, 16), "today", WED)[0])

    def test_whats_next(self):
        self.assertEqual(A.describe_next(self.st, WED), "Next up: Dentist at 3 pm, Friday.")
        self.assertEqual(A.describe_next(new_store(), WED), "Nothing coming up, sir. Your calendar is clear.")
        self.assertEqual(A.describe_next(new_store(), WED, BLANK), "Nothing coming up. Your calendar is clear.")

    def test_todo_list(self):
        self.st.add_task("Call the bank", D(2026, 9, 17))
        self.assertEqual(A.describe_todos(self.st, WED), "You have 3 to-dos, sir. Overdue: Finish the report. "
                                                         "Today: Buy milk. Later: Call the bank tomorrow.")
        self.assertEqual(A.describe_todos(new_store(), WED), "Your to-do list is empty, sir.")
        self.assertEqual(A.describe_todos(new_store(), WED, BLANK), "Your to-do list is empty.")

    def test_honorific_from_config(self):
        text, _ = A.describe_range(self.st, D(2026, 9, 16), D(2026, 9, 16), "today", WED, BLANK)
        self.assertTrue(text.startswith("Today you have 1 to-do: Buy milk."), text)
        text, _ = A.describe_range(self.st, D(2026, 9, 16), D(2026, 9, 16), "today", WED, {"address_as": "boss"})
        self.assertTrue(text.startswith("Today you have 1 to-do, boss: Buy milk."), text)


class Overflow(unittest.TestCase):
    def test_single_day_overflow(self):
        st = new_store()
        for h in range(8, 22):
            st.add_event(f"Slot {h}", D(2026, 9, 16), f"{h:02d}:00")
        text, over = A.describe_range(st, D(2026, 9, 16), D(2026, 9, 16), "today", WED)
        self.assertTrue(over)
        self.assertTrue(text.startswith("Today you have 14 events, sir: Slot 8 at 8 am, "), text)
        self.assertTrue(text.endswith("Slot 19 at 7 pm and 2 more."), text)

    def test_span_overflow(self):
        st = new_store()
        for day in range(16, 23):
            for h in (9, 12):
                st.add_event(f"E{day}-{h}", D(2026, 9, day), f"{h:02d}:00")
        text, over = A.describe_range(st, D(2026, 9, 16), D(2026, 9, 22), "in the next 7 days", WED)
        self.assertTrue(over)
        self.assertTrue(text.endswith("And 2 more. They're on the Calendar tab."), text)


class Briefing(unittest.TestCase):
    def test_v1_morning_briefing(self):
        st = new_store()
        st.add_task("Water the plants", D(2026, 9, 16))
        now = dt.datetime(2026, 9, 16, 8, 5)
        self.assertEqual(A.briefing(st, now), "Good morning, sir. Today you have 1 to-do: Water the plants.")
        self.assertEqual(A.briefing(st, now, BLANK), "Good morning. Today you have 1 to-do: Water the plants.")
        self.assertTrue(A.briefing(st, dt.datetime(2026, 9, 16, 13, 0)).startswith("Good afternoon, sir. Today"))
        self.assertTrue(A.briefing(st, dt.datetime(2026, 9, 16, 19, 0)).startswith("Good evening, sir. Today"))


class Notes(unittest.TestCase):
    def test_read_notes(self):
        st = new_store()
        st.add_note(D(2026, 9, 13), "buy milk", now=dt.datetime(2026, 9, 13, 8, 0))
        st.add_note(D(2026, 9, 13), "call the bank", now=dt.datetime(2026, 9, 13, 8, 1))
        today, tomorrow = D(2026, 9, 13), D(2026, 9, 14)
        self.assertEqual(A.describe_notes(st, today, today, SUN),
                         ("Two notes for today, sir. One: buy milk. Two: call the bank.", False))
        self.assertEqual(A.describe_notes(st, tomorrow, tomorrow, SUN), ("No notes for tomorrow, sir.", False))
        st.add_note(tomorrow, "bring the charger")
        self.assertEqual(A.describe_notes(st, tomorrow, tomorrow, SUN),
                         ("One note for tomorrow, sir: bring the charger.", False))
        friday = D(2026, 9, 18)
        for i in range(8):
            st.add_note(friday, f"note {i + 1}", now=dt.datetime(2026, 9, 13, 9, i))
        text, over = A.describe_notes(st, friday, friday, SUN)
        self.assertTrue(over)
        self.assertEqual(text, "Eight notes for Friday, sir. One: note 1. Two: note 2. Three: note 3. Four: note 4. "
                               "Five: note 5. And three more in the calendar.")
        self.assertEqual(A.describe_notes(st, D(2026, 9, 13), D(2026, 9, 19), SUN, label="this week")[0][:26],
                         "Eleven notes for this week")


class WhatsNext(unittest.TestCase):
    def test_and_after_that(self):
        st = new_store()
        dent = st.add_event("Dentist", D(2026, 9, 13), "15:00", 10)
        call = st.add_task("Call the bank", D(2026, 9, 13), "15:00", 0)
        gym = st.add_event("Gym", D(2026, 9, 13), "19:00")
        st.add_task("Rent", D(2026, 9, 14))
        self.assertEqual(A.describe_next(st, SUN), "Next up: Dentist at 3 pm, today.")
        seen = {(dent["id"], "2026-09-13")}
        at = dt.datetime(2026, 9, 13, 15, 0)
        self.assertEqual(A.next_after(SUN, st, at, exclude=seen), "Then Call the bank at 3 pm, today.")
        seen.add((call["id"], "2026-09-13"))
        self.assertEqual(A.next_after(SUN, st, at, exclude=seen), "Then Gym at 7 pm, today.")
        seen.add((gym["id"], "2026-09-13"))
        self.assertEqual(A.next_after(SUN, st, dt.datetime(2026, 9, 13, 19, 0), exclude=seen), "Then Rent, tomorrow.")
        d, it = A.next_after_item(st, SUN, dt.datetime(2026, 9, 13, 19, 0), seen)
        seen.add((it["id"], d.isoformat()))
        self.assertEqual(A.next_after(SUN, st, A.occ_start(d, it), exclude=seen), "Nothing after that, sir.")
        self.assertEqual([i["title"] for _, i in A.upcoming(st, SUN)][0], "Dentist")

    def test_next_event_label(self):
        st = new_store()
        self.assertIsNone(A.next_event_label(st, SUN))
        st.add_event("Dentist", D(2026, 9, 13), "12:30", 10)
        self.assertEqual(A.next_event_label(st, SUN), "Dentist at 12:30 pm · in 2 h")
        self.assertEqual(A.next_event_label(st, dt.datetime(2026, 9, 13, 12, 5)), "Dentist at 12:30 pm · in 25 min")
        st2 = new_store()
        st2.add_task("Rent", D(2026, 9, 14))
        self.assertEqual(A.next_event_label(st2, SUN), "Rent · tomorrow")


class Greetings(unittest.TestCase):
    def test_greeting(self):
        self.assertEqual(A.greeting(dt.datetime(2026, 9, 13, 5, 0)), "Good morning, sir.")
        self.assertEqual(A.greeting(dt.datetime(2026, 9, 13, 12, 0)), "Good afternoon, sir.")
        self.assertEqual(A.greeting(dt.datetime(2026, 9, 13, 17, 0)), "Good evening, sir.")
        # before 5 AM it says "Hello" (user report Sep 14 2026: "Good evening" at 3:34 AM was wrong)
        self.assertEqual(A.greeting(dt.datetime(2026, 9, 13, 4, 0)), "Hello, sir.")
        self.assertEqual(A.greeting(dt.datetime(2026, 9, 13, 19, 0), {"user_name": "Refath"}), "Good evening, Refath.")
        self.assertEqual(A.greeting(dt.datetime(2026, 9, 13, 19, 0), BLANK), "Good evening.")

    def test_startup_summary(self):
        st = new_store()
        now = dt.datetime(2026, 9, 13, 18, 0)
        cfg = {"user_name": "Refath", "address_as": "sir"}
        self.assertEqual(A.startup_summary(now, st, [], cfg), "Good evening, Refath. Xyrus online. Nothing on the calendar today.")
        dent = st.add_event("Dentist", D(2026, 9, 13), "15:00", 10)
        st.add_event("Gym", D(2026, 9, 13), "19:00")
        self.assertEqual(A.startup_summary(now, st, [], cfg),
                         "Good evening, Refath. Xyrus online. You have 2 things today; next is Gym at 7 pm.")
        missed = [R.Alert(dent, D(2026, 9, 13), "late"), R.Alert(dent, D(2026, 9, 13), "stale")]
        self.assertEqual(A.startup_summary(now, st, missed, cfg),
                         "Good evening, Refath. Xyrus online. While I was away, sir: Dentist at 3 pm, today. "
                         "You have 2 things today; next is Gym at 7 pm.")
        late_now = dt.datetime(2026, 9, 13, 21, 0)
        self.assertEqual(A.startup_summary(late_now, st, [], None),
                         "Good evening, sir. Xyrus online. You have 2 things today: Dentist at 3 pm and Gym at 7 pm.")

    def test_good_night(self):
        st = new_store()
        now = dt.datetime(2026, 9, 13, 23, 0)
        self.assertEqual(A.good_night(now, st), "Good night, sir. Nothing scheduled tomorrow.")
        st.add_task("Buy milk", D(2026, 9, 14))
        st.add_event("Standup", D(2026, 9, 10), "09:30", 0, repeat="daily")
        self.assertEqual(A.good_night(now, st), "Good night, sir. Tomorrow starts with Standup at 9:30 am.")
        self.assertEqual(A.good_night(now, st, BLANK), "Good night. Tomorrow starts with Standup at 9:30 am.")


class Memories(unittest.TestCase):
    def test_describe_memories(self):
        m = lambda *ts: [{"text": t} for t in ts]    # noqa: E731
        self.assertEqual(A.describe_memories([], 0), "Nothing yet, sir. Say 'remember that' and I'll keep it.")
        self.assertEqual(A.describe_memories(m("the wifi password is banana seven"), 1),
                         "You asked me to remember one thing: the wifi password is banana seven.")
        self.assertEqual(A.describe_memories(m("a", "b"), 2), "You asked me to remember two things. One: a. Two: b.")
        self.assertEqual(A.describe_memories(m("e", "d", "c"), 5),
                         "You asked me to remember five things. One: e. Two: d. Three: c. "
                         "There are two more. Say more to hear them.")
        self.assertEqual(A.describe_memories(m("b", "a"), 5, offset=3), "Four: b. Five: a.")
        self.assertEqual(A.describe_memories([], 5, offset=6), "That's everything, sir.")


class Contracts(unittest.TestCase):
    def test_cfg_get(self):
        self.assertEqual(A.cfg_get({"calendar": {"snooze_minutes": 5}}, "calendar.snooze_minutes", 10), 5)
        self.assertEqual(A.cfg_get({"calendar": {}}, "calendar.snooze_minutes", 10), 10)
        self.assertEqual(A.cfg_get(None, "x", 3), 3)

        class Cfg:                                    # xyrus.config.Config-style dotted get
            def get(self, key, default=None):
                return {"address_as": "", "calendar.snooze_minutes": 7}.get(key, default)
        self.assertEqual(A.sir(Cfg()), "")
        self.assertEqual(A.cfg_get(Cfg(), "calendar.snooze_minutes", 10), 7)

    def test_no_wake_words_in_any_summary(self):
        st = new_store()
        st.add_event("Dentist", D(2026, 9, 13), "15:00", 10)
        st.add_note(D(2026, 9, 13), "buy milk")
        texts = [A.describe_range(st, D(2026, 9, 13), D(2026, 9, 19), "this week", SUN)[0],
                 A.describe_next(st, SUN), A.describe_todos(st, SUN), A.briefing(st, SUN),
                 A.startup_summary(SUN, st, [], None), A.good_night(SUN, st), A.describe_memories([], 0),
                 A.describe_notes(st, D(2026, 9, 13), D(2026, 9, 13), SUN)[0]]
        for t in texts:
            for w in WAKE:
                self.assertNotIn(w, t.lower(), t)

    def test_g8_never_reads_the_clock(self):
        for mod in ("assistant.py", "reminders.py"):
            src = (Path(A.__file__).parent / mod).read_text("utf8")
            self.assertIsNone(re.search(r"datetime\.now\(|time\.time\(|\.today\(\)", src), mod)


if __name__ == "__main__":
    unittest.main()
