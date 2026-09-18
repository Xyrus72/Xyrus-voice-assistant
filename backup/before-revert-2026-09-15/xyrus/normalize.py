"""Sound-alike normalisation (§5.6), wake stripping, and the shared word sets.

Pure functions and constants; any thread. T4 imports the song/noise data from here (v1 arc.py lift).
"""
from __future__ import annotations

import re
from typing import Iterable

# ----------------------------------------------------------------------------- word sets
WAKE_ALIASES = ("cyrus", "zeros", "virus", "cirrus")        # = config DEFAULTS["wake_words"]
TYPED_WAKE = "xyrus"                                          # typed input also accepts the real name
WAKE_LIKE = frozenset({"cyrus", "zeros", "virus", "cirrus", "sirius", "serious", "zero", "zira", "xyrus"})
GREETING_LEAD = ("hey", "hi", "okay")

# v1 arc.py (verified song feature, §3.6)
# + "playback": with the larger meaning-layer grammar "xyrus play believer…" is heard "cyrus playback [unk]"
PLAY_LIKE = frozenset({"play", "plays", "played", "playing", "lay", "clay", "pray", "playback"})
SONG_ASK = frozenset({"", "music", "some music", "a song", "song", "songs", "something", "anything", "a track"})
NOISE_WORDS = frozenset({"hey", "how", "half", "show", "you", "do", "the", "a", "to", "huh", "and", "i", "oh",
                         "uh", "it"})
SONG_CANCEL = frozenset({"cancel", "stop", "no", "never mind", "nothing", "forget it"})   # v1 CANCEL_REPLIES

# dialogue answer words (dialogue.py and grammar.py both use these)
CANCEL_WORDS = frozenset({"cancel", "never mind", "forget it", "stop", "leave it"})   # anywhere; not "no"
YES_WORDS = frozenset({"yes", "correct", "yep", "yeah", "right", "confirm", "sure", "okay", "ok", "yes please",
                       "do it", "go ahead", "that's right", "yes it is"})
NO_WORDS = frozenset({"no", "wrong", "nope", "not quite", "no thanks", "that's wrong"})

TIME_UNITS = frozenset({"second", "seconds", "minute", "minutes", "hour", "hours"})
_NUMBER_TOKENS = frozenset({
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve",
    "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen", "twenty", "thirty",
    "forty", "fifty", "sixty", "seventy", "eighty", "ninety", "hundred", "a", "an", "half"})
_DAY_TOKENS = frozenset({"today", "tomorrow", "tonight", "monday", "tuesday", "wednesday", "thursday", "friday",
                         "saturday", "sunday", "next", "this", "the"})
_FOUR_AFTER = frozenset({"note", "reminder", "timer", "event"})


def phrase_in(text: str, phrase: str) -> bool:
    return f" {phrase} " in f" {text} "


def _replace_seq(tokens: list[str], src: tuple[str, ...], dst: tuple[str, ...]) -> list[str]:
    out, i, n = [], 0, len(src)
    while i < len(tokens):
        if tuple(tokens[i:i + n]) == src:
            out.extend(dst)
            i += n
        else:
            out.append(tokens[i])
            i += 1
    return out


def _is_numberish(tok: str) -> bool:
    return tok in _NUMBER_TOKENS or tok.isdigit()


_PUNCT_RE = re.compile(r"[?!,;:\"()]")
_DOT_RE = re.compile(r"(?<!\d)\.|\.(?!\d)")        # periods, but not decimal points


