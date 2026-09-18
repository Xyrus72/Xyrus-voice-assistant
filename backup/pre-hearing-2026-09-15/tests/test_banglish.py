"""Bangla song names (lead request, Sep 14 2026): Bengali script -> Banglish (xyrus/banglish.py), the Bangla
"play" carrier words, and the song flow - Whisper hears a song request in English, notices Bangla, hears it
again in Bengali script; YouTube is searched with the Bengali title first and the Banglish second; replies
speak the Latin part / the Banglish (SAPI can't read Bengali script).

No Whisper and no network here: FakeHearing (tests/test_capture.py) returns Bengali script, youtube_search is
mocked. Run: venv\\Scripts\\python.exe -m unittest tests.test_banglish -v
"""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

if not os.environ.get("XYRUS_DATA_DIR"):
    os.environ["XYRUS_DATA_DIR"] = tempfile.mkdtemp(prefix="xyrus_test_")

from tests.test_capture import FakeHearing, cap_engine, cmds, said
from xyrus import banglish as B
from xyrus.actions import web as aweb
from xyrus.capture import CapturedTranscript, Utterance
from xyrus.interfaces import ListenSpec

# Bengali -> accepted Banglish spellings: real Bangla song titles and song words
ACCEPTED = {
    "আমার ভিনদেশী তারা": {"amar bhindeshi tara"},
    "ঈশ্বর": {"isshor", "ishshor"},
    "তুমি": {"tumi"},
    "ভালোবাসি": {"bhalobasi", "bhalobashi", "valobashi", "valobasi"},
    "সোনার বাংলা": {"sonar bangla", "shonar bangla"},
    "আমার সোনার বাংলা": {"amar sonar bangla", "amar shonar bangla"},
    "একতারা": {"ektara"},
    "তোমাকে চাই": {"tomake chai"},
    "আমি তোমার কাছে": {"ami tomar kache", "ami tomar kachhe"},
    "জীবন": {"jibon"},
    "হৃদয়": {"hridoy"},
    "প্রিয়": {"priyo"},
    "বন্ধু": {"bondhu"},
    "স্বপ্ন": {"shopno", "swopno", "sopno"},
    "বিশ্বাস": {"bisshas", "bishshas", "biswas"},
    "চাঁদ": {"chand", "chad"},
    "আকাশ": {"akash"},
    "পদ্মা": {"podda", "padma"},
    "দুঃখ": {"dukkho", "dukho"},
    "লক্ষ্মী": {"lokkhi"},
    "জ্ঞান": {"gan", "gyan"},
    "কৃষ্ণ": {"krishno"},
    "সূর্য": {"surjo"},
    "বৃষ্টি": {"brishti"},
    "মেঘের পরে মেঘ": {"megher pore megh"},
    "তোমার জন্য": {"tomar jonno"},
    "শুধু তোমার জন্য": {"shudhu tomar jonno"},
    "ভুবন মাঝি": {"bhubon majhi"},
    "বেঁচে থাকো": {"benche thako", "beche thako"},
    "হাজার বছর": {"hajar bochor"},
    "আমি বাংলায় গান গাই": {"ami banglay gan gai", "ami banglay gaan gai"},
    "যায় যদি যাক প্রাণ": {"jay jodi jak pran"},
    "হারানো সুর": {"harano sur"},
    "খাঁচার ভিতর অচিন পাখি": {"khanchar bhitor ochin pakhi", "khachar bhitor ochin pakhi"},
    "এক নদী রক্ত": {"ek nodi rokto"},
    "আজ বৃষ্টি ঝরুক": {"aj brishti jhoruk", "aaj brishti jhoruk"},
    "নীলাঞ্জনা": {"nilanjona", "nilanjana"},
    "ধন ধান্য পুষ্প ভরা": {"dhon dhanno pushpo bhora"},
    "বাংলাদেশ": {"bangladesh"},
    "আমরা": {"amra"},
    "সন্ধ্যা": {"sondha", "shondha", "sondhya"},
    "মিথ্যা": {"mittha", "mitthya"},
    "ইচ্ছা": {"ichcha", "iccha"},
    "অন্তরে": {"ontore"},
    "সংগীত": {"songit", "shongit", "sangeet"},
    "প্রেম": {"prem"},
    "পাগল": {"pagol"},
    "বাউল": {"baul"},
    "কারার ঐ লৌহ কপাট": {"karar oi louho kopat", "karar oi louha kopat"},
    "ফাগুন": {"fagun", "phagun"},
    "ঢাকা": {"dhaka"},
    "চট্টগ্রাম": {"chottogram"},
    "পাহাড়": {"pahar"},
    "১৯৭১": {"1971"},
}


