"""The autoplay check must never hold up other commands. After "play <song>", the check that presses play
(up to ~25 s) runs on its own thread, so the action worker is free the moment the URL is opened.
Lead-owned test for the fix in xyrus/commands/web.py (_run_check / _thread_spawn)."""
import threading
import time
import unittest

from xyrus.commands import web as W
from xyrus.testing import FakeActions, make_test_engine

SONG = ("Alone by Alan Walker", "https://www.youtube.com/watch?v=1-xGerv5FOk", "Alan Walker - Alone")


class AutoplayCheckOffTheWorker(unittest.TestCase):
    def setUp(self):
        self.release = threading.Event()
        self.started = threading.Event()

        def slow_ensure(video, trust_state=True):      # stands in for the real ~25 s check
            self.started.set()
            self.release.wait(5)
            return "blocked"

        self.acts = FakeActions(find_song=SONG, browser_playing=False, ensure_playing=slow_ensure)
        self.engine, self.ns = make_test_engine(actions=self.acts)
        self._saved_spawn = W._spawn
        W._spawn = W._thread_spawn                    # the app's path: a real thread

    def tearDown(self):
        self.release.set()
        W._spawn = self._saved_spawn

    def _said(self):
        return [str(s) for s in self.ns.speaker.said]

    def test_play_returns_at_once_and_volume_is_not_held_up(self):
        t0 = time.monotonic()
        self.engine.handle("play alone", "typed")
        self.assertLess(time.monotonic() - t0, 1.0, "'play' waited for the autoplay check")
        self.assertIn("open_target", self.acts.names())
        self.assertTrue(self.started.wait(2), "the autoplay check never started")
        t1 = time.monotonic()
        self.engine.handle("volume up", "typed")
        self.assertLess(time.monotonic() - t1, 1.0, "'volume up' queued behind the autoplay check")
        self.assertIn("volume_step", self.acts.names())

    def test_blocked_reply_still_arrives(self):
        self.engine.handle("play alone", "typed")
        self.assertTrue(self.started.wait(2))
        self.release.set()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not any("blocked autoplay" in s for s in self._said()):
            time.sleep(0.05)
        self.assertTrue(any("blocked autoplay" in s for s in self._said()), self._said())


class HeadlessDefaultStaysInline(unittest.TestCase):
    """Without a threaded engine (tests) the check runs inline, so replies stay deterministic."""

    def test_inline_when_engine_not_threaded(self):
        acts = FakeActions(find_song=SONG, browser_playing=False, ensure_playing="blocked")
        engine, ns = make_test_engine(actions=acts)
        self.assertIsNone(W._spawn)
        engine.handle("play alone", "typed")
        self.assertTrue(any("blocked autoplay" in str(s) for s in ns.speaker.said), ns.speaker.said)


if __name__ == "__main__":
    unittest.main()