def normalize(text: str, wake_words: Iterable[str] | None = None) -> str:
    """Lowercase, strip typed punctuation, apply §5.6 in order, collapse spaces."""
    wakes = set(wake_words or WAKE_ALIASES) | {TYPED_WAKE}
    t = (text or "").lower().replace("’", "'").replace("‘", "'")
    t = t.replace("%", " percent ")
    t = _DOT_RE.sub(" ", t)
    t = _PUNCT_RE.sub(" ", t)
    t = re.sub(r"(?<=[a-z])-(?=[a-z])", " ", t)       # twenty-five, to-do
    toks = t.split()
    if not toks:
        return ""
    # 1. leading greeting before a wake token
    if len(toks) >= 2 and toks[0] in GREETING_LEAD and toks[1] in wakes:
        toks = toks[1:]
    # 2. whats -> what's
    toks = ["what's" if w == "whats" else w for w in toks]
    # 3. a m / p m -> am / pm
    toks = _replace_seq(toks, ("a", "m"), ("am",))
    toks = _replace_seq(toks, ("p", "m"), ("pm",))
    # 4. o'clock removed
    toks = [w for w in toks if w not in ("o'clock", "oclock")]
    # 5. add in/and/a event -> add an event
    for mid in ("in", "and", "a"):
        toks = _replace_seq(toks, ("add", mid, "event"), ("add", "an", "event"))
    # 6. remembered at / remember at -> remember that (utterance start, after the wake)
    start = 0
    for i, w in enumerate(toks):
        if w in wakes:
            start = i + 1
            break
    if len(toks) >= start + 2 and toks[start] in ("remembered", "remember") and toks[start + 1] == "at":
        toks = toks[:start] + ["remember", "that"] + toks[start + 2:]
    # 7. what did are ask -> what did i ask
    toks = _replace_seq(toks, ("what", "did", "are", "ask"), ("what", "did", "i", "ask"))
    # 8. can -> ten directly before a time unit
    toks = ["ten" if w == "can" and i + 1 < len(toks) and toks[i + 1] in TIME_UNITS else w
            for i, w in enumerate(toks)]
    # 9. four -> for after note/reminder/timer/event, before a day or number word
    toks = ["for" if (w == "four" and 0 < i < len(toks) - 1 and toks[i - 1] in _FOUR_AFTER
                      and (toks[i + 1] in _DAY_TOKENS or _is_numberish(toks[i + 1]))) else w
            for i, w in enumerate(toks)]
    # 10. two/too -> to (T3: "switch two chrome"): always after switch/go/bring/move/send…, otherwise when
    # followed by a word that is not a number, unit, date or maths word. Percent slots handle "two forty".
    toks = [_two_to(toks, i) for i in range(len(toks))]
    # 11. which to -> switch to at the start of the command (T3 recognition: "xyrus switch to chrome" is
    # heard "cyrus which to chrome"; "which to <x>" is never a real request)
    start = next((i + 1 for i, w in enumerate(toks) if w in wakes), 0)
    if toks[start:start + 2] == ["which", "to"]:
        toks = toks[:start] + ["switch", "to"] + toks[start + 2:]
    return " ".join(toks)


_TO_VERBS = frozenset({"switch", "go", "bring", "move", "send", "come", "navigate", "jump", "listen", "talk",
                       "back", "get", "skip"})
_TWO_KEEP_NEXT = frozenset({
    "second", "seconds", "minute", "minutes", "hour", "hours", "day", "days", "week", "weeks", "month", "months",
    "year", "years", "percent", "hundred", "thousand", "million", "point", "and", "dice", "die", "times",
    "plus", "minus", "over", "divided", "multiplied", "squared", "things", "notes", "events", "items",
    "reminders", "timers", "more", "of", "or", "am", "pm", "people", "songs", "tabs", "windows", "today",
    "tomorrow", "tonight", "morning", "afternoon", "evening", "night", "noon", "midnight", "next", "this", "in",
    "on", "at", "the", "every", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
    "november", "december", "a", "an", "thirty", "fifteen", "forty", "forty-five"})
_TOO_ADJ = frozenset({"loud", "quiet", "low", "high", "bright", "dark", "much", "many", "fast", "slow", "soft",
                      "big", "small", "long", "early", "late", "dim", "hot", "cold"})


def _two_to(toks: list[str], i: int) -> str:
    w = toks[i]
    if w not in ("two", "too") or i + 1 >= len(toks):
        return w
    nxt = toks[i + 1]
    if _is_numberish(nxt) and nxt not in ("a", "an", "half"):
        return w
    if i > 0 and toks[i - 1] in _TO_VERBS:
        return "to"
    if w == "too" and nxt in _TOO_ADJ:
        return w
    if nxt in _TWO_KEEP_NEXT or nxt.startswith("<") or nxt == "[unk]":
        return w
    return "to"


