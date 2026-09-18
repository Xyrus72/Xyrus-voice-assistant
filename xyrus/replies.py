"""Every engine/persona user-facing string (G10), plus the reply strings quoted in SPEC §3 so command
modules can share them. Templates use {sir} (rendered by persona.render / ctx.say as ", sir" or "") and
named fields. G6: no wake alias anywhere; "Xyrus" only in NAME_WHITELIST templates (unit-tested).
"""

# ----------------------------------------------------------------------------- engine core (§3.1, §4.4)
WAKE_REPLIES = ("Waiting for your command, sir.", "At your service, sir.", "Yes, sir?", "I'm listening, sir.")
NOT_HEARD = "Sorry, I didn't catch that{sir}."
DID_YOU_MEAN = "Did you mean {phrase}?"
DID_YOU_SAY = "Did you say {phrase}{sir}?"                  # D11: destructive command with [unk]
CANCELLED = ("Cancelled{sir}.", "Alright, dropped.")
ACK = ("Done{sir}.", "Noted{sir}.", "Got it{sir}.")
THANKS = ("Anytime{sir}.", "My pleasure{sir}.", "Of course.")
OKAY = "Okay{sir}."
DONE = "Done{sir}."
ACTION_FAILED = "That didn't work{sir}. It's in the log."
TOO_SLOW = "That's taking too long{sir}. I've stopped waiting."
NUDGE = "Still there{sir}? "
GIVE_UP = "Sorry{sir}, I'm not getting it. You can type it in the window, or try again later."
LEAVE = "I'll leave it for now{sir}. Say '{trigger}' when you're ready."
HELP = "Here's everything I can do{sir}."
REPEAT_NOTHING = "I haven't said anything yet{sir}."
HEARD = "I heard: {text}."
HEARD_NOTHING = "I haven't heard anything yet{sir}."
PAUSING = "Pausing. Mic off{sir}."
MIC_OFF_FOR = "Mic off for {duration}{sir}."
BACK = "I'm back{sir}."
ROUTINE_DEPTH = "That routine calls itself too deeply{sir}."
# hearing rebuild (Sep 15 2026): the captured command was cut short / too faint - one more chance per wake
GO_AHEAD = "Go ahead{sir}."
SAY_AGAIN = "Say that again{sir}?"
PLAYING_MAYBE = 'Playing {title} - say "not that one" if it is wrong{sir}.'
CONFIRM_POWER_HEARD = "Did you say {phrase}? Yes or no."
PLAY_TITLE_Q = "Play {phrase}?"                               # a bare "<title> by <artist>" after the wake reply

# ----------------------------------------------------------------------------- small talk / identity (§3.9)
STARTUP_ONLINE = "Xyrus online."                              # whitelisted (G6)
WHO_ARE_YOU = "I'm Xyrus{sir}. Your offline assistant — nothing I hear leaves this PC."   # whitelisted (G6)
TEST_VOICE = "Hello{sir}. This is how Xyrus sounds."          # Settings "Test voice" (whitelisted, G6)
NAME_WHITELIST = ("STARTUP_ONLINE", "WHO_ARE_YOU", "TEST_VOICE")
HOW_ARE_YOU = "Running smoothly{sir}. CPU at {cpu} percent."
HELLO = "Hello{sir}."
ARE_YOU_THERE = "Always{sir}."
YOUR_NAME = "You're {name}{sir}."
NO_NAME = "You haven't told me yet{sir}. It's in Settings."
MISSED_WHILE_AWAY = "While I was away{sir}: {items}."

# ----------------------------------------------------------------------------- power (§3.2)
CONFIRM_SHUTDOWN = "Shut down{sir}? Yes or no."
CONFIRM_RESTART = "Restart{sir}? Yes or no."
SHUTTING_DOWN = "Shutting down in {seconds} seconds{sir}. Say cancel to stop it."
RESTARTING = "Restarting in {seconds} seconds{sir}. Say cancel to stop it."
CONFIRM_SHUTDOWN_IN = "Shut down in {duration}{sir}? Yes or no."
CONFIRM_RESTART_IN = "Restart in {duration}{sir}? Yes or no."
SHUTDOWN_AT = "Done. Shutting down at {time}{sir}."
RESTART_AT = "Done. Restarting at {time}{sir}."
SHUTDOWN_WHEN = "At {time}{sir}."
NO_SHUTDOWN = "There's no shutdown planned{sir}."
SHUTDOWN_CANCELLED = "Cancelled{sir}."
SLEEPING = "Going to sleep{sir}."
HIBERNATING = "Hibernating{sir}."
HIBERNATE_OFF = "Hibernate is turned off on this PC{sir}."
LOCKING = "Locking{sir}."
CONFIRM_SIGN_OUT = "Sign out{sir}? Yes or no."
SIGNING_OUT = "Signing out."
SCREEN_OFF = "Screen off{sir}."
SCREEN_ON = "I'm here{sir}."
KEEP_AWAKE = "I'll keep it awake{sir}."
LET_SLEEP = "Okay, sleep is allowed again."

