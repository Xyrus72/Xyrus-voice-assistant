"""Play a song on YouTube (§3.6, port of v1 arc.py incl. the autoplay check), web / YouTube search, and
"type <text>" (§3.5).

Hearing rebuild (Sep 15 2026): play_song hands the hearing pass's n-best hypotheses (ctx.alternates) and the
Bengali decode (ctx.bn_text) to actions.find_song, which scores YouTube's results against what was said and
tries those alternates when the top results miss. A pick it is not sure about (score < 0.5) is announced with
PLAYING_MAYBE so "not that one" is an obvious next step; that path stays song_not_that -> next_song."""
from __future__ import annotations

import logging
import re
import threading
import time
import urllib.parse
import weakref
from dataclasses import dataclass, field
from typing import Any

from xyrus import normalize as N
from xyrus import replies as R
from xyrus.banglish import speakable
from xyrus.commands import say, text
from xyrus.registry import command

log = logging.getLogger("xyrus.commands.web")
SECTION = "Apps & web"
# youtube_search's own urlopen timeout is 6 s (§3.6) + the alternates' shared 2.5 s deadline + the oEmbed
# fallback (4 s); the executor's TOO_SLOW must not fire before a slow but honest lookup returns.
LOOKUP_TIMEOUT = 12.0
# "Playing X - say 'not that one' if it is wrong" for a pick find_song is not sure about (score < 0.5).
# replies.PLAYING_MAYBE is the lead's wording when it exists; this is the fallback (read at call time).
_PLAYING_MAYBE_DEFAULT = "Playing {title} - say 'not that one' if it is wrong{sir}."
UNSURE_SCORE = 0.5
# The autoplay check can take ~25 s (12 s window wait + 1.5 + 4 + 2 x 3.5), so it runs on its own thread
# (lead fix): T-exec is free as soon as the URL is opened and later commands never queue behind it.


def _thread_spawn(fn) -> None:
    """Run fn on its own daemon thread with COM initialised (the pycaw meter needs it)."""
    def run():
        try:
            import comtypes
            comtypes.CoInitialize()
        except Exception:
            pass
        try:
            fn()
        finally:
            try:
                comtypes.CoUninitialize()
            except Exception:
                pass
    threading.Thread(target=run, name="xyrus-autoplay", daemon=True).start()


_spawn = None   # None = own thread when the engine is threaded (the app), inline otherwise (headless tests)


def _run_check(ctx, fn) -> None:
    spawn = _spawn or (_thread_spawn if getattr(ctx.engine, "threaded", False) else (lambda f: f()))
    spawn(fn)
PAUSE_SETTLE_S = 0.8          # v1: after pausing other browser audio, let its stream wind down
AUTOPLAY_BLOCKED = "The browser blocked autoplay{sir}. Press play on the video."

# "Not that one" (lead, Sep 14; port of v1 _next_song / is_not_that / NOT_THAT / SONG_FOLLOWUP): after a song
# starts, these play the next YouTube result for the same request - without the wake word for 40 s, with it
# any time while a last song exists. Already-tried URLs are skipped.
SONG_FOLLOWUP_S = 40.0
NOT_THAT = ("not that one", "not this one", "not that song", "not this song", "wrong song", "wrong one",
            "that's not it", "that is not it", "another one", "[a] different one", "different song",
            "[the] next result", "try another", "not the right song")
TRYING = "Trying {title}{sir}."
SONG_NO_MORE = "That's all I found for {query}{sir}. Try saying the name again."
NO_SONG_YET = "I haven't played anything yet{sir}."
YOUTUBE_UNREACHABLE = "I couldn't reach YouTube{sir}."


@dataclass
class LastSong:
    query: str
    tried: list[str] = field(default_factory=list)   # watch URLs already opened for this request
    until: float = 0.0                                 # engine-clock mono deadline of the no-wake window
    search: str = ""                                   # the query that found it (an alternate, or the query)


_LAST: "weakref.WeakKeyDictionary[Any, LastSong]" = weakref.WeakKeyDictionary()   # engine -> last request


def last_song(engine) -> LastSong | None:
    return _LAST.get(engine)