# ============================================================================= meaning layer
# Commands are recognised by what the words MEAN (lead requirement, 2026-09-13; tables lifted from v1
# arc.py understand()): fillers ("can you", "please", "for me") are dropped, synonyms map to canonical words,
# and concept rules make word order irrelevant ("the volume, lower it" == "lower the volume" == "volume
# down"). The object decides between look-alikes: "turn off the sound" mutes and can never shut the PC down.
# canonical_items() is applied to BOTH the utterance and every registered pattern text, so any paraphrase
# that reduces to the same canonical words as a registered phrase runs that command.
ARTICLES = frozenset({"the", "my", "your", "our"})
FILLER_WORDS = frozenset({
    "can", "could", "would", "will", "you", "please", "kindly", "for", "me", "just", "i", "want", "need", "to",
    "hey", "a", "an", "now", "right", "go", "ahead", "and", "then", "sir", "do", "it", "of", "so", "very",
    "again", "hi", "hello", "its", "is", "be", "some", "u", "like", "i'd", "id", "are", "you're", "maybe",
    "actually", "quickly", "real", "really", "it's", "that's", "there's", "gonna", "wanna"})
STEP_SMALL = "@small"
STEP_BIG = "@big"
SOUND_WORDS = frozenset({"volume", "sound", "sounds", "audio", "music", "speaker", "speakers", "noise"})
BRIGHT_WORDS = frozenset({"brightness", "bright", "brighter", "dimmer", "dim", "darker", "lighter"})
SCREEN_WORDS = frozenset({"screen", "display", "monitor", "displays", "monitors", "screens"})
PC_WORDS = frozenset({"computer", "pc", "laptop", "system", "machine", "everything", "windows", "desktop"})
MEDIA_OBJECTS = frozenset({"music", "song", "songs", "video", "track", "playing", "youtube", "playback",
                           "spotify"})
UP_WORDS = frozenset({"up", "raise", "increase", "higher", "more", "boost", "louder", "amplify", "brighter",
                      "lighter", "@up"})
DOWN_WORDS = frozenset({"down", "lower", "decrease", "reduce", "drop", "less", "lessen", "cut", "quieter",
                        "softer", "dimmer", "dim", "darker", "@down"})
MAX_WORDS = frozenset({"max", "maximum", "full", "loudest", "highest", "brightest", "@max"})
MIN_WORDS = frozenset({"min", "minimum", "lowest", "darkest", "@min"})
QUESTION_WORDS = frozenset({"what", "what's", "how", "which", "current", "currently", "level", "tell", "is",
                            "know", "check", "got", "say", "@query"})
SET_VERBS = frozenset({"set", "change", "make", "put", "adjust", "turn", "switch", "bring", "at", "keep"})
OFF_VERBS = frozenset({"turn", "switch", "shut", "power", "off", "down", "shutdown", "put", "kill"})
OPEN_VERBS = frozenset({"open", "launch", "start", "run", "load"})
CLOSE_VERBS = frozenset({"close", "quit", "exit"})
APP_NOISE = frozenset({"app", "application", "program", "programme"})
_NOT_OFF_CONTEXT = frozenset({"tick", "cross", "mark", "check", "sign", "log"})
_CAL_WORDS = frozenset({"event", "events", "meeting", "appointment", "week", "weekend", "month", "day", "days",
                        "reminder", "todo", "calendar", "one", "time"})
_TIME_OK = QUESTION_WORDS | {"time", "now", "current", "please"}
_DATE_OK = QUESTION_WORDS | {"date", "day", "today"}

