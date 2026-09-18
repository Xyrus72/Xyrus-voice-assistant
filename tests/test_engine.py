"""Headless engine tests (§7.2 E1-E26) + engine mechanics, executor, G8.

Every E-case runs twice: against a local stub command set that follows the §3 phrases/replies exactly (so the
engine is verified independently of T2/T4), and against the real REGISTRY + commands.load_all when the
command package exists (skipped otherwise).
"""
import datetime as dt
import os
import re
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.parse import quote_plus

os.environ.setdefault("XYRUS_DATA_DIR", tempfile.mkdtemp(prefix="xyrus_test_"))

from xyrus import persona
from xyrus import replies as R
from xyrus.executor import Executor, InlineExecutor
from xyrus.interfaces import Transcript
from xyrus.parsing import say_number
from xyrus.registry import Registry
from xyrus.testing import FakeActions, StubMemory, StubStore, drain_ui, make_test_engine

BASE = Path(__file__).resolve().parent.parent
WAKE_RE = re.compile(r"\b(cyrus|zeros|virus|cirrus)\b", re.I)
V1_TIMER_CASES = {
    "set a timer for five minutes": 300, "timer twenty five seconds": 25, "timer four seven minutes": 420,
    "set timer four ten minutes": 600, "timer seven minutes": 420, "timer forty five seconds": 45,
    "timer half an hour": 1800, "timer one and a half hours": 5400, "timer a minute": 60,
    "timer two hours": 7200, "timer ninety seconds": 90, "set a timer for seven minutes": 420,
}


def _has(mod):
    try:
        __import__(mod)
        return True
    except ImportError:
        return False


HAVE_COMMANDS = _has("xyrus.commands") and hasattr(__import__("xyrus.commands").commands, "load_all") \
    if _has("xyrus.commands") else False


