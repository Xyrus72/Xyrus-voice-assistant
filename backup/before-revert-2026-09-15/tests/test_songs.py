"""T4 — the v1 D:\\arc\\test_songs.py expectations (26 checks) re-expressed against v2: song helpers, the
typed path, real synthesized speech through the recognizer (wake grammar -> command re-decode -> free
re-decode) into the engine, ask-then-answer with noise and cancel, bare 'play' staying play/pause, the offline
fallback, and a listener end-to-end run (fake microphone streaming real audio through Recognizer's thread).

Never opens a browser (FakeActions records open_target). Network checks skip when offline; speech checks
skip when the Vosk model is missing.
Run: venv\\Scripts\\python.exe -m unittest tests.test_songs -v
"""
from __future__ import annotations

import os
import queue
import re
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

if not os.environ.get("XYRUS_DATA_DIR"):
    os.environ["XYRUS_DATA_DIR"] = tempfile.mkdtemp(prefix="xyrus_test_")

from xyrus import normalize as N
from xyrus import paths
from xyrus.actions import web as aweb
from xyrus.commands import web as cweb
from xyrus.testing import FakeActions, FakeChime, FakeSpeaker, make_test_engine

WATCH = "https://www.youtube.com/watch?v=4bt-z4gxKqI"
HAVE_MODEL = (paths.MODEL_DIR / "am" / "final.mdl").exists()


def online() -> bool:
    try:
        socket.create_connection(("1.1.1.1", 53), 1.5).close()
        return True
    except OSError:
        return False


ONLINE = online()


def fake_lookup(q, **hints):
    """find_song stand-in: echoes the query as the title so tests can see what was looked up."""
    return (f"{q.title()} by Someone", WATCH)


# ======================================================================= helpers (v1 checks 1-8)
class TestHelpers(unittest.TestCase):
    def test_song_request(self):
        self.assertTrue(cweb.is_song_request("play alone"))                         # 1
        self.assertFalse(cweb.is_song_request("play"))                              # 2
        self.assertTrue(cweb.is_song_request("play [unk]"))                         # 3
        self.assertFalse(cweb.is_song_request("play pause"))

    def test_title_cleanup(self):
        self.assertEqual(N.clean_song_title("the song shape of you on youtube please"), "shape of you")   # 4
        self.assertEqual(N.words_after_play("sirius lay alone".split()), ["alone"])                      # 5
        self.assertEqual(cweb.song_title("cyrus play the track faded by alan walker now"), "faded by alan walker")
        self.assertEqual(cweb.song_title("zira clay believer"), "believer")

    def test_spoken_title(self):
        self.assertEqual(aweb.spoken_title("Alan Walker - Alone (Official Music Video)"), "Alone by Alan Walker")  # 6

    @unittest.skipUnless(ONLINE, "no network")
    def test_unrecognised_page_fetches_title_by_id(self):                                                  # 7
        with mock.patch.object(aweb, "youtube_search", lambda q, timeout=6: [("1-xGerv5FOk", "", "", None)]):
            self.assertEqual(aweb.find_song("alone")[0], "Alone by Alan Walker")

    def test_both_page_variants(self):                                                                     # 8
        rx = r'(?:var ytInitialData|window\["ytInitialData"\])\s*=\s*(\{.*?\});\s*</script>'
        for page in ('x<script>window["ytInitialData"] = {"a": 1};</script>y',
                     'x<script>var ytInitialData = {"a": 1};</script>y'):
            self.assertTrue(re.search(rx, page, re.S))


# ======================================================================= typed path (v1 checks 9-12)
class EngineCase(unittest.TestCase):
    def engine(self, **returns):
        returns.setdefault("find_song", fake_lookup)
        return make_test_engine(tmpdir=Path(tempfile.mkdtemp(prefix="xyrus_song_")), actions=FakeActions(**returns))