# ----------------------------------------------------------------------------- sound (§3.3)
VOLUME_AT = "Volume at {pct} percent{sir}."
VOLUME_IS = "Volume's at {pct} percent{sir}."
VOLUME_IS_MUTED = "Volume's at {pct} percent and muted{sir}."
APP_MUTED = "{app} muted{sir}."
APP_VOLUME = "{app} at {pct} percent{sir}."
APP_NO_AUDIO = "{app} isn't playing any sound{sir}."

# ----------------------------------------------------------------------------- display / windows (§3.4)
BRIGHTNESS_AT = "Brightness at {pct} percent{sir}."
BRIGHTNESS_IS = "Brightness is at {pct} percent{sir}."
NO_BRIGHTNESS = "This screen doesn't let me change brightness{sir}."
SWITCHED_TO = "{app}{sir}."
NOT_OPEN_OFFER = "{app} isn't open{sir}. Want me to open it?"
CLOSING_APP = "Closing {app}{sir}."
NOT_OPEN = "{app} isn't open{sir}."
CONFIRM_FORCE_CLOSE = "Force close {app}{sir}? Unsaved work is lost. Yes or no."
PINNED = "Pinned on top{sir}."
UNPINNED = "Not on top anymore{sir}."
WHATS_OPEN = "{apps} {verb} open{sir}."
NOTHING_OPEN = "Nothing's open{sir}."
DARK_MODE = "Dark mode{sir}."
LIGHT_MODE = "Light mode{sir}."
OWN_WINDOW = "That's my own window{sir}. I'll hide it instead."

# ----------------------------------------------------------------------------- apps / web / keys (§3.5, §3.6)
OPENING = "Opening {app}{sir}."
UNKNOWN_APP = "I don't know how to open {name}{sir}. Add it in the Apps tab."
SEARCHING = "Searching for {query}{sir}."
YOUTUBE_PAGE = "Here's YouTube for {query}{sir}."
CLIPBOARD_EMPTY = "The clipboard is empty{sir}."
CLEARED = "Cleared{sir}."
COPIED = "Copied{sir}."
SONG_ASK = "What should I play{sir}?"
PLAYING = "Playing {title}{sir}."
SONG_NOT_FOUND = "I couldn't find {query} on YouTube{sir}. Here's the search."
YOUTUBE_OFFLINE = "I couldn't reach YouTube{sir}. Opening the search instead."
SPOTIFY_SEARCH = "Searching Spotify for {query}{sir}."
TYPE_OWN_WINDOW = "I won't type into my own window{sir}."

# ----------------------------------------------------------------------------- info (§3.7)
TIME_IS = "It's {time}{sir}."
DATE_IS = "Today is {date}{sir}."
STATUS = "CPU at {cpu} percent, memory at {mem} percent, {battery}, up {uptime}{sir}."
BATTERY = "Battery at {pct} percent{charging}{sir}."
NO_BATTERY = "There's no battery, this is a desktop{sir}."
DISK = "{drive} has {free} gigabytes free of {total}{sir}."
DISK_NEARLY_FULL = "It's nearly full."
CPU_USERS = "{apps}{sir}."
SCREENSHOT_SAVED = "Screenshot saved{sir}."
NO_SCREENSHOT = "There's no screenshot yet{sir}."
CALC_RESULT = "{value}{sir}."
UNDEFINED = "That's undefined{sir}."
COIN = ("Heads.", "Tails.")
DICE = "You rolled a {n}."
DICE_TWO = "You rolled a {a} and a {b}."
PICK = "{n}."
WEATHER = "{temp} degrees and {desc} in {city}{sir}."
WEATHER_OFF = "Weather needs the internet — turn it on in Settings{sir}."
WEATHER_FAILED = "I couldn't get the weather{sir}."

