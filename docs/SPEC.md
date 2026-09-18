# Xyrus v2 — Final Specification

Status: FINAL, v2.0 (2026-09-13). This document is the contract for 4–5 engineers working in parallel
plus one integration task. Nobody implementing from it has seen the design conversation; if the spec
and your intuition disagree, the spec wins. If the spec is ambiguous, pick the reading that keeps the
module boundaries in §4 intact and write the decision in a code comment starting `# SPEC-AMBIGUITY:`.

Keywords: **must** = release blocker; **should** = expected in v2, may slip only with the lead's OK;
**nice** = only after everything else is green.

---

## 0. Ground rules (apply to every file)

| # | Rule |
|---|------|
| G1 | Entry point stays `D:\arc\arc.py`. The Startup shortcut runs `pythonw D:\arc\arc.py --tray`; the desktop shortcut runs it without `--tray`. All new code lives in the package `D:\arc\xyrus\`. |
| G2 | Runs under `pythonw.exe`: **never `print`** outside `tests/` and `--selftest`/`--text` modes; `sys.stdout`/`sys.stderr` are redirected at startup (§4.4 `log.py`). Every `subprocess` call goes through `xyrus.winutil.run()` / `popen()` which force `creationflags=0x08000000` (CREATE_NO_WINDOW) and `stdin=stdout=stderr=DEVNULL` unless output is captured. |
| G3 | TTS is `SAPI.SpVoice` via comtypes, owned by one thread. **Never import or use pyttsx3** (it is installed in the venv and hangs on re-init on this machine). |
| G4 | Only the Tk main thread touches Tk/customtkinter widgets. Other threads communicate with the UI **only** through `queue.Queue`s and immutable snapshots that the UI polls every 150 ms. |
| G5 | Only the recognizer thread touches Vosk `KaldiRecognizer` objects. Only the speaker thread touches the SAPI voice. COM objects (pycaw, SAPI, comtypes) are never shared across threads; every thread that uses COM calls `comtypes.CoInitialize()` first. |
| G6 | No reply string ever contains a wake alias (`cyrus`, `zeros`, `virus`, `cirrus`, or any entry of `config.wake_words`); instructions say "Say cancel to stop it", never "Say Xyrus cancel". The assistant's own name "Xyrus" may appear only in three whitelisted templates: the startup greeting ("… Xyrus online."), the "who are you" reply and the Settings "Test voice" line (all echo-gated, §5.4). Enforced by a unit test over `replies.py` and every string literal passed to `ctx.say`. |
| G7 | All JSON files are written atomically: write `<name>.tmp`, then `os.replace()`. |
| G8 | Time is never read with `time.time()`/`datetime.now()` inside `engine.py`, `dialogue.py`, `timers.py`, `reminders.py`, `assistant.py`, `persona.py`: they use the injected `Clock` (`clock.now() -> datetime`, `clock.mono() -> float`). |
| G9 | Python ≥ 3.10 syntax only (runs on 3.13.6 here). No new third-party dependency beyond §8 without the lead's OK. No `numpy`, no `requests` (use `urllib.request`), no `webrtcvad`, no `winrt`, no `pint`/`dateparser`. |
| G10 | Every user-facing string lives in `xyrus/replies.py` (engine/persona strings) or next to its command's registration (command-specific replies), never inside `actions/`. `actions/` modules return data or raise; they never speak. |

---

## 1. Goals / non-goals

### Goals
1. **Everything listed must actually work**, end to end, by voice, by typing in the window, and in headless tests. v1 feels like a toy; v2 must feel like a dependable personal assistant ("Jarvis") that addresses the user as "sir" with short, natural replies.
2. **Recognition that does not fire on room noise.** v1's `arc.log` has 219 "heard" lines of which ~180 are noise (`how` ×120, `hey` ×44, `half` ×11) because one open grammar is active all the time. v2 uses a grammar per listening state (§5).
3. **Personal-assistant layer:** calendar (events, reminders, notes per date), proactive alerts (toast + speech), briefings, "what's next", slot-filling dialogues, remember/recall.
4. **Play any song:** "Xyrus, play alone" plays "Alone" on YouTube (verified on this machine, §3.6).
5. **Testable without a microphone:** one engine entry `Engine.handle(text, source)` used by the mic, the UI text box, `--text` REPL and tests; recording fakes for all side effects.
6. **Robust always-on runtime:** no thread can die silently; mic loss recovers; second launch focuses the existing window; one-click installer that works on another PC.

### Non-goals (v2)
- No cloud speech, no LLM chat, no Whisper, no neural TTS (Piper), no neural wake word (openWakeWord/Porcupine).
- No features needing admin/UAC (Wi-Fi toggle, flush DNS), no undocumented COM (IPolicyConfig device switching, virtual-desktop internals), no WinRT (now-playing).
- No voice mouse control, unit conversion, world clock, single-letter key chords, continuous dictation.
- No confidence-score gating of recognitions (verified useless: noise → `how` at conf 0.88; OOV speech forced into grammar words at conf 1.0).
- No barge-in by voice while Xyrus is speaking (the mic is gated during TTS; stop via hotkey/UI button).
- No mobile/remote access, no multi-user.

---

## 2. Decisions that override earlier proposals (read this first)

| # | Decision | Why |
|---|----------|-----|
| D1 | Idle = **wake-only grammar**; full command grammar only after a wake or inside an open window; yes/no grammar while confirming; date grammar while asking "when"; unrestricted recognizer for free text. | Only design that removes the noise class; verified: noise that yields `how`@0.88 under the full grammar yields `''` under `["cyrus","zeros","virus","[unk]"]`. |
| D2 | One-breath commands ("Xyrus open chrome") work by **re-decoding the same utterance PCM** with the command grammar when the wake-only recognizer's final result contains a wake word. Song titles / search queries by re-decoding the same PCM with an **unrestricted** recognizer. | Re-decode verified (~0.5 s for free decode of a 3 s utterance). Simpler and more deterministic than switching grammars mid-utterance. |
| D3 | Bare wake word → spoken reply chosen at random from `config.wake_replies` (default: "Waiting for your command, sir." / "At your service, sir." / "Yes, sir?" / "I'm listening, sir."), and the 15 s command window starts **only after that reply finishes speaking**. | User requirement (overrides the UX proposal that removed spoken wake replies). A short chime on the wake partial is added as a *should* (default on). |
| D4 | Bare "Xyrus, play" / "pause" / "resume" = **media play/pause** (v1 behaviour). "play some music" / "play a song" / "play something" / "play music" → "What should I play, sir?" and the next utterance is free-decoded as the title. "play <anything else>" → YouTube. | Lead's decision (overrides earlier text). Already implemented in v1 `arc.py` (`is_song_request`, `SONG_ASK`) — port it. |
| D5 | Pause = **microphone stream closed**; nothing is captured until resume; header shows "Paused · mic off". | User requirement; v1 `listen_forever` already does it — keep semantics. |
| D6 | Hotkeys use `user32.RegisterHotKey` on a dedicated message thread, not the `keyboard` library's hooks. The `keyboard` library is used only for `keyboard.write()` (typing text; verified). Key chords are sent with `keybd_event` + real scan codes after releasing stuck modifiers (verified). | Low-level hooks drop under stalls, conflict with games; stuck-SHIFT hazard observed. |
| D7 | Snap uses `SetWindowPos` into the monitor work area with DWM border compensation (verified), never Win+Left/Right (opens Snap Assist and steals focus on Win 11). | Feasibility item 7. |
| D8 | Microphone devices are listed from **MME and DirectSound host APIs only**, persisted as `{name, hostapi}` and resolved to an index at stream open. WASAPI rejects 16 kHz (PaErrorCode -9997). | Feasibility item 14. |
| D9 | Chime/alert sounds are generated WAV bytes played with `winmm.PlaySoundW(SND_MEMORY|SND_ASYNC|SND_NODEFAULT)` from a **module-level** ctypes buffer. `winsound.PlaySound(bytes, SND_MEMORY|SND_ASYNC)` raises RuntimeError. | Feasibility item 9. |
| D10 | "Stop talking" purges SAPI on the speaker thread itself (async `Speak(text,1)` + `WaitUntilDone(50)` loop checking a `threading.Event`, then `Speak('',2)`). A cross-thread purge does not work (waits 5.4 s). | Feasibility item 6. |
| D11 | Destructive intents (shutdown, restart, sign out, close-all, force-close) are refused when the command region contains `[unk]` and always go through the confirm dialogue (unless `confirm_shutdown` is off, and even then never when `[unk]` is present). | Grammar-forced mishearings are otherwise silent disasters. |
| D12 | Timers are in-memory (not persisted) and multiple timers are allowed; calendar reminders are persisted in `data/calendar.json`. | Timers are relative and short; reminders must survive restarts. |
| D13 | Words not in the Vosk small model are never used in phrases: `unmute`, `unmuted`, `whatsapp`, `backspace`, `hotkey`, `lofi`, `vlc`, `oclock`, `xyrus`, `a.m.`, `p.m.`. Use "un mute" / "sound on", "o'clock", "am"/"pm". Verified with `Model.vosk_model_find_word`. | A word unknown to the model is silently dropped from the grammar, so the command can never fire. |
| D14 | The calendar is being built into v1 **now** (`D:\arc\when.py`, `D:\arc\calendar_store.py`, `D:\arc\calendar_tab.py`, data `D:\arc\data\calendar.json`). v2's `dates.py`, `store.py`, `reminders.py`, the spoken summaries in `assistant.py`, and `ui/calendar_tab.py` are **lifted from those files** (not from the scratchpad prototypes), and v2 must open v1's `data/calendar.json` unchanged. The calendar includes a **to-do list with tick boxes**; voice questions work for any span ("what do I have this week / these 3 days / the next N days / on Friday / rest of the week") via `parse_range`; voice adds (events, reminders, to-dos, notes) use the same full-vocabulary re-decode as songs (§5.3). Where v1's reply wording or time format ("3 pm") differs from examples in §3.9, **v1 is authoritative**. | Coordinator, 2026-09-13: the user asked for the calendar immediately and explicitly wants the to-do list and span questions. |

---

## 3. Final feature list

Notation. Phrases are written in the **registry pattern syntax** (§4.5): literal words; `[word]` = optional
word; `a|an` = alternatives for one position; `<slot>` = a typed slot (`<duration>`, `<percent>`,
`<number>`, `<app>`, `<song>`, `<query>`, `<text>`, `<when>`, `<day>`, `<expr>`, `<name>`). Every phrase
below is registered **exactly as written**, so the Commands tab shows these strings. All phrases are said
after the wake word (or inside an open window, or typed) unless the row says "no wake word".
`{sir}` in a reply renders as `, sir` (the honorific comes from `config.address_as`; blank → the fragment
is dropped). "silent" = no speech, only an Activity entry. Numbers are spelled out in these tables for readability; code passes digits to SAPI ("Volume at 40 percent, sir."), which reads them naturally, and tests assert the digit form.

"Verified" in the Implementation column cites a feasibility-report item (F1–F15, summarised in
Appendix A) or a prototype in
`C:\Users\msi\AppData\Local\Temp\claude\D--flextag\cbc35975-0734-4511-9c48-e5de63ecea1b\scratchpad\`
(written `SCRATCH\` below).

### 3.1 Listening core

| Feature | Pri | Phrases | Reply | Implementation |
|---|---|---|---|---|
| Wake word | must | "Xyrus" (model hears `cyrus`/`zeros`/`virus`/`cirrus`; list = `config.wake_words`) | — | Wake-only grammar in idle (§5.2). Typed input also accepts `xyrus`. |
| Bare wake → reply → window | must | "Xyrus" alone | random from `config.wake_replies` | Engine enters `ARMING`; on the `SpeechDone` of that reply → `ARMED` for `config.command_window_s` (15). Recognizer uses the command grammar while armed. |
| Wake chime | should | (automatic) | 200 ms two-tone chime | The recognizer plays `Chime.play("wake")` the first time a wake alias appears in `PartialResult()` of the current utterance (F5: wake visible ~2.5 s before the final). The chime is **not** echo-gated. `config.chime_on_wake` (default true). |
| One-breath command | must | "Xyrus open chrome" | per command | Re-decode the utterance PCM with the command grammar (D2, §5.3). |
| Unrecognised after wake | must | "Xyrus <garbage>" | "Sorry, I didn't catch that{sir}." then `ARMED` (15 s, starts after the reply) | Only when the rest has ≥ 1 non-`[unk]` token or ≥ 2 `[unk]`; a lone `[unk]` after the wake counts as a bare wake. If a literal phrase is close (difflib ratio ≥ 0.75) reply instead "Did you mean <phrase>?" and open a confirm dialogue; yes → run it. |
| Pause / resume listening | must | UI button, tray item, hotkey; voice: "stop listening", "pause listening" | "Pausing. Mic off{sir}." | `AudioCapture.stop()` closes the stream (D5). Resume only from UI/tray/hotkey (the mic is closed). Header "Paused · mic off". Typed commands still work while paused. |
| Stop listening for N | nice | "stop listening for <duration>" | "Mic off for ten minutes{sir}." | Pause + engine timer that resumes and says "I'm back{sir}." |
| Typed commands | must | Home tab text box, dialogue-banner box, `arc.py --text` REPL | same as voice | `Engine.submit_text(text, "typed")`; the wake word is optional. |
| Stop talking | should | hotkey Ctrl+Alt+S, header "Stop" button; "stop", "quiet", "be quiet", "stop talking" | silent | `Speaker.stop()` (D10). A voice "stop" while speaking is not heard (gated); the command also clears the say-queue and stops an alarm sound. |
| Repeat | should | "repeat", "repeat that", "say that again" | the last reply | `engine.last_reply`. |
| What did you hear | should | "what did you hear" | "I heard: <previous heard text, wake word removed>." | `engine.last_heard`. |
| What can you do | must | "what can you do", "help", "what are your commands" | "Here's everything I can do{sir}." | `ui_queue.put("show:Commands")`. |
| Follow-up window | should | no wake word; only the whitelist in §5.5 | per phrase | `FOLLOW_UP` state after briefings, what's next, alerts, recall, the shutdown countdown. |
| Mic device selection | must | Settings | — | D8. |
| Mic recovery | must | — | toast "Microphone lost — reconnecting." / "Microphone back." | §4.12 watchdog. |

### 3.2 Power

| Feature | Pri | Phrases | Reply | Implementation |
|---|---|---|---|---|
| Shut down | must | "shut down", "shutdown", "power off", "turn off the pc", "turn off the computer" | confirm "Shut down{sir}? Yes or no." → yes: "Shutting down in ten seconds{sir}. Say cancel to stop it." | Confirm dialogue (12 s) unless `confirm_shutdown` is false (never skipped when `[unk]` is present, D11). `shutdown /s /t {shutdown_delay_s}` via `winutil.run`. Then `FOLLOW_UP` for `shutdown_delay_s + 5` s with whitelist `cancel`, `stop`, `abort`. |
| Restart | must | "restart", "reboot", "restart the pc", "restart the computer" | same with "Restart"/"Restarting" | `shutdown /r /t N`. |
| Cancel shutdown | must | "cancel", "abort", "cancel shutdown", "cancel the shutdown", "stop the shutdown" | "Cancelled{sir}." | `shutdown /a`. Plain "cancel" outside any dialogue/timer context runs this (v1 behaviour). |
| Shut down in N | should | "shut down in <duration>", "restart in <duration>" | confirm "Shut down in thirty minutes{sir}? Yes or no." → "Done. Shutting down at 11:40 PM{sir}." | `shutdown /s /t secs`; remembers the fire time; "when is the shutdown" → "At 11:40 PM{sir}." |
| Sleep | must | "go to sleep", "sleep", "sleep mode" | "Going to sleep{sir}." | Speak, wait for `SpeechDone`, then `powrprof.SetSuspendState(0,1,0)` on the executor. |
| Hibernate | should | "hibernate" | "Hibernating{sir}." | `SetSuspendState(1,1,0)`; returns 0 → "Hibernate is turned off on this PC{sir}." |
| Lock | must | "lock", "lock screen", "lock the pc", "lock the computer" | "Locking{sir}." | After SpeechDone: `user32.LockWorkStation()`. |
| Sign out | should | "sign out", "log out", "log off" | confirm "Sign out{sir}? Yes or no." → "Signing out." | `user32.ExitWindowsEx(0, 0)`. Destructive. |
| Screen off | must | "screen off", "display off", "monitor off", "turn off the screen" | "Screen off{sir}." | After SpeechDone: `SendMessageW(HWND_BROADCAST, WM_SYSCOMMAND, SC_MONITORPOWER, 2)` (v1 `actions.screen_off`). |
| Screen on | must | "wake up", "screen on", "display on", "turn on the screen" | "I'm here{sir}." | v1 `actions.screen_on` (SetThreadExecutionState + 1-px mouse jiggle + SC_MONITORPOWER -1). |
| Keep awake | nice | "keep the pc awake", "stay awake" / "let it sleep" | "I'll keep it awake{sir}." / "Okay, sleep is allowed again." | A dedicated thread holds `SetThreadExecutionState(ES_CONTINUOUS|ES_SYSTEM_REQUIRED|ES_DISPLAY_REQUIRED)` until released (the flag is per-thread). |

### 3.3 Sound and media

| Feature | Pri | Phrases | Reply | Implementation |
|---|---|---|---|---|
| Volume up/down | must | "volume up", "louder", "turn it up", "volume down", "quieter", "turn it down" | silent | pycaw `AudioUtilities.GetSpeakers().EndpointVolume` (F1; import from `pycaw.utils`; the old `Activate()`+cast API raises AttributeError), ±10 %, clamped 0–100. On COMError fall back to v1 VK taps (5 × 0xAF/0xAE). |
| Volume by N | should | "volume up by <number>", "volume down by <number>" | silent | ± N %. |
| Set volume | must | "set [the] volume to <percent>", "volume <percent>", "set volume <percent>" | "Volume at forty percent{sir}." | `SetMasterVolumeLevelScalar(p/100, None)`. `<percent>` = last number 0–100 (§4.6). |
| Max volume | must | "volume max", "full volume", "max volume", "maximum volume" | silent | 100 %. |
| Query volume | must | "what's the volume", "what is the volume", "volume level" | "Volume's at forty percent{sir}." (+ " and muted" when muted) | `GetMasterVolumeLevelScalar`, `GetMute`. |
| Mute / un-mute | must | "mute", "mute the sound", "un mute", "sound on", "turn the sound on" | silent | Explicit `SetMute(1|0, None)`, never a blind toggle ("unmute" is not in the model, D13). |
| Play/pause | must | "play", "pause", "resume", "play pause", "pause the music", "resume the music", "pause it" | silent | VK_MEDIA_PLAY_PAUSE 0xB3 via `keys.tap`. Bare "play" is this (D4). |
| Next / previous | must | "next", "next track", "next song", "skip", "previous", "previous track", "previous song", "go back" | silent | VK 0xB0 / 0xB1. |
| Stop media | should | "stop the music", "stop playback" | silent | VK_MEDIA_STOP 0xB2. |
| Per-app volume | nice | "mute <app>", "set <app> volume to <percent>" | "Chrome muted{sir}." | `AudioUtilities.GetAllSessions()` → match `session.Process.name()`; `SimpleAudioVolume.SetMute/SetMasterVolume`. |


### 3.4 Display and windows

| Feature | Pri | Phrases | Reply | Implementation |
|---|---|---|---|---|
| Set brightness | should | "set [the] brightness to <percent>", "brightness <percent>" | "Brightness at fifty percent{sir}." | `screen_brightness_control.set_brightness(n)` on the executor (DDC/CI takes 100–500 ms; F2: LG ULTRAGEAR verified). `ScreenBrightnessError` → "This screen doesn't let me change brightness{sir}." Timeout 4 s. |
| Brighter / dimmer | should | "brightness up", "brighter", "brightness down", "dimmer", "dim the screen" | silent | `set_brightness('+15')` / `'-15'`; "dim the screen" = 20 %. |
| Query brightness | should | "what's the brightness", "brightness level" | "Brightness is at twenty seven percent{sir}." | `get_brightness()[0]`. |
| Show desktop | must | "show desktop", "minimize everything" | silent | Win+D (`keys.tap(VK_LWIN, 0x44)`). |
| Close focused window | must | "close window", "close this", "close this window" | silent | Alt+F4 (v1). Refuses if the foreground window belongs to Xyrus's own process (hide it instead). |
| Minimize / maximize / restore | must | "minimize", "minimise", "minimize this", "maximize", "maximise", "maximize this", "restore this", "normal size" | silent | `ShowWindow(GetForegroundWindow(), 6 / 3 / 1)` (F7; use SW_SHOWNORMAL=1 for "normal size"). |
| Snap left/right | must | "snap left", "snap right", "snap this left", "snap this right" | silent | `windows.snap(hwnd, side)` — SetWindowPos into the monitor work area with DWM border compensation (D7, F7 snippet). |
| Switch to app | must | "switch to <app>", "go to <app>", "bring up <app>" | "Chrome{sir}." / "Chrome isn't open{sir}. Want me to open it?" (confirm → open) | `windows.find_app_window(app)` → `force_foreground(hwnd)` (ALT-tap + AttachThreadInput fallback, F7). |
| Close app by name | must | "close <app>", "quit <app>", "exit <app>" | "Closing Chrome{sir}." / "Chrome isn't open{sir}." | `PostMessageW(hwnd, WM_CLOSE)` to every visible top-level window of the matching process (F7/F8). Never matches `explorer.exe`, `applicationframehost.exe`, `textinputhost.exe` or Xyrus's own PIDs (the venv pythonw launcher is a 2-process pair — exclude both via `os.getpid()` and its parent). |
| Force close app | should | "force close <app>", "kill <app>" | confirm "Force close Chrome{sir}? Unsaved work is lost. Yes or no." → "Done." | `taskkill /IM name.exe /F` (F8). Destructive. |
| Keep on top | nice | "keep this on top" / "stop keeping this on top" | "Pinned on top{sir}." | `SetWindowPos(hwnd, HWND_TOPMOST/NOTOPMOST, …, SWP_NOMOVE|SWP_NOSIZE)`. |
| What's open | should | "what's open", "what windows are open" | "Chrome, Notepad and Spotify are open{sir}." (max 6 names) | `windows.windowed_apps()` (F8). |
| Virtual desktops | nice | "new desktop", "next desktop", "previous desktop", "close this desktop" | silent | Win+Ctrl+D / Right / Left / F4 with `keys.tap` after `release_stuck_modifiers()` (F12). |
| Dark / light mode | nice | "dark mode", "light mode" | "Dark mode{sir}." | HKCU `…\Themes\Personalize` `AppsUseLightTheme`/`SystemUsesLightTheme` DWORD 0/1 + `SendMessageTimeoutW(HWND_BROADCAST, WM_SETTINGCHANGE, 0, "ImmersiveColorSet", SMTO_ABORTIFHUNG, 100)`. |

### 3.5 Apps, web, keys, clipboard

| Feature | Pri | Phrases | Reply | Implementation |
|---|---|---|---|---|
| Open app / site / settings page | must | "open <app>", "launch <app>", "start <app>" | "Opening Chrome{sir}." | `<app>` resolution order (§4.6): 1) `config.apps` aliases (exact, longest first); 2) Start Menu index fuzzy match on the free re-decode (should); 3) unknown → "I don't know how to open <name>{sir}. Add it in the Apps tab." Launch = `os.startfile(target)` for .lnk / `winutil.popen(["cmd","/c","start","",target])` otherwise (v1 `open_target`). |
| Default app list | must | — | — | v1 list + deep links: "bluetooth settings"→`ms-settings:bluetooth`, "sound settings"→`ms-settings:sound`, "display settings"→`ms-settings:display`, "network settings"→`ms-settings:network`, "windows update"→`ms-settings:windowsupdate`, "downloads"→`shell:Downloads`, "documents"→`shell:Personal`, "pictures"→`shell:My Pictures`, "recycle bin"→`shell:RecycleBinFolder`, "device manager"→`devmgmt.msc`, "control panel"→`control`. All alias words verified present in the model. |
| Start Menu index | should | "open <app>" for anything installed | as above | At startup and every 10 min on the executor: walk `%ProgramData%\…\Start Menu\Programs` and `%AppData%\…\Start Menu\Programs` `*.lnk` (skip names containing uninstall/readme/help/website/documentation). Name = stem lowercased, punctuation stripped. Not added to the grammar; matched with `difflib.get_close_matches(query, names, n=1, cutoff=0.75)` against the free re-decode of the utterance (the free trigger for `open <app>` fires only when the command-grammar tail contains `[unk]`). |
| Web search | should | "search for <query>", "google <query>", "search the web for <query>" | "Searching for cheap keyboards{sir}." | Free re-decode (§5.3). `webbrowser`-free: `winutil.open_url("https://www.google.com/search?q=" + quote_plus(q))`. The Activity entry shows the query. |
| YouTube search page | should | "search youtube for <query>", "youtube <query>" | "Here's YouTube for <query>{sir}." | Opens `https://www.youtube.com/results?search_query=…`. |
| Type text | nice | "type <text>" | silent (Activity shows the text) | Free re-decode, `keyboard.write(text, delay=0.01)` (verified under pythonw, F3/F15). Refuses when Xyrus's own window is foreground. |
| Keys / editing | should | "press enter", "press escape", "press tab", "press space", "select all", "copy", "copy that", "cut", "paste", "paste it", "undo", "redo", "save", "save this", "new tab", "close tab", "reopen tab", "refresh", "full screen" | silent | `keys.chord(...)`: Enter, Esc, Tab, Space, Ctrl+A/C/X/V/Z/Y/S/T/W, Ctrl+Shift+T, F5, F11. Sent with `keybd_event(vk, MapVirtualKeyW(vk,0), flags, 0)` after `release_stuck_modifiers()` (F12). Single-word ones ("copy", "paste", "undo", "save", "refresh") are accepted only with the wake word in the same utterance or inside `ARMED` (never in follow-up). |
| Read clipboard | should | "read the clipboard", "read my clipboard", "what's on the clipboard" | "<first 40 words>…" / "The clipboard is empty{sir}." | ctypes `OpenClipboard` retry ×5 / `GetClipboardData(CF_UNICODETEXT)` (F10). |
| Clear clipboard | nice | "clear the clipboard" | "Cleared{sir}." | `EmptyClipboard`. |
| Copy date/time | nice | "copy the date", "copy the time" | "Copied{sir}." | `clip_set` (F10). |