class Transliteration(unittest.TestCase):
    def test_song_titles_and_words(self):
        self.assertGreaterEqual(len(ACCEPTED), 30)
        bad = {bn: B.transliterate(bn) for bn, ok in ACCEPTED.items() if B.transliterate(bn) not in ok}
        self.assertEqual(bad, {})

    def test_the_rules(self):
        self.assertEqual(B.transliterate("মন"), "mon")                    # inherent o, final schwa deleted
        self.assertEqual(B.transliterate("ক"), "ko")                      # one letter keeps it
        self.assertEqual(B.transliterate("বন্ধ"), "bondho")               # a final conjunct keeps it
        self.assertEqual(B.transliterate("দেখলাম"), "dekhlam")            # medial schwa deletion
        self.assertEqual(B.transliterate("ক্ষমা"), "khoma")               # ক্ষ at the start = kh ...
        self.assertEqual(B.transliterate("পক্ষ"), "pokkho")               # ... inside = kkh
        self.assertEqual(B.transliterate("বিজ্ঞান"), "biggan")            # জ্ঞ = gg
        self.assertEqual(B.transliterate("কাব্য"), "kabbo")               # য-phala doubles
        self.assertEqual(B.transliterate("রঙ্গ"), "rongo")                # ঙ্গ = ng
        self.assertEqual(B.transliterate("বাড়ি"), "bari")                 # ড় (also decomposed ড + ়)
        self.assertEqual(B.transliterate("বাড়ি"), "bari")
        self.assertEqual(B.transliterate("আমার ভিনদেশী তারা।"), "amar bhindeshi tara")    # danda dropped
        self.assertEqual(B.transliterate("Play আমার গান 2"), "Play amar gan 2")          # mixed text

    def test_has_bengali(self):
        self.assertTrue(B.has_bengali("প্লে Shape"))
        self.assertFalse(B.has_bengali("Shape of You"))
        self.assertFalse(B.has_bengali("अग्वा"))                           # Devanagari is not Bengali


class Speaking(unittest.TestCase):
    def test_latin_part(self):
        self.assertEqual(B.latin_part("আমার ভিনদেশী তারা | Amar Bhindeshi Tara | Chandrabindoo"),
                         "Amar Bhindeshi Tara | Chandrabindoo")
        self.assertEqual(B.latin_part("আমার ভিনদেশী তারা (অফিসিয়াল ভিডিও)"), "")

    def test_speakable(self):
        self.assertEqual(B.speakable("Shape of You"), "Shape of You")
        self.assertEqual(B.speakable("আমার ভিনদেশী তারা"), "amar bhindeshi tara")
        self.assertEqual(B.speakable("ঈশ্বর - Vikings"), "Vikings")


class CarrierWords(unittest.TestCase):
    def test_song_request(self):
        self.assertEqual(B.song_request("আমার ভিনদেশী তারা বাজাও"), "আমার ভিনদেশী তারা")
        self.assertEqual(B.song_request("প্লে আমার ভিনদেশী তারা"), "আমার ভিনদেশী তারা")
        self.assertEqual(B.song_request("সাইরাস ঈশ্বর গানটা চালাও"), "ঈশ্বর")
        self.assertEqual(B.song_request("আমার সোনার বাংলা গানটা একটু বাজাও তো"), "আমার সোনার বাংলা")
        self.assertEqual(B.song_request("একটা গান বাজাও"), "")
        self.assertIsNone(B.song_request("আমার ভিনদেশী তারা"))              # no "play" in it ...
        self.assertEqual(B.song_request("আমার ভিনদেশী তারা", hinted=True), "আমার ভিনদেশী তারা")   # ... unless known
        self.assertIsNone(B.song_request("আজ আবহাওয়া কেমন"))