class TestTyped(EngineCase):
    @unittest.skipUnless(ONLINE, "no network")
    def test_real_lookup_opens_video(self):
        e, ns = self.engine(find_song=aweb.find_song)            # the real YouTube lookup, browser faked
        e.handle("cyrus play alone", "typed")
        opened = [a[0] for a in ns.actions.called("open_target")]
        self.assertTrue(opened and "watch?v=" in opened[0], opened)                          # 9
        self.assertTrue(ns.speaker.said[-1].startswith("Playing ") and "lone" in ns.speaker.said[-1],
                        ns.speaker.said)                                                   # 10

    def test_typed_title(self):
        e, ns = self.engine()
        e.handle("play shape of you", "typed")
        self.assertEqual(ns.actions.called("find_song"), [("shape of you",)])
        self.assertEqual(ns.speaker.said[-1], "Playing Shape Of You by Someone, sir.")
        self.assertIn((WATCH,), ns.actions.called("open_target"))

    def test_bare_play_and_pause_are_media(self):
        for text in ("cyrus play", "cyrus pause"):                                        # 11, 12
            e, ns = self.engine(browser_playing=True)      # pause presses only while something plays (Sep 15)
            e.handle(text, "typed")
            self.assertEqual(ns.actions.called("media"), [("play_pause",)])
            self.assertEqual(ns.actions.called("open_target"), [])

    def test_offline_fallback(self):                                                        # 25
        e, ns = self.engine(find_song=OSError("no network"))
        e.handle("cyrus play alone", "typed")
        self.assertIn(("https://www.youtube.com/results?search_query=alone",), ns.actions.called("open_target"))
        self.assertIn("couldn't reach", ns.speaker.said[-1])

    def test_ask_then_typed_answer_and_cancel(self):
        e, ns = self.engine()
        e.handle("cyrus play music", "typed")
        self.assertEqual(ns.speaker.said[-1], "What should I play, sir?")
        e.handle("cancel", "typed")                                                        # 24
        self.assertEqual(ns.speaker.said[-1], "Okay, sir.")
        self.assertIsNone(e.snapshot().dialogue)
        self.assertEqual(ns.actions.called("find_song"), [])

    def test_ask_window_times_out_silently(self):
        e, ns = self.engine()
        e.handle("cyrus play something", "typed")
        n = len(ns.speaker.said)
        ns.clock.advance(16)
        e.tick()
        self.assertIsNone(e.snapshot().dialogue)
        self.assertEqual(len(ns.speaker.said), n)