# ============================================================================= stub commands (§3 wording)
def stub_registry() -> Registry:
    r = Registry()
    c = r.command

    @c("lock", "lock", "lock screen", "lock the pc", "lock the computer", section="Power", help="locks")
    def lock(ctx, m):
        ctx.say(R.LOCKING, then=lambda: ctx.do(ctx.actions.lock))

    def go_shutdown(ctx):
        delay = int(ctx.config.get("shutdown_delay_s", 10))
        ctx.do(ctx.actions.shutdown, delay)
        ctx.say(ctx.render(R.SHUTTING_DOWN, seconds=delay))
        ctx.engine.shutdown_until = ctx.clock.mono() + delay + 5
        ctx.follow_up("shutdown", ["cancel_shutdown", "shutdown_stop", ("halt", "cancel_shutdown")],
                      seconds=delay + 5)

    @c("shutdown_stop", "stop", section="Power", help="stops a countdown", follow_up=True,
       armed_ok_single_word=False)
    def shutdown_stop(ctx, m):
        ctx.engine.shutdown_until = None
        ctx.do(ctx.actions.abort_shutdown)
        ctx.say(R.SHUTDOWN_CANCELLED)

    @c("fake_alert", "fake alert", section="Basics", help="opens an alert follow-up")
    def fake_alert(ctx, m):
        ctx.say("Sir, reminder: test.", force=True)
        ctx.follow_up("alert", ["alert_snooze"], payload="ALERT-1", seconds=20)

    @c("alert_snooze", "snooze", section="Calendar & reminders", help="snoozes", follow_up=True)
    def alert_snooze(ctx, m):
        ctx.engine.seen_payload = ctx.payload
        ctx.say("Snoozed{sir}.")

    @c("search", "search for <query>", "google <query>", "search the web for <query>", section="Apps & web",
       help="web search")
    def search(ctx, m):
        q = m.slots["query"]
        ctx.do(ctx.actions.open_target, "https://www.google.com/search?q=" + quote_plus(q))
        ctx.say(ctx.render(R.SEARCHING, query=q))

    @c("shutdown", "shut down", "shutdown", "power off", "turn off the pc", "turn off the computer",
       section="Power", help="shuts down", destructive=True)
    def shutdown(ctx, m):
        if ctx.config.get("confirm_shutdown", True):
            ctx.confirm(R.CONFIRM_SHUTDOWN, lambda: go_shutdown(ctx))
        else:
            go_shutdown(ctx)

    @c("cancel_shutdown", "cancel", "abort", "cancel shutdown", "cancel the shutdown", "stop the shutdown",
       section="Power", help="aborts", follow_up=True)
    def cancel_shutdown(ctx, m):
        ctx.engine.shutdown_until = None
        ctx.do(ctx.actions.abort_shutdown)
        ctx.say(R.SHUTDOWN_CANCELLED)

    @c("open_app", "open <app>", "launch <app>", "start <app>", section="Apps & web", help="opens")
    def open_app(ctx, m):
        app = m.slots["app"]
        if app.target is None:
            ctx.say(ctx.render(R.UNKNOWN_APP, name=app.spoken))
            return
        ctx.say(ctx.render(R.OPENING, app=(app.alias or app.spoken).title()))
        ctx.do(ctx.actions.open_target, app.target)

    def set_timer(ctx, secs, name=None):
        ctx.timers.add(secs, name)
        dur = persona.speak_duration(secs)
        if name:
            ctx.say(ctx.render(R.NAMED_TIMER_SET, Name=name.capitalize(), duration=dur))
        else:
            ctx.say(ctx.render(R.TIMER_SET, duration=dur))

    @c("timer_set", "set a timer for <duration>", "set timer for <duration>", "set timer <duration>",
       "timer for <duration>", "timer <duration>", "start a timer for <duration>", "<duration> timer",
       section="Timers", help="sets a timer")
    def timer_set(ctx, m):
        set_timer(ctx, m.slots["duration"])

    @c("timer_named", "<name> timer <duration>", "set a <name> timer for <duration>", section="Timers",
       help="named timer")
    def timer_named(ctx, m):
        set_timer(ctx, m.slots["duration"], m.slots["name"])

    @c("timer_missing", "set a timer", "timer", "start a timer", section="Timers", help="asks how long")
    def timer_missing(ctx, m):
        ctx.ask(R.TIMER_ASK, "duration", lambda secs: set_timer(ctx, secs))

    @c("cancel_timer", "cancel timer", "stop timer", "cancel the timer", "stop the timer", "cancel all timers",
       "cancel the <name> timer", section="Timers", help="cancels", wake_free=True,
       context=lambda e: e.timers.any(), follow_up=True)
    def cancel_timer(ctx, m):
        n = ctx.timers.cancel(name=m.slots.get("name"), all="all" in m.text.split())
        ctx.say(R.TIMER_CANCELLED if n else R.NO_TIMER)

    @c("timer_ack", "stop", "okay", "thanks", "thank you", "got it", section="Timers", help="stops the alarm",
       wake_free=True, context=lambda e: e.timers.ringing(), follow_up=True)
    def timer_ack(ctx, m):
        ctx.timers.stop_ringing()

    @c("timer_snooze", "snooze", section="Timers", help="five more minutes", follow_up=True)
    def timer_snooze(ctx, m):
        ctx.timers.snooze(300)
        ctx.say(R.TIMER_SNOOZED)

    @c("set_volume", "set [the] volume to <percent>", "volume <percent>", "set volume <percent>",
       section="Sound & media", help="volume")
    def set_volume(ctx, m):
        pct = m.slots["percent"]
        ctx.do(ctx.actions.volume_set, pct, then=lambda _: ctx.say(ctx.render(R.VOLUME_AT, pct=pct)))

    @c("media_play", "play", "pause", "resume", "play pause", "pause the music", "resume the music",
       section="Sound & media", help="play/pause")
    def media_play(ctx, m):
        ctx.do(ctx.actions.media, "play_pause")

    @c("media_next", "next", "next track", "next song", "skip", section="Sound & media", help="next")
    def media_next(ctx, m):
        ctx.do(ctx.actions.media, "next")

    def play(ctx, q):
        def ok(res):
            title, url = res
            ctx.do(ctx.actions.open_target, url)
            ctx.say(ctx.render(R.PLAYING, title=title))

        def err(e):
            ctx.do(ctx.actions.open_target, "https://www.youtube.com/results?search_query=" + quote_plus(q))
            ctx.say(R.YOUTUBE_OFFLINE if isinstance(e, OSError) else ctx.render(R.SONG_NOT_FOUND, query=q))
        ctx.do(ctx.actions.find_song, q, then=ok, error=err, timeout=6)

    @c("song_ask", "play some music", "play music", "play a song", "play something", "play anything",
       "play a track", section="Apps & web", help="asks what to play")
    def song_ask(ctx, m):
        ctx.ask(R.SONG_ASK, "song", lambda title: play(ctx, title), listen="free")

    @c("play_song", "play <song>", "play <song> on youtube", section="Apps & web", help="plays a song")
    def play_song(ctx, m):
        play(ctx, m.slots["song"])

    @c("help", "what can you do", "help", "what are your commands", section="Basics", help="lists commands")
    def help_(ctx, m):
        ctx.ui("show:Commands")
        ctx.say(R.HELP)

    @c("next_event", "what's next", "next event", "what's my next event", section="Calendar & reminders",
       help="next event", follow_up=True)
    def next_event(ctx, m):
        ctx.say("Nothing scheduled{sir}. Your calendar's clear.")

    @c("time", "what time is it", "what's the time", "what is the time", "the time", "time", section="Info & tools",
       help="time")
    def time_(ctx, m):
        ctx.say(ctx.render(R.TIME_IS, time=persona.speak_time(ctx.clock.now())))

    @c("calc", "what is <expr>", "what's <expr>", "calculate <expr>", section="Info & tools", help="calculator")
    def calc(ctx, m):
        v = m.slots["expr"]
        ctx.say(R.UNDEFINED if v != v else ctx.render(R.CALC_RESULT, value=say_number(v)))

    @c("copy", "copy", "copy that", section="Keys & clipboard", help="ctrl+c", armed_ok_single_word=False)
    def copy(ctx, m):
        ctx.do(ctx.actions.keys, "ctrl+c")

    @c("boom", "explode now", section="Basics", help="raises")
    def boom(ctx, m):
        raise RuntimeError("boom")

    @c("routine_loop", "loop forever", section="Your commands", help="re-enters", custom=True)
    def loop(ctx, m):
        ctx.handle("loop forever")

    @c("slow", "do something slow", section="Basics", help="times out")
    def slow(ctx, m):
        ctx.do(ctx.actions.volume_get)

    return r