def _song_window_open(engine) -> bool:
    """Context of the wake-free 'not that one': a song started less than 40 s ago."""
    st = _LAST.get(engine)
    if st is None:
        return False
    try:
        return engine.clock.mono() < st.until
    except Exception:
        return False


# ----------------------------------------------------------------------------- v1 helpers (arc.py)
def is_song_request(rest: str) -> bool:
    """'play alone' / 'play [unk]' is a song. Bare 'play' stays media play-pause (v1)."""
    words = rest.split()
    return len(words) >= 2 and words[0] == "play" and rest.strip() not in ("play pause", "play next", "play previous")


def song_title(text_: str, *, after_play: bool = True) -> str:
    """Title from a (free-decoded) utterance: words after the first play-like token (or a leading wake-like
    token), then clean_song_title (v1 _song_title)."""
    words = [w for w in N.normalize(text_).split()]
    if after_play:
        words = N.words_after_play(words)
    else:
        had, rest = N.strip_wake(" ".join(words), N.WAKE_LIKE)
        words = rest.split()
        if words and words[0] in N.PLAY_LIKE:
            words = words[1:]
    return N.clean_song_title(" ".join(words))


def youtube_search_url(query: str) -> str:
    """Same URL as actions.web.youtube_search_url (commands may not import actions, §4.14)."""
    return "https://www.youtube.com/results?" + urllib.parse.urlencode({"search_query": query})


def google_url(query: str) -> str:
    return "https://www.google.com/search?q=" + urllib.parse.quote_plus(query)


def spotify_search_uri(query: str) -> str:
    return "spotify:search:" + urllib.parse.quote(query)


# ----------------------------------------------------------------------------- playing
def _pause_other_audio(actions) -> bool:
    """T-exec job (v1 _play_song): if a browser is already making sound, press play/pause once so the new song
    doesn't play over it. Returns True when something was paused. Never raises."""
    # the sound meter, not the session flag: Chrome keeps a paused tab's session "Active", so the flag made this
    # press play/pause on an already PAUSED tab - resuming it, and the new song's autoplay check then heard that
    # old tab and never started the new video (Sep 15 2026)
    probe = getattr(actions, "media_sounding", None) or getattr(actions, "browser_playing", None)
    if probe is None:                        # SPEC-AMBIGUITY: not in interfaces.SystemActions yet (reported)
        return False
    try:
        if probe():
            actions.media("play_pause")
            time.sleep(PAUSE_SETTLE_S)       # sleeping is allowed on T-exec (§5.7 rule 5)
            return True
    except Exception as e:
        log.warning("audio check before playing failed: %r", e)
    return False


def _opened(ctx, video: str, paused_other: bool) -> None:
    """After the watch URL opened: the autoplay check, on its own thread (lead fix, see _run_check)."""
    ensure = getattr(ctx.actions, "ensure_playing", None)
    if ensure is None or not video:
        return

    def playback(result):
        ctx.event("info", f"playback: {result}")
        if result == "blocked":
            say(ctx, AUTOPLAY_BLOCKED)

    def check():            # up to ~25 s: never on T-exec, so other commands don't queue behind it
        try:
            result = ensure(video, trust_state=not paused_other)
        except Exception as e:
            log.warning("autoplay check failed: %r", e)
            return
        ctx.engine.post(lambda: playback(result))

    _run_check(ctx, check)


def _start_found(ctx, res, template: str = R.PLAYING) -> None:
    """A lookup result (spoken, url[, video title]) -> pause other browser audio -> "Playing …" / "Trying …" ->
    open -> autoplay check (v1 _open_song). Opens the 40 s "not that one" window when it is spoken."""
    a = ctx.actions
    spoken, url = speakable(res[0]), res[1]            # SAPI can't read Bengali script: Latin part / Banglish
    video = res[2] if len(res) > 2 else ""

    def start(paused_other):
        say(ctx, template, title=spoken)
        st = _LAST.get(ctx.engine)
        if st is not None:
            st.until = ctx.clock.mono() + SONG_FOLLOWUP_S
        ctx.do(a.open_target, url, then=lambda _: _opened(ctx, video, bool(paused_other)))
        ctx.event("info", url, meta=url)
    ctx.do(_pause_other_audio, a, then=start, error=lambda e: start(False))


