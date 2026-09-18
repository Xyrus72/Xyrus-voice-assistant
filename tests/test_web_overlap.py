"""Hearing rebuild (Sep 15 2026): find_song scores YouTube's results against what was said (overlap), and when
the top results miss it walks alternate queries - lexicon-corrected, the n-best hypotheses, '<artist> <title>',
the Bengali form, play history - fetched concurrently under one 2.5 s deadline.

Canned YouTube result tables only: youtube_search is replaced by a fake. NEVER touches the network.
Run: venv\\Scripts\\python.exe -m unittest tests.test_web_overlap -v
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

if not os.environ.get("XYRUS_DATA_DIR"):
    os.environ["XYRUS_DATA_DIR"] = tempfile.mkdtemp(prefix="xyrus_test_")

from xyrus import paths
from xyrus.actions import web as aweb

ISHWAR = ("ishwar00001", "Vikings - Ishwar", "Vikings Official", 301)
TIRED = ("tired000001", "Alan Walker - Tired (Official Video)", "Alan Walker", 190)
SHAPE = ("shape000001", "Ed Sheeran - Shape of You (Official Music Video)", "Ed Sheeran", 263)
TARA = ("tara0000001", "Amar Bhindeshi Tara | Chandrabindoo", "Chandrabindoo", 250)
KESARIYA = ("kesariya001", "Kesariya - Brahmastra | Arijit Singh", "Sony Music India", 170)
ICONS = ("icons000001", "Icons - Official Trailer", "Some Studio", 120)
HOOK = ("hook0000001", "Hook (1991) - Peter Pan returns", "Movie Clips", 140)
JUNK = ("junk0000001", "Random Vlog #12", "Someone", 600)

# normalised query words -> rows (what YouTube would answer); anything else -> JUNK only
TABLE = {
    "if shown you by icons": [ICONS, JUNK],
    "tired by ellen hook": [HOOK, JUNK],
    "tired by alan walker": [TIRED, JUNK],
    "ishwar by vikings": [ISHWAR, JUNK],
    "vikings ishwar": [ISHWAR],
    "shape of you by ed sheeran": [SHAPE, JUNK],
}


def _key(q):
    return " ".join(aweb._tokens(q))


class FakeYouTube:
    """youtube_search stand-in: canned rows, a call log, an optional per-query delay."""

    def __init__(self, table=None, delay=None):
        self.table = dict(TABLE if table is None else table)
        self.delay = delay or {}
        self.calls: list[str] = []
        self.lock = threading.Lock()

    def __call__(self, q, timeout=6):
        with self.lock:
            self.calls.append(q)
        d = self.delay.get(_key(q), 0)
        if d:
            time.sleep(d)
        return list(self.table.get(_key(q), [JUNK]))


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="xyrus_web_"))
        p = mock.patch.object(paths, "data_dir", lambda: self.tmp)
        p.start()
        self.addCleanup(p.stop)
        v = mock.patch.object(aweb, "video_title", lambda url, timeout=4: None)   # never the oEmbed call
        v.start()
        self.addCleanup(v.stop)

    def find(self, q, yt=None, **kw):
        yt = yt or FakeYouTube()
        with mock.patch.object(aweb, "youtube_search", yt):
            return aweb.find_song(q, **kw), yt


class Overlap(unittest.TestCase):
    def test_misses(self):
        self.assertFalse(aweb.is_hit("if shown you by icons", ISHWAR[1], ISHWAR[2]))
        self.assertLess(aweb.overlap("if shown you by icons", ISHWAR[1], ISHWAR[2]), 0.5)
        self.assertFalse(aweb.is_hit("tired by ellen hook", TIRED[1], TIRED[2]))
        self.assertAlmostEqual(aweb.overlap("tired by ellen hook", TIRED[1], TIRED[2]), 1 / 3)

    def test_hits(self):
        for q, row in (("ishwar by vikings", ISHWAR), ("ishbar by vikings", ISHWAR), ("eshvar by vikings", ISHWAR),
                       ("vikings ishvar song", ISHWAR), ("amar bindashi tara", TARA), ("kessaria", KESARIYA),
                       ("tired allen walker", TIRED), ("ঈশ্বর vikings", ISHWAR),
                       ("shape of you by ed sheeran", SHAPE)):
            with self.subTest(q=q):
                self.assertTrue(aweb.is_hit(q, row[1], row[2]))
                self.assertGreaterEqual(aweb.overlap(q, row[1], row[2]), 0.5)

    def test_stop_words_do_not_count(self):
        self.assertFalse(aweb.is_hit("play the official video", "Official Video", ""))
        self.assertEqual(aweb.overlap("the song", "The Song", ""), 0.0)


class Lexicon(unittest.TestCase):
    def test_corrections(self):
        self.assertEqual(aweb.lexicon_correct("tired by ellen hook"), "tired by alan walker")
        self.assertEqual(aweb.lexicon_correct("shape of you by ed sheeran"), "shape of you by ed sheeran")
        self.assertEqual(aweb.lexicon_correct("ishbar by vikings"), "ishwar by vikings")
        self.assertEqual(aweb.lexicon_correct("kessaria"), "kesariya")

    def test_short_words_untouched(self):
        self.assertEqual(aweb.lexicon_correct("if shown you by icons"), "if shown you by icons")

    def test_file_is_utf8_with_comments(self):
        text = (paths.BASE / "data" / "music_lexicon.txt").read_text("utf-8")
        entries = [ln.split("#", 1)[0].strip() for ln in text.splitlines()]
        entries = [e for e in entries if e]
        for want in ("Vikings", "Ishwar", "Alan Walker", "Amar Bhindeshi Tara", "Tum Hi Ho", "Kesariya"):
            self.assertIn(want, entries)


class FindSong(Case):
    def test_primary_hit_one_get(self):
        pick, yt = self.find("shape of you by ed sheeran")
        self.assertEqual((pick.vid, pick.query_used), (SHAPE[0], "shape of you by ed sheeran"))
        self.assertGreaterEqual(pick.score, 0.5)
        self.assertEqual(yt.calls, ["shape of you by ed sheeran"])

    def test_lexicon_alternate(self):
        pick, yt = self.find("tired by ellen hook")
        self.assertEqual(pick.vid, TIRED[0])
        self.assertEqual(pick.query_used, "tired by alan walker")
        self.assertEqual(yt.calls[0], "tired by ellen hook")

    def test_nbest_alternate_finds_ishwar(self):
        pick, yt = self.find("if shown you by icons", alternates=("Xyrus, play Ishwar by Vikings.",))
        self.assertEqual((pick.vid, pick.query_used), (ISHWAR[0], "ishwar by vikings"))
        self.assertLessEqual(len(yt.calls), 4)

    def test_bengali_alternate(self):
        table = {"if shown you by icons": [ICONS], "ঈশ্বর icons": [ISHWAR]}
        yt = FakeYouTube({_key(k): v for k, v in table.items()})
        pick, _ = self.find("if shown you by icons", yt, bn_text="ঈশ্বর")
        self.assertEqual(pick.vid, ISHWAR[0])

    def test_miss_returns_the_original_top_result_unsure(self):
        pick, yt = self.find("if shown you by icons")
        self.assertEqual(pick.vid, ICONS[0])
        self.assertLess(pick.score, 0.5)
        self.assertEqual(pick.query_used, "if shown you by icons")

    def test_alternate_order(self):
        alts = aweb.alternate_queries("tired by ellen hook", ("play tired by alan walker please", "tyred by alan",
                                                              "tired by helen", "fourth one"), "ঈশ্বর")
        self.assertEqual(alts[:5], ["tired by alan walker", "tyred by alan", "tired by helen", "ellen hook tired",
                                    "ঈশ্বর ellen hook"])
        self.assertIn("isshor", alts)
        self.assertNotIn("fourth one", alts)                     # only alternates[0:3]
        self.assertNotIn("tired by ellen hook", alts)            # never the query itself

    def test_first_hit_in_order_wins(self):
        # both the lexicon form and the n-best form hit: the lexicon one is first in order
        table = dict(TABLE)
        table["tired alan"] = [TIRED]
        pick, _ = self.find("tired by ellen hook", FakeYouTube(table), alternates=("play tired alan",))
        self.assertEqual(pick.query_used, "tired by alan walker")

    def test_at_most_four_gets(self):
        pick, yt = self.find("if shown you by icons", alternates=("a b c", "d e f", "g h i"), bn_text="ঈশ্বর")
        self.assertLessEqual(len(yt.calls), 4)
        self.assertEqual(len(yt.calls), 4)
        self.assertEqual(pick.vid, ICONS[0])

    def test_alternates_concurrent_under_the_deadline(self):
        slow = {"a b c": 0.6, "d e f": 0.6, "g h i": 0.6}
        yt = FakeYouTube(delay=slow)
        t0 = time.monotonic()
        self.find("if shown you by icons", yt, alternates=("a b c", "d e f", "g h i"))
        self.assertLess(time.monotonic() - t0, 1.5, "three 0.6 s GETs must run side by side")
        self.assertEqual(len(yt.calls), 4)

    def test_deadline_cuts_a_slow_alternate(self):
        table = dict(TABLE)
        table["ishwar by vikings"] = [ISHWAR]
        yt = FakeYouTube(table, delay={"ishwar by vikings": 1.5})
        with mock.patch.object(aweb, "ALT_DEADLINE_S", 0.3):
            t0 = time.monotonic()
            pick, _ = self.find("if shown you by icons", yt, alternates=("play ishwar by vikings",))
            took = time.monotonic() - t0
        self.assertLess(took, 1.0)
        self.assertEqual(pick.vid, ICONS[0])                     # not back in time: the unsure original
        self.assertLess(pick.score, 0.5)

    def test_max_song_secs_interplay(self):
        long_hit = ("mix00000001", "Alan Walker - Tired (1 hour loop)", "Loops", 3600)
        table = {"tired by alan walker": [long_hit, JUNK, TIRED]}
        pick, _ = self.find("tired by alan walker", FakeYouTube(table))
        self.assertEqual(pick.vid, TIRED[0])                     # the normal-length hit beats the hour-long one
        pick, _ = self.find("tired by alan walker", FakeYouTube({"tired by alan walker": [long_hit]}))
        self.assertEqual(pick.vid, long_hit[0])                  # ... which is still used when it is all there is

    def test_old_positional_call_and_tuple_shape(self):
        pick, _ = self.find("shape of you by ed sheeran")
        spoken, url, title = pick
        self.assertEqual(spoken, "Shape of You by Ed Sheeran")
        self.assertEqual(url, "https://www.youtube.com/watch?v=" + SHAPE[0])
        self.assertEqual(title, SHAPE[1])
        self.assertEqual((pick[0], pick[1]), (spoken, url))
        self.assertEqual((pick.vid, pick.title, pick.channel, pick.length), SHAPE)
        with mock.patch.object(aweb, "youtube_search", FakeYouTube()):
            self.assertEqual(aweb.find_song("shape of you by ed sheeran")[1], url)

    def test_nothing_found(self):
        with mock.patch.object(aweb, "youtube_search", lambda q, timeout=6: []):
            with self.assertRaises(LookupError):
                aweb.find_song("zzzz")


class PlayHistory(Case):
    def hist(self):
        return json.loads((self.tmp / "play_history.json").read_text("utf-8"))

    def test_accepted_queries_are_appended(self):
        self.find("tired by ellen hook")
        self.assertEqual(self.hist()[-1], {"query": "tired by alan walker", "title": TIRED[1]})

    def test_unsure_picks_are_not_remembered(self):
        self.find("if shown you by icons")
        self.assertFalse((self.tmp / "play_history.json").exists())

    def test_last_fifty(self):
        for i in range(60):
            aweb._history_add(f"song {i}", f"Title {i}")
        h = self.hist()
        self.assertEqual(len(h), 50)
        self.assertEqual(h[0]["query"], "song 10")
        self.assertEqual(h[-1]["query"], "song 59")

    def test_corrupt_file_ignored(self):
        (self.tmp / "play_history.json").write_text("{not json", "utf-8")
        pick, _ = self.find("shape of you by ed sheeran")
        self.assertEqual(pick.vid, SHAPE[0])
        self.assertEqual(self.hist(), [{"query": "shape of you by ed sheeran", "title": SHAPE[1]}])
        (self.tmp / "play_history.json").write_text('{"a": 1}', "utf-8")
        self.assertEqual(aweb._history_load(), [])

    def test_history_variant_is_an_alternate(self):
        aweb._history_add("ishwar by vikings", ISHWAR[1])
        self.assertIn("ishwar by vikings", aweb.alternate_queries("vikings ishvar"))
        pick, _ = self.find("vikings isvor", FakeYouTube({"ishwar by vikings": [ISHWAR], "vikings isvor": [JUNK]}))
        self.assertEqual(pick.vid, ISHWAR[0])


if __name__ == "__main__":
    unittest.main()