_R = [  # (source words, replacement) - matched longest first, after articles are dropped
    ("to do", "todo"), ("to dos", "todos"),
    ("a little bit", STEP_SMALL), ("a tiny bit", STEP_SMALL), ("a little", STEP_SMALL), ("a bit", STEP_SMALL),
    ("a tad", STEP_SMALL), ("little bit", STEP_SMALL), ("slightly", STEP_SMALL), ("a touch", STEP_SMALL),
    ("a lot", STEP_BIG), ("lots", STEP_BIG),
    ("all way up", "@max"), ("all way down", "@min"), ("as loud as possible", "@max @vimplied"),
    ("turn it down", "@down @vimplied"), ("turn down", "@down @vimplied"), ("bring it down", "@down @vimplied"),
    ("tone it down", "@down @vimplied"), ("tone down", "@down @vimplied"), ("dial it down", "@down @vimplied"),
    ("dial down", "@down @vimplied"), ("keep it down", "@down @vimplied"), ("lower it", "@down @vimplied"),
    ("turn it up", "@up @vimplied"), ("turn up", "@up @vimplied"), ("pump it up", "@up @vimplied"),
    ("pump up", "@up @vimplied"), ("crank it up", "@up @vimplied"), ("crank up", "@up @vimplied"),
    ("dial it up", "@up @vimplied"), ("dial up", "@up @vimplied"), ("raise it", "@up @vimplied"),
    ("too loud", "@down @vimplied"), ("so loud", "@down @vimplied"), ("less loud", "@down @vimplied"),
    ("not so loud", "@down @vimplied"), ("quieter", "@down @vimplied"), ("softer", "@down @vimplied"),
    ("too quiet", "@up @vimplied"), ("too low", "@up @vimplied"), ("can't hear", "@up @vimplied"),
    ("cant hear", "@up @vimplied"), ("can not hear", "@up @vimplied"), ("cannot hear", "@up @vimplied"),
    ("louder", "@up @vimplied"), ("how loud", "@query @vimplied"),
    ("un mute", "@unmute"), ("unmute", "@unmute"), ("sound back on", "@unmute"), ("bring back sound", "@unmute"),
    ("bring sound back", "@unmute"),
    ("mute", "@mute"), ("silence", "@mute"), ("no sound", "@mute"), ("kill sound", "@mute"),
    ("screen shot", "screenshot"), ("print screen", "screenshot"), ("screen capture", "screenshot"),
    ("screen grab", "screenshot"),
    ("today's", "today"), ("todays", "today"),
    ("countdown", "timer"), ("alarm", "timer"), ("stopwatch", "timer"),
    ("alert me in", "timer in"), ("ping me in", "timer in"), ("buzz me in", "timer in"),
    ("wake me up in", "timer in"), ("wake me in", "timer in"), ("let me know in", "timer in"),
    ("look up", "search"), ("search up", "search"),
    ("put on", "play"), ("listen to", "play"), ("i want to hear", "play"), ("play me", "play"),
    ("fire up", "open"), ("boot up", "open"), ("load up", "open"), ("close down", "close"),
    ("keep playing", "resume"), ("continue playing", "resume"), ("unpause", "resume"),
    ("kill", "force close"),
]
REWRITES = sorted(((tuple(a.split()), tuple(b.split())) for a, b in _R), key=lambda r: -len(r[0]))

# every word the meaning layer understands (added to the command grammar; unknown ones are dropped and logged
# by grammar.py, D13)
MEANING_WORDS = frozenset(
    {w for src, _ in REWRITES for w in src}
    | SOUND_WORDS | BRIGHT_WORDS | SCREEN_WORDS | PC_WORDS | MEDIA_OBJECTS | UP_WORDS | DOWN_WORDS | MAX_WORDS
    | MIN_WORDS | QUESTION_WORDS | SET_VERBS | OFF_VERBS | OPEN_VERBS | CLOSE_VERBS | APP_NOISE | FILLER_WORDS
    | ARTICLES | {"on", "by", "restart", "reboot", "sleep", "hibernate", "mode", "lock", "wake", "pause", "resume",
                  "stop", "next", "skip", "previous", "back", "last", "time", "date", "day", "today", "take",
                  "capture", "grab", "snap", "picture", "photo", "shot", "image", "force", "percent", "screenshot",
                  "search", "play", "timer", "in"}
) - {w for w in ("@up", "@down", "@max", "@min", "@query", "@small", "@big", "@mute", "@unmute", "@vimplied")}


# T3 grammar.py reads this name: every synonym is in the command grammar. D13: 'unmute'/'unpause' are not in
# the Vosk small model (checked with vosk_model_find_word); they stay as typed-input rewrites only.
SYNONYM_WORDS = MEANING_WORDS - {"unmute", "unpause"}
# <app> slot guards (parsing._p_app): an utterance made only of these objects is not an app name ...
NOT_APP_OBJECTS = SOUND_WORDS | SCREEN_WORDS | BRIGHT_WORDS | PC_WORDS | MEDIA_OBJECTS | {
    "lights", "light", "wifi", "internet", "bluetooth", "volume", "it", "everything"}
