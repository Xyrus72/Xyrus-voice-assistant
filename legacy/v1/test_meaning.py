"""Meaning layer: many ways of saying each command, the dangerous look-alikes, typed and spoken paths.
Never changes the real volume/brightness/power - all actions are stubbed."""
import json
import subprocess
import sys
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import config; config.load()
import arc as A

fails = 0


def check(name, cond, detail=""):
    global fails
    print(("  ok   " if cond else "  FAIL ") + name + (f"   [{detail}]" if detail and not cond else ""))
    fails += not cond


def expect(group, phrases, want_cmd, want_arg=None, arg_check=None):
    print(group)
    for p in phrases:
        cmd, arg = A.understand(p)
        ok = cmd == want_cmd and (want_arg is None or arg == want_arg) and (arg_check is None or arg_check(arg))
        check(f"{p!r:48} -> {cmd} {arg if arg is not None else ''}", ok, f"want {want_cmd} {want_arg or ''}")


down = lambda a: a is not None and a < 0
up = lambda a: a is not None and a > 0
expect("volume down", [
    "lower the volume", "can you lower the volume", "lower sound", "lower the sound", "decrease the sound",
    "reduce the volume please", "make it quieter", "turn it down", "turn the music down", "volume down",
    "could you turn the volume down", "it's too loud", "drop the volume", "less volume", "softer please",
    "can you decrease the volume for me", "the sound is too loud", "bring the volume down",
], "vol_step", arg_check=down)
expect("volume up", [
    "raise the volume", "increase the sound", "turn it up", "make it louder", "louder please",
    "i can't hear it", "boost the volume", "volume up", "turn the music up", "pump it up", "it's too quiet",
    "can you increase the volume", "more volume", "turn up the sound",
], "vol_step", arg_check=up)
print("step sizes")
for p, want in [("lower the volume a bit", -5), ("turn it down a little", -5), ("turn it up a lot", 25),
                ("lower the volume by twenty", -20), ("raise the sound slightly", 5), ("volume down", -10)]:
    check(f"{p!r:48} -> {A.understand(p)}", A.understand(p) == ("vol_step", want), want)
print("set / max / query")
for p, want in [("set the volume to thirty", ("vol_set", 30)), ("volume forty percent", ("vol_set", 40)),
                ("put the volume at fifty", ("vol_set", 50)), ("make the volume twenty five percent", ("vol_set", 25)),
                ("turn the volume up to eighty", ("vol_set", 80)), ("volume max", ("vol_set", 100)),
                ("full volume", ("vol_set", 100)), ("turn the volume all the way up", ("vol_set", 100)),
                ("volume a hundred percent", ("vol_set", 100)),
                ("what's the volume", ("vol_query", None)), ("how loud is it", ("vol_query", None)),
                ("tell me the volume level", ("vol_query", None))]:
    check(f"{p!r:48} -> {A.understand(p)}", A.understand(p) == want, want)
expect("mute", ["mute", "mute the sound", "turn off the sound", "sound off", "silence please", "no sound",
                "switch off the sound", "turn the sound off", "mute the volume"], "mute")
expect("unmute", ["un mute", "turn the sound back on", "sound on", "turn on the sound", "bring back the sound",
                  "switch on the sound"], "unmute")
expect("shutdown (asks to confirm first)", ["shut down", "shutdown the computer", "turn off the computer",
       "switch off the pc", "power off", "turn off", "power down the laptop", "please shut down"], "shutdown")
print("look-alikes that must NOT shut the PC down")
for p, want in [("turn off the sound", "mute"), ("turn off the screen", "screen_off"), ("turn off the lights", "cant"),
                ("shut down chrome", "cant"), ("turn off wifi", "cant"), ("switch off the display", "screen_off"),
                ("turn off the music", "play"), ("restart chrome", "cant"), ("lock the door", "cant"),
                ("turn [unk] off", None), ("turn on the lights", "cant")]:
    check(f"{p!r:48} -> {A.understand(p)[0]}", A.understand(p)[0] == want, want)