# ----------------------------------------------------------------------------- timers (§3.8)
TIMER_SET = "Timer set for {duration}{sir}."
NAMED_TIMER_SET = "{Name} timer set for {duration}{sir}."
TIMER_ASK = "For how long{sir}?"
TIMER_LEFT = "{left} left{sir}."
TIMER_LEFT_ITEM = "{Name}: {left}."
NO_TIMERS = "No timers running{sir}."
TIMER_ADDED = "Added {duration}{sir}."
TIMER_CANCELLED = "Timer cancelled{sir}."
TIMERS_CANCELLED = "All timers cancelled{sir}."
NO_TIMER = "There's no timer running{sir}."
TIMES_UP = "Time's up{sir}!"
TIMER_TOAST_TITLE = "Timer done"
TIMER_TOAST = "Timer done — {duration}"
TIMER_SNOOZED = "Five more minutes{sir}."

# ----------------------------------------------------------------------------- dialogues (§3.9, §4.9)
Q_EVENT_TITLE = "What's the event?"
Q_REMINDER_TITLE = "What should I remind you about?"
Q_NOTE = "What's the note?"
Q_TODO = "What's the to-do?"
Q_MEMORY = "What should I remember?"
Q_NAME = "What should I call you?"
Q_WHEN = "When?"
Q_TIME = "What time?"
Q_FIX = "What should I change: the title, the date, or the time?"
Q_WHICH = "Which one{sir}? {options}"
CORRECT = "{summary} Correct?"
MEMORY_CORRECT = "Got it: '{text}'. Correct?"
NAME_CORRECT = "{name}. Correct?"
SORRY_PREFIX = "Sorry, I didn't catch that."
HINT_TITLE = "say the title in a few words"
HINT_NOTE = "say the note in a few words"
HINT_MEMORY = "say what I should remember"
HINT_WHEN = "e.g. 'tomorrow at three pm' or 'next friday'"
HINT_TIME = "e.g. 'at three pm', 'noon', 'half past seven'"
HINT_FIX = "say 'the title', 'the date' or 'the time'"
HINT_CONFIRM = "say yes, no, or cancel"
HINT_PICK = "say 'the first', 'the second' …"
HINT_DURATION = "e.g. 'five minutes'"
HINT_FREE = "say it in a few words"
EVENT_SAVED = "Done{sir}. I'll remind you {lead} before."
EVENT_SAVED_NO_REMINDER = "Done{sir}. It's on your calendar."
EVENT_ADDED = "Added {title}, {when}{sir}."
REMINDER_SAVED = "I'll remind you {when} to {title}{sir}."
NOTE_SAVED = "Noted for {day}{sir}: {text}."
TODO_ADDED = "Added to your to-do list{sir}: {title}."
TODO_ADDED_DAY = "Added to your to-do list{sir}: {title}, due {day}."
DUPLICATE = "That's already on your calendar{sir}."
IN_THE_PAST = "That's in the past{sir}. Add it anyway?"
REMEMBERED = "Remembered{sir}."
FORGOTTEN = "Forgotten{sir}."
UNDO_REMOVED = "Removed {title}{sir}."
UNDO_NOTHING = "There's nothing to undo{sir}."

# ----------------------------------------------------------------------------- jokes (≥ 20; v1's six first)
JOKES = (
    "Why do programmers prefer dark mode? Because light attracts bugs.",
    "I would tell you a UDP joke, but you might not get it.",
    "There are 10 types of people: those who understand binary, and those who don't.",
    "Why was the computer cold? It left its Windows open.",
    "I told my PC I needed a break. It said: no problem, I'll go to sleep.",
    "A SQL query walks into a bar, sees two tables, and asks: can I join you?",
    "Why did the developer go broke? Because he used up all his cache.",
    "How many programmers does it take to change a light bulb? None. That's a hardware problem.",
    "I'd tell you a joke about the cloud, but you're offline and so am I.",
    "Why do Java developers wear glasses? Because they don't C sharp.",
    "My keyboard and I broke up. It said I wasn't its type.",
    "Why was the math book sad? It had too many problems.",
    "What do you call a computer that sings? A Dell.",
    "I asked the printer for a joke. It jammed.",
    "Why did the scarecrow get promoted? He was outstanding in his field.",
    "Debugging is like being the detective in a crime movie where you are also the murderer.",
    "Why don't keyboards sleep? Because they have two shifts.",
    "What's a computer's favourite snack? Microchips.",
    "I changed my password to incorrect, so now it reminds me whenever I forget it.",
    "Why did the PowerPoint presentation cross the road? To get to the other slide.",
    "What do you call eight hobbits? A hobbyte.",
    "Why was the robot tired when it got home? It had a hard drive.",
)
