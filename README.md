# Xyrus — an offline voice assistant for Windows

Say **"Xyrus"** and talk to your PC like a personal assistant: *"Xyrus, open Chrome"*, *"Xyrus, play Ishwar by
Vikings"*, *"Xyrus, remind me to call mom at five"*, *"Xyrus, what do I have this week?"*, *"Xyrus, set a tea timer
for three minutes"*, *"Xyrus, turn it down a bit"* … and it answers, briefly, calling you "sir".

- **Offline.** Speech is recognised on this PC (Vosk for the wake word and commands, Whisper for names and free
  text). Nothing you say leaves the PC. Only song lookups (YouTube) and the optional weather use the internet.
- **Always on, never jumpy.** While idle it only listens for its name, so music, videos and people talking don't
  trigger it. Starts hidden in the tray with Windows (you can turn that off).
- **About 150 commands and 570 ways to say them**, plus a meaning layer that understands other phrasings.
  Say *"what can you do"* for the full list (the Commands tab).
- **A real assistant:** calendar with events, reminders, to-dos and notes; spoken alerts and pop-ups; a morning
  briefing; timers; things to remember; your own routines.
- **A window you can also type into**, with a light and a dark look.

---

## What's inside (the full stack)

Everything runs locally on Windows. Python **3.13** (installed by the setup if missing). The exact package
versions are pinned in [`requirements.txt`](requirements.txt) (CPU) and [`requirements-gpu.txt`](requirements-gpu.txt)
(the NVIDIA runtime); the highlights:

**Speech recognition (offline):**
- **Vosk 0.3.45** — wake word ("Xyrus") and fast commands, using the **`vosk-model-small-en-us-0.15`** model
  (~40 MB, from alphacephei.com). This model *is* in the repo, under `model/`.
- **faster-whisper 1.2.1** (on **ctranslate2 4.8.2**) — free text: song names, search words, calendar titles. It
  picks the best of these models it finds, in order:
  - **`whisper-large-v3`** — the accurate one, NVIDIA only (~3 GB) — from `Systran/faster-whisper-large-v3`
  - **`whisper-large-v3-turbo`** — optional faster variant (~1.6 GB) — a faster-whisper/CT2 build of large-v3-turbo
  - **`whisper-small`** — the fallback that always installs (~480 MB) — from `Systran/faster-whisper-small`
  - **`whisper-large-v3-bn`** — a Bengali specialist for Bangla song names (~3 GB), **built on your PC** by
    `tools/convert_bangla.py` from a mozilla-ai Bangla fine-tune (there is no ready-made download).
- **onnxruntime 1.30.0** — runs the bundled **Silero VAD** (`silero_vad_v6.onnx`) that decides where a command
  starts and ends.

**Voice out & Windows control:** the built-in Windows SAPI voice (via `comtypes`/`pywin32`), **pycaw** (per-app
volume), **screen_brightness_control**, **keyboard** (hotkeys / typing), **psutil** + **WMI** (system info),
**sounddevice** (microphone).

**Window & tray:** **customtkinter** (the light/dark window), **pystray** + **pillow** (the tray icon).

> **Note on model files:** the four Whisper `model.bin` weight files (~7.9 GB total) and the `venv/` are **not in
> this repo** — GitHub rejects files over 100 MB, and both are re-created automatically by the setup (`pip` +
> the downloads above). The small model *metadata* files are kept so you can see exactly which models are used.
> Everything else — all the source code, tests, docs, the Vosk model, the installer, and the author's own config
> and data — is here.

---

## Setup on a new PC — one file

**Needs:** Windows 10/11, a microphone, the internet during setup, and free disk space: about **9 GB** with an
NVIDIA graphics card, about **4 GB** without one. Nothing else — Python is installed by the setup if missing.

1. Copy **`Xyrus-Setup.exe`** to the new PC (build it here with
   `powershell -ExecutionPolicy Bypass -File tools\installer\build_installer.ps1` → `dist\Xyrus-Setup.exe`, ~0.6 MB).