expect("screen off", ["turn off the screen", "screen off", "switch off the monitor", "turn the display off",
                      "put the screen to sleep", "monitor off"], "screen_off")
expect("wake", ["wake up", "turn on the screen", "wake the screen"], "wake")
expect("restart", ["restart", "reboot the computer", "restart my pc"], "restart")
expect("sleep", ["go to sleep", "sleep", "put the computer to sleep", "sleep mode"], "sleep")
expect("lock", ["lock", "lock the computer", "lock my pc", "lock the screen"], "lock")
expect("pause / resume", ["pause", "pause the music", "stop the music", "resume", "resume the video",
                          "keep playing"], "play")
expect("next track", ["next song", "skip this song", "skip", "next track", "play the next song"], "next")
expect("previous track", ["previous song", "go back to the last song", "previous track"], "prev")
expect("time", ["what time is it", "what's the time", "tell me the time", "do you know what time it is",
                "time please", "what is the time now"], "time")
expect("date", ["what's the date", "what day is it", "what's today's date", "tell me the date",
                "which day is it today", "what is today"], "date")
expect("screenshot", ["take a screenshot", "screenshot", "capture the screen", "take a picture of the screen",
                      "grab the screen", "screen shot please", "print screen"], "screenshot")
print("open apps")
for p, want in [("open chrome", "chrome"), ("can you open chrome", "chrome"), ("launch spotify", "spotify"),
                ("start notepad", "notepad"), ("open google chrome", "chrome"), ("fire up youtube", "youtube"),
                ("please open the calculator", "calculator"), ("run task manager", "task manager"),
                ("go to youtube", "youtube"), ("could you open the calculater for me", "calculator")]:
    check(f"{p!r:48} -> {A.understand(p)}", A.understand(p) == ("open", want), want)
check("'open my calendar' shows the Calendar tab", A.understand("open my calendar") == ("show_calendar", None))
print("brightness")
for p, want in [("increase the brightness", ("bright_step", 10)), ("make the screen brighter", ("bright_step", 10)),
                ("dim the screen", ("bright_step", -10)), ("lower the brightness a bit", ("bright_step", -5)),
                ("set brightness to seventy", ("bright_set", 70)), ("brightness max", ("bright_set", 100)),
                ("what's the brightness", ("bright_query", None)), ("turn up the brightness", ("bright_step", 10))]:
    check(f"{p!r:48} -> {A.understand(p)}", A.understand(p) == want, want)
expect("status", ["system status", "how is the battery", "check the cpu", "how much memory is used"], "status")
expect("joke", ["tell me a joke", "say something funny", "make me laugh"], "joke")
expect("help", ["help", "what can you do", "show me the commands"], "help")
expect("show desktop", ["show desktop", "minimize everything", "hide all windows"], "desktop")
expect("close window", ["close this window", "close it", "exit this app"], "close")
print("timers still go to the timer parser")
check("'set a timer for five minutes'", A.understand("set a timer for five minutes") == ("timer", 300))
check("'start a countdown for ten minutes'", A.understand("start a countdown for ten minutes") == ("timer", 600))

# ------------------------------------------------------------------ engine, typed ---
print("through the assistant (typed; actions stubbed)")
log = []
state = {"vol": 50, "muted": False}
act = A.actions
act.change_volume = lambda d: (log.append(("change", d)), state.__setitem__("vol", max(0, min(100, state["vol"] + d))))[1] or state["vol"]
act.set_volume = lambda p: (log.append(("set", p)), state.__setitem__("vol", p))[1] or state["vol"]
act.get_volume = lambda: state["vol"]
act.is_muted = lambda: state["muted"]
act.set_muted = lambda m: (log.append(("mute", m)), state.__setitem__("muted", m))
act.change_brightness = lambda d: (log.append(("bright", d)), 60)[1]
act.set_brightness = lambda p: (log.append(("bright_set", p)), p)[1]
act.screenshot = lambda: (log.append(("screenshot",)), Path("x.png"))[1]
act.open_target = lambda t: log.append(("open", t))
act.shutdown_pc = lambda: log.append(("SHUTDOWN",))
A._wait_for_speech = lambda timeout=8.0: None
a = A.Arc()
said = lambda: ([t for _, k, t in a.events if k == "say"] or [""])[-1]


