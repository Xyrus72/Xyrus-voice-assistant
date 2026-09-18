"""Song requests: typed path, real (synthesized) speech through the recognizer + full-vocabulary
re-decode, ask-then-answer, noise and cancel while waiting, and bare 'play' staying play/pause.
Uses the real YouTube lookup (needs internet) but never opens a browser."""
import json, subprocess, sys, time, wave
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import config; config.load()
import arc as A
from vosk import KaldiRecognizer, Model, SetLogLevel

SetLogLevel(-1)
model = Model(str(A.MODEL_DIR))
GRAMMAR = json.dumps(A.build_grammar())
TMP = Path(__file__).parent / "_song_test.wav"

opened, media = [], []
A.actions.open_target = opened.append              # never actually open a browser in tests
A.actions.media_play = lambda: media.append("play")
REAL_ENSURE = A.actions.ensure_playing
A.actions.browser_playing = lambda: False                 # no real audio probing / key presses in tests
A.actions.ensure_playing = lambda video, **kw: "autoplay"


def fresh():
    a = A.Arc()
    a.model = model
    a.run_async = lambda fn: fn()
    opened.clear(); media.clear()
    return a


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


def said(a):
    s = [t for _, k, t in a.events if k == "say"]
    return s[-1] if s else ""


def query(a):
    q = [t for _, k, t in a.events if k == "cmd" and t.startswith("play song: ")]
    return q[-1][len("play song: "):] if q else ""


fails = 0


def check(name, cond, detail=""):
    global fails
    print(("  ok   " if cond else "  FAIL ") + name + (f"   [{detail}]" if detail else ""))
    fails += not cond


print("helpers")
check("'play alone' is a song request", A.is_song_request("play alone"))
check("bare 'play' is not", not A.is_song_request("play"))
check("'play [unk]' is", A.is_song_request("play [unk]"))
check("title cleanup", A.clean_song_title("the song shape of you on youtube please") == "shape of you")
check("misheard wake + 'lay'", A.words_after_play("sirius lay alone".split()) == ["alone"])
check("spoken_title", A.actions.spoken_title("Alan Walker - Alone (Official Music Video)") == "Alone by Alan Walker")

real_search = A.actions.youtube_search
A.actions.youtube_search = lambda q, timeout=6: [("1-xGerv5FOk", "", "", None)]   # page layout not recognised
check("unrecognised page -> title fetched by video id", A.actions.find_song("alone")[0] == "Alone by Alan Walker",
      A.actions.find_song("alone"))
A.actions.youtube_search = real_search
import re as _re
_page = 'x<script>window["ytInitialData"] = {"a": 1};</script>y'
check("both ytInitialData page variants parse",
      bool(_re.search(r'(?:var ytInitialData|window\["ytInitialData"\])\s*=\s*(\{.*?\});\s*</script>', _page, _re.S)))

print("typed (text box path)")
a = fresh(); a.handle("cyrus play alone")
check("'play alone' opens a YouTube video", bool(opened) and "watch?v=" in opened[0], opened)
check("  announces the song", said(a).startswith("Playing ") and "lone" in said(a), said(a))
a = fresh(); a.handle("cyrus play")
check("bare 'play' = media play/pause, no browser", media == ["play"] and not opened, (media, opened))
a = fresh(); a.handle("cyrus pause")
check("'pause' = media play/pause", media == ["play"] and not opened, (media, opened))

print("spoken (grammar -> full-vocabulary re-decode)")
for phrase, expect in [("xyrus play alone", "alone"), ("xyrus play shape of you", "shape of you"),
                       ("xyrus play believer by imagine dragons", "believer by imagine dragons"),
                       ("xyrus play faded by alan walker", "faded by alan walker")]:
    a = fresh(); text, pcm = speak(phrase); a.handle(text, pcm)
    check(f"{phrase!r:42} grammar={text!r:24} query={query(a)!r}",
          query(a) == expect and bool(opened) and "watch?v=" in opened[0], said(a))

print("ask, then answer")
a = fresh(); text, pcm = speak("xyrus play some music"); a.handle(text, pcm)
check("'play some music' asks what to play",
      said(a) == "What should I play, sir?" and a.awaiting_song_until > time.time(), said(a))
a.handle("how")
check("  room noise while waiting is ignored", not opened and a.awaiting_song_until > time.time())
text, pcm = speak("blinding lights"); a.handle(text, pcm)
check(f"  answer 'blinding lights' plays it (grammar={text!r})", query(a) == "blinding lights" and bool(opened), said(a))
check("  and stops waiting", a.awaiting_song_until == 0)
a = fresh(); a.handle("cyrus play music"); a.handle("cancel")
check("'cancel' while waiting", a.awaiting_song_until == 0 and said(a) == "Okay, sir." and not opened, said(a))

print("autoplay (browsers block it for pages another program opens)")
_act = A.actions
_calls = []


def _run_case(audio_seq, hwnd=1, fg=True):
    seq = list(audio_seq)
    _calls.clear()
    names = ("_wait_youtube_window", "_audio_within", "_focus", "_press_play_key", "_click_player",
             "_window_exe", "_is_foreground")
    saved = {n: getattr(_act, n) for n in names}
    _act._wait_youtube_window = lambda title, timeout: hwnd
    _act._audio_within = lambda secs, trust_state=True, exe=None: seq.pop(0) if seq else False
    _act._window_exe = lambda h: "msedge.exe"
    _act._is_foreground = lambda h: fg
    _act._focus = lambda h: None
    _act._press_play_key = lambda h: _calls.append("k")
    _act._click_player = lambda h: _calls.append("click")
    try:
        return REAL_ENSURE("Alan Walker - Alone")
    finally:
        for n, fn in saved.items():
            setattr(_act, n, fn)