### 3.6 Play a song on YouTube — **must, VERIFIED on this machine** (lead, 2026-09-13)

Phrases: "play <song>", "play <song> on youtube"; ask-flow triggers "play some music", "play music",
"play a song", "play something", "play anything", "play a track"; nice: "play <song> on spotify".

Flow (port of v1 `arc.py` `is_song_request`, `words_after_play`, `clean_song_title`, `Arc.free_decode`,
`_song_title`, `_song_request`, `ask_song`, `_song_answer`, `_play_song`, `speech_finished`, and
`actions.py` `youtube_search_url`, `youtube_search`, `spoken_title`, `find_song`; tests in `D:\arc\test_songs.py`):

1. **Recognition.** The command grammar hears "xyrus play alone" as `cyrus play eleven` and most titles as
   `cyrus play [unk]`. Trigger = wake (or open window) + a play-like token + at least one more token
   (any token, including `[unk]` and number words). The recognizer then re-decodes the **same utterance
   PCM** (kept per utterance, max 20 s) with an unrestricted `KaldiRecognizer(model, 16000)` (~0.5 s) and
   attaches it as `Transcript.free_text`. Verified exact: "alone", "shape of you", "believer by imagine
   dragons", "faded by alan walker", "blinding lights", "see you again", "perfect by ed sheeran",
   "lose yourself by eminem". Weak: "despacito"→"despres cedar", "tum hi ho"→"tom hi ho" (YouTube search
   tolerates small errors).
2. **Title extraction.** In `free_text`: take the words after the first play-like token among the first 3
   words (`PLAY_LIKE = {play, plays, played, playing, lay, clay, pray}`); if none, drop a leading wake-like
   token (`cyrus zeros virus cirrus sirius serious zero zira`). Then `clean_song_title`: drop `[unk]`,
   strip prefixes `the song `, `song `, `me `, `some `; strip suffixes ` on youtube`, ` on you tube`,
   ` please`, ` for me`, ` now`, ` sir`. No `free_text` (typed input) → use the typed words after "play".
3. **Media vs song vs ask** (D4). Rest (after wake) is exactly `play` / `pause` / `resume` / `play pause` /
   `play next` / `play previous` → media command. Title ∈ `SONG_ASK = {"", "music", "some music", "a song",
   "song", "songs", "something", "anything", "a track"}` → ask flow. Otherwise → play the title.
4. **Ask flow.** Say "What should I play{sir}?"; a `song` dialogue (kind `play`, one free slot) whose window
   (`command_window_s`) starts after the question finishes speaking; recognizer mode `free`. While waiting:
   a single noise word (`NOISE_WORDS = {hey, how, half, show, you, do, the, a, to, huh, and, i, oh, uh, it}`)
   or a `SONG_ASK` value is ignored (keep waiting); `cancel`/`stop`/`no`/`never mind`/`nothing`/`forget it`
   → "Okay{sir}."; anything else → play it. Timeout → silent end.
5. **Lookup** (on the executor, timeout 6 s): GET `https://www.youtube.com/results?search_query=<q>` with a
   desktop Chrome User-Agent (`Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like
   Gecko) Chrome/128.0 Safari/537.36`) and `Accept-Language: en-US,en;q=0.9`. Regex
   `var ytInitialData = (\{.*?\});</script>` (`re.S`), walk the JSON in document order collecting
   `videoRenderer` dicts → `(videoId, "".join(title.runs[].text), "".join(ownerText.runs[].text),
   seconds(lengthText.simpleText))`. ~0.9 s per query. Pick the first result with length ≤ 20 min (skips
   mixes and live streams), else the first. If `ytInitialData` is missing, fall back to the unique
   `"videoId":"([\w-]{11})"` matches.
6. **Play.** Open `https://www.youtube.com/watch?v=<id>` with the default browser; say
   "Playing <spoken title>{sir}." where `spoken_title("Alan Walker - Alone (Official Music Video)")` =
   "Alone by Alan Walker" (drop bracketed parts, cut at ` | `, "A - B" → "B by A", max 10 words).
7. **Failures.** No results → "I couldn't find <q> on YouTube{sir}. Here's the search." + open the results
   page. Network error → "I couldn't reach YouTube{sir}. Opening the search instead." + open the results page.
8. **Spotify (nice).** Trailing ` on spotify` → open `spotify:search:<quote(q)>`; "Searching Spotify for <q>{sir}."
9. **Config.** `youtube_lookup` (default true); false → always open the results page ("Here's YouTube for <q>{sir}.").

### 3.7 Info and tools

| Feature | Pri | Phrases | Reply | Implementation |
|---|---|---|---|---|
| Time | must | "what time is it", "what's the time", "what is the time", "the time", "time" | "It's 4:32 PM{sir}." | `persona.speak_time`. |
| Date | must | "what day is it", "what's the date", "what is the date", "the date", "what's today" | "Today is Sunday, September 13{sir}." | |
| System status | must | "system status", "status", "system report" | "CPU at 12 percent, memory at 48 percent, no battery, up 3 hours 10 minutes{sir}." | psutil; `cpu_percent(interval=0.5)` on the executor. "How are you" moved to small talk. |
| Battery | should | "battery", "battery level", "how much battery" | "Battery at 80 percent, charging{sir}." / "There's no battery, this is a desktop{sir}." | `psutil.sensors_battery()`. |
| Disk space | should | "disk space", "how much space is left" | "C has 120 gigabytes free of 480{sir}." (+ "It's nearly full." above 90 %) | `psutil.disk_usage` per fixed partition, max 3 drives. |
| What's using the CPU | nice | "what's using the cpu" | "Chrome, Code and Discord{sir}." | `process_iter` with a 1 s sample. |
| Screenshot | must | "take a screenshot", "screenshot", "take a picture of the screen" | "Screenshot saved{sir}." | `ImageGrab.grab(all_screens=True)` → `Pictures\Xyrus\Screenshot_YYYY-MM-DD_HH-MM-SS.png`; Activity info row with the path (clickable). |
| Screenshot window | nice | "screenshot this window" | same | bbox from `GetWindowRect(GetForegroundWindow())`; call `shcore.SetProcessDpiAwareness(2)` at startup. |
| Open last screenshot | should | "open the last screenshot", "show the screenshot" | silent | newest file in `Pictures\Xyrus`. |
| Calculator | should | "what is <expr>", "what's <expr>", "calculate <expr>" | "Forty eight{sir}." (number spoken as digits, SAPI reads them) | `parsing.parse_expr` + recursive-descent evaluator (no `eval`): plus, minus, times, multiplied by, divided by, over, percent of, squared, square root of; numbers up to millions, "point". "for" after an operator = 4 (F5: "twelve times for"). Division by zero → "That's undefined{sir}." Must not capture "what is the time/date/volume/brightness" — those literal phrases are more specific (§4.5). |
| Joke | must | "tell me a joke", "joke", "tell me another" | one of ≥ 20 jokes, never the previous one | `replies.JOKES`. |
| Coin / dice / pick | should | "flip a coin", "heads or tails", "roll a die", "roll a dice", "roll two dice", "pick a number between <number> and <number>" | "Heads." / "You rolled a four." / "Seven." | `random`. |
| Weather (opt-in, online) | nice | "what's the weather", "will it rain today" | "Twenty eight degrees and clear in Dhaka{sir}." / off: "Weather needs the internet — turn it on in Settings{sir}." | `wttr.in/<city>?format=j1`, UA `curl/8.0`, **timeout 8 s** (3.0 s observed, F11), 15-min cache, executor only. |


### 3.8 Timers (must)

| Feature | Pri | Phrases | Reply | Implementation |
|---|---|---|---|---|
| Set timer | must | "set a timer for <duration>", "set timer for <duration>", "set timer <duration>", "timer for <duration>", "timer <duration>", "start a timer for <duration>", "<duration> timer" | "Timer set for seven minutes{sir}." (always echoes the parsed value) | `parsing.parse_duration` (port of v1, last number before the unit wins): "seven minutes"→420, "twenty five seconds"→25, "half an hour"→1800, "one and a half hours"→5400, "a minute"→60, "four seven minutes"→420 (for/four), "set timer four ten minutes"→600, "ninety seconds"→90, "two hours"→7200, "four can minutes"→600 (`can`→`ten` before a unit, §5.6). |
| Missing duration | must | "set a timer", "timer", "start a timer" | "For how long{sir}?" | Dialogue kind `timer`, one slot `duration`, recognizer mode `when`; answer "five minutes" → set. Noise words ignored; cancel words end it. |
| Multiple / named timers | should | "<name> timer <duration>", "set a <name> timer for <duration>" (`<name>` ∈ tea, pasta, coffee, pizza, egg, laundry, oven, break, work — all in the model) | "Tea timer set for three minutes{sir}." | `timers.TimerService` (in-memory, engine-thread, fake-clock driven). Unnamed timers are named by their duration ("the five minute timer"). |
| Time left | must | "how long is left", "how much time is left", "time left", "how long on the timer" | "Three minutes forty left{sir}." / multiple: "Tea: three minutes. Pizza: ten minutes." / none: "No timers running{sir}." | |
| Add time | should | "add <duration>", "add <duration> to the timer" | "Added five minutes{sir}." | applies to the soonest timer. |
| Cancel timer | must | "cancel timer", "stop timer", "cancel the timer", "stop the timer", "cancel all timers", "cancel the <name> timer" — **no wake word needed while any timer runs or rings** | "Timer cancelled{sir}." / "There's no timer running{sir}." | Idle grammar gets `cancel stop timer timers the all` + timer names while a timer exists (§5.2). |
| Timer done | must | — | "Time's up{sir}!" (force-spoken even if voice replies are off) | `actions.screen_on()`, `Chime.play("alert")` repeated every 3 s until acknowledged or 60 s, toast "Timer done — 7 minutes", Activity `alert`. `FOLLOW_UP` 60 s with whitelist `stop`, `okay`, `thanks`, `thank you`, `got it`, `snooze` ("snooze" = +5 min). |

### 3.9 Calendar, reminders, notes, personal assistant (must unless marked)

Behaviour below merges the two assistant/calendar designs with the v1 calendar being built now (D14).
Lift sources: `D:\arc\when.py` (the prototype parser + `fmt_time`, `day_name`, `day_label`, `mentions_date`,
`parse_range`), `D:\arc\calendar_store.py` (store with events, to-dos and notes, `undo_last`, fuzzy `find`,
`counts_by_day`, `next_item`, `due_reminders`/`reminder_text`, spoken `describe_range`/`describe_next`/
`describe_todos`/`briefing`), `D:\arc\calendar_tab.py` (Calendar tab), and `SCRATCH\dialogue_proto.py`
(slot-filling state machine, 9 scenarios). `SCRATCH\when_proto.py` and `SCRATCH\sched_proto.py` are superseded
by the v1 files (their test tables still apply). §4 says exactly how.

**Persona rules** (`persona.py`, must): honorific once per reply, at the end of the first clause or the end
("Done, sir."); questions carry none except the nudge "Still there, sir?"; acks ≤ 2 sentences, briefings
≤ 4, questions exactly 1; confirmations echo the parsed data ("Dentist, tomorrow at 3 PM. Correct?");
`pick(key)` chooses among 2–4 variants and never repeats the previous choice for that key; no "!" except
"Time's up!"; not-heard line is "Sorry, I didn't catch that{sir}."; times as a person says them
(`speak_time`: "3 PM", "9:30 AM", "noon", "midnight"); `speak_when(start, all_day, now)` = shortest true
form ("today", "tomorrow", "Friday" within 6 days, "Friday the 20th", "October 3rd"), plus relative form when
< 12 h away ("at 3 PM, in two hours"; minutes rounded to 5 above 20 min). Name (`config.user_name`) only in
greetings.

