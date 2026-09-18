"""
Xyrus - an always-on, offline voice assistant for Windows.

Say "Xyrus" then a command. Run `python arc.py` to open the app window,
or `python arc.py --tray` to start hidden in the tray (what the Startup
shortcut does). Speech recognition is fully offline (Vosk).
"""

import ctypes
import datetime as dt
import difflib
import json
import logging
import os
import queue
import random
import subprocess
import sys
import threading
import time
from array import array
from collections import deque
from pathlib import Path

import sounddevice as sd
from vosk import KaldiRecognizer, Model, SetLogLevel

import actions
import calendar_store as CS
import config
import when as W
try:
    import hearing            # Whisper for song names / titles; optional - falls back to Vosk
except Exception:             # missing or broken: never stop Xyrus from starting
    hearing = None

BASE = Path(__file__).resolve().parent
MODEL_DIR = BASE / "model"
LOG_FILE = BASE / "arc.log"
ICON_FILE = BASE / "xyrus.ico"

NAME = "Xyrus"
# "Xyrus" isn't an English word, so the offline model hears it as one of these
# sound-alikes depending on pronunciation (all must be words the model knows).
WAKE_WORDS = ("cyrus", "zeros", "virus")
SAMPLE_RATE = 16000
MAX_UTTERANCE_BYTES = SAMPLE_RATE * 2 * 20   # keep at most 20 s of raw audio per utterance
COMMAND_WINDOW = 15.0    # seconds (after the reply finishes) to say a command
CONFIRM_WINDOW = 12.0    # seconds to answer "are you sure?"

logging.basicConfig(filename=LOG_FILE, level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("xyrus")
logging.getLogger("comtypes").setLevel(logging.WARNING)

kernel32 = ctypes.windll.kernel32

# --------------------------------------------------------------------------- #
# Vocabulary
# --------------------------------------------------------------------------- #
# command key -> spoken phrases.  Longest phrases are matched first.
COMMANDS = {
    "shutdown":   ("shut down", "shutdown", "power off", "turn off"),
    "restart":    ("restart", "reboot"),
    "sleep":      ("go to sleep", "sleep"),
    "screen_off": ("screen off", "display off", "monitor off"),
    "wake":       ("wake up", "screen on", "display on"),
    "lock":       ("lock screen", "lock"),
    "cancel":     ("cancel", "stop", "no"),
    "yes":        ("yes", "confirm"),
    "vol_up":     ("volume up", "louder", "turn it up", "increase the volume", "raise the sound"),
    "vol_down":   ("volume down", "quieter", "turn it down", "lower the volume", "decrease the sound", "reduce the volume"),
    "vol_max":    ("volume max", "full volume", "maximum volume"),
    "mute":       ("mute", "sound off"),
    "unmute":     ("un mute", "sound on", "turn the sound back on"),
    "vol_query":  ("what is the volume", "how loud is it"),
    "bright_up":  ("brightness up", "brighter", "increase the brightness"),
    "bright_down": ("brightness down", "dimmer", "dim the screen", "lower the brightness"),
    "play":       ("play", "pause", "resume"),
    "next":       ("next track", "next song", "next", "skip"),
    "prev":       ("previous track", "previous song", "previous", "go back"),
    "time":       ("what time is it", "what is the time", "the time"),
    "date":       ("what day is it", "what is the date", "the date"),
    "screenshot": ("take a screenshot", "screenshot"),
    "status":     ("system status", "how are you", "status"),
    "desktop":    ("show desktop", "minimize everything"),
    "close":      ("close window", "close this"),
    "joke":       ("tell me a joke", "joke"),
    "help":       ("what can you do", "help"),
}
# key -> (section, what it does). Drives the Commands tab: every phrase in COMMANDS is listed.
COMMAND_INFO = {
    "shutdown":   ("Power", "asks you to confirm, then shuts down in 10 s"),
    "restart":    ("Power", "asks you to confirm, then restarts in 10 s"),
    "cancel":     ("Power", "aborts a pending shutdown / restart (also answers a confirm with no)"),
    "yes":        ("Power", "confirms a shutdown / restart"),
    "sleep":      ("Power", "puts the PC to sleep (wake it with a key or the power button)"),
    "lock":       ("Power", "locks Windows"),
    "screen_off": ("Power", "turns the display off; PC and Xyrus keep running"),
    "wake":       ("Power", "turns the display back on"),
    "vol_up":     ("Sound & media", "+10 % (a little = 5, a lot = 25), says the new level - any wording works"),
    "vol_down":   ("Sound & media", "−10 % (a little = 5, a lot = 25), says the new level - any wording works"),
    "vol_max":    ("Sound & media", "volume to 100 %; also 'set the volume to 30', 'volume forty percent'"),
    "mute":       ("Sound & media", "mutes the speakers; turn off the sound mutes too - it never shuts the PC down"),
    "unmute":     ("Sound & media", "sound back on"),
    "vol_query":  ("Sound & media", "tells you the current volume"),
    "bright_up":  ("Power", "screen brightness +10 % (also 'set brightness to 70', 'brightness max')"),
    "bright_down": ("Power", "screen brightness −10 %"),
    "play":       ("Sound & media", "play / pause whatever is playing (add a song name to play it)"),
    "next":       ("Sound & media", "next track"),
    "prev":       ("Sound & media", "previous track"),
    "desktop":    ("Apps & windows", "minimises everything"),
    "close":      ("Apps & windows", "closes the active window"),
    "screenshot": ("Info & tools", "saves a screenshot to Pictures\\Xyrus"),
    "time":       ("Info & tools", "says the time"),
    "date":       ("Info & tools", "says the date"),
    "status":     ("Info & tools", "CPU, memory, battery, uptime"),
    "joke":       ("Info & tools", "tells a joke"),
    "help":       ("Info & tools", "says what it can do and opens this list"),
}
OPEN_WORDS = ("open", "launch", "start")
NUMBER_WORDS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20,
    "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
    "half": 0.5,
}
TENS = {20, 30, 40, 50, 60, 70, 80, 90}
UNITS = {"second": 1, "seconds": 1, "minute": 60, "minutes": 60, "hour": 3600, "hours": 3600}
FILLER = ("please", "for", "set", "timer", "the", "to")

PLAY_LIKE = {"play", "plays", "played", "playing", "lay", "clay", "pray"}   # "play" as the full model may hear it
SONG_ASK = {"", "music", "some music", "a song", "song", "songs", "something", "anything", "a track"}
NOISE_WORDS = {"hey", "how", "half", "show", "you", "do", "the", "a", "to", "huh", "and", "i", "oh", "uh", "it"}
CANCEL_REPLIES = {"cancel", "stop", "no", "never mind", "nothing", "forget it"}

# ------------------------------------------------------------ calendar / to-do vocabulary
UNDO_PHRASES = {"undo", "undo that", "scratch that", "delete that", "remove that"}
GREETINGS = ("good morning", "good afternoon", "good evening")
ADD_START = ("add", "put", "remind", "schedule", "note", "make a note", "take a note", "write down",
             "set a reminder", "create", "new event", "new task")
DONE_START = ("mark", "tick off", "check off", "cross off", "complete", "i finished", "i have finished",
              "i did", "i am done with", "done with", "finished")
DELETE_START = ("delete", "remove")
NEXT_PHRASES = ("what is next", "whats next", "next up", "what is coming up", "whats coming up",
                "what is my next", "whats my next")
