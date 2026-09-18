"""Routing of Whisper-captured commands (hearing rebuild, Sep 15 2026) - transcript level, no audio.

The captured text is full-vocabulary: it is matched with free_text=None (so "put on shape of you" keeps the slot
"shape of you") but still reaches the command as ctx.free_text. When nothing parses: one "Go ahead" / "Say that
again" per wake, a bare "<title> by <artist>" is a song, did-you-mean tiers, then "Sorry". Faint power commands
are confirmed; Whisper's n-best alternates are tried after its own text.
"""
import os
import re
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("XYRUS_DATA_DIR", tempfile.mkdtemp(prefix="xyrus_test_"))

from xyrus import normalize as N
from xyrus import persona
from xyrus import replies as R
from xyrus.capture import CapturedTranscript, clean_text
from xyrus.registry import display_phrase
from xyrus.testing import FakeActions, make_test_engine

SONG = ("Faded", "https://www.youtube.com/watch?v=abc", "abc")


def said(text: str, **kw) -> CapturedTranscript:
    t = clean_text(text)
    fields = {"peak": 0.3, **kw}
    return CapturedTranscript(text=t, mode="command", free_text=t, raw=text, **fields)


def cap_engine(**kw):
    overrides = {"command_window_s": 0, **kw.pop("config_overrides", {})}
    kw.setdefault("actions", FakeActions(find_song=SONG))
    e, ns = make_test_engine(tmpdir=Path(tempfile.mkdtemp(prefix="xyrus_route_")), config_overrides=overrides,
                             **kw)
    e.capture_available = lambda: True
    return e, ns


def wake(e):
    e.handle("xyrus", "typed")


def cmds(e) -> list[str]:
    return [ev.text.split()[0] for ev in e.events if ev.kind == "cmd"]


def record_ctx(e) -> list:
    made = []
    orig = e._ctx

    def spy(*a, **k):
        c = orig(*a, **k)
        made.append(c)
        return c
    e._ctx = spy
    return made


def ctx_for(made, name):
    return [c for c in made if c.match is not None and c.match.command.name == name]


def render(ns, template, **kw):
    return persona.render(template, ns.config, **kw)


class QuestionForms(unittest.TestCase):
    FORMS = [("Can you play Shape of You?", "shape of you"), ("Could you play Shape of You?", "shape of you"),
             ("Would you play Faded?", "faded"), ("Will you play Believer?", "believer"),
             ("Can you please play Faded?", "faded"), ("Please play Believer.", "believer"),
             ("Play me Shape of You.", "shape of you"), ("Put on Shape of You.", "shape of you"),
             ("Can you put on Faded?", "faded"), ("I want to hear Believer.", "believer"),
             ("Can you play Tired by Alan Walker?", "tired by alan walker"),
             ("Could you please play me Faded?", "faded")]

    def test_twelve_question_forms_clean_slots(self):
        for text, song in self.FORMS:
            with self.subTest(text=text):
                e, ns = cap_engine()
                made = record_ctx(e)
                wake(e)
                e.handle_transcript(said(text))
                got = ctx_for(made, "play_song")
                self.assertTrue(got, (text, ns.speaker.said[-2:]))
                self.assertEqual(got[0].match.slots["song"], song)
                self.assertTrue(got[0].free_text)                # the captured text stays ctx.free_text

    def test_free_slot_command_sees_free_text(self):
        e, ns = cap_engine()
        made = record_ctx(e)
        wake(e)
        e.handle_transcript(said("Search for cheap flights to Dhaka."))
        got = ctx_for(made, "web_search")
        self.assertTrue(got, ns.speaker.said[-2:])
        self.assertIn("cheap flights", got[0].match.slots["query"])
        self.assertIn("cheap flights", got[0].free_text or "")