| Feature | Pri | Phrases (display) | Reply | Implementation |
|---|---|---|---|---|
| Add event (dialogue) | must | "add an event", "add an event <when>", "new event", "create an event", "schedule <text>", "put <text> on my calendar", "add a meeting <when>", "add an appointment <when>" | TITLE "What's the event?" → WHEN "When?" → TIME "What time?" → CONFIRM "Dentist, tomorrow at 3 PM. Correct?" → yes: "Done{sir}. I'll remind you ten minutes before." | **Keyword matcher** (mishearings "add in event", "add and event", "add a event"): (`add`|`new`|`create`|`schedule`|`put`) + (`event`|`meeting`|`appointment`|`calendar`). Title/date/time are parsed from `free_text` (full-vocabulary re-decode, same as songs, §5.3) with `dates.parse_when` (leftover words = title). A complete utterance is saved at once with a read-back ("Added dentist, tomorrow at 3 pm{sir}."; "undo that" removes it); the dialogue only asks for what is missing. Dialogue per `dialogue_proto.py` (§4.9): TITLE in `free` mode, WHEN/TIME in `when` mode, CONFIRM/FIX in `confirm` mode; "no" → FIX "What should I change: the title, the date, or the time?"; all-day via "all day"/"no time"/"any time"; duplicate guard "That's already on your calendar{sir}." |
| Quick reminder | must | "remind me <when> to <text>", "remind me to <text> <when>", "remind me <when>", "remind me to <text>", "remind me", "set a reminder", "set a reminder for <when>" | one-shot when the utterance's full-vocabulary re-decode yields both a title and a time: "I'll remind you at 5 PM to call mom{sir}."; else dialogue "What should I remind you about?" / "When?" + CONFIRM | Keyword matcher: word stem `remind`/`reminder`. Title/time from `free_text` (§5.3) via `dates.parse_when`. Saved with `store.add_event(title, date, time, reminder_min=0, source="voice")` (a reminder is an event whose reminder fires at its time). "undo that" removes it. `remind me in twenty minutes to check the oven` → 10:50. Past absolute date → "That's in the past{sir}. Add it anyway?" |
| Notes by date | must | "add a note", "add a note for <day>", "take a note", "make a note", "note for <day>", "note that <text>" | "What's the note?" (free) → "Noted for tomorrow{sir}: bring the charger." | Saved without confirm (editable in UI); "delete that note" within 60 s → "Deleted{sir}." After the word `note`, `for`/`four`/`on` are stripped before `parse_when` (verified mishearing "add note four tomorrow"). Default day = today. |
| Read notes | must | "read my notes", "read my notes for <day>", "what are my notes", "what are my notes for <day>", "any notes for <day>", "notes for <day>" | "Two notes for today{sir}. One: buy milk. Two: call the bank." / "No notes for tomorrow{sir}." | max 5 spoken, then "and three more in the calendar." + `show:Calendar`. |
| Clear notes | must | "clear my notes for <day>", "clear today's notes", "delete my notes for <day>" | confirm "Clear three notes for today{sir}? Yes or no." → "Cleared." | |
| Agenda | must | "what's on <day>", "what's on today", "what do i have <day>", "what do i have today", "my schedule", "my schedule for <day>", "anything on <day>", "what's coming up", "agenda", "what have i got <day>", "anything <day>" — `<day>` is any span: "today", "tomorrow", "on friday", "this week", "next week", "these three days", "the next three days", "the next ten days", "next two weeks", "rest of the week", "rest of the month", "this weekend", "on the twentieth" | v1 `describe_range` wording, e.g. "Today you have 2 events and 1 to-do, sir: standup at 9:30 am, dentist at 3 pm and pay rent." / "You have nothing planned in the next 3 days, sir. The next thing is gym, Saturday." / span: "This week you have 3 events, sir. Monday: dentist at 3 pm. Friday: team meeting at 10 am and gym at 6 pm." | Span = `dates.parse_range(text, now)` (v1, verbatim); default today; "what's coming up" = next 7 days. Text = `assistant.describe_range(store, start, end, label, now)` (lift v1): events and open to-dos by day, max 12 read, then "And N more. They're on the Calendar tab." + `show:Calendar`; overdue to-dos when the span includes today; notes. Arms follow-up (`read the notes`). |
| What's next | must | "what's next", "next event", "what's my next event", "when is my next event", "anything coming up" | "Next is dentist at 3 PM, in two hours{sir}." / "Nothing scheduled{sir}. Your calendar's clear." | overdue unacknowledged reminder first ("You still have a reminder: call the bank, from 20 minutes ago."). Follow-up "and after that"/"what's after that"/"next one" → "Then gym at 7 PM." Bare "next" stays media next track. |
| Delete event | must | "delete the <text> event", "delete <text>", "delete the next event", "delete <day>'s event", "cancel the <text> event", "cancel the <text> appointment", "cancel the <text> meeting", "cancel the <text> reminder" | one match: confirm "Delete dentist, tomorrow at 3 PM{sir}?" → "Deleted." ; several: "Which one{sir}? First: dentist at 3 PM. Second: gym at 6 PM." → "the second" → confirm | `store.find(text, kinds=("event", "task"))` (v1 fuzzy title match, cutoff 0.55). Plain "cancel" keeps its power meaning (needs a calendar keyword). Recurring: "Delete gym every Monday? This removes all of them." |
| Show calendar | must | "show my calendar", "open my calendar", "show the calendar" | "Here you are{sir}." | `show:Calendar`. |
| Add a to-do | must | "add a to do <text>", "add <text> to my to do list", "add a task <text>", "new to do <text>", "put <text> on my to do list", "add a to do" | "Added to your to-do list{sir}: buy milk." (+ day when not today: "…, due tomorrow.") / no title → "What's the to-do?" (free) | Keyword matcher: (`add`|`new`|`put`) + (`to do`|`task`); title/date/time from `free_text` (§5.3) via `parse_when` (leftover = title; default today). `store.add_task(title, date, time, reminder_min=0 if time else None, source="voice")`. "undo that" removes it. |
| Read the to-do list | must | "what's on my to do list", "what are my to dos", "read my to do list", "my to do list", "what do i need to do" | v1 `describe_todos`: "You have 3 to-dos, sir. Overdue: … Today: … Later: …" / "Your to-do list is empty, sir." | lift `describe_todos` into `assistant.py`. |
| Tick off a to-do | must | "mark <text> as done", "mark <text> done", "tick off <text>", "cross off <text>", "i finished <text>", "<text> is done" | "Ticked off: buy milk{sir}." / "I can't find that on your to-do list{sir}." | `store.find(text, kinds=("task",), only_open=True)` → `store.set_done(id, True)` (the Calendar tab tick boxes call the same method). nice: "mark <text> as not done". |
| Undo a voice add | must | "undo that", "scratch that", "delete that", "remove that" | "Removed dentist{sir}." / "There's nothing to undo{sir}." | `store.undo_last()` (voice adds only, newest first, this session). For 15 s after any voice add these phrases work without the wake word (follow-up kind `added`, §5.5). |
| Proactive alerts | must | — | v1 `reminder_text`: "Sir, dentist is at 3 pm, in 10 minutes." (reminder_min > 0) / "Sir, reminder: call mom at 5 pm." (at the time; all-day items at 09:00) | `reminders.due_alerts(store, now)` every 5 s in `Engine.tick` (§4.10); one reminder per occurrence (v1 model), done to-dos never remind. Screen on, alert chime, toast, force-spoken, Activity `alert`. Follow-up 20 s: "okay"/"got it"/"thanks"/"done"/"dismiss" → "Noted{sir}." (acknowledged); "snooze" → "I'll remind you again in ten minutes{sir}."; "remind me again in <duration>". Unacknowledged due alert repeats once after 2 min ("Dentist — still pending{sir}.") (should). Alerts wait while a dialogue is active. Quiet hours skip lead alerts only. |
| Missed while away | must | — | once, in the startup greeting or before the next reply: "While I was away{sir}: dentist was at 3 PM, and standup at 9:30." | late (≤ `catch_up_min` 120) spoken; stale → Activity only (v1 `due_reminders` rules). |
| Recurring events | should | "… every day", "… every <weekday>", "… every week", "daily", "weekly" | "Standup, every day at 9:30 AM. Correct?" | `repeat: daily|weekly` (parser + store occurrences already in the prototypes). |
| Startup greeting | must | — | "Good evening, Refath. Xyrus online. Two things today; next is gym at 7 PM." | morning 05–12, afternoon 12–17, evening otherwise. `config.startup_greeting`: `speak` (default) / `toast` / `off`; spoken after the mic reports listening. Missed alerts first (max 2 named). Then `FOLLOW_UP` 20 s. |
| Briefing | must | "good morning", "good afternoon", "good evening", "brief me", "briefing", "daily briefing", "what's my day like" | "Good morning{sir}. It's 8:15. You have two things today: standup at 9, and dentist at 3 PM. And you left a note for today — want to hear it?" | Part of day from the clock, not the words. Order: greeting; time; weather (only if cached, nice); today's items (none → "Nothing on the calendar today."; ≤ 3 listed; > 3 "You have five things today, starting with standup at 9."); overdue reminders; note offer (arms follow-up; "yes"/"read it" reads notes). Hard cap 4 sentences (drop time, then weather). |
| Auto morning brief | should | — | same as briefing | once a day at `calendar.morning_brief_time` (08:30) if the PC is on and there is ≥ 1 event or note; not after 11:00; `meta.last_brief` in calendar.json. |
| Good night | must | "good night", "goodnight", "going to bed" | "Good night{sir}. Tomorrow starts with standup at 9 AM." / "…Nothing scheduled tomorrow." | ends follow-up. |
| Remember | must | "remember that", "remember this", "remember", "keep in mind" | "What should I remember?" (free) → "Got it: 'the wifi password is banana seven'. Correct?" → "Remembered{sir}." | Keyword: stem `remember`/`remembered` without recall words (verified mishearing "remembered at"). `data/memory.json`. |
| Recall | must | "what did i ask you to remember", "what do you remember", "read my memories", "what did i tell you" | "You asked me to remember two things. One: … Two: …" (3 at a time; follow-up "more") / "Nothing yet{sir}. Say 'remember that' and I'll keep it." | Keyword: `remember`/`memories` + any of what/did/tell/list/read/recall ("what did are ask" normalised, §5.6). |
| Forget | should | "forget that", "forget the last one", "forget everything" | confirm "Forget 'the car is on level three'? Yes or no." → "Forgotten{sir}." | |
| Small talk | must | "thank you", "thanks", "thanks a lot" / "who are you", "what's your name", "what are you" / "how are you", "how are you doing" / "hello", "hi" / "are you there", "you there" | "Anytime{sir}." / "My pleasure{sir}." — "I'm Xyrus{sir}. Your offline assistant — nothing I hear leaves this PC." — "Running smoothly{sir}. CPU at 12 percent." — "Hello{sir}." (+ opens `ARMED` like a bare wake) — "Always{sir}." | "how are you" is no longer system status (behaviour change; README). "hey" is **not** a small-talk phrase (top noise word). |
| Your name | should | Settings; "what's my name"; nice: "call me" (free) | "You're Refath{sir}." / "You haven't told me yet{sir}. It's in Settings." | `config.user_name`, `config.address_as`. |

### 3.10 Custom commands and routines (must)

- Stored in `config.custom_commands` (schema §6). Each has a phrase and ordered steps. Step actions:
  `open` (target like the Apps list), `run` (command line → `winutil.popen(arg, shell=True)`), `keys`
  (chord string `ctrl+shift+t`, parsed by `keys.parse_chord`), `type` (text via `keyboard.write`), `say`
  (text), `command` (text re-entered via `engine.handle(text, "routine")`, max nesting depth 3), `wait`
  (seconds, ≤ 30). Steps run sequentially on the executor; `say`/`command` steps are posted back to the
  engine thread; overall timeout 60 s; a failing step logs, says "Step 2 of work mode failed{sir}." and stops.
- Default reply when a routine has no `say` step: "Done{sir}."
- Registered into the registry at startup and on every config change (section "Your commands"), so they
  appear in the Commands tab and the grammar. Grammar is rebuilt live (no stream restart).
- Validation on save (UI and loader): phrase must be ≥ 2 words or 1 word ≥ 5 letters; every word must be in
  the model vocabulary (`Vocab.unknown_words(phrase)`, §4.12) — else the UI shows
  "The speech model doesn't know 'kubernetes' — pick another word."; must not equal or contain a built-in
  literal phrase ("That's already a built-in command."); no wake words.
- Shipped templates (in the Routines tab "Add template" menu, not active by default): "movie mode"
  (brightness 40 → volume 60 → open netflix → wait 5 → keys f11), "work mode" (open code, open terminal,
  open chrome), "focus mode" (mute → say "Focus mode on, sir.").

### 3.11 App shell and system

| Feature | Pri | Behaviour | Implementation |
|---|---|---|---|
| Start with Windows toggle | must | Settings switch + tray check item | v1 `set_autostart` (WScript.Shell shortcut in `%APPDATA%\…\Startup\Xyrus.lnk` targeting venv `pythonw.exe "D:\arc\arc.py" --tray`) moved to `xyrus/autostart.py`; also verifies the existing .lnk points at this folder. |
| Single instance, second launch focuses | must | Double-clicking the shortcut while running shows the window (no message box) | Mutex `Local\XyrusVoiceAssistant` + auto-reset event `Local\XyrusShowWindow` (F13); second instance `OpenEventW`+`SetEvent` then exits 0; first polls `WaitForSingleObject(h, 0)` in the Tk poll. |
| Tray | must | Icon states: purple listening, green ring armed/follow-up/dialogue, grey paused, red mic error. Left click opens. Menu: Open Xyrus · Pause/Resume listening · Mute replies · Start with Windows ✓ · Quit Xyrus. Tooltip "Xyrus — next: dentist at 3 PM". | pystray `run_detached`; `icon.notify(body, "Xyrus")` for toasts (F4; any thread; ~1 s after run_detached). |
| Global hotkeys | should | Ctrl+Alt+X show window; Ctrl+Alt+Space push-to-talk (chime → `ARMED` 8 s, no spoken reply); Ctrl+Alt+S stop speaking | D6; conflicts (RegisterHotKey returns 0) shown in Settings. |
| Window | must | tabs Home · Calendar · Commands · Apps · Routines · Settings (§4.15) | customtkinter, v1 palette. Closing hides to tray. |
| Onboarding | nice | first-run wizard: mic pick + live meter, voice, start with Windows | never auto-adds wake tokens (free mode hears xyrus as "zero"/"zira", which collide with numbers/voice names). |
| High-accuracy model | nice | Settings button downloads `vosk-model-en-us-0.22-lgraph` | `urllib.request` + `zipfile` on a worker, swap on the recognizer thread. |
| Logging | must | `D:\arc\arc.log` rotating 1 MB × 3; crash dumps `crash.log` | §4.2. |
| CLI | must | `arc.py` (window), `--tray`, `--text` (stdin REPL, python.exe only, prints replies, TTS off unless `--speak`), `--selftest` (runs the headless test suite, exit code), `--dry-run` (FakeActions) | §4.15 `app.py`. |

### 3.12 Cut (do not build)

