"""xyrus.store.CalendarStore (lifted v1 calendar_store.py) and xyrus.memory.MemoryStore.

Every store lives in a temp dir (XYRUS_DATA_DIR is pointed at one too); the real D:\\arc\\data\\calendar.json is
only ever READ (copied into a temp dir for the compatibility test), never written."""
from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

_TMP_ROOT = tempfile.mkdtemp(prefix="xyrus_t2_store_")
os.environ.setdefault("XYRUS_DATA_DIR", _TMP_ROOT)

from xyrus.memory import MemoryStore  # noqa: E402
from xyrus.store import CalendarStore, data_file  # noqa: E402

ARC = Path(__file__).resolve().parent.parent
REAL_FILE = ARC / "data" / "calendar.json"
D = dt.date
WED = dt.datetime(2026, 9, 16, 10, 0)


def tmp_path(name="calendar.json") -> Path:
    return Path(tempfile.mkdtemp(dir=_TMP_ROOT)) / name


def tearDownModule():
    shutil.rmtree(_TMP_ROOT, ignore_errors=True)


class V1Behaviour(unittest.TestCase):
    """v1 test_calendar.py 'store + spoken summaries' checks, re-expressed (store part)."""

    def setUp(self):
        self.st = CalendarStore(tmp_path())
        st = self.st
        self.milk = st.add_task("Buy milk", D(2026, 9, 16))
        self.report = st.add_task("Finish the report", D(2026, 9, 15))                     # overdue
        self.dent = st.add_event("Dentist", D(2026, 9, 18), dt.time(15, 0), 10)
        self.gym = st.add_event("Gym", D(2026, 9, 14), dt.time(18, 0), None, repeat="weekly")   # Mondays
        st.add_note(D(2026, 9, 16), "Bring the charger")

    def test_schema_matches_spec_6_2(self):
        it = self.dent
        self.assertRegex(it["id"], r"^it_[0-9a-f]{8}$")
        self.assertEqual({k for k in it}, {"id", "kind", "title", "date", "time", "all_day", "reminder_min", "repeat",
                                            "done", "source", "created", "fired"})
        self.assertEqual((it["kind"], it["date"], it["time"], it["all_day"], it["reminder_min"]),
                         ("event", "2026-09-18", "15:00", False, 10))
        self.assertEqual((self.milk["kind"], self.milk["time"], self.milk["all_day"], self.milk["repeat"]),
                         ("task", None, True, None))
        saved = json.loads(self.st.path.read_text("utf8"))
        self.assertEqual(saved["version"], 1)
        self.assertEqual(set(saved), {"version", "items", "notes", "state"})
        self.assertRegex(saved["notes"][0]["id"], r"^nt_[0-9a-f]{8}$")

    def test_fuzzy_find(self):
        self.assertEqual((self.st.find("the report", kinds=("task",)) or {}).get("id"), self.report["id"])
        self.assertEqual(self.st.find("the dentist")["id"], self.dent["id"])
        self.assertIsNone(self.st.find("the dentist", kinds=("task",)))
        self.assertIsNone(self.st.find("quantum physics lecture"))
        self.st.set_done(self.milk["id"])
        self.assertIsNone(self.st.find("buy milk", kinds=("task",), only_open=True))
        self.assertEqual(self.st.find("buy milk", kinds=("task",))["id"], self.milk["id"])

    def test_fuzzy_find_partial_title(self):
        appt = self.st.add_event("Dentist appointment", D(2026, 9, 20), "10:00")
        self.st.delete_item(self.dent["id"])
        self.assertEqual(self.st.find("the dentist")["id"], appt["id"])

    def test_weekly_repeat_and_counts(self):
        counts = self.st.counts_by_day(D(2026, 9, 14), D(2026, 9, 21))
        self.assertEqual(counts[D(2026, 9, 21)]["event"], 1)
        self.assertEqual(counts[D(2026, 9, 14)]["event"], 1)
        self.assertEqual(counts[D(2026, 9, 16)], {"event": 0, "task": 1, "task_open": 1, "note": 1})
        self.assertEqual(counts[D(2026, 9, 15)], {"event": 0, "task": 1, "task_open": 1, "note": 0})
        self.assertNotIn(D(2026, 9, 17), counts)
        self.st.set_done(self.milk["id"])
        self.assertEqual(self.st.counts_by_day(D(2026, 9, 16), D(2026, 9, 16))[D(2026, 9, 16)]["task_open"], 0)

    def test_reloads_from_disk(self):
        again = CalendarStore(self.st.path)
        self.assertEqual(len(again.data["items"]), 4)
        self.assertEqual(again.notes_on(D(2026, 9, 16))[0]["text"], "Bring the charger")

    def test_items_between_order(self):
        self.st.add_event("Standup", D(2026, 9, 18), "09:30")
        self.st.add_task("Pay rent", D(2026, 9, 18))
        got = [(d.isoformat(), it["title"]) for d, it in self.st.items_between(D(2026, 9, 18), D(2026, 9, 18))]
        self.assertEqual(got, [("2026-09-18", "Pay rent"), ("2026-09-18", "Standup"), ("2026-09-18", "Dentist")])

    def test_todos(self):
        self.assertEqual([t["title"] for t in self.st.open_tasks()], ["Finish the report", "Buy milk"])
        self.assertEqual([t["title"] for t in self.st.overdue_tasks(D(2026, 9, 16))], ["Finish the report"])
        self.assertEqual([t["title"] for t in self.st.open_tasks(until=D(2026, 9, 15))], ["Finish the report"])
        self.st.set_done(self.report["id"])
        self.assertEqual(self.st.overdue_tasks(D(2026, 9, 16)), [])
        self.assertTrue(self.st.get_item(self.report["id"])["done"])
        self.st.set_done(self.report["id"], False)
        self.assertFalse(self.st.get_item(self.report["id"])["done"])
        # finished to-dos drop out of the not-done listing
        self.st.set_done(self.milk["id"])
        self.assertNotIn(self.milk["id"], [i["id"] for _, i in self.st.items_between(D(2026, 9, 16), D(2026, 9, 16),
                                                                                       include_done=False)])

    def test_next_item(self):
        d, it = self.st.next_item(WED)
        self.assertEqual((d, it["title"]), (D(2026, 9, 16) + dt.timedelta(days=1), "Buy milk") if False else (D(2026, 9, 18), "Dentist"))
        d, it = self.st.next_item(dt.datetime(2026, 9, 18, 15, 0))
        self.assertEqual((d, it["title"]), (D(2026, 9, 21), "Gym"))
        self.assertIsNone(CalendarStore(tmp_path()).next_item(WED))

    def test_update_moving_rearms(self):
        self.st.mark_fired(self.dent["id"], "2026-09-18", dt.datetime(2026, 9, 18, 14, 50))
        self.assertTrue(self.st.get_item(self.dent["id"])["fired"])
        self.st.update_item(self.dent["id"], title="Dentist (Dr. Rahman)")
        self.assertTrue(self.st.get_item(self.dent["id"])["fired"], "a rename keeps the fired mark")
        it = self.st.update_item(self.dent["id"], time=dt.time(16, 0))
        self.assertEqual((it["time"], it["all_day"], it["fired"]), ("16:00", False, {}))
        it = self.st.update_item(self.dent["id"], time=None)
        self.assertTrue(it["all_day"])
        self.assertIsNone(self.st.update_item("it_missing", title="x"))

    def test_notes(self):
        st = self.st
        n2 = st.add_note("2026-09-16", "  call the bank ")
        self.assertEqual(n2["text"], "call the bank")
        self.assertEqual([n["text"] for n in st.notes_between(D(2026, 9, 16), D(2026, 9, 16))],
                         ["Bring the charger", "call the bank"])
        self.assertEqual(st.update_note(n2["id"], "call the bank at noon")["text"], "call the bank at noon")
        self.assertTrue(st.delete_note(n2["id"]))
        self.assertFalse(st.delete_note(n2["id"]))
        self.assertEqual(st.clear_notes(D(2026, 9, 16)), 1)
        self.assertEqual(st.clear_notes(D(2026, 9, 16)), 0)

    def test_version_bumps_and_atomic_write(self):
        v = self.st.version
        self.st.add_task("Another", D(2026, 9, 17))
        self.assertEqual(self.st.version, v + 1)
        self.assertFalse(self.st.path.with_suffix(".tmp").exists())
        self.assertFalse(self.st.delete_item("it_nope"))
        self.assertEqual(self.st.version, v + 1, "no-op delete does not save")

    def test_state(self):
        self.st.set_state("last_brief", "2026-09-16")
        self.assertEqual(CalendarStore(self.st.path).get_state("last_brief"), "2026-09-16")
        self.assertEqual(self.st.get_state("missing", 7), 7)