class BareTitle(unittest.TestCase):
    POSITIVE = ["shape of you by ed sheeran", "tired by alan walker", "stand by me", "if shown you by icons"]
    NEGATIVE = ["volume up by ten", "close by", "one by one", "by the way", "raise the volume by five"]

    def test_positive_confirms_then_plays(self):
        for t in self.POSITIVE:
            with self.subTest(t=t):
                e, ns = cap_engine()
                made = record_ctx(e)
                wake(e)
                e.handle_transcript(said(t.capitalize() + ".", alternates=("x " + t,)))
                self.assertIsNotNone(e.dialogue)
                self.assertEqual(ns.speaker.said[-1], render(ns, R.PLAY_TITLE_Q, phrase=t))
                self.assertNotIn("play_song", cmds(e))           # asked, not played
                e.handle_transcript(said("Yes."))
                self.assertIn("play_song", cmds(e))
                got = ctx_for(made, "play_song")
                self.assertEqual(got[-1].match.slots["song"], t)
                self.assertEqual(got[-1].alternates, ("x " + t,))

    def test_negatives_are_not_songs(self):
        for t in self.NEGATIVE:
            with self.subTest(t=t):
                self.assertIsNone(N.bare_title(t))
                e, ns = cap_engine()
                made = record_ctx(e)
                wake(e)
                e.handle_transcript(said(t))
                self.assertFalse(ctx_for(made, "play_song"))
                self.assertFalse(ns.speaker.said[-1].startswith("Play "), ns.speaker.said[-1])

    def test_no_pattern_variant_is_a_title(self):
        e, ns = cap_engine()
        fill = {"number": "five", "duration": "five minutes", "app": "chrome", "song": "faded", "when": "tomorrow"}
        for c in ns.registry.commands():
            for p in c.patterns:
                for phrase in {display_phrase(p), re.sub(r"[\[\]]", "", p.text)}:
                    phrase = re.sub(r"<(\w+)>", lambda mm: fill.get(mm.group(1), "thing"), phrase)
                    phrase = " ".join(phrase.replace("|", " ").split())
                    with self.subTest(cmd=c.name, phrase=phrase):
                        self.assertIsNone(N.bare_title(phrase))

    def test_correction_within_90_s_plays_without_confirm(self):
        e, ns = cap_engine()
        made = record_ctx(e)
        wake(e)
        e.handle_transcript(said("Play Faded."))
        self.assertEqual(cmds(e).count("play_song"), 1)
        ns.clock.advance(30)
        wake(e)
        e.handle_transcript(said("Tired by Alan Walker."))
        self.assertIsNone(e.dialogue)
        self.assertEqual(cmds(e).count("play_song"), 2)
        self.assertEqual(ctx_for(made, "play_song")[-1].match.slots["song"], "tired by alan walker")
        ns.clock.advance(120)                                    # long after: a confirm again
        wake(e)
        e.handle_transcript(said("Stand by me."))
        self.assertIsNotNone(e.dialogue)
        self.assertEqual(cmds(e).count("play_song"), 2)


class ReArm(unittest.TestCase):
    def test_cut_off_request_go_ahead_once(self):
        e, ns = cap_engine()
        wake(e)
        e.handle_transcript(said("Can you please?"))
        self.assertEqual(ns.speaker.said[-1], render(ns, R.GO_AHEAD))
        self.assertIn(e.mode, ("arming", "armed"))
        e.handle_transcript(said("Um."))
        self.assertEqual(ns.speaker.said[-1], render(ns, R.NOT_HEARD))
        self.assertEqual(e.mode, "idle")
        n = len(ns.speaker.said)
        e.handle_transcript(said("Hmm."))                         # no wake: never a second Sorry
        self.assertEqual(len(ns.speaker.said), n)
        self.assertEqual(ns.speaker.said.count(render(ns, R.NOT_HEARD)), 1)

    def test_unsure_decode_say_again_once(self):
        e, ns = cap_engine()
        wake(e)
        e.handle_transcript(said("Zorp quibble flarn.", confidence=-0.8))
        self.assertEqual(ns.speaker.said[-1], render(ns, R.SAY_AGAIN))
        e.handle_transcript(said("Zorp quibble flarn.", confidence=-0.8))
        self.assertEqual(ns.speaker.said[-1], render(ns, R.NOT_HEARD))
        self.assertEqual(e.mode, "idle")

    def test_budget_is_shared(self):
        e, ns = cap_engine()
        wake(e)
        e.handle_transcript(said("Can you please?"))
        e.handle_transcript(said("Zorp quibble flarn.", confidence=-0.8))
        self.assertEqual(ns.speaker.said[-1], render(ns, R.NOT_HEARD))
        self.assertNotIn(render(ns, R.SAY_AGAIN), ns.speaker.said)
        wake(e)                                                  # a new wake: a fresh budget
        e.handle_transcript(said("Can you please?"))
        self.assertEqual(ns.speaker.said[-1], render(ns, R.GO_AHEAD))

    def test_sure_garbage_is_sorry(self):
        e, ns = cap_engine()
        wake(e)
        e.handle_transcript(said("Zorp quibble flarn.", confidence=-0.1))
        self.assertEqual(ns.speaker.said[-1], render(ns, R.NOT_HEARD))


