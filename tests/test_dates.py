"""xyrus.dates — v1 when.py's self-test table (57 cases + extra assertions), the parse_range spans of
§4.11 and v1 test_calendar.py, and the date-parsing rows of §5.6. Pure: writes nothing."""
from __future__ import annotations

import datetime as dt
import unittest

from xyrus import dates as W

NOW = dt.datetime(2026, 9, 13, 10, 30)     # Sunday 13 Sep 2026, 10:30
WED = dt.datetime(2026, 9, 16, 10, 0)      # Wednesday 16 Sep 2026, 10:00
D = dt.datetime
d = dt.date

# (spoken text, expected start, expected all_day, expected title) - verbatim from v1 when.py
CASES = [
    ("dentist tomorrow at three pm",              D(2026, 9, 14, 15, 0), False, "dentist"),
    ("dentist tomorrow three pm",                 D(2026, 9, 14, 15, 0), False, "dentist"),
    ("remind me at five to call mom",             D(2026, 9, 13, 17, 0), False, "call mom"),
    ("remind me at seven to call mom",            D(2026, 9, 13, 19, 0), False, "call mom"),   # 07:00 passed -> 19:00
    ("remind me at eleven to check the oven",     D(2026, 9, 13, 11, 0), False, "check the oven"),
    ("remind me in twenty minutes to check the oven", D(2026, 9, 13, 10, 50), False, "check the oven"),
    ("remind me in two hours to stretch",         D(2026, 9, 13, 12, 30), False, "stretch"),
    ("in forty five minutes",                     D(2026, 9, 13, 11, 15), False, ""),
    ("in half an hour",                           D(2026, 9, 13, 11, 0), False, ""),
    ("meeting next friday at ten am",             D(2026, 9, 18, 10, 0), False, "meeting"),   # Sunday: next week's Fri = 18th
    ("add event team meeting next friday at ten am", D(2026, 9, 18, 10, 0), False, "team meeting"),
    ("remind me to submit the assignment tomorrow at nine", D(2026, 9, 14, 9, 0), False, "submit the assignment"),
    ("add a note that the rent is due on the first", D(2026, 10, 1, 9, 0), True, "the rent is due"),
    ("meeting friday at ten am",                  D(2026, 9, 18, 10, 0), False, "meeting"),
    ("meeting on friday",                         D(2026, 9, 18, 9, 0), True, "meeting"),
    ("monday at ten am",                          D(2026, 9, 14, 10, 0), False, ""),
    ("this monday",                               D(2026, 9, 14, 9, 0), True, ""),
    ("party tonight at eight",                    D(2026, 9, 13, 20, 0), False, "party"),
    ("tonight",                                   D(2026, 9, 13, 20, 0), False, ""),
    ("this evening",                              D(2026, 9, 13, 18, 0), False, ""),
    ("friday evening",                            D(2026, 9, 18, 18, 0), False, ""),
    ("tomorrow morning",                          D(2026, 9, 14, 9, 0), False, ""),
    ("gym tomorrow morning at six",               D(2026, 9, 14, 6, 0), False, "gym"),
    ("at five thirty pm",                         D(2026, 9, 13, 17, 30), False, ""),
    ("october three at two pm",                   D(2026, 10, 3, 14, 0), False, ""),
    ("october third at two pm",                   D(2026, 10, 3, 14, 0), False, ""),
    ("on the twentieth at nine thirty",           D(2026, 9, 20, 9, 30), False, ""),
    ("on the twentieth at nine thirty pm",        D(2026, 9, 20, 21, 30), False, ""),
    ("the day after tomorrow at noon",            D(2026, 9, 15, 12, 0), False, ""),
    ("day after tomorrow",                        D(2026, 9, 15, 9, 0), True, ""),
    ("today at six",                              D(2026, 9, 13, 18, 0), False, ""),
    ("the fifteenth of october",                  D(2026, 10, 15, 9, 0), True, ""),
    ("exam on the fifteenth of october at nine",  D(2026, 10, 15, 9, 0), False, "exam"),
    ("at seven",                                  D(2026, 9, 13, 19, 0), False, ""),
    ("at three",                                  D(2026, 9, 13, 15, 0), False, ""),
    ("tomorrow at three",                         D(2026, 9, 14, 15, 0), False, ""),   # future day: 1-6 -> pm
    ("tomorrow at eight",                         D(2026, 9, 14, 8, 0), False, ""),    # future day: 7-11 -> am
    ("tomorrow at twelve",                        D(2026, 9, 14, 12, 0), False, ""),
    ("at midnight",                               D(2026, 9, 14, 0, 0), False, ""),    # next midnight
    ("half past seven tomorrow",                  D(2026, 9, 14, 7, 30), False, ""),
    ("quarter to eight in the evening",           D(2026, 9, 13, 19, 45), False, ""),
    ("the tenth",                                 D(2026, 10, 10, 9, 0), True, ""),    # 10th passed -> next month
    ("on the twenty first",                       D(2026, 9, 21, 9, 0), True, ""),
    ("next week",                                 D(2026, 9, 14, 9, 0), True, ""),     # range mon..sun
    ("this week",                                 D(2026, 9, 13, 9, 0), True, ""),
    ("this weekend",                              D(2026, 9, 19, 9, 0), True, ""),
    ("standup every day at nine thirty am",       D(2026, 9, 14, 9, 30), False, "standup"),
    ("gym every monday at six pm",                D(2026, 9, 14, 18, 0), False, "gym"),
    ("dentist tomorrow at three pm thirty minutes before", D(2026, 9, 14, 15, 0), False, "dentist"),
    ("pay rent on the first of october",          D(2026, 10, 1, 9, 0), True, "pay rent"),
    ("in three days",                             D(2026, 9, 16, 10, 30), True, ""),
    # typed (quick-add row)
    ("dentist tomorrow 3pm",                      D(2026, 9, 14, 15, 0), False, "dentist"),
    ("Dentist tomorrow 3:30pm",                   D(2026, 9, 14, 15, 30), False, "dentist"),
    ("call bank mon 10am",                        D(2026, 9, 14, 10, 0), False, "call bank"),
    ("flight oct 3 at 14:20",                     D(2026, 10, 3, 14, 20), False, "flight"),
    ("rent 10/1",                                 D(2026, 10, 1, 9, 0), True, "rent"),
    ("exam on the 20th at 9:30",                  D(2026, 9, 20, 9, 30), False, "exam"),
]