TODO_PHRASES = ("to do list", "my to do", "my to dos", "my tasks", "my todo", "todo list")
QUERY_PHRASES = ("what do i have", "what have i got", "what is on my", "whats on my", "on my calendar",
                 "my schedule", "my calendar", "my agenda", "my day", "what is planned", "anything planned",
                 "do i have anything", "am i busy", "am i free", "what is happening", "whats happening")
LIST_PHRASES = ("to my to do list", "on my to do list", "in my to do list", "to the to do list", "to my to do",
                "to my list", "on my list", "to the list", "to my calendar", "on my calendar", "in my calendar",
                "to the calendar", "on the calendar", "to my schedule", "on my schedule")
TITLE_PREFIXES = sorted((
    "add a to do", "add to do", "add a task", "add task", "add an event", "add event", "add an appointment",
    "add a meeting", "add a note", "add note", "make a note", "take a note", "write down", "set a reminder to",
    "set a reminder", "remind me to", "remind me about", "remind me", "note that", "note", "put", "add",
    "create", "schedule", "new event", "new task", "a to do", "to do", "task"), key=len, reverse=True)
DONE_SUFFIXES = (" off my list", " from my list", " as completed", " as complete", " as done", " is done",
                 " complete", " done", " off")
CAL_WORDS = (
    "what", "do", "i", "have", "got", "is", "on", "my", "calendar", "schedule", "agenda", "planned",
    "anything", "busy", "free", "happening", "coming", "up", "next", "these", "few", "couple", "of", "rest",
    "today", "tonight", "tomorrow", "day", "days", "week", "weeks", "weekend", "month", "this", "after",
    "morning", "afternoon", "evening", "good", "every", "at", "in", "am", "pm", "noon", "midnight", "before",
    "to do", "list", "tasks", "task", "event", "appointment", "meeting", "note", "notes", "my day",
    "add", "put", "remind", "me", "mark", "as", "done", "finished", "complete", "delete", "remove",
    "undo", "scratch that", "tick off",
) + tuple(W.WEEKDAYS) + tuple(W.MONTHS) + tuple(W.ORDINALS)
# Shown in the Commands tab (section "Calendar & to-dos"): (phrase, what it does)
CALENDAR_HELP = [
    ("what do I have today / tomorrow / this week / next week / this weekend / this month", "reads your events and to-dos"),
    ("what do I have in the next three days / the next ten days / these 3 days / the next two weeks", "any number of days or weeks"),
    ("what do I have on Friday / on the twentieth / on October third", "one specific day"),
    ("what's next / what's coming up", "your next event or to-do"),
    ("read my to do list / what's on my to do list", "open to-dos: overdue, today, later"),
    ("add buy milk to my to do list [tomorrow]", "adds a to-do (today if you don't say a day)"),
    ("remind me to call mom at five / tomorrow at nine / in twenty minutes", "a to-do that speaks and pops up at that time"),
    ("add dentist appointment on Friday at three pm", "adds an event and reminds you 10 minutes before"),
    ("schedule gym every Monday at six pm", "repeating event (every day / every <weekday>)"),
    ("add a note for Friday that the rent is due / note buy a charger", "a note on that day (today if no day)"),
    ("mark buy milk as done / I finished the report", "ticks off a to-do"),
    ("delete the dentist appointment", "removes an event or to-do"),
    ("undo / scratch that", "removes the last thing you added by voice"),
    ("good morning / good afternoon / good evening", "greets you with today's plan (also automatic once a day)"),
]

WAKE_REPLIES = (
    "Waiting for your command, sir.",
    "At your service, sir.",
    "Yes, sir?",
    "I'm listening, sir.",
)

HELP_TEXT = ("I can shut down, restart, sleep, lock, or turn the screen off and on. "
             "Control volume and music, and play any song on YouTube. Open apps. Set timers. Take screenshots. "
             "Manage your calendar and to-do list. Tell you the time, the date, system status, or a joke.")


def build_grammar() -> list:
    words = set(WAKE_WORDS) | set(FILLER) | set(NUMBER_WORDS) | set(UNITS) | set(OPEN_WORDS)
    for phrases in COMMANDS.values():
        words.update(phrases)
    words.update(CAL_WORDS)
    words.update(MEANING_WORDS)
    words.update(SONG_FIX_WORDS)
    for app in config.get("apps", {}):
        words.add(app)
    return sorted(words) + ["[unk]"]


def phrase_in(text: str, phrase: str) -> bool:
    return f" {phrase} " in f" {text} "


def strip_wake_word(text: str):
    """Return (had_wake_word, remainder)."""
    words = text.split()
    for i, w in enumerate(words):
        if w in WAKE_WORDS:
            return True, " ".join(words[i + 1:])
    return False, text


SONG_FOLLOWUP = 40.0      # seconds after a song starts in which "not that one" works without the wake word
NOT_THAT = ("not that one", "not this one", "not that song", "not this song", "wrong song", "wrong one",
            "thats not it", "that is not it", "another one", "a different one", "different one",
            "different song", "next result", "the next result", "try another", "not the right song")
SONG_FIX_WORDS = {"not", "that", "this", "one", "wrong", "song", "another", "different", "result", "try",
                  "right", "thats", "it"} | set(NOT_THAT)


def is_not_that(text: str) -> bool:
    t = _norm_cmd(text)
    return any(phrase_in(t, p) for p in NOT_THAT)


MEDIA_AFTER_PLAY = ("next", "the next", "previous", "the previous", "the last", "pause", "again")


def is_song_request(rest: str) -> bool:
    """'play alone' / 'play [unk]' is a song. Bare 'play' stays media play-pause, and
    'play the next song' / 'play the previous one' are track controls, not searches."""
    words = rest.split()
    if len(words) < 2 or words[0] != "play":
        return False
    after = " ".join(words[1:])
    return not any(after == p or after.startswith(p + " ") for p in MEDIA_AFTER_PLAY)


def words_after_play(words):
    """Words after the (possibly misheard) 'play' at the start of an utterance."""
    for i, w in enumerate(words[:3]):
        if w in PLAY_LIKE:
            return words[i + 1:]
    for i, w in enumerate(words[:2]):
        if w in WAKE_WORDS:
            return words[i + 1:]
    return words


def clean_song_title(text: str) -> str:
    """Drop filler around a spoken title: 'the song shape of you on youtube please' -> 'shape of you'.
    Only unambiguous filler - 'some' / 'me' stay, since titles start with them (Some Nights)."""
    t = " ".join(w for w in text.lower().split() if w != "[unk]")
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


def _norm_cmd(text: str) -> str:
    return " ".join(text.lower().replace("'", "").replace("-", " ").split())


def cal_intent(rest: str):
    """Which calendar command this is: add / done / delete / query / next / todos / undo / briefing, or None."""
    r = _norm_cmd(rest)
    if not r:
        return None
    if r in UNDO_PHRASES:
        return "undo"
    if r in GREETINGS:
        return "briefing"
    if any(r == p or r.startswith(p + " ") for p in ADD_START):
        return "add"
    if any(r.startswith(p + " ") for p in DONE_START):
        return "done"
    if any(r.startswith(p + " ") for p in DELETE_START) and r.split()[1:] != ["that"]:
        return "delete"
    if any(phrase_in(r, p) for p in NEXT_PHRASES):
        return "next"
    if any(phrase_in(r, p) for p in TODO_PHRASES):
        return "todos"
    if any(phrase_in(r, p) for p in QUERY_PHRASES):
        return "query"
    return None


