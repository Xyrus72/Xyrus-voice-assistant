"""Pause / resume the song (user report, Sep 15 2026):
- "pause" is a blind play/pause toggle key: said on a paused song it started it again;
- "play the song" / "play it" / "play the song that you paused" were searched on YouTube as titles ("that you
  paused" -> "You Were My Favorite Pause") instead of resuming the paused one;
- "pause the song" said while "What should I play?" was open became its answer and played "The Pause".

Run: venv\\Scripts\\python.exe -m unittest tests.test_media_control -v
"""
import unittest

from xyrus.testing import FakeActions, make_test_engine

RESUME = ("resume", "resume the song", "continue", "continue the song", "keep playing", "unpause", "play it",
          "play it again", "play the song", "play the song again", "play again", "play the music",
          "play the paused song", "play the song you paused", "play the song that you paused")
PAUSE = ("pause", "pause it", "pause the song", "pause this song", "pause the music", "pause the video")


def engine(playing: bool):
    return make_test_engine(actions=FakeActions(browser_playing=playing))


class PauseAndResume(unittest.TestCase):
    def test_pause_while_playing_presses_once(self):
        for phrase in PAUSE:
            with self.subTest(phrase=phrase):
                e, ns = engine(True)
                e.handle(phrase, "typed")
                self.assertEqual(ns.actions.called("media"), [("play_pause",)])

    def test_pause_when_already_paused_does_not_start_it(self):
        e, ns = engine(False)
        e.handle("pause the song", "typed")
        self.assertEqual(ns.actions.called("media"), [])
        self.assertTrue(ns.speaker.said and ns.speaker.said[-1].startswith("It's already paused"), ns.speaker.said)

    def test_resume_phrases_resume_the_paused_song(self):
        for phrase in RESUME:
            with self.subTest(phrase=phrase):
                e, ns = engine(False)
                e.handle(phrase, "typed")
                self.assertEqual(ns.actions.called("media"), [("play_pause",)])
                self.assertEqual(ns.actions.called("find_song"), [], "never searched as a title")

    def test_resume_when_already_playing_does_not_pause_it(self):
        e, ns = engine(True)
        e.handle("play the song", "typed")
        self.assertEqual(ns.actions.called("media"), [])
        self.assertTrue(ns.speaker.said and ns.speaker.said[-1].startswith("It's already playing"), ns.speaker.said)

    def test_a_real_title_still_searches(self):
        for phrase, title in (("play shape of you", "shape of you"), ("play the song faded", "faded"),
                              ("play it's my life", "it's my life"), ("play you raise me up", "you raise me up")):
            with self.subTest(phrase=phrase):
                e, ns = engine(False)
                e.handle(phrase, "typed")
                self.assertTrue(ns.actions.called("find_song"), (phrase, ns.actions.names()))
                self.assertEqual(ns.actions.called("find_song")[0][0], title)

    def test_bare_play_is_still_the_toggle(self):
        e, ns = engine(True)
        e.handle("play pause", "typed")
        self.assertEqual(ns.actions.called("media"), [("play_pause",)])


class SongQuestionYieldsToMediaCommands(unittest.TestCase):
    def ask(self, playing: bool):
        e, ns = engine(playing)
        e.handle("play a song", "typed")
        self.assertIsNotNone(e.dialogue, "'play a song' asks what to play")
        return e, ns

    def test_pause_the_song_is_not_a_title(self):
        e, ns = self.ask(True)
        e.handle("pause the song", "mic")
        self.assertIsNone(e.dialogue, "the question is dropped")
        self.assertEqual(ns.actions.called("find_song"), [])
        self.assertEqual(ns.actions.called("media"), [("play_pause",)])

    def test_volume_up_is_not_a_title(self):
        e, ns = self.ask(False)
        e.handle("volume up", "mic")
        self.assertIsNone(e.dialogue)
        self.assertEqual(ns.actions.called("find_song"), [])

    def test_a_title_is_still_the_answer(self):
        e, ns = self.ask(False)
        e.handle("shape of you", "mic")
        self.assertEqual(ns.actions.called("find_song")[0][0], "shape of you")


if __name__ == "__main__":
    unittest.main()
