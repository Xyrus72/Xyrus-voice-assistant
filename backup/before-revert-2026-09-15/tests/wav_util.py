"""Shared test helpers (T3, SPEC §7.1): synthesized speech, noise, silence - plus the small test doubles the
T3 suites share (stub config / stub registry built from the §3 phrase text, fake capture, gate speaker).

synth() renders 16 kHz mono PCM with System.Speech via PowerShell exactly like v1 test_arc.py and caches it
under tests/_cache/ by text hash; synth_many() renders every missing phrase in ONE PowerShell run."""
from __future__ import annotations

import hashlib
import os
import queue
import random
import re
import subprocess
import threading
import time
import wave
from array import array
from pathlib import Path

RATE = 16000
CHUNK_BYTES = 8000                       # 0.25 s int16 mono, like AudioCapture.BLOCK
CACHE_DIR = Path(__file__).resolve().parent / "_cache"
NO_WINDOW = 0x08000000


# ============================================================================ audio
def _cache_path(text: str) -> Path:
    return CACHE_DIR / (hashlib.sha1(text.encode("utf8")).hexdigest()[:16] + ".wav")


def read_wav(path: Path) -> bytes:
    with wave.open(str(path), "rb") as w:
        assert w.getframerate() == RATE and w.getnchannels() == 1 and w.getsampwidth() == 2, path
        return w.readframes(w.getnframes())


def _ps_quote(s: str) -> str:
    return str(s).replace("'", "''")


def synth_many(texts) -> dict[str, bytes]:
    texts = list(dict.fromkeys(texts))
    todo = [t for t in texts if not _cache_path(t).exists()]
    if todo:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        lines = ["Add-Type -AssemblyName System.Speech",
                 "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer",
                 "$f = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000,'Sixteen','Mono')"]
        tmps = {}
        for t in todo:
            tmp = _cache_path(t).with_suffix(".part")
            tmps[t] = tmp
            lines += [f"$s.SetOutputToWaveFile('{_ps_quote(tmp)}', $f)", f"$s.Speak('{_ps_quote(t)}')",
                      "$s.SetOutputToNull()"]
        lines.append("$s.Dispose()")
        script = CACHE_DIR / f"_synth_{os.getpid()}_{threading.get_ident()}.ps1"
        script.write_text("\r\n".join(lines), encoding="utf-8-sig")
        try:
            subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
                           check=True, capture_output=True, timeout=600, creationflags=NO_WINDOW)
        finally:
            script.unlink(missing_ok=True)
        for t, tmp in tmps.items():
            os.replace(tmp, _cache_path(t))
    return {t: read_wav(_cache_path(t)) for t in texts}


def synth(text: str) -> bytes:
    return synth_many([text])[text]


def noise(seconds: float, amp: int, seed: int = 0) -> bytes:
    """Uniform white noise in [-amp, amp]."""
    rnd = random.Random(seed)
    return array("h", (rnd.randint(-amp, amp) for _ in range(int(seconds * RATE)))).tobytes()


def room_noise(seconds: float, amp: int, seed: int = 0) -> bytes:
    """Low-frequency 'room' rumble: leaky-integrated white noise scaled to peak ~amp, plus faint hiss."""
    rnd = random.Random(seed)
    n = int(seconds * RATE)
    y, raw = 0.0, []
    for _ in range(n):
        y = 0.995 * y + rnd.uniform(-1, 1)
        raw.append(y)
    peak = max(abs(v) for v in raw) or 1.0
    return array("h", (max(-32768, min(32767, int(v / peak * amp + rnd.uniform(-amp, amp) * 0.1)))
                       for v in raw)).tobytes()


def silence(seconds: float) -> bytes:
    return bytes(int(seconds * RATE) * 2)


def faint(pcm: bytes, peak: float) -> bytes:
    """The same speech scaled so its loudest sample is `peak` (0..1) - the user's seat gives 0.03-0.05."""
    a = array("h")
    a.frombytes(pcm[: len(pcm) & ~1])
    if not a:
        return b""
    top = max(max(a), -min(a)) or 1
    k = peak * 32767 / top
    return array("h", (max(-32768, min(32767, int(v * k))) for v in a)).tobytes()