# ============================================================================= E-cases
class ECases:
    """Mixed into a TestCase; subclasses define make()."""

    def setUp(self):
        self._made = []

    def tearDown(self):
        for ns in self._made:                               # G6 over every line said (E24)
            for line in ns.speaker.said:
                self.assertIsNone(WAKE_RE.search(line), f"wake alias in reply: {line!r}")

    def make(self, **kw):
        raise NotImplementedError

    def _mk(self, **kw):
        e, ns = self.make(**kw)
        self._made.append(ns)
        return e, ns

    # ---- E1-E3: wake, window
    def test_E01_bare_wake_arms_after_reply(self):
        e, ns = self._mk()
        e.handle("cyrus", "mic")
        self.assertIn(ns.speaker.said[-1], ns.config.get("wake_replies"))
        snap = e.snapshot()
        self.assertEqual((snap.mode, snap.window_left_s, snap.listen), ("armed", 15, "command"))

    def test_E01b_window_starts_only_after_the_reply_finished(self):
        e, ns = self._mk(auto_done=False)
        e.handle("cyrus", "mic")
        self.assertEqual(e.snapshot().mode, "arming")
        ns.clock.advance(5)
        ns.speaker.finish()
        snap = e.snapshot()
        self.assertEqual((snap.mode, snap.window_left_s), ("armed", 15))

    def test_E02_window_expires(self):
        e, ns = self._mk()
        e.handle("cyrus", "mic")
        ns.clock.advance(16)
        e.tick()
        self.assertEqual(e.snapshot().mode, "idle")
        e.handle("lock", "mic")
        self.assertEqual(ns.actions.called("lock"), [])
        self.assertEqual(e.snapshot().events[-1].kind, "noise")

    def test_E03_command_inside_window_after_reply(self):
        e, ns = self._mk(auto_done=False)
        e.handle("cyrus", "mic")
        ns.speaker.finish()
        ns.clock.advance(10)
        e.handle("lock", "mic")
        self.assertEqual(ns.speaker.said[-1], "Locking, sir.")
        self.assertEqual(ns.actions.called("lock"), [])      # not before the reply finished
        ns.speaker.finish()
        self.assertEqual(ns.actions.called("lock"), [()])

    def test_E04_one_breath_open(self):
        e, ns = self._mk()
        e.handle("cyrus open chrome", "mic")
        self.assertEqual(ns.actions.called("open_target"), [("chrome",)])
        self.assertIn("Opening Chrome, sir.", ns.speaker.said)

    def test_E05_idle_noise(self):
        e, ns = self._mk()
        for w in ("how", "hey", "half"):
            e.handle(w, "mic")
        self.assertEqual(ns.speaker.said, [])
        self.assertEqual(ns.actions.calls, [])
        self.assertEqual([ev.kind for ev in e.snapshot().events[-3:]], ["noise"] * 3)

    def test_E06_timer_fires(self):
        e, ns = self._mk()
        e.handle("cyrus set a timer for five minutes", "mic")
        self.assertEqual(ns.speaker.said[-1], "Timer set for five minutes, sir.")
        ns.clock.advance(300)
        e.tick()
        self.assertIn("Time's up, sir!", ns.speaker.forced())
        self.assertTrue(ns.actions.called("screen_on"))
        self.assertTrue(any("Timer done" in body and "5 minutes" in body for _, body in ns.notifier.notes))
        self.assertIn("alert", ns.chime.played)
        self.assertEqual(e.snapshot().mode, "follow_up")

    def test_E07_v1_timer_table(self):
        for text, secs in V1_TIMER_CASES.items():
            with self.subTest(text=text):
                e, ns = self._mk()
                e.handle("cyrus " + text, "mic")
                views = e.timers.views()
                self.assertEqual(len(views), 1)
                self.assertEqual(e.timers.total_of(views[0].name), secs)

    def test_E08_cancel_timer_without_wake(self):
        e, ns = self._mk()
        e.handle("cyrus timer ten minutes", "mic")
        self.assertIn("timer", e.listen_spec().extra_words)
        e.handle("cancel timer", "mic")
        self.assertEqual(ns.speaker.said[-1], "Timer cancelled, sir.")
        self.assertFalse(e.timers.any())

    def test_E09_cancel_timer_without_timer_is_noise(self):
        e, ns = self._mk()
        e.handle("cancel timer", "mic")
        self.assertEqual(ns.speaker.said, [])
        self.assertEqual(e.snapshot().events[-1].kind, "noise")

    def test_E10_shutdown_confirm_then_cancel(self):
        e, ns = self._mk()
        e.handle("cyrus shut down", "mic")
        self.assertEqual(ns.speaker.said[-1], "Shut down, sir? Yes or no.")
        self.assertEqual(e.listen_spec().mode, "confirm")
        e.handle("yes", "mic")
        self.assertEqual(ns.actions.called("shutdown"), [(10,)])
        self.assertIn("Shutting down in 10 seconds, sir. Say cancel to stop it.", ns.speaker.said)
        self.assertEqual(e.snapshot().mode, "follow_up")
        self.assertIn("cancel", e.listen_spec().extra_words)
        e.handle("cancel", "mic")
        self.assertEqual(ns.actions.called("abort_shutdown"), [()])

    def test_E11_confirm_expires_silently(self):
        e, ns = self._mk()
        e.handle("cyrus shut down", "mic")
        n = len(ns.speaker.said)
        ns.clock.advance(13)
        e.tick()
        self.assertIsNone(e.snapshot().dialogue)
        self.assertEqual(len(ns.speaker.said), n)
        e.handle("yes", "mic")
        self.assertEqual(ns.actions.called("shutdown"), [])

    def test_E12_destructive_with_unk_is_confirmed(self):
        e, ns = self._mk()
        e.handle("cyrus shut [unk] down", "mic")
        self.assertEqual(ns.speaker.said[-1], "Did you say shut down, sir?")
        self.assertEqual(ns.actions.called("shutdown"), [])

    def test_E13_set_volume(self):
        for text in ("cyrus set volume to forty percent", "cyrus set volume two forty"):
            with self.subTest(text=text):
                e, ns = self._mk()
                e.handle(text, "mic")
                self.assertEqual(ns.actions.called("volume_set"), [(40,)])
                self.assertEqual(ns.speaker.said[-1], "Volume at 40 percent, sir.")

    def test_E14_bare_play_pause_resume_is_media(self):
        for text in ("cyrus play", "cyrus pause", "cyrus resume"):
            with self.subTest(text=text):
                e, ns = self._mk()
                e.handle(text, "mic")
                self.assertEqual(ns.actions.called("media"), [("play_pause",)])
                self.assertEqual(ns.actions.called("find_song"), [])

    def test_E15_play_song_free_redecode(self):
        acts = FakeActions(find_song=("Alone by Alan Walker", "https://www.youtube.com/watch?v=x"))
        e, ns = self._mk(actions=acts)
        e.handle("cyrus play eleven", "mic", free_text="cyrus play alone")
        self.assertEqual(acts.called("find_song"), [("alone",)])
        self.assertIn(("https://www.youtube.com/watch?v=x",), acts.called("open_target"))
        self.assertIn("Playing Alone by Alan Walker, sir.", ns.speaker.said)

    def test_E16_song_ask_flow(self):
        e, ns = self._mk()
        e.handle("cyrus play some music", "mic")
        self.assertEqual(ns.speaker.said[-1], "What should I play, sir?")
        self.assertEqual(e.listen_spec().mode, "free")
        n = len(ns.speaker.said)
        e.handle("how", "mic", free_text="how")
        self.assertEqual(len(ns.speaker.said), n)
        self.assertIsNotNone(e.snapshot().dialogue)
        e.handle("blinding lights", "mic", free_text="blinding lights")
        self.assertEqual(ns.actions.called("find_song"), [("blinding lights",)])

    def test_E17_song_ask_cancel(self):
        e, ns = self._mk()
        e.handle("cyrus play music", "mic")
        e.handle("cancel", "mic", free_text="cancel")
        self.assertEqual(ns.speaker.said[-1], "Okay, sir.")
        self.assertEqual(ns.actions.called("find_song"), [])

    def test_E18_youtube_offline(self):
        acts = FakeActions(find_song=OSError("offline"))
        e, ns = self._mk(actions=acts)
        e.handle("play alone", "typed")
        self.assertIn("I couldn't reach YouTube, sir. Opening the search instead.", ns.speaker.said)
        self.assertTrue(any("youtube.com/results" in a[0] for a in acts.called("open_target")))

    def test_E19_help_shows_commands(self):
        e, ns = self._mk()
        e.handle("cyrus what can you do", "mic")
        self.assertIn("show:Commands", drain_ui(ns.ui_queue))

    def test_E20_next_event_vs_next(self):
        e, ns = self._mk()
        e.handle("cyrus next event", "mic")
        self.assertEqual(ns.actions.called("media"), [])
        self.assertTrue(ns.speaker.said)
        e2, ns2 = self._mk()
        e2.handle("cyrus next", "mic")
        self.assertEqual(ns2.actions.called("media"), [("next",)])

    def test_E21_garbage_after_wake(self):
        e, ns = self._mk()
        e.handle("cyrus blah blah", "mic")
        self.assertEqual(ns.speaker.said[-1], "Sorry, I didn't catch that, sir.")
        snap = e.snapshot()
        self.assertEqual((snap.mode, snap.window_left_s), ("armed", 15))

    def test_E22_typed_without_wake(self):
        e, ns = self._mk()
        e.handle("open notepad", "typed")
        self.assertEqual(ns.actions.called("open_target"), [("notepad",)])
        self.assertEqual(ns.actions.called("abort_shutdown"), [])

    def test_E23_paused(self):
        e, ns = self._mk()
        e.set_paused(True)
        self.assertEqual(e.snapshot().listen, "paused")
        e.handle("cyrus what time is it", "mic")
        self.assertEqual(ns.speaker.said, [])
        e.handle("what time is it", "typed")
        self.assertTrue(ns.speaker.said[-1].startswith("It's "))

    def test_E25_calculator(self):
        e, ns = self._mk()
        e.handle("cyrus what is twelve times for", "mic")
        self.assertTrue(ns.speaker.said[-1].startswith("48"))