2. Double-click it and answer **Yes**. Windows may warn *"Windows protected your PC"* (the file isn't signed) —
   click **More info → Run anyway**. A window shows the progress (10–40 minutes). It
   - installs Python 3.13 for this user if the PC has no Python 3.11–3.13,
   - puts Xyrus in `%LOCALAPPDATA%\Programs\Xyrus` (no administrator rights needed),
   - installs the packages, plus the graphics-card runtime when an **NVIDIA** card is found,
   - downloads the speech models: the wake-word model (~40 MB), Whisper small (~480 MB) and — with an NVIDIA
     card — Whisper large-v3 (~3 GB, the accurate one),
   - adds Xyrus to Windows startup, puts a **Xyrus** shortcut on the desktop, runs a self-test and starts it.
3. Say **"Xyrus"**, wait for the reply, then **"what can you do."** If it answers, you're done.

Running `Xyrus-Setup.exe` again **updates** Xyrus and keeps your calendar, settings and downloaded models.
Your own calendar, to-dos, notes and memories are **not** in the installer — to bring them along, copy the `data\`
folder from the old PC into `%LOCALAPPDATA%\Programs\Xyrus\data` after setup.

**Bangla song names (optional):** the Bengali speech model has no ready-made download — it is built from a 6 GB
fine-tune on the PC (NVIDIA card only, 30–60 minutes). Run in the Xyrus folder: `install.bat -Bangla`.

### Setup by hand (a copied folder, or offline)
Copy `arc.py hearing.py xyrus\ docs\ tools\ requirements.txt requirements-gpu.txt setup.ps1 install.bat README.md`
to the PC (never `venv\`, `legacy\`, `config.json`, `*.log` or `*.lnk`), then double-click **`install.bat`**.
Options (also for `setup.ps1`; for the one-file setup put them in the `XYRUS_SETUP_ARGS` environment variable):

| Option | What it does |
|---|---|
| `-NoStartup` | don't add to Windows startup, no desktop shortcut, don't launch (only `Xyrus.lnk` in the folder) |
| `-CpuOnly` | don't use an NVIDIA card even if there is one (Whisper small on the processor) |
| `-Bangla` | also build the Bengali speech model (NVIDIA only; ~6 GB download, 30–60 minutes) |
| `-ModelSource <folder>` | copy the Vosk model from that folder instead of downloading it |
| `-WhisperSource <folder>` | copy Whisper models from a folder holding `whisper-small\`, `whisper-large-v3\`, `whisper-large-v3-bn\` (like `D:\arc\models`) instead of downloading |
| `-SkipWhisper` | skip the Whisper downloads for now (re-run later) |
| `-SkipPip` | don't run pip — for a fully offline install where `venv\` was copied along from a working PC |

Example (everything copied from this PC, no downloads but the packages):
`install.bat -NoStartup -ModelSource D:\arc\model -WhisperSource D:\arc\models`

**The first time, Windows may ask to allow microphone access** — say yes. If it never hears you:
Settings → Privacy & security → Microphone → allow desktop apps.

### Uninstall
Quit Xyrus from the tray (right-click → Quit Xyrus), untick *Start with Windows* first (or delete `Xyrus.lnk` from
`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup`), then delete the folder and the desktop shortcut.

---

## How to talk to it

- **Wake word:** say **"Xyrus"** (the speech model hears it as "cyrus", "zeros", "virus" or "cirrus" — all fine).
- **One breath:** *"Xyrus, open Chrome"* — the command right after the name.
- **Or wait for it:** say just *"Xyrus"* → *"Waiting for your command, sir."* → then say the command, without the
  name, whenever you're ready — there is **no time limit** (Settings → Listening → Command window; 0 = no limit).
  Xyrus listens until you've **finished talking** (a short pause ends it), hears the whole command with Whisper,
  runs it once and goes back to waiting for its name. If it didn't understand: *"Sorry, I didn't catch that"* —
  say the name again. *"Never mind"* / *"cancel"* / *"nothing"* ends it quietly.
- **Bangla song names:** *"play আমার ভিনদেশী তারা"* (say the title in Bangla) — Xyrus notices the Bangla, hears it
  in Bengali, searches YouTube with the Bangla title (then the Banglish, e.g. "amar bhindeshi tara") and says the
  name in Banglish. *"আমার ভিনদেশী তারা বাজাও"* / *"… চালাও"* work too.
- **Follow-ups without the name:** right after some replies a few answers work on their own — *"and after that"*
  after *"what's next"*, *"snooze"* after a reminder, *"cancel"* during a shutdown countdown, *"cancel timer"* while
  a timer runs, *"undo that"* right after you added something, *"not that one"* right after a song starts.
- **Questions back:** when it asks something (*"What time?"*, *"Shut down, sir? Yes or no."*), just answer. The
  window shows the question in a banner where you can also type the answer or press Cancel.
- **Typing:** everything you can say you can also type in the Home tab (the name is optional when typing).
- **Meaning, not exact words:** *"turn it down a bit"*, *"can you make it louder please"*, *"I can't hear it"*,
  *"make the screen darker"*, *"turn off the sound"* (mutes — it never shuts the PC down), *"shut down chrome"*
  (closes Chrome — never the PC). Dangerous commands always ask first, and are refused when half-heard.

## What it can do (highlights — the Commands tab has all of it)

**Songs** — *"play alone"*, *"play Tum Hi Ho by Arijit Singh"*, *"play Ishwar by Vikings"* finds the song on YouTube
and plays it (pausing whatever the browser was playing). *"play some music"* → *"What should I play, sir?"* → say the
name. Wrong video? Say **"not that one"** / *"wrong song"* / *"another one"* / *"next result"* within 40 seconds (no
name needed; with the name any time) and it tries the next result. *"play <song> on spotify"* searches Spotify.
Bare *"play"* / *"pause"* / *"next"* / *"previous"* control whatever is playing.

**Calendar & reminders** — *"remind me to call mom at five"*, *"remind me in twenty minutes to check the oven"*,
*"add dentist appointment on Friday at three pm"* (reminds you 10 minutes before), *"schedule gym every Monday at
six pm"*, *"add buy milk to my to do list tomorrow"*, *"add a note for Friday: the rent is due"*,
*"what do I have today / this week / on Friday / in the next ten days"*, *"what's next"* → *"and after that"*,
*"mark buy milk as done"*, *"delete the dentist appointment"*, *"undo that"*. Reminders speak, chime and pop up at the
time (and repeat once if you don't answer); say *"okay"*, *"snooze"* or *"remind me again in ten minutes"*. Missed
while the PC was off? It tells you when it starts. *"good morning"* gives today's plan (and it does so by itself once
each morning at 08:30 if there's something on). The **Calendar tab** has the month, tick boxes for to-dos, events and
notes per day, and a quick-add box (*"dentist tomorrow 3pm"*, *"todo buy milk friday"*).

**Timers** — *"set a timer for five minutes"*, *"tea timer three minutes"* (several at once), *"how long is left"*,
*"add two minutes"*, *"cancel timer"*. When it rings: *"stop"* or *"snooze"*.

**Clap to switch the display** — clap **three times**: the display turns off; three more: it turns back on (Xyrus keeps running). Knocks, thuds and setting the microphone down don't count. The number of claps (2–4), loudness and on/off are in Settings → Listening.

**Sound, screen & windows** — volume up/down/by N/to N percent/max, mute, un mute, per-app volume (*"mute chrome"*),
brightness, *"screen off"* / *"wake up"*, show desktop, minimise/maximise, *"snap left"*, *"switch to Spotify"*,
*"close Notepad"*, *"what's open"*, virtual desktops, *"keep this on top"*.

**Apps & web** — *"open chrome / youtube / calculator / bluetooth settings / downloads"* or anything in the Start menu
(edit the list in the Apps tab), *"search for cheap keyboards"*, *"search youtube for lofi beats"*.

**Power** — shut down / restart (asks first, then 10 s — *"cancel"* stops it), *"shut down in thirty minutes"*,
sleep, hibernate, lock, sign out, *"keep the PC awake"*.

**Keys & clipboard** — *"type hello world"*, *"press enter"*, copy / paste / undo / save / new tab / refresh,
*"read the clipboard"*.

**Info** — time, date, *"system status"*, battery, disk space, *"what's using the cpu"*, screenshots (to
`Pictures\Xyrus`), a calculator (*"what is twelve times four"*), jokes, coin, dice, weather (opt-in).

**Memory** — *"remember that the wifi password is banana seven"*, *"what did I ask you to remember"*, *"forget that"*.

**Look** — *"dark mode"* / *"light mode"* / *"follow windows theme"* switch Xyrus's window (also Settings →
Appearance). *"windows dark mode"* / *"windows light mode"* switch all of Windows.

**Your own commands (Routines tab)** — a phrase plus steps: open something, run a command line, press keys, type
text, say something, run another command, wait. Templates: *movie mode*, *work mode*, *focus mode*.

## Hearing names: Whisper

The small offline model knows common English words only, so a name like "Ishwar by Vikings" used to come out as
"issue war". For free text — song names, the answer to "What should I play?", search words, calendar titles and
notes — Xyrus now uses **Whisper** (faster-whisper "small", on the CPU, offline). The wake word and commands stay on
the fast model. Whisper loads in the background after start-up; until it's ready (or if its model isn't installed)
Xyrus simply uses the small model.

## The window, tray and hotkeys

- Click the tray icon (purple = listening, green ring = waiting for your command, grey = paused, red = mic problem)
  or the desktop shortcut. Closing the window keeps Xyrus running in the tray; **Quit** is in the tray menu.
- Tabs: **Home** (conversation, typing box, timers) · **Calendar** · **Commands** · **Apps** · **Routines** ·
  **Settings** (microphone + sensitivity, wake words, voice and speed, your name, greeting, reminders, quiet hours,
  morning brief, confirm before shutdown, YouTube lookup, weather, appearance, start with Windows, hotkeys, log,
  self-test).
- **Hotkeys:** `Ctrl+Alt+X` show the window · `Ctrl+Alt+Space` push-to-talk (chime, then 8 s to speak a command)
  · `Ctrl+Alt+S` stop talking.
- **Pause** (button, tray, or *"stop listening"*) **turns the microphone off** — nothing is heard until you resume
  from the window, the tray or the hotkey. Typing still works while paused.

## Privacy
Everything runs on this PC. Audio is never saved (unless you press "Save last 30 seconds" in Settings to report a
problem). The internet is used only to look up songs on YouTube, for the opt-in weather, and during setup.

## Changes from the first version
- *"how are you"* is small talk now (*"system status"* gives the numbers).
- Bare *"play"* still toggles play/pause; *"play <song>"* plays a song.
- Deleting something from the calendar asks first; *"undo"* alone works for 15 s after a voice add (else it is Ctrl+Z).
- *"what's coming up"* covers the next 7 days; the morning brief is at 08:30 (Settings).
- Times are spoken like a person says them ("3 PM").

## Troubleshooting
- **It doesn't hear me:** check the microphone in Settings (pick the MME entry of your mic; the meter must move),
  and Windows' microphone privacy switch. Raise *mic sensitivity* if you speak softly.
- **It hears its name but not the command:** say the command right after the name, or wait for the reply first.
  If it keeps mishearing the name, tick more wake variants (Settings → Listening).
- **Song names come out wrong:** Whisper may still be loading (it takes a little while after start-up) or its model
  is missing — re-run `install.bat`.
- **Log:** `arc.log` in the folder (Settings → Open log). Crashes go to `crash.log`.
- **Test without the microphone:** `venv\Scripts\python.exe arc.py --text` (type commands, see the replies).
- **Self-test:** `venv\Scripts\python.exe arc.py --selftest` (add `--full` to also run the unit tests).
- **Try an audio file:** `venv\Scripts\python.exe arc.py --wav clip.wav` (16 kHz mono) prints what it hears.

## Files
- `arc.py` starts Xyrus (`--tray` = hidden in the tray, as the Startup shortcut does) · `xyrus\` the program
  (`app.py` puts it together) · `hearing.py` Whisper for names · `tests\` self-tests · `docs\` the design spec
- `config.json` your settings · `data\calendar.json` calendar, to-dos, notes · `data\memory.json` things to remember
- `model\` speech model · `models\whisper-small\` Whisper model · `venv\` Python environment
- `arc.log` / `crash.log` logs · `Xyrus.lnk` opens the window · `setup.ps1` / `install.bat` installer ·
  `requirements.txt` packages · `legacy\v1\` the first version (kept, not used)
