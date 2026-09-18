"""Everything Xyrus can *do* to Windows. No speech logic here."""
import ctypes
import ctypes.wintypes as wt
import datetime as dt
import json
import random
import re
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
powrprof = ctypes.windll.powrprof

NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW for subprocess

# ---------------------------------------------------------------- power ---- #
HWND_BROADCAST = 0xFFFF
WM_SYSCOMMAND = 0x0112
SC_MONITORPOWER = 0xF170
MONITOR_OFF, MONITOR_ON = 2, -1
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002
MOUSEEVENTF_MOVE = 0x0001

SHUTDOWN_DELAY = 10


def screen_off():
    user32.SendMessageW(HWND_BROADCAST, WM_SYSCOMMAND, SC_MONITORPOWER, MONITOR_OFF)


def screen_on():
    kernel32.SetThreadExecutionState(ES_DISPLAY_REQUIRED | ES_SYSTEM_REQUIRED)
    user32.mouse_event(MOUSEEVENTF_MOVE, 0, 1, 0, 0)
    user32.mouse_event(MOUSEEVENTF_MOVE, 0, -1, 0, 0)
    user32.SendMessageW(HWND_BROADCAST, WM_SYSCOMMAND, SC_MONITORPOWER, MONITOR_ON)


def sleep_pc():
    powrprof.SetSuspendState(0, 1, 0)  # Hibernate=False, ForceCritical=True


def lock_pc():
    user32.LockWorkStation()


def shutdown_pc():
    subprocess.run(["shutdown", "/s", "/t", str(SHUTDOWN_DELAY)], creationflags=NO_WINDOW)


def restart_pc():
    subprocess.run(["shutdown", "/r", "/t", str(SHUTDOWN_DELAY)], creationflags=NO_WINDOW)


def abort_shutdown():
    subprocess.run(["shutdown", "/a"], creationflags=NO_WINDOW)


# ------------------------------------------------------------- keyboard ---- #
KEYEVENTF_KEYUP = 0x0002
VK = {
    "vol_up": 0xAF, "vol_down": 0xAE, "vol_mute": 0xAD,
    "play_pause": 0xB3, "next": 0xB0, "prev": 0xB1,
    "lwin": 0x5B, "d": 0x44, "alt": 0x12, "f4": 0x73,
}


def _tap(*keys, times=1):
    for _ in range(times):
        for k in keys:
            user32.keybd_event(VK[k], 0, 0, 0)
        for k in reversed(keys):
            user32.keybd_event(VK[k], 0, KEYEVENTF_KEYUP, 0)
        if times > 1:
            time.sleep(0.01)


def volume_up():    _tap("vol_up", times=5)      # each tap = 2 %
def volume_down():  _tap("vol_down", times=5)
def volume_max():   _tap("vol_up", times=50)
def volume_mute():  _tap("vol_mute")               # toggles
def media_play():   _tap("play_pause")
def media_next():   _tap("next")
def media_prev():   _tap("prev")
def show_desktop(): _tap("lwin", "d")
def close_window(): _tap("alt", "f4")


# ----------------------------------------------------------------- apps ---- #
def open_target(target: str):
    """Launch an exe / URL / URI the way `start` would."""
    subprocess.Popen(["cmd", "/c", "start", "", target], creationflags=NO_WINDOW)


# ------------------------------------------------------------- utilities --- #
def screenshot() -> Path:
    from PIL import ImageGrab
    folder = Path.home() / "Pictures" / "Xyrus"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / dt.datetime.now().strftime("Screenshot_%Y-%m-%d_%H-%M-%S.png")
    ImageGrab.grab(all_screens=True).save(path)
    return path


def time_text() -> str:
    return dt.datetime.now().strftime("It's %I:%M %p").replace(" 0", " ")


def date_text() -> str:
    return dt.datetime.now().strftime("Today is %A, %B %d")


def system_status() -> str:
    import psutil
    cpu = psutil.cpu_percent(interval=0.5)
    mem = psutil.virtual_memory()
    parts = [f"CPU at {cpu:.0f} percent", f"memory at {mem.percent:.0f} percent"]
    bat = psutil.sensors_battery()
    if bat is not None:
        parts.append(f"battery at {bat.percent:.0f} percent" + (", charging" if bat.power_plugged else ""))
    up = time.time() - psutil.boot_time()
    parts.append(f"up for {int(up // 3600)} hours {int(up % 3600 // 60)} minutes")
    return ", ".join(parts) + "."


def beep():
    import winsound
    for _ in range(3):
        winsound.Beep(880, 180)
        time.sleep(0.08)


JOKES = [
    "Why do programmers prefer dark mode? Because light attracts bugs.",
    "I would tell you a UDP joke, but you might not get it.",
    "There are 10 types of people: those who understand binary, and those who don't.",
    "Why was the computer cold? It left its Windows open.",
    "I told my PC I needed a break. It said: no problem, I'll go to sleep.",
    "A SQL query walks into a bar, sees two tables, and asks: can I join you?",
]


