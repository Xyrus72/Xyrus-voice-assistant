"""YouTube lookup + "make sure it plays" (ported from v1 actions.py — the verified implementation, §3.6),
search URLs and the opt-in weather (F11). Never speaks (G10). Runs on T-exec.

Hearing rebuild (Sep 15 2026): find_song no longer trusts YouTube's top hit blindly. The faint-mic transcripts
("if shown you by icons" for Ishwar by Vikings, "tired by ellen hook" for Alan Walker) search for the wrong
thing and YouTube happily returns something. So the top results are scored against the words the user said
(overlap), and when they miss, a few alternate queries (lexicon-corrected, the n-best hypotheses, the Bengali
form, past accepted queries) are fetched concurrently under one short deadline; the first that hits wins."""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import difflib
import json
import logging
import math
import re
import threading
import time
import urllib.parse
import urllib.request

log = logging.getLogger("xyrus.actions.web")

# seams for tests (never patch time.sleep globally)
_sleep = time.sleep
_now = time.monotonic

# ------------------------------------------------------------------ youtube (v1 verbatim) ---- #
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")
MAX_SONG_SECS = 20 * 60   # skip hour-long mixes / live streams when a normal video exists


def youtube_search_url(query: str) -> str:
    return "https://www.youtube.com/results?" + urllib.parse.urlencode({"search_query": query})


def _secs(text):
    try:
        total = 0
        for part in text.split(":"):
            total = total * 60 + int(part)
        return total
    except (AttributeError, ValueError):
        return None


def youtube_search(query: str, timeout=6):
    """Top videos for a query as [(video_id, title, channel, seconds)]. No API key needed."""
    req = urllib.request.Request(youtube_search_url(query),
                                 headers={"User-Agent": _UA, "Accept-Language": "en-US,en;q=0.9"})
    html = urllib.request.urlopen(req, timeout=timeout).read().decode("utf8", "replace")
    results = []
    m = re.search(r'(?:var ytInitialData|window\["ytInitialData"\])\s*=\s*(\{.*?\});\s*</script>', html, re.S)
    if m:
        def walk(o):
            if isinstance(o, dict):
                v = o.get("videoRenderer")
                if v and v.get("videoId"):
                    title = "".join(r.get("text", "") for r in v.get("title", {}).get("runs", []))
                    channel = "".join(r.get("text", "") for r in v.get("ownerText", {}).get("runs", []))
                    results.append((v["videoId"], title, channel,
                                    _secs(v.get("lengthText", {}).get("simpleText"))))
                for x in o.values():
                    walk(x)
            elif isinstance(o, list):
                for x in o:
                    walk(x)
        walk(json.loads(m.group(1)))
    if not results:   # page layout changed: fall back to bare video ids
        ids = dict.fromkeys(re.findall(r'"videoId":"([\w-]{11})"', html))
        results = [(i, "", "", None) for i in ids]
    return results


def spoken_title(title: str) -> str:
    """'Alan Walker - Alone (Official Video)' -> 'Alone by Alan Walker'."""
    t = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", "", title)
    t = re.split(r"\s+\|\s+", t)[0].strip(" -–—")
    m = re.match(r"^(.+?)\s+[-–—]\s+(.+)$", t)
    if m:
        t = f"{m.group(2)} by {m.group(1)}"
    words = t.split()
    return " ".join(words[:10]) if words else title


def _song_results(query: str):
    """YouTube results for a song request. A Bangla title (Bengali script, heard by Whisper in Bengali) is
    searched as written first, then as Banglish ("amar bhindeshi tara") when that finds nothing."""
    from xyrus import banglish
    results = youtube_search(query)
    if not results and banglish.has_bengali(query):
        results = youtube_search(banglish.transliterate(query))
    return results


def _spoken(title: str, query: str) -> str:
    """What SAPI says for a found video: its spoken_title; for a Bengali title its Latin part, else the Banglish
    (SAPI can't read Bengali script)."""
    from xyrus import banglish
    if not title:
        return banglish.speakable(query)
    if banglish.has_bengali(title):
        latin = banglish.latin_part(title)
        return spoken_title(latin) if latin else spoken_title(banglish.transliterate(title))
    return spoken_title(title)