class Undo(unittest.TestCase):
    def test_voice_adds_only_newest_first(self):
        st = CalendarStore(tmp_path())
        st.add_task("UI thing", D(2026, 9, 16), source="ui")
        st.add_event("Dentist", D(2026, 9, 18), "15:00", 10, source="voice")
        st.add_note(D(2026, 9, 18), "the rent is due", source="voice")
        self.assertEqual(st.undo_last(), "the rent is due")
        self.assertEqual(st.notes_on(D(2026, 9, 18)), [])
        self.assertEqual(st.undo_last(), "Dentist")
        self.assertIsNone(st.undo_last(), "UI adds are never undone")
        self.assertEqual([i["title"] for i in st.data["items"]], ["UI thing"])

    def test_skips_things_already_deleted(self):
        st = CalendarStore(tmp_path())
        a = st.add_task("First", D(2026, 9, 16), source="voice")
        b = st.add_task("Second", D(2026, 9, 16), source="voice")
        st.delete_item(b["id"])
        self.assertEqual(st.undo_last(), "First")
        self.assertIsNone(st.get_item(a["id"]))

    def test_session_only(self):
        st = CalendarStore(tmp_path())
        st.add_task("Voice", D(2026, 9, 16), source="voice")
        self.assertIsNone(CalendarStore(st.path).undo_last(), "a new session has nothing to undo")