def align_intent_word(free_words, grammar_rest):
    """The full-vocabulary pass can mishear the command word ('add' -> 'ad', 'mark' -> 'mac') that the
    grammar pass got right. When they disagree about the command, put the grammar's word back:
    replace a sound-alike, otherwise prepend (so no title word is lost)."""
    g = _norm_cmd(grammar_rest).split()
    if not g or not free_words or cal_intent(" ".join(free_words)) == cal_intent(grammar_rest):
        return free_words
    if difflib.SequenceMatcher(None, free_words[0], g[0]).ratio() >= 0.5:
        return [g[0]] + free_words[1:]
    return [g[0]] + free_words


def add_kind(text: str):
    """(kind, reminding): kind is 'note' / 'task' / 'event', or None = decide by whether a time was given."""
    t = _norm_cmd(text)
    reminding = t.startswith(("remind", "set a reminder"))
    if t.startswith(("note", "add a note", "add note", "make a note", "take a note", "write down")):
        return "note", False
    if reminding or any(phrase_in(t, p) for p in ("to do", "task", "my list", "the list")):
        return "task", reminding
    if t.startswith("schedule") or any(phrase_in(t, p) for p in ("event", "appointment", "meeting", "calendar")):
        return "event", False
    return None, False


def clean_cal_title(title: str) -> str:
    """'add buy milk to my to do list' -> 'buy milk'; 'remind me to call mom' -> 'call mom'."""
    t = " " + _norm_cmd(title) + " "
    for p in LIST_PHRASES:
        t = t.replace(" " + p + " ", " ")
    t = " ".join(w for w in t.split() if w not in WAKE_WORDS and w not in ("unk", "[unk]"))
    for _ in range(2):
        for p in TITLE_PREFIXES:
            if t == p or t.startswith(p + " "):
                t = t[len(p):].strip()
                break
    words = t.split()
    while words and words[0] in W.LEAD_FILLER:
        words.pop(0)
    while words and words[-1] in W.TRAIL_FILLER:
        words.pop()
    return " ".join(words)


# ------------------------------------------------------------ meaning layer
# Commands are recognised by what the words MEAN, not by fixed sentences: any sound word plus any
# "down" word lowers the volume, fillers ("can you", "please", "for me") don't matter, and the
# object decides between look-alikes ("turn off the sound" mutes; it never shuts the PC down).
REWRITES = sorted({
    "turn it down": "down vimplied", "turn down": "down vimplied", "bring it down": "down vimplied",
    "tone it down": "down vimplied", "tone down": "down vimplied", "dial it down": "down vimplied",
    "keep it down": "down vimplied",
    "turn it up": "up vimplied", "turn up": "up vimplied", "pump it up": "up vimplied", "pump up": "up vimplied",
    "crank it up": "up vimplied", "crank up": "up vimplied", "dial it up": "up vimplied",
    "too loud": "quieter", "so loud": "quieter", "less loud": "quieter", "not so loud": "quieter",
    "too quiet": "louder", "too low": "louder", "cant hear": "louder", "can not hear": "louder",
    "cannot hear": "louder", "how loud": "what volume",
    "un mute": "unmute", "sound on": "unmute", "sound back on": "unmute", "turn the sound on": "unmute",
    "turn on the sound": "unmute", "turn the sound back on": "unmute", "bring the sound back": "unmute",
    "bring back the sound": "unmute", "switch on the sound": "unmute", "switch the sound on": "unmute",
    "sound off": "mute", "no sound": "mute", "turn the sound off": "mute", "turn off the sound": "mute",
    "switch off the sound": "mute", "switch the sound off": "mute", "kill the sound": "mute", "silence": "mute",
    "a little bit": "littlebit", "a tiny bit": "littlebit", "a little": "littlebit", "a bit": "littlebit",
    "a tad": "littlebit", "little bit": "littlebit", "a lot": "alot",
    "all the way up": "max", "all the way down": "min", "as loud as possible": "volume max",
    "todays": "today", "print screen": "screenshot", "screen shot": "screenshot", "screen capture": "screenshot",
}.items(), key=lambda kv: len(kv[0]), reverse=True)
SOUND_WORDS = {"volume", "sound", "sounds", "audio", "music", "speaker", "speakers", "noise"}
BRIGHT_WORDS = {"brightness", "bright", "brighter", "dimmer", "dim", "darker", "lighter"}
SCREEN_WORDS = {"screen", "display", "monitor", "displays", "monitors"}
PC_WORDS = {"computer", "pc", "laptop", "system", "machine", "everything", "windows"}
DOWN_WORDS = {"down", "lower", "decrease", "reduce", "drop", "less", "lessen", "cut", "quieter", "softer",
              "dimmer", "dim", "darker"}
UP_WORDS = {"up", "raise", "increase", "higher", "more", "boost", "louder", "amplify", "brighter", "lighter"}
MAX_WORDS = {"max", "maximum", "full", "loudest", "highest", "brightest"}
MIN_WORDS = {"min", "minimum", "lowest", "darkest"}
SMALL_WORDS = {"littlebit", "slightly", "bit", "little", "tad", "tiny"}
BIG_WORDS = {"alot", "lot", "much", "way", "lots"}
QUESTION_WORDS = {"what", "whats", "how", "which", "current", "currently", "level", "tell", "is"}
MEDIA_OBJECTS = {"music", "song", "songs", "video", "track", "playing", "youtube", "playback"}
OFF_VERBS = {"turn", "switch", "shut", "power", "off", "down", "shutdown", "put"}
FILLER_WORDS = {
    "can", "could", "would", "will", "you", "please", "kindly", "for", "me", "just", "i", "want", "need",
    "to", "hey", "the", "my", "a", "an", "now", "right", "this", "that", "go", "ahead", "and", "then", "sir",
    "do", "it", "of", "so", "very", "again", "ok", "okay", "yeah", "hi", "hello", "our", "your", "its", "is",
    "be", "make", "get", "some", "bit", "little", "littlebit", "alot", "lot", "[unk]", "unk",
} | set(WAKE_WORDS)
MEANING_WORDS = (
    SOUND_WORDS | BRIGHT_WORDS | SCREEN_WORDS | PC_WORDS | DOWN_WORDS | UP_WORDS | MAX_WORDS | MIN_WORDS
    | QUESTION_WORDS | MEDIA_OBJECTS | OFF_VERBS
    | {"slightly", "bit", "little", "tad", "tiny", "lot", "much", "way", "lots", "on", "restart", "reboot",
       "sleep", "hibernate", "mode", "lock", "wake", "pause", "resume", "unpause", "stop", "continue", "keep",
       "next", "skip", "previous", "back", "last", "time", "date", "day", "today", "status", "cpu",
       "processor", "memory", "ram", "battery", "performance", "uptime", "joke", "jokes", "funny", "laugh",
       "help", "commands", "show", "desktop", "minimize", "hide", "all", "close", "exit", "quit", "window",
       "app", "open", "launch", "start", "run", "load", "fire", "bring", "screenshot", "shot", "capture",
       "grab", "picture", "snap", "photo", "set", "at", "by", "percent", "hundred", "can", "could", "would",
       "you", "please", "make", "me", "too", "loud", "quiet", "hear", "can't", "cant", "cannot", "not",
       "silence", "mute", "un", "calendar", "kill", "tone", "dial", "crank", "pump"}
    | {k for k, _ in REWRITES if "todays" not in k}
)
DESTRUCTIVE = {"shutdown", "restart"}


def _rewrite(text: str) -> str:
    t = " " + _norm_cmd(text) + " "
    for old, new in REWRITES:
        t = t.replace(" " + old + " ", " " + new + " ")
    return " ".join(t.split())