def hiss(seconds: float, rms: float = 0.0033, seed: int = 0) -> bytes:
    """Stationary room hiss with the given rms (0..1); the user's room floor measured 0.0026-0.0033."""
    rnd = random.Random(seed)
    amp = rms * 32768 * (3 ** 0.5)          # uniform noise: rms = amp / sqrt(3)
    return array("h", (max(-32768, min(32767, int(rnd.uniform(-amp, amp)))) for _ in range(int(seconds * RATE)))).tobytes()


def chunks(pcm: bytes, size: int = CHUNK_BYTES) -> list[bytes]:
    return [pcm[i:i + size] for i in range(0, len(pcm), size)]


# ============================================================================ test doubles
DEFAULT_APPS = {
    "chrome": "chrome", "browser": "chrome", "edge": "msedge", "youtube": "https://www.youtube.com",
    "spotify": "spotify:", "notepad": "notepad", "calculator": "calc", "explorer": "explorer", "files": "explorer",
    "code": "code", "settings": "ms-settings:", "task manager": "taskmgr", "terminal": "wt", "discord": "discord:",
    "bluetooth settings": "ms-settings:bluetooth", "sound settings": "ms-settings:sound",
    "display settings": "ms-settings:display", "network settings": "ms-settings:network",
    "windows update": "ms-settings:windowsupdate", "downloads": "shell:Downloads", "documents": "shell:Personal",
    "pictures": "shell:My Pictures", "recycle bin": "shell:RecycleBinFolder", "device manager": "devmgmt.msc",
    "control panel": "control"}


class StubConfig:
    """Dotted get/set over the §6.1 keys T3 reads (stands in for xyrus.config.Config)."""

    def __init__(self, **overrides):
        self.data = {"version": 2, "voice_replies": True, "wake_words": ["cyrus", "zeros", "virus", "cirrus"],
                     "chime_on_wake": True, "min_speech_peak": 0.02, "mic": {"name": None, "hostapi": "MME"},
                     "tts": {"voice": None, "rate": 0, "volume": 100}, "apps": dict(DEFAULT_APPS),
                     "custom_commands": []}
        for k, v in overrides.items():
            self.set(k.replace("__", "."), v)

    def get(self, key, default=None):
        node = self.data
        for part in key.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, key, value):
        parts = key.split(".")
        node = self.data
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value