class WhenTable(unittest.TestCase):
    def test_table_has_57_cases(self):
        self.assertEqual(len(CASES), 57)

    def test_v1_cases(self):
        bad = []
        for text, start, all_day, title in CASES:
            with self.subTest(text=text):
                w = W.parse_when(text, NOW)
                got = (w.start, w.all_day, w.title) if w else None
                if got != (start, all_day, title):
                    bad.append((text, got, (start, all_day, title)))
                self.assertEqual(got, (start, all_day, title))
        self.assertFalse(bad, f"{57 - len(bad)}/57 pass")

    def test_v1_extras(self):
        self.assertEqual(W.parse_when("standup every day at nine thirty am", NOW).repeat, "daily")
        self.assertEqual(W.parse_when("gym every monday at six pm", NOW).repeat, "weekly")
        self.assertEqual(W.parse_when("dentist tomorrow at three pm thirty minutes before", NOW).reminder_min, 30)
        w = W.parse_when("next week", NOW)
        self.assertEqual((w.start.date(), w.end), (d(2026, 9, 14), d(2026, 9, 20)))
        w = W.parse_when("this week", NOW)
        self.assertEqual((w.start.date(), w.end), (d(2026, 9, 13), d(2026, 9, 13)))   # Sunday
        self.assertIsNone(W.parse_when("call mom", NOW))
        self.assertIsNone(W.parse_when("", NOW))
        self.assertEqual(W.say_when(W.parse_when("dentist tomorrow at three pm", NOW), NOW), "tomorrow at 3 pm")
        self.assertEqual(W.say_when(W.parse_when("half past seven tomorrow", NOW), NOW), "tomorrow at 7:30 am")
        self.assertEqual(W.say_when(W.parse_when("meeting friday at ten am", NOW), NOW), "Friday at 10 am")
        self.assertEqual(W.say_when(W.parse_when("october three at two pm", NOW), NOW), "Saturday, October 3 at 2 pm")
        self.assertEqual(W.say_when(W.parse_when("the day after tomorrow at noon", NOW), NOW), "Tuesday at noon")
        self.assertEqual(W.parse_when("at seven", D(2026, 9, 13, 21, 30)).start, D(2026, 9, 14, 7, 0))
        self.assertEqual(W.parse_when("friday at three pm", D(2026, 9, 18, 10, 0)).start, D(2026, 9, 18, 15, 0))
        self.assertEqual(W.parse_when("friday at three pm", D(2026, 9, 18, 16, 0)).start, D(2026, 9, 25, 15, 0))
        self.assertEqual(W.parse_when("friday", WED).start.date(), d(2026, 9, 18))
        self.assertEqual(W.parse_when("next friday", WED).start.date(), d(2026, 9, 25))
        self.assertEqual(W.parse_when("this friday", WED).start.date(), d(2026, 9, 18))
        self.assertEqual(W.parse_when("monday", WED).start.date(), d(2026, 9, 21))
        self.assertEqual(W.parse_when("next monday", WED).start.date(), d(2026, 9, 21))
        self.assertEqual(W.parse_when("next week", WED).start.date(), d(2026, 9, 21))
        self.assertEqual(W.parse_when("this week", WED).end, d(2026, 9, 20))
        self.assertIsNone(W.parse_when("dentist", NOW))
        self.assertIsNone(W.parse_when("add event dentist", NOW))


