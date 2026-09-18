"""registry.py (§4.5): compilation, selection ordering, scopes, free slots, Commands-tab data."""
import datetime as dt
import os
import tempfile
import unittest

os.environ.setdefault("XYRUS_DATA_DIR", tempfile.mkdtemp(prefix="xyrus_test_"))

from xyrus.config import Config
from xyrus.parsing import SlotEnv
from xyrus.registry import REGISTRY, Registry, compile_pattern, command


def noop(ctx, m):
    pass


def build():
    """A registry with the §4.5 ordering-example phrases (and a few more)."""
    r = Registry()
    r.command("cancel_shutdown", "cancel", "abort", "cancel shutdown", "cancel the shutdown",
              section="Power", help="aborts a shutdown")(noop)
    r.command("cancel_timer", "cancel timer", "stop timer", "cancel the timer", "cancel the <name> timer",
              section="Timers", help="cancels a timer", wake_free=True,
              context=lambda e: bool(e and e.get("timer")), follow_up=True)(noop)
    r.command("media_next", "next", "next track", "skip", section="Sound & media", help="next track",
              armed_ok_single_word=False)(noop)
    r.command("next_event", "next event", "what's next", section="Calendar & reminders", help="next event",
              follow_up=True)(noop)
    r.command("time", "what time is it", "what is the time", "what's the time", section="Info & tools",
              help="time")(noop)
    r.command("volume_query", "what's the volume", "what is the volume", section="Sound & media",
              help="volume")(noop)
    r.command("calc", "what is <expr>", "what's <expr>", "calculate <expr>", section="Info & tools",
              help="calculator")(noop)
    r.command("media_play", "play", "pause", "resume", "play pause", section="Sound & media", help="play")(noop)
    r.command("play_song", "play <song>", "play <song> on youtube", section="Apps & web", help="song")(noop)
    r.command("show_calendar", "show my calendar", "open my calendar", section="Calendar & reminders",
              help="calendar")(noop)
    r.command("open_app", "open <app>", "launch <app>", "start <app>", section="Apps & web", help="open")(noop)
    r.command("close_tab", "close tab", section="Keys & clipboard", help="ctrl+w")(noop)
    r.command("close_app", "close <app>", "quit <app>", section="Display & windows", help="close app")(noop)
    r.command("delete_event", "delete the <text> event", "cancel the <text> event", "cancel the <text> meeting",
              section="Calendar & reminders", help="delete")(noop)
    r.command("timer_missing", "set a timer", "set timer", "timer", "start a timer", section="Timers",
              help="asks how long")(noop)
    r.command("timer_set", "set a timer for <duration>", "set timer for <duration>", "set timer <duration>",
              "timer for <duration>", "timer <duration>", "<duration> timer", section="Timers",
              help="sets a timer")(noop)
    r.command("set_volume", "set [the] volume to <percent>", "volume <percent>", "set volume <percent>",
              section="Sound & media", help="volume")(noop)
    r.command("shutdown", "shut down", "shutdown", "power off", section="Power", help="shutdown",
              destructive=True)(noop)
    r.command("pick", "pick a number between <number> and <number>", section="Info & tools", help="pick")(noop)
    r.command("search", "search for <query>", "search the web for <query>", section="Apps & web",
              help="search")(noop)
    r.command("thanks", "thank you", "thanks", section="Basics", help="thanks", follow_up=True)(noop)
    return r