# ... and one containing any of these is a timer, not an app
NOT_APP_ANY = frozenset({"timer", "timers", "countdown", "alarm", "stopwatch", "minute", "minutes", "second",
                         "seconds", "hour", "hours"})


def _opaque(t: str) -> bool:
    return t.startswith("<") or t.startswith("[") or "|" in t


def _is_num(t: str, nxt: str = "") -> bool:
    if t in ("<percent>", "<number>") or t.isdigit() or t == "percent":
        return True
    if t in ("a", "an"):
        return nxt in ("hundred", "thousand")
    return t in _NUMBER_TOKENS and t not in ("a", "an", "half")


Item = tuple  # (token, (start, end)) - end exclusive, indices into the source token list


def _rewrite(items: list[Item]) -> list[Item]:
    out: list[Item] = []
    i = 0
    toks = [t for t, _ in items]
    while i < len(items):
        for src, dst in REWRITES:
            n = len(src)
            if tuple(toks[i:i + n]) == src:
                span = (items[i][1][0], items[i + n - 1][1][1])
                out.extend((d, span) for d in dst)
                i += n
                break
        else:
            out.append(items[i])
            i += 1
    return out


def _drop_fillers(items: list[Item]) -> list[Item]:
    out = []
    for i, (t, span) in enumerate(items):
        nxt = items[i + 1][0] if i + 1 < len(items) else ""
        if t in ("a", "an") and (nxt in TIME_UNITS or nxt in ("half", "hundred", "thousand") or _is_num(nxt)):
            out.append((t, span))
        elif t in FILLER_WORDS:
            continue
        else:
            out.append((t, span))
    return out


def _emit(items: list[Item], head: list[str], consume) -> list[Item]:
    """head words first (spanning the consumed tokens), then the leftovers in their original order."""
    used = [it for it in items if consume(it[0])]
    rest = [it for it in items if not consume(it[0])]
    if used:
        span = (min(s[0] for _, s in used), max(s[1] for _, s in used))
    else:
        span = items[0][1] if items else (0, 0)
    return [(h, span) for h in head] + rest


def _level(kind: str, items: list[Item], objects: frozenset) -> list[Item] | None:
    toks = [t for t, _ in items]
    ws = set(toks)
    nums = [it for i, it in enumerate(items) if _is_num(it[0], toks[i + 1] if i + 1 < len(toks) else "")]
    num_ids = {id(it) for it in nums}
    last_dir = None
    for t in toks:
        if t in UP_WORDS:
            last_dir = "up"
        elif t in DOWN_WORDS:
            last_dir = "down"
    consumable = (objects | UP_WORDS | DOWN_WORDS | MAX_WORDS | MIN_WORDS | QUESTION_WORDS | SET_VERBS
                  | {"@vimplied", "by", "level", "to", STEP_SMALL, STEP_BIG})
    if kind == "brightness":
        consumable = consumable | SCREEN_WORDS
    rest = [it for it in items if it[0] not in consumable and id(it) not in num_ids]
    used = [it for it in items if it[0] in consumable and it[0] not in (STEP_SMALL, STEP_BIG)]
    span = (min(s[0] for _, s in used), max(s[1] for _, s in used)) if used else (0, 0)
    steps = [it for it in items if it[0] in (STEP_SMALL, STEP_BIG)]
    if ws & MAX_WORDS:
        head = [(kind, span), ("max", span)]
    elif ws & MIN_WORDS:
        head = [(kind, span), ("min", span)]
    elif nums and not ("by" in ws and last_dir):
        head = [("set", span), (kind, span)] + nums
    elif nums and last_dir:
        head = [(kind, span), (last_dir, span), ("by", span)] + nums
    elif last_dir:
        head = [(kind, span), (last_dir, span)]
    elif ws & QUESTION_WORDS:
        head = [(kind, span), ("level", span)]
    else:
        return None
    return head + rest + steps