class EngineStubCommands(ECases, unittest.TestCase):
    def make(self, **kw):
        kw.setdefault("registry", stub_registry())
        kw.setdefault("store", StubStore())
        kw.setdefault("memory", StubMemory())
        return make_test_engine(**kw)


@unittest.skipUnless(HAVE_COMMANDS, "xyrus.commands (T2/T4) not present yet")
class EngineRealCommands(ECases, unittest.TestCase):
    def make(self, **kw):
        return make_test_engine(**kw)

    def _need(self, mod):
        if not _has(mod):
            self.skipTest(f"{mod} not present yet")

    def test_E15_play_song_free_redecode(self):
        self._need("xyrus.commands.web")
        super().test_E15_play_song_free_redecode()

    def test_E16_song_ask_flow(self):
        self._need("xyrus.commands.web")
        super().test_E16_song_ask_flow()

    def test_E17_song_ask_cancel(self):
        self._need("xyrus.commands.web")
        super().test_E17_song_ask_cancel()

    def test_E18_youtube_offline(self):
        self._need("xyrus.commands.web")
        super().test_E18_youtube_offline()

    def test_E26_briefing_afternoon(self):
        self._need("xyrus.assistant")
        self._need("xyrus.commands.assistant")
        e, ns = self._mk(now=dt.datetime(2026, 9, 13, 15, 0))
        ns.store.add_event("standup", dt.date(2026, 9, 13), dt.time(16, 0), source="ui")
        ns.store.add_event("gym", dt.date(2026, 9, 13), dt.time(19, 0), source="ui")
        e.handle("cyrus good morning", "mic")
        said = ns.speaker.said[-1]
        self.assertTrue(said.startswith("Good afternoon, sir."), said)
        self.assertLessEqual(len(re.findall(r"[.?!](\s|$)", said)), 4)


