"""Calendar + to-do list: date ranges, store, spoken summaries, voice commands (typed and spoken),
reminders and the daily briefing. Uses temp calendar files - never the real data/calendar.json."""
import datetime as dt
import json
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import config; config.load()
import arc as A
import calendar_store as CS
import when as W

D = dt.date
WED = dt.datetime(2026, 9, 16, 10, 0)          # Wednesday 16 Sep 2026, 10:00
fails = 0


def check(name, cond, detail=""):
    global fails
    print(("  ok   " if cond else "  FAIL ") + name + (f"   [{detail}]" if detail and not cond else ""))
    fails += not cond


def temp_store():
    return CS.CalendarStore(Path(tempfile.mkdtemp()) / "calendar.json")


# ------------------------------------------------------------------ ranges ---
print("date ranges (said on Wednesday 16 Sep 2026)")
for text, want in [
    ("what do i have this week", (D(2026, 9, 16), D(2026, 9, 20), "this week")),
    ("what do i have next week", (D(2026, 9, 21), D(2026, 9, 27), "next week")),
    ("what do i have in the next three days", (D(2026, 9, 16), D(2026, 9, 18), "in the next 3 days")),
    ("what do i have these 3 days", (D(2026, 9, 16), D(2026, 9, 18), "in the next 3 days")),
    ("what do i have for the next ten days", (D(2026, 9, 16), D(2026, 9, 25), "in the next 10 days")),
    ("the next twenty one days", (D(2026, 9, 16), D(2026, 10, 6), "in the next 21 days")),
    ("next two weeks", (D(2026, 9, 16), D(2026, 9, 29), "in the next 2 weeks")),
    ("the next few days", (D(2026, 9, 16), D(2026, 9, 18), "in the next 3 days")),
    ("rest of the week", (D(2026, 9, 16), D(2026, 9, 20), "for the rest of the week")),
    ("tomorrow", (D(2026, 9, 17), D(2026, 9, 17), "tomorrow")),
    ("on friday", (D(2026, 9, 18), D(2026, 9, 18), "on Friday")),
    ("in three days", (D(2026, 9, 19), D(2026, 9, 19), "on Saturday")),
    ("this weekend", (D(2026, 9, 19), D(2026, 9, 20), "this weekend")),
    ("what do i have at the weekend", (D(2026, 9, 19), D(2026, 9, 20), "this weekend")),
    ("this month", (D(2026, 9, 16), D(2026, 9, 30), "this month")),
    ("on october third", (D(2026, 10, 3), D(2026, 10, 3), "on Saturday, October 3")),
]:
    got = W.parse_range(text, WED)
    check(f"{text!r:42} -> {got}", got == want, f"want {want}")
check("no range words -> None (caller uses today)", W.parse_range("what do i have", WED) is None)

# ------------------------------------------------------------------- store ---
print("store + spoken summaries")
st = temp_store()
milk = st.add_task("Buy milk", D(2026, 9, 16))
report = st.add_task("Finish the report", D(2026, 9, 15))                   # overdue
dent = st.add_event("Dentist", D(2026, 9, 18), dt.time(15, 0), 10)
gym = st.add_event("Gym", D(2026, 9, 14), dt.time(18, 0), None, repeat="weekly")   # Mondays
st.add_note(D(2026, 9, 16), "Bring the charger")
text, _ = CS.describe_range(st, D(2026, 9, 16), D(2026, 9, 16), "today", WED)
check("today: to-do + overdue + note", all(s in text for s in ("Buy milk", "overdue", "Finish the report", "Bring the charger")), text)
text, _ = CS.describe_range(st, D(2026, 9, 16), D(2026, 9, 22), "in the next 7 days", WED)
check("next 7 days grouped by day", "Friday: Dentist at 3 pm" in text and "Monday: Gym at 6 pm" in text, text)
text, _ = CS.describe_range(st, D(2026, 9, 19), D(2026, 9, 20), "this weekend", WED)
check("empty range says so and names the next thing",
      text.startswith("You have nothing planned this weekend") and "Gym" in text, text)