def spoken_number(words):
    """The last number said ('forty five' -> 45, '30' -> 30, 'a hundred' -> 100), or None."""
    out, prev = None, None
    for w in words:
        if w == "hundred":
            out, prev = (out or 1) * 100, None
            continue
        n = int(w) if w.isdigit() else (NUMBER_WORDS.get(w) if w not in ("a", "an", "half") else None)
        if n is None:
            prev = None
            continue
        out = out + n if (prev in TENS and 1 <= n <= 9 and out is not None) else n
        prev = n
    return out


def match_app(name: str):
    """'google chrome' -> 'chrome', 'the calculater' -> 'calculator'; None if nothing is close."""
    apps = config.get("apps", {})
    name = name.strip()
    if not name:
        return None
    if name in apps:
        return name
    for key in sorted(apps, key=len, reverse=True):
        if phrase_in(name, key):
            return key
    m = difflib.get_close_matches(name, list(apps), n=1, cutoff=0.75)
    return m[0] if m else None


def _level_intent(kind, ws, num):
    """Volume / brightness: query, set to N, max/min, or step up/down (a little = 5, a lot = 25, by N)."""
    direction = ws & (UP_WORDS | DOWN_WORDS)
    if ws & QUESTION_WORDS and not direction and num is None and not ws & (MAX_WORDS | MIN_WORDS):
        return kind + "_query", None
    if ws & MAX_WORDS:
        return kind + "_set", 100
    if ws & MIN_WORDS:
        return kind + "_set", 0
    if num is not None and not ("by" in ws and direction):
        return kind + "_set", num
    step = num if (num is not None and "by" in ws) else 5 if ws & SMALL_WORDS else 25 if ws & BIG_WORDS else 10
    if ws & DOWN_WORDS:
        return kind + "_step", -step
    if ws & UP_WORDS:
        return kind + "_step", step
    return None, None


def _understand(words):
    ws = set(words)
    num = spoken_number(words)
    if ws & {"timer", "countdown", "stopwatch"}:               # timers: any duration wording
        if ws & {"cancel", "stop"}:
            return "cancel_timer", None
        secs = parse_duration(words)
        return ("timer", secs) if secs > 0 else ("timer_missing", None)
    sound, bright, screen = ws & SOUND_WORDS, ws & BRIGHT_WORDS, ws & SCREEN_WORDS
    media = ws & MEDIA_OBJECTS

    # 1. power & screen: the OBJECT decides; an unknown object ("the lights") never powers anything off
    if ("off" in ws and (ws & {"turn", "switch", "shut", "power", "put"} or screen or sound)) \
            or "shutdown" in ws or ("down" in ws and ws & {"shut", "power"}):
        if screen:
            return "screen_off", None
        if ws & {"music", "song", "video", "youtube"}:
            return "play", None                                   # "turn off the music" = pause it
        if sound:
            return "mute", None
        leftover = ws - FILLER_WORDS - OFF_VERBS - PC_WORDS
        return ("shutdown", None) if not leftover else ("cant", None)     # "turn off the lights"
    if "on" in ws and ws & {"turn", "switch"}:
        if screen:
            return "wake", None
        if sound and not media:
            return "unmute", None
        return (None, None) if media else ("cant", None)
    if ws & {"restart", "reboot"}:
        return ("restart", None) if not (ws - FILLER_WORDS - {"restart", "reboot"} - PC_WORDS) else ("cant", None)
    if ws & {"sleep", "hibernate"}:
        if screen:
            return "screen_off", None
        left = ws - FILLER_WORDS - {"sleep", "hibernate", "mode", "put", "go"} - PC_WORDS
        return ("sleep", None) if not left else ("cant", None)
    if "lock" in ws:
        left = ws - FILLER_WORDS - {"lock", "up"} - PC_WORDS - SCREEN_WORDS
        return ("lock", None) if not left else ("cant", None)
    if "wake" in ws:
        return "wake", None
    if "unmute" in ws:
        return "unmute", None
    if "mute" in ws:
        return "mute", None

    # 2. screenshot (before brightness: "capture the screen" mentions the screen too)
    if "screenshot" in ws or (screen and ws & {"shot", "capture", "grab", "picture", "snap", "snapshot", "photo", "image"}):
        return "screenshot", None

    # 3. brightness
    if bright or (screen and ws & (UP_WORDS | DOWN_WORDS)):
        return _level_intent("bright", ws, num)

    # 4. media controls
    if ws & {"pause", "resume", "unpause"} or (ws & {"stop", "continue", "keep"} and media):
        return "play", None
    if ws & {"next", "skip"} and not ws & {"week", "weekend", "month", "day", "days", "time"}:
        return "next", None
    if "previous" in ws or ("back" in ws and media) or ("last" in ws and ws & {"song", "track", "video"}):
        return "prev", None

    # 5. volume
    if sound or ws & {"quieter", "louder", "softer", "vimplied"}:
        return _level_intent("vol", ws, num)

    # 6. information and the rest
    if "time" in ws and not ws & {"minutes", "seconds", "hours", "minute", "second", "hour"}:
        return "time", None
    if "date" in ws or ("day" in ws and ws & {"what", "whats", "which"}) \
            or ("today" in ws and ws & {"what", "whats"} and not (ws - FILLER_WORDS - {"what", "whats", "today"})):
        return "date", None
    if ws & {"status", "cpu", "processor", "memory", "ram", "battery", "performance", "uptime"}:
        return "status", None
    if ws & {"joke", "jokes", "funny", "laugh"}:
        return "joke", None
    if "help" in ws or "commands" in ws or {"what", "can", "you", "do"} <= ws:
        return "help", None
    if {"show", "desktop"} <= ws or (ws & {"minimize", "minimise", "hide"} and ws & {"everything", "all", "windows"}):
        return "desktop", None
    if ws & {"close", "exit", "quit"} and ws & {"window", "this", "it", "app"} and "tab" not in ws:
        return "close", None
    for i, w in enumerate(words):                              # "can you open chrome for me"
        two = " ".join(words[i:i + 2])
        if w in ("open", "launch", "start", "run", "load") or two in ("fire up", "bring up", "go to", "show me"):
            skip = 2 if two in ("fire up", "bring up", "go to", "show me") else 1
            name = " ".join(x for x in words[i + skip:]
                            if x not in FILLER_WORDS | {"up", "app", "application", "program"})
            if name in ("calendar", "todo", "to do", "to do list", "schedule", "agenda"):
                return "show_calendar", None
            app = match_app(name)
            if app:
                return "open", app
            break
    if "calendar" in ws and ws & {"show", "open"}:
        return "show_calendar", None
    return None, None


def understand(text: str):
    """What the user means, as (cmd, arg) for Arc.run, or (None, None). Destructive meanings are
    refused when part of the sentence wasn't heard ([unk])."""
    words = _rewrite(text).split()
    cmd, arg = _understand(words)
    if cmd in DESTRUCTIVE and "[unk]" in words:
        return None, None
    return cmd, arg


def legacy_command(text: str):
    """The old fixed-phrase table, as a fallback - minus anything destructive heard only in part."""
    cmd, arg = find_command(text)
    if cmd in DESTRUCTIVE and "[unk]" in text.split():
        return None, None
    return cmd, arg