class CompileTests(unittest.TestCase):
    def test_optional_and_alternatives(self):
        p = compile_pattern("set [the] volume to <percent>")
        self.assertEqual(p.slots, ("percent",))
        self.assertEqual(p.literal_count, 3)
        self.assertIsNone(p.free_slot)
        self.assertTrue(p.regex.match("set volume to forty"))
        self.assertTrue(p.regex.match("set the volume to forty"))
        self.assertFalse(p.regex.match("set thevolume to forty"))
        q = compile_pattern("roll a|the dice")
        self.assertEqual(q.literal_count, 3)
        self.assertTrue(q.regex.match("roll a dice") and q.regex.match("roll the dice"))
        self.assertFalse(q.regex.match("roll dice"))
        o = compile_pattern("[a|an] event [now]")
        self.assertEqual(o.literal_count, 1)
        for t in ("event", "a event", "an event now", "event now"):
            self.assertTrue(o.regex.match(t), t)

    def test_free_slot(self):
        self.assertEqual(compile_pattern("play <song>").free_slot, "song")
        self.assertEqual(compile_pattern("play <song> on spotify").free_slot, "song")
        self.assertEqual(compile_pattern("open <app>").free_slot, "app")
        self.assertIsNone(compile_pattern("timer <duration>").free_slot)

    def test_invalid(self):
        for bad in ("<duration>", "", "open <nosuchslot>", "[maybe]"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                compile_pattern(bad)
        compile_pattern("<duration> timer")          # allowed: a literal follows the slot

    def test_whole_words_only(self):
        p = compile_pattern("no")
        self.assertFalse(p.regex.match("open notepad"))
        self.assertTrue(p.regex.match("oh no"))


class MatchTests(unittest.TestCase):
    def setUp(self):
        self.r = build()
        self.cfg = Config(os.path.join(tempfile.mkdtemp(), "config.json")).load()
        self.env = SlotEnv(config=self.cfg, now=dt.datetime(2026, 9, 13, 10, 30))

    def m(self, text, **kw):
        kw.setdefault("env", self.env)
        return self.r.match(text, **kw)

    def name(self, text, **kw):
        hit = self.m(text, **kw)
        return hit.command.name if hit else None

    def test_ordering_examples(self):
        cases = {
            "cancel timer": "cancel_timer", "cancel": "cancel_shutdown",
            "next event": "next_event", "next": "media_next",
            "what is the time": "time", "what is twelve times four": "calc",
            "play pause": "media_play", "play alone": "play_song", "play": "media_play",
            "open my calendar": "show_calendar", "open chrome": "open_app",
            "close tab": "close_tab", "close chrome": "close_app",
            "cancel the dentist event": "delete_event",
            "set a timer": "timer_missing", "set a timer for five minutes": "timer_set",
            "what's the volume": "volume_query", "what's twelve times four": "calc",
            "five minute timer": "timer_set", "timer": "timer_missing",
        }
        for text, want in cases.items():
            with self.subTest(text=text):
                self.assertEqual(self.name(text), want)

    def test_slots(self):
        self.assertEqual(self.m("set a timer for five minutes").slots, {"duration": 300})
        self.assertEqual(self.m("set volume two forty").slots, {"percent": 40})
        self.assertEqual(self.m("set the volume to forty percent").slots, {"percent": 40})
        hit = self.m("pick a number between one and ten")
        self.assertEqual(hit.slots, {"number": 1, "number2": 10})
        self.assertEqual(self.m("cancel the tea timer").slots, {"name": "tea"})
        app = self.m("open task manager").slots["app"]
        self.assertEqual(app.target, "taskmgr")

    def test_extra_words(self):
        hit = self.m("please lock the timer set a timer for five minutes now")
        self.assertEqual(hit.command.name, "timer_set")
        hit = self.m("uh cancel timer")
        self.assertEqual((hit.command.name, hit.extra_words), ("cancel_timer", 1))

    def test_unk_destructive(self):
        hit = self.m("shut [unk] down")
        self.assertEqual(hit.command.name, "shutdown")
        self.assertTrue(hit.has_unk)
        hit = self.m("shut down")
        self.assertFalse(hit.has_unk)
        self.assertTrue(self.m("[unk] shut down").has_unk)

    def test_free_song(self):
        hit = self.m("play eleven", free_text="cyrus play alone")
        self.assertEqual((hit.command.name, hit.slots["song"]), ("play_song", "alone"))
        hit = self.m("play [unk] [unk]", free_text="sirius lay shape of you on youtube")
        self.assertEqual(hit.slots["song"], "shape of you")
        self.assertEqual(self.name("play [unk]"), "media_play")   # no free text: slot empty -> bare play
        self.assertEqual(self.m("play alone").slots["song"], "alone")  # typed path

    def test_free_query(self):
        hit = self.m("search for [unk] [unk]", free_text="cyrus search for cheap mechanical keyboards")
        self.assertEqual(hit.slots["query"], "cheap mechanical keyboards")
        hit = self.m("search the web for [unk]", free_text="search the web for weather in dhaka")
        self.assertEqual(hit.slots["query"], "weather in dhaka")

    def test_app_free_only_with_unk(self):
        from xyrus.testing import FakeAppIndex
        env = SlotEnv(config=self.cfg, now=self.env.now, app_index=FakeAppIndex({"obs studio": "C:/obs.lnk"}))
        hit = self.r.match("open [unk] [unk]", free_text="cyrus open obs studio", env=env)
        self.assertEqual(hit.slots["app"].target, "C:/obs.lnk")
        hit = self.r.match("open chrome", free_text="cyrus open crow", env=env)
        self.assertEqual(hit.slots["app"].target, "chrome")

    def test_scopes(self):
        self.assertIsNone(self.m("cancel timer", scope="idle", engine={"timer": False}))
        self.assertEqual(self.name("cancel timer", scope="idle", engine={"timer": True}), "cancel_timer")
        self.assertIsNone(self.m("lock", scope="idle", engine={"timer": True}))
        self.assertEqual(self.name("thanks", scope="follow_up", follow_up=["thanks"]), "thanks")
        self.assertIsNone(self.m("thanks", scope="follow_up", follow_up=["next_event"]))
        self.assertEqual(self.name("what's next", scope="follow_up", follow_up=["next_event"]), "next_event")
        # pattern-text whitelist entries allow only those patterns
        self.assertIsNone(self.m("next event", scope="follow_up", follow_up=["what's next"]))
        # follow_up=False commands never match in follow-up scope
        self.assertIsNone(self.m("cancel", scope="follow_up", follow_up=["cancel_shutdown"]))

    def test_matcher(self):
        r = build()
        r.register_matcher = None
        from xyrus.registry import Command
        r.register(Command(name="add_event", patterns=(compile_pattern("add an event"),), handler=noop,
                           section="Calendar & reminders", help="adds",
                           matcher=lambda t: {"kw": True} if "add" in t and "event" in t else None,
                           matcher_score=3))
        hit = r.match("add in the event tomorrow", env=self.env, free_text="add an event dentist tomorrow")
        self.assertEqual(hit.command.name, "add_event")
        self.assertIsNone(hit.pattern)
        self.assertEqual(hit.slots["free_text"], "add an event dentist tomorrow")
        self.assertEqual(r.match("add an event", env=self.env).pattern.text, "add an event")

    def test_close_phrase(self):
        self.assertEqual(self.r.close_phrase("shut dawn"), "shut down")
        self.assertEqual(self.r.close_phrase("cancel thetimer"), "cancel the timer")
        self.assertEqual(self.r.close_phrase("vollume up"), None)          # no such phrase registered here
        self.assertIsNone(self.r.close_phrase("blah blah"))
        self.assertIsNone(self.r.close_phrase("[unk]"))


def spec_registry():
    """§3 phrases (display text exactly as the spec registers them) for the meaning-layer tests."""
    r = Registry()
    rows = {
        "vol_up": ("volume up", "louder", "turn it up"),
        "vol_down": ("volume down", "quieter", "turn it down"),
        "vol_by_up": ("volume up by <number>",), "vol_by_down": ("volume down by <number>",),
        "set_volume": ("set [the] volume to <percent>", "volume <percent>", "set volume <percent>"),
        "vol_max": ("volume max", "full volume", "max volume", "maximum volume"),
        "vol_query": ("what's the volume", "what is the volume", "volume level"),
        "mute": ("mute", "mute the sound"), "unmute": ("un mute", "sound on", "turn the sound on"),
        "media_play": ("play", "pause", "resume", "play pause", "pause the music", "resume the music", "pause it"),
        "media_next": ("next", "next track", "next song", "skip"),
        "media_prev": ("previous", "previous track", "previous song", "go back"),
        "media_stop": ("stop the music", "stop playback"),
        "set_brightness": ("set [the] brightness to <percent>", "brightness <percent>"),
        "bright_up": ("brightness up", "brighter"), "bright_down": ("brightness down", "dimmer"),
        "dim_screen": ("dim the screen",),
        "bright_query": ("what's the brightness", "brightness level"),
        "open_app": ("open <app>", "launch <app>", "start <app>"),
        "close_app": ("close <app>", "quit <app>", "exit <app>"),
        "switch_app": ("switch to <app>", "go to <app>", "bring up <app>"),
        "close_window": ("close window", "close this", "close this window"),
        "time": ("what time is it", "what's the time", "what is the time", "the time", "time"),
        "date": ("what day is it", "what's the date", "what is the date", "the date", "what's today"),
        "timer_set": ("set a timer for <duration>", "set timer for <duration>", "set timer <duration>",
                      "timer for <duration>", "timer <duration>", "start a timer for <duration>", "<duration> timer"),
        "timer_missing": ("set a timer", "timer", "start a timer"),
        "screenshot": ("take a screenshot", "screenshot", "take a picture of the screen"),
        "screen_off": ("screen off", "display off", "monitor off", "turn off the screen"),
        "screen_on": ("wake up", "screen on", "display on", "turn on the screen"),
        "lock": ("lock", "lock screen", "lock the pc", "lock the computer"),
        "sleep": ("go to sleep", "sleep", "sleep mode"),
        "search": ("search for <query>", "google <query>", "search the web for <query>"),
        "play_song": ("play <song>", "play <song> on youtube"),
        "show_calendar": ("show my calendar", "open my calendar", "show the calendar"),
        "next_event": ("what's next", "next event", "what's my next event"),
        "joke": ("tell me a joke", "joke", "tell me another"),
        "help": ("what can you do", "help", "what are your commands"),
    }
    for name, phrases in rows.items():
        r.command(name, *phrases, section="Basics", help=name)(noop)
    r.command("shutdown", "shut down", "shutdown", "power off", "turn off the pc", "turn off the computer",
              section="Power", help="shutdown", destructive=True)(noop)
    r.command("restart", "restart", "reboot", "restart the pc", "restart the computer", section="Power",
              help="restart", destructive=True)(noop)
    r.command("force_close", "force close <app>", "kill <app>", section="Display & windows", help="kill",
              destructive=True)(noop)
    return r


PARAPHRASES = {   # at least 8 ways per common intent (lead requirement); v1 test_meaning.py re-expressed
    "vol_down": ["lower the volume", "can you lower the volume", "lower sound", "lower the sound",
                 "decrease the sound", "reduce the volume please", "make it quieter", "turn the music down",
                 "could you turn the volume down", "it's too loud", "drop the volume", "less volume",
                 "softer please", "can you decrease the volume for me", "the sound is too loud",
                 "bring the volume down", "the volume, lower it", "volume lower"],
    "vol_up": ["raise the volume", "increase the sound", "make it louder", "louder please", "i can't hear it",
               "boost the volume", "turn the music up", "pump it up", "it's too quiet", "can you increase the volume",
               "more volume", "turn up the sound", "raise the sound"],
    "set_volume": ["set the volume to thirty", "volume forty percent", "put the volume at fifty",
                   "make the volume twenty five percent", "turn the volume up to eighty", "change the volume to sixty",
                   "can you set the volume to seventy", "set sound to forty", "volume at ten"],
    "vol_max": ["volume max", "full volume", "turn the volume all the way up", "maximum volume please",
                "make it as loud as possible", "set the volume to maximum", "max the volume", "volume to the max"],
    "vol_query": ["what's the volume", "how loud is it", "tell me the volume level", "what is the volume now",
                  "what's volume", "current volume", "volume level", "how loud is the sound"],
    "mute": ["mute", "mute the sound", "turn off the sound", "sound off", "silence please", "no sound",
             "switch off the sound", "turn the sound off", "mute the volume", "kill the sound"],
    "unmute": ["un mute", "turn the sound back on", "sound on", "turn on the sound", "bring back the sound",
               "switch on the sound", "unmute", "turn the volume back on"],
    "bright_up": ["increase the brightness", "make the screen brighter", "turn up the brightness", "brightness up",
                  "raise the brightness", "brighter please", "screen brighter", "can you increase brightness"],
    "bright_down": ["lower the brightness", "decrease brightness", "turn the brightness down",
                    "make the screen darker", "dimmer", "reduce the brightness", "brightness down",
                    "the brightness, lower it"],
    "set_brightness": ["set brightness to seventy", "brightness fifty", "change the brightness to forty",
                       "put the brightness at thirty", "set the brightness to sixty percent",
                       "can you set brightness to eighty", "make the brightness twenty", "brightness at ninety"],
    "bright_query": ["what's the brightness", "how bright is the screen", "brightness level", "current brightness",
                     "what is the brightness", "tell me the brightness", "what's the brightness level",
                     "how bright is it"],
    "open_app": ["open chrome", "can you open chrome", "launch spotify", "start notepad", "open google chrome",
                 "fire up youtube", "please open the calculator", "run task manager",
                 "could you open the calculater for me", "load notepad"],
    "close_app": ["close chrome", "quit spotify", "exit notepad", "can you close chrome", "close down discord",
                  "please quit chrome", "close the calculator", "exit the calculator app"],
    "time": ["what time is it", "what's the time", "tell me the time", "do you know what time it is",
             "time please", "what is the time now", "current time", "can you tell me the time"],
    "date": ["what's the date", "what day is it", "what's today's date", "tell me the date",
             "which day is it today", "what is today", "what's the date today", "today's date please"],
    "timer_set": ["set a timer for five minutes", "can you set a timer for five minutes",
                  "start a countdown for ten minutes", "ping me in five minutes", "alert me in ten minutes",
                  "set an alarm for twenty minutes", "five minute timer", "timer for 5 minutes",
                  "please set a timer for three minutes"],
    "screenshot": ["take a screenshot", "screenshot", "capture the screen", "take a picture of the screen",
                   "grab the screen", "screen shot please", "print screen", "can you take a screenshot",
                   "snap the screen"],
    "shutdown": ["shut down", "shutdown the computer", "turn off the computer", "switch off the pc", "power off",
                 "please shut down", "power down the laptop", "could you shut down the computer"],
    "screen_off": ["turn off the screen", "screen off", "switch off the monitor", "turn the display off",
                   "put the screen to sleep", "monitor off", "turn off the display", "switch the screen off"],
    "screen_on": ["turn on the screen", "wake the screen", "switch on the display", "screen on",
                  "turn the monitor on", "wake up", "display on", "turn the screen back on"],
    "media_play": ["pause", "pause the music", "resume", "resume the video", "keep playing", "pause the song",
                   "can you pause the music", "unpause"],
    "media_next": ["next song", "skip this song", "skip", "next track", "play the next song",
                   "skip the track", "next video", "can you skip this song"],
    "switch_app": ["switch to chrome", "switch two chrome", "go to youtube", "bring up notepad",
                   "can you switch to chrome", "please switch to spotify", "go two notepad", "switch too chrome"],
}


class MeaningTests(unittest.TestCase):
    """Commands work by MEANING (lead requirement): fillers, synonyms and word order don't matter."""

    def setUp(self):
        self.r = spec_registry()
        self.cfg = Config(os.path.join(tempfile.mkdtemp(), "config.json")).load()
        self.env = SlotEnv(config=self.cfg, now=dt.datetime(2026, 9, 13, 10, 30))

    def m(self, text):
        from xyrus import normalize as N
        return self.r.match(N.normalize(text), env=self.env)

    def test_paraphrases(self):
        for want, phrases in PARAPHRASES.items():
            self.assertGreaterEqual(len(phrases), 8, want)
            for p in phrases:
                with self.subTest(phrase=p):
                    hit = self.m(p)
                    self.assertIsNotNone(hit, f"{p!r} matched nothing (want {want})")
                    self.assertEqual(hit.command.name, want, p)

    def test_slot_values_survive(self):
        cases = {"set the volume to thirty": ("percent", 30), "put the volume at fifty": ("percent", 50),
                 "turn the volume up to eighty": ("percent", 80), "timer for 5 minutes": ("duration", 300),
                 "start a countdown for ten minutes": ("duration", 600), "ping me in five minutes": ("duration", 300),
                 "set an alarm for twenty minutes": ("duration", 1200), "brightness at ninety": ("percent", 90)}
        for text, (slot, want) in cases.items():
            with self.subTest(text=text):
                self.assertEqual(self.m(text).slots[slot], want)
        self.assertEqual(self.m("could you open the calculater for me").slots["app"].target, "calc")
        self.assertEqual(self.m("run task manager").slots["app"].target, "taskmgr")
        hit = self.m("lower the volume by twenty")
        self.assertEqual((hit.command.name, hit.slots["number"]), ("vol_by_down", 20))
        hit = self.m("increase the volume by ten")
        self.assertEqual((hit.command.name, hit.slots["number"]), ("vol_by_up", 10))

    def test_step_modifier(self):
        hit = self.m("lower the volume a bit")
        self.assertEqual((hit.command.name, hit.slots.get("step")), ("vol_down", "small"))
        hit = self.m("turn it up a lot")
        self.assertEqual((hit.command.name, hit.slots.get("step")), ("vol_up", "large"))
        self.assertEqual(self.m("raise the sound slightly").slots.get("step"), "small")

    def test_object_decides_never_shutdown(self):
        for text, want in [("turn off the sound", "mute"), ("turn off the screen", "screen_off"),
                           ("switch off the display", "screen_off"), ("turn off the music", "media_play")]:
            with self.subTest(text=text):
                self.assertEqual(self.m(text).command.name, want)
        for text in ("turn off the lights", "shut down chrome", "turn off wifi", "restart chrome",
                     "turn on the lights", "please shut down chrome now"):
            with self.subTest(text=text):
                hit = self.m(text)
                self.assertTrue(hit is None or hit.command.name not in ("shutdown", "restart"), (text, hit))
        hit = self.m("turn [unk] off")
        self.assertTrue(hit is None or hit.has_unk)            # D11: confirmed by the engine, never direct

    def test_t3_cases(self):
        self.assertEqual(self.m("switch two chrome").command.name, "switch_app")
        self.assertEqual(self.m("switch two chrome").slots["app"].target, "chrome")
        self.assertEqual(self.m("what's volume").command.name, "vol_query")

    def test_exact_still_wins(self):
        self.assertEqual(self.m("open my calendar").command.name, "show_calendar")
        self.assertEqual(self.m("dim the screen").command.name, "dim_screen")
        self.assertEqual(self.m("next event").command.name, "next_event")
        self.assertEqual(self.m("play alone").slots["song"], "alone")
        self.assertEqual(self.m("search for the best pizza near me").slots["query"], "the best pizza near me")
        self.assertEqual(self.m("look up the best pizza").slots["query"], "the best pizza")

    def test_meaning_not_used_outside_full_scope(self):
        self.assertIsNone(self.r.match("can you lower the volume", scope="idle", env=self.env, engine=None))

    def test_grammar_words_include_synonyms(self):
        words = self.r.literal_words()
        for w in ("lower", "decrease", "reduce", "sound", "audio", "launch", "quit", "countdown"):
            self.assertIn(w, words)


class RegistryDataTests(unittest.TestCase):
    def test_sections_and_counts(self):
        r = build()
        secs = r.sections(None)
        names = [s for s, _ in secs]
        self.assertEqual(names[-1], "Slots")
        order = ["Basics", "Power", "Sound & media", "Display & windows", "Apps & web", "Keys & clipboard",
                 "Timers", "Calendar & reminders", "Info & tools"]
        self.assertEqual([n for n in names if n in order], order)
        timers = dict(secs)["Timers"]
        self.assertTrue(any("works without the wake word" in h for _, h in timers))
        self.assertIn(("set a timer / set timer / timer / start a timer", "asks how long"), timers)
        legend = dict(dict(secs)["Slots"])
        self.assertIn("<duration>", legend)
        n_cmds, n_pats = r.counts()
        self.assertEqual(n_cmds, len(r.commands()))
        self.assertEqual(n_pats, sum(len(c.patterns) for c in r.commands()))

    def test_version_and_custom(self):
        r = build()
        v = r.version()
        r.command("movie_mode", "movie mode", section="Your commands", help="routine", custom=True)(noop)
        self.assertGreater(r.version(), v)
        self.assertEqual(dict(r.sections(None))["Your commands"], [("movie mode", "routine")])
        v = r.version()
        r.unregister_custom()
        self.assertGreater(r.version(), v)
        self.assertIsNone(r.get("movie_mode"))

    def test_reregister_is_idempotent(self):
        r = build()
        n = len(r.commands())
        first = [c.name for c in r.commands()]
        r.command("time", "what time is it", section="Info & tools", help="time")(noop)
        self.assertEqual(len(r.commands()), n)
        self.assertEqual([c.name for c in r.commands()], first)

    def test_words_and_triggers(self):
        r = build()
        words = r.literal_words()
        for w in ("cancel", "timer", "volume", "the", "what's", "play"):
            self.assertIn(w, words)
        self.assertNotIn("<percent>", words)
        self.assertEqual(r.slot_types_used() >= {"duration", "percent", "song", "app", "expr", "name"}, True)
        trig = r.free_triggers()
        self.assertIn(("play",), trig)
        self.assertIn(("lay",), trig)
        self.assertIn(("playback",), trig)
        self.assertIn(("search", "for"), trig)
        self.assertIn(("search", "the", "web", "for"), trig)
        self.assertFalse(any(t[0] == "open" for t in trig))

    def test_global_decorator(self):
        self.assertIs(command.__self__, REGISTRY)

        def fn(ctx, m):
            pass
        self.assertIs(Registry().command("x_test", "x test phrase", section="Basics", help="h")(fn), fn)


class FuzzyTiers(unittest.TestCase):
    """Registry.fuzzy_tier / close_phrase_scored against the real command set (hearing rebuild, Sep 15 2026)."""
    RUN = {"volume sup": "volume_up", "show desk top": "show_desktop", "system stat us": "system_status",
           "take a screen shut": "screenshot"}
    ASK = {"mew the sound": "mute", "meet the sound": "mute", "flip a con": "coin", "roll a dies": "dice"}
    NOTHING = ("the start", "we start", "sign up", "line out", "log on", "power of", "restart the song",
               "split the point")
    DISRUPTIVE_ASK = {"asleep": "sleep", "hi bernate": "hibernate", "closet": "close_window"}

    @classmethod
    def setUpClass(cls):
        from xyrus.testing import make_test_engine
        cls.e, cls.ns = make_test_engine()
        cls.reg = cls.ns.registry

    def test_run_tier(self):
        for text, name in self.RUN.items():
            with self.subTest(text=text):
                tier = self.reg.fuzzy_tier(text)
                self.assertEqual((tier[0], tier[2].name) if tier else None, ("run", name))

    def test_ask_tier(self):
        for text, name in self.ASK.items():
            with self.subTest(text=text):
                tier = self.reg.fuzzy_tier(text)
                self.assertEqual((tier[0], tier[2].name) if tier else None, ("ask", name))

    def test_destructive_is_ask_only(self):
        tier = self.reg.fuzzy_tier("shot down")
        self.assertEqual((tier[0], tier[1], tier[2].name), ("ask", "shut down", "shutdown"))
        self.assertTrue(tier[2].destructive)

    def test_disruptive_asks_never_runs(self):
        for text, name in self.DISRUPTIVE_ASK.items():
            with self.subTest(text=text):
                tier = self.reg.fuzzy_tier(text)
                self.assertEqual((tier[0], tier[2].name) if tier else None, ("ask", name))
                self.assertTrue(tier[2].disruptive)

    def test_nothing(self):
        for text in self.NOTHING:
            with self.subTest(text=text):
                self.assertIsNone(self.reg.fuzzy_tier(text))

    def test_exact_phrases_keep_their_command(self):
        for text, name in (("shut down chrome", "close_app"), ("kill the sound", "mute")):
            with self.subTest(text=text):
                m = self.e._match(text, None, "full")
                self.assertEqual(m.command.name if m else None, name)

    def test_scored_is_sorted_with_commands(self):
        scored = self.reg.close_phrase_scored("volume sup")
        self.assertEqual(scored[0][0], "volume up")
        self.assertEqual(scored[0][2].name, "volume_up")
        ratios = [r for _p, r, _c in scored]
        self.assertEqual(ratios, sorted(ratios, reverse=True))
        self.assertEqual(self.reg.close_phrase_scored(""), [])
        self.assertEqual(self.reg.close_phrase("volume sup"), "volume up")     # the old helper stays

    def test_disruptive_flag(self):
        for name in ("sleep", "hibernate", "lock", "screen_off", "close_window", "close_app", "pause_listening"):
            with self.subTest(name=name):
                self.assertTrue(self.reg.get(name).disruptive)
        self.assertFalse(self.reg.get("volume_up").disruptive)
        self.assertFalse(self.reg.get("shutdown").disruptive)          # destructive covers it


class FreeValueAnchor(unittest.TestCase):
    def test_no_literal_anchor_is_empty(self):
        pat = compile_pattern("play <song>")
        self.assertEqual(Registry._free_value(pat, "hello there my friend"), [])

    def test_anchor_still_works(self):
        pat = compile_pattern("play <song>")
        self.assertEqual(Registry._free_value(pat, "play shape of you"), ["shape", "of", "you"])


if __name__ == "__main__":
    unittest.main()