class BanglaSongFlow(unittest.TestCase):
    def armed(self):
        e, ns = cap_engine()
        e.handle("cyrus", "mic")
        return e, ns

    @staticmethod
    def bn(text, mode="command", song_hint=False):
        return CapturedTranscript(text=text, mode=mode, free_text=text, peak=0.3, language="bn",
                                  song_hint=song_hint, raw=text)

    def test_play_with_a_bangla_title(self):
        e, ns = self.armed()
        e.handle_transcript(self.bn("প্লে আমার ভিনদেশী তারা", song_hint=True))
        self.assertEqual(ns.actions.called("find_song"), [("আমার ভিনদেশী তারা",)])
        self.assertEqual(ns.speaker.said[-1], "Playing amar bhindeshi tara, sir.")
        self.assertEqual(e.snapshot().mode, "idle")

    def test_bangla_carrier_words(self):
        for text, title in (("আমার ভিনদেশী তারা বাজাও", "আমার ভিনদেশী তারা"), ("ঈশ্বর গানটা চালাও", "ঈশ্বর")):
            with self.subTest(text=text):
                e, ns = self.armed()
                e.handle_transcript(self.bn(text))
                self.assertEqual(ns.actions.called("find_song"), [(title,)])

    def test_play_without_a_name_asks_and_takes_a_bangla_answer(self):
        e, ns = self.armed()
        e.handle_transcript(self.bn("একটা গান বাজাও"))
        self.assertEqual(ns.speaker.said[-1], "What should I play, sir?")
        e.handle_transcript(self.bn("আমার সোনার বাংলা", mode="free", song_hint=True))
        self.assertEqual(ns.actions.called("find_song"), [("আমার সোনার বাংলা",)])
        self.assertEqual(ns.speaker.said[-1], "Playing amar sonar bangla, sir.")

    def test_english_question_bangla_answer(self):
        e, ns = self.armed()
        e.handle_transcript(said("Play some music."))
        e.handle_transcript(self.bn("ঈশ্বর", mode="free", song_hint=True))
        self.assertEqual(ns.actions.called("find_song"), [("ঈশ্বর",)])

    def test_bangla_that_is_not_a_song_request(self):
        e, ns = self.armed()
        e.handle_transcript(self.bn("আজ আবহাওয়া কেমন"))
        self.assertEqual(ns.speaker.said[-1], "Sorry, I didn't catch that, sir.")
        self.assertEqual(e.snapshot().mode, "idle")
        self.assertEqual(ns.actions.called("find_song"), [])

    def test_song_not_found_is_spoken_in_banglish(self):
        e, ns = self.armed()
        ns.actions.returns["find_song"] = LookupError("nothing")
        e.handle_transcript(self.bn("প্লে আমার ভিনদেশী তারা", song_hint=True))
        self.assertIn("amar bhindeshi tara", ns.speaker.said[-1])
        self.assertFalse(B.has_bengali(ns.speaker.said[-1]))


class RecognizerHearsBangla(unittest.TestCase):
    """Recognizer._hear_command / request_language_check with a FakeHearing (no Vosk decoding needed)."""

    def make(self, hearing):
        from tests.test_capture import wire
        from xyrus.recognizer import Recognizer
        e, ns = cap_engine()
        rec = Recognizer("unused", None, None, get_spec=e.listen_spec, on_transcript=e.handle_transcript,
                         speaker=None, chime=None, config=ns.config)
        rec.set_command_hearing(hearing)
        wire(e, rec)
        return e, ns, rec

    @staticmethod
    def utt():
        return Utterance(pcm=b"\x10\x27" * 32000, t_start=10.0, t_end=12.0, peak=0.3, speech_s=1.5, reason="silence")

    def test_a_song_request_in_bangla_is_heard_in_bengali(self):
        h = FakeHearing(en="Play my foreign star.", bn="প্লে আমার ভিনদেশী তারা।",
                        detected=("bn", 0.71, [("bn", 0.71), ("en", 0.2)]))
        e, ns, rec = self.make(h)
        tr = rec._hear_command(self.utt(), ListenSpec("command"))
        self.assertEqual((tr.language, tr.text, tr.song_hint), ("bn", "প্লে আমার ভিনদেশী তারা", True))
        self.assertEqual(len(h.pcms), 1, "one encoding for both decodes")
        self.assertEqual([d[0] for d in h.decodes], ["en", "bn"])
        e.handle("cyrus", "mic")
        e.handle_transcript(tr)
        self.assertEqual(ns.actions.called("find_song"), [("আমার ভিনদেশী তারা",)])

    def test_hindi_song_stays_on_the_english_path(self):
        """Lead, Sep 14: a Hindi song ("Tum Hi Ho") must not be searched in Bengali script - the English path
        hears Hindi titles well (Whisper bench: "Tum Hi Ho", "Kesariya by Arijit Singh")."""
        h = FakeHearing(en="Play Tum Hi Ho.", bn="never used", detected=("hi", 0.8, [("hi", 0.8), ("bn", 0.05)]))
        e, ns, rec = self.make(h)
        self.assertEqual(rec._hear_command(self.utt(), ListenSpec("command")).language, "en")

    def test_bangla_confused_with_hindi_still_counts(self):
        h = FakeHearing(en="Play Tomake Chai.", bn="প্লে তোমাকে চাই",
                        detected=("hi", 0.45, [("hi", 0.45), ("bn", 0.35)]))
        e, ns, rec = self.make(h)
        self.assertEqual(rec._hear_command(self.utt(), ListenSpec("command")).language, "bn")

    def test_an_english_song_stays_english(self):
        h = FakeHearing(en="Play Shape of You by Ed Sheeran.", bn="never used")
        e, ns, rec = self.make(h)
        tr = rec._hear_command(self.utt(), ListenSpec("command"))
        self.assertEqual((tr.language, tr.free_text), ("en", "play shape of you by ed sheeran"))
        self.assertEqual(h.detections, 1)
        self.assertEqual([d[0] for d in h.decodes], ["en"])

    def test_no_language_check_for_other_commands(self):
        h = FakeHearing(en="What time is it?")
        e, ns, rec = self.make(h)
        rec._hear_command(self.utt(), ListenSpec("command"))
        self.assertEqual(h.detections, 0)

    def test_the_song_question_answer_is_checked(self):
        h = FakeHearing(en="My foreign star.", bn="আমার ভিনদেশী তারা", detected=("bn", 0.8, []))
        e, ns, rec = self.make(h)
        e.handle("play some music", "typed")
        tr = rec._hear_command(self.utt(), ListenSpec("free"))
        self.assertEqual(tr.language, "bn")
        e.handle_transcript(tr)
        self.assertEqual(ns.actions.called("find_song"), [("আমার ভিনদেশী তারা",)])

    def test_a_bengali_decode_without_bengali_script_is_ignored(self):
        h = FakeHearing(en="Play Believer.", bn="अग्वावावा", detected=("bn", 0.6, []))
        e, ns, rec = self.make(h)
        self.assertEqual(rec._hear_command(self.utt(), ListenSpec("command")).language, "en")

    def test_unmatched_english_is_checked_for_bangla(self):
        h = FakeHearing(en="Our foreign star, bring it.", bn="আমার ভিনদেশী তারা বাজাও", detected=("bn", 0.9, []))
        e, ns, rec = self.make(h)
        e.handle("cyrus", "mic")
        tr = rec._hear_command(self.utt(), ListenSpec("command"))
        self.assertEqual(tr.language, "en")
        rec._remember(self.utt().pcm, tr.t_start, tr.t_end)
        e.handle_transcript(tr)                              # no command matches -> Bangla check (inline here)
        self.assertEqual(ns.actions.called("find_song"), [("আমার ভিনদেশী তারা",)], ns.speaker.said)
        self.assertEqual(len(h.pcms), 1, "the check reuses the encoding")