def _concepts(items: list[Item]) -> list[Item]:
    toks = [t for t, _ in items]
    ws = set(toks)
    if not items or ws & {"timer", "timers"}:
        return items                                           # timers keep their own wording
    sound, bright, screen, media = ws & SOUND_WORDS, ws & BRIGHT_WORDS, ws & SCREEN_WORDS, ws & MEDIA_OBJECTS

    # 1. power & screen: the OBJECT decides; an unknown object never powers anything off
    is_off = (("off" in ws or "shutdown" in ws or {"shut", "down"} <= ws or {"power", "down"} <= ws)
              and not ws & _NOT_OFF_CONTEXT and "@mute" not in ws)
    if is_off:
        if screen:
            return _emit(items, ["screen", "off"], lambda t: t in OFF_VERBS | SCREEN_WORDS)
        if ws & {"music", "song", "video", "youtube"}:
            return _emit(items, ["pause"], lambda t: t in OFF_VERBS | MEDIA_OBJECTS)
        if sound:
            return _emit(items, ["@mute"], lambda t: t in OFF_VERBS | SOUND_WORDS)
        leftover = {t for t in ws if not _opaque(t)} - OFF_VERBS - PC_WORDS
        if not leftover:
            return _emit(items, ["shut", "down"], lambda t: t in OFF_VERBS | PC_WORDS)
        return items                                           # "turn off the lights": no meaning here
    if "on" in ws and ws & {"turn", "switch"}:
        if screen:
            return _emit(items, ["screen", "on"], lambda t: t in {"turn", "switch", "on", "back"} | SCREEN_WORDS)
        if sound and not media:
            return _emit(items, ["@unmute"], lambda t: t in {"turn", "switch", "on", "back"} | SOUND_WORDS)
        return items
    if ws & {"restart", "reboot"}:
        if not ({t for t in ws if not _opaque(t)} - {"restart", "reboot"} - PC_WORDS):
            return _emit(items, ["restart"], lambda t: t in {"restart", "reboot"} | PC_WORDS)
        return items
    if ws & {"sleep", "hibernate"}:
        if screen:
            return _emit(items, ["screen", "off"], lambda t: t in {"sleep", "put"} | SCREEN_WORDS)
        verb = "hibernate" if "hibernate" in ws else "sleep"
        if not (ws - {"sleep", "hibernate", "mode", "put"} - PC_WORDS):
            return _emit(items, [verb], lambda t: t in {"sleep", "hibernate", "mode", "put"} | PC_WORDS)
        return items
    if "lock" in ws:
        if not (ws - {"lock", "up"} - PC_WORDS - SCREEN_WORDS):
            return _emit(items, ["lock"], lambda t: t in {"lock", "up"} | PC_WORDS | SCREEN_WORDS)
        return items
    if "wake" in ws and not (ws - {"wake", "up"} - SCREEN_WORDS - PC_WORDS):
        return _emit(items, ["wake", "up"], lambda t: t in {"wake", "up"} | SCREEN_WORDS | PC_WORDS)
    if "@unmute" in ws:
        return _emit(items, ["@unmute"], lambda t: t in SOUND_WORDS | {"@unmute"})
    if "@mute" in ws:
        return _emit(items, ["@mute"], lambda t: t in SOUND_WORDS | {"@mute"})

    # 2. screenshot (before brightness: "capture the screen" mentions the screen too)
    shot_verbs = {"take", "capture", "grab", "snap", "picture", "photo", "shot", "image", "screenshot"}
    if "screenshot" in ws or (screen and ws & (shot_verbs - {"take"})):
        return _emit(items, ["screenshot"], lambda t: t in shot_verbs | SCREEN_WORDS)

    # 3. brightness
    if bright or (screen and ws & (UP_WORDS | DOWN_WORDS | MAX_WORDS | MIN_WORDS)):
        hit = _level("brightness", items, BRIGHT_WORDS)
        if hit is not None:
            return hit

    # 4. media controls
    if ws & {"pause", "resume"}:
        verb = "pause" if "pause" in ws else "resume"
        return _emit(items, [verb], lambda t: t in MEDIA_OBJECTS | {"pause", "resume"})
    if "stop" in ws and media:
        return _emit(items, ["stop", "music"], lambda t: t in MEDIA_OBJECTS | {"stop"})
    if ws & {"next", "skip"} and media and not ws & _CAL_WORDS:
        return _emit(items, ["next"], lambda t: t in MEDIA_OBJECTS | {"next", "skip", "play", "this"})
    if ("previous" in ws or ("back" in ws and media) or ("last" in ws and ws & {"song", "track", "video"})) \
            and not ws & _CAL_WORDS:
        return _emit(items, ["previous"], lambda t: t in MEDIA_OBJECTS | {"previous", "back", "last", "play"})

    # 5. volume
    if sound or ws & {"@vimplied"}:
        hit = _level("volume", items, SOUND_WORDS)
        if hit is not None:
            return hit

    # 6. time / date
    if "time" in ws and not ({t for t in ws if not _opaque(t)} - _TIME_OK):
        return _emit(items, ["time"], lambda t: t in _TIME_OK)
    if ("date" in ws or ("day" in ws and ws & {"what", "what's", "which"})
            or ("today" in ws and ws & {"what", "what's"})) and not (ws - _DATE_OK):
        return _emit(items, ["date"], lambda t: t in _DATE_OK)

    # 7. apps: any launch verb means open, any quit verb means close
    out = []
    for i, (t, span) in enumerate(items):
        if t in OPEN_VERBS:
            out.append(("open", span))
        elif t in CLOSE_VERBS:
            out.append(("close", span))
        elif t in APP_NOISE:
            continue
        else:
            out.append((t, span))
    return out