class V2Additions(unittest.TestCase):
    def test_acknowledge_persists(self):
        st = CalendarStore(tmp_path())
        it = st.add_event("Dentist", D(2026, 9, 14), "15:00", 10)
        self.assertFalse(st.is_acked(it["id"], "2026-09-14"))
        st.acknowledge(it["id"], D(2026, 9, 14))
        st.acknowledge(it["id"], "2026-09-14")               # idempotent
        self.assertTrue(st.is_acked(it["id"], "2026-09-14"))
        again = CalendarStore(st.path)
        self.assertEqual(again.get_state("acked"), {it["id"]: ["2026-09-14"]})

    def test_prune(self):
        st = CalendarStore(tmp_path())
        daily = st.add_event("Standup", D(2026, 5, 1), "09:30", 0, repeat="daily")
        once = st.add_event("Old dentist", D(2026, 5, 2), "15:00", 10)
        gone = st.add_event("Deleted", D(2026, 9, 1), "15:00", 10)
        for day in ("2026-05-01", "2026-06-01", "2026-07-20", "2026-09-12"):
            st.mark_fired(daily["id"], day, dt.datetime.fromisoformat(day + "T09:30:00"))
        st.mark_fired(once["id"], "2026-05-02", dt.datetime(2026, 5, 2, 14, 50))
        st.acknowledge(daily["id"], "2026-05-01")
        st.acknowledge(daily["id"], "2026-09-12")
        st.acknowledge(gone["id"], "2026-09-01")
        st.set_state("snoozed", [{"id": gone["id"], "occ": "2026-09-01", "at": "2026-09-01T15:10:00"}])
        st.delete_item(gone["id"])
        removed = st.prune(today=D(2026, 9, 13))
        self.assertEqual(removed, 2 + 1 + 1 + 1)      # 2 old fired marks, 1 old ack, 1 ack of a deleted item, 1 snooze
        self.assertEqual(sorted(st.get_item(daily["id"])["fired"]), ["2026-07-20", "2026-09-12"])
        self.assertIn("2026-05-02", st.get_item(once["id"])["fired"], "one-off items keep their mark")
        self.assertEqual(st.get_state("acked"), {daily["id"]: ["2026-09-12"]})
        self.assertEqual(st.get_state("snoozed"), [])
        self.assertEqual(CalendarStore(st.path).get_state("pruned"), "2026-09-13")
        v = st.version
        self.assertEqual(st.prune(today=D(2026, 9, 13)), 0)
        self.assertEqual(st.version, v, "a second prune the same day writes nothing")

    def test_default_path_is_data_dir(self):
        p = data_file()
        self.assertEqual(p.name, "calendar.json")
        try:
            from xyrus import paths
        except ImportError:
            self.assertEqual(p.parent, Path(os.environ["XYRUS_DATA_DIR"]))
        else:
            self.assertEqual(p.parent, Path(paths.data_dir() if hasattr(paths, "data_dir") else paths.DATA_DIR))
        self.assertEqual(CalendarStore().path, p)                # constructing never writes

    def test_corrupt_file_is_kept_and_store_starts_clean(self):
        path = tmp_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", "utf8")
        st = CalendarStore(path)
        self.assertEqual(st.data["items"], [])
        self.assertTrue(path.with_suffix(".corrupt.json").exists())