class CaptureHooks(unittest.TestCase):
    def wake_replies(self, ns):
        options = ns.config.get("wake_replies") or list(R.WAKE_REPLIES)
        return {render(ns, o) for o in options}

    def test_captured_while_arming_is_accepted(self):
        e, ns = cap_engine(auto_done=False)
        wake(e)
        self.assertEqual(e.mode, "arming")                       # "Yes, sir?" still being spoken
        e.handle_transcript(said("Take a screenshot."))
        self.assertIn("screenshot", cmds(e))

    def test_wake_with_command_runs_without_yes_sir(self):
        e, ns = cap_engine()
        e.on_wake_with_command("take a screenshot", said("Xyrus, take a screenshot."))
        self.assertIn("screenshot", cmds(e))
        self.assertIn("wake", ns.chime.played)
        self.assertFalse(self.wake_replies(ns) & set(ns.speaker.said))
        self.assertIsNone(e.dialogue)

    def test_wake_with_command_miss_falls_back_to_armed_capture(self):
        e, ns = cap_engine()
        e.on_wake_with_command("zorp quibble flarn", said("Xyrus zorp quibble flarn", confidence=-0.8))
        self.assertEqual(len(ns.speaker.said), 1, ns.speaker.said)
        self.assertIn(ns.speaker.said[0], self.wake_replies(ns))
        self.assertIn(e.mode, ("arming", "armed"))
        e.handle_transcript(said("Take a screenshot."))          # the normal capture follows
        self.assertIn("screenshot", cmds(e))

    def test_wake_with_command_without_transcript(self):
        e, ns = cap_engine()
        e.on_wake_with_command("what time is it", None)
        self.assertIn("time", cmds(e))

    def test_capture_timeout_is_silent(self):
        e, ns = cap_engine()
        wake(e)
        n = len(ns.speaker.said)
        e.on_capture_timeout()
        self.assertEqual(e.mode, "idle")
        self.assertEqual(len(ns.speaker.said), n)
        n_ev = len(e.events)
        e.on_capture_timeout()                                   # idle already: nothing at all
        self.assertEqual(len(e.events), n_ev)


class PowerConfirm(unittest.TestCase):
    def check(self, **kw):
        e, ns = cap_engine()
        wake(e)
        e.handle_transcript(said("Go to sleep.", **kw))
        self.assertFalse(ns.actions.called("sleep"))
        self.assertIsNotNone(e.dialogue)
        self.assertEqual(ns.speaker.said[-1], render(ns, R.CONFIRM_POWER_HEARD, phrase="go to sleep"))
        e.handle_transcript(said("Yes."))
        self.assertTrue(ns.actions.called("sleep"))

    def test_faint_peak(self):
        self.check(peak=0.03)

    def test_short_speech(self):
        self.check(vad_speech_s=0.5)

    def test_unsure_decode(self):
        self.check(confidence=-0.6)

    def test_clear_request_runs(self):
        e, ns = cap_engine()
        wake(e)
        e.handle_transcript(said("Go to sleep.", peak=0.3, vad_speech_s=1.2, confidence=-0.1))
        self.assertTrue(ns.actions.called("sleep"))

    def test_no_means_no(self):
        e, ns = cap_engine()
        wake(e)
        e.handle_transcript(said("Go to sleep.", peak=0.03))
        e.handle_transcript(said("No."))
        self.assertFalse(ns.actions.called("sleep"))