def joke() -> str:
    return random.choice(JOKES)


# -------------------------------------------------------------- youtube ---- #
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
    t = re.split(r"\s+\|\s+", t)[0].strip(" -\u2013\u2014")
    m = re.match(r"^(.+?)\s+[-\u2013\u2014]\s+(.+)$", t)
    if m:
        t = f"{m.group(2)} by {m.group(1)}"
    words = t.split()
    return " ".join(words[:10]) if words else title


def find_song(query: str):
    """(spoken title, watch URL, video title) of the best YouTube match. Raises LookupError if nothing is found."""
    results = youtube_search(query)
    if not results:
        raise LookupError(query)
    vid, title, _channel, _length = next((r for r in results if r[3] and r[3] <= MAX_SONG_SECS), results[0])
    url = f"https://www.youtube.com/watch?v={vid}"
    if not title:
        title = video_title(url) or ""
    return (spoken_title(title) if title else query), url, title


def video_title(url: str, timeout=4):
    """A video's title via YouTube's oEmbed endpoint (used when the results page layout isn't recognised)."""
    try:
        req = urllib.request.Request(
            "https://www.youtube.com/oembed?" + urllib.parse.urlencode({"url": url, "format": "json"}),
            headers={"User-Agent": _UA})
        return json.loads(urllib.request.urlopen(req, timeout=timeout).read().decode("utf8")).get("title")
    except Exception:
        return None


# ---------------------------------------------------- make sure it plays ---- #
# Browsers block autoplay-with-sound for pages opened by another program, so a YouTube page
# often loads paused. We watch the browser's audio meter and, only if it stays silent, focus the
# video's window and press play (YouTube's "k" key, then a click on the player).
BROWSERS = {"msedge.exe", "chrome.exe", "firefox.exe", "brave.exe", "opera.exe", "vivaldi.exe"}
VK_K, SC_K = 0x4B, 0x25
VK_MENU, SC_MENU = 0x12, 0x38
MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
SW_RESTORE = 9

_u32 = ctypes.WinDLL("user32", use_last_error=True)   # private copy: argtypes here don't leak into other code
_WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
_u32.EnumWindows.argtypes = [_WNDENUMPROC, wt.LPARAM]
_u32.GetForegroundWindow.restype = wt.HWND
_u32.IsWindowVisible.argtypes = [wt.HWND]
_u32.IsIconic.argtypes = [wt.HWND]
_u32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
_u32.SetForegroundWindow.argtypes = [wt.HWND]
_u32.GetWindowTextLengthW.argtypes = [wt.HWND]
_u32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
_u32.GetClientRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
_u32.ClientToScreen.argtypes = [wt.HWND, ctypes.POINTER(wt.POINT)]
_u32.GetCursorPos.argtypes = [ctypes.POINTER(wt.POINT)]


def _browser_audio(trust_state=True, exe=None) -> bool:
    """Is a web browser (only `exe`, e.g. 'msedge.exe', if given) sending sound out right now?
    trust_state=False ignores 'stream open' and only believes the meter (used right after we
    paused other media, whose stream may linger)."""
    import comtypes
    from pycaw.pycaw import AudioUtilities, IAudioMeterInformation
    try:
        comtypes.CoInitialize()
    except Exception:
        pass
    for s in AudioUtilities.GetAllSessions():
        try:
            name = s.Process.name().lower() if s.Process else ""
            if name not in BROWSERS or (exe and name != exe):
                continue
            if trust_state and s.State == 1:          # AudioSessionStateActive
                return True
            if s._ctl.QueryInterface(IAudioMeterInformation).GetPeakValue() > 0.002:
                return True
        except Exception:
            continue
    return False


def browser_playing() -> bool:
    return _browser_audio(trust_state=True)


def _audio_within(seconds, trust_state=True, exe=None) -> bool:
    end = time.time() + seconds
    while True:
        if _browser_audio(trust_state, exe):
            return True
        if time.time() >= end:
            return False
        time.sleep(0.2)


def _top_windows():
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


_u32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]


def _window_exe(hwnd):
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


def _title_key(text):
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def _wait_youtube_window(video_title, timeout):
    """The browser window showing THIS video (its title contains the video's title), or None.
    Never guesses: pressing play in some other YouTube window could pause the user's own video."""
    key = _title_key(video_title)[:20].strip()
    if not key:
        return None
    end = time.time() + timeout
    while time.time() < end:
        for h, t in _top_windows():
            tk = _title_key(t)
            if "youtube" in tk and key in tk:
                return h
        time.sleep(0.3)
    return None