# "play the song" / "play it" / "play the song that you paused" mean the song that is already open - they were
# searched on YouTube as titles ("that you paused" -> "You Were My Favorite Pause", Sep 15 2026)
_CURRENT_SONG = re.compile(
    r"^(?:(?:the|this|that|my|our)\s+)?(?:paused\s+)?(?:song|music|track|video|tune|it)(?:\s+again)?"
    r"(?:\s+(?:that|which)?\s*(?:you|i|we)?\s*(?:just\s+)?(?:paused|stopped|were playing|was playing))?$"
    r"|^(?:it\s+)?again$"
    # the slot cleaner strips a leading "the song": "play the song that you paused" arrives as "that you paused"
    r"|^(?:(?:that|which)\s+)?(?:you|i|we)\s+(?:just\s+)?(?:paused|stopped|were playing|was playing)$"
    r"|^(?:the\s+)?(?:just\s+)?paused(?:\s+(?:one|song|music|video|track))?$")


def is_current_song(title: str) -> bool:
    return bool(_CURRENT_SONG.match(" ".join(re.sub(r"[^\w\s']", " ", title.lower()).split())))


def play_song(ctx, title: str) -> None:
    """find_song -> pause other browser audio -> "Playing …" -> open -> ensure_playing (v1 order), every step
    on the executor through ctx.do; failures open the results page."""
    title = title.strip()
    if is_current_song(title):
        from xyrus.commands.sound import resume_playback
        resume_playback(ctx)
        return
    a = ctx.actions
    if not ctx.config.get("youtube_lookup", True):
        ctx.do(a.open_target, youtube_search_url(title),
               then=lambda _: say(ctx, R.YOUTUBE_PAGE, query=speakable(title)))
        return

    # engine-provided hints (set by the engine before dispatch; absent on the typed path and in older engines)
    alternates = tuple(getattr(ctx, "alternates", ()) or ())
    bn_text = getattr(ctx, "bn_text", None) or None

    def find_song():                          # T-exec job; named find_song so logs / InlineExecutor.jobs read as before
        if alternates or bn_text:
            try:
                return a.find_song(title, alternates=alternates, bn_text=bn_text)
            except TypeError as e:            # an actions layer that doesn't take the hints yet: plain lookup
                if "argument" not in str(e):
                    raise
                log.debug("find_song without hints: %s", e)
        return a.find_song(title)

    def found(res):
        used = getattr(res, "query_used", "") or title
        _LAST[ctx.engine] = LastSong(query=title, tried=[res[1]], search=used)
        score = getattr(res, "score", None)
        unsure = score is not None and score < UNSURE_SCORE
        if unsure:
            log.info("song pick unsure: q=%r used=%r score=%.2f", title, used, score)
        _start_found(ctx, res, getattr(R, "PLAYING_MAYBE", _PLAYING_MAYBE_DEFAULT) if unsure else R.PLAYING)

    def failed(e):
        log.warning("youtube lookup for %r failed: %r", title, e)
        say(ctx, R.SONG_NOT_FOUND if isinstance(e, LookupError) else R.YOUTUBE_OFFLINE, query=speakable(title))
        ctx.do(a.open_target, youtube_search_url(title))

    ctx.do(find_song, then=found, error=failed, timeout=LOOKUP_TIMEOUT)


@command("play_song", "play <song>", "play <song> on youtube", section="Sound & media",
         help="plays any song on YouTube: play alone, play shape of you")
def play_song_cmd(ctx, m):
    play_song(ctx, m.slots["song"])


