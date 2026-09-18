"""The 9 dialogue_proto.py scenarios against the real engine (+ real xyrus.dates when present, else the
prototype parser) and a stub store; plus Confirm/Ask/Pick dialogues and the construction-order regression."""
import datetime as dt
import os
import tempfile
import unittest

os.environ.setdefault("XYRUS_DATA_DIR", tempfile.mkdtemp(prefix="xyrus_test_"))

from xyrus import dialogue as D
from xyrus.registry import Registry
from xyrus.testing import StubMemory, StubStore, make_test_engine

NOW = dt.datetime(2026, 9, 13, 14, 0)
TOMORROW = dt.date(2026, 9, 14)


def registry():
    r = Registry()

    def saver(ctx, kind):
        def save(slots):
            ctx.store.add_event(slots["title"], slots["date"], slots["time"], reminder_min=10, source="voice")
            ctx.engine.saved.append({"kind": kind, **slots})
            ctx.say("Done{sir}.")
        return save

    @r.command("add_event", "add an event", section="Calendar & reminders", help="adds an event")
    def add_event(ctx, m):
        ctx.start_dialogue(D.Dialogue(ctx, "event", on_complete=saver(ctx, "event"), trigger="add an event"))

    @r.command("remind", "remind me", section="Calendar & reminders", help="reminder")
    def remind(ctx, m):
        tail = m.text.split("remind me", 1)[1].strip()
        slots = {}
        if tail:
            date, time, _, _ = D.parse_when(tail, ctx.clock.now(), ctx.config)
            slots = {"date": date, "time": time}
        ctx.start_dialogue(D.Dialogue(ctx, "reminder", slots=slots, on_complete=saver(ctx, "reminder"),
                                      trigger="remind me"))

    @r.command("note", "add a note", section="Calendar & reminders", help="note")
    def note(ctx, m):
        def save(slots):
            ctx.store.add_note(ctx.clock.now().date(), slots["title"], source="voice")
            ctx.say("Noted{sir}.")
        ctx.start_dialogue(D.Dialogue(ctx, "note", on_complete=save, trigger="add a note"))

    @r.command("remember", "remember that", section="Assistant", help="memory")
    def remember(ctx, m):
        def save(slots):
            ctx.memory.add(slots["title"], "voice")
            ctx.say("Remembered{sir}.")
        ctx.start_dialogue(D.Dialogue(ctx, "memory", on_complete=save, trigger="remember that"))

    @r.command("pick_test", "pick one", section="Basics", help="pick")
    def pick(ctx, m):
        ctx.start_dialogue(D.PickDialogue(ctx, ["dentist at 3 PM", "gym at 6 PM"],
                                          lambda i: ctx.say(f"Picked {i}.")))

    @r.command("ask_test", "ask me", section="Basics", help="ask")
    def ask(ctx, m):
        ctx.ask("How long?", "duration", lambda secs: ctx.say(f"Got {secs}."))

    return r