def canonical_items(tokens: list[str]) -> list[Item]:
    """Canonical meaning tokens with provenance: [(token, (start, end))], indices into `tokens`."""
    items = [(t, (i, i + 1)) for i, t in enumerate(tokens) if t != "[unk]"]
    items = [it for it in items if it[0] not in ARTICLES]
    items = _rewrite(items)
    items = _drop_fillers(items)
    return _concepts(items)


def canonical(text: str) -> str:
    return " ".join(t for t, _ in canonical_items(text.split()))


def step_modifier(tokens: list[str]) -> str | None:
    """'small' for a little / a bit / slightly, 'large' for a lot (volume/brightness step size)."""
    toks = [t for t in tokens if t != "[unk]"]
    for t, _ in _rewrite([(t, (i, i + 1)) for i, t in enumerate(toks) if t not in ARTICLES]):
        if t == STEP_SMALL:
            return "small"
        if t == STEP_BIG:
            return "large"
    return None


def strip_wake(text: str, wake_words: Iterable[str] | None = None) -> tuple[bool, str]:
    """(had_wake, rest). v1 semantics: the first wake token anywhere; rest = the words after it
    (a leading hey/hi/okay before the wake is dropped with everything else before it)."""
    wakes = set(wake_words or WAKE_ALIASES) | {TYPED_WAKE}
    words = text.split()
    for i, w in enumerate(words):
        if w in wakes:
            return True, " ".join(words[i + 1:])
    return False, text


def has_wake(text: str, wake_words: Iterable[str] | None = None) -> bool:
    wakes = set(wake_words or WAKE_ALIASES) | {TYPED_WAKE}
    return any(w in wakes for w in text.split())


def clean_song_title(text: str) -> str:
    """'the song shape of you on youtube please' -> 'shape of you' (v1 arc.py, verified).
    SPEC-AMBIGUITY: §3.6 also lists the prefixes 'song ', 'me ', 'some '; v1 deliberately keeps 'some'/'me'
    because titles start with them ("Some Nights"). v1 code wins; 'me <ask word>' is handled by the song slot."""
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


def words_after_play(words: list[str]) -> list[str]:
    """v1: the words after the (possibly misheard) 'play' near the start of a free-text utterance."""
    for i, w in enumerate(words[:3]):
        if w in PLAY_LIKE:
            return words[i + 1:]
    for i, w in enumerate(words[:2]):
        if w in WAKE_LIKE:
            return words[i + 1:]
    return words