class Ranges(unittest.TestCase):
    def test_spec_4_11_spans(self):
        # said on Sunday 13 Sep 2026
        for text, want in [
            ("this week", (d(2026, 9, 13), d(2026, 9, 13), "this week")),
            ("next three days", (d(2026, 9, 13), d(2026, 9, 15), "in the next 3 days")),
            ("these 3 days", (d(2026, 9, 13), d(2026, 9, 15), "in the next 3 days")),
            ("the next ten days", (d(2026, 9, 13), d(2026, 9, 22), "in the next 10 days")),
            ("next two weeks", (d(2026, 9, 13), d(2026, 9, 26), "in the next 2 weeks")),
            ("a week", (d(2026, 9, 13), d(2026, 9, 19), "in the next week")),
            ("rest of the week", (d(2026, 9, 13), d(2026, 9, 13), "for the rest of the week")),
            ("rest of the month", (d(2026, 9, 13), d(2026, 9, 30), "for the rest of the month")),
            ("tomorrow", (d(2026, 9, 14), d(2026, 9, 14), "tomorrow")),
            ("on friday", (d(2026, 9, 18), d(2026, 9, 18), "on Friday")),
            ("on the twentieth", (d(2026, 9, 20), d(2026, 9, 20), "on Sunday, September 20")),
            ("this weekend", (d(2026, 9, 19), d(2026, 9, 20), "this weekend")),
            ("this month", (d(2026, 9, 13), d(2026, 9, 30), "this month")),
            ("in three days", (d(2026, 9, 16), d(2026, 9, 16), "on Wednesday")),     # one day, not a span
        ]:
            with self.subTest(text=text):
                self.assertEqual(W.parse_range(text, NOW), want)

    def test_v1_test_calendar_spans(self):
        # verbatim from v1 test_calendar.py (said on Wednesday 16 Sep 2026)
        for text, want in [
            ("what do i have this week", (d(2026, 9, 16), d(2026, 9, 20), "this week")),
            ("what do i have next week", (d(2026, 9, 21), d(2026, 9, 27), "next week")),
            ("what do i have in the next three days", (d(2026, 9, 16), d(2026, 9, 18), "in the next 3 days")),
            ("what do i have these 3 days", (d(2026, 9, 16), d(2026, 9, 18), "in the next 3 days")),
            ("what do i have for the next ten days", (d(2026, 9, 16), d(2026, 9, 25), "in the next 10 days")),
            ("the next twenty one days", (d(2026, 9, 16), d(2026, 10, 6), "in the next 21 days")),
            ("next two weeks", (d(2026, 9, 16), d(2026, 9, 29), "in the next 2 weeks")),
            ("the next few days", (d(2026, 9, 16), d(2026, 9, 18), "in the next 3 days")),
            ("rest of the week", (d(2026, 9, 16), d(2026, 9, 20), "for the rest of the week")),
            ("tomorrow", (d(2026, 9, 17), d(2026, 9, 17), "tomorrow")),
            ("on friday", (d(2026, 9, 18), d(2026, 9, 18), "on Friday")),
            ("in three days", (d(2026, 9, 19), d(2026, 9, 19), "on Saturday")),
            ("this weekend", (d(2026, 9, 19), d(2026, 9, 20), "this weekend")),
            ("what do i have at the weekend", (d(2026, 9, 19), d(2026, 9, 20), "this weekend")),
            ("this month", (d(2026, 9, 16), d(2026, 9, 30), "this month")),
            ("on october third", (d(2026, 10, 3), d(2026, 10, 3), "on Saturday, October 3")),
        ]:
            with self.subTest(text=text):
                self.assertEqual(W.parse_range(text, WED), want)
        self.assertIsNone(W.parse_range("what do i have", WED))