class Scenario(unittest.TestCase):
    def setUp(self):
        self.e, self.ns = make_test_engine(now=NOW, registry=registry(), store=StubStore(), memory=StubMemory())
        self.e.saved = []

    def say(self, text, wake=False):
        self.e.handle(("cyrus " if wake else "") + text, "mic", free_text=text if self.mode() == "free" else None)
        return self.ns.speaker.said[-1] if self.ns.speaker.said else None

    def mode(self):
        return self.e.listen_spec().mode

    def wait(self, s):
        self.ns.clock.advance(s)
        self.e.tick()
        return self.ns.speaker.said[-1]

    @property
    def saved(self):
        return self.e.saved

    # 1
    def test_1_full_flow(self):
        self.assertEqual(self.say("add an event", wake=True), "What's the event?")
        self.assertEqual(self.mode(), "free")
        view = self.e.snapshot().dialogue
        self.assertEqual((view.kind, view.state, view.listening), ("event", "title", "free"))
        self.assertEqual(self.say("dentist appointment"), "When?")
        self.assertEqual(self.mode(), "when")
        self.assertEqual(self.say("tomorrow"), "What time?")
        self.assertEqual(self.say("at three pm"), "Dentist appointment, tomorrow at 3 PM. Correct?")
        self.assertEqual(self.mode(), "confirm")
        self.assertEqual(self.e.snapshot().dialogue.slots,
                         {"title": "Dentist appointment", "date": "tomorrow", "time": "3 PM"})
        self.assertEqual(self.say("yes"), "Done, sir.")
        self.assertEqual(self.saved[0]["title"], "dentist appointment")
        self.assertEqual((self.saved[0]["date"], self.saved[0]["time"]), (TOMORROW, dt.time(15)))
        self.assertIsNone(self.e.snapshot().dialogue)

    # 2
    def test_2_prefilled_date_and_time(self):
        self.assertEqual(self.say("remind me tomorrow at three pm", wake=True), "What should I remind you about?")
        self.assertEqual(self.say("call mom"), "Call mom, tomorrow at 3 PM. Correct?")
        self.say("correct")
        self.assertEqual((self.saved[0]["kind"], self.saved[0]["date"]), ("reminder", TOMORROW))

    # 3
    def test_3_title_carries_date_and_time(self):
        self.say("add an event", wake=True)
        self.assertEqual(self.say("team meeting tomorrow at nine am"), "Team meeting, tomorrow at 9 AM. Correct?")
        self.say("yes")
        self.assertEqual((self.saved[0]["title"], self.saved[0]["time"]), ("team meeting", dt.time(9)))

    # 4
    def test_4_bare_hour_inferred(self):
        self.say("remind me", wake=True)
        self.assertEqual(self.say("take the bins out"), "When?")
        self.assertEqual(self.say("at seven"), "Take the bins out, today at 7 PM. Correct?")
        self.say("yes")
        self.assertEqual((self.saved[0]["date"], self.saved[0]["time"]), (NOW.date(), dt.time(19)))

    # 5
    def test_5_no_then_fix_the_time(self):
        self.say("add an event", wake=True)
        self.say("dentist")
        self.say("tomorrow at three pm")
        self.assertEqual(self.say("no"), "What should I change: the title, the date, or the time?")
        self.assertEqual(self.say("the time"), "What time?")
        self.assertEqual(self.say("at nine am"), "Dentist, tomorrow at 9 AM. Correct?")
        self.say("yes")
        self.assertEqual(self.saved[0]["time"], dt.time(9))

    # 6
    def test_6_cancel_mid_dialogue(self):
        self.say("add an event", wake=True)
        self.say("dentist")
        self.assertIn(self.say("cancel"), ("Cancelled, sir.", "Alright, dropped."))
        self.assertEqual(self.saved, [])
        self.assertIsNone(self.e.snapshot().dialogue)
        self.assertEqual(self.mode(), "wake")

    # 7
    def test_7_noise_swallowed_garbage_costs_attempts(self):
        self.say("add an event", wake=True)
        self.say("dentist")
        n = len(self.ns.speaker.said)
        self.say("how")
        self.say("half")
        self.assertEqual(len(self.ns.speaker.said), n)          # swallowed, no reply
        self.assertEqual(self.say("play minute you"), "Sorry, I didn't catch that. When?")
        self.assertEqual(self.say("this is a launch"),
                         "Sorry, sir, I'm not getting it. You can type it in the window, or try again later.")
        self.assertIsNone(self.e.snapshot().dialogue)
        self.assertEqual(self.saved, [])

    # 8
    def test_8_nudge_then_leave(self):
        self.say("add an event", wake=True)
        n = len(self.ns.speaker.said)
        self.wait(19)
        self.assertEqual(len(self.ns.speaker.said), n)
        self.assertEqual(self.wait(2), "Still there, sir? What's the event?")
        self.assertEqual(self.wait(21), "I'll leave it for now, sir. Say 'add an event' when you're ready.")
        self.assertIsNone(self.e.snapshot().dialogue)

    # 9
    def test_9_all_day(self):
        self.say("add an event", wake=True)
        self.say("mothers birthday")
        self.assertEqual(self.say("tomorrow"), "What time?")
        self.assertEqual(self.say("all day"), "Mothers birthday, tomorrow. Correct?")
        self.say("yes")
        self.assertTrue(self.saved[0]["all_day"])
        self.assertIsNone(self.saved[0]["time"])

    # construction-order regression: the deadline is armed after the first question
    def test_deadline_armed_after_first_question(self):
        self.say("add an event", wake=True)
        self.assertIsNotNone(self.e.dialogue.deadline)
        self.assertEqual(self.e.snapshot().dialogue.seconds_left, 20)

    def test_deadline_only_after_question_finished(self):
        e, ns = make_test_engine(now=NOW, registry=registry(), store=StubStore(), memory=StubMemory(),
                                 auto_done=False)
        e.saved = []
        e.handle("cyrus add an event", "mic")
        self.assertIsNone(e.dialogue.deadline)
        self.assertEqual(e.snapshot().dialogue.seconds_left, 0)
        ns.clock.advance(30)
        e.tick()
        self.assertIsNotNone(e.dialogue)
        ns.speaker.finish()
        self.assertEqual(e.snapshot().dialogue.seconds_left, 20)

    def test_typed_answers_skip_noise_rule(self):
        self.say("add an event", wake=True)
        self.say("dentist")
        self.e.handle("banana", "typed")
        self.assertEqual(self.ns.speaker.said[-1], "Sorry, I didn't catch that. When?")

    def test_no_is_not_a_cancel_word_in_slots(self):
        self.say("add an event", wake=True)
        self.say("dentist")
        self.say("no")                                          # one word: noise in WHEN, not a cancel
        self.assertIsNotNone(self.e.snapshot().dialogue)

    def test_note_saves_without_confirm(self):
        self.assertEqual(self.say("add a note", wake=True), "What's the note?")
        self.assertEqual(self.say("bring the charger"), "Noted, sir.")
        self.assertEqual(self.ns.store.notes[0]["text"], "bring the charger")

    def test_memory_confirm(self):
        self.say("remember that", wake=True)
        self.assertEqual(self.say("the wifi password is banana seven"),
                         "Got it: 'the wifi password is banana seven'. Correct?")
        self.assertEqual(self.say("yes"), "Remembered, sir.")
        self.assertEqual(self.ns.memory.count(), 1)

    def test_pick(self):
        self.assertEqual(self.say("pick one", wake=True),
                         "Which one, sir? First: dentist at 3 PM. Second: gym at 6 PM.")
        self.assertEqual(self.say("the second"), "Picked 1.")

    def test_ask_duration(self):
        self.assertEqual(self.say("ask me", wake=True), "How long?")
        self.assertEqual(self.mode(), "when")
        self.say("how")
        self.assertEqual(self.say("five minutes"), "Got 300.")

    def test_alerts_wait_for_dialogue(self):
        self.say("add an event", wake=True)
        self.e._alert_queue.append(object())
        fired = []
        self.e._fire_alert = lambda a, now: fired.append(a)
        self.say("cancel")
        self.assertEqual(len(fired), 1)


if __name__ == "__main__":
    unittest.main()