class YouTubeSearchOrder(unittest.TestCase):
    def test_bengali_title_first_then_banglish(self):
        queries = []

        def search(q, timeout=6):
            queries.append(q)
            return [] if B.has_bengali(q) else [("abcdefghijk", "আমার ভিনদেশী তারা | Amar Bhindeshi Tara | Chandrabindoo",
                                                  "Chandrabindoo", 250)]
        with mock.patch.object(aweb, "youtube_search", search):
            spoken, url, title = aweb.find_song("আমার ভিনদেশী তারা")
        self.assertEqual(queries, ["আমার ভিনদেশী তারা", "amar bhindeshi tara"])
        self.assertEqual(spoken, "Amar Bhindeshi Tara")
        self.assertTrue(url.endswith("abcdefghijk"))

    def test_found_with_the_bengali_title(self):
        queries = []

        def search(q, timeout=6):
            queries.append(q)
            return [("abcdefghijk", "ভিনদেশী তারা - চন্দ্রবিন্দু", "", 250)]
        with mock.patch.object(aweb, "youtube_search", search):
            spoken, _, _ = aweb.find_song("আমার ভিনদেশী তারা")
            options = aweb.find_songs("আমার ভিনদেশী তারা")
        self.assertEqual(queries, ["আমার ভিনদেশী তারা", "আমার ভিনদেশী তারা"])
        self.assertFalse(B.has_bengali(spoken), spoken)
        self.assertIn("bhindeshi tara", spoken)
        self.assertFalse(B.has_bengali(options[0][0]))

    def test_english_songs_unchanged(self):
        with mock.patch.object(aweb, "youtube_search",
                               lambda q, timeout=6: [("abcdefghijk", "Alan Walker - Alone (Official Video)", "", 200)]):
            self.assertEqual(aweb.find_song("alone")[0], "Alone by Alan Walker")


class HearingClean(unittest.TestCase):
    def test_bengali_words_stay_whole(self):
        import hearing                                        # the module only; no model is loaded
        self.assertEqual(hearing.clean("আমার ভিনদেশী তারা।"), "আমার ভিনদেশী তারা")
        self.assertEqual(hearing.clean("Xyrus, play Ishwar by Vikings."), "xyrus play ishwar by vikings")


if __name__ == "__main__":
    unittest.main()