def parse_duration(words) -> int:
    """'seven minutes' -> 420.  'twenty five seconds' -> 25.  'half an hour' -> 1800.
    Numbers reset unless they chain like 'twenty' + 'five', so the last number
    spoken wins — which also survives 'for' being heard as 'four'."""
    number, prev, unit, article = 0.0, None, 60, False
    for w in words:
        if w in ("a", "an"):
            article = True
        elif w in NUMBER_WORDS:
            n = NUMBER_WORDS[w]
            if prev in TENS and 1 <= n <= 9:
                number += n                      # "twenty" + "five"
            elif w == "half" and number > 0:
                number += 0.5                    # "one and a half"
            else:
                number = n                       # a fresh number replaces the old one
            prev = n
        elif w in UNITS:
            unit = UNITS[w]
    if number == 0 and article:
        number = 1                               # "a minute", "an hour"
    if number == 0.5 and unit == 1:
        number = 30                              # "half a minute" style oddities: treat as 30 s
        unit = 1
    return int(round(number * unit))


def find_command(text: str):
    """Return (command, argument) or (None, None)."""
    text = text.strip()
    if not text:
        return None, None
    words = text.split()

    if words[0] in OPEN_WORDS and len(words) > 1:
        name = " ".join(words[1:])
        apps = config.get("apps", {})
        for key in sorted(apps, key=len, reverse=True):
            if phrase_in(name, key):
                return "open", key
        return "open", name

    if "timer" in words:
        if any(w in ("cancel", "stop") for w in words):
            return "cancel_timer", None
        secs = parse_duration(words)
        return ("timer", secs) if secs > 0 else ("timer_missing", None)

    for cmd, phrases in COMMANDS.items():
        for p in sorted(phrases, key=len, reverse=True):
            if phrase_in(text, p):
                return cmd, p
    return None, None


# --------------------------------------------------------------------------- #
# Text-to-speech
# --------------------------------------------------------------------------- #
_say_q: "queue.Queue[str]" = queue.Queue()
speaking = threading.Event()


def _speaker():
    import comtypes.client
    comtypes.CoInitialize()
    voice = comtypes.client.CreateObject("SAPI.SpVoice")
    while True:
        text = _say_q.get()
        speaking.set()
        try:
            voice.Speak(text)
        except Exception as e:
            log.warning("tts failed: %s", e)
        finally:
            speaking.clear()


def _wait_for_speech(timeout=8.0):
    deadline = time.time() + timeout
    time.sleep(0.3)
    while speaking.is_set() and time.time() < deadline:
        time.sleep(0.05)