# ======================================================================= spoken (v1 checks 13-23, 26)
@unittest.skipUnless(HAVE_MODEL, "speech model missing")
class TestSpoken(EngineCase):
    """Synthesized speech -> Recognizer.decode_pcm (the mic's final-result path: wake grammar, command
    re-decode, free re-decode) -> engine.handle_transcript."""

    @classmethod
    def setUpClass(cls):
        try:
            from tests import wav_util
            from xyrus.grammar import GrammarBuilder, Vocab
            from xyrus.recognizer import Recognizer
        except ImportError as e:
            raise unittest.SkipTest(f"T3 recognizer not available: {e}")
        cls.wav = wav_util
        cls.e0, cls.ns0 = make_test_engine(tmpdir=Path(tempfile.mkdtemp(prefix="xyrus_song_")),
                                           actions=FakeActions(find_song=fake_lookup))
        vocab = Vocab(paths.MODEL_DIR, cache_file=Path(tempfile.mkdtemp()) / "vocab_cache.json")
        grammar = GrammarBuilder(cls.ns0.registry, cls.ns0.config, vocab)
        cls.rec = Recognizer(paths.MODEL_DIR, None, grammar, get_spec=cls.e0.listen_spec,
                             on_transcript=lambda tr: None, speaker=FakeSpeaker(), chime=FakeChime(),
                             config=cls.ns0.config)
        vocab.attach(cls.rec.check_words)
        cls.rec.load()
        phrases = ["xyrus play alone", "xyrus play shape of you", "xyrus play believer by imagine dragons",
                   "xyrus play faded by alan walker", "xyrus play some music", "blinding lights"]
        cls.pcm = wav_util.synth_many(phrases) if hasattr(wav_util, "synth_many") else \
            {p: wav_util.synth(p) for p in phrases}

    def say_to(self, e, phrase):
        tr = self.rec.decode_pcm(self.pcm[phrase], e.listen_spec().mode)
        e.handle_transcript(tr)
        return tr

    def test_song_phrases(self):
        for phrase, expect in [("xyrus play alone", "alone"), ("xyrus play shape of you", "shape of you"),
                               ("xyrus play believer by imagine dragons", "believer by imagine dragons"),
                               ("xyrus play faded by alan walker", "faded by alan walker")]:        # 13-16
            with self.subTest(phrase=phrase):
                e, ns = self.engine()
                tr = self.say_to(e, phrase)
                self.assertEqual(ns.actions.called("find_song"), [(expect,)],
                                 f"grammar={tr.text!r} free={tr.free_text!r} said={ns.speaker.said}")
                self.assertIn((WATCH,), ns.actions.called("open_target"))

    def test_ask_noise_answer(self):
        e, ns = self.engine()
        tr = self.say_to(e, "xyrus play some music")
        self.assertEqual(ns.speaker.said[-1], "What should I play, sir?", (tr.text, tr.free_text))    # 17
        self.assertIsNotNone(e.snapshot().dialogue)
        self.assertEqual(e.listen_spec().mode, "free")
        e.handle("how", "mic")                                                                # 18
        self.assertEqual(ns.actions.called("find_song"), [])
        self.assertIsNotNone(e.snapshot().dialogue)
        tr = self.say_to(e, "blinding lights")                                              # 19
        self.assertEqual(ns.actions.called("find_song"), [("blinding lights",)], (tr.text, tr.free_text))
        self.assertIsNone(e.snapshot().dialogue)                                            # 20

    def test_listener_end_to_end(self):
        """Fake microphone: silence + the utterance + silence streamed in 0.25 s chunks through Recognizer's
        own thread; the transcript goes to the engine; the song is looked up and 'opened'."""
        from xyrus.grammar import GrammarBuilder, Vocab
        from xyrus.recognizer import Recognizer
        e, ns = self.engine()

        class FakeCapture:
            def __init__(self, pcm):
                self.chunks: "queue.Queue[tuple[bytes, float]]" = queue.Queue()
                data = bytes(16000 * 2) + pcm + bytes(16000 * 2) * 2
                for i in range(0, len(data), 8000):
                    self.chunks.put((data[i:i + 8000], 0.0))

            def running(self): return True
            def level(self): return 0.0
            def status(self): return "listening"
            def start(self): pass
            def stop(self): pass

        got = threading.Event()
        vocab = Vocab(paths.MODEL_DIR, cache_file=Path(tempfile.mkdtemp()) / "vocab_cache.json")
        rec = Recognizer(paths.MODEL_DIR, FakeCapture(self.pcm["xyrus play alone"]),
                         GrammarBuilder(ns.registry, ns.config, vocab), get_spec=e.listen_spec,
                         on_transcript=lambda tr: (e.handle_transcript(tr), got.set()),
                         speaker=FakeSpeaker(), chime=FakeChime(), config=ns.config)
        vocab.attach(rec.check_words)
        rec._model = self.rec.load()                        # share the loaded model (heavy)
        rec.start()
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and not ns.actions.called("find_song"):
                time.sleep(0.1)
        finally:
            rec.stop()
        self.assertEqual(ns.actions.called("find_song"), [("alone",)], ns.speaker.said)          # 21
        self.assertIn((WATCH,), ns.actions.called("open_target"))                                # 22