def next_song(ctx) -> None:
    """'Not that one': the next ranked YouTube result for the last request that hasn't been opened yet."""
    st = _LAST.get(ctx.engine)
    if st is None:
        say(ctx, NO_SONG_YET)
        return
    st.until = 0.0                          # re-opened by _start_found when the next one starts
    find = getattr(ctx.actions, "find_songs", None)
    if find is None:
        say(ctx, YOUTUBE_UNREACHABLE)
        return

    def got(options):
        nxt = next((o for o in (options or []) if o[1] not in st.tried), None)
        if nxt is None:
            say(ctx, SONG_NO_MORE, query=speakable(st.query))
            return
        st.tried.append(nxt[1])
        _start_found(ctx, nxt, TRYING)

    def failed(e):
        log.warning("youtube lookup (next result) for %r failed: %r", st.query, e)
        say(ctx, YOUTUBE_UNREACHABLE)

    # the list continues from the query that found the song (a corrected alternate, when one was used)
    ctx.do(find, st.search or st.query, then=got, error=failed, timeout=LOOKUP_TIMEOUT)


@command("song_not_that", *NOT_THAT, section="Sound & media",
         help="plays the next YouTube result for your last song request",
         wake_free=True, context=_song_window_open,
         wake_free_note=" — works without the wake word for 40 s after a song starts")
def song_not_that(ctx, m):
    next_song(ctx)


@command("play_spotify", "play <song> on spotify", section="Sound & media", help="searches Spotify for it")
def play_spotify(ctx, m):
    q = m.slots["song"]
    if q.endswith(" on spotify"):
        q = q[: -len(" on spotify")].strip()
    ctx.do(ctx.actions.open_target, spotify_search_uri(q), then=lambda _: say(ctx, R.SPOTIFY_SEARCH, query=q))


# ----------------------------------------------------------------------------- ask flow (D4, §3.6 step 4)
def handle_song_answer(ctx, answer: str) -> str:
    """v1 _song_answer: 'cancel' (or no/stop/nothing/never mind/forget it) ends with 'Okay'; noise or an
    ask-word keeps waiting; anything else plays. Returns 'cancel' | 'wait' | 'play'."""
    rest = N.strip_wake(N.normalize(answer), N.WAKE_LIKE)[1].strip()
    if rest in N.SONG_CANCEL:
        say(ctx, R.OKAY)
        return "cancel"
    title = song_title(answer, after_play=False)
    if title in N.SONG_ASK or (len(title.split()) == 1 and title in N.NOISE_WORDS):
        return "wait"
    play_song(ctx, title)
    return "play"


def ask_song(ctx) -> None:
    """'What should I play, sir?' then a free-mode AskDialogue (slot "song": kind "play", cancel words
    SONG_CANCEL -> 'Okay{sir}.', SONG_ASK / single noise words ignored, window command_window_s, silent
    timeout — dialogue._ASK_DEFAULTS)."""
    def on_answer(title):
        words = str(title).split()
        if len(words) > 1 and words[0] in N.PLAY_LIKE:     # "play believer" answered -> "believer"
            words = words[1:]
        play_song(ctx, N.clean_song_title(" ".join(words)))
    ctx.ask(text(ctx, R.SONG_ASK), "song", on_answer, listen="free", hint="say a song name")


@command("play_ask", "play some music", "play music", "play a song", "play something", "play anything",
         "play a track", "play me something", "play me a song", section="Sound & media",
         help="asks what to play, then plays it")
def play_ask(ctx, m):
    ask_song(ctx)


# ----------------------------------------------------------------------------- search / type
@command("web_search", "search for <query>", "google <query>", "search the web for <query>",
         "look up <query>", "search google for <query>", section=SECTION, help="Google search in the browser")
def web_search(ctx, m):
    q = m.slots["query"]
    ctx.do(ctx.actions.open_target, google_url(q), then=lambda _: say(ctx, R.SEARCHING, query=q))


@command("youtube_search", "search youtube for <query>", "youtube <query>", "search on youtube for <query>",
         section=SECTION, help="YouTube search results page")
def youtube_search(ctx, m):
    q = m.slots["query"]
    ctx.do(ctx.actions.open_target, youtube_search_url(q), then=lambda _: say(ctx, R.YOUTUBE_PAGE, query=q))


@command("type_text", "type <text>", section="Keys & clipboard",
         help="types the words into the focused window")
def type_text(ctx, m):
    t = m.slots["text"]
    a = ctx.actions

    def job():
        if a.foreground_is_self():
            return False
        a.type_text(t)
        return True
    ctx.do(job, then=lambda ok: None if ok else say(ctx, R.TYPE_OWN_WINDOW))