# ------------------------------------------------------------------ song pick (hearing rebuild) ---- #
# The words that carry no identity: a query's content words are what must show up in the video's title or
# channel. "official"/"video"/"lyrics" are in every title, so they must not count as agreement either.
STOP_WORDS = frozenset({"by", "the", "a", "of", "song", "play", "official", "video", "lyrics"})
WORD_SIM = 0.8               # per-word difflib ratio that counts as the same word ("ishbar" ~ "ishwar")
TOP_N = 3                    # how deep into the results the overlap check looks
ALT_DEADLINE_S = 2.5         # the song path's budget for ALL alternate fetches together (concurrent)
MAX_ALTERNATES = 3           # ... and how many are fetched (<= 4 GETs per request in total)
HISTORY_MAX = 50             # data/play_history.json keeps the last accepted queries
_PLAY_LIKE = frozenset({"play", "plays", "played", "playing", "lay", "clay", "pray"})
_WAKE_LIKE = frozenset({"xyrus", "cyrus", "zeros", "virus", "cirrus", "sirius", "serious", "zero", "zira"})


def _tokens(text: str) -> list[str]:
    """Lowercase word tokens; Bengali tokens are compared in their Banglish form (both sides), so a Bengali
    title and a Latin query can agree."""
    from xyrus import banglish
    out: list[str] = []
    for tok in (text or "").split():
        if banglish.has_bengali(tok):
            tok = banglish.transliterate(tok)
        for part in re.split(r"[^a-z0-9]+", tok.lower()):
            if part:
                out.append(part)
    return out


def _content_words(query: str) -> list[str]:
    return [w for w in _tokens(query) if w not in STOP_WORDS]


def _word_hit(word: str, pool: set[str], phonetic: bool = False) -> bool:
    """Same word: exact or difflib >= 0.8. phonetic (only for words the query wrote in Bengali script): also the
    same sound-alike key of 3+ letters - the Banglish of a Bengali decode ('isshor') and YouTube's spelling
    ('ishwar') are only 0.67 apart by difflib but sound the same. Never for Latin words: 'ellen' and 'alan' share
    a key, and 'tired by ellen hook' must stay a miss against Alan Walker."""
    if word in pool:
        return True
    if any(difflib.SequenceMatcher(None, word, p).ratio() >= WORD_SIM for p in pool):
        return True
    if not phonetic:
        return False
    key = _sound_key(word)
    return len(key) >= 3 and any(_sound_key(p) == key for p in pool)


def _bengali_words(query: str) -> set[str]:
    """The query's words that were written in Bengali script, in their Banglish form."""
    from xyrus import banglish
    return {w for tok in (query or "").split() if banglish.has_bengali(tok) for w in _tokens(tok)}


def _needed(n: int) -> int:
    """Content words that must agree for a hit: half of them rounded up (1/1, 1/2, 2/3, 2/4, 3/5)."""
    return math.ceil(0.5 * n) if n >= 2 else 1


def overlap(query: str, title: str, channel: str = "") -> float:
    """Share (0..1) of the query's content words found in the video's title + channel (per-word difflib >= 0.8,
    Bengali compared as Banglish). A miss is always < 0.5 and a hit always >= 0.5, see is_hit."""
    words = _content_words(query)
    if not words:
        return 0.0
    pool = set(_tokens(title) + _tokens(channel))
    bn = _bengali_words(query)
    hits = sum(1 for w in words if _word_hit(w, pool, phonetic=w in bn))
    return hits / len(words)


def is_hit(query: str, title: str, channel: str = "") -> bool:
    """The video is what the user asked for: at least ceil(n/2) of n content words agree (1 of 1)."""
    words = _content_words(query)
    if not words:
        return False
    return round(overlap(query, title, channel) * len(words)) >= _needed(len(words))


class SongPick(tuple):
    """find_song's answer. Attributes vid, title (video title), channel, length (s or None), score (overlap,
    < 0.5 = a guess), query_used (the query that found it), spoken, url. It is also the old
    (spoken, url, video title) tuple, so `spoken, url, title = find_song(q)` and res[1] keep working."""

    def __new__(cls, vid: str, title: str, channel: str, length, score: float, query_used: str,
                spoken: str | None = None):
        url = f"https://www.youtube.com/watch?v={vid}"
        if spoken is None:
            spoken = _spoken(title, query_used)
        self = super().__new__(cls, (spoken, url, title))
        self.vid, self.title, self.channel, self.length = vid, title, channel, length
        self.score, self.query_used, self.spoken, self.url = float(score), query_used, spoken, url
        return self

    def __getnewargs__(self):        # copy.deepcopy / pickle rebuild through __new__ (FakeActions deepcopies)
        return (self.vid, self.title, self.channel, self.length, self.score, self.query_used, self.spoken)

    def __repr__(self):
        return (f"SongPick(vid={self.vid!r}, title={self.title!r}, channel={self.channel!r}, "
                f"score={self.score:.2f}, query_used={self.query_used!r})")