# --------------------------------------------------------------------------- #
# The assistant
# --------------------------------------------------------------------------- #
class Arc:
    def __init__(self, calendar=None):
        self.paused = False
        self.status = "starting"
        self.level = 0.0                         # mic level 0..1 for the UI
        self.events = deque(maxlen=300)          # (time, kind, text) for the UI
        self.ui_queue: "queue.Queue[str]" = queue.Queue()
        self.grammar_dirty = False
        self.awaiting_command_until = 0.0
        self.arm_after_speech = False            # start the command window once the reply is spoken
        self.awaiting_song_until = 0.0           # after "What should I play, sir?"
        self.last_song = None                    # {"query", "tried": [urls]} for "not that one"
        self.song_followup_until = 0.0
        self.arm_song_after_speech = False
        self.model = None                        # set by the listener; used to re-decode song names
        self.run_async = lambda fn: threading.Thread(target=fn, daemon=True).start()
        self.calendar = calendar if calendar is not None else CS.CalendarStore()
        self.now = dt.datetime.now                  # tests pin the clock
        self.notify = lambda title, message: None   # main() points this at the tray toast
        self.pending_confirm = None
        self.pending_confirm_until = 0.0
        self.timer = None
        self.timer_end = 0.0

    # -- output --------------------------------------------------------------
    def event(self, kind, text):
        self.events.append((time.strftime("%H:%M:%S"), kind, text))

    def say(self, text: str, force=False):
        log.info("say: %s", text)
        self.event("say", text)
        if force or config.get("voice_replies", True):
            _say_q.put(text)

    # -- state helpers -------------------------------------------------------
    def _armed(self):
        return time.time() < self.awaiting_command_until

    def _confirming(self):
        return self.pending_confirm and time.time() < self.pending_confirm_until

    # -- main entry from the recognizer --------------------------------------
    def handle(self, text: str, pcm: bytes = None):
        """text = what the command grammar heard; pcm = the raw audio of that utterance (None when typed)."""
        text = text.strip()
        if not text:
            return
        if self.last_song and time.time() < self.song_followup_until and is_not_that(text):
            log.info("heard: %s", text)
            self.event("heard", text)
            self._next_song()
            return
        if time.time() < self.awaiting_song_until:
            self._song_answer(text, pcm)
            return
        if text == "[unk]":
            return
        log.info("heard: %s", text)
        self.event("heard", text)

        had_wake, rest = strip_wake_word(text)

        # A running timer can be cancelled without the wake word — it's harmless.
        if self.timer and not had_wake and find_command(text)[0] == "cancel_timer":
            self.run("cancel_timer")
            return

        if self._confirming():
            cmd, _ = find_command(rest if had_wake else text)
            if cmd == "yes":
                pending, self.pending_confirm = self.pending_confirm, None
                self._fire(pending)
                return
            if cmd == "cancel":
                self.pending_confirm = None
                self.say("Cancelled.")
                return

        if had_wake:
            if self.last_song and is_not_that(rest):
                self.awaiting_command_until = 0
                self._next_song()
                return
            if is_song_request(rest):
                self.awaiting_command_until = 0
                self._song_request(rest, pcm)
                return
            if self._calendar(rest, pcm):
                self.awaiting_command_until = 0
                return
            cmd, arg = understand(rest)
            if not cmd:
                cmd, arg = legacy_command(rest)
            if cmd:
                self.awaiting_command_until = 0
                self.run(cmd, arg)
            elif pcm and rest.strip() and self._free_fallback(pcm):
                self.awaiting_command_until = 0
            else:
                self.awaiting_command_until = time.time() + COMMAND_WINDOW
                if not rest.strip():
                    self.arm_after_speech = True
                    self.say(random.choice(WAKE_REPLIES))
            return

        if self._armed():
            if is_song_request(text):
                self.awaiting_command_until = 0
                self._song_request(text, pcm)
                return
            if self._calendar(text, pcm):
                self.awaiting_command_until = 0
                return
            cmd, arg = understand(text)
            if not cmd:
                cmd, arg = legacy_command(text)
            if cmd:
                self.awaiting_command_until = 0
                self.run(cmd, arg)
            elif pcm and (len(text.split()) >= 2 or "[unk]" in text.split()) and self._free_fallback(pcm):
                self.awaiting_command_until = 0

    # -- command execution ---------------------------------------------------
    # -- calendar / to-do list --------------------------------------------------
    def _calendar(self, rest: str, pcm) -> bool:
        """Calendar and to-do commands. Returns True if `rest` was one (and handles it)."""
        intent = cal_intent(rest)
        if intent is None or self.calendar is None:
            return False
        log.info("command: calendar %s", intent)
        self.event("cmd", f"calendar: {intent}")
        now = self.now()
        st = self.calendar
        try:
            if intent in ("add", "done", "delete"):
                text = rest
                if pcm:   # titles aren't in the command grammar: re-hear the whole utterance
                    fw = self.hear_free(pcm).split()
                    at = next((i for i, w in enumerate(fw[:3]) if w in WAKE_WORDS), -1)
                    fw = align_intent_word(fw[at + 1:], rest)
                    if fw:
                        text = " ".join(fw)
                getattr(self, "_cal_" + intent)(text, now)
            elif intent == "query":
                start, end, label = W.parse_range(_norm_cmd(rest), now) or (now.date(), now.date(), "today")
                text, overflow = CS.describe_range(st, start, end, label, now)
                self.say(text)
                if overflow:
                    self.ui_queue.put(f"calendar:{start}:{end}")
            elif intent == "next":
                self.say(CS.describe_next(st, now))
            elif intent == "todos":
                self.say(CS.describe_todos(st, now))
            elif intent == "undo":
                title = st.undo_last()
                self.say(f"Removed: {title}." if title else "There's nothing to undo, sir.")
            elif intent == "briefing":
                self.say(CS.briefing(st, now))
        except Exception:
            log.exception("calendar command failed")
            self.say("Something went wrong with the calendar, sir.")
        return True

    def _cal_add(self, text: str, now):
        st, today = self.calendar, now.date()
        kind, reminding = add_kind(text)
        w = W.parse_when(_norm_cmd(text), now)
        title = clean_cal_title(w.title if w else text)
        if not title:
            self.say("I didn't catch what to add, sir. Try: add buy milk to my to-do list.")
            return
        title = title[:1].upper() + title[1:]
        if kind == "note":
            d = w.start.date() if (w and W.mentions_date(text)) else today
            st.add_note(d, title, source="voice")
            self.say(f"Noted for {W.day_name(d, today)}: {title}.")
            return
        timed = w is not None and not w.all_day
        date = w.start.date() if w else today
        at = w.start.time().replace(second=0, microsecond=0) if timed else None
        if kind is None:
            kind = "event" if timed else "task"
        if w is not None and w.reminder_min is not None:
            rem = w.reminder_min
        elif kind == "task":
            rem = 0 if (timed or (reminding and date > today)) else None
        else:
            rem = 10 if timed else None
        repeat = w.repeat if (w and kind == "event") else None
        when_txt = W.say_when(w, now) if w else "today"
        if repeat:
            when_txt += " every day" if repeat == "daily" else " every " + w.start.strftime("%A")
        if kind == "task":
            st.add_task(title, date, at, rem, source="voice")
            if timed:
                self.say(f"I'll remind you {when_txt}: {title}.")
            else:
                msg = f"Added to your to-do list for {W.day_name(date, today)}, sir: {title}."
                if rem is not None:
                    msg += " I'll remind you that morning."
                elif reminding:
                    msg += " Tell me a time if you want a reminder."
                self.say(msg)
        else:
            st.add_event(title, date, at, rem, repeat=repeat, source="voice")
            self.say(f"Added to your calendar: {title}, {when_txt}."
                     + (" I'll remind you ten minutes before." if rem == 10 else ""))
        self.event("info", f"added {kind}: {title} ({date}{' ' + at.strftime('%H:%M') if at else ''})")

    def _cal_done(self, text: str, now):
        t = _norm_cmd(text)
        for p in sorted(DONE_START, key=len, reverse=True):
            if t.startswith(p + " "):
                t = t[len(p):].strip()
                break
        for suf in DONE_SUFFIXES:
            if t.endswith(suf):
                t = t[: -len(suf)].strip()
                break
        t = clean_cal_title(t) or t
        it = self.calendar.find(t, kinds=("task",), only_open=True)
        if it is None:
            self.say(f"I couldn't find {t} on your to-do list, sir.")
            return
        self.calendar.set_done(it["id"])
        self.say(f"Done: {it['title']}. Nice work, sir.")

    def _cal_delete(self, text: str, now):
        t = _norm_cmd(text)
        for p in DELETE_START:
            if t.startswith(p + " "):
                t = t[len(p):].strip()
        t = clean_cal_title(t) or t
        it = self.calendar.find(t)
        if it is None:
            self.say(f"I couldn't find {t} on your calendar, sir.")
            return
        self.calendar.delete_item(it["id"])
        self.say(f"Deleted {it['title']}, {W.day_name(dt.date.fromisoformat(it['date']), now.date())}.")

    def _free_fallback(self, pcm) -> bool:
        """The grammar only half-heard it ('cyrus [unk] ...'): try again with the full vocabulary."""
        fw = self.free_decode(pcm).split()
        at = next((i for i, w in enumerate(fw[:3]) if w in WAKE_WORDS), -1)
        rest = " ".join(fw[at + 1:])
        if is_song_request(rest):
            self._song_request(rest, pcm)          # Whisper re-hears the title if it's installed
            return True
        if self._calendar(rest, pcm):
            return True
        cmd, arg = understand(rest)
        if cmd:
            log.info("understood from the full-vocabulary pass: %s", rest)
            self.run(cmd, arg)
            return True
        return False

    def calendar_tick(self, now=None):
        """One scheduler step (main() runs it every 15 s): due reminders + the once-a-day briefing."""
        if self.calendar is None:
            return
        now = now or self.now()
        for it, occ, kind in CS.due_reminders(self.calendar, now):
            if kind == "stale":
                self.event("info", f"missed reminder while away: {it['title']} ({occ})")
                continue
            text = CS.reminder_text(it, occ, kind, now)
            actions.screen_on()
            self.say(text, force=True)
            self.notify("Xyrus reminder", text)
            self.event("info", f"reminder: {it['title']}")
        today = now.date().isoformat()
        if config.get("briefing", True) and now.hour >= 8 and self.calendar.get_state("briefed") != today:
            self.calendar.set_state("briefed", today)
            if self.calendar.items_between(now.date(), now.date(), include_done=False) \
                    or self.calendar.overdue_tasks(now.date()):
                self.say(CS.briefing(self.calendar, now))

    # -- songs ---------------------------------------------------------------
    def speech_finished(self):
        """Called by the listener when a spoken reply ends: any waiting window starts now."""
        now = time.time()
        if self.arm_after_speech:
            self.arm_after_speech = False
            self.awaiting_command_until = now + COMMAND_WINDOW
        if self.arm_song_after_speech:
            self.arm_song_after_speech = False
            self.awaiting_song_until = now + COMMAND_WINDOW

    def free_decode(self, pcm: bytes) -> str:
        """Re-decode an utterance with the full vocabulary (song names aren't in the command grammar)."""
        if not pcm or self.model is None:
            return ""
        rec = KaldiRecognizer(self.model, SAMPLE_RATE)
        rec.AcceptWaveform(pcm)
        text = json.loads(rec.FinalResult()).get("text", "").strip()
        if text:
            log.info("heard (full vocabulary): %s", text)
            self.event("heard", f"{text}   (full vocabulary)")
        return text

    def hear_free(self, pcm: bytes) -> str:
        """Free text (song names, calendar titles): Whisper when it's installed and loaded - it knows names
        like 'Ishwar' and 'Vikings' - otherwise the small model's full vocabulary."""
        if pcm and hearing is not None and hearing.available():
            try:
                t0 = time.time()
                text = hearing.clean(hearing.transcribe(pcm))
                if text:
                    log.info("heard (whisper, %.1f s): %s", time.time() - t0, text)
                    self.event("heard", f"{text}   (whisper)")
                    return text
            except Exception as e:
                log.warning("whisper failed: %s", e)
        return self.free_decode(pcm)

    def _song_title(self, text: str, pcm, after_play: bool) -> str:
        words = self.hear_free(pcm).split() if pcm else text.split()
        if after_play:
            words = words_after_play(words)
        else:
            words = strip_wake_word(" ".join(words))[1].split()
            if words and words[0] in PLAY_LIKE:
                words = words[1:]
        return clean_song_title(" ".join(words))

    def _song_request(self, rest: str, pcm):
        title = self._song_title(rest, pcm, after_play=True)
        if title in SONG_ASK:
            self.ask_song()
        else:
            self._play_song(title)

    def _open_song(self, spoken, url, video, prefix="Playing"):
        """Pause other browser audio, say what's playing, open it, and make sure it actually plays."""
        paused_other = False
        try:
            if actions.browser_playing():          # something's already playing: pause it first
                actions.media_play()
                paused_other = True
                time.sleep(0.8)
        except Exception as e:
            log.warning("audio check failed: %s", e)
        self.say(f"{prefix} {spoken}, sir.")
        actions.open_target(url)
        self.event("info", url)
        self.song_followup_until = time.time() + SONG_FOLLOWUP
        try:
            result = actions.ensure_playing(video, trust_state=not paused_other)
        except Exception as e:
            log.warning("autoplay check failed: %s", e)
            result = "unknown"
        log.info("playback: %s", result)
        self.event("info", f"playback: {result}")
        if result == "blocked":
            self.say("The browser blocked autoplay, sir. Press play on the video.")

    def _next_song(self):
        """'Not that one': play the next YouTube result for the same request."""
        last = self.last_song
        if not last:
            self.say("I haven't played anything yet, sir.")
            return
        self.song_followup_until = 0.0
        log.info("command: next song result for %s", last["query"])
        self.event("cmd", f"not that one: {last['query']}")

        def work():
            try:
                options = actions.find_songs(last["query"])
            except Exception as e:
                log.warning("youtube lookup failed: %s", e)
                self.say("I couldn't reach YouTube, sir.")
                return
            nxt = next((o for o in options if o[1] not in last["tried"]), None)
            if nxt is None:
                self.say(f"That's all I found for {last['query']}, sir. Try saying the name again.")
                return
            last["tried"].append(nxt[1])
            self._open_song(*nxt, prefix="Trying")

        self.run_async(work)

    def ask_song(self):
        self.awaiting_song_until = time.time() + COMMAND_WINDOW + 10   # provisional; restarts when the question ends
        self.arm_song_after_speech = True
        self.say("What should I play, sir?")

    def _song_answer(self, text: str, pcm):
        """The utterance after "What should I play, sir?" - anything goes, except noise."""
        log.info("heard: %s", text)
        self.event("heard", text)
        if strip_wake_word(text)[1].strip() in CANCEL_REPLIES:
            self.awaiting_song_until = 0
            self.say("Okay, sir.")
            return
        title = self._song_title(text, pcm, after_play=False)
        if title in SONG_ASK or (len(title.split()) == 1 and title in NOISE_WORDS):
            return   # room noise: keep waiting
        self.awaiting_song_until = 0
        self._play_song(title)

    def _play_song(self, title: str):
        log.info("command: play song %s", title)
        self.event("cmd", f"play song: {title}")

        def work():
            try:
                spoken, url, video = actions.find_song(title)
            except LookupError:
                self.say(f"I couldn't find {title} on YouTube, sir. Here's the search.")
                actions.open_target(actions.youtube_search_url(title))
                return
            except Exception as e:
                log.warning("youtube lookup failed: %s", e)
                self.say("I couldn't reach YouTube, sir. Opening the search instead.")
                actions.open_target(actions.youtube_search_url(title))
                return
            self.last_song = {"query": title, "tried": [url]}
            self._open_song(spoken, url, video)

        self.run_async(work)

    def run(self, cmd: str, arg=None):
        log.info("command: %s %s", cmd, arg or "")
        self.event("cmd", f"{cmd} {arg or ''}".strip())
        try:
            self._run(cmd, arg)
        except Exception as e:
            log.exception("command failed")
            self.say(f"That failed: {e}")

    def _run(self, cmd, arg):
        a = actions
        if cmd in ("shutdown", "restart"):
            if config.get("confirm_shutdown", True):
                self.pending_confirm = cmd
                self.pending_confirm_until = time.time() + CONFIRM_WINDOW
                self.say(f"{'Shut down' if cmd == 'shutdown' else 'Restart'}? Say yes to confirm, or cancel.")
            else:
                self._fire(cmd)
        elif cmd == "sleep":
            self.say("Going to sleep."); _wait_for_speech(); a.sleep_pc()
        elif cmd == "screen_off":
            self.say("Screen off."); _wait_for_speech(); a.screen_off()
        elif cmd == "wake":
            a.screen_on(); self.say("I'm here.")
        elif cmd == "lock":
            self.say("Locking."); _wait_for_speech(); a.lock_pc()
        elif cmd == "cancel":
            a.abort_shutdown(); self.say("Cancelled.")
        elif cmd == "yes":
            pass
        elif cmd in ("vol_up", "vol_down", "vol_step"):
            delta = arg if cmd == "vol_step" else (10 if cmd == "vol_up" else -10)
            self.say(f"Volume at {a.change_volume(delta)} percent, sir.")
        elif cmd in ("vol_max", "vol_set"):
            self.say(f"Volume at {a.set_volume(100 if cmd == 'vol_max' else arg)} percent, sir.")
        elif cmd == "vol_query":
            self.say(f"The volume is at {a.get_volume()} percent{', but muted' if a.is_muted() else ''}, sir.")
        elif cmd == "mute":
            self.say("Muting, sir."); _wait_for_speech(); a.set_muted(True)
        elif cmd == "unmute":
            a.set_muted(False); self.say(f"Sound's back on, sir. Volume at {a.get_volume()} percent.")
        elif cmd in ("bright_up", "bright_down", "bright_step", "bright_set", "bright_query"):
            try:
                if cmd == "bright_query":
                    self.say(f"Brightness is at {a.get_brightness()} percent, sir.")
                else:
                    v = (a.set_brightness(arg) if cmd == "bright_set" else
                         a.change_brightness(arg if cmd == "bright_step" else (10 if cmd == "bright_up" else -10)))
                    self.say(f"Brightness at {v} percent, sir.")
            except Exception as e:
                log.warning("brightness failed: %s", e)
                self.say("I can't change this screen's brightness, sir.")
        elif cmd == "cant":
            self.say("Sorry, sir, I can't do that one.")
        elif cmd == "show_calendar":
            self.ui_queue.put("show:Calendar"); self.say("Here's your calendar, sir.")
        elif cmd == "play":      a.media_play()
        elif cmd == "next":      a.media_next()
        elif cmd == "prev":      a.media_prev()
        elif cmd == "desktop":   a.show_desktop()
        elif cmd == "close":     a.close_window()
        elif cmd == "time":      self.say(a.time_text())
        elif cmd == "date":      self.say(a.date_text())
        elif cmd == "status":    self.say(a.system_status())
        elif cmd == "joke":      self.say(a.joke())
        elif cmd == "help":
            self.ui_queue.put("show:Commands"); self.say(HELP_TEXT)
        elif cmd == "screenshot":
            path = a.screenshot(); self.say("Screenshot saved."); self.event("info", str(path))
        elif cmd == "open":
            target = config.get("apps", {}).get(arg)
            if target:
                self.say(f"Opening {arg}."); a.open_target(target)
            else:
                self.say(f"I don't know how to open {arg}. Add it in the Apps tab.")
        elif cmd == "timer":
            self._start_timer(arg)
        elif cmd == "timer_missing":
            self.say("For how long?")
        elif cmd == "cancel_timer":
            if self.timer:
                self.timer.cancel(); self.timer = None; self.say("Timer cancelled.")
            else:
                self.say("There's no timer running.")

    def _fire(self, cmd):
        if cmd == "shutdown":
            self.say(f"Shutting down in {actions.SHUTDOWN_DELAY} seconds. Say {NAME} cancel to stop.")
            actions.shutdown_pc()
        elif cmd == "restart":
            self.say(f"Restarting in {actions.SHUTDOWN_DELAY} seconds. Say {NAME} cancel to stop.")
            actions.restart_pc()

    def _start_timer(self, secs: int):
        if self.timer:
            self.timer.cancel()
        self.timer_end = time.time() + secs
        self.timer = threading.Timer(secs, self._timer_done)
        self.timer.daemon = True
        self.timer.start()
        self.say(f"Timer set for {describe_secs(secs)}.")

    def _timer_done(self):
        self.timer = None
        actions.screen_on()
        self.say("Time's up!", force=True)
        actions.beep()

    def timer_remaining(self) -> int:
        return max(0, int(self.timer_end - time.time())) if self.timer else 0