# ======================================================================= autoplay (v1 round-2 checks)
class TestAutoplayLogic(unittest.TestCase):
    """ensure_playing with every Windows/audio probe stubbed (no real key press, click or focus change)."""

    def run_case(self, audio_seq, hwnd=1, fg=True):
        seq = list(audio_seq)
        fgs = list(fg) if isinstance(fg, (list, tuple)) else None
        presses: list[str] = []
        with mock.patch.multiple(
                aweb,
                _wait_youtube_window=lambda title, timeout: hwnd,
                _audio_within=lambda secs, trust_state=True, exe=None: seq.pop(0) if seq else False,
                _window_exe=lambda h: "msedge.exe",
                _is_foreground=(lambda h: fgs.pop(0) if fgs else False) if fgs is not None else (lambda h: fg),
                _focus=lambda h: None,
                _press_play_key=lambda h: presses.append("k"),
                _click_player=lambda h: presses.append("click"),
                _sleep=lambda s: None):
            return aweb.ensure_playing("Alan Walker - Alone"), presses

    def test_cases(self):
        self.assertEqual(self.run_case([True]), ("autoplay", []))                     # already playing
        self.assertEqual(self.run_case([False, True]), ("started", ["k"]))            # k starts it
        self.assertEqual(self.run_case([False, False, True]), ("started", ["k", "click"]))
        self.assertEqual(self.run_case([False, False, False]), ("blocked", ["k", "click"]))
        self.assertEqual(self.run_case([False], hwnd=None), ("no-window", []))         # can't tell: touch nothing
        self.assertEqual(self.run_case([True], hwnd=None), ("no-window", []))
        self.assertEqual(self.run_case([False], fg=False), ("blocked", []))            # focus lost: press nothing
        self.assertEqual(self.run_case([False, False], fg=[True, False]), ("blocked", ["k"]))  # lost before click

    def test_only_the_songs_browser_counts(self):
        import pycaw.pycaw as pc

        class Proc:
            def __init__(self, n): self._n = n
            def name(self): return self._n

        class Ctl:
            def __init__(self, p): self.p = p
            def QueryInterface(self, iface): return self
            def GetPeakValue(self): return self.p

        class Session:
            def __init__(self, n, state, p): self.Process, self.State, self._ctl = Proc(n), state, Ctl(p)

        sessions = [Session("chrome.exe", 1, 0.85), Session("msedge.exe", 0, 0.0), Session("spotify.exe", 1, 0.9)]
        with mock.patch.object(pc.AudioUtilities, "GetAllSessions", lambda: sessions):
            self.assertFalse(aweb._browser_audio(True, "msedge.exe"))   # a Chrome lecture isn't the Edge song
            self.assertTrue(aweb._browser_audio(True, None))            # but a browser IS playing (pause it first)
            self.assertTrue(aweb.browser_playing())
            self.assertTrue(aweb._browser_audio(False, "chrome.exe"))   # meter alone
        with mock.patch.object(pc.AudioUtilities, "GetAllSessions", lambda: [Session("spotify.exe", 1, 0.9)]):
            self.assertFalse(aweb.browser_playing())                    # only browsers count

    def test_finds_only_the_songs_window(self):
        clock = [0.0]
        wins = [(11, "Lecture 10 - YouTube - Google Chrome")]
        with mock.patch.multiple(aweb, _top_windows=lambda: wins, _now=lambda: clock[0],
                                 _sleep=lambda s: clock.__setitem__(0, clock[0] + s)):
            self.assertIsNone(aweb._wait_youtube_window("Alan Walker - Alone", 0.4))
            wins.append((22, "Alan Walker - Alone - YouTube - Personal - Microsoft​ Edge"))
            self.assertEqual(aweb._wait_youtube_window("Alan Walker – Alone", 0.4), 22)
            self.assertIsNone(aweb._wait_youtube_window("", 0.4))

    def test_click_only_when_foreground_and_cursor_restored(self):
        class U32:
            def __init__(self, fg): self.fg, self.log = fg, []
            def GetClientRect(self, h, ref): ref._obj.right, ref._obj.bottom = 1000, 800
            def ClientToScreen(self, h, ref): ref._obj.x, ref._obj.y = 100, 50
            def GetCursorPos(self, ref): ref._obj.x, ref._obj.y = 7, 9
            def SetCursorPos(self, x, y): self.log.append(("pos", x, y))
            def mouse_event(self, *a): self.log.append(("mouse", a[0]))
            def GetForegroundWindow(self): return self.fg

        for fg, clicked in ((5, True), (6, False)):
            u = U32(fg)
            with mock.patch.object(aweb, "_u32", u), mock.patch.object(aweb, "_sleep", lambda s: None):
                aweb._click_player(5)
            self.assertEqual(u.log[0], ("pos", 100 + 300, 50 + 320))
            self.assertEqual(u.log[-1], ("pos", 7, 9))                           # mouse put back
            self.assertEqual(any(e[0] == "mouse" for e in u.log), clicked)


