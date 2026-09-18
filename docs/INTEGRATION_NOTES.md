# Notes for T6 (integration) — collected by the lead during the build

## Wiring (from T3)
- `vocab = Vocab(MODEL_DIR); rec = Recognizer(...); vocab.attach(rec.check_words)` — Vocab needs the
  recognizer's word checker (added `Vocab.attach`, `Vocab.check(words)`, `cache_file=` kwarg).
- `Recognizer.start()` loads the model on the recognizer thread (non-blocking); wait on `Recognizer.ready` if needed.
- Extras: `SapiSpeaker(config, audio_output="memory", voice_factory=...)`, `.close()`, `.set_volume()`;
  `AudioCapture.close()`.
- The recognizer reads `grammar.registry.free_triggers()` / `registry.commands()`; `Pattern.free_slot == "app"`
  prefixes (open/launch/start/close/switch) only trigger a free re-decode when the tail has [unk].

## From T4
- `ctx.ui("hide")` is posted by "close window" when Xyrus's own window is in front (T5 handles "hide").
- `SystemActions.run_command(cmdline)` requested from T1 for routine `run` steps.
- "clear the clipboard" = `clip_set("")`.
- `winutil.force_foreground` no longer taps ALT when the window is already in front (ALT put Win11 Notepad
  into access-key mode).
- Live smoke tests: no key/typing/click injection unless `XYRUS_LIVE_INPUT=1` (user OK required) — an
  earlier run leaked keystrokes into the user's browser.

## Carry over from v1 (lead's work after the spec was written)
- Songs: autoplay handling — `actions.ensure_playing(video_title, trust_state)`, `browser_playing()`,
  pause-other-media-first in `_play_song`; `find_song()` returns (spoken, url, video_title). Only the song's
  own browser process counts, only the window whose title matches the video is ever touched, and nothing is
  pressed unless that window is in the foreground right before the key/click.
- Meaning layer: `understand()` in v1 arc.py + `test_meaning.py` (~150 paraphrases). Exact volume via pycaw
  (`AudioUtilities.GetSpeakers().EndpointVolume` works here), brightness via screen_brightness_control
  (LG ULTRAGEAR, DDC/CI works).
- Calendar: v1 `data/calendar.json` must open unchanged; v1 calendar phrases keep working (test_calendar.py).
- `is_song_request` treats "play the next song / play the previous one / play again" as track controls.

## From T4's final report
- `commands/__init__.load_all`: remove the temporary skip for T2's modules (calendar/assistant/timers) - make it strict.
- At startup: `custom.install(registry, config, engine, vocab)` and `screenshot.enable_dpi_awareness()` once.
- Construct `WinActions(config=...)` so app aliases resolve to the right process.
- URLs open via `os.startfile` (cmd `start` splits URLs at "&").
- Volume is pycaw-only and replies with the new level (no media-key fallback).
- Still to port into v2 (T4 finished before these existed): v1 `ensure_playing` / `browser_playing` / 3-tuple
  `find_song` into `actions/web.py`, and v1 `understand()` tables into T1's canonical layer - T6 must check both.