def describe_secs(secs: int) -> str:
    if secs % 3600 == 0:
        n = secs // 3600; return f"{n} hour{'s' if n != 1 else ''}"
    if secs % 60 == 0:
        n = secs // 60; return f"{n} minute{'s' if n != 1 else ''}"
    return f"{secs} seconds"


# --------------------------------------------------------------------------- #
# Microphone loop
# --------------------------------------------------------------------------- #
def listen_forever(arc: Arc):
    SetLogLevel(-1)
    model = Model(str(MODEL_DIR))
    arc.model = model
    audio_q: "queue.Queue[bytes]" = queue.Queue()

    def callback(indata, frames, t, status):
        data = bytes(indata)
        samples = array("h", data)
        if samples:
            peak = max(abs(s) for s in samples) / 32768.0
            arc.level = peak if peak > arc.level else arc.level * 0.7 + peak * 0.3
        audio_q.put(data)

    while True:
        if arc.paused:
            # Paused = microphone closed. Nothing is captured until resume.
            if arc.status != "paused":
                log.info("paused - mic closed")
                arc.status = "paused"
                arc.level = 0.0
                arc.awaiting_command_until = 0.0
                arc.pending_confirm = None
                arc.awaiting_song_until = 0.0
            time.sleep(0.2)
            continue

        rec = KaldiRecognizer(model, SAMPLE_RATE, json.dumps(build_grammar()))
        arc.grammar_dirty = False
        try:
            with sd.RawInputStream(samplerate=SAMPLE_RATE, blocksize=4000, dtype="int16",
                                   channels=1, callback=callback):
                log.info("listening")
                arc.status = "listening"
                was_speaking = False
                utt = bytearray()                  # raw audio of the current utterance
                while not arc.grammar_dirty and not arc.paused:
                    try:
                        data = audio_q.get(timeout=0.5)
                    except queue.Empty:
                        continue
                    if speaking.is_set():
                        rec.Reset()            # don't listen to ourselves
                        utt.clear()
                        was_speaking = True
                        continue
                    if was_speaking:
                        was_speaking = False
                        arc.speech_finished()      # the reply is done: now the clock starts
                    utt += data
                    if len(utt) > MAX_UTTERANCE_BYTES:
                        del utt[: len(utt) - MAX_UTTERANCE_BYTES]
                    if rec.AcceptWaveform(data):
                        pcm = bytes(utt)
                        utt.clear()
                        arc.handle(json.loads(rec.Result()).get("text", ""), pcm)
            # stream closed (pause or vocabulary change): drop whatever was buffered
            with audio_q.mutex:
                audio_q.queue.clear()
            arc.level = 0.0
        except Exception as e:
            log.error("audio stream error: %s - retrying in 3s", e)
            arc.status = "mic error, retrying"
            time.sleep(3)