# ============================================================================= bare "<title> by <artist>"
# Hearing rebuild (Sep 15 2026): after "Yes, sir?" the user often just says the song ("Shape of You by Ed
# Sheeran.") - no "play". Nothing in the registry starts a command with a title, so a bare "<title> by
# <artist>" is a song request; the engine confirms it ("Play shape of you by ed sheeran?") unless a song was
# played in the last 90 s (then it is a correction and plays at once).
# First words that make an utterance a command, never a title (every registered pattern's first literal
# word plus the meaning-layer verbs); a title may still contain them later ("stand by me" is fine).
COMMAND_VERBS = frozenset({
    "play", "open", "close", "launch", "start", "run", "load", "quit", "exit", "kill", "force", "switch", "go",
    "bring", "set", "turn", "raise", "lower", "increase", "decrease", "reduce", "boost", "drop", "volume", "mute",
    "unmute", "un", "sound", "silence", "brightness", "dim", "dimmer", "brighter", "screen", "display", "monitor",
    "lock", "sleep", "hibernate", "shut", "shutdown", "restart", "reboot", "power", "sign", "log", "wake", "keep",
    "let", "stay", "take", "capture", "grab", "snap", "screenshot", "print", "save", "search", "google", "look",
    "youtube", "type", "copy", "paste", "cut", "undo", "redo", "select", "press", "hit", "minimize", "minimise",
    "maximize", "maximise", "restore", "normal", "show", "hide", "new", "next", "previous", "skip", "pause",
    "resume", "continue", "stop", "quiet", "what", "what's", "whats", "how", "who", "when", "where", "which", "tell",
    "say", "repeat", "read", "clear", "timer", "cancel", "abort", "remind", "remember", "forget", "add", "delete",
    "remove", "note", "calculate", "roll", "flip", "toss", "pick", "heads", "help", "list", "thanks", "thank",
    "hello", "hi", "hey", "good", "weather", "will", "battery", "disk", "system", "status", "check", "full", "max",
    "maximum", "dark", "light", "night", "white", "windows", "follow", "match", "same", "use", "make", "change",
    "put", "adjust", "be", "are", "is", "do", "does", "it's", "tick", "mark", "done", "complete", "finish",
    "move", "reschedule", "edit", "rename", "brief", "briefing", "going", "more", "another", "different", "wrong",
    "not", "no", "yes", "okay", "ok", "never", "nothing", "on", "off", "up", "down", "in", "out", "to", "the", "a",
    "an", "my", "your", "our", "this", "that", "these", "those", "and", "or", "but", "by", "for", "of", "with",
    "at", "from", "about", "please", "just", "also", "again", "now", "later", "today", "tomorrow", "tonight",
    "everything", "all", "both", "any", "some", "something", "anything",
})
_TITLE_MAX_LEFT, _TITLE_MAX_RIGHT = 6, 4


def bare_title(text: str) -> str | None:
    """'shape of you by ed sheeran' -> itself; None when it isn't a bare '<title> by <artist>' request:
    no ' by ', a command verb / filler / number as the first word ('volume up by ten', 'close by',
    'one by one'), an empty side ('by the way'), too many words, or anything numeric on the right ('raise
    the volume by five'). Every registered pattern with 'by' is a volume step (numeric right side)."""
    words = [w for w in (text or "").lower().split() if w != "[unk]"]
    if "by" not in words:
        return None
    i = words.index("by")
    left, right = words[:i], words[i + 1:]
    if not left or not right or len(left) > _TITLE_MAX_LEFT or len(right) > _TITLE_MAX_RIGHT:
        return None
    first = left[0]
    if first in COMMAND_VERBS or first in FILLER_WORDS or first in ARTICLES or _is_numberish(first):
        return None
    if first in WAKE_LIKE or first.startswith("@"):
        return None
    for w in right:
        if w == "percent" or (_is_numberish(w) and w not in ("a", "an")):
            return None
    if right[0] == "by":
        return None
    return " ".join(words)


_PHONETIC_PAIRS = (("ph", "f"), ("ck", "k"), ("wr", "r"), ("kn", "n"), ("wh", "w"), ("gh", ""), ("sh", "x"),
                   ("ch", "x"), ("th", "t"), ("qu", "k"), ("ce", "se"), ("ci", "si"), ("cy", "sy"))
_VOWELS = frozenset("aeiouy")


def phonetic_key(text: str) -> str:
    """Consonant skeleton of the whole phrase ('shot down' / 'shut down' -> 'xtdwn'; 'hi bernate' /
    'hibernate' -> 'hbrnt'; 'asleep' / 'sleep' -> 'slp'). Doubled consonants stay ('of' -> 'f', 'off' -> 'ff'):
    the fuzzy tiers never confirm a destructive command whose key differs, so 'power of' is not 'power off'."""
    t = "".join(ch for ch in (text or "").lower() if ch.isalpha())
    for a, b in _PHONETIC_PAIRS:
        t = t.replace(a, b)
    t = t.replace("c", "k").replace("z", "s").replace("v", "f")
    return "".join(ch for ch in t if ch not in _VOWELS)