def _ordered(rows):
    """Normal-length videos first (MAX_SONG_SECS), the rest after - the old find_song / find_songs order."""
    normal = [r for r in rows if r[3] and r[3] <= MAX_SONG_SECS]
    return normal + [r for r in rows if r not in normal]


def _best(rows, query: str):
    """(row, score, hit) - the first of the top TOP_N ordered rows that agrees with the query, else the top row
    with its (low) score. None when there are no rows."""
    ordered = _ordered(rows)
    if not ordered:
        return None
    top = ordered[:TOP_N]
    for row in top:
        if is_hit(query, row[1] or "", row[2] or ""):
            return row, overlap(query, row[1] or "", row[2] or ""), True
    row = top[0]
    return row, overlap(query, row[1] or "", row[2] or ""), False


# ---- lexicon --------------------------------------------------------------------------------------------
_LEXICON_FILE = "music_lexicon.txt"
_lexicon_cache: tuple[list[tuple[str, ...]], set[str]] | None = None
_lexicon_lock = threading.Lock()


def _lexicon_paths():
    from xyrus import paths
    return [paths.BASE / "data" / _LEXICON_FILE, paths.data_dir() / _LEXICON_FILE]


def load_lexicon(force: bool = False):
    """([entry words tuples], {single words}) from data/music_lexicon.txt (+ the data dir's copy, if any).
    Missing / unreadable files are simply empty. Read once per process."""
    global _lexicon_cache
    with _lexicon_lock:
        if _lexicon_cache is not None and not force:
            return _lexicon_cache
        entries: list[tuple[str, ...]] = []
        seen: set[tuple[str, ...]] = set()
        for p in _lexicon_paths():
            try:
                text = p.read_text("utf-8")
            except OSError:
                continue
            for line in text.splitlines():
                line = line.split("#", 1)[0].strip()
                words = tuple(_tokens(line))
                if words and words not in seen:
                    seen.add(words)
                    entries.append(words)
        _lexicon_cache = (entries, {w for e in entries for w in e})
        return _lexicon_cache


def _sound_key(word: str) -> str:
    """Rough sound-alike key for a Latin word: doubles collapsed, vowels dropped (a leading one kept as 'a'),
    the glides h/w dropped - "ellen" and "alan" become "aln". Whisper's errors are phonetic, difflib's are not."""
    w = re.sub(r"[^a-z]", "", word.lower())
    w = re.sub(r"(.)\1+", r"\1", w)
    w = w.replace("ph", "f").replace("ck", "k").replace("wh", "w")
    out = []
    for i, c in enumerate(w):
        if c in "aeiouy":
            if i == 0:
                out.append("a")
        elif c not in "hw":
            out.append(c)
    return "".join(out)