# Stub command table: phrases written exactly as §3 registers them (pattern syntax of §4.5).
STUB_COMMANDS = [
    ("pause_listening", "stop listening / pause listening"),
    ("stop_talking", "stop / quiet / be quiet / stop talking"),
    ("repeat", "repeat / repeat that / say that again"),
    ("what_heard", "what did you hear"),
    ("help", "what can you do / help / what are your commands"),
    ("shutdown", "shut down / shutdown / power off / turn off the pc / turn off the computer"),
    ("restart", "restart / reboot / restart the pc / restart the computer"),
    ("cancel_shutdown", "cancel / abort / cancel shutdown / cancel the shutdown / stop the shutdown"),
    ("shutdown_in", "shut down in <duration> / restart in <duration>"),
    ("sleep", "go to sleep / sleep / sleep mode"),
    ("hibernate", "hibernate"),
    ("lock", "lock / lock screen / lock the pc / lock the computer"),
    ("sign_out", "sign out / log out / log off"),
    ("screen_off", "screen off / display off / monitor off / turn off the screen"),
    ("screen_on", "wake up / screen on / display on / turn on the screen"),
    ("volume_up", "volume up / louder / turn it up"),
    ("volume_down", "volume down / quieter / turn it down"),
    ("volume_by", "volume up by <number> / volume down by <number>"),
    ("set_volume", "set [the] volume to <percent> / volume <percent> / set volume <percent>"),
    ("max_volume", "volume max / full volume / max volume / maximum volume"),
    ("query_volume", "what's the volume / what is the volume / volume level"),
    ("mute", "mute / mute the sound"),
    ("unmute", "un mute / sound on / turn the sound on"),
    ("media_play", "play / pause / resume / play pause / pause the music / resume the music / pause it"),
    ("next_track", "next / next track / next song / skip"),
    ("prev_track", "previous / previous track / previous song / go back"),
    ("stop_media", "stop the music / stop playback"),
    ("set_brightness", "set [the] brightness to <percent> / brightness <percent>"),
    ("brighter", "brightness up / brighter"),
    ("dimmer", "brightness down / dimmer / dim the screen"),
    ("query_brightness", "what's the brightness / brightness level"),
    ("show_desktop", "show desktop / minimize everything"),
    ("close_window", "close window / close this / close this window"),
    ("minimize", "minimize / minimise / minimize this"),
    ("maximize", "maximize / maximise / maximize this"),
    ("restore", "restore this / normal size"),
    ("snap_left", "snap left / snap this left"),
    ("snap_right", "snap right / snap this right"),
    ("switch_to", "switch to <app> / go to <app> / bring up <app>"),
    ("close_app", "close <app> / quit <app> / exit <app>"),
    ("force_close", "force close <app> / kill <app>"),
    ("whats_open", "what's open / what windows are open"),
    ("open_app", "open <app> / launch <app> / start <app>"),
    ("web_search", "search for <query> / google <query> / search the web for <query>"),
    ("youtube_search", "search youtube for <query> / youtube <query>"),
    ("type_text", "type <text>"),
    ("keys", "press enter / press escape / press tab / press space / select all / copy / copy that / cut / paste / "
             "paste it / undo / redo / save / save this / new tab / close tab / reopen tab / refresh / full screen"),
    ("read_clipboard", "read the clipboard / read my clipboard / what's on the clipboard"),
    ("play_song", "play <song> / play <song> on youtube"),
    ("play_ask", "play some music / play music / play a song / play something / play anything / play a track"),
    ("time", "what time is it / what's the time / what is the time / the time / time"),
    ("date", "what day is it / what's the date / what is the date / the date / what's today"),
    ("status", "system status / status / system report"),
    ("battery", "battery / battery level / how much battery"),
    ("disk", "disk space / how much space is left"),
    ("screenshot", "take a screenshot / screenshot / take a picture of the screen"),
    ("calculate", "what is <expr> / what's <expr> / calculate <expr>"),
    ("joke", "tell me a joke / joke / tell me another"),
    ("coin", "flip a coin / heads or tails"),
    ("dice", "roll a die / roll a dice / roll two dice"),
    ("set_timer", "set a timer for <duration> / set timer for <duration> / set timer <duration> / "
                  "timer for <duration> / timer <duration> / start a timer for <duration> / <duration> timer"),
    ("timer_missing", "set a timer / timer / start a timer"),
    ("named_timer", "<name> timer <duration> / set a <name> timer for <duration>"),
    ("time_left", "how long is left / how much time is left / time left / how long on the timer"),
    ("cancel_timer", "cancel timer / stop timer / cancel the timer / stop the timer / cancel all timers / "
                     "cancel the <name> timer"),
    ("add_event", "add an event / add an event <when> / new event / create an event / schedule <text> / "
                  "add a meeting <when> / add an appointment <when>"),
    ("remind", "remind me <when> to <text> / remind me to <text> / remind me / set a reminder / "
               "set a reminder for <when>"),
    ("add_note", "add a note / add a note for <day> / take a note / make a note / note for <day> / note that <text>"),
    ("read_notes", "read my notes / read my notes for <day> / what are my notes / notes for <day>"),
    ("agenda", "what's on <day> / what's on today / what do i have <day> / what do i have today / my schedule / "
               "my schedule for <day> / what's coming up / agenda"),
    ("whats_next", "what's next / next event / what's my next event / when is my next event / anything coming up"),
    ("show_calendar", "show my calendar / open my calendar / show the calendar"),
    ("add_todo", "add a to do <text> / add a task <text> / new to do <text> / add a to do"),
    ("read_todos", "what's on my to do list / what are my to dos / read my to do list / my to do list / "
                   "what do i need to do"),
    ("tick_off", "mark <text> as done / tick off <text> / cross off <text> / i finished <text>"),
    ("undo_add", "undo that / scratch that / delete that / remove that"),
    ("briefing", "good morning / good afternoon / good evening / brief me / briefing / daily briefing / "
                 "what's my day like"),
    ("good_night", "good night / goodnight / going to bed"),
    ("remember", "remember that / remember this / remember / keep in mind"),
    ("recall", "what did i ask you to remember / what do you remember / read my memories / what did i tell you"),
    ("thanks", "thank you / thanks / thanks a lot"),
    ("who_are_you", "who are you / what's your name / what are you"),
    ("how_are_you", "how are you / how are you doing"),
    ("hello", "hello / hi"),
    ("are_you_there", "are you there / you there"),
]
STUB_FREE_TRIGGER_WORDS = ("add", "schedule", "put", "new", "remind", "reminder", "note", "task", "remember",
                           "mark", "tick", "cross", "finished")