# ============================================================================= mechanics
class EngineMechanics(unittest.TestCase):
    def make(self, **kw):
        kw.setdefault("registry", stub_registry())
        kw.setdefault("store", StubStore())
        kw.setdefault("memory", StubMemory())
        return make_test_engine(**kw)

    def test_listen_spec_modes_and_generation(self):
        e, ns = self.make()
        s0 = e.listen_spec()
        self.assertEqual((s0.mode, s0.extra_words), ("wake", ()))
        e.handle("cyrus", "mic")
        s1 = e.listen_spec()
        self.assertEqual(s1.mode, "command")
        self.assertGreater(s1.generation, s0.generation)
        ns.clock.advance(16)
        e.tick()
        self.assertEqual(e.listen_spec().mode, "wake")
        e.handle("cyrus set a timer", "mic")
        spec = e.listen_spec()
        self.assertEqual(spec.mode, "when")
        self.assertIn("cancel", spec.extra_words)
        e.handle("five minutes", "mic")
        self.assertEqual(ns.speaker.said[-1], "Timer set for five minutes, sir.")
        self.assertIn("tea", e.listen_spec().extra_words)

    def test_armed_single_word_needs_wake(self):
        e, ns = self.make()
        e.handle("cyrus", "mic")
        e.handle("copy", "mic")
        self.assertEqual(ns.actions.called("keys"), [])
        self.assertEqual(e.snapshot().mode, "armed")          # noise does not consume the window
        e.handle("cyrus copy", "mic")
        self.assertEqual(ns.actions.called("keys"), [("ctrl+c",)])

    def test_armed_two_unmatched_words_not_heard(self):
        e, ns = self.make()
        e.handle("cyrus", "mic")
        e.handle("flub the grommet", "mic")
        self.assertEqual(ns.speaker.said[-1], "Sorry, I didn't catch that, sir.")
        self.assertEqual(e.snapshot().mode, "armed")

    def test_lone_unk_after_wake_is_bare_wake(self):
        e, ns = self.make()
        e.handle("cyrus [unk]", "mic")
        self.assertIn(ns.speaker.said[-1], ns.config.get("wake_replies"))
        e2, ns2 = self.make()
        e2.handle("cyrus [unk] [unk]", "mic")
        self.assertEqual(ns2.speaker.said[-1], "Sorry, I didn't catch that, sir.")

    def test_did_you_mean(self):
        e, ns = self.make()
        e.handle("cyrus lok screen", "mic")
        self.assertEqual(ns.speaker.said[-1], "Did you mean lock screen?")
        e.handle("yes", "mic")
        self.assertEqual(ns.speaker.said[-1], "Locking, sir.")
        self.assertEqual(ns.actions.called("lock"), [()])

    def test_handler_exception_says_action_failed(self):
        e, ns = self.make()
        e.handle("explode now", "typed")
        self.assertEqual(ns.speaker.said[-1], "That didn't work, sir. It's in the log.")

    def test_do_timeout_default_reply(self):
        acts = FakeActions(volume_get=TimeoutError("volume_get"))
        e, ns = self.make(actions=acts)
        e.handle("do something slow", "typed")
        self.assertEqual(ns.speaker.said[-1], "That's taking too long, sir. I've stopped waiting.")

    def test_routine_depth_limit(self):
        e, ns = self.make()
        e.handle("loop forever", "typed")
        self.assertEqual(ns.speaker.said[-1], "That routine calls itself too deeply, sir.")

    def test_transcript_noise_rules(self):
        e, ns = self.make()
        e.handle_transcript(Transcript(text="cyrus lock", mode="command", peak=0.001))
        self.assertEqual(ns.actions.calls, [])
        e.handle_transcript(Transcript(text="", mode="wake", peak=0.5))
        e.handle_transcript(Transcript(text="[unk]", mode="wake", peak=0.5))
        self.assertEqual(ns.speaker.said, [])
        e.handle_transcript(Transcript(text="cyrus lock", mode="command", peak=0.5, wake_redecoded=True))
        self.assertEqual(ns.actions.called("lock"), [()])

    def test_paused_cancels_confirm(self):
        e, ns = self.make()
        e.handle("cyrus shut down", "mic")
        self.assertIsNotNone(e.snapshot().dialogue)
        e.set_paused(True)
        self.assertIsNone(e.snapshot().dialogue)
        self.assertEqual(e.snapshot().status, "paused")

    def test_push_to_talk(self):
        e, ns = self.make()
        e.submit_hotkey("push_to_talk")
        snap = e.snapshot()
        self.assertEqual((snap.mode, snap.window_left_s), ("armed", 8))
        self.assertEqual(ns.chime.played, ["wake"])
        self.assertEqual(ns.speaker.said, [])
        e.submit_hotkey("show_window")
        self.assertIn("show", drain_ui(ns.ui_queue))

    def test_follow_up_phrase_alias_and_expiry(self):
        e, ns = self.make(config_overrides={"confirm_shutdown": False})
        e.handle("cyrus shutdown", "mic")
        e.handle("halt", "mic")                                 # (phrase, command) alias in the whitelist
        self.assertEqual(ns.actions.called("abort_shutdown"), [()])
        e.handle("cyrus shutdown", "mic")
        ns.clock.advance(16)
        e.tick()
        self.assertEqual(e.snapshot().mode, "idle")

    def test_timer_ringing_chimes_then_stops_after_a_minute(self):
        e, ns = self.make()
        e.handle("timer ten seconds", "typed")
        ns.clock.advance(10)
        e.tick()
        n0 = ns.chime.played.count("alert")
        for _ in range(4):
            ns.clock.advance(3)
            e.tick()
        self.assertGreater(ns.chime.played.count("alert"), n0)
        ns.clock.advance(60)
        e.tick()
        self.assertFalse(e.timers.ringing())

    def test_timer_ack_in_follow_up(self):
        e, ns = self.make()
        e.handle("timer ten seconds", "typed")
        ns.clock.advance(10)
        e.tick()
        self.assertTrue(e.timers.ringing())
        e.handle("okay", "mic")
        self.assertFalse(e.timers.ringing())

    def test_snapshot_shape(self):
        e, ns = self.make()
        e.handle("timer five minutes", "typed")
        snap = e.snapshot()
        self.assertEqual(snap.timers[0].name, "5 minute")
        self.assertEqual(snap.last_reply, "Timer set for five minutes, sir.")
        self.assertTrue(any(ev.kind == "cmd" for ev in snap.events))
        self.assertEqual(snap.store_version, ns.store.version)

    def test_honorific_blank(self):
        e, ns = self.make(config_overrides={"address_as": ""})
        e.handle("timer five minutes", "typed")
        self.assertEqual(ns.speaker.said[-1], "Timer set for five minutes.")

    def test_threaded_mode_round_trip(self):
        from xyrus.clock import SystemClock
        from xyrus.config import Config
        from xyrus.engine import Engine
        from xyrus.testing import FakeChime, FakeNotifier, FakeSpeaker
        import queue as q
        tmp = tempfile.mkdtemp()
        cfg = Config(os.path.join(tmp, "config.json")).load()
        ex = Executor()
        speaker = FakeSpeaker()
        acts = FakeActions()
        e = Engine(config=cfg, registry=stub_registry(), clock=SystemClock(), speaker=speaker, chime=FakeChime(),
                   actions=acts, executor=ex, store=StubStore(), memory=StubMemory(), notifier=FakeNotifier(),
                   ui_queue=q.Queue(), threaded=True)
        stop = threading.Event()
        t = threading.Thread(target=e.run_forever, args=(stop,), daemon=True)
        t.start()
        try:
            e.submit_text("set volume to thirty")
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and "Volume at 30 percent, sir." not in speaker.said:
                time.sleep(0.02)
            self.assertIn("Volume at 30 percent, sir.", speaker.said)
            self.assertEqual(acts.called("volume_set"), [(30,)])
            self.assertEqual(e.snapshot().last_reply, "Volume at 30 percent, sir.")
        finally:
            stop.set()
            ex.stop()
            t.join(2)