- Live smoke (XYRUS_LIVE=1): 38 OK; key/focus/snap covered with patched Win32 calls; Notepad typing test skipped
  (needs XYRUS_LIVE_INPUT=1 and the user's OK).

## Free-decode retry hook (T3 <-> T1)
- Wire in app.py: `engine.request_free_decode = recognizer.request_free_decode`.
- `Recognizer.request_free_decode(tr, cb) -> bool`: finds the utterance by (tr.t_start, tr.t_end); PCM is kept for
  the last 4 mic utterances; False if gone or typed; returns immediately, cb(text) is called from T-rec (lowercase,
  "" on failure); inline/synchronous when T-rec isn't running (tests, --wav). The engine posts the retry itself.
- Grammar reads normalize.YES_WORDS/NO_WORDS/CANCEL_WORDS, parsing.SLOT_TYPES words, dates.DATE_WORDS/TIME_WORDS,
  and normalize.SYNONYMS (mapping) or normalize.SYNONYM_WORDS (iterable) for the meaning layer's words.

## From T3's final report (75 tests OK, 1 skip)
- Wiring: `vocab.attach(recognizer.check_words)`; `engine.request_free_decode = recognizer.request_free_decode`;
  engine pause -> `capture.stop()` / `capture.start()`; capture `on_status` changes -> toast notifier;
  shutdown -> `speaker.close()`, `capture.close()`.
- Recognition: 66/69 direct, 69/69 counting the free-decode retry. KNOWN ISSUE to fix before switching over:
  "switch to chrome" -> retry hears "switch to crow" (right command, wrong app). For app-slot retries
  (open/close/switch <app>), re-decode the tail with a grammar restricted to app names (config apps +
  StartMenuIndex names + [unk]) and fuzzy-match, instead of the unrestricted recognizer.
- Rerun tests.test_recognition after T1's synonym export (normalize.SYNONYMS / SYNONYM_WORDS), T1's to/two and
  dropped-article fixes, and T2's dates.DATE_WORDS / TIME_WORDS land (the synonym test currently skips).

## From T2's final report (125 tests OK; 57/57 dates)
- Opens v1 data/calendar.json unchanged (tested on a temp copy + v1-written file; v1 reads v2 output fine).
  v2 adds bookkeeping keys to calendar.json (acked, snoozed, repeated, last_alert, pruned) - v1 ignores them.
  The user IS using the calendar (added a note from the v1 tab at 22:04 on Sep 13) - never touch/replace the real file.
- Accepted behaviour changes vs v1: "undo" -> "undo that" (bare "undo" still works wake-free for 15 s after a
  voice add, otherwise Ctrl+Z); delete confirms first; "what's coming up" = next 7 days ("what's next" = next item);
  morning brief at 08:30 (config) instead of 08:00; reminders saved as timed to-dos (v1 behaviour/wording).
- Engine wiring left for T1 (sent): due_alerts(store, now, cfg=config); due_repeats + repeat_text for the 2-min
  unacked repeat; store.prune(today) at startup/daily; startup greeting before the auto brief; ctx.payload lost
  in follow-up handlers (T2 falls back to reminders.last_alert()).
- parsedatetime is NOT installed: install it (requirements) so parse_typed's quick-add fallback works.
- E10b ("stop" during shutdown countdown ignored) - engine fix requested from T1; must pass before switching over.

## From T5's final report (32 tests OK; calendar tab 76/76; Commands tab 142 commands / 501 phrasings)
- Single instance: call `ShowEvent()` right after `acquire()`; a second instance does acquire() -> False ->
  signal_existing() -> exit 0. v1 holds "Local\XyrusVoiceAssistant" until v1 is stopped at switch-over.
- Hotkeys: `show_window` -> `ui_queue.put("show")`; others -> `engine.submit_hotkey`.
- Tray Quit puts "quit" on ui_queue; the window destroys itself -> do cleanup after mainloop() returns;
  `App.quit()` must be thread-safe. The window poll already updates tray state + tooltip.
- Settings restarts the mic after a device change (unless paused) - app.py must not do it again.
- `vocab.attach(recognizer.check_words)` needed for name validation.
- Expose `app.app_index` (Start Menu index) so the Apps tab can list it (not in the §9.1 App surface).
- Confirm dialogues should set an `action` slot (e.g. "shutdown") so the header reads "Confirm shutdown?" (T1/T4).
- New config key: window.tray_hint_shown. Autostart writes the shortcut via COM.
- §7.7's FindWindowW(None, "Xyrus") finds the live v1 window while v1 runs - stop v1 first or match by PID.

## Lead's review of T5 screenshots (tests/_out/)
- Home tab Timers card reads "5 minute · 05:00" - pluralise ("5 minutes") via persona.speak_duration or the
  timer's display name. Everything else in tab_Home / banner_home / tab_Settings looked right.

## From T1's final report (171 tests in T1 suites; E1-E26 green on the real registry; recognition 69/69)
- app.py order: `log.setup()` first; run `engine.run_forever(stop)` on the engine thread; wire
  `engine.request_free_decode`, `engine.on_pause` (mic stop/start) and `engine.set_status`; call
  `engine.startup()` (morning brief waits for the greeting); `executor.stop()` on quit.
- Meaning layer: canonical second pass after wake / inside a window; synonyms exported as normalize.SYNONYM_WORDS
  (all in the model vocab); destructive commands never accept unknown objects.
- Deviations accepted: times spoken "3 PM" (calendar summaries keep v1 "3 pm"); "remind me in N minutes" stays a
  reminder; "kill <app>" = force close; dialogue give-up ends immediately.
- Known suite issues at hand-over: T4 TestForegroundPatched fails only in the full run; T5 six-tab screenshot test
  flaky (wrong tab selected) and calendar-tab check failed once in the full run; app-name retry mishears ("crow").

## User request (Sep 13, late): Light / Dark mode for the app
- T5: ui/theme.py colours as (light, dark) tuples; Settings "Appearance" Dark / Light / Follow Windows ->
  config `ui.appearance` ("dark" default | "light" | "system"), applied live; ui_queue "theme:dark|light|system".
- T4: bare "dark mode" / "light mode" / "white mode" / "night mode" switch the Xyrus window; the Windows-wide
  theme is "windows dark mode" / "windows light mode". Verify both after integration (screenshots in light mode).
- v1 is NOT getting this (it's replaced at switch-over).

## From T3 round 2 (79 tests, 0 skips; command set 69/69 direct)
- app.py MUST pass the Start Menu index: `recognizer.set_app_index(actions.apps.default_index())` (or `app_index=`
  at construction) - not automatic (the Start Menu walk is slow). Without it the app grammar only knows config apps.
- App-slot re-decode uses a grammar of trigger words + app names (config apps + Start Menu), fuzzy-matched, then
  unrestricted only if nothing matches; the engine's retry hook tries the app grammar first ("crow" fixed).

## From T4 round 2 (174 tests OK, 1 skip = key-injection test behind XYRUS_LIVE_INPUT)
- Song autoplay ported (actions/web.py + commands/web.py); interfaces.SystemActions gained browser_playing /
  ensure_playing (lead added them + FakeActions defaults). Light/dark voice commands in commands/display.py
  (app_dark / app_light / app_follow_windows -> ctx.ui("theme:..."); windows_dark / windows_light -> set_theme).
- tests/test_paraphrases.py: all ~150 v1 meaning phrasings through make_test_engine - no real failures left.
  Accepted v2 differences: brightness step 15 %; "stop the music" = media stop; "go to youtube" = switch_to;
  un-mute is silent; "shut down chrome" = close chrome (never shutdown); half-heard "shut down [unk]" gets the
  D11 "Did you say ...?" confirm.
- Registry gap (T1 file, not fixed): a 1-word pattern tolerates unknown extra words ("lock the door" matched
  "lock"). T4 guards lock/sleep/hibernate in commands/power.py (leftover non-PC/screen words -> NOT_HEARD).
  Optional general fix: reject non-follow-up matches of 1-literal patterns when canonical extra words aren't fillers.

## MUST FIX before switch-over (lead)
- commands/web.py: `ensure_playing` runs inside ctx.do on the executor and can block it ~25 s, so every command
  said right after "play <song>" queues behind it (executor timeout 30 s). Run the autoplay check on its own daemon
  thread (as v1 does) and post its "blocked" reply back through the engine; the executor must be free as soon as the
  URL is opened. Add a test: "play alone" then "volume up" -> the volume command completes without waiting.
- RESOLVED by the lead: the autoplay check now runs on its own COM-initialised daemon thread in the app
  (commands/web.py `_run_check` / `_thread_spawn`; inline only when the engine isn't threaded, i.e. headless tests)
  and posts its reply via engine.post. tests/test_autoplay_threads.py proves "play" returns at once and
  "volume up" isn't held up (113 tests OK with test_songs + test_commands).
- tests/test_ui_render.py (T5): module-level `winutil.force_foreground = lambda ...` leak -> mock.patch (sent to T5).

## Whisper hearing for free text (user decision, Sep 14 2026) - overrides SPEC non-goal "No Whisper"
- User asked for "perfect hearing" after "play Ishwar by Vikings" was heard as "play issue war" (Vosk small model
  is English-only). User approved faster-whisper + Systran/faster-whisper-small (~480 MB) at D:/arc/models/whisper-small.
- Lead owns D:/arc/hearing.py: load_async(), available(), transcribe(pcm16k: bytes) -> str (Whisper, int8 CPU,
  language en, name-style prompt), falls back to "" when not installed/ready.
- Use it ONLY for free text (song titles, "what should I play?" answers, search queries, calendar titles/notes);
  wake word + commands stay on Vosk (fast). v2: make the recognizer's free-decode pluggable
  (e.g. Recognizer.set_free_decoder(fn)) and prefer hearing.transcribe for free slots when available.
- ctranslate2.dll needs msvcp140.dll (VC++ runtime not installed on this PC) - being resolved with the user.
- Installer: requirements + model download (~480 MB) on new PCs; README must list it.

- Whisper benchmark (synthesized requests): Whisper small (en, int8) 10/10 names right in ~1.1-1.4 s vs Vosk 4/10.
  language="en" = same words as auto-detect at half the time. PROMPT is neutral (names only "Shape of You");
  a prompt naming the user's songs steered "is war by viking" -> "Ishwar" (rejected). YouTube resolves Whisper's
  phonetic spellings (Ishvar -> Ishwar, Tom Hiho -> Tum Hi Ho, Kasariya/Arujit -> Kesariya). Model warm-up in _load.

## Command capture + Bangla song names (Sep 14 2026, after "the voice command is absolute shit")
- After the wake reply the recognizer records ONE utterance with xyrus/capture.py Endpointer (no limit to start,
  ends 0.9 s after the user stops, 0.3 s pre-roll, 30 s cap) and Whisper hears it (hearing.listen: one encoding
  shared by the English decode, detect_language and the Bengali decode). Vosk keeps the wake word + one-breath
  commands; Whisper is the one-breath retry. Question answers (confirm/when/free) use the same path.
- Wiring (app.py): recognizer.set_command_hearing(CommandHearing(hearing)) in plug_hearing; engine.capture_available
  / on_capture_handled / request_language_check; recognizer.on_capture_state / song_question / ack_required.
- command_window_s 0 = no limit (default); without Whisper the Vosk fallback keeps a 60 s window.
- Bangla: xyrus/banglish.py (Bengali script -> Banglish, carrier words); a song request is language-checked and,
  if Bangla/Hindi, decoded in Bengali; find_song searches the Bengali title, then the Banglish.