class V1FileCompatibility(unittest.TestCase):
    """v2 must open v1's data/calendar.json unchanged (D14) and v1 must still read what v2 writes."""

    def test_real_file_copy_opens_unchanged(self):
        if not REAL_FILE.exists():
            self.skipTest("no real data/calendar.json on this PC")
        path = tmp_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REAL_FILE, path)                         # read-only use of the real file
        original = json.loads(path.read_text("utf8"))
        st = CalendarStore(path)
        for key in ("items", "notes", "state"):
            self.assertEqual(st.data[key], original.get(key, st.data[key]))
        st.add_note(D(2026, 9, 20), "v2 was here")               # a v2 write keeps every v1 key
        saved = json.loads(path.read_text("utf8"))
        self.assertEqual(saved["items"], original.get("items", []))
        self.assertEqual({k: v for k, v in saved["state"].items()}, original.get("state", {}))

    def _v1_module(self):
        sys.path.insert(0, str(ARC))
        try:
            import calendar_store as v1cs                          # v1 module, imported read-only
        except Exception as e:                                     # pragma: no cover - v1 moved to legacy/v1
            legacy = ARC / "legacy" / "v1"
            if not (legacy / "calendar_store.py").exists():
                self.skipTest(f"v1 calendar_store not importable: {e}")
            sys.path.insert(0, str(legacy))
            import calendar_store as v1cs
        return v1cs

    def test_fixture_written_by_v1_module(self):
        v1cs = self._v1_module()
        path = tmp_path()
        old = v1cs.CalendarStore(path)
        ev = old.add_event("Dentist", D(2026, 9, 14), dt.time(15, 0), 10, source="voice")
        old.add_event("Standup", D(2026, 9, 10), "09:30", 0, repeat="daily")
        old.add_task("Buy milk", D(2026, 9, 13))
        old.add_note(D(2026, 9, 13), "buy milk")
        old.set_state("briefed", "2026-09-13")
        v1cs.due_reminders(old, dt.datetime(2026, 9, 14, 14, 50, 5))     # v1 marks fired
        raw = json.loads(path.read_text("utf8"))

        st = CalendarStore(path)
        self.assertEqual(st.data["items"], raw["items"])
        self.assertEqual(st.data["notes"], raw["notes"])
        self.assertEqual(st.get_state("briefed"), "2026-09-13")
        self.assertTrue(st.get_item(ev["id"])["fired"]["2026-09-14"].startswith("fired@"))
        from xyrus import reminders
        self.assertEqual(reminders.due_alerts(st, dt.datetime(2026, 9, 14, 14, 50, 30)), [],
                         "what v1 already fired is not fired again by v2")
        # and v1 reads what v2 writes
        st.add_task("Call the bank", D(2026, 9, 15), dt.time(11, 0), 0, source="voice")
        back = v1cs.CalendarStore(path)
        self.assertEqual(back.data["items"], st.data["items"])
        self.assertEqual(v1cs.describe_todos(back, dt.datetime(2026, 9, 14, 10, 0)).split(".")[0],
                         "You have 2 to-dos, sir")

    def test_prototype_events_list_is_migrated(self):
        path = tmp_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"version": 1, "events": [
            {"id": "ev_12345678", "title": "Dentist", "date": "2026-09-14", "time": "15:00", "all_day": False,
             "reminder_min": 30, "repeat": None, "source": "voice", "created": "2026-09-13T10:00:00", "fired": {}}],
            "notes": [], "settings": {}}), "utf8")
        st = CalendarStore(path)
        self.assertEqual(len(st.data["items"]), 1)
        it = st.data["items"][0]
        self.assertEqual((it["kind"], it["done"], it["title"]), ("event", False, "Dentist"))
        self.assertEqual(st.items_between(D(2026, 9, 14), D(2026, 9, 14))[0][1]["id"], "ev_12345678")


class Memory(unittest.TestCase):
    def test_add_newest_count_delete(self):
        path = tmp_path("memory.json")
        m = MemoryStore(path)
        a = m.add("the wifi password is banana seven", now=dt.datetime(2026, 9, 13, 14, 0))
        self.assertRegex(a["id"], r"^m_[0-9a-f]{8}$")
        self.assertEqual(a, {"id": a["id"], "text": "the wifi password is banana seven",
                             "created": "2026-09-13T14:00:00", "source": "voice"})
        for t in ("the car is on level three", "mom's birthday is in june", "the spare key is under the pot"):
            m.add(t)
        self.assertEqual(m.count(), 4)
        self.assertEqual([x["text"] for x in m.newest()],
                         ["the spare key is under the pot", "mom's birthday is in june", "the car is on level three"])
        self.assertEqual([x["text"] for x in m.newest(offset=3)], ["the wifi password is banana seven"])
        self.assertEqual(json.loads(path.read_text("utf8"))[0]["text"], "the wifi password is banana seven")
        self.assertEqual(MemoryStore(path).count(), 4)
        self.assertEqual(m.delete_last()["text"], "the spare key is under the pot")
        self.assertTrue(m.delete(a["id"]))
        self.assertFalse(m.delete(a["id"]))
        v = m.version
        self.assertEqual(m.clear(), 2)
        self.assertEqual(m.version, v + 1)
        self.assertEqual(m.clear(), 0)
        self.assertIsNone(m.delete_last())
        self.assertFalse(path.with_suffix(".tmp").exists())

    def test_corrupt_memory_file(self):
        path = tmp_path("memory.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"not": "a list"}', "utf8")
        self.assertEqual(MemoryStore(path).count(), 0)
        self.assertTrue(path.with_suffix(".corrupt.json").exists())


if __name__ == "__main__":
    unittest.main()