class EngineRegressions(unittest.TestCase):
    """Bugs reported by T2/T3/T4 + the free re-decode retry and meaning-layer hooks."""

    def make(self, **kw):
        kw.setdefault("registry", stub_registry())
        kw.setdefault("store", StubStore())
        kw.setdefault("memory", StubMemory())
        return make_test_engine(**kw)

    def test_single_word_stop_in_countdown_follow_up(self):
        for word in ("stop", "cancel"):
            with self.subTest(word=word):
                e, ns = self.make(config_overrides={"confirm_shutdown": False})
                e.handle("cyrus shut down", "mic")
                self.assertEqual(ns.actions.called("shutdown"), [(10,)])
                e.handle(word, "mic")
                self.assertEqual(ns.actions.called("abort_shutdown"), [()])

    def test_follow_up_kwarg_regression(self):
        e, ns = self.make()
        e.handle("fake alert", "typed")                          # ctx.follow_up(kind, whitelist, payload=)
        self.assertEqual(e.snapshot().mode, "follow_up")
        self.assertEqual(e.follow_kind, "alert")

    def test_payload_reaches_follow_up_handler(self):
        e, ns = self.make()
        e.seen_payload = None
        e.handle("fake alert", "typed")
        e.handle("snooze", "mic")
        self.assertEqual(e.seen_payload, "ALERT-1")
        self.assertEqual(ns.speaker.said[-1], "Snoozed, sir.")

    def test_confirm_dialogue_action_slot(self):
        e, ns = self.make()
        e.handle("cyrus shut down", "mic")
        self.assertEqual(e.snapshot().dialogue.slots.get("action"), "shutdown")

    @unittest.skipUnless(_has("xyrus.reminders") and _has("xyrus.store"), "T2 reminders/store not present")
    def test_two_alerts_in_one_tick_are_both_spoken(self):
        e, ns = make_test_engine(registry=stub_registry())
        ns.store.add_event("dentist", dt.date(2026, 9, 13), dt.time(10, 40), reminder_min=10, source="ui")
        ns.store.add_event("standup", dt.date(2026, 9, 13), dt.time(10, 40), reminder_min=10, source="ui")
        e.tick()
        forced = ns.speaker.forced()
        self.assertEqual(len(forced), 2, forced)
        self.assertTrue(any("dentist" in f.lower() for f in forced))
        self.assertTrue(any("standup" in f.lower() for f in forced))
        self.assertEqual(len(ns.notifier.notes), 2)
        self.assertEqual(e.snapshot().mode, "follow_up")

    @unittest.skipUnless(_has("xyrus.assistant") and _has("xyrus.store"), "T2 assistant/store not present")
    def test_startup_greeting_before_morning_brief(self):
        e, ns = make_test_engine(registry=stub_registry(), now=dt.datetime(2026, 9, 13, 9, 0))
        ns.store.add_event("standup", dt.date(2026, 9, 13), dt.time(10, 0), source="ui")
        e.startup()
        e.tick()
        ns.clock.advance(10)
        e.tick()
        self.assertEqual(len(ns.speaker.said), 1, ns.speaker.said)
        self.assertTrue(ns.speaker.said[0].startswith("Good morning"))

    def _retry_engine(self, sync: bool):
        e, ns = self.make()
        calls = []

        def hook(tr, cb):
            calls.append(tr)
            if sync:
                cb(e.pending_free_text)
            else:
                e.pending_cb = cb
            return True
        e.request_free_decode = hook
        return e, ns, calls

    def test_free_decode_retry_async_and_sync(self):
        for sync in (False, True):
            with self.subTest(sync=sync):
                e, ns, calls = self._retry_engine(sync)
                e.pending_free_text = "cyrus lock screen"
                e.handle_transcript(Transcript(text="cyrus flub grommet", mode="command", peak=0.5))
                self.assertEqual(len(calls), 1)
                if not sync:
                    self.assertEqual(ns.speaker.said, [])            # waiting for the re-decode
                    e.pending_cb("cyrus lock screen")
                self.assertEqual(ns.speaker.said[-1], "Locking, sir.")
                self.assertEqual(ns.actions.called("lock"), [()])

    def test_free_decode_retry_empty_says_not_heard(self):
        e, ns, calls = self._retry_engine(sync=True)
        e.pending_free_text = ""
        e.handle_transcript(Transcript(text="cyrus flub grommet", mode="command", peak=0.5))
        self.assertEqual(ns.speaker.said[-1], "Sorry, I didn't catch that, sir.")

    def test_no_hook_says_not_heard(self):
        e, ns = self.make()
        e.handle_transcript(Transcript(text="cyrus flub grommet", mode="command", peak=0.5))
        self.assertEqual(ns.speaker.said[-1], "Sorry, I didn't catch that, sir.")

    def test_garbled_search_triggers_retry(self):
        for heard in ("cyrus search for fourteen [unk]", "cyrus search fourteen [unk]"):
            with self.subTest(heard=heard):
                e, ns, calls = self._retry_engine(sync=False)
                e.handle_transcript(Transcript(text=heard, mode="command", peak=0.5))
                self.assertEqual(len(calls), 1)
                self.assertEqual(ns.actions.called("open_target"), [])
                e.pending_cb("cyrus search for cheap keyboards")
                self.assertEqual(ns.actions.called("open_target"),
                                 [("https://www.google.com/search?q=cheap+keyboards",)])

    def test_playback_mishearing_uses_free_text(self):
        acts = FakeActions(find_song=("Believer by Imagine Dragons", "https://www.youtube.com/watch?v=b"))
        e, ns = self.make(actions=acts)
        e.handle("cyrus playback [unk]", "mic", free_text="cyrus play believer by imagine dragons")
        self.assertEqual(acts.called("find_song"), [("believer by imagine dragons",)])
        self.assertIn("Playing Believer by Imagine Dragons, sir.", ns.speaker.said)

    def test_free_text_fallback_without_retry(self):
        e, ns = self.make()
        e.handle("cyrus [unk] [unk]", "mic", free_text="cyrus can you lock the computer")
        self.assertEqual(ns.actions.called("lock"), [()])

    def test_meaning_paraphrase_through_engine(self):
        e, ns = self.make()
        e.handle("cyrus could you please set the volume to thirty for me", "mic")
        self.assertEqual(ns.actions.called("volume_set"), [(30,)])
        e.handle("cyrus shut down chrome", "mic")
        self.assertEqual(ns.actions.called("shutdown"), [])
        self.assertNotIn("Shut down, sir? Yes or no.", ns.speaker.said)