Confidence-threshold noise gates (probe: noise → `how`@0.88; `set the volume to forty percent` →
`set the volume to forty for set` all conf ≈ 1.0). Keeping the full grammar active while idle. WASAPI mic
entries (reject 16 kHz). Win+arrow snapping. `winsound` SND_MEMORY|SND_ASYNC. Cross-thread SAPI purge.
pyttsx3. Piper TTS. openWakeWord/Porcupine. Whisper / local LLM. WinRT now-playing. IPolicyConfig output
switching. Wi-Fi/DNS toggles (admin). Mic mute (mutes Xyrus's own input). Voice mouse control, unit
conversion, world clock, single-letter chords, continuous dictation, voice-taught custom commands,
fast-path execution on partial results, media-aware wake defence, fuzzy "search my memories",
"restart explorer", "close all windows". Weather is kept only as an opt-in nice.


---

## 4. Module layout

### 4.1 Tree

```
D:\arc\
  arc.py                    entry shim (G1): `from xyrus.app import main; main()`  (integration task)
  requirements.txt  setup.ps1  install.bat  README.md  xyrus.ico (generated)
  model\                    vosk-model-small-en-us-0.15 (not copied between PCs)
  data\                     runtime data, created on first save: calendar.json, memory.json, stdout.log
  config.json               settings (created on first save)
  arc.log, crash.log        logs
  legacy\v1\                v1 arc.py, actions.py, ui.py, config.py, when.py, calendar_store.py, calendar_tab.py, test_arc.py, test_songs.py (moved by integration; D:\arc is not a git repo, so never delete)
  docs\SPEC.md              this file
  tests\                    unittest suites (§7)
  xyrus\
    __init__.py             version = "2.0.0"; nothing else
    interfaces.py           shared dataclasses + Protocols (§4.3, verbatim)
    paths.py                BASE, MODEL_DIR, DATA_DIR, CONFIG_FILE, LOG_FILE, CRASH_FILE, ICON_FILE, SCREENSHOT_DIR (XYRUS_DATA_DIR env overrides DATA_DIR and CONFIG_FILE for tests)
    config.py               Config class, DEFAULTS, migration, atomic save, change callbacks
    log.py                  logging setup, excepthooks, faulthandler, stdout redirect
    clock.py                SystemClock, FakeClock
    winutil.py              run/popen/open_url (CREATE_NO_WINDOW, DEVNULL), force_foreground(hwnd)
    replies.py              every engine/persona string + JOKES + WAKE_REPLIES defaults
    persona.py              pick(), sir(), speak_time(), speak_when(), greeting(), number words
    normalize.py            homophone/sound-alike normalisation (§5.6), strip_wake()
    parsing.py              numbers, durations, percent, expressions, slot types (§4.6)
    dates.py                spoken date/time parser (lift D:\arc\when.py verbatim)
    registry.py             Pattern, Command, Registry, @command decorator, Commands-tab sections
    engine.py               Engine state machine, handle(), tick(), snapshot()
    dialogue.py             Dialogue slot-filling machine (lift SCRATCH\dialogue_proto.py) + Confirm/Ask kinds
    executor.py             Executor (worker thread, timeouts) + InlineExecutor
    timers.py               TimerService (in-memory, clock driven)
    store.py                CalendarStore (lift D:\arc\calendar_store.py) — events, to-dos, notes, undo
    reminders.py            due_alerts() + alert texts (lift due_reminders/reminder_text from D:\arc\calendar_store.py)
    memory.py               MemoryStore (data\memory.json)
    assistant.py            describe_range/describe_next/describe_todos/briefing (lift from D:\arc\calendar_store.py) + startup_summary, good_night
    grammar.py              GrammarBuilder: word sets per listen mode, model vocabulary check
    audio.py                AudioCapture (sounddevice), device listing/resolution, level, watchdog
    recognizer.py           Recognizer thread: modes, echo gate, utterance PCM, re-decode, Transcript
    speech.py               Speaker (SAPI thread) + Chime (winmm PlaySoundW)
    hotkeys.py              RegisterHotKey message thread
    tray.py                 pystray icon, state images, menu, Notifier
    single_instance.py      mutex + show event
    autostart.py            Startup shortcut create/remove/check
    testing.py              FakeActions, FakeSpeaker, FakeChime, FakeNotifier, make_test_engine()
    app.py                  composition root: argparse, wiring, thread start/stop, main()
    actions\
      __init__.py           WinActions (implements SystemActions by delegating to the modules below)
      keys.py               tap(), chord(), parse_chord(), release_stuck_modifiers(), type_text()
      power.py              shutdown/restart/abort/sleep/hibernate/lock/sign_out/screen_off/screen_on/keep_awake
      audio.py              volume get/set/step, mute get/set, app sessions (pycaw)
      display.py            brightness get/set, theme
      windows.py            list_windows, windowed_apps, find_app_window, force_foreground, min/max/restore, snap, close_app, kill_app, topmost
      apps.py               open_target, StartMenuIndex
      sysinfo.py            status dict, battery, disks, top processes, time/date helpers
      web.py                youtube_search, spoken_title, find_song, youtube_search_url, google_url, weather
      clipboard.py          clip_get, clip_set, clip_clear
      media.py              play_pause, next, prev, stop (VK media keys via keys.tap)
      screenshot.py         screenshot(window=False) -> Path, last_screenshot()
    commands\
      __init__.py           load_all(registry): imports every command module (registration side effects)
      core.py               wake-less helpers: help, repeat, what did you hear, stop talking, pause listening, small talk
      power.py  sound.py  display.py  windows.py  apps.py  info.py  web.py (play/search/type)  keys.py  clipboard.py  timers.py  fun.py
      calendar.py           add event / reminder / notes / agenda / what's next / delete / show calendar
      assistant.py          briefing / good night / remember / recall / forget / name
      custom.py             registers config.custom_commands, routine runner
    ui\
      __init__.py
      theme.py              palette (v1: ACCENT #7c5cff, ACCENT_HOVER #6a4ce6, GREEN #3ddc84, RED #ff5c5c, AMBER #ffb347, MUTED #9a9aa8, CARD #1b1b22, BG #121216), fonts
      window.py             XyrusWindow: header, dialogue banner, tabs, 150 ms poll
      home_tab.py  calendar_tab.py  commands_tab.py  apps_tab.py  routines_tab.py  settings_tab.py
      event_dialog.py       EventDialog (CTkToplevel)
```

### 4.2 Threads (who runs what)

| Thread | Owner module | Runs |
|---|---|---|
| T-main (Tk) | `ui/window.py` | mainloop, 150 ms `_poll`: drain `ui_queue`, read `engine.snapshot()`, `capture.level()`, show-event; user actions call thread-safe methods only (`engine.submit_text`, `engine.cancel_dialogue`, `config.set`, `store.*`, `speaker.stop`). |
| T-audio-cb | PortAudio | `AudioCapture` callback: `put_nowait((bytes, time.monotonic()))` + peak; nothing else. |
| T-rec | `recognizer.py` | the only Vosk user: chunk loop, echo gate, decode, re-decode, chime-on-partial, posts `Transcript`s to the engine. |
| T-engine | `engine.py` | single consumer of the engine inbox; `handle`, dialogues, timers, reminders tick (every 250 ms from `inbox.get(timeout=0.25)`), command handlers. Never blocks > 50 ms. |
| T-exec | `executor.py` | runs `ctx.do(...)` jobs (all `SystemActions` calls, YouTube lookup, psutil samples, brightness). `CoInitialize()` at start. |
| T-speak | `speech.py` | SAPI owner; say-queue; async speak + purge; on_done callbacks. |
| T-hotkey | `hotkeys.py` | RegisterHotKey + GetMessageW loop. |
| T-watch | `audio.py` | mic liveness watchdog (1 s period). |
| T-tray | pystray | menu callbacks (only enqueue / call thread-safe methods). |
| T-keepawake | `actions/power.py` | nice: holds execution state. |

### 4.3 `xyrus/interfaces.py` (verbatim — task T1 commits this file first; nobody else edits it)

```python
"""Shared types. Every module codes against these. Changing this file requires the lead's OK."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal, Protocol, runtime_checkable

Source = Literal["mic", "typed", "test", "hotkey", "routine", "alert", "ipc"]
ListenMode = Literal["wake", "command", "confirm", "when", "free", "paused"]
EngineMode = Literal["idle", "arming", "armed", "follow_up", "dialogue"]
EventKind = Literal["heard", "noise", "cmd", "say", "ask", "alert", "remind", "info", "error"]
ChimeKind = Literal["wake", "alert", "error"]


@dataclass(frozen=True)
class Word:
    text: str
    start: float          # seconds from utterance start
    end: float
    conf: float           # logged only, never used as a gate (§1 non-goals)


@dataclass(frozen=True)
class Transcript:
    text: str                         # grammar result, normalised (§5.6), may contain "[unk]"
    mode: ListenMode                  # mode whose grammar produced `text` ("command" after a wake re-decode)
    source: Source = "mic"
    free_text: str | None = None      # unrestricted re-decode of the same PCM (§5.3), lowercase
    words: tuple[Word, ...] = ()
    peak: float = 0.0                 # max chunk peak 0..1 over the utterance
    t_start: float = 0.0              # time.monotonic() of first chunk
    t_end: float = 0.0
    wake_redecoded: bool = False      # True if produced by wake -> command re-decode


@dataclass(frozen=True)
class ListenSpec:
    """What the recognizer should listen for next. Published by the engine, read by T-rec."""
    mode: ListenMode
    extra_words: tuple[str, ...] = ()  # context words added to the wake/confirm/when grammars
    generation: int = 0                # bumps on every change; T-rec re-reads when it differs


@dataclass(frozen=True)
class Event:
    ts: str                            # "HH:MM:SS"
    kind: EventKind
    text: str
    meta: str = ""                     # e.g. screenshot path, "noise"


@dataclass(frozen=True)
class DialogueView:
    kind: str                          # event|reminder|task|note|memory|forget|name|confirm|timer|play|pick
    state: str                         # title|when|time|confirm|fix|pick|free|parked
    question: str
    hint: str
    slots: dict[str, str]              # display strings: {"title": "Dentist", "date": "tomorrow", "time": "3 PM"}
    seconds_left: int                  # 0 until the question finished speaking
    listening: ListenMode
    parked: bool = False


@dataclass(frozen=True)
class TimerView:
    name: str                          # "tea" or "5 minute"
    remaining_s: int
    ringing: bool = False


@dataclass(frozen=True)
class EngineSnapshot:
    mode: EngineMode
    listen: ListenMode
    paused: bool
    status: str                        # "listening" | "paused" | "mic error, retrying" | "starting" (set by app)
    window_left_s: int                 # armed / follow-up seconds left (0 otherwise)
    speaking: bool
    dialogue: DialogueView | None
    timers: tuple[TimerView, ...]
    next_event: str | None             # "Dentist at 3 PM · in 2 h"
    last_reply: str
    events: tuple[Event, ...]          # newest last, max 300
    missed: int
    store_version: int                 # CalendarStore.version, for UI refresh


@runtime_checkable
class Clock(Protocol):
    def now(self) -> dt.datetime: ...          # local naive wall clock
    def mono(self) -> float: ...               # monotonic seconds


class Speaker(Protocol):
    def say(self, text: str, *, priority: int = 1, force: bool = False,
            on_done: Callable[[], None] | None = None) -> None:
        """Queue text. priority 0 = alerts (spoken before queued priority-1 replies, never interrupts).
        force=True speaks even when config.voice_replies is False. on_done is called exactly once, from
        T-speak, when the text finished, was purged, or was skipped (voice off) — immediately in that case."""
    def stop(self) -> None: ...                # purge current + queued (D10)
    def is_speaking(self) -> bool: ...
    def is_quiet_at(self, t_mono: float) -> bool:
        """False if t_mono falls inside [start-0.05, end+0.35] of any of the last 8 speech intervals
        (the open interval has end = +inf)."""
    def voices(self) -> list[str]: ...         # SAPI descriptions, e.g. "Microsoft Zira Desktop"
    def set_voice(self, name: str | None) -> None: ...
    def set_rate(self, rate: int) -> None: ... # -10..10


class Chime(Protocol):
    def play(self, kind: ChimeKind) -> None: ...   # async, returns in ~1 ms, safe from any thread
    def stop(self) -> None: ...


class Notifier(Protocol):
    def notify(self, title: str, body: str) -> None: ...  # toast; safe from any thread; never raises


class Executor(Protocol):
    def submit(self, fn: Callable[[], Any], *, timeout: float = 15.0, name: str = "",
               on_done: Callable[[Any, BaseException | None], None] | None = None) -> None:
        """Run fn on T-exec. on_done(result, error) is called on T-exec (the engine wraps it to post
        back to T-engine). Timeout -> on_done(None, TimeoutError(name)); the stuck worker is abandoned."""


class SystemActions(Protocol):
    """Every side effect on Windows. WinActions = real; testing.FakeActions records calls.
    Methods may block (run them via ctx.do). They raise on failure; they never speak."""
    # power
    def shutdown(self, delay_s: int) -> None: ...
    def restart(self, delay_s: int) -> None: ...
    def abort_shutdown(self) -> None: ...
    def sleep(self) -> None: ...
    def hibernate(self) -> bool: ...
    def lock(self) -> None: ...
    def sign_out(self) -> None: ...
    def screen_off(self) -> None: ...
    def screen_on(self) -> None: ...
    def keep_awake(self, on: bool) -> None: ...
    # sound + media
    def volume_get(self) -> int: ...
    def volume_set(self, pct: int) -> None: ...
    def volume_step(self, delta_pct: int) -> int: ...     # returns new level
    def mute_get(self) -> bool: ...
    def mute_set(self, muted: bool) -> None: ...
    def media(self, key: Literal["play_pause", "next", "prev", "stop"]) -> None: ...
    def app_volume(self, app: str, pct: int | None = None, mute: bool | None = None) -> bool: ...
    # display
    def brightness_get(self) -> int | None: ...
    def brightness_set(self, value: int | str) -> int | None: ...   # 50 or "+15"; None if unsupported
    def set_theme(self, dark: bool) -> None: ...
    # windows
    def windowed_apps(self) -> list[str]: ...              # process names, lowercase, no ".exe"
    def focus_app(self, app: str) -> bool: ...
    def close_app(self, app: str) -> bool: ...
    def kill_app(self, app: str) -> bool: ...
    def window_cmd(self, cmd: Literal["minimize", "maximize", "restore", "close", "topmost", "notopmost"]) -> None: ...
    def snap(self, side: Literal["left", "right"]) -> None: ...
    def show_desktop(self) -> None: ...
    def desktop(self, cmd: Literal["new", "next", "prev", "close"]) -> None: ...
    def foreground_is_self(self) -> bool: ...
    # apps / web
    def open_target(self, target: str) -> None: ...        # exe / URL / URI / shell: / .lnk path
    def find_app(self, spoken: str) -> tuple[str, str] | None: ...  # Start Menu index -> (name, lnk path)
    def find_song(self, query: str) -> tuple[str, str]: ...  # (spoken title, watch URL); LookupError / OSError
    def weather(self, city: str) -> dict: ...
    # input
    def keys(self, chord: str) -> None: ...                # "ctrl+shift+t", "enter", "f11"
    def type_text(self, text: str) -> None: ...
    # clipboard
    def clip_get(self) -> str: ...
    def clip_set(self, text: str) -> None: ...
    # info
    def system_status(self) -> dict: ...                   # {"cpu":12,"mem":48,"battery":None|{"pct":80,"plugged":True},"uptime_s":11400}
    def disks(self) -> list[dict]: ...                     # [{"drive":"C","free_gb":120,"total_gb":480,"pct":75}]
    def top_processes(self, n: int = 3) -> list[str]: ...
    def screenshot(self, window: bool = False) -> Path: ...
    def last_screenshot(self) -> Path | None: ...


class Store(Protocol):
    """CalendarStore surface, lifted from v1 D:\arc\calendar_store.py (§4.10). Thread-safe (RLock).
    Items (events and to-dos) share one list; item/note dicts follow §6.2."""
    version: int                       # bumps on every save; the UI polls it
    lock: Any
    def add_event(self, title: str, date: dt.date | str, time: dt.time | str | None = None,
                  reminder_min: int | None = None, repeat: str | None = None, source: str = "ui",
                  now: dt.datetime | None = None) -> dict: ...
    def add_task(self, title: str, date: dt.date | str, time: dt.time | str | None = None,
                 reminder_min: int | None = None, source: str = "ui", now: dt.datetime | None = None) -> dict: ...
    def get_item(self, item_id: str) -> dict | None: ...
    def update_item(self, item_id: str, **fields: Any) -> dict | None: ...   # moving it re-arms its reminder
    def set_done(self, item_id: str, done: bool = True) -> dict | None: ...
    def delete_item(self, item_id: str) -> bool: ...
    def items_between(self, start: dt.date, end: dt.date, include_done: bool = True) -> list[tuple[dt.date, dict]]: ...
    def open_tasks(self, until: dt.date | None = None) -> list[dict]: ...
    def overdue_tasks(self, today: dt.date) -> list[dict]: ...
    def counts_by_day(self, start: dt.date, end: dt.date) -> dict[dt.date, dict[str, int]]: ...  # event/task/task_open/note
    def next_item(self, now: dt.datetime, days: int = 60) -> tuple[dt.date, dict] | None: ...
    def find(self, text: str, kinds: tuple[str, ...] = ("event", "task"), only_open: bool = False,
             cutoff: float = 0.55) -> dict | None: ...
    def add_note(self, date: dt.date | str, text: str, source: str = "ui", now: dt.datetime | None = None) -> dict: ...
    def update_note(self, note_id: str, text: str) -> dict | None: ...
    def delete_note(self, note_id: str) -> bool: ...
    def notes_on(self, date: dt.date | str) -> list[dict]: ...
    def notes_between(self, start: dt.date, end: dt.date) -> list[dict]: ...
    def clear_notes(self, date: dt.date | str) -> int: ...
    def undo_last(self) -> str | None: ...         # removes the newest voice-added item/note, returns its title
    def get_state(self, key: str, default: Any = None) -> Any: ...
    def set_state(self, key: str, value: Any) -> None: ...
```

`Ctx` (the object every command handler receives) is defined in `engine.py`, not here, but its public
surface is frozen in §4.8.


### 4.4 Foundation modules

Each entry: **purpose · public API · thread · must never**. "Any" thread = internally locked.

**`paths.py`** — constants only. `BASE = Path(__file__).resolve().parent.parent`; `MODEL_DIR = BASE/"model"`;
`DATA_DIR = Path(os.environ.get("XYRUS_DATA_DIR", BASE/"data"))`; `CONFIG_FILE = DATA_DIR.parent/"config.json"`
when the env var is unset, else `DATA_DIR/"config.json"`; `LOG_FILE = BASE/"arc.log"`; `CRASH_FILE = BASE/"crash.log"`;
`ICON_FILE = BASE/"xyrus.ico"`; `SCREENSHOT_DIR = Path.home()/"Pictures"/"Xyrus"`. Never: create files on import.

**`config.py`** — settings. Thread: any (RLock).
```python
DEFAULTS: dict            # §6.1, exact
class Config:
    def __init__(self, path: Path | None = None): ...          # default paths.CONFIG_FILE
    def load(self) -> "Config":
        """Deep-merge file over DEFAULTS; migrate v1 (flat keys voice_replies/confirm_shutdown/apps are kept,
        version -> 2); a corrupt file is renamed config.corrupt.json and defaults are used."""
    def get(self, key: str, default: Any = None) -> Any: ...   # dotted: "calendar.default_reminder_min"
    def set(self, key: str, value: Any) -> None:
        """Set + atomic save (G7) + call on_change callbacks synchronously on the caller's thread."""
    def on_change(self, prefix: str, fn: Callable[[str, Any], None]) -> None: ...
    def all(self) -> dict: ...                                   # deep copy
```
Never: import any other xyrus module except `paths`; block on I/O longer than one file write.

**`log.py`** — `setup(debug: bool = False) -> None`: `RotatingFileHandler(LOG_FILE, 1_000_000, 3, encoding="utf8")`,
format `%(asctime)s %(levelname)s %(threadName)s %(name)s: %(message)s`; `logging.getLogger("comtypes").setLevel(WARNING)`;
`sys.excepthook` and `threading.excepthook` log with traceback; `faulthandler.enable(open(CRASH_FILE,"a"))`;
if `sys.stdout is None` (pythonw) redirect `sys.stdout = sys.stderr = open(DATA_DIR/"stdout.log","a",buffering=1)`.
Modules use `logging.getLogger("xyrus.<name>")`. Never: print.

**`clock.py`** — `SystemClock()` (`now()` = `datetime.now().replace(microsecond=0)`, `mono()` = `time.monotonic()`);
`FakeClock(start: datetime)` with `advance(seconds: float)`, `set(when: datetime)`; its `mono()` advances in step.

**`winutil.py`** — Thread: any.
```python
NO_WINDOW = 0x08000000
def run(args: list[str] | str, *, timeout: float = 15, capture: bool = False, shell: bool = False) -> subprocess.CompletedProcess
def popen(args: list[str] | str, *, shell: bool = False) -> subprocess.Popen      # detached launch, DEVNULL handles
def open_url(url_or_path: str) -> None                                             # os.startfile
def force_foreground(hwnd: int) -> bool                                            # F7 snippet: restore if iconic, ALT tap, SetForegroundWindow, AttachThreadInput fallback
def own_pids() -> set[int]                                                         # os.getpid() + parent if it is pythonw/python (venv launcher pair)
```
Never: call subprocess without NO_WINDOW; raise from `open_url` for a missing target (log + re-raise `OSError`).

**`replies.py`** — string constants/tuples only (G10), all using `{sir}` and named fields. Includes at least:
`WAKE_REPLIES` (default tuple, D3), `NOT_HEARD = "Sorry, I didn't catch that{sir}."`, `DID_YOU_MEAN = "Did you mean {phrase}?"`,
`CANCELLED = ("Cancelled{sir}.", "Alright, dropped.")`, `ACK = ("Done{sir}.", "Noted{sir}.", "Got it{sir}.")`,
`THANKS = ("Anytime{sir}.", "My pleasure{sir}.", "Of course.")`, `ACTION_FAILED = "That didn't work{sir}. It's in the log."`,
`TOO_SLOW = "That's taking too long{sir}. I've stopped waiting."`, `NUDGE = "Still there{sir}? "`,
`GIVE_UP = "Sorry{sir}, I'm not getting it. You can type it in the window, or try again later."`,
`LEAVE = "I'll leave it for now{sir}. Say '{trigger}' when you're ready."`, `HELP = "Here's everything I can do{sir}."`,
`JOKES` (≥ 20; the 6 v1 jokes + more), and every reply string quoted in §3. Never: contain a wake word (G6).

**`persona.py`** — pure functions; config passed in. Thread: any.
```python
def sir(cfg) -> str                          # ", sir" / ", Refath"-style never; honorific only: f", {address_as}" or ""
def render(template: str, cfg, **kw) -> str  # fills {sir} and kw
def pick(key: str, options: Sequence[str], cfg, **kw) -> str   # random, never the previous choice for key
def speak_time(t: dt.time | dt.datetime) -> str    # "3 PM", "9:30 AM", "noon", "midnight"
def speak_when(start: dt.datetime, all_day: bool, now: dt.datetime, *, relative: bool = True) -> str
def speak_duration(seconds: int) -> str      # "seven minutes", "one hour and thirty minutes", "twenty five seconds" (words, v1 describe_secs extended)
def greeting(now: dt.datetime, cfg) -> str   # "Good evening, Refath." / "Good evening, sir."
def part_of_day(now) -> Literal["morning","afternoon","evening"]
```

**`normalize.py`** — `normalize(text: str) -> str` applies §5.6 in order and collapses spaces;
`strip_wake(text: str, wake_words: Iterable[str]) -> tuple[bool, str]` (v1 semantics: first wake token anywhere; the
remainder is the words after it; a leading "hey"/"hi"/"okay" before the wake is dropped); `WAKE_LIKE` (for free-text
cleanup: cyrus zeros virus cirrus sirius serious zero zira xyrus); `NOISE_WORDS` (§3.6). Pure; any thread.

### 4.5 `registry.py` — commands, patterns, matching

```python
@dataclass(frozen=True)
class Pattern:
    text: str                        # as written, e.g. "set [the] volume to <percent>"
    regex: re.Pattern
    slots: tuple[str, ...]           # slot type names in order
    literal_count: int               # number of required literal words
    free_slot: str | None            # "song"|"query"|"text"|"app" if the LAST token is a free-capable slot

@dataclass(frozen=True)
class Command:
    name: str                        # unique, snake_case: "set_volume"
    patterns: tuple[Pattern, ...]
    handler: Callable[["Ctx", "Match"], None]
    section: str                     # Commands-tab section (order below)
    help: str                        # one line, shown in the Commands tab
    destructive: bool = False        # D11
    wake_free: bool = False          # may run without wake in IDLE when its context predicate is true
    context: Callable[["Engine"], bool] | None = None   # e.g. lambda e: e.timers.any()
    follow_up: bool = False          # allowed as a FOLLOW_UP answer (still subject to the follow-up whitelist)
    armed_ok_single_word: bool = True  # False -> a 1-word match needs the wake word in the same utterance
    matcher: Callable[[list[str]], dict | None] | None = None  # keyword matcher (calendar/remember); returns slots
    matcher_score: int = 0           # literal_count to use for matcher hits
    priority: int = 0                # tie-breaker only
    custom: bool = False             # from config.custom_commands
    free_trigger_words: tuple[str, ...] = ()  # matcher commands whose tail is free text (calendar/to-do/note/remember adds)

@dataclass(frozen=True)
class Match:
    command: Command
    pattern: Pattern | None          # None for matcher hits
    slots: dict[str, Any]
    text: str                        # the text that was matched (wake stripped)
    has_unk: bool
    extra_words: int                 # tokens outside the matched span

class Registry:
    def command(self, name: str, *phrases: str, section: str, help: str, **opts) -> Callable: ...  # decorator
    def register(self, cmd: Command) -> None: ...
    def unregister_custom(self) -> None: ...
    def match(self, text: str, *, free_text: str | None = None, scope: Literal["full","idle","follow_up"] = "full",
              env: "SlotEnv") -> Match | None: ...
    def close_phrase(self, text: str) -> str | None: ...     # difflib best literal phrase, ratio >= 0.75
    def commands(self) -> list[Command]: ...
    def sections(self, cfg) -> list[tuple[str, list[tuple[str, str]]]]: ...   # Commands tab (§4.15)
    def literal_words(self) -> set[str]: ...                 # every literal/optional word of every pattern
    def slot_types_used(self) -> set[str]: ...
    def free_triggers(self) -> list[tuple[str, ...]]: ...    # literal prefixes of patterns whose last token is a free slot + every free_trigger_words entry
    def version(self) -> int: ...                            # bumps on register/unregister (grammar rebuild trigger)

REGISTRY = Registry()
command = REGISTRY.command         # the ONE decorator used by every commands/*.py module
```

**Pattern syntax.** Tokens separated by spaces. `word` literal; `[word]` optional literal; `a|an|the`
alternatives (one required); `[a|an]` optional alternatives; `<type>` slot of a type from §4.6. Apostrophes are
part of words (`what's`). A pattern may not start with a slot unless it has ≥ 1 literal word after it
(`<duration> timer`).

**Compilation.** literal → `word`; optional → `(?:word )?`; alternatives → `(?:a|an)`; slot → `(?P<sN>.+?)`,
or `(?P<sN>.+)` if last. Joined with single spaces (optional groups carry their own trailing space). Full regex:
`^(?P<pre>(?:\S+ )*?)` + body + `(?P<post>(?: \S+)*)$`, applied to the normalised text. `extra_words` =
token count of `pre` + `post`.

**Selection.** Collect every (command, pattern) whose regex matches **and** whose slots all parse (slot parser
returns non-None), plus every matcher hit. Best = max by `(literal_count, -extra_words, priority, -registration_order)`.
Required ordering examples (unit-tested): "cancel timer" > "cancel"; "next event" > "next"; "what is the time" >
"what is <expr>"; "play pause" > "play <song>"; "open my calendar" > "open <app>"; "close tab" > "close <app>";
"cancel the dentist event" → delete event, not cancel shutdown; "set a timer" (no duration) → the timer-missing
command; "what's the volume" > "what's <expr>".

**Scopes.** `idle`: only commands with `wake_free and context(engine)`; `follow_up`: only commands with
`follow_up=True` whose name is in the engine's current follow-up whitelist; `full`: everything.

**Free slots.** When the best pattern ends in a free-capable slot and `free_text` is given, the slot value is
recomputed from `free_text`: find the pattern's literal prefix words (for `play`: any of `PLAY_LIKE`) in the
first 3 non-wake tokens of `free_text` and take everything after; else drop leading `WAKE_LIKE` tokens and the
first literal. Then the slot's own cleanup (song: `clean_song_title`). If there is no `free_text` the regex
capture is used with `[unk]` removed; an empty result means the slot did not parse.

**Sections order** (Commands tab): Basics · Power · Sound & media · Display & windows · Apps & web · Keys &
clipboard · Timers · Calendar & reminders · Assistant · Info & tools · Your commands · Slots (legend). Each command
is one row: all its pattern texts joined with " / ", and its help (plus " — works without the wake word while a
timer runs" etc. when `wake_free`). The Slots legend lists every slot type used with its doc and examples.
The count label reads "N commands · M ways to say them" (M = number of patterns). `sections()` is the only data
source for the Commands tab (never hand-written; v1 `_command_sections` behaviour).

### 4.6 `parsing.py` — numbers, durations, percent, expressions, slot types

Pure functions over token lists (already normalised). Any thread.
```python
NUMBER_WORDS: dict[str, float]     # v1 table + zero, hundred, thousand, million handled by parse_number
def parse_number(tokens: list[str]) -> float | None     # "two hundred and fifty"=250, "three point five"=3.5, "twenty five"=25, digits ok
def numbers_in(tokens) -> list[tuple[int, float]]       # (index, value) of each maximal number group
def parse_duration(tokens: list[str]) -> int            # v1 algorithm verbatim (last number before the unit wins; "a"/"an" -> 1; "one and a half" -> 1.5; default unit minutes); 0 = none
def parse_percent(tokens: list[str]) -> int | None      # last number group in 0..100; a "to"/"two" directly before a number is dropped
def parse_expr(tokens: list[str]) -> float | None       # recursive descent: plus minus times "multiplied by" "divided by" over "percent of" squared "square root of"; "for" after an operator = 4
def say_number(x: float) -> str                         # "48", "3.5" (digits; SAPI reads them)

@dataclass(frozen=True)
class SlotEnv:
    config: Any; now: dt.datetime; free_text: str | None = None; app_index: Any = None

@dataclass(frozen=True)
class SlotType:
    name: str
    parse: Callable[[list[str], SlotEnv], Any | None]
    words: frozenset[str]          # grammar words this slot needs (added to the command grammar)
    free: bool                     # value may come from the unrestricted re-decode
    doc: str                       # Commands-tab legend text
SLOT_TYPES: dict[str, SlotType]
```

| Slot | Parse result | Grammar words | Free | Doc / examples |
|---|---|---|---|---|
| `duration` | int seconds > 0 | numbers 1–99, hundred, a, an, and, half, second(s), minute(s), hour(s) | no | "seven minutes, twenty five seconds, half an hour" |
| `percent` | int 0–100 | numbers, percent, hundred | no | "forty percent, seventy" |
| `number` | float | numbers, point, hundred, thousand, million | no | "seven, two hundred" |
| `expr` | float | numbers + operator words | no | "twelve times four, fifteen percent of eighty" |
| `app` | `AppRef(spoken, alias or None, target or None)` — config alias (longest first) else Start-Menu hit from `free_text` else unresolved | config app alias words + calendar/settings words | yes (only when the grammar tail has `[unk]`) | apps from the Apps tab |
| `song` | str (cleaned title), non-empty and not in `SONG_ASK` | — | yes | "alone, shape of you, faded by alan walker" |
| `query` | str | — | yes | "cheap mechanical keyboards" |
| `text` | str | — | yes | free text |
| `when` | `dates.When` | DATE_WORDS + TIME_WORDS + numbers + ordinals | no | "tomorrow at three pm, next friday, in twenty minutes" |
| `day` | `dates.parse_range(text, now)` → `(start: date, end: date, label: str)` (v1, verbatim) | DATE_WORDS + numbers + `few couple several next these rest day days week weeks month` | no | "today, on friday, this week, these 3 days, the next ten days, next two weeks, rest of the week" |
| `name` | str ∈ timer names (§3.8) | those names | no | "tea, pizza" |


### 4.7 `engine.py` — the state machine

Purpose: the single place that decides what an utterance means and what happens next. Thread: T-engine in the
app; the calling thread in tests (`threaded=False`). Never: touch Vosk, SAPI, Tk or COM; call `SystemActions`
directly (only via `ctx.do`); sleep; block > 50 ms.

```python
class Engine:
    def __init__(self, *, config, registry, clock, speaker, chime, actions, executor, store, memory,
                 notifier, ui_queue: "queue.Queue[str]", threaded: bool = True, app_index=None): ...
    # ---- entry points (thread-safe; in threaded mode they enqueue to the inbox, else run inline)
    def submit_text(self, text: str, source: Source = "typed") -> None: ...
    def submit_transcript(self, tr: Transcript) -> None: ...
    def submit_hotkey(self, name: Literal["push_to_talk", "stop_speaking", "show_window", "pause"]) -> None: ...
    def post(self, fn: Callable[[], None]) -> None: ...                  # run fn on T-engine
    def cancel_dialogue(self) -> None: ...
    def set_paused(self, paused: bool) -> None: ...                      # also calls capture via app hook
    # ---- synchronous (T-engine / tests)
    def handle(self, text: str, source: Source = "typed", *, free_text: str | None = None) -> None:
        """THE single entry for text. Mic transcripts arrive via handle_transcript which calls this."""
    def handle_transcript(self, tr: Transcript) -> None: ...
    def tick(self) -> None: ...                                          # every 250 ms; tests call it
    def run_forever(self, stop: threading.Event) -> None: ...            # inbox loop: get(timeout=0.25) -> dispatch -> tick
    # ---- read side (thread-safe)
    def snapshot(self) -> EngineSnapshot: ...                            # immutable, built under a lock
    def listen_spec(self) -> ListenSpec: ...
    # ---- public attributes (read-only outside T-engine)
    last_reply: str; last_heard: str; replies: collections.deque[str]   # maxlen 50, every spoken/returned reply
    timers: TimerService
```

**Modes and transitions** (`mode_until` is a monotonic deadline; a window "starts after the reply" means the
engine passes `then=` to `ctx.say`, and the deadline is set in that callback):

| From | Event | To | Effect |
|---|---|---|---|
| idle | wake + empty rest (or rest == "[unk]") | arming | say random wake reply; on its SpeechDone → armed, `mode_until = mono + command_window_s` |
| idle / armed | wake + rest matches | idle | run command (it may open a dialogue / follow-up / arm) |
| idle / armed | wake + rest no match | arming | NOT_HEARD or DID_YOU_MEAN (confirm dialogue); after the reply → armed |
| armed | no wake, text matches (`armed_ok_single_word` respected) | idle | run |
| armed | no wake, no match, ≤ 1 word | armed | noise; deadline unchanged |
| armed | no wake, no match, ≥ 2 words | arming | NOT_HEARD; after the reply → armed (fresh 15 s) |
| armed / follow_up | deadline passes | idle | silent; Activity "window closed" (debug only) |
| any except dialogue | `ctx.start_dialogue(d)` | dialogue | `self.dialogue = d` **then** `d.start()` (order matters; the prototype caught this bug) |
| dialogue | dialogue finished/cancelled/abandoned | idle (or follow_up if the dialogue set one) | flush `alert_queue` |
| idle | command calls `ctx.follow_up(kind, whitelist, seconds)` | follow_up | after the current reply finishes; `mode_until = mono + seconds` |
| follow_up | no wake, `registry.match(scope="follow_up")` | idle (or new follow_up) | run |
| follow_up | no wake, no match | follow_up | noise |
| follow_up | wake present | (as idle) | follow-up ends |
| idle | no wake, `registry.match(scope="idle")` (timer cancel, ringing stop) | idle | run |
| idle | anything else without wake | idle | noise (Activity kind `noise`, hidden by default) |
| any | `set_paused(True)` | (mode kept) | windows closed, confirm dialogues cancelled silently, listen `paused` |
| any | hotkey push_to_talk | armed | chime("wake"), `mode_until = mono + 8`, no speech |

**Routing inside `handle(text, source, free_text)`:**
1. `text = normalize(text)`; store `last_heard`; add Event `heard` (or `noise`, decided below).
2. For sources other than `mic`, the wake word is optional: `had_wake = True` unless a dialogue is active.
3. Dialogue active → `self.dialogue.handle(rest_or_text, free_text=free_text, source=source)`; return.
4. Otherwise follow the transition table. Running a match = `_run(match, source, free_text)`:
   - `match.command.destructive and match.has_unk` → confirm "Did you say <first pattern text>{sir}?" (D11).
   - Event `cmd` "<command name> <slots>"; `handler(ctx, match)` inside try/except → on exception log + say ACTION_FAILED.
5. `listen_spec()` is recomputed after every transition (generation += 1 when it changes).

**`handle_transcript(tr)`** drops as noise, before routing, when (a) `tr.text` is empty, or (b)
`tr.source == "mic"` and `tr.peak < config.min_speech_peak`, or (c) mode is idle/follow_up and the text has no
wake word and no idle/follow-up match (the normal noise case). Then `handle(tr.text, "mic", free_text=tr.free_text)`.

**`listen_spec()`:** paused → `paused`; dialogue → `dialogue.listen_mode` with extra words = the dialogue's
answer words; arming/armed → `command`; follow_up → `wake` + whitelist words; idle → `wake` + (timer words
`cancel stop timer timers the all` + timer names if any timer exists or rings) + (`cancel stop abort` during a
shutdown countdown).

**`tick()`** (in this order): dialogue deadline; armed/follow-up expiry; `timers.tick(mono)` → fire timer-done
(§3.8); every 5 s `reminders.due_alerts(store, now)` → alerts (queued while a dialogue is active);
unacknowledged-alert repeat (should); daily morning brief (should); startup greeting once the app calls
`engine.startup()` (see §4.15).

**Speech-done plumbing.** `ctx.say(text, then=cb)` → engine calls `speaker.say(text, force=…, on_done=lambda:
self.post(lambda: self._speech_done(token)))`; `_speech_done` runs `cb` on T-engine. Replies are also appended to
`replies`/`last_reply` and an Event `say` (or `ask` when `ctx.say` is called by a dialogue question, `alert` for alerts).

### 4.8 `Ctx` — frozen surface for command handlers (defined in `engine.py`)

A new `Ctx` is created per command run / dialogue. All methods must be called on T-engine (handlers and
`then=` callbacks already are).

```python
class Ctx:
    engine: Engine; config: Config; clock: Clock; actions: SystemActions; store: Store
    memory: "MemoryStore"; timers: "TimerService"; source: Source; match: Match | None
    @property
    def sir(self) -> str: ...                     # ", sir" (from config.address_as) or ""
    def say(self, text: str, *, force: bool = False, priority: int = 1,
            then: Callable[[], None] | None = None) -> None: ...   # {sir} in text is rendered here
    def do(self, fn: Callable[..., Any], *args: Any, then: Callable[[Any], None] | None = None,
           error: Callable[[BaseException], None] | None = None, timeout: float = 15.0, **kw: Any) -> None:
        """Run fn(*args, **kw) on the executor; then/error run back on T-engine. Default error handler:
        log + say ACTION_FAILED (TimeoutError -> TOO_SLOW)."""
    def confirm(self, question: str, on_yes: Callable[[], None], on_no: Callable[[], None] | None = None,
                *, window_s: float | None = None) -> None: ...     # ConfirmDialogue; default window confirm_window_s
    def ask(self, question: str, slot: str, on_answer: Callable[[Any], None], *,
            listen: ListenMode = "when", hint: str = "", ignore: set[str] = frozenset()) -> None: ...  # AskDialogue
    def start_dialogue(self, dialogue: "Dialogue") -> None: ...
    def follow_up(self, kind: str, whitelist: Iterable[str], *, payload: Any = None,
                  seconds: float | None = None) -> None: ...        # starts after the current reply
    def arm(self, seconds: float | None = None) -> None: ...         # open a command window after the reply
    def ui(self, message: str) -> None: ...        # "show" | "show:<Tab>" | "show:Calendar:2026-09-14" | "quit"
    def event(self, kind: EventKind, text: str, meta: str = "") -> None: ...
    def notify(self, title: str, body: str) -> None: ...
    def chime(self, kind: ChimeKind) -> None: ...
    def handle(self, text: str, source: Source = "routine") -> None: ...   # re-entrant, depth <= 3
```

Example (the pattern every command module follows):
```python
from xyrus.registry import command

@command("set_volume", "set [the] volume to <percent>", "volume <percent>", "set volume <percent>",
         section="Sound & media", help="sets the volume to an exact level")
def set_volume(ctx, m):
    pct = m.slots["percent"]
    ctx.do(ctx.actions.volume_set, pct, then=lambda _: ctx.say(f"Volume at {pct} percent{{sir}}."))
```

### 4.9 `dialogue.py` — slot filling (lift `SCRATCH\dialogue_proto.py`)

Port `Dialogue` from the prototype nearly as-is. Required changes:
1. Replace `fake_parse_when` with an adapter over `dates.parse_when(text, now)` returning
   `(date|None, time|None, leftover_words)`; all-day results give `time=None`.
2. Replace `engine.say(q, then_listen=mode)` with `ctx.say(q, then=self.on_speech_done)` and set
   `self.listen_mode = mode` (the engine publishes it through `listen_spec`). Deadline = `clock.mono() + timeout`.
3. `Engine.save(d)` becomes the dialogue's `on_complete(slots)` callback supplied by the command.
4. Summary uses `persona.speak_when`. Strings come from `replies.py`.
5. Keep: construction order (engine holds the reference before `start()`), noise rule (unparseable ≤ 1 word is
   swallowed), attempts (2 failures → GIVE_UP → PARKED 60 s (should) or end), nudge once at `dialogue_timeout_s`
   (20) then LEAVE at 40 s, cancel words anywhere (`cancel`, `never mind`, `forget it`, `stop`, `leave it` —
   **not** `no`), yes-set only when it is the whole utterance, FIX by keywords title/date/day/time.

```python
class Dialogue:                                    # base: slot filling (kinds event|reminder|note|memory|name)
    kind: str; state: str; slots: dict[str, Any]; listen_mode: ListenMode; attempts: int
    def __init__(self, ctx: Ctx, kind: str, *, slots: dict | None = None,
                 on_complete: Callable[[dict], None], trigger: str = "add an event"): ...
    def start(self) -> None: ...
    def handle(self, text: str, *, free_text: str | None = None, source: Source = "mic") -> bool: ...
    def tick(self) -> None: ...
    def on_speech_done(self) -> None: ...
    def cancel(self, *, say: bool = True) -> None: ...
    def view(self) -> DialogueView: ...
    def answer_words(self) -> tuple[str, ...]: ...   # extra grammar words for the current state
class ConfirmDialogue(Dialogue): ...    # kind "confirm": one CONFIRM state, listen "confirm", window confirm_window_s, no nudge; expiry -> silent cancel + Activity "confirm timed out"
class AskDialogue(Dialogue): ...        # one question, one slot parsed with SLOT_TYPES[slot] (or raw free text), listen mode given
class PickDialogue(Dialogue): ...       # "Which one? First: … Second: …" -> first|second|third|one|two|three|all
```
Per-kind states: event/reminder = TITLE(free) → WHEN(when) → TIME(when, skipped if all_day or date+time given) →
CONFIRM(confirm) → FIX; note = TITLE(free) → save (date from trigger or today; no confirm); memory = TITLE(free) →
CONFIRM; name (nice) = TITLE(free) → CONFIRM. Mode words for `confirm`: yes-set, no-set, cancel words, title,
date, day, time, change, the. Typed answers (`source="typed"`) skip the noise rule and may use digits.
Acceptance = the 9 prototype scenarios re-expressed against the real engine (§7.2).

### 4.10 Data and assistant modules

**`executor.py`** — `Executor(name="exec", max_abandoned=2)` implements the Protocol: one worker thread with
`comtypes.CoInitialize()`, a job queue, a watchdog that marks a job timed out after `timeout`, calls
`on_done(None, TimeoutError)` and starts a fresh worker (the stuck one is abandoned; after `max_abandoned` log
ERROR). `InlineExecutor` runs jobs synchronously (tests, `--text`). Never: run two jobs concurrently on one worker.

**`timers.py`** — `TimerService(clock)`: `add(seconds, name=None) -> TimerView`, `cancel(name=None, all=False) -> int`,
`add_time(seconds) -> TimerView | None`, `views() -> tuple[TimerView, ...]`, `any() -> bool`, `ringing() -> bool`,
`stop_ringing()`, `tick() -> list[TimerView]` (newly fired). In-memory, clock-driven, engine thread only.

**`store.py`** — lift `D:\arc\calendar_store.py` `CalendarStore` **verbatim** (API = `interfaces.Store`), changing only:
`DATA_FILE` → `paths.DATA_DIR/"calendar.json"`; `import when as W` → `from xyrus import dates as W`; add
`acknowledge(item_id, occ_key)` (stores `state["acked"][item_id]`, used by the 2-minute repeat, should) and `prune()`
(fired keys older than 60 days on recurring items; on startup and daily). It must open v1's `data/calendar.json`
unchanged (including v1's migration of a prototype-era `events` list). `undo_last` covers voice adds of this session.
Thread: any (RLock). Never: speak, fire alerts, import engine/ui.

**`reminders.py`** — lift v1 `reminder_time`, `due_reminders`, `reminder_text` (module functions of
`calendar_store.py`) and wrap them:
```python
@dataclass(frozen=True)
class Alert: item: dict; occ: dt.date; kind: Literal["due", "late", "stale"]
def due_alerts(store, now: dt.datetime) -> list[Alert]    # = due_reminders (marks fired before returning)
def alert_text(a: Alert, now: dt.datetime, cfg) -> str    # = v1 reminder_text, honorific from config
def missed_summary(alerts: list[Alert], now, cfg) -> str  # one sentence for several 'late' alerts at startup
```
One reminder per occurrence at `start − reminder_min` (all-day at 09:00); `reminder_min None` never fires; done
to-dos never fire. Classification due (< 1.5 min late) / late (≤ 120 min, spoken "While I was away, sir: …") /
stale (silent, Activity only). Defaults for voice adds: events `calendar.default_reminder_min` (10), reminders 0,
to-dos with a time 0, to-dos without a time None.

**`memory.py`** — `MemoryStore(path=DATA_DIR/"memory.json")`: `add(text, source) -> dict`, `newest(offset=0, n=3)`,
`count()`, `delete(mem_id)`, `delete_last() -> dict | None`, `clear() -> int`, `version`. Same lock/atomic rules.

**`assistant.py`** — spoken summaries, pure functions (headless-testable with a fake clock and a temp store). Lift
v1 `calendar_store.py` `item_phrase`, `join_and`, `count_phrase`, `describe_range(store, start, end, label, now) ->
(text, overflowed)`, `describe_next`, `describe_todos`, `briefing` verbatim (literal "sir" → configured honorific),
and add `startup_summary(now, store, missed, cfg) -> str`, `good_night(now, store, cfg) -> str`,
`next_after(now, store, after: dt.datetime) -> str` (the "and after that" follow-up). When `describe_range` reports
overflow the caller posts `show:Calendar`. Wording in §3.9 / D14.

### 4.11 `dates.py` — spoken date/time parser (lift `D:\arc\when.py` verbatim)

Copy v1 `when.py` (= `when_proto.py` + `fmt_time`, `day_name`, `day_label`, `mentions_date`, `parse_range`,
`FEW`, `DATE_TOKENS`) into `xyrus/dates.py` unchanged, and its self-test table into `tests/test_dates.py` (57 cases +
extra assertions + `parse_range` cases: "this week", "next three days", "these 3 days", "the next ten days",
"next two weeks", "a week", "rest of the week", "rest of the month", "tomorrow", "on friday", "on the twentieth",
"this weekend", "this month", and "in three days" = one day). Additions are additive only (v1 behaviour and tests
must stay green):
1. Optional `waking=(7, 23)` parameter of `parse_when` fed from `config.calendar.waking_hours`.
2. In `normalise`: `add` → `at` when the next token is a number word/digit or `noon|midnight|half|quarter`
   (verified mishearing "add half past seven"); ordinals right after `at` read as hours (`at eighth` → 8).
3. `INTENT_PREFIXES` gains: "add in event", "add and event", "remind me again", "take a note", "note for",
   "add a note for", "add a to do", "add a task", "new to do", "to do", "on my to do list", "to my to do list".
4. `parse_typed(text, now) -> When | None`: `parse_when`, then `parsedatetime.Calendar().parseDT(text,
   sourceTime=now)` **only for the typed quick-add preview** (never for speech, never saves without the preview).
`parse_range` is the parser of the `<day>` slot (§4.6). `fmt_time` is what `persona.speak_time` uses.
Thread: any (pure). Never: read the clock itself when `now` is passed.

### 4.12 Audio, recognition, speech

**`grammar.py`** — builds word lists; pure except for reading the model word list. Thread: any.
```python
class Vocab:
    def __init__(self, model_dir: Path): ...   # reads model_dir/"graph"/"words.txt" if present, else lazily
                                               # asks the recognizer thread (engine-free callback) - see note
    def known(self, word: str) -> bool: ...
    def unknown_words(self, phrase: str) -> list[str]: ...
class GrammarBuilder:
    def __init__(self, registry, config, vocab: Vocab): ...
    def words(self, mode: ListenMode, extra: Iterable[str] = ()) -> list[str]:
        """Sorted JSON-ready list ending with "[unk]"; unknown words dropped and logged once. free -> []"""
    def version(self) -> tuple: ...            # (registry.version(), config apps/custom hash) for cache invalidation
```
Note: vosk-model-small-en-us-0.15 ships **no** `graph/words.txt` (verified: `graph/` holds `Gr.fst`, `HCLr.fst`,
`disambig_tid.int`, `phones/`). So `Vocab` obtains the word list by asking the recognizer thread:
`Recognizer.check_words(words) -> dict[str,bool]` (thread-safe request/response over a queue with a 2 s timeout,
answered with `model.vosk_model_find_word(w) >= 0`). Results are cached in `data/vocab_cache.json` keyed by model
path so the UI can validate offline and instantly. Mode word sets:
- `wake`: `config.wake_words` + extra.
- `command`: wake words + `registry.literal_words()` + words of every slot type used + config app alias words +
  custom phrase words + `DATE_WORDS` + answer words (yes-set, no-set, cancel words).
- `confirm`: wake words + yes-set + no-set + cancel words + `title date day time change the` + extra.
- `when`: wake words + `DATE_WORDS` + `TIME_WORDS` + numbers + ordinals + cancel words + `all day no any time` + extra.
- `free`: no grammar (unrestricted recognizer).

**`audio.py`** — capture. Thread: callback on T-audio-cb; `start/stop/restart` from any thread (lock); watchdog T-watch.
```python
@dataclass(frozen=True)
class MicDevice: index: int; name: str; hostapi: str; default_samplerate: float
def list_mics() -> list[MicDevice]          # max_input_channels > 0 and hostapi in {"MME", "Windows DirectSound"} (D8)
def resolve_mic(name: str | None, hostapi: str | None) -> int | None   # exact name+api, else same name any allowed api, else sd.default.device[0]
class AudioCapture:
    SAMPLE_RATE = 16000; BLOCK = 4000       # 0.25 s int16 mono (verified F14: 8000-byte blocks)
    def __init__(self, get_device: Callable[[], tuple[str | None, str | None]], on_status: Callable[[str], None]): ...
    chunks: "queue.Queue[tuple[bytes, float]]"    # maxsize 200 (50 s); on Full drop oldest, count drops
    def start(self) -> None: ...            # opens sd.RawInputStream(device, 16000, blocksize=4000, dtype="int16", channels=1)
    def stop(self) -> None: ...             # closes the stream (pause = D5); clears the queue; level -> 0
    def restart(self) -> None: ...
    def running(self) -> bool: ...
    def level(self) -> float: ...           # 0..1 decaying peak (every 8th sample), for the UI
    def status(self) -> str: ...            # "listening" | "paused" | "mic error, retrying" | "no microphone"
```
Watchdog: while started, if no chunk for 3 s or the stream raised → `stream.abort()/close()`, try
`sd._terminate(); sd._initialize()` (private API to refresh devices; wrapped in try), re-resolve the device, reopen
with backoff 1, 2, 5, 10, 10… s; `on_status("mic error, retrying")` then `"listening"`; toast on loss/recovery (via the
status callback, app wires the notifier). Never: do anything but enqueue in the callback; open WASAPI devices.

**`recognizer.py`** — Thread: T-rec (the only Vosk user).
```python
class Recognizer:
    def __init__(self, model_dir: Path, capture: AudioCapture, grammar: GrammarBuilder,
                 get_spec: Callable[[], ListenSpec], on_transcript: Callable[[Transcript], None],
                 speaker: Speaker, chime: Chime, config): ...
    def start(self) -> None: ...            # loads vosk.Model once (SetLogLevel(-1)), starts T-rec
    def stop(self) -> None: ...
    def check_words(self, words: list[str]) -> dict[str, bool]: ...   # thread-safe request (see grammar.py)
    def save_last_seconds(self, path: Path, seconds: int = 30) -> None: ...   # should: ring buffer -> WAV (Settings button)
    def decode_pcm(self, pcm: bytes, mode: ListenMode, extra=()) -> Transcript: ...  # tests/--wav: same code path, no mic
```
Loop (per chunk from `capture.chunks.get(timeout=0.5)`; never `sleep`):
1. Re-read `get_spec()`; if the generation changed, switch recognizer **at an utterance boundary** (immediately if
   the current partial is empty, else after the current final). Recognizers are cached per `(mode, words-hash)`
   (`KaldiRecognizer(model, 16000, json.dumps(words))`, `SetWords(True)`; free = `KaldiRecognizer(model, 16000)`);
   the cache is cleared when `grammar.version()` changes. Paused → drain and wait.
2. **Echo gate:** if `not speaker.is_quiet_at(t_chunk)` → drop the chunk, `rec.Reset()`, clear the utterance buffer.
3. Append the chunk to the utterance buffer (`bytearray`, cap 20 s; keep the previous chunk as 0.25 s pre-roll) and
   update `peak`.
4. `rec.AcceptWaveform(chunk)`; if False and mode is `wake` and `config.chime_on_wake`, check `PartialResult()`
   for a wake token; first hit per utterance → `chime.play("wake")`.
5. On a final result: `text = normalize(result["text"])`; build the Transcript (§5.3 re-decode rules); reset the
   buffer; `on_transcript(tr)` (the app wires this to `engine.submit_transcript`).
6. Every 30 s of continuous non-speech in wake mode, nothing special (Kaldi's 20 s max-utterance endpoint yields
   `""` or `[unk]` in wake mode; those are dropped as empty).
Never: call engine methods other than `on_transcript`; touch Tk; share recognizer objects.

**`speech.py`** — `Speaker` + `Chime`. Speaker thread per F6 verified snippet:
```python
class SapiSpeaker:     # implements interfaces.Speaker
    def __init__(self, config): ...         # starts T-speak: CoInitialize, SAPI.SpVoice, voice/rate from config
    # say / stop / is_speaking / is_quiet_at / voices / set_voice / set_rate  (see Protocol)
class WinmmChime:      # implements interfaces.Chime (D9, F9 snippet)
    SOUNDS = {"wake": (880, 1320) 100 ms each, "alert": (988, 1319, 988, 1319) 120 ms each, "error": (220,) 250 ms}
```
Speaker loop: `PriorityQueue[(priority, seq, text, force, on_done)]`; skip (call on_done at once) when
`not force and not config.voice_replies`; else record interval start, `v.Speak(text, 1)`, loop
`while not v.WaitUntilDone(50): if stop_event.is_set(): v.Speak("", 2); break`, record interval end, call on_done
(always, in `finally`). `stop()` sets the event and empties the queue (queued on_dones are still called).
On COMError re-create the voice (CoUninitialize/CoInitialize) and continue. Never: pyttsx3 (G3); cross-thread purge.

### 4.13 `actions/` — Windows side effects (implements `SystemActions`)

`actions/__init__.py` defines `class WinActions` whose methods delegate to the area modules; each area module
exposes plain functions (unit-testable where possible). All functions may block and are only ever called on T-exec
(via `ctx.do`) or from the smoke test. They raise on failure (`OSError`, `LookupError`, `RuntimeError` with a
short message); they never speak (G10) and never print.

| Module | Functions (→ SystemActions method) | Implementation notes |
|---|---|---|
| `keys.py` | `tap(*vks)`, `chord(spec)`, `parse_chord("ctrl+shift+t") -> list[int]`, `release_stuck_modifiers()`, `type_text(text)` | `keybd_event(vk, MapVirtualKeyW(vk,0), flags, 0)`; 30 ms between down and up; release stuck modifiers first (F12). `type_text` = `keyboard.write(text, delay=0.01)` (F3/F15). Key names: enter tab escape esc space f1–f12 up down left right home end delete insert pageup pagedown ctrl alt shift win a–z 0–9. |
| `power.py` | shutdown, restart, abort_shutdown, sleep, hibernate, lock, sign_out, screen_off, screen_on, keep_awake | v1 `actions.py` code moved; `winutil.run(["shutdown","/s","/t",str(n)])`. |
| `audio.py` | volume_get/set/step, mute_get/set, app_volume | F1 snippet; `CoInitialize` is done by the executor thread; fall back to VK taps on COMError. |
| `media.py` | media(key) | VK 0xB3 / 0xB0 / 0xB1 / 0xB2. |
| `display.py` | brightness_get/set, set_theme | F2; catch `ScreenBrightnessError` → return None. |
| `windows.py` | list_windows, windowed_apps, find_app_window(app), focus_app, close_app, kill_app, window_cmd, snap, show_desktop, desktop, foreground_is_self | F7/F8 snippets verbatim (DWM cloak filter, ALT-tap foreground, SetWindowPos snap, WM_CLOSE). App matching: config alias target basename (e.g. `chrome` → `chrome.exe`, `msedge`), then process stem, then title substring; spaces removed ("note pad" → notepad). Excluded processes: explorer, applicationframehost, textinputhost, `winutil.own_pids()`. |
| `apps.py` | open_target, find_app, `StartMenuIndex` (`refresh()`, `names()`, `lookup(spoken) -> (name, path) | None`) | §3.5. |
| `web.py` | find_song, youtube_search, youtube_search_url, spoken_title, google_url, weather | Port v1 `actions.py` youtube functions verbatim (they are the verified implementation, §3.6). |
| `clipboard.py` | clip_get, clip_set, clip_clear | F10 snippet. |
| `sysinfo.py` | system_status, disks, top_processes, battery | psutil; `cpu_percent(interval=0.5)`. |
| `screenshot.py` | screenshot(window=False), last_screenshot | v1 code + window bbox. |

### 4.14 `commands/` — registrations

Each `commands/<area>.py` only imports `xyrus.registry.command`, `xyrus.replies`, `xyrus.persona`, `xyrus.parsing`,
`xyrus.dates`, `xyrus.assistant`, `xyrus.reminders`, `xyrus.dialogue`, and registers handlers for the §3 rows of its
area. Handler rules: run on T-engine; no blocking; all side effects through `ctx.do`; every spoken string from
`replies.py` or a literal in the handler that passes the G6 test. `commands/__init__.py:load_all(registry)` imports
all modules once (idempotent). `commands/custom.py` exposes `register_custom(registry, config)` called at startup and
from `config.on_change("custom_commands")` (posted to T-engine).

### 4.15 UI and shell

**`ui/window.py`** — `XyrusWindow(ctk.CTk)`. Thread: T-main only.
`__init__(self, app: "App", start_hidden: bool)`; `show()` (deiconify, lift, `winutil.force_foreground(hwnd)` with
`hwnd = int(self.frame(), 16)`); `hide()`; `_poll()` every 150 ms wrapped in try/except (a UI bug never stops
polling): drain `app.ui_queue` (`show`, `show:<Tab>`, `show:Calendar:<iso>`, `quit`), check the show-event, read
`engine.snapshot()` and `capture.level()`, update header/banner/tabs only when values changed. Header: title,
status dot + label ("Listening" / "Listening for your command · 12 s" / "Listening for a follow-up · 6 s" /
"Speaking" / "Paused · mic off" / "Confirm shutdown?" / "Mic error, retrying"), timer label (soonest timer
"timer 04:59") or next event ("Next: dentist at 3 PM · in 2 h"), mic level bar, Stop button (enabled while
speaking), Pause/Resume button. Dialogue banner (visible when `snapshot.dialogue`): caption "Xyrus is asking",
question (16 px bold), hint (muted), slot chips, countdown "waits 14 s", entry "type your answer…" (Enter →
`engine.submit_text(text, "typed")`), Cancel (→ `engine.cancel_dialogue()`); amber when parked. Window geometry
and last tab saved to `config.window`. Closing hides (one-time toast "Still running in the tray.").

**Tabs** (CTkTabview, order fixed): 
- `home_tab.py` — Home: conversation feed (CTkScrollableFrame, max 200 rows, kinds styled: heard muted, cmd accent,
  say white, ask accent "?", alert amber "!", remind accent bell, info green with "Open" button for paths, error red;
  `noise` hidden unless "Show noise" switch), text command box at the bottom (Enter submits; Up/Down history of
  20), running timers with Cancel buttons, "Try saying" card rotating every 8 s through random `registry` patterns.
- `calendar_tab.py` — **lift `D:\arc\calendar_tab.py`** (being built in v1 by another agent) and adapt it:
  constructor takes `app` (uses `app.store`, `app.engine`, `app.clock`), colours from `ui/theme.py`, refresh from the
  window poll when `store.version` changes or once a minute; every write goes through the store. Required content
  (check after lifting, add what is missing): month grid (header `[<] September 2026 [>] [Today]`, Mo–Su strip,
  pre-created day cells reconfigured on change, per-day marks from `store.counts_by_day` — event dots, an open-to-do
  mark, a note dot; today outlined, selected filled; arrows ±1 day, PgUp/PgDn ±1 month, Home = today); day panel with
  three sections — **To-dos with tick boxes** (CTkCheckBox per task → `store.set_done(id, checked)`; overdue in red;
  done dimmed/struck; ✕ delete), **Events** (time | title | bell | repeat | ✎ | ✕ with inline "Delete? Yes"),
  **Notes** (inline edit, ✕, add box); quick-add entry ("dentist tomorrow 3pm" / "todo buy milk friday" — a leading
  `todo`/`to do`/`task` adds a to-do, otherwise an event; no date → the selected day via `dates.mentions_date`) with a
  live preview from `dates.parse_typed`; Upcoming list (next 14 days, events and open to-dos, click jumps).
- `event_dialog.py` — EventDialog (CTkToplevel, grab_set; reuse v1 calendar_tab's editor if it has one): Kind (event / to-do); Title; Date yyyy-mm-dd with −1/+1 day; All-day switch;
  hour 1–12, minute 00–55 step 5, AM/PM; Reminder (none / at the time / 5 / 10 / 15 / 30 / 60 minutes / 1 day before);
  Repeat (never / daily / weekly); Save / Delete / Cancel; inline red validation.
- `commands_tab.py` — v1 behaviour kept: count label + filter entry (filters phrase and help, case-insensitive) +
  scrollable sections from `registry.sections(config)` only. Each row: `"Xyrus, <phrases>"` (Consolas) + help +
  a "Try" button (patterns without slots only → `engine.submit_text(phrase, "typed")`). Re-renders when
  `registry.version()` changes.
- `apps_tab.py` — v1 editable alias list (name, target, ✕, + Add, Save → `config.set("apps", …)`), each name validated
  with `grammar.Vocab.unknown_words` (red hint); below, read-only Start Menu index with a filter (should).
- `routines_tab.py` — list of custom commands; editor: phrase entry (live vocabulary validation), step rows (action
  OptionMenu open/run/keys/type/say/command/wait + argument entry + ↑ ↓ ✕), Enabled switch, Test (→
  `engine.submit_text(phrase, "typed")`), Save (validation §3.10), Delete, "Add template".
- `settings_tab.py` — sections, every control applies live via `config.set` (no Save button): **Listening** (mic
  OptionMenu from `audio.list_mics()` + live level + "Test mic"; wake words checkboxes (cyrus/zeros/virus/cirrus +
  sirius/serious optional); command window seconds; follow-up on/off + seconds; chime on wake; mic sensitivity →
  `min_speech_peak` slider 0.005–0.08; "Save last 30 seconds of audio" (should)). **Voice** (voice OptionMenu from
  `speaker.voices()`, rate slider −10..10, voice replies switch, Test voice). **Assistant** (your name, address you as,
  startup greeting speak/toast/off, briefing on startup, reminder lead minutes, snooze minutes, quiet hours from/to,
  morning brief + time, confirm before shutdown, shutdown delay, YouTube lookup switch, weather switch + city).
  **System** (Start with Windows, hotkeys with conflict status, Open log, Open folder, Run self-test, version).

**`tray.py`** — `Tray(app)`: builds `pystray.Icon("xyrus", image, "Xyrus — listening", menu)`, `run_detached()`;
`set_state(state: Literal["listening","active","paused","error"])` swaps the PIL image (v1 `make_icon_image`
extended: active = green ring); `set_tooltip(text)`; implements `Notifier.notify` via `icon.notify(body, title)`
(guarded: before the icon is ready, queue and send after 1 s). Menu callbacks only enqueue/call thread-safe methods.
`ensure_icon_file()` writes `xyrus.ico` (16/32/48/64) if missing.

**`hotkeys.py`** — `Hotkeys(bindings: dict[str, str], on_hotkey: Callable[[str], None])`; `start()` spawns
T-hotkey which calls `RegisterHotKey(None, id, MOD_CONTROL|MOD_ALT|MOD_NOREPEAT(0x4000), vk)` for each binding and
pumps `GetMessageW`; `WM_HOTKEY` (0x0312) → `on_hotkey(name)`; `status() -> dict[str, bool]` (False = conflict);
`stop()` posts WM_QUIT via `PostThreadMessageW` and unregisters. Default bindings: show_window ctrl+alt+x,
push_to_talk ctrl+alt+space, stop_speaking ctrl+alt+s.

**`single_instance.py`** — `acquire() -> bool` (mutex `Local\XyrusVoiceAssistant`, keep the handle alive);
`signal_existing() -> bool` (OpenEventW + SetEvent on `Local\XyrusShowWindow`); `ShowEvent()` with `poll() -> bool`
(`WaitForSingleObject(h, 0) == 0`). F13 snippet (restype/argtypes c_void_p).

**`autostart.py`** — `enabled() -> bool`, `set_enabled(on: bool) -> None`, `target_ok() -> bool` (v1 code; the
shortcut targets `venv\Scripts\pythonw.exe` with arguments `"<BASE>\arc.py" --tray`, working dir BASE, icon
`xyrus.ico`). `create_desktop_shortcut()` used by setup.

**`app.py`** — composition root. `main(argv=None)`: parse args (`--tray`, `--text`, `--speak`, `--selftest`,
`--dry-run`, `--debug`, `--wav PATH`); `log.setup()`; single instance (second launch → `signal_existing()`, exit 0);
check `MODEL_DIR` (missing → MessageBox "Speech model missing. Run install.bat.", exit 1); build Config, Clock,
Registry (`commands.load_all`, `register_custom`), Store, MemoryStore, Speaker, Chime, Executor, WinActions (or
FakeActions with `--dry-run`), Tray (Notifier), Engine(threaded=True), AudioCapture, GrammarBuilder, Recognizer,
Hotkeys; start threads; `engine.startup()` (greeting once the recognizer reports listening, or after 5 s);
create `XyrusWindow` and run `mainloop()`; on quit: stop hotkeys, recognizer, capture, tray; `os._exit(0)`.
`--text`: no audio/tray/UI; InlineExecutor; FakeSpeaker unless `--speak`; REPL printing replies (python.exe only).
`--selftest`: run `unittest` discovery over `tests/` except `test_recognition` and `test_ui_render`, exit code.
`--wav PATH`: decode the file through `Recognizer.decode_pcm` in `command` and `free` modes and print the transcripts.

### 4.16 `testing.py` — fakes (owned by T1, used by everyone)

```python
class FakeActions:          # implements SystemActions; records calls; configurable return values
    calls: list[tuple[str, tuple, dict]]
    def __init__(self, **returns): ...          # e.g. FakeActions(volume_get=40, find_song=("Alone by Alan Walker", "https://www.youtube.com/watch?v=x"))
    def called(self, name: str) -> list[tuple]: ...
    fail: dict[str, BaseException]             # method name -> exception to raise
class FakeSpeaker:          # implements Speaker; said: list[str]; on_done called synchronously (auto_done=True) or on demand via finish()
class FakeChime, FakeNotifier (notes: list[tuple[str,str]])
def make_test_engine(*, now=dt.datetime(2026, 9, 13, 10, 30), tmpdir: Path | None = None, config_overrides=None,
                     actions=None) -> tuple[Engine, SimpleNamespace]:
    """Engine(threaded=False) with FakeClock, FakeSpeaker, FakeChime, FakeNotifier, FakeActions, InlineExecutor,
    temp CalendarStore/MemoryStore/Config (XYRUS_DATA_DIR=tmpdir), REGISTRY with commands.load_all.
    Returns (engine, ns) where ns has clock, speaker, actions, store, memory, notifier, ui_queue."""
```


---

## 5. Grammar strategy, threading and data flow

### 5.1 Facts this design rests on (all verified on this machine)
- Vosk small model `vosk-model-small-en-us-0.15`; `KaldiRecognizer(model, 16000, json_words)` restricts the
  vocabulary; `SetGrammar` hot-swaps (also `SetGrammar("[]")` → unrestricted); recognizers are cheap, the Model is the
  heavy part (load once).
- "Xyrus" is not a word in the model; it is heard as `cyrus` (also `zeros`, `virus`; `cirrus` is a sound-alike in
  the vocabulary). Free-form decoding hears it as `cyrus`/`zero`/`zira`.
- Full grammar on room noise: `how` (conf 0.75–0.88) every ~20 s; v1 log: `how` ×120, `hey` ×44, `half` ×11,
  once `virus` → a false wake. Wake-only grammar on the same noise: `""`.
- OOV speech in grammar mode is forced into grammar words at conf ≈ 1.0 ("percent" → "for set",
  "hello there how are you doing" → "how louder how are you do an"). Confidence is useless as a gate.
- Free decoding of a 3 s utterance ≈ 0.45–0.5 s. `AcceptWaveform` only returns True after trailing silence
  (endpoint rules: 0.5 / 0.75 / 1.0 s), so logic must not wait for it on WAV files without silence (tests call
  `FinalResult()`).

### 5.2 Grammar per listening mode

| Engine state | ListenMode | Grammar | Notes |
|---|---|---|---|
| idle | `wake` | `wake_words + extra + [unk]` | extra = timer words while a timer exists/rings; `cancel stop abort` during a shutdown countdown. |
| arming / armed | `command` | full command grammar (§4.12) | after the wake reply; typed text ignores modes. |
| follow_up | `wake` | `wake_words + whitelist words + [unk]` | whitelist per follow-up kind (§5.5). |
| dialogue TITLE / play-ask / type / search | `free` | unrestricted | exactly one utterance, then the dialogue changes state (and mode). |
| dialogue WHEN / TIME / timer duration | `when` | date/time/number grammar | narrow grammar removes most noise mappings. |
| dialogue CONFIRM / FIX / confirm / pick | `confirm` | yes/no/cancel + fix words (+ first/second/third/one/two/three/all for pick) | |
| paused | `paused` | stream closed | |

### 5.3 Re-decoding the same utterance (one-breath commands, songs, queries, apps)

The recognizer keeps the PCM of the current utterance (from the first chunk after the previous final, plus one
chunk of pre-roll, capped at 20 s). On a final result `text`:
1. **Wake re-decode.** If mode is `wake` and `text` contains a wake token followed by anything, or equals a wake
   token (the rest may simply be `[unk]`s): decode the utterance PCM with the (cached) `command` recognizer →
   `cmd_text` (`FinalResult()`). If `cmd_text` keeps a wake token, it becomes `text` with `mode="command",
   wake_redecoded=True`; otherwise keep the wake-mode text (the engine sees a bare wake).
2. **Free re-decode.** If (after step 1) the mode is `command` and the words after the wake token start with a free
   trigger from `registry.free_triggers()` (for `play`: any `PLAY_LIKE` token; for the calendar/to-do/note/remember adds: their `free_trigger_words` — `add schedule put new remind reminder note task remember mark tick cross finished` — so voice adds are re-decoded with the full vocabulary exactly like songs, D14) followed by ≥ 1 more token (any token,
   including `[unk]` and number words — "xyrus play alone" is heard `cyrus play eleven`), or `open|launch|start` +
   a tail containing `[unk]`: decode the same PCM with the unrestricted recognizer → `free_text`.
3. In `free` mode the utterance is already unrestricted: `text = free_text = result`.
4. Emit `Transcript(text, mode, free_text, words, peak, t_start, t_end, wake_redecoded)`.
Latency budget: wake final + command re-decode ≤ 0.4 s; + free re-decode ≤ 0.6 s more.

### 5.4 Noise rejection (in order)
1. Idle uses the wake-only grammar (primary defence).
2. Energy: a mic utterance with `peak < config.min_speech_peak` (default 0.02) is noise (v1 near-silence noise peaks at ≈ 0.0012).
3. Empty / all-`[unk]` results are dropped (except in `free` mode, where empty keeps waiting).
4. In armed mode an unmatched single word is noise and does not consume the window; commands with
   `armed_ok_single_word=False` (copy, paste, undo, save, refresh, stop, skip) need the wake word in the same utterance.
5. In dialogues: unparseable ≤ 1 word is swallowed; ≥ 2 words costs an attempt (prototype rule).
6. Follow-up accepts only whitelisted phrases without the wake word.
7. Destructive intents with `[unk]` in the command region are never executed directly (D11).
8. Echo gate: chunks inside a TTS interval (+0.35 s tail) are dropped and the recognizer reset, so Xyrus never hears
   itself; the wake chime is **not** gated (200 ms tone, no speech).
9. Noise events are logged at DEBUG and kept as Activity kind `noise` (hidden by default), never spoken.

### 5.5 Follow-up whitelists (no wake word)

| Kind | Opened by | Seconds | Accepted |
|---|---|---|---|
| `alert` | reminder/event alert | `follow_up_after_alert_s` (20) | okay, ok, got it, thanks, thank you, done, dismiss, snooze, remind me again in <duration>, what's next, and after that |
| `timer` | timer ringing | 60 | stop, okay, thanks, thank you, got it, snooze |
| `briefing` / `day` | briefing, agenda | 20 / `follow_up_s` | yes, sure, read it, read the notes, no, no thanks, and after that, thanks |
| `next` | what's next | `follow_up_s` (8) | and after that, what's after that, next one, thanks |
| `recall` | recall | `follow_up_s` | more, the rest, next ones, thanks |
| `shutdown` | shutdown/restart countdown | delay + 5 | cancel, stop, abort |
| `greeting` | startup greeting | 20 | what's my day like, what's next, thanks |
| `added` | any voice add (event, reminder, to-do, note) | 15 | undo that, scratch that, delete that, remove that, thanks |
Commands used here are registered with `follow_up=True`; the engine's whitelist decides which are live.

### 5.6 Normalisation table (applied by `normalize.normalize`, in this order)

| Heard | Becomes | Condition |
|---|---|---|
| `hey cyrus`, `hi cyrus`, `okay cyrus` | `cyrus` | leading greeting before a wake token |
| `whats` | `what's` | always |
| `a m`, `p m` | `am`, `pm` | always |
| `o'clock`, `oclock` | (removed) | always |
| `un mute`, `sound on`, `turn the sound on` | (unchanged; phrases) | "unmute" is not in the model (D13) |
| `add in event`, `add and event`, `add a event` | `add an event` | always |
| `remembered at`, `remember at` | `remember that` | at utterance start (after wake) |
| `what did are ask` | `what did i ask` | always |
| `can` | `ten` | directly before a time unit (`timer four can minutes` → `ten minutes`) |
| `four` | `for` | after `note`/`reminder`/`timer`/`event` and before a day or number word (`add note four tomorrow`) — durations/percent handle for/four themselves by "last number wins" |
| `to`, `two` | dropped | directly before a number word inside a percent slot (`set volume two forty` → 40) |
| `add` | `at` | before a number/time word, inside date parsing only (`dates.normalise`) |
| `eighth`, `seventh`, … | `8`, `7`, … | hour position after `at`, inside date parsing only |
| `for` | `4` | after a calculator operator (`twelve times for`) |
| wake aliases `cyrus zeros virus cirrus` (+ `xyrus` typed) | kept, detected by `strip_wake` | — |

### 5.7 Threading model and data flow

```
 mic ──PortAudio cb (T-audio-cb)──► capture.chunks Queue[(pcm, t_mono)]
                                          │
                              T-rec: Recognizer loop ◄── get_spec() ◄── Engine.listen_spec()  (immutable ListenSpec)
                              │  echo gate ◄── Speaker.is_quiet_at(t)
                              │  wake chime ──► Chime.play (winmm, async)
                              │  decode / re-decode (Vosk only here)
                              ▼
                   engine.submit_transcript(Transcript) ──► Engine inbox Queue
 UI text box / banner ──engine.submit_text──────────────────► Engine inbox
 hotkeys (T-hotkey) ──engine.submit_hotkey──────────────────► Engine inbox
 tray menu (T-tray) ──engine.set_paused / ui_queue──────────► Engine inbox / ui_queue
 Speaker on_done (T-speak) ──engine.post(speech_done)───────► Engine inbox
 Executor on_done (T-exec) ──engine.post(then/error)────────► Engine inbox
                                          │
                     T-engine: dispatch → handle → registry.match → handler(ctx, match)
                                          │           │            │
                   Speaker.say ◄──────────┘   Executor.submit   store / memory / timers (locked)
                   (T-speak, SAPI)            (T-exec: WinActions, YouTube, psutil, brightness)
                                          │
                              EngineSnapshot (immutable) ──polled 150 ms──► T-main (Tk)
                              ui_queue ("show", "show:Calendar", "quit") ──polled──► T-main
                              AudioCapture.level() ──polled──► T-main
```

Cross-thread rules:
1. Hand-offs are `queue.Queue`, `threading.Event`, immutable dataclasses, or methods documented "thread-safe"
   (they lock internally). No thread reads another thread's mutable internals.
2. Tk is touched only on T-main (G4). Tray callbacks and hotkeys never call Tk.
3. Vosk only on T-rec, SAPI only on T-speak, COM per thread with `CoInitialize` (G5).
4. `CalendarStore`, `MemoryStore`, `Config` are locked and may be called from T-main (UI edits), T-engine, T-exec.
   The UI refreshes from `store.version`; voice edits appear in an open window within 150 ms.
5. No `time.sleep` on T-audio-cb, T-rec, T-engine (queue timeouts only). Sleeps are allowed on T-exec (routine
   `wait` steps), T-watch, T-keepawake.
6. Shutdown: `app.stop_event.set()`; join threads with 2 s timeouts; `os._exit(0)`.

---

## 6. Configuration and data files

### 6.1 `D:\arc\config.json` (schema version 2; defaults = `config.DEFAULTS`)

```jsonc
{
  "version": 2,
  "voice_replies": true,                  // v1 key kept; alerts/timers always speak (force)
  "confirm_shutdown": true,               // v1 key kept
  "shutdown_delay_s": 10,
  "address_as": "sir",                    // honorific; "" drops it
  "user_name": "",                        // used only in greetings
  "wake_words": ["cyrus", "zeros", "virus", "cirrus"],
  "wake_replies": ["Waiting for your command, sir.", "At your service, sir.", "Yes, sir?", "I'm listening, sir."],
  "command_window_s": 15,
  "confirm_window_s": 12,
  "follow_up": true,
  "follow_up_s": 8,
  "follow_up_after_alert_s": 20,
  "dialogue_timeout_s": 20,
  "chime_on_wake": true,
  "startup_greeting": "speak",            // "speak" | "toast" | "off"
  "briefing_on_startup": true,
  "min_speech_peak": 0.02,
  "mic": {"name": null, "hostapi": "MME"},   // null name = Windows default input
  "tts": {"voice": null, "rate": 0, "volume": 100},   // voice = SAPI description or null (default voice)
  "youtube_lookup": true,
  "hotkeys": {"show_window": "ctrl+alt+x", "push_to_talk": "ctrl+alt+space", "stop_speaking": "ctrl+alt+s"},
  "apps": {                                // spoken name -> target (v1 list + deep links, §3.5)
    "chrome": "chrome", "browser": "chrome", "edge": "msedge", "youtube": "https://www.youtube.com",
    "spotify": "spotify:", "notepad": "notepad", "calculator": "calc", "explorer": "explorer", "files": "explorer",
    "code": "code", "settings": "ms-settings:", "task manager": "taskmgr", "terminal": "wt", "discord": "discord:",
    "bluetooth settings": "ms-settings:bluetooth", "sound settings": "ms-settings:sound",
    "display settings": "ms-settings:display", "network settings": "ms-settings:network",
    "windows update": "ms-settings:windowsupdate", "downloads": "shell:Downloads", "documents": "shell:Personal",
    "pictures": "shell:My Pictures", "recycle bin": "shell:RecycleBinFolder", "device manager": "devmgmt.msc",
    "control panel": "control"
  },
  "custom_commands": [
    // {"phrase": "movie mode", "enabled": true,
    //  "steps": [{"action": "command", "arg": "set brightness to forty percent"},
    //            {"action": "open", "arg": "https://www.netflix.com"}, {"action": "wait", "arg": 5},
    //            {"action": "keys", "arg": "f11"}]}
  ],
  "calendar": {
    "default_reminder_min": 10, "all_day_reminder_time": "09:00", "catch_up_min": 120, "snooze_minutes": 10,
    "morning_brief": true, "morning_brief_time": "08:30", "waking_hours": [7, 23],
    "repeat_unacked_after_min": 2, "toast": true
  },
  "quiet_hours": null,                    // or {"from": "23:00", "to": "07:00"}: lead alerts skipped, due alerts still speak
  "weather": {"enabled": false, "city": ""},
  "window": {"geometry": null, "last_tab": "Home", "show_noise": false}
}
```
Migration: a v1 file (no `version`) keeps `voice_replies`, `confirm_shutdown`, `apps` (merged over the new defaults,
user entries win); everything else from DEFAULTS; `version` set to 2 on first save.

### 6.2 `D:\arc\data\calendar.json` (owned by `CalendarStore`; identical to v1's file, D14)

```jsonc
{
  "version": 1,
  "items": [
    {"id": "it_dff16c7b", "kind": "event", "title": "Dentist", "date": "2026-09-14", "time": "15:00",
     "all_day": false, "reminder_min": 10, "repeat": null, "done": false, "source": "voice",
     "created": "2026-09-13T10:00:00", "fired": {"2026-09-14": "fired@2026-09-14T14:50:03"}},
    {"id": "it_0a1b2c3d", "kind": "task", "title": "Buy milk", "date": "2026-09-13", "time": null,
     "all_day": true, "reminder_min": null, "repeat": null, "done": false, "source": "ui",
     "created": "2026-09-13T08:00:00", "fired": {}}
  ],
  "notes": [{"id": "nt_1a2b3c4d", "date": "2026-09-13", "text": "buy milk", "source": "voice", "created": "2026-09-13T08:01:00"}],
  "state": {"last_brief": "2026-09-13", "pruned": "2026-09-13", "acked": {"it_dff16c7b": ["2026-09-14"]}}
}
```
Fields: `id` "it_"/"nt_" + 8 hex; `kind` "event" | "task" (to-do); `date` ISO (first occurrence); `time` "HH:MM" or
null (all day); `reminder_min` null = never reminds, 0 = at the time (a spoken "reminder" is an event with 0),
N = N minutes before; `repeat` null | "daily" | "weekly" (events only); `done` (to-dos); `source` "voice" | "ui";
`fired` occurrence date → "fired@ISO" | "missed@ISO". Invariants: written by v1 `calendar_store.py` today and by
v2 `store.py` later — same format; marked fired before speaking; moving an item (date/time/reminder_min/repeat)
resets `fired`; done to-dos never remind; a prototype-era top-level `events` list is migrated into `items`
(kind "event") on load; queries expand recurrences on the fly.

### 6.3 `D:\arc\data\memory.json`
`[{"id": "m_3f9a2c1e", "text": "the wifi password is banana seven", "created": "2026-09-13T14:00:00", "source": "voice"}]`

### 6.4 Other files
`data\vocab_cache.json` (`{"model": "<MODEL_DIR>", "words": {"kubernetes": false, ...}}`), `data\stdout.log`,
`arc.log` (+ `.1`–`.3`), `crash.log`, `Pictures\Xyrus\*.png`. `XYRUS_DATA_DIR` redirects `data\` and `config.json`
for tests. None of these are copied to another PC.


---

## 7. Testing

### 7.1 Runner and layout
- stdlib `unittest` (no pytest dependency): `D:\arc\venv\Scripts\python.exe -m unittest discover -s tests -v`.
  Every test file must also be runnable alone: `python -m unittest tests.test_engine`.
- `tests/` files: `test_parsing`, `test_registry`, `test_replies`, `test_engine`, `test_dialogue` (T1);
  `test_dates`, `test_store`, `test_reminders`, `test_assistant`, `test_calendar_flows` (T2);
  `test_grammar`, `test_speech`, `test_recognition` (T3); `test_commands`, `test_songs`, `test_actions_smoke` (T4);
  `test_ui_render`, `test_shell` (T5); `test_app_smoke` (T6). Shared helper `tests/wav_util.py` (T3):
  `synth(text) -> bytes` (16 kHz mono PCM via PowerShell System.Speech exactly as v1 `test_arc.py`, cached under
  `tests/_cache/` by text hash), `noise(seconds, amp, seed) -> bytes`, `silence(seconds) -> bytes`.
- Every test that writes data sets `XYRUS_DATA_DIR` to a `tempfile.mkdtemp()` directory.
- Tests needing hardware/network skip cleanly: `@unittest.skipUnless(os.environ.get("XYRUS_LIVE"), ...)` for the
  actions smoke test; network tests skip when `socket.create_connection(("1.1.1.1", 53), 1.5)` fails.

### 7.2 Headless engine tests (`make_test_engine`, FakeClock at Sun 2026-09-13 10:30, FakeActions, FakeSpeaker)

Each row: inputs via `engine.handle(text, "mic")` (wake word required) unless marked typed; assertions on
`ns.speaker.said`, `ns.actions.calls`, `engine.snapshot()`, `ns.store`.

| # | Input sequence | Expect |
|---|---|---|
| E1 | "cyrus" | said ∈ wake_replies; mode `armed`; window 15 s starts after the reply (FakeSpeaker auto_done) |
| E2 | "cyrus" → advance 16 s → tick → "lock" | window expired; "lock" ignored (noise) |
| E3 | "cyrus" → advance 10 s → "lock" | `lock` called after "Locking, sir." finished |
| E4 | "cyrus open chrome" | `open_target("chrome")`, said "Opening Chrome, sir." |
| E5 | "how", "hey", "half" (idle, no wake) | nothing said, no calls, events kind `noise` |
| E6 | "cyrus set a timer for five minutes" → advance 300 s → tick | said "Timer set for five minutes, sir."; later "Time's up, sir!" forced; `screen_on` called; notifier toast |
| E7 | v1 timer table (all 12 cases of v1 `test_arc.py`, e.g. "cyrus timer four seven minutes" → 420) | exact seconds |
| E8 | "cyrus timer ten minutes" → "cancel timer" (no wake) | "Timer cancelled, sir." |
| E9 | "cancel timer" with no timer, no wake | noise (not spoken) |
| E10 | "cyrus shut down" → "yes" | confirm question; then `shutdown(10)`; follow-up `shutdown` open; "cancel" (no wake) → `abort_shutdown` |
| E11 | "cyrus shut down" → advance 13 s → tick → "yes" | confirm expired silently; "yes" is noise; no shutdown |
| E12 | "cyrus shut [unk] down" (destructive + unk) | "Did you say shut down, sir?" confirm, no direct call |
| E13 | "cyrus set volume to forty percent" / "cyrus set volume two forty" | `volume_set(40)` both; said "Volume at 40 percent, sir." |
| E14 | "cyrus play" / "cyrus pause" / "cyrus resume" | `media("play_pause")`, no `find_song` |
| E15 | `handle("cyrus play eleven", "mic", free_text="cyrus play alone")` | `find_song("alone")`, `open_target(watch url)`, said "Playing Alone by Alan Walker, sir." |
| E16 | "cyrus play some music" → "how" → free "blinding lights" | "What should I play, sir?"; noise ignored; `find_song("blinding lights")` |
| E17 | "cyrus play music" → "cancel" | "Okay, sir."; no lookup |
| E18 | FakeActions(find_song raises OSError) + "cyrus play alone" (typed path) | "I couldn't reach YouTube, sir. Opening the search instead." + results URL opened |
| E19 | "cyrus what can you do" | `ui_queue` has "show:Commands" |
| E20 | "cyrus next event" vs "cyrus next" | agenda reply vs `media("next")` |
| E21 | "cyrus blah blah" | "Sorry, I didn't catch that, sir." then armed |
| E22 | typed "open notepad" (no wake) | runs (wake optional for typed); "no" inside "notepad" never cancels |
| E23 | paused → typed "what time is it" | works; mic transcripts ignored while paused |
| E24 | every reply template in `replies.py` + every literal said in E1–E23 | contains no wake word (G6) |
| E25 | "cyrus what is twelve times for" | "48" |
| E26 | "cyrus good morning" at 15:00 with 2 events | starts "Good afternoon, sir." ≤ 4 sentences |

### 7.3 Dialogue and calendar flows (T1 + T2)
- The 9 `dialogue_proto.py` scenarios, against the real engine + real `dates` + temp `CalendarStore`, with exact
  replies (§3.9): full flow; prefilled date/time; title carrying "tomorrow at nine am"; "at seven" at 14:00 → 7 PM
  today; no → the time → at nine am; cancel mid-dialogue; noise swallowed / garbage costs an attempt / two → give up;
  nudge at 20 s, abandon at 40 s; all-day via "all day". Plus: construction-order regression (deadline armed).
- v1 `when.py` self-test table (57 cases + extras) + the `parse_range` spans of §4.11 → `test_dates.py`, plus the normalisation rows of §5.6.
- v1 `calendar_store.py` behaviour → `test_store.py` / `test_reminders.py`: the `sched_proto.py` scenario (stale
  on first tick, all-day 09:00, never twice, restart, late after sleep, stale next day, edit clears fired); v1
  `data/calendar.json` compatibility (load a copy of the real file when present, else a fixture written by the v1
  module); to-dos (add, `set_done`, done to-dos never remind, overdue); `undo_last` (voice adds only); `find` fuzzy
  matches; `counts_by_day`; exact `describe_range` / `describe_next` / `describe_todos` text; acknowledge/snooze.
- Calendar flows (typed and mic): "cyrus remind me at five to call mom" → one-shot "I'll remind you at 5 PM to call
  mom, sir."; "cyrus add a note for tomorrow" → "bring the charger" → note saved; "cyrus read my notes for tomorrow";
  "cyrus delete the dentist event" → "yes"; "cyrus clear today's notes" → "yes"; "cyrus what's on today"; "cyrus
  what's next" → "and after that"; alert at t−10 and t with toast + forced speech; "snooze"; missed catch-up after a
  3-h clock jump spoken once; to-dos: "cyrus add a to do buy milk tomorrow" (with `free_text`) → task saved,
  "cyrus what's on my to do list", "cyrus mark buy milk as done" → done; spans: "cyrus what do i have these three
  days" / "the next ten days" / "on friday" / "rest of the week" → `describe_range` over the `parse_range` span;
  "undo that" (no wake word, within 15 s) after a voice add removes it; remember → recall → forget.

### 7.4 Recognition tests with synthesized speech (T3) — `test_recognition.py`
Use `Recognizer.decode_pcm` (same code path as the mic, no audio device) with WAVs from `wav_util.synth`.
1. **Command set:** ≥ 60 phrases, each "xyrus <phrase>" for every must command, decoded in `wake` mode (so the wake
   re-decode is exercised) → `registry.match` must give the expected command. Required pass rate 100 % for the 17
   v1 phrases of `test_arc.py` and ≥ 95 % overall; failures are printed with the heard text.
2. **Songs:** "xyrus play alone" → `free_text` title "alone"; same for shape of you, believer by imagine dragons,
   faded by alan walker, blinding lights (exact).
3. **Dialogue words:** in `when` mode "tomorrow at three pm", "next friday at ten am", "the fifteenth of october",
   "in forty five minutes" exact; in `confirm` mode yes/no/cancel/correct exact; in `free` mode "dentist
   appointment", "buy milk and eggs" exact.
4. **Noise:** 6 s white noise (amp 600) and 25 s near-silence (amp 40) in `wake` mode → no transcript containing a
   wake word; the same through `handle_transcript` produces no speech.
5. **Vocabulary:** every word of `GrammarBuilder.words("command")` is known to the model (`check_words`), i.e. nothing
   is silently dropped (catches `unmute`-style mistakes, D13).
6. **Echo gate:** `SapiSpeaker.is_quiet_at` false during a spoken test sentence, true 0.4 s after (`test_speech.py`).

### 7.5 Safe actions smoke test (T4) — `test_actions_smoke.py` (needs `XYRUS_LIVE=1`)
Allowed and restored: volume get → set 37 → read 37 → restore; mute on/off → restore; brightness get → set the
same value; clipboard round-trip with unicode → restore; launch Notepad → `focus_app("notepad")` → snap left/right
(assert rect halves of the work area) → maximize/restore → `keys("ctrl+a")`/type "xyrus test" → read back via
clipboard → `close_app("notepad")` (WM_CLOSE; discard dialog handled by typing into a new untitled doc only);
`windowed_apps()` contains "notepad"; `screenshot()` to a temp dir; `system_status()` keys; `find_song("alone")`
(network). **Never** called by any test: `shutdown`, `restart`, `sleep`, `hibernate`, `lock`, `sign_out`,
`screen_off`, `kill_app`, `set_theme` — the test module asserts this by wrapping `WinActions` in a guard that raises
on those names.

### 7.6 UI render test (T5) — `test_ui_render.py`
Pattern of `SCRATCH\shot_commands.py`: build `XyrusWindow` against a `FakeApp` (§9.1) using `make_test_engine`
(seeded with 3 events, 2 notes, 1 timer, a pending dialogue for the banner), geometry 1000×700; for each tab
(`Home`, `Calendar`, `Commands`, `Apps`, `Routines`, `Settings`): select, `update()`, sleep 0.6 s, `update()`,
grab `FindWindowW(None, "Xyrus")` rect with `ImageGrab`, save `tests/_out/tab_<name>.png`; assert the image is not
uniform and the Commands count label matches `N commands · M ways to say them` with N ≥ 60; filter "timer" leaves only
timer rows. Also a snapshot-poll test: feed engine events and assert the Home feed row count and the header text
"Paused · mic off" when paused.

### 7.7 App smoke (T6) — `test_app_smoke.py`
Launch `venv\Scripts\pythonw.exe arc.py --tray` with `XYRUS_DATA_DIR` temp → within 15 s `arc.log` shows
"listening"; launch a second instance → it exits 0 within 5 s and the first instance's window becomes visible
(`IsWindowVisible(FindWindowW(None,"Xyrus"))`); `python arc.py --text` with stdin "what time is it\n" prints a reply
starting "It's"; `python arc.py --selftest` exits 0. Kill only the processes this test started.

---

## 8. Installer, requirements, README

### 8.1 `requirements.txt` (exact)
```
vosk==0.3.45
sounddevice==0.5.6
comtypes==1.4.16
pystray==0.19.5
pillow==12.3.0
customtkinter==6.0.0
psutil==7.2.2
pycaw==20251023
pywin32==312
screen_brightness_control==0.27.2
keyboard==0.13.5
parsedatetime==2.6
```
(WMI 1.5.1, darkdetect, packaging arrive as dependencies. `pyttsx3` is installed in the current venv but is **not**
a requirement and must never be imported. `requests` is not needed.)

### 8.2 `setup.ps1` (keep v1 structure, idempotent, `-NoStartup` switch kept)
1. Python ≥ 3.10 detection (`python`, then `py`); message + exit 1 if missing.
2. venv create if missing; `pip install --quiet --disable-pip-version-check -r requirements.txt`.
3. Model download if `model\am\final.mdl` missing (v1 code, same URL).
4. `New-Item -ItemType Directory -Force data`.
5. Icon + shortcuts: `& $venvPy -c "import sys; sys.path.insert(0, r'$here'); from xyrus import tray, autostart; tray.ensure_icon_file(); autostart.create_desktop_shortcut()" + (unless -NoStartup) "; autostart.set_enabled(True)"`
   — `Xyrus.lnk` in the folder and on the desktop target `venv\Scripts\pythonw.exe "<here>\arc.py"`.
6. Checks: `& $venvPy -c "from xyrus import audio; print('mic:', [m.name for m in audio.list_mics()][:1])"`;
   `& $venvPy "$here\arc.py" --selftest` → print "self-test passed" or the failing test names (do not abort the install).
7. Launch `pythonw arc.py` (not `--tray`) unless `-NoStartup`; print `Say: "Xyrus, what can you do"`.
`install.bat` unchanged. Files to copy to another PC: `arc.py xyrus\ tests\ docs\ requirements.txt setup.ps1
install.bat README.md` (not `venv\ model\ data\ legacy\ config.json *.log *.lnk`).

### 8.3 README.md (rewrite)
What it is; setup on a new PC (as v1, updated file list); how to talk to it (wake word, the 15 s window after
"Waiting for your command, sir.", one-breath commands, typing instead of talking); command highlights by section
with a pointer to the Commands tab ("say 'what can you do'"); calendar and reminders; play songs; routines; settings;
hotkeys; pause = mic off; privacy (offline except YouTube lookup and opt-in weather); behaviour changes from v1
("how are you" is small talk now; bare "play" still toggles media); troubleshooting (mic privacy setting, MME device,
wake variants, `arc.log`, `--text`, `--selftest`); uninstall; files.

---

## 9. Work breakdown (4 parallel tasks + T5 UI + T6 integration)

### 9.1 Shared interfaces beyond `interfaces.py`
- `xyrus.registry.command` decorator, `Registry`, `Match`, `Command` (§4.5) — T1.
- `Engine` public methods and `Ctx` surface (§4.7, §4.8) — T1. `make_test_engine` (§4.16) — T1.
- Slot types table (§4.6) — T1; `when`/`day` slot parsers delegate to `xyrus.dates` (T2).
- `CalendarStore` / `MemoryStore` / `reminders` / `assistant` APIs (§4.10) — T2.
- `AudioCapture`, `Recognizer`, `GrammarBuilder`, `Vocab`, `SapiSpeaker`, `WinmmChime` (§4.12) — T3.
- `WinActions` and area functions (§4.13) — T4.
- `App` object (constructed by T6's `app.py`, faked by T5's `FakeApp`) exposes exactly: `config`, `clock`,
  `registry`, `engine`, `store`, `memory`, `speaker`, `chime`, `capture` (`level()`, `status()`, `start()`, `stop()`),
  `recognizer` (`save_last_seconds`), `vocab` (`unknown_words`), `hotkeys` (`status()`), `tray` (`set_state`,
  `set_tooltip`), `notifier`, `show_event` (`poll()`), `ui_queue: queue.Queue[str]`, `set_paused(bool)`, `quit()`.
- **Until a dependency lands**, code against the spec text: T1 commits `interfaces.py`, `paths.py`, `config.py`,
  `clock.py`, `registry.py`, `parsing.py` (slot table), `replies.py` skeleton and `testing.py` fakes as its first
  milestone **M0** (target: first 2–3 hours), and a working `Engine.handle` + `Ctx.say/do/confirm/ask` + `make_test_engine`
  as **M1**. Other tasks build their unit-level code first (actions functions, parser/store, recognizer, UI against
  `FakeApp`) and switch to `make_test_engine` when M1 exists. Nobody edits another task's files; interface bugs are
  reported to the lead, who decides and the owner changes the file.

### 9.2 Tasks, owned files, consumed interfaces, acceptance

**T1 — Engine core** (owner of the contract)
- Owns: `xyrus/__init__.py`, `interfaces.py`, `paths.py`, `config.py`, `log.py`, `clock.py`, `replies.py`,
  `persona.py`, `normalize.py`, `parsing.py`, `registry.py`, `engine.py`, `dialogue.py`, `executor.py`, `timers.py`,
  `testing.py`; `tests/__init__.py`, `tests/test_parsing.py`, `test_registry.py`, `test_replies.py`, `test_engine.py`,
  `test_dialogue.py`.
- Consumes: `dates.parse_when` (T2) inside `dialogue.py` (until it lands, the prototype's `fake_parse_when` shape);
  command modules (T2/T4) only through `commands.load_all` in `make_test_engine`.
- Acceptance: §7.2 E1–E14, E19–E25 green (E15–E18 with T4's `commands/web.py`, E26 with T2), §4.5 ordering examples,
  §5.6 rows, the 9 dialogue scenarios with a stub store, G6 test, no `time.time()`/`datetime.now()` in G8 modules
  (grep test), executor timeout/worker-replacement test.

**T2 — Assistant, calendar, time**
- Owns: `dates.py`, `store.py`, `reminders.py`, `memory.py`, `assistant.py`, `commands/calendar.py`,
  `commands/assistant.py`, `commands/timers.py`; `tests/test_dates.py`, `test_store.py`, `test_reminders.py`,
  `test_assistant.py`, `test_calendar_flows.py`.
- Lift: `D:\arc\when.py` → `dates.py` (+ §4.11 additions); `D:\arc\calendar_store.py` → `store.py` + `reminders.py` +
  the speech functions of `assistant.py`. Must stay file-compatible with v1 `data/calendar.json`. Coordinate phrase
  wording with whatever v1 `arc.py` ships for the calendar (v1 wording wins, D14).
- Consumes: registry decorator, `Ctx`, `Dialogue`/`AskDialogue`/`PickDialogue`/`ConfirmDialogue`, `TimerService`,
  `persona` (T1).
- Acceptance: §7.3 all green; 57/57 date cases; alerts fire exactly once across restart; §3.8 and §3.9 replies exact.

**T3 — Audio, recognition, speech**
- Owns: `grammar.py`, `audio.py`, `recognizer.py`, `speech.py`; `tests/wav_util.py`, `test_grammar.py`,
  `test_speech.py`, `test_recognition.py`.
- Consumes: `Transcript`, `ListenSpec`, `Speaker`, `Chime` (interfaces), `registry` (literal words, free triggers,
  slot words), `normalize.normalize`, `config`.
- Acceptance: §7.4 all green; pause closes the stream (no callbacks for 2 s after `stop()`); watchdog reopens after a
  simulated stall (inject by stopping the callback) within 5 s; `SapiSpeaker.stop()` cuts speech within 200 ms and
  calls every pending `on_done`; chime returns in < 5 ms; recognizer never blocks > 1 s on an empty queue.

**T4 — Actions and commands**
- Owns: `winutil.py`, `actions/*` (all 12 files), `commands/__init__.py`, `commands/core.py`, `power.py`,
  `sound.py`, `display.py`, `windows.py`, `apps.py`, `info.py`, `web.py`, `keys.py`, `clipboard.py`, `fun.py`,
  `custom.py`; `tests/test_commands.py`, `test_songs.py`, `test_actions_smoke.py`.
- Lift: v1 `actions.py` (power, VK, screenshot, youtube functions, `find_song`), v1 `arc.py` song helpers
  (`is_song_request`, `words_after_play`, `clean_song_title`, `SONG_ASK`, `PLAY_LIKE`, `NOISE_WORDS`) into
  `commands/web.py`/`normalize` data (coordinate `NOISE_WORDS` with T1: T1 owns `normalize.py`, T4 imports it),
  feasibility snippets F1, F2, F7, F8, F10, F12.
- Consumes: `SystemActions`, registry decorator, `Ctx`, slot types, `ConfirmDialogue`/`AskDialogue` (T1).
- Acceptance: §7.2 E4, E10–E18, E20, E22, E25 through `make_test_engine`; every §3.1–3.7 and §3.10 row has a
  registered pattern and a `test_commands.py` case (typed input → expected FakeActions call + reply); §7.5 green on
  this PC with `XYRUS_LIVE=1`; v1 `test_songs.py` expectations re-expressed.

**T5 — UI and shell**
- Owns: `ui/*` (all files), `tray.py`, `hotkeys.py`, `single_instance.py`, `autostart.py`; `tests/test_ui_render.py`,
  `test_shell.py`.
- Lift: `D:\arc\calendar_tab.py` (v1 Calendar tab with to-do tick boxes, being built now) → `ui/calendar_tab.py`,
  adapted to `App` and `ui/theme.py`.
- Consumes: `EngineSnapshot`, `DialogueView`, `Event`, `registry.sections`, `CalendarStore`, `dates.parse_typed`,
  `audio.list_mics`, `Vocab.unknown_words`, `Speaker.voices`, `App` (§9.1).
- Acceptance: §7.6 green with screenshots of all six tabs; single-instance test (two processes, event signalled, first
  shows); hotkey registration test (inject Ctrl+Alt+X with `keybd_event` + real scan codes → `on_hotkey("show_window")`
  within 1 s; conflict reported when the combo is already registered by another test thread); autostart
  create/remove/`target_ok` against a temp Startup folder path parameter; tray state images render.

**T6 — Integration (after T1–T5 are green)**
- Owns: `arc.py`, `xyrus/app.py`, `requirements.txt`, `setup.ps1`, `install.bat`, `README.md`,
  `tests/test_app_smoke.py`, `legacy/v1/`.
- Steps: 1) write `app.py` (§4.15) and the 3-line `arc.py`; 2) migrate v1 behaviour: config v1 → v2 (§6.1), the
  Startup shortcut stays valid (same target path and args), all v1 phrases still work (v1 `test_arc.py` cases +
  `test_songs.py` cases pass through v2), the lead's latest song work in v1 `arc.py`/`actions.py` is carried over
  (diff them against `legacy/v1` copies at the time of integration); v1 `data\calendar.json` stays in place and v2
  opens it unchanged; every v1 calendar voice phrase keeps working; 3) move `actions.py ui.py config.py test_arc.py
  test_songs.py when.py calendar_store.py calendar_tab.py` (and any v1 calendar tests) and the old `arc.py` into `legacy\v1\` (never delete — D:\arc is not a git repo); 4) update
  requirements/setup/README (§8); 5) run the full suite (`python -m unittest discover -s tests -v`, with
  `XYRUS_LIVE=1` once), `test_recognition`, `test_ui_render`, `test_app_smoke`; 6) run it live for 10 minutes idle and
  confirm `arc.log` has no executed command from noise; 7) run `install.bat` in a copy of the folder at another path
  (e.g. `C:\XyrusTest`) with `-NoStartup` and confirm self-test passes, then remove the copy.
- Acceptance: everything above green; §3 must rows spot-checked by voice on this PC (wake reply + window, one-breath
  command, timer + cancel without wake, play a song, add an event by dialogue, reminder fires with toast + speech,
  pause shows "Paused · mic off", second launch focuses the window, Start with Windows toggle).

---

## Appendix A — Evidence index

Feasibility report items (journal `wf_9e06a30a-3e0`, all **works: true** on this PC):
F1 pycaw 20251023 volume/mute via `AudioUtilities.GetSpeakers().EndpointVolume` (import `pycaw.utils`) ·
F2 screen_brightness_control on LG ULTRAGEAR via DDC/CI (0.1–0.15 s per call) ·
F3 `keyboard` hotkeys/push-to-talk/`write` (gotchas: `send` is invisible to its own hooks; inject with real scan codes; stuck SHIFT hazard) ·
F4 pystray `Icon.notify` toast from a detached icon, any thread ·
F5 Vosk free-form transcription + wake word in partials ~2.5 s before the final; handoff to a fresh free recognizer works ·
F6 SAPI async speak + `WaitUntilDone(50)` + owner-thread purge (cross-thread purge fails) ·
F7 EnumWindows/force-foreground/ShowWindow/SetWindowPos snap (Win+arrow triggers Snap Assist) ·
F8 `taskkill` by image name + windowed-apps list (exclude explorer, applicationframehost, own pythonw pair) ·
F9 chime via `winmm.PlaySoundW` SND_MEMORY|SND_ASYNC with a module-level buffer (winsound refuses) ·
F10 clipboard via ctypes with OpenClipboard retry ·
F11 wttr.in JSON (3.0 s observed; use 8 s) ·
F12 virtual desktops via Win+Ctrl chords with stuck-modifier release ·
F13 named auto-reset event for second-instance signalling ·
F14 sounddevice: MME/DirectSound at 16 kHz work, WASAPI rejects 16 kHz; persist name+hostapi ·
F15 `keyboard` works under pythonw (no console, no admin).
Snippets: `SCRATCH\feas\t01_pycaw.py` … `t17_snapassist.py` (runnable references for T4).

Prototypes and probes in `SCRATCH\`:
| File | Use |
|---|---|
| `when_proto.py` | Superseded by `D:\arc\when.py` (its lifted, extended copy) — lift **that** file (T2); the prototype's test table still applies. |
| `sched_proto.py` | Superseded by `D:\arc\calendar_store.py` (extended: items with kind event/task, undo, find, summaries) — lift **that** file (T2); the prototype scenario becomes a test. |
| `dialogue_proto.py` | **Lift nearly as-is** → `xyrus/dialogue.py` + the 9 scenarios in `tests/test_dialogue.py` (T1). |
| `recog_check.py`, `dictation_check.py`, `probe.py` + `probe_out.json`, `probe_vosk.py` | patterns for `tests/test_recognition.py` (T3): grammar vs free decode, SetGrammar hot-swap, noise behaviour. |
| `vocab_check.py`, `vocab_v2.py` | vocabulary checks (missing: unmute, unmuted, whatsapp, vlc, backspace, hotkey, lofi, oclock, xyrus, a.m., p.m.) → `tests/test_grammar.py`. |
| `recog_song.py`, `yt_check.py` | song re-decode and YouTube lookup references → `tests/test_songs.py` (T4). |
| `shot_commands.py` | UI screenshot pattern → `tests/test_ui_render.py` (T5). |
| `datetest.py`, `lib_check.py`, `datelibs\` | evidence that parsedatetime/dateparser mis-parse spoken phrases (fallback for typed text only). |

v1 code that is the verified implementation of the song feature (lead, 2026-09-13): `D:\arc\arc.py`
(`PLAY_LIKE`, `SONG_ASK`, `NOISE_WORDS`, `CANCEL_REPLIES`, `is_song_request`, `words_after_play`, `clean_song_title`,
`Arc.handle(text, pcm)`, `speech_finished`, `free_decode`, `_song_title`, `_song_request`, `ask_song`, `_song_answer`,
`_play_song`, listener keeping ≤ 20 s PCM per utterance), `D:\arc\actions.py` (`youtube_search_url`, `_secs`,
`youtube_search`, `spoken_title`, `find_song`, `MAX_SONG_SECS`), `D:\arc\test_songs.py`.

v1 calendar (built 2026-09-13 in parallel, D14) — the lift sources for v2's calendar: `D:\arc\when.py`,
`D:\arc\calendar_store.py`, `D:\arc\calendar_tab.py`; data file `D:\arc\data\calendar.json` (kept and reused by v2).