def _ratio(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()


def _ngram_match(words: list[str], entry: tuple[str, ...]) -> bool:
    """A run of query words is a multi-word lexicon entry: difflib >= 0.8 on the plain text, or on the
    sound-alike keys ("ellen hook" ~ "alan walker" = 0.83) when both keys are long enough to mean something."""
    a, b = " ".join(words), " ".join(entry)
    if _ratio(a, b) >= WORD_SIM:
        return True
    ka, kb = " ".join(_sound_key(w) for w in words), " ".join(_sound_key(w) for w in entry)
    return len(ka) >= 5 and len(kb) >= 5 and _ratio(ka, kb) >= WORD_SIM


def lexicon_correct(query: str) -> str:
    """The query with misheard words snapped to the music lexicon: multi-word entries first (as n-grams of the
    query, longest first), then each word of 4+ letters against the single lexicon words at difflib >= 0.8.
    'tired by ellen hook' -> 'tired by alan walker'; 'shape of you by ed sheeran' is already right."""
    entries, single = load_lexicon()
    words = _tokens(query)
    if not words:
        return query
    multi = sorted((e for e in entries if len(e) > 1), key=lambda e: -len(e))
    out: list[str] = []
    i = 0
    while i < len(words):
        hit = None
        for e in multi:
            n = len(e)
            if i + n <= len(words) and _ngram_match(words[i:i + n], e):
                hit = e
                break
        if hit is not None:
            out.extend(hit)
            i += len(hit)
            continue
        w = words[i]
        if len(w) >= 4 and w not in single:
            close = difflib.get_close_matches(w, single, n=1, cutoff=WORD_SIM)
            if close:
                w = close[0]
        out.append(w)
        i += 1
    return " ".join(out)


# ---- play history -----------------------------------------------------------------------------------------
def _history_file():
    from xyrus import paths
    return paths.data_dir() / "play_history.json"


def _history_load() -> list[dict]:
    """[{"query", "title"}] newest last; a missing or corrupt file is an empty history, never an error."""
    try:
        data = json.loads(_history_file().read_text("utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [d for d in data if isinstance(d, dict) and isinstance(d.get("query"), str)]


def _history_add(query: str, title: str) -> None:
    hist = [h for h in _history_load() if h.get("query") != query]
    hist.append({"query": query, "title": title or ""})
    hist = hist[-HISTORY_MAX:]
    try:
        p = _history_file()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(hist, ensure_ascii=False, indent=0), "utf-8")
    except OSError as e:
        log.debug("play history not saved: %r", e)


def _history_variants(query: str) -> list[str]:
    """Past accepted queries that share a content word with this one, newest first ('vikings ishvar song' ->
    'ishwar by vikings' once that has played)."""
    mine = set(_content_words(query))
    if not mine:
        return []
    out = []
    for h in reversed(_history_load()):
        q = h.get("query") or ""
        if q != query and mine & set(_content_words(q)):
            out.append(q)
    return out


# ---- alternates -------------------------------------------------------------------------------------------
def _title_part(text: str) -> str:
    """'Xyrus, play Ishwar by Vikings.' -> 'ishwar by vikings' (an n-best hypothesis is a whole utterance)."""
    from xyrus import banglish
    t = re.sub(r"[?!,;:\"()\.]", " ", (text or "").lower())
    words = t.split()
    for i, w in enumerate(words[:5]):
        if w.strip("'") in _PLAY_LIKE:
            words = words[i + 1:]
            break
    else:
        if words and words[0] in _WAKE_LIKE:
            words = words[1:]
    for suffix in (("on", "youtube"), ("please",), ("now",), ("sir",)):
        if len(words) > len(suffix) and tuple(words[-len(suffix):]) == suffix:
            words = words[:-len(suffix)]
    return " ".join(w for w in words if banglish.has_bengali(w) or re.search(r"[a-z0-9]", w))


def _artist_and_title(query: str) -> tuple[str, str]:
    """('ishwar', 'vikings') from 'ishwar by vikings'; artist '' without a 'by'."""
    words = query.split()
    if "by" in words[1:]:
        i = words.index("by", 1)
        return " ".join(words[:i]), " ".join(words[i + 1:])
    return query, ""


def alternate_queries(query: str, alternates=(), bn_text: str | None = None) -> list[str]:
    """The queries tried when the primary results miss, in order: lexicon-corrected; the hearing n-best
    hypotheses (title part); '<artist> <first title word>'; the Bengali form + the Latin artist word; its
    Banglish; past accepted queries sharing a word. Deduplicated; never the query itself."""
    from xyrus import banglish
    out: list[str] = []
    seen = {" ".join(_tokens(query))}         # same words = same search: no GET for a case/punctuation variant

    def add(q):
        q = " ".join((q or "").split())
        key = " ".join(_tokens(q))
        if q and key and key not in seen:
            seen.add(key)
            out.append(q)

    add(lexicon_correct(query))
    for alt in list(alternates or ())[:3]:
        add(_title_part(str(alt)))
    title, artist = _artist_and_title(query)
    if artist and title.split():
        add(f"{artist} {title.split()[0]}")
    if bn_text and banglish.has_bengali(bn_text):
        latin_artist = " ".join(w for w in artist.split() if not banglish.has_bengali(w))
        add(f"{bn_text} {latin_artist}".strip())
        add(banglish.transliterate(bn_text))
    for q in _history_variants(query):
        add(q)
    return out


def _fetch_alternates(queries: list[str], deadline_s: float = ALT_DEADLINE_S) -> list[tuple[str, list | None]]:
    """GET every query on its own thread, all under ONE deadline; [(query, rows | None)] in the given order
    (None = not back in time or failed). Threads that overrun are left to finish on their own (daemon)."""
    deadline = _now() + deadline_s
    got: dict[str, list | None] = {}

    def work(q):
        try:
            got[q] = youtube_search(q, timeout=deadline_s)
        except Exception as e:                       # noqa: BLE001 - one bad alternate must not sink the rest
            log.debug("alternate %r failed: %r", q, e)
            got[q] = None

    threads = [threading.Thread(target=work, args=(q,), name="xyrus-song-alt", daemon=True) for q in queries]
    for t in threads:
        t.start()
    for t in threads:
        t.join(max(0.0, deadline - _now()))
    return [(q, got.get(q)) for q in queries]


def find_song(query: str, alternates=(), bn_text: str | None = None) -> SongPick:
    """The best YouTube match for a song request, as a SongPick (also the old (spoken, url, video title) tuple).
    The primary results are scored against the query (overlap); when the top TOP_N miss, up to MAX_ALTERNATES
    alternate queries are fetched concurrently under ALT_DEADLINE_S and the first that hits is accepted;
    else the original top result comes back with score < 0.5 (a guess - the command says so).
    `alternates` are the hearing pass's n-best hypotheses, `bn_text` the Bengali decode (both optional).
    Raises LookupError when nothing at all is found."""
    from xyrus import banglish
    # the primary lookup (_song_results inlined so the GETs are counted: <= 1 + MAX_ALTERNATES per request)
    results = youtube_search(query)
    fetched = {" ".join(_tokens(query))}
    if not results and banglish.has_bengali(query):
        roman = banglish.transliterate(query)
        results = youtube_search(roman)
        fetched.add(" ".join(_tokens(roman)))
    budget = 1 + MAX_ALTERNATES - len(fetched)
    best = _best(results, query)
    accepted = None
    if best is not None and best[2]:
        accepted = (best[0], best[1], query)
    else:
        fallback = (best[0], best[1], query) if best is not None else None
        todo = [q for q in alternate_queries(query, alternates, bn_text)
                if " ".join(_tokens(q)) not in fetched][:budget]
        for alt, rows in _fetch_alternates(todo, ALT_DEADLINE_S):       # read at call time (tests shrink it)
            if not rows:
                continue
            b = _best(rows, alt)
            if b is not None and b[2]:
                accepted = (b[0], b[1], alt)
                break
            if fallback is None:
                fallback = (b[0], b[1], alt)
        if accepted is None:
            if fallback is None:
                raise LookupError(query)
            (vid, title, channel, length), score, used = fallback
            if not title:
                title = video_title(f"https://www.youtube.com/watch?v={vid}") or ""
            log.info("song: q=%r score=%.2f", used, score)
            return SongPick(vid, title, channel, length, score, used, _spoken(title, query))
    (vid, title, channel, length), score, used = accepted
    log.info("song: q=%r score=%.2f", used, score)
    _history_add(used, title)
    return SongPick(vid, title, channel, length, score, used, _spoken(title, query))


def find_songs(query: str, limit: int = 6):
    """Ranked [(spoken title, watch URL, video title)] for a song request - normal-length videos first.
    Used by 'not that one' to move on to the next result (v1 actions.find_songs)."""
    results = _song_results(query)
    if not results:
        raise LookupError(query)
    normal = [r for r in results if r[3] and r[3] <= MAX_SONG_SECS]
    ordered = normal + [r for r in results if r not in normal]
    out = []
    for vid, title, _channel, _length in ordered[:limit]:
        url = f"https://www.youtube.com/watch?v={vid}"
        out.append((_spoken(title, query), url, title))
    return out


def video_title(url: str, timeout=4):
    """A video's title via YouTube's oEmbed endpoint (used when the results page layout isn't recognised)."""
    try:
        req = urllib.request.Request(
            "https://www.youtube.com/oembed?" + urllib.parse.urlencode({"url": url, "format": "json"}),
            headers={"User-Agent": _UA})
        return json.loads(urllib.request.urlopen(req, timeout=timeout).read().decode("utf8")).get("title")
    except Exception:
        return None


# ---------------------------------------------------- make sure it plays (v1) ---- #
# Browsers block autoplay-with-sound for pages opened by another program, so a YouTube page often loads
# paused. Watch the song's own browser audio meter and, only if it stays silent, focus THAT video's window
# and press play (YouTube's "k" key, then one click on the player). Safety: only the song's browser process
# counts; only a window whose title matches the video is ever touched; nothing is pressed unless that window
# is in the foreground right before the key/click.
BROWSERS = {"msedge.exe", "chrome.exe", "firefox.exe", "brave.exe", "opera.exe", "vivaldi.exe"}
VK_K = 0x4B
MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
PEAK_SOUND = 0.002
AUDIO_SESSION_ACTIVE = 1

_u32 = ctypes.WinDLL("user32", use_last_error=True)   # private copy: argtypes here don't leak into other code
_WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
_u32.EnumWindows.argtypes = [_WNDENUMPROC, wt.LPARAM]
_u32.GetForegroundWindow.restype = wt.HWND
_u32.IsWindowVisible.argtypes = [wt.HWND]
_u32.GetWindowTextLengthW.argtypes = [wt.HWND]
_u32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
_u32.GetClientRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
_u32.ClientToScreen.argtypes = [wt.HWND, ctypes.POINTER(wt.POINT)]
_u32.GetCursorPos.argtypes = [ctypes.POINTER(wt.POINT)]
_u32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
_u32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]


def _browser_audio(trust_state: bool = True, exe: str | None = None) -> bool:
    """Is a web browser (only `exe`, e.g. 'msedge.exe', if given) sending sound out right now?
    trust_state=False ignores 'stream open' and only believes the meter (used right after we paused other
    media, whose stream may linger)."""
    from xyrus.actions import audio
    audio._com()
    from pycaw.pycaw import AudioUtilities, IAudioMeterInformation
    for s in AudioUtilities.GetAllSessions():
        try:
            name = s.Process.name().lower() if s.Process else ""
            if name not in BROWSERS or (exe and name != exe):
                continue
            if trust_state and s.State == AUDIO_SESSION_ACTIVE:
                return True
            if s._ctl.QueryInterface(IAudioMeterInformation).GetPeakValue() > PEAK_SOUND:
                return True
        except Exception:
            continue
    return False


def browser_playing() -> bool:
    return _browser_audio(trust_state=True)


def media_sounding(seconds: float = 0.8) -> bool:
    """Sound actually coming out of a browser within `seconds` (the meter, not the session state: Chrome keeps a
    paused tab's session Active for a while). Pause / resume decide on this (commands/sound.py)."""
    return _audio_within(seconds, trust_state=False)


def _audio_within(seconds: float, trust_state: bool = True, exe: str | None = None) -> bool:
    end = _now() + seconds
    while True:
        if _browser_audio(trust_state, exe):
            return True
        if _now() >= end:
            return False
        _sleep(0.2)


def _top_windows() -> list[tuple[int, str]]:
    out = []

    def cb(h, _):
        if _u32.IsWindowVisible(h):
            n = _u32.GetWindowTextLengthW(h)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                _u32.GetWindowTextW(h, buf, n + 1)
                out.append((h, buf.value))
        return True

    _u32.EnumWindows(_WNDENUMPROC(cb), 0)
    return out


def _window_exe(hwnd) -> str | None:
    """'msedge.exe' / 'chrome.exe' ... for the process that owns a window, or None."""
    pid = wt.DWORD()
    _u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    try:
        import psutil
        return psutil.Process(pid.value).name().lower()
    except Exception:
        return None


def _is_foreground(hwnd) -> bool:
    return _u32.GetForegroundWindow() == hwnd


def _title_key(text) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def _wait_youtube_window(video_title, timeout):
    """The browser window showing THIS video (its title contains the video's title), or None.
    Never guesses: pressing play in some other YouTube window could pause the user's own video."""
    key = _title_key(video_title)[:20].strip()
    if not key:
        return None
    end = _now() + timeout
    while _now() < end:
        for h, t in _top_windows():
            tk = _title_key(t)
            if "youtube" in tk and key in tk:
                return h
        _sleep(0.3)
    return None


def _focus(hwnd) -> None:
    from xyrus import winutil
    winutil.force_foreground(hwnd)       # tries SetForegroundWindow before any ALT tap
    _sleep(0.2)


def _press_play_key(hwnd) -> None:
    from xyrus.actions import keys
    keys.tap(VK_K)                       # YouTube: k = play / pause


def _click_player(hwnd) -> None:
    """One click on the video player (upper-left of the page in every YouTube layout), mouse put back."""
    r = wt.RECT()
    _u32.GetClientRect(hwnd, ctypes.byref(r))
    pt = wt.POINT(0, 0)
    _u32.ClientToScreen(hwnd, ctypes.byref(pt))
    x = pt.x + int((r.right - r.left) * 0.30)
    y = pt.y + int((r.bottom - r.top) * 0.40)
    old = wt.POINT()
    _u32.GetCursorPos(ctypes.byref(old))
    _u32.SetCursorPos(x, y)
    _sleep(0.05)
    try:
        if _is_foreground(hwnd):         # re-checked right before the click
            _u32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
            _u32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
            _sleep(0.05)
    finally:
        _u32.SetCursorPos(old.x, old.y)


def ensure_playing(video_title, trust_state=True, load_timeout=12.0) -> str:
    """Make sure the YouTube video we just opened is actually playing. Never presses anything if sound is
    already coming out. Returns 'autoplay', 'started', 'blocked' or 'no-window'."""
    hwnd = _wait_youtube_window(video_title, load_timeout)
    if hwnd is None:
        return "no-window"                              # can't tell which window is ours: touch nothing
    exe = _window_exe(hwnd)
    exe = exe if exe in BROWSERS else None              # only the song's own browser counts
    _sleep(1.5)                                         # let the player initialise
    # real sound only (trust_state is ignored): Chrome keeps a PAUSED tab's audio session "Active", and all tabs
    # share one session - the flag said "autoplay" for a new video that sat paused (user report, Sep 15 2026)
    if _audio_within(4.0, False, exe):
        return "autoplay"
    for press in (_press_play_key, _click_player):
        _focus(hwnd)
        _sleep(0.3)
        if not _is_foreground(hwnd):                    # focus was lost (the user clicked elsewhere):
            return "blocked"                            # never type or click into another window
        press(hwnd)
        if _audio_within(3.0, False, exe):
            return "started"
    return "blocked"


# ------------------------------------------------------------------ other URLs ---- #
def google_url(query: str) -> str:
    return "https://www.google.com/search?q=" + urllib.parse.quote_plus(query)


def spotify_search_uri(query: str) -> str:
    return "spotify:search:" + urllib.parse.quote(query)


# ------------------------------------------------------------------ weather (F11) ---- #
WEATHER_TIMEOUT = 8.0
WEATHER_TTL = 15 * 60
_wcache: dict[str, tuple[float, dict]] = {}
_wlock = threading.Lock()


def weather(city: str) -> dict:
    """{"city", "temp_c", "feels_c", "desc", "max_c", "min_c", "rain_chance"} from wttr.in (15-min cache).
    Raises OSError on network failure, LookupError on an unknown city."""
    key = city.strip().lower()
    if not key:
        raise LookupError("no city")
    now = time.monotonic()
    with _wlock:
        hit = _wcache.get(key)
        if hit and now - hit[0] < WEATHER_TTL:
            return dict(hit[1])
    url = "https://wttr.in/" + urllib.parse.quote(city.strip()) + "?format=j1"
    req = urllib.request.Request(url, headers={"User-Agent": "curl/8.0"})
    with urllib.request.urlopen(req, timeout=WEATHER_TIMEOUT) as r:
        raw = r.read()
    try:
        d = json.loads(raw)
        c = d["current_condition"][0]
        today = d["weather"][0]
        area = (d.get("nearest_area") or [{}])[0]
        name = (area.get("areaName") or [{"value": city}])[0]["value"]
        chances = [int(h.get("chanceofrain", 0)) for h in today.get("hourly", [])]
        out = {"city": name or city, "temp_c": int(c["temp_C"]), "feels_c": int(c["FeelsLikeC"]),
               "desc": c["weatherDesc"][0]["value"].strip(), "max_c": int(today["maxtempC"]),
               "min_c": int(today["mintempC"]), "rain_chance": max(chances) if chances else 0}
    except (KeyError, IndexError, ValueError, TypeError) as e:
        raise LookupError(f"weather for {city!r}: {e}") from e
    with _wlock:
        _wcache[key] = (now, out)
    return dict(out)