class TestAutoplayFlow(EngineCase):
    VIDEO = "Alan Walker - Alone (Official Music Video)"

    def play(self, **returns):
        returns.setdefault("find_song", ("Alone by Alan Walker", WATCH, self.VIDEO))
        e, ns = self.engine(**returns)
        with mock.patch.object(cweb, "PAUSE_SETTLE_S", 0):
            e.handle("cyrus play alone", "typed")
        return e, ns

    def ensure_calls(self, ns):
        return [(args, kw) for n, args, kw in ns.actions.calls if n == "ensure_playing"]

    def test_blocked_tells_you(self):
        e, ns = self.play(ensure_playing="blocked")
        self.assertIn("Playing Alone by Alan Walker, sir.", ns.speaker.said)
        self.assertEqual(ns.speaker.said[-1], "The browser blocked autoplay, sir. Press play on the video.")
        self.assertEqual(self.ensure_calls(ns), [((self.VIDEO,), {"trust_state": True})])

    def test_autoplay_says_nothing_more(self):
        e, ns = self.play()
        self.assertEqual(ns.speaker.said[-1], "Playing Alone by Alan Walker, sir.")
        self.assertIn((WATCH,), ns.actions.called("open_target"))
        self.assertEqual(ns.actions.called("media"), [])

    def test_other_audio_paused_first(self):
        e, ns = self.play(browser_playing=True)
        names = ns.actions.names()
        self.assertEqual(ns.actions.called("media"), [("play_pause",)])
        self.assertLess(names.index("media"), names.index("open_target"))     # paused, then the new song opens
        self.assertEqual(self.ensure_calls(ns), [((self.VIDEO,), {"trust_state": False})])

    def test_no_video_title_no_check(self):
        e, ns = self.engine()                                  # 2-tuple lookup (older shape)
        e.handle("cyrus play alone", "typed")
        self.assertEqual(self.ensure_calls(ns), [])

    def test_check_failure_is_quiet(self):
        e, ns = self.play(ensure_playing=OSError("pycaw broke"))
        self.assertEqual(ns.speaker.said[-1], "Playing Alone by Alan Walker, sir.")


# ======================================================================= "not that one" (lead, Sep 14; v1 _next_song)
RESULTS = [("Alone by Alan Walker", "https://www.youtube.com/watch?v=aaaaaaaaaaa", "Alan Walker - Alone"),
           ("Alone by Marshmello", "https://www.youtube.com/watch?v=bbbbbbbbbbb", "Marshmello - Alone"),
           ("Alone by Heart", "https://www.youtube.com/watch?v=ccccccccccc", "Heart - Alone")]


