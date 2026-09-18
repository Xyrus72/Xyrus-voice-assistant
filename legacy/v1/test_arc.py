"""Self-test: parser, offline recognition on synthesized speech, optional screen toggle."""
import json, sys, time, wave, subprocess
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import config; config.load()
import arc as A

# 1. parser
cases = {
    "cyrus shut down": ("shutdown", None), "hey cyrus sleep": ("sleep", None),
    "cyrus screen off": ("screen_off", None), "cyrus wake up": ("wake", None),
    "cyrus lock": ("lock", None), "cyrus cancel": ("cancel", None), "cyrus restart": ("restart", None),
    "cyrus volume up": ("vol_up", None), "cyrus mute": ("mute", None), "cyrus next": ("next", None),
    "cyrus open chrome": ("open", "chrome"), "cyrus open task manager": ("open", "task manager"),
    "cyrus open notepad": ("open", "notepad"),
    "cyrus set a timer for five minutes": ("timer", 300), "cyrus timer twenty five seconds": ("timer", 25),
    "cyrus timer four seven minutes": ("timer", 420),      # "for" heard as "four"
    "cyrus set timer four ten minutes": ("timer", 600), "cyrus timer seven minutes": ("timer", 420),
    "cyrus timer forty five seconds": ("timer", 45), "cyrus timer half an hour": ("timer", 1800),
    "cyrus timer one and a half hours": ("timer", 5400), "cyrus timer a minute": ("timer", 60),
    "cyrus timer two hours": ("timer", 7200), "cyrus timer ninety seconds": ("timer", 90),
    "cyrus cancel timer": ("cancel_timer", None), "cyrus what time is it": ("time", None),
    "cyrus take a screenshot": ("screenshot", None), "cyrus tell me a joke": ("joke", None),
    "cyrus show desktop": ("desktop", None),
}
for text, (want_cmd, want_arg) in cases.items():
    had, rest = A.strip_wake_word(text)
    cmd, arg = A.find_command(rest)
    ok = had and cmd == want_cmd and (want_arg is None or arg == want_arg)
    assert ok, (text, had, cmd, arg)
assert A.strip_wake_word("shut down")[0] is False
assert A.find_command("open notepad")[0] == "open"      # "no" inside "notepad" must not mean cancel
print("parser ok")

# 2. recognition on synthesized speech
from vosk import KaldiRecognizer, Model, SetLogLevel
SetLogLevel(-1)
model = Model(str(A.MODEL_DIR))
grammar = json.dumps(A.build_grammar())
tmp = Path(__file__).parent / "_tts.wav"
phrases = ["xyrus shut down", "xyrus sleep", "xyrus screen off", "xyrus wake up", "yes", "xyrus cancel",
           "xyrus volume up", "xyrus open chrome", "xyrus open youtube", "xyrus set a timer for five minutes",
           "xyrus what time is it", "xyrus take a screenshot", "xyrus tell me a joke", "xyrus play",
           "xyrus set a timer for seven minutes", "xyrus timer twelve minutes", "xyrus timer thirty seconds"]
bad = 0
for phrase in phrases:
    ps = ("Add-Type -AssemblyName System.Speech;"
          "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
          f"$s.SetOutputToWaveFile('{tmp}', (New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000,'Sixteen','Mono')));"
          f"$s.Speak('{phrase}');$s.Dispose()")
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True, capture_output=True)
    with wave.open(str(tmp), "rb") as w:
        pcm = w.readframes(w.getnframes())
    rec = KaldiRecognizer(model, 16000, grammar)
    rec.AcceptWaveform(pcm)
    heard = json.loads(rec.FinalResult())["text"]
    had, rest = A.strip_wake_word(heard)
    cmd, arg = A.find_command(rest or heard)
    flag = "" if cmd else "   <-- NOT UNDERSTOOD"
    bad += not cmd
    print(f"  said {phrase!r:36} heard {heard!r:36} -> {cmd} {arg or ''}{flag}")
tmp.unlink(missing_ok=True)
print("recognition ok" if not bad else f"{bad} phrase(s) not understood")

# 3. screen off / on (real)
if "--screen" in sys.argv:
    import actions
    print("screen off in 1s..."); time.sleep(1)
    actions.screen_off(); time.sleep(4)
    actions.screen_on(); print("screen on called")