_NUM = {w: i for i, w in enumerate(("zero one two three four five six seven eight nine ten eleven twelve thirteen "
                                    "fourteen fifteen sixteen seventeen eighteen nineteen").split())}
_NUM.update({"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80,
             "ninety": 90, "hundred": 100})
_UNITS = {"second", "seconds", "minute", "minutes", "hour", "hours"}
_TIMER_NAMES = {"tea", "pasta", "coffee", "pizza", "egg", "laundry", "oven", "break", "work"}
_WHEN = ({"today", "tonight", "tomorrow", "next", "am", "pm", "noon", "midnight", "morning", "evening",
          "afternoon", "in", "at", "minutes", "hours", "week", "monday", "tuesday", "wednesday", "thursday",
          "friday", "saturday", "sunday"} | set(_NUM))
_DAY = _WHEN | {"this", "these", "rest", "days", "weeks", "month", "weekend", "on"}
FREE_SLOTS = {"song", "query", "text", "app"}


def _slot_ok(name: str, value: str) -> bool:
    toks = [t for t in value.split() if t != "[unk]"]
    if not toks:
        return False
    if name == "duration":
        return any(t in _UNITS for t in toks) and any(t in _NUM or t in ("a", "an", "half") for t in toks)
    if name in ("percent", "number", "expr"):
        return any(t in _NUM for t in toks)
    if name == "name":
        return len(toks) == 1 and toks[0] in _TIMER_NAMES
    if name == "when":
        return any(t in _WHEN for t in toks)
    if name == "day":
        return any(t in _DAY for t in toks)
    return True


def _literal_prefixes(text: str) -> set[tuple[str, ...]]:
    prefixes = [()]
    for tok in text.split():
        if tok.startswith("<"):
            break
        opt = tok.startswith("[")
        alts = tok.strip("[]").split("|")
        prefixes = [p for p in prefixes if opt] + [p + (a,) for p in prefixes for a in alts]
    return {p for p in prefixes if p}


class StubPattern:
    def __init__(self, text: str):
        self.text = text
        toks = text.split()
        parts, slots, lit = [], [], 0
        for i, tok in enumerate(toks):
            last = i == len(toks) - 1
            if tok.startswith("<"):
                slots.append(tok[1:-1])
                parts.append((f"(?P<s{len(slots) - 1}>.+)" if last else f"(?P<s{len(slots) - 1}>.+?)", False))
            elif tok.startswith("["):
                parts.append(("(?:(?:%s) )?" % "|".join(map(re.escape, tok[1:-1].split("|"))), True))
            else:
                parts.append(("(?:%s)" % "|".join(map(re.escape, tok.split("|"))), False))
                lit += 1
        body = ""
        for i, (rx, optional) in enumerate(parts):
            body += rx + ("" if optional or i == len(parts) - 1 else " ")
        self.regex = re.compile(r"^(?P<pre>(?:\S+ )*?)" + body + r"(?P<post>(?: \S+)*)$")
        self.slots = tuple(slots)
        self.literal_count = lit
        self.free_slot = slots[-1] if toks[-1].startswith("<") and slots[-1] in FREE_SLOTS else None
        self.literals = {w for tok in toks if not tok.startswith("<") for w in tok.strip("[]").split("|")}


class StubCommand:
    def __init__(self, name: str, phrases: str):
        self.name = name
        self.patterns = tuple(StubPattern(p.strip()) for p in phrases.split(" / "))


class StubMatch:
    def __init__(self, command, pattern, slots, text, extra_words):
        self.command, self.pattern, self.slots, self.text = command, pattern, slots, text
        self.has_unk = "[unk]" in text.split()
        self.extra_words = extra_words