def run(text):
    log.clear()
    a.handle(text)
    return list(log)


check("'can you lower the volume a bit' -> -5 and says the level",
      run("cyrus can you lower the volume a bit") == [("change", -5)] and said() == "Volume at 45 percent, sir.", (log, said()))
check("'decrease the sound' -> -10", run("cyrus decrease the sound") == [("change", -10)], log)
check("'set the volume to thirty percent'", run("cyrus set the volume to thirty percent") == [("set", 30)] and said() == "Volume at 30 percent, sir.", (log, said()))
check("'turn off the sound' mutes, no shutdown question",
      run("cyrus turn off the sound") == [("mute", True)] and a.pending_confirm is None, (log, a.pending_confirm))
check("'turn the sound back on' unmutes", run("cyrus turn the sound back on") == [("mute", False)] and said().startswith("Sound's back on"), (log, said()))
run("cyrus turn off the computer")
check("'turn off the computer' asks to confirm, doesn't shut down yet", a.pending_confirm == "shutdown" and not log, (a.pending_confirm, log))
a.handle("cancel")
check("  cancel", a.pending_confirm is None)
check("'turn off the lights' does nothing and says it can't",
      run("cyrus turn off the lights") == [] and a.pending_confirm is None and said() == "Sorry, sir, I can't do that one.", (log, said()))
check("half-heard 'shut down [unk]' never reaches shutdown", run("cyrus shut down [unk]") == [] and a.pending_confirm is None, a.pending_confirm)
check("'could you open google chrome for me'", run("cyrus could you open google chrome for me") == [("open", "chrome")], log)
check("'dim the screen'", run("cyrus dim the screen") == [("bright", -10)] and said() == "Brightness at 60 percent, sir.", (log, said()))
check("'what's the volume'", run("cyrus what's the volume") == [] and said().startswith("The volume is at"), said())

# ---------------------------------------------------------------- engine, spoken ---
print("spoken (synthesized speech -> command grammar -> meaning, full-vocabulary retry if needed)")
from vosk import KaldiRecognizer, Model, SetLogLevel
SetLogLevel(-1)
model = Model(str(A.MODEL_DIR))
GRAMMAR = json.dumps(A.build_grammar())
TMP = Path(__file__).parent / "_meaning_test.wav"
a.model = model


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


for phrase, want in [
    ("xyrus can you decrease the sound", ("change", -10)), ("xyrus make it quieter", ("change", -10)),
    ("xyrus lower the volume a bit", ("change", -5)), ("xyrus turn the music down", ("change", -10)),
    ("xyrus can you turn the volume up", ("change", 10)), ("xyrus set the volume to thirty percent", ("set", 30)),
    ("xyrus turn off the sound", ("mute", True)), ("xyrus could you take a screenshot", ("screenshot",)),
    ("xyrus open google chrome", ("open", "chrome")),
]:
    log.clear()
    text, pcm = speak(phrase)
    a.handle(text, pcm)
    check(f"{phrase!r:44} grammar={text!r:40} -> {log[:1]}", log[:1] == [want], want)
a.pending_confirm = None
text, pcm = speak("xyrus what time is it")
a.handle(text, pcm)
check(f"'xyrus what time is it' grammar={text!r}", said().startswith("It's "), said())
TMP.unlink(missing_ok=True)

print("MEANING TESTS", "OK" if not fails else f"FAILED ({fails})")
sys.exit(1 if fails else 0)