class ExecutorTests(unittest.TestCase):
    def test_result_and_error(self):
        ex = Executor(watch_period=0.02)
        try:
            got, done = [], threading.Event()
            ex.submit(lambda: 41 + 1, on_done=lambda r, e: (got.append((r, e)), done.set()))
            self.assertTrue(done.wait(2))
            self.assertEqual(got, [(42, None)])
            got.clear()
            done.clear()

            def bad():
                raise OSError("nope")
            ex.submit(bad, on_done=lambda r, e: (got.append((r, e)), done.set()))
            self.assertTrue(done.wait(2))
            self.assertIsInstance(got[0][1], OSError)
        finally:
            ex.stop()

    def test_timeout_abandons_worker_and_replaces_it(self):
        ex = Executor(watch_period=0.02)
        release = threading.Event()
        try:
            results, t_done = [], threading.Event()
            ex.submit(lambda: release.wait(5), timeout=0.2, name="stuck",
                      on_done=lambda r, e: (results.append(e), t_done.set()))
            self.assertTrue(t_done.wait(2))
            self.assertIsInstance(results[0], TimeoutError)
            self.assertEqual(str(results[0]), "stuck")
            self.assertEqual(ex.abandoned, 1)
            out, d2 = [], threading.Event()
            ex.submit(lambda: "fresh", on_done=lambda r, e: (out.append(r), d2.set()))
            self.assertTrue(d2.wait(2))
            self.assertEqual(out, ["fresh"])
            release.set()
            time.sleep(0.15)
            self.assertEqual(len(results), 1)            # the late result is never delivered twice
        finally:
            release.set()
            ex.stop()

    def test_one_job_at_a_time(self):
        ex = Executor(watch_period=0.02)
        try:
            running, peak, lock = [0], [0], threading.Lock()
            done = threading.Semaphore(0)

            def job():
                with lock:
                    running[0] += 1
                    peak[0] = max(peak[0], running[0])
                time.sleep(0.02)
                with lock:
                    running[0] -= 1
            for _ in range(5):
                ex.submit(job, on_done=lambda r, e: done.release())
            for _ in range(5):
                self.assertTrue(done.acquire(timeout=2))
            self.assertEqual(peak[0], 1)
        finally:
            ex.stop()

    def test_inline(self):
        ex = InlineExecutor()
        got = []
        ex.submit(lambda: 5, name="five", on_done=lambda r, e: got.append((r, e)))
        self.assertEqual((got, ex.jobs), ([(5, None)], ["five"]))


