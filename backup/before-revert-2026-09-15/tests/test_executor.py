"""T-exec watchdog vs a PC suspend (hearing rebuild, Sep 15 2026): "sleep" / "hibernate" block across the
suspend and the monotonic clock keeps counting through it, so they must never be timed out (the false "That's
taking too long" after waking). Every other job still is.
Run: venv\\Scripts\\python.exe -m unittest tests.test_executor -v
"""
from __future__ import annotations

import logging
import threading
import time
import unittest

from xyrus.executor import SUSPEND_JOBS, Executor

DEFAULT = 0.2


class SuspendJobs(unittest.TestCase):
    def setUp(self):
        self.ex = Executor(name="test-exec", watch_period=0.01)
        self.addCleanup(self.ex.stop)

    def run_job(self, name, block_s):
        done = threading.Event()
        out = {}

        def fn():
            time.sleep(block_s)
            return "woke"

        def on_done(result, error):
            out["result"], out["error"] = result, error
            done.set()
        self.ex.submit(fn, timeout=DEFAULT, name=name, on_done=on_done)
        self.assertTrue(done.wait(block_s + 3))
        return out

    def test_sleep_is_never_timed_out(self):
        for name in ("sleep", "hibernate"):
            with self.subTest(name=name):
                with self.assertNoLogs("xyrus.executor", level=logging.WARNING):
                    out = self.run_job(name, 4 * DEFAULT)
                self.assertEqual(out, {"result": "woke", "error": None})
                self.assertEqual(self.ex.abandoned, 0)

    def test_a_normal_job_still_times_out(self):
        with self.assertLogs("xyrus.executor", level=logging.WARNING):
            out = self.run_job("find_song", 4 * DEFAULT)
        self.assertIsInstance(out["error"], TimeoutError)
        self.assertEqual(self.ex.abandoned, 1)

    def test_the_set(self):
        self.assertEqual(SUSPEND_JOBS, frozenset({"sleep", "hibernate"}))


if __name__ == "__main__":
    unittest.main()