class Normalisation(unittest.TestCase):
    """§5.6 rows that live in dates.normalise, and the other §4.11 additions."""

    def test_add_heard_for_at(self):
        self.assertEqual(W.normalise("add half past seven"), ["at", "half", "past", "7"])
        self.assertEqual(W.parse_when("add half past seven tomorrow", NOW).start, D(2026, 9, 14, 7, 30))
        self.assertEqual(W.parse_when("dentist tomorrow add three pm", NOW).start, D(2026, 9, 14, 15, 0))
        self.assertEqual(W.parse_when("dentist tomorrow add three pm", NOW).title, "dentist")
        self.assertEqual(W.parse_when("add seven", D(2026, 9, 13, 14, 0)).start, D(2026, 9, 13, 19, 0))
        self.assertEqual(W.parse_when("add noon", NOW).start, D(2026, 9, 13, 12, 0))
        self.assertEqual(W.parse_when("gym add quarter past six pm", NOW).start, D(2026, 9, 13, 18, 15))

    def test_add_command_is_not_a_time(self):
        self.assertEqual(W.normalise("add buy milk to my to do list")[0], "add")
        self.assertEqual(W.normalise("add two eggs to my to do list")[0], "add")
        self.assertIsNone(W.parse_when("add two eggs to my to do list", NOW))
        self.assertEqual(W.normalise("add an event")[0], "add")

    def test_ordinal_after_at_is_an_hour(self):
        self.assertEqual(W.normalise("at eighth"), ["at", "8"])
        self.assertEqual(W.parse_when("tomorrow at eighth", NOW).start, D(2026, 9, 14, 8, 0))
        self.assertEqual(W.parse_when("add eighth tomorrow", NOW).start, D(2026, 9, 14, 8, 0))
        # ordinals elsewhere stay days
        self.assertEqual(W.parse_when("on the eighth", NOW).start.date(), d(2026, 10, 8))

    def test_waking_hours_parameter(self):
        early = D(2026, 9, 13, 5, 0)
        self.assertEqual(W.parse_when("at six", early).start, D(2026, 9, 13, 18, 0))           # default 7-23
        self.assertEqual(W.parse_when("at six", early, waking=(5, 23)).start, D(2026, 9, 13, 6, 0))
        self.assertEqual(W.parse_when("at six", early, waking=[5, 23]).start, D(2026, 9, 13, 6, 0))
        self.assertEqual(W.infer_hour(6, 0, None, early.date(), early, (5, 23)), 6)
        self.assertEqual(W.parse_range("tomorrow", NOW, waking=(5, 23))[0], d(2026, 9, 14))

    def test_v2_intent_prefixes(self):
        for text, title in [
            ("add a to do buy milk tomorrow", "buy milk"),
            ("add a task finish the report friday", "finish the report"),
            ("new to do call the plumber tomorrow", "call the plumber"),
            ("take a note bring the charger tomorrow", "bring the charger"),
            ("note for tomorrow bring the charger", "bring the charger"),
            ("add a note for friday bring the charger", "bring the charger"),
            ("add in event dentist tomorrow at three pm", "dentist"),
            ("add and event dentist tomorrow at three pm", "dentist"),
            ("remind me again in ten minutes", ""),
        ]:
            with self.subTest(text=text):
                self.assertEqual(W.parse_when(text, NOW).title, title)
        for p in ("add in event", "add and event", "remind me again", "take a note", "note for", "add a note for",
                  "add a to do", "add a task", "new to do", "to do", "on my to do list", "to my to do list"):
            self.assertIn(p, W.INTENT_PREFIXES)
        self.assertEqual(W.INTENT_PREFIXES, sorted(W.INTENT_PREFIXES, key=len, reverse=True))


