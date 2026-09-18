# Xyrus — offline voice control for Windows

Say **"Xyrus, shut down"**, **"Xyrus, sleep"**, **"Xyrus, screen off"**, **"Xyrus, wake up"**,
**"Xyrus, open YouTube"**, **"Xyrus, set a timer for five minutes"**, **"Xyrus, what do I have this week?"**,
**"Xyrus, remind me to call mom at five"**, **"Xyrus, play alone"** … and it happens.

- Fully offline — nothing leaves the PC, no accounts, no internet needed after setup
- Always on — starts hidden in the tray with Windows (can be turned off)
- Wake word — only reacts after it hears "Xyrus", so videos and other people can't trigger it
- Talks back with the built-in Windows voice
- Calendar with a to-do list, events and notes for any day; reminders that speak and pop up; a daily briefing
- App window with live activity feed, calendar, mic meter, settings, and an editable "open apps" list

---

## Setup on a new PC

**Needs:** Windows 10/11, a microphone, and **Python 3.10 or newer**
(https://www.python.org/downloads/ — tick **"Add python.exe to PATH"** in the installer).

1. Copy this folder to the new PC (e.g. `D:\arc` or `C:\Xyrus`).
   You only need these files — **do not** copy `venv\`, `model\`, `arc.log`, `config.json`, `*.lnk`:
   ```
   arc.py  actions.py  ui.py  config.py  when.py  calendar_store.py  calendar_tab.py
   test_arc.py  test_songs.py  test_calendar.py
   requirements.txt  setup.ps1  install.bat  README.md
   ```
   To bring your calendar along, also copy the `data\` folder.
2. Double-click **`install.bat`**. It will:
   - create a private Python environment and install the packages
   - download the offline speech model (~40 MB, one time)
   - add Xyrus to Windows startup and put a **Xyrus** shortcut on the desktop
   - launch it
3. Say **"Xyrus, what can you do."** If it answers, you're done.

Re-running `install.bat` is safe (it skips what's already there).
To install without adding to startup: `powershell -ExecutionPolicy Bypass -File setup.ps1 -NoStartup`

**First time on a PC, Windows may ask to allow microphone access** — say yes.
If it never hears you: Settings → Privacy & security → Microphone → allow desktop apps.

### Uninstall
Quit Xyrus from the tray, delete the folder, and delete `Xyrus.lnk` from
`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup` (or untick *Start with Windows* first).

---

## Commands — say "Xyrus" first, then…

**Power**
- shut down / restart — asks "yes?" first (can be turned off in Settings), then 10 s delay; "Xyrus, cancel" aborts
- sleep · lock · screen off · wake up

**Sound & media**
- volume up / volume down / volume max / mute
- play / pause / next / previous
- **play <song name>** — finds it on YouTube and plays it: "Xyrus, play alone", "Xyrus, play shape of you by ed sheeran"
  ("play some music" → "What should I play, sir?" → say the name; needs internet; English titles work best)

**Calendar & to-dos**
- what do I have today / tomorrow / this week / next week / this weekend / this month
- what do I have in the next three days / these 3 days / the next ten days / the next two weeks (any number)
- what do I have on Friday / on the twentieth / on October third
- what's next · read my to do list
- add buy milk to my to do list [tomorrow] — a to-do (today if you don't say a day)
- remind me to call mom at five / tomorrow at nine / in twenty minutes — speaks + pops up at that time
- add dentist appointment on Friday at three pm — an event, reminder 10 min before
- schedule gym every Monday at six pm — repeating event
- add a note for Friday that the rent is due · note buy a charger
- mark buy milk as done · delete the dentist appointment · undo (removes the last thing you added by voice)
- good morning — today's plan (it also does this by itself once a day, the first time it's on after 8 am, if you have anything)

**Apps**
- open chrome / youtube / spotify / notepad / calculator / explorer / code / settings / task manager / terminal / discord
  (edit the list in the Apps tab — targets can be an .exe name, a URL, or a URI like `spotify:`)
- show desktop · close window

**Utilities**
- set a timer for five minutes / timer thirty seconds / cancel timer
- take a screenshot → `Pictures\Xyrus`
- what time is it · what day is it · system status · tell me a joke · what can you do

Just "Xyrus" → it says "Waiting for your command, sir." and listens for 15 s after it finishes speaking.

Notes
- Once the PC is truly asleep the mic is off, so "wake up" only works after "screen off", not after "sleep".
- The offline model has no entry for "Xyrus", so internally it listens for the sound-alike "cyrus" — you still just say "Xyrus".

---

## App window
Click the purple tray icon (or the Xyrus shortcut) to open it.
- **Activity** — live feed of what it heard / did / said; mic level meter; Pause button
- **Calendar** — month view; click a day for its to-dos (tick boxes), events and notes; quick-add understands
  "dentist tomorrow 3pm"; upcoming list. Everything you add by voice shows up here, and vice versa.
- **Commands** — every command and every way to say it (with a filter box)
- **Apps** — add/remove "open …" targets; Save applies instantly
- **Settings** — Start with Windows · Voice replies · Confirm before shutdown · Test voice · Open log

Closing the window keeps Xyrus running. Quit from the tray icon (right-click → Quit Xyrus).

## Files
- `arc.py` core · `actions.py` what it can do · `ui.py` window · `config.py` + `config.json` settings
- `setup.ps1` / `install.bat` installer · `requirements.txt` packages
- `when.py` spoken dates/times · `calendar_store.py` calendar + to-do data, reminders, spoken summaries · `calendar_tab.py` Calendar tab · `data\calendar.json` your calendar
- `model/` offline speech model · `venv/` Python env · `arc.log` log
- `Xyrus.lnk` opens the app window (the Startup shortcut runs `--tray`, hidden)
- `test_arc.py` self-test: `venv\Scripts\python test_arc.py [--screen]` · `test_songs.py` song playback (needs internet) · `test_calendar.py` calendar + to-dos

## Adding a voice command
1. `COMMANDS` in `arc.py`: add a key + spoken phrases
2. `Arc._run()`: handle the key (put the Windows action in `actions.py`)
3. Quit and relaunch Xyrus. The recognizer vocabulary is built from `COMMANDS` automatically.