st.set_done(milk["id"])
check("finished to-dos drop out of the summary",
      "Buy milk" not in CS.describe_range(st, D(2026, 9, 16), D(2026, 9, 16), "today", WED)[0])
check("fuzzy find", (st.find("the report", kinds=("task",)) or {}).get("id") == report["id"])
check("weekly repeat counted on the next Monday", st.counts_by_day(D(2026, 9, 14), D(2026, 9, 21))[D(2026, 9, 21)]["event"] == 1)
check("reloads from disk", len(CS.CalendarStore(st.path).data["items"]) == 4)
check("what's next", CS.describe_next(st, WED) == "Next up: Dentist at 3 pm, Friday.", CS.describe_next(st, WED))
check("to-do list", "Finish the report" in CS.describe_todos(st, WED) and "Overdue" in CS.describe_todos(st, WED),
      CS.describe_todos(st, WED))

# ------------------------------------------------------------ voice, typed ---
print("voice commands (typed - same path as the mic after recognition)")
a = A.Arc(calendar=temp_store())
a.now = lambda: WED
a.run_async = lambda fn: fn()
said = lambda: ([t for _, k, t in a.events if k == "say"] or [""])[-1]


def item(title):
    return next((i for i in a.calendar.data["items"] if i["title"].lower() == title.lower()), None)


a.handle("cyrus add buy milk to my to do list")
it = item("buy milk")
check("add to-do, no day -> today", it and it["kind"] == "task" and it["date"] == "2026-09-16" and it["time"] is None, (it, said()))
a.handle("cyrus add call the bank to my to do list tomorrow")
it = item("call the bank")
check("add to-do for tomorrow", it and it["date"] == "2026-09-17", (it, said()))
a.handle("cyrus remind me to call mom at five")
it = item("call mom")
check("remind me at five -> to-do at 17:00 with a reminder",
      it and it["kind"] == "task" and it["time"] == "17:00" and it["reminder_min"] == 0, (it, said()))
check("  reply", said() == "I'll remind you today at 5 pm: Call mom.", said())
a.handle("cyrus add dentist appointment on friday at three pm")
it = item("dentist appointment")
check("add event Friday 3 pm, reminder 10 min before",
      it and it["kind"] == "event" and it["date"] == "2026-09-18" and it["time"] == "15:00" and it["reminder_min"] == 10, (it, said()))
a.handle("cyrus schedule gym every monday at six pm")
it = item("gym")
check("repeating event", it and it["repeat"] == "weekly" and it["time"] == "18:00", (it, said()))
a.handle("cyrus add a note for friday that the rent is due")
nts = a.calendar.notes_on(D(2026, 9, 18))
check("note on Friday", len(nts) == 1 and nts[0]["text"] == "The rent is due", (nts, said()))

a.handle("cyrus what do i have today")
check("what do I have today", "Buy milk" in said() and "Call mom at 5 pm" in said(), said())
a.handle("cyrus what do i have in the next three days")
check("next three days", said().startswith("In the next 3 days you have") and "Friday: Dentist appointment at 3 pm" in said(), said())
a.handle("cyrus what do i have this week")
check("this week", said().startswith("This week you have"), said())
a.handle("cyrus what's next")
check("what's next", said() == "Next up: Call mom at 5 pm, today.", said())
a.handle("cyrus read my to do list")
check("read my to-do list", said().startswith("You have 3 to-dos, sir."), said())
a.handle("cyrus mark buy milk as done")
check("mark done", item("buy milk")["done"] and said().startswith("Done: Buy milk"), said())
a.handle("cyrus delete the dentist appointment")
check("delete", item("dentist appointment") is None and said().startswith("Deleted Dentist appointment"), said())
a.handle("cyrus undo")
check("undo removes the last voice-added thing (the note)", not a.calendar.notes_on(D(2026, 9, 18)) and said().startswith("Removed"), said())
a.handle("cyrus next")
check("bare 'next' is still next track, not the calendar", "calendar" not in [t for _, k, t in a.events if k == "cmd"][-1])