class Helpers(unittest.TestCase):
    def test_fmt_time(self):
        self.assertEqual(W.fmt_time("15:00"), "3 pm")
        self.assertEqual(W.fmt_time("09:30"), "9:30 am")
        self.assertEqual(W.fmt_time("12:00"), "noon")
        self.assertEqual(W.fmt_time("00:00"), "midnight")
        self.assertEqual(W.fmt_time(None), "")

    def test_day_name_and_label(self):
        today = d(2026, 9, 16)
        self.assertEqual(W.day_name(today, today), "today")
        self.assertEqual(W.day_name(d(2026, 9, 17), today), "tomorrow")
        self.assertEqual(W.day_name(d(2026, 9, 15), today), "yesterday")
        self.assertEqual(W.day_name(d(2026, 9, 18), today), "Friday")
        self.assertEqual(W.day_name(d(2026, 10, 3), today), "Saturday, October 3")
        self.assertEqual(W.day_name(d(2027, 1, 2), today), "Saturday, January 2 2027")
        self.assertEqual(W.day_label(d(2026, 9, 18), today), "on Friday")
        self.assertEqual(W.day_label(d(2026, 9, 17), today), "tomorrow")

    def test_mentions_date(self):
        for t in ("dentist tomorrow 3pm", "gym on friday", "rent on the 1st", "exam in 3 days", "call mom this week"):
            self.assertTrue(W.mentions_date(t), t)
        for t in ("dentist 3pm", "buy milk", "call at 5 pm"):
            self.assertFalse(W.mentions_date(t), t)

    def test_parse_typed(self):
        for text in ("dentist tomorrow 3pm", "call bank mon 10am", "rent 10/1"):
            a, b = W.parse_typed(text, NOW), W.parse_when(text, NOW)
            self.assertEqual((a.start, a.all_day, a.title), (b.start, b.all_day, b.title))
        self.assertIsNone(W.parse_typed("", NOW))
        try:
            import parsedatetime  # noqa: F401
        except ImportError:
            self.assertIsNone(W.parse_typed("call mom", NOW))     # no fallback installed -> None, never raises
        else:
            w = W.parse_typed("lunch on 2026-10-05", NOW)
            self.assertTrue(w is None or w.start.date() == d(2026, 10, 5))

    def test_grammar_word_sets(self):
        for w in ("monday", "october", "twentieth", "today", "tomorrow", "tonight", "week", "next", "every"):
            self.assertIn(w, W.DATE_WORDS)
        for w in ("am", "pm", "noon", "midnight", "half", "quarter", "past", "o'clock", "three", "thirty", "at"):
            self.assertIn(w, W.TIME_WORDS)
        self.assertTrue(all(" " not in w and w == w.lower() for w in W.DATE_WORDS | W.TIME_WORDS))
        self.assertNotIn("oclock", W.TIME_WORDS)          # D13: not in the model

    def test_pure_never_reads_clock_when_now_given(self):
        # a far-past `now` must be honoured (would differ if the real clock were read)
        past = D(2001, 1, 1, 10, 0)
        self.assertEqual(W.parse_when("tomorrow at nine", past).start, D(2001, 1, 2, 9, 0))


if __name__ == "__main__":
    unittest.main()