# --------------------------------------------------------------------------- #
# Start-with-Windows toggle
# --------------------------------------------------------------------------- #
STARTUP_LNK = Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs/Startup" / f"{NAME}.lnk"


def autostart_enabled() -> bool:
    return STARTUP_LNK.exists()


def set_autostart(enabled: bool):
    if not enabled:
        STARTUP_LNK.unlink(missing_ok=True)
        log.info("autostart disabled")
        return
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    ps = (
        "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('%s');"
        "$s.TargetPath='%s';$s.Arguments='\"%s\" --tray';$s.WorkingDirectory='%s';"
        "$s.IconLocation='%s';$s.Description='%s voice assistant';$s.Save()"
    ) % (STARTUP_LNK, pythonw, BASE / "arc.py", BASE, ICON_FILE, NAME)
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True,
                   capture_output=True, creationflags=actions.NO_WINDOW)
    log.info("autostart enabled")


# --------------------------------------------------------------------------- #
# Tray icon
# --------------------------------------------------------------------------- #
def make_icon_image(active=True):
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    color = (124, 92, 255, 255) if active else (110, 110, 120, 255)
    d.ellipse((4, 4, 60, 60), fill=color)
    d.arc((16, 16, 48, 48), start=200, end=340, fill="white", width=6)
    d.ellipse((27, 27, 37, 37), fill="white")
    return img


def ensure_icon_file():
    if not ICON_FILE.exists():
        make_icon_image().save(ICON_FILE, sizes=[(16, 16), (32, 32), (48, 48), (64, 64)])


def build_tray(arc: Arc):
    import pystray

    def show(icon, item):
        arc.ui_queue.put("show")

    def toggle_pause(icon, item):
        arc.paused = not arc.paused
        icon.icon = make_icon_image(active=not arc.paused)
        icon.title = f"{NAME} (paused)" if arc.paused else f"{NAME} - listening"

    def toggle_autostart(icon, item):
        try:
            set_autostart(not autostart_enabled())
        except Exception as e:
            log.error("autostart toggle failed: %s", e)

    def quit_(icon, item):
        arc.ui_queue.put("quit")

    menu = pystray.Menu(
        pystray.MenuItem(f"Open {NAME}", show, default=True),
        pystray.MenuItem(lambda item: "Resume listening" if arc.paused else "Pause listening", toggle_pause),
        pystray.MenuItem("Start with Windows", toggle_autostart, checked=lambda item: autostart_enabled()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(f"Quit {NAME}", quit_),
    )
    return pystray.Icon("xyrus", make_icon_image(), f"{NAME} - listening", menu)


# --------------------------------------------------------------------------- #
def single_instance():
    kernel32.CreateMutexW(None, False, "Local\\XyrusVoiceAssistant")
    return kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS


def main():
    if not single_instance():
        log.info("already running; exiting")
        ctypes.windll.user32.MessageBoxW(0, f"{NAME} is already running.\nClick its icon in the system tray to open it.", NAME, 0x40)
        return
    if not MODEL_DIR.exists():
        log.error("model folder missing at %s", MODEL_DIR)
        sys.exit(1)

    config.load()
    ensure_icon_file()
    arc = Arc()
    if hearing is not None:
        hearing.load_async()
    threading.Thread(target=_speaker, daemon=True).start()
    threading.Thread(target=listen_forever, args=(arc,), daemon=True).start()
    arc.say(f"{NAME} is online.")

    icon = build_tray(arc)
    icon.run_detached()

    def toast(title, message):
        try:
            icon.notify(message, title)
        except Exception as e:
            log.warning("toast failed: %s", e)
    arc.notify = toast

    def scheduler():
        while True:
            try:
                arc.calendar_tick()
            except Exception:
                log.exception("calendar tick failed")
            time.sleep(15)
    threading.Thread(target=scheduler, daemon=True).start()

    from ui import XyrusWindow
    win = XyrusWindow(arc, icon, start_hidden="--tray" in sys.argv)
    win.mainloop()

    icon.stop()
    os._exit(0)


if __name__ == "__main__":
    main()