# ------------------------------------------------------ reminders / briefing ---
print("reminders and briefing")
woke, toasts = [], []
A.actions.screen_on = lambda: woke.append(1)
a.notify = lambda title, message: toasts.append(message)
a.calendar.set_state("briefed", "2026-09-16")
n = len(a.events)
a.calendar_tick(dt.datetime(2026, 9, 16, 16, 59, 30))
check("nothing before it's due", not any(k == "say" for _, k, _t in list(a.events)[n:]))
a.calendar_tick(dt.datetime(2026, 9, 16, 17, 0, 10))
check("reminder fires at 5 pm: speech + toast + screen on",
      said() == "Sir, reminder: Call mom at 5 pm." and toasts and woke, (said(), toasts, woke))
n = len(a.events)
a.calendar_tick(dt.datetime(2026, 9, 16, 17, 0, 40))
check("never twice", not any(k == "say" for _, k, _t in list(a.events)[n:]))

b = A.Arc(calendar=temp_store())
b.calendar.add_task("Water the plants", D(2026, 9, 16))
b_said = lambda: [t for _, k, t in b.events if k == "say"]
b.calendar_tick(dt.datetime(2026, 9, 16, 7, 30))
check("no briefing before 8", not b_said())
b.calendar_tick(dt.datetime(2026, 9, 16, 8, 5))
check("morning briefing once", b_said() == ["Good morning, sir. Today you have 1 to-do: Water the plants."], b_said())
b.calendar_tick(dt.datetime(2026, 9, 16, 8, 10))
check("  and only once", len(b_said()) == 1)

# ----------------------------------------------------------- voice, spoken ---
print("spoken (synthesized speech -> grammar -> full-vocabulary re-decode)")
from vosk import KaldiRecognizer, Model, SetLogLevel
SetLogLevel(-1)
model = Model(str(A.MODEL_DIR))
GRAMMAR = json.dumps(A.build_grammar())
TMP = Path(__file__).parent / "_cal_test.wav"


def speak(text):
    ps = ("Add-Type -AssemblyName System.Speech;"
          "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
          f"$s.SetOutputToWaveFile('{TMP}',(New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000,'Sixteen','Mono')));"
          f"$s.Speak('{text}');$s.Dispose()")
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True, capture_output=True)
    with wave.open(str(TMP), "rb") as w:
        pcm = w.readframes(w.getnframes())
    g = KaldiRecognizer(model, 16000, GRAMMAR)
    g.AcceptWaveform(pcm)
    return json.loads(g.FinalResult())["text"], pcm


s = A.Arc(calendar=temp_store())
s.now = lambda: WED
s.model = model
s.run_async = lambda fn: fn()
s_said = lambda: ([t for _, k, t in s.events if k == "say"] or [""])[-1]
s_item = lambda title: next((i for i in s.calendar.data["items"] if i["title"].lower() == title), None)

text, pcm = speak("xyrus add buy eggs to my to do list"); s.handle(text, pcm)
check(f"spoken add to-do (grammar heard {text!r})", s_item("buy eggs") is not None, s_said())
text, pcm = speak("xyrus remind me to call the doctor tomorrow at five"); s.handle(text, pcm)
it = s_item("call the doctor")
check(f"spoken reminder (grammar heard {text!r})", it and it["date"] == "2026-09-17" and it["time"] == "17:00", (it, s_said()))
text, pcm = speak("xyrus what do i have this week"); s.handle(text, pcm)
check(f"spoken 'what do I have this week' (grammar heard {text!r})", s_said().startswith("This week you have"), s_said())
text, pcm = speak("xyrus what do i have in the next three days"); s.handle(text, pcm)
check(f"spoken 'next three days' (grammar heard {text!r})", s_said().startswith("In the next 3 days you have"), s_said())
text, pcm = speak("xyrus mark buy eggs as done"); s.handle(text, pcm)
check(f"spoken mark done (grammar heard {text!r})", (s_item("buy eggs") or {}).get("done"), s_said())
TMP.unlink(missing_ok=True)

print("CALENDAR TESTS", "OK" if not fails else f"FAILED ({fails})")
sys.exit(1 if fails else 0)