class StubRegistry:
    """The §4.5 surface T3 consumes (literal_words, slot_types_used, free_triggers, commands, version) plus a
    simplified match() (§4.5 selection order, slot presence checks only)."""

    def __init__(self, table=STUB_COMMANDS):
        self._commands = [StubCommand(n, p) for n, p in table]
        self._version = 1

    def commands(self):
        return list(self._commands)

    def literal_words(self) -> set[str]:
        return {w for c in self._commands for p in c.patterns for w in p.literals}

    def slot_types_used(self) -> set[str]:
        return {s for c in self._commands for p in c.patterns for s in p.slots}

    def free_triggers(self) -> list[tuple[str, ...]]:
        out = set()
        for c in self._commands:
            for p in c.patterns:
                if p.free_slot:
                    out |= _literal_prefixes(p.text)
        out |= {(w,) for w in STUB_FREE_TRIGGER_WORDS}
        return sorted(out)

    def version(self) -> int:
        return self._version

    def bump(self) -> None:
        self._version += 1

    def match(self, text, *, free_text=None, scope="full", env=None):
        text = " ".join(text.split())
        best = None
        for order, cmd in enumerate(self._commands):
            for p in cmd.patterns:
                m = p.regex.match(text)
                if not m:
                    continue
                vals = [m.group(f"s{i}") for i in range(len(p.slots))]
                # a free-capable last slot takes its value from free_text when there is one (§4.5 Free slots)
                if not all(_slot_ok(n, v) or (free_text and n == p.free_slot and i == len(vals) - 1)
                           for i, (n, v) in enumerate(zip(p.slots, vals))):
                    continue
                extra = len(m.group("pre").split()) + len(m.group("post").split())
                key = (p.literal_count, -extra, -order)
                if best is None or key > best[0]:
                    best = (key, StubMatch(cmd, p, dict(zip(p.slots, vals)), text, extra))
        return best[1] if best else None


class FakeCapture:
    """Stands in for AudioCapture on the recognizer side: just the chunks queue."""

    def __init__(self):
        self.chunks: "queue.Queue[tuple[bytes, float]]" = queue.Queue()

    def feed(self, pcm: bytes, *, start: float | None = None, realtime: float = 0.0) -> float:
        """Queue pcm as 0.25 s chunks stamped like the PortAudio callback (end-of-block times).
        Returns the timestamp of the last chunk."""
        t = time.monotonic() if start is None else start
        for c in chunks(pcm):
            t += len(c) / (RATE * 2)
            self.chunks.put((c, t))
            if realtime:
                time.sleep(realtime)
        return t


class GateSpeaker:
    """Speaker stand-in for the recognizer: is_quiet_at is False inside any interval in `loud`."""

    def __init__(self):
        self.loud: list[tuple[float, float]] = []

    def is_quiet_at(self, t: float) -> bool:
        return not any(s <= t <= e for s, e in self.loud)


class RecordingChime:
    def __init__(self):
        self.played: list[str] = []

    def play(self, kind):
        self.played.append(kind)

    def stop(self):
        pass


WAKE_LIKE = {"cyrus", "zeros", "virus", "cirrus", "sirius", "serious", "zero", "zira", "xyrus"}
PLAY_LIKE = {"play", "plays", "played", "playing", "lay", "clay", "pray"}


def song_title(free_text: str) -> str:
    """v1 words_after_play + clean_song_title (§3.6 step 2), for asserting on Transcript.free_text."""
    words = free_text.lower().split()
    for i, w in enumerate(words[:3]):
        if w in PLAY_LIKE:
            words = words[i + 1:]
            break
    else:
        while words and words[0] in WAKE_LIKE:
            words = words[1:]
    t = " ".join(w for w in words if w != "[unk]")
    changed = True
    while changed:
        changed = False
        for pre in ("the song ", "the track "):
            if t.startswith(pre):
                t, changed = t[len(pre):], True
        for suf in (" on youtube", " on you tube", " please", " for me", " now", " sir"):
            if t.endswith(suf):
                t, changed = t[: -len(suf)], True
    return t.strip()