def _focus(hwnd):
    if _u32.IsIconic(hwnd):
        _u32.ShowWindow(hwnd, SW_RESTORE)
    if _u32.GetForegroundWindow() != hwnd:
        user32.keybd_event(VK_MENU, SC_MENU, 0, 0)            # an Alt tap lets a background app hand over focus
        user32.keybd_event(VK_MENU, SC_MENU, KEYEVENTF_KEYUP, 0)
        _u32.SetForegroundWindow(hwnd)
        time.sleep(0.2)


def _press_play_key(hwnd):
    user32.keybd_event(VK_K, SC_K, 0, 0)                      # YouTube: k = play / pause
    user32.keybd_event(VK_K, SC_K, KEYEVENTF_KEYUP, 0)


def _click_player(hwnd):
    """One click on the video player (upper-left of the page in every YouTube layout), mouse put back."""
    r = wt.RECT()
    _u32.GetClientRect(hwnd, ctypes.byref(r))
    pt = wt.POINT(0, 0)
    _u32.ClientToScreen(hwnd, ctypes.byref(pt))
    x = pt.x + int((r.right - r.left) * 0.30)
    y = pt.y + int((r.bottom - r.top) * 0.40)
    old = wt.POINT()
    _u32.GetCursorPos(ctypes.byref(old))
    user32.SetCursorPos(x, y)
    time.sleep(0.05)
    user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    time.sleep(0.05)
    user32.SetCursorPos(old.x, old.y)


def ensure_playing(video_title, trust_state=True, load_timeout=12.0) -> str:
    """Make sure the YouTube video we just opened is actually playing. Never presses anything if
    sound is already coming out. Returns 'autoplay', 'started', 'blocked' or 'no-window'."""
    hwnd = _wait_youtube_window(video_title, load_timeout)
    if hwnd is None:
        return "no-window"                              # can't tell which window is ours: touch nothing
    exe = _window_exe(hwnd)
    exe = exe if exe in BROWSERS else None              # only the song's own browser counts
    time.sleep(1.5)                                     # let the player initialise
    if _audio_within(4.0, trust_state, exe):
        return "autoplay"
    for press in (_press_play_key, _click_player):
        _focus(hwnd)
        time.sleep(0.3)
        if not _is_foreground(hwnd):                    # focus was lost (you clicked elsewhere):
            return "blocked"                            # never type or click into another window
        press(hwnd)
        if _audio_within(3.0, True, exe):
            return "started"
    return "blocked"


# ---------------------------------------------------------- exact volume ---- #
def _endpoint_volume():
    """The default speakers' IAudioEndpointVolume (pycaw). Safe to call from any thread."""
    import comtypes
    try:
        comtypes.CoInitialize()
    except Exception:
        pass
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
    dev = AudioUtilities.GetSpeakers()
    ev = getattr(dev, "EndpointVolume", None)
    if ev is not None:
        return ev
    from comtypes import CLSCTX_ALL
    iface = dev.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
    return ctypes.cast(iface, ctypes.POINTER(IAudioEndpointVolume))


def get_volume() -> int:
    return int(round(_endpoint_volume().GetMasterVolumeLevelScalar() * 100))


def set_volume(percent) -> int:
    p = max(0, min(100, int(round(percent))))
    ev = _endpoint_volume()
    ev.SetMasterVolumeLevelScalar(p / 100.0, None)
    if p > 0 and ev.GetMute():
        ev.SetMute(0, None)
    return p


def change_volume(delta) -> int:
    return set_volume(get_volume() + delta)


def is_muted() -> bool:
    return bool(_endpoint_volume().GetMute())


def set_muted(muted: bool):
    _endpoint_volume().SetMute(1 if muted else 0, None)


# ------------------------------------------------------------ brightness ---- #
def get_brightness():
    import screen_brightness_control as sbc
    vals = sbc.get_brightness()
    return int(vals[0]) if vals else None


def set_brightness(percent) -> int:
    import screen_brightness_control as sbc
    p = max(0, min(100, int(round(percent))))
    sbc.set_brightness(p)
    return p


def change_brightness(delta) -> int:
    cur = get_brightness()
    if cur is None:
        raise RuntimeError("this screen doesn't report its brightness")
    return set_brightness(cur + delta)


def find_songs(query: str, limit: int = 6):
    """Ranked [(spoken title, watch URL, video title)] for a song request - normal-length videos first.
    Used by 'not that one' to move on to the next result."""
    results = youtube_search(query)
    if not results:
        raise LookupError(query)
    normal = [r for r in results if r[3] and r[3] <= MAX_SONG_SECS]
    ordered = normal + [r for r in results if r not in normal]
    out = []
    for vid, title, _channel, _length in ordered[:limit]:
        url = f"https://www.youtube.com/watch?v={vid}"
        out.append(((spoken_title(title) if title else query), url, title))
    return out