class AlternatesFusion(unittest.TestCase):
    def test_alternate_fixes_the_app(self):
        e, ns = cap_engine()
        wake(e)
        e.handle_transcript(said("Open throw.", alternates=("open chrome",)))
        self.assertTrue(any("chrome" in str(a) for a in ns.actions.called("open_target")),
                        ns.actions.calls)

    def test_destructive_alternate_only_asks(self):
        e, ns = cap_engine()
        wake(e)
        e.handle_transcript(said("Shot down.", alternates=("shut down",)))
        self.assertFalse(ns.actions.called("shutdown"))
        self.assertIsNotNone(e.dialogue)

    def test_ctx_carries_the_hearing_fields(self):
        e, ns = cap_engine()
        made = record_ctx(e)
        wake(e)
        e.handle_transcript(said("Play Faded.", alternates=("play fated",), bn_text="ফেডেড", confidence=-0.2))
        c = ctx_for(made, "play_song")[0]
        self.assertEqual(c.match.slots["song"], "faded")          # the title from Whisper's own text
        self.assertEqual(c.alternates, ("play fated",))
        self.assertEqual(c.bn_text, "ফেডেড")
        self.assertEqual(c.confidence, -0.2)

    def test_typed_ctx_has_defaults(self):
        e, ns = cap_engine()
        made = record_ctx(e)
        e.handle("play faded", "typed")
        c = ctx_for(made, "play_song")[0]
        self.assertEqual((c.alternates, c.bn_text, c.confidence), ((), None, None))


class DidYouMean(unittest.TestCase):
    def test_run_tier(self):
        e, ns = cap_engine()
        wake(e)
        e.handle_transcript(said("Show desk top."))
        self.assertIn("show_desktop", cmds(e))

    def test_ask_tier(self):
        e, ns = cap_engine()
        wake(e)
        e.handle_transcript(said("Flip a con."))
        self.assertEqual(ns.speaker.said[-1], render(ns, R.DID_YOU_MEAN, phrase="flip a coin"))
        e.handle_transcript(said("Yes."))
        self.assertIn("coin", cmds(e))

    def test_disruptive_only_asks(self):
        e, ns = cap_engine()
        wake(e)
        e.handle_transcript(said("Asleep."))
        self.assertFalse(ns.actions.called("sleep"))
        self.assertIsNotNone(e.dialogue)


class PlaybackFlag(unittest.TestCase):
    def test_lifecycle(self):
        e, ns = cap_engine()
        self.assertFalse(e.listen_spec().playback)
        wake(e)
        e.handle_transcript(said("Play Faded."))
        self.assertTrue(any(ev.text == "playback: autoplay" for ev in e.events))
        self.assertTrue(e.listen_spec().playback)
        wake(e)
        e.handle_transcript(said("Stop the music."))
        self.assertFalse(e.listen_spec().playback)
        wake(e)
        e.handle_transcript(said("Play Faded."))
        self.assertTrue(e.listen_spec().playback)
        ns.clock.advance(601)
        e.tick()
        self.assertFalse(e.listen_spec().playback)

    def test_blocked_autoplay_is_not_playback(self):
        e, ns = cap_engine(actions=FakeActions(find_song=SONG, ensure_playing="blocked"))
        wake(e)
        e.handle_transcript(said("Play Faded."))
        self.assertFalse(e.listen_spec().playback)


if __name__ == "__main__":
    unittest.main()