check("already playing -> presses nothing", _run_case([True]) == "autoplay" and _calls == [], _calls)
check("silent -> presses k -> plays", _run_case([False, True]) == "started" and _calls == ["k"], _calls)
check("k didn't work -> clicks the player", _run_case([False, False, True]) == "started" and _calls == ["k", "click"], _calls)
check("nothing works -> 'blocked'", _run_case([False, False, False]) == "blocked" and _calls == ["k", "click"], _calls)
check("no window and silent -> no key presses", _run_case([False], hwnd=None) == "no-window" and _calls == [], _calls)
check("song's window not found -> touches nothing, even with sound around", _run_case([True], hwnd=None) == "no-window" and _calls == [], _calls)
check("focus lost to another window -> presses nothing, says 'blocked'", _run_case([False], fg=False) == "blocked" and _calls == [], _calls)


class _FakeProc:
    def __init__(self, n): self._n = n
    def name(self): return self._n


class _FakeCtl:
    def __init__(self, p): self.p = p
    def QueryInterface(self, iface): return self
    def GetPeakValue(self): return self.p


class _FakeSession:
    def __init__(self, n, state, p): self.Process, self.State, self._ctl = _FakeProc(n), state, _FakeCtl(p)


import pycaw.pycaw as _pc
_real_sessions = _pc.AudioUtilities.GetAllSessions
_pc.AudioUtilities.GetAllSessions = staticmethod(lambda: [_FakeSession("chrome.exe", 1, 0.85), _FakeSession("msedge.exe", 0, 0.0)])
check("a Chrome lecture playing doesn't count as the Edge song playing", _act._browser_audio(True, "msedge.exe") is False)
check("  but does count as 'a browser is playing' (so it gets paused first)", _act._browser_audio(True, None) is True)
_pc.AudioUtilities.GetAllSessions = _real_sessions
_wins = [(11, "Lecture 10 - YouTube - Google Chrome")]
_real_top = _act._top_windows
_act._top_windows = lambda: _wins
check("never picks some other YouTube window", _act._wait_youtube_window("Alan Walker - Alone", 0.4) is None)
_wins.append((22, "Alan Walker - Alone - YouTube - Personal - Microsoft\u200b Edge"))
check("finds the song's own window (punctuation-insensitive)", _act._wait_youtube_window("Alan Walker \u2013 Alone", 0.4) == 22)
_act._top_windows = _real_top
_act.ensure_playing = lambda video, **kw: "blocked"
a = fresh(); a.handle("cyrus play alone")
check("blocked -> tells you to press play", said(a) == "The browser blocked autoplay, sir. Press play on the video.", said(a))
_act.ensure_playing = lambda video, **kw: "autoplay"
_act.browser_playing = lambda: True
a = fresh(); a.handle("cyrus play alone")
check("something already playing -> paused first, then the new song opens", media == ["play"] and bool(opened), (media, opened))
_act.browser_playing = lambda: False

print("offline fallback")
real = A.actions.youtube_search
def _offline(q, timeout=6):
    raise OSError("no network")
A.actions.youtube_search = _offline
a = fresh(); a.handle("cyrus play alone")
check("no internet -> opens the search page and says so",
      bool(opened) and "results?search_query=alone" in opened[0] and "couldn't reach" in said(a), (opened, said(a)))
A.actions.youtube_search = real

print("listener end-to-end (fake microphone streaming real audio)")
import logging, threading
_text, _pcm = speak("xyrus play alone")
_silence = bytes(16000 * 2)   # 1 s


class FakeMic:
    """Stands in for sounddevice.RawInputStream: streams silence + the utterance + silence to the callback."""
    def __init__(self, samplerate, blocksize, dtype, channels, callback, **kw):
        self.cb = callback

    def __enter__(self):
        data = _silence + _pcm + _silence * 2
        def feed():
            for i in range(0, len(data), 8000):
                self.cb(data[i:i + 8000], 4000, None, None)
                time.sleep(0.005)
        threading.Thread(target=feed, daemon=True).start()
        return self

    def __exit__(self, *exc):
        return False


_errors = []
class _Grab(logging.Handler):
    def emit(self, r):
        if "audio stream error" in r.getMessage():
            _errors.append(r.getMessage())
logging.getLogger("xyrus").addHandler(_Grab())
_real_stream = A.sd.RawInputStream
A.sd.RawInputStream = FakeMic
a = fresh()
a.model = None                      # the listener must hand the engine its model itself
threading.Thread(target=A.listen_forever, args=(a,), daemon=True).start()
_t0 = time.time()
while not opened and not _errors and time.time() - _t0 < 30:
    time.sleep(0.1)
check("mic -> recognizer -> handle(text, pcm) -> song plays",
      query(a) == "alone" and bool(opened) and "watch?v=" in opened[0] and not _errors, (query(a), opened, _errors))
check("  listener gave the engine its model", a.model is not None)
A.sd.RawInputStream = _real_stream

TMP.unlink(missing_ok=True)
print("SONG TESTS", "OK" if not fails else f"FAILED ({fails})")
sys.exit(1 if fails else 0)
