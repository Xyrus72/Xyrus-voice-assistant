"""Sound and media (§3.3). Bare play/pause/resume stay media keys (D4); songs live in commands/web.py.

Lead requirement (2026-09-13): level changes use exact pycaw control (±10 %, "a little/a bit" ±5 %,
"a lot" ±25 %) and reply with the resulting level; many paraphrases; "turn off the sound" mutes."""
from __future__ import annotations

from xyrus import replies as R
from xyrus.commands import app_key, app_label, say
from xyrus.registry import command

SECTION = "Sound & media"
STEP, SMALL_STEP, BIG_STEP = 10, 5, 25
_SMALL = {"little", "bit", "slightly", "tad", "touch"}
_BIG = {"lot", "much", "way"}


def step_size(words) -> int:
    """10 by default; 'a little' / 'a bit' / 'slightly' -> 5; 'a lot' / 'much' -> 25."""
    ws = set(words)
    if ws & _SMALL:
        return SMALL_STEP
    if ws & _BIG:
        return BIG_STEP
    return STEP


def _step(ctx, delta: int) -> None:
    ctx.do(ctx.actions.volume_step, delta,
           then=lambda level: say(ctx, R.VOLUME_AT, pct=level) if isinstance(level, int) and level >= 0 else None)


@command("volume_up", "volume up", "louder", "turn it up", "turn up the volume", "turn the volume up",
         "increase the volume", "increase volume", "raise the volume", "raise volume", "make it louder",
         "turn up the sound", "increase the sound", "more volume", "volume higher",
         section=SECTION, help="volume +10 % (a bit: +5 %, a lot: +25 %)")
def volume_up(ctx, m):
    _step(ctx, step_size(m.text.split()))


@command("volume_down", "volume down", "quieter", "turn it down", "turn down the volume", "turn the volume down",
         "lower the volume", "lower volume", "decrease the volume", "decrease volume", "reduce the volume",
         "reduce volume", "make it quieter", "lower the sound", "lower sound", "decrease the sound",
         "turn down the sound", "less volume", "volume lower", "too loud",
         section=SECTION, help="volume -10 % (a bit: -5 %, a lot: -25 %)")
def volume_down(ctx, m):
    _step(ctx, -step_size(m.text.split()))


@command("volume_up_by", "volume up by <number>", "turn it up by <number>", "increase the volume by <number>",
         "raise the volume by <number>", section=SECTION, help="volume up by that many percent")
def volume_up_by(ctx, m):
    _step(ctx, max(0, min(100, int(round(m.slots["number"])))))


@command("volume_down_by", "volume down by <number>", "turn it down by <number>", "lower the volume by <number>",
         "decrease the volume by <number>", "reduce the volume by <number>", section=SECTION,
         help="volume down by that many percent")
def volume_down_by(ctx, m):
    _step(ctx, -max(0, min(100, int(round(m.slots["number"])))))


@command("set_volume", "set [the] volume to <percent>", "volume <percent>", "set volume <percent>",
         "volume to <percent>", "turn the volume to <percent>", "turn it up to <percent>",
         "turn it down to <percent>", "change the volume to <percent>", "put the volume at <percent>",
         section=SECTION, help="sets the volume to an exact level", priority=1)
def set_volume(ctx, m):
    pct = int(m.slots["percent"])
    ctx.do(ctx.actions.volume_set, pct, then=lambda _: say(ctx, R.VOLUME_AT, pct=pct))


@command("max_volume", "volume max", "full volume", "max volume", "maximum volume", "volume all the way up",
         section=SECTION, help="volume to 100 %")
def max_volume(ctx, m):
    ctx.do(ctx.actions.volume_set, 100, then=lambda _: say(ctx, R.VOLUME_AT, pct=100))


@command("query_volume", "what's the volume", "what is the volume", "volume level", "how loud is it",
         "what's the volume level", section=SECTION, help="says the volume level")
def query_volume(ctx, m):
    a = ctx.actions

    def read():
        return a.volume_get(), a.mute_get()

    def reply(res):
        pct, muted = res
        say(ctx, R.VOLUME_IS_MUTED if muted else R.VOLUME_IS, pct=pct)
    ctx.do(read, then=reply)


