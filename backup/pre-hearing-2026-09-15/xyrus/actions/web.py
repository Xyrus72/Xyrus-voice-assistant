"""YouTube lookup + "make sure it plays" (ported from v1 actions.py — the verified implementation, §3.6),
search URLs and the opt-in weather (F11). Never speaks (G10). Runs on T-exec."""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import logging
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


def find_song(query: str):
    """(spoken title, watch URL, video title) of the best YouTube match. Raises LookupError if nothing is found."""
    results = _song_results(query)
    if not results:
        raise LookupError(query)
    vid, title, _channel, _length = next((r for r in results if r[3] and r[3] <= MAX_SONG_SECS), results[0])
    url = f"https://www.youtube.com/watch?v={vid}"
    if not title:
        title = video_title(url) or ""
    return _spoken(title, query), url, title


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
    if _audio_within(4.0, trust_state, exe):
        return "autoplay"
    for press in (_press_play_key, _click_player):
        _focus(hwnd)
        _sleep(0.3)
        if not _is_foreground(hwnd):                    # focus was lost (the user clicked elsewhere):
            return "blocked"                            # never type or click into another window
        press(hwnd)
        if _audio_within(3.0, True, exe):
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