class TestNotThatOne(EngineCase):
    """After a song starts, "not that one" / "wrong song" / "another one" / "different one" / "next result" play the
    next ranked YouTube result for the same request: without the wake word for 40 s, with it any time while a last
    song exists. Tried URLs are skipped; FakeActions only (no network, no browser)."""

    def play(self, **returns):
        returns.setdefault("find_song", RESULTS[0])
        returns.setdefault("find_songs", lambda q, limit=6: list(RESULTS))
        e, ns = self.engine(**returns)
        e.handle("cyrus play alone", "typed")
        self.assertEqual(ns.speaker.said[-1], "Playing Alone by Alan Walker, sir.")
        return e, ns

    def opened(self, ns):
        return [a[0] for a in ns.actions.called("open_target")]

    def test_next_results_without_the_wake_word(self):
        e, ns = self.play()
        e.handle("not that one", "mic")
        self.assertEqual(ns.speaker.said[-1], "Trying Alone by Marshmello, sir.")
        self.assertEqual(ns.actions.called("find_songs"), [("alone",)])
        e.handle("wrong song", "mic")
        self.assertEqual(ns.speaker.said[-1], "Trying Alone by Heart, sir.")
        self.assertEqual(self.opened(ns), [r[1] for r in RESULTS])            # each result opened once, in order
        e.handle("a different one", "mic")
        self.assertEqual(ns.speaker.said[-1], "That's all I found for alone, sir. Try saying the name again.")
        self.assertEqual(len(self.opened(ns)), 3)

    def test_each_phrase_works(self):
        for phrase in ("not that one", "not this song", "wrong one", "that's not it", "another one",
                       "different song", "next result", "the next result", "try another", "not the right song"):
            with self.subTest(phrase=phrase):
                e, ns = self.play()
                e.handle(phrase, "mic")
                self.assertEqual(ns.speaker.said[-1], "Trying Alone by Marshmello, sir.")

    def test_window_is_forty_seconds_then_the_wake_word_is_needed(self):
        e, ns = self.play()
        ns.clock.advance(39)
        e.tick()
        self.assertIn("result", e.listen_spec().extra_words)                 # the idle grammar can hear it
        ns.clock.advance(2)
        e.tick()
        self.assertNotIn("result", e.listen_spec().extra_words)
        n = len(ns.speaker.said)
        e.handle("not that one", "mic")                                        # 41 s later, no wake word: noise
        self.assertEqual(ns.speaker.said[n:], [])
        self.assertEqual(ns.actions.called("find_songs"), [])
        self.assertEqual(e.snapshot().events[-1].kind, "noise")
        e.handle("cyrus not that one", "mic")                                  # with it: any time
        self.assertEqual(ns.speaker.said[-1], "Trying Alone by Marshmello, sir.")

    def test_window_reopens_after_each_try(self):
        e, ns = self.play()
        ns.clock.advance(30)
        e.handle("not that one", "mic")
        ns.clock.advance(30)                                                   # 60 s after the first song
        e.handle("not that one", "mic")
        self.assertEqual(ns.speaker.said[-1], "Trying Alone by Heart, sir.")

    def test_before_any_song(self):
        e, ns = self.engine(find_songs=lambda q, limit=6: list(RESULTS))
        e.handle("not that one", "mic")
        self.assertEqual(ns.speaker.said, [])                                 # no wake, no song: noise
        e.handle("cyrus not that one", "mic")
        self.assertEqual(ns.speaker.said[-1], "I haven't played anything yet, sir.")
        self.assertEqual(ns.actions.called("find_songs"), [])

    def test_lookup_failure(self):
        e, ns = self.play(find_songs=OSError("offline"))
        e.handle("not that one", "mic")
        self.assertEqual(ns.speaker.said[-1], "I couldn't reach YouTube, sir.")

    def test_new_request_starts_a_new_list(self):
        e, ns = self.play()
        e.handle("not that one", "mic")
        ns.actions.returns["find_song"] = ("Believer by Imagine Dragons", "https://www.youtube.com/watch?v=ddddddddddd",
                                           "Imagine Dragons - Believer")
        ns.actions.returns["find_songs"] = lambda q, limit=6: [
            ("Believer by Imagine Dragons", "https://www.youtube.com/watch?v=ddddddddddd", "Imagine Dragons - Believer"),
            ("Believer (Lyrics)", "https://www.youtube.com/watch?v=eeeeeeeeeee", "Believer (Lyrics)")]
        e.handle("cyrus play believer", "typed")
        e.handle("not that one", "mic")
        self.assertEqual(ns.actions.called("find_songs")[-1], ("believer",))
        self.assertEqual(ns.speaker.said[-1], "Trying Believer (Lyrics), sir.")     # the old list is gone
        self.assertEqual(ns.actions.called("open_target")[-1], ("https://www.youtube.com/watch?v=eeeeeeeeeee",))

    def test_media_next_and_jokes_unchanged(self):
        e, ns = self.play()
        e.handle("cyrus next", "typed")
        self.assertEqual(ns.actions.called("media"), [("next",)])
        e.handle("cyrus tell me another one", "typed")
        self.assertEqual(ns.actions.called("find_songs"), [])

    def test_find_songs_ranks_normal_length_first(self):
        rows = [("mix0000000a", "Alone (1 hour mix)", "", 3600), ("vid0000000b", "Alan Walker - Alone", "", 161),
                ("live000000c", "Alone (Live)", "", None), ("vid0000000d", "Alone - Heart", "", 215)]
        with mock.patch.object(aweb, "youtube_search", lambda q, timeout=6: list(rows)):
            got = aweb.find_songs("alone", limit=3)
        self.assertEqual([u.rsplit("=", 1)[1] for _, u, _ in got], ["vid0000000b", "vid0000000d", "mix0000000a"])
        self.assertEqual(got[0][0], "Alone by Alan Walker")
        with mock.patch.object(aweb, "youtube_search", lambda q, timeout=6: []):
            with self.assertRaises(LookupError):
                aweb.find_songs("nothing")


