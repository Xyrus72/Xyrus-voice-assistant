"""Calendar, reminder, timer and assistant flows through the REAL engine (SPEC §7.3): make_test_engine (T1) +
real xyrus.dates + a temp CalendarStore / MemoryStore, FakeClock at Sun 2026-09-13 10:30 unless stated.
Includes v1 D:\\arc\\test_calendar.py's voice expectations re-expressed (v1 wording wins, D14) and the nine
dialogue_proto.py scenarios with exact replies."""
from __future__ import annotations

import datetime as dt
import shutil
import tempfile
import unittest
from pathlib import Path

try:
    from xyrus.testing import drain_ui, make_test_engine
    import xyrus.engine  # noqa: F401
    import xyrus.dialogue  # noqa: F401
    SKIP = None
except Exception as e:                      # pragma: no cover - only while T1's engine hasn't landed
    SKIP = f"T1 engine / make_test_engine not importable yet: {e!r}"

from xyrus import assistant as A
from xyrus import dates as W
from xyrus import persona as P
from xyrus import replies as RP

D = dt.date
T = dt.datetime
SUN = T(2026, 9, 13, 10, 30)
WED = T(2026, 9, 16, 10, 0)


@unittest.skipIf(SKIP, SKIP or "")
class EngineCase(unittest.TestCase):
    NOW = SUN

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="xyrus_t2_flow_"))
        self.e, self.ns = make_test_engine(now=self.NOW, tmpdir=self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    @property
    def store(self):
        return self.ns.store

    def say(self, text: str, source: str = "typed", free: str | None = None) -> list[str]:
        n = len(self.ns.speaker.said)
        self.e.handle(text, source, free_text=free)
        return self.ns.speaker.said[n:]

    def mic(self, text: str, free: str | None = None) -> list[str]:
        return self.say(text, "mic", free)

    def at(self, when: dt.datetime) -> list[str]:
        n = len(self.ns.speaker.said)
        self.ns.clock.set(when)
        self.e.tick()
        return self.ns.speaker.said[n:]

    def advance(self, seconds: float) -> list[str]:
        n = len(self.ns.speaker.said)
        self.ns.clock.advance(seconds)
        self.e.tick()
        return self.ns.speaker.said[n:]

    def item(self, title: str):
        return next((i for i in self.store.data["items"] if i["title"].lower() == title.lower()), None)

    def cmd_names(self) -> list[str]:
        return [ev.text.split()[0] for ev in self.e.events if ev.kind == "cmd"]


# ----------------------------------------------------------------------------- v1 test_calendar.py
class V1VoiceCommands(EngineCase):
    """v1 test_calendar.py 'voice commands (typed)' + 'reminders', same order, v1 wording."""
    NOW = WED

    def test_v1_sequence(self):
        self.assertEqual(self.say("cyrus add buy milk to my to do list"),
                         ["Added to your to-do list for today, sir: Buy milk."])
        it = self.item("buy milk")
        self.assertEqual((it["kind"], it["date"], it["time"]), ("task", "2026-09-16", None))

        self.say("cyrus add call the bank to my to do list tomorrow")
        self.assertEqual(self.item("call the bank")["date"], "2026-09-17")

        self.assertEqual(self.say("cyrus remind me to call mom at five"), ["I'll remind you today at 5 pm: Call mom."])
        it = self.item("call mom")
        self.assertEqual((it["kind"], it["time"], it["reminder_min"]), ("task", "17:00", 0))

        self.assertEqual(self.say("cyrus add dentist appointment on friday at three pm"),
                         ["Added to your calendar: Dentist appointment, Friday at 3 pm. I'll remind you ten minutes before."])
        it = self.item("dentist appointment")
        self.assertEqual((it["kind"], it["date"], it["time"], it["reminder_min"]), ("event", "2026-09-18", "15:00", 10))

        self.say("cyrus schedule gym every monday at six pm")
        it = self.item("gym")
        self.assertEqual((it["repeat"], it["time"]), ("weekly", "18:00"))

        self.assertEqual(self.say("cyrus add a note for friday that the rent is due"),
                         ["Noted for Friday: The rent is due."])
        self.assertEqual([n["text"] for n in self.store.notes_on(D(2026, 9, 18))], ["The rent is due"])

        (said,) = self.say("cyrus what do i have today")
        self.assertIn("Buy milk", said)
        self.assertIn("Call mom at 5 pm", said)
        (said,) = self.say("cyrus what do i have in the next three days")
        self.assertTrue(said.startswith("In the next 3 days you have"), said)
        self.assertIn("Friday: Dentist appointment at 3 pm", said)
        (said,) = self.say("cyrus what do i have this week")
        self.assertTrue(said.startswith("This week you have"), said)
        self.assertEqual(self.say("cyrus what's next"), ["Next up: Call mom at 5 pm, today."])
        (said,) = self.say("cyrus read my to do list")
        self.assertTrue(said.startswith("You have 3 to-dos, sir."), said)
        self.assertEqual(self.say("cyrus mark buy milk as done"), ["Done: Buy milk. Nice work, sir."])
        self.assertTrue(self.item("buy milk")["done"])

        # v2: delete asks first (§3.9); the v1 reply follows the yes
        self.assertEqual(self.say("cyrus delete the dentist appointment"),
                         ["Delete Dentist appointment, Friday at 3 pm, sir?"])
        (said,) = self.say("yes")
        self.assertTrue(said.startswith("Deleted Dentist appointment"), said)
        self.assertIsNone(self.item("dentist appointment"))

        # v1 "undo" -> v2 "undo that" (bare "undo" is Ctrl+Z, §3.5): removes the last voice add, the note
        (said,) = self.say("cyrus undo that")
        self.assertTrue(said.startswith("Removed"), said)
        self.assertEqual(self.store.notes_on(D(2026, 9, 18)), [])

        self.say("cyrus next")
        self.assertIn(("next",), self.ns.actions.called("media"))
        self.assertNotIn("whats_next", self.cmd_names()[-1:])

        # reminders: nothing early, speech + toast + screen on at 5 pm, never twice
        self.assertEqual(self.at(T(2026, 9, 16, 16, 59, 30)), [])
        self.ns.actions.reset()
        self.assertEqual(self.at(T(2026, 9, 16, 17, 0, 10)), ["Sir, reminder: Call mom at 5 pm."])
        self.assertIn("Sir, reminder: Call mom at 5 pm.", self.ns.speaker.forced())
        self.assertIn(("Xyrus reminder", "Sir, reminder: Call mom at 5 pm."), self.ns.notifier.notes)
        self.assertTrue(self.ns.actions.called("screen_on"))
        self.assertEqual(self.at(T(2026, 9, 16, 17, 0, 40)), [])

    def test_morning_briefing_once(self):
        self.store.add_task("Water the plants", D(2026, 9, 17))
        self.assertEqual(self.at(T(2026, 9, 17, 7, 30)), [])
        # SPEC-AMBIGUITY: v1 briefed from 08:00; v2's calendar.morning_brief_time default is 08:30 (§6.1)
        self.assertEqual(self.at(T(2026, 9, 17, 8, 31)), ["Good morning, sir. Today you have 1 to-do: Water the plants."])
        self.assertEqual(self.at(T(2026, 9, 17, 8, 40)), [])


# ----------------------------------------------------------------------------- §7.3 flows
class Adds(EngineCase):
    def test_one_shot_reminder(self):
        said = self.mic("cyrus remind me at five to call mom", free="cyrus remind me at five to call mom")
        self.assertEqual(said, ["I'll remind you today at 5 pm: Call mom."])       # v1 wording (D14)
        it = self.item("call mom")
        self.assertEqual((it["date"], it["time"], it["reminder_min"], it["source"]), ("2026-09-13", "17:00", 0, "voice"))

    def test_relative_reminder(self):
        self.say("cyrus remind me in twenty minutes to check the oven")
        self.assertEqual(self.item("check the oven")["time"], "10:50")

    def test_free_text_title_with_misheard_grammar(self):
        # the command grammar can't spell titles; the full-vocabulary re-decode carries them (§5.3)
        said = self.mic("cyrus add a to do [unk] [unk] tomorrow", free="cyrus add a to do buy milk tomorrow")
        self.assertEqual(said, ["Added to your to-do list for tomorrow, sir: Buy milk."])
        it = self.item("buy milk")
        self.assertEqual((it["kind"], it["date"], it["reminder_min"]), ("task", "2026-09-14", None))

    def test_note_dialogue_for_tomorrow(self):
        self.assertEqual(self.mic("cyrus add a note for tomorrow"), ["What's the note?"])
        self.assertEqual(self.mic("bring the charger", free="bring the charger"), ["Noted for tomorrow: Bring the charger."])
        self.assertEqual([n["text"] for n in self.store.notes_on(D(2026, 9, 14))], ["Bring the charger"])
        self.assertEqual(self.say("cyrus read my notes for tomorrow"), ["One note for tomorrow, sir: Bring the charger."])
        self.assertEqual(self.say("cyrus read my notes for friday"), ["No notes for Friday, sir."])

    def test_note_four_tomorrow_mishearing(self):
        self.say("cyrus add note four tomorrow bring the charger")
        self.assertEqual([n["text"] for n in self.store.notes_on(D(2026, 9, 14))], ["Bring the charger"])

    def test_duplicate_and_past(self):
        self.say("cyrus add dentist tomorrow at three pm")
        self.assertEqual(self.say("cyrus add dentist tomorrow at three pm"), ["That's already on your calendar, sir."])
        self.assertEqual(len([i for i in self.store.data["items"] if i["title"] == "Dentist"]), 1)
        self.assertEqual(self.say("cyrus add lunch yesterday at one pm"), ["That's in the past, sir. Add it anyway?"])
        self.assertIsNone(self.item("lunch"))
        self.say("yes")
        self.assertEqual(self.item("lunch")["date"], "2026-09-12")

    def test_event_without_a_date_asks_when(self):
        self.assertEqual(self.say("cyrus add dentist appointment"), ["When?"])
        self.assertEqual(self.say("tomorrow at three pm"), ["Dentist appointment, tomorrow at 3 PM. Correct?"])
        self.assertEqual(self.say("yes"), ["Done, sir. I'll remind you ten minutes before."])
        self.assertEqual(self.item("dentist appointment")["time"], "15:00")

    def test_undo_that_without_the_wake_word(self):
        self.say("cyrus add buy milk to my to do list")
        self.assertEqual(self.mic("undo that"), ["Removed: Buy milk."])
        self.assertIsNone(self.item("buy milk"))
        self.say("cyrus add call the bank to my to do list")
        self.advance(16)
        self.assertEqual(self.mic("undo that"), [], "the window is 15 s")
        self.assertIsNotNone(self.item("call the bank"))
        self.assertEqual(self.say("cyrus undo that"), ["Removed: Call the bank."])
        self.assertEqual(self.say("cyrus undo that"), ["There's nothing to undo, sir."])


class Dialogues(EngineCase):
    """dialogue_proto.py's nine scenarios against the real engine, dates and store (14:00)."""
    NOW = T(2026, 9, 13, 14, 0)

    def test_1_full_flow(self):
        self.assertEqual(self.mic("cyrus add an event"), ["What's the event?"])
        view = self.e.snapshot().dialogue
        self.assertEqual((view.state, view.listening, view.seconds_left), ("title", "free", 20),
                         "construction order: the deadline is armed after the first question")
        self.assertEqual(self.mic("dentist appointment", free="dentist appointment"), ["When?"])
        self.assertEqual(self.mic("tomorrow"), ["What time?"])
        self.assertEqual(self.mic("at three pm"), ["Dentist appointment, tomorrow at 3 PM. Correct?"])
        self.assertEqual(self.mic("yes"), ["Done, sir. I'll remind you ten minutes before."])
        it = self.item("dentist appointment")
        self.assertEqual((it["kind"], it["date"], it["time"], it["reminder_min"]), ("event", "2026-09-14", "15:00", 10))

    def test_2_prefilled_date_and_time(self):
        self.assertEqual(self.mic("cyrus remind me tomorrow at three pm"), ["What should I remind you about?"])
        self.assertEqual(self.mic("call mom", free="call mom"), ["Call mom, tomorrow at 3 PM. Correct?"])
        self.assertEqual(self.mic("correct"), ["I'll remind you tomorrow at 3 pm: Call mom."])
        it = self.item("call mom")
        self.assertEqual((it["date"], it["time"], it["reminder_min"]), ("2026-09-14", "15:00", 0))

    def test_3_title_carries_the_date(self):
        self.mic("cyrus add an event")
        self.assertEqual(self.mic("team meeting tomorrow at nine am", free="team meeting tomorrow at nine am"),
                         ["Team meeting, tomorrow at 9 AM. Correct?"])
        self.mic("yes")
        self.assertEqual(self.item("team meeting")["time"], "09:00")

    def test_4_bare_hour_is_seven_pm_today(self):
        self.mic("cyrus remind me")
        self.assertEqual(self.mic("take the bins out", free="take the bins out"), ["When?"])
        self.assertEqual(self.mic("at seven"), ["Take the bins out, today at 7 PM. Correct?"])
        self.assertEqual(self.mic("yes"), ["I'll remind you today at 7 pm: Take the bins out."])
        self.assertEqual((self.item("take the bins out")["date"], self.item("take the bins out")["time"]),
                         ("2026-09-13", "19:00"))

    def test_5_no_then_fix_the_time(self):
        self.mic("cyrus add an event")
        self.mic("dentist", free="dentist")
        self.assertEqual(self.mic("tomorrow at three pm"), ["Dentist, tomorrow at 3 PM. Correct?"])
        self.assertEqual(self.mic("no"), ["What should I change: the title, the date, or the time?"])
        self.assertEqual(self.mic("the time"), ["What time?"])
        self.assertEqual(self.mic("at nine am"), ["Dentist, tomorrow at 9 AM. Correct?"])
        self.mic("yes")
        self.assertEqual(self.item("dentist")["time"], "09:00")

    def test_6_cancel_mid_dialogue(self):
        self.mic("cyrus add an event")
        self.mic("dentist", free="dentist")
        (said,) = self.mic("cancel")
        self.assertIn(said, [P.render(c, self.ns.config) for c in RP.CANCELLED])
        self.assertIsNone(self.e.dialogue)
        self.assertEqual(self.store.data["items"], [])

    def test_7_noise_swallowed_garbage_costs_an_attempt(self):
        self.mic("cyrus add an event")
        self.mic("dentist", free="dentist")
        self.assertEqual(self.mic("how"), [])
        self.assertEqual(self.mic("half"), [])
        self.assertEqual(self.mic("play minute you"), ["Sorry, I didn't catch that. When?"])
        self.assertEqual(self.mic("this is a launch"),
                         ["Sorry, sir, I'm not getting it. You can type it in the window, or try again later."])
        self.assertIsNone(self.e.dialogue)
        self.assertEqual(self.store.data["items"], [])

    def test_8_nudge_then_leave(self):
        self.mic("cyrus add an event")
        self.assertEqual(self.advance(19), [])
        self.assertEqual(self.advance(2), ["Still there, sir? What's the event?"])
        self.assertEqual(self.advance(21), ["I'll leave it for now, sir. Say 'add an event' when you're ready."])
        self.assertIsNone(self.e.dialogue)

    def test_9_all_day(self):
        self.mic("cyrus add an event")
        self.mic("mothers birthday", free="mothers birthday")
        self.assertEqual(self.mic("tomorrow"), ["What time?"])
        (q,) = self.mic("all day")
        self.assertTrue(q.startswith("Mothers birthday, tomorrow") and q.endswith("Correct?"), q)   # T1's summary
        self.assertEqual(self.mic("yes"), ["Done, sir. It's on your calendar."])
        it = self.item("mothers birthday")
        self.assertEqual((it["all_day"], it["time"], it["reminder_min"]), (True, None, None))


class Reading(EngineCase):
    def seed(self):
        st = self.store
        st.add_event("Standup", D(2026, 9, 13), "12:30", 0)
        st.add_event("Dentist", D(2026, 9, 14), "15:00", 10)
        st.add_event("Gym", D(2026, 9, 14), "18:00")
        st.add_task("Pay rent", D(2026, 9, 18))
        st.add_event("Team meeting", D(2026, 9, 22), "10:00", 10)
        st.add_note(D(2026, 9, 13), "buy milk")

    def test_whats_on_today(self):
        self.seed()
        self.assertEqual(self.say("cyrus what's on today"),
                         ["Today you have 1 event, sir: Standup at 12:30 pm. Notes: buy milk."])

    def test_span_questions(self):
        self.seed()
        for phrase, span in (("cyrus what do i have these three days", "these three days"),
                             ("cyrus what do i have the next ten days", "the next ten days"),
                             ("cyrus what do i have on friday", "on friday"),
                             ("cyrus what do i have for the rest of the week", "for the rest of the week"),
                             ("cyrus what do i have next week", "next week")):
            start, end, label = W.parse_range(span, SUN)
            want, _ = A.describe_range(self.store, start, end, label, SUN, self.ns.config)
            with self.subTest(phrase=phrase):
                self.assertEqual(self.say(phrase), [want])
        self.assertEqual(self.say("cyrus what do i have the next ten days")[0][:44],
                         "In the next 10 days you have 4 events and 1 ")

    def test_whats_coming_up_is_the_next_seven_days(self):
        self.seed()
        want, _ = A.describe_range(self.store, D(2026, 9, 13), D(2026, 9, 19), "in the next 7 days", SUN, self.ns.config)
        self.assertEqual(self.say("cyrus what's coming up"), [want])

    def test_overflow_shows_the_calendar(self):
        for h in range(8, 22):
            self.store.add_event(f"Slot {h}", D(2026, 9, 14), f"{h:02d}:00")
        self.say("cyrus what do i have tomorrow")
        self.assertIn("show:Calendar:2026-09-14", drain_ui(self.ns.ui_queue))

    def test_whats_next_and_after_that(self):
        self.seed()
        self.assertEqual(self.say("cyrus what's next"), ["Next up: Standup at 12:30 pm, today."])
        self.assertEqual(self.mic("and after that"), ["Then Dentist at 3 pm, tomorrow."])
        self.assertEqual(self.mic("what's after that"), ["Then Gym at 6 pm, tomorrow."])
        self.assertEqual(self.say("cyrus next one"), ["Then Pay rent, Friday."])

    def test_whats_next_mentions_an_unacknowledged_reminder(self):
        self.store.add_task("Call the bank", D(2026, 9, 13), "10:40", 0)
        self.store.add_event("Gym", D(2026, 9, 13), "19:00")
        self.at(T(2026, 9, 13, 10, 40, 5))
        self.ns.clock.set(T(2026, 9, 13, 11, 0))
        self.assertEqual(self.say("cyrus what's next"),
                         ["You still have a reminder: Call the bank, from 20 minutes ago. Next up: Gym at 7 pm, today."])

    def test_todo_list_and_tick_off(self):
        self.say("cyrus add a to do buy milk tomorrow")
        self.store.add_task("Finish the report", D(2026, 9, 12))
        self.assertEqual(self.say("cyrus what's on my to do list"),
                         ["You have 2 to-dos, sir. Overdue: Finish the report. Later: Buy milk tomorrow."])
        self.assertEqual(self.say("cyrus mark buy milk as done"), ["Done: Buy milk. Nice work, sir."])
        self.assertTrue(self.item("buy milk")["done"])
        self.assertEqual(self.say("cyrus i finished the report"), ["Done: Finish the report. Nice work, sir."])
        self.assertEqual(self.say("cyrus what are my to dos"), ["Your to-do list is empty, sir."])
        self.assertEqual(self.say("cyrus mark buy milk as not done"), ["Okay, sir. Buy milk is back on your list."])
        self.assertEqual(self.say("cyrus tick off the dishes"), ["I couldn't find the dishes on your to-do list, sir."])

    def test_show_calendar(self):
        self.assertEqual(self.say("cyrus show my calendar"), ["Here you are, sir."])
        self.assertEqual(drain_ui(self.ns.ui_queue), ["show:Calendar"])


class Changing(EngineCase):
    def test_delete_asks_first(self):
        self.store.add_event("Dentist", D(2026, 9, 14), "15:00", 10)
        self.assertEqual(self.say("cyrus delete the dentist event"), ["Delete Dentist, tomorrow at 3 pm, sir?"])
        self.assertEqual(self.say("yes"), ["Deleted Dentist, tomorrow."])
        self.assertIsNone(self.item("dentist"))

    def test_cancel_the_x_event_is_not_cancel_shutdown(self):
        self.store.add_event("Dentist", D(2026, 9, 14), "15:00", 10)
        self.assertEqual(self.mic("cyrus cancel the dentist event", free="cyrus cancel the dentist event"),
                         ["Delete Dentist, tomorrow at 3 pm, sir?"])
        self.assertEqual(self.mic("no")[0], P.render(RP.CANCELLED[0], self.ns.config)
                         if self.ns.speaker.said[-1] == P.render(RP.CANCELLED[0], self.ns.config)
                         else self.ns.speaker.said[-1])
        self.assertIsNotNone(self.item("dentist"))
        self.assertEqual(self.ns.actions.called("abort_shutdown"), [])

    def test_several_matches_pick_one(self):
        self.store.add_event("Dentist", D(2026, 9, 14), "15:00", 10)
        self.store.add_event("Dentist", D(2026, 9, 15), "17:00", 10)
        self.assertEqual(self.say("cyrus delete dentist"),
                         ["Which one, sir? First: Dentist at 3 pm, tomorrow. Second: Dentist at 5 pm, Tuesday."])
        self.assertEqual(self.say("the second"), ["Delete Dentist, Tuesday at 5 pm, sir?"])
        self.say("yes")
        self.assertEqual([i["date"] for i in self.store.data["items"]], ["2026-09-14"])

    def test_recurring_delete_warns(self):
        self.store.add_event("Gym", D(2026, 9, 14), "18:00", repeat="weekly")
        self.assertEqual(self.say("cyrus delete gym"), ["Delete Gym every Monday? This removes all of them."])
        self.say("yes")
        self.assertEqual(self.store.data["items"], [])

    def test_delete_tomorrows_event(self):
        self.store.add_event("Dentist", D(2026, 9, 14), "15:00", 10)
        self.assertEqual(self.say("cyrus delete tomorrow's event"), ["Delete Dentist, tomorrow at 3 pm, sir?"])

    def test_delete_nothing_found(self):
        self.assertEqual(self.say("cyrus delete the dentist event"), ["I couldn't find the dentist on your calendar, sir."])   # v1 wording

    def test_clear_notes(self):
        for t in ("buy milk", "call the bank", "water the plants"):
            self.store.add_note(D(2026, 9, 13), t)
        self.assertEqual(self.say("cyrus clear today's notes"), ["Clear three notes for today, sir? Yes or no."])
        self.assertEqual(self.say("yes"), ["Cleared."])
        self.assertEqual(self.store.notes_on(D(2026, 9, 13)), [])
        self.assertEqual(self.say("cyrus clear my notes for tomorrow"), ["No notes for tomorrow, sir."])


class Alerts(EngineCase):
    def test_lead_and_at_time_alerts(self):
        self.store.add_event("Dentist", D(2026, 9, 13), "13:00", 10)
        self.store.add_task("Call mom", D(2026, 9, 13), "13:30", 0)
        self.assertEqual(self.at(T(2026, 9, 13, 12, 49, 50)), [])
        self.assertEqual(self.at(T(2026, 9, 13, 12, 50, 5)), ["Sir, Dentist is at 1 pm, in 10 minutes."])
        self.assertEqual(self.at(T(2026, 9, 13, 13, 30, 5)), ["Sir, reminder: Call mom at 1:30 pm."])
        forced = self.ns.speaker.forced()
        self.assertIn("Sir, Dentist is at 1 pm, in 10 minutes.", forced)
        self.assertIn("Sir, reminder: Call mom at 1:30 pm.", forced)
        self.assertEqual([b for _, b in self.ns.notifier.notes],
                         ["Sir, Dentist is at 1 pm, in 10 minutes.", "Sir, reminder: Call mom at 1:30 pm."])
        self.assertEqual(len(self.ns.actions.called("screen_on")), 2)
        self.assertEqual(self.at(T(2026, 9, 13, 13, 31)), [])

    def test_two_alerts_in_the_same_tick_both_speak(self):
        self.store.add_task("Pills", D(2026, 9, 13), "13:00", 0)
        self.store.add_task("Call mom", D(2026, 9, 13), "13:00", 0)
        self.assertEqual(sorted(self.at(T(2026, 9, 13, 13, 0, 5))),
                         ["Sir, reminder: Call mom at 1 pm.", "Sir, reminder: Pills at 1 pm."])

    def test_alert_waits_for_a_dialogue(self):
        self.store.add_task("Call mom", D(2026, 9, 13), "13:00", 0)
        self.ns.clock.set(T(2026, 9, 13, 12, 59, 50))
        self.mic("cyrus add an event")
        self.assertEqual(self.at(T(2026, 9, 13, 13, 0, 5)), [])
        said = self.mic("cancel")
        self.assertIn("Sir, reminder: Call mom at 1 pm.", said)        # flushed when the dialogue ends

    def test_snooze_okay_and_remind_again(self):
        self.store.add_task("Call mom", D(2026, 9, 13), "13:00", 0)
        self.at(T(2026, 9, 13, 13, 0, 5))
        self.assertEqual(self.mic("snooze"), ["I'll remind you again in ten minutes, sir."])
        self.assertEqual(self.at(T(2026, 9, 13, 13, 5)), [])
        self.assertEqual(self.at(T(2026, 9, 13, 13, 10, 10)), ["Sir, reminder: Call mom at 1 pm."])
        self.assertEqual(self.mic("remind me again in five minutes"), ["I'll remind you again in five minutes, sir."])
        self.assertEqual(self.at(T(2026, 9, 13, 13, 15, 15)), ["Sir, reminder: Call mom at 1 pm."])
        self.assertEqual(self.mic("okay"), ["Noted, sir."])
        self.assertTrue(self.store.is_acked(self.item("call mom")["id"], "2026-09-13"))
        self.assertEqual(self.at(T(2026, 9, 13, 13, 40)), [])

    def test_missed_after_a_three_hour_jump_spoken_once(self):
        self.store.add_event("Standup", D(2026, 9, 13), "12:15", 0)
        self.store.set_state("last_brief", "2026-09-13")       # isolate from the 08:30 auto brief (engine, T1)
        self.e.startup()
        greeting = self.advance(0.5)
        self.assertEqual(greeting, ["Good morning, sir. Xyrus online. You have 1 thing today; next is Standup at 12:15 pm."])
        said = self.at(T(2026, 9, 13, 13, 30))                     # 3 h after 10:30
        self.assertEqual(said, ["While I was away, sir: Standup at 12:15 pm, today."])
        self.assertIn(said[0], self.ns.speaker.forced())
        self.assertEqual(self.at(T(2026, 9, 13, 13, 31)), [])
        e2, ns2 = make_test_engine(now=T(2026, 9, 13, 13, 32), tmpdir=self.tmp)     # restart: never again
        e2.startup()
        ns2.clock.advance(1)
        e2.tick()
        ns2.clock.advance(10)
        e2.tick()
        self.assertEqual(ns2.speaker.said,        # the greeting names today's item once; no second "while I was away"
                         ["Good afternoon, sir. Xyrus online. You have 1 thing today: Standup at 12:15 pm."])


class Assistant(EngineCase):
    def test_remember_recall_forget(self):
        said = self.mic("cyrus remember that the wifi password is banana seven",
                        free="cyrus remember that the wifi password is banana seven")
        self.assertEqual(said, ["Got it: 'the wifi password is banana seven'. Correct?"])
        self.assertEqual(self.mic("yes"), ["Remembered, sir."])
        self.assertEqual(self.mic("cyrus remember"), ["What should I remember?"])
        self.assertEqual(self.mic("the car is on level three", free="the car is on level three"),
                         ["Got it: 'the car is on level three'. Correct?"])
        self.mic("yes")
        self.assertEqual(self.ns.memory.count(), 2)
        self.assertEqual(self.say("cyrus what did i ask you to remember"),
                         ["You asked me to remember two things. One: the car is on level three. "
                          "Two: the wifi password is banana seven."])
        self.assertEqual(self.say("cyrus forget that"), ["Forget 'the car is on level three'? Yes or no."])
        self.assertEqual(self.say("yes"), ["Forgotten, sir."])
        self.assertEqual(self.ns.memory.count(), 1)

    def test_recall_three_at_a_time(self):
        for i in range(1, 6):
            self.ns.memory.add(f"fact {i}", "voice")
        self.assertEqual(self.say("cyrus what do you remember"),
                         ["You asked me to remember five things. One: fact 5. Two: fact 4. Three: fact 3. "
                          "There are two more. Say more to hear them."])
        self.assertEqual(self.mic("more"), ["Four: fact 2. Five: fact 1."])

    def test_forget_everything(self):
        for i in range(3):
            self.ns.memory.add(f"fact {i}", "voice")
        self.assertEqual(self.say("cyrus forget everything"), ["Forget all three things, sir? Yes or no."])
        self.say("yes")
        self.assertEqual(self.ns.memory.count(), 0)
        self.assertEqual(self.say("cyrus what did i tell you"),
                         ["Nothing yet, sir. Say 'remember that' and I'll keep it."])

    def test_good_morning_at_three_pm(self):              # §7.2 E26
        self.store.add_event("Standup", D(2026, 9, 13), "16:00")
        self.store.add_event("Gym", D(2026, 9, 13), "19:00")
        self.ns.clock.set(T(2026, 9, 13, 15, 0))
        (said,) = self.say("cyrus good morning")
        self.assertEqual(said, "Good afternoon, sir. Today you have 2 events: Standup at 4 pm and Gym at 7 pm.")
        self.assertLessEqual(said.count(". ") + 1, 4)

    def test_good_night_and_name(self):
        self.store.add_event("Standup", D(2026, 9, 14), "09:30")
        self.assertEqual(self.say("cyrus good night"), ["Good night, sir. Tomorrow starts with Standup at 9:30 am."])
        self.assertEqual(self.say("cyrus what's my name"), ["You haven't told me yet, sir. It's in Settings."])
        self.ns.config.set("user_name", "Refath")
        self.assertEqual(self.say("cyrus what's my name"), ["You're Refath, sir."])


class Timers(EngineCase):
    V1_TABLE = {"cyrus set a timer for five minutes": 300, "cyrus timer twenty five seconds": 25,
                "cyrus timer four seven minutes": 420, "cyrus set timer four ten minutes": 600,
                "cyrus timer seven minutes": 420, "cyrus timer forty five seconds": 45,
                "cyrus timer half an hour": 1800, "cyrus timer one and a half hours": 5400,
                "cyrus timer a minute": 60, "cyrus timer two hours": 7200, "cyrus timer ninety seconds": 90}

    def test_v1_timer_table(self):
        for text, secs in self.V1_TABLE.items():
            with self.subTest(text=text):
                self.e.timers.cancel(all=True)
                self.assertEqual(self.mic(text), [f"Timer set for {P.speak_duration(secs)}, sir."])
                self.assertEqual([v.remaining_s for v in self.e.timers.views()], [secs])
        self.assertEqual(self.mic("cyrus cancel timer"), ["Timer cancelled, sir."])

    def test_named_left_add_cancel(self):
        self.assertEqual(self.say("cyrus tea timer three minutes"), ["Tea timer set for three minutes, sir."])
        self.assertEqual(self.say("cyrus how long is left"), ["Three minutes left, sir."])
        self.say("cyrus set a timer for ten minutes")
        self.assertEqual(self.say("cyrus how much time is left"),
                         ["Tea: three minutes. The 10 minute timer: ten minutes."])
        self.assertEqual(self.say("cyrus add five minutes"), ["Added five minutes, sir."])
        self.assertEqual(self.mic("cancel the tea timer"), ["Timer cancelled, sir."])      # no wake word
        self.assertEqual(self.mic("cancel all timers"), ["Timer cancelled, sir."])
        self.assertEqual(self.mic("cancel timer"), [], "no timer: noise")
        self.assertEqual(self.say("cyrus time left"), ["No timers running, sir."])
        self.assertEqual(self.say("cyrus add five minutes"), ["There's no timer running, sir."])

    def test_missing_duration(self):
        self.assertEqual(self.mic("cyrus set a timer"), ["For how long, sir?"])
        self.assertEqual(self.mic("how"), [])
        self.assertEqual(self.mic("five minutes"), ["Timer set for five minutes, sir."])

    def test_ringing_stop_and_snooze(self):
        self.say("cyrus set a timer for one minute")
        said = self.advance(61)
        self.assertEqual(said, ["Time's up, sir!"])
        self.assertEqual(self.mic("snooze"), ["Five more minutes, sir."])
        self.assertFalse(self.e.timers.ringing())
        self.assertEqual(self.advance(300), ["Time's up, sir!"])
        self.assertEqual(self.mic("stop"), [])
        self.assertFalse(self.e.timers.ringing())
        self.say("cyrus set a timer for one minute")
        self.advance(61)
        self.assertEqual(self.mic("cancel timer"), ["Timer cancelled, sir."])
        self.assertFalse(self.e.timers.any())


if __name__ == "__main__":
    unittest.main()
