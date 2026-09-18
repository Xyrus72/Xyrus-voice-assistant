"""Song hearing: Whisper for free text (stubbed here), "not that one" plays the next result, and the
full-vocabulary fallback hands the audio on. No network, no real browser - every action is stubbed."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import config; config.load()
import arc as A
import hearing as H

fails = 0


def check(name, cond, detail=""):
    global fails
    print(("  ok   " if cond else "  FAIL ") + name + (f"   [{detail}]" if detail and not cond else ""))
    fails += not cond


opened, media = [], []
A.actions.open_target = lambda t: opened.append(t)
A.actions.browser_playing = lambda: False
A.actions.ensure_playing = lambda video, **kw: "autoplay"
A.actions.media_play = lambda: media.append("play")
RESULTS = {"ishwar by vikings": [
    ("Ishwar by Vikings", "https://www.youtube.com/watch?v=AAA", "Ishwar I Vikings I Official Music Video"),
    ("Ishwar by Meet The Fan", "https://www.youtube.com/watch?v=BBB", "Ishwar Lyrics | Vikings"),
    ("Ishwar by Music Castle", "https://www.youtube.com/watch?v=CCC", "Ishwar - VIKINGS [Tribute]"),
]}
A.actions.find_song = lambda q: RESULTS[q][0] if q in RESULTS else (q.title(), "https://www.youtube.com/watch?v=ZZZ", q)
A.actions.find_songs = lambda q: RESULTS.get(q, [A.actions.find_song(q)])
PCM = b"\0" * 32000


def fresh():
    a = A.Arc()
    a.run_async = lambda fn: fn()
    return a


said = lambda a: ([t for _, k, t in a.events if k == "say"] or [""])[-1]
real_available, real_transcribe = H.available, H.transcribe

print("Whisper hears the name")
H.available = lambda wait=0.0: True
H.transcribe = lambda pcm, **kw: "Xyrus, play Ishwar by Vikings."
a = fresh()
a.free_decode = lambda pcm: "ios play issue war"          # what the small model heard in the real log
a.handle("cyrus play [unk]", PCM)
check("'play [unk]' -> Whisper -> plays Ishwar by Vikings",
      opened[-1:] == ["https://www.youtube.com/watch?v=AAA"] and said(a) == "Playing Ishwar by Vikings, sir.", (opened, said(a)))
opened.clear()
a = fresh()
a.free_decode = lambda pcm: "cyrus play issue war"
a.handle("cyrus [unk]", PCM)
check("garbled grammar -> full-vocabulary retry -> Whisper re-hears the title",
      opened[-1:] == ["https://www.youtube.com/watch?v=AAA"], (opened, said(a)))
H.transcribe = lambda pcm, **kw: "Remind me to call the dentist tomorrow at five."
b = A.Arc(calendar=__import__("calendar_store").CalendarStore(Path(__import__("tempfile").mkdtemp()) / "c.json"))
b.free_decode = lambda pcm: "remind me to call the then test tomorrow at five"
b.handle("cyrus remind [unk] tomorrow add five", PCM)
it = next(iter(b.calendar.data["items"]), {})
check("calendar titles use Whisper too", it.get("title") == "Call the dentist", (it, said(b)))

print("'not that one'")
a.handle("not that one")
check("no wake word needed right after a song: next result",
      opened[-1] == "https://www.youtube.com/watch?v=BBB" and said(a) == "Trying Ishwar by Meet The Fan, sir.", (opened, said(a)))
a.handle("cyrus wrong song")
check("'wrong song' -> the result after that", opened[-1] == "https://www.youtube.com/watch?v=CCC", opened)
a.handle("cyrus another one")
check("out of results -> says so", said(a).startswith("That's all I found for ishwar by vikings"), said(a))
c = fresh()
c.handle("not that one")
check("'not that one' before any song does nothing", not c.events or said(c) == "", said(c))
a.song_followup_until = 0
n = len(opened)
a.handle("another one")
check("outside the 40 s window it needs the wake word", len(opened) == n)

print("without Whisper (not installed / not loaded)")
H.available = lambda wait=0.0: False
opened.clear()
d = fresh()
d.free_decode = lambda pcm: "cyrus play alone"
d.handle("cyrus play [unk]", PCM)
check("falls back to the small model's full vocabulary, as before", said(d).startswith("Playing Alone"), said(d))
check("clean()", H.clean("Xyrus, play Ishwar by Vikings.") == "xyrus play ishwar by vikings")
H.available, H.transcribe = real_available, real_transcribe

print("HEARING TESTS", "OK" if not fails else f"FAILED ({fails})")
sys.exit(1 if fails else 0)