@command("mute", "mute", "mute the sound", "mute the volume", "turn off the sound", "turn the sound off",
         "sound off", "turn off the volume", "silence", section=SECTION, help="mutes the sound")
def mute(ctx, m):
    ctx.do(ctx.actions.mute_set, True)


@command("unmute", "un mute", "sound on", "turn the sound on", "turn on the sound", "turn the sound back on",
         "turn on the volume", section=SECTION,
         help="un-mutes the sound ('unmute' isn't a word the speech model knows)")
def unmute(ctx, m):
    ctx.do(ctx.actions.mute_set, False)


def _media_when(ctx, press_if_playing: bool, already: str) -> None:
    """Press play/pause only when it changes something. The key is a blind toggle - "pause" on a paused song
    started it again (user report, Sep 15 2026) - so the browser's audio meter decides first: is the song playing
    right now? Unknown (the meter failed) -> press, as before."""
    a = ctx.actions
    # the sound meter, not the session state (Chrome keeps a paused tab's session Active for a while)
    probe = getattr(a, "media_sounding", None) or a.browser_playing

    def decide(playing):
        if bool(playing) == press_if_playing:
            ctx.do(a.media, "play_pause")
        else:
            say(ctx, already)
    ctx.do(probe, then=decide, error=lambda e: ctx.do(a.media, "play_pause"))


def pause_playback(ctx) -> None:
    _media_when(ctx, True, R.ALREADY_PAUSED)


def resume_playback(ctx) -> None:
    """Also what "play the song" / "play it" / "play the song that you paused" mean (commands/web.play_song)."""
    _media_when(ctx, False, R.ALREADY_PLAYING)


@command("pause_media", "pause", "pause it", "pause the song", "pause this song", "pause the music", "pause the video",
         "pause the track", "pause playback", section=SECTION, help="pauses the song or video that is playing")
def pause_media(ctx, m):
    pause_playback(ctx)


# "play it" / "play the song" / "play the song that you paused" are NOT phrases here: as fixed phrases they also
# swallowed titles ("play the song of storms"); commands/web.play_song resumes when the whole title means "the song"
@command("resume_media", "resume", "resume it", "resume the song", "resume the music", "resume the video",
         "continue", "continue playing", "continue the song", "continue the music", "keep playing",
         "un pause", section=SECTION, help="plays the paused song again (not a new one)")
def resume_media(ctx, m):
    resume_playback(ctx)


@command("play_pause", "play", "play pause", section=SECTION,
         help="play / pause whatever is playing (add a song name to play it)")
def play_pause(ctx, m):
    ctx.do(ctx.actions.media, "play_pause")


@command("next_track", "next", "next track", "next song", "play next", "play the next song", section=SECTION,
         help="next track")
def next_track(ctx, m):
    ctx.do(ctx.actions.media, "next")


@command("skip_track", "skip", "skip this song", "skip this", section=SECTION, help="next track",
         armed_ok_single_word=False)
def skip_track(ctx, m):
    ctx.do(ctx.actions.media, "next")


@command("prev_track", "previous", "previous track", "previous song", "go back", "play previous",
         "play the previous song", "last song", section=SECTION, help="previous track")
def prev_track(ctx, m):
    ctx.do(ctx.actions.media, "prev")


@command("stop_media", "stop the music", "stop playback", "stop the song", "stop playing", section=SECTION,
         help="stops playback")
def stop_media(ctx, m):
    ctx.do(ctx.actions.media, "stop")


@command("app_mute", "mute <app>", section=SECTION, help="mutes one app, e.g. mute chrome")
def app_mute(ctx, m):
    ref = m.slots["app"]
    label = app_label(ref)
    ctx.do(ctx.actions.app_volume, app_key(ref), None, True,
           then=lambda ok: say(ctx, R.APP_MUTED if ok else R.APP_NO_AUDIO, app=label))


@command("app_volume", "set <app> volume to <percent>", section=SECTION,
         help="sets one app's volume, e.g. set spotify volume to thirty")
def app_volume(ctx, m):
    ref = m.slots["app"]
    pct = int(m.slots["percent"])
    label = app_label(ref)
    ctx.do(ctx.actions.app_volume, app_key(ref), pct, None,
           then=lambda ok: say(ctx, R.APP_VOLUME if ok else R.APP_NO_AUDIO, app=label, pct=pct))