class LogSetup(unittest.TestCase):
    def test_setup_writes_rotating_log_and_hooks(self):
        import logging
        import sys
        from xyrus import log as xlog
        tmp = Path(tempfile.mkdtemp())
        try:
            xlog.setup(log_file=tmp / "arc.log", crash_file=tmp / "crash.log", data_dir=tmp)
            xlog.setup(log_file=tmp / "arc.log", crash_file=tmp / "crash.log", data_dir=tmp)   # idempotent
            logging.getLogger("xyrus.test").info("hello from the test")
            try:
                raise ValueError("boom in a thread")
            except ValueError:
                sys.excepthook(*sys.exc_info())
            t = threading.Thread(target=lambda: 1 / 0, name="T-test")
            t.start()
            t.join()
            text = (tmp / "arc.log").read_text(encoding="utf8")
            self.assertIn("xyrus.test: hello from the test", text)
            self.assertIn("ValueError: boom in a thread", text)
            self.assertIn("uncaught exception in thread T-test", text)
            self.assertEqual(text.count("hello from the test"), 1)    # one handler, not two
            self.assertEqual(logging.getLogger("comtypes").level, logging.WARNING)
            self.assertTrue((tmp / "crash.log").exists())
        finally:
            xlog.teardown()


class G8NoWallClock(unittest.TestCase):
    FILES = ("engine.py", "dialogue.py", "timers.py", "reminders.py", "assistant.py", "persona.py")
    BAD = re.compile(r"\btime\.time\(|\bdatetime\.now\(|\bdt\.datetime\.now\(|\bdatetime\.today\(|"
                     r"\bdate\.today\(|\btime\.monotonic\(")

    def test_g8_modules_use_the_clock(self):
        checked = 0
        for name in self.FILES:
            path = BASE / "xyrus" / name
            if not path.exists():
                continue
            checked += 1
            for n, line in enumerate(path.read_text(encoding="utf8").splitlines(), 1):
                code = line.split("#", 1)[0]
                self.assertIsNone(self.BAD.search(code), f"{name}:{n} reads the wall clock: {line.strip()}")
        self.assertGreaterEqual(checked, 4)


class HearingRebuildHooks(unittest.TestCase):
    """The recognizer calls these by name (interfaces.ON_*); a faint / unsure destructive command asks first."""

    def cap_engine(self):
        e, ns = make_test_engine(config_overrides={"command_window_s": 0})
        e.capture_available = lambda: True
        return e, ns

    def test_hooks_defined(self):
        from xyrus.interfaces import ON_CAPTURE_TIMEOUT, ON_WAKE_WITH_COMMAND
        e, ns = self.cap_engine()
        self.assertTrue(callable(getattr(e, ON_CAPTURE_TIMEOUT, None)))
        self.assertTrue(callable(getattr(e, ON_WAKE_WITH_COMMAND, None)))
        self.assertFalse(e.listen_spec().playback)

    def test_low_confidence_destructive_confirms(self):
        from xyrus.capture import CapturedTranscript
        e, ns = self.cap_engine()
        e.handle("xyrus", "typed")
        e.handle_transcript(CapturedTranscript(text="shut down", mode="command", free_text="shut down", peak=0.3,
                                               confidence=-0.6))
        self.assertFalse(ns.actions.called("shutdown"))
        self.assertIsNotNone(e.dialogue)
        self.assertEqual(ns.speaker.said[-1], persona.render(R.CONFIRM_POWER_HEARD, ns.config, phrase="shut down"))


if __name__ == "__main__":
    unittest.main()