# ======================================================================= song pick (hearing rebuild, Sep 15)
class TestSongPick(EngineCase):
    """End to end through the engine with the REAL actions.find_song scoring canned YouTube tables (no network):
    the right video for clean requests, Ishwar found through the hearing alternates, the unsure reply when
    nothing agrees, and 'not that one' still moving on."""

    def setUp(self):
        from tests.test_web_overlap import FakeYouTube
        self.tmp = Path(tempfile.mkdtemp(prefix="xyrus_pick_"))
        self.yt = FakeYouTube()
        for p in (mock.patch.object(paths, "data_dir", lambda: self.tmp),
                  mock.patch.object(aweb, "youtube_search", self.yt),
                  mock.patch.object(aweb, "video_title", lambda url, timeout=4: None)):
            p.start()
            self.addCleanup(p.stop)

    def run_phrase(self, phrase, alternates=(), **returns):
        returns.setdefault("find_song", lambda q, **hints: aweb.find_song(q, **hints))
        e, ns = self.engine(**returns)
        orig = cweb.play_song

        def with_hints(ctx, title):                 # what the engine sets from the capture before dispatch
            if alternates:
                ctx.alternates = tuple(alternates)
            return orig(ctx, title)
        with mock.patch.object(cweb, "play_song", with_hints):
            e.handle(phrase, "typed")
        return e, ns

    def opened(self, ns):
        return [a[0] for a in ns.actions.called("open_target")]

    def test_clean_requests_pick_the_right_video(self):
        from tests.test_web_overlap import ISHWAR, SHAPE, TIRED
        for phrase, row in (("play ishwar by vikings", ISHWAR), ("play tired by alan walker", TIRED),
                            ("can you play shape of you by ed sheeran", SHAPE)):
            with self.subTest(phrase=phrase):
                e, ns = self.run_phrase(phrase)
                self.assertEqual(self.opened(ns)[:1], ["https://www.youtube.com/watch?v=" + row[0]], ns.speaker.said)
                self.assertTrue(ns.speaker.said[-1].startswith("Playing "), ns.speaker.said)
                self.assertNotIn("not that one", ns.speaker.said[-1])

    def test_ishwar_through_the_alternates(self):
        from tests.test_web_overlap import ISHWAR
        e, ns = self.run_phrase("play if shown you by icons", alternates=("Xyrus, play Ishwar by Vikings.",))
        self.assertEqual(self.opened(ns)[:1], ["https://www.youtube.com/watch?v=" + ISHWAR[0]], ns.speaker.said)
        self.assertEqual(ns.speaker.said[-1], "Playing Ishwar by Vikings, sir.")
        self.assertEqual(cweb.last_song(e).search, "ishwar by vikings")

    def test_unsure_pick_says_so_and_not_that_one_moves_on(self):
        from tests.test_web_overlap import ICONS
        e, ns = self.run_phrase("play if shown you by icons",
                                find_songs=lambda q, limit=6: list(RESULTS))
        self.assertEqual(self.opened(ns), ["https://www.youtube.com/watch?v=" + ICONS[0]])
        self.assertIn("not that one", ns.speaker.said[-1])
        self.assertTrue(ns.speaker.said[-1].startswith("Playing "), ns.speaker.said)
        e.handle("not that one", "mic")
        self.assertEqual(ns.speaker.said[-1], "Trying Alone by Alan Walker, sir.")
        self.assertEqual(ns.actions.called("find_songs"), [("if shown you by icons",)])
        self.assertEqual(len(self.opened(ns)), 2)


if __name__ == "__main__":
    unittest.main()
